"""Server read models on real PostgreSQL JSON, window and scoped paging SQL."""

from datetime import datetime, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.test_distributed_coordination import postgres_service
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_document import KnowledgeDocument
from app.models.document_management import KnowledgeFolder, DocumentTag, DocumentTagLink
from app.models.index_job import IndexJob, IndexJobItem, IndexJobEvent
from app.services.workspace_queries import documents_page, jobs_page, serialize_documents


START = datetime(2026, 1, 1, 12)


def seed(db):
    db.add_all([KnowledgeBase(id="workspace", slug="workspace", name="Workspace"),
                KnowledgeBase(id="private", slug="private", name="Private")])
    db.flush()
    db.add(KnowledgeFolder(id="folder", knowledge_base_id="workspace", name="Folder", path="folder"))
    db.add(DocumentTag(id="tag", knowledge_base_id="workspace", name="Selected"))
    db.flush()
    for identifier, kb, name, folder, status in [
        ("doc-a", "workspace", "report_100%.txt", "folder", "processing"),
        ("doc-b", "workspace", "report-100.txt", "folder", "indexed"),
        ("doc-c", "workspace", "notes.txt", None, "pending"),
        ("secret", "private", "report_100%.txt", None, "processing"),
    ]:
        db.add(KnowledgeDocument(id=identifier, knowledge_base_id=kb, source_path=identifier + ".txt",
            original_filename=name, folder_id=folder, status=status, file_hash=identifier,
            file_size=10, created_at=START))
    db.flush()
    db.add(DocumentTagLink(document_id="doc-a", tag_id="tag", knowledge_base_id="workspace"))
    db.commit()


def add_job(db, identifier, *, documents, kb="workspace", status="running", created_at=START,
            started_at=START, cancel=False):
    job = IndexJob(id=identifier, knowledge_base_id=kb, job_type="file", status=status,
                   created_at=created_at, started_at=started_at, cancel_requested=cancel)
    db.add(job)
    db.flush()
    for document in documents:
        db.add(IndexJobItem(job_id=job.id, knowledge_base_id=kb, document_id=document,
                            source_path=document + ".txt", action="add"))
    db.flush()
    return job


def add_stage(db, job, stage, status, *, document=None, at=START):
    payload = {"stage": stage, "status": status}
    if document:
        payload["document_id"] = document
    db.add(IndexJobEvent(job_id=job.id, knowledge_base_id=job.knowledge_base_id,
        event_type="stage", status=status, payload_json=payload, created_at=at))
    db.flush()


def test_real_postgres_document_paging_scope_tags_folder_and_literal_search(postgres_service):
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db)
        page = documents_page(db, "workspace", offset=1, limit=1)
        assert page["total"] == 3 and [row.id for row in page["items"]] == ["doc-b"]
        assert page["offset"] == 1 and page["limit"] == 1
        assert documents_page(db, "workspace", folder_id="folder")["total"] == 2
        assert documents_page(db, "workspace", status="pending")["total"] == 1
        tagged = documents_page(db, "workspace", tag_id="tag")
        assert tagged["total"] == 1 and tagged["items"][0].tag_ids == ["tag"]
        for literal in ("%", "_", "100%"):
            found = documents_page(db, "workspace", query=literal)
            assert found["total"] == 1 and found["items"][0].id == "doc-a"
        assert documents_page(db, "private", tag_id="tag")["total"] == 0
        assert documents_page(db, "workspace", offset=50)["items"] == []


def test_real_postgres_latest_job_json_stage_and_retry_progress(postgres_service):
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db)
        old = add_job(db, "old", documents=["doc-a"], status="failed", created_at=START - timedelta(days=1))
        add_stage(db, old, "graph", "failed", document="doc-a")
        batch = add_job(db, "new", documents=["doc-a", "doc-b"],
                        started_at=START + timedelta(minutes=10))
        add_stage(db, batch, "vector", "completed", document="doc-a", at=START)
        add_stage(db, batch, "parse", "running", document="doc-a", at=START + timedelta(minutes=11))
        add_stage(db, batch, "parse", "completed", document="doc-a", at=START + timedelta(minutes=12))
        add_stage(db, batch, "chunk", "running", document="doc-b", at=START + timedelta(minutes=13))
        private = add_job(db, "private-job", documents=["secret"], kb="private")
        add_stage(db, private, "graph", "failed", document="secret")
        db.commit()
        page = documents_page(db, "workspace")
        outputs = {row.id: row for row in page["items"]}
        first, second = outputs["doc-a"].processing, outputs["doc-b"].processing
        assert first.job_id == "new" and first.stage == "parse"
        first_states = {row.name: row.status for row in first.stages}
        assert first_states["parse"] == "completed" and first_states["vector"] == "unknown"
        assert second.stage == "chunk" and {row.name: row.status for row in second.stages}["parse"] == "unknown"
        assert outputs["doc-c"].processing is None
        jobs = jobs_page(db, "workspace", query="100%")
        assert jobs["total"] == 2
        latest = next(row for row in jobs["items"] if row.id == "new")
        assert set(latest.document_ids) == {"doc-a", "doc-b"} and latest.current_stage == "chunk"
        assert jobs_page(db, "workspace", status="failed")["total"] == 1
        assert jobs_page(db, "workspace", query="private-job")["total"] == 0


def test_real_postgres_progress_batches_queries_and_records_wait_reasons(postgres_service):
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db)
        pending = add_job(db, "queued", documents=["doc-a"], status="pending", started_at=None)
        cancelling = add_job(db, "stopping", documents=["doc-b"], cancel=True)
        add_stage(db, cancelling, "parse", "running", document="doc-b")
        db.commit()
        documents = list(db.query(KnowledgeDocument).filter_by(knowledge_base_id="workspace"))
        statements = []
        def count(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        event.listen(engine, "before_cursor_execute", count)
        try:
            outputs = {row.id: row for row in serialize_documents(db, "workspace", documents)}
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(statements) == 4  # tags, window associations, jobs, latest stages
        assert outputs["doc-a"].processing.waiting_reason
        assert outputs["doc-b"].processing.waiting_reason
        assert outputs["doc-a"].processing.stage is None
        assert outputs["doc-b"].processing.stage == "parse"
