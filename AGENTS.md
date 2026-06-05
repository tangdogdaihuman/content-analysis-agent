# AGENTS.md — 内容分析agent

## 启动

**方式一：双击 `启动.bat`**（推荐）

**方式二：命令行**
```powershell
.\runtime\python.exe start.py --prod   # 生产模式，无热重载，SSE 不断连
```

- 浏览器打开 `http://localhost:8001`。
- 首次使用需先运行 `setup_runtime.bat` 下载 Python 运行时 + 安装依赖。
- **Windows 环境**，路径分隔符是 `\`。

## 架构

```
backend/     FastAPI 后端，唯一入口 `main.py:app`
  main.py            FastAPI app，所有路由/SSE端点
  summarizer.py      DeepSeek LLM 摘要（核心，~51KB）
  video_processor.py yt-dlp + FFmpeg 下载/转码
  transcriber.py     faster-whisper 转录 (CPU, int8)
  translator.py      翻译
  llm_sanitize.py    LLM 输出清理
static/      单文件前端（原生 HTML/CSS/JS，无框架）
  index.html
  app.js
runtime/     便携 Python 3.11（setup_runtime.bat 生成，gitignored）
temp/        运行时临时目录（gitignored）
setup_runtime.bat   首次安装脚本
启动.bat            日常启动
```

## 关键事实

- **API 是 DeepSeek，不是 OpenAI**。`OPENAI_BASE_URL` 默认是 DeepSeek 端点，但代码走的是 OpenAI 兼容协议。
- **faster-whisper 跑在 CPU 上**（`device="cpu"`, `compute_type="int8"`），模型大小默认 `base`。
- **无测试、无 lint 配置、无 CI**。
- `.env` 存 API Key，已 gitignored。`runtime/` 和 `temp/` 也 gitignored。
- 项目自带 Python 运行时（`runtime/`），不依赖系统 Python。任意搬动文件夹均可运行。
- **LLM 调用全部异步**：`summarizer.py` 中所有 OpenAI API 调用通过 `asyncio.to_thread` + `API_SEMAPHORE` 防阻塞。
- **SSRF 防护**：`/api/models` 和视频 URL 均校验，拒绝内网/非 HTTPS/私有 IP。
- **XSS 防护**：前端 `marked.parse` 输出经 DOMPurify 净化。
- Docker 可选：`docker-compose.yml` 服务名 `ai-video-transcriber`。
- 音频/视频文件和模型缓存均 gitignored。
- `start.py` 可处理 GBK 控制台（已去除 emoji，改用 ASCII 标记）。
