"""Registration admission limits, isolated from login attempts."""

import hashlib
import threading
import time
from collections import OrderedDict, deque

from app.api.errors import APIError
from app.core.config import (
    AUTH_REGISTRATION_IP_MAX_ATTEMPTS,
    AUTH_REGISTRATION_MAX_ATTEMPTS,
    AUTH_REGISTRATION_WINDOW_SECONDS,
)

_buckets: OrderedDict[str, deque[float]] = OrderedDict()
_lock = threading.Lock()
_MAX_BUCKETS = 10000


def _keys(username: str, address: str) -> tuple[str, str]:
    digest = hashlib.sha256(username.strip().casefold().encode()).hexdigest()
    return f"account:{digest}", f"ip:{address}"


def _limited() -> APIError:
    return APIError("REGISTRATION_RATE_LIMITED", "注册申请过于频繁，请稍后再试", 429)


def check_registration_attempt(username: str, address: str) -> None:
    from app.core import config

    if config.CACHE_PROVIDER == "redis":
        from app.adapters.redis import registration_attempt_allowed

        if not registration_attempt_allowed(
            _keys(username, address), AUTH_REGISTRATION_MAX_ATTEMPTS,
            AUTH_REGISTRATION_WINDOW_SECONDS, AUTH_REGISTRATION_IP_MAX_ATTEMPTS,
        ):
            raise _limited()
        return
    if config.APP_PROFILE not in {"local", "test"}:
        from app.adapters.redis import CoordinationUnavailableError

        raise CoordinationUnavailableError("服务器注册需要 Redis 共享限流")
    now = time.monotonic()
    with _lock:
        entries = []
        for key, limit in zip(_keys(username, address), (
            AUTH_REGISTRATION_MAX_ATTEMPTS, AUTH_REGISTRATION_IP_MAX_ATTEMPTS,
        )):
            bucket = _buckets.get(key, deque())
            while bucket and bucket[0] <= now - AUTH_REGISTRATION_WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= limit:
                raise _limited()
            entries.append((key, bucket))
        for key, bucket in entries:
            _buckets[key] = bucket
            _buckets.move_to_end(key)
            bucket.append(now)
        while len(_buckets) > _MAX_BUCKETS:
            _buckets.popitem(last=False)


def reset_registration_limits() -> None:
    with _lock:
        _buckets.clear()
