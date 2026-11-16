import pytest
from sqlalchemy import select

from app.api.errors import APIError
from app.models.message import Message
from app.models.user import User
from app.repositories.conversation_repository import ConversationRepository
from app.services import chat_service, rag_service
from tests.conftest import make_fake_runtime


def test_stream_validates_before_returning_iterator(client):
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        user = db.scalar(select(User))
        with pytest.raises(APIError) as caught:
            chat_service.answer_stream(db, "missing-conversation", user.id, "hello")
        assert caught.value.status_code == 404


@pytest.mark.parametrize("consume_delta", [False, True])
def test_cancelled_stream_persists_partial_answer_and_closes_model(client, monkeypatch, consume_delta):
    from app.core.database import SessionLocal

    closed = []
    runtime = make_fake_runtime(chunks=["partial", "rest"])

    def upstream(*args, **kwargs):
        try:
            yield {"event": "delta", "data": {"content": "partial"}}
            yield {"event": "delta", "data": {"content": "rest"}}
        finally:
            closed.append(True)

    monkeypatch.setattr(rag_service, "active_runtime", lambda: runtime)
    monkeypatch.setattr(rag_service, "stream", upstream)
    with SessionLocal() as db:
        user = db.scalar(select(User))
        conversation = ConversationRepository(db).create(user.id, "cancel test")
        events = chat_service.answer_stream(db, conversation.id, user.id, "hello")
        first = next(events)
        if consume_delta:
            assert next(events)["event"] == "delta"
        events.close()
        row = db.get(Message, first["data"]["message_id"])
        assert row.status == "interrupted"
        assert row.content == ("partial" if consume_delta else "")
        assert bool(closed) == consume_delta


def test_restart_recovers_streaming_messages(client):
    from app.core.database import SessionLocal
    from app.core.init_db import recover_interrupted_messages

    with SessionLocal() as db:
        user = db.scalar(select(User))
        conversation = ConversationRepository(db).create(user.id, "restart")
        db.add(Message(conversation_id=conversation.id, role="assistant", content="checkpoint", status="streaming"))
        db.commit()
        assert recover_interrupted_messages(db) == 1
        row = db.scalar(select(Message).where(Message.conversation_id == conversation.id))
        assert row.content == "checkpoint"
        assert row.status == "interrupted"
