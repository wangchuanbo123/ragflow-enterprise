"""统一结构化切片器。

默认使用 recursive 策略（结构化递归切分）。
semantic 策略可选，但默认不启用以避免 Embedding 构建速度过慢。

每个片段生成确定性 chunk_id：
    sha256(document_id + file_hash + index_schema_version + chunk_index)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Sequence

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

from app.core.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CHUNK_STRATEGY,
    CODE_AWARE_MIN_BLOCK_LINES,
    INDEX_SCHEMA_VERSION,
    MIN_CHUNK_SIZE,
)
from rag.indexing.code_aware_splitter import split_documents_code_aware
from rag.indexing.tokens import count_tokens
from rag.loaders.image_extractor import IMAGE_PLACEHOLDER_PATTERN

__all__ = [
    "ChunkResult",
    "content_hash",
    "count_tokens",
    "deterministic_chunk_id",
    "split_documents",
]


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


def _scope_images_to_chunks(chunks: list[Document]) -> None:
    """只保留真正落在该片段内的图片。

    文档级 metadata 会被切分器复制到每个片段；这里依据 ``[[img-N]]`` 占位符
    是否在片段正文中出现，把图片列表收敛到该片段自己的那几张。
    """
    for chunk in chunks:
        images = chunk.metadata.get("images")
        if not images:
            continue
        wanted = {
            f"img-{number}"
            for number in IMAGE_PLACEHOLDER_PATTERN.findall(chunk.page_content)
        }
        kept = [item for item in images if item.get("marker") in wanted]
        if kept:
            chunk.metadata["images"] = kept
        else:
            chunk.metadata.pop("images", None)


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
    elif strategy == "code_aware":
        chunks = split_documents_code_aware(
            docs,
            chunk_size,
            chunk_overlap,
            min_chunk_size,
            min_block_lines=CODE_AWARE_MIN_BLOCK_LINES,
        )
    else:
        chunks = _split_recursive(docs, chunk_size, chunk_overlap, min_chunk_size)

    _scope_images_to_chunks(chunks)

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
