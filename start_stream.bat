@echo off
title HLS Proxy pro VLC
cd /d "%~dp0"

echo Ukoncuji pripadne predchozi instance proxy na portu 8080...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8080 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

python proxy.py
pause
