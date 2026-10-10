from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from . import cookie_refs, deps_check, voice_refs
from .job_manager import manager
from .models import BatchCreateResponse, JobStatus, Platform
from .storage import save_upload

app = FastAPI(title="Douyin Vietsub Tool API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local tool; tighten if you expose this beyond localhost
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/system/dependencies")
async def system_dependencies():
    """Reports install status of every external binary (f2, yt-dlp,
    ffmpeg, ffprobe) and Python package (faster-whisper, deep-translator,
    vieneu) the pipeline relies on — so missing ones show up here instead
    of surfacing as a cryptic error mid-job."""
    return {"items": await deps_check.check_all()}


@app.post("/api/system/dependencies/{name}/install")
async def install_dependency(name: str):
    """Installs a pip-installable dependency (f2, yt-dlp, faster_whisper,
    deep_translator, vieneu, ctranslate2, transformers, sentencepiece) or
    downloads/prepares a model (WHISPER_MODEL_NAME, NLLB_MODEL_NAME),
    streaming progress back as plain text lines in real time.

    SECURITY: `name` must be a key in deps_check.PIP_PACKAGE_BY_NAME or
    deps_check.MODEL_DOWNLOADER_BY_NAME — fixed, server-side whitelists. We
    deliberately do NOT accept a pip spec, repo id, or any install
    arguments from the client; otherwise any caller could make this
    backend run an arbitrary `pip install <anything>` (including
    `--index-url`/git URLs) or fetch an arbitrary Hugging Face repo, which
    is a remote code execution / SSRF vector. Binaries like ffmpeg/ffprobe
    that require system package managers + sudo are not in either
    whitelist on purpose and always return 400 here.
    """
    pip_spec = deps_check.PIP_PACKAGE_BY_NAME.get(name)
    model_downloader = deps_check.MODEL_DOWNLOADER_BY_NAME.get(name)

    if pip_spec is None and model_downloader is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' không thể tự cài qua API này, vui lòng cài thủ công.",
        )

    async def stream():
        try:
            if model_downloader is not None:
                async for line in model_downloader():
                    yield line + "\n"
                yield "\n[OK] Tải model thành công.\n"
            else:
                async for line in deps_check.install_pip_package(pip_spec):
                    yield line + "\n"
                yield "\n[OK] Cài đặt thành công.\n"
        except deps_check.DependencyInstallError as e:
            yield f"\n[LỖI] {e}\n"

    return StreamingResponse(stream(), media_type="text/plain")


@app.get("/api/voice-refs")
async def list_voice_refs(kind: Optional[str] = None):
    """Lists saved voice-clone reference clips, so the UI can offer 'reuse
    a previously saved voice' instead of forcing a fresh upload every
    batch. `kind` optionally filters to 'male', 'female', or 'single'."""
    if kind is not None and kind not in ("male", "female", "single"):
        raise HTTPException(
            status_code=422, detail="kind phải là 'male', 'female' hoặc 'single'"
        )
    return {"items": voice_refs.list_voice_refs(kind)}  # type: ignore[arg-type]


