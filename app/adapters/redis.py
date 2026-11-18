"""Shared Redis adapters. PostgreSQL, not the wakeup list, owns job state."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from functools import lru_cache

from rag.errors import RAGError
from rag.providers.interfaces import ProviderHealth, ProviderIdentity


class CoordinationUnavailableError(RAGError):
    code = "COORDINATION_UNAVAILABLE"
    status_code = 503


def key_prefix() -> str:
    from app.core import config
    value = getattr(config, "REDIS_KEY_PREFIX", os.getenv("REDIS_KEY_PREFIX", "rag"))
    if not value or any(character in value for character in "*?[]{}\r\n"):
        raise ValueError("Invalid Redis namespace")
    return value


@lru_cache(maxsize=8)
def _client_for_url(url: str):
    import redis
    return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2,
                               health_check_interval=15, decode_responses=False)


def get_redis_client():
    from app.core import config
    url = getattr(config, "REDIS_URL", os.getenv("REDIS_URL", ""))
    if not url:
        raise CoordinationUnavailableError("Redis未配置，拒绝使用进程内协调替代")
    return _client_for_url(url)


def redis_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except CoordinationUnavailableError:
        raise
    except Exception:
        # Redis errors may contain credentials or service URLs.
        raise CoordinationUnavailableError("Redis协调服务不可用") from None


class _RedisAdapter:
    kind = ""

    def __init__(self, url: str | None = None, prefix: str | None = None, *, client=None):
        self.client = client if client is not None else (_client_for_url(url) if url else get_redis_client())
        self.prefix = prefix or key_prefix()

    def identity(self):
        return ProviderIdentity(kind=self.kind, provider="redis", capabilities=("shared", "ttl"))

    def health(self):
        try:
            redis_call(self.client.ping)
            return ProviderHealth.ok(provider="redis")
        except CoordinationUnavailableError:
            return ProviderHealth.fail("Redis协调服务不可用", provider="redis")


class RedisTaskQueue(_RedisAdapter):
    kind = "task_queue"

    def enqueue(self, job_id: str) -> None:
        if not job_id:
            raise ValueError("job_id is required")
        # This bounded list is a hint. Dropped or duplicate hints are harmless:
        # workers always claim persisted, due jobs and poll without a signal.
        redis_call(self.client.eval, "redis.call('LPUSH',KEYS[1],ARGV[1]);redis.call('LTRIM',KEYS[1],0,999);return 1",
                   1, self.prefix + ":jobs:wakeup", job_id)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            # BLPOP must finish before the client's 2s socket timeout.
            item = redis_call(self.client.blpop, self.prefix + ":jobs:wakeup", timeout=min(1, max(.01, deadline - time.monotonic())))
            if item:
                return True
        return False


class RedisCache(_RedisAdapter):
    kind = "cache"

    def get(self, key: str) -> bytes | None:
        return redis_call(self.client.get, self.prefix + ":cache:" + key)

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None and ttl_seconds <= 0:
            self.delete(key)
            return
        redis_call(self.client.set, self.prefix + ":cache:" + key, bytes(value), ex=ttl_seconds)

    def delete(self, key: str) -> None:
        redis_call(self.client.delete, self.prefix + ":cache:" + key)

    def publish(self, channel: str, payload: str) -> None:
        redis_call(self.client.publish, self.prefix + ":events:" + channel, payload)


_ACQUIRE = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[1]) then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[3])
redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[2])*2);return 1
"""
_RENEW = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
local expires=redis.call('ZSCORE',KEYS[1],ARGV[2])
if not expires or tonumber(expires)<=now then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[1]),ARGV[2])
redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[1])*2);return 1
"""


class RedisSemaphoreLease:
    """Token-owned renewable slots; crashed owners expire without unlocking peers."""

    def __init__(self, name: str, limit: int, ttl_seconds: float = 60, *, client=None, prefix=None):
        if limit < 1 or ttl_seconds < .15:
            raise ValueError("Invalid Redis lease budget")
        self.client = client if client is not None else get_redis_client()
        self.key = (prefix or key_prefix()) + ":lease:" + name
        self.limit, self.ttl_ms = limit, int(ttl_seconds * 1000)
        self.token = uuid.uuid4().hex
        self._stop, self._lost = threading.Event(), threading.Event()
        self._thread = None

    def acquire(self, timeout: float) -> bool:
        if self._thread is not None:
            raise ValueError("Redis lease objects are single-use")
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if redis_call(self.client.eval, _ACQUIRE, 1, self.key, self.limit, self.ttl_ms, self.token):
                self._thread = threading.Thread(target=self._renew_loop, daemon=True, name="redis-slot-renewal")
                self._thread.start()
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def _renew_loop(self):
        while not self._stop.wait(self.ttl_ms / 3000):
            try:
                if not redis_call(self.client.eval, _RENEW, 1, self.key, self.ttl_ms, self.token):
                    self._lost.set()
                    return
            except CoordinationUnavailableError:
                self._lost.set()
                return

    def assert_held(self):
        if self._lost.is_set() or not redis_call(self.client.eval, _RENEW, 1, self.key, self.ttl_ms, self.token):
            self._lost.set()
            raise CoordinationUnavailableError("共享并发租约已丢失，任务结果未被接受")

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        try:
            self.assert_held()
        finally:
            redis_call(self.client.zrem, self.key, self.token)

    release = close


def publish_worker_heartbeat(worker_id: str, payload: dict, ttl_seconds: int = 20):
    client = get_redis_client()
    redis_call(client.set, key_prefix() + ":worker:" + worker_id,
               json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)


def remove_worker_heartbeat(worker_id: str):
    client = get_redis_client()
    redis_call(client.delete, key_prefix() + ":worker:" + worker_id)


def active_workers() -> list[dict]:
    client = get_redis_client()
    keys = redis_call(lambda: list(client.scan_iter(match=key_prefix() + ":worker:*", count=100)))
    if not keys:
        return []
    values = redis_call(client.mget, keys)
    try:
        return [json.loads(value) for value in values if value]
    except (ValueError, TypeError):
        raise CoordinationUnavailableError("Redis worker心跳格式无效") from None


_LOGIN = """
local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
for i=1,2 do
 redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now-tonumber(ARGV[1]))
 if redis.call('ZCARD',KEYS[i])>=tonumber(ARGV[i+1]) then return 0 end
end
for i=1,2 do redis.call('ZADD',KEYS[i],now,ARGV[4]);redis.call('PEXPIRE',KEYS[i],ARGV[1]) end
return 1
"""


def login_attempt_allowed(keys: tuple[str, str], limit: int, window_seconds: int, ip_limit: int | None = None) -> bool:
    client = get_redis_client()
    # Hash tag keeps the two Lua keys together if Redis Cluster is introduced.
    names = [key_prefix() + ":{login}:" + key for key in keys]
    return bool(redis_call(client.eval, _LOGIN, 2, *names, window_seconds * 1000, limit,
                           ip_limit if ip_limit is not None else limit * 6, uuid.uuid4().hex))


def clear_login_account(key: str):
    client = get_redis_client()
    redis_call(client.delete, key_prefix() + ":{login}:" + key)


def registration_attempt_allowed(keys: tuple[str, str], limit: int, window_seconds: int, ip_limit: int) -> bool:
    client = get_redis_client()
    names = [key_prefix() + ":{registration}:" + key for key in keys]
    return bool(redis_call(client.eval, _LOGIN, 2, *names, window_seconds * 1000,
                           limit, ip_limit, uuid.uuid4().hex))
