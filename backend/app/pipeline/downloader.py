"""
Downloads a single video — Douyin or Bilibili — by URL, using a cookie file
for auth. The platform is auto-detected from the URL's hostname; the UI has
no separate "platform" picker, users just paste whatever link they have.

Douyin is shelled out to f2 (https://github.com/Johnserf-Seed/f2), driven
via a per-job YAML config. f2's cookie field wants a raw "k=v; k2=v2"
cookie header string.

    f2 dy -c <config.yaml> -u <url> -o <download_dir>

(f2 does NOT support Bilibili — its README lists that as a planned feature,
not something the current CLI can do — so Bilibili never goes through f2.)

Bilibili has TWO selectable engines (BILIBILI_ENGINE env var, or the
`bilibili_engine` param threaded per-job from the UI — same pattern as
translate.py's TRANSLATE_ENGINE):

  ytdlp (default) — yt-dlp, solid general bilibili.com support:

        yt-dlp <url> -f "bv*+ba/b" --merge-output-format mp4 -o <template>

    Known weak spot: Bilibili's CDN sometimes routes a download to a PCDN
    (peer-cache) mirror that only works reliably from mainland-China IPs —
    surfaces as yt-dlp's "Got error: X bytes read, Y more expected", not a
    bug in this app (see yt-dlp issues #14498, #12421). --http-chunk-size
    below limits the blast radius of a cut connection but can't fix a
    mirror that's simply unreachable from this server's network.

  yutto (https://github.com/yutto-dev/yutto) — a Bilibili-only downloader
    that, unlike yt-dlp, tries Bilibili's other listed CDN mirrors and can
    be told to skip a specific bad one outright via --banned-mirrors-pattern
    (BILIBILI_YUTTO_BANNED_MIRRORS_PATTERN below) instead of only ever
    hitting whichever mirror the API happened to list first. Worth trying
    when a specific video keeps failing on ytdlp with the CDN error above.
    Install into an ISOLATED environment (pipx install yutto, or a venv
    dedicated to it) rather than the app's own venv — yutto pulls
    pydantic/websockets versions that conflict with f2's pinned ones, and
    since it's invoked as a subprocess here (not imported), it never needs
    to share an environment with the rest of this app. Point
    YUTTO_BIN at that isolated install's binary if it's not the first
    "yutto" on this process's PATH (e.g. "/opt/yutto-venv/bin/yutto" —
    pipx already puts its shim on PATH, so YUTTO_BIN is usually unnecessary
    with a pipx install).

Both yt-dlp and yutto want cookies in different shapes, and neither matches
f2's raw-header-string format: yt-dlp wants a Netscape cookies.txt file
(exported via e.g. a "Get cookies.txt LOCALLY" browser extension); yutto
wants just the SESSDATA cookie's value, extracted and re-wrapped into its
own "SESSDATA=<value>" inline string for --auth (see _download_bilibili_yutto).
The UI only has one generic cookie upload field, so _extract_sessdata()
below picks the SESSDATA value out of whichever shape was actually uploaded
(Netscape TSV, or a raw "k=v; k2=v2" header string), while yt-dlp keeps
getting the raw file path via --cookies as before.

Install requirements: `pip install f2 yt-dlp` (and `pipx install yutto` if
using that engine) and ffmpeg on PATH.

PROGRESS REPORTING: all three backends report progress now, via two
different techniques depending on why each one's plain-subprocess output
was unparseable to begin with:

  - Douyin (f2) and Bilibili/yutto both render their progress bars via
    `rich`, which — like pip, npm, and most `rich`/`tqdm`-based CLIs —
    only does live redraws when it detects stdout is an interactive
    terminal. Piped through a plain subprocess, it silently drops those
    redraws and prints little to nothing until it's done, so there'd be
    nothing to parse for progress. _download_douyin and
    _download_bilibili_yutto both work around this by running their CLI
    inside a pseudo-terminal (pty) instead — see _run_pty_with_progress()
    — so it renders exactly as it would interactively, and pulls whatever
    "NN%" is currently showing out of that raw output. Caveat: yutto
    downloads video and audio as two separate tasks (then muxes them),
    each with its own 0-100% bar, so the reported number will visibly
    reset partway through rather than climbing smoothly from 0 to 100 in
    one pass — still far more useful than no feedback at all, just not a
    perfectly linear bar. f2 doesn't have that particular caveat (one bar
    per download), but its exact rendered format isn't pinned to a fixed
    string either way, so the same generic "grab the last NN%" parsing is
    used rather than something f2-specific that could silently stop
    matching after an update.

  - Bilibili/yt-dlp doesn't need a pty: passed --newline, yt-dlp itself
    terminates each progress update with a real "\n" instead of
    overwriting the same line via "\r", so a plain pipe already yields
    one parseable "[download]  NN% of ..." line per update — see
    _run_with_line_progress(). No pty trick needed because yt-dlp's own
    "am I a terminal" check only controls \r vs \n, not whether it prints
    progress at all.
"""
from __future__ import annotations

