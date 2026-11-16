"""Telemetry and readiness must not grow the latency of business work."""
import threading
import time
from types import SimpleNamespace


def test_observe_never_performs_redis_io(monkeypatch):
    from app.core import config
    from app.services import observability as metrics
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(metrics, "_pending", {})
    monkeypatch.setattr(metrics, "_metric_client", lambda: (_ for _ in ()).throw(AssertionError("network on hot path")))
    for _ in range(1000):
        metrics.observe("test_request_seconds", .2)
    assert next(iter(metrics._pending.values()))["test_request_seconds:count"] == 1000


def test_slow_metric_flush_does_not_hold_observation_lock(monkeypatch):
    from app.core import config
    from app.services import observability as metrics
    entered, release = threading.Event(), threading.Event()
    class Pipe:
        def hincrbyfloat(self, *args): pass
        def expire(self, *args): pass
        def execute(self):
            entered.set()
            release.wait(2)
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(metrics, "_pending", {})
    monkeypatch.setattr(metrics, "_next_attempt", 0)
    monkeypatch.setattr(metrics, "_metric_client", lambda: SimpleNamespace(pipeline=lambda **kw: Pipe()))
    metrics.observe("slow_network")
    worker = threading.Thread(target=metrics.flush_metrics)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        metrics.observe("foreground_during_flush")
        assert time.monotonic() - started < .1
    finally:
        release.set()
        worker.join(2)
    assert next(iter(metrics._pending.values()))["foreground_during_flush:count"] == 1


def test_failed_flush_retains_batch_and_opens_backoff(monkeypatch):
    from app.core import config
    from app.services import observability as metrics
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(metrics, "_pending", {})
    monkeypatch.setattr(metrics, "_next_attempt", 0)
    monkeypatch.setattr(metrics, "_failures", 0)
    calls = []
    def unavailable():
        calls.append(True)
        raise OSError("private connection details")
    monkeypatch.setattr(metrics, "_metric_client", unavailable)
    metrics.observe("failed_flush")
    assert metrics.flush_metrics() is False
    assert metrics.flush_metrics() is False
    assert len(calls) == 1
    assert next(iter(metrics._pending.values()))["failed_flush:count"] == 1


def test_readiness_vector_probe_does_not_audit(monkeypatch):
    from app.services.readiness_service import _check_vector_connectivity
    class Provider:
        def probe(self):
            return SimpleNamespace(as_dict=lambda: {"status": "ok", "verification": "connectivity"})
        def health(self, *args, **kwargs):
            raise AssertionError("full collection health must not run")
    assert _check_vector_connectivity(Provider())["status"] == "ok"
