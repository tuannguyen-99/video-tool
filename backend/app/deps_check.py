"""
Checks whether the external tools and Python packages this app shells out
to / imports are actually installed, so the "system status" view can show
a clear installed/missing list instead of the user only finding out when a
job fails mid-run with something like "[Errno 2] No such file or directory".

Two kinds of dependencies:
  - "binary": an executable that must be on PATH (checked with shutil.which,
    then optionally probed with its --version/-version flag).
  - "python_package": a Python package that must be importable in this
    process (checked with importlib.util.find_spec — this does NOT import
    the module, so it's cheap and doesn't trigger e.g. loading a Whisper
    model just to check it's installed).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
from typing import AsyncIterator, Optional, TypedDict

# (binary name, human description, version flag, install hint, pip spec or None)
# pip spec is only set when the dependency can be safely auto-installed with
# `pip install <spec>` in this process — ffmpeg/ffprobe are system binaries
# (apt/brew + sudo), so they stay None and must be installed manually.
_BINARIES = [
    ("f2", "Tải video Douyin", "--version", "pip install f2", "f2"),
    ("yt-dlp", "Tải video Bilibili", "--version", "pip install yt-dlp", "yt-dlp"),
    ("ffmpeg", "Ghép audio/video, cắt đoạn, xuất file cuối", "-version",
     "Cài qua trình quản lý gói hệ thống, ví dụ: apt install ffmpeg (Ubuntu/Debian) hoặc brew install ffmpeg (macOS)",
     None),
    ("ffprobe", "Đọc thời lượng video (đi kèm ffmpeg)", "-version",
     "Cài qua trình quản lý gói hệ thống, ví dụ: apt install ffmpeg (Ubuntu/Debian) hoặc brew install ffmpeg (macOS)",
     None),
]

# (import name, human description, install hint, pip spec or None)
_PY_PACKAGES = [
    ("faster_whisper", "Nhận dạng giọng nói (speech-to-text)", "pip install faster-whisper", "faster-whisper"),
    ("deep_translator", "Dịch phụ đề sang tiếng Việt", "pip install deep-translator", "deep-translator"),
    ("vieneu", "Giọng đọc tiếng Việt (text-to-speech)", "pip install vieneu", "vieneu"),
]

# Server-side whitelist mapping a dependency's `name` to the exact pip spec
# that gets installed. The install endpoint only ever accepts a `name` that
# is a key here — it never accepts a pip spec/version string from the
# client — so a caller can't smuggle in an arbitrary `pip install` argument
# (e.g. `--index-url`, a git URL, or an unrelated package) against the
# backend host.
PIP_PACKAGE_BY_NAME: dict[str, str] = {
    **{name: pip_spec for name, _desc, _flag, _hint, pip_spec in _BINARIES if pip_spec},
    **{name: pip_spec for name, _desc, _hint, pip_spec in _PY_PACKAGES if pip_spec},
}

# The Whisper speech-to-text model isn't a pip package — it's a set of
# weight files (hundreds of MB to a few GB) that faster-whisper downloads
# from Hugging Face Hub on first use. It's checked/installed the same way
# as everything else above (same DependencyStatus shape, same "Cài đặt"
# button in the UI), just through a different mechanism under the hood.
#
# Fixed name -> resolved model size/repo id (read from WHISPER_MODEL, same
# env var transcribe.py itself reads, falling back to the same default) so
# this always reports on exactly the model that will actually be loaded.
WHISPER_MODEL_NAME = "whisper_model"


def _configured_whisper_model_size() -> str:
    from .pipeline.transcribe import DEFAULT_WHISPER_MODEL

    return os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)


# Kept as a "whitelist of one" for the same reason as PIP_PACKAGE_BY_NAME:
# the install endpoint decides what to download purely by looking up a
# client-supplied `name` in this dict — never from anything else the client
# sends — so it can't be used to fetch an arbitrary repo id.
MODEL_DOWNLOAD_BY_NAME: dict[str, str] = {WHISPER_MODEL_NAME: _configured_whisper_model_size()}


class DependencyStatus(TypedDict):
    name: str
    kind: str  # "binary" | "python_package"
    description: str
    installed: bool
    detail: Optional[str]  # resolved path (binary) or module origin (package)
    version: Optional[str]
    install_hint: str
    auto_installable: bool  # True if a "Cài đặt" button can pip-install this


class DependencyInstallError(Exception):
    pass


async def _binary_version(binary: str, version_flag: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, version_flag,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        text = (out or err).decode(errors="ignore").strip()
        first_line = text.splitlines()[0] if text else None
        return first_line[:200] if first_line else None
    except Exception:
        return None


def _whisper_model_status(model_size: str) -> tuple[bool, Optional[str]]:
    """Network-free check for whether the configured Whisper model has
    already been downloaded. Deliberately reuses faster_whisper's own
    download_model(..., local_files_only=True) — the exact function
    WhisperModel(...) calls internally to resolve a short size name
    (tiny/medium/large-v3/...) or a full HF repo id and locate it in the
    local cache — instead of reimplementing that resolution/cache lookup
    logic here and risking it drifting out of sync. Returns (False, None)
    for anything not cached yet, or if faster_whisper/huggingface_hub
    aren't importable (the python_package check above already reports that
    separately)."""
    try:
        from faster_whisper.utils import download_model

        path = download_model(model_size, local_files_only=True)
        return True, path
    except Exception:
        return False, None


async def check_all() -> list[DependencyStatus]:
    results: list[DependencyStatus] = []

    for binary, description, version_flag, install_hint, pip_spec in _BINARIES:
        path = shutil.which(binary)
        installed = path is not None
        version = await _binary_version(binary, version_flag) if installed else None
        results.append({
            "name": binary,
            "kind": "binary",
            "description": description,
            "installed": installed,
            "detail": path,
            "version": version,
            "install_hint": install_hint,
            "auto_installable": pip_spec is not None,
        })

    for module_name, description, install_hint, pip_spec in _PY_PACKAGES:
        spec = importlib.util.find_spec(module_name)
        installed = spec is not None
        results.append({
            "name": module_name,
            "kind": "python_package",
            "description": description,
            "installed": installed,
            "detail": spec.origin if spec else None,
            "version": None,
            "install_hint": install_hint,
            "auto_installable": pip_spec is not None,
        })

    model_size = MODEL_DOWNLOAD_BY_NAME[WHISPER_MODEL_NAME]
    model_installed, model_detail = _whisper_model_status(model_size)
    results.append({
        "name": WHISPER_MODEL_NAME,
        "kind": "model",
        "description": "Model nhận dạng giọng nói (speech-to-text)",
        "installed": model_installed,
        "detail": model_detail,
        "version": model_size,
        "install_hint": (
            "Chưa tải về. Model có thể từ vài trăm MB đến vài GB tùy cấu hình "
            "(WHISPER_MODEL), chỉ cần tải một lần và được lưu vào cache của "
            "Hugging Face (mặc định ~/.cache/huggingface)."
        ),
        "auto_installable": True,
    })

    return results


async def install_pip_package(pip_spec: str) -> AsyncIterator[str]:
    """Runs `pip install <pip_spec>` in this interpreter's environment and
    yields output line by line as it happens, so a caller can stream progress
    to the browser instead of the UI hanging silently for however long the
    install takes (e.g. faster-whisper pulling in torch).

    Raises DependencyInstallError if pip exits non-zero. `pip_spec` must come
    from PIP_PACKAGE_BY_NAME — callers must not pass client-supplied strings
    here directly.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", pip_spec,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    try:
        async for raw_line in proc.stdout:
            yield raw_line.decode(errors="ignore").rstrip("\n")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    returncode = await proc.wait()
    if returncode != 0:
        raise DependencyInstallError(
            f"pip install {pip_spec} exited with code {returncode}"
        )


