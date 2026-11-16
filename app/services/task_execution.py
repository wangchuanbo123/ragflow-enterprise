"""Task execution context and factual stage events, independent of job handlers."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_stage_callback = ContextVar("job_stage_callback", default=None)
_current_stage = ContextVar("current_job_stage", default="")


@contextmanager
def stage_recorder(callback):
    token = _stage_callback.set(callback)
    try:
        yield
    finally:
        _stage_callback.reset(token)


@contextmanager
def job_stage(stage, *, document_id=None):
    """Record only stages the executing code actually enters; never infer progress."""
    callback = _stage_callback.get()
    payload = {"stage": stage, "status": "running"}
    if document_id:
        payload["document_id"] = document_id
    started = time.monotonic()
    token = _current_stage.set(stage)
    if callback:
        callback(payload)
    try:
        yield
    except BaseException:
        if callback:
            callback({**payload, "status": "failed", "duration_seconds": time.monotonic() - started})
        raise
    else:
        if callback:
            callback({**payload, "status": "completed", "duration_seconds": time.monotonic() - started})
    finally:
        _current_stage.reset(token)


def persist_stage(db, repository, job, payload):
    # A failed SQL transaction must first be rolled back by its owner. Emitting
    # telemetry must not commit partial writes from an exception path.
    if payload["status"] == "failed":
        db.rollback()
    if not db.is_active:
        return
    repository.add_event(job, "stage", payload=payload)
    if "duration_seconds" in payload:
        from app.services.observability import observe
        observe("task_stage_" + payload["stage"] + "_seconds", payload["duration_seconds"])
