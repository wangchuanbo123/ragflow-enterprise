"""知识图谱 Pydantic 模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GraphStatsOut(BaseModel):
    entities: int
    aliases: int
    mentions: int
    relations: int
    evidence: int
    active_corrections: int = 0
    conflict_corrections: int = 0


class EntityOut(BaseModel):
    id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)


class NeighborRelationOut(BaseModel):
    relation_id: str
    hop: int
    subject_id: str
    subject_name: str
    subject_type: str
    predicate: str
    object_id: str
    object_name: str
    object_type: str
    confidence: float
    manual: bool = False
    correction_id: str | None = None


class GraphEvidenceOut(BaseModel):
    chunk_id: str
    quote: str
    confidence: float


class GraphSubgraphNodeOut(BaseModel):
    id: str
    label: str
    entity_type: str
    description: str | None = None
    mention_count: int = 0


class GraphSubgraphEdgeOut(BaseModel):
    id: str
    source: str
    target: str
    predicate: str
    confidence: float
    evidence: list[GraphEvidenceOut] = Field(default_factory=list)
    manual: bool = False
    correction_id: str | None = None


class DocumentSubgraphOut(BaseModel):
    document_id: str
    nodes: list[GraphSubgraphNodeOut] = Field(default_factory=list)
    edges: list[GraphSubgraphEdgeOut] = Field(default_factory=list)


class EntitySubgraphOut(BaseModel):
    seed_entity_ids: list[str]
    nodes: list[GraphSubgraphNodeOut] = Field(default_factory=list)
    edges: list[GraphSubgraphEdgeOut] = Field(default_factory=list)
    offset: int = 0
    next_offset: int | None = None


class GraphCorrectionCreate(BaseModel):
    action: Literal[
        "add_relation",
        "hide_relation",
        "update_relation",
        "update_entity",
    ]
    target_id: str | None = None
    payload: dict = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action == "add_relation":
            required = {"subject_entity_id", "object_entity_id", "predicate", "evidence"}
            missing = required - set(self.payload)
            if missing:
                raise ValueError(f"新增关系缺少字段：{', '.join(sorted(missing))}")
        elif not self.target_id:
            raise ValueError("该校正操作必须提供 target_id")
        if self.action == "update_relation" and not {
            "predicate", "confidence", "description"
        }.intersection(self.payload):
            raise ValueError("关系更新至少包含 predicate、confidence 或 description")
        if self.action == "update_entity" and not {
            "canonical_name", "description"
        }.intersection(self.payload):
            raise ValueError("实体更新至少包含 canonical_name 或 description")
        return self


class GraphCorrectionOut(BaseModel):
    id: str
    knowledge_base_id: str
    action: str
    target_type: str
    target_id: str | None = None
    payload: dict = Field(validation_alias="payload_json")
    reason: str
    status: str
    conflict_reason: str | None = None
    created_by: str | None = None
    reverted_by: str | None = None
    created_at: datetime
    reverted_at: datetime | None = None
    checked_at: datetime | None = None

    model_config = {"from_attributes": True}
