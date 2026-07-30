"""消息接口：同步问答与 SSE 流式问答。"""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.message import MessageAnswer, MessageCreate
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
    result = chat_service.answer_sync(db, conversation_id, current_user.id, payload.content)
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
    events = chat_service.answer_stream(db, conversation_id, current_user.id, payload.content)

    def event_stream():
        for evt in events:
            yield f"event: {evt['event']}\n"
            yield f"data: {json.dumps(evt['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
