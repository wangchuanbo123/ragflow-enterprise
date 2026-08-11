"""Independent text/graph retrieval and fusion nodes."""

from __future__ import annotations

from time import perf_counter

from app.core.config import (
    BM25_RETRIEVAL_WEIGHT,
    CODE_RETRIEVAL_WEIGHT,
    GRAPH_RETRIEVAL_WEIGHT,
    VECTOR_RETRIEVAL_WEIGHT,
)
from rag.nodes.retrieve_node import create_retrieve_node
from rag.retrieval_scope import filter_documents_by_scope
from rag.retrievers.hybrid_retriever import weighted_rrf_fusion


def create_text_retrieve_node(retriever):
    retrieve = create_retrieve_node(retriever)

    def text_retrieve_node(state):
        started = perf_counter()
        route = state.get("route") or {}
        error = None
        try:
            if "text" not in route.get("selected_channels", ["text"]):
                documents = []
                status = "skipped"
            else:
                documents = retrieve(state).get("docs", [])
                documents = filter_documents_by_scope(
                    documents,
                    state.get("allowed_document_ids"),
                )
                status = "ok"
        except Exception as exc:
            documents = []
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        channel_result = {
            "status": status,
            "count": len(documents),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }
        if error:
            channel_result["error"] = error[:500]
        return {
            "text_docs": documents,
            "text_channel_result": channel_result,
            "parallel_markers": ["text"],
        }

    return text_retrieve_node


def create_graph_retrieve_node(retriever):
    retrieve = create_retrieve_node(retriever) if retriever is not None else None

    def graph_retrieve_node(state):
        started = perf_counter()
        route = state.get("route") or {}
        error = None
        try:
            if retriever is None or "graph" not in route.get("selected_channels", []):
                documents = []
                status = "skipped"
            else:
                documents = retrieve(state).get("docs", [])
                documents = filter_documents_by_scope(
                    documents,
                    state.get("allowed_document_ids"),
                )
                status = "ok"
        except Exception as exc:
            documents = []
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        channel_result = {
            "status": status,
            "count": len(documents),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }
        if error:
            channel_result["error"] = error[:500]
        return {
            "graph_docs": documents,
            "graph_channel_result": channel_result,
            "parallel_markers": ["graph"],
        }

    return graph_retrieve_node


def create_code_retrieve_node(retriever):
    retrieve = create_retrieve_node(retriever) if retriever is not None else None

    def code_retrieve_node(state):
        started = perf_counter()
        route = state.get("route") or {}
        error = None
        try:
            if retriever is None or "code" not in route.get("selected_channels", []):
                documents = []
                status = "skipped"
            else:
                documents = retrieve(state).get("docs", [])
                documents = filter_documents_by_scope(
                    documents,
                    state.get("allowed_document_ids"),
                )
                status = "ok"
        except Exception as exc:
            documents = []
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        channel_result = {
            "status": status,
            "count": len(documents),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }
        if error:
            channel_result["error"] = error[:500]
        return {
            "code_docs": documents,
            "code_channel_result": channel_result,
            "parallel_markers": ["code"],
        }

    return code_retrieve_node


def fusion_node(state):
    text_docs = state.get("text_docs") or []
    graph_docs = state.get("graph_docs") or []
    code_docs = state.get("code_docs") or []
    text_weight = VECTOR_RETRIEVAL_WEIGHT + BM25_RETRIEVAL_WEIGHT
    ranked = []
    if text_docs:
        ranked.append((text_docs, text_weight or 1.0))
    if graph_docs:
        ranked.append((graph_docs, GRAPH_RETRIEVAL_WEIGHT or 1.0))
    if code_docs:
        ranked.append((code_docs, CODE_RETRIEVAL_WEIGHT or 1.0))
    documents = weighted_rrf_fusion(ranked) if ranked else []
    route = dict(state.get("route") or {})
    route["channel_results"] = {
        "text": state.get("text_channel_result") or {"status": "skipped", "count": 0, "elapsed_ms": 0},
        "graph": state.get("graph_channel_result") or {"status": "skipped", "count": 0, "elapsed_ms": 0},
        "code": state.get("code_channel_result") or {"status": "skipped", "count": 0, "elapsed_ms": 0},
    }
    route["fused_count"] = len(documents)
    return {
        "docs": documents,
        "retrieval_candidates": documents,
        "route": route,
    }
