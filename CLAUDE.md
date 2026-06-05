# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 启动

**方式一：双击 `启动.bat`**（推荐，无需任何配置）

**方式二：命令行**
```powershell
.\runtime\python.exe start.py --prod   # 生产模式无热重载，SSE 不断连
```
浏览器打开 `http://localhost:8001`

> 项目自带 Python 运行时（`runtime/`），不依赖系统 Python。任意位置搬动文件夹即可运行，无需 venv。

## 技术栈

- **后端**: Python 3.8+, FastAPI, uvicorn
- **AI**: DeepSeek API (`deepseek-v4-pro`)，OpenAI 兼容协议 — 前端可逐请求覆盖 API Key/Base URL/Model
- **转录**: faster-whisper (base 模型, CPU int8)
- **媒体**: yt-dlp, FFmpeg (统一转 m4a 单声道 16kHz)
- **前端**: 原生 HTML/CSS/JS (dark theme), 单文件 `static/`，内置中英双语 i18n
- **部署**: Docker 可选

## 项目结构

```
backend/
  main.py             FastAPI 主应用 — 路由、任务编排、SSE 广播、HTML 报告生成
  summarizer.py       摘要生成 — LLM 调用、分块策略、转录优化
  video_processor.py  视频下载处理 — yt-dlp 字幕提取 + 音频下载
  transcriber.py      Whisper 转录 (faster-whisper)
  translator.py       条件翻译 — 仅当转录语言 ≠ 摘要语言时触发
  llm_sanitize.py     LLM 输出清理 — 去掉尾部客套话
static/
  index.html          前端页面
  app.js              前端逻辑 — SSE 订阅、智能进度模拟、i18n
runtime/              便携 Python 3.11 运行时（文件夹搬到哪都能跑）
temp/                 运行时临时文件，可随时清理
start.py              启动入口 — 自动检测 runtime/python.exe，回退到系统 Python
启动.bat              双击启动
```

## 关键配置 (.env)

- `OPENAI_API_KEY` — DeepSeek API Key（也可在前端 AI 设置面板中逐请求传入）
- `OPENAI_BASE_URL` — `https://api.deepseek.com/v1`
- `MODEL_NAME` — 模型名，默认 `deepseek-chat`
- `WHISPER_MODEL_SIZE` — base
- `HOST` / `PORT` — 服务器地址
- `UPLOAD_MAX_MB` — 上传限制，默认 200

## 核心架构

### 双路径处理

```
链接/文件 → 字幕优先探测 → 有字幕：秒级出结果（跳过下载+Whisper）
                          → 无字幕：下载音频 → Whisper 转录 → 管线
```

1. **字幕快速路径**（`video_processor.fetch_subtitles`）：yt-dlp 探测字幕，命中直接拿文本，进度 10%→40%
2. **Whisper 慢速路径**：下载最佳音频 → FFmpeg 转 m4a 单声道 16kHz → faster-whisper 转录
3. 两条路径汇入同一个 `_run_post_extract_pipeline()` — 归档 raw → LLM 优化（≤8000 字）→ 条件翻译 → 摘要 → HTML 报告

### 逐请求模型覆盖

前端可在 AI 设置面板传入 `api_key` / `model_base_url` / `model_id`，后端会创建专用的 Summarizer/Translator 实例，不污染全局配置。不传则回退到 `.env`。

### SSE 实时推送

- `/api/task-stream/{task_id}` — SSE 端点，asyncio.Queue 广播任务状态
- 前端 `app.js` 订阅 SSE，驱动进度条（内置智能进度模拟 `this.sp`，在等待服务端更新期间平滑动画）
- 30s 心跳保活；任务完成/失败后自动关闭流
- `--prod` 模式禁用 uvicorn reload，避免 SSE 断连

### 任务生命周期

创建 → `processing`（进度 0→100）→ `completed` 或 `error`
- `tasks` dict 持久化到 `temp/tasks.json`（每 3 次写入才实际落盘，减少 IO）
- `active_tasks` dict 持有 asyncio.Task 引用，支持取消
- `processing_urls` set 去重，同 URL 不重复处理
- 后台每 10 分钟清理 >1 小时的已完成/失败任务

### 上传管线

流式写入（1MB 分块 → 临时文件 → rename），避免大文件 OOM：
- `.txt`：直接读 UTF-8，包装为与 Whisper 输出一致的 Markdown 结构
- 音视频：FFmpeg 转 m4a 单声道 16kHz → Whisper 转录 → 同 `_run_post_extract_pipeline`

### 并发控制

- `API_SEMAPHORE = asyncio.Semaphore(8)` — LLM API 调用全局并发限制
- 分块摘要时各块并行请求，受信号量约束
- 翻译和摘要并行执行（`asyncio.gather`）

### SSRF 防护

`/api/models` 的 base_url 强制 HTTPS，拒绝 localhost、私有 IP、回环地址、链路本地地址。

## 注意事项

- `.env` 不进 git；`temp/` 不进 git
- 长视频建议 `--prod` 避免 SSE 断连
- Whisper 配置了 `condition_on_previous_text=False` + VAD 过滤，防止连环重复
- LLM 输出经 `strip_llm_artifacts` 清理尾部客套话（中英文均覆盖）
- Windows 项目路径含中文可能导致 pip 安装 .dll/.pyd 失败
