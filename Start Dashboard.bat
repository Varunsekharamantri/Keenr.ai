@echo off
REM ---------------------------------------------------------------------------
REM  Market Signals dashboard - double-click to start.
REM
REM  Runs the server on its own, so the link keeps working even when Claude is
REM  closed. Leave this window open while you use the dashboard; close it (or
REM  press Ctrl+C) to stop the server.
REM ---------------------------------------------------------------------------
title Market Signals Dashboard
cd /d "%~dp0"

REM If a server is already answering on port 8000, just open the browser.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/api/insights/overview | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    echo Dashboard is already running - opening it.
    start "" http://localhost:8000
    timeout /t 3 >nul
    exit /b 0
)

echo Starting Market Signals dashboard...
echo It will open in your browser in a few seconds.
echo Keep this window open while you use it.
echo.

REM Open the browser once the server has had time to start.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 6; Start-Process 'http://localhost:8000'"

python backend\run.py

echo.
echo The dashboard server has stopped.
pause
