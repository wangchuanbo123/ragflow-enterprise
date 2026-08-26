"""Index job execution shared by local threads and distributed Celery workers."""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timezone

from app.core.config import (
    INDEX_JOB_HEARTBEAT_SECONDS,
    INDEX_JOB_LEASE_SECONDS,
    INDEX_WORKER_ENABLED,
)
from app.repositories.index_job_repository import IndexJobRepository

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_WORKER_ID = (
    f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
)


class _LeaseHeartbeat:
    def __init__(self, job_id: str, worker_id: str):
        self.job_id = job_id
        self.worker_id = worker_id
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lease_lost = False

    def _run(self) -> None:
        from app.bootstrap.container import get_provider_container

        container = get_provider_container()
        while not self.stop_event.wait(INDEX_JOB_HEARTBEAT_SECONDS):
            try:
                with container.metadata.session() as db:
                    alive = IndexJobRepository(
                        db,
                        knowledge_base_id=None,
                    ).heartbeat(
                        self.job_id,
                        worker_id=self.worker_id,
                        lease_seconds=INDEX_JOB_LEASE_SECONDS,
                    )
                if not alive:
                    self.lease_lost = True
                    logger.error("Lease lost for index job %s", self.job_id)
                    return
            except Exception as exc:
                logger.warning("Heartbeat failed for job %s: %s", self.job_id, exc)

    def __enter__(self):
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"job-heartbeat-{self.job_id[:8]}",
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


