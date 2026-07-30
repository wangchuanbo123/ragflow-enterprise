"""会话 CRUD 与用户隔离测试。"""

from app.core.security import hash_password
from app.models.user import User


def _login(client, username, password):
    client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _create_second_user():
    import app.core.database as db_module

    with db_module.SessionLocal() as db:
        user = User(
            username="alice",
            password_hash=hash_password("alice-pass"),
            display_name="Alice",
            role="user",
            is_active=True,
        )
        db.add(user)
        db.commit()
        return user.id


def test_conversation_crud(client):
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)

    create = client.post("/api/v1/conversations", json={"title": "会话一"})
    assert create.status_code == 201
    cid = create.json()["id"]
    assert create.json()["title"] == "会话一"

    listed = client.get("/api/v1/conversations")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    detail = client.get(f"/api/v1/conversations/{cid}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == []

    renamed = client.patch(f"/api/v1/conversations/{cid}", json={"title": "新名字"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新名字"

    deleted = client.delete(f"/api/v1/conversations/{cid}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/conversations/{cid}").status_code == 404


def test_user_isolation(client):
    """用户 A 不能访问用户 B 的会话。"""
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    _create_second_user()

    # admin 创建会话
    _login(client, INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    admin_conv = client.post("/api/v1/conversations", json={"title": "admin 的会话"})
    cid = admin_conv.json()["id"]

    # alice 登录后看不到 admin 的会话
    _login(client, "alice", "alice-pass")
    listed = client.get("/api/v1/conversations")
    assert all(c["id"] != cid for c in listed.json())

    assert client.get(f"/api/v1/conversations/{cid}").status_code == 404
    assert client.patch(f"/api/v1/conversations/{cid}", json={"title": "hack"}).status_code == 404
    assert client.delete(f"/api/v1/conversations/{cid}").status_code == 404
