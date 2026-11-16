"""Index job execution shared by local worker threads."""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone

from app.core.config import (
    INDEX_JOB_HEARTBEAT_SECONDS,
    INDEX_JOB_LEASE_SECONDS,
    INDEX_JOB_MAX_ATTEMPTS,
    INDEX_MAX_CONCURRENCY,
    INDEX_WORKER_ENABLED,
)
from app.repositories.index_job_repository import IndexJobRepository, JobLeaseLostError, signal_persisted_job

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_worker_failure: str | None = None
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
                self.lease_lost = True
                return

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


def _request_document_code_rebuild(db, knowledge_base_id: str, container):
    """文档索引完成后，排一个文档内代码图谱重建任务（幂等）。

    有活动目录源时重建该源并合并文档代码，否则重建文档代码快照。
    """
    from app.core.config import DOCUMENT_CODE_GRAPH_ENABLED
    from app.models.index_job import IndexJob
    from app.repositories.code_graph_repository import CodeGraphRepository
    from app.repositories.index_job_repository import IndexJobRepository

    if not DOCUMENT_CODE_GRAPH_ENABLED:
        return
    repository = CodeGraphRepository(db, knowledge_base_id)
    active = repository.active_snapshot()
    source_id = active.data_source_id if active is not None and active.data_source_id else "documents"
    job_repository = IndexJobRepository(db, knowledge_base_id)
    existing = job_repository.find_open(
        "code_index", option_key="data_source_id", option_value=source_id
    )
    if existing is not None:
        return existing
    job = job_repository.create(IndexJob(
        job_type="code_index",
        status="pending",
        requested_by="system:document-code",
        total_items=1,
        max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        options_json={"data_source_id": source_id},
    ))
    signal_persisted_job(container.task_queue, job.id)
    return job


def _link_followup_jobs(db, parent, children) -> None:
    identifiers = set((parent.options_json or {}).get("followup_job_ids") or [])
    run_id = (parent.options_json or {}).get("agent_run_id")
    for child in children:
        if child is None or child.id == parent.id:
            continue
        identifiers.add(child.id)
        options = dict(child.options_json or {})
        options.setdefault("parent_job_id", parent.id)
        if run_id:
            options.setdefault("agent_run_id", run_id)
        child.options_json = options
    if identifiers:
        parent.options_json = {**(parent.options_json or {}), "followup_job_ids": sorted(identifiers)}
        db.commit()


def _process_job(
    job_id: str, *, worker_id: str | None = None, already_claimed: bool = False,
) -> bool:
    from app.bootstrap.container import get_provider_container
    from app.services.knowledge_mutation import knowledge_mutation_lock, KnowledgeLockBusyError

    container = get_provider_container()
    worker_id = worker_id or f"{_WORKER_ID[:80]}:{uuid.uuid4().hex}"
    with container.metadata.session() as db:
        repository = IndexJobRepository(db, knowledge_base_id=None)
        job = repository.get(job_id) if already_claimed else repository.claim(
            job_id, worker_id=worker_id, lease_seconds=INDEX_JOB_LEASE_SECONDS,
        )
        if job is None:
            return False
        if job.status != "running" or job.lease_owner != worker_id:
            return False
        knowledge_base_id = job.knowledge_base_id
        job_type = job.job_type
    try:
        # Begin renewal before waiting for a busy knowledge base. Otherwise
        # queued lock waiters expire and another process can reclaim them.
        with _LeaseHeartbeat(job_id, worker_id) as heartbeat:
            if job_type == "agent_workflow":
                return _process_job_unlocked(job_id, worker_id=worker_id, already_claimed=True, _heartbeat=heartbeat)
            with knowledge_mutation_lock(knowledge_base_id, cancelled=lambda: _job_cancel_requested(container, job_id, heartbeat)):
                return _process_job_unlocked(job_id, worker_id=worker_id, already_claimed=True, _heartbeat=heartbeat)
    except Exception as exc:
        from app.agents.contracts import AgentCancelledError
        if isinstance(exc, JobLeaseLostError):
            logger.error("Lost execution lease for job %s", job_id)
            return False
        if isinstance(exc, KnowledgeLockBusyError):
            with container.metadata.session() as db:
                if IndexJobRepository(db, None).defer_lock_waiter(job_id, worker_id):
                    signal_persisted_job(container.task_queue, job_id)
                    return False
        with container.metadata.session() as db:
            repository = IndexJobRepository(db, knowledge_base_id=None)
            current = repository.get(job_id)
            if current is not None and current.status == "running" and current.lease_owner == worker_id:
                db.info["index_job_lease"] = (job_id, worker_id)
                repository.update(current, status="cancelled" if isinstance(exc, AgentCancelledError) else "failed",
                                  error_message="任务已取消" if isinstance(exc, AgentCancelledError) else "任务协调或知识库锁失败")
        logger.error("Job %s could not enter its execution scope: %s", job_id, type(exc).__name__)
        return False


