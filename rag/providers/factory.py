from functools import lru_cache

from app.core.config import (
    APP_PROFILE,
    CHROMA_HTTP_AUTH_TOKEN,
    CHROMA_HTTP_DATABASE,
    CHROMA_HTTP_HOST,
    CHROMA_HTTP_PORT,
    CHROMA_HTTP_SSL,
    CHROMA_HTTP_TENANT,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    GRAPH_STORE_PROVIDER,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_LLM_MODEL,
    PROVIDER_TIMEOUT_SECONDS,
    RERANK_TOP_K,
    RERANKER_API_KEY,
    RERANKER_BASE_URL,
    RERANKER_MODEL,
    RERANKER_PROVIDER,
    VECTOR_DB_DIR,
    VECTOR_STORE_PROVIDER,
)
from rag.providers.interfaces import (
    EmbeddingProvider,
    LLMProvider,
    Reranker,
    VectorStoreProvider,
)
from rag.providers.profiles import get_llm_profile


def create_llm_provider(
    provider=LLM_PROVIDER,
    model=LLM_MODEL,
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    ollama_model=OLLAMA_LLM_MODEL,
    ollama_base_url=OLLAMA_BASE_URL,
    allow_ollama_fallback: bool | None = None,
) -> LLMProvider:
    provider = provider.lower()
    if allow_ollama_fallback is None:
        allow_ollama_fallback = APP_PROFILE != "enterprise"

    if provider == "ollama":
        from rag.providers.llm import OllamaLLMProvider

        return OllamaLLMProvider(
            model=ollama_model,
            base_url=ollama_base_url,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    if provider in {
        "zhipu",
        "zai",
        "glm",
        "openai",
        "openai_compatible",
        "anthropic",
        "anthropic_compatible",
    }:
        if not api_key.strip() or not base_url.strip():
            if not allow_ollama_fallback:
                raise ValueError(
                    "Enterprise profile requires explicit LLM_API_KEY and "
                    "LLM_BASE_URL; implicit Ollama fallback is disabled"
                )
            from rag.providers.llm import OllamaLLMProvider

            return OllamaLLMProvider(
                model=ollama_model,
                base_url=ollama_base_url,
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )

        if provider in {"anthropic", "anthropic_compatible"}:
            from rag.providers.llm import AnthropicCompatibleLLMProvider as ProviderClass
        elif provider in {"zhipu", "zai", "glm"}:
            from rag.providers.llm import ZhipuLLMProvider as ProviderClass
        else:
            from rag.providers.llm import OpenAICompatibleLLMProvider as ProviderClass

        return ProviderClass(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


@lru_cache(maxsize=8)
def get_llm_provider(purpose: str = "answer") -> LLMProvider:
    profile = get_llm_profile(purpose)
    provider = create_llm_provider(
        provider=profile.provider,
        model=profile.model,
        api_key=profile.api_key,
        base_url=profile.base_url,
    )
    identity = provider.identity()
    missing = [
        item
        for item in profile.required_capabilities
        if item not in identity.capabilities
    ]
    if missing:
        raise ValueError(
            f"Model profile {purpose!r} is missing capabilities: {missing}"
        )
    return provider


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    if EMBEDDING_PROVIDER == "ollama":
        from rag.providers.embedding import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider(
            model=EMBEDDING_MODEL,
            base_url=OLLAMA_BASE_URL,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    if EMBEDDING_PROVIDER in {"openai", "openai_compatible"}:
        from rag.providers.embedding import OpenAICompatibleEmbeddingProvider

        return OpenAICompatibleEmbeddingProvider(
            model=EMBEDDING_MODEL,
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
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

    if RERANKER_PROVIDER in {"http", "remote"}:
        from rag.providers.reranker import HttpReranker

        return HttpReranker(
            model=RERANKER_MODEL,
            base_url=RERANKER_BASE_URL,
            api_key=RERANKER_API_KEY,
            default_top_k=RERANK_TOP_K,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

    raise ValueError(f"Unsupported reranker provider: {RERANKER_PROVIDER}")


@lru_cache(maxsize=1)
def get_vector_store_provider() -> VectorStoreProvider:
    if VECTOR_STORE_PROVIDER in {"chroma", "chroma_embedded"}:
        from rag.providers.vector_store import ChromaVectorStoreProvider

        return ChromaVectorStoreProvider(persist_dir=VECTOR_DB_DIR)

    if VECTOR_STORE_PROVIDER in {"chroma_http", "chroma_server"}:
        from rag.providers.vector_store import ChromaHttpVectorStoreProvider

        return ChromaHttpVectorStoreProvider(
            host=CHROMA_HTTP_HOST,
            port=CHROMA_HTTP_PORT,
            ssl=CHROMA_HTTP_SSL,
            tenant=CHROMA_HTTP_TENANT,
            database=CHROMA_HTTP_DATABASE,
            auth_token=CHROMA_HTTP_AUTH_TOKEN,
        )

    raise ValueError(f"Unsupported vector store provider: {VECTOR_STORE_PROVIDER}")


def get_graph_store(db, knowledge_base_id=None):
    """Create a session-scoped graph store adapter."""
    if GRAPH_STORE_PROVIDER in {"sqlite", "sqlalchemy", "postgres", "postgresql"}:
        from rag.knowledge_graph.sqlite_store import SqliteGraphStore

        return SqliteGraphStore(db, knowledge_base_id=knowledge_base_id)
    raise ValueError(f"Unsupported graph store provider: {GRAPH_STORE_PROVIDER}")


@lru_cache(maxsize=1)
def get_graph_extraction_llm_provider() -> LLMProvider:
    """获取图谱抽取 LLM。"""
    return get_llm_provider("extraction")


@lru_cache(maxsize=1)
def get_graph_escalation_llm_provider() -> LLMProvider:
    """获取图谱抽取升级 LLM（空结果/低置信度重试）。"""
    return get_llm_provider("graph_escalation")
