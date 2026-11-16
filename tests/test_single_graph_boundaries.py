"""Single-schema API boundaries and compatibility with old jobs and archives."""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session


def test_new_agent_tasks_cannot_create_professional_schemas():
    from app.schemas.agent_run import WorkflowOptions
    from app.services.knowledge_workflows.execution import WorkflowExecutor
    for kind in ("concept", "functional", "configuration", "standard"):
        with pytest.raises(ValidationError):
            WorkflowOptions(graph_kind=kind)
        historic = {"graph_kind": kind, "apply": True}
        run = SimpleNamespace(knowledge_base_id="kb", document_ids_json=[], options_json=historic)
        executor = WorkflowExecutor(None, run)
        assert executor.options["graph_kind"] == "general"
        assert run.options_json["graph_kind"] == kind


def test_retired_projection_query_parameters_are_rejected(client):
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD})
    base = f"/api/v1/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}/knowledge-graph"
    for kind in ("concept", "functional", "configuration", "standard"):
        assert client.get(base + "/entities", params={"query": "test", "graph_kind": kind}).status_code == 422
        assert client.get(base + "/subgraph", params={"entity_ids": "missing", "graph_kind": kind}).status_code == 422


def test_legacy_schema_job_retries_preserve_history_and_create_total_draft(client):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphSchemaDefinition
    from app.models.index_job import IndexJob
    from app.services.index_worker import _total_schema_options
    with SessionLocal() as db:
        old = GraphSchemaDefinition(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, version=20,
            status="draft", graph_kind="functional", entity_types_json=["SYSTEM"], relation_types_json=["CALLS"])
        db.add(old)
        db.commit()
        job = IndexJob(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, job_type="schema_generation",
            options_json={"graph_kind": "functional", "target_schema_id": old.id, "instructions": "keep my instructions"})
        db.add(job)
        db.commit()
        _total_schema_options(db, job)
        assert job.options_json["graph_kind"] == "general"
        assert job.options_json["target_schema_id"] is None
        assert job.options_json["legacy_target_schema_id"] == old.id
        assert job.options_json["legacy_graph_kind"] == "functional"
        assert job.options_json["instructions"] == "keep my instructions"
        assert old.graph_kind == "functional" and old.status == "draft"
        before = dict(job.options_json)
        _total_schema_options(db, job)
        assert job.options_json == before


def test_old_five_schema_zip_restores_into_single_schema_constraints(db_engine, tmp_path, monkeypatch):
    from app.adapters.local import LocalObjectStore
    from app.models.graph_governance import GraphSchemaDefinition
    from app.models.knowledge_graph import KgEntity
    from app.services.backup_service import BackupService
    from tests.test_backups import _seed
    from tests.test_index import FakeVectorProvider
    container = SimpleNamespace(vector_store=FakeVectorProvider(), embedding=SimpleNamespace(get_model=lambda: object()))
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: container)
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("documents/doc.txt", b"hello")
    with Session(db_engine) as db:
        kb, _, entity = _seed(db)
        entity_id = entity.id
        service = BackupService(db, kb.id, store)
        monkeypatch.setattr(service, "_queue_derived_rebuilds", lambda *args, **kwargs: None)
        backup = service.create("knowledge_base")
        with store.materialize(backup.object_key) as path:
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["manifest.json"])
        kinds = ("general", "concept", "functional", "configuration", "standard")
        rows = [{"id": f"legacy-{kind}", "knowledge_base_id": kb.id, "graph_kind": kind,
                 "version": index + 1, "status": "active", "entity_types_json": ["SYSTEM" if index == 0 else f"TYPE_{index}"],
                 "relation_types_json": [f"REL_{index}"], "generation_method": "manual",
                 "normalization_rules_json": {}, "extraction_prompt": f"preserved instruction {kind}"}
                for index, kind in enumerate(kinds)]
        manifest["tables"]["graph_schema_definitions"] = rows
        files["manifest.json"] = json.dumps(manifest).encode()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        original = payload.getvalue()
        store.put_bytes(backup.object_key, original)
        backup.content_hash = hashlib.sha256(original).hexdigest()
        backup_id, object_key = backup.id, backup.object_key
        db.commit()
        service.restore(backup_id)
        active = list(db.scalars(select(GraphSchemaDefinition).where(GraphSchemaDefinition.knowledge_base_id == kb.id,
                                                                       GraphSchemaDefinition.status == "active")))
        assert len(active) == 1 and active[0].graph_kind == "general"
        assert set(active[0].entity_types_json) == {"SYSTEM", "TYPE_1", "TYPE_2", "TYPE_3", "TYPE_4"}
        assert len(list(db.scalars(select(GraphSchemaDefinition).where(GraphSchemaDefinition.knowledge_base_id == kb.id)))) == 6
        assert db.get(KgEntity, entity_id) is not None
        with store.materialize(object_key) as path:
            assert path.read_bytes() == original
