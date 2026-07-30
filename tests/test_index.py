"""索引一致性测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document


class FakeCollection:
    def __init__(self):
        self.records: dict[str, Document] = {}

    def count(self):
        return len(self.records)

    def get(self, include=None, where=None):
        del include
        if where is None:
            return {"ids": list(self.records)}
        return {
            "ids": [
                chunk_id
                for chunk_id, doc in self.records.items()
                if all(
                    doc.metadata.get(key) == value
                    for key, value in where.items()
                )
            ]
        }

    def delete(self, ids=None, where=None):
        if ids:
            for chunk_id in ids:
                self.records.pop(chunk_id, None)
        if where:
            for chunk_id, doc in list(self.records.items()):
                if all(doc.metadata.get(key) == value for key, value in where.items()):
                    self.records.pop(chunk_id, None)


class FakeChromaClient:
    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, name, metadata=None):
        del metadata
        return self.collections.setdefault(name, FakeCollection())

    def get_collection(self, name):
        if name not in self.collections:
            raise ValueError(name)
        return self.collections[name]

    def delete_collection(self, name):
        if name not in self.collections:
            raise ValueError(name)
        del self.collections[name]

    def list_collections(self):
        return list(self.collections.values())


class FakeChromaStore:
    def __init__(self, collection: FakeCollection, fail=False):
        self.collection = collection
        self.fail = fail

    def add_documents(self, documents, ids=None):
        if self.fail:
            raise RuntimeError("embedding failed")
        ids = ids or [doc.metadata["chunk_id"] for doc in documents]
        for chunk_id, doc in zip(ids, documents):
            self.collection.records[chunk_id] = doc


def _attach_fake_chroma(manager, monkeypatch, *, fail=False):
    client = FakeChromaClient()
    manager._chroma_client = client

    def make_store(collection_name):
        collection = client.get_or_create_collection(collection_name)
        return FakeChromaStore(collection, fail=fail)

    monkeypatch.setattr(manager, "_make_chroma_store", make_store)
    return client


def test_file_classification(db_engine):
    """测试文件同步分类逻辑。"""
    from app.core.database import SessionLocal
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        mgr = IndexManager(db, embedding=object())

        # Create a temp doc dir
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "a.txt").write_text("content A", encoding="utf-8")
            (tmpdir / "b.txt").write_text("content B", encoding="utf-8")

            classification = mgr.classify_files(tmpdir)
            assert len(classification["added"]) == 2
            assert len(classification["modified"]) == 0
            assert len(classification["deleted"]) == 0


def test_index_state_singleton(db_engine):
    """测试索引状态单例。"""
    from app.core.database import SessionLocal
    from app.repositories.knowledge_repository import KnowledgeRepository

    with SessionLocal() as db:
        repo = KnowledgeRepository(db)
        state1 = repo.get_index_state()
        state2 = repo.get_index_state()
        assert state1.id == 1
        assert state2.id == 1

        repo.set_active_collection("test_collection")
        state3 = repo.get_index_state()
        assert state3.active_collection_name == "test_collection"


def test_chunk_repository_crud(db_engine):
    """测试 Chunk CRUD。"""
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.repositories.knowledge_repository import KnowledgeRepository

    with SessionLocal() as db:
        repo = KnowledgeRepository(db)

        doc = KnowledgeDocument(
            source_path="test.txt",
            original_filename="test.txt",
            file_hash="abc123",
            file_size=100,
            status="ready",
            chunk_count=2,
            embedding_provider="ollama",
            embedding_model="test",
        )
        repo.upsert_document(doc)

        chunk1 = KnowledgeChunk(
            id="chunk_id_1",
            document_id=doc.id,
            file_hash="abc123",
            chunk_index=0,
            content="content 1",
            content_hash="ch1",
            token_count=10,
        )
        chunk2 = KnowledgeChunk(
            id="chunk_id_2",
            document_id=doc.id,
            file_hash="abc123",
            chunk_index=1,
            content="content 2",
            content_hash="ch2",
            token_count=10,
        )
        repo.add_chunks([chunk1, chunk2])

        chunks = repo.list_chunks_for_document(doc.id)
        assert len(chunks) == 2

        active = repo.list_all_active_chunks()
        assert len(active) == 2

        deleted = repo.delete_chunks_for_document(doc.id)
        assert deleted == 2


def test_failed_document_is_classified_as_modified(db_engine, tmp_path):
    from app.core.config import EMBEDDING_MODEL, EMBEDDING_PROVIDER, INDEX_SCHEMA_VERSION
    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager
    from rag.utils.file_hash import file_hash

    path = tmp_path / "failed.txt"
    path.write_text("same content", encoding="utf-8")

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            source_path="failed.txt",
            original_filename="failed.txt",
            file_hash=file_hash(path),
            file_size=path.stat().st_size,
            status="failed",
            index_schema_version=INDEX_SCHEMA_VERSION,
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model=EMBEDDING_MODEL,
        )
        db.add(doc)
        db.commit()

        classification = IndexManager(db, embedding=object()).classify_files(tmp_path)
        assert [item["source_path"] for item in classification["modified"]] == [
            "failed.txt"
        ]
        assert classification["unchanged"] == []


def test_failed_update_keeps_old_chunks_and_vectors(
    db_engine, tmp_path, monkeypatch
):
    from app.core.config import EMBEDDING_MODEL, EMBEDDING_PROVIDER
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager
    from rag.utils.file_hash import file_hash

    path = tmp_path / "doc.txt"
    path.write_text("new content that should fail", encoding="utf-8")

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="doc-1",
            source_path="doc.txt",
            original_filename="doc.txt",
            file_hash="old-hash",
            file_size=10,
            status="ready",
            chunk_count=1,
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model=EMBEDDING_MODEL,
        )
        chunk = KnowledgeChunk(
            id="old-chunk",
            document_id=doc.id,
            file_hash="old-hash",
            chunk_index=0,
            content="old content",
            content_hash="old-content-hash",
            token_count=2,
        )
        db.add_all([doc, chunk])
        db.commit()

        manager = IndexManager(db, embedding=object())
        client = _attach_fake_chroma(manager, monkeypatch, fail=True)
        old_collection = client.get_or_create_collection("active")
        old_collection.records["old-chunk"] = Document(
            page_content="old content",
            metadata={"chunk_id": "old-chunk", "document_id": doc.id},
        )

        with pytest.raises(RuntimeError, match="embedding failed"):
            manager.index_document(
                "doc.txt",
                path,
                file_hash(path),
                "active",
                document_id=doc.id,
            )

        db.expire_all()
        preserved = db.get(KnowledgeDocument, doc.id)
        assert preserved.status == "ready"
        assert preserved.error_message
        assert [c.id for c in manager.repo.list_chunks_for_document(doc.id)] == [
            "old-chunk"
        ]
        assert set(old_collection.records) == {"old-chunk"}


def test_rebuild_indexes_unchanged_documents_before_switch(
    db_engine, tmp_path, monkeypatch
):
    from app.core.config import (
        EMBEDDING_MODEL,
        EMBEDDING_PROVIDER,
        INDEX_SCHEMA_VERSION,
    )
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager
    from rag.utils.file_hash import file_hash

    path = tmp_path / "unchanged.txt"
    path.write_text("unchanged document content", encoding="utf-8")

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="doc-unchanged",
            source_path="unchanged.txt",
            original_filename="unchanged.txt",
            file_hash=file_hash(path),
            file_size=path.stat().st_size,
            status="ready",
            chunk_count=1,
            index_schema_version=INDEX_SCHEMA_VERSION,
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model=EMBEDDING_MODEL,
        )
        db.add(doc)
        db.add(KnowledgeChunk(
            id="old-chunk",
            document_id=doc.id,
            file_hash=doc.file_hash,
            chunk_index=0,
            content="old content",
            content_hash="old-hash",
            token_count=2,
        ))
        db.commit()

        manager = IndexManager(db, embedding=object())
        client = _attach_fake_chroma(manager, monkeypatch)
        old_collection = client.get_or_create_collection("old-active")
        old_collection.records["old-chunk"] = Document(
            page_content="old content",
            metadata={"chunk_id": "old-chunk", "document_id": doc.id},
        )
        manager.repo.set_active_collection("old-active")

        result = manager.rebuild(tmp_path, job_id="safe")

        assert result["status"] == "completed"
        assert result["documents_indexed"] == 1
        assert result["total_chunks"] > 0
        assert manager.repo.get_index_state().active_collection_name == (
            "documents_build_safe"
        )
        active_chunks = manager.repo.list_all_active_chunks()
        new_collection = client.get_collection("documents_build_safe")
        assert new_collection.count() == len(active_chunks)
        assert set(new_collection.get()["ids"]) == {
            chunk.id for chunk in active_chunks
        }
        assert set(old_collection.records) == {"old-chunk"}


def test_failed_rebuild_keeps_old_database_and_active_collection(
    db_engine, tmp_path, monkeypatch
):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager

    path = tmp_path / "doc.txt"
    path.write_text("content", encoding="utf-8")

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="old-doc",
            source_path="doc.txt",
            original_filename="doc.txt",
            file_hash="outdated",
            file_size=7,
            status="ready",
            chunk_count=1,
            embedding_provider="old",
            embedding_model="old",
        )
        db.add(doc)
        db.add(KnowledgeChunk(
            id="old-chunk",
            document_id=doc.id,
            file_hash="outdated",
            chunk_index=0,
            content="old content",
            content_hash="old-content",
            token_count=2,
        ))
        db.commit()

        manager = IndexManager(db, embedding=object())
        client = _attach_fake_chroma(manager, monkeypatch, fail=True)
        old_collection = client.get_or_create_collection("old-active")
        old_collection.records["old-chunk"] = Document(
            page_content="old content",
            metadata={"chunk_id": "old-chunk", "document_id": doc.id},
        )
        manager.repo.set_active_collection("old-active")

        result = manager.rebuild(tmp_path, job_id="failed")

        assert result["status"] == "failed"
        assert manager.repo.get_index_state().active_collection_name == "old-active"
        assert manager.repo.list_chunk_ids_for_document(doc.id) == ["old-chunk"]
        assert set(old_collection.records) == {"old-chunk"}
        assert "documents_build_failed" not in client.collections


def test_upload_job_keeps_preallocated_document_id(
    db_engine, tmp_path, monkeypatch
):
    from app.core.database import SessionLocal
    from app.services import document_index_service
    from app.services.document_index_service import DocumentIndexService

    monkeypatch.setattr(document_index_service, "DOC_DIR", tmp_path)

    with SessionLocal() as db:
        service = DocumentIndexService(db)
        job, document_id = service.create_upload_job(
            "manual.txt",
            b"uploaded content",
        )
        document = service.get_document(document_id)
        items = service.job_repo.list_items(job.id)

        assert document is not None
        assert document.id == document_id
        assert document.status == "pending"
        assert items[0].document_id == document_id
        assert items[0].source_path == document.source_path


def test_readiness_rejects_vector_chunk_count_mismatch(
    db_engine, monkeypatch
):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from app.repositories.knowledge_repository import KnowledgeRepository
    from app.services import readiness_service

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="ready-doc",
            source_path="ready.txt",
            original_filename="ready.txt",
            file_hash="hash",
            file_size=10,
            status="ready",
            chunk_count=1,
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add(doc)
        db.add(KnowledgeChunk(
            id="sqlite-chunk",
            document_id=doc.id,
            file_hash="hash",
            chunk_index=0,
            content="content",
            content_hash="content-hash",
            token_count=1,
        ))
        db.commit()
        KnowledgeRepository(db).set_active_collection("active")

    fake_client = FakeChromaClient()
    active = fake_client.get_or_create_collection("active")
    active.records["vector-1"] = Document(page_content="1", metadata={})
    active.records["vector-2"] = Document(page_content="2", metadata={})

    monkeypatch.setitem(
        __import__("sys").modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda path: fake_client),
    )

    result = readiness_service._check_vector_store()
    assert result["status"] == "fail"
    assert result["vector_count"] == 2
    assert result["chunk_count"] == 1


def test_partial_vector_batch_is_cleaned_up(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from rag.indexing import index_manager as index_module
    from rag.indexing.index_manager import IndexManager, PreparedDocument

    with SessionLocal() as db:
        manager = IndexManager(db, embedding=object())
        client = FakeChromaClient()
        manager._chroma_client = client
        collection = client.get_or_create_collection("active")

        class FailSecondBatchStore:
            def __init__(self):
                self.calls = 0

            def add_documents(self, documents, ids=None):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("second batch failed")
                for chunk_id, doc in zip(ids or [], documents):
                    collection.records[chunk_id] = doc

        store = FailSecondBatchStore()
        monkeypatch.setattr(index_module, "INGEST_BATCH_SIZE", 1)
        monkeypatch.setattr(
            manager,
            "_make_chroma_store",
            lambda collection_name: store,
        )
        prepared = PreparedDocument(
            document_id="doc-1",
            source_path="doc.txt",
            original_filename="doc.txt",
            file_hash="hash",
            file_size=10,
            vector_documents=[
                Document(
                    page_content="one",
                    metadata={"chunk_id": "chunk-1", "document_id": "doc-1"},
                ),
                Document(
                    page_content="two",
                    metadata={"chunk_id": "chunk-2", "document_id": "doc-1"},
                ),
            ],
            chunk_records=[],
        )

        with pytest.raises(RuntimeError, match="second batch failed"):
            manager._write_prepared_vectors(prepared, "active")

        assert collection.records == {}


def test_rollback_rejects_collection_that_does_not_match_sqlite(
    db_engine, monkeypatch
):
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="doc-rollback",
            source_path="rollback.txt",
            original_filename="rollback.txt",
            file_hash="hash",
            file_size=10,
            status="ready",
            chunk_count=1,
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add_all(
            [
                doc,
                KnowledgeChunk(
                    id="current-chunk",
                    document_id=doc.id,
                    file_hash="hash",
                    chunk_index=0,
                    content="current",
                    content_hash="content-hash",
                    token_count=1,
                ),
            ]
        )
        db.commit()

        manager = IndexManager(db, embedding=object())
        client = _attach_fake_chroma(manager, monkeypatch)
        manager.repo.set_active_collection("current")
        incompatible = client.get_or_create_collection("old")
        incompatible.records["old-chunk"] = Document(
            page_content="old",
            metadata={"chunk_id": "old-chunk", "document_id": doc.id},
        )

        result = manager.rollback_collection("old")

        assert result["status"] == "error"
        assert manager.repo.get_index_state().active_collection_name == "current"


def test_upload_rejects_unsupported_type_and_reuses_duplicate_path(
    db_engine, tmp_path, monkeypatch
):
    import hashlib

    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument
    from app.services import document_index_service
    from app.services.document_index_service import DocumentIndexService
    from rag.errors import DocumentValidationError

    monkeypatch.setattr(document_index_service, "DOC_DIR", tmp_path)

    with SessionLocal() as db:
        service = DocumentIndexService(db)
        with pytest.raises(DocumentValidationError, match="Unsupported"):
            service.register_upload("payload.exe", b"payload")

        first_id, first_path = service.register_upload(
            "contract.docx",
            b"same-content",
        )
        document = KnowledgeDocument(
            id=first_id,
            source_path=first_path.relative_to(tmp_path).as_posix(),
            original_filename=first_path.name,
            file_hash=hashlib.sha256(b"same-content").hexdigest(),
            file_size=len(b"same-content"),
            status="ready",
            embedding_provider="ollama",
            embedding_model="test",
        )
        db.add(document)
        db.commit()

        duplicate_id, duplicate_path = service.register_upload(
            "renamed.docx",
            b"same-content",
        )

        assert duplicate_id == first_id
        assert duplicate_path == first_path
        assert not (first_path.parent / "renamed.docx").exists()


def test_failed_stale_vector_cleanup_is_retryable(
    db_engine, tmp_path, monkeypatch
):
    from app.core.config import EMBEDDING_MODEL, EMBEDDING_PROVIDER
    from app.core.database import SessionLocal
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_document import KnowledgeDocument
    from rag.indexing.index_manager import IndexManager
    from rag.utils.file_hash import file_hash

    path = tmp_path / "retry.txt"
    path.write_text("new content for cleanup retry", encoding="utf-8")

    with SessionLocal() as db:
        doc = KnowledgeDocument(
            id="retry-doc",
            source_path="retry.txt",
            original_filename="retry.txt",
            file_hash="old-hash",
            file_size=10,
            status="ready",
            chunk_count=1,
            embedding_provider=EMBEDDING_PROVIDER,
            embedding_model=EMBEDDING_MODEL,
        )
        db.add_all(
            [
                doc,
                KnowledgeChunk(
                    id="stale-chunk",
                    document_id=doc.id,
                    file_hash="old-hash",
                    chunk_index=0,
                    content="old",
                    content_hash="old-content",
                    token_count=1,
                ),
            ]
        )
        db.commit()

        manager = IndexManager(db, embedding=object())
        client = _attach_fake_chroma(manager, monkeypatch)
        active = client.get_or_create_collection("active")
        active.records["stale-chunk"] = Document(
            page_content="old",
            metadata={"chunk_id": "stale-chunk", "document_id": doc.id},
        )

        original_delete = active.delete

        def fail_delete(ids=None, where=None):
            raise RuntimeError("delete unavailable")

        active.delete = fail_delete
        with pytest.raises(RuntimeError, match="Failed to delete"):
            manager.index_document(
                "retry.txt",
                path,
                file_hash(path),
                "active",
                document_id=doc.id,
            )

        db.expire_all()
        assert manager.repo.get_by_id(doc.id).error_message
        classification = manager.classify_files(tmp_path)
        assert [item["source_path"] for item in classification["modified"]] == [
            "retry.txt"
        ]

        active.delete = original_delete
        manager.index_document(
            "retry.txt",
            path,
            file_hash(path),
            "active",
            document_id=doc.id,
        )

        current_ids = set(manager.repo.list_chunk_ids_for_document(doc.id))
        assert set(active.records) == current_ids
        assert manager.repo.get_by_id(doc.id).error_message is None
