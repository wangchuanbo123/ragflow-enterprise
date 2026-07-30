"""知识图谱仓储。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgRelation,
    KgRelationEvidence,
)


class KnowledgeGraphRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_entity(self, entity_id: str) -> KgEntity | None:
        return self.db.get(KgEntity, entity_id)

    def get_entity_aliases(self, entity_id: str) -> list[KgAlias]:
        return list(self.db.scalars(
            select(KgAlias).where(KgAlias.entity_id == entity_id)
        ))

    def get_entity_mentions(self, entity_id: str) -> list[KgEntityMention]:
        return list(self.db.scalars(
            select(KgEntityMention).where(KgEntityMention.entity_id == entity_id)
        ))

    def list_relations_for_entity(self, entity_id: str) -> list[KgRelation]:
        return list(self.db.scalars(
            select(KgRelation).where(
                (KgRelation.subject_entity_id == entity_id) |
                (KgRelation.object_entity_id == entity_id)
            )
        ))

    def get_evidence_for_relation(self, relation_id: str) -> list[KgRelationEvidence]:
        return list(self.db.scalars(
            select(KgRelationEvidence).where(KgRelationEvidence.relation_id == relation_id)
        ))

    def count_entities(self) -> int:
        from sqlalchemy import func
        return self.db.scalar(select(func.count(KgEntity.id))) or 0

    def count_relations(self) -> int:
        from sqlalchemy import func
        return self.db.scalar(select(func.count(KgRelation.id))) or 0
