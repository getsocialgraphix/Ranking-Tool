"""
ElevenLabs text-to-speech integration for per-clip voiceovers.

Usage:
  from core.elevenlabs_tts import list_voices, generate_voiceover

  voices = list_voices(api_key)          # [{id, name, category}]
  path   = generate_voiceover(api_key, voice_id, "Hello!", "/tmp/out.mp3")
"""

import gc
import requests
from pathlib import Path
from typing import Optional

EL_BASE = "https://api.elevenlabs.io/v1"


def _hdr(api_key: str) -> dict:
    return {"xi-api-key": api_key.strip()}


# ── Voice catalogue ───────────────────────────────────────────────────────────

def list_voices(api_key: str) -> list[dict]:
    """
    Fetch all voices available on the account.
    Returns [{id, name, category}] sorted by name.
    Returns [] on any error (no exception raised).
    """
    if not api_key or not api_key.strip():
        return []
    try:
        r = requests.get(
            f"{EL_BASE}/voices",
            headers=_hdr(api_key),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        r.close()
        del r
        voices = [
            {
                "id":       v["voice_id"],
                "name":     v["name"],
                "category": v.get("category", ""),
                # deliberately omit "labels" — can be a large nested dict
                # that bloats session state and wastes RAM on Cloud
            }
            for v in data.get("voices", [])
        ]
        del data
        gc.collect()
        return sorted(voices, key=lambda v: v["name"].lower())
    except Exception:
        return []


def list_models(api_key: str) -> list[dict]:
    """Return [{id, name}] for available TTS models."""
    if not api_key or not api_key.strip():
        return []
    try:
        r = requests.get(f"{EL_BASE}/models", headers=_hdr(api_key), timeout=10)
        r.raise_for_status()
        return [
            {"id": m["model_id"], "name": m.get("name", m["model_id"])}
            for m in r.json()
            if m.get("can_do_text_to_speech")
        ]
    except Exception:
        return []


# ── Speech generation ─────────────────────────────────────────────────────────

def generate_voiceover(
    api_key:          str,
    voice_id:         str,
    text:             str,
    output_path:      str,
    stability:        float = 0.50,
    similarity_boost: float = 0.75,
    style:            float = 0.00,
    model_id:         str   = "eleven_multilingual_v2",
) -> Optional[str]:
    """
    Synthesise `text` with ElevenLabs and write the result as an MP3.

    Parameters
    ----------
    api_key          : ElevenLabs API key
    voice_id         : voice ID (from list_voices)
    text             : the words to speak (emoji are supported where the model handles them)
    output_path      : destination file path (will be created / overwritten)
    stability        : 0–1, higher = more consistent/robotic
    similarity_boost : 0–1, higher = more similar to original voice
    style            : 0–1, voice style exaggeration (multilingual v2+)
    model_id         : ElevenLabs model to use

    Returns output_path on success, None on failure.
    """
    if not api_key or not text.strip():
        return None

    payload = {
        "text":     text.strip(),
        "model_id": model_id,
        "voice_settings": {
            "stability":        max(0.0, min(1.0, stability)),
            "similarity_boost": max(0.0, min(1.0, similarity_boost)),
            "style":            max(0.0, min(1.0, style)),
            "use_speaker_boost": True,
        },
    }
    try:
        r = requests.post(
            f"{EL_BASE}/text-to-speech/{voice_id}",
            headers={**_hdr(api_key), "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json=payload,
            timeout=90,
            stream=True,                    # stream → never load full audio into RAM
        )
        r.raise_for_status()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(output_path, "wb") as _f:  # write in 32 KB chunks
                for _chunk in r.iter_content(chunk_size=32_768):
                    if _chunk:
                        _f.write(_chunk)
        finally:
            r.close()                       # always release the TCP connection
        return output_path
    except Exception:
        return None


def validate_key(api_key: str) -> bool:
    """Quick check — returns True if the key is valid (can reach /user endpoint)."""
    if not api_key or not api_key.strip():
        return False
    try:
        r = requests.get(f"{EL_BASE}/user", headers=_hdr(api_key), timeout=5)
        return r.status_code == 200
    except Exception:
        return False
