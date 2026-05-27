@echo off
echo ============================================================
echo  Ranking Tool — Setup
echo ============================================================
echo.

REM ── Python packages ──────────────────────────────────────────
echo [1/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed. Make sure Python 3.10+ is on PATH.
    pause & exit /b 1
)

REM ── Font ─────────────────────────────────────────────────────
echo.
echo [2/3] Downloading Montserrat-Black font...
python download_font.py

REM ── FFmpeg check ─────────────────────────────────────────────
echo.
echo [3/3] Checking FFmpeg...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: ffmpeg not found on PATH.
    echo Download from https://ffmpeg.org/download.html
    echo Extract and add the bin\ folder to your system PATH, then re-run this script.
    echo.
    pause & exit /b 1
) else (
    echo FFmpeg found. OK.
)

echo.
echo ============================================================
echo  Setup complete!  Launch the app with:
echo    streamlit run app.py
echo ============================================================
pause
