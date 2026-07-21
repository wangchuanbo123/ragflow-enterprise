"""Rerank node."""

from rag.providers.factory import get_reranker
from rag.providers.interfaces import Reranker


def create_rerank_node(reranker: Reranker):
    def rerank_node(state):
        return {
            "docs": reranker.rerank(state["query"], state["docs"]),
        }

    return rerank_node


def rerank_node(state):
    return create_rerank_node(get_reranker())(state)
