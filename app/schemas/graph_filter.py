"""A closed, non-executable language for personal graph views."""

from datetime import datetime
from typing import Literal
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GraphFilterLimits(StrictDTO):
    max_nodes: int = Field(default=200, ge=1, le=500)
    max_edges: int = Field(default=400, ge=0, le=1000)


class GraphFilterPreviewRequest(GraphFilterLimits):
    principles: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("principles")
    @classmethod
    def clean_principles(cls, values):
        if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 500 for value in values):
            raise ValueError("每条原则须为1–500字，最多20条")
        return [value.strip() for value in values]


class GraphFilterSaveRequest(StrictDTO):
    name: str = Field(min_length=1, max_length=120)
    principles: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    _clean_principles = field_validator("principles")(GraphFilterPreviewRequest.clean_principles.__func__)


class GraphFilterCondition(StrictDTO):
    target: Literal["node", "edge"]
    field: Literal["id", "entity_type", "name", "keyword", "source_document", "confidence", "has_evidence",
                   "predicate", "source_type", "target_type", "endpoint_type", "source_name", "target_name"]
    operator: Literal["in", "not_in", "contains_any", "not_contains_any", "gte", "lte", "eq"]
    values: list[str] = Field(default_factory=list, max_length=50)
    number: float | None = None
    boolean: bool | None = None

    @model_validator(mode="after")
    def allowed_combination(self):
        node_fields = {"id", "entity_type", "name", "keyword", "source_document", "confidence", "has_evidence"}
        edge_fields = {"id", "predicate", "keyword", "source_document", "confidence", "has_evidence",
                       "source_type", "target_type", "endpoint_type", "source_name", "target_name"}
        if self.field not in (node_fields if self.target == "node" else edge_fields):
            raise ValueError("目标不支持该条件字段")
        if self.field == "confidence":
            if self.operator not in {"gte", "lte"} or self.number is None or not math.isfinite(self.number) or not 0 <= self.number <= 1 or self.values or self.boolean is not None:
                raise ValueError("置信度只支持0–1数值范围")
        elif self.field == "has_evidence":
            if self.operator != "eq" or self.boolean is None or self.values or self.number is not None:
                raise ValueError("证据存在性只支持布尔判断")
        else:
            if not self.values or any(not 1 <= len(value.strip()) <= 500 for value in self.values) or self.number is not None or self.boolean is not None:
                raise ValueError("文字条件须有1–50个有效值")
            if self.operator not in {"in", "not_in", "contains_any", "not_contains_any"}:
                raise ValueError("文字条件不支持该运算符")
            if self.field in {"entity_type", "predicate", "source_type", "target_type", "endpoint_type", "id", "source_document"} and self.operator not in {"in", "not_in"}:
                raise ValueError("标识符和类型只允许精确包含或排除")
            self.values = list(dict.fromkeys(value.strip() for value in self.values))
        return self


class GraphNeighborhood(StrictDTO):
    seeds: list[str] = Field(min_length=1, max_length=10)
    hops: int = Field(ge=1, le=3)
    direction: Literal["incoming", "outgoing", "both"] = "both"

    @field_validator("seeds")
    @classmethod
    def clean_seeds(cls, values):
        if any(not 1 <= len(value.strip()) <= 255 for value in values):
            raise ValueError("中心实体须为有效ID、名称或别名")
        return list(dict.fromkeys(value.strip() for value in values))


class GraphFilterClause(StrictDTO):
    conditions: list[GraphFilterCondition] = Field(default_factory=list, max_length=16)
    neighborhood: GraphNeighborhood | None = None
    hide_isolated: bool = False

    @model_validator(mode="after")
    def not_an_unfiltered_fallback(self):
        if not self.conditions and self.neighborhood is None and not self.hide_isolated:
            raise ValueError("非空原则不能编译成无条件全图")
        return self


class GraphPrinciplePlan(StrictDTO):
    index: int = Field(ge=0, le=19)
    status: Literal["applied", "unsupported", "ambiguous"]
    summary: str = Field(min_length=1, max_length=1000)
    alternatives: list[GraphFilterClause] = Field(default_factory=list, max_length=12)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("summary", "reason")
    @classmethod
    def meaningful_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("理解摘要及未应用原因不能为空白")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def complete_status(self):
        if self.status == "applied" and not self.alternatives:
            raise ValueError("已理解原则必须有受限条件")
        if self.status == "applied" and self.reason is not None:
            raise ValueError("已应用原则不能同时声明未应用原因")
        if self.status != "applied" and (self.alternatives or not self.reason):
            raise ValueError("未应用原则须明确原因且不能产生部分规则")
        return self


class GraphFilterPlan(StrictDTO):
    version: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=2000)
    principles: list[GraphPrinciplePlan] = Field(default_factory=list, max_length=20)

    @field_validator("summary")
    @classmethod
    def meaningful_summary(cls, value):
        if not value.strip():
            raise ValueError("理解摘要不能为空白")
        return value.strip()


class GraphFilterEvidence(StrictDTO):
    chunk_id: str
    document_id: str
    quote: str
    confidence: float


class GraphFilterNode(StrictDTO):
    id: str
    label: str
    entity_type: str
    description: str | None = None
    mention_count: int = 0
    confidence: float | None = None


class GraphFilterEdge(StrictDTO):
    id: str
    source: str
    target: str
    predicate: str
    confidence: float
    description: str | None = None
    evidence: list[GraphFilterEvidence] = Field(default_factory=list)
    evidence_count: int = 0
    manual: bool = False
    correction_id: str | None = None


class GraphFilterStats(StrictDTO):
    total_nodes: int
    total_edges: int
    matched_nodes: int
    matched_edges: int
    returned_nodes: int
    returned_edges: int
    truncated: bool


class GraphPrincipleCounts(StrictDTO):
    index: int
    matched_nodes: int
    matched_edges: int


class GraphFilterPreviewResponse(StrictDTO):
    nodes: list[GraphFilterNode]
    edges: list[GraphFilterEdge]
    stats: GraphFilterStats
    plan: GraphFilterPlan
    summary: str
    warnings: list[str]
    unapplied_principles: list[dict] = Field(default_factory=list)
    schema_fingerprint: str
    graph_fingerprint: str
    per_principle_counts: list[GraphPrincipleCounts] = Field(default_factory=list)


class GraphFilterViewOut(StrictDTO):
    id: str
    name: str
    principles: list[str]
    schema_fingerprint: str | None
    created_at: datetime
    updated_at: datetime
