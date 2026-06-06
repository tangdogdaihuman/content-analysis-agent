@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

:: Kill existing instance on port 8001
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING" 2^>nul') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: Check for yt-dlp update (5s timeout, skip if network unavailable)
echo [*] Checking for yt-dlp updates...
runtime\python.exe -m pip install --pre --upgrade yt-dlp --no-warn-script-location --quiet --timeout 5 --retries 1 2>nul
if errorlevel 1 echo [!] Update check skipped (network unavailable)

:: Start server
echo [*] Starting server...
start "" "%~dp0runtime\python.exe" start.py --prod

:: Wait for server to be ready, then open browser
echo [*] Waiting for server...
:waitloop
timeout /t 1 /nobreak >nul
curl -s -o NUL http://localhost:8001/ 2>nul
if errorlevel 1 goto waitloop

start http://localhost:8001
timeout /t 2 /nobreak >nul
exit
