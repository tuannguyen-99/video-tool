"""
Wraps f2 (https://github.com/Johnserf-Seed/f2) to download a single Douyin
video by URL, using a cookie file for auth.

f2 is primarily driven as a CLI tool with a YAML config file, so the most
reliable integration is to shell out to it rather than reach into internals
that can change between versions. We generate a small per-job YAML config,
point it at the cookie file the user uploaded, and run:

    f2 dy -c <config.yaml> -u <url> -o <download_dir>

If your installed f2 version exposes a stable Python API instead
(f2.apps.douyin.handler), swap the implementation of `download_video` below
for a direct call — everything else in the pipeline only depends on this
function returning the path to the downloaded mp4.
"""
from __future__ import annotations

import asyncio
import glob
import os
import textwrap
import uuid


class DownloadError(Exception):
    pass


def _write_f2_config(cookie_path: str, download_dir: str, config_path: str) -> None:
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
            naming: "{{aweme_id}}"
            mode: "one"
            music: false
            cover: false
            desc: false
            folderize: false
    """)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(config)


async def download_video(url: str, cookie_path: str, work_dir: str) -> str:
    """Downloads one Douyin video and returns the local mp4 path."""
    os.makedirs(work_dir, exist_ok=True)
    config_path = os.path.join(work_dir, f"f2_config_{uuid.uuid4().hex[:8]}.yaml")
    _write_f2_config(cookie_path, work_dir, config_path)

    # f2 always nests its output under <path>/douyin/<mode>/<nickname>/...,
    # even with folderize disabled, so the scan must be recursive.
    def _scan_mp4s() -> set[str]:
        return set(glob.glob(os.path.join(work_dir, "**", "*.mp4"), recursive=True))

    before = _scan_mp4s()

    proc = await asyncio.create_subprocess_exec(
        "f2", "dy", "-c", config_path, "-u", url, "-M", "one",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        raise DownloadError(
            f"f2 exited with code {proc.returncode}: {stderr.decode(errors='ignore')[-2000:]}"
        )

    after = _scan_mp4s()
    new_files = list(after - before)
    if not new_files:
        # Fall back to newest mp4 in the folder if diffing failed to catch it
        candidates = sorted(after, key=os.path.getmtime, reverse=True)
        if not candidates:
            raise DownloadError("f2 finished but no .mp4 file was found in the output folder")
        return candidates[0]

    return max(new_files, key=os.path.getmtime)