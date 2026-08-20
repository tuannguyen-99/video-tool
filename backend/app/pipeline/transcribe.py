"""
Speech-to-text using faster-whisper. Loads the model once per process
(module-level singleton) since loading is the expensive part.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List

_model = None

# Kept as a module constant (not just inline in _get_model()) so
# deps_check.py can check/report on the exact same default model — instead
# of duplicating this string and risking the two drifting apart.
DEFAULT_WHISPER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        # Defaults to large-v3-turbo (CT2 conversion by deepdml) — turbo
        # trades away Whisper's built-in X->English translation quality
        # (irrelevant here, we transcribe in the source language and
        # translate ourselves in translate.py) for much faster inference at
        # accuracy close to full large-v3, which noticeably improved
        # transcription quality over "medium" for this pipeline.
        #
        # WhisperModel accepts either a short size name (tiny/base/small/
        # medium/large-v3/...) or, as here, a full HuggingFace repo id of a
        # CT2-converted model — it downloads/caches it via huggingface_hub
        # the same way either way. First run for a given WHISPER_MODEL will
        # download it (~1.5GB at int8 for this one).
        #
        # Override via WHISPER_MODEL, e.g. back to "medium" for a smaller/
        # faster download, or another CT2 repo id.
        import os

        model_size = os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)
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