"""Embedding provider tests."""

from rag.providers.embedding import (
    OllamaBatchEmbeddings,
    OpenAICompatibleEmbeddingProvider,
)


class _Response:
    def __init__(self, embeddings):
        self._embeddings = embeddings

    def raise_for_status(self):
        return None

    def json(self):
        return {"embeddings": self._embeddings}


def test_embed_documents_uses_single_batch_request(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Response([[1.0, 0.0], [0.0, 1.0]])

    monkeypatch.setattr("rag.providers.embedding.httpx.post", fake_post)
    embeddings = OllamaBatchEmbeddings(
        model="test-model",
        base_url="http://localhost:11434/",
    )

    result = embeddings.embed_documents(["文档一", "文档二"])

    assert result == [[1.0, 0.0], [0.0, 1.0]]
    assert len(calls) == 1
    assert calls[0][0] == "http://localhost:11434/api/embed"
    assert calls[0][1]["input"] == ["passage: 文档一", "passage: 文档二"]


def test_embed_query_uses_query_instruction(monkeypatch):
    def fake_post(url, json, timeout):
        del url, timeout
        assert json["input"] == ["query: 用户问题"]
        return _Response([[0.5, 0.5]])

    monkeypatch.setattr("rag.providers.embedding.httpx.post", fake_post)
    embeddings = OllamaBatchEmbeddings(
        model="test-model",
        base_url="http://localhost:11434",
    )

    assert embeddings.embed_query("用户问题") == [0.5, 0.5]


def test_openai_compatible_embeddings_do_not_download_openai_tokenizer(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_embeddings(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("rag.providers.embedding.OpenAIEmbeddings", fake_embeddings)
    provider = OpenAICompatibleEmbeddingProvider(
        model="BAAI/bge-m3",
        api_key="test-key",
        base_url="https://embedding.example/v1",
    )

    assert provider.get_model() is sentinel
    assert captured["check_embedding_ctx_length"] is False
