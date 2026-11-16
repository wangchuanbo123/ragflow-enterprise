"""Reentrant knowledge writes: PostgreSQL session locks, local locks only offline."""

import hashlib
import time
from contextlib import contextmanager
from functools import wraps
from threading import Lock, RLock, local

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.adapters.redis import CoordinationUnavailableError

_locks: dict[str, RLock] = {}
_registry_lock = Lock()
_held = local()


class KnowledgeLockBusyError(CoordinationUnavailableError):
    code = "KNOWLEDGE_LOCK_BUSY"
    status_code = 409


def _held_locks():
    if not hasattr(_held, "locks"):
        _held.locks = {}
    return _held.locks


def _lock_key(identifier: str) -> int:
    return int.from_bytes(hashlib.sha256(("rag:knowledge:" + identifier).encode()).digest()[:8], "big", signed=True)


def _assert_connections(session=None):
    # A reconnect must not silently turn the lock connection into an unlocked
    # connection. Check before committing business writes, not from heartbeat
    # threads (which intentionally do not own a knowledge lock).
    for connection, pid in list(_held_locks().values()):
        try:
            if connection.invalidated or connection.closed or connection.scalar(text("SELECT pg_backend_pid()")) != pid:
                raise CoordinationUnavailableError("知识库写锁连接已丢失")
        except CoordinationUnavailableError:
            raise
        except Exception:
            raise CoordinationUnavailableError("知识库写锁连接不可用，拒绝提交写入") from None


event.listen(Session, "before_commit", _assert_connections)


@contextmanager
def knowledge_mutation_lock(knowledge_base_id: str, *, cancelled=None, timeout_seconds: float | None = None):
    from app.core import config, database

    identifier = str(knowledge_base_id)
    held = _held_locks()
    if identifier in held:
        _assert_connections()
        yield
        return
    if database.engine.dialect.name != "postgresql":
        if config.APP_PROFILE not in {"local", "test"}:
            raise CoordinationUnavailableError("服务器知识写入需要PostgreSQL跨进程锁")
        with _registry_lock:
            lock = _locks.setdefault(identifier, RLock())
        with lock:
            yield
        return

    timeout = timeout_seconds if timeout_seconds is not None else getattr(config, "KNOWLEDGE_LOCK_TIMEOUT_SECONDS", 120)
    deadline = time.monotonic() + timeout
    key = _lock_key(identifier)
    try:
        connection = database.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    except Exception:
        raise CoordinationUnavailableError("PostgreSQL知识库写锁连接不可用") from None
    acquired = False
    try:
        while True:
            if cancelled and cancelled():
                from app.agents.contracts import AgentCancelledError
                raise AgentCancelledError("等待知识库写锁期间任务已取消")
            try:
                acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
            except Exception:
                raise CoordinationUnavailableError("PostgreSQL知识库写锁不可用") from None
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise KnowledgeLockBusyError("等待知识库写锁超时")
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        held[identifier] = (connection, pid)
        yield
        _assert_connections()
    finally:
        held.pop(identifier, None)
        try:
            if acquired:
                if connection.invalidated or not connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key}):
                    raise CoordinationUnavailableError("知识库写锁已丢失")
        except Exception:
            connection.invalidate()
            raise CoordinationUnavailableError("知识库写锁释放失败，连接已隔离") from None
        finally:
            connection.close()


def serialized_knowledge_mutation(method):
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with knowledge_mutation_lock(self.knowledge_base_id):
            return method(self, *args, **kwargs)

    return serialized
