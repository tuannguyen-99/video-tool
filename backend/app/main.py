from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .job_manager import manager
from .models import BatchCreateResponse, JobStatus
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


@app.post("/api/jobs", response_model=BatchCreateResponse)
async def create_jobs(
    urls: str = Form(...),
    output_dir: Optional[str] = Form(None),
    source_lang: str = Form("zh-CN"),
    target_lang: str = Form("vi"),
    burn_subtitles: bool = Form(True),
    mix_music_volume: float = Form(0.15),
    resolution: str = Form("1080p"),
    cookie_file: UploadFile = File(...),
    music_file: Optional[UploadFile] = File(None),
):
    if resolution not in ("720p", "1080p"):
        raise HTTPException(status_code=422, detail="resolution phải là '720p' hoặc '1080p'")

    batch_id = uuid.uuid4().hex[:10]

    cookie_path = save_upload(batch_id, cookie_file.filename or "cookies.txt", await cookie_file.read())
    music_path = None
    if music_file is not None and music_file.filename:
        music_path = save_upload(batch_id, music_file.filename, await music_file.read())

    url_list = [u.strip() for u in urls.split(",") if u.strip()]

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


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    """Serves the finished video so the browser's own Save As / Downloads
    flow handles where it ends up — this is what the frontend uses instead
    of requiring a typed filesystem path, since browsers can't hand a real
    folder picker to a web page."""
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.SUCCESS or not job.output_path:
        raise HTTPException(status_code=409, detail="Job is not finished yet")
    if not os.path.exists(job.output_path):
        raise HTTPException(status_code=410, detail="Output file no longer exists on the server")

    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=job.output_filename or os.path.basename(job.output_path),
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