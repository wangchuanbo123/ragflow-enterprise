from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, List, Mapping, Protocol, Sequence

from langchain_core.documents import Document


@dataclass(frozen=True)
class ProviderIdentity:
    """Stable, vendor-neutral description exposed to diagnostics and traces."""

    kind: str
    provider: str
    model: str | None = None
    revision: str | None = None
    capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "revision": self.revision,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class ProviderHealth:
    """Normalized health result returned by infrastructure adapters."""

    status: str
    detail: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **metadata: Any) -> "ProviderHealth":
        return cls(status="ok", metadata=metadata)

    @classmethod
    def fail(cls, detail: str, **metadata: Any) -> "ProviderHealth":
        return cls(status="fail", detail=detail, metadata=metadata)

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.metadata)
        result["status"] = self.status
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class VectorIndexRef:
    """Application-owned reference to a logical vector index version."""

    name: str | None = None
    knowledge_base_id: str | None = None
    index_version: str | None = None
    embedding_profile_id: str | None = None


@dataclass(frozen=True)
class VectorIndexInfo:
    """Provider-neutral vector index listing result."""

    ref: VectorIndexRef
    count: int


class LLMProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the model and supported generation capabilities."""

    def health(self) -> ProviderHealth:
        """Validate model profile configuration without generating content."""

    def generate(self, prompt: str) -> str:
        """Generate text for a prompt."""

    def stream(self, prompt: str) -> Iterator[str]:
        """Yield text chunks for a prompt as a real stream."""

    def get_model(self) -> Any:
        """Return the underlying LangChain model when an integration needs it."""


class EmbeddingProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the embedding model and supported operations."""

    def health(self) -> ProviderHealth:
        """Validate embedding profile configuration."""

    def get_model(self) -> Any:
        """Return a LangChain-compatible embedding model."""


class Reranker(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the reranker implementation."""

    def health(self) -> ProviderHealth:
        """Validate reranker profile configuration."""

    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        """Return documents ordered by relevance."""


class VectorStoreProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Return a stable description without leaking a vendor client."""

    def open(self, embedding: Any, index_ref: VectorIndexRef) -> Any:
        """Open or create a logical vector index."""

    def health(
        self,
        index_ref: VectorIndexRef,
        expected_ids: set[str] | None = None,
    ) -> ProviderHealth:
        """Check connectivity and, when provided, index ID consistency."""

    def list_indexes(self) -> list[VectorIndexInfo]:
        """List logical indexes without returning vendor collection objects."""

    def delete_index(self, index_ref: VectorIndexRef) -> None:
        """Delete one logical index."""

    def count(self, index_ref: VectorIndexRef) -> int:
        """Return the number of vectors in an index."""

    def list_ids(
        self,
        index_ref: VectorIndexRef,
        where: Mapping[str, Any] | None = None,
    ) -> set[str]:
        """Return vector IDs, optionally filtered by metadata."""

    def delete_vectors(
        self,
        index_ref: VectorIndexRef,
        *,
        ids: Sequence[str] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> None:
        """Delete vectors by IDs or metadata filter."""


class Retriever(Protocol):
    def get_relevant_documents(self, query: str) -> List[Document]:
        """Retrieve documents relevant to a query."""
