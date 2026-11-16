from functools import cached_property

import httpx
import math
from openai import OpenAI

from rag.providers.interfaces import ProviderHealth, ProviderIdentity


class OpenAIEmbeddings:
    """Raw-string embedding requests; compatible endpoints own tokenization."""
    def __init__(self, *, model, api_key, base_url, request_timeout=120, max_retries=2,
                 http_client=None, dimensions=None, **_compatibility):
        self.model, self.base_url, self.dimensions = model, base_url, dimensions
        self.embed_instruction = self.query_instruction = ''
        self._sdk = OpenAI(api_key=api_key, base_url=base_url, timeout=request_timeout,
                           max_retries=max_retries, http_client=http_client)
        self.client = self._sdk.embeddings

    @property
    def max_retries(self):
        return self._sdk.max_retries

    @max_retries.setter
    def max_retries(self, value):
        self._sdk.max_retries = value

    def embed_documents(self, texts):
        if not texts:
            return []
        vectors = []
        for offset in range(0, len(texts), 128):
            batch = texts[offset:offset+128]
            payload = {'model': self.model, 'input': batch, 'encoding_format': 'float'}
            if self.dimensions is not None:
                payload['dimensions'] = self.dimensions
            response = self.client.create(**payload)
            rows = sorted(response.data, key=lambda row: row.index)
            if [row.index for row in rows] != list(range(len(batch))):
                raise ValueError('Embedding response does not match the requested inputs')
            values = [row.embedding for row in rows]
            if any(not vector or not all(math.isfinite(float(value)) for value in vector) for vector in values):
                raise ValueError('Embedding response contains empty or non-finite vectors')
            vectors.extend(values)
        if len({len(vector) for vector in vectors}) > 1:
            raise ValueError('Embedding response contains inconsistent vector dimensions')
        return vectors

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    def close(self):
        self._sdk.close()


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
    """Embedding adapter for OpenAI-compatible endpoints."""

    def __init__(self, model: str, api_key: str, base_url: str, timeout: float = 120):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    @cached_property
    def _model(self):
        if not self.api_key or not self.base_url:
            raise RuntimeError("Embedding API key and base URL are required")
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
