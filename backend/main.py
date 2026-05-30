from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import os
import asyncio
import logging
from pathlib import Path
from typing import Optional
import aiofiles
import uuid
import json
import re
import threading
import openai
from datetime import datetime
import markdown

from video_processor import VideoProcessor
from transcriber import Transcriber
from summarizer import Summarizer
from translator import Translator

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_old_tasks())
    yield
    cleanup_task.cancel()

app = FastAPI(title="内容分析 Agent", version="1.0.0", lifespan=lifespan)

# CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 获取项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

# 创建临时目录
TEMP_DIR = PROJECT_ROOT / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# 初始化处理器
video_processor = VideoProcessor()
transcriber = Transcriber()
summarizer = Summarizer()
translator = Translator()

# 存储任务状态 - 使用文件持久化
TASKS_FILE = TEMP_DIR / "tasks.json"
tasks_lock = threading.Lock()

def load_tasks():
    """加载任务状态"""
    try:
        if TASKS_FILE.exists():
            with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_tasks(tasks_data, force: bool = False):
    """保存任务状态。非强制模式下每3次调用才真正写入磁盘。"""
    if not force:
        save_tasks._counter = getattr(save_tasks, '_counter', 0) + 1
        if save_tasks._counter % 3 != 0:
            return
    try:
        with tasks_lock:
            with open(TASKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(tasks_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存任务状态失败: {e}")

async def broadcast_task_update(task_id: str, task_data: dict):
    """向所有连接的SSE客户端广播任务状态更新"""
    logger.info(f"广播任务更新: {task_id}, 状态: {task_data.get('status')}, 连接数: {len(sse_connections.get(task_id, []))}")
    if task_id in sse_connections:
        connections_to_remove = []
        for queue in sse_connections[task_id]:
            try:
                await queue.put(json.dumps(task_data, ensure_ascii=False))
                logger.debug(f"消息已发送到队列: {task_id}")
            except Exception as e:
                logger.warning(f"发送消息到队列失败: {e}")
                connections_to_remove.append(queue)
        
        # 移除断开的连接
        for queue in connections_to_remove:
            sse_connections[task_id].remove(queue)
        
        # 如果没有连接了，清理该任务的连接列表
        if not sse_connections[task_id]:
            del sse_connections[task_id]

# 启动时加载任务状态
tasks = load_tasks()
# 存储正在处理的URL，防止重复处理
processing_urls = set()
# 存储活跃的任务对象，用于控制和取消
active_tasks = {}
# 存储SSE连接，用于实时推送状态更新
sse_connections = {}

# 定期清理过期任务（>1小时）
async def _cleanup_old_tasks():
    while True:
        await asyncio.sleep(600)  # 每10分钟
        now = datetime.now()
        expired = []
        for tid, t in list(tasks.items()):
            if t.get("status") in ("completed", "error"):
                ts = t.get("completed_at") or t.get("created_at", "")
                if ts:
                    try:
                        age = (now - datetime.fromisoformat(ts)).total_seconds()
                        if age > 3600:
                            expired.append(tid)
                    except Exception:
                        pass
        for tid in expired:
            del tasks[tid]
        if expired:
            save_tasks(tasks, force=True)
            logger.info(f"清理 {len(expired)} 个过期任务")

# 本地上传：允许的类型与大小上限（MB），可用环境变量 UPLOAD_MAX_MB 调整
UPLOAD_ALLOWED_EXT = frozenset({".txt", ".mp3", ".mp4", ".m4a", ".wav", ".webm", ".mkv", ".ogg", ".flac"})
UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "200"))


def _sanitize_title_for_filename(title: str) -> str:
    """将视频标题清洗为安全的文件名片段。"""
    if not title:
        return "untitled"
    # 仅保留字母数字、下划线、连字符与空格
    safe = re.sub(r"[^\w\-\s]", "", title)
    # 压缩空白并转为下划线
    safe = re.sub(r"\s+", "_", safe).strip("._-")
    # 最长限制，避免过长文件名问题
    return safe[:80] or "untitled"


