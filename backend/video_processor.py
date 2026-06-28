import os
import re
import shutil
import uuid
import asyncio
import subprocess
import yt_dlp
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class VideoProcessor:
    """视频处理器，使用yt-dlp下载和转换视频"""
    
    def __init__(self):
        # BiliBili 等站点需要的通用请求头，避免 412 Precondition Failed
        # 关键：BiliBili API 现已强制校验 Origin 头，缺失则返回 412
        _http_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://www.bilibili.com',
            'Origin': 'https://www.bilibili.com',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        self.ydl_opts = {
            'format': 'bestaudio/best',  # 优先下载最佳音频源
            'outtmpl': '%(title)s.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                # 直接在提取阶段转换为单声道 16k（空间小且稳定）
                'preferredcodec': 'm4a',
                'preferredquality': '192'
            }],
            # 全局FFmpeg参数：单声道 + 16k 采样率 + faststart
            'postprocessor_args': ['-ac', '1', '-ar', '16000', '-movflags', '+faststart'],
            'prefer_ffmpeg': True,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,  # 强制只下载单个视频，不下载播放列表
            'concurrent_fragment_downloads': 4,  # 并发下载分片
            'http_headers': _http_headers,
        }
        # 可选 cookies 来源（二选一，文件优先）：
        #   COOKIES_FILE=/path/to/cookies.txt        手动导出的 cookies 文件
        #   COOKIES_FROM_BROWSER=chrome|edge|firefox  自动从浏览器读取（免操作）
        self._cookies_source = None  # 实际生效的 cookies 配置
        _cookies = os.getenv("COOKIES_FILE", "").strip()
        if _cookies:
            # 相对路径以项目根目录为基准（因为 uvicorn 的 CWD 可能在 backend/）
            _cookie_path = Path(_cookies)
            if not _cookie_path.is_absolute():
                _cookie_path = (Path(__file__).parent.parent / _cookie_path).resolve()
            if _cookie_path.is_file():
                self.ydl_opts['cookiefile'] = str(_cookie_path)
                self._cookies_source = 'file'
                logger.info(f"使用 cookies 文件: {_cookie_path}")
        else:
            _browser = os.getenv("COOKIES_FROM_BROWSER", "").strip().lower()
            if _browser in ('chrome', 'edge', 'firefox', 'brave', 'opera', 'chromium'):
                self.ydl_opts['cookiesfrombrowser'] = (_browser,)
                self._cookies_source = 'browser'
                logger.info(f"从浏览器读取 cookies: {_browser}（浏览器需关闭才能读取）")

    async def normalize_local_media_to_m4a(self, input_path: Path, output_dir: Path) -> str:
        """
        将本地上传的音视频转为单声道 16kHz AAC m4a，供 Faster-Whisper 使用（与 yt-dlp 后处理参数对齐）。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        unique_id = str(uuid.uuid4())[:8]
        out_path = output_dir / f"upload_norm_{unique_id}.m4a"

        cmd = [
            "ffmpeg", "-y", "-nostdin", "-i", str(input_path.resolve()),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(out_path.resolve()),
        ]

        def _run():
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                raise Exception(f"FFmpeg 转换失败: {err[:800]}")
            if not out_path.exists():
                raise Exception("FFmpeg 未生成输出文件")

        await asyncio.to_thread(_run)
        return str(out_path)
    
    def _is_cookie_lock_error(self, exc: Exception) -> bool:
        """判断是否为浏览器 cookie 数据库被锁的错误。"""
        msg = str(exc).lower()
        return ('cookie' in msg and ('copy' in msg or 'lock' in msg or 'database' in msg or '7271' in msg))

    def _strip_cookies(self, opts: dict) -> dict:
        """返回去掉 cookie 相关键的新 opts 副本。"""
        return {k: v for k, v in opts.items() if k not in ('cookiefile', 'cookiesfrombrowser')}

    def _disable_cookies(self):
        """首次遇到 cookie 锁错误时，禁用后续 cookie 尝试并记录一次警告。"""
        if self._cookies_source:
            logger.warning(
                "浏览器 cookie 数据库被锁定（Edge/Chrome 正在运行），"
                "BiliBili 字幕将不可用。关闭浏览器后重启服务即可。"
            )
            self._cookies_source = None
            for key in ('cookiefile', 'cookiesfrombrowser'):
                self.ydl_opts.pop(key, None)

    async def fetch_subtitles(self, url: str, output_dir: Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        先尝试从平台获取字幕文本，比下载音频快得多。

        Returns:
            (subtitle_markdown, video_title, language_code)
            subtitle_markdown 为 None 表示无可用字幕。
        """
        import asyncio

        output_dir.mkdir(exist_ok=True)
        unique_id = str(uuid.uuid4())[:8]
        sub_dir = output_dir / f"subs_{unique_id}"

        try:
            # 1. 获取视频标题（轻量探测，不拉字幕）
            check_opts = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "http_headers": self.ydl_opts.get("http_headers", {}),
            }
            if "cookiefile" in self.ydl_opts:
                check_opts["cookiefile"] = self.ydl_opts["cookiefile"]
            if "cookiesfrombrowser" in self.ydl_opts:
                check_opts["cookiesfrombrowser"] = self.ydl_opts["cookiesfrombrowser"]
            try:
                with yt_dlp.YoutubeDL(check_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, False)
            except Exception as e:
                if self._is_cookie_lock_error(e):
                    self._disable_cookies()
                    with yt_dlp.YoutubeDL(self._strip_cookies(check_opts)) as ydl:
                        info = await asyncio.to_thread(ydl.extract_info, url, False)
                else:
                    raise

            video_title = info.get("title", "unknown")

            # 2. 直接尝试下载字幕（extract_info 返回的字幕信息不可靠，BiliBili
            #    等站点的字幕 API 只在 writesubtitles/writeautomaticsub 开启时才调用）
            sub_dir.mkdir(exist_ok=True)
            dl_opts = {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitlesformat": "vtt/srt/best",
                "subtitleslangs": ["all"],
                "skip_download": True,
                "outtmpl": str(sub_dir / "sub.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "http_headers": self.ydl_opts.get("http_headers", {}),
            }
            if "cookiefile" in self.ydl_opts:
                dl_opts["cookiefile"] = self.ydl_opts["cookiefile"]
            if "cookiesfrombrowser" in self.ydl_opts:
                dl_opts["cookiesfrombrowser"] = self.ydl_opts["cookiesfrombrowser"]
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])
            except Exception as e:
                if self._is_cookie_lock_error(e):
                    self._disable_cookies()
                    with yt_dlp.YoutubeDL(self._strip_cookies(dl_opts)) as ydl:
                        await asyncio.to_thread(ydl.download, [url])
                else:
                    raise

            # 3. 查找下载的字幕文件（排除弹幕 xml）
            sub_files = [f for f in list(sub_dir.glob("*.vtt")) + list(sub_dir.glob("*.srt"))
                         if "danmaku" not in f.name.lower()]
            if not sub_files:
                logger.info(f"视频无可用字幕: {url}")
                return None, video_title, None

            # 4. 按语言优先级选最佳字幕
            _lang_priority = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-HK", "en", "en-US", "ja", "ko"]
            sub_file = None
            for lang in _lang_priority:
                for f in sub_files:
                    if lang.lower() in f.stem.lower():
                        sub_file = f
                        break
                if sub_file:
                    break
            if not sub_file:
                sub_file = sub_files[0]  # fallback

            # 从文件名提取语言代码
            stem_parts = sub_file.stem.split(".")
            file_lang = stem_parts[-1] if len(stem_parts) > 1 else "unknown"

            # 5. 解析字幕文件
            if sub_file.suffix == ".vtt":
                entries = self._parse_vtt(str(sub_file))
            else:
                entries = self._parse_srt(str(sub_file))

            if not entries:
                logger.warning("字幕解析结果为空，回退音频模式")
                return None, video_title, None

            # 6. 格式化为与 Whisper 输出兼容的 Markdown
            formatted = self._format_subtitle_entries(entries, file_lang)
            logger.info(f"字幕获取成功: lang={file_lang}, {len(entries)} 条目")
            return formatted, video_title, file_lang

        except Exception as e:
            logger.warning(f"字幕获取失败（将回退至音频下载）: {e}")
            return None, None, None
        finally:
            if sub_dir.exists():
                try:
                    shutil.rmtree(str(sub_dir))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 字幕解析辅助方法
    # ------------------------------------------------------------------

    def _parse_vtt(self, filepath: str) -> list:
        """解析 WebVTT 字幕文件，返回去重后的条目列表。

        特别处理 YouTube 自动字幕的「滚动追加」格式：
        同一句话会被分成多个 cue 逐字追加，只保留每组的「最终版本」。
        """
        raw_entries = []
        seen_texts: set = set()

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"读取 VTT 文件失败: {e}")
            return []

        # 移除 WEBVTT 文件头，按空行分割 cue 块
        content = re.sub(r"^WEBVTT[^\n]*\n", "", content)
        blocks = re.split(r"\n{2,}", content.strip())

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            lines = block.split("\n")
            timing_idx = next((i for i, l in enumerate(lines) if "-->" in l), -1)
            if timing_idx < 0:
                continue

            timing_line = lines[timing_idx]
            text_lines = lines[timing_idx + 1:]

            match = re.match(
                r"(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*-->\s*"
                r"(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)",
                timing_line,
            )
            if not match:
                continue

            start_str = self._normalize_time(match.group(1))
            end_str = self._normalize_time(match.group(2))

            raw_text = " ".join(text_lines)
            # 去除 HTML / VTT 内联标签（包括 YouTube 逐字时间码标签）
            text = re.sub(r"<[^>]+>", "", raw_text)
            text = (
                text.replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&nbsp;", " ")
                    .replace("&#39;", "'")
                    .replace("&quot;", '"')
                    .strip()
            )
            # 合并行内多余空白
            text = re.sub(r"\s+", " ", text).strip()

            if not text or text in seen_texts:
                continue

            seen_texts.add(text)
            raw_entries.append({"start": start_str, "end": end_str, "text": text})

        # ── 二次去重：过滤 YouTube「滚动追加」的中间状态 ──────────────────
        # 滚动追加特征：条目 i 文本是条目 i+1 文本的前缀子串，逐字追加。
        # 保守策略：仅当文本不以句末标点结尾时才可能被标记为中间态。
        # 完整句子即使被后续条目包含，也保留。
        if not raw_entries:
            return []

        entries = []
        for i, entry in enumerate(raw_entries):
            text = entry["text"]
            if len(text) < 2:
                continue
            # 如果文本以句末标点结尾，说明是完整句子，直接保留
            if text.rstrip()[-1] in "。！？.!?":
                entries.append(entry)
                continue
            # 检查后续若干条是否以当前文本开头（滚动追加的特征）
            is_intermediate = False
            for j in range(i + 1, min(i + 4, len(raw_entries))):
                next_text = raw_entries[j]["text"]
                if next_text.startswith(text) and len(next_text) > len(text):
                    is_intermediate = True
                    break
            if not is_intermediate:
                entries.append(entry)

        return entries

    def _parse_srt(self, filepath: str) -> list:
        """解析 SRT 字幕文件，返回去重后的条目列表。"""
        entries = []
        seen_texts: set = set()

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"读取 SRT 文件失败: {e}")
            return []

        blocks = re.split(r"\n{2,}", content.strip())

        for block in blocks:
            lines = block.strip().split("\n")
            timing_idx = next((i for i, l in enumerate(lines) if "-->" in l), -1)
            if timing_idx < 0:
                continue

            timing_line = lines[timing_idx]
            text_lines = lines[timing_idx + 1:]

            # SRT 允许 MM:SS,mmm 或 HH:MM:SS,mmm 两种格式
            match = re.match(
                r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d+)\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d+)",
                timing_line,
            )
            if not match:
                continue

            start_str = self._normalize_time(match.group(1))
            end_str = self._normalize_time(match.group(2))

            text = " ".join(text_lines)
            text = re.sub(r"<[^>]+>", "", text).strip()

            if not text or text in seen_texts:
                continue

            seen_texts.add(text)
            entries.append({"start": start_str, "end": end_str, "text": text})

        return entries

    def _normalize_time(self, time_str: str) -> str:
        """将 HH:MM:SS.mmm 或 MM:SS.mmm 统一转为 MM:SS 格式。"""
        time_str = re.sub(r"[.,]\d+$", "", time_str)
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{h * 60 + m:02d}:{s:02d}"
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return f"{m:02d}:{s:02d}"
        return time_str

    def _format_subtitle_entries(self, entries: list, language: str) -> str:
        """将字幕条目格式化为连续文章（无时间戳），供下游管道直接使用。"""
        lines = [
            "# Video Transcription",
            "",
            f"**Detected Language:** {language}",
            "**Language Probability:** 1.00",
            "",
            "## Transcription Content",
            "",
        ]
        for entry in entries:
            text = entry.get("text", "").strip()
            if text:
                lines.append(text)
        return "\n\n".join(lines)

    async def download_and_convert(
        self,
        url: str,
        output_dir: Path,
        prefetched_title: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        下载视频并转换为m4a格式。

        prefetched_title: 若调用方已通过 fetch_subtitles 探测过视频信息，
        可直接传入视频标题，跳过重复的 extract_info 网络请求。
        """
        try:
            # 创建输出目录
            output_dir.mkdir(exist_ok=True)
            
            # 生成唯一的文件名
            unique_id = str(uuid.uuid4())[:8]
            output_template = str(output_dir / f"audio_{unique_id}.%(ext)s")
            
            # 更新yt-dlp选项
            ydl_opts = self.ydl_opts.copy()
            ydl_opts['outtmpl'] = output_template

            logger.info(f"开始下载视频: {url}")

            import asyncio

            def _do_extract_and_download(opts):
                """同步执行 yt-dlp 提取和下载，遇 cookie 锁错误自动重试。"""
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = None if prefetched_title else ydl.extract_info(url, False)
                        ydl.download([url])
                        return (
                            prefetched_title or (info.get('title', 'unknown') if info else 'unknown'),
                            (info.get('duration') or 0) if info else 0,
                        )
                except Exception as e:
                    if self._is_cookie_lock_error(e):
                        self._disable_cookies()
                        return _do_extract_and_download(self._strip_cookies(opts))
                    raise

            video_title, expected_duration = await asyncio.to_thread(
                _do_extract_and_download, ydl_opts
            )
            
            # 查找生成的m4a文件
            audio_file = str(output_dir / f"audio_{unique_id}.m4a")
            
            if not os.path.exists(audio_file):
                # 如果m4a文件不存在，查找其他音频格式
                for ext in ['webm', 'mp4', 'mp3', 'wav']:
                    potential_file = str(output_dir / f"audio_{unique_id}.{ext}")
                    if os.path.exists(potential_file):
                        audio_file = potential_file
                        break
                else:
                    raise Exception("未找到下载的音频文件")
            
            def _probe_duration(path: str) -> float:
                try:
                    out = subprocess.check_output(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", path]
                    ).decode().strip()
                    return float(out) if out else 0.0
                except Exception:
                    return 0.0

            # 仅当有预期时长时才探测（字幕路径不设置 expected_duration，跳过此开销）
            actual_duration = _probe_duration(audio_file) if expected_duration else 0.0

            if expected_duration and actual_duration and abs(actual_duration - expected_duration) / expected_duration > 0.1:
                logger.warning(
                    f"音频时长异常，期望{expected_duration}s，实际{actual_duration}s，尝试重封装修复…"
                )
                try:
                    fixed_path = str(output_dir / f"audio_{unique_id}_fixed.m4a")
                    subprocess.check_call([
                        "ffmpeg", "-y", "-i", audio_file, "-vn", "-c:a", "aac",
                        "-b:a", "192k", "-movflags", "+faststart", fixed_path
                    ])
                    audio_file = fixed_path
                    actual_duration2 = _probe_duration(audio_file)
                    logger.info(f"重封装完成，新时长≈{actual_duration2:.2f}s")
                except Exception as e:
                    logger.error(f"重封装失败：{e}")
            
            logger.info(f"音频文件已保存: {audio_file}")
            return audio_file, video_title
            
        except Exception as e:
            logger.error(f"下载视频失败: {str(e)}")
            raise Exception(f"下载视频失败: {str(e)}")
