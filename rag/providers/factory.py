from functools import lru_cache

from app.core.config import (
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    GRAPH_EXTRACTION_MODEL,
    GRAPH_EXTRACTION_PROVIDER,
    GRAPH_STORE_PROVIDER,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_LLM_MODEL,
    PROVIDER_TIMEOUT_SECONDS,
    RERANK_TOP_K,
    RERANKER_MODEL,
    RERANKER_PROVIDER,
    VECTOR_STORE_PROVIDER,
)
from rag.providers.interfaces import (
    EmbeddingProvider,
    LLMProvider,
    Reranker,
    VectorStoreProvider,
)


def create_llm_provider(
    provider=LLM_PROVIDER,
    model=LLM_MODEL,
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    ollama_model=OLLAMA_LLM_MODEL,
    ollama_base_url=OLLAMA_BASE_URL,
) -> LLMProvider:
    provider = provider.lower()

    if provider == "ollama":
        from rag.providers.llm import OllamaLLMProvider

        return OllamaLLMProvider(
            model=ollama_model,
            base_url=ollama_base_url,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    if provider in {"zhipu", "zai", "glm"}:
        if not api_key.strip() or not base_url.strip():
            from rag.providers.llm import OllamaLLMProvider

            return OllamaLLMProvider(
                model=ollama_model,
                base_url=ollama_base_url,
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )

        from rag.providers.llm import ZhipuLLMProvider

        return ZhipuLLMProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    return create_llm_provider()


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    if EMBEDDING_PROVIDER == "ollama":
        from rag.providers.embedding import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider(
            model=EMBEDDING_MODEL,
            base_url=OLLAMA_BASE_URL,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    raise ValueError(f"Unsupported embedding provider: {EMBEDDING_PROVIDER}")


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    if RERANKER_PROVIDER == "bge":
        from rag.providers.reranker import BgeReranker

        return BgeReranker(model=RERANKER_MODEL, default_top_k=RERANK_TOP_K)

    if RERANKER_PROVIDER in {"none", "passthrough"}:
        from rag.providers.reranker import PassthroughReranker

        return PassthroughReranker(default_top_k=RERANK_TOP_K)

    raise ValueError(f"Unsupported reranker provider: {RERANKER_PROVIDER}")


@lru_cache(maxsize=1)
def get_vector_store_provider() -> VectorStoreProvider:
    if VECTOR_STORE_PROVIDER == "chroma":
        from rag.providers.vector_store import ChromaVectorStoreProvider

        return ChromaVectorStoreProvider()

    raise ValueError(f"Unsupported vector store provider: {VECTOR_STORE_PROVIDER}")


def get_graph_store():
    """获取图谱存储实例（每次返回新的 store，由调用方管理 session）。"""
    if GRAPH_STORE_PROVIDER == "sqlite":
        return "sqlite"
    raise ValueError(f"Unsupported graph store provider: {GRAPH_STORE_PROVIDER}")


@lru_cache(maxsize=1)
def get_graph_extraction_llm_provider() -> LLMProvider:
    """获取图谱抽取 LLM。"""
    provider = GRAPH_EXTRACTION_PROVIDER.lower()

    if provider == "current":
        return get_llm_provider()

    if provider == "ollama":
        from rag.providers.llm import OllamaLLMProvider
        return OllamaLLMProvider(
            model=GRAPH_EXTRACTION_MODEL,
            base_url=OLLAMA_BASE_URL,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    # Fall back to current LLM
    return get_llm_provider()
