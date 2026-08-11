"""基于 SQLite knowledge_chunks 的中文 BM25 检索器。

使用 jieba 进行中文分词，英文统一转小写后按词切分。
BM25 索引从 knowledge_chunks 表构建，而非运行时重新读取原始文档。
"""

from __future__ import annotations

import logging
import threading
from typing import Sequence

import jieba
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from app.core.database import SessionLocal
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)

_BM25_LOCK = threading.Lock()


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
        self._bm25: BM25Okapi | None = None
        self._built = False

    def build(self, db=None) -> None:
        """从 knowledge_chunks 构建索引。"""
        with _BM25_LOCK:
            own_session = False
            if db is None:
                db = SessionLocal()
                own_session = True
            try:
                repo = KnowledgeRepository(db, self.knowledge_base_id)
                chunks = repo.list_all_active_chunks()

                self._docs = []
                self._tokenized = []

                for chunk in chunks:
                    doc = Document(
                        page_content=chunk.content,
                        metadata={
                            "chunk_id": chunk.id,
                            "knowledge_base_id": self.knowledge_base_id,
                            "document_id": chunk.document_id,
                            "source": _get_source_path(
                                db,
                                chunk.document_id,
                                self.knowledge_base_id,
                            ),
                            "file_hash": chunk.file_hash,
                            "chunk_index": chunk.chunk_index,
                            "page": chunk.page,
                            "section": chunk.section,
                            "content_hash": chunk.content_hash,
                        },
                    )
                    self._docs.append(doc)
                    self._tokenized.append(_tokenize(chunk.content))

                if self._tokenized:
                    self._bm25 = BM25Okapi(self._tokenized)
                else:
                    self._bm25 = None

                self._built = True
                logger.info("BM25 index built: %d chunks", len(self._docs))
            except Exception as exc:
                logger.error("BM25 build failed: %s", exc)
                self._bm25 = None
                self._docs = []
                self._tokenized = []
                self._built = True
            finally:
                if own_session:
                    db.close()

    def invalidate(self) -> None:
        with _BM25_LOCK:
            self._built = False
            self._bm25 = None
            self._docs = []
            self._tokenized = []

    def ensure_built(self, db=None) -> None:
        if not self._built:
            self.build(db)

    def get_relevant_documents(
        self,
        query: str,
        top_k: int = 8,
        allowed_document_ids: set[str] | None = None,
    ) -> list[Document]:
        self.ensure_built()
        if self._bm25 is None or not self._docs:
            return []
        if allowed_document_ids is not None and not allowed_document_ids:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results: list[Document] = []
        for idx, score in ranked:
            if score <= 0:
                break
            doc = self._docs[idx]
            if (
                allowed_document_ids is not None
                and doc.metadata.get("document_id") not in allowed_document_ids
            ):
                continue
            doc.metadata["bm25_score"] = float(score)
            results.append(doc)
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


_bm25_by_knowledge_base: dict[str, ChunkBM25Retriever] = {}


def get_bm25_retriever(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> ChunkBM25Retriever:
    if knowledge_base_id not in _bm25_by_knowledge_base:
        _bm25_by_knowledge_base[knowledge_base_id] = ChunkBM25Retriever(
            knowledge_base_id
        )
    return _bm25_by_knowledge_base[knowledge_base_id]


def invalidate_bm25(knowledge_base_id: str | None = None) -> None:
    if knowledge_base_id is None:
        for retriever in _bm25_by_knowledge_base.values():
            retriever.invalidate()
        return
    get_bm25_retriever(knowledge_base_id).invalidate()
