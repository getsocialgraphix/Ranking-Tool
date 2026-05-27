"""
Ranking Tool — Streamlit entry point.
Run with:  streamlit run app.py

Layout
──────
LEFT  (tabs)  → 🎞 Clips  |  ⚙️ Advanced (style + audio settings)
RIGHT (plain) → 👁 Preview (top)  +  🎬 Generate (bottom)
Sidebar       → 💾 Presets  +  ⚙ Output settings
"""

import io
import json
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from config import DEFAULT_PRESET, FFPROBE_BIN, OUTPUT_DIR, TEMP_DIR, setup_check
from core.audio import add_audio_overlays, mix_audio_for_video
from core.compositor import assemble_video, generate_preview_frame, list_fonts
from core.downloader import download_clips_parallel
from core.elevenlabs_tts import generate_voiceover, list_voices
from core.presets import delete_preset, list_presets, load_preset, save_preset
from core.processor import trim_clip

# Optional drag-and-drop component
# Catch Exception (not just ImportError) — newer Streamlit versions can raise
# AttributeError or other exceptions if the component ABI has changed.
try:
    from streamlit_sortables import sort_items as _sort_items_fn
    _HAS_SORTABLES = True
except Exception:
    _HAS_SORTABLES = False

# ── PWA icon generation ────────────────────────────────────────────────────────
# Generates icon-192.png / icon-512.png in ./static/ on first run.
# Uses the bundled Montserrat-Black font if available, falls back to default.
_STATIC_DIR = Path(__file__).parent / "static"
try:
    _STATIC_DIR.mkdir(exist_ok=True)
except Exception:
    pass  # static/ already exists or filesystem is read-only — harmless

