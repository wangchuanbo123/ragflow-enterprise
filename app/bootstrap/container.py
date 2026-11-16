"""Provider dependency container shared by API, workers, and RAG runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.adapters.interfaces import (
    CacheProvider,
    MetadataSessionProvider,
    ObjectStoreProvider,
    TaskQueueProvider,
)
from app.core.config import (
    APP_PROFILE,
    CACHE_PROVIDER,
    REDIS_URL,
    REDIS_KEY_PREFIX,
    GRAPH_STORE_PROVIDER,
    LOCAL_OBJECT_STORE_DIR,
    METADATA_STORE_PROVIDER,
    OBJECT_STORE_PROVIDER,
    TASK_QUEUE_PROVIDER,
    VECTOR_STORE_PROVIDER,
)
from rag.providers.interfaces import (
    EmbeddingProvider,
    LLMProvider,
    Reranker,
    VectorStoreProvider,
)


def validate_provider_configuration(
    *,
    profile: str,
    metadata_store: str,
    vector_store: str,
    object_store: str,
    task_queue: str,
    cache: str,
    graph_store: str = "sqlalchemy",
) -> None:
    if profile not in {"local", "test", "server"}:
        raise ValueError(f"Unsupported application profile: {profile}")

    allowed = {
        "METADATA_STORE_PROVIDER": (metadata_store, {"sqlite", "postgresql"}),
        "VECTOR_STORE_PROVIDER": (vector_store, {"chroma", "chroma_embedded", "pgvector"}),
        "OBJECT_STORE_PROVIDER": (object_store, {"local"}),
        "TASK_QUEUE_PROVIDER": (task_queue, {"inline", "in_process", "redis"}),
        "CACHE_PROVIDER": (cache, {"memory", "redis"}),
        "GRAPH_STORE_PROVIDER": (graph_store, {"sqlite", "sqlalchemy"}),
    }
    invalid = [
        f"{name}={value}"
        for name, (value, supported) in allowed.items()
        if value not in supported
    ]
    if invalid:
        raise ValueError(
            "Unsupported provider configuration: " + ", ".join(invalid)
        )
    if profile == "server" and (
        metadata_store != "postgresql" or vector_store != "pgvector"
        or task_queue != "redis" or cache != "redis" or graph_store != "sqlalchemy"
    ):
        raise ValueError("Server profile requires PostgreSQL, pgvector, Redis and SQLAlchemy graph storage")


@dataclass(frozen=True)
class ProviderContainer:
    """Runtime provider set; concrete SDK clients remain inside adapters."""

    profile: str
    llm: LLMProvider
    rewrite_llm: LLMProvider
    router_llm: LLMProvider
    extraction_llm: LLMProvider
    embedding: EmbeddingProvider
    reranker: Reranker
    vector_store: VectorStoreProvider
    metadata: MetadataSessionProvider
    object_store: ObjectStoreProvider
    task_queue: TaskQueueProvider
    cache: CacheProvider

    def graph_store(self, db, knowledge_base_id=None):
        from rag.providers.factory import get_graph_store

        return get_graph_store(db, knowledge_base_id=knowledge_base_id)


@lru_cache(maxsize=1)
def get_provider_container() -> ProviderContainer:
    validate_provider_configuration(
        profile=APP_PROFILE,
        metadata_store=METADATA_STORE_PROVIDER,
        vector_store=VECTOR_STORE_PROVIDER,
        object_store=OBJECT_STORE_PROVIDER,
        task_queue=TASK_QUEUE_PROVIDER,
        cache=CACHE_PROVIDER,
        graph_store=GRAPH_STORE_PROVIDER,
    )

    from app.adapters.local import (
        InlineTaskQueue,
        LocalObjectStore,
        MemoryCache,
        SQLAlchemyMetadataSessionProvider,
    )
    from app.core import database
    from rag.providers.factory import (
        get_embedding_provider,
        get_llm_provider,
        get_reranker,
        get_vector_store_provider,
    )
    if TASK_QUEUE_PROVIDER == "redis" or CACHE_PROVIDER == "redis":
        from app.adapters.redis import RedisTaskQueue, RedisCache

    return ProviderContainer(
        profile=APP_PROFILE,
        llm=get_llm_provider("answer"),
        rewrite_llm=get_llm_provider("rewrite"),
        router_llm=get_llm_provider("router"),
        # Compatibility for explicitly injected legacy consumers. Production
        # knowledge reasoning resolves AgentBackend independently and must not
        # fail because an unused historical extraction-model setting changed.
        extraction_llm=get_llm_provider("answer"),
        embedding=get_embedding_provider(),
        reranker=get_reranker(),
        vector_store=get_vector_store_provider(),
        metadata=SQLAlchemyMetadataSessionProvider(
            lambda: database.SessionLocal(),
            provider_name=METADATA_STORE_PROVIDER,
        ),
        object_store=LocalObjectStore(LOCAL_OBJECT_STORE_DIR),
        task_queue=RedisTaskQueue(REDIS_URL, prefix=REDIS_KEY_PREFIX) if TASK_QUEUE_PROVIDER == "redis" else InlineTaskQueue(),
        cache=RedisCache(REDIS_URL, prefix=REDIS_KEY_PREFIX) if CACHE_PROVIDER == "redis" else MemoryCache(),
    )


def invalidate_provider_container() -> None:
    get_provider_container.cache_clear()
