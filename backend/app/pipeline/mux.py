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
import json
import os
import subprocess
from typing import List, Optional

from .transcribe import Segment


class FfmpegError(Exception):
    pass


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


async def build_tts_track(
    clip_paths: List[str], segments: List[Segment], total_duration: float, out_wav_path: str
) -> str:
    """Lays each per-segment TTS clip onto a single track at its original
    timestamp using ffmpeg's `adelay` + `amix`, so the Vietnamese voice stays
    roughly in sync with the original speech timing."""
    if not clip_paths:
        await _run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
            "-t", str(max(total_duration, 0.5)), out_wav_path,
        ])
        return out_wav_path

    inputs: List[str] = []
    filters: List[str] = []
    for i, clip in enumerate(clip_paths):
        inputs += ["-i", clip]
        delay_ms = int(max(segments[i].start, 0) * 1000)
        filters.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")

    mix_inputs = "".join(f"[a{i}]" for i in range(len(clip_paths)))
    filters.append(f"{mix_inputs}amix=inputs={len(clip_paths)}:dropout_transition=0:normalize=0[aout]")
    filter_complex = ";".join(filters)

    await _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]", "-t", str(total_duration),
        out_wav_path,
    ])
    return out_wav_path


async def export_final(
    video_path: str,
    tts_track_path: str,
    out_path: str,
    music_path: Optional[str] = None,
    srt_path: Optional[str] = None,
    music_volume: float = 0.15,
    burn_subtitles: bool = True,
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

    video_filter = None
    if burn_subtitles and srt_path:
        # Escape order matters: backslash first, then colon and single quote.
        # `filename=` must be explicit — passing the path as a bare positional
        # value (e.g. subtitles='path') trips newer ffmpeg's option parser
        # with "No option name near '<path>'".
        escaped = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        video_filter = f"subtitles=filename='{escaped}'"

    cmd = ["ffmpeg", "-y", *inputs]

    fc_parts = list(filter_parts)
    map_video = "0:v"
    if video_filter:
        fc_parts.append(f"[0:v]{video_filter}[vout]")
        map_video = "[vout]"

    if fc_parts:
        cmd += ["-filter_complex", ";".join(fc_parts)]

    cmd += ["-map", map_video]
    cmd += ["-map", audio_label if audio_label.startswith("[") else audio_label]
    cmd += ["-c:v", "libx264", "-c:a", "aac", "-shortest", out_path]

    await _run(cmd)
    return out_path