"""
Optional pitch-based gender detection, used to pick a TTS voice that roughly
matches the source speaker's voice (male/female) instead of always using one
fixed voice.

This is a coarse heuristic, not real speaker diarization/classification: it
estimates the fundamental frequency (F0) with librosa.pyin and thresholds
the median F0 at ~165Hz — typical male speech averages ~85-180Hz, typical
female speech ~165-255Hz. Two entry points are exposed:

  - detect_gender(): one gender for the whole clip (aggregates all
    transcribed speech). Good for single-narrator videos.
  - detect_gender_per_segment(): one gender per transcribed segment, so a
    dialogue between a male and female speaker gets each line voiced with
    the matching gender instead of one voice for the entire video.

Neither of these tracks speaker *identity* — they only classify pitch per
segment. Two speakers of the same gender will both map to the same voice;
this can't tell them apart. It's also noisier per-segment than aggregated
over the whole clip, since short utterances (a word or two) don't give the
pitch estimator much to work with — detect_gender_per_segment() carries the
last confident detection forward across inconclusive segments (rather than
falling back to the default voice line-by-line) since consecutive short
lines are more often the same speaker continuing than a coin flip.

Because of all that, this only runs when the user explicitly opts in
(`match_voice_gender`), and any failure here just falls back to the
default single voice rather than failing the job — it's a nice-to-have,
never a hard requirement.

Install: pip install librosa
"""
from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

from .transcribe import Segment

Gender = Literal["male", "female"]

# Rough male/female F0 crossover. Values below this are classified male,
# at/above are classified female.
_MALE_FEMALE_THRESHOLD_HZ = 165.0

# Need a reasonable amount of voiced audio for the pitch estimate to be
# trustworthy; below this, detection is skipped rather than guessing.
_MIN_VOICED_SAMPLES_SECONDS = 1.0
# Per-segment version uses a lower bar — dialogue lines are often short —
# but still needs enough samples for pyin to produce anything meaningful.
_MIN_SEGMENT_SAMPLES_SECONDS = 0.35


def _classify_f0(samples, sr: int) -> Optional[Gender]:
    import librosa
    import numpy as np

    if samples.size < int(_MIN_SEGMENT_SAMPLES_SECONDS * sr):
        return None

    f0, voiced_flag, _voiced_prob = librosa.pyin(
        samples,
        fmin=float(librosa.note_to_hz("C2")),
        fmax=float(librosa.note_to_hz("C6")),
        sr=sr,
    )
    if f0 is None:
        return None

    valid_f0 = f0[voiced_flag]
    valid_f0 = valid_f0[~np.isnan(valid_f0)]
    if valid_f0.size == 0:
        return None

    median_f0 = float(np.median(valid_f0))
    return "male" if median_f0 < _MALE_FEMALE_THRESHOLD_HZ else "female"


def _load_audio(audio_path: str, sr: int = 16000):
    import librosa

    y, actual_sr = librosa.load(audio_path, sr=sr, mono=True)
    return y, actual_sr


def _detect_sync(audio_path: str, segments: List[Segment]) -> Optional[Gender]:
    y, sr = _load_audio(audio_path)

    # Only analyze the spans faster-whisper actually transcribed as speech,
    # so background music/silence/noise between segments doesn't skew the
    # pitch estimate.
    if segments:
        chunks = []
        for seg in segments:
            start = max(int(seg.start * sr), 0)
            end = min(int(seg.end * sr), len(y))
            if end > start:
                chunks.append(y[start:end])
        import numpy as np

        voiced = np.concatenate(chunks) if chunks else y
    else:
        voiced = y

    return _classify_f0(voiced, sr)


def _detect_per_segment_sync(
    audio_path: str, segments: List[Segment]
) -> List[Optional[Gender]]:
    y, sr = _load_audio(audio_path)

    results: List[Optional[Gender]] = []
    last_confident: Optional[Gender] = None

    for seg in segments:
        start = max(int(seg.start * sr), 0)
        end = min(int(seg.end * sr), len(y))
        chunk = y[start:end] if end > start else y[:0]

        gender = _classify_f0(chunk, sr)
        if gender is None:
            # Too short/inconclusive on its own — assume the same speaker
            # is still talking rather than reverting to the default voice
            # for one word. First segment with no prior falls through to
            # None, which callers treat as "use the default voice".
            gender = last_confident
        else:
            last_confident = gender

        results.append(gender)

    return results


async def detect_gender(audio_path: str, segments: List[Segment]) -> Optional[Gender]:
    """Best-effort single gender guess for the whole clip, from pitch. Run
    off the event loop since librosa's analysis is CPU-bound. Returns None
    if detection can't produce a confident result (too little voiced audio,
    librosa not installed, or any other error) — callers should fall back
    to the default voice in that case rather than failing the job."""
    try:
        return await asyncio.to_thread(_detect_sync, audio_path, segments)
    except Exception:
        return None


async def detect_gender_per_segment(
    audio_path: str, segments: List[Segment]
) -> List[Optional[Gender]]:
    """Best-effort per-segment gender guess, for dialogue between speakers
    of different genders — each transcribed line gets its own detection
    (with inconclusive short lines carrying forward the last confident
    result) instead of one gender for the whole video. Returns a list the
    same length as `segments`; entries are None where nothing could be
    determined (caller should use the default voice for those). On total
    failure (e.g. librosa not installed), returns an all-None list of the
    same length rather than raising, so callers can zip it with segments
    unconditionally."""
    try:
        return await asyncio.to_thread(_detect_per_segment_sync, audio_path, segments)
    except Exception:
        return [None] * len(segments)