"""
聊天服务

负责：
- 校验会话归属
- 保存用户消息
- 创建 streaming 状态的助手消息
- 调用 RAG 两个阶段（prepare_context -> generate/stream_generate）
- 成功后保存完整内容、来源和 completed 状态
- 失败标记为 failed

SSE 事件通过生成器产出，供路由层包装为 text/event-stream。
"""

from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.api.errors import bad_request, not_found
from app.core.config import MAX_HISTORY_MESSAGES
from app.models.conversation import Conversation
from app.models.message import Message
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.services.rag_service import prepare_context, stream_generate


def _ensure_conversation(db: Session, conversation_id: str, user_id: str) -> Conversation:
    repo = ConversationRepository(db)
    conv = repo.get_for_user(conversation_id, user_id)
    if not conv:
        raise not_found("会话不存在")
    return conv


def _load_history(db: Session, conversation_id: str, user_id: str) -> list[dict]:
    rows = MessageRepository(db).list_for_conversation(conversation_id, user_id, MAX_HISTORY_MESSAGES)
    return [{"role": r.role, "content": r.content} for r in rows]


def _to_message_out(message: Message):
    """把 ORM 消息转为可序列化字典。"""
    from app.schemas.message import MessageOut, SourceRef

    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        status=message.status,
        sources=[SourceRef(**s) for s in (message.sources_json or [])],
        error_message=message.error_message,
        created_at=message.created_at,
    )


def answer_sync(
    db: Session, conversation_id: str, user_id: str, content: str
) -> dict:
    """同步问答：保存用户消息，生成完整答案并持久化助手消息。"""
    conv = _ensure_conversation(db, conversation_id, user_id)

    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")

    conv_repo = ConversationRepository(db)
    msg_repo = MessageRepository(db)

    user_message = Message(conversation_id=conv.id, role="user", content=content, status="completed")
    msg_repo.add(user_message)
    conv_repo.touch(conv)

    history = _load_history(db, conv.id, user_id)
    # history 此时包含刚保存的用户消息
    try:
        from app.services.rag_concurrency import rag_concurrency

        with rag_concurrency():
            prepared = prepare_context(content, history)
            from app.services.rag_service import generate

            answer = generate(content, prepared["context"], history)
    except Exception as exc:  # noqa: BLE001
        assistant = Message(
            conversation_id=conv.id,
            role="assistant",
            content="",
            status="failed",
            error_message=str(exc)[:1000],
        )
        msg_repo.add(assistant)
        conv_repo.touch(conv)
        raise

    assistant = Message(
        conversation_id=conv.id,
        role="assistant",
        content=answer,
        status="completed",
        sources_json=prepared["sources"],
    )
    msg_repo.add(assistant)
    conv_repo.touch(conv)

    return {"user_message": user_message, "assistant_message": assistant}


def answer_stream(
    db: Session, conversation_id: str, user_id: str, content: str
) -> Iterator[dict]:
    """流式问答：产出 SSE 事件字典序列。

    事件结构：
      {"event": "message", "data": {"message_id": ...}}
      {"event": "delta", "data": {"content": ...}}
      {"event": "sources", "data": {"sources": [...]}}
      {"event": "done", "data": {"message_id": ..., "status": "completed"}}
      {"event": "error", "data": {"code": ..., "message": ...}}
    """
    conv = _ensure_conversation(db, conversation_id, user_id)

    content = (content or "").strip()
    if not content:
        raise bad_request("消息内容不能为空")

    conv_repo = ConversationRepository(db)
    msg_repo = MessageRepository(db)

    user_message = Message(conversation_id=conv.id, role="user", content=content, status="completed")
    msg_repo.add(user_message)
    conv_repo.touch(conv)

    history = _load_history(db, conv.id, user_id)

    assistant = Message(
        conversation_id=conv.id,
        role="assistant", content="", status="streaming"
    )
    msg_repo.add(assistant)

    yield {"event": "message", "data": {"message_id": assistant.id}}

    try:
        from app.services.rag_concurrency import rag_concurrency

        with rag_concurrency():
            prepared = prepare_context(content, history)
            sources = prepared["sources"]

            collected: list[str] = []
            for chunk in stream_generate(content, prepared["context"], history):
                collected.append(chunk)
                yield {"event": "delta", "data": {"content": chunk}}

        full = "".join(collected)
        msg_repo.update(
            assistant,
            content=full,
            status="completed",
            sources_json=sources,
            error_message=None,
        )
        conv_repo.touch(conv)

        yield {"event": "sources", "data": {"sources": sources}}
        yield {
            "event": "done",
            "data": {"message_id": assistant.id, "status": "completed"},
        }
    except Exception as exc:  # noqa: BLE001
        msg_repo.update(
            assistant,
            status="failed",
            error_message=str(exc)[:1000],
        )
        yield {
            "event": "error",
            "data": {
                "code": "RAG_GENERATION_FAILED",
                "message": "回答生成失败，请稍后重试",
            },
        }
