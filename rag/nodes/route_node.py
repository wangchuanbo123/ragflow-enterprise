"""LangGraph node for explainable retrieval routing and scope resolution."""

from __future__ import annotations

from rag.retrieval_scope import resolve_scope_document_ids
from rag.routing import route_query


def create_route_node(runtime):
    def route_node(state):
        options = state.get("retrieval_options") or {}
        scope = options.get("scope") or {}
        route = route_query(
            state.get("original_query") or state.get("query", ""),
            explicit_channels=options.get("channels") or [],
            scope=scope,
            router_llm=runtime.router_llm,
            graph_available=runtime.graph_retriever is not None,
            code_available=runtime.code_retriever is not None,
        )
        allowed_document_ids = resolve_scope_document_ids(
            runtime.knowledge_base_id,
            scope,
        )
        route["scope_match_count"] = (
            None if allowed_document_ids is None else len(allowed_document_ids)
        )
        return {
            "route": route,
            "allowed_document_ids": allowed_document_ids,
        }

    return route_node
