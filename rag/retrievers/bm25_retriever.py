"""基于 SQLite knowledge_chunks 的中文 BM25 检索器。

使用 jieba 进行中文分词，英文统一转小写后按词切分。
BM25 索引从 knowledge_chunks 表构建，而非运行时重新读取原始文档。
"""

from __future__ import annotations

import logging
import heapq
import threading
import time
from pathlib import Path
from collections import OrderedDict
from typing import Sequence

import jieba
from langchain_core.documents import Document
from rank_bm25 import BM25L

from app.core.database import SessionLocal
from app.core.config import BM25_MAX_CACHE_KNOWLEDGE_BASES, BM25_MAX_CHUNKS_PER_KNOWLEDGE_BASE
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.errors import RetrievalError

logger = logging.getLogger(__name__)


def _build_failure(message, code):
    error = RetrievalError(message)
    error.code = code
    return error

def _tokenize(text: str) -> list[str]:
    """中文用 jieba 分词，英文转小写按空格切分。"""
    tokens: list[str] = []
    for word in jieba.cut_for_search(text):
        word = word.strip().lower()
        if word and len(word) > 0:
            tokens.append(word)
    return tokens


class ChunkBM25Retriever:
    """从 knowledge_chunks 表构建的 BM25 检索器。

    - 应用启动时构建一次
    - 索引切换后主动失效并重建
    - 没有活动 Chunk 时返回空结果
    """

    def __init__(
        self,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.knowledge_base_id = knowledge_base_id
        self._docs: list[Document] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25L | None = None
        self._built = False
        self._lock = threading.RLock()
        self._retry_after = 0.0
        self._build_error = None
        self._build_error_code = None
        self.build_stats = {"chunks": 0, "reused_tokens": 0, "tokenized_chunks": 0}

    def build(self, db=None) -> None:
        """从 knowledge_chunks 构建索引。"""
        with self._lock:
            if time.monotonic() < self._retry_after:
                raise _build_failure(self._build_error or "BM25 rebuild is backing off", self._build_error_code or "bm25_build_backoff")
            own_session = False
            if db is None:
                db = SessionLocal()
                own_session = True
            try:
                repo = KnowledgeRepository(db, self.knowledge_base_id)
                if repo.count_chunks() > BM25_MAX_CHUNKS_PER_KNOWLEDGE_BASE:
                    raise _build_failure("知识库超过当前关键词索引容量，请调整容量配置或接入共享搜索服务", "bm25_capacity_exceeded")
                chunks = repo.list_all_active_chunks()
                source_paths = {
                    document.id: document.source_path
                    for document in repo.list_ready_documents()
                }
                documents = []
                tokenized = []
                from app.core import config
                from rag.retrievers.token_cache import TokenCache, content_signature
                directory = getattr(config, "BM25_TOKEN_CACHE_DIR", None)
                if directory is None and config.APP_PROFILE != "test":
                    directory = str(Path(config.DATA_DIR) / "search-cache")
                if not getattr(config, "BM25_TOKEN_CACHE_ENABLED", config.APP_PROFILE != "test"):
                    directory = None
                bind = db.get_bind()
                engine = getattr(bind, "engine", bind)
                namespace = str(engine.url.render_as_string(hide_password=True))
                if engine.dialect.name == "postgresql":
                    from sqlalchemy import text
                    namespace += ":" + str(db.scalar(text("SELECT current_schema()")))
                cache = TokenCache(directory, namespace, self.knowledge_base_id) if directory else None
                prior = cache.read() if cache else {}
                updated, reused = {}, 0

                for chunk in chunks:
                    metadata = {
                        "chunk_id": chunk.id,
                        "knowledge_base_id": self.knowledge_base_id,
                        "document_id": chunk.document_id,
                        "source": source_paths.get(chunk.document_id, "unknown"),
                        "file_hash": chunk.file_hash,
                        "chunk_index": chunk.chunk_index,
                        "page": chunk.page,
                        "section": chunk.section,
                        "content_hash": chunk.content_hash,
                    }
                    # 图片引用、contains_code 等富元数据存在 metadata_json 里；
                    # BM25 通道也要带出来，context_builder 才能还原图片。
                    for key, value in (chunk.metadata_json or {}).items():
                        if key not in metadata:
                            metadata[key] = value
                    doc = Document(
                        page_content=chunk.content,
                        metadata=metadata,
                    )
                    documents.append(doc)
                    signature = content_signature(chunk.content)
                    previous = prior.get(chunk.id, {})
                    if previous.get("sha256") == signature:
                        tokens = previous["tokens"]
                        reused += 1
                    else:
                        tokens = _tokenize(chunk.content)
                    tokenized.append(tokens)
                    updated[chunk.id] = {"sha256": signature, "tokens": tokens}

                # BM25L uses positive IDF even for terms present in every
                # chunk, so a one-document corpus still has lexical recall.
                self._bm25 = BM25L(tokenized) if any(tokenized) else None
                self._docs = documents
                self._tokenized = []

                self._built = True
                self._retry_after = 0.0
                self._build_error = None
                self._build_error_code = None
                self.build_stats = {"chunks": len(documents), "reused_tokens": reused,
                                    "tokenized_chunks": len(documents) - reused}
                if cache:
                    cache.write(updated)
                logger.info("BM25 index built: %d chunks", len(self._docs))
            except Exception as exc:
                logger.error("BM25 build failed: %s", exc)
                self._bm25 = None
                self._docs = []
                self._tokenized = []
                self._built = False
                self._retry_after = time.monotonic() + 60
                self._build_error = "BM25 capacity exceeded" if isinstance(exc, RetrievalError) else f"BM25 build failed ({type(exc).__name__})"
                self._build_error_code = exc.code if isinstance(exc, RetrievalError) else "bm25_build_failed"
                raise _build_failure(self._build_error, self._build_error_code) from exc
            finally:
                if own_session:
                    db.close()

    def invalidate(self) -> None:
        with self._lock:
            self._built = False
            self._bm25 = None
            self._docs = []
            self._tokenized = []
            self._retry_after = 0.0
            self._build_error = None
            self._build_error_code = None

    def ensure_built(self, db=None) -> None:
        with self._lock:
            if not self._built:
                self.build(db)

    def get_relevant_documents(
        self,
        query: str,
        top_k: int = 8,
        allowed_document_ids: set[str] | None = None,
    ) -> list[Document]:
        if top_k <= 0:
            return []
        self.ensure_built()
        with self._lock:
            bm25, documents = self._bm25, self._docs
        if bm25 is None or not documents:
            return []
        if allowed_document_ids is not None and not allowed_document_ids:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        if allowed_document_ids is None:
            candidates = range(len(documents))
            scores = bm25.get_scores(tokens)
        else:
            candidates = [idx for idx, doc in enumerate(documents)
                          if doc.metadata.get("document_id") in allowed_document_ids]
            if not candidates:
                return []
            scores = bm25.get_batch_scores(tokens, candidates)
        ranked = heapq.nlargest(top_k, zip(candidates, scores), key=lambda item: item[1])

        results: list[Document] = []
        for idx, score in ranked:
            if score <= 0:
                break
            doc = documents[idx]
            if (
                allowed_document_ids is not None
                and doc.metadata.get("document_id") not in allowed_document_ids
            ):
                continue
            results.append(Document(
                page_content=doc.page_content,
                metadata={**doc.metadata, "bm25_score": float(score)},
            ))
            if len(results) >= top_k:
                break

        return results

    @property
    def chunk_count(self) -> int:
        return len(self._docs)


def _get_source_path(db, document_id: str, knowledge_base_id: str) -> str:
    from app.models.knowledge_document import KnowledgeDocument
    from sqlalchemy import select

    doc = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
        )
    )
    return doc.source_path if doc else "unknown"


_bm25_by_knowledge_base: OrderedDict[str, ChunkBM25Retriever] = OrderedDict()
_registry_lock = threading.RLock()


def get_bm25_retriever(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> ChunkBM25Retriever:
    evicted_items = []
    with _registry_lock:
        if knowledge_base_id not in _bm25_by_knowledge_base:
            _bm25_by_knowledge_base[knowledge_base_id] = ChunkBM25Retriever(knowledge_base_id)
        _bm25_by_knowledge_base.move_to_end(knowledge_base_id)
        while len(_bm25_by_knowledge_base) > BM25_MAX_CACHE_KNOWLEDGE_BASES:
            _, evicted = _bm25_by_knowledge_base.popitem(last=False)
            evicted_items.append(evicted)
        retriever = _bm25_by_knowledge_base[knowledge_base_id]
    for evicted in evicted_items:
        evicted.invalidate()
    return retriever


def invalidate_bm25(knowledge_base_id: str | None = None) -> None:
    if knowledge_base_id is None:
        with _registry_lock:
            retrievers = list(_bm25_by_knowledge_base.values())
        for retriever in retrievers:
            retriever.invalidate()
        return
    get_bm25_retriever(knowledge_base_id).invalidate()
