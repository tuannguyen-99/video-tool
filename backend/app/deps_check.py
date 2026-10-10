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
from typing import AsyncIterator, Callable, Optional, TypedDict

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
    ("funasr", "Nhận dạng giọng nói (speech-to-text) — engine mặc định", "pip install funasr", "funasr"),
    ("faster_whisper", "Nhận dạng giọng nói (speech-to-text) — engine dự phòng, chọn qua STT_ENGINE=whisper", "pip install faster-whisper", "faster-whisper"),
    ("vieneu", "Giọng đọc tiếng Việt (text-to-speech)", "pip install vieneu", "vieneu"),
    ("ctranslate2", "Chạy model dịch offline (NLLB-200 và Hachimi) — bắt buộc; cũng được faster_whisper dùng nội bộ", "pip install ctranslate2", "ctranslate2"),
    ("transformers", "Tokenizer cho cả hai model dịch offline — bắt buộc", "pip install transformers", "transformers"),
    ("sentencepiece", "Tokenizer phụ trợ cho cả hai model dịch offline — bắt buộc", "pip install sentencepiece", "sentencepiece"),
    ("huggingface_hub", "Tải model từ Hugging Face (Whisper, Hachimi) — thường có sẵn kèm transformers", "pip install huggingface_hub", "huggingface_hub"),
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


# NLLB-200 (local, offline translation engine — TRANSLATE_ENGINE=nllb in
# translate.py) isn't a plain download like the Whisper model above: it's
# the original HF checkpoint CONVERTED into CTranslate2 format via the
# `ct2-transformers-converter` CLI (installed by the `ctranslate2` pip
# package), which needs `transformers` + `torch` importable to run (torch
# only for this one-off conversion step — never touched again at
# inference time, same as faster-whisper's own CT2 models). Unlike
# Whisper's model id (resolved automatically via faster_whisper's own HF
# cache lookup), the converted NLLB directory is just a plain filesystem
# path the user points at via NLLB_MODEL_DIR — so "installed" here is a
# direct directory check, not a cache-resolution lookup.
NLLB_MODEL_NAME = "nllb_model"


def _configured_nllb_model_dir() -> str:
    from .pipeline.translate import DEFAULT_NLLB_MODEL_DIR

    return os.environ.get("NLLB_MODEL_DIR", DEFAULT_NLLB_MODEL_DIR)


def _configured_nllb_source_repo() -> str:
    return os.environ.get("NLLB_SOURCE_REPO", "facebook/nllb-200-distilled-600M")


def _configured_nllb_quantization() -> str:
    return os.environ.get("NLLB_QUANTIZATION", "int8")


def _nllb_model_status() -> tuple[bool, Optional[str]]:
    """Direct filesystem check: does the configured NLLB_MODEL_DIR exist
    and look like a converted CTranslate2 model (has config.json and at
    least one model.* weights file)? No cache/registry to consult here —
    unlike Whisper, this path is exactly whatever the user (or a prior
    run of download_nllb_model() below) put there."""
    model_dir = _configured_nllb_model_dir()
    if not os.path.isdir(model_dir):
        return False, None
    has_config = os.path.isfile(os.path.join(model_dir, "config.json"))
    has_weights = any(fn.startswith("model.") for fn in os.listdir(model_dir))
    if has_config and has_weights:
        return True, model_dir
    return False, None


# HachimiMT-60-zh-vi (engine dịch offline thứ hai — TRANSLATE_ENGINE=hachimi
# hoặc chọn theo từng job trên UI). Khác hẳn NLLB ở chỗ repo trên Hugging
# Face ĐÃ có sẵn bản export CTranslate2 int8 trong thư mục con
# 'ct2-int8_float32/', nên ở đây chỉ cần tải snapshot về cache HF là xong:
# không cần ct2-transformers-converter, không cần torch, và nhẹ hơn NLLB
# rất nhiều (model 57M tham số so với 600M).
#
# Việc phân giải đường dẫn (HACHIMI_MODEL_DIR hay cache HF, thư mục con CT2
# nào) nằm hẳn trong translate.py và được import xuống đây, để trạng thái
# "đã cài" luôn nói về đúng thư mục mà translate.py sẽ thực sự load.
HACHIMI_MODEL_NAME = "hachimi_model"


def _configured_hachimi_model_id() -> str:
    from .pipeline.translate import hachimi_model_id

    return hachimi_model_id()


