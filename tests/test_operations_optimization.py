"""Scheduling, shared capacity, telemetry and safe backup-delivery contracts."""
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading

import pytest
from sqlalchemy.orm import Session

from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.repositories.index_job_repository import IndexJobRepository


def seed(engine):
    with Session(engine) as db:
        db.add_all([KnowledgeBase(id="ops-a", name="A", slug="ops-a"), KnowledgeBase(id="ops-b", name="B", slug="ops-b")])
        db.commit()


def create(db, kb, kind="file", **values):
    return IndexJobRepository(db, kb).create(IndexJob(job_type=kind, **values))


def test_priority_and_aging_prevent_long_job_starvation(db_engine, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "JOB_PRIORITY_AGING_SECONDS", 60, raising=False)
    seed(db_engine)
    with Session(db_engine) as db:
        old = create(db, "ops-a", "rebuild", created_at=datetime.utcnow()-timedelta(minutes=3), options_json={"priority": 95})
        create(db, "ops-b", "delete")
        assert IndexJobRepository(db, None).claim_next_pending().id == old.id


def test_explicit_priority_and_fair_knowledge_base_rotation(db_engine):
    seed(db_engine)
    with Session(db_engine) as db:
        first = create(db, "ops-a", created_at=datetime.utcnow()-timedelta(seconds=3), options_json={"priority": 5})
        create(db, "ops-a", created_at=datetime.utcnow()-timedelta(seconds=2), options_json={"priority": 5})
        other = create(db, "ops-b", created_at=datetime.utcnow()-timedelta(seconds=1), options_json={"priority": 5})
        repo = IndexJobRepository(db, None)
        chosen = repo.claim_next_pending()
        assert chosen.id == first.id
        repo.update(chosen, status="completed")
        assert repo.claim_next_pending().id == other.id
        with pytest.raises(ValueError):
            create(db, "ops-a", options_json={"priority": True})


def test_resource_budget_skips_saturated_jobs(db_engine, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "JOB_PARSE_CONCURRENCY", 1, raising=False)
    seed(db_engine)
    with Session(db_engine) as db:
        # SQLite's server timestamp has second precision. Give this capacity
        # test a deliberate order instead of depending on random UUID ties.
        now = datetime.utcnow()
        first = create(db, "ops-a", created_at=now-timedelta(seconds=3))
        create(db, "ops-b", created_at=now-timedelta(seconds=2))
        vector = create(db, "ops-b", "entity_vector_rebuild", created_at=now-timedelta(seconds=1))
        repo = IndexJobRepository(db, None)
        assert repo.claim_next_pending().id == first.id
        assert repo.claim_next_pending().id == vector.id


@pytest.fixture
def local_model_budget(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "MODEL_BUDGET_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MODEL_MAX_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(config, "MODEL_BACKGROUND_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "MODEL_QUEUE_TIMEOUT_SECONDS", .05, raising=False)
    monkeypatch.setattr(config, "CACHE_PROVIDER", "local")


def test_background_capacity_preserves_foreground_and_reentrant_slots(local_model_budget):
    from app.services.model_budget import model_slot
    from rag.errors import RAGBusyError
    ready, release = threading.Event(), threading.Event()
    def hold():
        with model_slot(background=True):
            ready.set(); release.wait(3)
    worker = threading.Thread(target=hold)
    worker.start()
    assert ready.wait(1)
    try:
        with pytest.raises(RAGBusyError):
            with model_slot(background=True): pass
        with model_slot(background=False):
            with model_slot(background=False): pass
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive()


def test_stream_close_releases_shared_slot(local_model_budget, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "MODEL_MAX_CONCURRENCY", 1)
    from app.services.model_budget import measured_model_call, model_slot
    @measured_model_call
    def stream():
        yield "one"
        yield "two"
    response = stream()
    assert next(response) == "one"
    response.close()
    with model_slot(background=True): pass


