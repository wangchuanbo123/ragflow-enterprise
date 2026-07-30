"""索引 Worker：单进程、单并发的本地后台索引执行器。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.core.config import INDEX_WORKER_ENABLED
from app.core.database import SessionLocal
from app.repositories.index_job_repository import IndexJobRepository

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()


def _process_job(job_id: str) -> None:
    """处理一个索引任务。"""
    from app.core.config import DOC_DIR
    from rag.indexing.index_manager import IndexManager

    with SessionLocal() as db:
        job_repo = IndexJobRepository(db)
        job = job_repo.get(job_id)
        if not job:
            return

        try:
            mgr = IndexManager(db)

            if job.job_type == "sync":
                result = mgr.sync(DOC_DIR, dry_run=False)
                status = result.get("status", "completed")
                job_repo.update(
                    job,
                    status=status,
                    total_items=result.get("indexed", 0) + result.get("deleted", 0),
                    processed_items=result.get("indexed", 0) + result.get("deleted", 0),
                    succeeded_items=result.get("indexed", 0),
                    failed_items=result.get("failed", 0),
                    error_message="; ".join(result.get("errors", []))[:500] if result.get("errors") else None,
                )
            elif job.job_type == "rebuild":
                result = mgr.rebuild(DOC_DIR, dry_run=False, job_id=job_id[:8])
                status = result.get("status", "completed")
                job_repo.update(
                    job,
                    status=status,
                    total_items=result.get("documents_indexed", 0),
                    processed_items=result.get("documents_indexed", 0),
                    succeeded_items=result.get("documents_indexed", 0),
                    failed_items=result.get("documents_failed", 0),
                    error_message="; ".join(result.get("errors", []))[:500] if result.get("errors") else None,
                )
            elif job.job_type in ("file", "delete"):
                items = job_repo.list_items(job_id)
                succeeded = 0
                failed = 0
                for item in items:
                    item.started_at = datetime.now(timezone.utc)
                    item.status = "running"
                    db.commit()
                    try:
                        active_coll = None
                        state = mgr.repo.get_index_state()
                        active_coll = state.active_collection_name

                        if active_coll is None:
                            result = mgr.rebuild(DOC_DIR, dry_run=False)
                            if result.get("status") != "completed":
                                raise RuntimeError(
                                    "Initial index rebuild failed: "
                                    + "; ".join(result.get("errors", []))
                                )
                            active_coll = result.get("build_collection")

                        if item.action == "delete":
                            mgr.delete_document(item.source_path, active_coll)
                        else:
                            file_path = DOC_DIR / item.source_path
                            from rag.utils.file_hash import file_hash as calc_hash
                            fh = calc_hash(file_path)
                            mgr.index_document(
                                item.source_path,
                                file_path,
                                fh,
                                active_coll,
                                document_id=item.document_id,
                            )

                        item.status = "completed"
                        item.finished_at = datetime.now(timezone.utc)
                        succeeded += 1
                    except Exception as exc:
                        item.status = "failed"
                        item.error_message = str(exc)[:500]
                        item.finished_at = datetime.now(timezone.utc)
                        failed += 1
                    db.commit()

                job_repo.update(
                    job,
                    status="completed" if failed == 0 else "partial",
                    total_items=len(items),
                    processed_items=len(items),
                    succeeded_items=succeeded,
                    failed_items=failed,
                )

            # Refresh every in-process retrieval dependency after an index change.
            from app.services.rag_service import invalidate_rag_caches
            invalidate_rag_caches()
            from app.services.readiness_service import invalidate_readiness_cache
            invalidate_readiness_cache()

        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc)
            job_repo.update(job, status="failed", error_message=str(exc)[:500])


def _worker_loop():
    """Worker 主循环：不断领取 pending 任务。"""
    logger.info("Index worker started")
    while not _worker_stop.is_set():
        try:
            with SessionLocal() as db:
                job_repo = IndexJobRepository(db)
                if job_repo.has_running_write_job():
                    _worker_stop.wait(2)
                    continue
                job = job_repo.claim_next_pending()

            if job is None:
                _worker_stop.wait(5)
                continue

            logger.info("Processing index job: %s (type=%s)", job.id, job.job_type)
            _process_job(job.id)
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            _worker_stop.wait(10)

    logger.info("Index worker stopped")


def start_worker():
    """启动后台 Worker 线程。"""
    global _worker_thread
    if not INDEX_WORKER_ENABLED:
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="index-worker")
    _worker_thread.start()


def stop_worker():
    """停止后台 Worker。"""
    _worker_stop.set()
    if _worker_thread:
        _worker_thread.join(timeout=10)


def get_worker_status() -> dict:
    """Return the actual in-process index worker state for readiness checks."""
    if not INDEX_WORKER_ENABLED:
        return {"enabled": False, "alive": False, "stopping": False}
    thread = _worker_thread
    return {
        "enabled": True,
        "alive": bool(thread and thread.is_alive()),
        "stopping": _worker_stop.is_set(),
        "thread_name": thread.name if thread else None,
    }
