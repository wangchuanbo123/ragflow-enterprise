"""RAG request scope, routing, and explainability schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import RETRIEVAL_SCOPE_MAX_IDS
from app.schemas.artifacts import CodeSnippet, SourceImage


RetrievalChannel = Literal["text", "graph", "code"]
RetrievalStrategy = Literal["fast", "standard", "deep"]


class RetrievalScope(BaseModel):
    selection_kind: Literal['all', 'knowledge_base', 'document', 'folder', 'tag'] | None = None
    knowledge_base_ids: list[str] = Field(
        default_factory=list,
        max_length=RETRIEVAL_SCOPE_MAX_IDS,
    )
    document_ids: list[str] = Field(
        default_factory=list,
        max_length=RETRIEVAL_SCOPE_MAX_IDS,
    )
    tag_ids: list[str] = Field(
        default_factory=list,
        max_length=RETRIEVAL_SCOPE_MAX_IDS,
    )
    folder_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator('knowledge_base_ids', 'document_ids', 'tag_ids')
    @classmethod
    def nonblank_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError('检索范围不能包含空白编号')
        return values

    @field_validator('folder_id')
    @classmethod
    def nonblank_folder(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError('请选择有效文件夹')
        return value

    @model_validator(mode='after')
    def explicit_scope_requires_selection(self):
        required = {'all': self.knowledge_base_ids, 'knowledge_base': self.knowledge_base_ids,
                    'document': self.document_ids, 'folder': self.folder_id, 'tag': self.tag_ids}
        if self.selection_kind is not None and not required[self.selection_kind]:
            raise ValueError('请先完成检索范围选择；空范围不会自动扩大为整个知识库')
        return self


class RetrievalOptions(BaseModel):
    channels: list[RetrievalChannel] = Field(default_factory=list, max_length=3)
    scope: RetrievalScope = Field(default_factory=RetrievalScope)
    strategy: RetrievalStrategy = "standard"


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)


class RouteDecisionOut(BaseModel):
    method: str
    selected_channels: list[RetrievalChannel]
    requested_channels: list[RetrievalChannel] = Field(default_factory=list)
    unavailable_channels: list[RetrievalChannel] = Field(default_factory=list)
    confidence: float
    reasons: list[str]
    scope: RetrievalScope
    scope_match_count: int | None = None
    channel_results: dict[str, dict] = Field(default_factory=dict)
    fused_count: int = 0
    strategy: RetrievalStrategy = "standard"
    rewrite: dict = Field(default_factory=dict)
    query_embedding: dict = Field(default_factory=dict)
    knowledge_bases: list[dict] = Field(default_factory=list)
    retrieval_diagnostics: list[dict] = Field(default_factory=list)
    evidence_status: Literal['sufficient', 'insufficient', 'unavailable'] | None = None
    citation_validation: dict = Field(default_factory=dict)


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    images: list[SourceImage] = Field(default_factory=list)
    code_snippets: list[CodeSnippet] = Field(default_factory=list)
    route: RouteDecisionOut


class RetrievalDebugResponse(BaseModel):
    original_query: str
    retrieval_queries: list[str]
    route: RouteDecisionOut
    sources: list[dict]
    context_preview: str
