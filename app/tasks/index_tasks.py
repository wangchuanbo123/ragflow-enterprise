"""Celery task wrappers around the shared persisted-job executor."""

from __future__ import annotations

from app.core.config import (
    CELERY_CONNECTOR_SCHEDULE_TASK_NAME,
    CELERY_GRAPH_MAINTENANCE_TASK_NAME,
    CELERY_INDEX_TASK_NAME,
    CELERY_RECONCILE_TASK_NAME,
)
from app.tasks.celery_app import celery_app


@celery_app.task(name=CELERY_INDEX_TASK_NAME, bind=True)
def process_index_job(self, job_id: str) -> dict:
    from app.services.index_worker import _process_job

    hostname = getattr(self.request, "hostname", None) or "unknown"
    task_id = getattr(self.request, "id", None) or "unknown"
    worker_id = f"celery:{hostname}:{task_id}"
    processed = _process_job(job_id, worker_id=worker_id, already_claimed=False)
    return {"job_id": job_id, "processed": processed}


@celery_app.task(name=CELERY_RECONCILE_TASK_NAME)
def reconcile_index_jobs() -> dict:
    from app.services.index_worker import reconcile_pending_jobs

    return reconcile_pending_jobs()


@celery_app.task(name=CELERY_CONNECTOR_SCHEDULE_TASK_NAME)
def schedule_due_connectors() -> dict:
    from app.services.connector_scheduler import enqueue_due_sources

    return {"queued": enqueue_due_sources()}


@celery_app.task(name=CELERY_GRAPH_MAINTENANCE_TASK_NAME)
def schedule_graph_maintenance() -> dict:
    from app.services.graph_maintenance import enqueue_stale_community_rebuilds

    return {"queued": enqueue_stale_community_rebuilds()}