import asyncio
import fcntl
import glob
import json
import os
import pty
import re
import shutil
import struct
import termios
import uuid

import yaml
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

Platform = str  # "douyin" | "bilibili"
BilibiliEngine = str  # "ytdlp" | "yutto"

DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
BILIBILI_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")

BILIBILI_ENGINE_YTDLP = "ytdlp"
BILIBILI_ENGINE_YUTTO = "yutto"
SUPPORTED_BILIBILI_ENGINES = (BILIBILI_ENGINE_YTDLP, BILIBILI_ENGINE_YUTTO)


def default_bilibili_engine() -> BilibiliEngine:
    """Server-wide default engine for Bilibili, read fresh from env each
    call (not cached at import) so changing it takes effect without a
    restart — same reasoning as translate.py's default_engine()."""
    engine = os.environ.get("BILIBILI_ENGINE", BILIBILI_ENGINE_YUTTO).strip().lower()
    if engine not in SUPPORTED_BILIBILI_ENGINES:
        raise DownloadError(
            f"BILIBILI_ENGINE={engine!r} không hợp lệ — chỉ hỗ trợ "
            f"{' hoặc '.join(repr(e) for e in SUPPORTED_BILIBILI_ENGINES)}."
        )
    return engine


def _resolve_bilibili_engine(engine: Optional[BilibiliEngine]) -> BilibiliEngine:
    if engine is None:
        return default_bilibili_engine()
    normalized = engine.strip().lower()
    if normalized not in SUPPORTED_BILIBILI_ENGINES:
        raise DownloadError(
            f"Engine tải Bilibili {engine!r} không hợp lệ — chỉ hỗ trợ "
            f"{' hoặc '.join(repr(e) for e in SUPPORTED_BILIBILI_ENGINES)}."
        )
    return normalized


