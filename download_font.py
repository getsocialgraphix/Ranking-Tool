"""
One-time helper: downloads Montserrat-Black.ttf from Google Fonts into assets/fonts/.
Run before first use:  python download_font.py
"""

import sys
import urllib.request
from pathlib import Path

FONT_URL = (
    "https://github.com/google/fonts/raw/main/ofl/montserrat/"
    "Montserrat%5Bwght%5D.ttf"
)

# Static-weight fallback (direct TTF, no variable-font axis needed by Pillow)
FONT_URL_STATIC = (
    "https://github.com/google/fonts/raw/main/ofl/montserrat/static/"
    "Montserrat-Black.ttf"
)

DEST = Path(__file__).parent / "assets" / "fonts" / "Montserrat-Black.ttf"


def download() -> None:
    if DEST.exists():
        print(f"Font already present at {DEST}")
        return

    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Montserrat-Black.ttf …")

    for url in (FONT_URL_STATIC, FONT_URL):
        try:
            urllib.request.urlretrieve(url, DEST)
            print(f"Saved to {DEST}")
            return
        except Exception as exc:
            print(f"  Attempt failed ({exc}), trying next URL…")

    print(
        "\nAutomatic download failed.\n"
        "Please manually download Montserrat-Black.ttf from "
        "https://fonts.google.com/specimen/Montserrat and place it at:\n"
        f"  {DEST}"
    )
    sys.exit(1)


if __name__ == "__main__":
    download()