def _generate_analysis_html(title: str, markdown_content: str, language: str) -> str:
    """将 Markdown 分析转换为精美的 HTML 页面。"""
    md_html = markdown.markdown(
        markdown_content,
        extensions=["extra", "codehilite", "toc"]
    )
    
    lang_name = {"en": "English", "zh": "中文", "ja": "日本語", "ko": "한국어"}.get(language, language)
    today = datetime.now().strftime("%Y.%m.%d")
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title} / 内容分析</title>
<style>
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}

  :root {{
    --bg: #0d0d0d;
    --surface: #161616;
    --surface-hi: #1e1e1e;
    --ink: #e6e1d8;
    --ink-soft: #b0aba2;
    --ink-dim: #77736c;
    --accent: #d4784e;
    --accent-ghost: #d4784e1a;
    --accent-bright: #f09860;
    --border: #2a2723;
    --shadow: 0 1px 3px rgba(0,0,0,0.4);
    --font-body: 'Noto Serif SC', 'STSong', 'Songti SC', 'SimSun', 'NSimSun', 'Source Han Serif SC', serif;
    --font-mono: 'Cascadia Code', 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
  }}

  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 16px;
    line-height: 2;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }}

  /* ── Texture overlay ── */
  body::before {{
    content: '';
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 9999;
    opacity: 0.03;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.95' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }}

  .page {{
    max-width: 780px;
    margin: 0 auto;
    padding: 80px 40px 60px;
  }}

  /* ── Masthead ── */
  .masthead {{
    position: relative;
    margin-bottom: 60px;
    padding-bottom: 40px;
    border-bottom: 1px solid var(--border);
  }}

  .masthead-tag {{
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 24px;
  }}

  .masthead h1 {{
    font-size: 38px;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.35;
    color: var(--ink);
  }}

  .masthead-meta {{
    display: flex;
    align-items: center;
    gap: 18px;
    margin-top: 28px;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--ink-dim);
    letter-spacing: 0.05em;
  }}

  .masthead-meta .sep {{
    width: 4px; height: 4px;
    border-radius: 50%;
    background: var(--accent);
    opacity: 0.6;
  }}

  /* ── Stamp ── */
  .stamp {{
    position: absolute;
    top: 0; right: 0;
    width: 56px; height: 56px;
    border: 2px solid var(--accent);
    border-radius: 50%;
    opacity: 0.3;
    display: flex;
    align-items: center;
    justify-content: center;
    transform: rotate(8deg);
  }}
  .stamp::after {{
    content: '析';
    font-family: var(--font-body);
    font-size: 20px;
    font-weight: 700;
    color: var(--accent);
  }}

  /* ── Article body ── */
  .article {{ }}

  .article h2 {{
    font-size: 24px;
    font-weight: 700;
    margin: 48px 0 16px;
    padding-left: 16px;
    border-left: 3px solid var(--accent);
    letter-spacing: -0.01em;
    color: var(--ink);
  }}
  .article h2:first-of-type {{ margin-top: 0; }}

  .article h3 {{
    font-size: 19px;
    font-weight: 600;
    margin: 36px 0 10px;
    color: var(--ink-soft);
    letter-spacing: -0.005em;
  }}

  .article h4 {{
    font-size: 16px;
    font-weight: 600;
    margin: 24px 0 8px;
    color: var(--ink-dim);
  }}

  .article p {{
    margin-bottom: 16px;
    text-align: justify;
    hyphens: auto;
    text-indent: 2em;
  }}
  .article p:first-of-type {{ text-indent: 0; }}

  .article ul, .article ol {{
    padding-left: 28px;
    margin: 12px 0 20px;
  }}
  .article li {{
    margin-bottom: 8px;
    padding-left: 4px;
  }}
  .article li::marker {{ color: var(--accent); }}

  .article strong {{ color: var(--ink); font-weight: 700; }}
  .article em {{ font-style: italic; color: var(--ink-soft); }}

  .article blockquote {{
    margin: 28px 0;
    padding: 20px 28px;
    background: var(--surface-hi);
    border-left: 2px solid var(--accent);
    font-style: italic;
    color: var(--ink-soft);
    border-radius: 0 4px 4px 0;
    position: relative;
  }}
  .article blockquote::before {{
    content: '"';
    position: absolute;
    top: -8px; left: 12px;
    font-size: 48px;
    color: var(--accent);
    opacity: 0.1;
    font-family: var(--font-body);
    line-height: 1;
  }}

  .article code {{
    font-family: var(--font-mono);
    font-size: 0.88em;
    background: var(--surface);
    padding: 2px 6px;
    border-radius: 3px;
    color: var(--accent);
  }}

  .article pre {{
    background: var(--surface);
    padding: 20px 24px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 18px 0;
    font-size: 14px;
    border: 1px solid var(--border);
  }}
  .article pre code {{
    background: none;
    padding: 0;
    color: var(--ink-soft);
  }}

  .article table {{
    width: 100%;
    border-collapse: collapse;
    margin: 20px 0;
    font-size: 15px;
  }}
  .article th, .article td {{
    border: 1px solid var(--border);
    padding: 12px 16px;
    text-align: left;
  }}
  .article th {{
    background: var(--surface);
    font-weight: 600;
    font-size: 14px;
    letter-spacing: 0.02em;
    color: var(--accent);
  }}

  .article hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 40px 0;
  }}

  .article a {{
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px solid var(--accent-ghost);
    transition: border-color 0.2s;
  }}
  .article a:hover {{ border-color: var(--accent-bright); }}

  /* ── Colophon ── */
  .colophon {{
    margin-top: 72px;
    padding-top: 32px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--ink-dim);
    letter-spacing: 0.05em;
  }}

  .colophon-dot {{
    width: 8px; height: 8px;
    background: var(--accent);
    opacity: 0.25;
    border-radius: 50%;
  }}

  /* ── Mobile ── */
  @media (max-width: 680px) {{
    .page {{ padding: 40px 20px 40px; }}
    .masthead h1 {{ font-size: 26px; }}
    .masthead {{ margin-bottom: 40px; padding-bottom: 28px; }}
    .stamp {{ width: 40px; height: 40px; top: -4px; }}
    .stamp::after {{ font-size: 16px; }}
    .article h2 {{ font-size: 20px; margin-top: 36px; }}
    .article h3 {{ font-size: 17px; }}
    .article p {{ text-align: left; text-indent: 1.5em; }}
    .article blockquote {{ padding: 16px 20px; }}
  }}
