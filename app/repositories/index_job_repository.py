"""Knowledge index job persistence, leases, progress, and recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import case, event, func, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import IndexJob, IndexJobEvent, IndexJobItem

logger = logging.getLogger(__name__)


def signal_persisted_job(queue, job_id: str) -> None:
    """Redis is a wake-up hint; the worker also polls committed DB jobs."""
    try:
        queue.enqueue(job_id)
    except Exception:
        logger.warning("Task wake-up failed; database polling will recover job %s", job_id)


class JobLeaseLostError(RuntimeError):
    code = "JOB_LEASE_LOST"


@event.listens_for(Session, "before_commit")
def _fence_worker_commit(db):
    lease = db.info.get("index_job_lease")
    if not lease or db.info.get("index_job_finishing"):
        return
    job_id, owner = lease
    row = db.execute(select(IndexJob.status, IndexJob.lease_owner, IndexJob.lease_expires_at).where(
        IndexJob.id == job_id,
    ).with_for_update()).one_or_none()
    if row is None or row.status != "running" or row.lease_owner != owner or row.lease_expires_at is None or row.lease_expires_at <= _utcnow():
        raise JobLeaseLostError("任务租约已丢失，拒绝提交旧任务写入")


def _utcnow() -> datetime:
    # Models use timezone-naive DateTime.
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

    def create(self, job: IndexJob, items: list[IndexJobItem] | None = None) -> IndexJob:
        """Publish a job, its complete item set and creation event atomically."""
        if self.knowledge_base_id is not None:
            if job.knowledge_base_id not in {None, self.knowledge_base_id}:
                raise ValueError("Index job belongs to another knowledge base")
            job.knowledge_base_id = self.knowledge_base_id
        from app.services.job_policy import PRIORITIES
        priority = (job.options_json or {}).get("priority", job.priority)
        if priority is None:
            priority = PRIORITIES.get(job.job_type, 50)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValueError("任务优先级必须是0到100的整数")
        job.priority = priority
        try:
            self.db.add(job)
            self.db.flush()
            for item in items or []:
                if item.job_id not in {None, job.id}:
                    raise ValueError("Index item belongs to another job")
                if item.knowledge_base_id not in {None, job.knowledge_base_id}:
                    raise ValueError("Index job item belongs to another knowledge base")
                item.job_id = job.id
                item.knowledge_base_id = job.knowledge_base_id
                self.db.add(item)
            self.db.add(IndexJobEvent(
                job_id=job.id, knowledge_base_id=job.knowledge_base_id,
                event_type="created", status=job.status,
                processed_items=job.processed_items, total_items=job.total_items,
            ))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(job)
        return job

    def get(self, job_id: str) -> IndexJob | None:
        return self.db.scalar(
            self._scope(select(IndexJob).where(IndexJob.id == job_id))
        )

    def find_open_document(self, document_id: str) -> IndexJob | None:
        return self.db.scalar(self._scope(select(IndexJob).join(
            IndexJobItem, IndexJobItem.job_id == IndexJob.id,
        ).where(
            IndexJob.job_type == "file", IndexJob.status.in_(["pending", "running"]),
            IndexJob.cancel_requested.is_(False),
            IndexJobItem.document_id == document_id,
            IndexJobItem.action.in_(["add", "update"]), IndexJobItem.status != "completed",
        )).order_by(IndexJob.created_at, IndexJob.id).limit(1))

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
        from app.services.job_policy import resource_expression, resource_limits
        # Serialize only the short scheduling decision. Job execution remains independent.
        # This makes resource counters consistent across multiple Worker processes.
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(724945382)")).scalar()
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
        if self.db.get_bind().dialect.name == "postgresql":
            running = aliased(IndexJob)
            busy_knowledge_base = select(running.id).where(
                running.knowledge_base_id == IndexJob.knowledge_base_id,
                running.status == "running", running.job_type != "agent_workflow",
                running.lease_expires_at > now,
            ).exists()
            pending = pending.where(or_(IndexJob.job_type == "agent_workflow", ~busy_knowledge_base))
            pending = pending.with_for_update(skip_locked=True)
        running_resource = aliased(IndexJob)
        running_class = resource_expression(running_resource.job_type)
        live = select(running_class.label("resource"), func.count().label("count")).where(
            running_resource.status == "running", running_resource.lease_expires_at > now,
        ).group_by(running_class)
        saturated = [row.resource for row in self.db.execute(live) if row._mapping["count"] >= resource_limits().get(row.resource, 2)]
        if saturated:
            pending = pending.where(resource_expression(IndexJob.job_type).not_in(saturated))
        # Old jobs cross the priority boundary after the aging interval; within a
        # band, prefer the KB that has waited longest since its last claim.
        from app.core import config
        aging = max(1, int(getattr(config, "JOB_PRIORITY_AGING_SECONDS", 900)))
        served = aliased(IndexJob)
        last_served = select(func.max(served.started_at)).where(
            served.knowledge_base_id == IndexJob.knowledge_base_id,
        ).correlate(IndexJob).scalar_subquery()
        priority = case((IndexJob.created_at <= now - timedelta(seconds=aging), -1),
                        else_=IndexJob.priority)
        pending = pending.order_by(priority.asc(), last_served.asc().nulls_first(),
                                   IndexJob.created_at.asc(), IndexJob.id).limit(1)
        job_id = self.db.scalar(pending)
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
        from app.services.observability import observe
        observe("task_queue_wait_seconds", max(0, (now - job.created_at).total_seconds()))
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
                IndexJob.lease_expires_at > now,
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

    def defer_lock_waiter(self, job_id: str, owner: str, delay_seconds: int = 2) -> bool:
        """Return an unstarted lock waiter without spending its retry budget."""
        now = _utcnow()
        changed = self.db.execute(update(IndexJob).where(
            IndexJob.id == job_id, IndexJob.status == "running", IndexJob.lease_owner == owner,
            IndexJob.lease_expires_at > now, IndexJob.cancel_requested.is_(False),
        ).values(status="pending", attempt_count=IndexJob.attempt_count - 1, started_at=None,
                 visible_at=now + timedelta(seconds=delay_seconds), lease_owner=None,
                 lease_expires_at=None, heartbeat_at=None).execution_options(synchronize_session=False))
        self.db.commit()
        if changed.rowcount:
            job = self.get(job_id)
            self.db.refresh(job)
            self.add_event(job, "lock_wait_deferred", message="知识库繁忙，任务已延后且未消耗重试次数")
        return changed.rowcount == 1

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
        owned_lease = self.db.info.get("index_job_lease")
        if owned_lease and owned_lease[0] == job.id:
            values = {key: value for key, value in {
                "status": status, "total_items": total_items, "processed_items": processed_items,
                "succeeded_items": succeeded_items, "failed_items": failed_items,
                "error_message": error_message,
            }.items() if value is not None}
            terminal = status in {"completed", "partial", "failed", "cancelled"}
            if terminal:
                values.update(finished_at=_utcnow(), lease_owner=None, lease_expires_at=None, heartbeat_at=None)
            changed = self.db.execute(update(IndexJob).where(
                IndexJob.id == job.id, IndexJob.status == "running",
                IndexJob.lease_owner == owned_lease[1], IndexJob.lease_expires_at > _utcnow(),
            ).values(**values).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                self.db.rollback()
                raise JobLeaseLostError("任务租约已丢失，拒绝更新旧任务")
            self.db.info["index_job_finishing"] = terminal
            try:
                self.db.commit()
            finally:
                self.db.info.pop("index_job_finishing", None)
            if terminal:
                self.db.info.pop("index_job_lease", None)
            self.db.refresh(job)
            self.add_event(job, "progress" if status is None else "status", message=error_message)
            return job
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

    def leased_cancel_requested(self, job_id: str, owner: str) -> bool:
        statement = self._scope(select(IndexJob.cancel_requested).where(
            IndexJob.id == job_id, IndexJob.status == "running",
            IndexJob.lease_owner == owner, IndexJob.lease_expires_at > _utcnow(),
        ))
        cancelled = self.db.scalar(statement)
        if cancelled is None:
            raise JobLeaseLostError("任务执行租约已过期或已由其他worker接管")
        return bool(cancelled)

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
        # The recovery decision and its write share a row lock. A heartbeat
        # arriving before the lock changes the predicate; one arriving later
        # cannot resurrect the expired attempt.
        if self.db.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        stale = list(self.db.scalars(stmt.execution_options(populate_existing=True)))
        recovered = []
        for job in stale:
            expected_owner = job.lease_owner
            expected_expiry = job.lease_expires_at
            if job.cancel_requested:
                values = {"status": "cancelled", "finished_at": now}
            elif job.attempt_count < job.max_attempts:
                values = {"status": "pending", "started_at": None}
            else:
                values = {"status": "failed", "error_message": "Job lease expired after maximum attempts", "finished_at": now}
            values.update(lease_owner=None, lease_expires_at=None, heartbeat_at=None)
            changed = self.db.execute(update(IndexJob).where(
                IndexJob.id == job.id, IndexJob.status == "running", IndexJob.lease_owner == expected_owner,
                IndexJob.lease_expires_at == expected_expiry,
                or_(IndexJob.lease_expires_at.is_(None), IndexJob.lease_expires_at <= now),
            ).values(**values).execution_options(synchronize_session=False))
            if changed.rowcount == 1:
                recovered.append(job)
        if recovered:
            self.db.commit()
            for job in recovered:
                self.db.refresh(job)
                self.add_event(job, "lease_recovered")
        else:
            self.db.rollback()
        return len(recovered)

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
        if event_type == "status" and job.status in {"completed", "partial", "failed", "cancelled"}:
            from app.services.observability import observe
            observe("task_" + job.status)
            if job.started_at and job.finished_at:
                observe("task_execution_seconds", max(0, (job.finished_at - job.started_at).total_seconds()))
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
