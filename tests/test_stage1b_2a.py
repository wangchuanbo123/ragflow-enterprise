from types import SimpleNamespace

from app.adapters.local import InlineTaskQueue, LocalObjectStore
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.repositories.document_management_repository import DocumentManagementRepository
from app.services.document_index_service import DocumentIndexService
from rag.indexing.index_manager import IndexManager
from rag.knowledge_graph.schemas import ChunkExtraction, ExtractedEntity
from rag.knowledge_graph.sqlite_store import SqliteGraphStore
from rag.retrievers.bm25_retriever import ChunkBM25Retriever


SECOND_KB_ID = "00000000-0000-0000-0000-000000000088"


def _add_knowledge_bases(db):
    db.add_all(
        [
            KnowledgeBase(
                id=DEFAULT_KNOWLEDGE_BASE_ID,
                name="Default",
                slug="default",
                is_default=True,
                is_active=True,
            ),
            KnowledgeBase(
                id=SECOND_KB_ID,
                name="Second",
                slug="second-scope",
                is_default=False,
                is_active=True,
            ),
        ]
    )
    db.commit()


def _add_ready_document(db, kb_id: str, document_id: str, chunk_id: str, text: str):
    document = KnowledgeDocument(
        id=document_id,
        knowledge_base_id=kb_id,
        source_path=f"{document_id}.txt",
        original_filename=f"{document_id}.txt",
        file_hash=f"hash-{document_id}",
        file_size=len(text),
        status="ready",
    )
    chunk = KnowledgeChunk(
        id=chunk_id,
        knowledge_base_id=kb_id,
        document_id=document_id,
        file_hash=document.file_hash,
        chunk_index=0,
        content=text,
        content_hash=f"content-{chunk_id}",
        token_count=2,
    )
    db.add_all([document, chunk])
    db.commit()
    return document, chunk


def test_graph_entities_with_same_name_are_isolated_by_knowledge_base(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _add_knowledge_bases(db)
        default_doc, default_chunk = _add_ready_document(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            "default-doc",
            "default-chunk",
            "SharedEntity",
        )
        second_doc, second_chunk = _add_ready_document(
            db,
            SECOND_KB_ID,
            "second-doc",
            "second-chunk",
            "SharedEntity",
        )
        extraction_default = ChunkExtraction(
            chunk_id=default_chunk.id,
            entities=[
                ExtractedEntity(local_id="e1", name="SharedEntity", type="MODULE")
            ],
            relations=[],
        )
        extraction_second = extraction_default.model_copy(
            update={"chunk_id": second_chunk.id}
        )

        default_store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
        second_store = SqliteGraphStore(db, SECOND_KB_ID)
        default_store.upsert_extraction(
            {
                "chunk_id": default_chunk.id,
                "document_id": default_doc.id,
                "content": default_chunk.content,
            },
            extraction_default,
        )
        second_store.upsert_extraction(
            {
                "chunk_id": second_chunk.id,
                "document_id": second_doc.id,
                "content": second_chunk.content,
            },
            extraction_second,
        )

        default_entity = default_store.search_entities("SharedEntity")[0]
        second_entity = second_store.search_entities("SharedEntity")[0]
        assert default_entity["id"] != second_entity["id"]
        assert default_store.stats()["entities"] == 1
        assert second_store.stats()["entities"] == 1


def test_bm25_indexes_are_isolated_per_knowledge_base(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _add_knowledge_bases(db)
        _add_ready_document(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            "default-doc",
            "default-chunk",
            "alpha-only-token",
        )
        _add_ready_document(
            db,
            SECOND_KB_ID,
            "second-doc",
            "second-chunk",
            "beta-only-token",
        )
        default_bm25 = ChunkBM25Retriever(DEFAULT_KNOWLEDGE_BASE_ID)
        second_bm25 = ChunkBM25Retriever(SECOND_KB_ID)
        default_bm25.build(db)
        second_bm25.build(db)

        assert default_bm25.chunk_count == 1
        assert second_bm25.chunk_count == 1
        assert default_bm25._docs[0].metadata["knowledge_base_id"] == DEFAULT_KNOWLEDGE_BASE_ID
        assert second_bm25._docs[0].metadata["knowledge_base_id"] == SECOND_KB_ID


def test_document_versions_are_immutable_and_not_classified_as_documents(
    db_engine,
    tmp_path,
):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _add_knowledge_bases(db)
        object_store = LocalObjectStore(tmp_path)
        service = DocumentIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=object_store,
            task_queue=InlineTaskQueue(),
        )
        _, document_id = service.create_upload_job("guide.txt", b"version one")
        document = service.get_document(document_id)
        _, version_two = service.create_document_version(
            document_id,
            "guide.txt",
            b"version two",
        )

        versions = service.management_repo.list_versions(document_id)
        assert [version.version_no for version in versions] == [2, 1]
        assert version_two.status == "active"
        assert versions[1].status == "superseded"
        with object_store.materialize(versions[1].object_key) as path:
            assert path.read_bytes() == b"version one"
        with object_store.materialize(versions[0].object_key) as path:
            assert path.read_bytes() == b"version two"
        with object_store.materialize(document.source_path) as path:
            assert path.read_bytes() == b"version two"

        classification = IndexManager(
            db,
            embedding=object(),
            vector_provider=SimpleNamespace(),
        ).classify_files(tmp_path)
        classified_paths = [
            item["source_path"]
            for group in classification.values()
            for item in group
        ]
        assert classified_paths == [document.source_path]


def test_folder_tag_chunk_and_batch_management_are_scoped(db_engine, tmp_path):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _add_knowledge_bases(db)
        service = DocumentIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=LocalObjectStore(tmp_path),
            task_queue=InlineTaskQueue(),
        )
        _, first_id = service.create_upload_job("first.txt", b"first")
        _, second_id = service.create_upload_job("second.txt", b"second")
        management = DocumentManagementRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        folder = management.create_folder(name="Policies")
        tag = management.create_tag(name="approved", color="#00aa00")

        tagged = service.request_batch(
            [first_id, second_id],
            "tag",
            tag_ids=[tag.id],
        )
        moved = service.request_batch(
            [first_id, second_id],
            "move",
            folder_id=folder.id,
        )
        assert tagged["updated"] == 2
        assert moved["updated"] == 2
        assert {
            document.id
            for document in service.list_documents(
                folder_id=folder.id,
                tag_ids=[tag.id],
            )
        } == {first_id, second_id}

        first = service.get_document(first_id)
        first.status = "ready"
        db.add(
            KnowledgeChunk(
                id="viewable-chunk",
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                document_id=first_id,
                file_hash=first.file_hash,
                chunk_index=0,
                content="chunk content",
                content_hash="viewable-content",
                token_count=2,
            )
        )
        db.commit()
        assert management.list_chunks(first_id)[0].content == "chunk content"

        batch_job = service.request_batch([first_id, second_id], "reindex")
        assert batch_job.total_items == 2
        assert len(service.job_repo.list_items(batch_job.id)) == 2

