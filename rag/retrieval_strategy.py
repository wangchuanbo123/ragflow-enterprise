"""Central retrieval-depth policies shared by every retrieval channel."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import (
    BM25_SEARCH_K,
    CODE_SEARCH_K,
    GRAPH_ENTITY_SEARCH_K,
    GRAPH_MAX_HOPS,
    GRAPH_MAX_RELATIONS,
    GRAPH_RELATION_SEARCH_K,
    MAX_CONTEXT_CHUNKS,
    MAX_CONTEXT_TOKENS,
    RERANK_TOP_K,
    RETRIEVAL_CANDIDATE_K,
    VECTOR_SEARCH_K,
)


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    vector_k: int
    bm25_k: int
    candidate_k: int
    graph_entity_k: int
    graph_relation_k: int
    graph_max_hops: int
    graph_max_relations: int
    graph_top_k: int
    code_k: int
    rerank_k: int
    context_chunks: int
    context_tokens: int


def _at_least(value: int, minimum: int = 1) -> int:
    return max(minimum, int(value))


def get_retrieval_policy(name: str | None) -> RetrievalPolicy:
    strategy = (name or "standard").lower()
    if strategy == "fast":
        return RetrievalPolicy(
            name="fast",
            vector_k=_at_least(VECTOR_SEARCH_K // 2, 2),
            bm25_k=_at_least(BM25_SEARCH_K // 2, 2),
            candidate_k=_at_least(RETRIEVAL_CANDIDATE_K // 2, 4),
            graph_entity_k=_at_least(GRAPH_ENTITY_SEARCH_K // 2, 2),
            graph_relation_k=_at_least(GRAPH_RELATION_SEARCH_K // 2, 2),
            graph_max_hops=1,
            graph_max_relations=_at_least(GRAPH_MAX_RELATIONS // 2, 10),
            graph_top_k=_at_least(RETRIEVAL_CANDIDATE_K // 3, 3),
            code_k=_at_least(CODE_SEARCH_K // 2, 4),
            rerank_k=_at_least(RERANK_TOP_K - 2, 3),
            context_chunks=_at_least(MAX_CONTEXT_CHUNKS - 2, 3),
            context_tokens=_at_least(int(MAX_CONTEXT_TOKENS * 0.6), 800),
        )
    if strategy == "deep":
        return RetrievalPolicy(
            name="deep",
            vector_k=_at_least(VECTOR_SEARCH_K * 2),
            bm25_k=_at_least(BM25_SEARCH_K * 2),
            candidate_k=_at_least(RETRIEVAL_CANDIDATE_K * 2),
            graph_entity_k=_at_least(GRAPH_ENTITY_SEARCH_K * 2),
            graph_relation_k=_at_least(GRAPH_RELATION_SEARCH_K * 2),
            graph_max_hops=min(2, max(1, GRAPH_MAX_HOPS)),
            graph_max_relations=_at_least(GRAPH_MAX_RELATIONS * 2),
            graph_top_k=_at_least(RETRIEVAL_CANDIDATE_K),
            code_k=_at_least(CODE_SEARCH_K * 2),
            rerank_k=_at_least(RERANK_TOP_K * 2),
            context_chunks=_at_least(MAX_CONTEXT_CHUNKS + 3),
            context_tokens=_at_least(int(MAX_CONTEXT_TOKENS * 1.67)),
        )
    return RetrievalPolicy(
        name="standard",
        vector_k=VECTOR_SEARCH_K,
        bm25_k=BM25_SEARCH_K,
        candidate_k=RETRIEVAL_CANDIDATE_K,
        graph_entity_k=GRAPH_ENTITY_SEARCH_K,
        graph_relation_k=GRAPH_RELATION_SEARCH_K,
        graph_max_hops=GRAPH_MAX_HOPS,
        graph_max_relations=GRAPH_MAX_RELATIONS,
        graph_top_k=min(RETRIEVAL_CANDIDATE_K, 8),
        code_k=CODE_SEARCH_K,
        rerank_k=RERANK_TOP_K,
        context_chunks=MAX_CONTEXT_CHUNKS,
        context_tokens=MAX_CONTEXT_TOKENS,
    )

