"""Real services only with explicit test URLs; isolated schema and Redis prefix."""

import os
import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


@pytest.fixture
def redis_service(monkeypatch):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for real Redis contract tests")
    import redis
    from app.core import config
    client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    client.ping()
    prefix = "test:coord:" + uuid.uuid4().hex
    monkeypatch.setattr(config, "REDIS_URL", url)
    monkeypatch.setattr(config, "REDIS_KEY_PREFIX", prefix)
    yield client, prefix, url
    keys = list(client.scan_iter(match=prefix + ":*"))
    if keys:
        client.delete(*keys)
    client.close()


@pytest.fixture
def postgres_service(monkeypatch):
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for real PostgreSQL contracts")
    from app import models
    from app.core import database
    schema = "coord_" + uuid.uuid4().hex
    administrator = create_engine(url)
    with administrator.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"}, pool_size=12, max_overflow=12)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    yield engine, url, schema
    engine.dispose()
    with administrator.begin() as db:
        db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    administrator.dispose()


def _seed_jobs(engine, count=1, *, distinct_bases=False):
    from app.models.knowledge_base import KnowledgeBase
    from app.models.index_job import IndexJob
    with Session(engine) as db:
        db.add(KnowledgeBase(id="coord-kb", slug="coord", name="original"))
        if distinct_bases:
            db.add_all(KnowledgeBase(id=f"coord-kb-{number}", slug=f"coord-{number}", name="original") for number in range(count))
        db.flush()
        jobs = [IndexJob(knowledge_base_id=f"coord-kb-{number}" if distinct_bases else "coord-kb", job_type="file", status="pending", max_attempts=2) for number in range(count)]
        db.add_all(jobs)
        db.commit()
        return [job.id for job in jobs]


def test_real_redis_cache_wakeup_and_heartbeat(redis_service):
    from app.adapters.redis import RedisCache, RedisTaskQueue, active_workers, publish_worker_heartbeat
    client, prefix, _ = redis_service
    first, second = RedisCache(client=client, prefix=prefix), RedisCache(client=client, prefix=prefix)
    first.set("shared", b"value", ttl_seconds=30)
    assert second.get("shared") == b"value"
    second.delete("shared")
    assert first.get("shared") is None
    producer, consumer = RedisTaskQueue(client=client, prefix=prefix), RedisTaskQueue(client=client, prefix=prefix)
    producer.enqueue("persisted-job-id")
    assert consumer.wait(.1)
    assert not producer.wait(.05)
    publish_worker_heartbeat("w1", {"worker_id": "w1", "stopping": False}, 20)
    assert active_workers() == [{"worker_id": "w1", "stopping": False}]
    client.delete(prefix + ":worker:w1")
    assert active_workers() == []


def test_real_redis_lease_renewal_expiry_and_stale_owner(redis_service):
    from app.adapters.redis import RedisSemaphoreLease, CoordinationUnavailableError
    client, prefix, _ = redis_service
    owner = RedisSemaphoreLease("capacity", 1, .3, client=client, prefix=prefix)
    assert owner.acquire(0)
    time.sleep(.45)  # Longer than the original lease: renewal must hold the slot.
    assert not RedisSemaphoreLease("capacity", 1, .3, client=client, prefix=prefix).acquire(0)
    owner._stop.set()
    owner._thread.join(timeout=2)  # Simulate a crashed owner, without releasing.
    time.sleep(.4)
    successor = RedisSemaphoreLease("capacity", 1, 2, client=client, prefix=prefix)
    assert successor.acquire(0)
    with pytest.raises(CoordinationUnavailableError):
        owner.close()
    assert not RedisSemaphoreLease("capacity", 1, 2, client=client, prefix=prefix).acquire(0)
    successor.close()
    assert client.zcard(successor.key) == 0


def _child_lease(url, prefix, queue):
    from unittest.mock import patch
    with patch("dotenv.load_dotenv", return_value=False):
        import redis
        from app.adapters.redis import RedisSemaphoreLease
        client = redis.Redis.from_url(url)
        lease = RedisSemaphoreLease("process-capacity", 1, 2, client=client, prefix=prefix)
        acquired = lease.acquire(.1)
        queue.put(acquired)
        if acquired:
            lease.close()
        client.close()


def test_real_redis_capacity_is_global_across_processes(redis_service):
    from app.adapters.redis import RedisSemaphoreLease
    client, prefix, url = redis_service
    owner = RedisSemaphoreLease("process-capacity", 1, 2, client=client, prefix=prefix)
    assert owner.acquire(0)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    try:
        child = ctx.Process(target=_child_lease, args=(url, prefix, queue))
        child.start(); child.join(timeout=15)
        assert child.exitcode == 0 and queue.get(timeout=2) is False
    finally:
        owner.close()
    child = ctx.Process(target=_child_lease, args=(url, prefix, queue))
    child.start(); child.join(timeout=15)
    assert child.exitcode == 0 and queue.get(timeout=2) is True
    queue.close()