@app.delete("/api/voice-refs/{ref_id}")
async def delete_voice_ref(ref_id: str):
    ok = voice_refs.delete_voice_ref(ref_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy giọng đã lưu")
    return {"ok": True}


@app.get("/api/cookie-refs")
async def list_cookie_refs(platform: Optional[str] = None):
    """Lists saved cookie files, so the UI can offer 'reuse a previously
    saved cookie' instead of forcing a fresh upload every batch.
    `platform` optionally filters to 'douyin' or 'bilibili'."""
    if platform is not None and platform not in ("douyin", "bilibili"):
        raise HTTPException(
            status_code=422, detail="platform phải là 'douyin' hoặc 'bilibili'"
        )
    return {"items": cookie_refs.list_cookie_refs(platform)}  # type: ignore[arg-type]


@app.delete("/api/cookie-refs/{ref_id}")
async def delete_cookie_ref(ref_id: str):
    ok = cookie_refs.delete_cookie_ref(ref_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy cookie đã lưu")
    return {"ok": True}


@app.post("/api/jobs", response_model=BatchCreateResponse)
async def create_jobs(
    urls: str = Form(""),
    platform: str = Form("douyin"),
    output_dir: Optional[str] = Form(None),
    source_lang: str = Form("zh-CN"),
    target_lang: str = Form("vi"),
    burn_subtitles: bool = Form(True),
    mix_music_volume: float = Form(0.15),
    resolution: str = Form("1080p"),
    split_long_video: bool = Form(False),
    split_video_minutes: float = Form(10.0),
    match_voice_gender: bool = Form(False),
    # Erases hard-coded source-language subtitles already burned into the
    # source video's pixels (AI inpainting via VSR) before the new
    # Vietnamese subtitles get burned in. Off by default — slow, only
    # meaningful when the source actually has a hard-sub.
    remove_hardsub: bool = Form(False),
    # Channel-name watermark burned into the top-left corner of the export
    # (see mux.export_final). Empty/omitted = no watermark, unchanged
    # behavior from before this option existed.
    watermark_text: Optional[str] = Form(None),
    cookie_file: Optional[UploadFile] = File(None),
    # Reuse a previously saved cookie (see /api/cookie-refs) instead of
    # uploading a fresh file. Takes priority over cookie_file if both are
    # sent — same convention as the voice ref *_id/*_file pairs below.
    cookie_ref_id: Optional[str] = Form(None),
    # Non-empty = also persist the matching fresh cookie_file permanently
    # under this label (tagged with `platform`), so it shows up in
    # /api/cookie-refs for future batches instead of needing a re-upload.
    save_cookie_label: Optional[str] = Form(None),
    music_file: Optional[UploadFile] = File(None),
    male_voice_ref_file: Optional[UploadFile] = File(None),
    female_voice_ref_file: Optional[UploadFile] = File(None),
    # Single voice-clone reference used for the whole video when
    # match_voice_gender is OFF — the "one voice, not gender-matched"
    # alternative to the male/female pair above.
    voice_ref_file: Optional[UploadFile] = File(None),
    # Reuse a previously saved voice (see /api/voice-refs) instead of
    # uploading a fresh clip. Takes priority over the matching *_file
    # above if both happen to be sent.
    male_voice_ref_id: Optional[str] = Form(None),
    female_voice_ref_id: Optional[str] = Form(None),
    voice_ref_id: Optional[str] = Form(None),
    # Non-empty = also persist the matching fresh upload (*_file, not
    # *_id) permanently under this label, so it shows up in /api/voice-refs
    # for future batches instead of needing to be re-uploaded every time.
    save_male_voice_ref_label: Optional[str] = Form(None),
    save_female_voice_ref_label: Optional[str] = Form(None),
    save_voice_ref_label: Optional[str] = Form(None),
    # Pre-downloaded video files the user already has on their machine —
    # an alternative to (or combined with) `urls`. No cookie/download step
    # needed for these; see JobManager._run_job's local_video_path handling.
    video_files: list[UploadFile] = File(default_factory=list),
):
    if resolution not in ("720p", "1080p"):
        raise HTTPException(status_code=422, detail="resolution phải là '720p' hoặc '1080p'")
    if platform not in ("douyin", "bilibili"):
        raise HTTPException(status_code=422, detail="platform phải là 'douyin' hoặc 'bilibili'")
    if split_long_video and not (0.5 <= split_video_minutes <= 180):
        raise HTTPException(
            status_code=422, detail="split_video_minutes phải trong khoảng 0.5 đến 180 phút"
        )
    if watermark_text and len(watermark_text) > 60:
        raise HTTPException(
            status_code=422, detail="Tên watermark tối đa 60 ký tự"
        )

    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    uploaded_videos = [f for f in video_files if f is not None and f.filename]

    if not url_list and not uploaded_videos:
        raise HTTPException(
            status_code=422,
            detail="Cần ít nhất 1 URL hoặc 1 file video đã tải sẵn trên máy.",
        )

    batch_id = uuid.uuid4().hex[:10]

    # Cookie is only needed to authenticate the URL-based download (f2/
    # yt-dlp) — a purely-local batch (only video_files, no urls) doesn't
    # need one at all. Same two-way resolution as the voice refs below:
    # an existing saved cookie (cookie_ref_id) takes priority over a fresh
    # upload (cookie_file); if a fresh upload also carries a non-empty
    # save_cookie_label, the same bytes get persisted via
    # cookie_refs.save_cookie_ref() so this file shows up in
    # /api/cookie-refs (filtered to this `platform`) for future batches.
    cookie_path: Optional[str] = None
    if cookie_ref_id:
        cookie_path = cookie_refs.get_cookie_ref_path(cookie_ref_id)
        if cookie_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"Không tìm thấy cookie đã lưu (id={cookie_ref_id!r})",
            )
    elif cookie_file is not None and cookie_file.filename:
        cookie_bytes = await cookie_file.read()
        cookie_path = save_upload(batch_id, cookie_file.filename or "cookies.txt", cookie_bytes)
        if save_cookie_label and save_cookie_label.strip():
            cookie_refs.save_cookie_ref(
                platform, save_cookie_label, cookie_file.filename or "cookies.txt", cookie_bytes
            )

    if url_list and cookie_path is None:
        platform_label = "Douyin" if platform == "douyin" else "Bilibili"
        raise HTTPException(
            status_code=422,
            detail=f"Vui lòng chọn file cookie để xác thực với {platform_label}.",
        )

    music_path = None
    if music_file is not None and music_file.filename:
        music_path = save_upload(batch_id, music_file.filename, await music_file.read())

    # Voice-clone reference clips (~3-5s each): each of the 3 slots (male,
    # female, single-for-whole-video) resolves the same way —
    #   1. an existing saved voice (*_id) if given — reused as-is, nothing
    #      new written to disk;
    #   2. else a freshly uploaded file (*_file) — saved into this batch's
    #      own upload folder (ephemeral, same as cookie_file/music_file);
    #      if a non-empty save_*_label was also sent, the SAME bytes are
    #      additionally persisted via voice_refs.save_voice_ref() so this
    #      clip shows up in /api/voice-refs for future batches too;
    #   3. else None — falls back to a preset/default voice downstream.
    async def _resolve_ref(
        ref_id: Optional[str],
        file: Optional[UploadFile],
        kind: voice_refs.VoiceRefKind,
        save_label: Optional[str],
    ) -> Optional[str]:
        if ref_id:
            path = voice_refs.get_voice_ref_path(ref_id)
            if path is None:
                raise HTTPException(
                    status_code=404, detail=f"Không tìm thấy giọng đã lưu (id={ref_id!r})"
                )
            return path
        if file is not None and file.filename:
            data = await file.read()
            path = save_upload(batch_id, file.filename, data)
            if save_label and save_label.strip():
                voice_refs.save_voice_ref(kind, save_label, file.filename, data)
            return path
        return None

    male_ref_path = await _resolve_ref(
        male_voice_ref_id, male_voice_ref_file, "male", save_male_voice_ref_label
    )
    female_ref_path = await _resolve_ref(
        female_voice_ref_id, female_voice_ref_file, "female", save_female_voice_ref_label
    )
    single_ref_path = await _resolve_ref(
        voice_ref_id, voice_ref_file, "single", save_voice_ref_label
    )

    local_video_paths = [
        save_upload(batch_id, f.filename, await f.read()) for f in uploaded_videos
    ]

    job_ids = manager.create_batch(
        urls=url_list,
        cookie_path=cookie_path,
        output_dir=output_dir,
        music_path=music_path,
        source_lang=source_lang,
        target_lang=target_lang,
        burn_subtitles=burn_subtitles,
        mix_music_volume=mix_music_volume,
        resolution=resolution,
        platform=Platform(platform),
        split_long_video=split_long_video,
        split_video_minutes=split_video_minutes,
        match_voice_gender=match_voice_gender,
        remove_hardsub=remove_hardsub,
        male_ref_path=male_ref_path,
        female_ref_path=female_ref_path,
        single_ref_path=single_ref_path,
        local_video_paths=local_video_paths,
        watermark_text=(watermark_text.strip() if watermark_text else None),
    )

    return BatchCreateResponse(batch_id=batch_id, job_ids=job_ids)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = manager.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job đã hoàn tất hoặc đã bị hủy trước đó")
    return {"ok": True}


@app.post("/api/jobs/cancel-all")
async def cancel_all_jobs():
    cancelled = manager.cancel_all()
    return {"ok": True, "cancelled_job_ids": cancelled}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    """Re-queues a FAILED job, resuming from the stage it failed at. Earlier
    stages aren't redone if their output is still on disk (see
    JobManager._job_cache) — e.g. retrying an export failure reuses the
    already-downloaded video, transcript, translation, and TTS track."""
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = manager.retry_job(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Chỉ có thể thử lại job đang ở trạng thái lỗi (failed)")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = manager.delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Hãy hủy tác vụ trước khi xóa")
    return {"ok": True}


@app.delete("/api/jobs")
async def delete_all_finished_jobs():
    deleted = manager.delete_all_finished()
    return {"ok": True, "deleted_job_ids": deleted}


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str, file: Optional[str] = None):
    """Serves a finished output file so the browser's own Save As /
    Downloads flow handles where it ends up — this is what the frontend
    uses instead of requiring a typed filesystem path, since browsers can't
    hand a real folder picker to a web page.

    When split_long_video produced multiple parts, pass `?file=<filename>`
    (matching one of job.output_files[].filename) to pick which part to
    download; omitting it downloads the first part.
    """
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.SUCCESS or not job.output_files:
        raise HTTPException(status_code=409, detail="Job is not finished yet")

    target = job.output_files[0]
    if file is not None:
        match = next((f for f in job.output_files if f.filename == file), None)
        if match is None:
            raise HTTPException(status_code=404, detail="File not found for this job")
        target = match

    if not os.path.exists(target.path):
        raise HTTPException(status_code=410, detail="Output file no longer exists on the server")

    return FileResponse(
        target.path,
        media_type="video/mp4",
        filename=target.filename,
    )


@app.websocket("/api/ws")
async def job_status_ws(websocket: WebSocket):
    """Streams every job update (across all batches) as JSON. The frontend
    filters by the job_ids it cares about."""
    await websocket.accept()

    # Send current snapshot first
    for job in manager.jobs.values():
        await websocket.send_json(job.model_dump())

    queue: list = []

    def on_update(job):
        queue.append(job.model_dump())

    manager.add_listener(on_update)
    import asyncio

    try:
        while True:
            if queue:
                payload = queue.pop(0)
                await websocket.send_json(payload)
            else:
                await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_listener(on_update)