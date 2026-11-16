"""Real PostgreSQL snapshot / Redis quota contracts with a deterministic compiler."""

import threading
import time

import pytest
from sqlalchemy.orm import Session

from tests.test_distributed_coordination import postgres_service, redis_service
from tests.test_graph_filters import seed, Backend, plan, condition
from app.schemas.graph_filter import GraphFilterPreviewRequest
from app.services.graph_filter_service import GraphFilterService, GraphFilterError


def test_real_postgres_full_graph_and_effective_source_evidence(postgres_service):
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db, extra=240)
        result = GraphFilterService(db, "filter-kb", "reader").preview(GraphFilterPreviewRequest(principles=[], max_nodes=5))
        assert result.stats.total_nodes == 246 and result.stats.matched_nodes == 246 and result.stats.truncated
        backend = Backend(plan([{"conditions": [condition("edge", "source_document", values=["历史手册.txt"])]}]))
        filtered = GraphFilterService(db, "filter-kb", "reader", backend=backend).preview(GraphFilterPreviewRequest(principles=["历史手册中的关系"]))
        assert {node.id for node in filtered.nodes} == {"a", "b"}
        assert {proof.document_id for proof in filtered.edges[0].evidence} == {"doc-a", "doc-b"}


def test_real_redis_global_quota_survives_timed_out_noncooperative_backend(postgres_service, redis_service, monkeypatch):
    from app.core import config
    engine, _, _ = postgres_service
    client, prefix, _ = redis_service
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    monkeypatch.setattr(config, "GRAPH_FILTER_MAX_CONCURRENCY", 1, raising=False)
    with Session(engine) as db:
        seed(db)
    entered, finish = threading.Event(), threading.Event()
    backend = Backend(plan([{"hide_isolated": True}]), callback=lambda: (entered.set(), finish.wait(5)))
    try:
        with Session(engine) as db, pytest.raises(GraphFilterError) as timed_out:
            GraphFilterService(db, "filter-kb", "reader", backend=backend, timeout_seconds=1).preview(GraphFilterPreviewRequest(principles=["隐藏孤点"]))
        assert entered.is_set() and timed_out.value.status == 504
        assert client.zcard(prefix + ":lease:graph-filter-global") == 1
        assert client.zcard(prefix + ":lease:graph-filter:reader") == 1
        with Session(engine) as db, pytest.raises(GraphFilterError) as other_user:
            GraphFilterService(db, "filter-kb", "reader2").preview(GraphFilterPreviewRequest(principles=[]))
        assert other_user.value.status == 429
        assert client.zcard(prefix + ":lease:graph-filter:reader2") == 0
        with Session(engine) as db, pytest.raises(GraphFilterError) as same_user:
            GraphFilterService(db, "filter-kb", "reader").preview(GraphFilterPreviewRequest(principles=[]))
        assert same_user.value.status == 429
    finally:
        finish.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and client.zcard(prefix + ":lease:graph-filter-global"):
        time.sleep(.02)
    assert client.zcard(prefix + ":lease:graph-filter-global") == 0
    assert client.zcard(prefix + ":lease:graph-filter:reader") == 0
    with Session(engine) as db:
        assert GraphFilterService(db, "filter-kb", "reader2").preview(GraphFilterPreviewRequest(principles=[])).stats.total_nodes == 6
    assert list(client.scan_iter(match=prefix + ":lease:graph-filter*")) == []


def test_resource_budget_is_loaded_from_configuration(postgres_service, monkeypatch):
    from app.core import config
    engine, _, _ = postgres_service
    with Session(engine) as db:
        seed(db)
    monkeypatch.setattr(config, "GRAPH_FILTER_MAX_RECORDS", 2, raising=False)
    with Session(engine) as db, pytest.raises(GraphFilterError) as error:
        GraphFilterService(db, "filter-kb", "reader").preview(GraphFilterPreviewRequest(principles=[]))
    assert error.value.code == "GRAPH_FILTER_BUDGET_OR_DATA"


def test_redis_lease_failure_keeps_both_slots_until_ignored_call_finishes(postgres_service, redis_service, monkeypatch):
    from app.adapters.redis import RedisSemaphoreLease, CoordinationUnavailableError
    from app.core import config
    engine, _, _ = postgres_service
    client, prefix, _ = redis_service
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    monkeypatch.setattr(config, "GRAPH_FILTER_MAX_CONCURRENCY", 1)
    with Session(engine) as db:
        seed(db)
    entered, finish = threading.Event(), threading.Event()
    original = RedisSemaphoreLease.assert_held
    def unavailable(lease):
        if entered.is_set() and lease.key.endswith(":graph-filter-global"):
            raise CoordinationUnavailableError("synthetic lease check outage")
        return original(lease)
    monkeypatch.setattr(RedisSemaphoreLease, "assert_held", unavailable)
    backend = Backend(plan([{"hide_isolated": True}]), callback=lambda: (entered.set(), finish.wait(5)))
    try:
        with Session(engine) as db, pytest.raises(CoordinationUnavailableError):
            GraphFilterService(db, "filter-kb", "reader", backend=backend).preview(GraphFilterPreviewRequest(principles=["隐藏孤点"]))
        assert entered.is_set()
        assert client.zcard(prefix + ":lease:graph-filter-global") == 1
        assert client.zcard(prefix + ":lease:graph-filter:reader") == 1
        with Session(engine) as db, pytest.raises(GraphFilterError) as busy:
            GraphFilterService(db, "filter-kb", "reader2").preview(GraphFilterPreviewRequest(principles=[]))
        assert busy.value.status == 429
    finally:
        finish.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and list(client.scan_iter(match=prefix + ":lease:graph-filter*")):
        time.sleep(.02)
    assert list(client.scan_iter(match=prefix + ":lease:graph-filter*")) == []
