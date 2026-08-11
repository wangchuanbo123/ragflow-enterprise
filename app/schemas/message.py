"""消息相关 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.config import MAX_MESSAGE_LENGTH
from app.schemas.rag import RetrievalOptions, RouteDecisionOut


class SourceItem(BaseModel):
    source: str
    preview: str = ""


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)


class SourceRef(BaseModel):
    source: str
    preview: str = ""


class MessageOut(BaseModel):
    id: str
    knowledge_base_id: str
    conversation_id: str
    role: str
    content: str
    status: str
    sources: list[SourceRef] = Field(default_factory=list)
    route: RouteDecisionOut | None = None
    error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageAnswer(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
