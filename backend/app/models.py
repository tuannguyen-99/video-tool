from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StageName(str, Enum):
    QUEUED = "queued"
    DOWNLOAD = "download"
    REMOVE_HARDSUB = "remove_hardsub"
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


class Platform(str, Enum):
    DOUYIN = "douyin"
    BILIBILI = "bilibili"
    # A video file the user already has on their machine, uploaded directly
    # instead of being fetched from a URL. Skips the download stage
    # entirely — see JobManager._run_job's local_video_path handling.
    LOCAL = "local"


class VoiceGender(str, Enum):
    MALE = "male"
    FEMALE = "female"


class OutputFile(BaseModel):
    filename: str
    path: str


class JobState(BaseModel):
    id: str
    url: str
    platform: Platform = Platform.DOUYIN
    status: JobStatus = JobStatus.PENDING
    stage: StageName = StageName.QUEUED
    # Original video title/caption in its source language, if the
    # downloader could recover it (best-effort — see downloader.py). None
    # if the source had no caption or the platform's tooling didn't expose
    # one; the UI should fall back to showing `url` in that case.
    title: Optional[str] = None
    # Vietnamese translation of `title`, populated shortly after download
    # succeeds. None while translation hasn't run yet (or `title` itself is
    # None) — this is best-effort and never fails the job on its own.
    title_vi: Optional[str] = None
    # Which stage failed at, if any (download / speech_to_text / translate / text_to_speech / export)
    failed_stage: Optional[StageName] = None
    error: Optional[str] = None
    # Whether videos longer than 10 minutes get cut into 10-minute parts.
    split_long_video: bool = False
    # Whether TTS voice (male/female) is auto-matched to the source
    # speaker's detected voice, instead of always using one fixed voice.
    match_voice_gender: bool = False
    # Whether hard-coded source-language subtitles already burned into the
    # source video's pixels get erased (AI inpainting, via VSR) before the
    # new Vietnamese subtitles are burned in. Off by default — slow
    # (frame-by-frame AI), only meaningful for source videos that actually
    # have a hard-sub. See job_manager.py / pipeline/subtitle_remover.py.
    remove_hardsub: bool = False
    # Populated once match_voice_gender is on and detection actually ran
    # (during the text_to_speech stage). None if the option is off, or if
    # detection was inconclusive and the default voice was used instead.
    detected_voice_gender: Optional[VoiceGender] = None
    # Populated on success. Normally a single entry; if split_long_video
    # kicked in, one entry per 10-minute part, in order.
    output_files: list[OutputFile] = Field(default_factory=list)
    # Kept for backwards compatibility with older frontends: mirrors
    # output_files[0] once the job succeeds.
    output_filename: Optional[str] = None
    output_path: Optional[str] = None
    progress: float = 0.0  # 0..1 within the current stage, best-effort


class BatchCreateResponse(BaseModel):
    batch_id: str
    job_ids: list[str]


class CreateBatchForm(BaseModel):
    urls: str = Field(..., description="Comma-separated video URLs, all for the same platform")
    platform: Platform = Field(Platform.DOUYIN, description="Which site the URLs belong to")
    output_dir: Optional[str] = Field(
        None, description="Absolute folder path on the server to save results into. If omitted, files are kept in server-side storage and served via the download endpoint."
    )
    source_lang: str = "zh-CN"
    target_lang: str = "vi"
    burn_subtitles: bool = True
    mix_music_volume: float = 0.15  # 0..1, relative volume of background music under TTS voice
    resolution: str = "1080p"  # "720p" or "1080p" — output height, width scaled to preserve aspect ratio
    split_long_video: bool = False  # cut the final video into parts if longer than split_video_minutes
    split_video_minutes: float = 10.0  # part length in minutes, only used when split_long_video is True
    match_voice_gender: bool = False  # pick TTS voice (male/female) to match the source speaker instead of one fixed voice
    remove_hardsub: bool = False  # erase hard-coded source-language subtitles already burned into the source video before burning in the new Vietnamese ones (slow — AI inpainting)