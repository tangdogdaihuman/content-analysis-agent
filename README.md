# 内容分析 Agent

丢链接即可自动生成视频/音频内容的要点总结 + 精美网页报告。开源、本地部署、数据不过第三方。

基于 [AI-Video-Transcriber](https://github.com/wendy7756/AI-Video-Transcriber) 深度改造。

## 功能

- **丢链接出要点**：B站、YouTube、抖音、TikTok 等 30+ 平台
- **要点总结**：核心观点 + 逐条要点 + 关键引用 + 适用人群
- **HTML 报告**：深色主题，离线可看，无外部追踪
- **字幕优先**：有字幕秒级出结果，无需下载视频
- **多语言**：中文/英文/日文/韩文等
- **本地文件**：支持 mp4/mp3/txt 等

## 快速开始

### 依赖

- FFmpeg（[下载](https://ffmpeg.org/download.html)）
- Python **不需要安装**——项目自带便携运行时

### 安装

```powershell
git clone https://github.com/tangdogdaihuman/content-analysis-agent.git
cd content-analysis-agent
setup_runtime.bat          # 首次运行：自动下载 Python + 安装依赖（~360MB，仅一次）
```

### 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```
OPENAI_API_KEY=你的Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-v4-pro
```

支持所有 OpenAI 兼容接口（DeepSeek / OpenAI / OpenRouter 等）。也可在 Web UI 的 AI 设置面板中配置。

### 获取 API Key

推荐 [DeepSeek](https://platform.deepseek.com)：注册 → API Keys → 创建 Key → 粘贴，按量计费每篇分析约 1-3 分钱。

### 启动

双击 `启动.bat`，或：

```powershell
.\runtime\python.exe start.py --prod   # 生产模式，长视频 SSE 不断连
```

浏览器打开 `http://localhost:8001`。

## 使用

1. 粘贴视频链接（支持带标题的分享文案，自动提取 URL）
2. 选择摘要语言
3. 点击「分析」
4. 下载 HTML 报告 / Markdown 要点 / 原文

## 与原始项目对比

| | 原始 AI-Video-Transcriber | 本仓库 |
|---|---|---|
| 摘要风格 | 简短执行摘要 | 要点总结（核心观点+逐条要点+引用） |
| HTML 输出 | 无 | 深色主题网页报告，离线可看 |
| UI | sipsip.ai 品牌 | 独立品牌，宋体排版 |
| 性能 | 分块串行调用 API | 并行 asyncio.gather，10 倍提速 |
| Whisper | beam_size=5 best_of=5 | 降参至 3+1，3-5 倍提速 |
| 模型配置 | 硬编码 gpt-4o | 环境变量 MODEL_NAME + Web UI |
| 临时文件 | 堆积不清理 | 完成后自动删除 + 过期任务清理 |
| 并发控制 | 无 | Semaphore 限流防 API 限频 |
| 安全 | 无校验 | SSRF 防护 + 日志去敏 + DOMPurify XSS |
| 启动 | 命令行 | 双击 bat 一键启动 |
| 部署 | 依赖系统 Python + venv | 自带便携 Python 运行时，搬文件夹即用 |

## 更新日志

### 2026.06 — v2.0 重大修复
- **事件循环阻塞**：LLM API 调用全部改为异步 (`asyncio.to_thread`)，高并发不再卡死
- **SSRF 防护**：yt-dlp 调用增加 URL 安全校验，拒绝内网/文件协议
- **XSS 防护**：前端引入 DOMPurify，Markdown 渲染输出全部净化
- **竞态条件**：任务删除/异常处理增加状态守卫
- **资源泄漏**：过期任务关联的输出文件自动清理
- **原子写入**：tasks.json 使用 temp+rename 模式防数据损坏
- **便携部署**：自带 Python 3.11 运行时，双击 `启动.bat` 即可运行

## License

Apache-2.0
