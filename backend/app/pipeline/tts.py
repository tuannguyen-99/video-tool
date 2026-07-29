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
"""
from __future__ import annotations

import asyncio
import os

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from vieneu import Vieneu

        _engine = Vieneu()
    return _engine


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


def _synthesize_sync(text: str, out_wav_path: str, voice: str | None) -> None:
    if not text.strip():
        # Keep downstream muxing/timing intact even for empty segments
        _write_silence(out_wav_path, duration_s=0.3)
        return

    engine = _get_engine()

    # Optional voice cloning: set VIENEU_REF_AUDIO (and optionally
    # VIENEU_REF_TEXT) to clone from a 3-5s reference clip instead of a
    # named preset voice. Falls back to the requested/default preset voice.
    ref_audio = os.environ.get("VIENEU_REF_AUDIO")
    if ref_audio:
        audio = engine.infer(
            text,
            ref_audio=ref_audio,
            ref_text=os.environ.get("VIENEU_REF_TEXT") or None,
        )
    elif voice:
        audio = engine.infer(text, voice=voice)
    else:
        default_voice = os.environ.get("VIENEU_VOICE")
        audio = engine.infer(text, voice=default_voice) if default_voice else engine.infer(text)

    engine.save(audio, out_wav_path)


async def synthesize(text: str, out_wav_path: str, voice: str | None = None) -> None:
    await asyncio.to_thread(_synthesize_sync, text, out_wav_path, voice)
