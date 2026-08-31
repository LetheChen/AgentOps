"""extract_content 工具：URL/音视频内容提取（Layer B）。

背景：content_curator agent 接收用户提供的内容素材（博客 / 技术文档 / 音视频链接），
需要先提取为文本，再走价值评估 + 冲突检测。本工具封装多种提取器：
- article（博客/技术文档）：trafilatura
- pdf：pdfplumber（复用 obsidian_vault 的抽取器）
- video/audio：whisper-small 本地转文字（Q-2 默认，GPU 不可用降级到 whisper-tiny）
- auto：根据 URL 后缀 + content-type 自动判断

设计：
- lazy import 避免强依赖（缺 trafilatura/whisper 返回 missing_dependency 错误）
- async + 同步阻塞部分用 asyncio.to_thread 包裹
- fallback 策略：trafilatura 失败 → 提示用户手工粘贴；whisper-small 失败 → 降级到 tiny
- 不做 LLM 提炼（那是 agent 的职责），工具只做「文本提取」

参考配置：config/tools/extract_content.yaml
设计文档：docs/knowledge-base/DESIGN_content_curator_agent.md §3.1
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 支持的 content_type → 提取器
CONTENT_TYPES = ["auto", "article", "video", "audio", "pdf", "doc"]

# URL 后缀 → content_type
SUFFIX_MAP = {
    # video
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video", ".webm": "video",
    # audio
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio", ".aac": "audio", ".ogg": "audio",
    # pdf
    ".pdf": "pdf",
    # doc
    ".docx": "doc", ".doc": "doc",
}

# Whisper 模型降级链（Q-2 默认 small，GPU 不可用降级到 tiny）
WHISPER_FALLBACK_CHAIN = ["small", "tiny"]

# 默认提取文本最大长度
DEFAULT_MAX_LENGTH = 50000


async def extract_content(args: dict[str, Any]) -> dict[str, Any]:
    """URL/音视频内容提取工具。

    Args:
        args: dict
            - url (str, required): URL（http/https/file）
            - content_type (str): auto | article | video | audio | pdf | doc（默认 auto）
            - max_length (int): 提取文本最大长度（字符数，默认 50000）
            - agent_id (str): 调用方 agent id

    Returns:
        dict，至少包含 content（人类可读摘要）+ url + content_type + extracted_text。
        失败时包含 error 字段。
    """
    url = (args.get("url") or "").strip()
    content_type = (args.get("content_type") or "auto").strip().lower()
    max_length = int(args.get("max_length") or DEFAULT_MAX_LENGTH)
    agent_id = args.get("agent_id") or "unknown_agent"

    if not url:
        return {"content": "调用失败：缺少 url 参数", "error": "missing_url"}

    if content_type not in CONTENT_TYPES:
        return {
            "content": f"调用失败：未知 content_type '{content_type}'",
            "error": "unknown_content_type",
            "available_types": CONTENT_TYPES,
        }

    try:
        # 自动判断 content_type
        if content_type == "auto":
            content_type = _detect_content_type(url)
            logger.info("extract_content auto-detected type=%s url=%s", content_type, url)

        # 路由到对应提取器
        if content_type == "article":
            result = await _extract_article(url, max_length)
        elif content_type == "pdf":
            result = await _extract_pdf(url, max_length)
        elif content_type == "doc":
            result = await _extract_doc(url, max_length)
        elif content_type in ("video", "audio"):
            result = await _extract_media(url, content_type, max_length)
        else:
            return {
                "content": f"调用失败：未支持的 content_type '{content_type}'",
                "error": "unsupported_content_type",
            }

        # 注入 agent_id 用于审计
        result["agent_id"] = agent_id
        result["url"] = url
        result["content_type"] = content_type
        return result

    except Exception as e:
        logger.exception("extract_content failed url=%s type=%s: %s", url, content_type, e)
        return {
            "content": f"提取失败：{e}",
            "error": "extract_failed",
            "url": url,
            "content_type": content_type,
        }


# ==================== content_type 识别 ====================


def _detect_content_type(url: str) -> str:
    """根据 URL 后缀自动判断 content_type。"""
    parsed = urlparse(url)
    path = parsed.path.lower()
    for suffix, ctype in SUFFIX_MAP.items():
        if path.endswith(suffix):
            return ctype
    # 默认按 article 处理（trafilatura）
    return "article"


# ==================== article 提取（trafilatura）====================


async def _extract_article(url: str, max_length: int) -> dict[str, Any]:
    """用 trafilatura 提取博客/技术文档。"""
    try:
        import trafilatura
    except ImportError:
        return {
            "content": "提取失败：缺少依赖 trafilatura（pip install trafilatura）",
            "error": "missing_dependency",
            "missing_module": "trafilatura",
        }

    # trafilatura 是同步的，用 to_thread 包裹
    downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
    if not downloaded:
        return {
            "content": f"提取失败：URL 抓取失败（反爬/403/网络错误）：{url}",
            "error": "fetch_failed",
            "fallback_hint": "conversational 模式可提示用户手工粘贴正文文本",
        }

    # 提取 metadata + 正文
    try:
        metadata = await asyncio.to_thread(
            trafilatura.bare_extraction, downloaded, with_metadata=True
        )
    except Exception as e:
        logger.warning("trafilatura bare_extraction failed url=%s: %s", url, e)
        # fallback：不带 metadata 提取
        text = await asyncio.to_thread(trafilatura.extract, downloaded)
        if not text:
            return {
                "content": f"提取失败：trafilatura 解析失败：{url}",
                "error": "parse_failed",
                "fallback_hint": "conversational 模式可提示用户手工粘贴正文文本",
            }
        return _build_success(
            extracted_text=text[:max_length],
            title="", author="", published="",
            extracted_by="trafilatura",
            fallback_used=True,
        )

    if not metadata or not metadata.get("text"):
        return {
            "content": f"提取失败：trafilatura 返回空内容：{url}",
            "error": "empty_content",
        }

    text = metadata.get("text") or ""
    return _build_success(
        extracted_text=text[:max_length],
        title=metadata.get("title") or "",
        author=metadata.get("author") or "",
        published=metadata.get("date") or "",
        extracted_by="trafilatura",
        fallback_used=False,
    )


# ==================== pdf 提取（pdfplumber，复用 obsidian_vault 抽取器）====================


async def _extract_pdf(url: str, max_length: int) -> dict[str, Any]:
    """用 pdfplumber 提取 PDF 文本。"""
    try:
        import pdfplumber
    except ImportError:
        return {
            "content": "提取失败：缺少依赖 pdfplumber（pip install pdfplumber）",
            "error": "missing_dependency",
            "missing_module": "pdfplumber",
        }

    # 下载 PDF 到临时文件
    pdf_path = await _download_to_temp(url, suffix=".pdf")
    if not pdf_path:
        return {
            "content": f"提取失败：PDF 下载失败：{url}",
            "error": "download_failed",
        }

    try:
        def _extract():
            pages = []
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    pages.append(f"## 第 {i} 页\n\n{text}")
            return "\n\n".join(pages)

        text = await asyncio.to_thread(_extract)
        if not text.strip():
            return {
                "content": f"提取失败：PDF 无文本内容（可能是扫描件）：{url}",
                "error": "empty_content",
            }
        return _build_success(
            extracted_text=text[:max_length],
            title=pdf_path.stem, author="", published="",
            extracted_by="pdfplumber",
            fallback_used=False,
        )
    finally:
        # 清理临时文件
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            pass


# ==================== doc 提取（python-docx）====================


async def _extract_doc(url: str, max_length: int) -> dict[str, Any]:
    """用 python-docx 提取 Word 文档文本。"""
    try:
        import docx
    except ImportError:
        return {
            "content": "提取失败：缺少依赖 python-docx（pip install python-docx）",
            "error": "missing_dependency",
            "missing_module": "python-docx",
        }

    suffix = ".docx" if url.lower().endswith(".docx") else ".doc"
    doc_path = await _download_to_temp(url, suffix=suffix)
    if not doc_path:
        return {
            "content": f"提取失败：文档下载失败：{url}",
            "error": "download_failed",
        }

    try:
        def _extract():
            doc = docx.Document(doc_path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)

        text = await asyncio.to_thread(_extract)
        if not text.strip():
            return {
                "content": f"提取失败：文档无文本内容：{url}",
                "error": "empty_content",
            }
        return _build_success(
            extracted_text=text[:max_length],
            title=doc_path.stem, author="", published="",
            extracted_by="python-docx",
            fallback_used=False,
        )
    finally:
        try:
            doc_path.unlink(missing_ok=True)
        except OSError:
            pass


# ==================== video/audio 提取（whisper）====================


async def _extract_media(url: str, media_type: str, max_length: int) -> dict[str, Any]:
    """用 whisper 把音视频转文字。Q-2 默认 whisper-small，失败降级到 tiny。"""
    try:
        import whisper
    except ImportError:
        return {
            "content": "提取失败：缺少依赖 openai-whisper（pip install openai-whisper）",
            "error": "missing_dependency",
            "missing_module": "openai-whisper",
        }

    # 下载媒体文件
    suffix = ".mp4" if media_type == "video" else ".mp3"
    media_path = await _download_to_temp(url, suffix=suffix)
    if not media_path:
        return {
            "content": f"提取失败：{media_type} 下载失败：{url}",
            "error": "download_failed",
        }

    try:
        # 按降级链尝试 whisper 模型（small → tiny）
        last_error = None
        for model_name in WHISPER_FALLBACK_CHAIN:
            try:
                logger.info("whisper loading model=%s url=%s", model_name, url)
                model = await asyncio.to_thread(whisper.load_model, model_name)
                result = await asyncio.to_thread(model.transcribe, str(media_path))
                text = result.get("text", "").strip()
                if not text:
                    last_error = "whisper 返回空文本"
                    continue
                return _build_success(
                    extracted_text=text[:max_length],
                    title=media_path.stem, author="", published="",
                    extracted_by=f"whisper-{model_name}",
                    fallback_used=(model_name != WHISPER_FALLBACK_CHAIN[0]),
                )
            except Exception as e:
                logger.warning("whisper model=%s failed url=%s: %s", model_name, url, e)
                last_error = str(e)
                continue

        return {
            "content": f"提取失败：所有 whisper 模型都失败（{last_error}）：{url}",
            "error": "transcribe_failed",
            "fallback_chain_tried": WHISPER_FALLBACK_CHAIN,
        }
    finally:
        try:
            media_path.unlink(missing_ok=True)
        except OSError:
            pass


# ==================== 辅助函数 ====================


async def _download_to_temp(url: str, suffix: str) -> Path | None:
    """下载 URL 到临时文件，返回路径；失败返回 None。"""
    import urllib.request

    try:
        # 创建临时文件
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="extract_")
        Path(tmp_path).unlink(missing_ok=True)  # 删掉空文件，让 urlretrieve 重建

        # file:// 直接 copy，http(s):// 用 urlretrieve
        if url.startswith("file://"):
            src = url[7:]  # 去掉 file://
            import shutil
            shutil.copy(src, tmp_path)
        else:
            def _download():
                urllib.request.urlretrieve(url, tmp_path)
            await asyncio.to_thread(_download)

        return Path(tmp_path)
    except Exception as e:
        logger.warning("download failed url=%s: %s", url, e)
        return None


def _build_success(
    extracted_text: str,
    title: str,
    author: str,
    published: str,
    extracted_by: str,
    fallback_used: bool,
) -> dict[str, Any]:
    """构造成功返回结构。"""
    word_count = len(extracted_text)
    return {
        "content": f"提取成功：{extracted_by}（{word_count} 字符）{'+ fallback' if fallback_used else ''}",
        "extracted_text": extracted_text,
        "title": title,
        "author": author,
        "published": published,
        "word_count": word_count,
        "extracted_by": extracted_by,
        "fallback_used": fallback_used,
        "truncated": word_count >= DEFAULT_MAX_LENGTH,
    }
