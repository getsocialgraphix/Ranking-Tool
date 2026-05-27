"""
Pipeline Monitor — live dashboard for the Ranking Tool.

Shows:
  • System health  (FFmpeg, ffprobe, CPU, RAM, disk)
  • Active FFmpeg processes
  • Current pipeline state + % progress
  • Live log tail  (written by app.py to TEMP_DIR/pipeline.log)
  • Temp & output directory file browser

Refresh manually or toggle auto-refresh (3-second loop).
"""

import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import FFMPEG_BIN, FFPROBE_BIN, OUTPUT_DIR, TEMP_DIR

# ── Persistent-state files written by app.py ────────────────────────────────
LOG_FILE   = TEMP_DIR / "pipeline.log"
STATE_FILE = TEMP_DIR / "pipeline_state.json"

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Monitor · Ranking Tool",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Optional psutil ───────────────────────────────────────────────────────────
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ═══════════════════════════════════════════════════════════════════════════════
#  Shared CSS (matches main app glassmorphism theme)
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

*, *::before, *::after { box-sizing: border-box; }

.stApp {
    background: radial-gradient(ellipse at 20% 20%, #0f0a1e 0%, #08080f 40%, #0a0d12 100%) !important;
    font-family: 'Inter', system-ui, sans-serif !important;
}
.block-container { padding: 1.25rem 1.75rem 2.5rem !important; max-width: 100% !important; }

/* Glass card */
.mon-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(167,139,250,0.15);
    border-radius: 16px;
    padding: 1rem 1.25rem;
    margin-bottom: 1rem;
}
.mon-card-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: rgba(196,181,253,0.6);
    margin-bottom: 0.75rem;
}

/* Metric pill */
.mon-metric {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(124,58,237,0.1);
    border: 1px solid rgba(124,58,237,0.25);
    border-radius: 999px;
    padding: 4px 14px;
    font-size: 0.82rem;
    font-weight: 600;
    color: rgba(230,220,255,0.9);
    margin: 3px 4px 3px 0;
}
.mon-metric.ok   { border-color:rgba(52,199,89,0.4);  background:rgba(52,199,89,0.08);  color:rgba(134,239,172,0.95); }
.mon-metric.warn { border-color:rgba(255,149,0,0.4);  background:rgba(255,149,0,0.08);  color:rgba(253,186,116,0.95); }
.mon-metric.err  { border-color:rgba(255,59,48,0.4);  background:rgba(255,59,48,0.08);  color:rgba(252,165,165,0.95); }

/* Progress bar */
.mon-bar-wrap {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(167,139,250,0.2);
    border-radius: 999px;
    height: 10px;
    overflow: hidden;
    margin: 8px 0 4px;
}
.mon-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #7c3aed, #6366f1, #06b6d4);
    border-radius: 999px;
    transition: width 0.4s ease;
}

/* Log code block */
.mon-log {
    background: rgba(0,0,0,0.4) !important;
    border: 1px solid rgba(167,139,250,0.12) !important;
    border-radius: 12px !important;
    font-size: 0.75rem !important;
    font-family: 'JetBrains Mono', 'Cascadia Code', monospace !important;
    max-height: 320px;
    overflow-y: auto;
    padding: 0.75rem !important;
    color: rgba(200,200,230,0.85) !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
}

/* File row */
.mon-file {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    font-size: 0.78rem;
    color: rgba(220,210,255,0.8);
}
.mon-file:last-child { border-bottom: none; }
.mon-file-size { color: rgba(167,139,250,0.6); font-size: 0.72rem; }

/* Process row */
.mon-proc {
    display: flex;
    gap: 10px;
    padding: 5px 0;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    font-size: 0.76rem;
    color: rgba(200,200,230,0.8);
}
.mon-proc:last-child { border-bottom: none; }

