"""低峰期社区维护：为 stale 状态的知识库排队延迟社区重建任务。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.core.config import (
    GRAPH_COMMUNITY_MAINTENANCE_ENABLED,
    GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS,
    GRAPH_COMMUNITY_MAINTENANCE_WINDOW,
    INDEX_JOB_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)
_thread: threading.Thread | None = None
_stop = threading.Event()


def within_maintenance_window(now: datetime | None = None) -> bool:
    """GRAPH_COMMUNITY_MAINTENANCE_WINDOW 形如 '22-6'（起止小时，可跨天）；空值表示全天。"""
    if not GRAPH_COMMUNITY_MAINTENANCE_WINDOW:
        return True
    try:
        start_text, end_text = GRAPH_COMMUNITY_MAINTENANCE_WINDOW.split("-", 1)
        start, end = int(start_text) % 24, int(end_text) % 24
    except ValueError:
        return True
    hour = (now or datetime.utcnow()).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def enqueue_stale_community_rebuilds(limit: int = 20) -> int:
    """为所有 stale 社区的知识库创建 graph_maintenance 任务（自动去重）。"""
    from app.bootstrap.container import get_provider_container
    from app.models.graph_governance import GraphCommunityState
    from app.models.index_job import IndexJob
    from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
    from sqlalchemy import select

    if not GRAPH_COMMUNITY_MAINTENANCE_ENABLED:
        return 0
    if not within_maintenance_window():
        return 0

    container = get_provider_container()
    queued = 0
    with container.metadata.session() as db:
        knowledge_base_ids = list(db.scalars(
            select(GraphCommunityState.knowledge_base_id)
            .where(GraphCommunityState.status == "stale")
            .order_by(GraphCommunityState.updated_at.asc())
            .limit(limit)
        ))
        for knowledge_base_id in knowledge_base_ids:
            repository = IndexJobRepository(db, knowledge_base_id)
            if repository.find_open("graph_rebuild") is not None:
                continue
            if repository.find_open("graph_maintenance") is not None:
                continue
            job = repository.create(IndexJob(
                job_type="graph_maintenance",
                status="pending",
                requested_by=None,
                total_items=1,
                max_attempts=INDEX_JOB_MAX_ATTEMPTS,
                options_json={"trigger": "community_maintenance"},
            ))
            signal_persisted_job(container.task_queue, job.id)
            queued += 1
    if queued:
        logger.info("Queued %d community maintenance jobs", queued)
    return queued


def _loop() -> None:
    while not _stop.wait(GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS):
        try:
            enqueue_stale_community_rebuilds()
        except Exception:
            logger.exception("Community maintenance iteration failed")


def start_graph_maintenance_scheduler() -> None:
    global _thread
    if not GRAPH_COMMUNITY_MAINTENANCE_ENABLED or (_thread and _thread.is_alive()):
        return
    from app.bootstrap.container import get_provider_container
    if get_provider_container().task_queue.identity().provider != "inline":
        logger.info("In-process community maintenance disabled for external task queue")
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, daemon=True, name="graph-community-maintenance"
    )
    _thread.start()


def stop_graph_maintenance_scheduler() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
