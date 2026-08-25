from langchain_core.documents import Document

from rag.graph.rag_graph import build_graph
from rag.retrieval_strategy import get_retrieval_policy
from rag.retrievers.hybrid_retriever import HybridRRFRetriever
from rag.runtime.runtime import RAGRuntime
from tests.conftest import FakeReranker, FakeStreamLLM


class CountingEmbedding:
    def __init__(self):
        self.queries = []

    def embed_query(self, query):
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class OptionRetriever:
    def __init__(self, channel):
        self.channel = channel
        self.calls = []

    def retrieve_with_options(
        self,
        queries,
        *,
        query_embedding=None,
        strategy="standard",
        allowed_document_ids=None,
    ):
        self.calls.append({
            "queries": queries,
            "embedding": query_embedding,
            "strategy": strategy,
            "allowed": allowed_document_ids,
        })
        return [Document(
            page_content=self.channel,
            metadata={
                "chunk_id": self.channel,
                "source": f"{self.channel}.txt",
                "_channel": self.channel,
            },
        )]


class VectorStoreSpy:
    def __init__(self):
        self.vector_calls = 0
        self.text_calls = 0

    def similarity_search_by_vector(self, embedding, **kwargs):
        del embedding, kwargs
        self.vector_calls += 1
        return [Document(page_content="vector", metadata={"chunk_id": "v1"})]

    def similarity_search(self, query, **kwargs):
        del query, kwargs
        self.text_calls += 1
        return []


def test_retrieval_profiles_have_increasing_budgets():
    fast = get_retrieval_policy("fast")
    standard = get_retrieval_policy("standard")
    deep = get_retrieval_policy("deep")

    assert fast.vector_k < standard.vector_k < deep.vector_k
    assert fast.candidate_k < standard.candidate_k < deep.candidate_k
    assert fast.context_tokens < standard.context_tokens < deep.context_tokens
    assert fast.graph_max_hops == 1
    assert deep.graph_max_hops == 2


def test_query_embedding_is_created_once_and_shared_across_channels():
    embedding = CountingEmbedding()
    text = OptionRetriever("text")
    graph = OptionRetriever("graph")
    code = OptionRetriever("code")
    runtime = RAGRuntime(
        llm=FakeStreamLLM(),
        retriever=text,
        graph_retriever=graph,
        code_retriever=code,
        reranker=FakeReranker(),
        embedding_model=embedding,
    )

    result = build_graph(runtime).invoke({
        "original_query": "检查调用关系",
        "query": "检查调用关系",
        "history": [],
        "retrieval_options": {
            "channels": ["text", "graph", "code"],
            "strategy": "deep",
            "scope": {},
        },
        "generation_mode": "context",
    })

    assert embedding.queries == ["检查调用关系"]
    for retriever in (text, graph, code):
        assert retriever.calls[0]["embedding"] == [0.1, 0.2, 0.3]
        assert retriever.calls[0]["strategy"] == "deep"
    assert result["route"]["query_embedding"]["shared"] is True
    assert result["route"]["query_embedding"]["dimensions"] == 3


def test_text_multi_query_uses_shared_vector_only_once():
    store = VectorStoreSpy()
    retriever = HybridRRFRetriever(store)

    docs = retriever.retrieve_multi_query(
        ["原始问题", "改写问题"],
        query_embedding=[0.1, 0.2],
        strategy="standard",
    )

    assert store.vector_calls == 1
    assert store.text_calls == 0
    assert docs[0].metadata["chunk_id"] == "v1"


def test_multi_turn_rewrite_rechecks_code_route_without_router_llm():
    text = OptionRetriever("text")
    code = OptionRetriever("code")
    runtime = RAGRuntime(
        llm=FakeStreamLLM(answer="这个函数的源码在哪里实现？"),
        retriever=text,
        code_retriever=code,
        reranker=FakeReranker(),
    )

    result = build_graph(runtime).invoke({
        "original_query": "它在哪里？",
        "query": "它在哪里？",
        "history": [{"role": "user", "content": "订单超时处理函数"}],
        "retrieval_options": {"strategy": "standard", "scope": {}},
        "generation_mode": "context",
    })

    assert result["route"]["rewrite"]["applied"] is True
    assert result["route"]["selected_channels"] == ["text", "code"]
    assert any("复核路由" in reason for reason in result["route"]["reasons"])
