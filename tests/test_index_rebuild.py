"""Full KB rebuilds preserve the active index until a complete replacement exists."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.adapters.local import LocalObjectStore
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID, object_prefix_for_knowledge_base
from app.models.index_job import IndexJob, KnowledgeIndexState
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseMember
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.user import User
from app.services.index_rebuild_service import inspect_rebuild_originals, rebuild_registered_originals
from rag.indexing.index_manager import IndexManager
from tests.test_index import FakeVectorProvider
from tests.test_pgvector_provider import pgvector_engine, Embedding


class Queue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, job_id):
        self.jobs.append(job_id)


def _seed(db, store, kb="rebuild-kb", *, source=None, document_id="registered-doc", status="ready", original=True):
    if db.get(KnowledgeBase, kb) is None:
        db.add(KnowledgeBase(id=kb, name=kb, slug=kb, is_active=True))
        db.flush()
    prefix = object_prefix_for_knowledge_base(kb)
    key = source or (f"{prefix}/manual.txt" if prefix else "manual.txt")
    if original:
        store.put_bytes(key, b"Registered original describes a useful operational procedure.")
    document = KnowledgeDocument(
        id=document_id, knowledge_base_id=kb, source_path=key,
        original_filename="manual.txt", file_hash="historical-hash", file_size=57,
        status=status, chunk_count=1 if status == "ready" else 0,
        metadata_json={"embedding_fingerprint": "legacy", "pipeline_fingerprint": "old"},
    )
    db.add(document)
    db.flush()
    if status == "ready":
        db.add(KnowledgeChunk(
            id=f"old-{document_id}", knowledge_base_id=kb, document_id=document_id,
            file_hash="historical-hash", chunk_index=0, content="Old usable text",
            content_hash="old-content", token_count=4,
        ))
    if db.scalar(
        select(KnowledgeIndexState).where(KnowledgeIndexState.knowledge_base_id == kb)
    ) is None:
        db.add(KnowledgeIndexState(knowledge_base_id=kb, active_collection_name=f"legacy-{kb}"))
    db.commit()
    return document


def _manager(db, kb="rebuild-kb", provider=None):
    class GuardedProvider(FakeVectorProvider):
        def open(self, embedding, index_ref):
            assert not index_ref.name.startswith("legacy-"), "legacy identity must never be opened"
            return super().open(embedding, index_ref)

        def delete_index(self, index_ref):
            assert not index_ref.name.startswith("legacy-"), "previous collection must be retained"
            return super().delete_index(index_ref)

    provider = provider or GuardedProvider()
    return IndexManager(db, embedding=Embedding(), vector_provider=provider, knowledge_base_id=kb)


@pytest.fixture
def originals(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.indexing.index_manager.IMAGE_EXTRACTION_ENABLED", False)
    return LocalObjectStore(tmp_path / "objects")


def test_worker_rebuild_uses_only_selected_kbs_registered_originals(db_engine, originals):
    from app.core.database import SessionLocal
    from app.services.job_handlers import handle_rebuild

    with SessionLocal() as db:
        _seed(db, originals)
        _seed(db, originals, "another-kb", document_id="other-doc")
        _seed(db, originals, source="knowledge-bases/rebuild-kb/deleted.txt", document_id="deleted-doc", status="deleted")
        _seed(db, originals, source="knowledge-bases/rebuild-kb/pending-delete.txt", document_id="pending-delete-doc", status="pending_delete")
        originals.put_bytes("knowledge-bases/rebuild-kb/unregistered.txt", b"must never be indexed")
        manager = _manager(db)
        job = SimpleNamespace(knowledge_base_id="rebuild-kb")
        ctx = SimpleNamespace(db=db, job=job, job_repo=None, mgr=manager,
            container=SimpleNamespace(object_store=originals), job_id="rebuild-job-id",
            heartbeat=None, agent_context=None, agent_document_ids=None,
            agent_cancelled=None, agent_event_callback=None, link_followup_jobs=None)
        outcome = handle_rebuild(ctx).deferred_completion
        assert outcome["status"] == "completed" and outcome["succeeded_items"] == 1
        first = manager.repo.get_index_state().active_collection_name
        assert first.startswith("documents_rebuildkb_build_rebuild-")
        assert {chunk.document_id for chunk in manager.repo.list_all_active_chunks()} == {"registered-doc"}
        assert len(manager.vector_provider.list_ids(manager._index_ref(first))) > 0
        other = db.scalar(select(KnowledgeIndexState).where(KnowledgeIndexState.knowledge_base_id == "another-kb"))
        assert other.active_collection_name == "legacy-another-kb"
        assert db.get(KnowledgeChunk, "old-other-doc") is not None
        assert originals.exists("knowledge-bases/rebuild-kb/unregistered.txt")
        # A retry of an already switched job creates a fresh shadow instead of
        # resetting the active collection associated with its previous attempt.
        assert handle_rebuild(ctx).deferred_completion["status"] == "completed"
        assert manager.repo.get_index_state().active_collection_name != first
        assert first in manager.vector_provider.client.collections


def test_missing_registered_original_aborts_before_build_and_keeps_metadata(db_engine, originals, monkeypatch):
    from app.core.database import SessionLocal
    from rag.errors import DocumentValidationError

    with SessionLocal() as db:
        _seed(db, originals)
        _seed(db, originals, source="knowledge-bases/rebuild-kb/missing.txt", document_id="missing", original=False)
        manager = _manager(db)
        monkeypatch.setattr(manager, "rebuild", lambda *a, **kw: pytest.fail("must not build a partial corpus"))
        with pytest.raises(DocumentValidationError, match="missing"):
            rebuild_registered_originals(manager, originals, job_id="missing")
        assert manager.repo.get_index_state().active_collection_name == "legacy-rebuild-kb"
        assert manager.repo.list_chunk_ids_for_document("registered-doc") == ["old-registered-doc"]


@pytest.mark.parametrize("kb,key", [
    ("rebuild-kb", "knowledge-bases/another-kb/secret.txt"),
    ("rebuild-kb", "knowledge-bases/rebuild-kb/../secret.txt"),
    ("rebuild-kb", "knowledge-bases/rebuild-kb/.versions/1/manual.txt"),
    ("rebuild-kb", "knowledge-bases/rebuild-kb/C:/manual.txt"),
    ("rebuild-kb", "knowledge-bases/rebuild-kb/unsupported.zip"),
    (DEFAULT_KNOWLEDGE_BASE_ID, "knowledge-bases/another-kb/secret.txt"),
])
def test_invalid_or_cross_kb_registered_keys_fail_closed(db_engine, originals, kb, key):
    from app.core.database import SessionLocal
    from rag.errors import DocumentValidationError

    with SessionLocal() as db:
        _seed(db, originals, kb, source=key, original=False)
        plan = inspect_rebuild_originals(db, kb, originals)
        assert plan["ready"] is False and plan["error_count"] == 1
        assert "secret.txt" not in json.dumps(plan)
        with pytest.raises(DocumentValidationError):
            rebuild_registered_originals(_manager(db, kb), originals, job_id="invalid")


def test_cancellation_after_vector_validation_discards_shadow(db_engine, originals):
    from app.agents.contracts import AgentCancelledError
    from app.core.database import SessionLocal

    cancelled = False

    class CancelOnValidation(FakeVectorProvider):
        def count(self, ref):
            nonlocal cancelled
            cancelled = True
            return super().count(ref)

    with SessionLocal() as db:
        _seed(db, originals)
        provider = CancelOnValidation()
        manager = _manager(db, provider=provider)
        with pytest.raises(AgentCancelledError):
            rebuild_registered_originals(manager, originals, job_id="cancelled", cancelled=lambda: cancelled)
        assert manager.repo.get_index_state().active_collection_name == "legacy-rebuild-kb"
        assert manager.repo.list_chunk_ids_for_document("registered-doc") == ["old-registered-doc"]
        assert provider.client.collections == {}


def test_lease_loss_during_validation_discards_shadow(db_engine, originals):
    from app.core.database import SessionLocal
    from app.repositories.index_job_repository import JobLeaseLostError

    class LeaseLost(FakeVectorProvider):
        def count(self, ref):
            raise JobLeaseLostError("lost")

    with SessionLocal() as db:
        _seed(db, originals)
        provider = LeaseLost()
        manager = _manager(db, provider=provider)
        with pytest.raises(JobLeaseLostError):
            rebuild_registered_originals(manager, originals, job_id="lost")
        assert manager.repo.get_index_state().active_collection_name == "legacy-rebuild-kb"
        assert manager.repo.list_chunk_ids_for_document("registered-doc") == ["old-registered-doc"]
        assert provider.client.collections == {}


def test_metadata_commit_failure_rolls_back_chunks_and_pointer(db_engine, originals):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _seed(db, originals)
        manager = _manager(db)

        def reject_switch(session):
            if any(isinstance(row, KnowledgeIndexState) and row.active_collection_name.endswith("_commitfail")
                   for row in session.identity_map.values()):
                raise RuntimeError("simulated DB commit failure")

        event.listen(db, "before_commit", reject_switch)
        try:
            result = rebuild_registered_originals(manager, originals, job_id="commitfail")
        finally:
            event.remove(db, "before_commit", reject_switch)
        assert result["status"] == "failed"
        assert manager.repo.get_index_state().active_collection_name == "legacy-rebuild-kb"
        assert manager.repo.list_chunk_ids_for_document("registered-doc") == ["old-registered-doc"]
        assert manager.vector_provider.client.collections == {}


def _administrator(db, *, role="admin", active=True):
    user = User(username="rebuild-operator", password_hash="unused", display_name="Operator", role=role, is_active=active)
    db.add(user)
    db.commit()
    return user


def test_cli_dry_run_is_read_only_and_apply_queues_normal_worker(db_engine, originals, monkeypatch, capsys):
    from app.core.database import SessionLocal
    from scripts.rebuild_index import main

    queue = Queue()
    monkeypatch.setattr("app.core.config.LOCAL_OBJECT_STORE_DIR", originals.root)
    with SessionLocal() as db:
        _seed(db, originals)
        user_id = _administrator(db).id
    def no_models():
        pytest.fail("dry run must not construct model providers")
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", no_models)
    statements = []
    def track_sql(connection, cursor, statement, *args):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(db_engine, "before_cursor_execute", track_sql)
    args = ["--knowledge-base", "rebuild-kb", "--admin-username", "rebuild-operator"]
    try:
        assert main(args) == 0
    finally:
        event.remove(db_engine, "before_cursor_execute", track_sql)
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True and plan["documents_to_rebuild"] == 1
    assert set(statements) <= {"SELECT", "PRAGMA"}
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(task_queue=queue))
    assert main([*args, "--apply"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["status"] == "accepted" and queued["job_type"] == "rebuild"
    with SessionLocal() as db:
        job = db.get(IndexJob, queued["job_id"])
        assert job.knowledge_base_id == "rebuild-kb" and job.status == "pending"
        assert job.requested_by == user_id and job.options_json["source_kind"] == "registered_objects"
        assert db.get(KnowledgeChunk, "old-registered-doc") is not None
    assert queue.jobs == [queued["job_id"]]
    assert main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["job_id"] == queued["job_id"]


@pytest.mark.parametrize("role,active,kb_active", [("user", True, True), ("admin", False, True), ("admin", True, False)])
def test_cli_requires_active_administrator_and_active_kb(db_engine, originals, monkeypatch, role, active, kb_active):
    from app.core.database import SessionLocal
    from scripts.rebuild_index import main

    monkeypatch.setattr("app.core.config.LOCAL_OBJECT_STORE_DIR", originals.root)
    with SessionLocal() as db:
        _seed(db, originals)
        _administrator(db, role=role, active=active)
        db.get(KnowledgeBase, "rebuild-kb").is_active = kb_active
        db.commit()
    with pytest.raises(SystemExit) as error:
        main(["--knowledge-base", "rebuild-kb", "--admin-username", "rebuild-operator", "--apply"])
    assert error.value.code == 2
    with SessionLocal() as db:
        assert list(db.scalars(select(IndexJob))) == []


def test_cli_blocks_missing_originals_without_queuing_partial_rebuild(db_engine, originals, monkeypatch, capsys):
    from app.core.database import SessionLocal
    from scripts.rebuild_index import main

    monkeypatch.setattr("app.core.config.LOCAL_OBJECT_STORE_DIR", originals.root)
    with SessionLocal() as db:
        _seed(db, originals, original=False)
        _administrator(db)
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: pytest.fail("must not queue"))
    assert main(["--knowledge-base", "rebuild-kb", "--admin-username", "rebuild-operator", "--apply"]) == 1
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "blocked" and plan["error_count"] == 1
    with SessionLocal() as db:
        assert list(db.scalars(select(IndexJob))) == []


def test_rebuild_api_rejects_empty_kb(db_engine, originals, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1.knowledge_bases import request_rebuild
    from app.core.database import SessionLocal

    queue = Queue()
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(object_store=originals, task_queue=queue))
    with SessionLocal() as db:
        db.add(KnowledgeBase(id="empty-kb", name="Empty", slug="empty-kb", is_active=True))
        user = _administrator(db)
        with pytest.raises(HTTPException) as error:
            request_rebuild("empty-kb", user, db)
        assert error.value.status_code == 400
        assert queue.jobs == []


@pytest.mark.parametrize("role,allowed", [("owner", True), ("editor", True), ("reader", False), (None, False)])
def test_rebuild_api_requires_kb_edit_permission(db_engine, originals, monkeypatch, role, allowed):
    from fastapi import HTTPException
    from app.api.v1.knowledge_bases import request_rebuild
    from app.core.database import SessionLocal

    queue = Queue()
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(object_store=originals, task_queue=queue))
    with SessionLocal() as db:
        _seed(db, originals)
        user = _administrator(db, role="user")
        if role:
            db.add(KnowledgeBaseMember(knowledge_base_id="rebuild-kb", user_id=user.id, role=role))
            db.commit()
        if allowed:
            response = request_rebuild("rebuild-kb", user, db)
            assert response["status"] == "accepted" and queue.jobs == [response["job_id"]]
        else:
            with pytest.raises(HTTPException) as error:
                request_rebuild("rebuild-kb", user, db)
            assert error.value.status_code == 403
            assert queue.jobs == []


def test_rebuild_endpoint_returns_202_and_queues_selected_kb(client, originals, monkeypatch):
    from app.adapters.local import SQLAlchemyMetadataSessionProvider
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
    from app.core.database import SessionLocal

    queue = Queue()
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(
        object_store=originals, task_queue=queue, metadata=SQLAlchemyMetadataSessionProvider(lambda: SessionLocal(), "sqlite"),
    ))
    with SessionLocal() as db:
        _seed(db, originals)
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    response = client.post("/api/v1/knowledge-bases/rebuild-kb/index-jobs/rebuild")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert client.post("/api/v1/knowledge-bases/rebuild-kb/index-jobs/rebuild").json()["job_id"] == job_id
    with SessionLocal() as db:
        assert db.get(IndexJob, job_id).knowledge_base_id == "rebuild-kb"
        db.get(KnowledgeBase, "rebuild-kb").is_active = False
        db.commit()
    assert client.post("/api/v1/knowledge-bases/rebuild-kb/index-jobs/rebuild").status_code == 403


def test_real_pgvector_rebuild_replaces_incompatible_legacy_identity(pgvector_engine, originals):
    from app.models import Base
    from rag.providers.interfaces import VectorIndexRef
    from rag.providers.pgvector_store import PgVectorStoreProvider

    Base.metadata.create_all(pgvector_engine)
    provider = PgVectorStoreProvider(pgvector_engine)
    legacy = VectorIndexRef("legacy-rebuild-kb", "rebuild-kb")
    provider.open(Embedding(), legacy).add_texts(["Old usable text"], ids=["old-registered-doc"])
    with Session(pgvector_engine) as db:
        _seed(db, originals)
        manager = _manager(db, provider=provider)
        with pytest.raises(ValueError, match="Legacy vector identity is unverified"):
            provider.open(Embedding(), manager._index_ref(legacy.name))
        result = rebuild_registered_originals(manager, originals, job_id="newprofile")
        assert result["status"] == "completed" and result["old_collection"] == legacy.name
        assert provider.list_ids(legacy) == {"old-registered-doc"}
        new_ref = manager._index_ref(result["build_collection"])
        assert provider.list_ids(new_ref) == {chunk.id for chunk in manager.repo.list_all_active_chunks()}
        assert provider.count(new_ref) > 0
        assert manager.repo.get_index_state().active_collection_name == new_ref.name
