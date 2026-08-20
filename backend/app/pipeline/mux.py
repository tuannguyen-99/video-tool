"""
ffmpeg-based assembly:
  1. extract_audio(video) -> wav, for whisper
  2. build_tts_track(clips, segments, total_duration) -> one wav where each
     TTS clip starts at its segment's original timestamp (silence-padded)
  3. export_final(video, tts_track, music?, srt?, out_path) -> replaces the
     video's audio with the TTS track (optionally mixed with background
     music) and, if requested, burns the .srt into the picture.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
import wave
from typing import List, Optional

import numpy as np

from .transcribe import Segment


class FfmpegError(Exception):
    pass


# Target output heights for the resolution picker. Width is derived with
# scale=-2:<height> so aspect ratio is preserved and the resulting width is
# always even (required by libx264).
RESOLUTIONS = {
    "720p": 720,
    "1080p": 1080,
}

# How much build_tts_track is willing to speed up a TTS clip (pitch
# preserved) to stop it from overlapping into the next segment. librosa's
# phase-vocoder time-stretch (used below) stays fairly natural for speech up
# to roughly 1.3x; pushed further the reconstruction gets audibly
# glitchy/"vỡ tiếng" — garbled syllables, metallic artifacts — which hurts
# intelligibility more than letting the clip run slightly long into the
# next line's slot, so this is capped lower than it used to be (was 1.8).
_MAX_TIME_STRETCH_RATE = 1.5
# Tiny buffer kept between a clip's end and the next segment's start, so
# consecutive clips don't butt exactly against each other (rounding at the
# sample-index boundary would otherwise let them just barely touch).
_MIN_GAP_SECONDS = 0.03


async def _run(cmd: List[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        err_text = err.decode(errors="ignore")
        # Ghi full log ra file để debug, thay vì chỉ giữ 2000 ký tự cuối
        print("FFMPEG FULL STDERR:\n", err_text)
        raise FfmpegError(err_text[-4000:])  # tăng giới hạn hoặc bỏ hẳn slicing


async def probe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    data = json.loads(out or b"{}")
    return float(data.get("format", {}).get("duration", 0.0))


async def extract_audio(video_path: str, out_wav_path: str) -> str:
    await _run([
        "ffmpeg", "-y", "-i", video_path, "-vn",
        "-ac", "1", "-ar", "16000", out_wav_path,
    ])
    return out_wav_path


async def _time_stretch_int16(samples: np.ndarray, rate: float, sample_rate: int) -> np.ndarray:
    """Speeds up a mono int16 PCM buffer by `rate` (e.g. 1.3 = 30% faster)
    using ffmpeg's `atempo` filter, so a too-long TTS clip can be tightened
    to fit its slot without sounding pitch-shifted/chipmunked.

    Previously this used librosa's `time_stretch`, a phase-vocoder (STFT/
    frequency-domain) algorithm. Phase vocoders reconstruct each frame from
    its frequency content and stitched-together phase — which works well
    for steady, slowly-changing sounds (sustained music notes) but speech
    is full of short, abrupt transients (plosives like p/t/k, fast
    consonant-to-vowel transitions), and forcing those into
    frequency-domain frames close together introduces phase mismatches at
    exactly those transients — audible as "vỡ tiếng": crackly, robotic,
    garbled syllables, not just "faster".

    `atempo` uses WSOLA instead — it works directly in the time domain,
    finding good splice points and overlap-adding waveform chunks rather
    than reconstructing from frequency+phase. That handles speech's sharp
    transients far more gracefully, at the cost of being a little less
    precise on pure tones/music (irrelevant here — this only ever touches
    spoken TTS clips).

    ffmpeg's atempo accepts a single filter instance's tempo in [0.5, 100],
    which comfortably covers _MAX_TIME_STRETCH_RATE, so no chaining is
    needed. Runs over raw PCM piped through stdin/stdout (no temp files).
    If ffmpeg fails for any reason, returns `samples` unchanged — this is a
    quality improvement on top of the base pipeline, not a hard
    requirement, so a failure here just means this one clip's overlap
    isn't corrected rather than breaking the whole export."""
    if samples.size == 0 or rate <= 1.0:
        return samples

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-filter:a", f"atempo={rate}",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=samples.tobytes())
    except asyncio.CancelledError:
        raise
    except Exception:
        return samples

    if proc.returncode != 0 or not out:
        print("FFMPEG atempo FULL STDERR:\n", err.decode(errors="ignore") if err else "")
        return samples

    return np.frombuffer(out, dtype=np.int16)


