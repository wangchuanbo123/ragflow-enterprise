"""Upload memory bounds and paged, factual workspace progress contracts."""
import asyncio
import hashlib
import io
import tracemalloc
from datetime import datetime
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.adapters.local import LocalObjectStore
from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD


def login(client):
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME,
                                                "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    return client.get("/api/v1/knowledge-bases").json()[0]["id"]


def test_file_storage_is_atomic_and_has_bounded_peak_memory(tmp_path):
    source = tmp_path / "large.txt"
    with source.open("wb") as stream:
        for _ in range(32):
            stream.write(b"a" * (1024 * 1024))
    store = LocalObjectStore(tmp_path / "objects")
    tracemalloc.start()
    try:
        ref = store.put_file("large.txt", source)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    assert ref.size == 32 * 1024 * 1024
    with source.open("rb") as stream:
        assert ref.content_hash == hashlib.file_digest(stream, "sha256").hexdigest()
    with pytest.raises(ValueError):
        store.put_file("../outside.txt", source)
    assert not list((tmp_path / "objects").glob("*.tmp"))


def test_batch_staging_rejects_oversize_by_original_index_and_always_cleans(tmp_path, monkeypatch):
    from app.core import config
    from app.services.upload_staging import stage_uploads
    monkeypatch.setattr(config, "MAX_DOCUMENT_SIZE_MB", 1)
    monkeypatch.setattr(config, "MAX_BATCH_UPLOAD_TOTAL_MB", 5)
    files = [UploadFile(io.BytesIO(b"a" * (1024 * 1024 + 1)), filename="same.txt"),
             UploadFile(io.BytesIO(b"valid"), filename="same.txt")]
    paths = []
    async def run():
        async with stage_uploads(files, partial=True) as (accepted, rejected):
            assert rejected[0]["index"] == 0
            assert accepted[0][0] == 1
            paths.extend(item[2] for item in accepted)
            assert paths[0].read_bytes() == b"valid"
    asyncio.run(run())
    assert all(not path.exists() for path in paths)
    assert all(file.file.closed for file in files)


def test_cancelled_upload_releases_slot_and_closes_stream(monkeypatch):
    from app.core import config
    from app.services.upload_staging import stage_uploads
    monkeypatch.setattr(config, "UPLOAD_MAX_CONCURRENCY", 1)
    class Cancelled(UploadFile):
        async def read(self, size=-1):
            raise asyncio.CancelledError()
    file = Cancelled(io.BytesIO(b"x"), filename="x.txt")
    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with stage_uploads([file]):
                pytest.fail("Cancelled uploads must not be registered")
        async with stage_uploads([UploadFile(io.BytesIO(b"ok"), filename="ok.txt")]):
            pass
    asyncio.run(run())
    assert file.file.closed


def test_paged_documents_are_scoped_filtered_and_progress_is_recorded(client):
    kb = login(client)
    from app.core.database import SessionLocal
    from app.models.index_job import IndexJob, IndexJobItem, IndexJobEvent
    from app.models.knowledge_document import KnowledgeDocument
    with SessionLocal() as db:
        for i in range(4):
            db.add(KnowledgeDocument(id=f"page-doc-{i}", knowledge_base_id=kb, source_path=f"{i}.txt",
                original_filename=f"run-{i}.txt", file_hash=str(i), file_size=4, status="processing"))
        job = IndexJob(id="page-job", knowledge_base_id=kb, job_type="file", status="running", total_items=1,
                       started_at=datetime(2026, 1, 1))
        db.add(job); db.flush()
        db.add(IndexJobItem(job_id=job.id, document_id="page-doc-1", source_path="1.txt", action="add"))
        db.add(IndexJobEvent(job_id=job.id, knowledge_base_id=kb, event_type="stage", status="running",
            payload_json={"stage": "parse", "status": "completed", "document_id": "page-doc-1"}))
        db.commit()
    response = client.get(f"/api/v1/knowledge-bases/{kb}/documents/page", params={"query": "run-", "limit": 2})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 4 and len(response.json()["items"]) == 2
    detail = client.get(f"/api/v1/knowledge-bases/{kb}/documents/page-doc-1").json()
    assert detail["processing"]["stage"] == "parse"
    states = {row["name"]: row["status"] for row in detail["processing"]["stages"]}
    assert states["parse"] == "completed" and states["vector"] == "unknown"
    jobs = client.get(f"/api/v1/knowledge-bases/{kb}/index-jobs/page", params={"query": "run-1"})
    assert jobs.status_code == 200 and jobs.json()["total"] == 1
    assert jobs.json()["items"][0]["document_names"] == ["run-1.txt"]
    assert client.get(f"/api/v1/knowledge-bases/{kb}/documents/page", params={"query": "%"}).json()["total"] == 0
    assert client.get(f"/api/v1/knowledge-bases/{kb}/documents/page", params={"limit": 201}).status_code == 422


def test_batch_duplicate_names_preserve_request_indices_and_stream_to_store(client, monkeypatch):
    kb = login(client)
    original = LocalObjectStore.put_file
    calls = []
    def tracking(self, key, path):
        assert isinstance(path, Path)
        calls.append(path.stat().st_size)
        return original(self, key, path)
    monkeypatch.setattr(LocalObjectStore, "put_file", tracking)
    result = client.post(f"/api/v1/knowledge-bases/{kb}/documents/batch-upload", files=[
        ("files", ("bad.exe", b"invalid")), ("files", ("same.txt", b"first")), ("files", ("same.txt", b"second")),
    ])
    assert result.status_code == 202, result.text
    body = result.json()
    assert body["accepted"] == 2 and body["rejected"][0]["index"] == 0
    assert calls and max(calls) == 6


def test_backup_accepts_only_exact_revision_routine_and_application_triggers():
    from scripts.server_backup import validate_database_toc, BackupError
    base = b"1; 1259 1 TABLE public knowledge_graph_revisions rag\n"
    validate_database_toc(base + b"2; 1255 2 FUNCTION public bump_knowledge_graph_revision() rag\n"
                          b"3; 2620 3 TRIGGER public kg_entities graph_revision_insert rag\n")
    for extra in [b"2; 1255 2 FUNCTION public attacker() rag\n",
                  b"2; 2620 2 TRIGGER public users graph_revision_insert rag\n",
                  b"2; 1255 2 FUNCTION public bump_knowledge_graph_revision(text) rag\n"]:
        with pytest.raises(BackupError):
            validate_database_toc(base + extra)
