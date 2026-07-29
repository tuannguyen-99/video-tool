"""
Speech-to-text using faster-whisper. Loads the model once per process
(module-level singleton) since loading is the expensive part.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        # "small" / "medium" trade speed for accuracy. "medium" or "large-v3"
        # generally do better on Mandarin. Set WHISPER_MODEL env var to override.
        import os

        model_size = os.environ.get("WHISPER_MODEL", "medium")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        device = os.environ.get("WHISPER_DEVICE", "auto")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _model


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _transcribe_sync(audio_path: str, source_lang: str) -> List[Segment]:
    model = _get_model()
    # faster-whisper wants a bare language code like "zh", not "zh-CN"
    lang = source_lang.split("-")[0]
    segments, _info = model.transcribe(audio_path, language=lang, vad_filter=True)
    return [Segment(start=s.start, end=s.end, text=s.text.strip()) for s in segments if s.text.strip()]


async def transcribe(audio_path: str, source_lang: str = "zh") -> List[Segment]:
    return await asyncio.to_thread(_transcribe_sync, audio_path, source_lang)
