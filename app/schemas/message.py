"""消息相关 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.config import MAX_MESSAGE_LENGTH


class SourceItem(BaseModel):
    source: str
    preview: str = ""


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class SourceRef(BaseModel):
    source: str
    preview: str = ""


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    status: str
    sources: list[SourceRef] = []
    error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageAnswer(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
