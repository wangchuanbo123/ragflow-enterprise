"""Small persisted connector scheduler for local deployments."""

from __future__ import annotations

import logging
import threading

from app.core.config import CONNECTOR_SCHEDULER_ENABLED, CONNECTOR_SCHEDULER_INTERVAL_SECONDS

logger = logging.getLogger(__name__)
_thread: threading.Thread | None = None
_stop = threading.Event()


def enqueue_due_sources(limit: int = 100) -> int:
    from app.bootstrap.container import get_provider_container
    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
    from app.services.data_source_service import DataSourceService

    container = get_provider_container()
    with container.metadata.session() as db:
        sources = KnowledgeBaseRepository(db).list_due_data_sources(
            __import__("datetime").datetime.utcnow(), limit
        )
        queued = 0
        for source in sources:
            try:
                if DataSourceService(
                    db, source.knowledge_base_id, task_queue=container.task_queue
                ).request_sync(source.id, requested_by=None):
                    queued += 1
            except Exception:
                logger.exception("Failed to schedule connector source %s", source.id)
        return queued


def _loop() -> None:
    while not _stop.wait(CONNECTOR_SCHEDULER_INTERVAL_SECONDS):
        try:
            enqueue_due_sources()
        except Exception:
            logger.exception("Connector scheduler iteration failed")


def start_connector_scheduler() -> None:
    global _thread
    if not CONNECTOR_SCHEDULER_ENABLED or (_thread and _thread.is_alive()):
        return
    from app.bootstrap.container import get_provider_container
    if get_provider_container().task_queue.identity().provider != "inline":
        logger.info("In-process connector scheduler disabled for external task queue")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="connector-scheduler")
    _thread.start()


def stop_connector_scheduler() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
