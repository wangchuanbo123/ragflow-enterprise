"""Knowledge-graph build and governance API models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


GraphKind = Literal["general", "concept", "functional", "configuration", "standard"]
BusinessGraphKind = Literal["concept", "functional", "configuration", "standard"]


class GraphProfileEntityType(BaseModel):
    name: str
    label: str
    color: str


class GraphProfileRelationType(BaseModel):
    name: str
    label: str
    source_types: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)


class GraphProfileOut(BaseModel):
    key: GraphKind
    label: str
    short_label: str
    purpose: str
    positioning: str
    recommended_layout: Literal["cose", "concentric", "breadthfirst"]
    color: str
    document_hints: list[str] = Field(default_factory=list)
    extraction_rules: list[str] = Field(default_factory=list)
    entity_types: list[GraphProfileEntityType] = Field(default_factory=list)
    relation_types: list[GraphProfileRelationType] = Field(default_factory=list)
    entity_count: int = 0
    relation_count: int = 0
    schema_entity_coverage: int = 0
    schema_relation_coverage: int = 0


class GraphProfileTemplateRequest(BaseModel):
    graph_kind: BusinessGraphKind


class GraphSchemaCreate(BaseModel):
    graph_kind: GraphKind = "general"
    entity_types: list[str] = Field(min_length=1)
    relation_types: list[str] = Field(min_length=1)
    normalization_rules: dict = Field(default_factory=dict)
    extraction_prompt: str | None = Field(default=None, max_length=10000)
    graph_profiles: dict | None = None


class GraphSchemaUpdate(GraphSchemaCreate):
    """Draft schemas are editable; active schemas remain immutable versions."""


class GraphAutomationSettings(BaseModel):
    auto_build_enabled: bool = True
    auto_schema_generation_enabled: bool = True


class GraphSchemaOut(BaseModel):
    id: str
    knowledge_base_id: str
    graph_kind: GraphKind = "general"
    version: int
    status: str
    entity_types_json: list
    relation_types_json: list
    graph_profiles_json: dict | None = None
    normalization_rules_json: dict | None = None
    extraction_prompt: str | None = None
    generation_method: str = "manual"
    source_document_ids_json: list[str] | None = None
    generation_summary_json: dict | None = None
    created_by: str | None = None
    created_at: datetime
    activated_at: datetime | None = None

    model_config = {"from_attributes": True}


class GraphBuildRequest(BaseModel):
    """三档策略优先；旧布尔字段保留兼容（None 表示未指定）。"""

    profile: Literal["fast", "standard", "deep"] | None = None
    mode: Literal["pending", "retry_failed", "full"] = "pending"
    graph_kind: GraphKind = "general"
    rebuild_entity_vectors: bool | None = None
    rebuild_relation_vectors: bool | None = None
    rebuild_communities: bool | None = None


class GraphSchemaGenerateRequest(BaseModel):
    graph_kind: GraphKind = "general"
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    instructions: str | None = Field(default=None, max_length=2000)
    mode: Literal["new", "overwrite"] = "new"
    target_schema_id: str | None = None

    @model_validator(mode="after")
    def validate_generation_mode(self):
        if self.mode == "overwrite" and not self.target_schema_id:
            raise ValueError("覆盖模式必须指定 Schema 草稿")
        if self.mode == "new" and self.target_schema_id:
            raise ValueError("新建模式不能指定目标 Schema")
        return self


class GraphSchemaDriftRequest(BaseModel):
    graph_kind: GraphKind = "general"
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    instructions: str | None = Field(default=None, max_length=2000)


class DocumentGraphCoverageItem(BaseModel):
    document_id: str
    filename: str
    document_status: str
    coverage_status: str
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    pending_chunks: int
    fact_chunks: int
    entity_mentions: int
    unique_entities: int
    relation_evidence: int
    unique_relations: int
    processing_rate: float
    fact_coverage_rate: float
    schema_versions: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DocumentGraphCoverageOut(BaseModel):
    summary: dict
    documents: list[DocumentGraphCoverageItem]


class GraphSchemaImpactOut(BaseModel):
    change_kind: str
    affected_documents: int
    affected_chunks: int
    estimated_llm_requests: int
    estimated_minutes: float
    removed_entity_types: list[str] = Field(default_factory=list)
    removed_relation_types: list[str] = Field(default_factory=list)


class CommunityStateOut(BaseModel):
    status: str = "empty"
    communities: int = 0
    entities: int = 0
    built_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


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


class RelationMergeCreate(BaseModel):
    canonical_relation_id: str
    merged_relation_id: str
    reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def distinct_relations(self):
        if self.canonical_relation_id == self.merged_relation_id:
            raise ValueError("合并关系必须不同")
        return self


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


class RelationVectorStateOut(BaseModel):
    index_name: str | None = None
    embedding_fingerprint: str = ""
    graph_fingerprint: str = ""
    relation_count: int = 0
    status: str = "empty"
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
