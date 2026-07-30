"""Rerank node. 必须使用原始问题重排，不能使用改写问题。"""

import logging

from rag.providers.interfaces import Reranker

logger = logging.getLogger(__name__)


def create_rerank_node(reranker: Reranker):
    def rerank_node(state):
        query = state.get("original_query") or state.get("query", "")
        docs = state.get("docs") or state.get("retrieval_candidates") or []

        if not docs:
            return {"docs": []}

        try:
            reranked = reranker.rerank(query, docs)
        except Exception as exc:
            logger.warning("Reranker failed, degrading by fusion score: %s", exc)
            reranked = sorted(
                docs,
                key=lambda d: d.metadata.get("fusion_score", 0.0),
                reverse=True,
            )
            for doc in reranked:
                doc.metadata["reranker_degraded"] = True

        return {"docs": reranked}

    return rerank_node


def rerank_node(state):
    from rag.providers.factory import get_reranker
    return create_rerank_node(get_reranker())(state)
