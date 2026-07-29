from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StageName(str, Enum):
    QUEUED = "queued"
    DOWNLOAD = "download"
    SPEECH_TO_TEXT = "speech_to_text"
    TRANSLATE = "translate"
    TEXT_TO_SPEECH = "text_to_speech"
    EXPORT = "export"
    DONE = "done"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobState(BaseModel):
    id: str
    url: str
    status: JobStatus = JobStatus.PENDING
    stage: StageName = StageName.QUEUED
    # Which stage failed at, if any (download / speech_to_text / translate / text_to_speech / export)
    failed_stage: Optional[StageName] = None
    error: Optional[str] = None
    output_filename: Optional[str] = None
    output_path: Optional[str] = None
    progress: float = 0.0  # 0..1 within the current stage, best-effort


class BatchCreateResponse(BaseModel):
    batch_id: str
    job_ids: list[str]


class CreateBatchForm(BaseModel):
    urls: str = Field(..., description="Comma-separated Douyin URLs")
    output_dir: Optional[str] = Field(
        None, description="Absolute folder path on the server to save results into. If omitted, files are kept in server-side storage and served via the download endpoint."
    )
    source_lang: str = "zh-CN"
    target_lang: str = "vi"
    burn_subtitles: bool = True
    mix_music_volume: float = 0.15  # 0..1, relative volume of background music under TTS voice