def test_real_shared_login_limits_and_explicit_redis_failure(redis_service, monkeypatch):
    from app.core import config
    from app.services import login_throttle
    from app.api.errors import APIError
    from app.adapters.redis import RedisCache, CoordinationUnavailableError
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(login_throttle, "AUTH_LOGIN_MAX_ATTEMPTS", 2)
    login_throttle.check_login_attempt("SharedAccount", "address-a")
    login_throttle.check_login_attempt("sharedaccount", "address-b")
    with pytest.raises(APIError) as caught:
        login_throttle.check_login_attempt("SHAREDACCOUNT", "address-c")
    assert caught.value.status_code == 429
    login_throttle.clear_account_failures("sharedaccount", "address-a")
    login_throttle.check_login_attempt("sharedaccount", "address-c")
    class Broken:
        def get(self, key):
            raise RuntimeError("synthetic-credential-must-not-leak")
    with pytest.raises(CoordinationUnavailableError) as failure:
        RedisCache(client=Broken()).get("x")
    assert "synthetic-credential" not in str(failure.value)


def test_real_account_and_ip_login_budgets_are_independent(redis_service, monkeypatch):
    from app.core import config
    from app.services import login_throttle
    from app.api.errors import APIError
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    monkeypatch.setattr(config, "AUTH_LOGIN_IP_MAX_ATTEMPTS", 40, raising=False)
    monkeypatch.setattr(login_throttle, "AUTH_LOGIN_MAX_ATTEMPTS", 2)
    for number in range(40):
        login_throttle.check_login_attempt(f"member-{number}", "shared-team-ip")
    with pytest.raises(APIError):
        login_throttle.check_login_attempt("member-41", "shared-team-ip")
    login_throttle.check_login_attempt("sensitive-account", "other-address-a")
    login_throttle.check_login_attempt("sensitive-account", "other-address-b")
    with pytest.raises(APIError):
        login_throttle.check_login_attempt("sensitive-account", "other-address-c")


def test_real_postgres_skip_locked_claims_each_job_once(postgres_service, monkeypatch):
    from app.core import config
    # This contract exercises atomic ownership with capacity deliberately above
    # the batch size; bounded resource scheduling has a separate contract.
    monkeypatch.setattr(config, "JOB_PARSE_CONCURRENCY", 24, raising=False)
    from app.repositories.index_job_repository import IndexJobRepository
    engine, _, _ = postgres_service
    identifiers = _seed_jobs(engine, 24, distinct_bases=True)
    def claim(number):
        with Session(engine) as db:
            job = IndexJobRepository(db, None).claim_next_pending(worker_id=f"worker-{number}")
            return job.id if job else None
    with ThreadPoolExecutor(12) as executor:
        claimed = list(executor.map(claim, range(24)))
    assert set(claimed) == set(identifiers) and len(claimed) == len(set(claimed))
    assert claim(25) is None


def test_real_postgres_recovery_fences_expired_owner_and_business_commit(postgres_service):
    from app.models.index_job import IndexJob
    from app.models.knowledge_base import KnowledgeBase
    from app.repositories.index_job_repository import IndexJobRepository, JobLeaseLostError, _utcnow
    engine, _, _ = postgres_service
    identifier = _seed_jobs(engine)[0]
    with Session(engine) as db:
        repository = IndexJobRepository(db, None)
        job = repository.claim(identifier, worker_id="old-attempt", lease_seconds=30)
        job.lease_expires_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        assert not repository.heartbeat(identifier, worker_id="old-attempt", lease_seconds=30)
        assert repository.recover_expired_leases() == 1
        successor = repository.claim(identifier, worker_id="new-attempt", lease_seconds=30)
        assert successor.attempt_count == 2
        with pytest.raises(JobLeaseLostError):
            repository.leased_cancel_requested(identifier, "old-attempt")
    with Session(engine) as stale:
        stale.info["index_job_lease"] = (identifier, "old-attempt")
        stale.get(KnowledgeBase, "coord-kb").name = "stale-write"
        with pytest.raises(JobLeaseLostError):
            stale.commit()
        stale.rollback()
        with pytest.raises(JobLeaseLostError):
            IndexJobRepository(stale, None).update(stale.get(IndexJob, identifier), status="completed")
    with Session(engine) as db:
        assert db.get(KnowledgeBase, "coord-kb").name == "original"
        db.info["index_job_lease"] = (identifier, "new-attempt")
        IndexJobRepository(db, None).update(db.get(IndexJob, identifier), status="completed")
        assert "index_job_lease" not in db.info


