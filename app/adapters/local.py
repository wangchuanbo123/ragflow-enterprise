"""Local adapters used on a developer laptop and in tests."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from sqlalchemy import text

from app.adapters.interfaces import ObjectRef
from rag.providers.interfaces import ProviderHealth, ProviderIdentity


class LocalObjectStore:
    """Filesystem-backed object store with safe keys and atomic writes."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="object_store",
            provider="local",
            capabilities=("atomic_put", "materialize"),
        )

    def _resolve(self, key: str) -> Path:
        if not key or "\\" in key or "\x00" in key:
            raise ValueError("Object key must be a non-empty POSIX path")
        logical = PurePosixPath(key)
        if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts):
            raise ValueError(f"Unsafe object key: {key}")

        target = (self.root / Path(*logical.parts)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Object key escapes storage root: {key}") from exc
        return target

    def put_bytes(self, key: str, data: bytes) -> ObjectRef:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

        return ObjectRef(
            key=key,
            size=len(data),
            content_hash=hashlib.sha256(data).hexdigest(),
        )

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    @contextmanager
    def materialize(self, key: str):
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(f"Object not found: {key}")
        yield path

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    def health(self) -> ProviderHealth:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.root.is_dir():
                return ProviderHealth.fail(f"Not a directory: {self.root}")
            return ProviderHealth.ok(
                provider=self.identity().provider,
                root=str(self.root),
            )
        except Exception as exc:
            return ProviderHealth.fail(str(exc)[:200])


class InlineTaskQueue:
    """In-process wake-up signal; the database remains the job source of truth."""

    def __init__(self):
        self._event = threading.Event()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="task_queue",
            provider="inline",
            capabilities=("wakeup_signal",),
        )

    def enqueue(self, job_id: str) -> None:
        if not job_id:
            raise ValueError("job_id is required")
        self._event.set()

    def wait(self, timeout: float) -> bool:
        signaled = self._event.wait(timeout)
        if signaled:
            self._event.clear()
        return signaled

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(provider=self.identity().provider)


class MemoryCache:
    """Small process-local cache used only by local and test profiles."""

    def __init__(self):
        self._values: dict[str, tuple[bytes, float | None]] = {}
        self._lock = threading.Lock()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="cache",
            provider="memory",
            capabilities=("ttl", "local_invalidation"),
        )

    def get(self, key: str) -> bytes | None:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            value, expires_at = item
            if expires_at is not None and expires_at <= time.time():
                self._values.pop(key, None)
                return None
            return value

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        with self._lock:
            self._values[key] = (bytes(value), expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def publish(self, channel: str, payload: str) -> None:
        del channel, payload

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(provider=self.identity().provider)


class SQLAlchemyMetadataSessionProvider:
    """SQLAlchemy session adapter shared by SQLite and PostgreSQL profiles."""

    def __init__(self, session_factory, provider_name: str):
        self.session_factory = session_factory
        self.provider_name = provider_name

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="metadata_store",
            provider=self.provider_name,
            capabilities=("transactions", "orm_session"),
        )

    @contextmanager
    def session(self):
        with self.session_factory() as db:
            yield db

    def health(self) -> ProviderHealth:
        try:
            with self.session() as db:
                db.execute(text("SELECT 1"))
            return ProviderHealth.ok(provider=self.provider_name)
        except Exception as exc:
            return ProviderHealth.fail(str(exc)[:200], provider=self.provider_name)
