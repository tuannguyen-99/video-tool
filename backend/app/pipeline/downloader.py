"""
Downloads a single video — Douyin or Bilibili — by URL, using a cookie file
for auth. The platform is auto-detected from the URL's hostname; the UI has
no separate "platform" picker, users just paste whatever link they have.

Two different backends are used per platform, because their tooling needs
differ:

  - Douyin: shelled out to f2 (https://github.com/Johnserf-Seed/f2), driven
    via a per-job YAML config. f2's cookie field wants a raw "k=v; k2=v2"
    cookie header string.

        f2 dy -c <config.yaml> -u <url> -o <download_dir>

  - Bilibili: f2 does NOT actually support Bilibili yet — its README lists
    it as a planned feature for a future release, not something the current
    CLI can do. So Bilibili is downloaded with yt-dlp instead, which has
    solid native bilibili.com support.

        yt-dlp <url> -f "bv*+ba/b" --merge-output-format mp4 -o <template>

    yt-dlp expects cookies in Netscape cookies.txt format (e.g. exported via
    a "Get cookies.txt LOCALLY" browser extension), NOT the raw header
    string f2 wants. Since the UI only has one generic cookie upload field,
    we pass that same file straight to yt-dlp's --cookies flag: if it's a
    valid Netscape file, login-gated/high-quality streams work; if it's a
    raw cookie string (i.e. someone's Douyin cookie), yt-dlp will just
    ignore/fail to parse it gracefully and fall back to whatever bilibili
    exposes to logged-out viewers.

Install requirements: `pip install f2 yt-dlp` and ffmpeg on PATH (yt-dlp
uses it to merge separate video/audio streams into one mp4).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import textwrap
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

Platform = str  # "douyin" | "bilibili"

DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
BILIBILI_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")

# Maps each platform to the CLI binary it shells out to, and the command to
# install it — used to fail fast with an actionable message instead of a
# bare "[Errno 2] No such file or directory" from the OS.
_REQUIRED_BINARIES = {
    "douyin": ("f2", "pip install f2"),
    "bilibili": ("yt-dlp", "pip install yt-dlp"),
}


class DownloadError(Exception):
    pass


class UnsupportedUrlError(DownloadError):
    pass


class MissingDependencyError(DownloadError):
    pass


@dataclass
class DownloadResult:
    video_path: str
    # Original video title/caption in its source language (zh), if it could
    # be recovered. Best-effort: None if the platform's tooling didn't
    # expose it or extraction failed — callers must not treat this as
    # required, the job should still succeed without a title. job_manager
    # translates this to Vietnamese for display; downloader.py itself never
    # translates anything.
    title: Optional[str] = None


def _require_binary(platform: Platform) -> None:
    binary, install_cmd = _REQUIRED_BINARIES[platform]
    if shutil.which(binary) is None:
        raise MissingDependencyError(
            f"Chưa cài '{binary}' trên máy chạy backend (cần cho tải video {platform}). "
            f"Cài bằng lệnh: {install_cmd}"
        )


def detect_platform(url: str) -> Platform:
    """Figures out which site a URL belongs to, purely from its hostname.
    Raises UnsupportedUrlError for anything we don't know how to handle so
    the job fails fast with a clear message instead of a confusing error
    from whatever downloader backend would've been picked."""
    host = (urlparse(url.strip()).hostname or "").lower()

    if any(host == h or host.endswith("." + h) for h in DOUYIN_HOSTS):
        return "douyin"
    if any(host == h or host.endswith("." + h) for h in BILIBILI_HOSTS):
        return "bilibili"

    raise UnsupportedUrlError(
        f"Không nhận diện được nền tảng từ URL: {url!r}. "
        "Hiện chỉ hỗ trợ Douyin và Bilibili."
    )


def _scan_mp4s(work_dir: str) -> set[str]:
    # Both backends may nest output in subfolders (f2 does; yt-dlp usually
    # doesn't but this stays safe either way), so scan recursively.
    return set(glob.glob(os.path.join(work_dir, "**", "*.mp4"), recursive=True))


