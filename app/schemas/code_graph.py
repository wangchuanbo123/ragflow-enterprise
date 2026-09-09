"""Versioned code graph API models."""

from datetime import datetime

from pydantic import BaseModel, Field


class CodeIndexRequest(BaseModel):
    data_source_id: str = Field(min_length=1, max_length=36)


class CodeSnapshotOut(BaseModel):
    id: str
    knowledge_base_id: str
    data_source_id: str | None = None
    revision: str
    content_hash: str
    parser_version: str
    status: str
    language_stats_json: dict | None = None
    error_message: str | None = None
    created_at: datetime
    activated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CodeSnapshotDiffOut(BaseModel):
    base_snapshot_id: str
    target_snapshot_id: str
    added_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    unchanged_files: int = 0


class CodeFileIssueOut(BaseModel):
    path: str
    language: str
    status: str
    errors: list[str] = Field(default_factory=list)


class CodeSymbolOut(BaseModel):
    id: str
    snapshot_id: str
    file_id: str
    path: str
    language: str
    kind: str
    name: str
    qualified_name: str
    signature: str | None = None
    start_line: int
    end_line: int
    source_excerpt: str | None = None
    functional_summary: str | None = None
    metadata: dict = Field(default_factory=dict)


class CodeRelationOut(BaseModel):
    id: str
    source: str
    source_name: str
    target: str | None = None
    target_name: str
    relation_type: str
    confidence: float
    resolution_status: str
    path: str
    start_line: int
    end_line: int


class CodeSubgraphOut(BaseModel):
    nodes: list[CodeSymbolOut] = Field(default_factory=list)
    edges: list[CodeRelationOut] = Field(default_factory=list)


class CodeGraphStatsOut(BaseModel):
    active_snapshot_id: str | None = None
    revision: str | None = None
    files: int = 0
    symbols: int = 0
    relations: int = 0
    resolved_relations: int = 0


class CodeVectorStateOut(BaseModel):
    snapshot_id: str | None = None
    index_name: str | None = None
    embedding_fingerprint: str = ""
    symbol_fingerprint: str = ""
    symbol_count: int = 0
    status: str = "empty"
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
