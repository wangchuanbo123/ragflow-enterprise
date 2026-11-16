from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME, JWT_COOKIE_NAME


def login(client, username=INITIAL_ADMIN_USERNAME, password=INITIAL_ADMIN_PASSWORD):
    client.cookies.clear()
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def create(client, username="review-user", password="Testing-password-123", **extra):
    return client.post("/api/v1/users", json={"username": username, "password": password, **extra})


def test_admin_can_manage_accounts_and_members_cannot(client):
    assert client.get("/api/v1/users").status_code == 401
    login(client)
    response = create(client)
    assert response.status_code == 201
    user_id = response.json()["id"]
    assert "password" not in str(response.json())
    assert create(client).status_code == 400
    assert any(u["id"] == user_id for u in client.get("/api/v1/users").json())
    login(client, "review-user", "Testing-password-123")
    assert client.get("/api/v1/users").status_code == 403
    assert create(client, "unauthorized").status_code == 403


def test_last_admin_cannot_be_disabled_or_demoted(client):
    login(client)
    admin_id = client.get("/api/v1/auth/me").json()["id"]
    for payload in ({"is_active": False}, {"role": "user"}):
        assert client.patch(f"/api/v1/users/{admin_id}", json=payload).status_code == 400
    assert client.get("/api/v1/auth/me").status_code == 200


def test_logout_revokes_copied_cookie(client):
    login(client)
    copied = client.cookies.get(JWT_COOKIE_NAME)
    assert client.post("/api/v1/auth/logout").status_code == 200
    client.cookies.set(JWT_COOKIE_NAME, copied)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_change_password_revokes_all_sessions(client):
    login(client)
    old_token = client.cookies.get(JWT_COOKIE_NAME)
    response = client.post("/api/v1/auth/password", json={
        "current_password": INITIAL_ADMIN_PASSWORD, "new_password": "Changed-password-123",
    })
    assert response.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.set(JWT_COOKIE_NAME, old_token)
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.clear()
    assert login(client, password="Changed-password-123").status_code == 200
    assert login(client).status_code == 400


def test_admin_reset_and_disable_revoke_sessions(client):
    login(client)
    user_id = create(client).json()["id"]
    admin_token = client.cookies.get(JWT_COOKIE_NAME)
    login(client, "review-user", "Testing-password-123")
    user_token = client.cookies.get(JWT_COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(JWT_COOKIE_NAME, admin_token)
    assert client.patch(f"/api/v1/users/{user_id}", json={"password": "Reset-password-456"}).status_code == 200
    client.cookies.clear()
    client.cookies.set(JWT_COOKIE_NAME, user_token)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert login(client, "review-user", "Reset-password-456").status_code == 200
    user_token = client.cookies.get(JWT_COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(JWT_COOKIE_NAME, admin_token)
    assert client.patch(f"/api/v1/users/{user_id}", json={"is_active": False}).status_code == 200
    client.cookies.clear()
    client.cookies.set(JWT_COOKIE_NAME, user_token)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_password_policy_and_login_rate_limit(client, monkeypatch):
    login(client)
    assert create(client, password="alllettersbutnodigits").status_code == 400
    assert create(client, password="中" * 30 + "abc123").status_code == 400
    from app.services import login_throttle

    monkeypatch.setattr(login_throttle, "AUTH_LOGIN_MAX_ATTEMPTS", 2)
    for _ in range(2):
        assert login(client, "nonexistent", "wrong").status_code == 400
    assert login(client, "nonexistent", "wrong").status_code == 429
    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: 10**15)
    assert login(client, "nonexistent", "wrong").status_code == 400


def test_password_reset_during_verification_prevents_old_password_session(client, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User
    from app.services import auth_service
    from sqlalchemy import select

    original = auth_service.verify_password
    reset = []

    def verify_then_reset(password, password_hash):
        valid = original(password, password_hash)
        if not reset:
            reset.append(True)
            with SessionLocal() as db:
                user = db.scalar(select(User).where(User.username == INITIAL_ADMIN_USERNAME))
                user.password_hash = hash_password("Admin-reset-password-456")
                auth_service.revoke_user_sessions(db, user.id)
                db.commit()
        return valid

    monkeypatch.setattr(auth_service, "verify_password", verify_then_reset)
    assert login(client).status_code == 401
    assert login(client, password="Admin-reset-password-456").status_code == 200


def test_password_reset_during_self_change_cannot_be_overwritten(client, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User
    from app.services import auth_service
    from sqlalchemy import select

    login(client)
    original = auth_service.verify_password
    reset = []

    def verify_then_reset(password, password_hash):
        valid = original(password, password_hash)
        if not reset:
            reset.append(True)
            with SessionLocal() as db:
                user = db.scalar(select(User).where(User.username == INITIAL_ADMIN_USERNAME))
                user.password_hash = hash_password("Admin-reset-password-456")
                auth_service.revoke_user_sessions(db, user.id)
                db.commit()
        return valid

    monkeypatch.setattr(auth_service, "verify_password", verify_then_reset)
    response = client.post("/api/v1/auth/password", json={
        "current_password": INITIAL_ADMIN_PASSWORD, "new_password": "Should-not-be-saved-789",
    })
    assert response.status_code == 401
    assert login(client, password="Admin-reset-password-456").status_code == 200
