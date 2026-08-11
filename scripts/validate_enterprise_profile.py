"""Validate an enterprise provider profile without migrating document content."""

from __future__ import annotations

import argparse
import json
import sys
import uuid


def _health(provider) -> dict:
    try:
        return provider.health().as_dict()
    except Exception as exc:
        return {"status": "fail", "detail": str(exc)[:500]}


def validate(write_check: bool = False) -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext

    from app.bootstrap.container import get_provider_container
    from app.core import config
    from app.core.database import engine
    from rag.providers.interfaces import VectorIndexRef

    container = get_provider_container()
    components = {
        "metadata": _health(container.metadata),
        "object_store": _health(container.object_store),
        "task_queue": _health(container.task_queue),
        "cache": _health(container.cache),
        "answer_model": _health(container.llm),
        "embedding_model": _health(container.embedding),
        "reranker": _health(container.reranker),
    }
    try:
        indexes = container.vector_store.list_indexes()
        components["vector_store"] = {
            "status": "ok",
            "indexes": len(indexes),
            "provider": container.vector_store.identity().provider,
        }
    except Exception as exc:
        components["vector_store"] = {"status": "fail", "detail": str(exc)[:500]}

    alembic_config = Config("alembic.ini")
    expected = ScriptDirectory.from_config(alembic_config).get_current_head()
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        components["migrations"] = {
            "status": "ok" if current == expected else "fail",
            "current": current,
            "expected": expected,
        }
    except Exception as exc:
        components["migrations"] = {"status": "fail", "detail": str(exc)[:500]}

    writes = {}
    if write_check:
        marker = uuid.uuid4().hex
        object_key = f"enterprise-validation/{marker}.txt"
        try:
            container.object_store.put_bytes(object_key, marker.encode())
            visible = container.object_store.exists(object_key)
            container.object_store.delete(object_key)
            writes["object_store"] = {"status": "ok" if visible else "fail"}
        except Exception as exc:
            writes["object_store"] = {"status": "fail", "detail": str(exc)[:500]}
        try:
            cache_key = f"validation:{marker}"
            container.cache.set(cache_key, marker.encode(), ttl_seconds=60)
            visible = container.cache.get(cache_key) == marker.encode()
            container.cache.delete(cache_key)
            writes["cache"] = {"status": "ok" if visible else "fail"}
        except Exception as exc:
            writes["cache"] = {"status": "fail", "detail": str(exc)[:500]}

    configured = {
        "profile": config.APP_PROFILE,
        "metadata": container.metadata.identity().as_dict(),
        "vector": container.vector_store.identity().as_dict(),
        "object_store": container.object_store.identity().as_dict(),
        "task_queue": container.task_queue.identity().as_dict(),
        "cache": container.cache.identity().as_dict(),
        "graph_store": config.GRAPH_STORE_PROVIDER,
        "models": {
            "answer": container.llm.identity().as_dict(),
            "rewrite": container.rewrite_llm.identity().as_dict(),
            "router": container.router_llm.identity().as_dict(),
            "extraction": container.extraction_llm.identity().as_dict(),
            "embedding": container.embedding.identity().as_dict(),
        },
    }
    all_checks = list(components.values()) + list(writes.values())
    return {
        "status": "ready" if all(item.get("status") == "ok" for item in all_checks) else "not_ready",
        "configured": configured,
        "components": components,
        "write_checks": writes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-check", action="store_true")
    args = parser.parse_args()
    result = validate(args.write_check)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
