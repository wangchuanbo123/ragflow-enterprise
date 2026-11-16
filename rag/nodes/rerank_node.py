"""Rerank node. 必须使用原始问题重排，不能使用改写问题。"""

import logging
import math
from time import perf_counter

from rag.providers.interfaces import Reranker
from rag.retrieval_strategy import get_retrieval_policy

logger = logging.getLogger(__name__)


def _attach_relevance_scores(docs):
    """Expose an interpretable score without treating RRF rank scores as probabilities."""
    for doc in docs:
        raw_score = doc.metadata.get("rerank_score")
        if raw_score is None:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        # Most HTTP rerank APIs return a normalized score. Local cross encoders
        # may return logits, which need a sigmoid before they can be shown as %.
        if not 0.0 <= score <= 1.0:
            score = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, score))))
        doc.metadata["relevance_score"] = round(score, 6)
    return docs


def create_rerank_node(reranker: Reranker):
    def rerank_node(state):
        query = state.get("original_query") or state.get("query", "")
        docs = state.get("docs") or state.get("retrieval_candidates") or []
        policy = get_retrieval_policy(state.get("retrieval_strategy"))

        if not docs:
            return {"docs": []}

        started = perf_counter()
        status, code = "ok", "reranked"
        identity = getattr(reranker, "identity", None)
        if callable(identity) and identity().provider == "passthrough":
            status, code = "skipped", "disabled"
        try:
            reranked = reranker.rerank(query, docs, top_k=policy.rerank_k)
        except Exception as exc:
            status, code = "degraded", type(exc).__name__
            logger.warning("Reranker failed, degrading by fusion score (%s)", type(exc).__name__)
            reranked = sorted(
                docs,
                key=lambda d: d.metadata.get("fusion_score", 0.0),
                reverse=True,
            )
            reranked = reranked[:policy.rerank_k]
            for doc in reranked:
                doc.metadata["reranker_degraded"] = True

        route = dict(state.get("route") or {})
        route["retrieval_diagnostics"] = [*(route.get("retrieval_diagnostics") or []), {
            "channel": "reranker", "status": status, "code": code,
            "count": len(reranked), "elapsed_ms": round((perf_counter()-started)*1000, 2),
        }]
        for doc in reranked:
            doc.metadata["retrieval_diagnostics"] = [*(doc.metadata.get("retrieval_diagnostics") or []), route["retrieval_diagnostics"][-1]]
        return {"docs": _attach_relevance_scores(reranked), "route": route}

    return rerank_node


def rerank_node(state):
    from rag.providers.factory import get_reranker
    return create_rerank_node(get_reranker())(state)
