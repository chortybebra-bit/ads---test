@echo off
title AdsPower Warmup Manager
echo ============================================
echo   AdsPower Warmup Manager - Launcher
echo ============================================
echo.

echo [1/2] Checking dependencies...
python -m pip install -r requirements.txt --quiet >nul 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    echo Make sure Python and pip are installed.
    pause
    exit /b 1
)
echo       Dependencies OK.
echo.

echo [2/2] Starting application...
echo.
python app.py

if errorlevel 1 (
    echo.
    echo ERROR: Application exited with an error.
    pause
)
