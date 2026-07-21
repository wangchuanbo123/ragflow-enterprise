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
            Document(page_content="次要资料", metadata={"source": "secondary.txt"}),
            Document(page_content="主要资料", metadata={"source": "primary.txt"}),
        ]


class FakeReranker:
    def rerank(self, query, docs, top_k=None):
        del query, top_k
        return list(reversed(docs))


class ArchitectureTests(unittest.TestCase):
    def test_missing_remote_config_falls_back_to_ollama(self):
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

    def test_factories_create_configured_providers_without_loading_models(self):
        self.assertTrue(callable(get_llm_provider().generate))
        self.assertTrue(callable(get_embedding_provider().get_model))
        self.assertTrue(callable(get_reranker().rerank))
        self.assertTrue(callable(get_vector_store_provider().load))
        self.assertEqual(get_runtime.cache_info().currsize, 0)

    def test_graph_uses_injected_runtime_end_to_end(self):
        llm = FakeLLMProvider()
        retriever = FakeRetriever()
        runtime = RAGRuntime(
            llm=llm,
            retriever=retriever,
            reranker=FakeReranker(),
        )
        graph = build_graph(runtime)

        result = ask_question("原始问题", graph=graph)

        self.assertEqual(retriever.queries, ["重写后的问题"])
        self.assertEqual(result["answer"], "基于知识库生成的答案")
        self.assertEqual(result["sources"][0]["source"], "primary.txt")
        self.assertEqual(len(llm.prompts), 2)


if __name__ == "__main__":
    unittest.main()