def test_real_busy_knowledge_base_is_not_claimed_and_deferral_keeps_retry_budget(postgres_service):
    from app.models.index_job import IndexJob
    from app.repositories.index_job_repository import IndexJobRepository, _utcnow
    engine, _, _ = postgres_service
    identifiers = _seed_jobs(engine, 2)
    with Session(engine) as db:
        repository = IndexJobRepository(db, None)
        first = repository.claim_next_pending(worker_id="owner-a")
        assert first.id in identifiers
        assert repository.claim_next_pending(worker_id="owner-b") is None
        assert repository.defer_lock_waiter(first.id, "owner-a")
        assert db.get(IndexJob, first.id).attempt_count == 0
        # Another due document from the same KB can now run; the deferred one
        # remains gated rather than spinning or spending its attempt budget.
        second = repository.claim_next_pending(worker_id="owner-b")
        assert second.id != first.id
        db.info["index_job_lease"] = (second.id, "owner-b")
        repository.update(second, status="completed")
        assert repository.claim_next_pending(worker_id="owner-c") is None
        first.visible_at = _utcnow() - timedelta(seconds=1)
        db.commit()
        again = repository.claim_next_pending(worker_id="owner-c")
        assert again.id == first.id and again.attempt_count == 1


def _child_lock(url, schema, queue):
    # Child processes must not read the real .env when importing application code.
    from unittest.mock import patch
    with patch("dotenv.load_dotenv", return_value=False):
        from app.core import database
        from app.services.knowledge_mutation import knowledge_mutation_lock
        from app.adapters.redis import CoordinationUnavailableError
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
        database.engine = engine
        try:
            with knowledge_mutation_lock("coord-kb", timeout_seconds=.3):
                queue.put("acquired")
        except CoordinationUnavailableError:
            queue.put("blocked")
        finally:
            engine.dispose()


def test_real_postgres_lock_crosses_processes_is_reentrant_and_allows_heartbeat(postgres_service):
    from app.services.knowledge_mutation import knowledge_mutation_lock
    from app.repositories.index_job_repository import IndexJobRepository
    engine, url, schema = postgres_service
    identifier = _seed_jobs(engine)[0]
    with Session(engine) as db:
        IndexJobRepository(db, None).claim(identifier, worker_id="heartbeat-owner", lease_seconds=30)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with knowledge_mutation_lock("coord-kb"):
        with knowledge_mutation_lock("coord-kb"):
            child = ctx.Process(target=_child_lock, args=(url, schema, queue))
            child.start(); child.join(timeout=15)
            assert child.exitcode == 0 and queue.get(timeout=2) == "blocked"
            def heartbeat():
                with Session(engine) as db:
                    return IndexJobRepository(db, None).heartbeat(identifier, worker_id="heartbeat-owner", lease_seconds=30)
            with ThreadPoolExecutor(1) as executor:
                assert executor.submit(heartbeat).result(timeout=2)
                def cancel():
                    with Session(engine) as db:
                        IndexJobRepository(db, None).request_cancel(identifier)
                        return IndexJobRepository(db, None).leased_cancel_requested(identifier, "heartbeat-owner")
                assert executor.submit(cancel).result(timeout=2)
    child = ctx.Process(target=_child_lock, args=(url, schema, queue))
    child.start(); child.join(timeout=15)
    assert child.exitcode == 0 and queue.get(timeout=2) == "acquired"
    queue.close()


def test_real_postgres_lock_cancellation_and_connection_loss_refuse_commit(postgres_service):
    from app.services.knowledge_mutation import knowledge_mutation_lock, _held_locks
    from app.agents.contracts import AgentCancelledError
    from app.adapters.redis import CoordinationUnavailableError
    from app.models.knowledge_base import KnowledgeBase
    engine, _, _ = postgres_service
    _seed_jobs(engine)
    with pytest.raises(AgentCancelledError):
        with knowledge_mutation_lock("coord-kb", cancelled=lambda: True):
            pytest.fail("Cancelled lock wait must not enter its body")
    with pytest.raises(CoordinationUnavailableError):
        with knowledge_mutation_lock("coord-kb"):
            with Session(engine) as db:
                db.get(KnowledgeBase, "coord-kb").name = "must-not-commit"
                _held_locks()["coord-kb"][0].invalidate()
                db.commit()
    with Session(engine) as db:
        assert db.get(KnowledgeBase, "coord-kb").name == "original"


