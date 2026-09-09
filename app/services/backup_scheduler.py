"""Low-frequency monthly knowledge-base backup scheduler."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import (
    BACKUP_MONTHLY_ENABLED,
    BACKUP_MONTHLY_INTERVAL_DAYS,
    BACKUP_RETENTION_VERSIONS,
    BACKUP_SCHEDULER_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)
_thread: threading.Thread | None = None
_stop = threading.Event()


def run_monthly_backups() -> int:
    if not BACKUP_MONTHLY_ENABLED:
        return 0
    from app.bootstrap.container import get_provider_container
    from app.models.backup import KnowledgeBackup
    from app.models.knowledge_base import KnowledgeBase
    from app.services.backup_service import BackupService

    container = get_provider_container()
    created = 0
    cutoff = datetime.utcnow() - timedelta(days=BACKUP_MONTHLY_INTERVAL_DAYS)
    with container.metadata.session() as db:
        knowledge_bases = list(db.scalars(select(KnowledgeBase).where(KnowledgeBase.is_active.is_(True))))
        for kb in knowledge_bases:
            latest = db.scalar(select(KnowledgeBackup).where(
                KnowledgeBackup.knowledge_base_id == kb.id,
                KnowledgeBackup.backup_type == "knowledge_base",
                KnowledgeBackup.trigger == "monthly",
            ).order_by(KnowledgeBackup.created_at.desc()).limit(1))
            if latest is not None and latest.created_at >= cutoff:
                continue
            try:
                BackupService(db, kb.id, container.object_store).create(
                    "knowledge_base", label="每月自动备份", trigger="monthly"
                )
                created += 1
                monthly = list(db.scalars(select(KnowledgeBackup).where(
                    KnowledgeBackup.knowledge_base_id == kb.id,
                    KnowledgeBackup.backup_type == "knowledge_base",
                    KnowledgeBackup.trigger == "monthly",
                ).order_by(KnowledgeBackup.created_at.desc())))
                for old in monthly[BACKUP_RETENTION_VERSIONS:]:
                    container.object_store.delete(old.object_key)
                    db.delete(old)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Monthly backup failed for knowledge base %s", kb.id)
    return created


def _loop() -> None:
    while not _stop.is_set():
        try:
            run_monthly_backups()
        except Exception:
            logger.exception("Monthly backup scheduler iteration failed")
        if _stop.wait(BACKUP_SCHEDULER_INTERVAL_SECONDS):
            break


def start_backup_scheduler() -> None:
    global _thread
    if not BACKUP_MONTHLY_ENABLED or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="backup-scheduler")
    _thread.start()


def stop_backup_scheduler() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
