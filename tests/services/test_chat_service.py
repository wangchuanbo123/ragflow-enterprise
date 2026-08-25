"""RAG/聊天服务层测试：两阶段拆分与原始问题保留。"""

from rag.runtime.runtime import RAGRuntime
from app.services import rag_service
from tests.conftest import FakeReranker, FakeRetriever, FakeStreamLLM


def _runtime():
    return RAGRuntime(llm=FakeStreamLLM(), retriever=FakeRetriever(), reranker=FakeReranker())


def test_prepare_context_keeps_original_query():
    runtime = _runtime()
    prepared = rag_service.prepare_context("原始问题", [], runtime)
    assert "context" in prepared
    assert "sources" in prepared
    assert "retrieval_queries" in prepared
    assert "原始问题" in prepared["retrieval_queries"]
    assert runtime.retriever.queries


def test_stream_generate_is_real_stream():
    runtime = _runtime()
    prepared = rag_service.prepare_context("原始问题", [], runtime)
    chunks = list(rag_service.stream_generate("原始问题", prepared["context"], [], runtime))
    assert len(chunks) == len(runtime.llm.chunks)
    assert "".join(chunks) == "".join(runtime.llm.chunks)


def test_generate_uses_original_query_and_multi_turn_rewrite():
    """多轮检索可改写，但生成答案仍使用原始问题。"""
    seen_prompts: list[str] = []

    class TrackingLLM(FakeStreamLLM):
        def generate(self, prompt):
            seen_prompts.append(prompt)
            if "改写后的问题" in prompt or "改写后" in prompt:
                return "改写后的查询"
            return "最终答案"

    runtime = RAGRuntime(llm=TrackingLLM(), retriever=FakeRetriever(), reranker=FakeReranker())
    history = [{"role": "user", "content": "上一轮问题"}]
    prepared = rag_service.prepare_context("我的原始问题", history, runtime)
    answer = rag_service.generate("我的原始问题", prepared["context"], history, runtime)

    assert answer == "最终答案"
    # Dual-query: both original and rewritten are sent to retriever
    assert "我的原始问题" in runtime.retriever.queries
    assert "改写后的查询" in runtime.retriever.queries


def test_simple_high_confidence_question_skips_rewrite():
    runtime = _runtime()

    prepared = rag_service.prepare_context("订单模块依赖哪些服务？", [], runtime)

    assert prepared["retrieval_queries"] == ["订单模块依赖哪些服务？"]
    assert prepared["route"]["rewrite"] == {
        "applied": False,
        "requested": False,
        "reason": "not_needed",
    }
    assert runtime.llm.calls == []


def test_low_confidence_route_requests_rewrite():
    runtime = _runtime()

    prepared = rag_service.prepare_context(
        "只查询图谱",
        [],
        runtime,
        retrieval_options={"channels": ["graph"], "strategy": "fast"},
    )

    assert prepared["route"]["confidence"] == 0.5
    assert prepared["route"]["rewrite"]["requested"] is True
    assert prepared["route"]["strategy"] == "fast"
