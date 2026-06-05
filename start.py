#!/usr/bin/env python3
"""
内容分析 Agent 启动脚本 (portable — uses bundled runtime if available)
"""

import os
import sys
import subprocess
from pathlib import Path

# Find the Python to use: bundled runtime > current interpreter
PROJECT_ROOT = Path(__file__).parent
RUNTIME_PYTHON = PROJECT_ROOT / "runtime" / "python.exe"
if RUNTIME_PYTHON.exists():
    PYTHON_EXE = str(RUNTIME_PYTHON.resolve())
else:
    PYTHON_EXE = sys.executable

# Force UTF-8 for subprocesses (uvicorn, etc.)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")


def _p(msg: str) -> None:
    """Print safely regardless of console encoding."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def check_dependencies():
    """检查依赖是否安装"""
    required_packages = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "yt-dlp": "yt_dlp",
        "faster-whisper": "faster_whisper",
        "openai": "openai"
    }

    missing_packages = []
    for display_name, import_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append(display_name)

    if missing_packages:
        _p("[X] Missing dependencies:")
        for package in missing_packages:
            _p(f"   - {package}")
        _p("\nRun: pip install -r requirements.txt")
        return False

    _p("[V] All dependencies installed")
    return True


def check_ffmpeg():
    """检查FFmpeg是否安装"""
    try:
        subprocess.run(["ffmpeg", "-version"],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL,
                      check=True)
        _p("[V] FFmpeg installed")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        _p("[X] FFmpeg not found")
        _p("Install FFmpeg:")
        _p("  macOS: brew install ffmpeg")
        _p("  Ubuntu: sudo apt install ffmpeg")
        _p("  Windows: https://ffmpeg.org/download.html")
        return False


def setup_environment():
    """设置环境变量"""
    from dotenv import load_dotenv
    load_dotenv()

    # 设置OpenAI配置
    if not os.getenv("OPENAI_API_KEY"):
        _p("[!] Warning: OPENAI_API_KEY not set")
        _p("Set it in .env or environment variables")
        return False

    _p("[V] OpenAI API Key set")

    if not os.getenv("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
        _p("[V] OpenAI Base URL set")

    # 设置其他默认配置
    if not os.getenv("WHISPER_MODEL_SIZE"):
        os.environ["WHISPER_MODEL_SIZE"] = "base"

    _p("[KEY] OpenAI API ready")
    return True


def main():
    """主函数"""
    # 检查是否使用生产模式
    production_mode = "--prod" in sys.argv or os.getenv("PRODUCTION_MODE") == "true"

    _p("Content Analysis Agent - Startup Check")
    if production_mode:
        _p("[PROD] Production mode - hot reload disabled")
    else:
        _p("[DEV] Development mode - hot reload enabled")
    _p("=" * 50)

    # 检查依赖
    if not check_dependencies():
        sys.exit(1)

    # 检查FFmpeg
    if not check_ffmpeg():
        _p("[!] FFmpeg missing - some video formats may not work")

    # 设置环境
    setup_environment()

    _p("\n[OK] Startup check complete")
    _p("=" * 50)

    # 启动服务器
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))

    _p(f"\n[SERVING] Starting server...")
    _p(f"   http://localhost:{port}")
    _p(f"   Press Ctrl+C to stop")
    _p("=" * 50)

    try:
        backend_dir = PROJECT_ROOT / "backend"
        os.chdir(backend_dir)

        cmd = [
            PYTHON_EXE, "-m", "uvicorn", "main:app",
            "--host", host,
            "--port", str(port)
        ]

        if not production_mode:
            cmd.append("--reload")

        subprocess.run(cmd)

    except KeyboardInterrupt:
        _p("\n\n[STOPPED] Server stopped")
    except Exception as e:
        _p(f"\n[X] Startup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
