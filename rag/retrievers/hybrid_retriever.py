"""
混合检索器：向量检索 + BM25 检索 + 加权 RRF 融合。

按 chunk_id 使用 Weighted Reciprocal Rank Fusion：
    rrf_score(chunk) = sum( channel_weight / (RRF_K + rank) )
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from langchain_core.documents import Document

from app.core.config import (
    BM25_SEARCH_K,
    HYBRID_VECTOR_WEIGHT,
    RETRIEVAL_CANDIDATE_K,
    RRF_K,
    VECTOR_SEARCH_K,
)
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.retrievers.bm25_retriever import get_bm25_retriever
from rag.retrieval_strategy import get_retrieval_policy
from rag.retrievers.diagnostics import RetrievalDocuments, diagnostic

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
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.vector_store = vector_store
        self.vector_k = vector_k
        self.bm25_k = bm25_k
        self.vector_weight = vector_weight
        self.bm25_weight = 1.0 - vector_weight
        self.rrf_k = rrf_k
        self.candidate_k = candidate_k
        self.knowledge_base_id = knowledge_base_id

    def get_relevant_documents(self, query: str) -> list[Document]:
        return self.retrieve_single(query)

    def retrieve_single(
        self, query: str, allowed_document_ids: set[str] | None = None,
        query_embedding: list[float] | None = None, strategy: str = "standard",
    ) -> list[Document]:
        return self.retrieve_multi_query([query], allowed_document_ids=allowed_document_ids,
            query_embedding=query_embedding, strategy=strategy)

    def retrieve_multi_query(
        self, queries: list[str], allowed_document_ids: set[str] | None = None,
        query_embedding: list[float] | None = None, strategy: str = "standard",
    ) -> list[Document]:
        if not queries or (allowed_document_ids is not None and not allowed_document_ids):
            return RetrievalDocuments()
        vector_filter = {"document_id": {"$in": sorted(allowed_document_ids)}} if allowed_document_ids is not None else None
        kwargs = {"filter": vector_filter} if vector_filter else {}
        policy = get_retrieval_policy(strategy)
        ranked, diagnostics = [], []
        vector_docs = []
        started = perf_counter()
        vector_status, vector_code = "ok", "retrieved"
        try:
            if query_embedding is not None and not len(query_embedding):
                raise ValueError("shared_embedding_unavailable")
            method = ("similarity_search_by_vector_with_relevance_scores" if query_embedding is not None
                      else "similarity_search_with_relevance_scores")
            scored = getattr(self.vector_store, method)(query_embedding if query_embedding is not None else queries[-1],
                                                       k=policy.vector_k, **kwargs)
            for doc, score in scored:
                doc.metadata["vector_score"] = float(score)
                vector_docs.append(doc)
        except (AttributeError, NotImplementedError):
            try:
                method = "similarity_search_by_vector" if query_embedding is not None else "similarity_search"
                vector_docs = getattr(self.vector_store, method)(query_embedding if query_embedding is not None else queries[-1],
                                                                k=policy.vector_k, **kwargs)
                vector_status, vector_code = "degraded", "scores_unavailable"
            except Exception as exc:
                vector_status, vector_code = "error", type(exc).__name__
        except Exception as exc:
            vector_status, vector_code = ("skipped", "shared_embedding_unavailable") if query_embedding is not None and not len(query_embedding) else ("error", type(exc).__name__)
        vector_docs = [doc for doc in vector_docs if allowed_document_ids is None or doc.metadata.get("document_id") in allowed_document_ids]
        for doc in vector_docs:
            doc.metadata["_channel"] = "vector"
        diagnostics.append(diagnostic("vector", vector_status, vector_code, len(vector_docs), (perf_counter()-started)*1000))
        ranked.append((vector_docs, self.vector_weight))
        # A single semantic vector is shared; lexical recall uses every distinct query.
        for query in dict.fromkeys(queries):
            started, bm25_docs = perf_counter(), []
            status, code = "ok", "retrieved"
            try:
                bm25_docs = get_bm25_retriever(self.knowledge_base_id).get_relevant_documents(
                    query, top_k=policy.bm25_k, allowed_document_ids=allowed_document_ids)
            except Exception as exc:
                status = "error"
                code = getattr(exc, "code", type(exc).__name__)
            for doc in bm25_docs:
                doc.metadata["_channel"] = "bm25"
            diagnostics.append(diagnostic("bm25", status, code, len(bm25_docs), (perf_counter()-started)*1000))
            ranked.append((bm25_docs, self.bm25_weight))
        documents = weighted_rrf_fusion(ranked, rrf_k=self.rrf_k, candidate_k=policy.candidate_k)
        for doc in documents:
            doc.metadata["retrieval_diagnostics"] = diagnostics
        return RetrievalDocuments(documents, diagnostics=diagnostics)

    def retrieve_with_options(
        self,
        queries: list[str],
        *,
        query_embedding: list[float] | None = None,
        strategy: str = "standard",
        allowed_document_ids: set[str] | None = None,
    ) -> list[Document]:
        if len(queries) > 1:
            return self.retrieve_multi_query(
                queries,
                allowed_document_ids=allowed_document_ids,
                query_embedding=query_embedding,
                strategy=strategy,
            )
        return self.retrieve_single(
            queries[0],
            allowed_document_ids=allowed_document_ids,
            query_embedding=query_embedding,
            strategy=strategy,
        )


def create_hybrid_retriever(
    vector_store,
    docs=None,
    vector_k: int = VECTOR_SEARCH_K,
    bm25_k: int = BM25_SEARCH_K,
    vector_weight: float = HYBRID_VECTOR_WEIGHT,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
):
    """工厂函数：创建 RRF 混合检索器。

    docs 参数保留兼容但不再使用（BM25 从 knowledge_chunks 读取）。
    """
    return HybridRRFRetriever(
        vector_store=vector_store,
        vector_k=vector_k,
        bm25_k=bm25_k,
        vector_weight=vector_weight,
        knowledge_base_id=knowledge_base_id,
    )
