@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   内容分析 Agent - 便携环境安装
echo ============================================
echo.

if exist "runtime\python.exe" (
    echo [V] runtime already exists. Checking deps...
    runtime\python.exe -c "import fastapi" >nul 2>&1
    if !errorlevel! equ 0 (
        echo [V] All dependencies OK. Nothing to do.
        goto :done
    )
    echo [!] Runtime exists but deps missing. Reinstalling...
)

:: Download embedded Python
set PYTHON_ZIP=%TEMP%\python-embed.zip
set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip

if not exist "%PYTHON_ZIP%" (
    echo [*] Downloading Python 3.11.9 embedded (~11MB)...
    powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_ZIP%'" 2>nul
    if !errorlevel! neq 0 (
        echo [X] Download failed. Check network or try: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [V] Downloaded
) else (
    echo [V] Using cached Python download
)

:: Extract
echo [*] Extracting Python runtime...
if exist "runtime" rmdir /s /q "runtime"
powershell -Command "Expand-Archive -Path '%PYTHON_ZIP%' -DestinationPath 'runtime' -Force" 2>nul
echo [V] Extracted

:: Configure for site-packages
echo [*] Configuring Python...
(
echo python311.zip
echo .
echo # Uncomment to run site.main^(^) automatically
echo import site
echo Lib
) > runtime\python311._pth
mkdir runtime\Lib\site-packages >nul 2>&1

:: Install pip
echo [*] Installing pip...
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TEMP%\get-pip.py'" 2>nul
runtime\python.exe "%TEMP%\get-pip.py" --no-warn-script-location >nul 2>&1
echo [V] pip installed

:: Install project dependencies
echo [*] Installing project dependencies (this may take a minute)...
runtime\python.exe -m pip install -r requirements.txt --no-warn-script-location >nul 2>&1
if !errorlevel! neq 0 (
    echo [!] Some packages failed, retrying...
    runtime\python.exe -m pip install -r requirements.txt --no-warn-script-location
)
echo [V] Dependencies installed

:: Verify
runtime\python.exe -c "import fastapi, uvicorn, yt_dlp, faster_whisper, openai; print('[V] All imports OK')"

echo.
echo ============================================
echo   [OK] Portable environment ready!
echo   Double-click 启动.bat to start.
echo ============================================

:done
endlocal
pause
