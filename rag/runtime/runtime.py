"""Lazy runtime dependency container for the RAG workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock

from app.core.config import VECTOR_DB_DIR
from app.core.database import SessionLocal
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.providers.factory import (
    get_embedding_provider,
    get_llm_provider,
    get_reranker,
)
from rag.providers.interfaces import LLMProvider, Reranker, Retriever
from rag.retrievers.hybrid_retriever import create_hybrid_retriever

logger = logging.getLogger(__name__)

_runtime_lock = Lock()


@dataclass(frozen=True)
class RAGRuntime:
    llm: LLMProvider
    retriever: Retriever
    reranker: Reranker
    index_signature: str | None = None


@lru_cache(maxsize=1)
def get_runtime() -> RAGRuntime:
    print("初始化 RAG 系统...")

    embedding = get_embedding_provider().get_model()

    # Determine active collection
    with SessionLocal() as db:
        repo = KnowledgeRepository(db)
        state = repo.get_index_state()
        active_collection = state.active_collection_name
        index_signature = repo.get_index_signature()

    # Load vector store with active collection (or default for baseline compat)
    from rag.providers.factory import get_vector_store_provider

    if active_collection:
        vector_db = get_vector_store_provider().load(
            embedding=embedding,
            persist_dir=str(VECTOR_DB_DIR),
            collection_name=active_collection,
        )
    else:
        # Baseline compatibility: load default collection
        vector_db = get_vector_store_provider().load(
            embedding=embedding,
            persist_dir=str(VECTOR_DB_DIR),
        )

    # Build retriever
    from app.core.config import KNOWLEDGE_GRAPH_ENABLED
    if KNOWLEDGE_GRAPH_ENABLED:
        from rag.retrievers.hybrid_graph_retriever import create_hybrid_retriever
        retriever = create_hybrid_retriever(vector_db)
        print("知识图谱检索已启用（三路融合）")
    else:
        retriever = create_hybrid_retriever(vector_db)

    runtime = RAGRuntime(
        llm=get_llm_provider(),
        retriever=retriever,
        reranker=get_reranker(),
        index_signature=index_signature,
    )

    # Build BM25 index from knowledge_chunks if available
    try:
        from rag.retrievers.bm25_retriever import get_bm25_retriever
        bm25 = get_bm25_retriever()
        bm25.build()
    except Exception as exc:
        logger.warning("BM25 index build skipped: %s", exc)

    print("RAG 初始化完成")
    return runtime


def invalidate_runtime_cache() -> None:
    """清除运行时缓存，下次调用 get_runtime() 时重新初始化。"""
    get_runtime.cache_clear()
    try:
        from rag.retrievers.bm25_retriever import invalidate_bm25

        invalidate_bm25()
    except Exception:
        logger.exception("Failed to invalidate BM25 cache")


def get_current_runtime() -> RAGRuntime:
    """Refresh cached retrieval dependencies when another process changes the index."""
    runtime = get_runtime()
    with SessionLocal() as db:
        current_signature = KnowledgeRepository(db).get_index_signature()
    if runtime.index_signature == current_signature:
        return runtime

    with _runtime_lock:
        runtime = get_runtime()
        with SessionLocal() as db:
            current_signature = KnowledgeRepository(db).get_index_signature()
        if runtime.index_signature != current_signature:
            invalidate_runtime_cache()
            runtime = get_runtime()
    return runtime


def get_active_collection_name() -> str | None:
    try:
        with SessionLocal() as db:
            state = KnowledgeRepository(db).get_index_state()
            return state.active_collection_name
    except Exception:
        return None
