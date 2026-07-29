from __future__ import annotations

import os
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR = os.path.join(BASE_DIR, "storage", "uploads")
JOBS_DIR = os.path.join(BASE_DIR, "storage", "jobs")
OUTPUTS_DIR = os.path.join(BASE_DIR, "storage", "outputs")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)


def default_output_dir(job_id: str) -> str:
    """Server-side folder used when the user doesn't type an absolute path.
    Files here are served back to the browser through the download endpoint,
    which lets the browser's own Save As dialog handle "where does this go"
    instead of requiring a real filesystem path from the frontend."""
    path = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(path, exist_ok=True)
    return path


def new_job_workdir(job_id: str) -> str:
    path = os.path.join(JOBS_DIR, job_id)
    os.makedirs(path, exist_ok=True)
    return path


def save_upload(batch_id: str, filename: str, content: bytes) -> str:
    safe_name = f"{batch_id}_{uuid.uuid4().hex[:8]}_{os.path.basename(filename)}"
    path = os.path.join(UPLOADS_DIR, safe_name)
    with open(path, "wb") as fh:
        fh.write(content)
    return path
