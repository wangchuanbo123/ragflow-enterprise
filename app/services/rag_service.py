"""
RAG Service

提供统一调用接口，分为两个可复用阶段：
1. prepare_context: rewrite -> retrieve -> rerank -> build_context
2. generate / stream_generate: 生成或流式输出答案
"""

from collections.abc import Iterator
from functools import lru_cache

from rag.context.context_builder import build_context
from rag.context.context_exporter import export_context_snapshot
from rag.graph.rag_graph import build_graph
from rag.nodes.generate_node import _build_prompt as build_answer_prompt
from rag.nodes.generate_node import PROMPT as ANSWER_PROMPT
from rag.nodes.rerank_node import create_rerank_node
from rag.nodes.retrieve_node import create_retrieve_node
from rag.runtime.runtime import (
    RAGRuntime,
    get_active_collection_name,
    get_current_runtime,
    invalidate_runtime_cache,
)

_runtime_override: RAGRuntime | None = None


def set_runtime_override(runtime: RAGRuntime | None) -> None:
    """测试用：注入或清除 RAG 运行时，避免依赖真实模型。"""
    global _runtime_override
    _runtime_override = runtime
    if "_get_graph" in globals():
        _get_graph.cache_clear()


def active_runtime() -> RAGRuntime:
    return _runtime_override or get_current_runtime()


@lru_cache(maxsize=1)
def _get_graph(index_signature: str):
    del index_signature
    return build_graph(active_runtime())


def get_graph():
    runtime = active_runtime()
    return _get_graph(runtime.index_signature or get_active_collection_name() or "")


def invalidate_rag_caches() -> None:
    """Invalidate all in-process RAG caches after an index mutation."""
    invalidate_runtime_cache()
    _get_graph.cache_clear()


def prepare_context(
    original_query: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
) -> dict:
    """阶段一：rewrite -> retrieve -> rerank -> build_context。

    返回检索问题、最终上下文、来源，以及启用调试导出时的 JSON 文件路径。
    original_query 不会被覆盖。
    """
    rt = runtime or active_runtime()
    state = {"original_query": original_query, "history": history or []}

    from rag.nodes.rewrite_node import create_rewrite_node as _make_rewrite
    rewrite_fn = _make_rewrite(rt.llm)
    state.update(rewrite_fn(state))

    retrieve_fn = create_retrieve_node(rt.retriever)
    state.update(retrieve_fn(state))

    rerank_fn = create_rerank_node(rt.reranker)
    state.update(rerank_fn(state))

    context, sources = build_context(state["docs"])
    state["context"] = context
    state["sources"] = sources
    snapshot = export_context_snapshot(state)
    result = {
        "retrieval_query": state.get("retrieval_query", original_query),
        "retrieval_queries": state.get("retrieval_queries", [original_query]),
        "context": context,
        "sources": sources,
    }
    if snapshot is not None:
        result["context_json_file"] = str(snapshot)
    return result


def generate(
    original_query: str,
    context: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
) -> str:
    """阶段二（同步）：生成完整答案。"""
    rt = runtime or active_runtime()
    state = {
        "original_query": original_query,
        "context": context,
        "history": history or [],
    }
    prompt = build_answer_prompt(state, ANSWER_PROMPT)
    return rt.llm.generate(prompt)


def stream_generate(
    original_query: str,
    context: str,
    history: list[dict] | None = None,
    runtime: RAGRuntime | None = None,
) -> Iterator[str]:
    """阶段二（流式）：逐段产出真实模型输出。"""
    rt = runtime or active_runtime()
    state = {
        "original_query": original_query,
        "context": context,
        "history": history or [],
    }
    prompt = build_answer_prompt(state, ANSWER_PROMPT)
    yield from rt.llm.stream(prompt)


def ask(query: str, history: list[dict] | None = None, runtime: RAGRuntime | None = None) -> dict:
    """带历史的完整问答（同步），供聊天同步接口使用。"""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        rt = runtime or active_runtime()
        prepared = prepare_context(query, history, rt)
        answer = generate(query, prepared["context"], history, rt)
    return {"answer": answer, "sources": prepared["sources"]}


def ask_question(query, graph=None):
    """原有 /ask 入口，无历史，保持兼容。"""
    from app.services.rag_concurrency import rag_concurrency

    with rag_concurrency():
        active_graph = graph or get_graph()
        result = active_graph.invoke({
            "original_query": query,
            "query": query,
        })

    answer = result["answer"]
    sources = result.get("sources", [])

    print("\n==== RAG Sources ====")
    for s in sources:
        print("文件:", s.get("source", "unknown"))
        print("引用:", s.get("citation_id", ""))
        print("内容片段:", s.get("preview", "")[:100])
        print("------")

    return {
        "answer": answer,
        "sources": sources,
    }
