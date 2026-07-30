"""Ollama batch embedding tests."""

from rag.providers.embedding import OllamaBatchEmbeddings


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