def _cached_size_bytes(model_size: str) -> int:
    """Best-effort lookup of how much this model has downloaded so far, by
    scanning the local Hugging Face cache. `model_size` may be a short
    faster-whisper name (e.g. "medium") rather than the full repo id
    (e.g. "Systran/faster-whisper-medium"), so this matches loosely rather
    than requiring an exact repo_id — good enough for a progress readout,
    not used for the installed/not-installed decision (see
    _whisper_model_status, which resolves it properly)."""
    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        for repo in info.repos:
            if repo.repo_type != "model":
                continue
            if model_size == repo.repo_id or model_size in repo.repo_id:
                return repo.size_on_disk
    except Exception:
        pass
    return 0


async def download_whisper_model(model_size: str) -> AsyncIterator[str]:
    """Downloads (or resumes) the configured Whisper model into the local
    Hugging Face cache by calling faster_whisper's own download_model() —
    same function WhisperModel(...) uses internally, so this can't drift
    out of sync with what actually gets loaded at transcribe time.

    That call is synchronous and gives no line-by-line progress hook, so it
    runs in a background thread here while this yields periodic lines based
    on cache size growth, the same shape install_pip_package() streams pip
    output in — good enough to show the browser something is happening on a
    download that can take several minutes, without blocking the event
    loop. Safe to call even if partially downloaded already; Hugging Face's
    downloader resumes rather than starting over.
    """
    from faster_whisper.utils import download_model

    yield f"Đang tải model '{model_size}'... (có thể mất vài phút tùy dung lượng và tốc độ mạng)"

    errors: list[BaseException] = []

    def _run_download() -> None:
        try:
            download_model(model_size)
        except BaseException as e:  # surfaced to the caller below
            errors.append(e)

    task = asyncio.create_task(asyncio.to_thread(_run_download))

    last_reported_tenth_gb = -1
    try:
        while not task.done():
            await asyncio.sleep(3)
            size_gb = _cached_size_bytes(model_size) / (1024 ** 3)
            tenth = int(size_gb * 10)
            if size_gb > 0 and tenth != last_reported_tenth_gb:
                last_reported_tenth_gb = tenth
                yield f"Đã tải: {size_gb:.1f} GB..."
    except asyncio.CancelledError:
        task.cancel()
        raise

    await task
    if errors:
        raise DependencyInstallError(f"Tải model '{model_size}' thất bại: {errors[0]}")

    yield "Tải xong."