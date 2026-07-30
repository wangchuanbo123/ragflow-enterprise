"""实体关系抽取的 Pydantic Schema 与校验。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 实体类型白名单
VALID_ENTITY_TYPES = frozenset({
    "SYSTEM", "MODULE", "INTERFACE", "REQUIREMENT", "TEST_CASE",
    "ROLE", "ENVIRONMENT", "DOCUMENT", "VERSION", "TERM", "OTHER",
})

# 关系谓词白名单
VALID_PREDICATES = frozenset({
    "CONTAINS", "PART_OF", "DEPENDS_ON", "CALLS", "PROVIDES",
    "IMPLEMENTS", "SATISFIES", "VERIFIED_BY", "DEPLOYED_ON",
    "OWNED_BY", "USES", "RELATED_TO",
})


class ExtractedEntity(BaseModel):
    local_id: str = Field(description="片段内唯一标识，如 e1")
    name: str = Field(min_length=1, max_length=255)
    type: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None


class ExtractedRelation(BaseModel):
    subject: str = Field(description="主语的 local_id")
    predicate: str
    object: str = Field(description="宾语的 local_id")
    evidence: str = Field(min_length=1)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class ChunkExtraction(BaseModel):
    chunk_id: str
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    chunks: list[ChunkExtraction] = Field(default_factory=list)
