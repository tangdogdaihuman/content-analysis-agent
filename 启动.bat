@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

:: Kill existing instance on port 8001 (avoid "address already in use")
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING" 2^>nul') do (
    taskkill /f /pid %%a >nul 2>&1
)

"%~dp0runtime\python.exe" start.py --prod
pause
