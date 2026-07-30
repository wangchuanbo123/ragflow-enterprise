"""知识图谱 Pydantic 模型。"""

from pydantic import BaseModel


class GraphStatsOut(BaseModel):
    entities: int
    aliases: int
    mentions: int
    relations: int
    evidence: int


class EntityOut(BaseModel):
    id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    description: str | None = None
    aliases: list[str] = []


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
