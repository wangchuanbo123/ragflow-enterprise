"""上下文构建器：预算、来源配额、去重和稳定引用编号。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import tiktoken
from langchain_core.documents import Document

from app.core.config import (
    CONTEXT_TOKEN_SAFETY_RATIO,
    MAX_CHUNKS_PER_SOURCE,
    MAX_CONTEXT_CHUNKS,
    MAX_CONTEXT_TOKENS,
)

logger = logging.getLogger(__name__)

_ENCODER: tiktoken.Encoding | None = None


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        try:
            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 2)


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


def build_context(docs: list[Document]) -> tuple[str, list[dict]]:
    """构建上下文文本和来源列表。

    返回 (context_str, sources)。
    """
    if not docs:
        return "知识库中没有与该问题相关的信息。", []

    token_budget = int(MAX_CONTEXT_TOKENS * CONTEXT_TOKEN_SAFETY_RATIO)

    # Deduplicate by chunk_id and content_hash
    seen_chunk_ids: set[str] = set()
    seen_content_hashes: set[str] = set()
    deduped: list[Document] = []

    for doc in docs:
        chunk_id = doc.metadata.get("chunk_id")
        content_hash = doc.metadata.get("content_hash")
        if chunk_id and chunk_id in seen_chunk_ids:
            continue
        if content_hash and content_hash in seen_content_hashes:
            continue
        if chunk_id:
            seen_chunk_ids.add(chunk_id)
        if content_hash:
            seen_content_hashes.add(content_hash)
        deduped.append(doc)

    # Per-source quota
    source_counts: dict[str, int] = {}
    quota_filtered: list[Document] = []
    for doc in deduped:
        source = _normalize_source(doc.metadata.get("source"))
        if source_counts.get(source, 0) >= MAX_CHUNKS_PER_SOURCE:
            continue
        source_counts[source] = source_counts.get(source, 0) + 1
        quota_filtered.append(doc)

    # Limit total chunks
    quota_filtered = quota_filtered[:MAX_CONTEXT_CHUNKS]

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
        content_tokens = count_tokens(content)
        used_tokens += content_tokens

        # Build context block
        lines = [f"[{citation_id}]"]
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
            "fusion_score": doc.metadata.get("fusion_score", 0.0),
        })

    context = "\n\n".join(context_parts) if context_parts else "知识库中没有与该问题相关的信息。"
    return context, sources
