"""Single RAG application service backed by the compiled LangGraph workflow."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from rag.artifacts import collect_source_artifacts
from rag.graph.rag_graph import build_graph
from rag.context.context_builder import build_context
from rag.nodes.generate_node import PROMPT as ANSWER_PROMPT
from rag.nodes.generate_node import _build_prompt as build_answer_prompt
from rag.nodes.rerank_node import create_rerank_node
from rag.retrieval_strategy import get_retrieval_policy
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


def _answer_payload(answer: str, sources: list[dict], route: dict) -> dict:
    images, code_snippets = collect_source_artifacts(sources)
    return {
        "answer": answer,
        "sources": sources,
        "images": images,
        "code_snippets": code_snippets,
        "route": route,
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
        "docs": state.get("docs", []),
    }
    if state.get("context_json_file"):
        result["context_json_file"] = state["context_json_file"]
    return result


def _scope_with_defaults(retrieval_options) -> dict:
    retrieval = _retrieval_dict(retrieval_options)
    scope = dict(retrieval.get("scope") or {})
    scope.setdefault("knowledge_base_ids", [])
    scope.setdefault("document_ids", [])
    scope.setdefault("tag_ids", [])
    scope.setdefault("folder_id", None)
    scope.setdefault("metadata", {})
    return scope


def _local_retrieval_options(retrieval_options) -> dict:
    """Remove the cross-base selector before running one base's local graph."""
    retrieval = dict(_retrieval_dict(retrieval_options))
    scope = dict(retrieval.get("scope") or {})
    scope.pop("knowledge_base_ids", None)
    retrieval["scope"] = scope
    return retrieval


def _combine_routes(
    routes: list[dict],
    knowledge_bases: list[dict[str, str]],
    retrieval_options,
    fused_count: int,
    failures: list[dict[str, str]],
) -> dict:
    channel_order = ("text", "graph", "code")
    selected = [
        name for name in channel_order
        if any(name in (route.get("selected_channels") or []) for route in routes)
    ]
    requested = [
        name for name in channel_order
        if any(name in (route.get("requested_channels") or []) for route in routes)
    ]
    unavailable = [
        name for name in channel_order
        if any(name in (route.get("unavailable_channels") or []) for route in routes)
    ]
    channel_results: dict[str, dict[str, Any]] = {}
    for name in channel_order:
        children = [
            (route.get("channel_results") or {}).get(name)
            for route in routes
        ]
        children = [item for item in children if item]
        if not children:
            continue
        count = sum(int(item.get("count") or 0) for item in children)
        elapsed_ms = round(sum(float(item.get("elapsed_ms") or 0) for item in children), 2)
        channel_results[name] = {
            "status": "ok" if count else children[0].get("status", "empty"),
            "count": count,
            "elapsed_ms": elapsed_ms,
        }

    match_counts = [route.get("scope_match_count") for route in routes]
    scope_match_count = None
    if match_counts and all(value is not None for value in match_counts):
        scope_match_count = sum(int(value) for value in match_counts)

    confidences = [float(route.get("confidence") or 0) for route in routes]
    reasons = [f"已在 {len(routes)} 个知识库中检索并统一重排"]
    if failures:
        reasons.append(f"另有 {len(failures)} 个知识库检索失败，已自动降级")
    child_reasons = [reason for route in routes for reason in route.get("reasons", [])]
    reasons.extend(list(dict.fromkeys(child_reasons))[:4])
    retrieval = _retrieval_dict(retrieval_options)
    strategy = retrieval.get("strategy") or "standard"
    knowledge_base_status = [
        {"id": item["id"], "name": item["name"], "status": "ok"}
        for item in knowledge_bases
        if not any(failure["id"] == item["id"] for failure in failures)
    ] + failures
    return {
        "method": "multi-knowledge-base",
        "selected_channels": selected,
        "requested_channels": requested,
        "unavailable_channels": unavailable,
        "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "reasons": reasons,
        "scope": _scope_with_defaults(retrieval_options),
        "scope_match_count": scope_match_count,
        "channel_results": channel_results,
        "fused_count": fused_count,
        "strategy": strategy,
        "rewrite": routes[0].get("rewrite", {}) if routes else {},
        "query_embedding": {
            "status": "ready" if routes else "unavailable",
            "shared": bool(routes) and all(
                bool((route.get("query_embedding") or {}).get("shared"))
                for route in routes
            ),
            "scope": "within_each_knowledge_base",
            "build_count": len(routes),
            "elapsed_ms": round(sum(float((route.get("query_embedding") or {}).get("elapsed_ms") or 0) for route in routes), 2),
        },
        "knowledge_bases": knowledge_base_status,
    }


