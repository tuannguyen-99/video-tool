"""
Chinese -> Vietnamese translation using deep_translator's GoogleTranslator.
Batches per-segment calls with a small concurrency limit to stay polite to
the free endpoint.
"""
from __future__ import annotations

import asyncio
from typing import List

from .transcribe import Segment

_SEM = asyncio.Semaphore(4)


def _translate_sync(text: str, source: str, target: str) -> str:
    from deep_translator import GoogleTranslator

    if not text.strip():
        return ""
    return GoogleTranslator(source=source, target=target).translate(text)


async def _translate_one(text: str, source: str, target: str) -> str:
    async with _SEM:
        return await asyncio.to_thread(_translate_sync, text, source, target)


async def translate_segments(
    segments: List[Segment], source_lang: str = "zh-CN", target_lang: str = "vi"
) -> List[Segment]:
    translations = await asyncio.gather(
        *[_translate_one(seg.text, source_lang, target_lang) for seg in segments]
    )
    return [
        Segment(start=seg.start, end=seg.end, text=translated)
        for seg, translated in zip(segments, translations)
    ]
