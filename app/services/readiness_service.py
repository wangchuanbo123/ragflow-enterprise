"""Readiness 检查服务。"""

from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timezone

from app.core.config import READINESS_CACHE_SECONDS, READINESS_TIMEOUT_SECONDS
from app.core.branding import public_metadata
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID

logger = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires_at": 0}
_cache_lock = threading.RLock()


def _check_vector_connectivity(provider) -> dict:
    """Readiness never enumerates knowledge bases/chunks or computes counts."""
    try:
        if callable(getattr(provider, "probe", None)):
            return provider.probe().as_dict()
        from rag.providers.interfaces import VectorIndexRef
        return provider.health(VectorIndexRef(name=None, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID)).as_dict()
    except Exception:
        return {"status": "fail", "detail": "向量存储连接或结构不可用"}


def audit_index_integrity(batch_size: int = 500) -> dict:
    """Explicit admin audit; bounded memory, with counts and batched ID checks."""
    from app.bootstrap.container import get_provider_container
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.index_job import KnowledgeIndexState
    from rag.providers.interfaces import VectorIndexRef
    from sqlalchemy import select
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 2000:
        raise ValueError("Invalid audit batch size")
    container = get_provider_container()
    started, checks = time.monotonic(), {}
    with container.metadata.session() as db:
        states = db.execute(select(KnowledgeBase.id, KnowledgeIndexState.active_collection_name).outerjoin(
            KnowledgeIndexState, KnowledgeIndexState.knowledge_base_id == KnowledgeBase.id
        ).where(KnowledgeBase.is_active.is_(True))).all()
        for kb_id, name in states:
            if not name:
                exists = db.scalar(select(KnowledgeChunk.id).join(
                    KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
                ).where(KnowledgeChunk.knowledge_base_id == kb_id,
                        KnowledgeDocument.knowledge_base_id == kb_id,
                        KnowledgeDocument.status.in_(["ready", "indexed"])).limit(1))
                checks[kb_id] = {"status": "fail" if exists else "ok", "detail": "尚无活动索引"}
                continue
            ref = VectorIndexRef(name=name, knowledge_base_id=kb_id)
            try:
                health = container.vector_store.health(ref).as_dict()
                if health.get("status") != "ok":
                    checks[kb_id] = health
                    continue
                store = container.vector_store.open(None, ref)
                cursor, count, missing = "", 0, []
                while True:
                    ids = list(db.scalars(select(KnowledgeChunk.id).join(
                        KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
                    ).where(KnowledgeChunk.knowledge_base_id == kb_id,
                            KnowledgeDocument.knowledge_base_id == kb_id,
                            KnowledgeDocument.status.in_(["ready", "indexed"]), KnowledgeChunk.id > cursor
                    ).order_by(KnowledgeChunk.id).limit(batch_size)))
                    if not ids:
                        break
                    actual = set(store.get(ids=ids, include=[])["ids"])
                    missing.extend(identifier for identifier in ids if identifier not in actual and len(missing) < 5)
                    count += len(ids)
                    cursor = ids[-1]
                checks[kb_id] = {**health, "chunk_count": count, "missing": missing,
                                 "status": "ok" if not missing and health.get("vector_count") == count else "fail"}
            except Exception as exc:
                # A broken collection must not discard other bases' audit results.
                # Driver errors may contain SQL, credentials or private content.
                checks[kb_id] = {"status": "fail", "code": type(exc).__name__,
                                 "detail": "索引审计失败，请检查向量存储或在索引任务完成后重试。"}
    return {"status": "ok" if all(item["status"] == "ok" for item in checks.values()) else "fail",
            "knowledge_bases": checks, "elapsed_seconds": round(time.monotonic() - started, 3),
            "checked_at": datetime.now(timezone.utc).isoformat(), "batch_size": batch_size,
            "detail": "在线审计期间发生索引切换时，请在任务完成后重新审计。"}


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
        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_chunk import KnowledgeChunk
        from app.models.knowledge_document import KnowledgeDocument
        from sqlalchemy import select
        from rag.providers.interfaces import VectorIndexRef

        container = get_provider_container()
        provider = vector_provider or container.vector_store
        checks = {}
        with container.metadata.session() as db:
            knowledge_base_ids = list(db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.is_active.is_(True))))
            if not knowledge_base_ids:
                knowledge_base_ids = [DEFAULT_KNOWLEDGE_BASE_ID]
            for knowledge_base_id in knowledge_base_ids:
                repo = KnowledgeRepository(db, knowledge_base_id)
                state = repo.get_index_state()
                chunk_ids = set(db.scalars(select(KnowledgeChunk.id).join(
                    KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
                ).where(
                    KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                    KnowledgeDocument.status.in_(["ready", "indexed"]),
                )))
                if not state.active_collection_name and not chunk_ids:
                    checks[knowledge_base_id] = {"status": "ok", "detail": "知识库为空，尚未创建索引"}
                else:
                    checks[knowledge_base_id] = provider.health(
                        VectorIndexRef(name=state.active_collection_name, knowledge_base_id=knowledge_base_id),
                        expected_ids=chunk_ids,
                    ).as_dict()
        return {
            **checks.get(DEFAULT_KNOWLEDGE_BASE_ID, {}),
            "status": "ok" if all(v["status"] == "ok" for v in checks.values()) else "fail",
            "knowledge_bases": checks,
        }
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
            def canonical(name):
                return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"

            installed = {canonical(name) for name in models}
            missing = sorted(
                model
                for model in required_models
                if canonical(model) not in installed
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

    try:
        worker = get_worker_status()
    except Exception:
        return {"status": "fail", "detail": "独立worker心跳不可用"}
    if not worker["enabled"]:
        return {"status": "ok", "detail": "disabled", **worker}
    if worker["alive"] and not worker["stopping"]:
        return {"status": "ok", **worker}
    return {"status": "fail", "detail": "Index worker is not running", **worker}


def _check_scheduler() -> dict:
    from app.core import config
    if config.APP_PROFILE != "server":
        return {"status": "ok", "detail": "not required"}
    try:
        from app.adapters.redis import get_redis_client, key_prefix, redis_call
        alive = redis_call(get_redis_client().get, key_prefix() + ":scheduler:heartbeat")
        return {"status": "ok" if alive else "fail", "detail": "alive" if alive else "独立scheduler心跳缺失"}
    except Exception:
        return {"status": "fail", "detail": "独立scheduler心跳不可用"}


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
            "embedding": container.embedding,
            "reranker": container.reranker,
        }.items():
            health = provider.health().as_dict()
            profiles[purpose] = {
                **provider.identity().as_dict(),
                "status": health.get("status", "fail"),
                **({"detail": health["detail"]} if health.get("detail") else {}),
                "verification": "configuration_only",
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
    with _cache_lock:
        return public_metadata(_check_readiness(force))


def _check_readiness(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] and now < _cache["expires_at"]:
        return _cache["data"]

    from app.bootstrap.container import get_provider_container

    container = get_provider_container()
    from app.services.model_probe_service import last_model_probe

    model_probe = last_model_probe(container)
    components = {
        "database": _check_database(container.metadata),
        "object_store": _check_provider(container.object_store),
        "task_queue": _check_provider(container.task_queue),
        "cache": _check_provider(container.cache),
        "vector_store": _check_vector_connectivity(container.vector_store),
        "model_profiles": _check_model_profiles(container),
        "ollama": _check_ollama(container),
        "index_worker": _check_index_worker(),
        "scheduler": _check_scheduler(),
        "agent_backend": _check_agent_backend(),
    }
    if model_probe:
        components["model_probe"] = {
            "status": "ok" if model_probe["status"] == "verified" else "fail",
            "profiles": model_probe["profiles"], "checked_at": model_probe["checked_at"],
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
        "model_inference_verified": bool(model_probe and model_probe["status"] == "verified"),
        "detail": "组件检查不执行模型推理；模型问答质量须单独验收。",
    }

    _cache["data"] = result
    _cache["expires_at"] = now + READINESS_CACHE_SECONDS
    return result


def invalidate_readiness_cache() -> None:
    with _cache_lock:
        _cache["data"] = None
        _cache["expires_at"] = 0


def _check_agent_backend() -> dict:
    try:
        from app.agents.factory import get_agent_backend
        return get_agent_backend().health()
    except Exception:
        return {"status": "fail", "detail": "智能体配置无效或运行时未就绪"}
