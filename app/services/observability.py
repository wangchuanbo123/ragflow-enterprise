"""Bounded operational metrics and safe cross-process log context.

Redis keeps shared counters. Local/test mode uses a process-local registry.
Metric failures are isolated from business correctness; coordination is not.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
import math
import re
import threading
import time

_context = ContextVar("operation_context", default={})
_local = {}
_lock = threading.Lock()
_ALLOWED = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_BUCKETS = (.1, .5, 1, 5, 15, 30, 60, 300)
_pending = {}
_flush_lock = threading.Lock()
_stop = threading.Event()
_thread = None
_next_attempt = 0.0
_failures = 0
_last_warning = 0.0
_MAX_FIELDS = 2048


def _metric_client():
    # Separate pool: telemetry must never occupy coordination connections or
    # inherit their retry/socket budget.
    from app.core import config
    return _cached_metric_client(config.REDIS_URL)


@lru_cache(maxsize=4)
def _cached_metric_client(url):
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    return redis.Redis.from_url(url, socket_connect_timeout=.2, socket_timeout=.2,
                               retry=Retry(NoBackoff(), 0), max_connections=2,
                               retry_on_timeout=False, decode_responses=False)


def start_metrics():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_metric_loop, name="operational-metrics", daemon=True)
        _thread.start()


def _metric_loop():
    while not _stop.wait(1):
        flush_metrics()


def stop_metrics():
    _stop.set()
    if _thread:
        _thread.join(timeout=.5)
    flush_metrics()


def flush_metrics():
    """Bounded background I/O. A failed batch remains pending with backoff."""
    global _next_attempt, _failures, _last_warning
    from app.core import config
    if config.CACHE_PROVIDER != "redis" or time.monotonic() < _next_attempt:
        return False
    if not _flush_lock.acquire(blocking=False):
        return False
    batch = {}
    try:
        with _lock:
            batch.update(_pending)
            _pending.clear()
        if not batch:
            return True
        from app.adapters.redis import key_prefix
        pipe = _metric_client().pipeline(transaction=True)
        for day, fields in batch.items():
            key = key_prefix() + ":metrics:v1:" + day
            for field, increment in fields.items():
                pipe.hincrbyfloat(key, field, increment)
            pipe.expire(key, 8 * 86400)
        pipe.execute()
        _failures, _next_attempt = 0, 0.0
        return True
    except Exception:
        with _lock:
            for day, fields in batch.items():
                target = _pending.setdefault(day, {})
                for field, value in fields.items():
                    if field in target or len(target) < _MAX_FIELDS:
                        target[field] = target.get(field, 0) + value
        _failures = min(_failures + 1, 6)
        _next_attempt = time.monotonic() + min(60, 2 ** _failures)
        if time.monotonic() - _last_warning >= 60:
            logging.getLogger(__name__).warning("Shared metrics unavailable; retaining bounded local batch")
            _last_warning = time.monotonic()
        return False
    finally:
        _flush_lock.release()


def context_values():
    return dict(_context.get())


@contextmanager
def operation_context(**values):
    token = _context.set({**_context.get(), **{key: str(value)[:128] for key, value in values.items()
                                            if key in {"request_id", "job_id", "knowledge_base_id", "stage"} and value}})
    try:
        yield
    finally:
        _context.reset(token)


def observe(name, value=1):
    if not _ALLOWED.fullmatch(name) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid bounded operational metric")
    increments = {name + ":count": 1, name + ":sum": float(value)}
    if name.endswith("_seconds"):
        increments.update({name + ":le:" + str(bucket): 1 for bucket in _BUCKETS if value <= bucket})
    from app.core import config
    with _lock:
        target = _local
        if config.CACHE_PROVIDER == "redis":
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            target = _pending.setdefault(day, {})
            for expired in sorted(_pending)[:-7]:
                _pending.pop(expired, None)
        for field, increment in increments.items():
            if field in target or len(target) < _MAX_FIELDS:
                target[field] = target.get(field, 0) + increment


def metric_snapshot():
    from app.core import config
    source, raw = "process", {}
    if config.CACHE_PROVIDER == "redis":
        try:
            from app.adapters.redis import key_prefix
            from datetime import timedelta
            client = _metric_client()
            pipe = client.pipeline(transaction=False)
            now = datetime.now(timezone.utc)
            for offset in range(7):
                pipe.hgetall(key_prefix() + ":metrics:v1:" + (now - timedelta(days=offset)).strftime("%Y%m%d"))
            for bucket in pipe.execute():
                for field, value in bucket.items():
                    key = field.decode() if isinstance(field, bytes) else field
                    raw[key] = raw.get(key, 0) + float(value)
            source = "shared_redis"
        except Exception:
            source = "process_fallback"
    with _lock:
        for key, value in _local.items():
            raw[key] = raw.get(key, 0) + value
        for fields in _pending.values():
            for key, value in fields.items():
                raw[key] = raw.get(key, 0) + value
    names = sorted({key.split(":")[0] for key in raw})
    metrics = {}
    for name in names:
        count, total = raw.get(name + ":count", 0), raw.get(name + ":sum", 0)
        metrics[name] = {"observations": count, "total": total, "mean": total / count if count else 0}
        if name.endswith("_seconds"):
            metrics[name]["buckets_le"] = {str(bucket): raw.get(name + ":le:" + str(bucket), 0) for bucket in _BUCKETS}
    return {"source": source, "retention_days": 7, "metrics": metrics}


def operations_snapshot(db):
    import shutil
    import psutil
    from sqlalchemy import func, select
    from app.core import config, database
    from app.models.index_job import IndexJob
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = db.execute(select(IndexJob.status, func.count()).group_by(IndexJob.status)).all()
    oldest = db.scalar(select(func.min(IndexJob.created_at)).where(IndexJob.status == "pending"))
    queued_seconds = max(0, (now - oldest).total_seconds()) if oldest else 0
    usage = shutil.disk_usage(config.DATA_DIR)
    memory = psutil.virtual_memory()
    memory_percent, memory_scope = memory.percent, "host"
    # Linux container limits are smaller than psutil's host memory view.
    from pathlib import Path
    try:
        limit_raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        if limit_raw != "max" and int(limit_raw) > 0:
            memory_percent, memory_scope = 100 * used / int(limit_raw), "container"
    except (OSError, ValueError):
        pass
    pool = database.engine.pool
    pool_stats = {name: getattr(pool, name)() for name in ("size", "checkedin", "checkedout", "overflow") if callable(getattr(pool, name, None))}
    if "overflow" in pool_stats:
        # QueuePool reports a negative value before the base pool is filled;
        # the operations view reports the number of extra live connections.
        pool_stats["overflow"] = max(0, pool_stats["overflow"])
    warnings = []
    if queued_seconds > getattr(config, "OPS_QUEUE_WARN_SECONDS", 300):
        warnings.append({"code": "QUEUE_AGE", "message": "后台任务等待时间超过阈值", "seconds": queued_seconds})
    if usage.free / usage.total < getattr(config, "OPS_DISK_FREE_WARN_RATIO", .1):
        warnings.append({"code": "DISK_LOW", "message": "数据目录所在磁盘剩余空间不足"})
    if memory_percent > getattr(config, "OPS_MEMORY_WARN_PERCENT", 90):
        warnings.append({"code": "MEMORY_HIGH", "message": "服务器内存使用超过阈值"})
    from datetime import timedelta
    failures = db.scalar(select(func.count()).select_from(IndexJob).where(
        IndexJob.status.in_(["failed", "partial"]), IndexJob.finished_at >= now - timedelta(minutes=15),
    )) or 0
    if failures:
        warnings.append({"code": "RECENT_TASK_FAILURES", "message": "最近十五分钟存在失败或部分完成的任务", "count": failures})
    status_path = Path(getattr(config, "SERVER_BACKUP_STATUS_PATH", "/app/operations/backup-status.json"))
    backup = {"status": "not_recorded"}
    try:
        if status_path.stat().st_size <= 65536:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Invalid backup status")
            backup = {key: val for key, val in value.items() if key in {
                "status", "last_success_at", "last_attempt_at", "last_error", "mirror_status",
                "rpo_target_hours", "rto_target_minutes", "rto_verified", "mirror_error",
                "maintenance_seconds", "backup_seconds"}}
        last = datetime.fromisoformat(backup["last_success_at"])
        if (datetime.now(timezone.utc) - last).total_seconds() > float(backup.get("rpo_target_hours", 24)) * 3600:
            warnings.append({"code": "BACKUP_STALE", "message": "完整备份已超过恢复点目标"})
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append({"code": "BACKUP_NOT_RECORDED", "message": "尚无可核对的完整服务器备份调度记录"})
    if backup.get("status") == "failed":
        warnings.append({"code": "BACKUP_FAILED", "message": "最近一次完整备份或镜像复制失败，请检查备份任务"})
    if backup.get("mirror_status") != "verified":
        warnings.append({"code": "BACKUP_MIRROR_UNVERIFIED", "message": "异机备份副本尚未配置或未通过校验"})
    maintenance = {"status": "unknown"}
    try:
        maintenance_path = status_path.parent / "maintenance.json"
        if maintenance_path.stat().st_size <= 65536:
            value = json.loads(maintenance_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                maintenance = {key: val for key, val in value.items() if key in {
                    "status", "updated_at", "maintenance_seconds", "backup_seconds", "restore_seconds"}}
        if maintenance.get("status") == "recovery_required":
            warnings.append({"code": "RECOVERY_REQUIRED", "message": "恢复未完成，需核对保护备份及服务状态"})
    except (OSError, ValueError):
        pass
    return {"checked_at": datetime.now(timezone.utc).isoformat(), **metric_snapshot(),
            "tasks": {status: count for status, count in rows}, "oldest_pending_seconds": queued_seconds,
            "database_pool": pool_stats, "process": {"rss_bytes": psutil.Process().memory_info().rss},
            "disk": {"total_bytes": usage.total, "free_bytes": usage.free},
            "memory": {"percent": memory_percent, "scope": memory_scope}, "backup": backup,
            "maintenance": maintenance, "alerts": warnings}


def install_database_metrics(engine):
    """Install once per engine; store durations, never SQL or bound values."""
    from sqlalchemy import event
    if getattr(engine, "_operation_metrics_installed", False):
        return
    engine._operation_metrics_installed = True
    @event.listens_for(engine, "before_cursor_execute")
    def before(connection, cursor, statement, parameters, context, executemany):
        context._operation_started = time.monotonic()
    @event.listens_for(engine, "after_cursor_execute")
    def after(connection, cursor, statement, parameters, context, executemany):
        elapsed = time.monotonic() - context._operation_started
        observe("database_query_seconds", elapsed)
        if elapsed > 1:
            observe("database_slow_queries")
    @event.listens_for(engine, "handle_error")
    def failed(context):
        observe("database_query_errors")


from sqlalchemy.pool import QueuePool

class ObservedQueuePool(QueuePool):
    def _do_get(self):
        started = time.monotonic()
        try:
            return super()._do_get()
        finally:
            observe("database_pool_wait_seconds", time.monotonic() - started)


_alert_signature, _alert_at = None, 0.0

def emit_operation_alerts():
    """Scheduler emits changed alerts immediately and repeats at most every 15m."""
    global _alert_signature, _alert_at
    from app.core import database
    with database.SessionLocal() as db:
        snapshot = operations_snapshot(db)
    alerts = snapshot["alerts"]
    signature = tuple(sorted(item["code"] for item in alerts))
    now = time.monotonic()
    if signature != _alert_signature or now - _alert_at >= 900:
        for alert in alerts:
            logging.getLogger(__name__).warning("Operational alert %s: %s", alert["code"], alert["message"])
        _alert_signature, _alert_at = signature, now
    return alerts
