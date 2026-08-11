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
    CELERY_BROKER_URL,
    CELERY_INDEX_QUEUE,
    CELERY_INDEX_TASK_NAME,
    CELERY_RESULT_BACKEND,
    LOCAL_OBJECT_STORE_DIR,
    GRAPH_STORE_PROVIDER,
    METADATA_STORE_PROVIDER,
    OBJECT_STORE_PROVIDER,
    REDIS_CACHE_PREFIX,
    REDIS_URL,
    S3_ACCESS_KEY,
    S3_ADDRESSING_STYLE,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_PREFIX,
    S3_REGION,
    S3_SECRET_KEY,
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
    """Fail fast when an enterprise profile points at local-only adapters."""

    if profile not in {"local", "test", "enterprise"}:
        raise ValueError(f"Unsupported application profile: {profile}")

    if profile != "enterprise":
        return

    local_only = {
        "METADATA_STORE_PROVIDER": (metadata_store, {"sqlite"}),
        "VECTOR_STORE_PROVIDER": (
            vector_store,
            {"chroma", "chroma_embedded"},
        ),
        "OBJECT_STORE_PROVIDER": (object_store, {"local"}),
        "TASK_QUEUE_PROVIDER": (task_queue, {"inline", "in_process"}),
        "CACHE_PROVIDER": (cache, {"memory"}),
        "GRAPH_STORE_PROVIDER": (graph_store, {"sqlite"}),
    }
    invalid = [
        f"{name}={value}"
        for name, (value, forbidden) in local_only.items()
        if value in forbidden
    ]
    if invalid:
        raise ValueError(
            "Enterprise profile cannot use local-only providers: "
            + ", ".join(invalid)
        )


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

    if OBJECT_STORE_PROVIDER == "local":
        object_store = LocalObjectStore(LOCAL_OBJECT_STORE_DIR)
    elif OBJECT_STORE_PROVIDER in {"s3", "minio"}:
        from app.adapters.remote import S3ObjectStore

        object_store = S3ObjectStore(
            bucket=S3_BUCKET,
            endpoint_url=S3_ENDPOINT_URL,
            access_key=S3_ACCESS_KEY,
            secret_key=S3_SECRET_KEY,
            region=S3_REGION,
            prefix=S3_PREFIX,
            addressing_style=S3_ADDRESSING_STYLE,
        )
    else:
        raise ValueError(
            f"Unsupported object store adapter: {OBJECT_STORE_PROVIDER}"
        )

    if TASK_QUEUE_PROVIDER in {"inline", "in_process"}:
        task_queue = InlineTaskQueue()
    elif TASK_QUEUE_PROVIDER == "celery":
        from app.adapters.remote import CeleryTaskQueue

        task_queue = CeleryTaskQueue(
            broker_url=CELERY_BROKER_URL,
            result_backend=CELERY_RESULT_BACKEND,
            queue_name=CELERY_INDEX_QUEUE,
            task_name=CELERY_INDEX_TASK_NAME,
        )
    else:
        raise ValueError(
            f"Unsupported task queue adapter: {TASK_QUEUE_PROVIDER}"
        )

    if CACHE_PROVIDER == "memory":
        cache = MemoryCache()
    elif CACHE_PROVIDER == "redis":
        from app.adapters.remote import RedisCache

        cache = RedisCache(REDIS_URL, prefix=REDIS_CACHE_PREFIX)
    else:
        raise ValueError(f"Unsupported cache adapter: {CACHE_PROVIDER}")
    if METADATA_STORE_PROVIDER not in {"sqlite", "postgres", "postgresql"}:
        raise ValueError(
            f"Unsupported metadata store adapter: {METADATA_STORE_PROVIDER}"
        )

    return ProviderContainer(
        profile=APP_PROFILE,
        llm=get_llm_provider("answer"),
        rewrite_llm=get_llm_provider("rewrite"),
        router_llm=get_llm_provider("router"),
        extraction_llm=get_llm_provider("extraction"),
        embedding=get_embedding_provider(),
        reranker=get_reranker(),
        vector_store=get_vector_store_provider(),
        metadata=SQLAlchemyMetadataSessionProvider(
            lambda: database.SessionLocal(),
            provider_name=METADATA_STORE_PROVIDER,
        ),
        object_store=object_store,
        task_queue=task_queue,
        cache=cache,
    )


def invalidate_provider_container() -> None:
    get_provider_container.cache_clear()
