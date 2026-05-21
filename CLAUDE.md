# 内容分析agent

AI 视频/播客转录与摘要工具。支持 30+ 平台链接 + 本地上传，Faster-Whisper 转录 + DeepSeek LLM 摘要。

## 启动

```bash
cd C:\Users\admin\Desktop\内容分析agent
venv\Scripts\activate
python start.py --prod   # 生产模式无热重载，SSE 不断连
```
浏览器打开 `http://localhost:8000`

## 技术栈

- **后端**: Python 3.8+, FastAPI, uvicorn
- **AI**: DeepSeek API (`deepseek-v4-pro`)，OpenAI 兼容协议
- **转录**: faster-whisper (base 模型)
- **媒体**: yt-dlp, FFmpeg
- **前端**: 原生 HTML/CSS/JS (dark theme), 单文件在 `static/`
- **部署**: Docker 可选

## 项目结构

```
backend/
  main.py             FastAPI 主应用 (41KB)
  summarizer.py       摘要生成 (51KB)
  video_processor.py  视频下载处理 (19KB)
  transcriber.py      Whisper 转录 (6KB)
  translator.py       翻译 (12KB)
  llm_sanitize.py     LLM 输出清理
static/
  index.html          前端页面 (22KB)
  app.js              前端逻辑 (36KB)
temp/                 运行时临时文件
start.py              启动入口
requirements.txt      依赖列表
.env                  API Key 等敏感配置
```

## 关键配置 (.env)

- `OPENAI_API_KEY` — DeepSeek API Key
- `OPENAI_BASE_URL` — `https://api.deepseek.com/v1`
- `WHISPER_MODEL_SIZE` — base
- `HOST` / `PORT` — 服务器地址

## 工作流

链接/文件 → 字幕优先（YouTube等）→ 无字幕则下载 → Whisper 转录 → LLM 优化/翻译/摘要 → 输出 Markdown

## 注意事项

- `.env` 存有 API Key，不进 git
- `temp/` 是运行时目录，可随时清理
- 长视频建议 `python start.py --prod` 避免 SSE 断连
- 本地上传单文件上限 200MB，可通过 `UPLOAD_MAX_MB` 调整
