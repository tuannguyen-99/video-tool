from __future__ import annotations

import asyncio
import os
import uuid
from typing import Callable, Dict, List, Optional

from .models import JobState, JobStatus, StageName
from .pipeline import downloader, mux, srt_utils, transcribe, translate, tts
from .storage import default_output_dir, new_job_workdir

Listener = Callable[[JobState], None]


class JobManager:
    def __init__(self) -> None:
        self.jobs: Dict[str, JobState] = {}
        self._listeners: List[Listener] = []

        # Jobs run one at a time, in submission order: a single background
        # worker consumes this queue and fully awaits each job (download ->
        # ... -> export) before starting the next one.
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._job_args: Dict[str, tuple] = {}

        # The job currently being executed (if any), so a cancel request can
        # reach the right asyncio.Task.
        self._current_job_id: Optional[str] = None
        self._current_task: Optional[asyncio.Task] = None

        # Jobs cancelled while still waiting in the queue (never started).
        self._cancelled_before_start: set[str] = set()

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, job: JobState) -> None:
        for fn in list(self._listeners):
            try:
                fn(job)
            except Exception:
                pass

    def _update(self, job_id: str, **kwargs) -> None:
        job = self.jobs[job_id]
        for k, v in kwargs.items():
            setattr(job, k, v)
        self._emit(job)

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()

            if job_id in self._cancelled_before_start:
                self._cancelled_before_start.discard(job_id)
                self._job_args.pop(job_id, None)
                continue

            args = self._job_args.pop(job_id, None)
            if args is None:
                continue

            self._current_job_id = job_id
            task = asyncio.create_task(self._run_job(job_id, *args))
            self._current_task = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._current_job_id = None
                self._current_task = None

    def create_batch(
        self,
        urls: List[str],
        cookie_path: str,
        output_dir: Optional[str],
        music_path: Optional[str],
        source_lang: str,
        target_lang: str,
        burn_subtitles: bool,
        mix_music_volume: float,
    ) -> List[str]:
        job_ids = []
        for url in urls:
            job_id = uuid.uuid4().hex[:12]
            self.jobs[job_id] = JobState(id=job_id, url=url)
            self._job_args[job_id] = (
                url, cookie_path, output_dir, music_path,
                source_lang, target_lang, burn_subtitles, mix_music_volume,
            )
            job_ids.append(job_id)
            self._queue.put_nowait(job_id)

        self._ensure_worker()
        return job_ids

    def cancel_job(self, job_id: str) -> bool:
        """Cancels a single job. Returns False if the job doesn't exist or
        has already finished (success/failed/cancelled)."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED):
            return False

        if job_id == self._current_job_id and self._current_task is not None:
            # Currently running: cancel the task. _run_job's CancelledError
            # handler is responsible for updating status to CANCELLED and
            # killing any subprocess it started.
            self._current_task.cancel()
        else:
            # Still waiting in the queue: mark it cancelled immediately so
            # the worker skips it when its turn comes up.
            self._cancelled_before_start.add(job_id)
            self._job_args.pop(job_id, None)
            self._update(job_id, status=JobStatus.CANCELLED, error="Đã hủy bởi người dùng")
        return True

    def cancel_all(self) -> List[str]:
        """Cancels every job that isn't already finished. Returns the ids
        that were cancelled."""
        cancelled = []
        for job_id, job in list(self.jobs.items()):
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                if self.cancel_job(job_id):
                    cancelled.append(job_id)
        return cancelled

    async def _run_job(
        self,
        job_id: str,
        url: str,
        cookie_path: str,
        output_dir: Optional[str],
        music_path: Optional[str],
        source_lang: str,
        target_lang: str,
        burn_subtitles: bool,
        mix_music_volume: float,
    ) -> None:
        work_dir = new_job_workdir(job_id)
        resolved_output_dir = output_dir.strip() if output_dir and output_dir.strip() else default_output_dir(job_id)
        self._update(job_id, status=JobStatus.RUNNING, stage=StageName.DOWNLOAD)

        try:
            try:
                video_path = await downloader.download_video(url, cookie_path, work_dir)
            except Exception as e:
                self._fail(job_id, StageName.DOWNLOAD, e)
                return

            try:
                self._update(job_id, stage=StageName.SPEECH_TO_TEXT)
                audio_path = os.path.join(work_dir, "audio.wav")
                await mux.extract_audio(video_path, audio_path)
                segments = await transcribe.transcribe(audio_path, source_lang)
            except Exception as e:
                self._fail(job_id, StageName.SPEECH_TO_TEXT, e)
                return

            try:
                self._update(job_id, stage=StageName.TRANSLATE)
                vi_segments = await translate.translate_segments(segments, source_lang, target_lang)
            except Exception as e:
                self._fail(job_id, StageName.TRANSLATE, e)
                return

            try:
                self._update(job_id, stage=StageName.TEXT_TO_SPEECH)
                clip_paths = []
                for i, seg in enumerate(vi_segments):
                    clip_path = os.path.join(work_dir, f"tts_{i:04d}.wav")
                    await tts.synthesize(seg.text, clip_path)
                    clip_paths.append(clip_path)

                total_duration = await mux.probe_duration(video_path)
                tts_track_path = os.path.join(work_dir, "tts_track.wav")
                await mux.build_tts_track(clip_paths, vi_segments, total_duration, tts_track_path)
            except Exception as e:
                self._fail(job_id, StageName.TEXT_TO_SPEECH, e)
                return

            try:
                self._update(job_id, stage=StageName.EXPORT)
                srt_path = os.path.join(work_dir, "vietsub.srt")
                srt_utils.write_srt(vi_segments, srt_path)

                os.makedirs(resolved_output_dir, exist_ok=True)
                out_filename = f"{os.path.splitext(os.path.basename(video_path))[0]}_vietsub.mp4"
                out_path = os.path.join(resolved_output_dir, out_filename)

                await mux.export_final(
                    video_path=video_path,
                    tts_track_path=tts_track_path,
                    out_path=out_path,
                    music_path=music_path,
                    srt_path=srt_path,
                    music_volume=mix_music_volume,
                    burn_subtitles=burn_subtitles,
                )
            except Exception as e:
                self._fail(job_id, StageName.EXPORT, e)
                return

            self._update(
                job_id,
                status=JobStatus.SUCCESS,
                stage=StageName.DONE,
                output_filename=out_filename,
                output_path=out_path,
                progress=1.0,
            )
        except asyncio.CancelledError:
            # Exception (not CancelledError) is caught by the per-stage
            # try/except blocks above, so reaching here means a cancel was
            # requested mid-stage. downloader/mux are responsible for
            # killing their own subprocess before this propagates.
            self._update(job_id, status=JobStatus.CANCELLED, error="Đã hủy bởi người dùng")
            raise

    def _fail(self, job_id: str, stage: StageName, error: Exception) -> None:
        self._update(
            job_id,
            status=JobStatus.FAILED,
            failed_stage=stage,
            error=str(error)[:500],
        )


manager = JobManager()