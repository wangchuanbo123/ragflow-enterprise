"""Schema writes serialize on PostgreSQL; schema reads remain independent of writers."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.database import create_database_engine, normalize_database_url
from app.core.init_db import database_startup_lock, run_migrations
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.models.graph_governance import GraphSchemaDefinition
from app.services.graph_governance_service import GraphGovernanceService
from app.services.knowledge_mutation import knowledge_mutation_lock


@pytest.fixture
def postgres_schema_engine(monkeypatch):
    value = os.getenv("TEST_POSTGRES_URL")
    if not value:
        pytest.skip("Set TEST_POSTGRES_URL for real concurrent Schema API tests")
    url = normalize_database_url(value)
    root = create_database_engine(url)
    name = "test_schema_api_" + uuid.uuid4().hex
    with root.begin() as connection:
        connection.execute(text("CREATE SCHEMA " + name))
    engine = create_database_engine(url.set(query={**url.query, "options": "-csearch_path=" + name + ",public"}))
    run_migrations(target_engine=engine)
    from app.core import database
    from app.bootstrap import container
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(container, "get_provider_container", lambda: SimpleNamespace(task_queue=object()))
    try:
        yield engine
    finally:
        engine.dispose()
        with root.begin() as connection:
            connection.execute(text("DROP SCHEMA " + name + " CASCADE"))
        root.dispose()


def test_existing_and_explicitly_read_only_schema_paths_never_acquire_write_lock(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.models.knowledge_base import KnowledgeBase
    import app.services.graph_governance_service as module

    with SessionLocal() as db:
        db.add(KnowledgeBase.default())
        db.commit()
        service = GraphGovernanceService(db, KB, task_queue=object())
        active = service.active_schema()

        def forbidden(*args, **kwargs):
            pytest.fail("Reading an existing/missing read-only Schema must not take a KB write lock")

        monkeypatch.setattr(module, "knowledge_mutation_lock", forbidden)
        assert service.active_schema().id == active.id
        assert service.active_schema(create_default=False).id == active.id
        assert service.ensure_total_schema().id == active.id
        with pytest.raises(ValueError):
            service.active_schema(graph_kind="functional", create_default=False)


def test_two_postgresql_api_threads_create_unique_versions_and_activate_without_500(postgres_schema_engine, monkeypatch):
    from app.api.v1 import graph_governance as routes
    from app.models.user import User

    engine = postgres_schema_engine
    with Session(engine) as db:
        actor = User(id="schema-editor", username="schema-editor", password_hash="synthetic-hash", role="admin")
        db.add(actor)
        db.commit()
        db.refresh(actor)
        db.expunge(actor)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")

    def session():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[routes.get_db] = session
    app.dependency_overrides[routes.get_current_user] = lambda: actor
    original_next = GraphGovernanceService._next_schema_version
    original_kind = GraphGovernanceService.schema_change_kind

    def slow_next(self):
        version = original_next(self)
        time.sleep(0.05)  # Force the old max+1 race if the outer write lock is lost.
        return version

    def slow_kind(previous, schema):
        kind = original_kind(previous, schema)
        time.sleep(0.05)
        return kind

    monkeypatch.setattr(GraphGovernanceService, "_next_schema_version", slow_next)
    monkeypatch.setattr(GraphGovernanceService, "schema_change_kind", staticmethod(slow_kind))
    path = f"/api/v1/knowledge-bases/{KB}/knowledge-graph/schemas"
    payload = {"entity_types": ["SYSTEM"], "relation_types": ["DEPENDS_ON"], "normalization_rules": {}}

    with TestClient(app, raise_server_exceptions=False) as client:
        def parallel(paths, body=None):
            barrier = threading.Barrier(2)

            def post(url):
                barrier.wait(timeout=5)
                return client.post(url, json=body)

            with ThreadPoolExecutor(max_workers=2) as executor:
                return list(executor.map(post, paths))

        responses = parallel([path, path], payload)
        assert [response.status_code for response in responses] == [201, 201], [response.text for response in responses]
        drafts = [response.json() for response in responses]
        versions = sorted(item["version"] for item in drafts)
        assert versions[1] == versions[0] + 1
        responses = parallel([path + "/" + item["id"] + "/activate" for item in drafts])
        assert [response.status_code for response in responses] == [200, 200], [response.text for response in responses]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == KB, GraphSchemaDefinition.status == "active")) == 1
        assert db.scalar(select(func.count()).select_from(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == KB, GraphSchemaDefinition.status == "superseded")) == 1


@pytest.mark.parametrize("kinds", [("general", "general")])
def test_missing_default_schema_double_checks_under_postgresql_lock(postgres_schema_engine, monkeypatch, kinds):
    engine = postgres_schema_engine
    barrier = threading.Barrier(2)
    calls = threading.local()
    original = GraphGovernanceService._read_active_schema

    def read(self, kind):
        result = original(self, kind)
        if not getattr(calls, "read_once", False):
            calls.read_once = True
            assert result is None
            barrier.wait(timeout=5)  # Both callers observe missing before either creates.
        return result

    monkeypatch.setattr(GraphGovernanceService, "_read_active_schema", read)

    def create(kind):
        with Session(engine) as db:
            schema = GraphGovernanceService(db, KB, task_queue=object()).active_schema(graph_kind=kind)
            return schema.id, schema.version

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, kinds))
    assert (results[0][0] == results[1][0]) == (kinds[0] == kinds[1])
    if kinds[0] != kinds[1]:
        assert results[0][1] != results[1][1]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(GraphSchemaDefinition)) == len(set(kinds))


def test_startup_reader_does_not_wait_for_worker_knowledge_lock(postgres_schema_engine):
    engine = postgres_schema_engine
    with Session(engine) as db:
        identifier = GraphGovernanceService(db, KB, task_queue=object()).active_schema().id
    acquired, release = threading.Event(), threading.Event()

    def worker():
        with knowledge_mutation_lock(KB, timeout_seconds=3):
            acquired.set()
            assert release.wait(timeout=10)

    def reader():
        # Simulates startup holding its global seed lock or a separate MCP
        # request while the worker is waiting for that request's response.
        with database_startup_lock(engine) as connection:
            with Session(bind=connection) as db:
                service = GraphGovernanceService(db, KB, task_queue=object())
                missing = GraphGovernanceService(db, "no-schema-kb", task_queue=object())
                return service.ensure_total_schema().id, missing.active_schema(create_default=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        held = executor.submit(worker)
        assert acquired.wait(timeout=3)
        future = executor.submit(reader)
        try:
            assert future.result(timeout=2) == (identifier, None)
        finally:
            release.set()
        held.result(timeout=3)


def test_stale_schema_identity_cannot_edit_a_version_activated_by_another_session(postgres_schema_engine):
    engine = postgres_schema_engine
    with Session(engine) as db:
        draft = GraphGovernanceService(db, KB, task_queue=object()).create_schema(
            entity_types=["SYSTEM"], relation_types=["DEPENDS_ON"], normalization_rules={},
            extraction_prompt=None, created_by=None)
        identifier = draft.id
    with Session(engine, expire_on_commit=False) as stale:
        old = stale.get(GraphSchemaDefinition, identifier)
        assert old.status == "draft"
        with Session(engine) as writer:
            # Also prove the public short writer is reentrant under a worker's
            # already-held PostgreSQL session lock.
            with knowledge_mutation_lock(KB):
                GraphGovernanceService(writer, KB, task_queue=object()).activate_schema(identifier)
        service = GraphGovernanceService(stale, KB, task_queue=object())
        with pytest.raises(ValueError, match="仅草稿"):
            service.update_schema(identifier, entity_types=["PERSON"], relation_types=["DEPENDS_ON"],
                                  normalization_rules={}, extraction_prompt="must not overwrite an active version")
        assert stale.get(GraphSchemaDefinition, identifier).status == "active"
    with Session(engine) as db:
        assert db.get(GraphSchemaDefinition, identifier).entity_types_json == ["SYSTEM"]