async def build_tts_track(
    clip_paths: List[str], segments: List[Segment], total_duration: float, out_wav_path: str
) -> str:
    """Lays each per-segment TTS clip onto a single track at its original
    timestamp, so the Vietnamese voice stays roughly in sync with the
    original speech timing.

    A translated/synthesized line often takes longer to speak than the
    original did, which — if placed as-is at seg.start — would run past
    the next segment's start and audibly overlap it. Before mixing, each
    clip is checked against the gap until the next segment (or the end of
    the video, for the last one): if it doesn't fit, it's sped up
    (pitch-preserving time-stretch, capped at _MAX_TIME_STRETCH_RATE so it
    never gets unintelligible) just enough to land before the next line
    starts. Segments that already fit their slot are left untouched.

    Does the mixing itself in pure Python (wave + numpy) rather than
    shelling out to one ffmpeg call with an `-i <clip>` per segment: ffmpeg
    opens every `-i` input simultaneously, and a long video can easily have
    hundreds of segments — enough to blow past the OS's open-file-descriptor
    limit (macOS defaults `ulimit -n` to 256) partway through, failing with
    "Too many open files". Reading/writing one wav at a time here has no
    such ceiling, so it scales to videos of any length/segment count.
    """
    sample_rate = 24000  # matches vieneu's TTS output and _write_silence()'s anullsrc rate

    if not clip_paths:
        await _run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
            "-t", str(max(total_duration, 0.5)), out_wav_path,
        ])
        return out_wav_path

    total_samples = max(int(total_duration * sample_rate), 1)
    # int32 accumulator: headroom for overlapping segments before clipping
    # down to int16 at the end. Mirrors the old amix's normalize=0 intent —
    # overlaps aren't auto-attenuated, just prevented from wrapping around.
    mix = np.zeros(total_samples, dtype=np.int32)

    for i, (clip_path, seg) in enumerate(zip(clip_paths, segments)):
        with wave.open(clip_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise FfmpegError(
                    f"{clip_path}: expected mono 16-bit PCM, got "
                    f"{wf.getnchannels()}ch/{wf.getsampwidth() * 8}bit"
                )
            clip_sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        samples = np.frombuffer(raw, dtype=np.int16)

        if clip_sr != sample_rate and samples.size > 0:
            # Shouldn't normally happen (tts.py always writes 24kHz), but
            # resample defensively instead of desyncing the whole track.
            duration = samples.size / clip_sr
            new_len = max(int(duration * sample_rate), 1)
            samples = np.interp(
                np.linspace(0, samples.size - 1, new_len),
                np.arange(samples.size),
                samples.astype(np.float64),
            ).astype(np.int16)

        # How much room is there before the next line starts (or the video
        # ends, for the last segment)? If this clip runs longer than that,
        # tighten it up so it doesn't bleed into the next line.
        next_start = segments[i + 1].start if i + 1 < len(segments) else total_duration
        available_s = max(next_start - seg.start - _MIN_GAP_SECONDS, 0.0)
        available_samples = int(available_s * sample_rate)

        if available_samples > 0 and samples.size > available_samples:
            rate = min(samples.size / available_samples, _MAX_TIME_STRETCH_RATE)
            samples = await _time_stretch_int16(samples, rate, sample_rate)

        samples_i32 = samples.astype(np.int32)
        start = max(int(seg.start * sample_rate), 0)
        end = min(start + samples_i32.size, total_samples)
        if start >= total_samples or end <= start:
            continue
        mix[start:end] += samples_i32[: end - start]

    clipped = np.clip(mix, -32768, 32767).astype(np.int16)

    with wave.open(out_wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(clipped.tobytes())

    return out_wav_path


def _escape_drawtext(text: str) -> str:
    """Escapes a string for safe use inside ffmpeg's drawtext `text=`
    option. drawtext has its own mini escaping layer on top of the outer
    filtergraph escaping (colons separate filter options, single quotes
    wrap the value, backslashes are the escape char, and `%` starts a
    strftime-style expansion drawtext supports) — order matters: backslash
    first (so later-inserted escapes aren't themselves re-escaped), then
    the rest.
    """
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")  # drawtext can't escape a literal ' inside
        # a single-quoted value at all; swap for a visually-close
        # right-quote rather than breaking the filter.
        .replace("%", "\\%")
    )


# Default watermark look: white text, low opacity so it reads as a
# subtle brand mark rather than competing with the video, with a
# semi-transparent black outline so it stays legible ("nổi") over both
# light and dark backgrounds without needing a solid background box
# (a box would cover more of the picture than just the glyphs).
_WATERMARK_FONT_SIZE_RATIO = 0.035  # ~3.5% of output height
_WATERMARK_MARGIN_RATIO = 0.02      # ~2% of output height, from each edge
_WATERMARK_FONT_COLOR = "white@0.55"
_WATERMARK_BORDER_COLOR = "black@0.45"
_WATERMARK_BORDER_WIDTH = 2


async def export_final(
    video_path: str,
    tts_track_path: str,
    out_path: str,
    music_path: Optional[str] = None,
    srt_path: Optional[str] = None,
    music_volume: float = 0.15,
    burn_subtitles: bool = True,
    resolution: Optional[str] = None,
    watermark_text: Optional[str] = None,
) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    inputs = ["-i", video_path, "-i", tts_track_path]
    audio_label = "1:a"
    filter_parts: List[str] = []

    if music_path:
        inputs += ["-stream_loop", "-1", "-i", music_path]
        filter_parts.append(f"[2:a]volume={music_volume}[music]")
        filter_parts.append(f"[1:a][music]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        audio_label = "[aout]"

    # Video filter chain: scale (if requested), then the channel-name
    # watermark, then burn subtitles last — in that order so both the
    # watermark and subtitles are sized/positioned relative to the final
    # output resolution rather than the source resolution.
    video_filters: List[str] = []
    if resolution and resolution in RESOLUTIONS:
        target_h = RESOLUTIONS[resolution]
        video_filters.append(f"scale=-2:{target_h}")

    if watermark_text and watermark_text.strip():
        escaped_text = _escape_drawtext(watermark_text.strip())
        # fontsize/x/y as expressions of `h` (output frame height) so the
        # watermark scales sensibly whether exporting 720p or 1080p,
        # instead of a fixed pixel size that'd look oversized/tiny
        # depending on resolution.
        video_filters.append(
            "drawtext="
            f"text='{escaped_text}'"
            f":fontsize=h*{_WATERMARK_FONT_SIZE_RATIO}"
            f":fontcolor={_WATERMARK_FONT_COLOR}"
            f":borderw={_WATERMARK_BORDER_WIDTH}"
            f":bordercolor={_WATERMARK_BORDER_COLOR}"
            f":x=h*{_WATERMARK_MARGIN_RATIO}"
            f":y=h*{_WATERMARK_MARGIN_RATIO}"
        )

    if burn_subtitles and srt_path:
        # Escape order matters: backslash first, then colon and single quote.
        # `filename=` must be explicit — passing the path as a bare positional
        # value (e.g. subtitles='path') trips newer ffmpeg's option parser
        # with "No option name near '<path>'".
        escaped = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        video_filters.append(f"subtitles=filename='{escaped}'")

    cmd = ["ffmpeg", "-y", *inputs]

    fc_parts = list(filter_parts)
    map_video = "0:v"
    if video_filters:
        fc_parts.append(f"[0:v]{','.join(video_filters)}[vout]")
        map_video = "[vout]"

    if fc_parts:
        cmd += ["-filter_complex", ";".join(fc_parts)]

    cmd += ["-map", map_video]
    cmd += ["-map", audio_label if audio_label.startswith("[") else audio_label]
    cmd += ["-c:v", "libx264", "-c:a", "aac", "-shortest", out_path]

    await _run(cmd)
    return out_path


async def split_into_segments(
    input_path: str, out_dir: str, segment_seconds: int = 600, base_name: str = "part"
) -> List[str]:
    """Cuts a finished video into consecutive parts of at most
    `segment_seconds` each, via ffmpeg's segment muxer with stream copy (no
    re-encode, so this is fast). Returns the part paths in order.

    Uses -c copy for speed, which means cut points snap to the nearest
    keyframe rather than landing exactly on the second — fine for splitting
    a finished export into digestible chunks, but not frame-accurate.
    """
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, f"{base_name}_%03d.mp4")

    await _run([
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        pattern,
    ])

    return sorted(glob.glob(os.path.join(out_dir, f"{base_name}_*.mp4")))