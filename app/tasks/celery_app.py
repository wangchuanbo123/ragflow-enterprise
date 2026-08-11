"""Celery application used by enterprise index workers."""

from __future__ import annotations

try:
    from celery import Celery
except ImportError as exc:  # pragma: no cover - enterprise dependency guard
    raise RuntimeError(
        "Celery worker requires the enterprise dependencies from requirements.txt"
    ) from exc

from app.core.config import (
    CELERY_BROKER_URL,
    CELERY_INDEX_QUEUE,
    CELERY_INDEX_TASK_NAME,
    CELERY_CONNECTOR_SCHEDULE_TASK_NAME,
    CELERY_RECONCILE_TASK_NAME,
    CELERY_RESULT_BACKEND,
    CONNECTOR_SCHEDULER_INTERVAL_SECONDS,
    INDEX_JOB_RECONCILE_SECONDS,
)

celery_app = Celery(
    "ragflow-enterprise",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND or None,
    include=["app.tasks.index_tasks"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue=CELERY_INDEX_QUEUE,
    task_routes={
        CELERY_INDEX_TASK_NAME: {"queue": CELERY_INDEX_QUEUE},
        CELERY_RECONCILE_TASK_NAME: {"queue": CELERY_INDEX_QUEUE},
        CELERY_CONNECTOR_SCHEDULE_TASK_NAME: {"queue": CELERY_INDEX_QUEUE},
    },
    beat_schedule={
        "reconcile-index-jobs": {
            "task": CELERY_RECONCILE_TASK_NAME,
            "schedule": INDEX_JOB_RECONCILE_SECONDS,
        },
        "schedule-due-connectors": {
            "task": CELERY_CONNECTOR_SCHEDULE_TASK_NAME,
            "schedule": CONNECTOR_SCHEDULER_INTERVAL_SECONDS,
        },
    },
    broker_connection_retry_on_startup=True,
)
