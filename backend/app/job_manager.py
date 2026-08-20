from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from typing import Callable, Dict, List, Optional

from .models import JobState, JobStatus, OutputFile, Platform, StageName, VoiceGender
from .pipeline import downloader, gender_detect, mux, srt_utils, transcribe, translate, tts
from .storage import default_output_dir, new_job_workdir

logger = logging.getLogger(__name__)

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

        # _job_args gets popped by the worker once a job starts, so a copy
        # is kept here for the job's whole lifetime — retry_job() needs the
        # original request (urls/cookie/options) again to re-submit it.
        self._job_original_args: Dict[str, tuple] = {}

        # Outputs already produced by earlier stages of a job, keyed by
        # job_id (e.g. {"video_path": ..., "segments": [...], ...}).
        # Populated incrementally as _run_job succeeds past each stage.
        # On retry, _run_job checks this cache before redoing a stage — so
        # retrying a job that failed at "export" doesn't re-download,
        # re-transcribe, re-translate, or re-synthesize speech, it just
        # reruns the export step with what's already on disk. Cleared once
        # the job succeeds or is deleted.
        self._job_cache: Dict[str, dict] = {}

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
        cookie_path: Optional[str],
        output_dir: Optional[str],
        music_path: Optional[str],
        source_lang: str,
        target_lang: str,
        burn_subtitles: bool,
        mix_music_volume: float,
        resolution: Optional[str] = None,
        platform: Platform = Platform.DOUYIN,
        split_long_video: bool = False,
        split_video_minutes: float = 10.0,
        match_voice_gender: bool = False,
        male_ref_path: Optional[str] = None,
        female_ref_path: Optional[str] = None,
        single_ref_path: Optional[str] = None,
        local_video_paths: Optional[List[str]] = None,
        watermark_text: Optional[str] = None,
    ) -> List[str]:
        job_ids = []

        def _spawn(display_label: str, job_platform: Platform, local_video_path: Optional[str]) -> None:
            job_id = uuid.uuid4().hex[:12]
            self.jobs[job_id] = JobState(
                id=job_id,
                url=display_label,
                platform=job_platform,
                split_long_video=split_long_video,
                match_voice_gender=match_voice_gender,
            )
            args = (
                display_label, cookie_path, output_dir, music_path,
                source_lang, target_lang, burn_subtitles, mix_music_volume,
                resolution, job_platform, split_long_video, split_video_minutes,
                match_voice_gender, male_ref_path, female_ref_path, single_ref_path,
                local_video_path, watermark_text,
            )
            self._job_args[job_id] = args
            self._job_original_args[job_id] = args
            job_ids.append(job_id)
            self._queue.put_nowait(job_id)

        for url in urls:
            _spawn(url, platform, None)

        # Pre-downloaded local uploads: no URL/cookie needed, download stage
        # is skipped entirely (see _run_job). `url` field on the job is set
        # to the original filename purely for display in the job list.
        for local_path in local_video_paths or []:
            _spawn(os.path.basename(local_path), Platform.LOCAL, local_path)

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

    def retry_job(self, job_id: str) -> bool:
        """Re-queues a job that FAILED. Reuses the original request args and
        whatever intermediate results earlier stages already produced (see
        self._job_cache) — so a job that failed at, say, export doesn't
        redo download/transcribe/translate/tts, it picks up right where it
        left off. Returns False if the job doesn't exist or isn't currently
        FAILED (e.g. still running, or already succeeded/cancelled)."""
        job = self.jobs.get(job_id)
        if job is None or job.status != JobStatus.FAILED:
            return False

        args = self._job_original_args.get(job_id)
        if args is None:
            return False

        self._job_args[job_id] = args
        self._update(
            job_id,
            status=JobStatus.PENDING,
            stage=job.failed_stage or StageName.QUEUED,
            failed_stage=None,
            error=None,
        )
        self._queue.put_nowait(job_id)
        self._ensure_worker()
        return True

    def delete_job(self, job_id: str) -> bool:
        """Removes a finished job (success/failed/cancelled) from memory.
        Returns False if the job doesn't exist or is still pending/running —
        callers must cancel a job before it can be deleted, since ripping
        its entry out of self.jobs while _run_job is mid-flight would make
        that task's next self._update(...) call KeyError."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
            return False
        del self.jobs[job_id]
        self._job_args.pop(job_id, None)
        self._job_original_args.pop(job_id, None)
        self._job_cache.pop(job_id, None)
        self._cancelled_before_start.discard(job_id)
        return True

    def delete_all_finished(self) -> List[str]:
        """Deletes every job that's already finished (success/failed/
        cancelled). Returns the ids that were deleted."""
        deleted = []
        for job_id, job in list(self.jobs.items()):
            if job.status in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED):
                if self.delete_job(job_id):
                    deleted.append(job_id)
        return deleted

    async def _run_job(
        self,
        job_id: str,
        url: str,
        cookie_path: Optional[str],
        output_dir: Optional[str],
        music_path: Optional[str],
        source_lang: str,
        target_lang: str,
        burn_subtitles: bool,
        mix_music_volume: float,
        resolution: Optional[str] = None,
        platform: Platform = Platform.DOUYIN,
        split_long_video: bool = False,
        split_video_minutes: float = 10.0,
        match_voice_gender: bool = False,
        male_ref_path: Optional[str] = None,
        female_ref_path: Optional[str] = None,
        single_ref_path: Optional[str] = None,
        local_video_path: Optional[str] = None,
        watermark_text: Optional[str] = None,
    ) -> None:
        # new_job_workdir(job_id) must resolve to the SAME directory every
        # time for the same job_id (and must not wipe it), since a retry's
        # whole point is finding the files a previous attempt already left
        # behind here (video, audio.wav, tts_XXXX.wav, tts_track.wav, ...).
        work_dir = new_job_workdir(job_id)
        resolved_output_dir = output_dir.strip() if output_dir and output_dir.strip() else default_output_dir(job_id)
        self._update(job_id, status=JobStatus.RUNNING, stage=StageName.DOWNLOAD)

        # Results from a previous (failed) attempt at this same job_id, if
        # any. A fresh job_id always starts with an empty cache, so this
        # code path behaves identically whether it's a first run or a retry
        # — each stage below just happens to find nothing to reuse.
        cache = self._job_cache.setdefault(job_id, {})

        # Deterministic path regardless of which attempt wrote it, so a
        # retry that skips re-running speech_to_text (segments already
        # cached) can still find the audio for gender detection below.
        audio_path = os.path.join(work_dir, "audio.wav")

        try:
            video_path = cache.get("video_path")
            if not video_path or not os.path.exists(video_path):
                if local_video_path:
                    # Pre-downloaded upload: nothing to fetch, just confirm
                    # the file main.py saved is still there. No title
                    # metadata is available for a local upload (no platform
                    # to query it from), so title/title_vi stay unset — the
                    # UI falls back to showing the filename (job.url) as-is.
                    if not os.path.exists(local_video_path):
                        self._fail(
                            job_id, StageName.DOWNLOAD,
                            FileNotFoundError(f"File video đã tải lên không còn tồn tại: {local_video_path}"),
                        )
                        return
                    video_path = local_video_path
                    cache["video_path"] = video_path
                    cache["title"] = None
                else:
                    try:
                        result = await downloader.download_video(url, cookie_path, work_dir, platform=platform)
                        video_path = result.video_path
                        cache["video_path"] = video_path
                        cache["title"] = result.title
                    except Exception as e:
                        self._fail(job_id, StageName.DOWNLOAD, e)
                        return

            # Best-effort: translate the source title to Vietnamese for
            # display in the job list while later stages run. Never fails
            # the job — a title is a nice-to-have, not a requirement, and
            # this reuses the same Google->MyMemory fallback chain as
            # subtitle translation. Cached so a retry doesn't re-translate.
            title = cache.get("title")
            if title and "title_vi" not in cache:
                try:
                    cache["title_vi"] = await translate.translate_text(title, source_lang, target_lang)
                except Exception:
                    cache["title_vi"] = None
            if title or cache.get("title_vi"):
                self._update(job_id, title=title, title_vi=cache.get("title_vi"))

            segments = cache.get("segments")
            if segments is None:
                try:
                    self._update(job_id, stage=StageName.SPEECH_TO_TEXT)
                    await mux.extract_audio(video_path, audio_path)
                    segments = await transcribe.transcribe(audio_path, source_lang)
                    cache["segments"] = segments
                except Exception as e:
                    self._fail(job_id, StageName.SPEECH_TO_TEXT, e)
                    return

            vi_segments = cache.get("vi_segments")
            if vi_segments is None:
                try:
                    self._update(job_id, stage=StageName.TRANSLATE)
                    vi_segments = await translate.translate_segments(segments, source_lang, target_lang)
                    cache["vi_segments"] = vi_segments
                except Exception as e:
                    self._fail(job_id, StageName.TRANSLATE, e)
                    return

            tts_track_path = cache.get("tts_track_path")
            total_duration = cache.get("total_duration")
            if not tts_track_path or not os.path.exists(tts_track_path) or total_duration is None:
                try:
                    self._update(job_id, stage=StageName.TEXT_TO_SPEECH)

                    # Optional: guess each line's speaker gender from pitch
                    # and pick a matching TTS voice preset per segment,
                    # instead of always using one fixed voice for the whole
                    # video. This naturally covers both a single narrator
                    # (segments mostly agree on one gender) and a two-person
                    # dialogue (segments alternate between genders) — it's
                    # still just pitch classification, not real speaker
                    # diarization, so two same-gender speakers can't be told
                    # apart. Cached so a retry doesn't redo the analysis.
                    # Never fails the job — gender_detect swallows its own
                    # errors and falls back to an all-None list on trouble,
                    # which just means every segment uses the default voice.
                    segment_voices: List[Optional[str]] = [None] * len(vi_segments)
                    # Per-segment cloned reference clip (uploaded for this
                    # batch), gender-matched the same way as segment_voices.
                    # Stays all-None (falls back to segment_voices / server
                    # defaults inside tts.py) unless the user uploaded at
                    # least one of male_ref_path/female_ref_path for this
                    # batch — see tts.resolve_ref_audio_for_gender().
                    segment_ref_audio: List[Optional[str]] = [None] * len(vi_segments)
                    if match_voice_gender:
                        if "segment_genders" not in cache:
                            if os.path.exists(audio_path):
                                cache["segment_genders"] = await gender_detect.detect_gender_per_segment(
                                    audio_path, segments
                                )
                            else:
                                cache["segment_genders"] = [None] * len(segments)
                        # gender_detect.py returns plain "male"/"female" strings
                        # (its Gender = Literal["male","female"], not the
                        # pydantic VoiceGender enum used on JobState) — keep
                        # that type here too, and only convert to VoiceGender
                        # right at the JobState boundary below. Passing a raw
                        # str into a field typed Optional[VoiceGender] doesn't
                        # get validated/coerced on plain attribute assignment,
                        # so it silently stores the wrong type and only
                        # surfaces later as a pydantic serializer warning
                        # ("Expected `enum` but got `str`") when the job is
                        # serialized for the websocket.
                        segment_genders: List[Optional[str]] = cache["segment_genders"]

                        segment_voices = [
                            tts.resolve_voice_for_gender(g) for g in segment_genders
                        ]
                        segment_ref_audio = [
                            tts.resolve_ref_audio_for_gender(g, male_ref_path, female_ref_path)
                            for g in segment_genders
                        ]

                        # Surface the dominant detected gender on the job for
                        # display (e.g. a "Giọng: Nam/Nữ" badge) — the most
                        # common non-None value across segments, or None if
                        # detection was inconclusive throughout.
                        confident = [g for g in segment_genders if g]
                        if confident:
                            dominant = max(set(confident), key=confident.count)
                            self._update(job_id, detected_voice_gender=VoiceGender(dominant))
                    elif single_ref_path:
                        # match_voice_gender is off: no per-speaker detection,
                        # so if the user uploaded one generic voice-clone
                        # reference for the whole video, apply it uniformly
                        # to every segment instead of leaving ref_audio
                        # unused (which would silently fall back to a
                        # preset/default voice — not what was uploaded for).
                        segment_ref_audio = [single_ref_path] * len(vi_segments)

                    clip_paths = []
                    for i, seg in enumerate(vi_segments):
                        clip_path = os.path.join(work_dir, f"tts_{i:04d}.wav")
                        # A previous attempt may have already synthesized this
                        # exact clip (filename is deterministic by index)
                        # before failing later on — e.g. the "Too many open
                        # files" case failed in build_tts_track, well after
                        # every tts_XXXX.wav was already written. Skip
                        # redoing the (slower) TTS call for those.
                        if not os.path.exists(clip_path):
                            voice = segment_voices[i] if i < len(segment_voices) else None
                            ref_audio = segment_ref_audio[i] if i < len(segment_ref_audio) else None
                            await tts.synthesize(seg.text, clip_path, voice=voice, ref_audio=ref_audio)
                        clip_paths.append(clip_path)

                    total_duration = await mux.probe_duration(video_path)
                    tts_track_path = os.path.join(work_dir, "tts_track.wav")
                    await mux.build_tts_track(clip_paths, vi_segments, total_duration, tts_track_path)
                    cache["tts_track_path"] = tts_track_path
                    cache["total_duration"] = total_duration
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
                    resolution=resolution,
                    watermark_text=watermark_text,
                )

                # Optional: cut the finished export into parts if it runs
                # longer than split_video_minutes. Kept separate from
                # export_final itself so a failure here can't corrupt the
                # already-good single-file export.
                output_files: List[OutputFile] = []
                if split_long_video:
                    split_seconds = max(int(split_video_minutes * 60), 1)
                    final_duration = await mux.probe_duration(out_path)
                    if final_duration > split_seconds:
                        parts_dir = os.path.join(resolved_output_dir, f"{os.path.splitext(out_filename)[0]}_parts")
                        base_name = os.path.splitext(out_filename)[0]
                        part_paths = await mux.split_into_segments(
                            out_path, parts_dir, segment_seconds=split_seconds, base_name=base_name
                        )
                        output_files = [
                            OutputFile(filename=os.path.basename(p), path=p) for p in part_paths
                        ]

                if not output_files:
                    output_files = [OutputFile(filename=out_filename, path=out_path)]
            except Exception as e:
                self._fail(job_id, StageName.EXPORT, e)
                return

            self._update(
                job_id,
                status=JobStatus.SUCCESS,
                stage=StageName.DONE,
                output_files=output_files,
                output_filename=output_files[0].filename,
                output_path=output_files[0].path,
                progress=1.0,
            )
            # Job finished for good — nothing left to resume, drop the cache.
            self._job_cache.pop(job_id, None)
        except asyncio.CancelledError:
            # Exception (not CancelledError) is caught by the per-stage
            # try/except blocks above, so reaching here means a cancel was
            # requested mid-stage. downloader/mux are responsible for
            # killing their own subprocess before this propagates. The
            # cache is intentionally left in place: cancelling isn't
            # failing, but if the user retries later there's no harm in
            # reusing whatever had already completed.
            self._update(job_id, status=JobStatus.CANCELLED, error="Đã hủy bởi người dùng")
            raise

    def _fail(self, job_id: str, stage: StageName, error: Exception) -> None:
        # The UI only ever shows a short, single-line summary (job.error,
        # capped below) — nowhere near enough to debug a deep import-chain
        # error (e.g. a broken transitive dependency several frames down).
        # Full traceback goes to the server's own log/console instead;
        # that's where to look when the UI message alone isn't enough.
        # logging.error() alone doesn't include the traceback — must pass
        # exc_info explicitly since `error` here is a caught exception
        # object being handled out-of-band, not the live one on the stack.
        logger.error(
            "Job %s thất bại ở bước %s",
            job_id,
            getattr(stage, "value", stage),
            exc_info=(type(error), error, error.__traceback__),
        )
        self._update(
            job_id,
            status=JobStatus.FAILED,
            failed_stage=stage,
            error=str(error)[:1000],
        )


manager = JobManager()