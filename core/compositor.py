"""
Ranking overlay renderer (Pillow) + clip assembler (FFmpeg).

Speed notes
───────────
• process_clip + add_overlay merged into ONE FFmpeg pass per clip
• trim_clip uses stream-copy (instant, no re-encode)
• Local files are never copied — used in-place

Visual style
────────────
• Compact rank list on the left — no forced dark panel (panel_alpha=0 default)
• Numbers use fixed pixel spacing instead of equal-height slots
• UPCOMING  → dim gray number only
• CURRENT   → bright rank-colour number + glow highlight + title
• REVEALED  → dimmed rank-colour number + dim title
• Clip titles appear ONLY once a clip starts playing (CURRENT or REVEALED)
• Optional top-banner for the video title

Customisation
─────────────
All visual parameters live in  preset["overlay_style"]  (see config.py for defaults).
Use  list_fonts()  to get a {display_name: path} dict for font pickers in the UI.
Use  generate_preview_frame()  to get a ready-to-display PNG composite.
"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from config import (
    AUDIO_BITRATE, CRF, FFMPEG_BIN, FFPROBE_BIN, FFMPEG_PRESET,
    FONTS_DIR, FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH, TEMP_DIR, VIDEO_BITRATE,
)

OVERLAY_DIR = TEMP_DIR / "overlays"
OVERLAY_DIR.mkdir(parents=True, exist_ok=True)

# Higher CRF for intermediate renders that will be re-encoded by xfade concat.
# When stream-copy concat is used (crossfade=0) the CRF below is bypassed and
# the final CRF from config.py is used instead so quality is never sacrificed.
_INTERMEDIATE_CRF = min(CRF + 6, 28)


# ═══════════════════════════════════════════════════════════════════════════════
#  Font helpers
# ═══════════════════════════════════════════════════════════════════════════════

# Curated Windows fonts we know by filename
_WIN_FONTS: dict[str, str] = {
    "Arial":              "arial.ttf",
    "Arial Bold":         "arialbd.ttf",
    "Arial Black":        "ariblk.ttf",
    "Impact":             "impact.ttf",
    "Verdana":            "verdana.ttf",
    "Verdana Bold":       "verdanab.ttf",
    "Tahoma":             "tahoma.ttf",
    "Tahoma Bold":        "tahomabd.ttf",
    "Segoe UI":           "segoeui.ttf",
    "Segoe UI Bold":      "segoeuib.ttf",
    "Segoe UI Emoji 😀":  "seguiemj.ttf",
    "Calibri":            "calibri.ttf",
    "Calibri Bold":       "calibrib.ttf",
    "Times New Roman":    "times.ttf",
    "Georgia":            "georgia.ttf",
    "Comic Sans MS":      "comic.ttf",
    "Trebuchet MS":       "trebuc.ttf",
    "Courier New":        "cour.ttf",
    "Consolas":           "consola.ttf",
    "Franklin Gothic":    "framd.ttf",
    "Century Gothic":     "gothic.ttf",
}


def list_fonts() -> dict[str, str]:
    """
    Return  {display_name: path}  for every usable font we can find.
    The value "auto" means "let compositor choose the best available font."
    Dict is ordered: Auto first, bundled fonts second, then system fonts.
    """
    fonts: dict[str, str] = {"⚡ Auto (best available)": "auto"}

    # ── Bundled fonts (assets/fonts/) ────────────────────────────────────────
    for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        for f in sorted(FONTS_DIR.glob(ext)):
            display = "★ " + f.stem.replace("-", " ").replace("_", " ")
            fonts[display] = str(f)

    # ── Curated Windows system fonts ─────────────────────────────────────────
    win_dir = Path(r"C:\Windows\Fonts")
    if win_dir.exists():
        for display, filename in _WIN_FONTS.items():
            p = win_dir / filename
            if p.exists() and display not in fonts:
                fonts[display] = str(p)

    # ── All remaining system fonts (scanned, deduped) ─────────────────────────
    if win_dir.exists():
        seen_names = set(fonts.values())
        for ext in ("*.ttf", "*.otf"):
            for f in sorted(win_dir.glob(ext)):
                if str(f) not in seen_names:
                    display = f.stem.replace("-", " ").replace("_", " ")
                    if display not in fonts:
                        fonts[display] = str(f)
                        seen_names.add(str(f))

    return fonts


@lru_cache(maxsize=64)
def _load_font(path_or_auto: str, size: int) -> ImageFont.FreeTypeFont:
    """
    Load a font by path, falling back gracefully through known good fonts.
    Results are cached so repeated calls with the same (path, size) are instant.
    Thread-safe: lru_cache uses an internal lock on cache writes.
    """
    candidates = []
    if path_or_auto and path_or_auto != "auto":
        candidates.append(path_or_auto)

    # Auto-fallback chain
    candidates += [
        str(FONTS_DIR / "Montserrat-Black.ttf"),
        r"C:\Windows\Fonts\ariblk.ttf",   # Arial Black
        r"C:\Windows\Fonts\impact.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "arialbd.ttf",
        "Arial Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════════
#  Color helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _hex_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ═══════════════════════════════════════════════════════════════════════════════
#  Style resolver
# ═══════════════════════════════════════════════════════════════════════════════

def _default_style() -> dict:
    return {
        "panel_alpha":          0,
        "panel_width":          260,
        "num_font":             "auto",
        "num_size":             72,
        "num_stroke":           5,
        "num_stroke_color":     "#000000",
        "num_x":                20,
        "num_spacing":          140,
        "num_start_y":          60,
        "num_use_rank_color":   True,
        "num_color":            "#FFFFFF",
        "title_font":           "auto",
        "title_size":           26,
        "title_color":          "#FFFFFF",
        "title_stroke":         3,
        "title_stroke_color":   "#000000",
        "title_gap":            6,
        "banner_enabled":       True,
        "banner_bg_alpha":      255,
        "banner_font":          "auto",
        "banner_size":          48,
        "banner_color":         "#FFFFFF",
        "banner_stroke":        4,
        "banner_stroke_color":  "#000000",
        "rank_prefixes":        {str(i): "" for i in range(1, 11)},
    }


def _get_style(preset: dict) -> dict:
    """Merge preset's overlay_style over the defaults."""
    base = _default_style()
    base.update(preset.get("overlay_style", {}))
    return base


