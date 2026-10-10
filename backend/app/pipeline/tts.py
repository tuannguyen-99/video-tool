"""
Vietnamese TTS using VieNeu-TTS (https://github.com/pnnbao97/VieNeu-TTS),
via its `vieneu` Python SDK.

Install (torch-free, runs v3 Turbo on CPU via ONNX Runtime):
    pip install vieneu

On a CUDA machine the SDK auto-switches to the PyTorch engine; the code
below doesn't need to change either way.

SDK shape used here (per vieneu's own docs — "Voice cloning" / "Save &
reuse a cloned voice"):
    from vieneu import Vieneu
    tts = Vieneu()                              # defaults to v3 Turbo
    audio = tts.infer(text)                     # default built-in voice
    audio = tts.infer(text, voice=name)         # named voice (preset OR
                                                 # previously add_voice()'d)
    tts.add_voice(name, ref_audio_path, denoise=True)  # enroll a reference
                                                 # clip once: cleans it up
                                                 # (denoise, trim to <=8s)
                                                 # and extracts its speaker
                                                 # profile
    tts.save(audio, "output.wav")

Voice cloning and preset voices share the exact same call shape here —
infer(text, voice=<name>) — because add_voice() turns a reference clip
into a named voice usable exactly like a built-in preset. This is
deliberately simpler than an earlier version of this file, which passed
ref_audio/ref_text into infer() directly per call: that required a
non-obvious ref_text alongside ref_audio on some vieneu backends, which
needed auto-transcribing every reference clip just to get a value for it.
Per vieneu's docs, none of that was ever necessary — add_voice() is the
documented way to clone a voice and reuse it, and needs no ref_text at
all.

Enrollment (add_voice) happens lazily, on first use of a given ref_audio
path, and is cached in-memory for the life of the process — denoise +
speaker-profile extraction is too expensive to redo on every line when a
job reuses the same one or two reference clips across every segment. Not
persisted via vieneu's own save_voices(): voice_refs.py already handles
cross-session reuse of the underlying audio clips on our side, so
re-enrolling once per process start (rather than once ever) keeps this
file self-contained and never dependent on vieneu's own voices file being
present or in sync with what voice_refs.py has on disk.

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
  a) ref_audio passed to synthesize() for this call (per-job cloned
     voice, gender-matched) — enrolled via add_voice() on first use.
  b) VIENEU_REF_AUDIO env var (server-wide cloned voice, if configured —
     kept for backwards compatibility with single-voice deployments).
  c) voice passed to synthesize() for this call (per-job gender-matched
     preset)
  d) VIENEU_VOICE env var (server-wide default preset)
  e) vieneu's own built-in default voice
"""
from __future__ import annotations

import asyncio
import os
from typing import Literal, Optional

from .transcribe import strip_annotation_emoji

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


# ref_audio paths already enrolled via add_voice() in this process. Keyed
# by the path itself, since the same reference clip (a batch's uploaded
# male/female sample, or a saved voice_refs.py clip) gets reused across
# every matching segment in a job, and add_voice()'s denoise + profile
# extraction is far too expensive to redo per line.
_enrolled_ref_voices: set[str] = set()


def _voice_name_for_ref(ref_audio: str) -> str:
    """Stable per-clip name to enroll/reuse via vieneu's add_voice()/
    infer(voice=...). Prefixed so it can never collide with a real preset
    name configured via VIENEU_VOICE_MALE/VIENEU_VOICE_FEMALE/VIENEU_VOICE."""
    return f"__ref_clone__:{ref_audio}"


def _ensure_ref_voice_enrolled(engine, ref_audio: str) -> str:
    name = _voice_name_for_ref(ref_audio)
    if ref_audio not in _enrolled_ref_voices:
        if not hasattr(engine, "add_voice"):
            # Per vieneu's own docs: "denoise, add_voice, and cloning
            # require the PyTorch (GPU) engine; built-in voices work
            # everywhere." A torch-free/CPU-only install (or an older
            # vieneu version predating add_voice) won't have this method
            # at all — surfaced here as a clear, actionable error instead
            # of a bare AttributeError deep in engine.infer().
            raise RuntimeError(
                f"Bản 'vieneu' đang cài (engine={type(engine).__name__}) không có "
                f"add_voice() — tính năng clone giọng cần bản vieneu đủ mới và/hoặc "
                f"engine PyTorch (pip install \"vieneu[gpu]\"), theo README của "
                f"package. Chạy `pip show vieneu` và `pip install --upgrade vieneu` "
                f"(hoặc `--upgrade \"vieneu[gpu]\"`) rồi thử lại."
            )
        # denoise=True is documented as the default, but pass it
        # explicitly with a fallback in case an older/newer add_voice
        # signature doesn't accept the kwarg.
        try:
            engine.add_voice(name, ref_audio, denoise=True)
        except TypeError:
            engine.add_voice(name, ref_audio)
        _enrolled_ref_voices.add(ref_audio)
    return name


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
) -> None:
    # transcribe.py already strips these at the source, but text can
    # arrive here after passing through translation (or any other step)
    # that might reintroduce/preserve stray symbols — a chunk that's
    # only emoji gives vieneu's tokenizer nothing to synthesize, which
    # surfaces downstream as "No valid speech tokens found in the
    # output." rather than as an empty-text case already handled below.
    text = strip_annotation_emoji(text)
    if not text.strip():
        # Keep downstream muxing/timing intact even for empty segments
        _write_silence(out_wav_path, duration_s=0.3)
        return

    engine = _get_engine()

    # (a) Per-call cloned reference (e.g. this job's gender-matched
    # uploaded clip) takes priority over everything else — it's the most
    # specific choice available for this exact segment.
    if ref_audio:
        voice_name = _ensure_ref_voice_enrolled(engine, ref_audio)
        audio = engine.infer(text, voice=voice_name)
    else:
        # (b) Server-wide cloned reference, kept for backwards
        # compatibility with single-voice deployments that configure this
        # instead of passing ref_audio per call.
        env_ref_audio = os.environ.get("VIENEU_REF_AUDIO")
        if env_ref_audio:
            voice_name = _ensure_ref_voice_enrolled(engine, env_ref_audio)
            audio = engine.infer(text, voice=voice_name)
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
) -> None:
    """Synthesizes one line of text to a wav file.

    `voice` and `ref_audio` are both optional and typically supplied
    per-segment by job_manager (via resolve_voice_for_gender() /
    resolve_ref_audio_for_gender()) when match_voice_gender is on; leave
    both None to use whatever server-wide default is configured (see the
    priority order in this module's docstring).
    """
    await asyncio.to_thread(_synthesize_sync, text, out_wav_path, voice, ref_audio)