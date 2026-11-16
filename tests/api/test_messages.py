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
    assert body["assistant_message"]["route"]["selected_channels"] == ["text"]

    # 刷新后历史仍存在
    detail = client.get(f"/api/v1/conversations/{cid}").json()
    assert len(detail["messages"]) == 2


def test_answer_feedback_is_saved_and_returned(client, monkeypatch):
    runtime = make_fake_runtime(answer="可评价回答")
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda: runtime, raising=True
    )
    _login(client)
    cid = _create_conversation(client)
    answer = client.post(
        f"/api/v1/conversations/{cid}/messages", json={"content": "评价测试"}
    ).json()["assistant_message"]

    saved = client.put(
        f"/api/v1/conversations/{cid}/messages/{answer['id']}/feedback",
        json={"rating": "helpful", "comment": "来源清晰"},
    )

    assert saved.status_code == 200
    assert saved.json()["rating"] == "helpful"
    detail = client.get(f"/api/v1/conversations/{cid}").json()
    assistant = next(item for item in detail["messages"] if item["role"] == "assistant")
    assert assistant["feedback"] == "helpful"


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
    assert "route" in event_names
    assert "delta" in event_names
    assert "sources" in event_names
    assert "done" in event_names

    # 真实流式：delta 内容按块逐个发送
    deltas = [e["data"]["content"] for e in events if e["event"] == "delta"]
    assert deltas == ["A", "B", "C"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["status"] == "completed"
    source_event = next(e for e in events if e["event"] == "sources")
    assert source_event["data"]["images"] == []
    assert source_event["data"]["code_snippets"] == []

    # 完整内容已持久化
    detail = client.get(f"/api/v1/conversations/{cid}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "ABC"
    assert assistant["status"] == "completed"
    assert assistant["route"]["selected_channels"] == ["text"]


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


def test_incompatible_index_stream_reports_rebuild_action(client, monkeypatch):
    from rag.errors import IndexCompatibilityError
    def incompatible(*args, **kwargs):
        raise IndexCompatibilityError("请在文档管理中执行重建全库索引")
    monkeypatch.setattr("app.services.rag_service.active_runtime", incompatible)
    _login(client)
    cid = _create_conversation(client)
    with client.stream("POST", f"/api/v1/conversations/{cid}/messages/stream", json={"content": "test"}) as response:
        payload = response.read().decode("utf-8")
    assert "INDEX_REBUILD_REQUIRED" in payload
    assert "重建全库索引" in payload
    from app.core.database import SessionLocal
    from app.models.message import Message
    with SessionLocal() as db:
        message = db.query(Message).filter_by(conversation_id=cid, role="assistant").one()
        assert message.status == "failed"
        assert "重建全库索引" in message.error_message


def test_image_and_code_artifacts_survive_message_history(client, monkeypatch):
    from langchain_core.documents import Document

    runtime = make_fake_runtime(answer="请查看相关图片和原始代码。")
    runtime.retriever._docs = [Document(
        page_content=(
            "恢复流程如下。\n[[img-1]]\n"
            "```python\ndef restart_service():\n    return 'ok'\n```"
        ),
        metadata={
            "source": "恢复手册.md",
            "document_id": "doc-media",
            "chunk_id": "chunk-media",
            "content_hash": "media-hash",
            "contains_code": True,
            "images": [{
                "marker": "img-1",
                "url": "/api/v1/objects/doc-media/images/img-1.png",
                "caption": "恢复流程图",
                "mime_type": "image/png",
                "filename": "img-1.png",
            }],
        },
    )]
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda: runtime, raising=True
    )
    _login(client)
    cid = _create_conversation(client)

    response = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"content": "给我恢复流程图片和代码"},
    )

    assert response.status_code == 200
    answer = response.json()["assistant_message"]
    assert answer["images"][0]["caption"] == "恢复流程图"
    assert answer["images"][0]["document_id"] == "doc-media"
    assert answer["code_snippets"][0]["content"] == "def restart_service():\n    return 'ok'"
    assert answer["code_snippets"][0]["is_verbatim"] is True
    assert answer["sources"][0]["images"][0]["marker"] == "img-1"

    history = client.get(f"/api/v1/conversations/{cid}").json()
    historical = next(item for item in history["messages"] if item["role"] == "assistant")
    assert historical["images"] == answer["images"]
    assert historical["code_snippets"] == answer["code_snippets"]
    assert historical["sources"][0]["images"] == answer["sources"][0]["images"]


def test_all_knowledge_bases_are_retrieved_and_globally_combined(client, monkeypatch):
    runtime = make_fake_runtime(answer="跨库答案")
    monkeypatch.setattr(
        "app.services.rag_service.active_runtime", lambda *args: runtime, raising=True
    )
    _login(client)
    knowledge_bases = client.get("/api/v1/knowledge-bases").json()
    created = client.post(
        "/api/v1/knowledge-bases", json={"name": "第二知识库"}
    ).json()
    knowledge_base_ids = [knowledge_bases[0]["id"], created["id"]]
    cid = _create_conversation(client)

    response = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={
            "content": "跨库查询",
            "retrieval": {
                "strategy": "standard",
                "scope": {"knowledge_base_ids": knowledge_base_ids},
            },
        },
    )

    assert response.status_code == 200
    answer = response.json()["assistant_message"]
    assert answer["content"] == "跨库答案"
    assert answer["route"]["method"] == "multi-knowledge-base"
    assert answer["route"]["scope"]["knowledge_base_ids"] == knowledge_base_ids
    assert {item["id"] for item in answer["route"]["knowledge_bases"]} == set(knowledge_base_ids)
    assert {source["knowledge_base_id"] for source in answer["sources"]} == set(knowledge_base_ids)
    assert {source["knowledge_base_name"] for source in answer["sources"]} == {
        knowledge_bases[0]["name"],
        "第二知识库",
    }


def test_cross_base_scope_rejects_inaccessible_knowledge_base(client):
    import app.core.database as db_module
    from app.core.security import hash_password
    from app.models.knowledge_base import KnowledgeBaseMember
    from app.models.user import User

    _login(client)
    default_knowledge_base = client.get("/api/v1/knowledge-bases").json()[0]
    private_knowledge_base = client.post(
        "/api/v1/knowledge-bases", json={"name": "私有知识库"}
    ).json()
    with db_module.SessionLocal() as db:
        user = User(
            username="scope-reader",
            password_hash=hash_password("scope-reader-pass"),
            display_name="Scope Reader",
            role="user",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(KnowledgeBaseMember(
            knowledge_base_id=default_knowledge_base["id"],
            user_id=user.id,
            role="viewer",
        ))
        db.commit()

    client.post(
        "/api/v1/auth/login",
        json={"username": "scope-reader", "password": "scope-reader-pass"},
    )
    cid = _create_conversation(client)
    response = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={
            "content": "尝试越权",
            "retrieval": {
                "scope": {"knowledge_base_ids": [private_knowledge_base["id"]]},
            },
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