# ═══════════════════════════════════════════════════════════════════════════════
#  Overlay renderer  (Pillow)
# ═══════════════════════════════════════════════════════════════════════════════

def create_ranking_overlay(
    sorted_clips:      list[dict],
    current_idx:       int,
    preset:            dict,
    video_title:       str        = "",
    title_word_colors: list[str]  = None,   # per-word colors for the banner
    index:             int        = 0,
) -> str:
    """
    Generate a full-frame RGBA PNG with ranking list + optional banner.

    Rendering order
    ───────────────
    1. Panel background (optional)
    2. Banner (first — so we know its height before placing numbers)
    3. Rank numbers (auto-positioned below banner)

    Banner supports multi-line text (\\n) with per-word colour.
    Rank numbers support custom per-rank emoji/text prefixes.
    """
    style = _get_style(preset)
    img   = Image.new("RGBA", (OUTPUT_WIDTH, OUTPUT_HEIGHT), (0, 0, 0, 0))

    # ── 1. Optional panel background ──────────────────────────────────────────
    panel_alpha = int(style["panel_alpha"])
    if panel_alpha > 0:
        pw  = int(style["panel_width"])
        bg  = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(bg).rectangle([0, 0, pw, OUTPUT_HEIGHT], fill=(0, 0, 0, panel_alpha))
        img = Image.alpha_composite(img, bg)

    draw = ImageDraw.Draw(img)

    # ── 2. Banner (rendered first so we can measure its height) ───────────────
    b_h = 0   # will be set below if banner is visible
    show_banner = bool(video_title.strip() and style.get("banner_enabled", True))

    if show_banner:
        b_size   = int(style["banner_size"])
        b_alpha  = int(style["banner_bg_alpha"])
        b_stroke = int(style["banner_stroke"])
        b_font   = _load_font(style["banner_font"], b_size)
        b_rgb    = _hex_rgb(style["banner_color"])
        b_sc     = _hex_rgb(style["banner_stroke_color"])

        lines        = video_title.strip().split("\n")
        line_h       = b_size + 10          # approximate line height in px
        _auto_h      = max(80, line_h * len(lines) + 20)
        _auto_h      = min(_auto_h, OUTPUT_HEIGHT // 3)  # never eat more than 1/3 of frame
        _fixed_h     = int(style.get("banner_bg_height", 0))
        b_h          = _fixed_h if _fixed_h > 0 else _auto_h

        if b_alpha > 0:
            bar = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(bar).rectangle([0, 0, OUTPUT_WIDTH, b_h], fill=(0, 0, 0, b_alpha))
            img  = Image.alpha_composite(img, bar)
            draw = ImageDraw.Draw(img)

        total_text_h = line_h * len(lines)
        ty           = max(6, (b_h - total_text_h) // 2)
        word_idx     = 0

        # Measure space width for word spacing
        _sp_bb  = draw.textbbox((0, 0), "  ", font=b_font)
        space_w = max(6, (_sp_bb[2] - _sp_bb[0]) // 2)

        for line in lines:
            words = line.split()
            if not words:
                ty += line_h
                continue

            # Compute word widths and total line width for centering
            word_widths: list[tuple[str, int]] = []
            for word in words:
                bb = draw.textbbox((0, 0), word, font=b_font)
                word_widths.append((word, bb[2] - bb[0]))

            line_w = sum(w for _, w in word_widths) + space_w * (len(words) - 1)
            lx     = max(int(style["num_x"]), (OUTPUT_WIDTH - line_w) // 2)

            for word, w in word_widths:
                # Per-word colour: use title_word_colors list by position
                if title_word_colors and word_idx < len(title_word_colors):
                    try:
                        wr, wg, wb = _hex_rgb(title_word_colors[word_idx])
                    except Exception:
                        wr, wg, wb = b_rgb
                else:
                    wr, wg, wb = b_rgb

                draw.text(
                    (lx, ty), word, font=b_font,
                    fill=(wr, wg, wb, 245),
                    stroke_width=b_stroke,
                    stroke_fill=(*b_sc, 210),
                )
                lx       += w + space_w
                word_idx += 1

            ty += line_h

    # ── 3. Rank numbers (always below banner) ─────────────────────────────────
    num_size   = int(style["num_size"])
    title_size = int(style["title_size"])
    num_font   = _load_font(style["num_font"],   num_size)
    title_font = _load_font(style["title_font"], title_size)

    num_x      = int(style["num_x"])
    spacing    = int(style["num_spacing"])
    title_gap  = int(style["title_gap"])
    num_stroke = int(style["num_stroke"])
    num_sc_rgb = _hex_rgb(style["num_stroke_color"])
    title_stk  = int(style["title_stroke"])
    title_sc   = _hex_rgb(style["title_stroke_color"])
    title_clr  = _hex_rgb(style["title_color"])

    # Effective start Y: always pushed below the banner automatically
    start_y = max(int(style["num_start_y"]), b_h + 10)

    rank_prefixes = style.get("rank_prefixes", {})

    for idx, clip in enumerate(sorted_clips):
        rank  = clip["rank"]
        title = clip.get("title", "").strip()

        num_y = start_y + idx * spacing
        if num_y > OUTPUT_HEIGHT - 10:
            break   # clip would be off-screen — stop rendering

        # Build number text first so we can measure its width for side-by-side layout
        prefix   = rank_prefixes.get(str(rank), "")
        num_text = f"{prefix}{rank}."

        # Measure number pixel width — title sits BESIDE the number on the same row
        _num_bb = draw.textbbox((0, 0), num_text, font=num_font, stroke_width=num_stroke)
        _num_w  = max(1, _num_bb[2] - _num_bb[0])
        ttl_x   = num_x + _num_w + title_gap          # title X: right of number + gap
        ttl_y   = num_y + max(0, (num_size - title_size) // 2)  # title Y: vertically centred

        # Rank colour
        if style["num_use_rank_color"]:
            nr, ng, nb = _hex_rgb(preset["rank_colors"].get(str(rank), "#FFFFFF"))
        else:
            nr, ng, nb = _hex_rgb(style["num_color"])

        # Display list is ascending (rank 1 = idx 0 at top, rank N = idx n-1 at bottom).
        # Play order is descending: rank N plays first (idx n-1), rank 1 plays last (idx 0).
        # Therefore, when current_idx = k:
        #   idx > k → higher display index → already played (revealed)
        #   idx = k → currently playing
        #   idx < k → lower display index → plays later (upcoming)
        is_current  = (idx == current_idx)
        is_revealed = (idx > current_idx)   # higher idx = played earlier

        # Labels appear the moment a clip starts playing and STAY for the rest of the video.
        # "Shown" = currently playing (is_current) OR already played (is_revealed).
        show_title = bool(title and idx >= current_idx)  # current + revealed

        if is_current:
            # ── Glow highlight spanning number + label on the same row ────────
            _disp_glow = (title[:26] + "…" if len(title) > 27 else title) if show_title else ""
            _ttl_extra = 0
            if _disp_glow:
                _tb = draw.textbbox((0, 0), _disp_glow, font=title_font, stroke_width=title_stk)
                _ttl_extra = title_gap + max(0, _tb[2] - _tb[0])
            glow_h = num_size + 24
            glow_w = _num_w + _ttl_extra + 28
            glow   = Image.new("RGBA", img.size, (0, 0, 0, 0))
            gx2    = min(OUTPUT_WIDTH, num_x + glow_w)
            gy2    = min(OUTPUT_HEIGHT, num_y + glow_h)
            ImageDraw.Draw(glow).rounded_rectangle(
                [max(0, num_x - 12), max(0, num_y - 10), gx2, gy2],
                radius=12, fill=(nr, ng, nb, 45),
            )
            img  = Image.alpha_composite(img, glow)
            draw = ImageDraw.Draw(img)

            # Full rank colour number
            draw.text((num_x, num_y), num_text, font=num_font,
                      fill=(nr, ng, nb, 255),
                      stroke_width=num_stroke, stroke_fill=(*num_sc_rgb, 220))
            # Label appears beside number
            if show_title:
                disp = title[:26] + "…" if len(title) > 27 else title
                draw.text((ttl_x, ttl_y), disp, font=title_font,
                          fill=(255, 255, 255, 255),
                          stroke_width=title_stk, stroke_fill=(*title_sc, 210))

        elif is_revealed:
            # ── Already played: full rank colour, label stays visible ─────────
            draw.text((num_x, num_y), num_text, font=num_font,
                      fill=(nr, ng, nb, 255),
                      stroke_width=num_stroke, stroke_fill=(*num_sc_rgb, 200))
            if show_title:
                disp = title[:26] + "…" if len(title) > 27 else title
                draw.text((ttl_x, ttl_y), disp, font=title_font,
                          fill=(*title_clr, 225),
                          stroke_width=title_stk, stroke_fill=(*title_sc, 170))

        else:
            # ── Upcoming: full rank colour, no label yet ──────────────────────
            draw.text((num_x, num_y), num_text, font=num_font,
                      fill=(nr, ng, nb, 185),
                      stroke_width=max(0, num_stroke - 1), stroke_fill=(*num_sc_rgb, 140))

    out_path = str(OVERLAY_DIR / f"ranking_{index:02d}.png")
    img.save(out_path, "PNG")
    return out_path


# ═══════════════════════════════════════════════════════════════════════════════
#  Live-preview frame (composited on dark background, scaled for UI display)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_preview_frame(
    sorted_clips:      list[dict],
    current_idx:       int,
    preset:            dict,
    video_title:       str       = "",
    title_word_colors: list[str] = None,
    scale:             float     = 0.35,
) -> str:
    """
    Render the overlay onto a dark background and return a path to a
    scaled-down PNG suitable for  st.image().
    """
    overlay_path = create_ranking_overlay(
        sorted_clips, current_idx, preset, video_title,
        title_word_colors=title_word_colors,
        index=99,
    )
    overlay = Image.open(overlay_path).convert("RGBA")

    # Dark gradient background to simulate a video frame
    bg = Image.new("RGBA", (OUTPUT_WIDTH, OUTPUT_HEIGHT), (18, 18, 22, 255))
    bg.paste(overlay, mask=overlay.split()[3])

    w = max(1, int(OUTPUT_WIDTH  * scale))
    h = max(1, int(OUTPUT_HEIGHT * scale))
    preview = bg.resize((w, h), Image.LANCZOS).convert("RGB")

    preview_path = str(OVERLAY_DIR / "preview_frame.png")
    preview.save(preview_path, "PNG")
    return preview_path


# ═══════════════════════════════════════════════════════════════════════════════
#  FFprobe helpers  — ONE call per file instead of two
# ═══════════════════════════════════════════════════════════════════════════════

def _probe_full(path: str) -> dict:
    """
    Single FFprobe invocation that returns width, height, duration AND
    has_audio in one shot — replaces the old _probe_video + _has_audio pair.
    Returns {} on failure.
    """
    cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",   # all streams (video + audio)
        "-show_format",    # container-level duration
        path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {}
    data    = json.loads(r.stdout)
    streams = data.get("streams", [])
    fmt     = data.get("format", {})
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "width":     int(vstream.get("width", 0)),
        "height":    int(vstream.get("height", 0)),
        "duration":  float(vstream.get("duration") or fmt.get("duration") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


# Keep thin compatibility shims so nothing outside this module breaks.
def _probe_video(path: str) -> dict:
    info = _probe_full(path)
    return {k: info[k] for k in ("width", "height", "duration") if k in info}

def _has_audio(path: str) -> bool:
    return _probe_full(path).get("has_audio", False)


# ═══════════════════════════════════════════════════════════════════════════════
#  Single-clip render  (scale + overlay = ONE FFmpeg pass)
# ═══════════════════════════════════════════════════════════════════════════════

def _render_one_clip(
    clip:        dict,
    overlay_png: str,
    output_path: str,
    crf:         Optional[int]                   = None,
    status_cb:   Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Scale the raw clip to 1080×1920 AND burn the overlay — one FFmpeg call.

    • Vertical (9:16) sources: scale + centre-pad + overlay
    • Landscape sources: blurred/darkened bg + centred fg + overlay

    Uses `ultrafast` preset for intermediate renders.
    All output is yuv420p H.264 + AAC for consistent downstream concat.

    crf: override encode quality (None → use module default based on pipeline).
    """
    path     = clip["path"]
    duration = float(clip.get("duration", 0))
    w = clip.get("width") or 0
    h = clip.get("height") or 0

    # Single FFprobe call for all metadata (width, height, duration, audio)
    info      = _probe_full(path)
    if not (w and h):
        w = info.get("width", 0)
        h = info.get("height", 0)
        if not duration:
            duration = info.get("duration", 0)
    has_audio = info.get("has_audio", True)

    if not (w and h):
        if status_cb:
            status_cb(f"  ⚠ Cannot determine dimensions for: {Path(path).name}")
        return None

    aspect   = w / h
    _crf_use = crf if crf is not None else CRF

    if abs(aspect - 9 / 16) < 0.12:
        # Vertical / near-9:16 — scale + centre-pad, then overlay
        vf = (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
            f":force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1,fps={FPS},format=yuv420p[bg];"
            f"[bg][1:v]overlay=0:0[out]"
        )
    else:
        # Landscape — blurred+darkened background, centred foreground, overlay.
        # [0:v] must be split into two before filter_complex can use it twice.
        vf = (
            f"[0:v]split=2[vid_bg][vid_fg];"
            f"[vid_bg]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
            f":force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
            f"gblur=sigma=28,"
            f"colorchannelmixer=rr=0.45:gg=0.45:bb=0.45,"
            f"setsar=1[bg];"
            f"[vid_fg]scale={OUTPUT_WIDTH}:-2,"
            f"setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"fps={FPS},format=yuv420p[comp];"
            f"[comp][1:v]overlay=0:0[out]"
        )

    _enc = [
        "-c:v", "libx264", "-crf", str(_crf_use), "-preset", "ultrafast",
        "-threads", "1",         # single-threaded encode → less frame-buffer RAM
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-ar", "44100",          # force consistent sample rate across all clips
        "-ac", "2",              # force stereo — avoids mono/stereo mismatch at concat
        "-movflags", "+faststart",
    ]

    if has_audio:
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", path, "-i", overlay_png,
            "-filter_complex", vf,
            "-map", "[out]", "-map", "0:a:0",
            *_enc,
            output_path,
        ]
    else:
        # No audio in source — synthesise a silent track.
        # Use explicit -t to end reliably instead of relying on -shortest.
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", path, "-i", overlay_png,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-filter_complex", vf,
            "-map", "[out]", "-map", "2:a",
            *_enc,
        ]
        if duration > 0:
            cmd += ["-t", f"{duration:.4f}"]
        else:
            cmd += ["-shortest"]
        cmd.append(output_path)

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        if status_cb:
            err_tail = (r.stderr or "no stderr output")[-1200:]
            status_cb(f"  ⚠ FFmpeg render error (exit {r.returncode}):\n{err_tail}")
        return None
    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        if status_cb:
            status_cb(f"  ⚠ FFmpeg produced empty/missing output: {output_path}")
        return None
    return output_path


def _create_black_intro(
    voiceover_path: str,
    overlay_png:    str,
    output_path:    str,
    crf:            int                              = 26,
    status_cb:      Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Build a plain black-screen clip whose duration matches the voiceover audio.

    No overlay is shown — the screen stays fully black while the commentary
    plays. The actual ranked footage starts immediately after this clip ends.

    Inputs:
      [0:v] — infinite black lavfi colour source (no overlay composited)
      [1:a] — voiceover audio (determines output duration via -shortest)

    overlay_png is accepted but intentionally unused so the call-site signature
    stays consistent with the rest of the pipeline.
    """
    cmd = [
        FFMPEG_BIN, "-y",
        # Input 0: infinite black video source
        "-f", "lavfi",
        "-i", f"color=c=black:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:r={FPS}",
        # Input 1: voiceover audio — drives duration via -shortest
        "-i", voiceover_path,
        "-map", "0:v",
        "-map", "1:a",
        "-vf", f"setsar=1,fps={FPS},format=yuv420p",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "ultrafast",
        "-threads", "1",         # single-threaded encode → less frame-buffer RAM
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-ar", "44100",          # match the sample rate used by _render_one_clip
        "-ac", "2",              # upmix mono ElevenLabs audio to stereo
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        if status_cb:
            status_cb(f"  ⚠ Black intro FFmpeg error:\n{(r.stderr or '')[-600:]}")
        return None
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  Concatenation
# ═══════════════════════════════════════════════════════════════════════════════

def _simple_concat(
    clip_paths: list[str],
    output_path: str,
    status_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Concatenate clips via the FFmpeg concat demuxer.

    Strategy (fastest → slowest):
      1. Stream-copy  (-c copy)  — near-instant, no quality loss.
         Works whenever all clips share the same codec/resolution/fps,
         which is always the case when they came from _render_one_clip.
      2. Re-encode with libx264 + target bitrate — slower fallback.
      3. filter_complex concat — last resort (requires audio on every input).
    """
    list_file = TEMP_DIR / "_concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as _f:
        for p in clip_paths:
            _f.write(f"file '{str(Path(p)).replace(chr(92), '/')}'\n")

    list_path = str(list_file)

    # ── 1. Stream-copy (fastest — no re-encode) ────────────────────────────
    cmd_copy = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    r = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=900)
    if r.returncode == 0:
        return output_path

    if status_cb:
        status_cb("  stream-copy concat failed — re-encoding…")

    # ── 2. Re-encode fallback ──────────────────────────────────────────────
    cmd_enc = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", FFMPEG_PRESET,
        "-threads", "1",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    r2 = subprocess.run(cmd_enc, capture_output=True, text=True, timeout=900)
    if r2.returncode == 0:
        return output_path

    if status_cb:
        status_cb("  concat-demuxer failed — trying filter-complex fallback…")

    # ── 3. filter_complex concat (last resort) ─────────────────────────────
    n      = len(clip_paths)
    inputs = sum([["-i", p] for p in clip_paths], [])
    fc     = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vout][aout]"
    cmd3   = (
        [FFMPEG_BIN, "-y"] + inputs
        + ["-filter_complex", fc,
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", FFMPEG_PRESET,
           "-threads", "1",
           "-c:a", "aac", "-b:a", AUDIO_BITRATE,
           "-ar", "44100", "-ac", "2",
           output_path]
    )
    r3 = subprocess.run(cmd3, capture_output=True, text=True, timeout=900)
    return output_path if r3.returncode == 0 else None


def concatenate_with_xfade(
    clip_paths:  list[str],
    durations:   list[float],
    output_path: str,
    crossfade:   float = 0.25,
    status_cb:   Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    if len(clip_paths) == 1:
        # Single clip: just re-encode to the target format (no xfade needed)
        cmd = [
            FFMPEG_BIN, "-y", "-i", clip_paths[0],
            "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", FFMPEG_PRESET,
            "-threads", "1",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            if status_cb:
                status_cb(f"  ⚠ single-clip encode failed:\n{(r.stderr or '')[-600:]}")
            return None
        return output_path

    # Clamp crossfade so it is never ≥ 40 % of the shortest clip's duration.
    # xfade crashes if the offset would land past the end of a clip.
    if durations:
        safe_max = max(0.0, min(durations) * 0.4)
        crossfade = min(crossfade, safe_max)
    if crossfade < 0.05:
        return _simple_concat(clip_paths, output_path, status_cb)

    n, inputs = len(clip_paths), []
    for p in clip_paths:
        inputs += ["-i", p]

    parts, cum, prev_v, prev_a = [], 0.0, "[0:v]", "[0:a]"
    for i in range(1, n):
        cum   += durations[i - 1] - crossfade
        last   = (i == n - 1)
        tag_v  = "[vout]" if last else f"[v{i}]"
        tag_a  = "[aout]" if last else f"[a{i}]"
        parts.append(
            f"{prev_v}[{i}:v]xfade=transition=fade"
            f":duration={crossfade:.4f}:offset={cum:.4f}{tag_v}"
        )
        parts.append(f"{prev_a}[{i}:a]acrossfade=d={crossfade:.4f}{tag_a}")
        prev_v, prev_a = tag_v, tag_a

    cmd = (
        [FFMPEG_BIN, "-y"] + inputs
        + ["-filter_complex", ";".join(parts),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", FFMPEG_PRESET,
           "-threads", "1",         # single-threaded encode → less frame-buffer RAM
           "-c:a", "aac", "-b:a", AUDIO_BITRATE,
           "-ar", "44100", "-ac", "2",
           "-movflags", "+faststart",
           output_path]
    )
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        if status_cb:
            status_cb("  xfade failed — falling back to simple concat…")
        return _simple_concat(clip_paths, output_path, status_cb)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
#  Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

def assemble_video(
    clip_data:         list[dict],
    preset:            dict,
    output_path:       str,
    video_title:       str                             = "",
    title_word_colors: list[str]                       = None,
    status_cb:         Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Full pipeline with raw clip paths:

      1. Sort clips rank DESC  (rank N first → rank 1 last for climax)
      2. Per clip: generate overlay PNG (Pillow) then scale+burn (one FFmpeg pass)
      3. Concatenate with crossfade transitions

    Required clip_data keys: path, rank, title, duration, width, height
    Optional key:  commentary  (used by app.py for ElevenLabs timing, ignored here)

    Returns: path to the assembled .mp4, or None on failure.
    Also returns clip_timings_ms list via  assemble_video.timings  attribute
    so the caller can synchronise voiceovers.
    """
    def _log(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    if not clip_data:
        return None

    crossfade = float(preset.get("crossfade_duration", 0.25))

    # ── Memory guard: force stream-copy concat when voiceovers are present ────
    # Combined clips (intro+footage) already have natural black-screen pauses.
    # xfade re-encode of N combined clips uses ~350-400 MB FFmpeg; stream-copy ~50 MB.
    _has_voiceovers = any(c.get("voiceover_path") for c in clip_data)
    if _has_voiceovers and crossfade > 0:
        _log("  ℹ️  Voiceover mode: stream-copy concat (no xfade) — conserving RAM…")
        crossfade = 0.0   # triggers _simple_concat() path via crossfade < 0.05 guard

    # Play order : rank N plays first → rank 1 plays last (the grand reveal / climax)
    play_order    = sorted(clip_data, key=lambda c: c["rank"], reverse=True)
    # Display order: rank 1 at top → rank N at bottom  (ascending, matches what the UI shows)
    display_order = sorted(clip_data, key=lambda c: c["rank"])
    n             = len(play_order)

    # Compute start times (in play order) so the caller can sync voiceovers
    timings_ms: list[int] = []
    t = 0.0
    for i, c in enumerate(play_order):
        timings_ms.append(int(t * 1000))
        t += c["duration"] - (crossfade if i < n - 1 else 0)

    assemble_video.timings_ms = timings_ms  # type: ignore[attr-defined]

    # Adaptive CRF: when xfade will re-encode the intermediates use a higher
    # (faster) CRF; when stream-copy is used the intermediate CRF IS the final
    # quality so we keep the configured CRF from config.py.
    _use_xfade  = (crossfade >= 0.05 and n > 1)
    _render_crf = _INTERMEDIATE_CRF if _use_xfade else CRF

    _log(f"Rendering {n} clip(s) with ranking overlay"
         f"  [CRF={_render_crf}, {'xfade' if _use_xfade else 'stream-copy'} concat]…")

    # ── Phase 1: Generate all overlay PNGs in parallel (pure Pillow / CPU) ──
    # Each overlay writes to a unique path so there are no file-write races.
    # max_workers=2 keeps memory pressure low on constrained cloud instances.
    _log("  Generating overlay frames…")
    overlay_paths: list[str] = [""] * n

    def _make_overlay(play_idx: int) -> tuple[int, str]:
        display_idx = n - 1 - play_idx
        path = create_ranking_overlay(
            display_order, display_idx, preset,
            video_title=video_title,
            title_word_colors=title_word_colors,
            index=play_idx,
        )
        return play_idx, path

    with ThreadPoolExecutor(max_workers=min(n, 2)) as _pool:
        futures = {_pool.submit(_make_overlay, i): i for i in range(n)}
        for fut in as_completed(futures):
            try:
                _idx, _path = fut.result()
                overlay_paths[_idx] = _path
            except Exception as _ov_exc:
                _log(f"  ⚠ Overlay generation failed for slot {futures[fut]}: {_ov_exc}")

    # ── Phase 2: Render each clip sequentially (FFmpeg — Streamlit log-safe) ─
    # When a voiceover black-intro exists, it is merged with the rendered clip
    # via stream-copy BEFORE being added to `rendered`.  This keeps the final
    # xfade concat down to N inputs (not 2N), halving FFmpeg's memory footprint
    # on constrained cloud containers.
    rendered:  list[str]   = []
    durations: list[float] = []

    for play_idx, clip in enumerate(play_order):
        rank = clip["rank"]
        _log(f"  [{play_idx + 1}/{n}] Rank #{rank} — \"{clip.get('title', '')}\"")

        # ── Render the actual clip first ──────────────────────────────────────
        out    = str(TEMP_DIR / f"rendered_{play_idx:02d}.mp4")
        result = _render_one_clip(
            clip, overlay_paths[play_idx], out,
            crf=_render_crf, status_cb=_log,
        )
        clip_dur = max(0.1, float(clip.get("duration", 1.0)))
        clip_out = result if result else clip["path"]
        if not result:
            _log("         ⚠ render failed — using raw clip as fallback")
        else:
            _log("         ✓")

        # ── Optional black intro: merge (intro + clip) into ONE segment ───────
        # Building a single combined segment (stream-copy) means the downstream
        # xfade concat sees N inputs instead of 2N — roughly half the FFmpeg
        # memory compared to feeding intros and clips separately.
        vo_path = clip.get("voiceover_path", "")
        if vo_path and Path(vo_path).exists():
            _intro_out = str(TEMP_DIR / f"intro_{play_idx:02d}.mp4")
            _log("         🎙 Building black intro (voiceover)…")
            _intro = _create_black_intro(
                vo_path, overlay_paths[play_idx], _intro_out,
                crf=_render_crf, status_cb=_log,
            )
            if _intro:
                _intro_dur = _probe_full(_intro).get("duration", 0.0)
                _log(f"         🎙 Intro {_intro_dur:.1f}s ✓ — merging with clip…")
                _combined_out = str(TEMP_DIR / f"combined_{play_idx:02d}.mp4")
                _merged = _simple_concat([_intro, clip_out], _combined_out,
                                         status_cb=_log)
                if _merged:
                    rendered.append(_merged)
                    durations.append(max(0.1, _intro_dur) + clip_dur)
                    _log("         🎙 Combined intro+clip ✓")
                else:
                    # Merge failed — fall back to clip only (voiceover lost)
                    _log("         ⚠ Combine failed — using clip only")
                    rendered.append(clip_out)
                    durations.append(clip_dur)
            else:
                _log("         ⚠ Black intro failed — skipping (voiceover lost)")
                rendered.append(clip_out)
                durations.append(clip_dur)
        else:
            rendered.append(clip_out)
            durations.append(clip_dur)

    _log("Concatenating clips…")
    return concatenate_with_xfade(
        rendered, durations, output_path,
        crossfade=crossfade, status_cb=_log,
    )
