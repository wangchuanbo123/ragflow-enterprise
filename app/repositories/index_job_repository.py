"""Knowledge index job persistence, leases, progress, and recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import IndexJob, IndexJobEvent, IndexJobItem


def _utcnow() -> datetime:
    # Models currently use timezone-naive DateTime for SQLite/PostgreSQL parity.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _due(now: datetime):
    """Jobs without a visible_at gate are due immediately; others wait for it."""
    return or_(
        IndexJob.visible_at.is_(None),
        IndexJob.visible_at <= now,
    )


class IndexJobRepository:
    def __init__(
        self,
        db: Session,
        knowledge_base_id: str | None = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    def _scope(self, stmt):
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        return stmt

    def create(self, job: IndexJob) -> IndexJob:
        if self.knowledge_base_id is not None:
            if job.knowledge_base_id not in {None, self.knowledge_base_id}:
                raise ValueError("Index job belongs to another knowledge base")
            job.knowledge_base_id = self.knowledge_base_id
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        self.add_event(job, "created")
        return job

    def get(self, job_id: str) -> IndexJob | None:
        return self.db.scalar(
            self._scope(select(IndexJob).where(IndexJob.id == job_id))
        )

    def list_jobs(self, limit: int = 50) -> list[IndexJob]:
        stmt = self._scope(select(IndexJob))
        return list(
            self.db.scalars(
                stmt.order_by(IndexJob.created_at.desc()).limit(limit)
            )
        )

    def find_open(
        self,
        job_type: str,
        *,
        option_key: str | None = None,
        option_value: str | None = None,
    ) -> IndexJob | None:
        rows = list(self.db.scalars(self._scope(select(IndexJob).where(
            IndexJob.job_type == job_type,
            IndexJob.status.in_(["pending", "running"]),
            IndexJob.cancel_requested.is_(False),
        )).order_by(IndexJob.created_at.desc())))
        if option_key is None:
            return rows[0] if rows else None
        return next((
            row for row in rows
            if str((row.options_json or {}).get(option_key, "")) == str(option_value or "")
        ), None)

    def claim_next_pending(
        self,
        worker_id: str = "inline-worker",
        lease_seconds: int = 300,
    ) -> IndexJob | None:
        now = _utcnow()
        pending = select(IndexJob.id).where(
            IndexJob.status == "pending",
            IndexJob.cancel_requested.is_(False),
            IndexJob.attempt_count < IndexJob.max_attempts,
            _due(now),
        )
        if self.knowledge_base_id is not None:
            pending = pending.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        job_id = self.db.scalar(
            pending.order_by(IndexJob.created_at.asc()).limit(1)
        )
        if job_id is None:
            return None
        return self.claim(job_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> IndexJob | None:
        now = _utcnow()
        statement = (
            update(IndexJob)
            .where(
                IndexJob.id == job_id,
                IndexJob.status == "pending",
                IndexJob.cancel_requested.is_(False),
                IndexJob.attempt_count < IndexJob.max_attempts,
                _due(now),
            )
            .values(
                status="running",
                started_at=now,
                finished_at=None,
                attempt_count=IndexJob.attempt_count + 1,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )
        )
        if self.knowledge_base_id is not None:
            statement = statement.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        claimed = self.db.execute(statement)
        self.db.commit()
        if claimed.rowcount != 1:
            return None
        job = self.db.get(IndexJob, job_id)
        if job is None:
            return None
        self.db.refresh(job)
        self.add_event(job, "claimed", payload={"worker_id": worker_id})
        return job

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        now = _utcnow()
        statement = (
            update(IndexJob)
            .where(
                IndexJob.id == job_id,
                IndexJob.status == "running",
                IndexJob.lease_owner == worker_id,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
        )
        if self.knowledge_base_id is not None:
            statement = statement.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        result = self.db.execute(statement)
        self.db.commit()
        return result.rowcount == 1

    def has_running_write_job(self) -> bool:
        stmt = self._scope(
            select(IndexJob).where(
                IndexJob.status == "running",
                IndexJob.job_type.in_([
                    "sync", "rebuild", "connector_sync", "code_index",
                    "graph_rebuild", "entity_vector_rebuild",
                    "relation_vector_rebuild", "code_vector_rebuild",
                    "graph_maintenance", "schema_generation", "schema_drift",
                ]),
            )
        )
        return self.db.scalar(stmt.limit(1)) is not None

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
        if status in {"completed", "partial", "failed", "cancelled"}:
            job.finished_at = _utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
        self.db.commit()
        self.db.refresh(job)
        self.add_event(
            job,
            "progress" if status is None else "status",
            message=error_message,
        )
        return job

    def request_cancel(self, job_id: str) -> IndexJob | None:
        job = self.get(job_id)
        if job is None or job.status in {"completed", "failed", "cancelled"}:
            return job
        job.cancel_requested = True
        if job.status == "pending":
            job.status = "cancelled"
            job.finished_at = _utcnow()
        self.db.commit()
        self.db.refresh(job)
        self.add_event(job, "cancel_requested")
        return job

    def is_cancel_requested(self, job_id: str) -> bool:
        stmt = select(IndexJob.cancel_requested).where(IndexJob.id == job_id)
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        return bool(self.db.scalar(stmt))

    def recover_expired_leases(self) -> int:
        now = _utcnow()
        stmt = select(IndexJob).where(
            IndexJob.status == "running",
            or_(
                IndexJob.lease_expires_at.is_(None),
                IndexJob.lease_expires_at <= now,
            ),
        )
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        stale = list(self.db.scalars(stmt))
        for job in stale:
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = now
            elif job.attempt_count < job.max_attempts:
                job.status = "pending"
                job.started_at = None
            else:
                job.status = "failed"
                job.error_message = "Job lease expired after maximum attempts"
                job.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
        if stale:
            self.db.commit()
            for job in stale:
                self.add_event(job, "lease_recovered")
        return len(stale)

    def recover_stale_jobs(self) -> int:
        return self.recover_expired_leases()

    def list_pending_ids(self, limit: int = 500) -> list[str]:
        now = _utcnow()
        stmt = select(IndexJob.id).where(
            IndexJob.status == "pending",
            IndexJob.cancel_requested.is_(False),
            IndexJob.attempt_count < IndexJob.max_attempts,
            _due(now),
        )
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJob.knowledge_base_id == self.knowledge_base_id
            )
        return list(
            self.db.scalars(
                stmt.order_by(IndexJob.created_at.asc()).limit(limit)
            )
        )

    def retry(self, job_id: str) -> IndexJob | None:
        """Reset only unfinished items and return the job to the pending state."""
        job = self.get(job_id)
        if job is None:
            return None
        if job.status not in {"failed", "partial", "cancelled"}:
            raise ValueError(f"Job in status {job.status!r} cannot be retried")

        items = self.list_items(job_id)
        completed = 0
        for item in items:
            if item.status == "completed":
                completed += 1
                continue
            item.status = "pending"
            item.error_message = None
            item.started_at = None
            item.finished_at = None

        job.status = "pending"
        job.processed_items = completed
        job.succeeded_items = completed
        job.failed_items = 0
        job.error_message = None
        job.cancel_requested = False
        job.attempt_count = 0
        job.started_at = None
        job.finished_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        self.db.commit()
        self.db.refresh(job)
        self.add_event(job, "retried")
        return job

    def add_event(
        self,
        job: IndexJob,
        event_type: str,
        *,
        message: str | None = None,
        payload: dict | None = None,
    ) -> IndexJobEvent:
        event = IndexJobEvent(
            knowledge_base_id=job.knowledge_base_id,
            job_id=job.id,
            event_type=event_type,
            status=job.status,
            processed_items=job.processed_items,
            total_items=job.total_items,
            message=message,
            payload_json=payload,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def list_events(
        self,
        job_id: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[IndexJobEvent]:
        stmt = select(IndexJobEvent).where(
            IndexJobEvent.job_id == job_id,
            IndexJobEvent.id > after_id,
        )
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJobEvent.knowledge_base_id == self.knowledge_base_id
            )
        return list(
            self.db.scalars(stmt.order_by(IndexJobEvent.id).limit(limit))
        )

    def add_items(self, items: list[IndexJobItem]) -> None:
        for item in items:
            if self.knowledge_base_id is not None:
                if item.knowledge_base_id not in {None, self.knowledge_base_id}:
                    raise ValueError("Index job item belongs to another knowledge base")
                item.knowledge_base_id = self.knowledge_base_id
            self.db.add(item)
        self.db.commit()

    def list_items(self, job_id: str) -> list[IndexJobItem]:
        stmt = select(IndexJobItem).where(IndexJobItem.job_id == job_id)
        if self.knowledge_base_id is not None:
            stmt = stmt.where(
                IndexJobItem.knowledge_base_id == self.knowledge_base_id
            )
        return list(
            self.db.scalars(stmt.order_by(IndexJobItem.source_path))
        )
