"""Regressions for upload event-loop safety, publication and archive workers."""
import asyncio
from contextlib import contextmanager
import io
from pathlib import Path
import threading
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import event, func, select
from starlette.datastructures import UploadFile


@pytest.mark.parametrize("kind", ["single", "batch", "version", "legacy"])
def test_upload_registration_does_not_block_event_loop_or_share_session(db_engine, monkeypatch, kind):
    from app.core.database import SessionLocal
    from app.api.v1 import knowledge_bases, documents
    from app.services.document_index_service import DocumentIndexService

    main_thread = threading.get_ident()
    release = threading.Event()
    observed = []
    monkeypatch.setattr(DocumentIndexService, "__init__",
        lambda self, db, *args, **kwargs: setattr(self, "db", db))

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        with SessionLocal() as request_db:
            def authorize(owned_db, *args):
                assert threading.get_ident() != main_thread
                assert owned_db is not request_db
            monkeypatch.setattr(knowledge_bases, "_require_upload_edit", authorize)

            def register(service, *args):
                assert threading.get_ident() != main_thread
                assert service.db is not request_db
                observed.append(service.db)
                loop.call_soon_threadsafe(started.set)
                assert release.wait(3), "event loop did not run while upload held a blocking lock"
                job = SimpleNamespace(id="accepted-job")
                if kind == "batch":
                    return job, ["doc"], []
                if kind == "version":
                    return job, SimpleNamespace(id="version", version_no=2)
                return job, "doc"

            method = {"single": "create_upload_job", "batch": "create_batch_upload_job",
                      "version": "create_document_version", "legacy": "create_upload_job"}[kind]
            monkeypatch.setattr(DocumentIndexService, method, register)
            file = UploadFile(io.BytesIO(b"content"), filename="a.txt")
            user = SimpleNamespace(id="user", role="admin")
            if kind == "batch":
                call = knowledge_bases.batch_upload_documents("kb", [file], user, request_db)
            elif kind == "version":
                call = knowledge_bases.upload_document_version("kb", "doc", file, user, request_db)
            elif kind == "legacy":
                call = documents.upload_document(file, user, request_db)
            else:
                call = knowledge_bases.upload_document("kb", file, user, request_db)
            task = asyncio.create_task(call)
            try:
                signal = asyncio.create_task(started.wait())
                done, _ = await asyncio.wait([signal, task], timeout=3, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task  # Surface failures before the synchronization point.
                assert signal in done
                # This callback must execute while registration is still blocked.
                assert not task.done()
            finally:
                release.set()
            result = await task
            assert result["job_id"] == "accepted-job"
            assert file.file.closed
    asyncio.run(run())
    assert observed


def test_staging_admission_open_write_and_cleanup_run_in_threads(monkeypatch):
    from app.services import upload_staging

    main_thread = threading.get_ident()
    original_open = Path.open
    operations = []

    @contextmanager
    def slot():
        assert threading.get_ident() != main_thread
        operations.append("acquire")
        try:
            yield
        finally:
            assert threading.get_ident() != main_thread
            operations.append("release")

    @contextmanager
    def recording_open(path, *args, **kwargs):
        assert threading.get_ident() != main_thread
        operations.append("open")
        with original_open(path, *args, **kwargs) as target:
            def write(block):
                assert threading.get_ident() != main_thread
                operations.append("write")
                return target.write(block)
            yield SimpleNamespace(write=write)
        assert threading.get_ident() != main_thread
        operations.append("close")

    monkeypatch.setattr(upload_staging, "upload_slot", slot)
    monkeypatch.setattr(Path, "open", recording_open)
    paths = []
    async def run():
        async with upload_staging.stage_uploads([UploadFile(io.BytesIO(b"content"), filename="a.txt")]) as (accepted, _):
            paths.append(accepted[0][2])
    asyncio.run(run())
    assert operations == ["acquire", "open", "write", "close", "release"]
    assert not paths[0].exists()


def test_worker_cannot_claim_reindex_before_its_items_and_event_exist(db_engine):
    from app.core.database import SessionLocal
    from app.models.index_job import IndexJobEvent
    from app.models.knowledge_document import KnowledgeDocument
    from app.repositories.index_job_repository import IndexJobRepository
    from app.services.document_index_service import DocumentIndexService

    with SessionLocal() as db:
        db.add(KnowledgeDocument(id="doc", knowledge_base_id="kb", source_path="a.txt",
            original_filename="a.txt", file_hash="hash", file_size=3, status="ready"))
        db.commit()
        claimed = []
        def claim_immediately_after_publication(session):
            # Mimic a worker winning the scheduling race at the first commit.
            with SessionLocal() as worker_db:
                repo = IndexJobRepository(worker_db, "kb")
                job = repo.claim_next_pending(worker_id="test-worker", lease_seconds=60)
                if job:
                    claimed.append(job.id)
                    assert len(repo.list_items(job.id)) == job.total_items == 1
                    assert worker_db.scalar(select(func.count()).select_from(IndexJobEvent).where(
                        IndexJobEvent.job_id == job.id, IndexJobEvent.event_type == "created")) == 1
        event.listen(db, "after_commit", claim_immediately_after_publication)
        try:
            job = DocumentIndexService(db, "kb", object_store=object(),
                task_queue=SimpleNamespace(enqueue=lambda _: None)).request_reindex("doc", "user")
        finally:
            event.remove(db, "after_commit", claim_immediately_after_publication)
        assert claimed == [job.id]


def test_item_failure_rolls_back_job_and_event(db_engine):
    from app.core.database import SessionLocal
    from app.models.index_job import IndexJob, IndexJobEvent, IndexJobItem
    from app.repositories.index_job_repository import IndexJobRepository

    with SessionLocal() as db:
        repo = IndexJobRepository(db, "kb")
        with pytest.raises(ValueError, match="another knowledge base"):
            repo.create(IndexJob(job_type="file", total_items=1), [IndexJobItem(
                knowledge_base_id="other", source_path="a.txt", action="add")])
        assert db.scalar(select(func.count()).select_from(IndexJob)) == 0
        assert db.scalar(select(func.count()).select_from(IndexJobEvent)) == 0
        assert db.scalar(select(func.count()).select_from(IndexJobItem)) == 0


def test_redis_wakeup_failure_preserves_accepted_reindex(db_engine):
    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument
    from app.repositories.index_job_repository import IndexJobRepository
    from app.services.document_index_service import DocumentIndexService

    def unavailable(_):
        raise ConnectionError("Redis is unavailable")
    with SessionLocal() as db:
        db.add(KnowledgeDocument(id="doc", knowledge_base_id="kb", source_path="a.txt",
            original_filename="a.txt", file_hash="hash", file_size=3, status="ready"))
        db.commit()
        job = DocumentIndexService(db, "kb", object_store=object(),
            task_queue=SimpleNamespace(enqueue=unavailable)).request_reindex("doc", "user")
        persisted_id = job.id
    with SessionLocal() as worker_db:
        repo = IndexJobRepository(worker_db, "kb")
        job = repo.claim_next_pending(worker_id="polling-worker", lease_seconds=60)
        assert job.id == persisted_id
        assert len(repo.list_items(job.id)) == 1


def test_repeated_upload_and_reindex_reuse_open_work(db_engine, tmp_path):
    from app.adapters.local import LocalObjectStore
    from app.core.database import SessionLocal
    from app.models.index_job import IndexJob
    from app.services.document_index_service import DocumentIndexService

    with SessionLocal() as db:
        service = DocumentIndexService(db, "kb", LocalObjectStore(tmp_path / "objects"),
            SimpleNamespace(enqueue=lambda _: None))
        first, first_doc = service.create_upload_job("a.txt", b"same content", "user")
        repeated, repeated_doc = service.create_upload_job("a.txt", b"same content", "user")
        reindex = service.request_reindex(first_doc, "user")
        assert repeated.id == reindex.id == first.id
        assert repeated_doc == first_doc
        assert db.scalar(select(func.count()).select_from(IndexJob)) == 1


def test_reindex_parse_options_are_validated_persisted_and_visible(client):
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD
    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument

    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME,
        "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    kb = client.get("/api/v1/knowledge-bases").json()[0]["id"]
    summary = {"page_count": 3, "ocr_recommended_pages": [1], "ocr_pages": [],
               "preview": [{"page": 0, "text": "preview"}], "table_pages": [2]}
    with SessionLocal() as db:
        db.add(KnowledgeDocument(id="ocr-doc", knowledge_base_id=kb, source_path="a.pdf",
            original_filename="a.pdf", file_hash="hash", file_size=3, status="ready",
            metadata_json={"parse_summary": summary, "custom": "preserved"}))
        db.commit()
    path = f"/api/v1/knowledge-bases/{kb}/documents/ocr-doc"
    for invalid in [{"ocr_pages": [-1]}, {"ocr_pages": [True]}, {"ocr_pages": ["1"]},
                    {"ocr_pages": list(range(41))}, {"force_ocr": "true"}]:
        assert client.post(path + "/reindex", json=invalid).status_code == 422
    response = client.post(path + "/reindex", json={"force_ocr": False, "ocr_pages": [1]})
    assert response.status_code == 202, response.text
    detail = client.get(path).json()
    assert detail["metadata_json"]["parse_options"] == {"force_ocr": False, "ocr_pages": [1]}
    assert detail["metadata_json"]["parse_summary"] == summary
    assert detail["metadata_json"]["custom"] == "preserved"
    # Repeat on an already published job updates configuration and reuses work.
    reset = client.post(path + "/reindex", json={"force_ocr": False, "ocr_pages": None})
    assert reset.status_code == 202 and reset.json()["job_id"] == response.json()["job_id"]
    assert client.get(path).json()["metadata_json"]["parse_options"] == {"force_ocr": False, "ocr_pages": None}
    assert client.post(path + "/reindex").status_code == 202


def _zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, body in entries:
            package.writestr(name, body)
    return path


def test_archive_api_queues_preparation_without_opening_zip(client, monkeypatch, tmp_path):
    from app.core import config
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD

    monkeypatch.setattr(config, "DOC_DIR", tmp_path / "documents")
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME,
        "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    kb = client.get("/api/v1/knowledge-bases").json()[0]["id"]
    archive = _zip(tmp_path / "upload.zip", [("pkg/main.py", "def main(): return 1")]).read_bytes()
    def must_run_in_worker(*args, **kwargs):
        pytest.fail("ZIP inspection ran during the HTTP request")
    monkeypatch.setattr(zipfile, "ZipFile", must_run_in_worker)
    response = client.post(f"/api/v1/knowledge-bases/{kb}/code-graph/upload",
        files={"file": ("source.zip", archive)})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "preparing"
    assert body["last_sync_status"] == "queued"
    assert body["last_sync_job_id"]
    root = config.DOC_DIR / "code_uploads" / kb / body["id"]
    assert (root / "source.zip").exists()
    assert not (root / "content").exists()


def test_archive_worker_prepares_indexes_and_can_repeat_after_preparation(db_engine, monkeypatch, tmp_path):
    from app.core import config
    from app.core.database import SessionLocal
    from app.services.code_archive_service import CodeArchiveService
    from app.services.code_index_service import CodeIndexService
    from app.services.data_source_service import DataSourceService
    from app.services.job_handlers import handle_code_index
    from app.repositories.index_job_repository import IndexJobRepository

    monkeypatch.setattr(config, "DOC_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "CODE_VECTOR_ENABLED", False)
    indexed = []
    monkeypatch.setattr(CodeIndexService, "__init__", lambda self, *args, **kwargs: None)
    def index(self, root, **kwargs):
        indexed.append(Path(root))
        assert (root / "main.py").read_text() == "def main(): return 1"
        return {"status": "completed"}
    monkeypatch.setattr(CodeIndexService, "index_directory", index)
    monkeypatch.setattr(DataSourceService, "resolve_local_source", lambda self, source: Path(source.config_json["location"]))
    queue = SimpleNamespace(enqueue=lambda _: None)
    incoming = _zip(tmp_path / "incoming.zip", [("pkg/main.py", "def main(): return 1")])
    with SessionLocal() as db:
        service = CodeArchiveService(db, "kb", queue)
        source = service.stage("source.zip", incoming, "user")
        repo = IndexJobRepository(db, "kb")
        job = repo.get(source.last_sync_job_id)
        ctx = SimpleNamespace(db=db, job=job, job_repo=repo, mgr=None, container=SimpleNamespace(task_queue=queue),
            job_id=job.id, heartbeat=None, agent_context=None, agent_document_ids=None,
            agent_cancelled=lambda: False, agent_event_callback=None, link_followup_jobs=None)
        handle_code_index(ctx)
        assert source.status == "active" and source.last_sync_status == "completed"
        assert job.status == "completed" and len(indexed) == 1
        assert service.prepare(source) == indexed[0]
        root = config.DOC_DIR / "code_uploads" / "kb" / source.id
        assert not (root / "source.zip").exists()
        assert not list(root.glob("extract-*"))


@pytest.mark.parametrize("attack", ["traversal", "backslash", "absolute", "symlink", "count", "directory", "expanded", "corrupt"])
def test_archive_worker_rejects_unsafe_or_unbounded_zip(db_engine, monkeypatch, tmp_path, attack):
    from app.core import config
    from app.core.database import SessionLocal
    from app.services import code_archive_service as archive_module

    monkeypatch.setattr(config, "DOC_DIR", tmp_path / "documents")
    entry = {"traversal": "../outside.py", "backslash": "..\\outside.py", "absolute": "/outside.py"}.get(attack, "main.py")
    if attack == "symlink":
        entry = zipfile.ZipInfo("linked.py")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
    entries = [(entry, "abcde")]
    if attack == "count":
        monkeypatch.setattr(archive_module, "MAX_ARCHIVE_ENTRIES", 1)
        entries.append(("other.py", "other"))
    if attack == "expanded":
        monkeypatch.setattr(archive_module, "MAX_EXPANDED_BYTES", 4)
    if attack == "directory":
        monkeypatch.setattr(archive_module, "MAX_CENTRAL_DIRECTORY_BYTES", 4)
    incoming = _zip(tmp_path / "incoming.zip", entries)
    if attack == "corrupt":
        incoming.write_bytes(b"not a ZIP")
    with SessionLocal() as db:
        service = archive_module.CodeArchiveService(db, "kb", SimpleNamespace(enqueue=lambda _: None))
        source = service.stage("source.zip", incoming, "user")
        with pytest.raises(ValueError):
            service.prepare(source)
        assert source.status == "error" and source.last_sync_status == "failed"
        root = config.DOC_DIR / "code_uploads" / "kb" / source.id
        assert not (root / "content").exists()
        assert not list(root.glob("extract-*"))
        assert not (root / "outside.py").exists()
