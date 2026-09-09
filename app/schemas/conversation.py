"""会话相关 Pydantic 模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.schemas.artifacts import CodeSnippet, SourceImage
from app.schemas.message import SourceRef
from app.schemas.rag import RouteDecisionOut


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=128)
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=128)


class MessageBrief(BaseModel):
    id: str
    role: str
    content: str
    status: str
    sources: list[SourceRef] = Field(
        default_factory=list,
        validation_alias="sources_json",
    )
    images: list[SourceImage] = Field(default_factory=list)
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    route: RouteDecisionOut | None = Field(
        default=None,
        validation_alias="routing_json",
    )
    error_message: str | None = None
    feedback: Literal["helpful", "unhelpful"] | None = None
    created_at: datetime

    @field_validator("route", mode="before")
    @classmethod
    def normalize_empty_route(cls, value):
        return value or None

    @field_validator("feedback", mode="before")
    @classmethod
    def normalize_feedback(cls, value):
        return getattr(value, "rating", value) if value else None

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    knowledge_base_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageBrief] = Field(default_factory=list)

    model_config = {"from_attributes": True}
