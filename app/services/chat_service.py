"""Conversation persistence around the unified RAG application service."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.api.errors import bad_request, not_found
from app.core.config import MAX_HISTORY_MESSAGES
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.conversation import Conversation
from app.models.message import Message
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)


def _runtime_for_knowledge_base(knowledge_base_id: str):
    """Resolve through the module so runtime/test overrides stay observable."""
    from app.services import rag_service

    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return rag_service.active_runtime()
    return rag_service.active_runtime(knowledge_base_id)


def _ensure_conversation(db: Session, conversation_id: str, user_id: str) -> Conversation:
    conversation = ConversationRepository(db).get_for_user(conversation_id, user_id)
    if not conversation:
        raise not_found("会话不存在")
    return conversation


def _load_history(db: Session, conversation_id: str, user_id: str) -> list[dict]:
    rows = MessageRepository(db).list_for_conversation(
        conversation_id,
        user_id,
        MAX_HISTORY_MESSAGES,
    )
    return [{"role": row.role, "content": row.content} for row in rows]


def _to_message_out(message: Message):
    from app.schemas.message import MessageOut, SourceRef
    from app.schemas.rag import RouteDecisionOut

    route = message.routing_json or None
    return MessageOut(
        id=message.id,
        knowledge_base_id=message.knowledge_base_id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        status=message.status,
        sources=[SourceRef(**source) for source in (message.sources_json or [])],
        route=RouteDecisionOut(**route) if route else None,
        error_message=message.error_message,
        created_at=message.created_at,
    )


def answer_sync(
    db: Session,
    conversation_id: str,
    user_id: str,
    content: str,
    retrieval_options=None,
) -> dict:
    """Persist a synchronous answer produced by the compiled LangGraph."""
    conversation = _ensure_conversation(db, conversation_id, user_id)
    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")

    conversation_repo = ConversationRepository(db)
    message_repo = MessageRepository(db)
    user_message = message_repo.add(
        Message(
            conversation_id=conversation.id,
            knowledge_base_id=conversation.knowledge_base_id,
            role="user",
            content=content,
            status="completed",
        )
    )
    conversation_repo.touch(conversation)
    history = _load_history(db, conversation.id, user_id)

    try:
        from app.services import rag_service

        result = rag_service.ask(
            content,
            history,
            _runtime_for_knowledge_base(conversation.knowledge_base_id),
            conversation.knowledge_base_id,
            retrieval_options,
        )
    except Exception as exc:  # noqa: BLE001
        message_repo.add(
            Message(
                conversation_id=conversation.id,
                knowledge_base_id=conversation.knowledge_base_id,
                role="assistant",
                content="",
                status="failed",
                error_message=str(exc)[:1000],
            )
        )
        conversation_repo.touch(conversation)
        raise

    assistant_message = message_repo.add(
        Message(
            conversation_id=conversation.id,
            knowledge_base_id=conversation.knowledge_base_id,
            role="assistant",
            content=result["answer"],
            status="completed",
            sources_json=result["sources"],
            routing_json=result["route"],
        )
    )
    conversation_repo.touch(conversation)
    return {
        "user_message": user_message,
        "assistant_message": assistant_message,
    }


def answer_stream(
    db: Session,
    conversation_id: str,
    user_id: str,
    content: str,
    retrieval_options=None,
) -> Iterator[dict]:
    """Persist and expose message, route, delta, sources, done/error SSE events."""
    conversation = _ensure_conversation(db, conversation_id, user_id)
    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")

    conversation_repo = ConversationRepository(db)
    message_repo = MessageRepository(db)
    message_repo.add(
        Message(
            conversation_id=conversation.id,
            knowledge_base_id=conversation.knowledge_base_id,
            role="user",
            content=content,
            status="completed",
        )
    )
    conversation_repo.touch(conversation)
    history = _load_history(db, conversation.id, user_id)
    assistant = message_repo.add(
        Message(
            conversation_id=conversation.id,
            knowledge_base_id=conversation.knowledge_base_id,
            role="assistant",
            content="",
            status="streaming",
        )
    )
    yield {"event": "message", "data": {"message_id": assistant.id}}

    collected: list[str] = []
    sources: list[dict] = []
    route: dict = {}
    try:
        from app.services import rag_service

        events = rag_service.stream(
            content,
            history,
            _runtime_for_knowledge_base(conversation.knowledge_base_id),
            conversation.knowledge_base_id,
            retrieval_options,
        )
        for event in events:
            if event["event"] == "delta":
                collected.append(event["data"]["content"])
            elif event["event"] == "sources":
                sources = event["data"]["sources"]
            elif event["event"] == "route":
                route = event["data"]["route"]
            yield event

        message_repo.update(
            assistant,
            content="".join(collected),
            status="completed",
            sources_json=sources,
            routing_json=route,
            error_message=None,
        )
        conversation_repo.touch(conversation)
        yield {
            "event": "done",
            "data": {"message_id": assistant.id, "status": "completed"},
        }
    except Exception as exc:  # noqa: BLE001
        message_repo.update(
            assistant,
            status="failed",
            routing_json=route,
            error_message=str(exc)[:1000],
        )
        yield {
            "event": "error",
            "data": {
                "code": "RAG_GENERATION_FAILED",
                "message": "回答生成失败，请稍后重试",
            },
        }
