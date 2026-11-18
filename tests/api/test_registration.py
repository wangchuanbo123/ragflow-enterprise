"""Public applications remain unusable until an administrator approves them."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME, JWT_COOKIE_NAME

PASSWORD = "Registered-password-123"


@pytest.fixture(autouse=True)
def isolate_registration_rate_limit(monkeypatch):
    from app.services import registration_throttle

    monkeypatch.setattr(registration_throttle, "check_registration_attempt", lambda *_: None)


def login(client, username=INITIAL_ADMIN_USERNAME, password=INITIAL_ADMIN_PASSWORD):
    client.cookies.clear()
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def apply(client, username="new-user", **changes):
    return client.post("/api/v1/auth/register", json={
        "username": username, "password": PASSWORD, "display_name": "新用户", **changes,
    })


def user_id(username="new-user"):
    from app.core.database import SessionLocal
    from app.models.user import User

    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.username == username))


def review(client, target_id, decision="approved", **extra):
    return client.post(f"/api/v1/users/{target_id}/registration-review", json={"decision": decision, **extra})


def test_registration_creates_only_an_inactive_pending_user_without_a_session(client):
    from app.core.database import SessionLocal
    from app.core.security import verify_password
    from app.models.auth_session import AuthSession
    from app.models.user import User

    response = apply(client)
    assert response.status_code == 202
    assert response.json()["registration_status"] == "pending"
    assert "管理员审核" in response.json()["message"]
    assert "set-cookie" not in response.headers
    assert JWT_COOKIE_NAME not in client.cookies
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "new-user"))
        assert user.role == "user" and not user.is_active
        assert user.registration_status == "pending"
        assert user.password_hash != PASSWORD and verify_password(PASSWORD, user.password_hash)
        assert db.scalar(select(AuthSession.id).where(AuthSession.user_id == user.id)) is None
    denied = login(client, "new-user", PASSWORD)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "REGISTRATION_PENDING"
    assert client.get("/api/v1/auth/me").status_code == 401


def test_admin_approval_records_audit_and_enables_login(client):
    assert apply(client).status_code == 202
    target = user_id()
    assert login(client).status_code == 200
    admin_id = client.get("/api/v1/auth/me").json()["id"]
    pending = next(user for user in client.get("/api/v1/users").json() if user["id"] == target)
    assert pending["registration_status"] == "pending"
    assert pending["created_at"]
    assert pending["registration_reviewed_at"] is None

    approved = review(client, target, note=" 已核实申请人 ")
    assert approved.status_code == 200
    body = approved.json()
    assert body["registration_status"] == "approved" and body["is_active"]
    assert body["role"] == "user"
    assert body["registration_reviewed_by"] == admin_id
    assert body["registration_reviewed_at"]
    assert body["registration_review_note"] == "已核实申请人"
    assert "password" not in str(body).lower()
    assert login(client, "new-user", PASSWORD).status_code == 200
    assert client.get("/api/v1/auth/me").json()["registration_status"] == "approved"


def test_rejection_is_not_activation_and_cannot_be_overwritten(client):
    apply(client)
    target = user_id()
    login(client)
    rejected = review(client, target, "rejected", note="缺少申请依据")
    assert rejected.status_code == 200
    assert not rejected.json()["is_active"]
    assert rejected.json()["registration_status"] == "rejected"
    assert review(client, target).status_code == 409
    assert client.patch(f"/api/v1/users/{target}", json={"is_active": True}).status_code == 400
    denied = login(client, "new-user", PASSWORD)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "REGISTRATION_REJECTED"


def test_registration_review_requires_an_administrator(client):
    apply(client)
    target = user_id()
    assert review(client, target).status_code == 401
    login(client)
    created = client.post("/api/v1/users", json={"username": "ordinary", "password": PASSWORD})
    assert created.status_code == 201 and created.json()["registration_status"] == "approved"
    assert login(client, "ordinary", PASSWORD).status_code == 200
    assert review(client, target).status_code == 403


@pytest.mark.parametrize("extra", [
    {"role": "admin"}, {"is_active": True}, {"registration_status": "approved"},
    {"registration_reviewed_by": "attacker"},
])
def test_registration_rejects_privileged_fields(client, extra):
    assert apply(client, **extra).status_code == 422
    assert user_id() is None


@pytest.mark.parametrize("changes,status", [
    ({"username": "bad name"}, 422), ({"password": "too-short"}, 422),
    ({"password": "letterswithoutnumbers"}, 400),
    ({"password": "中" * 30 + "abc123"}, 400),
])
def test_registration_uses_existing_username_and_password_rules(client, changes, status):
    assert apply(client, **changes).status_code == status
    assert user_id() is None


def test_duplicate_applications_do_not_replace_the_password_or_review_state(client):
    assert apply(client).status_code == 202
    duplicate = apply(client, password="Another-password-456")
    assert duplicate.status_code == 400
    assert "用户名已存在" in duplicate.json()["error"]["message"]
    login(client)
    review(client, user_id())
    assert login(client, "new-user", PASSWORD).status_code == 200
    assert login(client, "new-user", "Another-password-456").status_code == 400


def test_administrator_update_cannot_bypass_pending_review(client):
    apply(client)
    target = user_id()
    login(client)
    for values in ({"is_active": True}, {"role": "admin"}):
        assert client.patch(f"/api/v1/users/{target}", json=values).status_code == 400
    assert review(client, target).status_code == 200
    assert review(client, target, "rejected").status_code == 409
    assert review(client, "does-not-exist").status_code == 404


def test_review_rejects_invalid_decisions_and_overlong_notes(client):
    apply(client)
    login(client)
    target = user_id()
    assert review(client, target, "pending").status_code == 422
    assert review(client, target, note="x" * 501).status_code == 422
    assert review(client, target, is_active=True).status_code == 422


def test_stale_second_reviewer_cannot_override_a_completed_decision(client):
    from app.core.database import SessionLocal
    from app.models.user import User
    from app.schemas.auth import RegistrationReviewRequest
    from app.services.user_service import review_registration
    from app.api.errors import APIError

    apply(client)
    target = user_id()
    with SessionLocal() as first, SessionLocal() as second:
        stale = second.get(User, target)
        assert stale.registration_status == "pending"
        review_registration(first, target, RegistrationReviewRequest(decision="approved"), "reviewer-one")
        with pytest.raises(APIError) as error:
            review_registration(second, target, RegistrationReviewRequest(decision="rejected"), "reviewer-two")
        assert error.value.status_code == 409
        second.refresh(stale)
        assert stale.registration_status == "approved"
        assert stale.registration_reviewed_by == "reviewer-one"


def test_pending_user_cannot_use_a_token_even_if_active_flag_is_set(client):
    from app.core.database import SessionLocal
    from app.core.security import create_access_token, decode_access_token
    from app.models.auth_session import AuthSession
    from app.models.user import User

    apply(client)
    target = user_id()
    token = create_access_token(target, {"username": "new-user", "role": "user"})
    claims = decode_access_token(token)
    with SessionLocal() as db:
        db.get(User, target).is_active = True
        db.add(AuthSession(id=claims["jti"], user_id=target, expires_at=datetime.utcnow() + timedelta(hours=1)))
        db.commit()
    client.cookies.set(JWT_COOKIE_NAME, token)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert login(client, "new-user", PASSWORD).json()["error"]["code"] == "REGISTRATION_PENDING"


def test_duplicate_registration_commit_race_is_a_clean_client_error(client, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from app.api.errors import APIError
    from app.core.database import SessionLocal
    from app.schemas.auth import RegistrationRequest
    from app.services.user_service import register_user

    with SessionLocal() as db:
        def duplicate_commit():
            raise IntegrityError("INSERT INTO users", {}, Exception("unique username"))
        monkeypatch.setattr(db, "commit", duplicate_commit)
        with pytest.raises(APIError) as error:
            register_user(db, RegistrationRequest(username="racing-user", password=PASSWORD))
        assert error.value.status_code == 400
        assert "用户名已存在" in error.value.message
        assert not db.new


def test_existing_active_and_disabled_accounts_keep_their_states_after_migration(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///" + (tmp_path / "registration-migration.sqlite").as_posix())
    config = Config("alembic.ini")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "j2b3c4d5e6f7")
        for target, enabled in (("old-active", True), ("old-disabled", False)):
            connection.execute(text("INSERT INTO users (id, username, password_hash, display_name, role, is_active) VALUES (:id, :id, 'hash', :id, 'user', :active)"), {"id": target, "active": enabled})
        connection.commit()
        command.upgrade(config, "head")
        rows = list(connection.execute(text("SELECT id, is_active, registration_status, registration_reviewed_at, registration_review_note FROM users ORDER BY id")).mappings())
        assert [(row["id"], bool(row["is_active"]), row["registration_status"]) for row in rows] == [("old-active", True, "approved"), ("old-disabled", False, "approved")]
        assert all(row["registration_reviewed_at"] is None and row["registration_review_note"] == "" for row in rows)
        connection.commit()
        command.downgrade(config, "j2b3c4d5e6f7")
        assert connection.scalar(text("SELECT count(*) FROM users")) == 2
        connection.commit()
        command.upgrade(config, "head")
        command.check(config)
    engine.dispose()