def prepare_context_multi(
    original_query: str,
    history: list[dict] | None,
    knowledge_bases: list[dict[str, str]],
    retrieval_options=None,
) -> dict:
    """Retrieve per authorized base, then globally rerank and build one context."""
    prepared_results: list[dict] = []
    failures: list[dict[str, str]] = []
    first_error: Exception | None = None
    local_options = _local_retrieval_options(retrieval_options)

    def retrieve_one(knowledge_base: dict[str, str], runtime: RAGRuntime) -> dict:
        return prepare_context(
            original_query,
            history,
            runtime,
            local_options,
            knowledge_base["id"],
        )

    work_items = [
        (knowledge_base, _active_runtime_for_knowledge_base(knowledge_base["id"]))
        for knowledge_base in knowledge_bases
    ]
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(work_items)))) as executor:
        futures = [
            (knowledge_base, executor.submit(retrieve_one, knowledge_base, runtime))
            for knowledge_base, runtime in work_items
        ]
        for knowledge_base, future in futures:
            knowledge_base_id = knowledge_base["id"]
            try:
                prepared = future.result()
            except Exception as exc:  # noqa: BLE001 - one unavailable base must not hide others
                first_error = first_error or exc
                failures.append({
                    "id": knowledge_base_id,
                    "name": knowledge_base["name"],
                    "status": "failed",
                })
                continue
            for doc in prepared.get("docs", []):
                doc.metadata["knowledge_base_id"] = knowledge_base_id
                doc.metadata["knowledge_base_name"] = knowledge_base["name"]
            prepared_results.append(prepared)

    if not prepared_results and first_error is not None:
        raise first_error

    docs = [doc for result in prepared_results for doc in result.get("docs", [])]
    strategy = _retrieval_dict(retrieval_options).get("strategy") or "standard"
    primary_runtime = _active_runtime_for_knowledge_base(knowledge_bases[0]["id"])
    reranked = create_rerank_node(primary_runtime.reranker)({
        "original_query": original_query,
        "docs": docs,
        "retrieval_strategy": strategy,
    }).get("docs", [])
    policy = get_retrieval_policy(strategy)
    context, sources = build_context(
        reranked,
        max_chunks=policy.context_chunks,
        max_tokens=policy.context_tokens,
    )
    routes = [result.get("route", {}) for result in prepared_results]
    route = _combine_routes(routes, knowledge_bases, retrieval_options, len(reranked), failures)
    return {
        "retrieval_query": prepared_results[0].get("retrieval_query", original_query) if prepared_results else original_query,
        "retrieval_queries": list(dict.fromkeys(
            query for result in prepared_results for query in result.get("retrieval_queries", [])
        )) or [original_query],
        "context": context,
        "sources": sources,
        "route": route,
        "docs": reranked,
    }


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
    return _answer_payload(
        result.get("answer", ""),
        result.get("sources", []),
        result.get("route", {}),
    )


def ask_multi(
    query: str,
    history: list[dict] | None,
    knowledge_bases: list[dict[str, str]],
    retrieval_options=None,
) -> dict:
    """Answer from multiple authorized knowledge bases with one global rerank."""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        prepared = prepare_context_multi(
            query,
            history,
            knowledge_bases,
            retrieval_options,
        )
        runtime = _active_runtime_for_knowledge_base(knowledge_bases[0]["id"])
        answer = generate(query, prepared["context"], history, runtime)
    return _answer_payload(
        answer, prepared["sources"], prepared["route"]
    )


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
        images, code_snippets = collect_source_artifacts(prepared["sources"])
        yield {"event": "sources", "data": {
            "sources": prepared["sources"],
            "images": images,
            "code_snippets": code_snippets,
        }}


def stream_multi(
    query: str,
    history: list[dict] | None,
    knowledge_bases: list[dict[str, str]],
    retrieval_options=None,
) -> Iterator[dict]:
    """Stream one answer after cross-base retrieval and global reranking."""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        prepared = prepare_context_multi(
            query,
            history,
            knowledge_bases,
            retrieval_options,
        )
        runtime = _active_runtime_for_knowledge_base(knowledge_bases[0]["id"])
        yield {"event": "route", "data": {"route": prepared["route"]}}
        for chunk in stream_generate(query, prepared["context"], history, runtime):
            yield {"event": "delta", "data": {"content": chunk}}
        images, code_snippets = collect_source_artifacts(prepared["sources"])
        yield {"event": "sources", "data": {
            "sources": prepared["sources"],
            "images": images,
            "code_snippets": code_snippets,
        }}


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
    return _answer_payload(
        result.get("answer", ""),
        result.get("sources", []),
        result.get("route", {}),
    )
