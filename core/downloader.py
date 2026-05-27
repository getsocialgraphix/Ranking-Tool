"""
Download remote clips via our own TikTok downloader or yt-dlp.

TikTok pipeline (talks directly to TikTok — no third-party services):
  1. Resolve shortened URL via GET redirect  (vm.tiktok.com → full URL)
  2. Extract numeric video ID from the resolved URL
  3. Method A – SIGI_STATE  page scrape  (current TikTok structure)
     Method A2 – __NEXT_DATA__ scrape    (older TikTok structure, fallback)
     Method A3 – raw JSON regex          (catch-all page scrape)
  4. Method B – TikTok mobile feed API   (Android app endpoint)
  5. Method C – yt-dlp with browser cookies (NoSessionContext fix)
  6. Method D – yt-dlp without cookies   (last resort)

All other platforms (YouTube, Instagram, etc.) go straight to yt-dlp.
"""

import glob
import json
import re
import random
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
import yt_dlp

from config import FFMPEG_BIN, FFPROBE_BIN, TEMP_DIR

DOWNLOAD_DIR = TEMP_DIR / "downloaded"


def _resolve_ffmpeg_dir() -> str:
    """
    Return the directory that contains the ffmpeg binary so yt-dlp can find it.

    When FFMPEG_BIN is an absolute path (Windows custom install) use its parent.
    When FFMPEG_BIN is just 'ffmpeg' (Linux/PATH fallback) locate the binary via
    shutil.which() so yt-dlp gets a real directory instead of '.' (current dir),
    which would cause yt-dlp post-processing to silently fail.
    """
    p = Path(FFMPEG_BIN)
    if p.is_absolute() and p.parent != Path("."):
        return str(p.parent)
    found = shutil.which("ffmpeg")
    return str(Path(found).parent) if found else ""


_FFMPEG_DIR = _resolve_ffmpeg_dir()

# ── User-agent strings ────────────────────────────────────────────────────────
_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
_UA_ANDROID = (
    "com.ss.android.ugc.trill/2022600030 "
    "(Linux; U; Android 12; en_US; Pixel 6; Build/SQ3A.220705.004;"
    " Cronet/58.0.2991.0)"
)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    path: str = ""
    source_url: str = ""
    original_width: int = 0
    original_height: int = 0
    duration_s: float = 0.0
    error: Optional[str] = None


# ── FFprobe ───────────────────────────────────────────────────────────────────

def _probe(path: str) -> dict:
    cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "v:0",
        "-show_format", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {}
    data    = json.loads(r.stdout)
    streams = data.get("streams", [])
    fmt     = data.get("format", {})
    if not streams:
        return {}
    s = streams[0]
    return {
        "width":    int(s.get("width", 0)),
        "height":   int(s.get("height", 0)),
        "duration": float(s.get("duration") or fmt.get("duration") or 0),
    }


# ── Shared helpers ────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _strip_ansi(t: str) -> str:
    return _ANSI_RE.sub("", t)

def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()

def _find_output(index: int) -> Optional[Path]:
    prefix = f"clip_{index:02d}"
    exact  = DOWNLOAD_DIR / f"{prefix}.mp4"
    if exact.exists() and exact.stat().st_size > 0:
        return exact
    matches: list[Path] = []
    for p in glob.glob(str(DOWNLOAD_DIR / f"{prefix}.*")):
        path = Path(p)
        if path.suffix.lower() in (".part", ".ytdl", ".tmp"):
            continue
        if "." in path.stem:          # format-numbered intermediate
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        matches.append(path)
    if not matches:
        return None
    for m in matches:
        if m.suffix.lower() == ".mp4":
            return m
    return matches[0]

def _purge_stale(index: int) -> None:
    for p in glob.glob(str(DOWNLOAD_DIR / f"clip_{index:02d}.*")):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass

def _stream_save(
    cdn_url: str,
    dest: Path,
    extra_headers: dict,
    index: int,
    progress_cb: Optional[Callable[[str], None]],
) -> bool:
    headers = {
        "User-Agent": _UA_MOBILE,
        "Referer":    "https://www.tiktok.com/",
        "Accept":     "*/*",
        **extra_headers,
    }
    with requests.get(cdn_url, headers=headers, stream=True, timeout=120) as resp:
        if resp.status_code == 403:
            return False
        resp.raise_for_status()
        total   = int(resp.headers.get("content-length", 0))
        written = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    if progress_cb and total:
                        progress_cb(f"  Clip {index + 1}: {written / total * 100:.1f}%")
    return dest.exists() and dest.stat().st_size > 0

