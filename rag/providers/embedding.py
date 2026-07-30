from functools import cached_property

import httpx


class OllamaBatchEmbeddings:
    """LangChain-compatible Ollama embeddings using the batch API."""

    def __init__(
        self,
        model: str,
        base_url: str,
        timeout: float = 120,
        embed_instruction: str = "passage: ",
        query_instruction: str = "query: ",
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.embed_instruction = embed_instruction
        self.query_instruction = query_instruction

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = httpx.post(
            f"{self.base_url}/api/embed",
            json={
                "model": self.model,
                "input": texts,
                "truncate": True,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings") or []
        if len(embeddings) != len(texts):
            raise ValueError(
                f"Ollama returned {len(embeddings)} embeddings for {len(texts)} texts"
            )
        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([
            f"{self.embed_instruction}{text}"
            for text in texts
        ])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"{self.query_instruction}{text}"])[0]


class OllamaEmbeddingProvider:
    def __init__(self, model: str, base_url: str, timeout: float = 120):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    @cached_property
    def _model(self):
        return OllamaBatchEmbeddings(
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    def get_model(self):
        return self._model
