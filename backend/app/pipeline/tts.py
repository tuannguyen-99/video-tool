"""
Vietnamese TTS using VieNeu-TTS (https://github.com/pnnbao97/VieNeu-TTS),
via its `vieneu` Python SDK.

Install (torch-free, runs v3 Turbo on CPU via ONNX Runtime):
    pip install vieneu

On a CUDA machine the SDK auto-switches to the PyTorch engine; the code
below doesn't need to change either way.

SDK shape used here (from the project's README):
    from vieneu import Vieneu
    tts = Vieneu()                      # defaults to v3 Turbo
    audio = tts.infer(text)             # default built-in voice
    audio = tts.infer(text, voice=name) # named preset voice
    audio = tts.infer(text, ref_audio=path, ref_text=optional_str)  # voice cloning
    tts.save(audio, "output.wav")

Two independent ways to pick a non-default voice, both optional and
resolved per-call rather than only from global env config:

1. Preset voice, matched to detected gender: when the pipeline's
   "match_voice_gender" option is on, job_manager detects the source
   speaker's likely gender (gender_detect.py) and asks for a voice via
   resolve_voice_for_gender() below, which maps "male"/"female" to a
   preset name configured through VIENEU_VOICE_MALE / VIENEU_VOICE_FEMALE
   env vars. Deployment-wide default; nothing changes for setups that
   don't configure these env vars.

2. Voice cloning, matched to detected gender: a job can instead supply its
   own reference clips (uploaded per-batch from the UI, one for a male
   voice and one for a female voice) via `synthesize(..., ref_audio=...)`.
   resolve_ref_audio_for_gender() picks the right one of the two paths
   based on the same per-segment gender detection used for (1). This is a
   per-job choice, not a server-wide setting — different batches can clone
   different voices without touching server config.

Priority per segment, highest first (see _synthesize_sync):
  a) ref_audio passed to synthesize() for this call (per-job cloned voice,
     gender-matched)
  b) VIENEU_REF_AUDIO env var (server-wide cloned voice, if configured —
     kept for backwards compatibility with single-voice deployments)
  c) voice passed to synthesize() for this call (per-job gender-matched
     preset)
  d) VIENEU_VOICE env var (server-wide default preset)
  e) vieneu's own built-in default voice
"""
from __future__ import annotations

import asyncio
import os
from typing import Literal, Optional

Gender = Literal["male", "female"]

_GENDER_VOICE_ENV = {
    "male": "VIENEU_VOICE_MALE",
    "female": "VIENEU_VOICE_FEMALE",
}

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from vieneu import Vieneu

        _engine = Vieneu()
    return _engine


def resolve_voice_for_gender(gender: Optional[Gender]) -> Optional[str]:
    """Maps a detected gender to a configured VieNeu preset voice name.
    Returns None (caller should fall back to the default voice) if gender
    is None (detection didn't run or came back inconclusive) or the
    matching env var isn't configured.

    Set these to real preset names from your `vieneu` install, e.g.:
        VIENEU_VOICE_MALE=male_1
        VIENEU_VOICE_FEMALE=female_1
    (check `vieneu`'s docs/README for the exact preset names it ships with).
    """
    if gender is None:
        return None
    env_name = _GENDER_VOICE_ENV.get(gender)
    if env_name is None:
        return None
    value = os.environ.get(env_name)
    return value.strip() or None if value else None


def resolve_ref_audio_for_gender(
    gender: Optional[Gender],
    male_ref_path: Optional[str],
    female_ref_path: Optional[str],
) -> Optional[str]:
    """Picks which per-job cloned-voice reference clip to use for a
    segment, based on its detected gender. Returns None (caller should
    fall back to resolve_voice_for_gender()/env config/default) if gender
    is None, or the matching path wasn't supplied for this batch (e.g. the
    user only uploaded a male reference clip, or didn't opt into
    match_voice_gender at all)."""
    if gender is None:
        return None
    if gender == "male":
        return male_ref_path
    if gender == "female":
        return female_ref_path
    return None


def _write_silence(path: str, duration_s: float) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(duration_s), path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _synthesize_sync(
    text: str,
    out_wav_path: str,
    voice: str | None,
    ref_audio: str | None,
    ref_text: str | None,
) -> None:
    if not text.strip():
        # Keep downstream muxing/timing intact even for empty segments
        _write_silence(out_wav_path, duration_s=0.3)
        return

    engine = _get_engine()

    # (a) Per-call cloned reference (e.g. this job's gender-matched
    # uploaded clip) takes priority over everything else — it's the most
    # specific choice available for this exact segment.
    if ref_audio:
        audio = engine.infer(text, ref_audio=ref_audio, ref_text=ref_text or None)
    else:
        # (b) Server-wide cloned reference, kept for backwards
        # compatibility with single-voice deployments that configure this
        # instead of passing ref_audio per call.
        env_ref_audio = os.environ.get("VIENEU_REF_AUDIO")
        if env_ref_audio:
            audio = engine.infer(
                text,
                ref_audio=env_ref_audio,
                ref_text=os.environ.get("VIENEU_REF_TEXT") or None,
            )
        elif voice:
            # (c) Per-call gender-matched preset voice.
            audio = engine.infer(text, voice=voice)
        else:
            # (d)/(e) Server-wide default preset, or vieneu's built-in default.
            default_voice = os.environ.get("VIENEU_VOICE")
            audio = engine.infer(text, voice=default_voice) if default_voice else engine.infer(text)

    engine.save(audio, out_wav_path)


async def synthesize(
    text: str,
    out_wav_path: str,
    voice: str | None = None,
    ref_audio: str | None = None,
    ref_text: str | None = None,
) -> None:
    """Synthesizes one line of text to a wav file.

    `voice` and `ref_audio` are both optional and typically supplied
    per-segment by job_manager (via resolve_voice_for_gender() /
    resolve_ref_audio_for_gender()) when match_voice_gender is on; leave
    both None to use whatever server-wide default is configured (see the
    priority order in this module's docstring).
    """
    await asyncio.to_thread(_synthesize_sync, text, out_wav_path, voice, ref_audio, ref_text)