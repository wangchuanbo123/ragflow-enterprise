"""Readiness 检查服务。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.core.config import READINESS_CACHE_SECONDS, READINESS_TIMEOUT_SECONDS
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID

logger = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires_at": 0}


def _check_database(metadata_provider=None) -> dict:
    try:
        from app.bootstrap.container import get_provider_container

        provider = metadata_provider or get_provider_container().metadata
        return provider.health().as_dict()
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_vector_store(vector_provider=None) -> dict:
    try:
        from app.bootstrap.container import get_provider_container
        from app.repositories.knowledge_repository import KnowledgeRepository
        from rag.providers.interfaces import VectorIndexRef

        container = get_provider_container()
        with container.metadata.session() as db:
            repo = KnowledgeRepository(db)
            state = repo.get_index_state()
            active = state.active_collection_name
            chunk_ids = {
                chunk.id for chunk in repo.list_all_active_chunks()
            }

        provider = vector_provider or container.vector_store
        return provider.health(
            VectorIndexRef(
                name=active,
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            ),
            expected_ids=chunk_ids,
        ).as_dict()
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_ollama(container=None) -> dict:
    try:
        import httpx
        from app.bootstrap.container import get_provider_container
        from app.core.config import OLLAMA_BASE_URL

        container = container or get_provider_container()

        required_models: set[str] = set()
        providers = (
            container.llm,
            container.rewrite_llm,
            container.router_llm,
            container.extraction_llm,
            container.embedding,
        )
        for provider in providers:
            identity = provider.identity()
            if identity.provider == "ollama" and identity.model:
                required_models.add(identity.model)

        if not required_models:
            return {"status": "ok", "detail": "not required", "models": []}

        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=READINESS_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            model_bases = {name.split(":", 1)[0] for name in models}
            missing = sorted(
                model
                for model in required_models
                if model not in models and model.split(":", 1)[0] not in model_bases
            )
            if missing:
                return {
                    "status": "fail",
                    "models": models,
                    "required_models": sorted(required_models),
                    "detail": f"Missing Ollama models: {', '.join(missing)}",
                }
            return {
                "status": "ok",
                "models": models,
                "required_models": sorted(required_models),
            }
        return {"status": "fail", "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_index_worker() -> dict:
    from app.services.index_worker import get_worker_status

    worker = get_worker_status()
    if not worker["enabled"]:
        return {"status": "ok", "detail": "disabled", **worker}
    if worker["alive"] and not worker["stopping"]:
        return {"status": "ok", **worker}
    return {"status": "fail", "detail": "Index worker is not running", **worker}


def _check_provider(provider) -> dict:
    try:
        return provider.health().as_dict()
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_model_profiles(container) -> dict:
    try:
        profiles = {}
        for purpose, provider in {
            "answer": container.llm,
            "rewrite": container.rewrite_llm,
            "router": container.router_llm,
            "extraction": container.extraction_llm,
            "embedding": container.embedding,
            "reranker": container.reranker,
        }.items():
            health = provider.health().as_dict()
            profiles[purpose] = {
                **provider.identity().as_dict(),
                "status": health.get("status", "fail"),
                **({"detail": health["detail"]} if health.get("detail") else {}),
            }
        status = (
            "ok"
            if all(item["status"] == "ok" for item in profiles.values())
            else "fail"
        )
        return {"status": status, "profiles": profiles}
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def check_readiness(force: bool = False) -> dict:
    """检查所有组件就绪状态。结果缓存 READINESS_CACHE_SECONDS 秒。"""
    now = time.time()
    if not force and _cache["data"] and now < _cache["expires_at"]:
        return _cache["data"]

    from app.bootstrap.container import get_provider_container

    container = get_provider_container()
    components = {
        "database": _check_database(container.metadata),
        "object_store": _check_provider(container.object_store),
        "task_queue": _check_provider(container.task_queue),
        "cache": _check_provider(container.cache),
        "vector_store": _check_vector_store(),
        "model_profiles": _check_model_profiles(container),
        "ollama": _check_ollama(container),
        "index_worker": _check_index_worker(),
    }

    status = (
        "ready"
        if all(c.get("status") == "ok" for c in components.values())
        else "not_ready"
    )
    result = {
        "status": status,
        "components": components,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    _cache["data"] = result
    _cache["expires_at"] = now + READINESS_CACHE_SECONDS
    return result


def invalidate_readiness_cache() -> None:
    _cache["data"] = None
    _cache["expires_at"] = 0
