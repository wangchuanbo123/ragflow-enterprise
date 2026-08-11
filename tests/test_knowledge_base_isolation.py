from types import SimpleNamespace

from app.adapters.local import InlineTaskQueue, LocalObjectStore
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.document_index_service import DocumentIndexService
from rag.indexing.index_manager import IndexManager, PreparedDocument


SECOND_KB_ID = "00000000-0000-0000-0000-000000000099"


def _create_knowledge_bases(db):
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
                slug="second-kb",
                is_default=False,
                is_active=True,
            ),
        ]
    )
    db.commit()


def test_document_repository_never_returns_another_knowledge_base(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _create_knowledge_bases(db)
        db.add_all(
            [
                KnowledgeDocument(
                    id="doc-default",
                    knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                    source_path="same.txt",
                    original_filename="same.txt",
                    file_hash="a",
                    file_size=1,
                ),
                KnowledgeDocument(
                    id="doc-second",
                    knowledge_base_id=SECOND_KB_ID,
                    source_path="same.txt",
                    original_filename="same.txt",
                    file_hash="b",
                    file_size=1,
                ),
            ]
        )
        db.commit()

        default_repo = KnowledgeRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        second_repo = KnowledgeRepository(db, SECOND_KB_ID)

        assert default_repo.get_by_source_path("same.txt").id == "doc-default"
        assert second_repo.get_by_source_path("same.txt").id == "doc-second"
        assert default_repo.get_by_id("doc-second") is None
        assert second_repo.get_by_id("doc-default") is None


def test_rebuild_commit_deletes_chunks_only_inside_selected_knowledge_base(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _create_knowledge_bases(db)
        default_doc = KnowledgeDocument(
            id="doc-default",
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_path="default.txt",
            original_filename="default.txt",
            file_hash="old-default",
            file_size=1,
            status="ready",
        )
        second_doc = KnowledgeDocument(
            id="doc-second",
            knowledge_base_id=SECOND_KB_ID,
            source_path="knowledge-bases/00000000-0000-0000-0000-000000000099/second.txt",
            original_filename="second.txt",
            file_hash="second",
            file_size=1,
            status="ready",
        )
        db.add_all(
            [
                default_doc,
                second_doc,
                KnowledgeChunk(
                    id="old-default-chunk",
                    knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                    document_id=default_doc.id,
                    file_hash="old-default",
                    chunk_index=0,
                    content="old",
                    content_hash="old",
                    token_count=1,
                ),
                KnowledgeChunk(
                    id="second-chunk",
                    knowledge_base_id=SECOND_KB_ID,
                    document_id=second_doc.id,
                    file_hash="second",
                    chunk_index=0,
                    content="second",
                    content_hash="second",
                    token_count=1,
                ),
            ]
        )
        db.commit()

        manager = IndexManager(
            db,
            embedding=object(),
            vector_provider=SimpleNamespace(),
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        )
        prepared = PreparedDocument(
            document_id=default_doc.id,
            source_path=default_doc.source_path,
            original_filename=default_doc.original_filename,
            file_hash="new-default",
            file_size=2,
            vector_documents=[],
            chunk_records=[
                KnowledgeChunk(
                    id="new-default-chunk",
                    knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                    document_id=default_doc.id,
                    file_hash="new-default",
                    chunk_index=0,
                    content="new",
                    content_hash="new",
                    token_count=1,
                )
            ],
        )

        manager._commit_rebuild(
            [prepared],
            deleted_source_paths=set(),
            build_collection="documents_build_isolated",
            corpus_fingerprint="fingerprint",
        )

        assert db.get(KnowledgeChunk, "old-default-chunk") is None
        assert db.get(KnowledgeChunk, "new-default-chunk") is not None
        assert db.get(KnowledgeChunk, "second-chunk") is not None


def test_uploads_with_same_content_are_isolated_by_knowledge_base(
    db_engine,
    tmp_path,
):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _create_knowledge_bases(db)
        object_store = LocalObjectStore(tmp_path)
        task_queue = InlineTaskQueue()
        default_service = DocumentIndexService(
            db,
            DEFAULT_KNOWLEDGE_BASE_ID,
            object_store=object_store,
            task_queue=task_queue,
        )
        second_service = DocumentIndexService(
            db,
            SECOND_KB_ID,
            object_store=object_store,
            task_queue=task_queue,
        )

        _, default_id = default_service.create_upload_job("same.txt", b"same")
        _, second_id = second_service.create_upload_job("same.txt", b"same")
        default_doc = default_service.get_document(default_id)
        second_doc = second_service.get_document(second_id)

        assert default_id != second_id
        assert default_doc.source_path == f"{default_id}/same.txt"
        assert second_doc.source_path.startswith(f"knowledge-bases/{SECOND_KB_ID}/")
        assert default_service.get_document(second_id) is None
        assert second_service.get_document(default_id) is None


def test_index_jobs_are_scoped_but_worker_can_claim_across_knowledge_bases(db_engine):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        _create_knowledge_bases(db)
        default_repo = IndexJobRepository(db, DEFAULT_KNOWLEDGE_BASE_ID)
        second_repo = IndexJobRepository(db, SECOND_KB_ID)
        default_job = default_repo.create(IndexJob(job_type="sync", status="pending"))
        second_job = second_repo.create(IndexJob(job_type="sync", status="pending"))

        assert default_repo.get(second_job.id) is None
        assert second_repo.get(default_job.id) is None
        assert [job.id for job in default_repo.list_jobs()] == [default_job.id]
        assert [job.id for job in second_repo.list_jobs()] == [second_job.id]

        claimed = IndexJobRepository(db, knowledge_base_id=None).claim_next_pending()
        assert claimed.id in {default_job.id, second_job.id}


def test_file_classification_ignores_other_knowledge_base_prefixes(
    db_engine,
    tmp_path,
):
    from app.core.database import SessionLocal

    (tmp_path / "default.txt").write_text("default", encoding="utf-8")
    second_path = tmp_path / "knowledge-bases" / SECOND_KB_ID / "second.txt"
    second_path.parent.mkdir(parents=True)
    second_path.write_text("second", encoding="utf-8")

    with SessionLocal() as db:
        _create_knowledge_bases(db)
        default_manager = IndexManager(
            db,
            embedding=object(),
            vector_provider=SimpleNamespace(),
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        )
        second_manager = IndexManager(
            db,
            embedding=object(),
            vector_provider=SimpleNamespace(),
            knowledge_base_id=SECOND_KB_ID,
        )

        default_added = default_manager.classify_files(tmp_path)["added"]
        second_added = second_manager.classify_files(tmp_path)["added"]

        assert [item["source_path"] for item in default_added] == ["default.txt"]
        assert [item["source_path"] for item in second_added] == [
            f"knowledge-bases/{SECOND_KB_ID}/second.txt"
        ]

