# 内容分析 Agent

丢链接即可自动生成视频/音频内容的要点总结+网页报告。告别手动看视频。

基于 [AI-Video-Transcriber](https://github.com/wendy7756/AI-Video-Transcriber) 深度改造。

## 功能

- **丢链接出摘要**：支持 B站、YouTube、抖音、TikTok 等 30+ 平台
- **要点总结**：核心观点 + 逐条要点 + 关键引用 + 适用人群
- **网页报告**：深色主题精美 HTML，可直接分享
- **字幕优先**：有字幕的平台秒级出结果，无需下载视频
- **多语言**：中文/英文/日文/韩文等摘要
- **本地文件**：支持上传 mp4/mp3/txt 等

## 快速开始

### 依赖

- Python 3.8+
- FFmpeg

### 安装

```powershell
git clone https://github.com/tangdogdaihuman/content-analysis-agent.git
cd content-analysis-agent
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env`，填入你的 API Key：

```
OPENAI_API_KEY=你的API_Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-v4-pro
```

支持所有 OpenAI 兼容接口（DeepSeek / OpenAI / OpenRouter 等）。

### 启动

```powershell
$env:PYTHONIOENCODING='utf-8'
python start.py
```

或双击 `启动服务.bat`。浏览器打开 `http://localhost:8000`。

## 使用

1. 粘贴视频链接
2. 选择摘要语言
3. 点击 Transcribe
4. 下载 HTML / Markdown / 原文

## 与原始项目的区别

| | 原始 AI-Video-Transcriber | 本仓库 |
|---|---|---|
| 摘要风格 | 简短执行摘要 | 要点总结（核心观点+逐条要点+引用） |
| HTML 输出 | 无 | 深色主题网页报告 |
| 模型配置 | 硬编码 gpt-4o | 环境变量 MODEL_NAME |
| 临时文件 | 堆积不清理 | 完成后自动删除 |
| 前端 | 原版 | 新增 HTML 下载按钮 |
| 启动 | 命令行 | 双击 bat 一键启动 |

## License

Apache-2.0
