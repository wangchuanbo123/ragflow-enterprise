"""Conversation persistence around the unified RAG application service."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
import logging
import time

from sqlalchemy.orm import Session

from app.api.errors import bad_request, forbidden, not_found
from app.core.config import MAX_HISTORY_MESSAGES
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

logger = logging.getLogger(__name__)


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
    user = db.get(User, user_id)
    if not user or not KnowledgeBaseRepository(db).can_read(
        conversation.knowledge_base_id, user_id, is_admin=user.role == "admin"
    ):
        raise not_found("会话不存在")
    return conversation


def _load_history(db: Session, conversation_id: str, user_id: str) -> list[dict]:
    rows = MessageRepository(db).list_for_conversation(
        conversation_id,
        user_id,
        MAX_HISTORY_MESSAGES,
    )
    return [{"role": row.role, "content": row.content} for row in rows]


def _requested_knowledge_base_ids(retrieval_options) -> list[str]:
    if retrieval_options is None:
        return []
    if hasattr(retrieval_options, "scope"):
        return list(getattr(retrieval_options.scope, "knowledge_base_ids", []) or [])
    retrieval = dict(retrieval_options)
    scope = retrieval.get("scope") or {}
    if hasattr(scope, "model_dump"):
        scope = scope.model_dump()
    return list(scope.get("knowledge_base_ids") or [])


def _resolve_knowledge_bases(
    db: Session,
    user_id: str,
    current_knowledge_base_id: str,
    retrieval_options,
) -> list[dict[str, str]]:
    requested = list(dict.fromkeys(_requested_knowledge_base_ids(retrieval_options)))
    if not requested:
        requested = [current_knowledge_base_id]
    user = db.get(User, user_id)
    is_admin = bool(user and user.role == "admin")
    accessible = KnowledgeBaseRepository(db).list_for_user(user_id, is_admin=is_admin)
    accessible_by_id = {item.id: item for item in accessible}
    denied = [knowledge_base_id for knowledge_base_id in requested if knowledge_base_id not in accessible_by_id]
    if denied:
        raise forbidden("所选回答范围包含无权访问或已停用的知识库")
    return [
        {"id": knowledge_base_id, "name": accessible_by_id[knowledge_base_id].name}
        for knowledge_base_id in requested
    ]


def _annotate_single_base_result(result: dict, knowledge_base: dict[str, str]) -> dict:
    for source in result.get("sources", []):
        if not source.get("knowledge_base_id"):
            source["knowledge_base_id"] = knowledge_base["id"]
        if not source.get("knowledge_base_name"):
            source["knowledge_base_name"] = knowledge_base["name"]
    route = result.get("route") or {}
    route["knowledge_bases"] = [{**knowledge_base, "status": "ok"}]
    result["route"] = route
    return result


def _to_message_out(message: Message):
    from app.schemas.message import MessageOut, SourceRef
    from app.schemas.rag import RouteDecisionOut
    from rag.artifacts import collect_source_artifacts

    route = message.routing_json or None
    images, code_snippets = collect_source_artifacts(message.sources_json)
    return MessageOut(
        id=message.id,
        knowledge_base_id=message.knowledge_base_id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        status=message.status,
        sources=[SourceRef(**source) for source in (message.sources_json or [])],
        images=images,
        code_snippets=code_snippets,
        route=RouteDecisionOut(**route) if route else None,
        error_message=message.error_message,
        feedback=message.feedback.rating if message.feedback else None,
        created_at=message.created_at,
    )


def answer_sync(
    db: Session,
    conversation_id: str,
    user_id: str,
    content: str,
    retrieval_options=None,
) -> dict:
    """Persist a synchronous answer produced by the retrieval workflow."""
    conversation = _ensure_conversation(db, conversation_id, user_id)
    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")
    knowledge_bases = _resolve_knowledge_bases(
        db, user_id, conversation.knowledge_base_id, retrieval_options
    )

    conversation_repo = ConversationRepository(db)
    message_repo = MessageRepository(db)
    history = _load_history(db, conversation.id, user_id)
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

    try:
        from app.services import rag_service

        if len(knowledge_bases) > 1:
            result = rag_service.ask_multi(
                content, history, knowledge_bases, retrieval_options
            )
        else:
            knowledge_base = knowledge_bases[0]
            result = rag_service.ask(
                content,
                history,
                _runtime_for_knowledge_base(knowledge_base["id"]),
                knowledge_base["id"],
                retrieval_options,
            )
            result = _annotate_single_base_result(result, knowledge_base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Synchronous answer failed (%s)", type(exc).__name__)
        message_repo.add(
            Message(
                conversation_id=conversation.id,
                knowledge_base_id=conversation.knowledge_base_id,
                role="assistant",
                content="",
                status="failed",
                error_message=exc.message if getattr(exc, "code", "") == "INDEX_REBUILD_REQUIRED" else "回答生成失败，请稍后重试",
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
    *,
    session_factory=None,
) -> Iterator[dict]:
    """Validate eagerly; the returned iterator owns persistence and cancellation."""
    conversation = _ensure_conversation(db, conversation_id, user_id)
    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")
    knowledge_bases = _resolve_knowledge_bases(
        db, user_id, conversation.knowledge_base_id, retrieval_options
    )

    history = _load_history(db, conversation.id, user_id)
    # Only immutable values cross the request/stream session boundary.
    knowledge_base_id = conversation.knowledge_base_id
    conversation_id = conversation.id
    db.commit()

    def events():
        scope = session_factory() if session_factory else nullcontext(db)
        with scope as stream_db:
            yield from _stream_answer(
                stream_db, conversation_id, knowledge_base_id,
                content, history, knowledge_bases, retrieval_options,
            )

    return events()


def _stream_answer(db, conversation_id, knowledge_base_id, content, history, knowledge_bases, retrieval_options):
    conversation_repo = ConversationRepository(db)
    message_repo = MessageRepository(db)
    message_repo.add(
        Message(
            conversation_id=conversation_id,
            knowledge_base_id=knowledge_base_id,
            role="user",
            content=content,
            status="completed",
        )
    )
    assistant = message_repo.add(
        Message(
            conversation_id=conversation_id,
            knowledge_base_id=knowledge_base_id,
            role="assistant",
            content="",
            status="streaming",
        )
    )
    assistant_id = assistant.id
    db.commit()
    collected: list[str] = []
    sources: list[dict] = []
    route: dict = {}
    state = "interrupted"
    error_message = None
    upstream = None
    lease = None
    checkpoint_at = time.monotonic()

    def persist(status):
        # Reload after rollback/commit; do not hold a read transaction while
        # waiting on the model. A deleted conversation need not be recreated.
        row = db.get(Message, assistant_id)
        if row is not None:
            message_repo.update(
                row, content="".join(collected), status=status,
                sources_json=sources, routing_json=route,
                error_message=error_message,
            )
            conversation = db.get(Conversation, conversation_id)
            if conversation:
                conversation_repo.touch(conversation)
        db.commit()

    try:
        from app.services.stream_lifecycle import stream_lease
        lease = stream_lease(assistant_id)
        yield {"event": "message", "data": {"message_id": assistant_id}}
        from app.services import rag_service

        if len(knowledge_bases) > 1:
            upstream = rag_service.stream_multi(
                content, history, knowledge_bases, retrieval_options
            )
        else:
            knowledge_base = knowledge_bases[0]
            upstream = rag_service.stream(
                content,
                history,
                _runtime_for_knowledge_base(knowledge_base["id"]),
                knowledge_base["id"],
                retrieval_options,
            )
        for event in upstream:
            if lease is not None:
                lease.assert_held()
            if event["event"] == "delta":
                collected.append(event["data"]["content"])
                if time.monotonic() - checkpoint_at >= 1:
                    persist("streaming")
                    checkpoint_at = time.monotonic()
            elif event["event"] == "answer":
                collected[:] = [event["data"]["content"]]
            elif event["event"] == "sources":
                sources = event["data"]["sources"]
                if len(knowledge_bases) == 1:
                    _annotate_single_base_result(
                        {"sources": sources, "route": route}, knowledge_bases[0]
                    )
                from rag.artifacts import collect_source_artifacts

                images, code_snippets = collect_source_artifacts(sources)
                event["data"]["images"] = images
                event["data"]["code_snippets"] = code_snippets
            elif event["event"] == "route":
                route = event["data"]["route"]
                if len(knowledge_bases) == 1:
                    route["knowledge_bases"] = [
                        {**knowledge_bases[0], "status": "ok"}
                    ]
            yield event

        if lease is not None:
            lease.assert_held()
        state = "completed"
        persist(state)
        yield {
            "event": "done",
            "data": {"message_id": assistant_id, "status": "completed"},
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        state = "failed"
        # Provider exceptions can contain endpoint URLs or credentials. Keep
        # public and persisted errors generic; record only the exception type.
        logger.warning("Answer stream failed (%s)", type(exc).__name__)
        incompatible_index = getattr(exc, "code", "") == "INDEX_REBUILD_REQUIRED"
        error_message = exc.message if incompatible_index else "回答生成失败，请稍后重试"
        persist(state)
        yield {
            "event": "error",
            "data": {
                "code": "INDEX_REBUILD_REQUIRED" if incompatible_index else "RAG_GENERATION_FAILED",
                "message": error_message,
            },
        }
    finally:
        try:
            if upstream is not None and hasattr(upstream, "close"):
                upstream.close()
        finally:
            try:
                persist(state)
            finally:
                if lease is not None:
                    lease.close()
