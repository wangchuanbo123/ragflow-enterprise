"""Explicit, cached inference probes with small inputs and bounded timeouts."""

import copy
import hashlib
import math
import json
import logging
import threading
import time
from datetime import datetime, timezone

from langchain_core.documents import Document

_lock = threading.Lock()
_cached = None
_cached_key = None
_expires_at = 0.0
logger = logging.getLogger(__name__)


def _profile_key(container):
    return tuple((name, hashlib.sha256(json.dumps({
        "identity": getattr(container, name).identity().as_dict(),
        "endpoint": getattr(getattr(container, name), "base_url", ""),
        "credential": getattr(getattr(container, name), "api_key", ""),
    }, sort_keys=True).encode()).hexdigest()) for name in ("llm", "embedding", "reranker"))


def last_model_probe(container=None):
    if container is None:
        from app.bootstrap.container import get_provider_container

        container = get_provider_container()
    if _cached_key != _profile_key(container) or time.monotonic() >= _expires_at:
        return None
    return copy.deepcopy(_cached)


def _probe_one(kind, provider):
    started = time.perf_counter()
    probe = copy.copy(provider)
    if hasattr(probe, "timeout"):
        probe.timeout = 10
    # Do not share or mutate cached SDK clients with normal requests.
    for key in ("_model", "_client"):
        if key in vars(probe) and provider.identity().provider != "bge_local":
            vars(probe).pop(key, None)
    if hasattr(provider, "_client"):
        probe._client = None
    if hasattr(probe, "max_tokens"):
        probe.max_tokens = 32
    result = {**provider.identity().as_dict(), "status": "fail"}
    sdk_clients = []

    def model_without_retries():
        model = probe.get_model()
        if hasattr(model, "max_retries"):
            model.max_retries = 0
        for resource in (getattr(model, "client", None), getattr(model, "async_client", None)):
            sdk = getattr(resource, "_client", None)
            if sdk is not None:
                sdk.max_retries = 0
                sdk_clients.append(sdk)
        return model

    try:
        if kind == "answer":
            model = model_without_retries()
            if hasattr(model, "max_tokens"):
                model.max_tokens = 32
            if hasattr(model, "num_predict"):
                model.num_predict = 32
            valid = bool(probe.generate("Reply with the single word OK.").strip())
        elif kind == "embedding":
            vector = model_without_retries().embed_query("local acceptance probe")
            valid = bool(vector) and all(math.isfinite(float(value)) for value in vector)
        else:
            if provider.identity().provider == "bge_local":
                result["error_type"] = "LOCAL_MODEL_REQUIRES_ACCEPTANCE"
                result["detail"] = "本地重排模型需通过实际问答单独验收；诊断不会下载或运行本地模型。"
                return result
            valid = len(probe.rerank("probe", [Document(page_content="acceptance probe")])) == 1
        result["status"] = "ok" if valid else "fail"
        if not valid:
            result["error_type"] = "INVALID_MODEL_OUTPUT"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["http_status"] = getattr(getattr(exc, "response", None), "status_code", None)
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        client = vars(probe).get("_client")
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                logger.warning("Could not close model probe HTTP client")
        import asyncio
        import inspect

        for sdk in sdk_clients:
            try:
                closed = sdk.close()
                if inspect.isawaitable(closed):
                    asyncio.run(closed)
            except Exception:
                logger.warning("Could not close model probe SDK client")
    return result


def probe_models(container=None):
    global _cached, _cached_key, _expires_at
    from concurrent.futures import ThreadPoolExecutor, wait
    from app.bootstrap.container import get_provider_container
    from app.services.readiness_service import invalidate_readiness_cache

    container = container or get_provider_container()
    with _lock:
        cached = last_model_probe(container)
        if cached is not None:
            return cached
        executor = ThreadPoolExecutor(max_workers=3)
        jobs = {
            "answer": executor.submit(_probe_one, "answer", container.llm),
            "embedding": executor.submit(_probe_one, "embedding", container.embedding),
            "reranker": executor.submit(_probe_one, "reranker", container.reranker),
        }
        done, pending = wait(jobs.values(), timeout=20)
        profiles = {}
        for name, future in jobs.items():
            if future in done:
                profiles[name] = future.result()
            else:
                future.cancel()
                profiles[name] = {"status": "fail", "error_type": "PROBE_TIMEOUT", "elapsed_ms": 20000}
        executor.shutdown(wait=False, cancel_futures=True)
        _cached = {
            "status": "verified" if all(p["status"] == "ok" for p in profiles.values()) else "failed",
            "profiles": profiles,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _cached_key = _profile_key(container)
        _expires_at = time.monotonic() + 60
        invalidate_readiness_cache()
        return copy.deepcopy(_cached)
