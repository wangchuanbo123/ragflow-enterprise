"""消息相关 Pydantic 模型。"""

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import MAX_MESSAGE_LENGTH
from app.schemas.artifacts import CodeSnippet, SourceImage
from app.schemas.rag import RetrievalOptions, RouteDecisionOut


class SourceItem(BaseModel):
    source: str
    preview: str = ""
    citation_id: str = ""
    document_id: str = ""
    chunk_id: str = ""
    page: int | str | None = None
    section: str | None = None
    retrieval_channels: list[str] = Field(default_factory=list)
    graph_facts: list[str] = Field(default_factory=list)
    fusion_score: float = 0.0
    relevance_score: float | None = None
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    images: list[SourceImage] = Field(default_factory=list)
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    contains_code: bool = False


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)


class SourceRef(BaseModel):
    source: str
    preview: str = ""
    citation_id: str = ""
    document_id: str = ""
    chunk_id: str = ""
    page: int | str | None = None
    section: str | None = None
    retrieval_channels: list[str] = Field(default_factory=list)
    graph_facts: list[str] = Field(default_factory=list)
    fusion_score: float = 0.0
    relevance_score: float | None = None
    knowledge_base_id: str = ""
    knowledge_base_name: str = ""
    images: list[SourceImage] = Field(default_factory=list)
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    contains_code: bool = False


class MessageOut(BaseModel):
    id: str
    knowledge_base_id: str
    conversation_id: str
    role: str
    content: str
    status: str
    sources: list[SourceRef] = Field(default_factory=list)
    images: list[SourceImage] = Field(default_factory=list)
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    route: RouteDecisionOut | None = None
    error_message: str | None = None
    feedback: Literal["helpful", "unhelpful"] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageAnswer(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut


class MessageFeedbackCreate(BaseModel):
    rating: Literal["helpful", "unhelpful"]
    comment: str | None = Field(default=None, max_length=1000)


class MessageFeedbackOut(BaseModel):
    message_id: str
    rating: Literal["helpful", "unhelpful"]
    comment: str | None = None
