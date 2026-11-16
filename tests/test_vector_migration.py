"""Offline migration preserves original vectors without invoking a model."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from rag.providers.interfaces import VectorIndexRef
from rag.providers.pgvector_store import PgVectorStoreProvider, vector_records_hash
from scripts.migrate_vectors_to_pgvector import migrate_collections, source_snapshot
from tests.test_pgvector_provider import pgvector_engine, Embedding


class SourceCollection:
    def __init__(self, name, records):
        self.name, self.records = name, records

    def count(self):
        return len(self.records)

    def get(self, ids=None, include=None):
        rows = [row for row in self.records if ids is None or row["id"] in ids]
        # Return a different order to prove export preserves ID/vector mapping.
        return {key: [row[field] for row in reversed(rows)] for key, field in (
            ("ids", "id"), ("documents", "document"), ("metadatas", "metadata"), ("embeddings", "embedding"))}


class Source:
    def __init__(self, collections):
        self.collections = {collection.name: collection for collection in collections}

    def list_collections(self):
        return [SimpleNamespace(name=name) for name in self.collections]

    def get_collection(self, name, **kwargs):
        return self.collections[name]


def records():
    return [{"id": "chunk:α", "document": "原文内容", "metadata": {"document_id": "doc-a", "rank": 1}, "embedding": [1.0, 0.5, 0]},
            {"id": "chunk:中", "document": "另一条原文", "metadata": {"document_id": "doc-b", "flag": True}, "embedding": [0, 1.0, 0]}]


def seed_states(engine, names):
    from app.models import Base
    from app.models.knowledge_base import KnowledgeBase
    from app.models.index_job import KnowledgeIndexState
    from app.models.code_graph import EntityVectorIndexState, RelationVectorIndexState, CodeVectorIndexState
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id="migration-kb", name="Migration", slug="migration"))
        db.commit()
        for model, key, name in zip((KnowledgeIndexState, EntityVectorIndexState, RelationVectorIndexState, CodeVectorIndexState),
            ("active_collection_name", "index_name", "index_name", "index_name"), names):
            db.add(model(knowledge_base_id="migration-kb", **{key: name}))
        db.commit()


def test_four_collection_types_preserve_vectors_metadata_and_idempotency(pgvector_engine):
    names = ["documents_v1", "entities_v1", "relations_v1", "code_v1"]
    seed_states(pgvector_engine, names)
    source = Source([SourceCollection(name, records()) for name in names] + [SourceCollection("old_shadow", records())])
    provider = PgVectorStoreProvider(pgvector_engine, batch_size=1)
    with pytest.raises(ValueError, match="Historical"):
        migrate_collections(source, provider)
    assert provider.list_indexes() == []
    report = migrate_collections(source, provider, active_only=True, batch_size=1)
    assert report["imported_collections"] == 4 and report["preserved_collections"] == 1 and report["models_called"] is False
    for name in names:
        store = provider.open(None, VectorIndexRef(name, "migration-kb"))
        exported = store.get(include=["documents", "metadatas", "embeddings"])
        assert exported["ids"] == [item["id"] for item in records()]
        assert exported["documents"] == [item["document"] for item in records()]
        assert exported["metadatas"] == [item["metadata"] for item in records()]
        assert exported["embeddings"] == [item["embedding"] for item in records()]
    assert migrate_collections(source, provider, active_only=True)["verified_collections"] == 4
    provider.open(Embedding(), VectorIndexRef(names[0], "migration-kb")).add_texts(["changed"], ids=[records()[0]["id"]])
    with pytest.raises(ValueError, match="refusing overwrite"):
        migrate_collections(source, provider, active_only=True)


def test_failed_digest_or_generator_rolls_back_new_collection(pgvector_engine):
    provider = PgVectorStoreProvider(pgvector_engine, batch_size=1)
    ref = VectorIndexRef("rollback", "kb-a")
    digest = vector_records_hash(records())
    with pytest.raises(ValueError, match="verification"):
        provider.import_records(ref, iter(records()), expected_count=2, expected_hash="0" * 64)
    assert provider.list_indexes() == []
    def interrupted():
        yield records()[0]
        raise RuntimeError("source interrupted")
    with pytest.raises(RuntimeError):
        provider.import_records(ref, interrupted(), expected_count=2, expected_hash=digest)
    assert provider.list_indexes() == []
    assert provider.import_records(ref, iter(records()), expected_count=2, expected_hash=digest)["status"] == "imported"


def test_active_only_explicitly_reports_source_with_no_index_references(pgvector_engine):
    seed_states(pgvector_engine, [])
    report = migrate_collections(Source([SourceCollection("orphan", records())]), PgVectorStoreProvider(pgvector_engine), active_only=True)
    assert report["preserved_collections"] == 1 and report["imported_collections"] == 0
    assert report["collections"][0]["status"] == "preserved_in_source"


def test_real_chroma_snapshot_import_never_changes_original_files(pgvector_engine, tmp_path):
    import chromadb
    from chromadb.config import Settings
    names = ["real_documents", "real_entities", "real_relations", "real_code"]
    seed_states(pgvector_engine, names)
    source = tmp_path / "original"
    original = chromadb.PersistentClient(path=str(source), settings=Settings(anonymized_telemetry=False))
    try:
        for name in names:
            collection = original.create_collection(name, embedding_function=None)
            collection.add(ids=[row["id"] for row in records()], documents=[row["document"] for row in records()],
                metadatas=[row["metadata"] for row in records()], embeddings=[row["embedding"] for row in records()])
        before = {path.relative_to(source): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
        with source_snapshot(source) as snapshot:
            report = migrate_collections(snapshot, PgVectorStoreProvider(pgvector_engine), active_only=True, batch_size=1)
        assert report["imported_collections"] == 4
        with pgvector_engine.connect() as connection:
            from sqlalchemy import text
            schema = connection.scalar(text("SELECT current_schema()"))
        target = pgvector_engine.url.update_query_dict({"options": f"-csearch_path={schema},public"})
        report_file = tmp_path / "migration-report.json"
        command = [sys.executable, "-m", "scripts.migrate_vectors_to_pgvector", "--source", str(source),
            "--database-url", target.render_as_string(hide_password=False), "--active-only", "--batch-size", "1", "--report", str(report_file)]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
            text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        saved = json.loads(report_file.read_text(encoding="utf-8"))
        assert saved["status"] == "completed" and saved["verified_collections"] == 4 and saved["models_called"] is False
        assert json.loads(result.stdout)["source_preserved"] == str(source.resolve())
        assert before == {path.relative_to(source): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
    finally:
        original._system.stop()
