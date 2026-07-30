from typing import Any, Iterator, List, Protocol, Sequence

from langchain_core.documents import Document


class LLMProvider(Protocol):
    def generate(self, prompt: str) -> str:
        """Generate text for a prompt."""

    def stream(self, prompt: str) -> Iterator[str]:
        """Yield text chunks for a prompt as a real stream."""

    def get_model(self) -> Any:
        """Return the underlying LangChain model when an integration needs it."""


class EmbeddingProvider(Protocol):
    def get_model(self) -> Any:
        """Return a LangChain-compatible embedding model."""


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        docs: Sequence[Document],
        top_k: int | None = None,
    ) -> List[Document]:
        """Return documents ordered by relevance."""


class VectorStoreProvider(Protocol):
    def load(self, embedding: Any, persist_dir: str, collection_name: str | None = None) -> Any:
        """Load or create a vector store."""


class Retriever(Protocol):
    def get_relevant_documents(self, query: str) -> List[Document]:
        """Retrieve documents relevant to a query."""