def _total_schema_options(db, job):
    """Retry legacy schema jobs against the single KB schema, preserving audit."""
    if job.job_type not in {"schema_generation", "schema_drift"}:
        return
    options = dict(job.options_json or {})
    previous = options.get("graph_kind") or "general"
    if previous not in {"general", "concept", "functional", "configuration", "standard"}:
        raise ValueError("图谱类型不可用")
    if previous != "general":
        options["legacy_graph_kind"] = previous
    options["graph_kind"] = "general"
    target_id = options.get("target_schema_id")
    if target_id:
        from app.models.graph_governance import GraphSchemaDefinition
        target = db.get(GraphSchemaDefinition, target_id)
        if target is not None and target.knowledge_base_id == job.knowledge_base_id and target.graph_kind != "general":
            options["legacy_target_schema_id"] = target_id
            options["target_schema_id"] = None
    if options != (job.options_json or {}):
        job.options_json = options
        db.commit()


def _process_job_unlocked(
    job_id: str,
    *,
    worker_id: str | None = None,
    already_claimed: bool = False,
    _heartbeat=None,
) -> bool:
    """Claim and process one persisted job."""
    from app.bootstrap.container import get_provider_container
    from app.core.config import DOC_DIR
    from rag.indexing.index_manager import IndexManager

    worker_id = worker_id or _WORKER_ID
    from app.agents.contracts import AgentCancelledError
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

        db.info["index_job_lease"] = (job_id, worker_id)

        attached_service = None
        attached_run_id = None
        attached_result = None
        attached_error_code = None
        ownership_lost = False
        try:
            from app.services.task_execution import stage_recorder, persist_stage
            from app.services.observability import operation_context
            with stage_recorder(lambda payload: persist_stage(db, job_repo, job, payload)), operation_context(
                job_id=job_id, knowledge_base_id=job.knowledge_base_id,
            ), (nullcontext(_heartbeat) if _heartbeat is not None else _LeaseHeartbeat(job_id, worker_id)) as heartbeat:
                mgr = IndexManager(
                    db,
                    vector_provider=container.vector_store,
                    knowledge_base_id=job.knowledge_base_id,
                )
                from app.models.knowledge_base import KnowledgeBase

                knowledge_base = db.get(KnowledgeBase, job.knowledge_base_id)
                if knowledge_base is None or not knowledge_base.is_active:
                    job_repo.update(job, status="cancelled", error_message="知识库已归档或不存在")
                    return True
                _total_schema_options(db, job)
                parent_run_id = (job.options_json or {}).get("agent_run_id")
                if parent_run_id:
                    from app.models.agent_run import AgentRun

                    parent_run = db.get(AgentRun, parent_run_id)
                    if parent_run is not None and (parent_run.cancel_requested or parent_run.status == "cancelled"):
                        job_repo.update(job, status="cancelled", error_message="所属Agent任务已取消")
                        return True

                if job_repo.is_cancel_requested(job_id):
                    job_repo.update(job, status="cancelled")
                    return True

                deferred_completion = None
                agent_context = None
                agent_document_ids = None
                agent_event_callback = None
                def agent_cancelled():
                    return _job_cancel_requested(container, job_id, heartbeat) or (
                        attached_service is not None and attached_service.attached_cancelled(attached_run_id)
                    )
                agent_job_types = {"graph_rebuild", "schema_generation", "schema_drift"}
                from app.core.config import CODE_FUNCTION_VECTOR_ENABLED, CODE_FUNCTION_SUMMARY_LLM_ENABLED, CODE_VECTOR_ENABLED
                if CODE_FUNCTION_VECTOR_ENABLED and CODE_FUNCTION_SUMMARY_LLM_ENABLED:
                    agent_job_types.add("code_vector_rebuild")
                    if CODE_VECTOR_ENABLED:
                        agent_job_types.add("code_index")
                if job.job_type in agent_job_types:
                    from app.services.agent_workflow_service import AgentWorkflowService

                    attached_service = AgentWorkflowService(db, job.knowledge_base_id)
                    attached_run, agent_context = attached_service.attach_existing_job(
                        job.id, task=("extraction" if job.job_type == "graph_rebuild" else "knowledge_organization"
                                      if job.job_type in {"code_index", "code_vector_rebuild"} else "schema_governance"),
                        document_ids=(job.options_json or {}).get("document_ids"),
                    )
                    attached_run_id = attached_run.id
                    agent_document_ids = list(attached_run.document_ids_json)
                    if job.job_type in {"code_index", "code_vector_rebuild"}:
                        agent_context = {**agent_context, "scope_document_ids": agent_document_ids,
                                         "allow_repository_symbols": not bool(getattr(attached_run, "parent_run_id", None))}
                    agent_event_callback = lambda event: attached_service.append_attached_event(attached_run_id, event)
                from app.services.job_handlers import JobContext, execute_job
                from app.services.job_policy import STAGES
                from app.services.task_execution import job_stage
                with job_stage(STAGES.get(job.job_type, "task")):
                    handled = execute_job(JobContext(
                        db, job, job_repo, mgr, container, job_id, heartbeat,
                        agent_context, agent_document_ids, agent_cancelled,
                        agent_event_callback, _link_followup_jobs,
                    ))
                if handled.stop:
                    return True
                deferred_completion = handled.deferred_completion
                attached_result = handled.attached_result
                attached_error_code = handled.attached_error_code

                if job.job_type in {"sync", "rebuild", "file", "delete", "graph_rebuild"}:
                    if (
                        deferred_completion is not None
                        and deferred_completion["status"] in {"completed", "partial"}
                        and deferred_completion["succeeded_items"] > 0
                    ):
                        from app.services.graph_governance_service import GraphGovernanceService

                        children = []
                        if job.job_type in {"sync", "rebuild", "file"}:
                            children.append(GraphGovernanceService(
                                db,
                                job.knowledge_base_id,
                                task_queue=container.task_queue,
                            ).request_automatic_schema_generation(
                                job.requested_by,
                                source_job_id=job.id,
                            ))
                        children.append(GraphGovernanceService(
                            db,
                            job.knowledge_base_id,
                            task_queue=container.task_queue,
                        ).request_automatic_build(
                            job.requested_by,
                            source_job_id=job.id,
                        ))
                        children.append(_request_document_code_rebuild(
                            db, job.knowledge_base_id, container
                        ))
                        _link_followup_jobs(db, job, children)
                # Publish the parent terminal status only after all derived
                # jobs are linked. Lazy run reconciliation must not observe a
                # completed parent in the gap before its children are enqueued.
                if deferred_completion is not None:
                    job_repo.update(job, **deferred_completion)

            from app.services.rag_service import invalidate_rag_caches

            invalidate_rag_caches()
            from app.services.readiness_service import invalidate_readiness_cache

            invalidate_readiness_cache()
            return True
        except JobLeaseLostError:
            ownership_lost = True
            db.rollback()
            db.info.pop("index_job_lease", None)
            logger.error("Job %s stopped after losing its execution lease", job_id)
            return False
        except AgentCancelledError:
            attached_error_code = "AGENT_CANCELLED"
            db.rollback()
            current = job_repo.get(job_id)
            if current is not None and current.lease_owner == worker_id:
                job_repo.update(current, status="cancelled", error_message="Agent任务已取消")
            return True
        except Exception as exc:
            attached_error_code = getattr(exc, "code", "AGENT_JOB_FAILED")
            logger.error("Job %s failed: %s", job_id, exc)
            db.rollback()
            current = job_repo.get(job_id)
            if current is not None and current.lease_owner == worker_id:
                job_repo.update(
                    current,
                    status="failed",
                    error_message=str(exc)[:500],
                )
            return False
        finally:
            try:
                from app.services.agent_workflow_service import reconcile_workflow_followups

                if not ownership_lost and attached_service is not None and attached_run_id is not None:
                    current = job_repo.get(job_id)
                    status = "succeeded" if current and current.status == "completed" else (
                        "cancelled" if current and current.status == "cancelled" else "failed"
                    )
                    attached_service.finish_attached(attached_run_id, result=attached_result, status=status, error_code=attached_error_code)
                reconcile_workflow_followups(db, job.knowledge_base_id)
            except Exception:
                db.rollback()
                logger.exception("Could not reconcile agent follow-up jobs")


