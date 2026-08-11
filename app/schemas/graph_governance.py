"""Knowledge-graph build and governance API models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GraphSchemaCreate(BaseModel):
    entity_types: list[str] = Field(min_length=1)
    relation_types: list[str] = Field(min_length=1)
    normalization_rules: dict = Field(default_factory=dict)
    extraction_prompt: str | None = Field(default=None, max_length=10000)


class GraphSchemaOut(BaseModel):
    id: str
    knowledge_base_id: str
    version: int
    status: str
    entity_types_json: list
    relation_types_json: list
    normalization_rules_json: dict | None = None
    extraction_prompt: str | None = None
    created_by: str | None = None
    created_at: datetime
    activated_at: datetime | None = None

    model_config = {"from_attributes": True}


class GraphBuildRequest(BaseModel):
    mode: Literal["pending", "retry_failed", "full"] = "pending"
    rebuild_entity_vectors: bool = True
    rebuild_communities: bool = True


class GraphQualityOut(BaseModel):
    id: str
    knowledge_base_id: str
    graph_version: str
    status: str
    metrics_json: dict
    gate_results_json: dict
    created_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EntityMergeCreate(BaseModel):
    canonical_entity_id: str
    merged_entity_id: str
    reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def distinct_entities(self):
        if self.canonical_entity_id == self.merged_entity_id:
            raise ValueError("合并实体必须不同")
        return self


class EntityMergeOut(BaseModel):
    id: str
    knowledge_base_id: str
    canonical_entity_id: str
    merged_entity_id: str
    reason: str
    status: str
    created_by: str | None = None
    reverted_by: str | None = None
    created_at: datetime
    reverted_at: datetime | None = None

    model_config = {"from_attributes": True}


class ReviewResolve(BaseModel):
    decision: Literal["accepted", "rejected"]


class ReviewItemOut(BaseModel):
    id: str
    knowledge_base_id: str
    item_type: str
    target_id: str | None = None
    payload_json: dict
    reason: str
    confidence: float
    status: str
    reviewed_by: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None

    model_config = {"from_attributes": True}


class CommunityOut(BaseModel):
    id: str
    parent_id: str | None = None
    level: int
    title: str
    summary: str | None = None
    evidence_json: list | None = None
    members: list[dict] = Field(default_factory=list)


class EntityVectorStateOut(BaseModel):
    index_name: str | None = None
    embedding_fingerprint: str = ""
    graph_fingerprint: str = ""
    entity_count: int = 0
    status: str = "empty"
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
