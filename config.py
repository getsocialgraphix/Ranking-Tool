import os
import subprocess
from pathlib import Path

# ── Output spec ───────────────────────────────────────────────────────────────
OUTPUT_WIDTH  = 1080
OUTPUT_HEIGHT = 1920
FPS           = 30
CRF           = 20
FFMPEG_PRESET = "fast"
VIDEO_BITRATE = "8000k"
AUDIO_BITRATE = "192k"

# ── Transitions ───────────────────────────────────────────────────────────────
CROSSFADE_DURATION = 0.25  # seconds

# ── Audio ducking ─────────────────────────────────────────────────────────────
MUSIC_DUCK_LEVEL   = 0.12   # 12 % volume while clip audio plays
MUSIC_FULL_LEVEL   = 0.35   # 35 % between clips
SILENCE_THRESH_DB  = -35    # dBFS threshold used by pydub silence detector

# ── Overlay typography ────────────────────────────────────────────────────────
RANK_FONT_SIZE     = 120
TITLE_FONT_SIZE    = 56
STROKE_WIDTH       = 6
TITLE_MAX_WIDTH_PX = 900

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT_DIR    = Path(__file__).parent
ASSETS_DIR  = ROOT_DIR / "assets"
FONTS_DIR   = ASSETS_DIR / "fonts"
TEMP_DIR    = ROOT_DIR / "temp"
OUTPUT_DIR  = ROOT_DIR / "output"
PRESETS_DIR = Path.home() / ".ranking_tool" / "presets"

# ── FFmpeg binaries (resolved once at import) ─────────────────────────────────
# Embeddable Python doesn't inherit the user PATH set by the installer,
# so we resolve the full path explicitly and inject it into os.environ.
_FFMPEG_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs" / "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin",
    Path("C:/ffmpeg/bin"),
]
for _ff_bin in _FFMPEG_CANDIDATES:
    if (_ff_bin / "ffmpeg.exe").exists():
        _ff_str = str(_ff_bin)
        if _ff_str not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _ff_str + os.pathsep + os.environ.get("PATH", "")
        FFMPEG_BIN  = str(_ff_bin / "ffmpeg.exe")
        FFPROBE_BIN = str(_ff_bin / "ffprobe.exe")
        break
else:
    # Fall back to assuming they're on PATH already
    FFMPEG_BIN  = "ffmpeg"
    FFPROBE_BIN = "ffprobe"

for _d in [
    TEMP_DIR / "downloaded",
    TEMP_DIR / "processed",
    TEMP_DIR / "overlays",
    TEMP_DIR / "audio",
    OUTPUT_DIR,
    FONTS_DIR,
    PRESETS_DIR,
]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Default preset ────────────────────────────────────────────────────────────
DEFAULT_PRESET: dict = {
    "name": "default",
    "rank_colors": {
        "1":  "#FF3B30",
        "2":  "#FF9500",
        "3":  "#FFCC00",
        "4":  "#34C759",
        "5":  "#007AFF",
        "6":  "#AF52DE",
        "7":  "#FF2D55",
        "8":  "#5AC8FA",
        "9":  "#4CD964",
        "10": "#FF6B35",
    },
    "rank_font_size":      RANK_FONT_SIZE,
    "title_font_size":     TITLE_FONT_SIZE,
    "video_bitrate":       VIDEO_BITRATE,
    "music_duck_level":    MUSIC_DUCK_LEVEL,
    "music_full_level":    MUSIC_FULL_LEVEL,
    "crossfade_duration":  CROSSFADE_DURATION,

    # ── Overlay visual style ──────────────────────────────────────────────────
    "overlay_style": {
        # Left panel background (0 = fully transparent / off)
        "panel_alpha":          0,      # 0–255
        "panel_width":          260,    # px

        # Rank numbers
        "num_font":             "auto",
        "num_size":             72,     # font size px
        "num_stroke":           5,      # outline width px
        "num_stroke_color":     "#000000",
        "num_x":                20,     # left margin px
        "num_spacing":          140,    # px between consecutive number centres
        "num_start_y":          60,     # y of first number from top
        "num_use_rank_color":   True,   # True → use rank_colors; False → num_color
        "num_color":            "#FFFFFF",

        # Clip titles (appear only when a clip is CURRENT or REVEALED)
        "title_font":           "auto",
        "title_size":           26,     # font size px
        "title_color":          "#FFFFFF",
        "title_stroke":         3,
        "title_stroke_color":   "#000000",
        "title_gap":            6,      # px gap between number baseline and title

        # Top banner (video title text)
        "banner_enabled":       True,
        "banner_bg_alpha":      150,    # 0–255
        "banner_bg_height":     0,      # 0 = auto-fit to text; >0 = fixed px height
        "banner_font":          "auto",
        "banner_size":          48,
        "banner_color":         "#FFFFFF",
        "banner_stroke":        4,
        "banner_stroke_color":  "#000000",

        # Per-rank custom prefix (emoji / text) prepended to the number
        # e.g. {"1": "🥇", "2": "🥈", "3": "🥉"}
        "rank_prefixes": {str(i): "" for i in range(1, 11)},
    },
}


def setup_check() -> list[str]:
    """Return a list of human-readable error strings for missing dependencies."""
    issues: list[str] = []

    for binary, resolved in (("ffmpeg", FFMPEG_BIN), ("ffprobe", FFPROBE_BIN)):
        try:
            subprocess.run(
                [resolved, "-version"],
                capture_output=True, check=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            issues.append(
                f"`{binary}` not found. "
                "Install FFmpeg from https://ffmpeg.org/download.html and ensure it is on PATH "
                "(or place it at C:/ffmpeg/bin)."
            )

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        issues.append("Pillow not installed — run: pip install Pillow")

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        issues.append("yt-dlp not installed — run: pip install yt-dlp")

    return issues
