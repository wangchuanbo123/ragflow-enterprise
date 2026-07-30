"""统一结构化切片器。

默认使用 recursive 策略（结构化递归切分）。
semantic 策略可选，但默认不启用以避免 Embedding 构建速度过慢。

每个片段生成确定性 chunk_id：
    sha256(document_id + file_hash + index_schema_version + chunk_index)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Sequence

import tiktoken
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

from app.core.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CHUNK_STRATEGY,
    INDEX_SCHEMA_VERSION,
    MIN_CHUNK_SIZE,
)

_ENCODER: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
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


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def deterministic_chunk_id(
    document_id: str,
    file_hash: str,
    chunk_index: int,
    schema_version: int = INDEX_SCHEMA_VERSION,
) -> str:
    raw = f"{document_id}:{file_hash}:{schema_version}:{chunk_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


@dataclass
class ChunkResult:
    chunks: list[Document] = field(default_factory=list)
    token_counts: list[int] = field(default_factory=list)
    content_hashes: list[str] = field(default_factory=list)


def split_documents(
    docs: Sequence[Document],
    strategy: str = CHUNK_STRATEGY,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    min_chunk_size: int = MIN_CHUNK_SIZE,
    document_id: str = "",
    file_hash_val: str = "",
    schema_version: int = INDEX_SCHEMA_VERSION,
) -> ChunkResult:
    """统一入口：把加载后的 Document 列表切分为最终片段。

    返回 ChunkResult，包含 chunks、token_counts、content_hashes。
    """
    if not docs:
        return ChunkResult()

    if strategy == "semantic":
        chunks = _split_semantic(docs, chunk_size, chunk_overlap)
    else:
        chunks = _split_recursive(docs, chunk_size, chunk_overlap, min_chunk_size)

    # Deduplicate exact duplicates within same batch by content_hash
    seen_hashes: set[str] = set()
    final_chunks: list[Document] = []
    final_tokens: list[int] = []
    final_hashes: list[str] = []

    for idx, chunk in enumerate(chunks):
        ch = content_hash(chunk.page_content)
        if ch in seen_hashes:
            continue
        seen_hashes.add(ch)

        chunk.metadata["chunk_index"] = len(final_chunks)
        chunk.metadata["content_hash"] = ch
        chunk.metadata["chunk_id"] = deterministic_chunk_id(
            document_id, file_hash_val, len(final_chunks), schema_version
        )
        if document_id:
            chunk.metadata["document_id"] = document_id
        chunk.metadata["index_schema_version"] = schema_version

        tc = count_tokens(chunk.page_content)
        chunk.metadata["token_count"] = tc

        final_chunks.append(chunk)
        final_tokens.append(tc)
        final_hashes.append(ch)

    return ChunkResult(chunks=final_chunks, token_counts=final_tokens, content_hashes=final_hashes)


_SEPARATORS = [
    "\n\n\n",
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    ". ",
    "! ",
    "? ",
    " ",
    "",
]


def _split_recursive(
    docs: Sequence[Document],
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_size: int,
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
    )
    chunks = splitter.split_documents(list(docs))

    # Merge tiny tail chunks into the previous chunk
    if min_chunk_size > 0 and len(chunks) > 1:
        merged: list[Document] = []
        for chunk in chunks:
            if merged and count_tokens(chunk.page_content) < min_chunk_size:
                prev = merged[-1]
                prev.page_content = prev.page_content + "\n" + chunk.page_content
            else:
                merged.append(chunk)
        chunks = merged

    return chunks


def _split_semantic(
    docs: Sequence[Document],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    try:
        from langchain_experimental.text_splitter import SemanticChunker
        from rag.providers.factory import get_embedding_provider

        embedding = get_embedding_provider().get_model()
        coarse_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            chunk_size=chunk_size * 2,
            chunk_overlap=chunk_overlap,
        )
        coarse_docs = coarse_splitter.split_documents(list(docs))
        semantic = SemanticChunker(embedding)
        result = semantic.split_documents(coarse_docs)
    except Exception:
        result = _split_recursive(docs, chunk_size, chunk_overlap, 0)

    final_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return final_splitter.split_documents(result)
