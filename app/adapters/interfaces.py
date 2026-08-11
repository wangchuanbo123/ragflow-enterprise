"""Vendor-neutral contracts for storage, jobs, and metadata sessions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rag.providers.interfaces import ProviderHealth, ProviderIdentity


@dataclass(frozen=True)
class ObjectRef:
    """Stable reference to an object; callers do not depend on a local path."""

    key: str
    size: int
    content_hash: str


class ObjectStoreProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the configured object store."""

    def put_bytes(self, key: str, data: bytes) -> ObjectRef:
        """Atomically store bytes and return their logical reference."""

    def exists(self, key: str) -> bool:
        """Return whether an object exists."""

    def materialize(self, key: str) -> AbstractContextManager[Path]:
        """Expose an object as a local file for parsers that require a Path."""

    def delete(self, key: str) -> None:
        """Delete one object."""

    def health(self) -> ProviderHealth:
        """Check object-store availability."""


class TaskQueueProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the configured task queue."""

    def enqueue(self, job_id: str) -> None:
        """Signal that a persisted job is ready for processing."""

    def wait(self, timeout: float) -> bool:
        """Wait for a signal and return True when one was received."""

    def health(self) -> ProviderHealth:
        """Check task-queue availability."""


class CacheProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the configured cache."""

    def get(self, key: str) -> bytes | None:
        """Return a cached value."""

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        """Store a value with an optional TTL."""

    def delete(self, key: str) -> None:
        """Delete one cached value."""

    def publish(self, channel: str, payload: str) -> None:
        """Publish a cache-invalidation event."""

    def health(self) -> ProviderHealth:
        """Check cache availability."""


class MetadataSessionProvider(Protocol):
    def identity(self) -> ProviderIdentity:
        """Describe the configured metadata store."""

    def session(self) -> AbstractContextManager:
        """Create a transactional ORM session context."""

    def health(self) -> ProviderHealth:
        """Check metadata-store connectivity."""
