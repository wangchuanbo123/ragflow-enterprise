"""Remote infrastructure adapters used by the enterprise profile."""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from app.adapters.interfaces import ObjectRef
from rag.providers.interfaces import ProviderHealth, ProviderIdentity


def _safe_object_key(key: str) -> str:
    if not key or "\\" in key or "\x00" in key:
        raise ValueError("Object key must be a non-empty POSIX path")
    logical = PurePosixPath(key)
    if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts):
        raise ValueError(f"Unsafe object key: {key}")
    return logical.as_posix()


class S3ObjectStore:
    """S3-compatible object storage adapter, including MinIO."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        prefix: str = "",
        addressing_style: str = "path",
        client=None,
    ):
        if not bucket:
            raise ValueError("S3_BUCKET is required")
        if addressing_style not in {"path", "virtual", "auto"}:
            raise ValueError("S3_ADDRESSING_STYLE must be path, virtual, or auto")
        self.bucket = bucket
        self.endpoint_url = endpoint_url or None
        self.access_key = access_key or None
        self.secret_key = secret_key or None
        self.region = region
        self.prefix = prefix.strip("/")
        self.addressing_style = addressing_style
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RuntimeError(
                    "S3ObjectStore requires boto3; install enterprise dependencies"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(s3={"addressing_style": self.addressing_style}),
            )
        return self._client

    def _storage_key(self, key: str) -> str:
        logical = _safe_object_key(key)
        return f"{self.prefix}/{logical}" if self.prefix else logical

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="object_store",
            provider="s3",
            capabilities=("shared_storage", "materialize", "content_hash"),
        )

    def put_bytes(self, key: str, data: bytes) -> ObjectRef:
        logical = _safe_object_key(key)
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._storage_key(logical),
            Body=data,
            Metadata={"sha256": digest},
        )
        return ObjectRef(key=logical, size=len(data), content_hash=digest)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._storage_key(key))
            return True
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    @contextmanager
    def materialize(self, key: str):
        logical = _safe_object_key(key)
        suffix = PurePosixPath(logical).suffix
        with tempfile.TemporaryDirectory(prefix="rag-object-") as directory:
            path = Path(directory) / f"object{suffix}"
            self.client.download_file(self.bucket, self._storage_key(logical), str(path))
            yield path

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._storage_key(key))

    def health(self) -> ProviderHealth:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return ProviderHealth.ok(
                provider=self.identity().provider,
                bucket=self.bucket,
                endpoint=self.endpoint_url or "aws",
            )
        except Exception as exc:
            return ProviderHealth.fail(
                str(exc)[:200],
                provider=self.identity().provider,
                bucket=self.bucket,
            )


class CeleryTaskQueue:
    """Celery producer adapter; job payloads stay in the metadata database."""

    def __init__(
        self,
        *,
        broker_url: str,
        result_backend: str,
        queue_name: str,
        task_name: str,
        app=None,
    ):
        if not broker_url:
            raise ValueError("CELERY_BROKER_URL is required")
        self.broker_url = broker_url
        self.result_backend = result_backend
        self.queue_name = queue_name
        self.task_name = task_name
        self._app = app

    @property
    def app(self):
        if self._app is None:
            try:
                from celery import Celery
            except ImportError as exc:
                raise RuntimeError(
                    "CeleryTaskQueue requires celery; install enterprise dependencies"
                ) from exc
            self._app = Celery(
                "ragflow-client",
                broker=self.broker_url,
                backend=self.result_backend or None,
            )
        return self._app

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="task_queue",
            provider="celery",
            capabilities=("distributed", "durable_delivery", "named_queue"),
        )

    def enqueue(self, job_id: str) -> None:
        if not job_id:
            raise ValueError("job_id is required")
        self.app.send_task(self.task_name, args=[job_id], queue=self.queue_name)

    def wait(self, timeout: float) -> bool:
        del timeout
        return False

    def health(self) -> ProviderHealth:
        try:
            with self.app.connection_for_write() as connection:
                connection.ensure_connection(max_retries=0)
            return ProviderHealth.ok(
                provider=self.identity().provider,
                queue=self.queue_name,
            )
        except Exception as exc:
            return ProviderHealth.fail(
                str(exc)[:200],
                provider=self.identity().provider,
                queue=self.queue_name,
            )


class RedisCache:
    """Redis-backed shared cache and invalidation publisher."""

    def __init__(self, url: str, prefix: str = "ragflow", client=None):
        if not url:
            raise ValueError("REDIS_URL is required")
        self.url = url
        self.prefix = prefix.strip(":")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError(
                    "RedisCache requires redis; install enterprise dependencies"
                ) from exc
            self._client = redis.Redis.from_url(self.url)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}" if self.prefix else key

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="cache",
            provider="redis",
            capabilities=("ttl", "shared", "pubsub"),
        )

    def get(self, key: str) -> bytes | None:
        return self.client.get(self._key(key))

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        self.client.set(self._key(key), value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self.client.delete(self._key(key))

    def publish(self, channel: str, payload: str) -> None:
        self.client.publish(self._key(channel), payload)

    def health(self) -> ProviderHealth:
        try:
            self.client.ping()
            return ProviderHealth.ok(provider=self.identity().provider)
        except Exception as exc:
            return ProviderHealth.fail(
                str(exc)[:200],
                provider=self.identity().provider,
            )