[data-testid="stMetric"] {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(167,139,250,0.12);
    border-radius: 12px;
    padding: 0.6rem 0.9rem !important;
}
[data-testid="stMetricLabel"] { font-size: 0.72rem !important; color: rgba(196,181,253,0.6) !important; }
[data-testid="stMetricValue"] { font-size: 1.3rem !important; font-weight: 700 !important; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _age(path: Path) -> str:
    secs = time.time() - path.stat().st_mtime
    if secs < 60:   return f"{int(secs)}s ago"
    if secs < 3600: return f"{int(secs/60)}m ago"
    return f"{int(secs/3600)}h ago"


def _check_binary(bin_path: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            [bin_path, "-version"],
            capture_output=True, text=True, timeout=6,
        )
        if r.returncode == 0:
            first_line = (r.stdout or r.stderr or "").split("\n")[0].strip()
            return True, first_line
        return False, f"exit {r.returncode}"
    except FileNotFoundError:
        return False, "not found on PATH"
    except Exception as exc:
        return False, str(exc)


def _get_ffmpeg_procs() -> list[dict]:
    """Return running ffmpeg processes (cross-platform)."""
    procs = []
    if _HAS_PSUTIL:
        try:
            for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "status"]):
                try:
                    if "ffmpeg" in (p.info["name"] or "").lower():
                        cmd = " ".join(p.info["cmdline"] or [])[:120]
                        procs.append({
                            "pid":    p.info["pid"],
                            "cpu":    p.info["cpu_percent"],
                            "status": p.info["status"],
                            "cmd":    cmd,
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass
    else:
        # Fallback: pgrep (Linux/macOS)
        try:
            r = subprocess.run(
                ["pgrep", "-a", "ffmpeg"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if parts:
                    procs.append({"pid": parts[0], "cpu": "?", "status": "running",
                                  "cmd": parts[1][:120] if len(parts) > 1 else ""})
        except Exception:
            pass
    return procs


def _read_pipeline_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_log(tail: int = 60) -> str:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-tail:])
    except Exception:
        return ""


def _list_dir(directory: Path, exts: set | None = None) -> list[Path]:
    try:
        files = [
            f for f in directory.rglob("*")
            if f.is_file() and (exts is None or f.suffix.lower() in exts)
        ]
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Page header
# ═══════════════════════════════════════════════════════════════════════════════

_hcol1, _hcol2, _hcol3 = st.columns([5, 1, 1])
with _hcol1:
    st.markdown(
        "<h2 style='margin:0;background:linear-gradient(90deg,#a78bfa,#818cf8);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;"
        "font-size:1.6rem;'>📊 Pipeline Monitor</h2>",
        unsafe_allow_html=True,
    )
    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
with _hcol2:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()
with _hcol3:
    auto = st.toggle("Auto", value=False, help="Auto-refresh every 3 s")

if auto:
    time.sleep(3)
    st.rerun()

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
#  Row 1 — System health metrics
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("<div class='mon-card-title'>🔧 System Health</div>", unsafe_allow_html=True)

_ffmpeg_ok,  _ffmpeg_ver  = _check_binary(FFMPEG_BIN)
_ffprobe_ok, _ffprobe_ver = _check_binary(FFPROBE_BIN)

_m1, _m2, _m3, _m4, _m5 = st.columns(5)

with _m1:
    st.metric(
        "FFmpeg",
        "✅ Ready" if _ffmpeg_ok else "❌ Missing",
        help=_ffmpeg_ver,
    )
with _m2:
    st.metric(
        "FFprobe",
        "✅ Ready" if _ffprobe_ok else "❌ Missing",
        help=_ffprobe_ver,
    )
with _m3:
    if _HAS_PSUTIL:
        _cpu = psutil.cpu_percent(interval=0.3)
        _col = "normal" if _cpu < 80 else "inverse"
        st.metric("CPU", f"{_cpu:.0f}%")
    else:
        st.metric("CPU", "install psutil")
with _m4:
    if _HAS_PSUTIL:
        _ram = psutil.virtual_memory()
        st.metric("RAM used", f"{_ram.percent:.0f}%",
                  delta=f"{_ram.available / 1e9:.1f} GB free",
                  delta_color="off")
    else:
        st.metric("RAM", "install psutil")
with _m5:
    if _HAS_PSUTIL:
        try:
            _disk = psutil.disk_usage(str(TEMP_DIR))
            st.metric("Disk free", f"{_disk.free / 1e9:.1f} GB")
        except Exception:
            st.metric("Disk", "N/A")
    else:
        st.metric("Disk", "install psutil")

st.markdown(
    f"<div style='font-size:0.72rem;color:rgba(167,139,250,0.5);margin-top:4px;'>"
    f"Platform: {platform.system()} {platform.release()} &nbsp;|&nbsp; "
    f"Python {platform.python_version()} &nbsp;|&nbsp; "
    f"psutil: {'✅' if _HAS_PSUTIL else '❌ (add to requirements.txt)'}"
    f"</div>",
    unsafe_allow_html=True,
)

st.markdown("<br>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  Row 2 — Pipeline state + log  |  Active processes
# ═══════════════════════════════════════════════════════════════════════════════

_left, _right = st.columns([3, 2])

# ── Left: pipeline state + log ────────────────────────────────────────────────
with _left:
    # Current pipeline state
    state = _read_pipeline_state()
    if state:
        _pct = int(state.get("pct", 0))
        _msg = state.get("msg", "")
        _ts  = state.get("ts", "")

        _done = _pct >= 100
        _clr  = "rgba(52,199,89,0.9)" if _done else "rgba(196,181,253,0.95)"

        st.markdown(
            f"""
            <div class='mon-card'>
                <div class='mon-card-title'>⚡ Current Pipeline Step</div>
                <div style='display:flex;justify-content:space-between;
                            align-items:center;margin-bottom:4px;'>
                    <span style='font-size:0.9rem;font-weight:600;color:{_clr};'>{_msg}</span>
                    <span style='font-size:0.85rem;font-weight:700;
                                 color:rgba(167,139,250,0.9);
                                 background:rgba(124,58,237,0.15);
                                 padding:2px 12px;border-radius:999px;
                                 border:1px solid rgba(124,58,237,0.3);'>{_pct}%</span>
                </div>
                <div class='mon-bar-wrap'>
                    <div class='mon-bar-fill' style='width:{_pct}%;'></div>
                </div>
                <div style='font-size:0.68rem;color:rgba(167,139,250,0.45);margin-top:4px;'>
                    Updated: {_ts}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='mon-card'>"
            "<div class='mon-card-title'>⚡ Pipeline State</div>"
            "<div style='color:rgba(167,139,250,0.45);font-size:0.82rem;'>"
            "No active pipeline — generate a video to see progress here.</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    # Log tail
    st.markdown("<div class='mon-card-title'>📋 Pipeline Log (last 60 lines)</div>",
                unsafe_allow_html=True)
    _log_txt = _read_log(60)
    if _log_txt:
        st.code(_log_txt, language=None)
    else:
        st.markdown(
            "<div style='color:rgba(167,139,250,0.4);font-size:0.82rem;'>"
            "No log yet — log appears here once a pipeline starts.</div>",
            unsafe_allow_html=True,
        )
    if LOG_FILE.exists():
        st.caption(f"Log file: `{LOG_FILE}`  ({_fmt_bytes(LOG_FILE.stat().st_size)})")

# ── Right: active FFmpeg processes ────────────────────────────────────────────
with _right:
    _procs = _get_ffmpeg_procs()
    _proc_title = f"🎬 Active FFmpeg Processes ({len(_procs)})"
    st.markdown(
        f"<div class='mon-card'>"
        f"<div class='mon-card-title'>{_proc_title}</div>",
        unsafe_allow_html=True,
    )
    if _procs:
        for p in _procs:
            st.markdown(
                f"<div class='mon-proc'>"
                f"<span style='color:rgba(167,139,250,0.7);font-weight:700;'>PID {p['pid']}</span>"
                f"<span style='color:rgba(196,181,253,0.5);'>{p['status']}</span>"
                f"<span style='color:rgba(200,200,230,0.7);word-break:break-all;'>{p['cmd']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            "<div style='color:rgba(167,139,250,0.4);font-size:0.82rem;'>"
            "No FFmpeg processes running.</div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # FFmpeg version detail
    st.markdown(
        f"<div class='mon-card'><div class='mon-card-title'>🔩 FFmpeg Details</div>",
        unsafe_allow_html=True,
    )
    if _ffmpeg_ok:
        st.markdown(
            f"<div style='font-size:0.75rem;color:rgba(134,239,172,0.85);"
            f"word-break:break-word;'>{_ffmpeg_ver}</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='font-size:0.75rem;color:rgba(252,165,165,0.85);'>"
            f"❌ {_ffmpeg_ver}</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div style='font-size:0.72rem;color:rgba(167,139,250,0.5);margin-top:6px;'>"
        f"Binary: <code>{FFMPEG_BIN}</code></div>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Row 3 — File browsers (temp + output)
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("<br>", unsafe_allow_html=True)
_fc1, _fc2 = st.columns(2)

def _render_file_list(title: str, directory: Path, exts: set | None = None,
                       max_files: int = 30) -> None:
    files = _list_dir(directory, exts)[:max_files]
    st.markdown(
        f"<div class='mon-card'>"
        f"<div class='mon-card-title'>{title} ({len(files)} files)</div>",
        unsafe_allow_html=True,
    )
    if not files:
        st.markdown(
            "<div style='color:rgba(167,139,250,0.4);font-size:0.82rem;'>Empty.</div>",
            unsafe_allow_html=True,
        )
    else:
        for f in files:
            try:
                _sz  = _fmt_bytes(f.stat().st_size)
                _ago = _age(f)
            except Exception:
                _sz = _ago = "?"
            _rel = str(f.relative_to(directory))
            st.markdown(
                f"<div class='mon-file'>"
                f"<span title='{f}'>📄 {_rel}</span>"
                f"<span class='mon-file-size'>{_sz} · {_ago}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)

with _fc1:
    _render_file_list("📁 Temp Directory", TEMP_DIR)

with _fc2:
    _render_file_list(
        "🎬 Output Videos", OUTPUT_DIR,
        exts={".mp4", ".mov", ".mkv"},
    )
    # Download latest output video if any exist
    _output_vids = _list_dir(OUTPUT_DIR, {".mp4", ".mov", ".mkv"})
    if _output_vids:
        _latest = _output_vids[0]
        with open(_latest, "rb") as _vf:
            st.download_button(
                f"⬇ Download latest: {_latest.name}",
                data=_vf,
                file_name=_latest.name,
                mime="video/mp4",
                use_container_width=True,
            )

# ═══════════════════════════════════════════════════════════════════════════════
#  Row 4 — Quick diagnostics
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("<br>", unsafe_allow_html=True)
with st.expander("🔬 Run Diagnostics"):
    _d1, _d2, _d3 = st.columns(3)
    with _d1:
        if st.button("▶ Test FFmpeg", use_container_width=True):
            _ok, _ver = _check_binary(FFMPEG_BIN)
            if _ok:
                st.success(f"✅ {_ver}")
            else:
                st.error(f"❌ {_ver}")
    with _d2:
        if st.button("▶ Test FFprobe", use_container_width=True):
            _ok, _ver = _check_binary(FFPROBE_BIN)
            if _ok:
                st.success(f"✅ {_ver}")
            else:
                st.error(f"❌ {_ver}")
    with _d3:
        if st.button("🗑 Clear temp files", use_container_width=True,
                     help="Removes all files in temp/ — does NOT delete output videos."):
            _cleared = 0
            for _f in TEMP_DIR.rglob("*"):
                if _f.is_file() and _f.parent != OUTPUT_DIR:
                    try:
                        _f.unlink()
                        _cleared += 1
                    except Exception:
                        pass
            st.success(f"Cleared {_cleared} temp file(s).")
            st.rerun()
