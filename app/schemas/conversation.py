"""会话相关 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=128)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=128)


class MessageBrief(BaseModel):
    id: str
    role: str
    content: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageBrief] = []

    model_config = {"from_attributes": True}
