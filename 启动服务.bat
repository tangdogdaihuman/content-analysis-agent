@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ========================================
echo   AI-Video-Transcriber 启动中...
echo   请访问 http://localhost:8000
echo   按 Ctrl+C 停止服务
echo ========================================
echo.
start http://localhost:8000
".\venv\Scripts\python.exe" start.py
pause
