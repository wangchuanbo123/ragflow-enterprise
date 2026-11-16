"""Validated task inputs, bounded plans and evidence-backed outputs."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Task = Literal["extraction", "ingestion", "schema_governance", "quality_review", "incremental_maintenance",
               "knowledge_organization", "evaluation", "business_analysis", "operations"]


class GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    expected_document_ids: list[str] = Field(default_factory=list, max_length=100)
    expected_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    reference_answer: str = Field(default="", max_length=10000)
    provenance: Literal["manual", "agent_generated"] = "manual"
    source_run_id: str | None = None
    candidate_id: str | None = None


class WorkflowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply: bool = False
    graph_kind: Literal["general"] = "general"
    mode: Literal["pending", "retry_failed", "full"] = "pending"
    data_source_id: str | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    job_ids: list[str] = Field(default_factory=list, max_length=20)
    questions: list[str] = Field(default_factory=list, max_length=20)
    gold_cases: list[GoldCase] = Field(default_factory=list, max_length=20)
    runtime_profile: str | None = Field(default=None, max_length=80)
    comparison_profiles: list[str] = Field(default_factory=list, max_length=5)
    prompt_variants: list[dict[str, str]] = Field(default_factory=list, max_length=5)
    allowed_actions: list[str] | None = Field(default=None, max_length=20)
    max_actions: int = Field(default=10, ge=1, le=50)
    max_runtime_seconds: int = Field(default=900, ge=30, le=3600)
    max_agent_calls: int = Field(default=40, ge=1, le=200)
    generate_cases: bool = False
    max_generated_cases: int = Field(default=5, ge=1, le=20)


class AgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Task
    instructions: str = Field(default="", max_length=12000)
    document_ids: list[str] = Field(default_factory=list, max_length=500)
    options: WorkflowOptions = Field(default_factory=WorkflowOptions)


class WorkflowEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    quote: str = Field(min_length=1, max_length=2000)
    document_id: str | None = None


class GeneratedEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["evaluation_case"] = "evaluation_case"
    question: str = Field(min_length=1, max_length=4000)
    reference_answer: str = Field(min_length=1, max_length=10000)
    expected_document_ids: list[str] = Field(min_length=1, max_length=100)
    expected_chunk_ids: list[str] = Field(min_length=1, max_length=100)
    evidence: list[WorkflowEvidence] = Field(min_length=1, max_length=50)


class WorkflowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=60)
    document_ids: list[str] = Field(default_factory=list, max_length=500)
    params: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


class WorkflowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=20000)
    items: list[dict] = Field(default_factory=list, max_length=200)
    evidence: list[WorkflowEvidence] = Field(default_factory=list, max_length=200)
    actions: list[WorkflowAction] = Field(default_factory=list, max_length=50)
    diagnostics: list[dict] = Field(default_factory=list, max_length=100)
    metrics: dict = Field(default_factory=dict)