</style>
</head>
<body>
<div class="page">

  <header class="masthead">
    <div class="stamp"></div>
    <div class="masthead-tag">Content Analysis</div>
    <h1>{safe_title}</h1>
    <div class="masthead-meta">
      <span>{lang_name}</span>
      <span class="sep"></span>
      <span>{today}</span>
    </div>
  </header>

  <article class="article">
    {md_html}
  </article>

  <footer class="colophon">
    <span>内容分析 Agent</span>
    <span class="colophon-dot"></span>
    <span>{today}</span>
  </footer>

</div>
</body>
</html>"""


def _txt_to_raw_transcript_markdown(body: str) -> str:
    """将纯文本包装为与 Whisper 输出结构一致的 Markdown。"""
    text = body.strip() if body.strip() else "(empty)"
    return "\n".join([
        "# Video Transcription",
        "",
        "**Detected Language:**",
        "**Language Probability:** —",
        "",
        "## Transcription Content",
        "",
        text,
    ])


async def _run_post_extract_pipeline(
    task_id: str,
    raw_script: str,
    video_title: str,
    source_ref: str,
    summary_language: str,
    request_summarizer: Summarizer,
    dedup_url: Optional[str] = None,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
) -> None:
    """取得 raw_script 后的共用管线：归档、优化、翻译、摘要、广播。"""
    short_id = task_id.replace("-", "")[:6]
    safe_title = _sanitize_title_for_filename(video_title)

    raw_md_path = None
    try:
        raw_md_filename = f"raw_{safe_title}_{short_id}.md"
        raw_md_path = TEMP_DIR / raw_md_filename
        with open(raw_md_path, "w", encoding="utf-8") as f:
            f.write((raw_script or "") + f"\n\nsource: {source_ref}\n")
        tasks[task_id].update({"raw_script_file": raw_md_filename})
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
    except Exception as e:
        logger.error(f"保存原始转录Markdown失败: {e}")

    tasks[task_id].update({
        "progress": 55,
        "message": "正在优化转录文本...",
    })
    save_tasks(tasks)
    await broadcast_task_update(task_id, tasks[task_id])

    if len(raw_script) > 8000:
        logger.info(f"文本过长({len(raw_script)} chars)，跳过LLM优化，直接使用原始文本")
        script = raw_script
        tasks[task_id].update({
            "progress": 60,
            "message": "文本较长，已跳过优化，直接生成要点总结…",
        })
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
    else:
        script = await request_summarizer.optimize_transcript(raw_script)

    script_with_title = f"# {video_title}\n\n{script}\n\nsource: {source_ref}\n"

    detected_language = transcriber.get_detected_language(raw_script)
    detected_language = (detected_language or "").strip()
    if not detected_language:
        detected_language = translator.infer_language_code(raw_script)
    detected_language = translator.normalize_lang_code(detected_language) or detected_language

    logger.info(f"检测到的语言: {detected_language}, 摘要语言: {summary_language}")

    translation_content = None
    translation_filename = None
    translation_path = None

    eff_key = (api_key or "").strip()
    eff_base = (model_base_url or "").strip().rstrip("/")
    if eff_key:
        request_translator = Translator(
            api_key=eff_key,
            base_url=eff_base or None,
            model=model_id or None,
        )
    else:
        request_translator = translator

    need_translation = translator.languages_differ_for_translation(
        detected_language, summary_language
    )

    if need_translation:
        logger.info(f"需要翻译: {detected_language} -> {summary_language}")
        tasks[task_id].update({
            "progress": 75,
            "message": "正在并行生成翻译和要点总结...",
        })
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])

        translation_task = asyncio.create_task(
            request_translator.translate_text(script, summary_language, detected_language)
        )
        summary_task = asyncio.create_task(
            request_summarizer.summarize(script, summary_language, video_title)
        )
        results = await asyncio.gather(translation_task, summary_task)
        translation_content, summary = results[0], results[1]

        translation_with_title = f"# {video_title}\n\n{translation_content}\n\nsource: {source_ref}\n"
        translation_filename = f"translation_{safe_title}_{short_id}.md"
        translation_path = TEMP_DIR / translation_filename
        async with aiofiles.open(translation_path, "w", encoding="utf-8") as f:
            await f.write(translation_with_title)
    else:
        logger.info(
            f"不需要翻译: detected_language={detected_language}, summary_language={summary_language}, "
            f"need_translation={need_translation}"
        )
        tasks[task_id].update({
            "progress": 80,
            "message": "正在生成要点总结...",
        })
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])

        summary = await request_summarizer.summarize(script, summary_language, video_title)

    summary_with_source = summary + f"\n\nsource: {source_ref}\n"

    script_filename = f"transcript_{task_id}.md"
    script_path = TEMP_DIR / script_filename
    async with aiofiles.open(script_path, "w", encoding="utf-8") as f:
        await f.write(script_with_title)

    new_script_filename = f"transcript_{safe_title}_{short_id}.md"
    new_script_path = TEMP_DIR / new_script_filename
    try:
        if script_path.exists():
            script_path.rename(new_script_path)
            script_path = new_script_path
    except Exception:
        pass

    summary_filename = f"summary_{safe_title}_{short_id}.md"
    summary_path = TEMP_DIR / summary_filename
    async with aiofiles.open(summary_path, "w", encoding="utf-8") as f:
        await f.write(summary_with_source)

    html_content = _generate_analysis_html(video_title, summary, summary_language)
    html_filename = f"analysis_{task_id}.html"
    html_path = TEMP_DIR / html_filename
    async with aiofiles.open(html_path, "w", encoding="utf-8") as f:
        await f.write(html_content)

    task_result = {
        "status": "completed",
        "progress": 100,
        "completed_at": datetime.now().isoformat(),
        "message": "处理完成！",
        "video_title": video_title,
        "script": script_with_title,
        "summary": summary_with_source,
        "script_path": str(script_path),
        "summary_path": str(summary_path),
        "html_path": str(html_path),
        "short_id": short_id,
        "safe_title": safe_title,
        "detected_language": detected_language,
        "summary_language": summary_language,
    }

    if translation_content and translation_path:
        task_result.update({
            "translation": translation_with_title,
            "translation_path": str(translation_path),
            "translation_filename": translation_filename,
        })

    tasks[task_id].update(task_result)
    save_tasks(tasks, force=True)
    await broadcast_task_update(task_id, tasks[task_id])
    logger.info(f"最终状态已广播: {task_id}")

    if dedup_url:
        processing_urls.discard(dedup_url)
    if task_id in active_tasks:
        del active_tasks[task_id]

    # 清理临时文件：删除raw转录和音频，只保留最终输出
    try:
        if raw_md_path and raw_md_path.exists():
            raw_md_path.unlink()
            logger.info(f"已删除原始转录: {raw_md_path}")
    except Exception as e:
        logger.warning(f"删除原始转录失败: {e}")


@app.get("/")
async def read_root():
    """返回前端页面"""
    return FileResponse(str(PROJECT_ROOT / "static" / "index.html"))

@app.post("/api/models")
async def list_models(
    base_url: str = Form(default=""),
    api_key:  str = Form(default=""),
):
    """Proxy: fetch model list from any OpenAI-compatible API."""
    effective_key = api_key or os.getenv("OPENAI_API_KEY", "")
    effective_url = base_url.rstrip("/") or os.getenv("OPENAI_BASE_URL") or None

    if not effective_key:
        raise HTTPException(status_code=400, detail="API key is required")

    # SSRF防护：仅允许 https，拒绝内网/本地地址
    from urllib.parse import urlparse
    import ipaddress
    parsed = urlparse(effective_url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Only HTTPS URLs are allowed")
    hostname = (parsed.hostname or "").lower()
    # 先检查常见字符串（localhost / IPv6 loopback）
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise HTTPException(status_code=400, detail="Internal addresses are not allowed")
    # 再用 ipaddress 精确匹配整个私有/回环/链路本地地址段
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(status_code=400, detail="Internal addresses are not allowed")
    except ValueError:
        pass  # 不是 IP，是域名，放行

    try:
        client = openai.OpenAI(api_key=effective_key, base_url=effective_url)
        resp   = await asyncio.to_thread(client.models.list)
        models = [{"id": m.id, "name": getattr(m, "name", m.id)} for m in resp.data]
        # Sort by id for readability
        models.sort(key=lambda x: x["id"])
        return {"data": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _enqueue_upload_job(
    file: UploadFile,
    summary_language: str,
    api_key: str,
    model_base_url: str,
    model_id: str,
) -> dict:
    """保存上传文件并入队 process_upload_task，返回 {task_id, message}。"""
    raw_name = file.filename or "upload.bin"
    if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe_name = os.path.basename(raw_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'}",
        )

    max_bytes = UPLOAD_MAX_MB * 1024 * 1024
    task_id = str(uuid.uuid4())
    unique_stem = task_id.replace("-", "")[:12]
    dest = TEMP_DIR / f"upload_{unique_stem}{ext}"

    total = 0
    # 先写临时文件，避免把整个文件缓冲在内存（上限 200MB 时可能 OOM）
    tmp_dest = TEMP_DIR / f"_tmp_{unique_stem}{ext}"
    try:
        with open(tmp_dest, "wb") as out_f:
            while True:
                # 每次读 1MB，但最多读到 max_bytes（不再 +1，靠循环后判断）
                chunk = await file.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                out_f.write(chunk)
                if total > max_bytes:
                    break  # 已超限，停止写入但继续读完（避免连接残留）

        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds limit of {UPLOAD_MAX_MB} MB",
            )
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty file")

        # 大小合法，rename 到位
        tmp_dest.rename(dest)
    except HTTPException:
        # 清理临时文件
        try:
            tmp_dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    except Exception:
        try:
            tmp_dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    video_title = _sanitize_title_for_filename(Path(safe_name).stem) or "upload"
    source_label = f"upload:{safe_name}"

    tasks[task_id] = {
        "status": "processing",
        "progress": 0,
        "created_at": datetime.now().isoformat(),
        "message": "开始处理上传文件...",
        "script": None,
        "summary": None,
        "error": None,
        "url": source_label,
    }
    save_tasks(tasks)

    bg = asyncio.create_task(
        process_upload_task(
            task_id,
            dest,
            safe_name,
            video_title,
            ext,
            summary_language,
            api_key,
            model_base_url,
            model_id,
        )
    )
    active_tasks[task_id] = bg

    return {"task_id": task_id, "message": "任务已创建，正在处理中..."}


@app.post("/api/process-video")
async def process_video(
    url: str = Form(default=""),
    summary_language: str = Form(default="zh"),
    api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_id: str = Form(default=""),
    file: Optional[UploadFile] = File(None),
):
    """
    处理视频链接或本地上传（multipart 中带 file 且无有效 URL 时走上传流程）。
    上传与 URL 共用此路径，便于反向代理只放行 /api/process-video 的环境。
    """
    try:
        if file is not None and (file.filename or "").strip():
            return await _enqueue_upload_job(
                file, summary_language, api_key, model_base_url, model_id
            )

        stripped = (url or "").strip()
        if not stripped:
            raise HTTPException(
                status_code=400,
                detail="Provide a video URL or upload a file",
            )

        # 从粘贴文本中提取纯 URL（支持 b23.tv 等短链+标题文案）
        url_match = re.search(r"(https?://[^\s]+)", stripped)
        if url_match:
            url = url_match.group(1)
        else:
            url = stripped

        # 检查是否已经在处理相同的URL
        if url in processing_urls:
            # 查找现有任务
            for tid, task in tasks.items():
                if task.get("url") == url:
                    return {"task_id": tid, "message": "该视频正在处理中，请等待..."}
            
        # 生成唯一任务ID
        task_id = str(uuid.uuid4())
        
        # 标记URL为正在处理
        processing_urls.add(url)
        
        # 初始化任务状态
        tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "message": "开始处理视频...",
            "script": None,
            "summary": None,
            "error": None,
            "url": url  # 保存URL用于去重
        }
        save_tasks(tasks)
        
        # 创建并跟踪异步任务
        task = asyncio.create_task(process_video_task(task_id, url, summary_language, api_key, model_base_url, model_id))
        active_tasks[task_id] = task
        
        return {"task_id": task_id, "message": "任务已创建，正在处理中..."}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理视频时出错: {str(e)}")
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

async def process_video_task(
    task_id: str,
    url: str,
    summary_language: str,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
):
    """
    异步处理视频任务
    """
    try:
        audio_path = None  # 初始化，字幕路径下无音频文件

        # ── 阶段一：优先尝试获取平台字幕（快速路径） ──────────────────────
        tasks[task_id].update({
            "status": "processing",
            "progress": 10,
            "message": "正在检测视频字幕..."
        })
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
        await asyncio.sleep(0.1)

        # 如果前端传入了 API 凭据，创建专用 Summarizer（线程安全，覆盖全局实例）
        if api_key:
            effective_url = model_base_url.rstrip("/") or None
            request_summarizer = Summarizer(
                api_key=api_key,
                base_url=effective_url,
                model=model_id or None,
            )
            logger.info(f"使用前端API Key, model={model_id or 'default'}")
        else:
            request_summarizer = summarizer  # 全局实例（使用环境变量）

        subtitle_text, sub_title, sub_lang = await video_processor.fetch_subtitles(url, TEMP_DIR)
        if subtitle_text:
            # ── 快速路径：有字幕，跳过音频下载和 Whisper ──────────────────
            video_title = sub_title
            raw_script = subtitle_text
            # 把语言写入 transcriber，保持下游逻辑一致
            transcriber.last_detected_language = sub_lang

            tasks[task_id].update({
                "progress": 40,
                "message": f"字幕获取成功（{sub_lang}），正在处理文本..."
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])
        else:
            # ── 慢速路径：无字幕，下载音频 → Whisper 转录 ─────────────────
            tasks[task_id].update({
                "progress": 15,
                "message": "未找到字幕，正在下载视频音频..."
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            audio_path, video_title = await video_processor.download_and_convert(
                url, TEMP_DIR, prefetched_title=sub_title or None
            )

            tasks[task_id].update({
                "progress": 35,
                "message": "音频下载完成，准备转录..."
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            tasks[task_id].update({
                "progress": 40,
                "message": "正在转录音频（Whisper）..."
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            raw_script = await transcriber.transcribe(audio_path)

        await _run_post_extract_pipeline(
            task_id=task_id,
            raw_script=raw_script,
            video_title=video_title,
            source_ref=url,
            summary_language=summary_language,
            request_summarizer=request_summarizer,
            dedup_url=url,
            api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
        )

        # 清理音频临时文件
        if audio_path:
            try:
                Path(audio_path).unlink(missing_ok=True)
                logger.info(f"已删除音频文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除音频文件失败: {e}")

    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}")
        processing_urls.discard(url)
        if task_id in active_tasks:
            del active_tasks[task_id]
        # 清理可能遗留的音频文件
        if audio_path:
            try:
                Path(audio_path).unlink(missing_ok=True)
            except Exception:
                pass
        tasks[task_id].update({
            "status": "error",
            "error": str(e),
            "message": f"处理失败: {str(e)}"
        })
        save_tasks(tasks, force=True)
        await broadcast_task_update(task_id, tasks[task_id])

@app.post("/api/process-upload")
async def process_upload(
    file: UploadFile = File(...),
    summary_language: str = Form(default="zh"),
    api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_id: str = Form(default=""),
):
    """独立上传入口；逻辑与 multipart 带 file 的 /api/process-video 相同。"""
    return await _enqueue_upload_job(
        file, summary_language, api_key, model_base_url, model_id
    )


async def process_upload_task(
    task_id: str,
    saved_path: Path,
    original_name: str,
    video_title: str,
    ext_lower: str,
    summary_language: str,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
):
    source_ref = f"upload:{original_name}"
    audio_path = None  # 初始化
    try:
        if api_key:
            effective_url = model_base_url.rstrip("/") or None
            request_summarizer = Summarizer(
                api_key=api_key,
                base_url=effective_url,
                model=model_id or None,
            )
            logger.info(
                f"上传任务使用前端API Key, model={model_id or 'default'}"
            )
        else:
            request_summarizer = summarizer

        if ext_lower == ".txt":
            tasks[task_id].update({
                "progress": 20,
                "message": "正在读取文本文件...",
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            body = saved_path.read_text(encoding="utf-8", errors="replace")
            if not body.strip():
                raise Exception("文本文件为空")
            transcriber.last_detected_language = None
            raw_script = _txt_to_raw_transcript_markdown(body)
        else:
            tasks[task_id].update({
                "progress": 15,
                "message": "正在转换音频格式...",
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            audio_path = await video_processor.normalize_local_media_to_m4a(saved_path, TEMP_DIR)

            tasks[task_id].update({
                "progress": 35,
                "message": "音频准备完成，准备转录...",
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            tasks[task_id].update({
                "progress": 40,
                "message": "正在转录音频（Whisper）...",
            })
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            raw_script = await transcriber.transcribe(audio_path)

        await _run_post_extract_pipeline(
            task_id=task_id,
            raw_script=raw_script,
            video_title=video_title,
            source_ref=source_ref,
            summary_language=summary_language,
            request_summarizer=request_summarizer,
            dedup_url=None,
            api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
        )

        # 清理音频临时文件
        if audio_path:
            try:
                Path(audio_path).unlink(missing_ok=True)
                logger.info(f"已删除音频文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除音频文件失败: {e}")

    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}")
        if task_id in active_tasks:
            del active_tasks[task_id]
        if audio_path:
            try:
                Path(audio_path).unlink(missing_ok=True)
            except Exception:
                pass
        tasks[task_id].update({
            "status": "error",
            "error": str(e),
            "message": f"处理失败: {str(e)}",
        })
        save_tasks(tasks, force=True)
        await broadcast_task_update(task_id, tasks[task_id])


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    """
    获取任务状态
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return tasks[task_id]