def test_actual_stage_events_include_document_and_failure():
    from app.services.task_execution import job_stage, stage_recorder
    events = []
    with stage_recorder(events.append):
        with job_stage("parse", document_id="doc"): pass
        with pytest.raises(ValueError):
            with job_stage("vector", document_id="doc"):
                raise ValueError("safe")
    assert [(e["stage"], e["status"]) for e in events] == [("parse", "running"), ("parse", "completed"), ("vector", "running"), ("vector", "failed")]
    assert all(e["document_id"] == "doc" for e in events)


def test_structured_logs_redact_credentials_and_keep_context():
    from app.core.logger import StructuredFormatter
    from app.services.observability import operation_context
    record = logging.LogRecord("test", logging.ERROR, "", 1,
                               "password=private sk-abcdefghijklmnop https://user:password@example.test", (), None)
    with operation_context(request_id="req-1", job_id="job-1", knowledge_base_id="kb-1"):
        result = json.loads(StructuredFormatter().format(record))
    assert result["request_id"] == "req-1" and result["job_id"] == "job-1"
    assert "private" not in result["message"] and "abcdefghijklmnop" not in result["message"]
    assert "user:password" not in result["message"]


def test_metrics_endpoint_requires_admin(client, monkeypatch):
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD
    from app.core import database
    from types import SimpleNamespace
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=SimpleNamespace(
        size=lambda: 10, checkedin=lambda: 0, checkedout=lambda: 1, overflow=lambda: -9,
    )))
    assert client.get("/api/v1/operations/metrics").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    response = client.get("/api/v1/operations/metrics")
    assert response.status_code == 200
    assert "tasks" in response.json() and "alerts" in response.json()
    assert response.json()["source"] == "process"
    assert response.json()["database_pool"]["overflow"] == 0


def test_verified_mirror_is_atomic_and_rejects_corruption(tmp_path, monkeypatch):
    from scripts import backup_schedule as module
    source = tmp_path / "server-original"
    source.mkdir()
    for name in (*module.COMPONENTS, "manifest.json"):
        (source / name).write_bytes(b"safe")
    def validate(path):
        if any((Path(path) / name).read_bytes() != b"safe" for name in (*module.COMPONENTS, "manifest.json")):
            raise ValueError("bad digest")
        return Path(path), {}
    monkeypatch.setattr(module, "validate_backup", validate)
    monkeypatch.setattr(module, "_restrict_private", lambda *a, **kw: None)
    target = module.copy_verified_backup(source, tmp_path / "mirror")
    assert target.name == source.name and (target / "env.original").read_bytes() == b"safe"
    assert module.copy_verified_backup(source, tmp_path / "mirror") == target
    (target / "database.dump").write_bytes(b"corrupt")
    with pytest.raises(ValueError): module.copy_verified_backup(source, tmp_path / "mirror")


def test_backup_mirror_failure_preserves_local_success_and_retries_only_copy(tmp_path, monkeypatch):
    from scripts import backup_schedule as module
    (tmp_path / ".env.server").write_text("SERVER_BACKUP_MIRROR_DIRECTORY=" + str(tmp_path / "remote") + "\n")
    calls = []
    class Service:
        def __init__(self, root): pass
        def backup(self, output):
            calls.append("backup")
            path = Path(output) / "backup-one"
            path.mkdir()
            return {"backup": str(path)}
    monkeypatch.setattr(module, "validate_backup", lambda path: (path, {}))
    monkeypatch.setattr(module, "_private_write", lambda path, data, **kw: Path(path).write_bytes(data))
    def fail(*args): raise OSError("offline")
    monkeypatch.setattr(module, "copy_verified_backup", fail)
    now = datetime.now(timezone.utc)
    with pytest.raises(OSError): module.run_once(tmp_path, service_factory=Service, now=now)
    state = json.loads((tmp_path / "data/server-backups/schedule-status.json").read_text())
    assert state["last_success_at"] == now.isoformat() and state["mirror_status"] == "failed"
    monkeypatch.setattr(module, "copy_verified_backup", lambda *args: tmp_path / "remote/backup-one")
    result = module.run_once(tmp_path, service_factory=Service, now=now+timedelta(minutes=1))
    assert result["mirror_status"] == "verified" and calls == ["backup"]
    published = json.loads((tmp_path / "data/operations/backup-status.json").read_text())
    assert "backup" not in published and "mirror" not in published


