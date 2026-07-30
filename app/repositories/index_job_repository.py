"""索引任务仓储。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.index_job import IndexJob, IndexJobItem


class IndexJobRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, job: IndexJob) -> IndexJob:
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get(self, job_id: str) -> IndexJob | None:
        return self.db.get(IndexJob, job_id)

    def list_jobs(self, limit: int = 50) -> list[IndexJob]:
        return list(self.db.scalars(
            select(IndexJob).order_by(IndexJob.created_at.desc()).limit(limit)
        ))

    def claim_next_pending(self) -> IndexJob | None:
        """原子领取一个 pending 任务并标记为 running。"""
        job_id = self.db.scalar(
            select(IndexJob.id)
            .where(IndexJob.status == "pending")
            .order_by(IndexJob.created_at.asc())
            .limit(1)
        )
        if job_id is None:
            return None

        claimed = self.db.execute(
            update(IndexJob)
            .where(IndexJob.id == job_id, IndexJob.status == "pending")
            .values(
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()
        if claimed.rowcount != 1:
            return None

        job = self.db.get(IndexJob, job_id)
        if job is None:
            return None
        self.db.refresh(job)
        return job

    def has_running_write_job(self) -> bool:
        running = self.db.scalar(
            select(IndexJob).where(
                IndexJob.status == "running",
                IndexJob.job_type.in_(["sync", "rebuild"]),
            ).limit(1)
        )
        return running is not None

    def update(
        self,
        job: IndexJob,
        *,
        status: str | None = None,
        total_items: int | None = None,
        processed_items: int | None = None,
        succeeded_items: int | None = None,
        failed_items: int | None = None,
        error_message: str | None = None,
    ) -> IndexJob:
        if status is not None:
            job.status = status
        if total_items is not None:
            job.total_items = total_items
        if processed_items is not None:
            job.processed_items = processed_items
        if succeeded_items is not None:
            job.succeeded_items = succeeded_items
        if failed_items is not None:
            job.failed_items = failed_items
        if error_message is not None:
            job.error_message = error_message
        if status in ("completed", "partial", "failed"):
            job.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(job)
        return job

    def recover_stale_jobs(self) -> int:
        """把遗留 running 任务恢复为 pending。"""
        stale = list(self.db.scalars(
            select(IndexJob).where(IndexJob.status == "running")
        ))
        for job in stale:
            job.status = "pending"
        if stale:
            self.db.commit()
        return len(stale)

    # --- Items ---

    def add_items(self, items: list[IndexJobItem]) -> None:
        for item in items:
            self.db.add(item)
        self.db.commit()

    def list_items(self, job_id: str) -> list[IndexJobItem]:
        return list(self.db.scalars(
            select(IndexJobItem)
            .where(IndexJobItem.job_id == job_id)
            .order_by(IndexJobItem.source_path)
        ))
