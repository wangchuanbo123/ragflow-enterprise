"""Explainable retrieval routing and scope resolution."""

from __future__ import annotations

from rag.retrieval_scope import resolve_scope_document_ids
from rag.retrieval_strategy import get_retrieval_policy
from rag.routing import route_query


def create_route_node(runtime):
    def route_node(state):
        options = state.get("retrieval_options") or {}
        scope = options.get("scope") or {}
        policy = get_retrieval_policy(options.get("strategy"))
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
        route["strategy"] = policy.name
        return {
            "route": route,
            "allowed_document_ids": allowed_document_ids,
            "retrieval_strategy": policy.name,
        }

    return route_node


def create_post_rewrite_route_node(runtime):
    """Recheck rule intent after rewrite without a second router-LLM call."""

    def post_rewrite_route_node(state):
        current = dict(state.get("route") or {})
        rewrite = current.get("rewrite") or {}
        options = state.get("retrieval_options") or {}
        if not rewrite.get("applied") or options.get("channels"):
            return {}

        query = state.get("retrieval_query") or state.get("original_query", "")
        rerouted = route_query(
            query,
            explicit_channels=[],
            scope=options.get("scope") or {},
            router_llm=None,
            graph_available=runtime.graph_retriever is not None,
            code_available=runtime.code_retriever is not None,
        )
        rerouted["strategy"] = current.get("strategy", "standard")
        rerouted["rewrite"] = rewrite
        rerouted["scope_match_count"] = current.get("scope_match_count")
        rerouted["reasons"] = list(rerouted.get("reasons") or []) + [
            "问题改写后执行轻量规则复核路由"
        ]
        return {"route": rerouted}

    return post_rewrite_route_node
