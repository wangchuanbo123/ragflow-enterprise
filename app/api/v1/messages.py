"""消息接口：同步问答与 SSE 流式问答。"""

import json
import anyio

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.models.message import MessageFeedback
from app.repositories.conversation_repository import MessageRepository
from app.schemas.message import MessageAnswer, MessageCreate, MessageFeedbackCreate, MessageFeedbackOut
from app.services import chat_service

router = APIRouter(prefix="/conversations", tags=["messages"])


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageAnswer,
)
def create_message(
    conversation_id: str,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """同步问答接口，用于测试和第三方接入。"""
    result = chat_service.answer_sync(
        db,
        conversation_id,
        current_user.id,
        payload.content,
        payload.retrieval,
    )
    return MessageAnswer(
        user_message=chat_service._to_message_out(result["user_message"]),
        assistant_message=chat_service._to_message_out(result["assistant_message"]),
    )


@router.post("/{conversation_id}/messages/stream")
def stream_message(
    conversation_id: str,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE 流式问答接口，Web 默认调用。"""
    from app.bootstrap.container import get_provider_container

    events = chat_service.answer_stream(
        db,
        conversation_id,
        current_user.id,
        payload.content,
        payload.retrieval,
        session_factory=get_provider_container().metadata.session,
    )

    def next_event():
        try:
            return next(events)
        except StopIteration:
            return None

    async def event_stream():
        try:
            while True:
                evt = await run_in_threadpool(next_event)
                if evt is None:
                    break
                yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'], ensure_ascii=False)}\n\n"
        finally:
            # Starlette cancels the response iterator on disconnect. Shield
            # persistence so partial output and upstream resources are closed.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(events.close)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.put(
    "/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageFeedbackOut,
)
def save_message_feedback(
    conversation_id: str,
    message_id: str,
    payload: MessageFeedbackCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    message = MessageRepository(db).get_for_user(message_id, conversation_id, current_user.id)
    if message is None or message.role != "assistant":
        from app.api.errors import not_found
        raise not_found("回答不存在")
    feedback = db.scalar(select(MessageFeedback).where(MessageFeedback.message_id == message_id))
    if feedback is None:
        feedback = MessageFeedback(message_id=message_id, user_id=current_user.id, rating=payload.rating)
        db.add(feedback)
    feedback.rating = payload.rating
    feedback.comment = payload.comment.strip() if payload.comment else None
    db.commit()
    return MessageFeedbackOut(message_id=message_id, rating=feedback.rating, comment=feedback.comment)
