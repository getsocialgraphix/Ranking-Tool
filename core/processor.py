"""
Convert any source clip to uniform 1080x1920 @ 30 fps via direct FFmpeg calls.

Aspect-ratio strategy
─────────────────────
  Already 9:16 (± 5 %)  →  scale + pad to 1080×1920
  Landscape / wide       →  blurred+darkened full-frame background,
                             centre-cropped foreground overlaid on top
  Everything else        →  same blurred background technique
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import (
    AUDIO_BITRATE, CRF, FFMPEG_BIN, FFPROBE_BIN, FFMPEG_PRESET, FPS,
    OUTPUT_HEIGHT, OUTPUT_WIDTH, TEMP_DIR,
)

PROCESSED_DIR = TEMP_DIR / "processed"


@dataclass
class ProcessResult:
    path: str = ""
    duration_s: float = 0.0
    error: Optional[str] = None


# ── FFprobe helpers ───────────────────────────────────────────────────────────

def _probe_video_stream(path: str) -> dict:
    cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "v:0",
        path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {}
    streams = json.loads(r.stdout).get("streams", [])
    return streams[0] if streams else {}


def _probe_duration(path: str) -> float:
    cmd = [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return 0.0
    return float(json.loads(r.stdout).get("format", {}).get("duration", 0.0))


def _has_audio(path: str) -> bool:
    cmd = [
        FFPROBE_BIN, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "a:0",
        path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False
    return bool(json.loads(r.stdout).get("streams"))


# ── Public API ────────────────────────────────────────────────────────────────

def trim_clip(input_path: str, start_s: float, end_s: float, output_path: str) -> Optional[str]:
    """
    Trim [start_s, end_s] from input using stream copy — no re-encode, near-instant.
    Returns output_path or None on error.
    """
    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", str(start_s),
        "-i", input_path,
        "-t", str(max(0.1, end_s - start_s)),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return output_path if r.returncode == 0 else None


def process_clip(input_path: str, index: int) -> ProcessResult:
    """
    Produce temp/processed/proc_NN.mp4 at 1080×1920 / 30 fps.
    Always outputs an audio track (silent if the source has none).
    """
    output_path = str(PROCESSED_DIR / f"proc_{index:02d}.mp4")

    stream = _probe_video_stream(input_path)
    if not stream:
        return ProcessResult(error=f"Cannot probe video stream: {input_path}")

    w = int(stream.get("width", 0))
    h = int(stream.get("height", 0))
    if w == 0 or h == 0:
        return ProcessResult(error=f"Zero dimensions detected: {input_path}")

    has_audio = _has_audio(input_path)
    aspect    = w / h

    # Audio tail for every branch: always emit an AAC stream.
    # When source has no audio we pull a silent track from lavfi.
    if has_audio:
        audio_input_args  = []
        audio_map         = ["-map", "0:a:0"]
        audio_encode_args = ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    else:
        audio_input_args  = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        audio_map         = ["-map", "1:a"]
        audio_encode_args = ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-shortest"]

    if abs(aspect - (9 / 16)) < 0.08:
        # ── Already vertical ──────────────────────────────────────────────────
        vf = (
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
            f":force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1,fps={FPS}"
        )
        cmd = (
            [FFMPEG_BIN, "-y", "-i", input_path]
            + audio_input_args
            + ["-vf", vf, "-map", "0:v"]
            + audio_map
            + ["-c:v", "libx264", "-crf", str(CRF), "-preset", FFMPEG_PRESET]
            + audio_encode_args
            + [output_path]
        )

    else:
        # ── Landscape / non-standard → blurred background composite ──────────
        # bg:  scale to fill 1080×1920, heavy blur, darken
        # fg:  scale to fit within 1080 width, preserve aspect
        filter_complex = (
            f"[0:v]"
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
            f"gblur=sigma=28,"
            f"colorchannelmixer=rr=0.45:gg=0.45:bb=0.45,"
            f"setsar=1[bg];"

            f"[0:v]"
            f"scale={OUTPUT_WIDTH}:-2:force_original_aspect_ratio=decrease,"
            f"setsar=1[fg];"

            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"fps={FPS}[out]"
        )
        if has_audio:
            cmd = (
                [FFMPEG_BIN, "-y", "-i", input_path]
                + audio_input_args
                + ["-filter_complex", filter_complex,
                   "-map", "[out]", "-map", "0:a:0",
                   "-c:v", "libx264", "-crf", str(CRF), "-preset", FFMPEG_PRESET]
                + audio_encode_args
                + [output_path]
            )
        else:
            cmd = (
                [FFMPEG_BIN, "-y", "-i", input_path]
                + audio_input_args
                + ["-filter_complex", filter_complex,
                   "-map", "[out]", "-map", "1:a",
                   "-c:v", "libx264", "-crf", str(CRF), "-preset", FFMPEG_PRESET]
                + audio_encode_args
                + [output_path]
            )

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = r.stderr[-600:] if r.stderr else "(no stderr)"
        return ProcessResult(error=f"FFmpeg failed:\n{tail}")

    return ProcessResult(path=output_path, duration_s=_probe_duration(output_path))
