"""上下文构建器：预算、来源配额、去重和稳定引用编号。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from langchain_core.documents import Document

from app.core.config import (
    CONTEXT_TOKEN_SAFETY_RATIO,
    IMAGE_CONTEXT_MAX_PER_CHUNK,
    MAX_CHUNKS_PER_SOURCE,
    MAX_CONTEXT_CHUNKS,
    MAX_CONTEXT_TOKENS,
)
from rag.indexing.image_metadata import decode_images
from rag.indexing.code_segments import iter_code_segments
from rag.indexing.tokens import _get_encoder, count_tokens
from rag.loaders.image_extractor import IMAGE_PLACEHOLDER_PATTERN

logger = logging.getLogger(__name__)


def _truncate_at_boundary(text: str, max_tokens: int) -> str:
    """按句子/段落边界截断，不截断到半个多字节字符。"""
    enc = _get_encoder()
    if enc is not None:
        try:
            tokens = enc.encode(text)
            if len(tokens) <= max_tokens:
                return text
            truncated_tokens = tokens[:max_tokens]
            text = enc.decode(truncated_tokens)
        except Exception:
            pass

    # Character-based fallback
    max_chars = max_tokens * 2
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    # Find sentence boundary
    for sep in ["。", ".", "！", "！", "?", "？", "\n"]:
        idx = cut.rfind(sep)
        if idx > max_chars // 2:
            return cut[:idx + len(sep)]

    return cut


def _normalize_source(source: str | None) -> str:
    if not source:
        return "unknown"
    p = Path(str(source))
    parts = p.parts
    if parts and parts[0] == "data":
        parts = parts[1:]
    if parts and parts[0] == "docs":
        parts = parts[1:]
    return Path(*parts).as_posix() if parts else p.as_posix()


def _render_image_references(content: str, images: list[dict]) -> str:
    """把正文里的 ``[[img-N]]`` 占位符还原成 Markdown 图片引用。"""
    if not images:
        return IMAGE_PLACEHOLDER_PATTERN.sub("", content)
    by_marker = {
        str(item.get("marker")): item for item in images if item.get("marker")
    }

    def replace(match: "re.Match[str]") -> str:
        item = by_marker.get(f"img-{match.group(1)}")
        if item is None:
            return ""
        caption = str(item.get("caption") or "").strip() or "图片"
        url = str(item.get("url") or "").strip()
        if not url:
            return ""
        return f"![{caption}]({url})"

    return IMAGE_PLACEHOLDER_PATTERN.sub(replace, content)


def _code_snippets(doc: Document, source: str) -> list[dict]:
    """Return verbatim code artifacts carried by or extracted from a result."""
    supplied = doc.metadata.get("code_snippets")
    if isinstance(supplied, list):
        return [dict(item) for item in supplied if isinstance(item, dict) and item.get("content")]
    if not doc.metadata.get("contains_code") and "```" not in doc.page_content and "<pre" not in doc.page_content.lower():
        return []
    snippets = []
    for segment in iter_code_segments(doc.page_content, min_block_lines=1):
        if not segment.text.strip():
            continue
        snippets.append({
            "content": segment.text,
            "language": segment.language or "",
            "path": source,
            "start_line": segment.start_line,
            "end_line": segment.end_line,
            "line_scope": "chunk",
            "revision": "",
            "origin": "document",
            "is_verbatim": True,
        })
    return snippets


def build_context(
    docs: list[Document],
    *,
    max_chunks: int | None = None,
    max_tokens: int | None = None,
) -> tuple[str, list[dict]]:
    """构建上下文文本和来源列表。

    返回 (context_str, sources)。
    """
    if not docs:
        return "知识库中没有与该问题相关的信息。", []

    token_budget = int(
        (max_tokens or MAX_CONTEXT_TOKENS) * CONTEXT_TOKEN_SAFETY_RATIO
    )

    # IDs and hashes are only unique inside one knowledge base. Keep equal IDs
    # from different knowledge bases when performing a cross-base retrieval.
    seen_chunk_ids: set[tuple[str, str]] = set()
    seen_content_hashes: set[tuple[str, str]] = set()
    deduped: list[Document] = []

    for doc in docs:
        chunk_id = doc.metadata.get("chunk_id")
        content_hash = doc.metadata.get("content_hash")
        knowledge_base_id = str(doc.metadata.get("knowledge_base_id") or "")
        chunk_key = (knowledge_base_id, str(chunk_id))
        hash_key = (knowledge_base_id, str(content_hash))
        if chunk_id and chunk_key in seen_chunk_ids:
            continue
        if content_hash and hash_key in seen_content_hashes:
            continue
        if chunk_id:
            seen_chunk_ids.add(chunk_key)
        if content_hash:
            seen_content_hashes.add(hash_key)
        deduped.append(doc)

    # Per-source quota
    source_counts: dict[str, int] = {}
    quota_filtered: list[Document] = []
    for doc in deduped:
        source = _normalize_source(doc.metadata.get("source"))
        source_key = f"{doc.metadata.get('knowledge_base_id', '')}:{source}"
        if source_counts.get(source_key, 0) >= MAX_CHUNKS_PER_SOURCE:
            continue
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        quota_filtered.append(doc)

    # Limit total chunks
    quota_filtered = quota_filtered[:(max_chunks or MAX_CONTEXT_CHUNKS)]

    # Build context with citation numbers
    context_parts: list[str] = []
    sources: list[dict] = []
    used_tokens = 0

    for idx, doc in enumerate(quota_filtered):
        source = _normalize_source(doc.metadata.get("source"))
        page = doc.metadata.get("page")
        section = doc.metadata.get("section")
        content = doc.page_content

        citation_id = f"来源{idx + 1}"
        remaining = token_budget - used_tokens
        if remaining <= 10:
            break

        content = _truncate_at_boundary(content, remaining)
        images = decode_images(doc.metadata.get("images"))
        images = images[:IMAGE_CONTEXT_MAX_PER_CHUNK]
        code_snippets = _code_snippets(doc, source)
        if images or IMAGE_PLACEHOLDER_PATTERN.search(content):
            content = _render_image_references(content, images)
        content_tokens = count_tokens(content)
        used_tokens += content_tokens

        # Build context block
        lines = [f"[{citation_id}]"]
        if doc.metadata.get("knowledge_base_name"):
            lines.append(f"知识库：{doc.metadata['knowledge_base_name']}")
        lines.append(f"文件：{source}")
        if page is not None:
            lines.append(f"页码：{page}")
        if section:
            lines.append(f"章节：{section}")
        graph_facts = doc.metadata.get("graph_facts")
        if graph_facts:
            lines.append("图谱事实：" + "; ".join(graph_facts[:3]))
        lines.append(f"内容：{content}")
        context_parts.append("\n".join(lines))

        sources.append({
            "citation_id": citation_id,
            "document_id": doc.metadata.get("document_id", ""),
            "chunk_id": doc.metadata.get("chunk_id", ""),
            "source": source,
            "page": page,
            "section": section,
            "preview": doc.page_content[:200],
            "retrieval_channels": doc.metadata.get("retrieval_channels", []),
            "graph_facts": doc.metadata.get("graph_facts", []),
            "images": images,
            "contains_code": bool(doc.metadata.get("contains_code", False)),
            "code_snippets": code_snippets,
            "fusion_score": doc.metadata.get("fusion_score", 0.0),
            "relevance_score": doc.metadata.get("relevance_score"),
            "knowledge_base_id": doc.metadata.get("knowledge_base_id", ""),
            "knowledge_base_name": doc.metadata.get("knowledge_base_name", ""),
        })

    context = "\n\n".join(context_parts) if context_parts else "知识库中没有与该问题相关的信息。"
    return context, sources