def test_real_worker_polls_db_and_honors_configured_parallelism(postgres_service, redis_service, monkeypatch):
    from app.core import config
    from app.services import index_worker
    from app.adapters.redis import RedisTaskQueue
    from app.models.index_job import IndexJob
    from app.repositories.index_job_repository import IndexJobRepository
    engine, _, _ = postgres_service
    client, prefix, _ = redis_service
    _seed_jobs(engine, 2, distinct_bases=True)
    @contextmanager
    def metadata_session():
        with Session(engine) as db:
            yield db
    container = SimpleNamespace(metadata=SimpleNamespace(session=metadata_session), task_queue=RedisTaskQueue(client=client, prefix=prefix))
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: container)
    monkeypatch.setattr(index_worker, "INDEX_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(index_worker, "_worker_stop", threading.Event())
    monkeypatch.setattr(config, "TASK_QUEUE_PROVIDER", "redis")
    started, release = threading.Barrier(3), threading.Event()
    owners, failures = [], []
    def process(identifier, *, worker_id, already_claimed):
        owners.append(worker_id)
        started.wait(timeout=10)
        assert release.wait(10)
        with Session(engine) as db:
            db.info["index_job_lease"] = (identifier, worker_id)
            IndexJobRepository(db, None).update(db.get(IndexJob, identifier), status="completed")
        return True
    monkeypatch.setattr(index_worker, "_process_job", process)
    def run():
        try:
            index_worker.run_worker_forever()
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        started.wait(timeout=10)
        assert len(set(owners)) == 2
        assert index_worker.get_worker_status()["alive"]
    finally:
        index_worker._worker_stop.set()
        release.set()
        worker.join(timeout=15)
    assert not worker.is_alive() and not failures
    assert not index_worker.get_worker_status()["alive"]


def test_server_never_embeds_worker_or_falls_back_to_local_coordination(monkeypatch):
    from app.core import config
    from app.services import index_worker
    from app.services.knowledge_mutation import knowledge_mutation_lock
    from app.adapters.redis import CoordinationUnavailableError
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    monkeypatch.setattr(config, "APP_ROLE", "api")
    monkeypatch.setattr(index_worker, "INDEX_WORKER_ENABLED", True)
    monkeypatch.setattr(index_worker, "_worker_thread", None)
    index_worker.start_worker()
    assert index_worker._worker_thread is None
    with pytest.raises(CoordinationUnavailableError):
        with knowledge_mutation_lock("server-requires-pg"):
            pytest.fail("Server must not use a local lock")
    monkeypatch.setattr(config, "CACHE_PROVIDER", "memory")
    from app.services.rag_concurrency import rag_concurrency
    from app.services.login_throttle import check_login_attempt
    with pytest.raises(CoordinationUnavailableError):
        with rag_concurrency():
            pytest.fail("Server must not use a per-process semaphore")
    with pytest.raises(CoordinationUnavailableError):
        check_login_attempt("account", "address")


def test_real_external_worker_and_scheduler_heartbeats_are_required(redis_service, monkeypatch):
    from app.core import config
    from app.adapters.redis import publish_worker_heartbeat
    from app.services.readiness_service import _check_index_worker, _check_scheduler
    client, prefix, _ = redis_service
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    monkeypatch.setattr(config, "TASK_QUEUE_PROVIDER", "redis")
    assert _check_index_worker()["status"] == "fail"
    assert _check_scheduler()["status"] == "fail"
    publish_worker_heartbeat("external", {"worker_id": "external", "stopping": False}, 20)
    client.set(prefix + ":scheduler:heartbeat", "alive", ex=20)
    assert _check_index_worker()["status"] == "ok"
    assert _check_scheduler()["status"] == "ok"
    client.delete(prefix + ":worker:external", prefix + ":scheduler:heartbeat")
    assert _check_index_worker()["status"] == "fail"
    assert _check_scheduler()["status"] == "fail"


def test_worker_entry_initializes_without_embedded_services_and_owns_runtime(monkeypatch):
    from scripts import run_worker
    from app.services import index_worker
    from app.agents import runtime
    calls = []
    monkeypatch.setenv("APP_ROLE", "api")
    monkeypatch.setattr(run_worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr("app.core.logger.setup_logging", lambda: None)
    monkeypatch.setattr("app.core.init_db.init_app", lambda **kwargs: calls.append(("initialize", kwargs)))
    monkeypatch.setattr(runtime, "start_managed_codecode", lambda: calls.append("start-runtime") or {"status": "ok"})
    monkeypatch.setattr(runtime, "stop_managed_codecode", lambda: calls.append("stop-runtime"))
    monkeypatch.setattr(index_worker, "run_worker_forever", lambda: calls.append("run-worker"))
    monkeypatch.setattr(index_worker, "stop_worker", lambda: calls.append("stop-worker"))
    assert run_worker.main([]) == 0
    assert os.environ["APP_ROLE"] == "worker"
    assert calls == [("initialize", {"start_services": False}), "start-runtime", "run-worker", "stop-worker", "stop-runtime"]


def test_worker_help_does_not_initialize_services(monkeypatch):
    from scripts import run_worker
    monkeypatch.setattr("app.core.init_db.init_app", lambda **kwargs: pytest.fail("Help must not touch the database"))
    with pytest.raises(SystemExit) as result:
        run_worker.main(["--help"])
    assert result.value.code == 0