def _newest_new_file(before: set[str], after: set[str]) -> str | None:
    new_files = list(after - before)
    if new_files:
        return max(new_files, key=os.path.getmtime)
    candidates = sorted(after, key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Douyin (f2)
# ---------------------------------------------------------------------------

def _write_douyin_config(cookie_path: str, download_dir: str, config_path: str) -> None:
    # f2's douyin config format (subset). See f2's docs for the full schema;
    # unknown/extra keys are generally ignored.
    cookie = ""
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            cookie = fh.read().strip()
    except OSError:
        pass

    config = textwrap.dedent(f"""\
        douyin:
            cookie: "{cookie}"
            path: "{download_dir}"
            naming: "{{aweme_id}}_{{desc}}"
            mode: "one"
            music: false
            cover: false
            desc: false
            folderize: false
    """)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(config)


def _parse_douyin_title(video_path: str) -> Optional[str]:
    """Best-effort recovery of the video's original caption/title from its
    filename. f2's naming template above is "{aweme_id}_{desc}" — aweme_id
    is purely numeric, so splitting on the FIRST underscore separates it
    from the description text that follows. f2 sanitizes/truncates {desc}
    for filesystem safety (illegal characters stripped, ~50 chars max — see
    f2's docs), so this is an approximation of the real caption, not an
    exact copy; good enough for a display title, not for anything that
    needs the precise original text. Returns None if nothing usable is left
    after the aweme_id (e.g. the source video had no caption at all)."""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    _aweme_id, _sep, desc = stem.partition("_")
    desc = desc.strip()
    return desc or None


async def _download_douyin(url: str, cookie_path: str, work_dir: str) -> DownloadResult:
    config_path = os.path.join(work_dir, f"f2_config_{uuid.uuid4().hex[:8]}.yaml")
    _write_douyin_config(cookie_path, work_dir, config_path)

    before = _scan_mp4s(work_dir)

    proc = await asyncio.create_subprocess_exec(
        "f2", "dy", "-c", config_path, "-u", url, "-M", "one",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        raise DownloadError(
            f"f2 exited with code {proc.returncode}: {stderr.decode(errors='ignore')[-2000:]}"
        )

    result = _newest_new_file(before, _scan_mp4s(work_dir))
    if result is None:
        raise DownloadError("f2 finished but no .mp4 file was found in the output folder")
    return DownloadResult(video_path=result, title=_parse_douyin_title(result))


# ---------------------------------------------------------------------------
# Bilibili (yt-dlp)
# ---------------------------------------------------------------------------

async def _download_bilibili(url: str, cookie_path: str, work_dir: str) -> DownloadResult:
    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp", url,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        # Writes "<id>.info.json" alongside the video with full metadata
        # (including the real "title" field) — no extra network round trip
        # needed to recover the title separately.
        "--write-info-json",
        "-o", out_template,
    ]
    # Only pass --cookies if a file was actually uploaded; yt-dlp errors out
    # on a missing/empty path instead of silently skipping it.
    if cookie_path and os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        cmd += ["--cookies", cookie_path]

    before = _scan_mp4s(work_dir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        raise DownloadError(
            f"yt-dlp exited with code {proc.returncode}: {stderr.decode(errors='ignore')[-2000:]}"
        )

    result = _newest_new_file(before, _scan_mp4s(work_dir))
    if result is None:
        raise DownloadError("yt-dlp finished but no .mp4 file was found in the output folder")

    title = _read_yt_dlp_title(result)
    return DownloadResult(video_path=result, title=title)


def _read_yt_dlp_title(video_path: str) -> Optional[str]:
    """Best-effort read of the title out of the "<id>.info.json" sidecar
    --write-info-json produces next to the video. Returns None (never
    raises) on anything unexpected — a bad/missing sidecar just means no
    title, not a failed download, since the video itself already succeeded
    by the time this is called."""
    info_json_path = os.path.splitext(video_path)[0] + ".info.json"
    try:
        with open(info_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        title = data.get("title")
        return title.strip() if isinstance(title, str) and title.strip() else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

_DOWNLOADERS = {
    "douyin": _download_douyin,
    "bilibili": _download_bilibili,
}


async def download_video(
    url: str, cookie_path: str, work_dir: str, platform: Platform | None = None
) -> DownloadResult:
    """Downloads one video (Douyin via f2, Bilibili via yt-dlp) and returns
    a DownloadResult with the local mp4 path and, best-effort, the video's
    original (source-language) title. `platform` should normally be passed
    explicitly — the UI now has a platform picker — but falls back to
    auto-detecting from the URL's hostname if omitted."""
    resolved_platform = platform or detect_platform(url)
    _require_binary(resolved_platform)
    os.makedirs(work_dir, exist_ok=True)
    return await _DOWNLOADERS[resolved_platform](url, cookie_path, work_dir)