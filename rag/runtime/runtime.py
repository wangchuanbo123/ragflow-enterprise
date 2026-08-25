"""Lazy runtime dependency container for the RAG workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import Any

from app.bootstrap.container import get_provider_container
from app.core.database import SessionLocal
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.providers.interfaces import (
    LLMProvider,
    Reranker,
    Retriever,
    VectorIndexRef,
)
from rag.retrievers.hybrid_retriever import create_hybrid_retriever

logger = logging.getLogger(__name__)

_runtime_lock = Lock()


@dataclass(frozen=True)
class RAGRuntime:
    llm: LLMProvider
    retriever: Retriever
    reranker: Reranker
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID
    index_signature: str | None = None
    rewrite_llm: LLMProvider | None = None
    router_llm: LLMProvider | None = None
    graph_retriever: Retriever | None = None
    code_retriever: Retriever | None = None
    embedding_model: Any | None = None


@lru_cache(maxsize=32)
def get_runtime(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> RAGRuntime:
    print("初始化 RAG 系统...")

    providers = get_provider_container()
    embedding = providers.embedding.get_model()

    # Determine active collection
    with SessionLocal() as db:
        repo = KnowledgeRepository(db, knowledge_base_id)
        state = repo.get_index_state()
        active_collection = state.active_collection_name
        index_signature = repo.get_index_signature()

    # The adapter owns connection/path details; runtime only knows a logical index.
    vector_db = providers.vector_store.open(
        embedding=embedding,
        index_ref=VectorIndexRef(
            name=active_collection,
            knowledge_base_id=knowledge_base_id,
        ),
    )

    # Text retrieval remains the baseline channel. Graph and code retrieval are
    # exposed separately so LangGraph can route and explain each channel.
    retriever = create_hybrid_retriever(
        vector_db,
        knowledge_base_id=knowledge_base_id,
    )
    graph_retriever = None
    from app.core.config import CODE_GRAPH_ENABLED, KNOWLEDGE_GRAPH_ENABLED
    if KNOWLEDGE_GRAPH_ENABLED:
        from rag.knowledge_graph.retriever import get_graph_retriever

        graph_retriever = get_graph_retriever(knowledge_base_id)
        print("知识图谱检索已启用（独立路由通道）")

    code_retriever = None
    if CODE_GRAPH_ENABLED:
        from rag.code_graph.retriever import get_code_graph_retriever

        code_retriever = get_code_graph_retriever(knowledge_base_id)
        if code_retriever is not None:
            print("代码图检索已启用（独立路由通道）")

    runtime = RAGRuntime(
        llm=providers.llm,
        retriever=retriever,
        reranker=providers.reranker,
        knowledge_base_id=knowledge_base_id,
        index_signature=index_signature,
        rewrite_llm=providers.rewrite_llm,
        router_llm=providers.router_llm,
        graph_retriever=graph_retriever,
        code_retriever=code_retriever,
        embedding_model=embedding,
    )

    # Build BM25 index from knowledge_chunks if available
    try:
        from rag.retrievers.bm25_retriever import get_bm25_retriever
        bm25 = get_bm25_retriever(knowledge_base_id)
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


def _get_runtime_for_knowledge_base(knowledge_base_id: str) -> RAGRuntime:
    """Keep the default-KB call shape compatible with existing integrations."""
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return get_runtime()
    return get_runtime(knowledge_base_id)


def get_current_runtime(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> RAGRuntime:
    """Refresh cached retrieval dependencies when another process changes the index."""
    runtime = _get_runtime_for_knowledge_base(knowledge_base_id)
    with SessionLocal() as db:
        current_signature = KnowledgeRepository(
            db,
            knowledge_base_id,
        ).get_index_signature()
    if runtime.index_signature == current_signature:
        return runtime

    with _runtime_lock:
        runtime = _get_runtime_for_knowledge_base(knowledge_base_id)
        with SessionLocal() as db:
            current_signature = KnowledgeRepository(
                db,
                knowledge_base_id,
            ).get_index_signature()
        if runtime.index_signature != current_signature:
            invalidate_runtime_cache()
            runtime = _get_runtime_for_knowledge_base(knowledge_base_id)
    return runtime


def get_active_collection_name(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> str | None:
    try:
        with SessionLocal() as db:
            state = KnowledgeRepository(db, knowledge_base_id).get_index_state()
            return state.active_collection_name
    except Exception:
        return None
