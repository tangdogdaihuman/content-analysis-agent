import os
import re
import asyncio
import openai
import logging
from typing import Optional

# 全局并发限制，防止过多同步 API 调用
API_SEMAPHORE = asyncio.Semaphore(8)

from llm_sanitize import strip_llm_artifacts

logger = logging.getLogger(__name__)

class Summarizer:
    """文本总结器，使用OpenAI API生成多语言摘要"""
    
    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        """
        初始化总结器。

        优先级：参数 > 环境变量。
        model 指定时会同时作为 fast_model 和 advanced_model 使用。
        """
        effective_key = api_key or os.getenv("OPENAI_API_KEY")
        effective_url = base_url or os.getenv("OPENAI_BASE_URL")

        if not effective_key:
            logger.warning("未设置OPENAI_API_KEY环境变量，将无法使用摘要功能")

        if effective_key:
            kwargs = {"api_key": effective_key, "timeout": 120.0, "max_retries": 1}
            if effective_url:
                kwargs["base_url"] = effective_url
                logger.info(f"OpenAI客户端已初始化，base_url={effective_url}")
            else:
                logger.info("OpenAI客户端已初始化，使用默认端点")
            self.client = openai.OpenAI(**kwargs)
        else:
            self.client = None

        # 允许前端指定模型，env MODEL_NAME -> 硬编码后备
        env_model = os.getenv("MODEL_NAME", "")
        self.fast_model     = model or env_model or "gpt-3.5-turbo"
        self.advanced_model = model or env_model or "gpt-4o"
        
        # 支持的语言映射
        self.language_map = {
            "en": "English",
            "zh": "中文（简体）",
            "es": "Español",
            "fr": "Français", 
            "de": "Deutsch",
            "it": "Italiano",
            "pt": "Português",
            "ru": "Русский",
            "ja": "日本語",
            "ko": "한국어",
            "ar": "العربية"
        }
    
    async def optimize_transcript(self, raw_transcript: str) -> str:
        """
        优化转录文本：修正错别字，按含义分段
        支持长文本自动分块处理
        
        Args:
            raw_transcript: 原始转录文本
            
        Returns:
            优化后的转录文本（Markdown格式）
        """
        try:
            if not self.client:
                logger.warning("OpenAI API不可用，返回原始转录")
                return raw_transcript

            # 预处理：仅移除时间戳与元信息，保留全部口语/重复内容
            preprocessed = self._remove_timestamps_and_meta(raw_transcript)
            # 使用JS策略：按字符长度分块（更贴近tokens上限，避免估算误差）
            detected_lang_code = self._detect_transcript_language(preprocessed)
            max_chars_per_chunk = 4000  # 对齐JS：每块最大约4000字符

            if len(preprocessed) > max_chars_per_chunk:
                logger.info(f"文本较长({len(preprocessed)} chars)，启用分块优化")
                return await self._format_long_transcript_in_chunks(preprocessed, detected_lang_code, max_chars_per_chunk)
            else:
                return await self._format_single_chunk(preprocessed, detected_lang_code)

        except Exception as e:
            logger.error(f"优化转录文本失败: {str(e)}")
            logger.info("返回原始转录文本")
            return raw_transcript

    def _estimate_tokens(self, text: str) -> int:
        """
        改进的token数量估算算法
        更保守的估算，考虑系统prompt和格式化开销
        """
        # 更保守的估算：考虑实际使用中的token膨胀
        chinese_chars = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
        english_words = len([word for word in text.split() if word.isascii() and word.isalpha()])
        
        # 计算基础tokens
        base_tokens = chinese_chars * 1.5 + english_words * 1.3
        
        # 考虑markdown格式、时间戳等开销（约30%额外开销）
        format_overhead = len(text) * 0.15
        
        # 考虑系统prompt开销（约2000-3000 tokens）
        system_prompt_overhead = 2500
        
        total_estimated = int(base_tokens + format_overhead + system_prompt_overhead)
        
        return total_estimated

    # ===== JS openaiService.js 移植：分块/上下文/去重/格式化 =====

    def _ensure_markdown_paragraphs(self, text: str) -> str:
        """确保Markdown段落空行、标题后空行、压缩多余空行。"""
        if not text:
            return text
        formatted = text.replace("\r\n", "\n")
        # 标题后加空行
        formatted = re.sub(r"(^#{1,6}\s+.*)\n([^\n#])", r"\1\n\n\2", formatted, flags=re.M)
        # 压缩≥3个换行为2个
        formatted = re.sub(r"\n{3,}", "\n\n", formatted)
        # 去首尾空行
        formatted = re.sub(r"^\n+", "", formatted)
        formatted = re.sub(r"\n+$", "", formatted)
        return formatted

    async def _format_single_chunk(self, chunk_text: str, transcript_language: str = 'zh') -> str:
        """单块优化（修正+格式化），遵循4000 tokens 限制。"""
        # 构建与JS版一致的系统/用户提示
        if transcript_language == 'zh':
            prompt = (
                "请对以下音频转录文本进行智能优化和格式化，要求：\n\n"
                "**内容优化（正确性优先）：**\n"
                "1. 错误修正（转录错误/错别字/同音字/专有名词）\n"
                "2. 适度改善语法，补全不完整句子，保持原意和语言不变\n"
                "3. 口语处理：保留自然口语与重复表达，不要删减内容，仅添加必要标点\n"
                "4. **绝对不要改变人称代词（I/我、you/你等）和说话者视角**\n\n"
                "**分段规则：**\n"
                "- 按主题和逻辑含义分段，每段包含1-8个相关句子\n"
                "- 单段长度不超过400字符\n"
                "- 避免过多的短段落，合并相关内容\n\n"
                "**格式要求：**Markdown 段落，段落间空行\n\n"
                f"原始转录文本：\n{chunk_text}"
            )
            system_prompt = (
                "你是专业的音频转录文本优化助手，修正错误、改善通顺度和排版格式，"
                "必须保持原意，不得删减口语/重复/细节；仅移除时间戳或元信息。"
                "绝对不要改变人称代词或说话者视角。这可能是访谈对话，访谈者用'you'，被访者用'I/we'。"
            )
        else:
            prompt = (
                "Please intelligently optimize and format the following audio transcript text:\n\n"
                "Content Optimization (Accuracy First):\n"
                "1. Error Correction (typos, homophones, proper nouns)\n"
                "2. Moderate grammar improvement, complete incomplete sentences, keep original language/meaning\n"
                "3. Speech processing: keep natural fillers and repetitions, do NOT remove content; only add punctuation if needed\n"
                "4. **NEVER change pronouns (I, you, he, she, etc.) or speaker perspective**\n\n"
                "Segmentation Rules: Group 1-8 related sentences per paragraph by topic/logic; paragraph length NOT exceed 400 characters; avoid too many short paragraphs\n\n"
                "Format: Markdown paragraphs with blank lines between paragraphs\n\n"
                f"Original transcript text:\n{chunk_text}"
            )
            system_prompt = (
                "You are a professional transcript formatting assistant. Fix errors and improve fluency "
                "without changing meaning or removing any content; only timestamps/meta may be removed; keep Markdown paragraphs with blank lines. "
                "NEVER change pronouns or speaker perspective. This may be an interview: interviewer uses 'you', interviewee uses 'I/we'."
            )

        try:
            response = self.client.chat.completions.create(
                model=self.fast_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=4000,  # 对齐JS：优化/格式化阶段最大tokens≈4000
                temperature=0.1
            )
            optimized_text = strip_llm_artifacts(response.choices[0].message.content or "")
            # 移除诸如 "# Transcript" / "## Transcript" 等标题
            optimized_text = self._remove_transcript_heading(optimized_text)
            enforced = self._enforce_paragraph_max_chars(optimized_text.strip(), max_chars=400)
            return self._ensure_markdown_paragraphs(enforced)
        except Exception as e:
            logger.error(f"单块文本优化失败: {e}")
            return self._apply_basic_formatting(chunk_text)

    def _smart_split_long_chunk(self, text: str, max_chars_per_chunk: int) -> list:
        """在句子/空格边界处安全切分超长文本。"""
        chunks = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_chars_per_chunk, len(text))
            if end < len(text):
                # 优先句子边界
                sentence_endings = ['。', '！', '？', '.', '!', '?']
                best = -1
                for ch in sentence_endings:
                    idx = text.rfind(ch, pos, end)
                    if idx > best:
                        best = idx
                if best > pos + int(max_chars_per_chunk * 0.7):
                    end = best + 1
                else:
                    # 次选：空格边界
                    space_idx = text.rfind(' ', pos, end)
                    if space_idx > pos + int(max_chars_per_chunk * 0.8):
                        end = space_idx
            chunks.append(text[pos:end].strip())
            pos = end
        return [c for c in chunks if c]

    def _find_safe_cut_point(self, text: str) -> int:
        """找到安全的切割点（段落>句子>短语）。"""
        # 段落
        p = text.rfind("\n\n")
        if p > 0:
            return p + 2
        # 句子
        last_sentence_end = -1
        for m in re.finditer(r"[。！？\.!?]\s*", text):
            last_sentence_end = m.end()
        if last_sentence_end > 20:
            return last_sentence_end
        # 短语
        last_phrase_end = -1
        for m in re.finditer(r"[，；,;]\s*", text):
            last_phrase_end = m.end()
        if last_phrase_end > 20:
            return last_phrase_end
        return len(text)

    def _find_overlap_between_texts(self, text1: str, text2: str) -> str:
        """检测相邻两段的重叠内容，用于去重。"""
        max_len = min(len(text1), len(text2))
        # 逐步从长到短尝试
        for length in range(max_len, 19, -1):
            suffix = text1[-length:]
            prefix = text2[:length]
            if suffix == prefix:
                cut = self._find_safe_cut_point(prefix)
                if cut > 20:
                    return prefix[:cut]
                return suffix
        return ""

    def _apply_basic_formatting(self, text: str) -> str:
        """当AI失败时的回退：按句子拼段，段落≤250字符，双换行分隔。"""
        if not text or not text.strip():
            return text
        parts = re.split(r"([。！？\.!?]+\s*)", text)
        sentences = []
        current = ""
        for i, part in enumerate(parts):
            if i % 2 == 0:
                current += part
            else:
                current += part
                if current.strip():
                    sentences.append(current.strip())
                    current = ""
        if current.strip():
            sentences.append(current.strip())
        paras = []
        cur = ""
        sentence_count = 0
        for s in sentences:
            candidate = (cur + " " + s).strip() if cur else s
            sentence_count += 1
            # 改进的分段逻辑：考虑句子数量和长度
            should_break = False
            if len(candidate) > 400 and cur:  # 段落过长
                should_break = True
            elif len(candidate) > 200 and sentence_count >= 3:  # 中等长度且句子数足够
                should_break = True
            elif sentence_count >= 6:  # 句子数过多
                should_break = True
            
            if should_break:
                paras.append(cur.strip())
                cur = s
                sentence_count = 1
            else:
                cur = candidate
        if cur.strip():
            paras.append(cur.strip())
        return self._ensure_markdown_paragraphs("\n\n".join(paras))

    async def _format_long_transcript_in_chunks(self, raw_transcript: str, transcript_language: str, max_chars_per_chunk: int) -> str:
        """智能分块+上下文+去重 合成优化文本（JS策略移植）。"""
        # 先按句子切分，组装不超过max_chars_per_chunk的块
        parts = re.split(r"([。！？\.!?]+\s*)", raw_transcript)
        sentences = []
        buf = ""
        for i, part in enumerate(parts):
            if i % 2 == 0:
                buf += part
            else:
                buf += part
                if buf.strip():
                    sentences.append(buf.strip())
                    buf = ""
        if buf.strip():
            sentences.append(buf.strip())

        chunks = []
        cur = ""
        for s in sentences:
            candidate = (cur + " " + s).strip() if cur else s
            if len(candidate) > max_chars_per_chunk and cur:
                chunks.append(cur.strip())
                cur = s
            else:
                cur = candidate
        if cur.strip():
            chunks.append(cur.strip())

        # 对仍然过长的块二次安全切分
        final_chunks = []
        for c in chunks:
            if len(c) <= max_chars_per_chunk:
                final_chunks.append(c)
            else:
                final_chunks.extend(self._smart_split_long_chunk(c, max_chars_per_chunk))

        logger.info(f"文本分为 {len(final_chunks)} 块处理")

        # 构建带上下文的块（全从原文取，各块独立可并行）
        chunk_contexts = []
        for i, c in enumerate(final_chunks):
            chunk_with_context = c
            if i > 0:
                prev_tail = final_chunks[i - 1][-100:]
                marker = f"[上文续：{prev_tail}]" if transcript_language == 'zh' else f"[Context continued: {prev_tail}]"
                chunk_with_context = marker + "\n\n" + c
            chunk_contexts.append(chunk_with_context)

        async def _process_one_format_chunk(i: int, chunk_with_context: str):
            try:
                oc = await self._format_single_chunk(chunk_with_context, transcript_language)
                oc = re.sub(r"^\[(上文续|Context continued)：?:?.*?\]\s*", "", oc, flags=re.S)
                return i, oc
            except Exception as e:
                logger.warning(f"第 {i+1} 块优化失败，使用基础格式化: {e}")
                return i, self._apply_basic_formatting(final_chunks[i])

        results = await asyncio.gather(*[_process_one_format_chunk(i, cc) for i, cc in enumerate(chunk_contexts)])
        optimized = [None] * len(results)
        for i, oc in results:
            optimized[i] = oc
        optimized = [o for o in optimized if o]

        # 邻接块去重
        deduped = []
        for i, c in enumerate(optimized):
            cur_txt = c
            if i > 0 and deduped:
                prev = deduped[-1]
                overlap = self._find_overlap_between_texts(prev[-200:], cur_txt[:200])
                if overlap:
                    cur_txt = cur_txt[len(overlap):].lstrip()
                    if not cur_txt:
                        continue
            if cur_txt.strip():
                deduped.append(cur_txt)

        merged = "\n\n".join(deduped)
        merged = self._remove_transcript_heading(merged)
        enforced = self._enforce_paragraph_max_chars(merged, max_chars=400)
        return self._ensure_markdown_paragraphs(enforced)

    def _remove_timestamps_and_meta(self, text: str) -> str:
        """仅移除时间戳行与明显元信息（标题、检测语言等），保留原文口语/重复。"""
        lines = text.split('\n')
        kept = []
        for line in lines:
            s = line.strip()
            # 跳过时间戳与元信息
            if (s.startswith('**[') and s.endswith(']**')):
                continue
            if s.startswith('# '):
                # 跳过顶级标题（通常是视频标题，可在最终加回）
                continue
            if s.startswith('**检测语言:**') or s.startswith('**语言概率:**'):
                continue
            kept.append(line)
        # 规范空行
        cleaned = '\n'.join(kept)
        return cleaned

    def _enforce_paragraph_max_chars(self, text: str, max_chars: int = 400) -> str:
        """按段落拆分并确保每段不超过max_chars，必要时按句子边界拆为多段。"""
        if not text:
            return text
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if p is not None]
        new_paragraphs = []
        for para in paragraphs:
            para = para.strip()
            if len(para) <= max_chars:
                new_paragraphs.append(para)
                continue
            # 句子切分
            parts = re.split(r"([。！？\.!?]+\s*)", para)
            sentences = []
            buf = ""
            for i, part in enumerate(parts):
                if i % 2 == 0:
                    buf += part
                else:
                    buf += part
                    if buf.strip():
                        sentences.append(buf.strip())
                        buf = ""
            if buf.strip():
                sentences.append(buf.strip())
            cur = ""
            for s in sentences:
                candidate = (cur + (" " if cur else "") + s).strip()
                if len(candidate) > max_chars and cur:
                    new_paragraphs.append(cur)
                    cur = s
                else:
                    cur = candidate
            if cur:
                new_paragraphs.append(cur)
        return "\n\n".join([p.strip() for p in new_paragraphs if p is not None])

    def _remove_transcript_heading(self, text: str) -> str:
        """移除开头或段落中的以 Transcript 为标题的行（任意级别#），不改变正文。"""
        if not text:
            return text
        # 移除形如 '## Transcript'、'# Transcript Text'、'### transcript' 的标题行
        lines = text.split('\n')
        filtered = []
        for line in lines:
            stripped = line.strip()
            if re.match(r"^#{1,6}\s*transcript(\s+text)?\s*$", stripped, flags=re.I):
                continue
            filtered.append(line)
        return '\n'.join(filtered)

    async def _final_paragraph_organization(self, text: str, lang_instruction: str) -> str:
        """
        对合并后的文本进行最终的段落整理
        使用改进的prompt和工程验证
        """
        try:
            # 估算文本长度，如果太长则分块处理
            estimated_tokens = self._estimate_tokens(text)
            if estimated_tokens > 3000:  # 对于很长的文本，分块处理
                return await self._organize_long_text_paragraphs(text, lang_instruction)
            
            system_prompt = f"""你是专业的{lang_instruction}文本段落整理专家。你的任务是按照语义和逻辑重新组织段落。

🎯 **核心原则**：
1. **严格保持原始语言({lang_instruction})**，绝不翻译
2. **保持所有内容完整**，不删除不添加任何信息
3. **按语义逻辑分段**：每段围绕一个完整的思想或话题
4. **严格控制段落长度**：每段绝不超过250词
5. **保持自然流畅**：段落间应有逻辑连接

📏 **分段标准**：
- **语义完整性**：每段讲述一个完整概念或事件
- **适中长度**：3-7个句子，每段绝不超过250词
- **逻辑边界**：在话题转换、时间转换、观点转换处分段
- **自然断点**：遵循说话者的自然停顿和逻辑

⚠️ **严禁**：
- 创造超过250词的巨型段落
- 强行合并不相关的内容
- 打断完整的故事或论述

输出格式：段落间用空行分隔。"""

            user_prompt = f"""请重新整理以下{lang_instruction}文本的段落结构。严格按照语义和逻辑进行分段，确保每段不超过200词：

{text}

重新分段后的文本："""

            response = self.client.chat.completions.create(
                model=self.advanced_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4000,  # 对齐JS：段落整理阶段最大tokens≈4000
                temperature=0.05  # 降低温度，提高一致性
            )
            
            organized_text = strip_llm_artifacts(response.choices[0].message.content or "")
            
            # 工程验证：检查段落长度
            validated_text = self._validate_paragraph_lengths(organized_text)
            
            return validated_text
            
        except Exception as e:
            logger.error(f"最终段落整理失败: {e}")
            # 失败时使用基础分段处理
            return self._basic_paragraph_fallback(text)

    async def _organize_long_text_paragraphs(self, text: str, lang_instruction: str) -> str:
        """
        对于很长的文本，分块进行段落整理
        """
        try:
            # 按现有段落分割
            paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
            organized_chunks = []
            
            current_chunk = []
            current_tokens = 0
            max_chunk_tokens = 2500  # 适应4000 tokens限制的chunk大小
            
            for para in paragraphs:
                para_tokens = self._estimate_tokens(para)
                
                if current_tokens + para_tokens > max_chunk_tokens and current_chunk:
                    # 处理当前chunk
                    chunk_text = '\n\n'.join(current_chunk)
                    organized_chunk = await self._organize_single_chunk(chunk_text, lang_instruction)
                    organized_chunks.append(organized_chunk)
                    
                    current_chunk = [para]
                    current_tokens = para_tokens
                else:
                    current_chunk.append(para)
                    current_tokens += para_tokens
            
            # 处理最后一个chunk
            if current_chunk:
                chunk_text = '\n\n'.join(current_chunk)
                organized_chunk = await self._organize_single_chunk(chunk_text, lang_instruction)
                organized_chunks.append(organized_chunk)
            
            return '\n\n'.join(organized_chunks)
            
        except Exception as e:
            logger.error(f"长文本段落整理失败: {e}")
            return self._basic_paragraph_fallback(text)

    async def _organize_single_chunk(self, text: str, lang_instruction: str) -> str:
        """
        整理单个文本块的段落
        """
        system_prompt = f"""You are a {lang_instruction} paragraph organization expert. Reorganize paragraphs by semantics, ensuring each paragraph does not exceed 200 words.

Core requirements:
1. Strictly maintain the original {lang_instruction} language
2. Organize by semantic logic, one theme per paragraph
3. Each paragraph must not exceed 250 words
4. Separate paragraphs with blank lines
5. Keep content complete, do not reduce information"""

        user_prompt = f"""Re-paragraph the following text in {lang_instruction}, ensuring each paragraph does not exceed 200 words:

{text}"""

        response = self.client.chat.completions.create(
            model=self.advanced_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=1200,  # 适应4000 tokens总限制
            temperature=0.05
        )
        
        return strip_llm_artifacts(response.choices[0].message.content or "")

    def _validate_paragraph_lengths(self, text: str) -> str:
        """
        验证段落长度，如果有超长段落则尝试分割
        """
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        validated_paragraphs = []
        
        for para in paragraphs:
            word_count = len(para.split())
            
            if word_count > 300:  # 如果段落超过300词
                logger.warning(f"检测到超长段落({word_count}词)，尝试分割")
                # 尝试按句子分割长段落
                split_paras = self._split_long_paragraph(para)
                validated_paragraphs.extend(split_paras)
            else:
                validated_paragraphs.append(para)
        
        return '\n\n'.join(validated_paragraphs)

    def _split_long_paragraph(self, paragraph: str) -> list:
        """
        分割过长的段落
        """
        
        # 按句子分割
        sentences = re.split(r'[.!?。！？]\s+', paragraph)
        sentences = [s.strip() + '.' for s in sentences if s.strip()]
        
        split_paragraphs = []
        current_para = []
        current_words = 0
        
        for sentence in sentences:
            sentence_words = len(sentence.split())
            
            if current_words + sentence_words > 200 and current_para:
                # 当前段落达到长度限制
                split_paragraphs.append(' '.join(current_para))
                current_para = [sentence]
                current_words = sentence_words
            else:
                current_para.append(sentence)
                current_words += sentence_words
        
        # 添加最后一段
        if current_para:
            split_paragraphs.append(' '.join(current_para))
        
        return split_paragraphs

    def _basic_paragraph_fallback(self, text: str) -> str:
        """
        基础分段fallback机制
        当GPT整理失败时，使用简单的规则分段
        """
        
        # 移除多余的空行
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        basic_paragraphs = []
        
        for para in paragraphs:
            word_count = len(para.split())
            
            if word_count > 250:
                # 长段落按句子分割
                split_paras = self._split_long_paragraph(para)
                basic_paragraphs.extend(split_paras)
            elif word_count < 30 and basic_paragraphs:
                # 短段落与上一段合并（如果合并后不超过200词）
                last_para = basic_paragraphs[-1]
                combined_words = len(last_para.split()) + word_count
                
                if combined_words <= 200:
                    basic_paragraphs[-1] = last_para + ' ' + para
                else:
                    basic_paragraphs.append(para)
            else:
                basic_paragraphs.append(para)
        
        return '\n\n'.join(basic_paragraphs)

    async def summarize(self, transcript: str, target_language: str = "zh", video_title: str = None) -> str:
        """
        生成视频转录的摘要
        
        Args:
            transcript: 转录文本
            target_language: 目标语言代码
            
        Returns:
            摘要文本（Markdown格式）
        """
        try:
            if not self.client:
                logger.warning("OpenAI API不可用，生成备用摘要")
                return self._generate_fallback_summary(transcript, target_language, video_title)
            
            # 估算转录文本长度，决定是否需要分块摘要
            estimated_tokens = self._estimate_tokens(transcript)
            max_summarize_tokens = 4000  # 提高限制，优先使用单文本处理以获得更好的总结质量
            
            if estimated_tokens <= max_summarize_tokens:
                # 短文本直接摘要
                return await self._summarize_single_text(transcript, target_language, video_title)
            else:
                # 长文本分块摘要
                logger.info(f"文本较长({estimated_tokens} tokens)，启用分块摘要")
                return await self._summarize_with_chunks(transcript, target_language, video_title, max_summarize_tokens)
            
        except Exception as e:
            logger.error(f"生成摘要失败: {str(e)}")
            return self._generate_fallback_summary(transcript, target_language, video_title)

    async def _summarize_single_text(self, transcript: str, target_language: str, video_title: str = None) -> str:
        """
        对单个文本进行摘要
        """
        # 获取目标语言名称
        language_name = self.language_map.get(target_language, "中文（简体）")
        
        # 构建英文提示词，适用于所有目标语言
        system_prompt = f"""You are a professional content analyst. Read the following transcript and produce a KEY POINTS SUMMARY in {language_name}.

WHAT TO DO:
1. Identify the speaker's MAIN ARGUMENT or central message in 2-3 sentences.
2. Extract 8-15 KEY POINTS. Each point should:
   - Capture one distinct idea, claim, or insight from the speaker
   - Be specific and substantive, not vague
   - Include supporting details: examples, data, counterarguments mentioned
   - Note timestamps when possible (e.g. [12:30])
3. List 3-5 NOTABLE QUOTES — verbatim statements that are particularly striking, controversial, or quotable.
4. End with a brief CONCLUSION summarizing who this content is relevant to and why.

FORMAT:
## 核心观点
(2-3 sentences capturing the thesis)

## 要点
- **[timestamp if available]** Point 1 with supporting detail...
- **[timestamp if available]** Point 2 with supporting detail...
- ...

## 关键引用
> "verbatim quote" — [timestamp]

## 适用人群
(1-2 sentences on target audience and value)

HARD RULES:
- Output in {language_name} ONLY. No preamble, no "Here is...", no closings like "希望以上内容..." or "let me know if...".
- Each key point must be a complete, self-contained insight — not a vague topic label.
- Do NOT skip sections of the transcript. Cover the full content."""

        user_prompt = f"""Extract key points from this transcript in {language_name}:

{transcript}"""

        logger.info(f"正在生成{language_name}要点总结...")
        
        # 调用OpenAI API
        response = self.client.chat.completions.create(
            model=self.advanced_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=4096,
            temperature=0.25
        )
        
        summary = strip_llm_artifacts(response.choices[0].message.content or "")

        return self._format_summary_with_meta(summary, target_language, video_title)

    async def _summarize_with_chunks(self, transcript: str, target_language: str, video_title: str, max_tokens: int) -> str:
        """
        分块摘要长文本
        """
        language_name = self.language_map.get(target_language, "中文（简体）")

        # 使用JS策略：按字符进行智能分块（段落>句子）
        chunks = self._smart_chunk_text(transcript, max_chars_per_chunk=4000)
        logger.info(f"分割为 {len(chunks)} 个块进行摘要")
        
        chunk_summaries = [None] * len(chunks)

        async def _process_one_chunk(i: int, chunk: str):
            system_prompt = f"""You are a content analyst. Write a detailed section analysis in {language_name}.

This is part {i+1} of {len(chunks)} of the full transcript.

Rules:
- Be thorough: extract ALL key points, arguments, data, and quotes from this section.
- Length: minimum 150 words in {language_name}, more if content is dense.
- Use bullet points for key claims and sub-points.
- Quote notable statements with approximate timestamps.
- No preamble or meta-closings."""

            user_prompt = f"""[Part {i+1}/{len(chunks)}] Provide a DETAILED analysis in {language_name}:

{chunk}

Output content only, no headings like "Analysis:" or "Summary:"."""

            try:
                async with API_SEMAPHORE:
                    response = await asyncio.to_thread(
                        lambda: self.client.chat.completions.create(
                        model=self.advanced_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=1200,
                        temperature=0.25
                    )
                )
                return i, strip_llm_artifacts(response.choices[0].message.content or "")
            except Exception as e:
                logger.error(f"摘要第 {i+1} 块失败: {e}")
                return i, f"第{i+1}部分内容概述：" + chunk[:200] + "..."

        tasks = [_process_one_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks)
        for i, summary in results:
            chunk_summaries[i] = summary
        chunk_summaries = [s for s in chunk_summaries if s]  # 过滤 None
        
        # 合并所有局部摘要（带编号），如分块较多则分层整合（不引入小标题）
        combined_summaries = "\n\n".join([f"[Part {idx+1}]\n" + s for idx, s in enumerate(chunk_summaries)])

        logger.info("正在整合最终摘要...")
        if len(chunk_summaries) > 10:
            final_summary = await self._integrate_hierarchical_summaries(chunk_summaries, target_language)
        else:
            final_summary = await self._integrate_chunk_summaries(combined_summaries, target_language)

        return self._format_summary_with_meta(final_summary, target_language, video_title)

    def _smart_chunk_text(self, text: str, max_chars_per_chunk: int = 3500) -> list:
        """智能分块（先段落后句子），按字符上限切分。"""
        chunks = []
        paragraphs = [p for p in text.split('\n\n') if p.strip()]
        cur = ""
        for p in paragraphs:
            candidate = (cur + "\n\n" + p).strip() if cur else p
            if len(candidate) > max_chars_per_chunk and cur:
                chunks.append(cur.strip())
                cur = p
            else:
                cur = candidate
        if cur.strip():
            chunks.append(cur.strip())

        # 二次按句子切分过长块
        final_chunks = []
        for c in chunks:
            if len(c) <= max_chars_per_chunk:
                final_chunks.append(c)
            else:
                sentences = [s.strip() for s in re.split(r"[。！？\.!?]+", c) if s.strip()]
                scur = ""
                for s in sentences:
                    candidate = (scur + '。' + s).strip() if scur else s
                    if len(candidate) > max_chars_per_chunk and scur:
                        final_chunks.append(scur.strip())
                        scur = s
                    else:
                        scur = candidate
                if scur.strip():
                    final_chunks.append(scur.strip())
        return final_chunks

    async def _integrate_hierarchical_summaries(
        self, chunk_summaries: list, target_language: str
    ) -> str:
        """Many partial summaries: fold through the same integrator as the <=10 case."""
        combined = "\n\n".join(
            f"[Part {idx + 1}]\n{s}" for idx, s in enumerate(chunk_summaries)
        )
        return await self._integrate_chunk_summaries(combined, target_language)

    async def _integrate_chunk_summaries(self, combined_summaries: str, target_language: str) -> str:
        """
        整合分块摘要为最终连贯摘要
        """
        language_name = self.language_map.get(target_language, "中文（简体）")
        
        try:
            system_prompt = f"""You are a professional content analyst. Merge these partial analyses into ONE comprehensive content analysis in {language_name}.

OUTPUT STRUCTURE:
## 核心观点
- Synthesize the central thesis across all sections (2-4 paragraphs).

## 内容拆解
Organize the merged content into logical topics. For each:
### {{{{topic title}}}}
- Integrated key points from relevant sections
- Supporting arguments and examples
- Notable quotes with timestamps

## 关键信息
- All important facts, statistics, names, dates
- Controversial or surprising claims
- Practical advice or takeaways

## 总结
- 1-2 paragraph synthesis
- Target audience and value

RULES:
- Remove ALL duplication across sections.
- Be comprehensive: minimum 500 words in {language_name}.
- No preamble or meta-closings."""
            
            user_prompt = f"""Merge the following partial analyses into one comprehensive analysis in {language_name}:

{combined_summaries}"""

            response = self.client.chat.completions.create(
                model=self.advanced_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4096,
                temperature=0.25
            )

            return strip_llm_artifacts(response.choices[0].message.content or "")
        except Exception as e:
            logger.error(f"整合摘要失败: {e}")
            # 失败时直接合并
            return combined_summaries

    def _format_summary_with_meta(self, summary: str, target_language: str, video_title: str = None) -> str:
        """
        为摘要添加标题和元信息
        """
        language_name = self.language_map.get(target_language, "中文（简体）")
        meta_labels = self._get_summary_labels(target_language)
        
        # 不加任何小标题/免责声明，可保留视频标题作为一级标题
        if video_title:
            prefix = f"# {video_title}\n\n"
        else:
            prefix = ""
        return prefix + summary

    def _generate_fallback_summary(self, transcript: str, target_language: str, video_title: str = None) -> str:
        """
        生成备用摘要（当OpenAI API不可用时）
        
        Args:
            transcript: 转录文本
            video_title: 视频标题
            target_language: 目标语言代码
            
        Returns:
            备用摘要文本
        """
        language_name = self.language_map.get(target_language, "中文（简体）")
        
        # 简单的文本处理，提取关键信息
        lines = transcript.split('\n')
        content_lines = [line for line in lines if line.strip() and not line.startswith('#') and not line.startswith('**')]
        
        # 计算大概的长度
        total_chars = sum(len(line) for line in content_lines)
        
        # 使用目标语言的标签
        meta_labels = self._get_summary_labels(target_language)
        fallback_labels = self._get_fallback_labels(target_language)
        
        # 直接使用视频标题作为主标题  
        title = video_title if video_title else "Summary"
        
        summary = f"""# {title}

**{meta_labels['language_label']}:** {language_name}
**{fallback_labels['notice']}:** {fallback_labels['api_unavailable']}



## {fallback_labels['overview_title']}

**{fallback_labels['content_length']}:** {fallback_labels['about']} {total_chars} {fallback_labels['characters']}
**{fallback_labels['paragraph_count']}:** {len(content_lines)} {fallback_labels['paragraphs']}

## {fallback_labels['main_content']}

{fallback_labels['content_description']}

{fallback_labels['suggestions_intro']}

1. {fallback_labels['suggestion_1']}
2. {fallback_labels['suggestion_2']}
3. {fallback_labels['suggestion_3']}

## {fallback_labels['recommendations']}

- {fallback_labels['recommendation_1']}
- {fallback_labels['recommendation_2']}


<br/>

<p style="color: #888; font-style: italic; text-align: center; margin-top: 16px;"><em>{fallback_labels['fallback_disclaimer']}</em></p>"""
        
        return summary
    
    def _get_current_time(self) -> str:
        """获取当前时间字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def get_supported_languages(self) -> dict:
        """
        获取支持的语言列表
        
        Returns:
            语言代码到语言名称的映射
        """
        return self.language_map.copy()
    
    def _detect_transcript_language(self, transcript: str) -> str:
        """
        检测转录文本的主要语言
        
        Args:
            transcript: 转录文本
            
        Returns:
            检测到的语言代码
        """
        # 简单的语言检测逻辑：查找转录文本中的语言标记
        if "**检测语言:**" in transcript:
            # 从Whisper转录中提取检测到的语言
            lines = transcript.split('\n')
            for line in lines:
                if "**检测语言:**" in line:
                    # 提取语言代码，例如: "**检测语言:** en"
                    lang = line.split(":")[-1].strip()
                    return lang
        
        # 如果没有找到语言标记，使用简单的字符检测
        # 计算英文字符、中文字符等的比例
        total_chars = len(transcript)
        if total_chars == 0:
            return "en"  # 默认英文
            
        # 统计中文字符
        chinese_chars = sum(1 for char in transcript if '\u4e00' <= char <= '\u9fff')
        chinese_ratio = chinese_chars / total_chars
        
        # 统计英文字母
        english_chars = sum(1 for char in transcript if char.isascii() and char.isalpha())
        english_ratio = english_chars / total_chars
        
        # 根据比例判断
        if chinese_ratio > 0.3:
            return "zh"
        elif english_ratio > 0.3:
            return "en"
        else:
            return "en"  # 默认英文
    
    def _get_language_instruction(self, lang_code: str) -> str:
        """
        根据语言代码获取优化指令中使用的语言名称
        
        Args:
            lang_code: 语言代码
            
        Returns:
            语言名称
        """
        language_instructions = {
            "en": "English",
            "zh": "中文",
            "ja": "日本語",
            "ko": "한국어",
            "es": "Español",
            "fr": "Français",
            "de": "Deutsch",
            "it": "Italiano",
            "pt": "Português",
            "ru": "Русский",
            "ar": "العربية"
        }
        return language_instructions.get(lang_code, "English")
    

    def _get_summary_labels(self, lang_code: str) -> dict:
        """
        获取摘要页面的多语言标签
        
        Args:
            lang_code: 语言代码
            
        Returns:
            标签字典
        """
        labels = {
            "en": {
                "language_label": "Summary Language",
                "disclaimer": "This summary is automatically generated by AI for reference only"
            },
            "zh": {
                "language_label": "摘要语言",
                "disclaimer": "本摘要由AI自动生成，仅供参考"
            },
            "ja": {
                "language_label": "要約言語",
                "disclaimer": "この要約はAIによって自動生成されており、参考用です"
            },
            "ko": {
                "language_label": "요약 언어",
                "disclaimer": "이 요약은 AI에 의해 자동 생성되었으며 참고용입니다"
            },
            "es": {
                "language_label": "Idioma del Resumen",
                "disclaimer": "Este resumen es generado automáticamente por IA, solo para referencia"
            },
            "fr": {
                "language_label": "Langue du Résumé",
                "disclaimer": "Ce résumé est généré automatiquement par IA, à titre de référence uniquement"
            },
            "de": {
                "language_label": "Zusammenfassungssprache",
                "disclaimer": "Diese Zusammenfassung wird automatisch von KI generiert, nur zur Referenz"
            },
            "it": {
                "language_label": "Lingua del Riassunto",
                "disclaimer": "Questo riassunto è generato automaticamente dall'IA, solo per riferimento"
            },
            "pt": {
                "language_label": "Idioma do Resumo",
                "disclaimer": "Este resumo é gerado automaticamente por IA, apenas para referência"
            },
            "ru": {
                "language_label": "Язык резюме",
                "disclaimer": "Это резюме автоматически генерируется ИИ, только для справки"
            },
            "ar": {
                "language_label": "لغة الملخص",
                "disclaimer": "هذا الملخص تم إنشاؤه تلقائياً بواسطة الذكاء الاصطناعي، للمرجع فقط"
            }
        }
        return labels.get(lang_code, labels["en"])
    
    def _get_fallback_labels(self, lang_code: str) -> dict:
        """
        获取备用摘要的多语言标签
        
        Args:
            lang_code: 语言代码
            
        Returns:
            标签字典
        """
        labels = {
            "en": {
                "notice": "Notice",
                "api_unavailable": "OpenAI API is unavailable, this is a simplified summary",
                "overview_title": "Transcript Overview",
                "content_length": "Content Length",
                "about": "About",
                "characters": "characters",
                "paragraph_count": "Paragraph Count",
                "paragraphs": "paragraphs",
                "main_content": "Main Content",
                "content_description": "The transcript contains complete video speech content. Since AI summary cannot be generated currently, we recommend:",
                "suggestions_intro": "For detailed information, we suggest you:",
                "suggestion_1": "Review the complete transcript text for detailed information",
                "suggestion_2": "Focus on important paragraphs marked with timestamps",
                "suggestion_3": "Manually extract key points and takeaways",
                "recommendations": "Recommendations",
                "recommendation_1": "Configure OpenAI API key for better summary functionality",
                "recommendation_2": "Or use other AI services for text summarization",
                "fallback_disclaimer": "This is an automatically generated fallback summary"
            },
            "zh": {
                "notice": "注意",
                "api_unavailable": "由于OpenAI API不可用，这是一个简化的摘要",
                "overview_title": "转录概览",
                "content_length": "内容长度",
                "about": "约",
                "characters": "字符",
                "paragraph_count": "段落数量",
                "paragraphs": "段",
                "main_content": "主要内容",
                "content_description": "转录文本包含了完整的视频语音内容。由于当前无法生成智能摘要，建议您：",
                "suggestions_intro": "为获取详细信息，建议您：",
                "suggestion_1": "查看完整的转录文本以获取详细信息",
                "suggestion_2": "关注时间戳标记的重要段落",
                "suggestion_3": "手动提取关键观点和要点",
                "recommendations": "建议",
                "recommendation_1": "配置OpenAI API密钥以获得更好的摘要功能",
                "recommendation_2": "或者使用其他AI服务进行文本总结",
                "fallback_disclaimer": "本摘要为自动生成的备用版本"
            }
        }
        return labels.get(lang_code, labels["en"])
    
    def is_available(self) -> bool:
        """
        检查摘要服务是否可用
        
        Returns:
            True if OpenAI API is configured, False otherwise
        """
        return self.client is not None
