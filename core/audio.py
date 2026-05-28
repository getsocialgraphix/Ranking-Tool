"""
Audio pipeline:
  1. Mix ElevenLabs voiceovers and SFX into the assembled video (pydub overlay).
  2. Add background music with sidechain ducking via a SINGLE FFmpeg call.

Ducking is implemented with FFmpeg's native sidechaincompress filter — replacing
the old pydub Python-loop approach which was 10–50× slower on long videos.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from pydub import AudioSegment

from config import (
    AUDIO_BITRATE, FFMPEG_BIN, FFPROBE_BIN, MUSIC_DUCK_LEVEL, MUSIC_FULL_LEVEL,
    TEMP_DIR,
)

AUDIO_DIR = TEMP_DIR / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _probe_duration(path: str) -> float:
    cmd = [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return 0.0
    return float(json.loads(r.stdout).get("format", {}).get("duration", 0.0))


def _extract_audio(video_path: str) -> Optional[str]:
    """Export audio from video as 44.1 kHz stereo WAV. Returns path or None."""
    out = str(AUDIO_DIR / "clip_audio.wav")
    cmd = [
        FFMPEG_BIN, "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out if (r.returncode == 0 and Path(out).exists()) else None


def _replace_audio(video_path: str, audio_wav: str, output_path: str) -> Optional[str]:
    """Copy video stream verbatim, replace audio with audio_wav."""
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", video_path, "-i", audio_wav,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest",
        output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return output_path if r.returncode == 0 else None


# ── Public API ────────────────────────────────────────────────────────────────

def add_audio_overlays(
    video_path:  str,
    output_path: str,
    voiceovers:  Optional[list[dict]] = None,
    sfx_list:    Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Mix ElevenLabs voiceovers and/or sound-effects into the video's audio track.

    voiceovers  — list of {"path": str, "start_ms": int}
    sfx_list    — list of {"path": str, "start_ms": int, "volume_db": float}

    Returns output_path on success, original video_path if nothing to mix,
    or None on FFmpeg error.
    """
    if not voiceovers and not sfx_list:
        return video_path

    duration_s = _probe_duration(video_path)
    target_ms  = max(1000, int(duration_s * 1000))

    clip_wav = _extract_audio(video_path)
    if clip_wav:
        base = AudioSegment.from_file(clip_wav)
    else:
        base = AudioSegment.silent(duration=target_ms)

    if len(base) < target_ms:
        base = base + AudioSegment.silent(duration=target_ms - len(base))

    # Mix voiceovers (at -3 dB so they sit under the original audio)
    for vo in (voiceovers or []):
        try:
            seg  = AudioSegment.from_file(vo["path"])
            seg  = seg.apply_gain(-3)
            base = base.overlay(seg, position=int(vo.get("start_ms", 0)))
        except Exception:
            pass

    # Mix SFX
    for sfx in (sfx_list or []):
        try:
            seg = AudioSegment.from_file(sfx["path"])
            if sfx.get("volume_db", 0) != 0:
                seg = seg.apply_gain(sfx["volume_db"])
            base = base.overlay(seg, position=int(sfx.get("start_ms", 0)))
        except Exception:
            pass

    mixed_wav = str(AUDIO_DIR / "overlays_mixed.wav")
    base.export(mixed_wav, format="wav")
    return _replace_audio(video_path, mixed_wav, output_path)


def mix_audio_for_video(
    video_path:  str,
    music_path:  Optional[str],
    output_path: str,
    duck_level:  float = MUSIC_DUCK_LEVEL,
    full_level:  float = MUSIC_FULL_LEVEL,
) -> Optional[str]:
    """
    Mix background music into the assembled video using FFmpeg's native
    sidechaincompress filter for sidechain ducking.

    Replaces the old pydub-based pipeline (which used Python loops over 10 ms
    audio chunks) with a single FFmpeg subprocess — 10–50× faster on long videos.

    Strategy
    ────────
    • No music  → loudnorm pass only (video stream copied, no re-encode).
    • With music → loop music via -stream_loop, apply sidechaincompress ducking,
                   mix with speech, copy video stream.

    Falls back to simple amix (no ducking) if sidechaincompress is unavailable
    in the local FFmpeg build.

    Returns output_path on success, None on failure.
    """
    if not music_path:
        # No music — just normalise loudness; copy video stream verbatim
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_path,
            "-af", "loudnorm=I=-14:LRA=11:TP=-1",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return output_path if r.returncode == 0 else None

    duration_s = _probe_duration(video_path)
    if duration_s <= 0:
        duration_s = 1.0
    fade_start = max(0.0, duration_s - 2.0)

    # ── Attempt 1: sidechaincompress ducking (FFmpeg native, very fast) ──────
    # Filter graph:
    #   [speech]      — gentle loudnorm pass (replaces dynaudnorm which could
    #                   over-amplify voiceover silence between clips)
    #   [music_prep]  — volume-limited, trimmed, fade-in/out
    #   sidechaincompress ducks [music_prep] when [speech] is loud
    #   amix combines both streams
    filter_sc = (
        "[0:a]loudnorm=I=-16:LRA=11:TP=-1.5[speech];"
        f"[1:a]volume={full_level:.6f},"
        f"atrim=0:{duration_s + 3.0:.3f},"
        "asetpts=PTS-STARTPTS,"
        "afade=t=in:st=0:d=1,"
        f"afade=t=out:st={fade_start:.3f}:d=2[music_prep];"
        "[music_prep][speech]"
        "sidechaincompress="
        "threshold=0.015:ratio=6:attack=5:release=150:"
        "level_sc=0.9[music_ducked];"
        "[speech][music_ducked]amix=inputs=2:duration=first:normalize=0[out]"
    )
    cmd_sc = [
        FFMPEG_BIN, "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", filter_sc,
        "-map", "0:v",
        "-map", "[out]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest",
        output_path,
    ]
    r = subprocess.run(cmd_sc, capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        return output_path

    # ── Attempt 2: simple amix without ducking (older FFmpeg builds) ─────────
    filter_simple = (
        "[0:a]loudnorm=I=-16:LRA=11:TP=-1.5[speech];"
        f"[1:a]volume={duck_level:.6f},"
        f"atrim=0:{duration_s + 3.0:.3f},"
        "asetpts=PTS-STARTPTS,"
        "afade=t=in:st=0:d=1,"
        f"afade=t=out:st={fade_start:.3f}:d=2[music_simple];"
        "[speech][music_simple]amix=inputs=2:duration=first:normalize=0[out]"
    )
    cmd_simple = [
        FFMPEG_BIN, "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", filter_simple,
        "-map", "0:v",
        "-map", "[out]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest",
        output_path,
    ]
    r2 = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=300)
    return output_path if r2.returncode == 0 else None