def test_handlers_cover_every_registered_job_type():
    from app.services.job_handlers import HANDLERS
    from app.services.job_policy import RESOURCE_TYPES
    assert set(HANDLERS) == {kind for kinds in RESOURCE_TYPES.values() for kind in kinds}


# Real services are isolated by the shared fixture's private schema/namespace.
from tests.test_distributed_coordination import postgres_service, redis_service


def test_real_database_resource_quotas_are_atomic_across_workers(postgres_service, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.core import config
    monkeypatch.setattr(config, "JOB_PARSE_CONCURRENCY", 1, raising=False)
    engine, _, _ = postgres_service
    with Session(engine) as db:
        for number in range(8):
            db.add(KnowledgeBase(id=f"fair-{number}", name="safe", slug=f"fair-{number}"))
            db.flush()
            IndexJobRepository(db, f"fair-{number}").create(IndexJob(job_type="file"))
    def claim(number):
        with Session(engine) as db:
            job = IndexJobRepository(db, None).claim_next_pending(worker_id=f"worker-{number}")
            return job.id if job else None
    with ThreadPoolExecutor(8) as executor:
        selected = list(executor.map(claim, range(8)))
    assert len([item for item in selected if item]) == 1


def test_real_redis_model_quota_preserves_foreground(redis_service, monkeypatch):
    from app.core import config
    from app.services.model_budget import model_slot
    from rag.errors import RAGBusyError
    monkeypatch.setattr(config, "MODEL_BUDGET_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(config, "MODEL_MAX_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(config, "MODEL_BACKGROUND_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(config, "MODEL_QUEUE_TIMEOUT_SECONDS", .05, raising=False)
    ready, release, failures = threading.Event(), threading.Event(), []
    def background():
        try:
            with model_slot(background=True):
                ready.set(); release.wait(3)
        except Exception as exc: failures.append(exc)
    thread = threading.Thread(target=background)
    thread.start()
    assert ready.wait(2)
    try:
        with pytest.raises(RAGBusyError):
            with model_slot(background=True): pass
        with model_slot(background=False): pass
    finally:
        release.set(); thread.join(2)
    assert not failures and not thread.is_alive()



def test_stream_resumes_in_distinct_thread_contexts_without_leaking_quota(local_model_budget, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "MODEL_MAX_CONCURRENCY", 1)
    from contextvars import copy_context
    from app.services.model_budget import measured_model_call, model_slot
    @measured_model_call
    def stream():
        yield "one"
        yield "two"
    contexts = [copy_context() for _ in range(3)]
    iterator = stream()
    assert contexts[0].run(next, iterator) == "one"
    assert contexts[1].run(next, iterator) == "two"
    contexts[2].run(iterator.close)
    # No copied caller context retains the reentrancy flag after the stream.
    for context in contexts:
        def acquire():
            with model_slot(background=True): pass
        context.run(acquire)



def test_provider_reported_usage_does_not_treat_unknown_price_as_free(monkeypatch):
    from app.services.model_budget import record_usage
    observed = []
    monkeypatch.setattr("app.services.observability.observe", lambda *args: observed.append(args))
    assert record_usage({"input_tokens": 20, "output_tokens": 5, "cost": 0, "currency": "USD"})
    assert ("model_input_tokens", 20) in observed and ("model_cost_unknown",) in observed
    assert not any(item[0].endswith("reported_cost_usd") for item in observed)
