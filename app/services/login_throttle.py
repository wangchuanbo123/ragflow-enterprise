"""Shared Redis login throttling; bounded local buckets for offline profiles."""

import hashlib
import threading
import time
from collections import OrderedDict, deque

from app.api.errors import APIError
from app.core.config import AUTH_LOGIN_MAX_ATTEMPTS, AUTH_LOGIN_WINDOW_SECONDS

_buckets: OrderedDict[str, deque[float]] = OrderedDict()
_lock = threading.Lock()
_MAX_BUCKETS = 10000


def _keys(username: str, address: str) -> tuple[str, str]:
    name = hashlib.sha256(username.strip().casefold().encode()).hexdigest()
    return f"account:{name}", f"ip:{address}"


def check_login_attempt(username: str, address: str) -> None:
    from app.core import config
    if config.CACHE_PROVIDER == "redis":
        from app.adapters.redis import login_attempt_allowed
        if not login_attempt_allowed(_keys(username, address), AUTH_LOGIN_MAX_ATTEMPTS, AUTH_LOGIN_WINDOW_SECONDS,
                                     getattr(config, "AUTH_LOGIN_IP_MAX_ATTEMPTS", AUTH_LOGIN_MAX_ATTEMPTS * 6)):
            raise APIError("LOGIN_RATE_LIMITED", "登录尝试过于频繁，请稍后再试", 429)
        return
    if config.APP_PROFILE not in {"local", "test"}:
        from app.adapters.redis import CoordinationUnavailableError
        raise CoordinationUnavailableError("服务器登录需要Redis共享限流")
    now = time.monotonic()
    with _lock:
        entries = []
        for key, limit in zip(_keys(username, address), (AUTH_LOGIN_MAX_ATTEMPTS,
                            getattr(config, "AUTH_LOGIN_IP_MAX_ATTEMPTS", AUTH_LOGIN_MAX_ATTEMPTS * 6))):
            bucket = _buckets.setdefault(key, deque())
            while bucket and bucket[0] <= now - AUTH_LOGIN_WINDOW_SECONDS:
                bucket.popleft()
            _buckets.move_to_end(key)
            if len(bucket) >= limit:
                raise APIError("LOGIN_RATE_LIMITED", "登录尝试过于频繁，请稍后再试", 429)
            entries.append(bucket)
        for bucket in entries:
            bucket.append(now)
        while len(_buckets) > _MAX_BUCKETS:
            _buckets.popitem(last=False)


def clear_account_failures(username: str, address: str) -> None:
    from app.core import config
    if config.CACHE_PROVIDER == "redis":
        from app.adapters.redis import clear_login_account
        clear_login_account(_keys(username, address)[0])
        return
    with _lock:
        _buckets.pop(_keys(username, address)[0], None)


def reset_login_limits() -> None:
    with _lock:
        _buckets.clear()
