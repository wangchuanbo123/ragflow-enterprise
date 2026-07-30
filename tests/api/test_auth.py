"""鉴权接口测试。"""

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME


def test_login_success(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == INITIAL_ADMIN_USERNAME
    assert "password" not in str(body).lower()


def test_login_wrong_password(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": "wrong-pass"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_login_unknown_user(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "x"},
    )
    assert r.status_code == 400


def test_me_requires_auth(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_me_after_login(client):
    client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD},
    )
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == INITIAL_ADMIN_USERNAME


def test_logout_clears_session(client):
    client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD},
    )
    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_unauthenticated_conversations_blocked(client):
    assert client.get("/api/v1/conversations").status_code == 401
    assert client.post("/api/v1/conversations", json={"title": "x"}).status_code == 401


def test_legacy_ask_requires_login(client):
    response = client.post("/ask", json={"query": "test"})
    assert response.status_code == 401
