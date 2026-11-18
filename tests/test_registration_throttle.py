from types import SimpleNamespace

import pytest

from app.api.errors import APIError
from app.core import config
from app.services import registration_throttle as throttle


@pytest.fixture(autouse=True)
def local_limits(monkeypatch):
    monkeypatch.setattr(config, "CACHE_PROVIDER", "memory")
    monkeypatch.setattr(config, "APP_PROFILE", "test")
    monkeypatch.setattr(throttle, "AUTH_REGISTRATION_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(throttle, "AUTH_REGISTRATION_IP_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(throttle, "AUTH_REGISTRATION_WINDOW_SECONDS", 60)


def test_limits_accounts_and_ip_independently(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(throttle, "time", SimpleNamespace(monotonic=lambda: now[0]))
    throttle.check_registration_attempt("alice", "ip-a")
    throttle.check_registration_attempt("ALICE", "ip-b")
    with pytest.raises(APIError, match="注册申请过于频繁") as caught:
        throttle.check_registration_attempt("alice", "ip-c")
    assert caught.value.status_code == 429
    assert caught.value.code == "REGISTRATION_RATE_LIMITED"
    throttle.check_registration_attempt("bob", "ip-a")
    throttle.check_registration_attempt("carol", "ip-a")
    with pytest.raises(APIError):
        throttle.check_registration_attempt("dave", "ip-a")
    now[0] += 61
    throttle.check_registration_attempt("alice", "ip-a")


def test_registration_limits_do_not_block_login():
    from app.services.login_throttle import check_login_attempt

    throttle.check_registration_attempt("alice", "ip-a")
    throttle.check_registration_attempt("alice", "ip-a")
    with pytest.raises(APIError):
        throttle.check_registration_attempt("alice", "ip-a")
    check_login_attempt("alice", "ip-a")


def test_public_registration_endpoint_applies_limits_before_creating_accounts(client, monkeypatch):
    from sqlalchemy import select
    from app.core.database import SessionLocal
    from app.models.user import User

    monkeypatch.setattr(throttle, "AUTH_REGISTRATION_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(throttle, "AUTH_REGISTRATION_IP_MAX_ATTEMPTS", 2)
    payload = {"username": "alice", "password": "Registered-pass-123"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 202
    assert client.post("/api/v1/auth/register", json=payload).status_code == 429
    assert client.post("/api/v1/auth/register", json={**payload, "username": "bob"}).status_code == 202
    denied = client.post("/api/v1/auth/register", json={**payload, "username": "carol"})
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "REGISTRATION_RATE_LIMITED"
    with SessionLocal() as db:
        assert db.scalar(select(User.id).where(User.username == "carol")) is None


def test_rejected_new_usernames_do_not_grow_local_storage():
    for name in ("alice", "bob", "carol"):
        throttle.check_registration_attempt(name, "ip-a")
    size = len(throttle._buckets)
    for index in range(50):
        with pytest.raises(APIError):
            throttle.check_registration_attempt(f"blocked-{index}", "ip-a")
    assert len(throttle._buckets) == size


def test_server_uses_shared_limits_and_propagates_denial(monkeypatch):
    from app.adapters import redis

    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    calls = []
    monkeypatch.setattr(redis, "registration_attempt_allowed", lambda *args: calls.append(args) or False)
    with pytest.raises(APIError) as caught:
        throttle.check_registration_attempt("alice", "ip-a")
    assert caught.value.status_code == 429
    assert calls[0][1:] == (2, 60, 3)
    assert "alice" not in calls[0][0][0]


def test_server_without_shared_storage_refuses_registration(monkeypatch):
    from app.adapters.redis import CoordinationUnavailableError

    monkeypatch.setattr(config, "APP_PROFILE", "server")
    with pytest.raises(CoordinationUnavailableError):
        throttle.check_registration_attempt("alice", "ip-a")