def _process_job(
    job_id: str,
    *,
    worker_id: str | None = None,
    already_claimed: bool = False,
) -> bool:
    """Claim and process one persisted job; safe for local or Celery workers."""
    from app.bootstrap.container import get_provider_container
    from app.core.config import DOC_DIR
    from rag.indexing.index_manager import IndexManager

    worker_id = worker_id or _WORKER_ID
    container = get_provider_container()
    with container.metadata.session() as db:
        job_repo = IndexJobRepository(db, knowledge_base_id=None)
        if already_claimed:
            job = job_repo.get(job_id)
            if (
                job is None
                or job.status != "running"
                or job.lease_owner != worker_id
            ):
                return False
        else:
            job = job_repo.claim(
                job_id,
                worker_id=worker_id,
                lease_seconds=INDEX_JOB_LEASE_SECONDS,
            )
            if job is None:
                return False

        try:
            with _LeaseHeartbeat(job_id, worker_id) as heartbeat:
                mgr = IndexManager(
                    db,
                    vector_provider=container.vector_store,
                    knowledge_base_id=job.knowledge_base_id,
                )

                if job_repo.is_cancel_requested(job_id):
                    job_repo.update(job, status="cancelled")
                    return True

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
                        error_message=(
                            "; ".join(result.get("errors", []))[:500]
                            if result.get("errors")
                            else None
                        ),
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
                        error_message=(
                            "; ".join(result.get("errors", []))[:500]
                            if result.get("errors")
                            else None
                        ),
                    )
                elif job.job_type in {"file", "delete"}:
                    all_items = job_repo.list_items(job_id)
                    items = [item for item in all_items if item.status != "completed"]
                    succeeded = sum(
                        1 for item in all_items if item.status == "completed"
                    )
                    failed = 0
                    for position, item in enumerate(items, start=1):
                        if heartbeat.lease_lost:
                            raise RuntimeError("Job lease was lost during processing")
                        if job_repo.is_cancel_requested(job_id):
                            job_repo.update(
                                job,
                                status="cancelled",
                                processed_items=succeeded + failed,
                                succeeded_items=succeeded,
                                failed_items=failed,
                            )
                            return True

                        item.started_at = datetime.now(timezone.utc)
                        item.status = "running"
                        db.commit()
                        try:
                            state = mgr.repo.get_index_state()
                            active_coll = state.active_collection_name
                            if active_coll is None:
                                active_coll = mgr.ensure_active_collection(job_id[:8])

                            if item.action == "delete":
                                mgr.delete_document(item.source_path, active_coll)
                            else:
                                with container.object_store.materialize(
                                    item.source_path
                                ) as file_path:
                                    from rag.utils.file_hash import file_hash as calc_hash

                                    file_digest = calc_hash(file_path)
                                    mgr.index_document(
                                        item.source_path,
                                        file_path,
                                        file_digest,
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
                            processed_items=succeeded + failed,
                            succeeded_items=succeeded,
                            failed_items=failed,
                        )

                    job_repo.update(
                        job,
                        status="completed" if failed == 0 else "partial",
                        total_items=len(all_items),
                        processed_items=succeeded + failed,
                        succeeded_items=succeeded,
                        failed_items=failed,
                    )
                elif job.job_type == "connector_sync":
                    from app.services.data_source_service import DataSourceService

                    source_id = str((job.options_json or {}).get("data_source_id", ""))
                    result = DataSourceService(
                        db, job.knowledge_base_id, task_queue=container.task_queue
                    ).execute_sync(source_id, job.requested_by)
                    failed = 1 if result.get("status") == "failed" else len(result.get("rejected", []))
                    job_repo.update(
                        job,
                        status="failed" if result.get("status") == "failed" else ("partial" if failed else "completed"),
                        total_items=1,
                        processed_items=1,
                        succeeded_items=0 if result.get("status") == "failed" else 1,
                        failed_items=failed,
                        error_message=result.get("detail"),
                    )
                elif job.job_type == "code_index":
                    import tempfile
                    from pathlib import Path

                    from app.services.code_index_service import CodeIndexService
                    from app.services.data_source_service import DataSourceService
                    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

                    source_id = str((job.options_json or {}).get("data_source_id", ""))
                    source = KnowledgeBaseRepository(db).get_data_source(job.knowledge_base_id, source_id)
                    if source is None:
                        raise ValueError("数据源不存在")
                    service = CodeIndexService(db, job.knowledge_base_id)
                    if source.source_type == "local_directory":
                        root = DataSourceService._resolve_local_directory(str((source.config_json or {}).get("location", "")))
                        result = service.index_directory(root, data_source_id=source.id)
                    elif source.source_type == "git":
                        with tempfile.TemporaryDirectory(prefix="rag-code-git-") as temp_dir:
                            root, revision = DataSourceService(db, job.knowledge_base_id)._checkout_git(source, Path(temp_dir))
                            result = service.index_directory(root, data_source_id=source.id, revision=revision)
                    else:
                        raise ValueError("代码索引仅支持本地目录和 Git 数据源")
                    from app.core.config import CODE_VECTOR_ENABLED
                    if CODE_VECTOR_ENABLED:
                        from app.services.code_vector_service import CodeVectorService

                        result["code_vectors"] = CodeVectorService(
                            db, job.knowledge_base_id, container=container
                        ).rebuild()
                    job_repo.update(
                        job, status="completed", total_items=1, processed_items=1,
                        succeeded_items=1, failed_items=0,
                    )
                elif job.job_type == "entity_vector_rebuild":
                    from app.services.entity_vector_service import EntityVectorService

                    result = EntityVectorService(
                        db, job.knowledge_base_id, container=container
                    ).rebuild()
                    job_repo.update(
                        job, status="completed", total_items=1, processed_items=1,
                        succeeded_items=1, failed_items=0,
                    )
                elif job.job_type == "relation_vector_rebuild":
                    from app.services.relation_vector_service import RelationVectorService

                    result = RelationVectorService(
                        db, job.knowledge_base_id, container=container
                    ).rebuild()
                    job_repo.update(
                        job, status="completed", total_items=1, processed_items=1,
                        succeeded_items=1, failed_items=0,
                    )
                elif job.job_type == "code_vector_rebuild":
                    from app.services.code_vector_service import CodeVectorService

                    result = CodeVectorService(
                        db, job.knowledge_base_id, container=container
                    ).rebuild()
                    job_repo.update(
                        job, status="completed", total_items=1, processed_items=1,
                        succeeded_items=1, failed_items=0,
                    )
                elif job.job_type == "graph_rebuild":
                    from app.services.entity_vector_service import EntityVectorService
                    from app.services.graph_governance_service import GraphGovernanceService
                    from app.services.knowledge_graph_service import GraphBuildService
                    from app.services.relation_vector_service import RelationVectorService

                    options = job.options_json or {}
                    mode = options.get("mode", "pending")
                    vector_update = options.get("vector_update", "full")
                    community_update = options.get("community_update", "full")
                    result = GraphBuildService(
                        db, knowledge_base_id=job.knowledge_base_id
                    ).build_graph(
                        only_pending=mode != "full",
                        retry_failed=mode == "retry_failed",
                    )
                    changes = result.get("changes", {})
                    governance = GraphGovernanceService(
                        db, job.knowledge_base_id, task_queue=container.task_queue
                    )
                    report = governance.assess_quality(job.requested_by)
                    if community_update == "defer":
                        # 图谱立即可用，社区延迟到维护任务重建。
                        if int(result.get("succeeded", 0)) or changes:
                            governance.mark_communities_stale()
                    elif community_update == "affected":
                        result["communities"] = governance.rebuild_communities(
                            changed_entity_ids=changes.get("community_entity_ids")
                        )
                    else:
                        result["communities"] = governance.rebuild_communities()
                    if vector_update == "incremental":
                        result["entity_vectors"] = EntityVectorService(
                            db, job.knowledge_base_id, container=container
                        ).apply_changes(
                            changes.get("changed_entity_ids"),
                            changes.get("deleted_entity_ids"),
                        )
                        result["relation_vectors"] = RelationVectorService(
                            db, job.knowledge_base_id, container=container
                        ).apply_changes(
                            changes.get("changed_relation_ids"),
                            changes.get("deleted_relation_ids"),
                        )
                    elif vector_update == "full":
                        result["entity_vectors"] = EntityVectorService(
                            db, job.knowledge_base_id, container=container
                        ).rebuild()
                        result["relation_vectors"] = RelationVectorService(
                            db, job.knowledge_base_id, container=container
                        ).rebuild()
                    try:
                        result["entity_disambiguation"] = (
                            governance.scan_semantic_merge_candidates()
                        )
                        result["relation_disambiguation"] = (
                            governance.scan_relation_merge_candidates()
                        )
                    except Exception as exc:
                        logger.warning(
                            "Graph disambiguation candidate scan failed for %s: %s",
                            job.knowledge_base_id,
                            exc,
                        )
                    failed = int(result.get("failed", 0))
                    job_repo.update(
                        job,
                        status="partial" if failed or report.status != "ready" else "completed",
                        total_items=int(result.get("total_chunks", 0)),
                        processed_items=int(result.get("succeeded", 0)) + failed,
                        succeeded_items=int(result.get("succeeded", 0)),
                        failed_items=failed,
                        error_message=("图谱质量门禁未全部通过" if report.status != "ready" else None),
                    )
                elif job.job_type == "graph_maintenance":
                    from app.services.graph_governance_service import GraphGovernanceService

                    governance = GraphGovernanceService(
                        db, job.knowledge_base_id, task_queue=container.task_queue
                    )
                    result = {}
                    state = governance.community_state(create=False)
                    if state is not None and state.status == "stale":
                        result["communities"] = governance.rebuild_communities()
                    report = governance.assess_quality(job.requested_by)
                    job_repo.update(
                        job,
                        status="completed" if report.status == "ready" else "partial",
                        total_items=1,
                        processed_items=1,
                        succeeded_items=1,
                        failed_items=0,
                        error_message=(
                            None if report.status == "ready" else "图谱质量门禁未全部通过"
                        ),
                    )
                elif job.job_type == "schema_generation":
                    from app.services.graph_schema_induction_service import (
                        GraphSchemaInductionService,
                    )

                    options = job.options_json or {}
                    schema = GraphSchemaInductionService(
                        db,
                        job.knowledge_base_id,
                        llm=container.extraction_llm,
                    ).generate_draft(
                        created_by=job.requested_by,
                        document_ids=options.get("document_ids") or None,
                        instructions=options.get("instructions") or None,
                        target_schema_id=options.get("target_schema_id") or None,
                    )
                    options = dict(options)
                    options["generated_schema_id"] = schema.id
                    options["generated_schema_version"] = schema.version
                    job.options_json = options
                    job_repo.update(
                        job,
                        status="completed",
                        total_items=len(schema.source_document_ids_json or []) or 1,
                        processed_items=len(schema.source_document_ids_json or []) or 1,
                        succeeded_items=len(schema.source_document_ids_json or []) or 1,
                        failed_items=0,
                    )
                elif job.job_type == "schema_drift":
                    from app.services.graph_schema_induction_service import (
                        GraphSchemaInductionService,
                    )

                    options = dict(job.options_json or {})
                    drift = GraphSchemaInductionService(
                        db,
                        job.knowledge_base_id,
                        llm=container.extraction_llm,
                    ).detect_drift(
                        created_by=job.requested_by,
                        document_ids=options.get("document_ids") or None,
                        instructions=options.get("instructions") or None,
                    )
                    options["drift_result"] = drift
                    job.options_json = options
                    source_count = len(drift.get("source_document_ids") or []) or 1
                    job_repo.update(
                        job,
                        status="completed",
                        total_items=source_count,
                        processed_items=source_count,
                        succeeded_items=source_count,
                        failed_items=0,
                    )
                else:
                    raise ValueError(f"Unsupported index job type: {job.job_type}")

                if job.job_type in {"sync", "rebuild", "file", "graph_rebuild"}:
                    completed_document_job = job_repo.get(job_id)
                    if (
                        completed_document_job is not None
                        and completed_document_job.status in {"completed", "partial"}
                        and completed_document_job.succeeded_items > 0
                    ):
                        from app.services.graph_governance_service import GraphGovernanceService

                        if job.job_type in {"sync", "rebuild", "file"}:
                            GraphGovernanceService(
                                db,
                                job.knowledge_base_id,
                                task_queue=container.task_queue,
                            ).request_automatic_schema_generation(
                                job.requested_by,
                                source_job_id=job.id,
                            )
                        GraphGovernanceService(
                            db,
                            job.knowledge_base_id,
                            task_queue=container.task_queue,
                        ).request_automatic_build(
                            job.requested_by,
                            source_job_id=job.id,
                        )

            from app.services.rag_service import invalidate_rag_caches

            invalidate_rag_caches()
            from app.services.readiness_service import invalidate_readiness_cache

            invalidate_readiness_cache()
            return True
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc)
            current = job_repo.get(job_id)
            if current is not None and current.lease_owner == worker_id:
                job_repo.update(
                    current,
                    status="failed",
                    error_message=str(exc)[:500],
                )
            return False


