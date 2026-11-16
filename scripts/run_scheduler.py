"""Run team maintenance outside API processes; one renewable shared leader."""

from __future__ import annotations

import logging
import signal
import threading
import time


def main() -> int:
    from app.core import config
    from app.core.init_db import init_app
    from app.core.logger import setup_logging
    from app.adapters.redis import RedisSemaphoreLease, get_redis_client, key_prefix, redis_call
    from app.services.connector_scheduler import enqueue_due_sources
    from app.services.graph_maintenance import enqueue_stale_community_rebuilds
    from app.services.backup_scheduler import run_monthly_backups
    from app.services.stream_lifecycle import recover_abandoned_streams
    from app.services.observability import emit_operation_alerts, start_metrics, stop_metrics
    from app.core import database
    if config.APP_PROFILE != "server" or config.APP_ROLE != "scheduler":
        raise ValueError("Scheduler requires APP_PROFILE=server and APP_ROLE=scheduler")
    setup_logging()
    start_metrics()
    init_app(start_services=False)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    due = {}
    logger = logging.getLogger(__name__)
    tasks = [
        ("connectors", config.CONNECTOR_SCHEDULER_INTERVAL_SECONDS,
         lambda: enqueue_due_sources() if config.CONNECTOR_SCHEDULER_ENABLED else None),
        ("communities", config.GRAPH_COMMUNITY_MAINTENANCE_INTERVAL_SECONDS, enqueue_stale_community_rebuilds),
        ("backups", config.BACKUP_SCHEDULER_INTERVAL_SECONDS, run_monthly_backups),
        ("operations", 60, emit_operation_alerts),
    ]
    lease = None
    heartbeat_stop = threading.Event()
    heartbeat_failed = threading.Event()
    heartbeat_thread = None

    def heartbeat():
        while not heartbeat_stop.is_set():
            try:
                lease.assert_held()
                redis_call(get_redis_client().set, key_prefix() + ":scheduler:heartbeat", lease.token, ex=20)
            except Exception:
                heartbeat_failed.set()
                return
            heartbeat_stop.wait(5)

    try:
        while not stop.is_set():
            if lease is None:
                candidate = RedisSemaphoreLease("scheduler", 1, 60)
                if not candidate.acquire(0):
                    stop.wait(5)
                    continue
                lease = candidate
                heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name="scheduler-heartbeat")
                heartbeat_thread.start()
            if heartbeat_failed.is_set():
                raise RuntimeError("Scheduler shared heartbeat was lost")
            lease.assert_held()
            for name, interval, action in tasks:
                if time.monotonic() < due.get(name, 0):
                    continue
                lease.assert_held()
                try:
                    action()
                except Exception as exc:
                    logger.error("Scheduled %s failed (%s)", name, type(exc).__name__)
                due[name] = time.monotonic() + interval
            if time.monotonic() >= due.get("streams", 0):
                lease.assert_held()
                with database.SessionLocal() as db:
                    recover_abandoned_streams(db)
                due["streams"] = time.monotonic() + 30
            stop.wait(5)
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=3)
        if lease is not None:
            lease.close()
        stop_metrics()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
