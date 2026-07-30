"""
混合检索器：向量检索 + BM25 检索 + 加权 RRF 融合。

按 chunk_id 使用 Weighted Reciprocal Rank Fusion：
    rrf_score(chunk) = sum( channel_weight / (RRF_K + rank) )
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.documents import Document

from app.core.config import (
    BM25_SEARCH_K,
    HYBRID_VECTOR_WEIGHT,
    RETRIEVAL_CANDIDATE_K,
    RRF_K,
    VECTOR_SEARCH_K,
)
from rag.retrievers.bm25_retriever import get_bm25_retriever

logger = logging.getLogger(__name__)


def _merge_ranked_metadata(target: dict, incoming: dict) -> None:
    """Merge channel-specific metadata for the same Chunk."""
    for key in ("graph_facts", "matched_queries", "retrieval_channels"):
        merged = list(target.get(key) or [])
        for value in incoming.get(key) or []:
            if value not in merged:
                merged.append(value)
        if merged:
            target[key] = merged

    for key in ("graph_score",):
        if key in incoming:
            target[key] = max(
                float(target.get(key, 0.0) or 0.0),
                float(incoming.get(key, 0.0) or 0.0),
            )

    for key, value in incoming.items():
        if key not in target or target[key] in (None, "", [], {}):
            target[key] = value


def weighted_rrf_fusion(
    ranked_lists: list[tuple[list[Document], float]],
    rrf_k: int = RRF_K,
    candidate_k: int = RETRIEVAL_CANDIDATE_K,
) -> list[Document]:
    """对多个排序好的列表做加权 RRF 融合，按 chunk_id 去重。

    Args:
        ranked_lists: [(docs_sorted_by_rank, channel_weight), ...]
        rrf_k: RRF 常数
        candidate_k: 最终返回候选数量上限
    """
    scores: dict[str, float] = {}
    docs_by_id: dict[str, Document] = {}
    matched_channels: dict[str, list[str]] = {}

    for docs, weight in ranked_lists:
        for rank, doc in enumerate(docs):
            chunk_id = doc.metadata.get("chunk_id") or id(doc)
            if chunk_id not in docs_by_id:
                docs_by_id[chunk_id] = Document(
                    page_content=doc.page_content,
                    metadata=dict(doc.metadata),
                )
                matched_channels[chunk_id] = []
            else:
                _merge_ranked_metadata(
                    docs_by_id[chunk_id].metadata,
                    doc.metadata,
                )
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (rrf_k + rank + 1)
            channel = doc.metadata.get("_channel", "unknown")
            if channel not in matched_channels[chunk_id]:
                matched_channels[chunk_id].append(channel)

    ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)

    results: list[Document] = []
    for chunk_id in ranked_ids[:candidate_k]:
        doc = docs_by_id[chunk_id]
        meta = dict(doc.metadata)
        meta["fusion_score"] = scores[chunk_id]
        meta["matched_queries"] = meta.get("matched_queries", [])
        existing_channels = list(meta.get("retrieval_channels") or [])
        for channel in matched_channels.get(chunk_id, []):
            if channel not in existing_channels:
                existing_channels.append(channel)
        meta["retrieval_channels"] = existing_channels
        doc.metadata = meta
        results.append(doc)

    return results


class HybridRRFRetriever:
    """向量 + BM25 + RRF 融合检索器。

    替代旧 EnsembleRetriever，支持显式 RRF 融合和稳定 chunk_id。
    """

    def __init__(
        self,
        vector_store,
        vector_k: int = VECTOR_SEARCH_K,
        bm25_k: int = BM25_SEARCH_K,
        vector_weight: float = HYBRID_VECTOR_WEIGHT,
        rrf_k: int = RRF_K,
        candidate_k: int = RETRIEVAL_CANDIDATE_K,
    ):
        self.vector_store = vector_store
        self.vector_k = vector_k
        self.bm25_k = bm25_k
        self.vector_weight = vector_weight
        self.bm25_weight = 1.0 - vector_weight
        self.rrf_k = rrf_k
        self.candidate_k = candidate_k

    def get_relevant_documents(self, query: str) -> list[Document]:
        return self.retrieve_single(query)

    def retrieve_single(self, query: str) -> list[Document]:
        """对单个 query 执行向量+BM25 双通道检索和 RRF 融合。"""
        # Vector retrieval
        vector_docs: list[Document] = []
        try:
            vector_docs = self.vector_store.similarity_search_with_relevance_scores(
                query, k=self.vector_k
            )
            vector_docs = [doc for doc, _ in vector_docs]
        except Exception:
            try:
                vector_docs = self.vector_store.similarity_search(query, k=self.vector_k)
            except Exception as exc:
                logger.warning("Vector retrieval failed: %s", exc)

        for doc in vector_docs:
            doc.metadata["_channel"] = "vector"

        # BM25 retrieval
        bm25_docs: list[Document] = []
        try:
            bm25_retriever = get_bm25_retriever()
            bm25_docs = bm25_retriever.get_relevant_documents(query, top_k=self.bm25_k)
        except Exception as exc:
            logger.warning("BM25 retrieval failed: %s", exc)

        for doc in bm25_docs:
            doc.metadata["_channel"] = "bm25"

        # RRF fusion
        return weighted_rrf_fusion(
            [
                (vector_docs, self.vector_weight),
                (bm25_docs, self.bm25_weight),
            ],
            rrf_k=self.rrf_k,
            candidate_k=self.candidate_k,
        )

    def retrieve_multi_query(self, queries: list[str]) -> list[Document]:
        """对多个 query 分别执行双通道检索，然后按 chunk_id 累加 RRF。"""
        all_ranked: list[tuple[list[Document], float]] = []

        for q in queries:
            # Vector
            vector_docs: list[Document] = []
            try:
                vector_docs = self.vector_store.similarity_search(q, k=self.vector_k)
            except Exception as exc:
                logger.warning("Vector retrieval failed for query '%s': %s", q[:50], exc)
            for doc in vector_docs:
                doc.metadata["_channel"] = "vector"
            all_ranked.append((vector_docs, self.vector_weight))

            # BM25
            bm25_docs: list[Document] = []
            try:
                bm25_docs = get_bm25_retriever().get_relevant_documents(q, top_k=self.bm25_k)
            except Exception as exc:
                logger.warning("BM25 retrieval failed for query '%s': %s", q[:50], exc)
            for doc in bm25_docs:
                doc.metadata["_channel"] = "bm25"
            all_ranked.append((bm25_docs, self.bm25_weight))

        return weighted_rrf_fusion(
            all_ranked,
            rrf_k=self.rrf_k,
            candidate_k=self.candidate_k,
        )


def create_hybrid_retriever(
    vector_store,
    docs=None,
    vector_k: int = VECTOR_SEARCH_K,
    bm25_k: int = BM25_SEARCH_K,
    vector_weight: float = HYBRID_VECTOR_WEIGHT,
):
    """工厂函数：创建 RRF 混合检索器。

    docs 参数保留兼容但不再使用（BM25 从 knowledge_chunks 读取）。
    """
    return HybridRRFRetriever(
        vector_store=vector_store,
        vector_k=vector_k,
        bm25_k=bm25_k,
        vector_weight=vector_weight,
    )
