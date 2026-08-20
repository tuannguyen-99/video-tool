"""
Chinese -> Vietnamese translation using deep_translator. Batches per-segment
calls with a small concurrency limit.

Google Translate's free/unofficial endpoint (what deep_translator's
GoogleTranslator scrapes) occasionally raises "No translation was found
using the current translator" — this is almost always a transient
rate-limit or parsing hiccup on Google's end, not a real translation
failure. So each segment gets a few retries with backoff, and if Google
keeps failing, falls back to MyMemoryTranslator before giving up on that
segment (which is literally what the error message suggests: "Try another
translator?").

MyMemory uses region-qualified language codes (e.g. "vi-VN", not bare
"vi") and doesn't recognize the same short codes Google does, so falling
back naively with Google's codes fails with "No support for the provided
language". _to_mymemory_code() below resolves a bare/short code to the
matching region-qualified one MyMemory expects before calling it.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Dict, List, Optional

from .transcribe import Segment

_SEM = asyncio.Semaphore(3)
_MAX_RETRIES = 3
_BASE_DELAY_S = 0.6

# Known-good overrides for the codes this app actually uses — checked
# before falling back to scanning MyMemory's full supported-language dict,
# so the common path never depends on that lookup succeeding.
_MYMEMORY_CODE_OVERRIDES = {
    "vi": "vi-VN",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "en": "en-GB",
}

_mymemory_supported_cache: Optional[Dict[str, str]] = None


class TranslationError(Exception):
    pass


def _mymemory_supported_languages() -> Dict[str, str]:
    global _mymemory_supported_cache
    if _mymemory_supported_cache is None:
        try:
            from deep_translator import MyMemoryTranslator

            _mymemory_supported_cache = MyMemoryTranslator().get_supported_languages(as_dict=True)
        except Exception:
            _mymemory_supported_cache = {}
    return _mymemory_supported_cache


def _to_mymemory_code(code: str) -> str:
    """Resolves a Google-style code (e.g. "vi", "zh-CN") to the
    region-qualified code MyMemory expects (e.g. "vi-VN"). Falls through:
    known override -> exact match in MyMemory's own supported list ->
    same base language in that list -> the original code unchanged (so
    MyMemory itself raises a clear error rather than this silently
    swallowing an unrecognized code)."""
    normalized = code.strip().lower()
    if normalized in _MYMEMORY_CODE_OVERRIDES:
        return _MYMEMORY_CODE_OVERRIDES[normalized]

    supported_values = _mymemory_supported_languages().values()
    values_by_lower = {v.lower(): v for v in supported_values}

    if normalized in values_by_lower:
        return values_by_lower[normalized]

    base = normalized.split("-")[0]
    for v_lower, v in values_by_lower.items():
        if v_lower.split("-")[0] == base:
            return v

    return code


def _translate_sync(text: str, source: str, target: str) -> str:
    from deep_translator import GoogleTranslator, MyMemoryTranslator

    if not text.strip():
        return ""

    last_err: Optional[Exception] = None

    for attempt in range(_MAX_RETRIES):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result:
                return result
            last_err = TranslationError("Google trả về kết quả rỗng")
        except Exception as e:
            last_err = e

        # Backoff with jitter before retrying — gives a rate limit time to
        # clear instead of hammering the endpoint again immediately.
        time.sleep(_BASE_DELAY_S * (attempt + 1) + random.uniform(0, 0.3))

    # Google kept failing after retries — fall back to MyMemory rather than
    # failing the whole job over one flaky segment. MyMemory needs its own
    # region-qualified codes, not Google's.
    try:
        mm_source = _to_mymemory_code(source)
        mm_target = _to_mymemory_code(target)
        result = MyMemoryTranslator(source=mm_source, target=mm_target).translate(text)
        if result:
            return result
        last_err = TranslationError("MyMemory trả về kết quả rỗng")
    except Exception as e:
        last_err = e

    raise TranslationError(f"Không dịch được đoạn {text!r}: {last_err}")


async def _translate_one(text: str, source: str, target: str) -> str:
    async with _SEM:
        return await asyncio.to_thread(_translate_sync, text, source, target)


async def translate_text(text: str, source_lang: str = "zh-CN", target_lang: str = "vi") -> str:
    """One-off translation of a short string (e.g. a video's title/caption),
    reusing the same Google -> MyMemory fallback chain and retry logic as
    translate_segments(), just without the Segment wrapper. Shares the
    module's concurrency semaphore so a title translation doesn't add load
    beyond the limit already applied to per-segment subtitle translation."""
    return await _translate_one(text, source_lang, target_lang)


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