"""Single RAG application service backed by the compiled LangGraph workflow."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.graph.rag_graph import build_graph
from rag.nodes.generate_node import PROMPT as ANSWER_PROMPT
from rag.nodes.generate_node import _build_prompt as build_answer_prompt
from rag.runtime.runtime import (
    RAGRuntime,
    get_active_collection_name,
    get_current_runtime,
    invalidate_runtime_cache,
)

_runtime_override: RAGRuntime | None = None


def set_runtime_override(runtime: RAGRuntime | None) -> None:
    """Inject or clear a runtime for tests and local diagnostics."""
    global _runtime_override
    _runtime_override = runtime
    _get_graph.cache_clear()


def active_runtime(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> RAGRuntime:
    return _runtime_override or get_current_runtime(knowledge_base_id)


def _active_runtime_for_knowledge_base(knowledge_base_id: str) -> RAGRuntime:
    """Keep the default-KB no-argument call shape compatible with integrations."""
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return active_runtime()
    return active_runtime(knowledge_base_id)


@lru_cache(maxsize=32)
def _get_graph(knowledge_base_id: str, index_signature: str):
    del index_signature
    return build_graph(_active_runtime_for_knowledge_base(knowledge_base_id))


def get_graph(knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID):
    runtime = _active_runtime_for_knowledge_base(knowledge_base_id)
    signature = runtime.index_signature
    if signature is None:
        signature = get_active_collection_name(knowledge_base_id) or ""
    return _get_graph(knowledge_base_id, signature)


def invalidate_rag_caches() -> None:
    """Invalidate all in-process RAG caches after an index mutation."""
    invalidate_runtime_cache()
    _get_graph.cache_clear()
    from rag.knowledge_graph.retriever import invalidate_graph_retrievers

    invalidate_graph_retrievers()
    try:
        from app.bootstrap.container import get_provider_container

        get_provider_container().cache.publish("rag-cache-invalidate", "all")
    except Exception:
        # Database signatures still provide cross-process correctness.
        pass


def _retrieval_dict(retrieval_options) -> dict:
    if retrieval_options is None:
        return {"channels": [], "scope": {}}
    if hasattr(retrieval_options, "model_dump"):
        return retrieval_options.model_dump()
    return dict(retrieval_options)


def _initial_state(
    query: str,
    history: list[dict] | None,
    retrieval_options,
    generation_mode: str,
) -> dict:
    return {
        "original_query": query,
        "query": query,
        "history": history or [],
        "retrieval_options": _retrieval_dict(retrieval_options),
        "generation_mode": generation_mode,
    }


def prepare_context(
    original_query: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
    retrieval_options=None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> dict:
    """Run rewrite, routing, retrieval, reranking, and context construction."""
    rt = runtime or _active_runtime_for_knowledge_base(knowledge_base_id)
    graph = build_graph(rt) if runtime is not None else get_graph(knowledge_base_id)
    state = graph.invoke(
        _initial_state(original_query, history, retrieval_options, "context")
    )
    result = {
        "retrieval_query": state.get("retrieval_query", original_query),
        "retrieval_queries": state.get("retrieval_queries", [original_query]),
        "context": state.get("context", ""),
        "sources": state.get("sources", []),
        "route": state.get("route", {}),
    }
    if state.get("context_json_file"):
        result["context_json_file"] = state["context_json_file"]
    return result


def generate(
    original_query: str,
    context: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
) -> str:
    """Compatibility helper for callers that already own a prepared context."""
    rt = runtime or _active_runtime_for_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID)
    prompt = build_answer_prompt(
        {
            "original_query": original_query,
            "context": context,
            "history": history or [],
        },
        ANSWER_PROMPT,
    )
    return rt.llm.generate(prompt)


def stream_generate(
    original_query: str,
    context: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
) -> Iterator[str]:
    """Compatibility helper for true model streaming over prepared context."""
    rt = runtime or _active_runtime_for_knowledge_base(DEFAULT_KNOWLEDGE_BASE_ID)
    prompt = build_answer_prompt(
        {
            "original_query": original_query,
            "context": context,
            "history": history or [],
        },
        ANSWER_PROMPT,
    )
    yield from rt.llm.stream(prompt)


def ask(
    query: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    retrieval_options=None,
) -> dict:
    """Run the complete compiled workflow and return an explainable answer."""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        rt = runtime or _active_runtime_for_knowledge_base(knowledge_base_id)
        graph = build_graph(rt) if runtime is not None else get_graph(knowledge_base_id)
        result = graph.invoke(
            _initial_state(query, history, retrieval_options, "sync")
        )
    return {
        "answer": result.get("answer", ""),
        "sources": result.get("sources", []),
        "route": result.get("route", {}),
    }


def stream(
    query: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    retrieval_options=None,
) -> Iterator[dict]:
    """Prepare through LangGraph, then expose true token/chunk streaming events."""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        rt = runtime or _active_runtime_for_knowledge_base(knowledge_base_id)
        prepared = prepare_context(
            query,
            history,
            rt,
            retrieval_options,
            knowledge_base_id,
        )
        yield {"event": "route", "data": {"route": prepared["route"]}}
        for chunk in stream_generate(query, prepared["context"], history, rt):
            yield {"event": "delta", "data": {"content": chunk}}
        yield {"event": "sources", "data": {"sources": prepared["sources"]}}


def inspect_retrieval(
    query: str,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    retrieval_options=None,
    runtime: RAGRuntime | None = None,
) -> dict:
    """Run the shared graph without answer generation for safe retrieval debugging."""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        rt = runtime or _active_runtime_for_knowledge_base(knowledge_base_id)
        prepared = prepare_context(
            query,
            [],
            rt,
            retrieval_options,
            knowledge_base_id,
        )
    return {
        "original_query": query,
        "retrieval_queries": prepared["retrieval_queries"],
        "route": prepared["route"],
        "sources": prepared["sources"],
        "context_preview": prepared["context"][:4000],
    }


def ask_question(
    query,
    graph=None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    retrieval_options=None,
):
    """Legacy `/ask` compatibility wrapper over the same compiled workflow."""
    if graph is None:
        return ask(
            query,
            knowledge_base_id=knowledge_base_id,
            retrieval_options=retrieval_options,
        )

    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        result = graph.invoke(
            _initial_state(query, [], retrieval_options, "sync")
        )
    return {
        "answer": result.get("answer", ""),
        "sources": result.get("sources", []),
        "route": result.get("route", {}),
    }