@app.get("/api/task-stream/{task_id}")
async def task_stream(task_id: str):
    """
    SSE实时任务状态流
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    async def event_generator():
        # 创建任务专用的队列
        queue = asyncio.Queue()
        
        # 将队列添加到连接列表
        if task_id not in sse_connections:
            sse_connections[task_id] = []
        sse_connections[task_id].append(queue)
        
        try:
            # 立即发送当前状态
            current_task = tasks.get(task_id, {})
            yield f"data: {json.dumps(current_task, ensure_ascii=False)}\n\n"
            
            # 持续监听状态更新
            while True:
                try:
                    # 等待状态更新，超时时间30秒发送心跳
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                    
                    # 如果任务完成或失败，结束流
                    task_data = json.loads(data)
                    if task_data.get("status") in ["completed", "error"]:
                        break
                        
                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                    
        except asyncio.CancelledError:
            logger.info(f"SSE连接被取消: {task_id}")
        except Exception as e:
            logger.error(f"SSE流异常: {e}")
        finally:
            # 清理连接
            if task_id in sse_connections and queue in sse_connections[task_id]:
                sse_connections[task_id].remove(queue)
                if not sse_connections[task_id]:
                    del sse_connections[task_id]
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Cache-Control"
        }
    )

@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """直接从temp目录下载文件"""
    try:
        if not (filename.endswith('.md') or filename.endswith('.html')):
            raise HTTPException(status_code=400, detail="仅支持下载.md和.html文件")
        
        if '..' in filename or '/' in filename or '\\' in filename:
            raise HTTPException(status_code=400, detail="文件名格式无效")
            
        file_path = TEMP_DIR / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        
        media_type = "text/html" if filename.endswith('.html') else "text/markdown"
        return FileResponse(file_path, filename=filename, media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"下载文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"下载失败: {str(e)}")


@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str):
    """
    取消并删除任务
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 如果任务还在运行，先取消它
    if task_id in active_tasks:
        task = active_tasks[task_id]
        if not task.done():
            task.cancel()
            logger.info(f"任务 {task_id} 已被取消")
        del active_tasks[task_id]
    
    # 从处理URL列表中移除
    task_url = tasks[task_id].get("url")
    if task_url:
        processing_urls.discard(task_url)
    
    # 删除任务记录
    del tasks[task_id]
    return {"message": "任务已取消并删除"}

@app.get("/api/tasks/active")
async def get_active_tasks():
    """
    获取当前活跃任务列表（用于调试）
    """
    active_count = len(active_tasks)
    processing_count = len(processing_urls)
    return {
        "active_tasks": active_count,
        "processing_urls": processing_count,
        "task_ids": list(active_tasks.keys())
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
