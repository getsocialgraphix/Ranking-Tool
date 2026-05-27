"""
Save / load / delete named presets as JSON files in ~/.ranking_tool/presets/.
Each preset is a self-contained dict; load_preset() falls back to DEFAULT_PRESET
if the file does not exist.
"""

import json
from pathlib import Path

from config import DEFAULT_PRESET, PRESETS_DIR


def list_presets() -> list[str]:
    """Return alphabetically sorted preset names (without .json extension)."""
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


def save_preset(name: str, config: dict) -> None:
    config = {**config, "name": name}
    (PRESETS_DIR / f"{name}.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


def load_preset(name: str) -> dict:
    path = PRESETS_DIR / f"{name}.json"
    if not path.exists():
        return DEFAULT_PRESET.copy()
    data = json.loads(path.read_text(encoding="utf-8"))
    # Merge with defaults so new keys added in future versions always exist
    merged = DEFAULT_PRESET.copy()
    merged.update(data)
    return merged


def delete_preset(name: str) -> bool:
    path = PRESETS_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
        return True
    return False
