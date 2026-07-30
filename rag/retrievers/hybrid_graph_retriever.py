"""三路混合检索器：向量 + BM25 + 图谱，加权 RRF 融合。

当 KNOWLEDGE_GRAPH_ENABLED=true 时使用三路融合，
否则降级为向量 + BM25 两路融合（权重重新归一化）。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.documents import Document

from app.core.config import (
    BM25_RETRIEVAL_WEIGHT,
    BM25_SEARCH_K,
    GRAPH_RETRIEVAL_WEIGHT,
    HYBRID_VECTOR_WEIGHT,
    KNOWLEDGE_GRAPH_ENABLED,
    RETRIEVAL_CANDIDATE_K,
    RRF_K,
    VECTOR_RETRIEVAL_WEIGHT,
    VECTOR_SEARCH_K,
)
from rag.retrievers.bm25_retriever import get_bm25_retriever
from rag.retrievers.hybrid_retriever import weighted_rrf_fusion

logger = logging.getLogger(__name__)


class HybridGraphRetriever:
    """三路 RRF 混合检索器。"""

    def __init__(
        self,
        vector_store,
        vector_k: int = VECTOR_SEARCH_K,
        bm25_k: int = BM25_SEARCH_K,
        vector_weight: float | None = None,
        bm25_weight: float | None = None,
        graph_weight: float | None = None,
        rrf_k: int = RRF_K,
        candidate_k: int = RETRIEVAL_CANDIDATE_K,
    ):
        self.vector_store = vector_store
        self.vector_k = vector_k
        self.bm25_k = bm25_k

        # Determine weights
        if KNOWLEDGE_GRAPH_ENABLED:
            self.vector_weight = vector_weight if vector_weight is not None else VECTOR_RETRIEVAL_WEIGHT
            self.bm25_weight = bm25_weight if bm25_weight is not None else BM25_RETRIEVAL_WEIGHT
            self.graph_weight = graph_weight if graph_weight is not None else GRAPH_RETRIEVAL_WEIGHT
            self.graph_enabled = True
        else:
            # Two-way: renormalize vector + bm25
            total = VECTOR_RETRIEVAL_WEIGHT + BM25_RETRIEVAL_WEIGHT
            if total > 0:
                self.vector_weight = VECTOR_RETRIEVAL_WEIGHT / total
                self.bm25_weight = BM25_RETRIEVAL_WEIGHT / total
            else:
                self.vector_weight = HYBRID_VECTOR_WEIGHT
                self.bm25_weight = 1.0 - HYBRID_VECTOR_WEIGHT
            self.graph_weight = 0.0
            self.graph_enabled = False

        self.rrf_k = rrf_k
        self.candidate_k = candidate_k

    def get_relevant_documents(self, query: str) -> list[Document]:
        return self.retrieve_single(query)

    def retrieve_single(self, query: str) -> list[Document]:
        ranked_lists: list[tuple[list[Document], float]] = []

        # Vector retrieval
        vector_docs = self._vector_search(query)
        for doc in vector_docs:
            doc.metadata["_channel"] = "vector"
        ranked_lists.append((vector_docs, self.vector_weight))

        # BM25 retrieval
        bm25_docs = self._bm25_search(query)
        for doc in bm25_docs:
            doc.metadata["_channel"] = "bm25"
        ranked_lists.append((bm25_docs, self.bm25_weight))

        # Graph retrieval
        if self.graph_enabled:
            graph_docs = self._graph_search(query)
            for doc in graph_docs:
                doc.metadata["_channel"] = "graph"
            ranked_lists.append((graph_docs, self.graph_weight))

        return weighted_rrf_fusion(ranked_lists, rrf_k=self.rrf_k, candidate_k=self.candidate_k)

    def retrieve_multi_query(self, queries: list[str]) -> list[Document]:
        all_ranked: list[tuple[list[Document], float]] = []

        for q in queries:
            vector_docs = self._vector_search(q)
            for doc in vector_docs:
                doc.metadata["_channel"] = "vector"
            all_ranked.append((vector_docs, self.vector_weight))

            bm25_docs = self._bm25_search(q)
            for doc in bm25_docs:
                doc.metadata["_channel"] = "bm25"
            all_ranked.append((bm25_docs, self.bm25_weight))

            if self.graph_enabled:
                graph_docs = self._graph_search(q)
                for doc in graph_docs:
                    doc.metadata["_channel"] = "graph"
                all_ranked.append((graph_docs, self.graph_weight))

        return weighted_rrf_fusion(all_ranked, rrf_k=self.rrf_k, candidate_k=self.candidate_k)

    def _vector_search(self, query: str) -> list[Document]:
        try:
            try:
                pairs = self.vector_store.similarity_search_with_relevance_scores(query, k=self.vector_k)
                return [doc for doc, _ in pairs]
            except Exception:
                return self.vector_store.similarity_search(query, k=self.vector_k)
        except Exception as exc:
            logger.warning("Vector retrieval failed: %s", exc)
            return []

    def _bm25_search(self, query: str) -> list[Document]:
        try:
            return get_bm25_retriever().get_relevant_documents(query, top_k=self.bm25_k)
        except Exception as exc:
            logger.warning("BM25 retrieval failed: %s", exc)
            return []

    def _graph_search(self, query: str) -> list[Document]:
        try:
            from rag.knowledge_graph.retriever import get_graph_retriever
            return get_graph_retriever().get_relevant_documents(query, top_k=self.vector_k)
        except Exception as exc:
            logger.warning("Graph retrieval failed: %s", exc)
            return []


def create_hybrid_retriever(
    vector_store,
    docs=None,
    vector_k: int = VECTOR_SEARCH_K,
    bm25_k: int = BM25_SEARCH_K,
    vector_weight: float = HYBRID_VECTOR_WEIGHT,
):
    """工厂函数。"""
    return HybridGraphRetriever(
        vector_store=vector_store,
        vector_k=vector_k,
        bm25_k=bm25_k,
    )
