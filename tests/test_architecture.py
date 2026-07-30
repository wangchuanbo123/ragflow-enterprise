import unittest

from langchain_core.documents import Document

from app.services.rag_service import ask_question
from rag.graph.rag_graph import build_graph
from rag.providers.factory import (
    create_llm_provider,
    get_embedding_provider,
    get_llm_provider,
    get_reranker,
    get_vector_store_provider,
)
from rag.runtime.runtime import RAGRuntime, get_runtime


class FakeLLMProvider:
    def __init__(self):
        self.prompts = []

    def get_model(self):
        return self

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "改写后的问题" in prompt:
            return "重写后的问题"
        return "基于知识库生成的答案"


class FakeRetriever:
    def __init__(self):
        self.queries = []

    def get_relevant_documents(self, query: str):
        self.queries.append(query)
        return [
            Document(page_content="次要资料", metadata={"source": "secondary.txt", "chunk_id": "c1", "content_hash": "h1"}),
            Document(page_content="主要资料", metadata={"source": "primary.txt", "chunk_id": "c2", "content_hash": "h2"}),
        ]

    def retrieve_single(self, query: str):
        return self.get_relevant_documents(query)

    def retrieve_multi_query(self, queries: list):
        results = []
        for q in queries:
            results.extend(self.get_relevant_documents(q))
        seen = set()
        deduped = []
        for doc in results:
            cid = doc.metadata.get("chunk_id")
            if cid not in seen:
                seen.add(cid)
                deduped.append(doc)
        return deduped


class FakeReranker:
    def rerank(self, query, docs, top_k=None):
        del query, top_k
        return list(reversed(docs))


class ArchitectureTests(unittest.TestCase):
    def testMissingRemoteConfigFallsBackToOllama(self):
        cases = [
            ("", "https://api.example.com/v1"),
            ("test-key", ""),
        ]

        for api_key, base_url in cases:
            with self.subTest(api_key=bool(api_key), base_url=bool(base_url)):
                provider = create_llm_provider(
                    provider="zhipu",
                    model="glm-test",
                    api_key=api_key,
                    base_url=base_url,
                    ollama_model="qwen3:4b",
                    ollama_base_url="http://localhost:11434",
                )
                self.assertEqual(type(provider).__name__, "OllamaLLMProvider")
                self.assertEqual(provider.model, "qwen3:4b")

    def testFactoriesCreateConfiguredProvidersWithoutLoadingModels(self):
        self.assertTrue(callable(get_llm_provider().generate))
        self.assertTrue(callable(get_embedding_provider().get_model))
        self.assertTrue(callable(get_reranker().rerank))
        self.assertTrue(callable(get_vector_store_provider().load))
        self.assertEqual(get_runtime.cache_info().currsize, 0)

    def testGraphUsesInjectedRuntimeEndToEnd(self):
        llm = FakeLLMProvider()
        retriever = FakeRetriever()
        runtime = RAGRuntime(
            llm=llm,
            retriever=retriever,
            reranker=FakeReranker(),
        )
        graph = build_graph(runtime)

        result = ask_question("原始问题", graph=graph)

        # Dual-query: original + rewritten are both queried
        self.assertIn("原始问题", retriever.queries)
        self.assertIn("重写后的问题", retriever.queries)
        self.assertEqual(result["answer"], "基于知识库生成的答案")
        self.assertTrue(len(result["sources"]) > 0)
        self.assertTrue(len(llm.prompts), 2)  # rewrite + generate


def test_runtime_refreshes_after_cross_process_index_change(
    db_engine, monkeypatch
):
    import rag.runtime.runtime as runtime_module
    from app.core.database import SessionLocal
    from app.repositories.knowledge_repository import KnowledgeRepository

    monkeypatch.setattr(runtime_module, "SessionLocal", SessionLocal)
    with SessionLocal() as db:
        current_signature = KnowledgeRepository(db).get_index_signature()

    stale = RAGRuntime(
        llm=object(),
        retriever=object(),
        reranker=object(),
        index_signature="stale",
    )
    fresh = RAGRuntime(
        llm=object(),
        retriever=object(),
        reranker=object(),
        index_signature=current_signature,
    )
    state = {"invalidated": False}

    monkeypatch.setattr(
        runtime_module,
        "get_runtime",
        lambda: fresh if state["invalidated"] else stale,
    )
    monkeypatch.setattr(
        runtime_module,
        "invalidate_runtime_cache",
        lambda: state.update(invalidated=True),
    )

    assert runtime_module.get_current_runtime() is fresh
    assert state["invalidated"] is True


if __name__ == "__main__":
    unittest.main()
