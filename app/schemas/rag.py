"""RAG request scope, routing, and explainability schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import RETRIEVAL_SCOPE_MAX_IDS


RetrievalChannel = Literal["text", "graph", "code"]


class RetrievalScope(BaseModel):
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


class RetrievalOptions(BaseModel):
    channels: list[RetrievalChannel] = Field(default_factory=list, max_length=3)
    scope: RetrievalScope = Field(default_factory=RetrievalScope)


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


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    route: RouteDecisionOut


class RetrievalDebugResponse(BaseModel):
    original_query: str
    retrieval_queries: list[str]
    route: RouteDecisionOut
    sources: list[dict]
    context_preview: str
