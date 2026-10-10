"""
Removes hardcoded (burned-in) subtitles from a downloaded source video —
e.g. Chinese hard-subs the original uploader already baked into the pixels
— using video-subtitle-remover (VSR): https://github.com/YaoFANGUK/video-subtitle-remover

This is AI inpainting (detect the subtitle region, then repaint it from
neighboring frames), not text/overlay manipulation — nothing else in this
app's pipeline does anything like it, so it has its own heavy dependency
stack (torch + paddlepaddle + onnxruntime) kept OUT of the main app's venv
entirely (see _NATIVE_PYTHON below) — same reasoning as yutto in
downloader.py, just a much heavier install this time.

Runs as a pipeline step between download_video() and everything else:
Chinese audio is unaffected either way — confirmed by reading VSR's
source, its SubtitleRemover.run() calls merge_audio_to_video() at the end
to re-attach the ORIGINAL audio track onto the inpainted video, it never
touches audio — but this MUST run before mux.py burns the new Vietnamese
subtitles in, or the two subtitle layers would visually stack.

Two backends, chosen per-machine via VSR_MODE — this is a machine-level
deployment setting, NOT a per-job UI choice like translate_engine/
bilibili_engine: which mode works at all depends on what's physically
installed on that machine, not on anything about the job.

  docker (VSR_MODE=docker) — for a machine with an NVIDIA GPU and Docker,
      e.g. a PC with an RTX 50-series card: runs one of VSR's official
      prebuilt images (no local torch/paddle/CUDA install needed at all).
      RTX 50-series (Blackwell) wants the cuda12.8-tagged image:
          VSR_DOCKER_IMAGE=eritpchy/video-subtitle-remover:1.4.0-cuda12.8
      Do NOT use this mode on Apple Silicon: Docker on macOS runs
      containers inside a Linux VM with no path to the Mac's own GPU
      (Metal), so a "GPU" container there would silently just run on CPU
      anyway, slower than running natively — see the "native" mode below
      for what macOS should use instead.

  native (VSR_MODE=native, default) — invokes a local Python install of
      VSR's own backend/main.py directly. This is the only realistic mode
      for Apple Silicon (a Mac, e.g. M1): VSR's README has a dedicated
      macOS/Apple-Silicon install path (CPU-mode torch+paddlepaddle — no
      CUDA involved, since Apple GPUs don't run CUDA) and notes PP-OCRv4's
      subtitle-region detection is somewhat weaker on Apple Silicon than
      elsewhere, which is one more reason to pass an explicit
      --subtitle-area-coords (see `subtitle_area` below) there rather than
      relying on full-frame auto-detection. A CUDA machine can also use
      native mode instead of Docker if preferred (installing the CUDA
      build of torch/paddlepaddle locally per VSR's README) — Docker is
      just the easier default when available.

      Setup (do this ONCE per machine, in an isolated venv — do not
      install into this app's own venv, torch/paddlepaddle pin versions
      that are highly likely to fight with whatever this app's other
      pieces need):

          git clone https://github.com/YaoFANGUK/video-subtitle-remover /opt/vsr
          python3 -m venv /opt/vsr-venv
          /opt/vsr-venv/bin/pip install -r /opt/vsr/requirements.txt
          # then EITHER the CUDA torch/paddlepaddle build (PC/RTX 5050) OR
          # the CPU build (Mac M1) — see VSR's README for the exact pip
          # command for your platform, they differ.

      Then point these two env vars at that install:
          VSR_REPO_DIR=/opt/vsr
          VSR_PYTHON=/opt/vsr-venv/bin/python

Common env vars (both modes):
  VSR_INPAINT_MODE   one of sttn-auto (default), sttn-det, lama,
                     propainter, opencv — see VSR's README for the
                     speed/quality tradeoffs between these; sttn-auto is a
                     reasonable default for live-action video.
  VSR_SUBTITLE_AREA  "ymin,ymax,xmin,xmax" — the pixel box the hard-sub
                     sits in. Strongly recommended over leaving this unset:
                     without it VSR auto-detects text anywhere in the
                     frame, which is slower and risks erasing unrelated
                     on-screen text (signs, other watermarks) instead of
                     just the hard-sub. Leave unset only if the source
                     videos don't have a consistent subtitle position.
                     Can be overridden per-call via `subtitle_area`.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

SubtitleArea = Tuple[int, int, int, int]  # (ymin, ymax, xmin, xmax)

VSR_MODE_DOCKER = "docker"
VSR_MODE_NATIVE = "native"
SUPPORTED_VSR_MODES = (VSR_MODE_DOCKER, VSR_MODE_NATIVE)

DEFAULT_INPAINT_MODE = "sttn-auto"
DEFAULT_DOCKER_IMAGE = "eritpchy/video-subtitle-remover:1.4.0-cuda12.8"


class SubtitleRemovalError(Exception):
    pass


@dataclass
class SubtitleRemovalResult:
    video_path: str


def _resolve_mode() -> str:
    mode = os.environ.get("VSR_MODE", VSR_MODE_NATIVE).strip().lower()
    if mode not in SUPPORTED_VSR_MODES:
        raise SubtitleRemovalError(
            f"VSR_MODE={mode!r} không hợp lệ — chỉ hỗ trợ "
            f"{' hoặc '.join(repr(m) for m in SUPPORTED_VSR_MODES)}."
        )
    return mode


def _parse_subtitle_area(raw: str) -> SubtitleArea:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise SubtitleRemovalError(
            f"VSR_SUBTITLE_AREA={raw!r} không hợp lệ — cần đúng 4 số "
            f"'ymin,ymax,xmin,xmax', ví dụ '820,1080,0,1920'."
        )
    try:
        ymin, ymax, xmin, xmax = (int(p) for p in parts)
    except ValueError:
        raise SubtitleRemovalError(
            f"VSR_SUBTITLE_AREA={raw!r} không hợp lệ — cả 4 giá trị phải là số nguyên."
        )
    return (ymin, ymax, xmin, xmax)


def _resolve_subtitle_area(subtitle_area: Optional[SubtitleArea]) -> Optional[SubtitleArea]:
    if subtitle_area is not None:
        return subtitle_area
    raw = os.environ.get("VSR_SUBTITLE_AREA")
    return _parse_subtitle_area(raw) if raw else None


def _inpaint_mode() -> str:
    return os.environ.get("VSR_INPAINT_MODE", DEFAULT_INPAINT_MODE)


def _area_flags(subtitle_area: Optional[SubtitleArea]) -> list[str]:
    if subtitle_area is None:
        return []
    ymin, ymax, xmin, xmax = subtitle_area
    return ["-c", str(ymin), str(ymax), str(xmin), str(xmax)]


# VSR's own SubtitleRemover.run() (backend/main.py) drives a
# tqdm(total=frame_count, ..., file=sys.__stdout__, desc='Subtitle
# Removing') bar — confirmed by reading VSR's source, not guessed. tqdm's
# default renderer writes a bar like "Subtitle Removing:  45%|███ |
# 450/1000 [...]" to that one line repeatedly via "\r", not "\n", so a
# plain line-by-line stdout read (readline()) would sit blocked and never
# see any of it until the very end. This matches the *number* on that bar,
# which tracks processed video frames — a reasonable proxy for "how much
# of this video is done", not wall-clock time (inpaint mode/scene
# complexity can make some frames much slower than others).
_TQDM_PERCENT_RE = re.compile(r"(\d{1,3})%\|")


async def _run_with_progress(
    cmd: list[str],
    on_progress: Optional[Callable[[float], None]],
    extra_env: Optional[dict[str, str]] = None,
) -> None:
    env = {**os.environ, **(extra_env or {})}
    # Without this, Python's own stdio layer block-buffers when stdout
    # isn't a real terminal (exactly the case once we pipe it) regardless
    # of tqdm's internal flush() calls — on_progress would then arrive in
    # one big useless burst right at the end instead of live. This is the
    # single most important line for progress actually being visible.
    env["PYTHONUNBUFFERED"] = "1"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stderr_chunks: list[bytes] = []
    last_reported = -1.0

    async def _drain_stdout() -> None:
        nonlocal last_reported
        assert proc.stdout is not None
        buffer = ""
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            buffer += chunk.decode(errors="ignore")
            # tqdm rewrites the same line via "\r"; split on both "\r" and
            # "\n" so each rewritten frame of the bar is checked, not just
            # whatever accumulates before the next real newline (which may
            # never come until the process exits).
            *complete, buffer = re.split(r"[\r\n]", buffer)
            for piece in complete:
                match = _TQDM_PERCENT_RE.search(piece)
                if match and on_progress is not None:
                    pct = float(match.group(1))
                    if pct != last_reported:
                        last_reported = pct
                        on_progress(pct)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    try:
        await asyncio.gather(_drain_stdout(), _drain_stderr())
        returncode = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    if returncode != 0:
        stderr_text = b"".join(stderr_chunks).decode(errors="ignore")
        raise SubtitleRemovalError(
            f"VSR exited with code {returncode}: {stderr_text[-2000:]}"
        )


# ---------------------------------------------------------------------------
# native mode — local VSR checkout, run directly (Mac M1's only real option)
# ---------------------------------------------------------------------------

def _require_native() -> Tuple[str, str]:
    repo_dir = os.environ.get("VSR_REPO_DIR")
    python_bin = os.environ.get("VSR_PYTHON")
    if not repo_dir or not python_bin:
        raise SubtitleRemovalError(
            "VSR_MODE=native cần cả VSR_REPO_DIR (thư mục clone video-subtitle-remover) "
            "và VSR_PYTHON (python trong venv riêng đã cài dependencies của nó) — "
            "xem hướng dẫn cài trong docstring của subtitle_remover.py."
        )
    main_py = os.path.join(repo_dir, "backend", "main.py")
    if not os.path.isfile(main_py):
        raise SubtitleRemovalError(
            f"Không thấy {main_py!r} — VSR_REPO_DIR={repo_dir!r} có đúng là "
            f"thư mục clone video-subtitle-remover không?"
        )
    if shutil.which(python_bin) is None and not os.path.isfile(python_bin):
        raise SubtitleRemovalError(
            f"VSR_PYTHON={python_bin!r} không phải python thực thi được. "
            f"Xem hướng dẫn tạo venv riêng trong docstring của subtitle_remover.py."
        )
    return python_bin, main_py


async def _remove_native(
    video_path: str,
    out_path: str,
    subtitle_area: Optional[SubtitleArea],
    on_progress: Optional[Callable[[float], None]],
) -> None:
    python_bin, main_py = _require_native()
    cmd = [
        python_bin, main_py,
        "-i", video_path,
        "-o", out_path,
        "--inpaint-mode", _inpaint_mode(),
        *_area_flags(subtitle_area),
    ]
    await _run_with_progress(cmd, on_progress)


# ---------------------------------------------------------------------------
# docker mode — official prebuilt image (PC/RTX 5050's easy path)
# ---------------------------------------------------------------------------

async def _remove_docker(
    video_path: str,
    out_path: str,
    subtitle_area: Optional[SubtitleArea],
    on_progress: Optional[Callable[[float], None]],
) -> None:
    if shutil.which("docker") is None:
        raise SubtitleRemovalError(
            "VSR_MODE=docker nhưng không tìm thấy lệnh 'docker' trên máy này."
        )
    image = os.environ.get("VSR_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE)

    # Mount just the two files' common parent directory read/write, and
    # reference them inside the container by their basenames under /data —
    # keeps this working regardless of where the job's work_dir actually
    # lives on the host, without mounting anything wider than necessary.
    in_dir = os.path.dirname(os.path.abspath(video_path))
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if in_dir != out_dir:
        raise SubtitleRemovalError(
            "VSR_MODE=docker hiện chỉ hỗ trợ video_path và out_path cùng thư mục "
            f"(đang là {in_dir!r} và {out_dir!r}) — đơn giản hoá việc mount volume. "
            "Gọi với out_path cùng work_dir với video_path."
        )

    cmd = [
        "docker", "run", "--rm", "--gpus", "all",
        "-v", f"{in_dir}:/data",
        image,
        "python", "backend/main.py",
        "-i", f"/data/{os.path.basename(video_path)}",
        "-o", f"/data/{os.path.basename(out_path)}",
        "--inpaint-mode", _inpaint_mode(),
        *_area_flags(subtitle_area),
    ]
    await _run_with_progress(cmd, on_progress)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

_BACKENDS = {
    VSR_MODE_NATIVE: _remove_native,
    VSR_MODE_DOCKER: _remove_docker,
}


async def remove_hardcoded_subtitles(
    video_path: str,
    out_path: str,
    subtitle_area: Optional[SubtitleArea] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> SubtitleRemovalResult:
    """Runs VSR on `video_path`, writing a hard-sub-free copy to `out_path`
    (must NOT equal video_path — VSR reads and writes as separate files;
    if you want to replace the original, pass a temp path and os.replace()
    it yourself after this returns).

    `subtitle_area` is (ymin, ymax, xmin, xmax) in source pixel
    coordinates; omit to fall back to VSR_SUBTITLE_AREA, or to VSR's own
    full-frame auto-detection if that's unset too (slower, see module
    docstring for the tradeoff).

    `on_progress`, if given, is called with a float 0-100 every time VSR's
    own frame-processing progress bar advances — parsed live from its
    stdout as the subprocess runs (confirmed by reading VSR's source:
    backend/main.py drives a tqdm(total=frame_count) bar), not just once
    at the end. This is the ONLY progress signal VSR exposes over its CLI
    (no separate "percent done" file or API), and it tracks frames
    processed, not wall-clock time — inpaint mode and scene complexity can
    make some frames take much longer than others, so progress may not
    advance smoothly.

    Which of VSR's two run modes (native process vs Docker container) is
    used is a machine-level setting (VSR_MODE env var), not a per-call
    choice — see the module docstring for why (it depends on what's
    physically installed on this machine, e.g. Mac M1 vs a CUDA PC).
    """
    if os.path.abspath(video_path) == os.path.abspath(out_path):
        raise SubtitleRemovalError(
            "video_path và out_path không được trùng nhau — VSR ghi ra file mới, "
            "không sửa tại chỗ."
        )
    if not os.path.isfile(video_path):
        raise SubtitleRemovalError(f"Không tìm thấy video đầu vào: {video_path!r}")

    resolved_area = _resolve_subtitle_area(subtitle_area)
    mode = _resolve_mode()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    await _BACKENDS[mode](video_path, out_path, resolved_area, on_progress)

    if not os.path.isfile(out_path):
        raise SubtitleRemovalError(
            "VSR báo chạy xong nhưng không thấy file đầu ra — kiểm tra log ở trên."
        )
    return SubtitleRemovalResult(video_path=out_path)