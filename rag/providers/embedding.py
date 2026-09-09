from functools import cached_property

import httpx

from rag.providers.interfaces import ProviderHealth, ProviderIdentity


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

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="embedding",
            provider="ollama",
            model=self.model,
            capabilities=("documents", "query", "batch"),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
            endpoint=self.base_url,
        )


class OpenAICompatibleEmbeddingProvider:
    """Embedding adapter for OpenAI-compatible enterprise endpoints."""

    def __init__(self, model: str, api_key: str, base_url: str, timeout: float = 120):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    @cached_property
    def _model(self):
        if not self.api_key or not self.base_url:
            raise RuntimeError("Embedding API key and base URL are required")
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            request_timeout=self.timeout,
            max_retries=2,
            # Compatible BGE endpoints own their input limits and do not use
            # OpenAI tokenization. Disabling this also prevents an unnecessary
            # first-run download of cl100k_base in offline deployments.
            check_embedding_ctx_length=False,
        )

    def get_model(self):
        return self._model

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="embedding",
            provider="openai_compatible",
            model=self.model,
            capabilities=("documents", "query", "batch"),
        )

    def health(self) -> ProviderHealth:
        if not self.api_key or not self.base_url:
            return ProviderHealth.fail("Embedding API key and base URL are required")
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
        )