def _worker_loop() -> None:
    from app.bootstrap.container import get_provider_container

    container = get_provider_container()
    logger.info("Index worker started as %s", _WORKER_ID)
    while not _worker_stop.is_set():
        try:
            with container.metadata.session() as db:
                job_repo = IndexJobRepository(db, knowledge_base_id=None)
                job_repo.recover_expired_leases()
                if job_repo.has_running_write_job():
                    _worker_stop.wait(2)
                    continue
                job = job_repo.claim_next_pending(
                    worker_id=_WORKER_ID,
                    lease_seconds=INDEX_JOB_LEASE_SECONDS,
                )

            if job is None:
                container.task_queue.wait(5)
                continue

            logger.info("Processing index job: %s (type=%s)", job.id, job.job_type)
            _process_job(job.id, worker_id=_WORKER_ID, already_claimed=True)
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            _worker_stop.wait(10)

    logger.info("Index worker stopped")


def reconcile_pending_jobs(limit: int = 500) -> dict:
    """Recover expired leases and republish pending job IDs idempotently."""
    from app.bootstrap.container import get_provider_container

    container = get_provider_container()
    with container.metadata.session() as db:
        repository = IndexJobRepository(db, knowledge_base_id=None)
        recovered = repository.recover_expired_leases()
        pending_ids = repository.list_pending_ids(limit=limit)
    published = 0
    for job_id in pending_ids:
        container.task_queue.enqueue(job_id)
        published += 1
    return {"recovered": recovered, "published": published}


def start_worker() -> None:
    global _worker_thread
    if not INDEX_WORKER_ENABLED:
        return
    from app.bootstrap.container import get_provider_container

    if get_provider_container().task_queue.identity().provider != "inline":
        logger.info("In-process worker disabled for external task queue")
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        daemon=True,
        name="index-worker",
    )
    _worker_thread.start()


def stop_worker() -> None:
    _worker_stop.set()
    if _worker_thread:
        _worker_thread.join(timeout=10)


def get_worker_status() -> dict:
    if not INDEX_WORKER_ENABLED:
        return {"enabled": False, "alive": False, "stopping": False}
    thread = _worker_thread
    return {
        "enabled": True,
        "alive": bool(thread and thread.is_alive()),
        "stopping": _worker_stop.is_set(),
        "thread_name": thread.name if thread else None,
        "worker_id": _WORKER_ID,
    }
