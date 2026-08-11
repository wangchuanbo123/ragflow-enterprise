from threading import Barrier

from langchain_core.documents import Document

from rag.graph.rag_graph import build_graph
from rag.runtime.runtime import RAGRuntime
from tests.conftest import FakeReranker, FakeStreamLLM


class BarrierRetriever:
    def __init__(self, barrier: Barrier, channel: str, *, fail: bool = False):
        self.barrier = barrier
        self.channel = channel
        self.fail = fail

    def get_relevant_documents(self, query: str):
        del query
        self.barrier.wait(timeout=2)
        if self.fail:
            raise RuntimeError(f"{self.channel} unavailable")
        return [
            Document(
                page_content=f"{self.channel} result",
                metadata={
                    "chunk_id": f"{self.channel}-1",
                    "source": f"{self.channel}.txt",
                    "_channel": self.channel,
                },
            )
        ]


def _run_graph(text, graph, code):
    runtime = RAGRuntime(
        llm=FakeStreamLLM(answer="parallel"),
        retriever=text,
        graph_retriever=graph,
        code_retriever=code,
        reranker=FakeReranker(),
    )
    return build_graph(runtime).invoke({
        "original_query": "parallel",
        "query": "parallel",
        "history": [],
        "retrieval_options": {
            "channels": ["text", "graph", "code"],
            "scope": {},
        },
        "generation_mode": "context",
    })


def test_retrieval_channels_fan_out_concurrently_and_fan_in_once():
    barrier = Barrier(3)
    result = _run_graph(
        BarrierRetriever(barrier, "text"),
        BarrierRetriever(barrier, "graph"),
        BarrierRetriever(barrier, "code"),
    )

    assert set(result["route"]["channel_results"]) == {"text", "graph", "code"}
    assert all(
        item["status"] == "ok"
        for item in result["route"]["channel_results"].values()
    )
    assert result["route"]["fused_count"] == 3


def test_one_parallel_channel_failure_does_not_discard_other_results():
    barrier = Barrier(3)
    result = _run_graph(
        BarrierRetriever(barrier, "text"),
        BarrierRetriever(barrier, "graph"),
        BarrierRetriever(barrier, "code", fail=True),
    )

    assert result["route"]["channel_results"]["code"]["status"] == "error"
    assert "RuntimeError" in result["route"]["channel_results"]["code"]["error"]
    assert result["route"]["fused_count"] == 2
