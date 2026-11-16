"""Schemas for folders, tags, versions, chunks, and batch operations."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: str | None = None
    description: str | None = Field(default=None, max_length=1000)


class FolderOut(BaseModel):
    id: str
    knowledge_base_id: str
    parent_id: str | None = None
    name: str
    path: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: str | None = None
    description: str | None = Field(default=None, max_length=1000)


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)


class TagOut(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    color: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = Field(default=None, max_length=16)


class DocumentVersionOut(BaseModel):
    id: str
    knowledge_base_id: str
    document_id: str
    version_no: int
    object_key: str
    file_hash: str
    file_size: int
    mime_type: str | None = None
    metadata_json: dict | None = None
    status: str
    created_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: str
    knowledge_base_id: str
    document_id: str
    chunk_index: int
    content: str
    content_hash: str
    token_count: int
    page: int | None = None
    section: str | None = None
    metadata_json: dict | None = None
    graph_status: str
    graph_schema_version: int

    model_config = {"from_attributes": True}


class ChunkNeighborOut(BaseModel):
    chunk_id: str
    chunk_index: int
    content: str
    page: int | None = None
    section: str | None = None


class ChunkDetailOut(BaseModel):
    """片段详情：全量内容 + 前后相邻片段，用于前端原文定位预览。"""

    chunk_id: str
    document_id: str
    document_name: str
    chunk_index: int
    content: str
    page: int | None = None
    section: str | None = None
    images: list[dict] = Field(default_factory=list)
    previous: list[ChunkNeighborOut] = Field(default_factory=list)
    next: list[ChunkNeighborOut] = Field(default_factory=list)


class SearchHitOut(BaseModel):
    """全文搜索命中：片段原文 + 摘要 + 定位信息。"""

    chunk_id: str
    document_id: str
    document_name: str
    chunk_index: int
    page: int | None = None
    section: str | None = None
    score: float
    snippet: str
    content: str
    images: list[dict] = Field(default_factory=list)


class DocumentTagsUpdate(BaseModel):
    tag_ids: list[str] = Field(default_factory=list, max_length=100)


class BatchDocumentRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["delete", "reindex", "tag", "move"]
    tag_ids: list[str] = Field(default_factory=list, max_length=100)
    folder_id: str | None = None