def _clean_cdn_url(raw: str) -> str:
    """Decode JSON unicode escapes and tidy slashes."""
    try:
        # Handle / → / and similar escapes
        raw = raw.encode("utf-8").decode("unicode_escape")
    except Exception:
        pass
    raw = raw.replace("\\/", "/")
    return raw.strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  TikTok downloader  —  pure Python, talks directly to TikTok's servers
# ═══════════════════════════════════════════════════════════════════════════════

class TikTokDownloader:
    """
    Downloads TikTok videos without any third-party download service.

    Method A  – SIGI_STATE page scrape    (TikTok's current page structure)
    Method A2 – __NEXT_DATA__ page scrape (older TikTok pages)
    Method A3 – raw JSON regex            (catch-all for other JSON layouts)
    Method B  – TikTok mobile feed API   (Android app endpoint)
    """

    # ── URL resolution ────────────────────────────────────────────────────────

    @staticmethod
    def resolve_url(url: str) -> str:
        """
        Follow HTTP redirects to expand short links
        (vm.tiktok.com, vt.tiktok.com, tiktok.com/t/…)
        into the canonical /@user/video/ID form.
        Uses GET (not HEAD) because TikTok ignores HEAD redirects.
        """
        if re.search(r"(vm|vt)\.tiktok\.com|tiktok\.com/t/", url):
            try:
                r = requests.get(
                    url, allow_redirects=True, timeout=10,
                    headers={"User-Agent": _UA_MOBILE},
                    stream=True,       # don't download body
                )
                r.close()
                return r.url
            except Exception:
                pass
        return url

    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        m = re.search(r"/video/(\d+)", url)
        return m.group(1) if m else None

    # ── Method A: SIGI_STATE scrape (current TikTok 2024+) ───────────────────

    @staticmethod
    def _parse_sigi_state(html: str) -> Optional[str]:
        m = re.search(
            r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return None
        try:
            sigi = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        # ItemModule is a dict keyed by video ID
        for item in sigi.get("ItemModule", {}).values():
            video = item.get("video", {})
            cdn = video.get("downloadAddr") or video.get("playAddr")
            if cdn:
                return _clean_cdn_url(cdn)

        # VideoPage layout (another SIGI_STATE variant)
        vp = sigi.get("VideoPage", {})
        for item in vp.get("itemInfo", {}).values():
            video = item.get("video", {})
            cdn = video.get("downloadAddr") or video.get("playAddr")
            if cdn:
                return _clean_cdn_url(cdn)

        return None

    # ── Method A2: __NEXT_DATA__ scrape (older TikTok pages) ─────────────────

    @staticmethod
    def _parse_next_data(html: str) -> Optional[str]:
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        # Standard path
        try:
            item  = (data["props"]["pageProps"]["itemInfo"]["itemStruct"])
            video = item["video"]
            cdn   = video.get("downloadAddr") or video.get("playAddr")
            if cdn:
                return _clean_cdn_url(cdn)
        except (KeyError, TypeError):
            pass

        return None

    # ── Method A3: raw JSON regex (catch-all) ─────────────────────────────────

    @staticmethod
    def _parse_raw_json(html: str) -> Optional[str]:
        for key in ("downloadAddr", "playAddr"):
            # JSON strings may contain \uXXXX escapes or \/ slashes
            m = re.search(
                rf'"{key}"\s*:\s*"((?:[^"\\]|\\.){{20,}})"',
                html,
            )
            if m:
                cdn = _clean_cdn_url(m.group(1))
                if cdn.startswith("http"):
                    return cdn
        return None

    # ── Page scrape orchestrator ──────────────────────────────────────────────

    def _try_page_scrape(
        self,
        url: str,
        dest: Path,
        index: int,
        progress_cb: Optional[Callable[[str], None]],
    ) -> bool:
        resp = requests.get(
            url,
            headers={
                "User-Agent":      _UA_DESKTOP,
                "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer":         "https://www.tiktok.com/",
            },
            timeout=20,
        )
        resp.raise_for_status()
        html = resp.text

        cdn_url = (
            self._parse_sigi_state(html)
            or self._parse_next_data(html)
            or self._parse_raw_json(html)
        )

        if not cdn_url:
            return False

        if progress_cb:
            progress_cb(f"  Clip {index + 1}: downloading…")

        return _stream_save(cdn_url, dest, {}, index, progress_cb)

    # ── Method B: mobile feed API (Android app endpoint) ─────────────────────

    def _try_mobile_api(
        self,
        video_id: str,
        dest: Path,
        index: int,
        progress_cb: Optional[Callable[[str], None]],
    ) -> bool:
        api_url = (
            "https://api22-normal-c-useast2a.tiktokv.com/aweme/v1/feed/"
            f"?aweme_id={video_id}&version_code=262036&app_id=1233&count=1"
        )
        resp = requests.get(
            api_url,
            headers={"User-Agent": _UA_ANDROID},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        aweme_list = data.get("aweme_list", [])
        if not aweme_list:
            raise ValueError(f"API returned empty aweme_list for id={video_id}")

        video   = aweme_list[0].get("video", {})
        cdn_url = None

        for field in ("download_addr", "play_addr"):
            urls = video.get(field, {}).get("url_list", [])
            if urls:
                cdn_url = urls[0]
                break

        if not cdn_url:
            for br in video.get("bit_rate", []):
                urls = br.get("play_addr", {}).get("url_list", [])
                if urls:
                    cdn_url = urls[0]
                    break

        if not cdn_url:
            raise ValueError("No CDN URL found in mobile API response")

        if progress_cb:
            progress_cb(f"  Clip {index + 1}: downloading…")

        return _stream_save(cdn_url, dest, {}, index, progress_cb)

    # ── Public entry point ────────────────────────────────────────────────────

    def download(
        self,
        url: str,
        index: int,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        dest     = DOWNLOAD_DIR / f"clip_{index:02d}.mp4"
        full_url = self.resolve_url(url)
        video_id = self.extract_video_id(full_url)

        # Method A — page scrape (SIGI_STATE / __NEXT_DATA__ / raw regex)
        try:
            if progress_cb:
                progress_cb(f"  Clip {index + 1}: reading TikTok page…")
            if self._try_page_scrape(full_url, dest, index, progress_cb):
                return str(dest)
            if progress_cb:
                progress_cb(f"  Clip {index + 1}: page scrape found no URL, trying API…")
        except Exception as exc:
            if progress_cb:
                progress_cb(
                    f"  Clip {index + 1}: page scrape error "
                    f"({type(exc).__name__}: {exc}), trying API…"
                )
        dest.unlink(missing_ok=True)

        # Method B — mobile API
        if video_id:
            try:
                if progress_cb:
                    progress_cb(f"  Clip {index + 1}: trying TikTok mobile API…")
                if self._try_mobile_api(video_id, dest, index, progress_cb):
                    return str(dest)
                if progress_cb:
                    progress_cb(f"  Clip {index + 1}: mobile API returned no file")
            except Exception as exc:
                if progress_cb:
                    progress_cb(
                        f"  Clip {index + 1}: mobile API error "
                        f"({type(exc).__name__}: {exc})"
                    )
            dest.unlink(missing_ok=True)
        else:
            if progress_cb:
                progress_cb(
                    f"  Clip {index + 1}: could not extract video ID from "
                    f"'{full_url}' — skipping mobile API"
                )

        return None  # caller falls through to yt-dlp


# ── yt-dlp helpers ────────────────────────────────────────────────────────────

class _YTLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []
    def debug(self, msg: str)   -> None: pass
    def info(self, msg: str)    -> None: pass
    def warning(self, msg: str) -> None: self.messages.append(f"WARNING: {msg}")
    def error(self, msg: str)   -> None: self.messages.append(f"ERROR: {msg}")


def _ytdlp_base_opts(index: int, progress_cb) -> dict:
    def _hook(d: dict) -> None:
        if progress_cb and d.get("status") == "downloading":
            progress_cb(f"  Clip {index + 1}: {_strip_ansi(d.get('_percent_str', '?%').strip())}")
    return {
        "format": (
            "bestvideo[ext=mp4][height<=1920]+bestaudio[ext=m4a]"
            "/bestvideo[height<=1920]+bestaudio"
            "/best[ext=mp4]/best"
        ),
        "outtmpl":             str(DOWNLOAD_DIR / f"clip_{index:02d}.%(ext)s"),
        "merge_output_format": "mp4",
        "progress_hooks":      [_hook] if progress_cb else [],
        "quiet":               True,
        "no_warnings":         True,
        "ffmpeg_location":     _FFMPEG_DIR,
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }


def _download_via_ytdlp(
    url: str,
    index: int,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Download via yt-dlp.  For TikTok URLs, automatically tries browser
    cookies (Chrome → Edge → Firefox) to work around NoSessionContext.
    Raises on failure.
    """
    base_opts = _ytdlp_base_opts(index, progress_cb)
    logger    = _YTLogger()
    base_opts["logger"] = logger

    # Build list of option-sets to try in order
    opt_sets: list[dict] = []
    if _is_tiktok(url):
        # Try with cookies from each common browser first
        for browser in ("chrome", "edge", "firefox", "chromium"):
            opt_sets.append({**base_opts, "cookiesfrombrowser": (browser,)})
    opt_sets.append(base_opts)   # final attempt: no cookies

    last_error = "yt-dlp: no attempts succeeded"
    for opts in opt_sets:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                retcode = ydl.download([url])
            if retcode == 0:
                actual = _find_output(index)
                if actual:
                    return str(actual)
                logged = "; ".join(logger.messages)
                raise FileNotFoundError(
                    "Output file missing after yt-dlp"
                    + (f" — {logged}" if logged else "")
                )
        except Exception as exc:
            last_error = str(exc).strip() or repr(exc)
            # Only retry with next cookie source if it's a session/auth error
            if not any(kw in last_error for kw in
                       ("SessionContext", "login", "cookie", "sign in", "403")):
                break   # non-auth error — retrying with different cookies won't help

    raise RuntimeError(last_error)


# ── Single-clip coordinator ────────────────────────────────────────────────────

_tt = TikTokDownloader()


def _download_single(
    url: str,
    index: int,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> DownloadResult:
    _purge_stale(index)
    last_error = "Max retries exceeded"

    # ── TikTok: custom methods A/B, then yt-dlp fallback ─────────────────────
    if _is_tiktok(url):
        try:
            path = _tt.download(url, index, progress_cb)
            if path:
                meta = _probe(path)
                return DownloadResult(
                    path=path, source_url=url,
                    original_width=meta.get("width", 0),
                    original_height=meta.get("height", 0),
                    duration_s=meta.get("duration", 0.0),
                )
            last_error = "TikTok: no URL found via page scrape or mobile API"
        except Exception as exc:
            last_error = str(exc).strip() or repr(exc)

        _purge_stale(index)
        if progress_cb:
            progress_cb(f"  Clip {index + 1}: falling back to yt-dlp…")

    # ── yt-dlp (primary for non-TikTok, fallback for TikTok) ─────────────────
    for attempt in range(2):
        try:
            path = _download_via_ytdlp(url, index, progress_cb)
            meta = _probe(path)
            return DownloadResult(
                path=path, source_url=url,
                original_width=meta.get("width", 0),
                original_height=meta.get("height", 0),
                duration_s=meta.get("duration", 0.0),
            )
        except Exception as exc:
            last_error = str(exc).strip() or repr(exc)
            if attempt == 0:
                time.sleep(random.uniform(2.0, 5.0))

    return DownloadResult(source_url=url, error=last_error)


# ── Local file copy ───────────────────────────────────────────────────────────

def copy_local_file(path: str, index: int) -> DownloadResult:
    """
    Return the local file path in-place — no copy, no I/O overhead.
    Just strip surrounding quotes and probe the file for its dimensions.
    """
    path = path.strip().strip("\"'")
    src = Path(path)
    if not src.exists():
        return DownloadResult(source_url=path, error=f"File not found: {path}")
    meta = _probe(str(src))
    return DownloadResult(
        path=str(src), source_url=path,
        original_width=meta.get("width", 0),
        original_height=meta.get("height", 0),
        duration_s=meta.get("duration", 0.0),
    )


# ── Parallel batch ────────────────────────────────────────────────────────────

def download_clips_parallel(
    sources: list[dict],
    progress_cb: Optional[Callable[[str], None]] = None,
    max_workers: int = 3,
) -> list[DownloadResult]:
    results: list[Optional[DownloadResult]] = [None] * len(sources)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, src in enumerate(sources):
            if src.get("is_local"):
                fut = pool.submit(copy_local_file, src["url"], i)
            else:
                fut = pool.submit(_download_single, src["url"], i, progress_cb)
            futures[fut] = i

        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                results[idx] = DownloadResult(
                    source_url=sources[idx]["url"],
                    error=str(exc) or repr(exc),
                )

    for i in range(len(results)):
        if results[i] is None:
            results[i] = DownloadResult(
                source_url=sources[i]["url"],
                error="Internal error: no result recorded",
            )

    return results  # type: ignore[return-value]
