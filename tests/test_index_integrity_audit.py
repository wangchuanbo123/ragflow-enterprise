"""Admin corpus audits enumerate IDs in batches; readiness only probes storage."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.index_job import KnowledgeIndexState
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.services.readiness_service import audit_index_integrity, _check_vector_connectivity
from rag.providers.interfaces import ProviderHealth, VectorIndexRef
from tests.test_pgvector_provider import pgvector_engine


def seed(engine, *, kb="audit-kb", count=1205, status="ready", collection="active"):
    identifiers = [f"{kb}-chunk-{index:05}" for index in range(count)]
    with Session(engine) as db:
        db.add(KnowledgeBase(id=kb, name=kb, slug=kb, is_active=True))
        db.add(KnowledgeIndexState(knowledge_base_id=kb, active_collection_name=collection))
        db.add(KnowledgeDocument(id=kb + "-doc", knowledge_base_id=kb, source_path=kb + ".txt",
            original_filename="manual.txt", file_hash="hash", status=status, chunk_count=count))
        db.add_all([KnowledgeChunk(id=identifier, knowledge_base_id=kb, document_id=kb + "-doc",
            file_hash="hash", chunk_index=index, content="Synthetic audit fixture", content_hash=str(index), token_count=4)
            for index, identifier in enumerate(identifiers)])
        db.commit()
    return identifiers


def bind_container(monkeypatch, engine, vector):
    @contextmanager
    def session():
        with Session(engine) as db:
            yield db
    container = SimpleNamespace(metadata=SimpleNamespace(session=session), vector_store=vector)
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: container)


class FakeVectors:
    def __init__(self, identifiers):
        self.identifiers = set(identifiers)
        self.batches = []
        self.opened = []

    def health(self, ref, expected_ids=None):
        assert expected_ids is None, "audit must not materialize the whole corpus ID set"
        return ProviderHealth.ok(vector_count=len(self.identifiers))

    def open(self, embedding, ref):
        assert embedding is None, "audit must not load or invoke an embedding model"
        self.opened.append(ref)
        return self

    def get(self, *, ids, include):
        assert include == [], "audit must not export document contents or embeddings"
        self.batches.append(list(ids))
        return {"ids": [identifier for identifier in ids if identifier in self.identifiers]}


def test_audit_checks_more_than_thousand_ids_in_bounded_batches(db_engine, monkeypatch):
    identifiers = seed(db_engine)
    vector = FakeVectors(identifiers)
    bind_container(monkeypatch, db_engine, vector)
    result = audit_index_integrity(batch_size=500)
    assert result["status"] == "ok"
    assert result["knowledge_bases"]["audit-kb"]["chunk_count"] == 1205
    assert list(map(len, vector.batches)) == [500, 500, 205]
    assert [identifier for batch in vector.batches for identifier in batch] == identifiers


def test_equal_counts_cannot_hide_missing_and_unexpected_vectors(db_engine, monkeypatch):
    identifiers = seed(db_engine)
    vector = FakeVectors([*identifiers[2:], "unexpected-a", "unexpected-b"])
    bind_container(monkeypatch, db_engine, vector)
    result = audit_index_integrity(500)
    check = result["knowledge_bases"]["audit-kb"]
    assert result["status"] == check["status"] == "fail"
    assert check["vector_count"] == check["chunk_count"] == 1205
    assert check["missing"] == identifiers[:2]
    assert max(map(len, vector.batches)) == 500


def test_missing_examples_are_bounded_but_all_batches_are_checked(db_engine, monkeypatch):
    identifiers = seed(db_engine)
    vector = FakeVectors([])
    bind_container(monkeypatch, db_engine, vector)
    check = audit_index_integrity(500)["knowledge_bases"]["audit-kb"]
    assert check["missing"] == identifiers[:5]
    assert check["chunk_count"] == 1205
    assert len(vector.batches) == 3


@pytest.mark.parametrize("status, expected", [("ready", "fail"), ("deleted", "ok")])
def test_no_active_collection_only_requires_active_chunks(db_engine, monkeypatch, status, expected):
    seed(db_engine, count=1, status=status, collection=None)
    bind_container(monkeypatch, db_engine, FakeVectors([]))
    assert audit_index_integrity()["status"] == expected


def test_audit_keeps_other_results_when_collection_fails_and_hides_driver_details(db_engine, monkeypatch):
    seed(db_engine, kb="broken", count=1)
    seed(db_engine, kb="healthy", count=0)
    class Broken(FakeVectors):
        def open(self, embedding, ref):
            if ref.knowledge_base_id == "broken":
                raise RuntimeError("SECRET_DSN_OR_PRIVATE_SOURCE")
            return super().open(embedding, ref)
    bind_container(monkeypatch, db_engine, Broken([]))
    result = audit_index_integrity()
    assert result["status"] == "fail"
    assert result["knowledge_bases"]["healthy"]["status"] == "ok"
    assert result["knowledge_bases"]["broken"]["code"] == "RuntimeError"
    assert "SECRET" not in str(result)


@pytest.mark.parametrize("batch_size", [True, False, 0, -1, 2001, 2.5, "500"])
def test_invalid_batch_sizes_are_rejected_before_storage_calls(batch_size):
    with pytest.raises(ValueError, match="batch size"):
        audit_index_integrity(batch_size)


def test_readiness_probe_never_opens_or_counts_the_corpus():
    def forbidden(*args, **kwargs):
        pytest.fail("readiness must not open, export or count a corpus")
    provider = SimpleNamespace(probe=lambda: ProviderHealth.ok(provider="probe"),
        open=forbidden, health=forbidden, list_ids=forbidden, count=forbidden)
    assert _check_vector_connectivity(provider) == {"status": "ok", "provider": "probe"}


def test_real_pgvector_audit_uses_bounded_id_exports_and_detects_same_count_swap(db_engine, pgvector_engine, monkeypatch):
    from rag.providers.pgvector_store import PgVectorStoreProvider
    identifiers = seed(db_engine, count=7)
    provider = PgVectorStoreProvider(pgvector_engine)
    ref = VectorIndexRef("active", "audit-kb", "parser-profile", "embedding-profile")
    store = provider.open(None, ref)
    store.add_embeddings([("synthetic", [1, 0]) for _ in identifiers], ids=identifiers)
    batches = []
    class Recorded:
        health = provider.health
        probe = provider.probe
        def open(self, embedding, index_ref):
            opened = provider.open(embedding, index_ref)
            def get(*, ids, include):
                assert include == []
                batches.append(list(ids))
                return opened.get(ids=ids, include=include)
            return SimpleNamespace(get=get)
    bind_container(monkeypatch, db_engine, Recorded())
    assert audit_index_integrity(3)["status"] == "ok"
    assert list(map(len, batches)) == [3, 3, 1]
    store.delete(ids=[identifiers[0]])
    store.add_embeddings([("unexpected", [0, 1])], ids=["unexpected"])
    check = audit_index_integrity(3)["knowledge_bases"]["audit-kb"]
    assert check["status"] == "fail"
    assert check["chunk_count"] == check["vector_count"] == 7
    assert check["missing"] == identifiers[:1]