def _generate_pwa_icons() -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
        _font_path = Path(__file__).parent / "assets" / "fonts" / "Montserrat-Black.ttf"
        for size in (192, 512):
            icon_path = _STATIC_DIR / f"icon-{size}.png"
            if icon_path.exists():
                continue
            img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            # Rounded rectangle background  (purple → indigo)
            r = size // 5
            draw.rounded_rectangle([0, 0, size - 1, size - 1],
                                   radius=r, fill=(124, 58, 237, 255))
            pad = size // 6
            draw.rounded_rectangle([pad, pad, size - pad - 1, size - pad - 1],
                                   radius=size // 7, fill=(79, 70, 229, 160))
            # "R" letter centred
            fs = size // 2
            try:
                font = ImageFont.truetype(str(_font_path), fs) if _font_path.exists() \
                       else ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()
            bb = draw.textbbox((0, 0), "R", font=font)
            draw.text(
                ((size - (bb[2] - bb[0])) // 2 - bb[0],
                 (size - (bb[3] - bb[1])) // 2 - bb[1]),
                "R", fill=(255, 255, 255, 245), font=font,
            )
            img.save(str(icon_path), "PNG")
    except Exception:
        pass  # icons are cosmetic — never crash the app

_generate_pwa_icons()

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Ranking Tool",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
/* ================================================================
   RANKING TOOL — 2026 DARK GLASSMORPHISM  |  MOBILE-FIRST UI
   Design: deep-dark base · purple/indigo accent · frosted glass
   ================================================================ */

/* ── Fonts & base ──────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

*, *::before, *::after { box-sizing: border-box; }

.stApp {
    background: radial-gradient(ellipse at 20% 20%, #0f0a1e 0%, #08080f 40%, #0a0d12 100%) !important;
    font-family: 'Inter', 'Segoe UI Variable', -apple-system, BlinkMacSystemFont, system-ui, sans-serif !important;
    min-height: 100vh;
}

/* ── Layout container ──────────────────────────────────────────── */
.block-container {
    padding: 1.25rem 1.75rem 2.5rem !important;
    max-width: 100% !important;
}

/* ── Hero header ───────────────────────────────────────────────── */
.rt-hero {
    background: linear-gradient(135deg,
        rgba(124,58,237,0.18) 0%,
        rgba(79,70,229,0.12) 50%,
        rgba(6,182,212,0.08) 100%);
    border: 1px solid rgba(167,139,250,0.15);
    border-radius: 20px;
    padding: 1.1rem 1.4rem;
    margin-bottom: 1.2rem;
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    display: flex;
    align-items: center;
    gap: 12px;
}
.rt-hero-icon { font-size: 2rem; line-height: 1; }
.rt-hero-title {
    font-size: 1.55rem;
    font-weight: 800;
    background: linear-gradient(135deg, #c4b5fd 0%, #818cf8 50%, #67e8f9 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    line-height: 1.15;
    letter-spacing: -0.02em;
}
.rt-hero-sub {
    font-size: 0.72rem;
    color: rgba(196,181,253,0.55);
    font-weight: 500;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-top: 2px;
}

/* ── Headings ──────────────────────────────────────────────────── */
h1, h2, h3 {
    letter-spacing: -0.02em;
    font-weight: 700;
}
h2[data-testid], .stSubheader {
    background: linear-gradient(135deg, #c4b5fd, #818cf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

/* ── Sidebar ───────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background: rgba(8, 6, 18, 0.96) !important;
    border-right: 1px solid rgba(124,58,237,0.15) !important;
    backdrop-filter: blur(28px) saturate(160%) !important;
    -webkit-backdrop-filter: blur(28px) saturate(160%) !important;
}
[data-testid="stSidebar"] > div:first-child {
    padding-top: 1.2rem;
}
[data-testid="stSidebarContent"] .stMarkdown h1,
[data-testid="stSidebarContent"] .stMarkdown h2,
[data-testid="stSidebarContent"] .stMarkdown h3 {
    background: linear-gradient(135deg, #c4b5fd, #67e8f9);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

/* ── Glass cards (bordered containers) ─────────────────────────── */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 18px !important;
    overflow: hidden;
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
    background: rgba(255,255,255,0.032) !important;
    border: 1px solid rgba(255,255,255,0.075) !important;
    border-radius: 18px !important;
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    transition: border-color 0.25s ease, box-shadow 0.25s ease;
}
[data-testid="stVerticalBlockBorderWrapper"] > div:hover {
    border-color: rgba(167,139,250,0.22) !important;
    box-shadow: 0 0 28px rgba(124,58,237,0.12), 0 4px 24px rgba(0,0,0,0.3);
}

/* ── Expanders ─────────────────────────────────────────────────── */
[data-testid="stExpander"] {
    background: rgba(255,255,255,0.022) !important;
    border: 1px solid rgba(255,255,255,0.065) !important;
    border-radius: 14px !important;
    overflow: hidden;
    margin-bottom: 6px;
    transition: border-color 0.2s;
}
[data-testid="stExpander"]:hover {
    border-color: rgba(167,139,250,0.18) !important;
}
[data-testid="stExpander"] summary {
    font-weight: 600 !important;
    font-size: 0.88rem !important;
    letter-spacing: 0.01em;
    padding: 0.55rem 0.9rem !important;
    color: rgba(228,220,255,0.85) !important;
}
[data-testid="stExpander"] summary:hover {
    color: #c4b5fd !important;
}

/* ── Tabs ──────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
    background: rgba(255,255,255,0.04) !important;
    border-radius: 14px !important;
    padding: 4px !important;
    gap: 3px !important;
    border: 1px solid rgba(255,255,255,0.065);
}
.stTabs [data-baseweb="tab"] {
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: 0.85rem !important;
    transition: all 0.2s ease;
    padding: 6px 16px !important;
    color: rgba(255,255,255,0.5) !important;
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, rgba(124,58,237,0.35), rgba(79,70,229,0.25)) !important;
    color: #c4b5fd !important;
    box-shadow: 0 2px 12px rgba(124,58,237,0.25);
}

/* ── Buttons ───────────────────────────────────────────────────── */
.stButton > button {
    border-radius: 12px !important;
    font-weight: 600 !important;
    font-size: 0.86rem !important;
    transition: all 0.22s ease !important;
    letter-spacing: 0.02em;
    min-height: 40px;
    padding: 0 1rem !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%) !important;
    border: none !important;
    color: #fff !important;
    box-shadow: 0 4px 20px rgba(124,58,237,0.4) !important;
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%) !important;
    box-shadow: 0 6px 32px rgba(124,58,237,0.6) !important;
    transform: translateY(-1px);
}
.stButton > button[kind="primary"]:active {
    transform: translateY(0px);
    box-shadow: 0 2px 12px rgba(124,58,237,0.4) !important;
}
.stButton > button:not([kind="primary"]) {
    background: rgba(255,255,255,0.055) !important;
    border: 1px solid rgba(255,255,255,0.11) !important;
    color: rgba(196,181,253,0.85) !important;
}
.stButton > button:not([kind="primary"]):hover {
    background: rgba(167,139,250,0.1) !important;
    border-color: rgba(167,139,250,0.28) !important;
    color: #c4b5fd !important;
}

/* Download button */
[data-testid="stDownloadButton"] > button {
    background: linear-gradient(135deg, #059669 0%, #0891b2 100%) !important;
    border: none !important;
    border-radius: 12px !important;
    color: #fff !important;
    font-weight: 700 !important;
    box-shadow: 0 4px 20px rgba(5,150,105,0.35) !important;
}
[data-testid="stDownloadButton"] > button:hover {
    box-shadow: 0 6px 28px rgba(5,150,105,0.55) !important;
    transform: translateY(-1px);
}

/* ── Labels (universal) ────────────────────────────────────────── */
.stTextInput > label, .stTextArea > label,
.stNumberInput > label, .stSelectbox > label,
.stSlider > label, .stFileUploader > label,
.stRadio > label, .stCheckbox > label,
.stColorPicker > label {
    font-size: 0.72rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.07em !important;
    text-transform: uppercase !important;
    color: rgba(196,181,253,0.6) !important;
    margin-bottom: 5px !important;
}

/* ── Text inputs & textareas ───────────────────────────────────── */
.stTextInput input, .stTextArea textarea {
    background: rgba(255,255,255,0.048) !important;
    border: 1px solid rgba(255,255,255,0.1) !important;
    border-radius: 11px !important;
    color: #e8e8f2 !important;
    font-size: 0.9rem !important;
    transition: border-color 0.2s, box-shadow 0.2s;
    padding: 8px 12px !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color: rgba(124,58,237,0.55) !important;
    box-shadow: 0 0 0 3px rgba(124,58,237,0.14) !important;
    outline: none;
}

/* ── Number inputs & selects ───────────────────────────────────── */
.stNumberInput input {
    background: rgba(255,255,255,0.048) !important;
    border: 1px solid rgba(255,255,255,0.1) !important;
    border-radius: 11px !important;
    color: #e8e8f2 !important;
}
.stSelectbox > div > div {
    background: rgba(255,255,255,0.048) !important;
    border: 1px solid rgba(255,255,255,0.1) !important;
    border-radius: 11px !important;
    color: #e8e8f2 !important;
}

/* ── Radio buttons ─────────────────────────────────────────────── */
.stRadio > div {
    gap: 6px !important;
    flex-direction: row !important;
    flex-wrap: wrap !important;
}
.stRadio [data-baseweb="radio"] > div:first-child {
    border-color: rgba(124,58,237,0.4) !important;
}

/* ── Checkboxes ────────────────────────────────────────────────── */
.stCheckbox [data-baseweb="checkbox"] span {
    border-color: rgba(124,58,237,0.5) !important;
    border-radius: 5px !important;
}
.stCheckbox [data-testid="stWidgetLabel"] {
    font-size: 0.85rem !important;
    font-weight: 500 !important;
    color: rgba(228,220,255,0.8) !important;
    text-transform: none !important;
    letter-spacing: 0 !important;
}

/* ── Sliders ───────────────────────────────────────────────────── */
[data-testid="stSlider"] > div > div {
    background: rgba(255,255,255,0.1) !important;
    border-radius: 999px !important;
    height: 5px !important;
}
[data-testid="stSlider"] > div > div > div {
    background: linear-gradient(90deg, #7c3aed, #4f46e5) !important;
    border-radius: 999px !important;
}
[data-testid="stSlider"] [role="slider"] {
    background: #fff !important;
    border: 2px solid #a78bfa !important;
    box-shadow: 0 0 0 3px rgba(124,58,237,0.3), 0 2px 8px rgba(0,0,0,0.4) !important;
    width: 18px !important;
    height: 18px !important;
    cursor: grab;
}
[data-testid="stSlider"] [role="slider"]:active { cursor: grabbing; }

/* ── Progress bar ──────────────────────────────────────────────── */
[data-testid="stProgress"] > div {
    background: rgba(255,255,255,0.08) !important;
    border-radius: 999px !important;
    height: 7px !important;
    overflow: hidden;
}
[data-testid="stProgress"] > div > div {
    background: linear-gradient(90deg, #7c3aed, #818cf8, #06b6d4) !important;
    border-radius: 999px !important;
    transition: width 0.35s ease;
    box-shadow: 0 0 10px rgba(124,58,237,0.5);
}

/* ── File uploader ─────────────────────────────────────────────── */
[data-testid="stFileUploader"] > div {
    background: rgba(255,255,255,0.025) !important;
    border: 1.5px dashed rgba(167,139,250,0.22) !important;
    border-radius: 14px !important;
    transition: all 0.2s ease;
}
[data-testid="stFileUploader"] > div:hover {
    border-color: rgba(167,139,250,0.45) !important;
    background: rgba(124,58,237,0.06) !important;
    box-shadow: 0 0 20px rgba(124,58,237,0.08);
}

/* ── Alerts ────────────────────────────────────────────────────── */
[data-testid="stAlert"] {
    border-radius: 13px !important;
    backdrop-filter: blur(8px);
    font-size: 0.86rem !important;
}

/* ── Code / log block ──────────────────────────────────────────── */
.stCodeBlock, [data-testid="stCode"] {
    background: rgba(0,0,0,0.5) !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    border-radius: 12px !important;
    font-size: 0.78rem !important;
}

/* ── Dividers ──────────────────────────────────────────────────── */
hr {
    border: none !important;
    border-top: 1px solid rgba(255,255,255,0.07) !important;
    margin: 1rem 0 !important;
}

/* ── Captions ──────────────────────────────────────────────────── */
[data-testid="stCaptionContainer"], .stCaption {
    color: rgba(255,255,255,0.38) !important;
    font-size: 0.74rem !important;
}

/* ── Color pickers ─────────────────────────────────────────────── */
[data-testid="stColorPicker"] > div {
    border: 1px solid rgba(255,255,255,0.1) !important;
    border-radius: 10px !important;
    overflow: hidden;
}

/* ── Selected-clip highlight bar ───────────────────────────────── */
.sel-bar {
    height: 3px;
    background: linear-gradient(90deg, #7c3aed, #06b6d4);
    border-radius: 999px;
    margin-bottom: 8px;
    box-shadow: 0 0 14px rgba(124,58,237,0.55);
    animation: sel-pulse 2s ease-in-out infinite;
}
@keyframes sel-pulse {
    0%,100% { opacity: 1; }
    50%      { opacity: 0.65; }
}

/* ── Scrollbar ─────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: rgba(255,255,255,0.03); border-radius: 999px; }
::-webkit-scrollbar-thumb { background: rgba(124,58,237,0.4); border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: rgba(124,58,237,0.65); }

/* ── Spinner ───────────────────────────────────────────────────── */
[data-testid="stSpinner"] > div {
    border-top-color: #7c3aed !important;
}

/* ================================================================
   MOBILE  —  ≤ 768 px
   Stack columns, thumb-friendly targets, tighter padding
   ================================================================ */
@media (max-width: 768px) {

    .block-container {
        padding: 0.6rem 0.55rem 1.5rem !important;
    }

    /* Stack left / right columns vertically */
    [data-testid="stHorizontalBlock"] {
        flex-direction: column !important;
        gap: 0.8rem !important;
    }
    [data-testid="column"] {
        width: 100% !important;
        min-width: 100% !important;
        flex: none !important;
    }

    /* Hero shrinks gracefully */
    .rt-hero { padding: 0.85rem 1rem; gap: 10px; }
    .rt-hero-icon { font-size: 1.6rem; }
    .rt-hero-title { font-size: 1.2rem; }

    /* Headings */
    h1 { font-size: 1.3rem !important; }
    h2 { font-size: 1.05rem !important; }
    h3 { font-size: 0.95rem !important; }

    /* Buttons — thumb friendly */
    .stButton > button {
        min-height: 46px !important;
        font-size: 0.9rem !important;
    }

    /* Cards */
    [data-testid="stVerticalBlockBorderWrapper"] > div {
        border-radius: 14px !important;
        padding: 0.65rem !important;
    }

    /* Expanders */
    [data-testid="stExpander"] { margin-bottom: 4px; }
    [data-testid="stExpander"] summary { font-size: 0.83rem !important; padding: 0.5rem 0.75rem !important; }

    /* Tabs */
    .stTabs [data-baseweb="tab"] { padding: 6px 10px !important; font-size: 0.8rem !important; }

    /* Sliders — wider thumb for touch */
    [data-testid="stSlider"] [role="slider"] {
        width: 22px !important;
        height: 22px !important;
    }

    /* Labels smaller */
    .stTextInput > label, .stTextArea > label,
    .stNumberInput > label, .stSelectbox > label,
    .stSlider > label { font-size: 0.68rem !important; }

    /* Preview image full width */
    [data-testid="stImage"] img { border-radius: 14px; width: 100% !important; }
    [data-testid="stVideo"] { width: 100% !important; border-radius: 14px; overflow: hidden; }

    /* Stat columns inside cards — allow wrap */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
    }
}

/* ================================================================
   EXTRA SMALL  —  ≤ 480 px
   ================================================================ */
@media (max-width: 480px) {
    .block-container { padding: 0.4rem 0.35rem 1rem !important; }
    [data-testid="stVerticalBlockBorderWrapper"] > div {
        border-radius: 12px !important;
        padding: 0.5rem !important;
    }
    .stButton > button { min-height: 48px !important; border-radius: 14px !important; }
    .rt-hero { border-radius: 14px; }
}

/* ================================================================
   PWA / APP-FEEL  —  hide Streamlit browser chrome
   ================================================================ */

/* Footer "Made with Streamlit" */
footer { visibility: hidden !important; height: 0 !important; }

/* Top-right toolbar (deploy / share buttons) */
[data-testid="stToolbar"] { display: none !important; }

/* Coloured decoration ribbon at top */
[data-testid="stDecoration"] { display: none !important; }

/* Hamburger menu (three-line icon) — hide text inside, keep sidebar toggle */
#MainMenu { visibility: hidden !important; }

/* Viewer badge (bottom-right "Hosted with Streamlit") */
.viewerBadge_container__r5tak,
.viewerBadge_link__qRIco { display: none !important; }
</style>
""", unsafe_allow_html=True)


# ── PWA meta tags (injected into page — chrome treats link/meta in body fine) ─
st.markdown("""
<link rel="manifest"          href="/app/static/manifest.json">
<link rel="apple-touch-icon"  href="/app/static/icon-192.png">
<meta name="theme-color"                    content="#7c3aed">
<meta name="apple-mobile-web-app-capable"   content="yes">
<meta name="apple-mobile-web-app-title"     content="Rank">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="mobile-web-app-capable"         content="yes">
<meta name="application-name"               content="Rank">
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Session-state initialisation
# ═══════════════════════════════════════════════════════════════════════════════

def _blank_clip(rank: int = 1) -> dict:
    return {
        "id":             uuid.uuid4().hex[:8],
        "url":            "",
        "rank":           rank,
        "title":          "",
        "trim_start":     "",
        "trim_end":       "",
        "trim_start_sec": 0.0,
        "trim_end_sec":   0.0,
        "duration_hint":  0.0,
        "is_local":       False,
        "commentary":     "",
    }


_SS_DEFAULTS: dict = {
    "clips":             [_blank_clip(1)],
    "preset":            DEFAULT_PRESET.copy(),
    "video_title":       "",
    "title_word_colors": [],
    "output_video":      None,
    "processing":        False,
    "selected_clip_id":  None,
    "preview_idx":       0,
    # Bulk uploader tracking: {f"{filename}_{size}": clip_id}
    # keeps us from re-creating slots when Streamlit rerenders
    "bulk_seen":         {},
    # ElevenLabs
    "el_api_key":        "",
    "el_voices":         [],
    "el_voice_id":       "",
    "el_stability":      0.50,
    "el_similarity":     0.75,
    "el_style":          0.00,
    "el_model":          "eleven_multilingual_v2",
    "sfx_list":          [],
    "music_path":        None,
    # Auto-commentary: generate voiceover scripts from rank when commentary is blank
    "el_auto_commentary": True,
    # PWA notification — set True after generation, cleared after firing
    "notification_pending": False,
}
for _k, _v in _SS_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# Back-compat: ensure all clip fields exist
for _c in st.session_state.clips:
    if "id" not in _c:
        _c["id"] = uuid.uuid4().hex[:8]
    _c.setdefault("duration_hint",  0.0)
    _c.setdefault("trim_start_sec", 0.0)
    _c.setdefault("trim_end_sec",   0.0)

# Ensure overlay_style is complete
_ov = st.session_state.preset.setdefault("overlay_style", {})
for _dk, _dv in DEFAULT_PRESET["overlay_style"].items():
    _ov.setdefault(_dk, _dv)
_rp = _ov.setdefault("rank_prefixes", {})
for _ri in range(1, 11):
    _rp.setdefault(str(_ri), "")
for _ri in range(1, 11):
    st.session_state.preset["rank_colors"].setdefault(
        str(_ri), DEFAULT_PRESET["rank_colors"].get(str(_ri), "#FFFFFF")
    )


# ── Dependency check ──────────────────────────────────────────────────────────
_issues = setup_check()
if _issues:
    for _i in _issues:
        st.error(_i)
    st.stop()

# ── Hero header ────────────────────────────────────────────────────────────────
st.markdown("""
<div class="rt-hero">
  <div class="rt-hero-icon">🎬</div>
  <div>
    <div class="rt-hero-title">Ranking Tool</div>
    <div class="rt-hero-sub">AI-powered · 9:16 vertical · Auto-ranking overlays</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  Sidebar — presets + output settings
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("""
    <div style="
        background:linear-gradient(135deg,rgba(124,58,237,0.2),rgba(79,70,229,0.1));
        border:1px solid rgba(167,139,250,0.18);
        border-radius:14px;
        padding:0.75rem 1rem;
        margin-bottom:0.75rem;">
        <div style="font-size:1.05rem;font-weight:800;
                    background:linear-gradient(135deg,#c4b5fd,#67e8f9);
                    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
                    background-clip:text;letter-spacing:-0.02em;">
            ⚙️ Settings
        </div>
        <div style="font-size:0.68rem;color:rgba(196,181,253,0.5);
                    text-transform:uppercase;letter-spacing:0.07em;margin-top:2px;">
            Presets · Output · Controls
        </div>
    </div>
    """, unsafe_allow_html=True)

    with st.expander("💾 Presets", expanded=True):
        saved_presets = list_presets()
        sel = st.selectbox("Load preset", ["(default)"] + saved_presets, key="preset_sel")
        _pc1, _pc2 = st.columns(2)
        with _pc1:
            if sel != "(default)" and st.button("Load", use_container_width=True):
                p = load_preset(sel)
                p.setdefault("overlay_style", {})
                for dk, dv in DEFAULT_PRESET["overlay_style"].items():
                    p["overlay_style"].setdefault(dk, dv)
                rp2 = p["overlay_style"].setdefault("rank_prefixes", {})
                for ri in range(1, 11):
                    rp2.setdefault(str(ri), "")
                st.session_state.preset = p
                st.success(f"Loaded: {sel}")
                st.rerun()
        with _pc2:
            if sel != "(default)" and st.button("Delete", use_container_width=True, type="secondary"):
                delete_preset(sel)
                st.rerun()
        new_name = st.text_input("Save current as…", placeholder="my_style", key="preset_save_name")
        if st.button("💾 Save Preset", use_container_width=True) and new_name.strip():
            save_preset(new_name.strip(), st.session_state.preset)
            st.success(f"Saved: {new_name.strip()}")
            st.rerun()

    with st.expander("⚙ Output settings"):
        st.session_state.preset["crossfade_duration"] = st.slider(
            "Crossfade (s)", 0.0, 1.0,
            float(st.session_state.preset.get("crossfade_duration", 0.25)),
            step=0.05, key="cf_slider",
        )

    # ── Notifications ──────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("""
    <div style="font-size:0.72rem;font-weight:600;letter-spacing:0.07em;
                text-transform:uppercase;color:rgba(196,181,253,0.6);
                margin-bottom:6px;">🔔 Notifications</div>
    """, unsafe_allow_html=True)
    # Button asks the browser for push-notification permission
    if st.button("Enable Notifications", use_container_width=True, key="notif_perm_btn"):
        import streamlit.components.v1 as _sc
        _sc.html("""
<script>
(function() {
  var par = window.parent;
  if (!('Notification' in par)) {
    alert('This browser does not support desktop notifications.');
    return;
  }
  if (par.Notification.permission === 'granted') {
    new par.Notification('🎬 Ranking Tool', {
      body: 'Notifications are already enabled!',
      icon: '/app/static/icon-192.png',
    });
  } else if (par.Notification.permission !== 'denied') {
    par.Notification.requestPermission().then(function(perm) {
      if (perm === 'granted') {
        new par.Notification('🎬 Ranking Tool', {
          body: 'Notifications enabled! You\\'ll be alerted when your video is ready.',
          icon: '/app/static/icon-192.png',
        });
      }
    });
  } else {
    alert('Notifications are blocked. Please enable them in your browser settings for this site.');
  }
})();
</script>
""", height=1)
    st.caption("Tap to allow alerts when video generation finishes.")

    # ── PWA install hint ────────────────────────────────────────────────────────
    st.markdown("""
    <div style="margin-top:8px;padding:8px 10px;
                background:rgba(124,58,237,0.08);
                border:1px solid rgba(167,139,250,0.15);
                border-radius:10px;font-size:0.72rem;
                color:rgba(196,181,253,0.65);line-height:1.5;">
        📱 <strong style="color:rgba(196,181,253,0.85);">Add to Home Screen</strong><br>
        iOS Safari: Share → Add to Home Screen<br>
        Android Chrome: Menu → Add to Home Screen
    </div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _auto_commentary(rank: int, n_total: int,
                     clip_title: str = "", video_title: str = "") -> str:
    """
    Build a natural-sounding narrator line for a ranking position.

    Called when a clip has no custom commentary but ElevenLabs is configured
    and auto-commentary is enabled.  The output mirrors the countdown style
    that characterises viral ranking videos: highest rank first (suspense),
    #1 last (grand reveal).

    Examples
    --------
    rank=5, n_total=5, title="Spider-Man NWH"
        → "Coming in at number 5... Spider-Man No Way Home!"
    rank=2, title=""
        → "Coming in at number 2!"
    rank=1, title="Avengers Endgame"
        → "And the number 1 is... Avengers Endgame!"
    """
    t = clip_title.strip()
    title_part = f"... {t}!" if t else "!"

    if rank == 1:
        if t:
            return f"And the number 1 is... {t}!"
        return "And the number 1!"
    elif rank == 2:
        return f"Coming in at number 2{title_part}"
    elif rank == 3:
        return f"At number 3{title_part}"
    else:
        return f"Coming in at number {rank}{title_part}"


@st.cache_data(show_spinner=False)
def _cached_fonts() -> dict:
    return list_fonts()


@st.cache_data(show_spinner=False, max_entries=1)
def _cached_video_bytes(path: str, mtime: float) -> bytes:
    """Return video file bytes, cached by (path, mtime) so the file is only
    read once per generated video even across many page rerenders."""
    return Path(path).read_bytes()


@st.cache_data(show_spinner=False, max_entries=40, ttl=300)
def _cached_preview_frame(
    clips_json:  str,
    display_idx: int,
    preset_json: str,
    video_title: str,
    colors_json: str,
) -> bytes:
    """
    Generate the overlay preview PNG and return it as raw bytes.
    Cached by (clips, display_idx, preset, title, colors) so repeated
    interactions that don't change any of those are instant.
    TTL=300 s ensures stale font/style changes never persist too long.
    """
    import json as _json
    _sorted = _json.loads(clips_json)
    _preset = _json.loads(preset_json)
    _colors = _json.loads(colors_json) if colors_json else None
    path = generate_preview_frame(
        sorted_clips=_sorted,
        current_idx=display_idx,
        preset=_preset,
        video_title=video_title,
        title_word_colors=_colors,
    )
    with open(path, "rb") as _fh:
        return _fh.read()


def _parse_mmss(t: str) -> float:
    if not t or not t.strip():
        return 0.0
    parts = t.strip().split(":")
    try:
        return int(parts[0]) * 60 + float(parts[1]) if len(parts) == 2 else float(parts[0])
    except ValueError:
        return 0.0


def _sec_to_mmss(s: float) -> str:
    m = int(s // 60)
    return f"{m}:{s % 60:05.2f}"


def _probe_duration_local(path: str) -> float:
    """Run FFprobe on a local file and return duration in seconds (0.0 on failure)."""
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return float(json.loads(r.stdout).get("format", {}).get("duration", 0.0))
    except Exception:
        pass
    return 0.0


def _title_words() -> list[str]:
    raw = st.session_state.get("video_title", "")
    return [w for w in raw.replace("\n", " ").split() if w]


def _sync_word_colors() -> None:
    words  = _title_words()
    colors = list(st.session_state.title_word_colors)
    n      = len(words)
    if len(colors) < n:
        colors.extend(["#FFFFFF"] * (n - len(colors)))
    st.session_state.title_word_colors = colors[:n]


def _label_for_clip(clip: dict, idx: int) -> str:
    rank  = clip.get("rank", "?")
    title = clip.get("title", "").strip()
    # Pick a human-readable source hint
    if clip.get("is_local") and clip.get("url"):
        src = re.sub(r"^clip_[0-9a-f]+_", "", Path(clip["url"]).name)[:22]
    elif clip.get("url"):
        src = clip["url"][-22:]
    else:
        src = f"Clip {idx + 1}"
    display = title[:24] if title else src
    cid = clip["id"]
    dur = clip.get("duration_hint", 0)
    dur_str = f"  {dur:.0f}s" if dur > 0 else ""
    return f"#{rank} — {display}{dur_str}  [{cid}]"


def _cid_from_label(label: str) -> str | None:
    m = re.search(r"\[([0-9a-f]{8})\]$", label)
    return m.group(1) if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  Main 2-column layout
# ═══════════════════════════════════════════════════════════════════════════════

left_col, right_col = st.columns([3, 2], gap="large")

_upload_dir = TEMP_DIR / "uploaded"
_upload_dir.mkdir(parents=True, exist_ok=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LEFT COLUMN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with left_col:
    tab_clips, tab_adv = st.tabs(["🎞 Clips", "⚙️ Advanced"])

    # ══════════════════════════════════════════════════════════════════════════
    #  CLIPS TAB
    # ══════════════════════════════════════════════════════════════════════════
    with tab_clips:

        # ── ① BULK MULTI-FILE UPLOADER ────────────────────────────────────────
        st.markdown("""
        <div style="font-size:0.72rem;font-weight:700;letter-spacing:0.07em;
                    text-transform:uppercase;color:rgba(196,181,253,0.6);
                    margin-bottom:6px;">📁 Drop all videos at once</div>
        """, unsafe_allow_html=True)

        _CLIP_TYPES = ["mp4", "mov", "avi", "mkv", "webm", "m4v", "ts", "mts"]
        _bulk_files = st.file_uploader(
            "Bulk upload",
            type=_CLIP_TYPES,
            accept_multiple_files=True,
            key="bulk_uploader",
            label_visibility="collapsed",
            help="Select or drop multiple videos — a clip slot is created for each one automatically.",
        )

        if _bulk_files:
            _added_count = 0
            for _uf in _bulk_files:
                _fkey = f"{_uf.name}_{_uf.size}"   # stable identity across rerenders
                if _fkey in st.session_state.bulk_seen:
                    continue  # already created a slot for this file

                # cap at 10 clips total
                if len(st.session_state.clips) >= 10:
                    st.warning("Maximum 10 clips reached — some uploads were skipped.")
                    break

                # Assign the next unused rank
                _used = {int(c.get("rank", 0)) for c in st.session_state.clips}
                _nr   = next((r for r in range(1, 11) if r not in _used), len(st.session_state.clips) + 1)

                # Save file
                _bcid = uuid.uuid4().hex[:8]
                _bdest = _upload_dir / f"clip_{_bcid}_{_uf.name}"
                _bdest.write_bytes(_uf.getvalue())

                # Auto-detect duration
                _bdur = _probe_duration_local(str(_bdest))

                # Auto-title: filename stem, cleaned up
                _auto_title = re.sub(r"[_\-]+", " ", Path(_uf.name).stem)[:32].strip()

                _new_clip = {
                    "id":             _bcid,
                    "url":            str(_bdest),
                    "rank":           _nr,
                    "title":          _auto_title,
                    "trim_start":     "",
                    "trim_end":       "",
                    "trim_start_sec": 0.0,
                    "trim_end_sec":   _bdur,
                    "duration_hint":  _bdur,
                    "is_local":       True,
                    "commentary":     "",
                }
                st.session_state.clips.append(_new_clip)
                st.session_state.bulk_seen[_fkey] = _bcid
                _added_count += 1

            if _added_count:
                st.rerun()

            # Show summary of what's loaded via bulk
            _bulk_names = [f.name for f in _bulk_files]
            st.markdown(
                f"<div style='font-size:0.75rem;color:rgba(167,139,250,0.7);"
                f"margin-top:4px;'>✓ {len(_bulk_names)} file(s) loaded — "
                f"edit details below</div>",
                unsafe_allow_html=True,
            )

        st.markdown("---")

        # ── ② DRAG-TO-REORDER (always visible, primary mobile interaction) ────
        _n_clips_now = len(st.session_state.clips)
        if _n_clips_now > 1:
            st.markdown("""
            <div style="font-size:0.72rem;font-weight:700;letter-spacing:0.07em;
                        text-transform:uppercase;color:rgba(196,181,253,0.6);
                        margin-bottom:4px;">⠿ Drag to reorder play sequence</div>
            """, unsafe_allow_html=True)

            if _HAS_SORTABLES:
                _drag_labels = [
                    _label_for_clip(c, i) for i, c in enumerate(st.session_state.clips)
                ]
                _new_labels = _sort_items_fn(_drag_labels, key="clip_drag_sortables")
                if _new_labels != _drag_labels:
                    _id_map    = {c["id"]: c for c in st.session_state.clips}
                    _reordered = []
                    for _lbl in _new_labels:
                        _cid2 = _cid_from_label(_lbl)
                        if _cid2 and _cid2 in _id_map:
                            _reordered.append(_id_map[_cid2])
                    if len(_reordered) == _n_clips_now:
                        st.session_state.clips = _reordered
                        st.rerun()
            else:
                st.caption("Install `streamlit-sortables` for drag-and-drop.")
        else:
            st.caption("Rank 1 = top of screen, revealed last for maximum impact. Add up to 10 clips.")

        st.markdown("---")

        # ── ③ CLIP CARDS — collapsible for mobile, full detail on expand ──────
        pending_delete: list[int] = []

        for i, clip in enumerate(st.session_state.clips):
            cid     = clip["id"]
            _is_sel = (st.session_state.get("selected_clip_id") == cid)

            # Card wrapper
            with st.container(border=True):

                if _is_sel:
                    st.markdown('<div class="sel-bar"></div>', unsafe_allow_html=True)

                # ── Always-visible header row ─────────────────────────────────
                _hc_rank, _hc_title, _hc_sel, _hc_del = st.columns([1, 5, 1, 1])

                with _hc_rank:
                    _rank_badge_color = st.session_state.preset["rank_colors"].get(
                        str(clip.get("rank", i + 1)), "#7c3aed"
                    )
                    st.markdown(
                        f"<div style='background:{_rank_badge_color};color:#fff;"
                        f"border-radius:8px;text-align:center;font-weight:800;"
                        f"font-size:1rem;padding:6px 2px;margin-top:2px;'>"
                        f"#{clip.get('rank', i+1)}</div>",
                        unsafe_allow_html=True,
                    )

                with _hc_title:
                    _card_title = clip.get("title", "").strip()
                    if not _card_title and clip.get("is_local") and clip.get("url"):
                        _card_title = re.sub(r"^clip_[0-9a-f]+_", "", Path(clip["url"]).name)
                    _dur_hint = clip.get("duration_hint", 0)
                    _dur_tag  = f" · {_dur_hint:.0f}s" if _dur_hint > 0 else ""
                    st.markdown(
                        f"<div style='font-weight:600;font-size:0.9rem;"
                        f"color:#e8e8f2;margin-top:4px;overflow:hidden;"
                        f"text-overflow:ellipsis;white-space:nowrap;'>"
                        f"{_card_title or 'Untitled'}"
                        f"<span style='color:rgba(196,181,253,0.5);font-size:0.75rem;'>"
                        f"{_dur_tag}</span></div>",
                        unsafe_allow_html=True,
                    )

                with _hc_sel:
                    if st.button("🔵" if _is_sel else "🎯", key=f"sel_{cid}",
                                 help="Preview this clip"):
                        st.session_state.selected_clip_id = None if _is_sel else cid
                        if not _is_sel:
                            _sel_rank  = int(clip.get("rank", 1))
                            _asc_clips = sorted(st.session_state.clips,
                                                key=lambda c: c.get("rank", 1))
                            _asc_ranks = [c.get("rank", 1) for c in _asc_clips]
                            if _sel_rank in _asc_ranks:
                                _sync_pi = _asc_ranks.index(_sel_rank)
                                st.session_state.preview_idx    = _sync_pi
                                st.session_state["prev_idx_sl"] = _sync_pi
                        st.rerun()

                with _hc_del:
                    if st.button("✕", key=f"del_{cid}", help="Remove clip"):
                        pending_delete.append(i)

                # ── Collapsible detail (clean on mobile) ──────────────────────
                with st.expander("Edit details", expanded=False):

                    # ── Source ────────────────────────────────────────────────
                    # Default to "📁 Upload" tab for bulk-uploaded (is_local) clips
                    # so the user can immediately see which file is attached.
                    _src_default = 1 if clip.get("is_local") else 0
                    mode = st.radio(
                        "Source", ["🔗 URL", "📁 Upload"],
                        index=_src_default,
                        key=f"mode_{cid}", horizontal=True, label_visibility="collapsed",
                    )
                    if mode == "🔗 URL":
                        cur = "" if clip.get("is_local") else clip.get("url", "")
                        clip["url"]      = st.text_input(
                            "URL", value=cur,
                            placeholder="YouTube / TikTok / Instagram…",
                            key=f"url_{cid}", label_visibility="collapsed",
                        )
                        clip["is_local"] = False
                    else:
                        # Per-clip single-file uploader (for swapping a specific clip)
                        uploaded = st.file_uploader(
                            "Swap video",
                            type=_CLIP_TYPES,
                            key=f"upload_{cid}", label_visibility="collapsed",
                        )
                        if uploaded is not None:
                            dest = _upload_dir / f"clip_{cid}_{uploaded.name}"
                            dest.write_bytes(uploaded.getvalue())
                            clip["url"]      = str(dest)
                            clip["is_local"] = True
                            if clip.get("duration_hint", 0) == 0:
                                _det = _probe_duration_local(str(dest))
                                if _det > 0:
                                    clip["duration_hint"] = _det
                                    clip["trim_end_sec"]  = _det
                            st.caption(f"✓ **{uploaded.name}** · {uploaded.size / 1_048_576:.1f} MB")
                        elif clip.get("is_local") and clip.get("url"):
                            _fn = re.sub(r"^clip_[0-9a-f]+_", "", Path(clip["url"]).name)
                            st.caption(f"✓ **{_fn}** — loaded")

                    # ── Rank + label ──────────────────────────────────────────
                    _r1, _r2 = st.columns([1, 3])
                    with _r1:
                        clip["rank"] = st.number_input(
                            "Rank", min_value=1, max_value=10,
                            value=int(clip.get("rank", i + 1)),
                            key=f"rank_{cid}",
                        )
                    with _r2:
                        clip["title"] = st.text_input(
                            "Label", value=clip.get("title", ""),
                            placeholder="e.g. Instant Regret 😂",
                            key=f"title_{cid}", label_visibility="collapsed",
                        )

                    # ── Trim sliders ──────────────────────────────────────────
                    with st.expander("✂️ Trim"):
                        _dur = float(clip.get("duration_hint", 0.0))

                        if clip.get("is_local") and clip.get("url") and _dur == 0:
                            if st.button("🔍 Detect duration", key=f"det_{cid}"):
                                _dur = _probe_duration_local(clip["url"])
                                if _dur > 0:
                                    clip["duration_hint"] = _dur
                                    clip["trim_end_sec"]  = _dur
                                    st.rerun()
                                else:
                                    st.warning("Could not detect — enter manually below.")

                        _dur_inp = st.number_input(
                            "Video length (s)",
                            min_value=0.0, value=_dur, step=1.0,
                            key=f"dur_{cid}",
                            help="Auto-filled for uploaded files. Enter manually for URL clips.",
                        )
                        if _dur_inp != _dur:
                            clip["duration_hint"] = _dur_inp
                            _dur = _dur_inp
                            if clip.get("trim_end_sec", 0) == 0:
                                clip["trim_end_sec"] = _dur_inp

                        if _dur > 0.5:
                            _ts0 = float(clip.get("trim_start_sec", 0.0))
                            _te0 = float(clip.get("trim_end_sec", _dur)) or _dur
                            _ts0 = max(0.0, min(_ts0, _dur - 0.25))
                            _te0 = max(_ts0 + 0.25, min(_te0, _dur))

                            _rng = st.slider(
                                "Trim range (s)",
                                0.0, _dur, (_ts0, _te0),
                                step=0.25, format="%.1fs",
                                key=f"trim_sl_{cid}",
                            )
                            clip["trim_start_sec"] = _rng[0]
                            clip["trim_end_sec"]   = _rng[1]
                            clip["trim_start"] = _sec_to_mmss(_rng[0]) if _rng[0] > 0.01 else ""
                            clip["trim_end"]   = _sec_to_mmss(_rng[1]) if _rng[1] < _dur - 0.01 else ""

                            _i1, _i2, _i3 = st.columns([2, 2, 3])
                            with _i1:
                                st.caption(f"▶ {_sec_to_mmss(_rng[0])}")
                            with _i2:
                                st.caption(f"⏹ {_sec_to_mmss(_rng[1])}")
                            with _i3:
                                st.caption(f"⏱ {_rng[1] - _rng[0]:.1f}s")

                            if st.button("✂️ Apply trim now", key=f"tapply_{cid}",
                                         help="Pre-trim with FFmpeg; trimmed clip feeds into Generate"):
                                with st.spinner("Trimming…"):
                                    _tpath = str(TEMP_DIR / f"manual_trim_{cid}.mp4")
                                    _res   = trim_clip(clip["url"], _rng[0], _rng[1], _tpath)
                                if _res:
                                    clip["url"]            = _tpath
                                    clip["is_local"]       = True
                                    clip["trim_start"]     = ""
                                    clip["trim_end"]       = ""
                                    clip["trim_start_sec"] = 0.0
                                    clip["trim_end_sec"]   = _rng[1] - _rng[0]
                                    clip["duration_hint"]  = _rng[1] - _rng[0]
                                    st.success(f"Trimmed to {_rng[1] - _rng[0]:.1f}s ✓")
                                    st.rerun()
                                else:
                                    st.error("Trim failed — check the file format.")
                        else:
                            st.caption("Enter video length above to enable sliders.")
                            _t1, _t2 = st.columns(2)
                            with _t1:
                                clip["trim_start"] = st.text_input(
                                    "Start (MM:SS)", value=clip.get("trim_start", ""),
                                    placeholder="0:00", key=f"ts_{cid}",
                                )
                            with _t2:
                                clip["trim_end"] = st.text_input(
                                    "End (MM:SS)", value=clip.get("trim_end", ""),
                                    placeholder="optional", key=f"te_{cid}",
                                )

                    # ── Voiceover commentary ──────────────────────────────────
                    clip["commentary"] = st.text_area(
                        "🎙️ Voiceover commentary  (ElevenLabs — optional)",
                        value=clip.get("commentary", ""),
                        placeholder="What to say when this clip plays…  Emoji OK 🔥",
                        key=f"commentary_{cid}",
                        height=55,
                    )

        # ── Apply deletes ─────────────────────────────────────────────────────
        for idx in sorted(pending_delete, reverse=True):
            _del_clip  = st.session_state.clips[idx]
            _del_id    = _del_clip.get("id")
            st.session_state.clips.pop(idx)
            if _del_id and st.session_state.get("selected_clip_id") == _del_id:
                st.session_state.selected_clip_id = None
            # Remove from bulk_seen so the slot can be re-created if needed
            _del_keys = [k for k, v in st.session_state.bulk_seen.items() if v == _del_id]
            for _dk in _del_keys:
                del st.session_state.bulk_seen[_dk]
        if pending_delete:
            st.rerun()

        if len(st.session_state.clips) < 10:
            if st.button("➕  Add URL / empty slot", use_container_width=True):
                existing_ranks = [int(c.get("rank", 1)) for c in st.session_state.clips]
                next_rank = min((max(existing_ranks) + 1) if existing_ranks else 1, 10)
                st.session_state.clips.append(_blank_clip(next_rank))
                st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    #  ADVANCED TAB
    # ══════════════════════════════════════════════════════════════════════════
    with tab_adv:
        ov        = st.session_state.preset["overlay_style"]
        _af       = _cached_fonts()
        _fn_names = list(_af.keys())
        _fn_paths = list(_af.values())

        def _fi(path: str) -> int:
            try:
                return _fn_paths.index(path)
            except ValueError:
                return 0

        st.markdown("#### 🎨 Visual Style")

        with st.expander("🏅 Per-Rank Customization", expanded=True):
            st.caption("Emoji prefix  ·  colour — per rank number")
            for rank_n in range(1, 11):
                _ra, _rb, _rc = st.columns([1, 4, 2])
                with _ra:
                    st.markdown(f"**#{rank_n}**")
                with _rb:
                    _pfx = ov["rank_prefixes"].get(str(rank_n), "")
                    ov["rank_prefixes"][str(rank_n)] = st.text_input(
                        f"Prefix {rank_n}", value=_pfx, placeholder="🥇 or blank",
                        key=f"pfx_{rank_n}", label_visibility="collapsed",
                    )
                with _rc:
                    _cc = st.session_state.preset["rank_colors"].get(str(rank_n), "#FFFFFF")
                    st.session_state.preset["rank_colors"][str(rank_n)] = st.color_picker(
                        f"Colour {rank_n}", _cc, key=f"rcolor_{rank_n}",
                        label_visibility="collapsed",
                    )

        with st.expander("🔢 Numbers"):
            _cnf = st.selectbox("Font", _fn_names, index=_fi(ov["num_font"]), key="num_font_sel")
            ov["num_font"] = _af[_cnf]
            _n1, _n2 = st.columns(2)
            with _n1:
                ov["num_size"]    = st.slider("Size (px)",         24, 140, int(ov["num_size"]),    key="num_sz")
                ov["num_spacing"] = st.slider("Line spacing (px)", 50, 320, int(ov["num_spacing"]), key="num_sp")
                ov["num_start_y"] = st.slider("Start Y (px)",      10, 500, int(ov["num_start_y"]), key="num_sy")
            with _n2:
                ov["num_x"]      = st.slider("X offset (px)",  0, 300, int(ov["num_x"]),     key="num_x_sl")
                ov["num_stroke"] = st.slider("Outline width",  0,  14, int(ov["num_stroke"]), key="num_str")
                ov["num_stroke_color"] = st.color_picker("Outline colour", ov["num_stroke_color"], key="num_sc")
            ov["num_use_rank_color"] = st.checkbox(
                "Use per-rank colour", value=bool(ov["num_use_rank_color"]), key="num_urc",
            )
            if not ov["num_use_rank_color"]:
                ov["num_color"] = st.color_picker("Number colour", ov["num_color"], key="num_clr")

        with st.expander("🏷️ Clip Labels  (appear when clip plays)"):
            _ctf = st.selectbox("Font", _fn_names, index=_fi(ov["title_font"]), key="ttl_font_sel")
            ov["title_font"] = _af[_ctf]
            _tl1, _tl2 = st.columns(2)
            with _tl1:
                ov["title_size"]   = st.slider("Size (px)",           10, 72, int(ov["title_size"]),   key="ttl_sz")
                ov["title_stroke"] = st.slider("Outline width",        0, 10, int(ov["title_stroke"]), key="ttl_str")
                ov["title_gap"]    = st.slider("Gap from number (px)", 0, 60, int(ov["title_gap"]),    key="ttl_gap")
            with _tl2:
                ov["title_color"]        = st.color_picker("Text colour",    ov["title_color"],        key="ttl_clr")
                ov["title_stroke_color"] = st.color_picker("Outline colour", ov["title_stroke_color"], key="ttl_sc")

        with st.expander("📛 Video Title Banner"):
            ov["banner_enabled"] = st.checkbox("Show banner", value=bool(ov["banner_enabled"]), key="bnr_on")
            if ov["banner_enabled"]:
                _cbf = st.selectbox("Font", _fn_names, index=_fi(ov["banner_font"]), key="bnr_font_sel")
                ov["banner_font"] = _af[_cbf]
                _bn1, _bn2 = st.columns(2)
                with _bn1:
                    ov["banner_size"]      = st.slider("Size (px)",           18, 96,  int(ov["banner_size"]),                key="bnr_sz")
                    ov["banner_stroke"]    = st.slider("Outline width",        0, 10,  int(ov["banner_stroke"]),              key="bnr_str")
                    ov["banner_bg_alpha"]  = st.slider("Background opacity",   0, 255, int(ov["banner_bg_alpha"]),            key="bnr_bg")
                    ov["banner_bg_height"] = st.slider("Background height (px — 0 = auto)",
                                                        0, 600, int(ov.get("banner_bg_height", 0)), key="bnr_h")
                with _bn2:
                    ov["banner_color"]        = st.color_picker("Text colour",    ov["banner_color"],        key="bnr_clr")
                    ov["banner_stroke_color"] = st.color_picker("Outline colour", ov["banner_stroke_color"], key="bnr_sc")

        with st.expander("🌑 Panel Background  (default: off)"):
            ov["panel_alpha"] = st.slider(
                "Opacity  (0 = transparent)", 0, 220, int(ov["panel_alpha"]), key="panel_a"
            )
            if int(ov["panel_alpha"]) > 0:
                ov["panel_width"] = st.slider("Width (px)", 80, 500, int(ov["panel_width"]), key="panel_w")

        st.divider()
        st.markdown("#### 🔊 Audio")

        with st.expander("🎵 Background Music", expanded=True):
            music_file = st.file_uploader(
                "Upload MP3 / WAV", type=["mp3", "wav"],
                label_visibility="collapsed", key="music_upload",
            )
            if music_file:
                dest = TEMP_DIR / f"music_{music_file.name}"
                dest.write_bytes(music_file.getvalue())
                st.session_state.music_path = str(dest)
                st.success(f"Loaded: {music_file.name}")

            if st.session_state.music_path:
                _dp = int(st.session_state.preset.get("music_duck_level", 0.12) * 100)
                _dp = st.slider("Duck during speech (%)", 1, 60, _dp, key="duck_sl")
                st.session_state.preset["music_duck_level"] = _dp / 100
                if st.button("Remove music", key="rm_music"):
                    st.session_state.music_path = None
                    st.rerun()
            else:
                st.caption("No music loaded.")

        with st.expander("🎙️ ElevenLabs Voiceovers"):
            try:
                st.caption(
                    "Connect your ElevenLabs API key and a voice -- "
                    "narrator lines are auto-generated for each rank position. "
                    "Override per clip using the commentary field in the Clips tab."
                )
                el_key = st.text_input(
                    "API Key", value=st.session_state.el_api_key,
                    type="password", placeholder="sk-...", key="el_api_input",
                )
                if el_key != st.session_state.el_api_key:
                    st.session_state.el_api_key  = el_key
                    st.session_state.el_voices   = []
                    st.session_state.el_voice_id = ""

                _ec1, _ec2 = st.columns([2, 1])
                with _ec2:
                    if st.button("🔌 Load voices", use_container_width=True) and el_key:
                        with st.spinner("Connecting..."):
                            voices = list_voices(el_key)
                        if voices:
                            st.session_state.el_voices   = voices
                            st.session_state.el_voice_id = voices[0]["id"]
                            st.success(f"{len(voices)} voices loaded")
                        else:
                            st.error("Could not load voices -- check API key.")

                if st.session_state.el_voices:
                    _vn  = [f"{v['name']}  [{v['category']}]" for v in st.session_state.el_voices]
                    _vid = [v["id"] for v in st.session_state.el_voices]
                    try:
                        _cvi = _vid.index(st.session_state.el_voice_id)
                    except ValueError:
                        _cvi = 0
                    _sv = st.selectbox("Voice", _vn, index=_cvi, key="el_voice_sel")
                    st.session_state.el_voice_id = _vid[_vn.index(_sv)]

                    _model_opts = {
                        "Multilingual v2 (best)": "eleven_multilingual_v2",
                        "Multilingual v1":        "eleven_multilingual_v1",
                        "English v1":             "eleven_monolingual_v1",
                        "Turbo v2 (fast)":        "eleven_turbo_v2",
                    }
                    _cur_model_key = next(
                        (k for k, v in _model_opts.items()
                         if v == st.session_state.get("el_model", "eleven_multilingual_v2")),
                        "Multilingual v2 (best)",
                    )
                    _sm = st.selectbox(
                        "Model", list(_model_opts.keys()),
                        index=list(_model_opts.keys()).index(_cur_model_key),
                        key="el_model_sel",
                    )
                    st.session_state.el_model = _model_opts[_sm]

                    with st.expander("Voice settings"):
                        st.session_state.el_stability = st.slider(
                            "Stability", 0.0, 1.0,
                            float(st.session_state.get("el_stability", 0.5)),
                            0.05, key="el_stab",
                        )
                        st.session_state.el_similarity = st.slider(
                            "Similarity boost", 0.0, 1.0,
                            float(st.session_state.get("el_similarity", 0.75)),
                            0.05, key="el_sim",
                        )
                        st.session_state.el_style = st.slider(
                            "Style exaggeration", 0.0, 1.0,
                            float(st.session_state.get("el_style", 0.0)),
                            0.05, key="el_sty",
                        )

                    st.divider()
                    _auto_val = bool(st.session_state.get("el_auto_commentary", True))
                    st.session_state.el_auto_commentary = st.checkbox(
                        "🤖 Auto-generate commentary",
                        value=_auto_val,
                        key="el_auto_tog",
                        help=(
                            "When ON: clips with no commentary text automatically get a "
                            "narrator line based on their rank (e.g. 'Coming in at number 5"
                            "... Clip Title!'). Custom text you type always takes priority."
                        ),
                    )
                    if st.session_state.el_auto_commentary:
                        st.caption(
                            "Narrator lines are auto-written from rank + clip title. "
                            "Type custom text in each clip card to override."
                        )
                else:
                    st.info("Enter API key and click **Load voices**.")

            except Exception as _el_err:
                st.error(f"ElevenLabs section error: {_el_err}")
                with st.expander("Error details"):
                    import traceback as _tb
                    st.code(_tb.format_exc())

        with st.expander("💥 Sound Effects"):
            st.caption("Upload a clip and set the second it plays in the final video.")
            sfx_upload = st.file_uploader(
                "Upload SFX", type=["mp3", "wav", "ogg"],
                key="sfx_upload", label_visibility="collapsed",
            )
            if sfx_upload is not None:
                sfx_dest = TEMP_DIR / f"sfx_{sfx_upload.name}"
                sfx_dest.write_bytes(sfx_upload.getvalue())
                _s1, _s2, _s3 = st.columns([3, 2, 1])
                with _s1:
                    sfx_name = st.text_input("Label", value=sfx_upload.name.rsplit(".", 1)[0], key="sfx_nm")
                with _s2:
                    sfx_t = st.number_input("Start (s)", min_value=0.0, step=0.5, value=0.0, key="sfx_t")
                with _s3:
                    sfx_vol = st.number_input("Vol (dB)", value=0, min_value=-24, max_value=12, key="sfx_v")
                if st.button("➕ Add", key="sfx_add"):
                    st.session_state.sfx_list.append({
                        "name":      sfx_name,
                        "path":      str(sfx_dest),
                        "start_s":   float(sfx_t),
                        "volume_db": int(sfx_vol),
                    })
                    st.rerun()

            if st.session_state.sfx_list:
                st.markdown("**Queued:**")
                _to_rm: list[int] = []
                for si, sfx in enumerate(st.session_state.sfx_list):
                    _sc1, _sc2, _sc3, _sc4 = st.columns([3, 1, 1, 1])
                    with _sc1:
                        st.markdown(f"🔊 **{sfx['name']}**")
                    with _sc2:
                        st.caption(f"@{sfx['start_s']:.1f}s")
                    with _sc3:
                        st.caption(f"{sfx['volume_db']:+d}dB")
                    with _sc4:
                        if st.button("✕", key=f"sfx_del_{si}"):
                            _to_rm.append(si)
                for si in sorted(_to_rm, reverse=True):
                    st.session_state.sfx_list.pop(si)
                if _to_rm:
                    st.rerun()
            else:
                st.caption("No sound effects added yet.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RIGHT COLUMN — Preview (top)  +  Generate (bottom)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with right_col:

    # ══════════════════════════════════════════════════════════════════════════
    #  PREVIEW
    # ══════════════════════════════════════════════════════════════════════════

    st.markdown("""<div style="font-size:1.05rem;font-weight:700;
        background:linear-gradient(135deg,#c4b5fd,#818cf8);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text;margin-bottom:0.5rem;">👁 Preview</div>""",
        unsafe_allow_html=True)

    _all_clips = st.session_state.clips
    _n_all     = len(_all_clips)

    if _n_all == 0:
        st.caption("Add clips to see a preview.")
    else:
        # ── If a generated video exists, show it as the main preview ─────────
        _out_vid = st.session_state.get("output_video")
        _has_vid = bool(_out_vid and Path(_out_vid).exists())

        if _has_vid:
            st.video(str(_out_vid))
            st.caption("▲ Generated video preview — click Generate to update")
            if st.button("🖼 Switch to overlay preview", key="switch_overlay",
                         use_container_width=True):
                st.session_state.output_video = None
                st.rerun()
        else:
            # ── Static overlay preview ────────────────────────────────────────
            # Ascending sort: rank 1 at top, rank N at bottom
            _sorted_prev = sorted(
                [
                    {
                        "rank":  int(c.get("rank", i + 1)),
                        "title": c.get("title", "").strip() or f"#{c.get('rank', i + 1)}",
                    }
                    for i, c in enumerate(_all_clips)
                ],
                key=lambda x: x["rank"],
            )

            _max_pi = _n_all - 1

            # Sync slider when clip is selected via 🎯
            _sel_id = st.session_state.get("selected_clip_id")
            if _sel_id:
                _sc = next((c for c in _all_clips if c.get("id") == _sel_id), None)
                if _sc:
                    _sel_rank  = int(_sc.get("rank", 1))
                    _asc_ranks = [c["rank"] for c in _sorted_prev]
                    if _sel_rank in _asc_ranks:
                        _forced = _asc_ranks.index(_sel_rank)
                        st.session_state["prev_idx_sl"] = _forced
                        st.session_state.preview_idx    = _forced

            def _on_slider_change():
                st.session_state.selected_clip_id = None

            _auto_p = st.checkbox("Auto-update preview", value=True, key="prev_auto")
            _ref_p  = st.button("🖼️ Refresh", use_container_width=True, key="prev_refresh")

            if _max_pi > 0:
                # Slider 0 → first clip plays (rank N, bottom of list → display_idx n-1)
                # Slider n-1 → last clip plays (rank 1, top of list → display_idx 0)
                _cur_pi = st.slider(
                    "Timeline  (left = clip plays first, right = grand reveal)",
                    0, _max_pi,
                    min(int(st.session_state.get("preview_idx", 0)), _max_pi),
                    key="prev_idx_sl",
                    on_change=_on_slider_change,
                )
                st.session_state.preview_idx = _cur_pi
                # Invert: slider left = first to play = rank N at bottom of ascending list
                _display_idx = _max_pi - _cur_pi
            else:
                _cur_pi      = 0
                _display_idx = 0
                st.session_state.preview_idx = 0

            if _ref_p or _auto_p:
                try:
                    _sync_word_colors()
                    # Serialise inputs to hashable keys for the cache
                    _clips_json  = json.dumps(_sorted_prev,                sort_keys=True, default=str)
                    _preset_json = json.dumps(st.session_state.preset,     sort_keys=True, default=str)
                    _colors_json = json.dumps(st.session_state.title_word_colors or [])
                    _img_bytes   = _cached_preview_frame(
                        _clips_json, _display_idx, _preset_json,
                        st.session_state.video_title, _colors_json,
                    )
                    _cur_rank = _sorted_prev[_display_idx]["rank"] if _sorted_prev else "?"
                    _playing  = "grand reveal 🏆" if _display_idx == 0 else f"clip #{_n_all - _display_idx} of {_n_all}"
                    st.image(_img_bytes, use_container_width=True,
                             caption=f"Rank #{_cur_rank} highlighted  ·  {_playing}")
                except Exception as _exc:
                    st.warning(f"Preview error: {_exc}")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════════
    #  GENERATE
    # ══════════════════════════════════════════════════════════════════════════

    st.markdown("""<div style="font-size:1.05rem;font-weight:700;
        background:linear-gradient(135deg,#c4b5fd,#818cf8);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text;margin-bottom:0.5rem;">🎬 Generate</div>""",
        unsafe_allow_html=True)

    # Video title (multi-line)
    _new_title = st.text_area(
        "📝 Video title  (optional — top banner)",
        value=st.session_state.video_title,
        placeholder="Top 5 Viral Moments 🔥\nPress Enter for a 2nd line",
        key="video_title_area",
        height=74,
    )
    if _new_title != st.session_state.video_title:
        st.session_state.video_title = _new_title
        _sync_word_colors()

    # Per-word colour pickers
    _sync_word_colors()
    _tw = _title_words()
    if _tw:
        with st.expander(f"🎨 Word colours  ({len(_tw)} word{'s' if len(_tw) != 1 else ''})",
                         expanded=False):
            _cpr = 4
            for _rs in range(0, len(_tw), _cpr):
                _rw   = _tw[_rs:_rs + _cpr]
                _rcls = st.columns(len(_rw))
                for _ci, _word in enumerate(_rw):
                    _wi = _rs + _ci
                    with _rcls[_ci]:
                        _lbl = _word[:10] + ("…" if len(_word) > 10 else "")
                        st.session_state.title_word_colors[_wi] = st.color_picker(
                            _lbl, value=st.session_state.title_word_colors[_wi],
                            key=f"twc_{_wi}",
                        )

    st.divider()

    valid_clips = [c for c in st.session_state.clips if c.get("url", "").strip()]

    if not valid_clips:
        st.info("Add at least one clip URL or file to generate.")
    else:
        st.write(f"**{len(valid_clips)}** clip(s) ready")

        # ── Themed progress bar (sits ABOVE the button, hidden until generating) ─
        _prog_status_ph = st.empty()   # step label + percentage
        _prog_bar_ph    = st.empty()   # glass progress bar

        def _render_progress(pct: int, msg: str) -> None:
            """Render the glassmorphism progress bar above the Generate button."""
            _prog_status_ph.markdown(
                f"""
                <div style="display:flex;justify-content:space-between;
                            align-items:center;margin-bottom:6px;">
                    <span style="font-size:0.82rem;font-weight:600;
                                 color:rgba(196,181,253,0.95);">{msg}</span>
                    <span style="font-size:0.78rem;font-weight:700;
                                 color:rgba(167,139,250,0.8);
                                 background:rgba(124,58,237,0.15);
                                 padding:2px 10px;border-radius:999px;
                                 border:1px solid rgba(124,58,237,0.3);">{pct}%</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            _prog_bar_ph.markdown(
                f"""
                <div style="background:rgba(255,255,255,0.05);
                            border:1px solid rgba(167,139,250,0.2);
                            border-radius:999px;height:8px;
                            overflow:hidden;margin-bottom:14px;">
                    <div style="width:{pct}%;height:100%;
                                background:linear-gradient(90deg,#7c3aed,#6366f1,#06b6d4);
                                border-radius:999px;
                                transition:width 0.3s ease;"></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        generate_btn = st.button(
            "▶  Generate Video", type="primary",
            use_container_width=True,
            disabled=st.session_state.processing,
        )

        if generate_btn:
            st.session_state.processing   = True
            st.session_state.output_video = None

            # Persistent files read by the Monitor page
            _LOG_FILE   = TEMP_DIR / "pipeline.log"
            _STATE_FILE = TEMP_DIR / "pipeline_state.json"
            try:
                _LOG_FILE.write_text("", encoding="utf-8")   # fresh log
            except Exception:
                pass

            log_area     = st.empty()
            log_lines: list[str] = []

            def _log(msg: str) -> None:
                log_lines.append(msg)
                log_area.code("\n".join(log_lines[-18:]))
                # Write to persistent log file for Monitor page
                try:
                    ts = datetime.now().strftime("%H:%M:%S")
                    with open(_LOG_FILE, "a", encoding="utf-8") as _lf:
                        _lf.write(f"[{ts}] {msg}\n")
                except Exception:
                    pass

            def _advance(pct: int, msg: str) -> None:
                _render_progress(pct, msg)
                _log(msg)
                # Persist current step for Monitor page
                try:
                    _STATE_FILE.write_text(
                        json.dumps({
                            "pct": pct,
                            "msg": msg,
                            "ts":  datetime.now().strftime("%H:%M:%S"),
                        }),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            try:
                total     = len(valid_clips)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                # Step 1: Load / Download ──────────────────────────────────────
                sources  = [
                    {"url": c["url"].strip().strip("\"'"), "is_local": c.get("is_local", False)}
                    for c in valid_clips
                ]
                n_local  = sum(1 for s in sources if s["is_local"])
                n_remote = total - n_local
                if n_remote == 0:
                    fetch_msg = f"Loading {total} local clip(s)…"
                elif n_local == 0:
                    fetch_msg = f"Downloading {total} clip(s)…"
                else:
                    fetch_msg = f"Fetching {total} clip(s) ({n_local} local, {n_remote} remote)…"
                _advance(5, fetch_msg)

                dl_results = download_clips_parallel(sources, progress_cb=_log)

                def _dl_ok(r) -> bool:
                    return r is not None and not r.error and bool(r.path)

                for fi, fr in [(i, r) for i, r in enumerate(dl_results) if not _dl_ok(r)]:
                    _log(f"  ⚠ Clip {fi + 1} failed: {fr.error if fr else 'unknown'}")

                ok_pairs = [(i, r) for i, r in enumerate(dl_results) if _dl_ok(r)]
                if not ok_pairs:
                    st.error("All clip downloads failed.")
                    st.session_state.processing = False
                    st.stop()

                # Step 2: Trim ────────────────────────────────────────────────
                _advance(12, f"Preparing {len(ok_pairs)} clip(s)…")
                clip_data: list[dict] = []
                for seq_idx, (orig_idx, dl) in enumerate(ok_pairs):
                    cfg        = valid_clips[orig_idx]
                    input_path = dl.path
                    duration   = dl.duration_s

                    start_s = _parse_mmss(cfg.get("trim_start", ""))
                    end_s   = _parse_mmss(cfg.get("trim_end", "")) or dl.duration_s
                    if start_s > 0 or (end_s and end_s < dl.duration_s - 0.5):
                        trimmed = str(TEMP_DIR / f"trimmed_{orig_idx:02d}.mp4")
                        result  = trim_clip(input_path, start_s, end_s, trimmed)
                        if result:
                            input_path = trimmed
                            duration   = max(0.1, end_s - start_s)
                            _log(f"  Clip {seq_idx + 1}: trimmed {start_s:.1f}s – {end_s:.1f}s")

                    clip_data.append({
                        "path":       input_path,
                        "rank":       int(cfg.get("rank", seq_idx + 1)),
                        "title":      cfg.get("title", f"Clip {seq_idx + 1}"),
                        "duration":   duration,
                        "width":      dl.original_width,
                        "height":     dl.original_height,
                        "commentary": cfg.get("commentary", ""),
                    })
                    _log(f"  Clip {seq_idx + 1} — {duration:.1f}s  ✓")

                if not clip_data:
                    st.error("All clips failed preparation.")
                    st.session_state.processing = False
                    st.stop()

                # Step 3: ElevenLabs voiceovers ────────────────────────────────
                # Voiceovers are now baked into the video as black-screen intro
                # clips BEFORE each ranked clip starts — no post-mix timing needed.
                # The path is stored directly on each clip dict; the compositor
                # reads it and prepends the black intro during assembly.
                el_key = st.session_state.get("el_api_key", "").strip()
                el_vid = st.session_state.get("el_voice_id", "").strip()

                if el_key and el_vid:
                    _advance(17, "Generating ElevenLabs voiceovers…")
                    _auto_on  = st.session_state.get("el_auto_commentary", True)
                    _n_clips  = len(clip_data)
                    _vtitle   = st.session_state.get("video_title", "").strip()
                    # Countdown order: highest rank first (suspense), rank 1 last
                    _sft = sorted(clip_data, key=lambda c: c["rank"], reverse=True)
                    for vo_i, c in enumerate(_sft):
                        commentary = c.get("commentary", "").strip()
                        # Auto-generate a narrator line when commentary is blank
                        if not commentary and _auto_on:
                            commentary = _auto_commentary(
                                rank=c["rank"],
                                n_total=_n_clips,
                                clip_title=c.get("title", ""),
                                video_title=_vtitle,
                            )
                            _log(f"  🤖 Auto-commentary Rank #{c['rank']}: '{commentary}'")
                        if commentary:
                            vo_path = str(TEMP_DIR / f"voiceover_{vo_i:02d}.mp3")
                            out_vo  = generate_voiceover(
                                el_key, el_vid, commentary, vo_path,
                                stability=st.session_state.get("el_stability", 0.5),
                                similarity_boost=st.session_state.get("el_similarity", 0.75),
                                style=st.session_state.get("el_style", 0.0),
                                model_id=st.session_state.get("el_model", "eleven_multilingual_v2"),
                            )
                            if out_vo:
                                # Store on the clip dict — compositor will prepend
                                # a black-screen intro of exactly this audio's length.
                                c["voiceover_path"] = out_vo
                                _log(f"  🎙️ Voiceover for Rank #{c['rank']} ✓  (black intro will be inserted)")
                            else:
                                _log(f"  ⚠ Voiceover for Rank #{c['rank']} failed")

                # Step 4: Render + assemble ────────────────────────────────────
                _advance(22, "Rendering ranking overlays and assembling…")

                # ── Pre-flight: log every clip's state so failures are visible ─
                _log("  Clip validation:")
                for _cd in clip_data:
                    _p   = Path(_cd["path"])
                    _ok  = "✓" if _p.exists() else "✗ FILE MISSING"
                    _sz  = f"{_p.stat().st_size / 1e6:.1f} MB" if _p.exists() else "—"
                    _log(
                        f"    rank#{_cd['rank']} {_ok}  "
                        f"{_cd['width']}×{_cd['height']}  "
                        f"{_cd['duration']:.1f}s  {_sz}  {_p.name}"
                    )
                    if not _p.exists():
                        _log(f"    ERROR: file not found → {_p}")

                assembled_path = str(TEMP_DIR / f"assembled_{timestamp}.mp4")
                word_colors    = st.session_state.title_word_colors or None
                assembled = assemble_video(
                    clip_data=clip_data,
                    preset=st.session_state.preset,
                    output_path=assembled_path,
                    video_title=st.session_state.get("video_title", ""),
                    title_word_colors=word_colors,
                    status_cb=_log,
                )
                if not assembled:
                    st.error("Assembly failed — check the log above for FFmpeg error details.")
                    st.session_state.processing = False
                    st.stop()

                # Step 5: Mix SFX (voiceovers are now baked into black intros) ──
                # ElevenLabs voiceovers are embedded as black-screen clips by the
                # compositor in Step 4 — no post-mix timing pass needed.
                # Only sound effects still require the post-process overlay step.
                sfx_raw = st.session_state.get("sfx_list", [])
                sfx_ms  = [
                    {"path": s["path"], "start_ms": int(s["start_s"] * 1000),
                     "volume_db": s.get("volume_db", 0)}
                    for s in sfx_raw if s.get("path") and Path(s["path"]).exists()
                ]
                if sfx_ms:
                    _advance(75, "Mixing sound effects…")
                    ov_path = str(TEMP_DIR / f"with_overlays_{timestamp}.mp4")
                    result  = add_audio_overlays(assembled, ov_path, [], sfx_ms)
                    if result:
                        assembled = result

                # Step 6: Background music ─────────────────────────────────────
                _advance(82, "Mixing audio…")
                final_path = str(OUTPUT_DIR / f"ranking_{timestamp}.mp4")
                mixed = mix_audio_for_video(
                    video_path=assembled,
                    music_path=st.session_state.music_path,
                    output_path=final_path,
                    duck_level=float(st.session_state.preset.get("music_duck_level", 0.12)),
                    full_level=float(st.session_state.preset.get("music_full_level", 0.35)),
                )
                if not mixed:
                    _log("  Audio mix failed — using clip audio only")
                    shutil.copy2(assembled, final_path)

                _advance(100, "Done! 🎉")
                st.session_state.output_video        = final_path
                st.session_state.processing          = False
                st.session_state.notification_pending = True   # fire on next render
                st.rerun()   # reload so preview area shows the video

            except Exception as exc:
                _tb = traceback.format_exc()
                # Write full traceback to persistent log so Monitor page shows it
                try:
                    ts = datetime.now().strftime("%H:%M:%S")
                    with open(_LOG_FILE, "a", encoding="utf-8") as _ef:
                        _ef.write(f"\n[{ts}] ❌ PIPELINE EXCEPTION:\n{_tb}\n")
                except Exception:
                    pass
                st.error(f"❌ Pipeline error: {exc}")
                with st.expander("🔍 Full error details (share this when reporting)"):
                    st.code(_tb)
                st.session_state.processing = False

    # ── Completion notification (fires once on the render after generation) ──────
    if st.session_state.get("notification_pending"):
        st.session_state.notification_pending = False
        try:
            import streamlit.components.v1 as _components
            _components.html("""
<script>
(function() {
  var par = window.parent;

  // 1. Tab title flash (always works)
  var orig = par.document.title;
  var flashes = 0;
  var iv = setInterval(function() {
    flashes++;
    par.document.title = (flashes % 2 === 0) ? '✅ Video Ready! 🎬' : orig;
    if (flashes >= 12) { clearInterval(iv); par.document.title = orig; }
  }, 700);

  // 2. Subtle audio beep via Web Audio API
  try {
    var ctx = new (par.AudioContext || par.webkitAudioContext)();
    [[880, 0, 0.12], [1108, 0.14, 0.12], [1320, 0.28, 0.18]].forEach(function(t) {
      var osc = ctx.createOscillator();
      var g   = ctx.createGain();
      osc.connect(g); g.connect(ctx.destination);
      osc.type = 'sine';
      osc.frequency.value = t[0];
      g.gain.setValueAtTime(0, ctx.currentTime + t[1]);
      g.gain.linearRampToValueAtTime(0.25, ctx.currentTime + t[1] + 0.04);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t[1] + t[2]);
      osc.start(ctx.currentTime + t[1]);
      osc.stop(ctx.currentTime  + t[1] + t[2]);
    });
  } catch(e) {}

  // 3. Browser push notification (requires granted permission)
  try {
    if ('Notification' in par && par.Notification.permission === 'granted') {
      new par.Notification('🎬 Ranking Tool', {
        body: 'Your video is ready to download!',
        icon: '/app/static/icon-192.png',
        badge: '/app/static/icon-192.png',
        tag: 'ranking-done',
        renotify: true,
      });
    }
  } catch(e) {}
})();
</script>
""", height=1)
        except Exception:
            pass  # Notification is best-effort; never crash the page over it

    # ── Download button (shown below generate regardless of preview) ──────────
    if st.session_state.output_video:
        out = Path(st.session_state.output_video)
        if out.exists():
            sz = out.stat().st_size / 1_048_576
            st.caption(f"`{out.name}`  ·  {sz:.1f} MB")
            try:
                # Use cached bytes so large files are not re-read on every
                # rerender (e.g. when the user types the ElevenLabs API key).
                _vbytes = _cached_video_bytes(str(out), out.stat().st_mtime)
                st.download_button(
                    "⬇  Download MP4",
                    data=_vbytes,
                    file_name=out.name,
                    mime="video/mp4",
                    use_container_width=True,
                )
            except Exception as _dl_err:
                st.error(f"Could not prepare download: {_dl_err}")
            if st.button("🗑  Clear & start over", use_container_width=True):
                st.session_state.output_video = None
                for f in TEMP_DIR.rglob("*"):
                    if f.is_file():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                st.rerun()
