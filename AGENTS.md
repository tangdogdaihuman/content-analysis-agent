# AGENTS.md — 内容分析agent

## 启动

```powershell
venv\Scripts\activate
python start.py --prod   # 生产模式，无热重载，SSE 不断连
```

- 开发模式 `python start.py` 有 `--reload`，长视频 SSE 可能断连，优先用 `--prod`。
- 浏览器打开 `http://localhost:8000`。
- **Windows 环境**，路径分隔符是 `\`。

## 架构

```
backend/     FastAPI 后端，唯一入口 `main.py:app`
  main.py            FastAPI app，所有路由/SSE端点
  summarizer.py      DeepSeek LLM 摘要（核心，51KB）
  video_processor.py yt-dlp + FFmpeg 下载/转码
  transcriber.py     faster-whisper 转录 (CPU, int8)
  translator.py      翻译
  llm_sanitize.py    LLM 输出清理
static/      单文件前端（原生 HTML/CSS/JS，无框架）
  index.html
  app.js
temp/        运行时临时目录（gitignored）
```

## 关键事实

- **API 是 DeepSeek，不是 OpenAI**。`OPENAI_BASE_URL` 默认是 DeepSeek 端点，但代码走的是 OpenAI 兼容协议。改 `OPENAI_BASE_URL` 即可切模型。
- **faster-whisper 跑在 CPU 上**（`device="cpu"`, `compute_type="int8"`），模型大小默认 `base`。
- **无测试、无 lint 配置、无 CI**。不要尝试跑测试或 lint。
- `.env` 存 API Key，已 gitignored。需要时读 `backend/main.py` 里的环境变量读取逻辑来推断配置项。
- `requirements.txt` 中 `uvicorn[standard]` 方括号在 PowerShell 里会报错，安装时注意。
- Docker 是可选的，`docker-compose.yml` 里服务名是 `ai-video-transcriber`。
- 音频/视频文件（`.mp3`, `.mp4` 等）和模型缓存（`models/`, `.cache/`）均 gitignored。
- **venv 不在项目目录内**。`venv/` 是一个目录 junction 指向 `C:\Users\admin\Desktop\.agent-venv`（Python 3.12）。原因是 Windows 上含中文的路径会导致 pip 安装时 `.dll`/`.pyd` 文件写入失败（编码乱码报 PermissionError）。实际 venv 二进制和 site-packages 在 `.agent-venv` 里。
- **不要手动设置 stdout 编码**。`start.py` 已通过 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1` 环境变量处理中文输出。之前的 `TextIOWrapper` hack 会在 GBK 控制台上造成乱码。
