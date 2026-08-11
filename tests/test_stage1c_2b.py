from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from app.adapters.local import InlineTaskQueue, LocalObjectStore, MemoryCache
from app.adapters.remote import CeleryTaskQueue, RedisCache, S3ObjectStore
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.repositories.index_job_repository import IndexJobRepository, _utcnow
from app.services.document_index_service import DocumentIndexService
from rag.providers.interfaces import VectorIndexRef
from rag.providers.vector_store import ChromaHttpVectorStoreProvider
from rag.providers.profiles import get_llm_profile


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, Metadata):
        self.objects[(Bucket, Key)] = (bytes(Body), Metadata)

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}
            raise error
        return {}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)][0])

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def head_bucket(self, *, Bucket):
        assert Bucket == "rag-source"
        return {}


class FakeConnection:
    def __init__(self):
        self.connected = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        del args

    def ensure_connection(self, max_retries=0):
        assert max_retries == 0
        self.connected = True


class FakeCeleryApp:
    def __init__(self):
        self.sent = []
        self.connection = FakeConnection()

    def send_task(self, name, args, queue):
        self.sent.append((name, args, queue))

    def connection_for_write(self):
        return self.connection


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.messages = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.last_ttl = ex

    def delete(self, key):
        self.values.pop(key, None)

    def publish(self, channel, payload):
        self.messages.append((channel, payload))

    def ping(self):
        return True


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.ids = {"one", "two"}

    def count(self):
        return len(self.ids)

    def get(self, include=None, where=None):
        del include, where
        return {"ids": list(self.ids)}

    def delete(self, ids=None, where=None):
        del where
        self.ids.difference_update(ids or [])


class FakeChromaClient:
    def __init__(self):
        self.collections = {"documents": FakeCollection("documents")}

    def get_collection(self, name):
        return self.collections[name]

    def list_collections(self):
        return [SimpleNamespace(name=name) for name in self.collections]

    def delete_collection(self, name):
        del self.collections[name]


def _ensure_default_kb(db):
    if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
        db.add(KnowledgeBase.default())
        db.commit()


def test_s3_celery_redis_and_chroma_http_adapters_keep_vendor_clients_hidden():
    s3_client = FakeS3Client()
    object_store = S3ObjectStore(
        bucket="rag-source",
        endpoint_url="http://minio:9000",
        prefix="tenant-a",
        client=s3_client,
    )
    ref = object_store.put_bytes("kb/doc.txt", b"hello")
    assert ref.content_hash
    assert object_store.exists(ref.key)
    with object_store.materialize(ref.key) as path:
        assert path.read_bytes() == b"hello"
    assert object_store.health().status == "ok"
    object_store.delete(ref.key)
    assert not object_store.exists(ref.key)

    celery_app = FakeCeleryApp()
    queue = CeleryTaskQueue(
        broker_url="redis://redis/0",
        result_backend="redis://redis/1",
        queue_name="rag.index",
        task_name="ragflow.index.process_job",
        app=celery_app,
    )
    queue.enqueue("job-1")
    assert celery_app.sent == [
        ("ragflow.index.process_job", ["job-1"], "rag.index")
    ]
    assert queue.health().status == "ok"

    redis_client = FakeRedis()
    cache = RedisCache("redis://redis/2", prefix="rag", client=redis_client)
    cache.set("key", b"value", ttl_seconds=30)
    assert cache.get("key") == b"value"
    cache.publish("invalidate", "all")
    assert redis_client.messages == [("rag:invalidate", "all")]
    assert cache.health().status == "ok"

    vector = ChromaHttpVectorStoreProvider(
        host="chroma",
        client=FakeChromaClient(),
    )
    vector_ref = VectorIndexRef(name="documents")
    assert vector.identity().provider == "chroma_http"
    assert vector.count(vector_ref) == 2
    assert vector.health(vector_ref, {"one", "two"}).status == "ok"


def test_memory_cache_preserves_local_profile_behavior():
    cache = MemoryCache()
    cache.set("key", b"value", ttl_seconds=10)
    assert cache.get("key") == b"value"
    cache.delete("key")
    assert cache.get("key") is None


def test_model_profiles_are_independently_fingerprinted(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "ANSWER_LLM_MODEL", "answer-model")
    monkeypatch.setattr(config, "REWRITE_LLM_MODEL", "rewrite-model")
    monkeypatch.setattr(config, "ROUTER_LLM_MODEL", "router-model")

    answer = get_llm_profile("answer")
    rewrite = get_llm_profile("rewrite")
    router = get_llm_profile("router")

    assert answer.model == "answer-model"
    assert rewrite.model == "rewrite-model"
    assert router.model == "router-model"
    assert len({answer.profile_id, rewrite.profile_id, router.profile_id}) == 3


def test_job_claim_heartbeat_recovery_cancel_and_events(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _ensure_default_kb(db)
        repository = IndexJobRepository(db)
        job = repository.create(
            IndexJob(job_type="file", status="pending", max_attempts=2)
        )
        claimed = repository.claim(
            job.id,
            worker_id="worker-a",
            lease_seconds=60,
        )
        assert claimed is not None
        assert claimed.attempt_count == 1
        assert repository.claim(
            job.id,
            worker_id="worker-b",
            lease_seconds=60,
        ) is None
        assert repository.heartbeat(
            job.id,
            worker_id="worker-a",
            lease_seconds=60,
        )

        claimed.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        assert repository.recover_expired_leases() == 1
        assert repository.get(job.id).status == "pending"

        claimed_again = repository.claim(
            job.id,
            worker_id="worker-b",
            lease_seconds=60,
        )
        assert claimed_again.attempt_count == 2
        claimed_again.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        assert repository.recover_expired_leases() == 1
        assert repository.get(job.id).status == "failed"
        assert len(repository.list_events(job.id)) >= 5

        cancellable = repository.create(IndexJob(job_type="file", status="pending"))
        repository.request_cancel(cancellable.id)
        assert repository.get(cancellable.id).status == "cancelled"
        repository.retry(cancellable.id)
        assert repository.get(cancellable.id).status == "pending"
        assert repository.get(cancellable.id).attempt_count == 0


def test_batch_upload_creates_one_job_and_reports_rejected_files(db_engine, tmp_path):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _ensure_default_kb(db)
        queue = InlineTaskQueue()
        service = DocumentIndexService(
            db,
            object_store=LocalObjectStore(tmp_path),
            task_queue=queue,
        )
        job, document_ids, rejected = service.create_batch_upload_job(
            [
                ("one.txt", b"first document"),
                ("two.md", b"second document"),
                ("bad.exe", b"not supported"),
            ]
        )

        assert job.total_items == 2
        assert len(document_ids) == 2
        assert rejected[0]["filename"] == "bad.exe"
        assert len(IndexJobRepository(db).list_items(job.id)) == 2
        assert queue.wait(0) is True