def _job_cancel_requested(container, job_id, heartbeat) -> bool:
    if heartbeat.lease_lost:
        raise JobLeaseLostError("任务租约心跳已丢失")
    # Called from extraction threads: never reuse the worker's ORM session.
    with container.metadata.session() as db:
        return IndexJobRepository(db, knowledge_base_id=None).leased_cancel_requested(job_id, heartbeat.worker_id)


def _worker_loop() -> None:
    run_worker_forever()


def run_worker_forever() -> None:
    """Run a dedicated worker. Redis only wakes polling; DB claims own jobs."""
    global _worker_failure
    from app.bootstrap.container import get_provider_container
    from app.core import config

    container = get_provider_container()
    distributed = container.task_queue.identity().provider == "redis"
    if config.APP_PROFILE not in {"local", "test"} and not distributed:
        raise RuntimeError("Server workers require the Redis wakeup adapter")
    _worker_failure = None
    logger.info("Index worker started as %s", _WORKER_ID)
    futures = set()
    last_heartbeat = 0.0
    try:
        with ThreadPoolExecutor(max_workers=INDEX_MAX_CONCURRENCY, thread_name_prefix="index-job") as pool:
            while not _worker_stop.is_set():
                if distributed and time.monotonic() - last_heartbeat >= getattr(config, "WORKER_HEARTBEAT_SECONDS", 5):
                    from app.adapters.redis import publish_worker_heartbeat
                    publish_worker_heartbeat(_WORKER_ID, {
                        "worker_id": _WORKER_ID, "pid": os.getpid(), "concurrency": INDEX_MAX_CONCURRENCY,
                        "running_jobs": len(futures), "stopping": False,
                    }, getattr(config, "WORKER_HEARTBEAT_TTL_SECONDS", 20))
                    last_heartbeat = time.monotonic()
                for future in tuple(futures):
                    if future.done():
                        futures.remove(future)
                        future.result()
                if len(futures) >= INDEX_MAX_CONCURRENCY:
                    _worker_stop.wait(.1)
                    continue
                owner = f"{_WORKER_ID[:80]}:{uuid.uuid4().hex}"
                with container.metadata.session() as db:
                    repository = IndexJobRepository(db, knowledge_base_id=None)
                    repository.recover_expired_leases()
                    job = repository.claim_next_pending(worker_id=owner, lease_seconds=INDEX_JOB_LEASE_SECONDS)
                    job_id = job.id if job else None
                if job_id is None:
                    container.task_queue.wait(.5)
                    continue
                futures.add(pool.submit(_process_job, job_id, worker_id=owner, already_claimed=True))
    except Exception as exc:
        _worker_failure = type(exc).__name__
        logger.exception("Worker stopped because shared coordination failed")
        raise
    finally:
        if distributed:
            from app.adapters.redis import remove_worker_heartbeat
            remove_worker_heartbeat(_WORKER_ID)
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
    from app.core import config

    if config.APP_PROFILE not in {"local", "test"} or getattr(config, "APP_ROLE", "all") not in {"all", "worker"}:
        logger.info("API process does not embed index workers")
        return
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
    from app.core import config
    if config.TASK_QUEUE_PROVIDER == "redis":
        from app.adapters.redis import active_workers
        workers = active_workers()
        live = [worker for worker in workers if not worker.get("stopping")]
        return {"enabled": True, "alive": bool(live), "stopping": False,
                "workers": workers, "mode": "external", "worker_count": len(live)}
    if not INDEX_WORKER_ENABLED:
        return {"enabled": False, "alive": False, "stopping": False}
    thread = _worker_thread
    return {
        "enabled": True,
        "alive": bool(thread and thread.is_alive()),
        "stopping": _worker_stop.is_set(),
        "thread_name": thread.name if thread else None,
        "worker_id": _WORKER_ID,
        "error": _worker_failure,
    }