# Maps each (platform, engine) pair to the CLI binary it shells out to, and
# the install command — used to fail fast with an actionable message
# instead of a bare "[Errno 2] No such file or directory" from the OS.
# The Bilibili binary depends on which engine is selected, resolved via
# _binary_for() below rather than being a single static entry.
_REQUIRED_BINARIES = {
    "douyin": ("f2", "pip install f2"),
}
_BILIBILI_BINARIES = {
    BILIBILI_ENGINE_YTDLP: ("yt-dlp", "pip install yt-dlp"),
    # Default binary name assumes a pipx install (puts its shim on PATH
    # under the plain "yutto" name); override via YUTTO_BIN if yutto lives
    # in a separately-managed venv not already on this process's PATH —
    # see the module docstring for why it's kept in its own environment.
    BILIBILI_ENGINE_YUTTO: (
        os.environ.get("YUTTO_BIN", "yutto"),
        "pipx install yutto  (or: python3 -m venv <dir> && <dir>/bin/pip install yutto, "
        "then set YUTTO_BIN=<dir>/bin/yutto)",
    ),
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


def _require_binary(platform: Platform, bilibili_engine: BilibiliEngine = BILIBILI_ENGINE_YTDLP) -> None:
    if platform == "bilibili":
        binary, install_cmd = _BILIBILI_BINARIES[bilibili_engine]
    else:
        binary, install_cmd = _REQUIRED_BINARIES[platform]

    # shutil.which() also accepts an absolute path (e.g. YUTTO_BIN pointing
    # into an isolated venv) and just checks that exact file is executable,
    # so this one call covers both "plain name resolved via PATH" and
    # "explicit path" without extra branching.
    if shutil.which(binary) is None:
        raise MissingDependencyError(
            f"Chưa cài '{binary}' trên máy chạy backend (cần cho tải video {platform}"
            f"{f', engine {bilibili_engine!r}' if platform == 'bilibili' else ''}). "
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

def _cookie_file_to_header_string(cookie_path: str) -> str:
    """Normalizes whatever shape the UI's generic cookie upload actually
    contains into the single-line "k1=v1; k2=v2" HTTP header string that
    f2's config `cookie` field expects — f2 cannot use a Netscape
    cookies.txt file directly, unlike yt-dlp (--cookies wants that file's
    *path*) or yutto (wants just SESSDATA's value, see _extract_sessdata).
    A raw Netscape file (tab-separated, multi-line) read straight through
    no longer breaks the YAML file itself now that _write_douyin_config
    serializes via PyYAML, but the *cookie value* f2 ends up with is still
    that same multi-line garbage — f2 (or the HTTP library underneath it)
    then rejects it when trying to actually send it as a header, which is
    a different failure than the YAML one but just as broken. Handles
    both shapes a user might upload (same two shapes _extract_sessdata()
    handles for Bilibili, generalized here to every cookie in the file
    instead of pulling out just SESSDATA):

      - Netscape cookies.txt: tab-separated, 7 fields per line. Lines
        prefixed "#HttpOnly_" are real cookie data (HttpOnly cookies, per
        the Netscape-file convention used by cookie-export extensions),
        not comments, even though they start with "#" like one — only
        genuine comment lines get skipped.
      - An already-raw "k1=v1; k2=v2" header string: returned basically
        as-is, just collapsed onto one line in case it was saved/pasted
        with wrapping.

    Returns "" (never raises) on a missing/unreadable file — f2 then just
    runs unauthenticated rather than failing the whole job over it.
    """
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return ""

    _HTTPONLY_PREFIX = "#HttpOnly_"
    pairs: list[str] = []
    looks_like_netscape = False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        candidate = line
        if candidate.startswith(_HTTPONLY_PREFIX):
            candidate = candidate[len(_HTTPONLY_PREFIX):]
        elif candidate.startswith("#"):
            continue  # a genuine comment line
        fields = candidate.split("\t")
        if len(fields) >= 7:
            looks_like_netscape = True
            name, value = fields[5], fields[6].strip()
            if name:
                pairs.append(f"{name}={value}")

    if looks_like_netscape:
        return "; ".join(pairs)

    # Not a Netscape file — treat as an already-raw header string.
    return " ".join(content.split())


def _write_douyin_config(cookie_path: str, download_dir: str, config_path: str) -> None:
    # Built as a plain dict and serialized via PyYAML rather than a
    # hand-formatted f-string/textwrap.dedent template — that guarantees
    # valid, correctly-escaped YAML no matter what the cookie string (or
    # download_dir path) happens to contain, which string-templating
    # can't reliably promise. Field set matches f2's documented "自定义配置
    # 文件" (custom config, the kind passed via -c — see f2.wiki/site-
    # config) schema in full, rather than a trimmed-down subset: f2
    # 0.0.1.6+ validates this config strictly enough that missing fields
    # from that schema can fail outright with a bare "配置文件解析错误"
    # instead of silently defaulting them the way older f2 did.
    cookie = _cookie_file_to_header_string(cookie_path) if cookie_path else ""

    config = {
        "douyin": {
            "url": "",
            "cookie": cookie,
            "naming": "{aweme_id}_{desc}",
            "path": download_dir,
            "mode": "one",
            "music": False,
            "cover": False,
            "desc": False,
            "folderize": False,
            "lyric": False,
            "interval": "all",
            "timeout": 10,
            "max_retries": 5,
            "max_connections": 5,
            "max_counts": 0,
            "max_tasks": 10,
            "page_counts": 20,
            "languages": None,
        }
    }
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)


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


async def _download_douyin(
    url: str,
    cookie_path: str,
    work_dir: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> DownloadResult:
    # f2 renders its own progress bar via `rich` too (same library yutto
    # uses — f2's changelog bumps its rich pin explicitly), so it has the
    # exact same "only live-redraws on a real terminal" problem described
    # in the module docstring's PROGRESS REPORTING section. Routed through
    # _run_pty_with_progress (defined further down, alongside yutto's use
    # of it) for the same reason: a pty makes f2 think it's talking to an
    # interactive terminal, so its bar actually renders instead of staying
    # silent until the process exits.
    config_path = os.path.join(work_dir, f"f2_config_{uuid.uuid4().hex[:8]}.yaml")
    _write_douyin_config(cookie_path, work_dir, config_path)

    before = _scan_mp4s(work_dir)

    returncode, output = await _run_pty_with_progress(
        ["f2", "dy", "-c", config_path, "-u", url, "-M", "one"],
        on_progress,
    )

    if returncode != 0:
        raise DownloadError(
            f"f2 exited with code {returncode}: {output.decode(errors='ignore')[-2000:]}"
        )

    result = _newest_new_file(before, _scan_mp4s(work_dir))
    if result is None:
        raise DownloadError(
            "f2 finished but no .mp4 file was found in the output folder. "
            f"f2's own output (may say why — login required, video removed, "
            f"already downloaded elsewhere, etc.): "
            f"{output.decode(errors='ignore')[-2000:]}"
        )
    return DownloadResult(video_path=result, title=_parse_douyin_title(result))


def _extract_sessdata(cookie_path: str) -> Optional[str]:
    """Pulls just the SESSDATA cookie's value out of whatever the UI's
    generic cookie upload actually contains — yutto's --auth flag wants
    that value wrapped as "SESSDATA=<value>", unlike yt-dlp (whole Netscape
    file path) or f2 (raw "k=v; k2=v2" header string, which it also happens
    to embed SESSDATA in). Handles both shapes users might have uploaded:

      - Netscape cookies.txt: tab-separated, 7 fields per line
        (domain, flag, path, secure, expiration, name, value). Bilibili
        marks SESSDATA HttpOnly, and cookie-export extensions (e.g. "Get
        cookies.txt LOCALLY") represent that per the Netscape-file
        convention by prefixing the domain field with "#HttpOnly_" —
        that's real cookie data, not a comment, even though it starts
        with "#" like one. Only skip lines that are genuinely commented
        out; unwrap "#HttpOnly_" ones and keep parsing them.
      - A raw header string (e.g. someone's f2 cookie): "k1=v1; k2=v2".

    Returns None (never raises) if the file is missing/empty or SESSDATA
    isn't present in either shape — the caller falls back to an
    unauthenticated download rather than failing the whole job over an
    optional cookie.
    """
    if not cookie_path or not os.path.exists(cookie_path) or os.path.getsize(cookie_path) == 0:
        return None

    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return None

    _HTTPONLY_PREFIX = "#HttpOnly_"
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_HTTPONLY_PREFIX):
            line = line[len(_HTTPONLY_PREFIX):]
        elif line.startswith("#"):
            continue  # a genuine comment line
        fields = line.split("\t")
        if len(fields) >= 7 and fields[5] == "SESSDATA":
            value = fields[6].strip()
            return value or None

    # Not a Netscape file (or SESSDATA wasn't in it) — try the raw
    # "k=v; k2=v2" header-string shape instead.
    if match := re.search(r"(?:^|[;\s])SESSDATA=([^;\s]+)", content):
        return match.group(1)

    return None


# ---------------------------------------------------------------------------
# Bilibili — yutto engine
# ---------------------------------------------------------------------------

# Matches "45%", "12.3 %", etc. — deliberately not anchored to any specific
# surrounding text, since we just want whatever percentage figure is
# currently visible in the progress bar's rendered output.
_PERCENT_RE = re.compile(rb"(\d{1,3}(?:\.\d+)?)\s*%")


async def _run_pty_with_progress(
    cmd: list[str],
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[int, bytes]:
    """Runs `cmd` attached to a pseudo-terminal instead of a plain pipe, and
    calls `on_progress(pct)` (0-100) every time the live output's currently
    displayed percentage changes. Returns (returncode, full raw output) —
    same "raise on nonzero" handling as the plain subprocess calls
    elsewhere in this file is left to the caller, so error messages here
    can quote from the actual captured output either way.

    WHY A PTY: see the module docstring's "PROGRESS REPORTING" section —
    in short, `rich` (which yutto's progress bar is built on) only live-
    redraws when stdout looks like a real terminal; a pty is what makes it
    think that's the case.
    """
    master_fd, slave_fd = pty.openpty()
    # A narrow-ish fixed size keeps rich's layout compact and predictable
    # regardless of whatever terminal (if any) this server process itself
    # happens to be running under.
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
    except OSError:
        pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=slave_fd,
        stderr=slave_fd,
        stdin=asyncio.subprocess.DEVNULL,
    )
    os.close(slave_fd)  # only the child needs the slave end from here on

    loop = asyncio.get_event_loop()
    chunks: list[bytes] = []
    last_reported: Optional[float] = None
    done = asyncio.Event()

    def _on_readable() -> None:
        nonlocal last_reported
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            # EIO here means the child closed its end (process exited) —
            # the normal way a pty master signals EOF, not a real error.
            data = b""
        if not data:
            loop.remove_reader(master_fd)
            done.set()
            return
        chunks.append(data)
        if on_progress is None:
            return
        # A single read() can contain several \r-redraws; only the LAST
        # percentage in this chunk reflects the bar's current state.
        matches = _PERCENT_RE.findall(data)
        if not matches:
            return
        try:
            pct = float(matches[-1])
        except ValueError:
            return
        pct = max(0.0, min(100.0, pct))
        if pct != last_reported:
            last_reported = pct
            on_progress(pct)

    loop.add_reader(master_fd, _on_readable)
    try:
        await proc.wait()
        # Give the pty a moment to flush whatever it still has buffered
        # after the child exits, instead of racing _on_readable for it.
        try:
            await asyncio.wait_for(done.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    finally:
        loop.remove_reader(master_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass

    return proc.returncode, b"".join(chunks)


async def _download_bilibili_yutto(
    url: str,
    cookie_path: str,
    work_dir: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> DownloadResult:
    """Downloads via yutto instead of yt-dlp. Deliberately NOT using
    yutto's -b/--batch mode (which would fetch every part of a multi-P
    video in one call): this function's contract, like the yt-dlp path and
    like Douyin's f2 path, is one call in, one DownloadResult out. Batch
    mode would produce several .mp4s per call, and _newest_new_file()
    would silently keep only the last one — worse than just downloading
    the single part the given URL actually points at (page 1, or whichever
    page a "?p=N" in the URL selects). To process every part of a
    multi-P video, call this once per part with each part's own "?p=N"
    URL — that's a job_manager-level concern, not this function's.
    """
    binary = _BILIBILI_BINARIES[BILIBILI_ENGINE_YUTTO][0]

    cmd = [
        binary, "download", url,
        "-d", work_dir,
        # yt-dlp's --merge-output-format equivalent; "infer" (yutto's own
        # default) would leave e.g. a lone audio-only stream as .m4a
        # instead of a scannable .mp4, which _scan_mp4s() wouldn't find.
        "--output-format", "mp4",
    ]

    sessdata = _extract_sessdata(cookie_path)
    if sessdata:
        # Recent yutto (the CLI rewrite that added `yutto auth login` /
        # auth.toml profiles) dropped the old bare-value -c/--sessdata flag
        # in favor of --auth, which takes an inline cookie-header-style
        # string ("SESSDATA=xxx" — same "k=v; k2=v2" shape as f2's cookie
        # field above, parsed via yutto's own parse_auth_inline) rather than
        # a bare SESSDATA value on its own. Passing the old bare value via
        # -c here is silently not recognized as auth by current yutto — it
        # just proceeds logged-out (see yutto's own "未提供登录认证信息…请通过
        # --auth 参数提供认证信息" message when that happens), so this must
        # stay --auth "SESSDATA=..." going forward, not -c.
        cmd += ["--auth", f"SESSDATA={sessdata}"]

    # Lets a specific known-bad Bilibili CDN mirror be excluded without a
    # code change, once BILIBILI_ENGINE_YUTTO_BANNED_MIRRORS_PATTERN is
    # actually set to that mirror's hostname — see the module docstring.
    # This is yutto's own mechanism for the "backup CDN mirror" case yt-dlp
    # (--http-chunk-size below) can only retry into, not route around.
    if banned_pattern := os.environ.get("BILIBILI_YUTTO_BANNED_MIRRORS_PATTERN"):
        cmd += ["--banned-mirrors-pattern", banned_pattern]

    before = _scan_mp4s(work_dir)

    returncode, output = await _run_pty_with_progress(cmd, on_progress)

    if returncode != 0:
        raise DownloadError(
            f"yutto exited with code {returncode}: {output.decode(errors='ignore')[-2000:]}"
        )

    result = _newest_new_file(before, _scan_mp4s(work_dir))
    if result is None:
        raise DownloadError(
            "yutto finished but no .mp4 file was found in the output folder. "
            f"yutto's own output (may say why — login/VIP-only video, region "
            f"lock, URL needs --batch for a multi-part/anthology page, etc.): "
            f"{output.decode(errors='ignore')[-2000:]}"
        )

    # yutto's default subpath template for a single (non-batch) UGC video
    # is bare "{title}" — the file name itself IS the original title
    # (filesystem-sanitized), no separate metadata sidecar needed. More
    # exact than f2's Douyin approach (which truncates {desc} to ~50
    # chars), though still an approximation wherever sanitization changed
    # a character.
    title = os.path.splitext(os.path.basename(result))[0].strip() or None
    return DownloadResult(video_path=result, title=title)


# ---------------------------------------------------------------------------
# Bilibili (yt-dlp)
# ---------------------------------------------------------------------------

async def _run_with_line_progress(
    cmd: list[str],
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[int, bytes]:
    """Runs `cmd` as a plain (non-pty) subprocess and calls `on_progress(pct)`
    every time a newly read stdout line shows a changed "NN%". Returns
    (returncode, combined stdout+stderr) — same "raise on nonzero" handling
    as the other subprocess calls in this file is left to the caller.

    Unlike f2/yutto (see _run_pty_with_progress above), yt-dlp doesn't need
    a pty to produce parseable progress: called with --newline (added by
    _download_bilibili below), it terminates each progress update with a
    real "\\n" instead of overwriting the same line via "\\r", so a plain
    pipe already yields one line per update — readline() alone is enough.
    PYTHONUNBUFFERED is set anyway, out of the same caution as
    subtitle_remover.py's _run_with_progress: costs nothing even if
    yt-dlp's own flushing already makes it unnecessary.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    last_reported: Optional[float] = None

    async def _drain_stdout() -> None:
        nonlocal last_reported
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            stdout_chunks.append(line)
            if on_progress is None:
                continue
            match = _PERCENT_RE.search(line)
            if not match:
                continue
            try:
                pct = float(match.group(1))
            except ValueError:
                continue
            pct = max(0.0, min(100.0, pct))
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

    return returncode, b"".join(stdout_chunks) + b"".join(stderr_chunks)


async def _download_bilibili(
    url: str,
    cookie_path: str,
    work_dir: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> DownloadResult:
    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")

    cmd = [
        "yt-dlp", url,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        # Forces each progress update onto its own "\n"-terminated line
        # instead of yt-dlp's default same-line "\r" overwrite — see
        # _run_with_line_progress()/the module docstring's PROGRESS
        # REPORTING section for why that's what makes it parseable at all
        # over a plain pipe.
        "--newline",
        # Writes "<id>.info.json" alongside the video with full metadata
        # (including the real "title" field) — no extra network round trip
        # needed to recover the title separately.
        "--write-info-json",
        # Bilibili's CDN frequently cuts off a long single-stream download
        # mid-transfer (surfaces as yt-dlp's "Got error: X bytes read, Y
        # more expected" — a known, common yt-dlp+bilibili issue, not a
        # bug in this app). Without --http-chunk-size, yt-dlp fetches the
        # whole file as one HTTP request; a cut connection means the next
        # retry re-downloads the entire multi-hundred-MB file from byte
        # zero, and with a flaky connection that can burn through all 10
        # default retries before ever finishing. --http-chunk-size splits
        # the download into bounded Range-request chunks instead, so a
        # dropped connection only costs re-fetching that one chunk.
        # Raising --retries/--fragment-retries on top gives it more
        # attempts per chunk too, since each chunk is now a much cheaper
        # unit of work to retry.
        "--http-chunk-size", "10M",
        "--retries", "20",
        "--fragment-retries", "20",
        "-o", out_template,
    ]
    # Only pass --cookies if a file was actually uploaded; yt-dlp errors out
    # on a missing/empty path instead of silently skipping it.
    if cookie_path and os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        cmd += ["--cookies", cookie_path]

    before = _scan_mp4s(work_dir)

    returncode, output = await _run_with_line_progress(cmd, on_progress)

    if returncode != 0:
        raise DownloadError(
            f"yt-dlp exited with code {returncode}: {output.decode(errors='ignore')[-2000:]}"
        )

    result = _newest_new_file(before, _scan_mp4s(work_dir))
    if result is None:
        raise DownloadError(
            "yt-dlp finished but no .mp4 file was found in the output folder. "
            f"yt-dlp's own output (may say why): "
            f"{output.decode(errors='ignore')[-2000:]}"
        )

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
}
_BILIBILI_DOWNLOADERS = {
    BILIBILI_ENGINE_YTDLP: _download_bilibili,
    BILIBILI_ENGINE_YUTTO: _download_bilibili_yutto,
}


async def download_video(
    url: str,
    cookie_path: str,
    work_dir: str,
    platform: Platform | None = None,
    bilibili_engine: BilibiliEngine | None = None,
    # Called with a 0-100 float as the download progresses. Implemented for
    # all three backends (Douyin/f2, Bilibili/ytdlp, Bilibili/yutto) — see
    # the module docstring's "PROGRESS REPORTING" section for how each one
    # gets there. Safe to pass unconditionally either way: a backend with
    # nothing to report would just never call it.
    on_progress: Optional[Callable[[float], None]] = None,
) -> DownloadResult:
    """Downloads one video (Douyin via f2; Bilibili via yt-dlp or yutto —
    see `bilibili_engine`) and returns a DownloadResult with the local mp4
    path and, best-effort, the video's original (source-language) title.

    `platform` should normally be passed explicitly — the UI now has a
    platform picker — but falls back to auto-detecting from the URL's
    hostname if omitted. `bilibili_engine` is ignored for Douyin; for
    Bilibili it selects "ytdlp" (default) or "yutto" per job, falling back
    to the server-wide BILIBILI_ENGINE env var if omitted — same pattern as
    translate.py's `engine` param.
    """
    resolved_platform = platform or detect_platform(url)

    if resolved_platform == "bilibili":
        resolved_engine = _resolve_bilibili_engine(bilibili_engine)
        _require_binary(resolved_platform, resolved_engine)
        os.makedirs(work_dir, exist_ok=True)
        return await _BILIBILI_DOWNLOADERS[resolved_engine](url, cookie_path, work_dir, on_progress)

    _require_binary(resolved_platform)
    os.makedirs(work_dir, exist_ok=True)
    return await _DOWNLOADERS[resolved_platform](url, cookie_path, work_dir, on_progress)