def _hachimi_model_status() -> tuple[bool, Optional[str]]:
    """Kiểm tra không cần mạng: snapshot Hachimi đã có sẵn trong cache HF
    (hoặc ở HACHIMI_MODEL_DIR) chưa. Gọi thẳng resolve_hachimi_dirs() của
    translate.py với local_files_only=True — chính hàm mà _get_hachimi_model()
    dùng — nên không thể lệch nhau giữa 'báo đã cài' và 'load được thật'."""
    try:
        from .pipeline.translate import resolve_hachimi_dirs

        _snapshot_dir, ct2_dir = resolve_hachimi_dirs(local_files_only=True)
        return True, ct2_dir
    except Exception:
        return False, None


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
        "description": "Model Whisper (speech-to-text) — chỉ cần nếu dùng STT_ENGINE=whisper",
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

    nllb_installed, nllb_detail = _nllb_model_status()
    results.append({
        "name": NLLB_MODEL_NAME,
        "kind": "model",
        "description": "Model NLLB-200 (dịch offline, đa ngôn ngữ) — cần nếu chọn engine dịch 'NLLB-200'",
        "installed": nllb_installed,
        "detail": nllb_detail,
        "version": f"{_configured_nllb_source_repo()} ({_configured_nllb_quantization()})",
        "install_hint": (
            "Chưa convert. Cần tải model gốc (~2.4GB) rồi convert sang định dạng "
            "CTranslate2 (~600MB sau khi nén int8) — bước này cần thêm 'torch' đã "
            "cài (thường có sẵn qua funasr); chỉ chạy một lần, tốn vài phút tùy "
            "tốc độ máy/mạng. Kết quả lưu tại "
            f"{_configured_nllb_model_dir()}."
        ),
        "auto_installable": True,
    })

    hachimi_installed, hachimi_detail = _hachimi_model_status()
    results.append({
        "name": HACHIMI_MODEL_NAME,
        "kind": "model",
        "description": (
            "Model HachimiMT-60 (dịch offline Trung → Việt, chuyên truyện mạng) "
            "— cần nếu chọn engine dịch 'Hachimi'"
        ),
        "installed": hachimi_installed,
        "detail": hachimi_detail,
        "version": _configured_hachimi_model_id(),
        "install_hint": (
            "Chưa tải. Chỉ khoảng 100MB và KHÔNG cần bước convert (repo đã có sẵn "
            "bản CTranslate2 int8), cũng không cần 'torch' — tải một lần vào cache "
            "Hugging Face là dùng được ngay."
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


async def download_nllb_model() -> AsyncIterator[str]:
    """Downloads the NLLB-200 checkpoint from Hugging Face and converts it
    to CTranslate2 format via the `ct2-transformers-converter` CLI (a
    console script the `ctranslate2` pip package installs) — a genuinely
    different mechanism from download_whisper_model()'s plain HF download,
    since translate.py's NLLB engine needs the CTranslate2-converted
    format, not the raw HF checkpoint.

    Requires `transformers` and `torch` importable for this one-off
    conversion step (torch is never touched again afterwards — inference
    goes through ctranslate2 only, same as faster-whisper). Raises
    DependencyInstallError with an actionable message if either the
    converter binary or torch isn't available, rather than letting the
    subprocess fail with a confusing traceback.

    On success, sets NLLB_MODEL_DIR in this process's environment so the
    model is immediately usable (once TRANSLATE_ENGINE=nllb is also set)
    without needing a server restart — translate.py's _get_nllb_model()
    reads that env var fresh on first use, not at import time.
    """
    if shutil.which("ct2-transformers-converter") is None:
        raise DependencyInstallError(
            "Chưa có lệnh 'ct2-transformers-converter' (được cài kèm gói "
            "'ctranslate2'). Cài 'ctranslate2' ở mục bên trên trước, rồi thử lại."
        )
    if importlib.util.find_spec("torch") is None:
        raise DependencyInstallError(
            "Bước convert NLLB-200 cần 'torch' đã cài (chỉ dùng một lần cho bước "
            "này, không cần lúc chạy dịch thật). Cài bằng: pip install torch "
            "--index-url https://download.pytorch.org/whl/cpu (hoặc bản có sẵn "
            "nếu máy đã cài qua funasr)."
        )

    source_repo = _configured_nllb_source_repo()
    quantization = _configured_nllb_quantization()
    output_dir = _configured_nllb_model_dir()
    os.makedirs(os.path.dirname(output_dir) or ".", exist_ok=True)

    yield (
        f"Đang tải + convert model '{source_repo}' (quantization={quantization})... "
        f"(vài phút tùy tốc độ mạng/máy)"
    )

    proc = await asyncio.create_subprocess_exec(
        "ct2-transformers-converter",
        "--model", source_repo,
        "--quantization", quantization,
        "--output_dir", output_dir,
        "--force",  # allow re-running over a partial/previous conversion
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
            f"ct2-transformers-converter exited with code {returncode}"
        )

    # Make the model usable immediately in this running process — without
    # this, the person would have to restart the server just to have
    # translate.py's _get_nllb_model() see the directory it just created.
    os.environ["NLLB_MODEL_DIR"] = output_dir

    yield f"Convert xong. Model lưu tại {output_dir}."


async def download_hachimi_model() -> AsyncIterator[str]:
    """Tải snapshot HachimiMT-60-zh-vi từ Hugging Face về cache local.

    Đơn giản hơn download_nllb_model() rất nhiều vì repo đã có sẵn bản
    export CTranslate2 int8 — không phải gọi ct2-transformers-converter,
    không cần 'torch', chỉ là một lần snapshot_download. allow_patterns
    lấy thẳng từ translate.py (HACHIMI_ALLOW_PATTERNS) nên chỉ kéo về đúng
    những file engine thực sự đọc, bỏ qua checkpoint PyTorch gốc.

    snapshot_download() là hàm đồng bộ và không có hook progress theo
    dòng, nên nó chạy trong thread nền còn ở đây yield vài dòng trạng thái
    theo dung lượng cache tăng lên — cùng hình dạng output mà
    install_pip_package() stream ra, đủ để trình duyệt thấy là có đang
    chạy. Chạy lại khi đang tải dở cũng an toàn: HF tự resume.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise DependencyInstallError(
            "Chưa cài 'huggingface_hub' (thường có sẵn kèm 'transformers'). "
            "Cài gói đó ở mục bên trên trước, rồi thử lại."
        ) from e

    from .pipeline.translate import HACHIMI_ALLOW_PATTERNS

    model_id = _configured_hachimi_model_id()
    yield f"Đang tải model '{model_id}' (~100MB, chỉ tải một lần)..."

    errors: list[BaseException] = []
    downloaded_path: list[str] = []

    def _run_download() -> None:
        try:
            downloaded_path.append(
                snapshot_download(model_id, allow_patterns=list(HACHIMI_ALLOW_PATTERNS))
            )
        except BaseException as e:  # surfaced to the caller below
            errors.append(e)

    task = asyncio.create_task(asyncio.to_thread(_run_download))

    last_reported_mb = -1
    try:
        while not task.done():
            await asyncio.sleep(2)
            size_mb = int(_cached_size_bytes(model_id) / (1024 ** 2))
            if size_mb > 0 and size_mb != last_reported_mb:
                last_reported_mb = size_mb
                yield f"Đã tải: {size_mb} MB..."
    except asyncio.CancelledError:
        task.cancel()
        raise

    await task
    if errors:
        raise DependencyInstallError(f"Tải model '{model_id}' thất bại: {errors[0]}")

    # Không cần set env gì cả (khác NLLB): translate.py tự phân giải lại
    # đường dẫn qua cache HF ở lần dịch đầu tiên, nên model dùng được ngay
    # mà không phải restart server.
    yield f"Tải xong. Model lưu tại {downloaded_path[0] if downloaded_path else 'cache Hugging Face'}."


# Dispatch table for main.py's install endpoint: maps a model's `name` to
# the async generator function that downloads/prepares it. Generalizes
# what used to be a Whisper-only hardcoded call, now that a second model
# (NLLB-200) needs a genuinely different download mechanism (convert via
# CLI, not a plain HF download) — same whitelist spirit as
# PIP_PACKAGE_BY_NAME: only names present here are ever actually
# downloadable through the API, and the callable to run is decided
# entirely server-side, never by anything the client sends beyond `name`.
MODEL_DOWNLOADER_BY_NAME: dict[str, Callable[[], AsyncIterator[str]]] = {
    WHISPER_MODEL_NAME: lambda: download_whisper_model(_configured_whisper_model_size()),
    NLLB_MODEL_NAME: download_nllb_model,
    HACHIMI_MODEL_NAME: download_hachimi_model,
}