"""
Audio pipeline:
  1. Mix ElevenLabs voiceovers and SFX into the assembled video via a single
     FFmpeg filter_complex call (adelay + sidechaincompress).
  2. Add background music with sidechain ducking via a SINGLE FFmpeg call.

Both steps use pure FFmpeg — no pydub — to avoid:
  • Sub-sample rounding errors from pydub's ms-to-sample conversion
  • Abrupt hard gain cuts at voiceover start/end boundaries (the "chop")
  • Unnecessary 44100 Hz → 48000 Hz resampling artefacts
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

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


# ── Public API ────────────────────────────────────────────────────────────────

def add_audio_overlays(
    video_path:  str,
    output_path: str,
    voiceovers:  Optional[list[dict]] = None,
    sfx_list:    Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Mix ElevenLabs voiceovers and/or sound-effects into the video's audio track
    using a single FFmpeg filter_complex — no pydub.

    voiceovers  — list of {"path": str, "start_ms": int}
    sfx_list    — list of {"path": str, "start_ms": int, "volume_db": float}

    Filter graph
    ────────────
    1. aformat → normalise original video audio to 48 kHz stereo
    2. aresample + aformat → normalise each overlay to 48 kHz stereo
       (handles ElevenLabs 44.1 kHz MP3 natively, without pydub rounding)
    3. adelay → position each overlay at its exact start_ms timestamp
    4. sidechaincompress → voiceovers drive smooth ducking of clip audio
       (attack=5 ms / release=150 ms — no hard gain cuts, no click artefacts)
    5. amix → ducked clip audio + voiceovers + SFX → final track

    Falls back to plain amix (no ducking) if sidechaincompress is unavailable
    in the local FFmpeg build.

    Returns output_path on success, original video_path if nothing to overlay,
    or None on FFmpeg error.
    """
    valid_vos: list[dict] = [
        v for v in (voiceovers or [])
        if v.get("path") and Path(v["path"]).exists()
    ]
    valid_sfx: list[dict] = [
        s for s in (sfx_list or [])
        if s.get("path") and Path(s["path"]).exists()
    ]
    if not valid_vos and not valid_sfx:
        return video_path

    n_vo  = len(valid_vos)
    n_sfx = len(valid_sfx)

    # ── Inputs ────────────────────────────────────────────────────────────────
    # Input [0]           = original video (with audio)
    # Inputs [1]..[n_vo]  = voiceover MP3/WAV files
    # Inputs [n_vo+1]..   = SFX files
    base_cmd: list[str] = [FFMPEG_BIN, "-y", "-i", video_path]
    for vo in valid_vos:
        base_cmd += ["-i", vo["path"]]
    for sfx in valid_sfx:
        base_cmd += ["-i", sfx["path"]]

    # ── Filter builder ────────────────────────────────────────────────────────
    def _build_filter(with_sidechain: bool) -> str:
        parts: list[str] = []

        # Normalise original video audio → 48 kHz stereo
        parts.append(
            "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[vid_a]"
        )

        # ── Voiceover inputs: resample → optional delay ───────────────────────
        vo_tags: list[str] = []
        for i, vo in enumerate(valid_vos):
            tag      = f"vo{i}"
            delay_ms = int(vo.get("start_ms", 0))
            # aresample converts EL 44100 Hz to 48000 Hz (FFmpeg SWR, clean)
            chain    = (
                f"[{i + 1}:a]"
                "aresample=48000,"
                "aformat=sample_rates=48000:channel_layouts=stereo"
            )
            if delay_ms > 0:
                # adelay inserts silence before audio — positions the narrator
                # at the exact millisecond its clip starts in the assembled video
                chain += f",adelay={delay_ms}|{delay_ms}"
            chain += f"[{tag}]"
            parts.append(chain)
            vo_tags.append(tag)

        # ── SFX inputs: resample → optional gain → optional delay ─────────────
        sfx_tags: list[str] = []
        for j, sfx in enumerate(valid_sfx):
            tag      = f"sfx{j}"
            in_idx   = n_vo + j + 1
            delay_ms = int(sfx.get("start_ms", 0))
            vol_db   = float(sfx.get("volume_db", 0.0))
            chain    = (
                f"[{in_idx}:a]"
                "aresample=48000,"
                "aformat=sample_rates=48000:channel_layouts=stereo"
            )
            if vol_db != 0.0:
                chain += f",volume={vol_db:.2f}dB"
            if delay_ms > 0:
                chain += f",adelay={delay_ms}|{delay_ms}"
            chain += f"[{tag}]"
            parts.append(chain)
            sfx_tags.append(tag)

        # ── Sidechain ducking (voiceovers only, attempt 1) ────────────────────
        if vo_tags and with_sidechain:
            # Combine all voiceover streams → one sidechain key
            if len(vo_tags) == 1:
                # Single voiceover: split for (a) sidechain key (b) output mix
                parts.append(f"[{vo_tags[0]}]asplit=2[vo_key][vo_mix]")
            else:
                # Multiple voiceovers: merge then split
                mixed = "".join(f"[{t}]" for t in vo_tags)
                parts.append(
                    f"{mixed}amix=inputs={len(vo_tags)}:duration=longest:normalize=0[vo_all]"
                )
                parts.append("[vo_all]asplit=2[vo_key][vo_mix]")

            # sidechaincompress: clip audio ducked by 6:1 ratio while narrator speaks
            # attack=5 ms  → quick response, no syllable clipping
            # release=150 ms → smooth fade-back, no pumping artefacts
            # threshold=0.015 ≈ −36 dBFS → triggers on any audible speech
            parts.append(
                "[vid_a][vo_key]"
                "sidechaincompress="
                "threshold=0.015:ratio=6:attack=5:release=150:level_sc=0.9"
                "[vid_ducked]"
            )
            audio_base   = "[vid_ducked]"
            overlay_tags = ["[vo_mix]"] + [f"[{t}]" for t in sfx_tags]

        else:
            # No voiceovers, or sidechaincompress unavailable → plain mix
            audio_base   = "[vid_a]"
            overlay_tags = [f"[{t}]" for t in vo_tags] + [f"[{t}]" for t in sfx_tags]

        # ── Final mix ──────────────────────────────────────────────────────────
        # duration=first → output length matches the video audio (first input)
        n_mix   = 1 + len(overlay_tags)
        all_mix = audio_base + "".join(overlay_tags)
        parts.append(
            f"{all_mix}amix=inputs={n_mix}:duration=first:normalize=0[out]"
        )
        return ";".join(parts)

    # ── Runner ────────────────────────────────────────────────────────────────
    def _run(filter_complex: str) -> bool:
        cmd = base_cmd + [
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[out]",
            "-c:v", "copy",                  # video stream untouched
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",       # moov atom first → smooth browser playback
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return r.returncode == 0

    # Attempt 1: sidechain ducking (needs sidechaincompress in FFmpeg build)
    if valid_vos and _run(_build_filter(with_sidechain=True)):
        return output_path

    # Attempt 2: plain amix — SFX-only case or older FFmpeg without sidechain
    if _run(_build_filter(with_sidechain=False)):
        return output_path

    return None


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
    • No music  → dynaudnorm pass only (video stream copied, no re-encode).
    • With music → loop music via -stream_loop, apply sidechaincompress ducking,
                   mix with speech, copy video stream.

    Falls back to simple amix (no ducking) if sidechaincompress is unavailable
    in the local FFmpeg build.

    Returns output_path on success, None on failure.
    """
    if not music_path:
        # No music — normalise loudness with slow dynaudnorm; copy video stream
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_path,
            "-af", "dynaudnorm=f=500:g=15:r=0.9:p=0.95,"
                   "acompressor=threshold=0.1:ratio=2:attack=20:release=300",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
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
    #   [speech]     — dynaudnorm with a very long window (f=500 ms)
    #                  smooths level differences between voiceover and clip audio
    #                  without fast pumping/glitching. acompressor catches peaks.
    #   [music_prep] — volume-limited, trimmed, fade-in/out
    #   sidechaincompress ducks [music_prep] when [speech] is loud
    #   amix combines both streams
    filter_sc = (
        "[0:a]dynaudnorm=f=500:g=15:r=0.9:p=0.95,"
        "acompressor=threshold=0.1:ratio=2:attack=20:release=300[speech];"
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
        "-movflags", "+faststart",
        output_path,
    ]
    r = subprocess.run(cmd_sc, capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        return output_path

    # ── Attempt 2: simple amix without ducking (older FFmpeg builds) ─────────
    filter_simple = (
        "[0:a]dynaudnorm=f=500:g=15:r=0.9:p=0.95,"
        "acompressor=threshold=0.1:ratio=2:attack=20:release=300[speech];"
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
        "-movflags", "+faststart",
        output_path,
    ]
    r2 = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=300)
    return output_path if r2.returncode == 0 else None
