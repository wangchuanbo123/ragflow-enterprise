from __future__ import annotations

from datetime import timedelta

from app.adapters.local import InlineTaskQueue, LocalObjectStore, MemoryCache
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.repositories.index_job_repository import IndexJobRepository, _utcnow
from app.services.document_index_service import DocumentIndexService
from rag.providers.profiles import get_llm_profile


def _ensure_default_kb(db):
    if db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID) is None:
        db.add(KnowledgeBase.default())
        db.commit()


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
