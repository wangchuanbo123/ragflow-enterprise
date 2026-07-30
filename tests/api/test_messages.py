"""消息同步与流式接口测试（使用 Fake LLM，不依赖真实模型）。"""

import json

from app.services.rag_service import set_runtime_override
from tests.conftest import make_fake_runtime


def _login(client):
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    client.post(
        "/api/v1/auth/login",
        json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD},
    )


def _create_conversation(client):
    r = client.post("/api/v1/conversations", json={"title": "聊天"})
    return r.json()["id"]


def test_sync_message(client, monkeypatch):
    runtime = make_fake_runtime(answer="完整答案。", chunks=["完", "整", "答案。"])
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda: runtime, raising=True
    )

    _login(client)
    cid = _create_conversation(client)

    r = client.post(f"/api/v1/conversations/{cid}/messages", json={"content": "你好"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_message"]["content"] == "你好"
    assert body["assistant_message"]["content"] == "完整答案。"
    assert body["assistant_message"]["status"] == "completed"
    assert body["assistant_message"]["sources"][0]["source"] == "primary.txt"

    # 刷新后历史仍存在
    detail = client.get(f"/api/v1/conversations/{cid}").json()
    assert len(detail["messages"]) == 2


def test_stream_message_real_streaming(client, monkeypatch):
    runtime = make_fake_runtime(answer="完整答案。", chunks=["A", "B", "C"])
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda: runtime, raising=True
    )

    _login(client)
    cid = _create_conversation(client)

    with client.stream(
        "POST", f"/api/v1/conversations/{cid}/messages/stream", json={"content": "你好"}
    ) as resp:
        assert resp.status_code == 200
        text = resp.read().decode("utf-8")

    # 解析 SSE 事件
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[len("event: "):]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[len("data: "):])
        if ev:
            events.append(ev)

    event_names = [e["event"] for e in events]
    assert "message" in event_names
    assert "delta" in event_names
    assert "sources" in event_names
    assert "done" in event_names

    # 真实流式：delta 内容按块逐个发送
    deltas = [e["data"]["content"] for e in events if e["event"] == "delta"]
    assert deltas == ["A", "B", "C"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["status"] == "completed"

    # 完整内容已持久化
    detail = client.get(f"/api/v1/conversations/{cid}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "ABC"
    assert assistant["status"] == "completed"


def test_stream_marks_failed_on_error(client, monkeypatch):
    class BoomLLM:
        def get_model(self):
            return self

        def generate(self, prompt):
            return "x"

        def stream(self, prompt):
            raise RuntimeError("模型爆炸")
            yield  # noqa: E701  让它成为生成器

    from rag.runtime.runtime import RAGRuntime

    runtime = RAGRuntime(llm=BoomLLM(), retriever=__import__(
        "tests.conftest", fromlist=["FakeRetriever"]
    ).FakeRetriever(), reranker=__import__(
        "tests.conftest", fromlist=["FakeReranker"]
    ).FakeReranker())
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda: runtime, raising=True
    )

    _login(client)
    cid = _create_conversation(client)

    with client.stream(
        "POST", f"/api/v1/conversations/{cid}/messages/stream", json={"content": "你好"}
    ) as resp:
        text = resp.read().decode("utf-8")

    assert "event: error" in text
    assert "RAG_GENERATION_FAILED" in text

    # 数据库中助手消息状态为 failed
    from app.core.database import SessionLocal
    from app.models.message import Message

    with SessionLocal() as db:
        msgs = db.query(Message).filter_by(conversation_id=cid, role="assistant").all()
        assert msgs and msgs[0].status == "failed"
