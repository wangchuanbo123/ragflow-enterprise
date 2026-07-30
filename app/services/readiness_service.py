"""Readiness 检查服务。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.core.config import READINESS_CACHE_SECONDS, READINESS_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_cache: dict = {"data": None, "expires_at": 0}


def _check_database() -> dict:
    try:
        from app.core.database import SessionLocal
        with SessionLocal() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_vector_store() -> dict:
    try:
        import chromadb
        from app.core.config import VECTOR_DB_DIR
        from app.core.database import SessionLocal
        from app.repositories.knowledge_repository import KnowledgeRepository

        client = chromadb.PersistentClient(path=str(VECTOR_DB_DIR))

        with SessionLocal() as db:
            repo = KnowledgeRepository(db)
            state = repo.get_index_state()
            active = state.active_collection_name
            chunk_ids = {
                chunk.id for chunk in repo.list_all_active_chunks()
            }

        if not active:
            return {"status": "fail", "detail": "No active collection configured"}

        try:
            collection = client.get_collection(active)
        except Exception:
            return {
                "status": "fail",
                "detail": f"Active collection '{active}' not found",
            }

        vector_count = collection.count()
        chunk_count = len(chunk_ids)
        result = {
            "collection": active,
            "vector_count": vector_count,
            "chunk_count": chunk_count,
        }
        if vector_count != chunk_count:
            return {
                **result,
                "status": "fail",
                "detail": (
                    f"Vector count ({vector_count}) != active SQLite "
                    f"chunk count ({chunk_count})"
                ),
            }

        vector_ids = set(collection.get(include=[])["ids"])
        if vector_ids != chunk_ids:
            missing = sorted(chunk_ids - vector_ids)[:5]
            unexpected = sorted(vector_ids - chunk_ids)[:5]
            return {
                **result,
                "status": "fail",
                "detail": (
                    "Vector IDs do not match active SQLite chunk IDs; "
                    f"missing={missing}, unexpected={unexpected}"
                ),
            }

        return {**result, "status": "ok"}
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:200]}


def _check_ollama() -> dict:
    try:
        import httpx
        from app.core.config import (
            EMBEDDING_MODEL,
            EMBEDDING_PROVIDER,
            GRAPH_EXTRACTION_MODEL,
            GRAPH_EXTRACTION_PROVIDER,
            LLM_API_KEY,
            LLM_BASE_URL,
            LLM_PROVIDER,
            OLLAMA_BASE_URL,
            OLLAMA_LLM_MODEL,
        )

        required_models: set[str] = set()
        if EMBEDDING_PROVIDER == "ollama":
            required_models.add(EMBEDDING_MODEL)
        if LLM_PROVIDER == "ollama" or (
            LLM_PROVIDER in {"zhipu", "zai", "glm"}
            and (not LLM_API_KEY.strip() or not LLM_BASE_URL.strip())
        ):
            required_models.add(OLLAMA_LLM_MODEL)
        if GRAPH_EXTRACTION_PROVIDER == "ollama":
            required_models.add(GRAPH_EXTRACTION_MODEL)

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


def check_readiness(force: bool = False) -> dict:
    """检查所有组件就绪状态。结果缓存 READINESS_CACHE_SECONDS 秒。"""
    now = time.time()
    if not force and _cache["data"] and now < _cache["expires_at"]:
        return _cache["data"]

    components = {
        "database": _check_database(),
        "vector_store": _check_vector_store(),
        "ollama": _check_ollama(),
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
