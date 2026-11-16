from functools import cached_property
from typing import List, Sequence

from langchain_core.documents import Document

from rag.providers.interfaces import ProviderHealth, ProviderIdentity


class BgeReranker:
    def __init__(self, model: str, default_top_k: int = 5):
        self.model = model
        self.default_top_k = default_top_k

    @cached_property
    def _model(self):
        from sentence_transformers import CrossEncoder
        return CrossEncoder(self.model)

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        if not docs:
            return []

        pairs = [(query, doc.page_content) for doc in docs]
        scores = self._model.predict(pairs)
        ranked = sorted(zip(docs, scores), key=lambda item: item[1], reverse=True)
        limit = self.default_top_k if top_k is None else top_k
        result: list[Document] = []
        for doc, score in ranked[:limit]:
            doc.metadata = {**doc.metadata, "rerank_score": float(score)}
            result.append(doc)
        return result

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="reranker",
            provider="bge_local",
            model=self.model,
            capabilities=("rerank",),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(provider=self.identity().provider, model=self.model)


class PassthroughReranker:
    def __init__(self, default_top_k: int = 5):
        self.default_top_k = default_top_k

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        del query
        limit = self.default_top_k if top_k is None else top_k
        return list(docs[:limit])

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="reranker",
            provider="passthrough",
            capabilities=("rerank",),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(provider=self.identity().provider)


class HttpReranker:
    """Vendor-neutral HTTP reranker using a common /rerank request shape."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "",
        default_top_k: int = 5,
        timeout: float = 120,
        client=None,
    ):
        if not base_url:
            raise ValueError("RERANKER_BASE_URL is required for the HTTP reranker")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_top_k = default_top_k
        self.timeout = timeout
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import httpx

            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.Client(timeout=self.timeout, headers=headers)
        return self._client

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        if not docs:
            return []
        limit = self.default_top_k if top_k is None else top_k
        response = self.client.post(
            f"{self.base_url}/rerank",
            json={
                "model": self.model,
                "query": query,
                "documents": [doc.page_content for doc in docs],
                "top_n": limit,
            },
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        ranked: list[Document] = []
        for item in results:
            index = int(item["index"])
            if 0 <= index < len(docs):
                document = docs[index]
                document.metadata = {
                    **document.metadata,
                    "rerank_score": item.get("relevance_score"),
                }
                ranked.append(document)
        return ranked[:limit]

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="reranker",
            provider="http",
            model=self.model,
            capabilities=("rerank", "remote"),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
            endpoint=self.base_url,
        )
