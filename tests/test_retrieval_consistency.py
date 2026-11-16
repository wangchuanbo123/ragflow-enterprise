"""Regression coverage for snapshot restore, derived code and lexical retrieval."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from sqlalchemy import event, select

from app.adapters.local import LocalObjectStore
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.backup_service import BackupService
from app.services.code_index_service import CodeIndexService
from rag.indexing.index_manager import IndexManager
from tests.test_index import FakeChromaStore, FakeVectorProvider


class DeterministicEmbedding:
    def identity(self):
        from rag.providers.interfaces import ProviderIdentity

        return ProviderIdentity(kind="embedding", provider="test", model="deterministic")

    def get_model(self):
        return self

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0] if "original" in text else [0.0, 1.0]


class SearchableStore(FakeChromaStore):
    def similarity_search_with_relevance_scores(self, query, k, **kwargs):
        documents = list(self.collection.records.values())
        scope = (kwargs.get("filter") or {}).get("document_id", {}).get("$in")
        if scope is not None:
            documents = [doc for doc in documents if doc.metadata.get("document_id") in scope]
        documents.sort(key=lambda doc: query in doc.page_content, reverse=True)
        return [(doc, 0.9) for doc in documents[:k]]


class SearchableProvider(FakeVectorProvider):
    def open(self, embedding, index_ref):
        return SearchableStore(self.client.get_or_create_collection(index_ref.name))


@pytest.fixture
def infrastructure(db_engine, tmp_path, monkeypatch):
    from app.core.database import SessionLocal

    container = SimpleNamespace(
        vector_store=SearchableProvider(),
        object_store=LocalObjectStore(tmp_path / "objects"),
        embedding=DeterministicEmbedding(),
        task_queue=SimpleNamespace(enqueue=lambda job_id: None),
        metadata=SimpleNamespace(session=SessionLocal),
    )
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: container)
    with SessionLocal() as db:
        db.add(KnowledgeBase(id=KB, name="Audit", slug="audit"))
        db.commit()
    return container


def index_text(db, infrastructure, name, content):
    import hashlib

    infrastructure.object_store.put_bytes(name, content.encode())
    manager = IndexManager(
        db, knowledge_base_id=KB, embedding=infrastructure.embedding,
        vector_provider=infrastructure.vector_store, image_sink=object(),
    )
    with infrastructure.object_store.materialize(name) as path:
        manager.index_document(name, path, hashlib.sha256(content.encode()).hexdigest(), "audit_active")
    KnowledgeRepository(db, KB).set_active_collection("audit_active")
    return manager


def test_restore_replaces_vectors_and_removes_post_backup_files(infrastructure, monkeypatch):
    from app.core.database import SessionLocal
    from rag.providers.interfaces import VectorIndexRef

    with SessionLocal() as db:
        manager = index_text(db, infrastructure, "a.txt", "original document")
        service = BackupService(db, KB, infrastructure.object_store)
        monkeypatch.setattr(service, "_queue_derived_rebuilds", lambda *args, **kwargs: None)
        backup = service.create("knowledge_base")
        index_text(db, infrastructure, "b.txt", "POST_BACKUP_SECRET")
        db.add(IndexJob(knowledge_base_id=KB, job_type="sync", status="pending"))
        db.commit()

        service.restore(backup.id)

        repository = KnowledgeRepository(db, KB)
        state = repository.get_index_state()
        assert state.active_collection_name != "audit_active"
        active = infrastructure.vector_store.open(
            infrastructure.embedding, VectorIndexRef(state.active_collection_name, KB),
        )
        assert {item.metadata["source"] for item, _ in active.similarity_search_with_relevance_scores("POST_BACKUP_SECRET", 10)} == {"a.txt"}
        assert infrastructure.vector_store.list_ids(VectorIndexRef(state.active_collection_name, KB)) == set(repository.list_chunk_ids_for_document(repository.get_by_source_path("a.txt").id))
        assert not infrastructure.object_store.exists("b.txt")
        assert manager.classify_files(infrastructure.object_store.root)["added"] == []
        assert db.scalar(select(IndexJob.status).where(IndexJob.job_type == "sync")) == "cancelled"


def test_backup_preserves_local_images_and_saved_chunk_anchors(infrastructure, monkeypatch):
    from app.core.database import SessionLocal
    from rag.context.context_builder import build_context
    from rag.providers.interfaces import VectorIndexRef

    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", "original [[img-1]]")
        repository = KnowledgeRepository(db, KB)
        document = repository.get_by_source_path("a.txt")
        chunk = repository.list_chunks_for_document(document.id)[0]
        key = f"{document.id}/images/img-1.png"
        image = b"\x89PNG\r\n\x1a\nimage-content"
        infrastructure.object_store.put_bytes(key, image)
        chunk.metadata_json = {"images": [{"marker": "img-1", "url": f"/api/v1/objects/{key}", "caption": "architecture"}]}
        db.commit()
        service = BackupService(db, KB, infrastructure.object_store)
        monkeypatch.setattr(service, "_queue_derived_rebuilds", lambda *args, **kwargs: None)
        backup = service.create("knowledge_base")
        manifest, objects = service._read_archive(backup.object_key)
        assert objects[key] == image
        infrastructure.object_store.delete(key)

        service.restore(backup.id)

        with infrastructure.object_store.materialize(key) as path:
            assert path.read_bytes() == image
        active = infrastructure.vector_store.open(infrastructure.embedding, VectorIndexRef(repository.get_index_state().active_collection_name, KB))
        documents = [item for item, _ in active.similarity_search_with_relevance_scores("original", 10)]
        context, sources = build_context(documents)
        assert f"![architecture](/api/v1/objects/{key})" in context
        assert sources[0]["images"][0]["url"] == f"/api/v1/objects/{key}"


def test_restore_failure_keeps_current_files_metadata_and_active_index(infrastructure, monkeypatch):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", "original document")
        service = BackupService(db, KB, infrastructure.object_store)
        backup = service.create("knowledge_base")
        index_text(db, infrastructure, "a.txt", "changed document")
        index_text(db, infrastructure, "b.txt", "new document")
        def fail(*args):
            raise RuntimeError("metadata unavailable")
        monkeypatch.setattr(service, "_restore_knowledge_base", fail)

        with pytest.raises(RuntimeError, match="metadata unavailable"):
            service.restore(backup.id)

        repository = KnowledgeRepository(db, KB)
        assert repository.get_index_state().active_collection_name == "audit_active"
        assert {item.source_path for item in repository.list_documents()} == {"a.txt", "b.txt"}
        with infrastructure.object_store.materialize("a.txt") as path:
            assert path.read_text() == "changed document"
        assert infrastructure.object_store.exists("b.txt")
        assert list(infrastructure.vector_store.client.collections) == ["audit_active"]


def test_backup_rejects_cross_document_image_references(infrastructure):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", "original")
        chunk = KnowledgeRepository(db, KB).list_all_active_chunks()[0]
        chunk.metadata_json = {"images": [{"url": "/api/v1/objects/another-document/images/img-1.png"}]}
        db.commit()
        with pytest.raises(ValueError, match="不属于当前文档"):
            BackupService(db, KB, infrastructure.object_store).create("knowledge_base")


CODE = "```python\ndef secret_function(value):\n    result = value + 1\n    return result\n```"


def test_document_code_scope_origin_delete_and_empty_rebuild(infrastructure):
    from app.core.database import SessionLocal
    from app.repositories.code_graph_repository import CodeGraphRepository
    from rag.code_graph.retriever import CodeGraphRetriever
    from rag.nodes.channel_retrieval_nodes import create_code_retrieve_node

    with SessionLocal() as db:
        manager = index_text(db, infrastructure, "a.txt", CODE)
        document = KnowledgeRepository(db, KB).get_by_source_path("a.txt")
        service = CodeIndexService(db, KB, object_store=infrastructure.object_store, task_queue=infrastructure.task_queue)
        service.index_document_code()
        retriever = CodeGraphRetriever(KB)
        scoped = create_code_retrieve_node(retriever)({
            "original_query": "secret_function", "allowed_document_ids": {document.id},
            "route": {"selected_channels": ["code"]},
        })["code_docs"]
        assert len(scoped) == 1
        assert scoped[0].metadata["document_id"] == document.id
        assert scoped[0].metadata["source"] == "a.txt"
        assert scoped[0].metadata["code_snippets"][0]["origin"] == "document"

        manager.delete_document("a.txt", "audit_active")
        assert retriever.get_relevant_documents("secret_function") == []
        result = service.index_document_code()
        assert result["symbols"] == 0
        assert CodeGraphRepository(db, KB).stats()["symbols"] == 0
        assert retriever.get_relevant_documents("secret_function") == []


def test_code_limits_apply_to_each_document_and_skip_empty_chunks():
    from rag.code_graph.document_source import extract_document_code

    files = extract_document_code([
        ("empty", 0, ""), ("first", 0, CODE), ("first", 1, CODE), ("second", 0, CODE),
    ], max_blocks=1)
    assert len(files) == 2
    assert {file.path.split("/")[1] for file in files} == {"first", "second"}


def test_code_semantic_scope_is_applied_before_candidate_limit(infrastructure):
    from app.core.database import SessionLocal
    from app.services.code_vector_service import CodeVectorService
    from app.repositories.code_graph_repository import CodeGraphRepository

    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", CODE)
        index_text(db, infrastructure, "b.txt", CODE)
        target_id = KnowledgeRepository(db, KB).get_by_source_path("b.txt").id
        CodeIndexService(db, KB, infrastructure.object_store, infrastructure.task_queue).index_document_code()
        service = CodeVectorService(db, KB, container=infrastructure, summary_agent_enabled=False)
        result = service.rebuild()
        assert result["index_name"].startswith("code_symbols_v4_")
        rows = service.semantic_search("secret_function", 1, allowed_document_ids={target_id})
        assert rows and all(row["metadata"]["document_source"]["document_id"] == target_id for row in rows)
        rows = CodeGraphRepository(db, KB).search_symbols("secret_function", 1, allowed_document_ids={target_id})
        assert len(rows) == 1 and rows[0]["metadata"]["document_source"]["document_id"] == target_id


def test_bm25_matches_single_and_common_terms_without_n_plus_one(infrastructure, db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from rag.retrievers.bm25_retriever import ChunkBM25Retriever
    import rag.retrievers.bm25_retriever as bm25_module

    monkeypatch.setattr(bm25_module, "SessionLocal", SessionLocal)
    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", "enterprise retrieval system")
    retriever = ChunkBM25Retriever(KB)
    assert retriever.get_relevant_documents("enterprise")
    assert not retriever.get_relevant_documents("totallyabsent")
    with SessionLocal() as db:
        for number in range(20):
            db.add(KnowledgeDocument(id=f"d{number}", knowledge_base_id=KB, source_path=f"x{number}.txt", original_filename="x.txt", file_hash="x", file_size=1, status="ready"))
            db.add(KnowledgeChunk(id=f"c{number}", knowledge_base_id=KB, document_id=f"d{number}", file_hash="x", chunk_index=0, content="enterprise common", content_hash=f"h{number}"))
        db.commit()
    queries = []
    def record(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    event.listen(db_engine, "before_cursor_execute", record)
    try:
        retriever.build()
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
    # One bounded-capacity COUNT plus the two bulk reads; never a query per
    # document/chunk while resolving source metadata.
    assert len(queries) == 3
    results = retriever.get_relevant_documents("enterprise", top_k=30)
    assert len(results) == 21
    results[0].metadata["source"] = "mutated"
    assert all(doc.metadata["source"] != "mutated" for doc in retriever.get_relevant_documents("enterprise", top_k=30))


def test_graph_retriever_uses_independent_scoped_sessions(monkeypatch):
    import rag.knowledge_graph.retriever as module

    barrier = Barrier(2)
    opened, closed = [], []
    @contextmanager
    def factory():
        session = object()
        opened.append(session)
        try:
            yield session
        finally:
            closed.append(session)
    monkeypatch.setattr(module, "SessionLocal", factory)
    def retrieve(self, db, *args, **kwargs):
        barrier.wait(timeout=5)
        assert db not in closed
        return []
    monkeypatch.setattr(module.KnowledgeGraphRetriever, "_retrieve_with_db", retrieve)
    retriever = module.KnowledgeGraphRetriever(knowledge_base_id=KB)
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(retriever._retrieve, "query", 2) for _ in range(2)]
        assert [future.result() for future in results] == [[], []]
    assert len(opened) == len(closed) == 2
    assert opened[0] is not opened[1]
    assert retriever._db is None


def test_worker_delete_removes_canonical_file_and_rebuilds_code(infrastructure, monkeypatch):
    from app.core.database import SessionLocal
    from app.services.document_index_service import DocumentIndexService
    from app.services.index_worker import _process_job

    monkeypatch.setattr("app.services.graph_governance_service.GraphGovernanceService.request_automatic_build", lambda *args, **kwargs: None)
    with SessionLocal() as db:
        index_text(db, infrastructure, "a.txt", CODE)
        document = KnowledgeRepository(db, KB).get_by_source_path("a.txt")
        CodeIndexService(db, KB, object_store=infrastructure.object_store, task_queue=infrastructure.task_queue).index_document_code()
        job = DocumentIndexService(db, KB, infrastructure.object_store, infrastructure.task_queue).request_delete(document.id)
        job_id = job.id

    assert _process_job(job_id)

    with SessionLocal() as db:
        assert db.get(IndexJob, job_id).status == "completed"
        assert not infrastructure.object_store.exists("a.txt")
        queued = db.scalar(select(IndexJob).where(IndexJob.job_type == "code_index"))
        assert queued is not None and queued.options_json["data_source_id"] == "documents"
        assert BackupService(db, KB, infrastructure.object_store).create("knowledge_base").status == "ready"


def test_worker_cancels_jobs_for_archived_knowledge_bases(infrastructure):
    from app.core.database import SessionLocal
    from app.services.index_worker import _process_job

    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, KB)
        kb.is_active = False
        job = IndexJob(knowledge_base_id=KB, job_type="rebuild", status="pending")
        db.add(job)
        db.commit()
        job_id = job.id
    assert _process_job(job_id)
    with SessionLocal() as db:
        assert db.get(IndexJob, job_id).status == "cancelled"
    assert infrastructure.vector_store.client.collections == {}


def test_real_chroma_restore_never_retrieves_post_backup_content(tmp_path):
    """A child process closes Chroma's Windows file handles before temp cleanup."""
    import subprocess
    import sys

    pytest.importorskip("chromadb")
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.core.database as database
from app.models import Base
from app.models.knowledge_base import KnowledgeBase
from app.adapters.local import LocalObjectStore
from app.services.backup_service import BackupService
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.providers.vector_store import ChromaVectorStoreProvider
from rag.providers.interfaces import VectorIndexRef
from tests.test_retrieval_consistency import DeterministicEmbedding, index_text, KB
root = Path(sys.argv[1])
engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
factory = sessionmaker(bind=engine, autoflush=False)
database.SessionLocal = factory
Base.metadata.create_all(engine)
container = SimpleNamespace(object_store=LocalObjectStore(root / 'objects'), vector_store=ChromaVectorStoreProvider(root / 'chroma'), embedding=DeterministicEmbedding(), task_queue=SimpleNamespace(enqueue=lambda job: None))
with patch('app.bootstrap.container.get_provider_container', lambda: container), factory() as db:
    db.add(KnowledgeBase(id=KB, name='Test', slug='test'))
    db.commit()
    manager = index_text(db, container, 'a.txt', 'original document')
    service = BackupService(db, KB, container.object_store)
    service._queue_derived_rebuilds = lambda *args, **kwargs: None
    backup = service.create('knowledge_base')
    index_text(db, container, 'b.txt', 'POST_BACKUP_SECRET')
    service.restore(backup.id)
    state = KnowledgeRepository(db, KB).get_index_state()
    store = container.vector_store.open(container.embedding, VectorIndexRef(state.active_collection_name, KB))
    results = store.similarity_search('POST_BACKUP_SECRET', k=8)
    assert results and all(item.metadata['source'] == 'a.txt' for item in results)
    assert not container.object_store.exists('b.txt')
    assert manager.classify_files(container.object_store.root)['added'] == []
print('restore verified')
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "restore verified" in result.stdout
