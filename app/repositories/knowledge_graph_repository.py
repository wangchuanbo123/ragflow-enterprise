"""知识图谱仓储。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgGraphCorrection,
    KgRelation,
    KgRelationEvidence,
)
from app.models.knowledge_document import KnowledgeDocument


class KnowledgeGraphRepository:
    def __init__(
        self,
        db: Session,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    def get_entity(self, entity_id: str) -> KgEntity | None:
        return self.db.scalar(
            select(KgEntity).where(
                KgEntity.id == entity_id,
                KgEntity.knowledge_base_id == self.knowledge_base_id,
            )
        )

    def list_entities(self, limit: int | None = None) -> list[KgEntity]:
        statement = (
            select(KgEntity)
            .where(KgEntity.knowledge_base_id == self.knowledge_base_id)
            .order_by(KgEntity.entity_type, KgEntity.normalized_name)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.db.scalars(statement))

    def list_relations(self, limit: int | None = None) -> list[KgRelation]:
        statement = (
            select(KgRelation)
            .where(KgRelation.knowledge_base_id == self.knowledge_base_id)
            .order_by(KgRelation.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.db.scalars(statement))

    def get_entity_aliases(self, entity_id: str) -> list[KgAlias]:
        return list(self.db.scalars(
            select(KgAlias).where(
                KgAlias.knowledge_base_id == self.knowledge_base_id,
                KgAlias.entity_id == entity_id,
            )
        ))

    def get_entity_mentions(self, entity_id: str) -> list[KgEntityMention]:
        return list(self.db.scalars(
            select(KgEntityMention).where(
                KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                KgEntityMention.entity_id == entity_id,
            )
        ))

    def list_relations_for_entity(self, entity_id: str) -> list[KgRelation]:
        return list(self.db.scalars(
            select(KgRelation).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                (KgRelation.subject_entity_id == entity_id) |
                (KgRelation.object_entity_id == entity_id)
            )
        ))

    def get_evidence_for_relation(self, relation_id: str) -> list[KgRelationEvidence]:
        return list(self.db.scalars(
            select(KgRelationEvidence).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.relation_id == relation_id,
            )
        ))

    def count_entities(self) -> int:
        from sqlalchemy import func
        return self.db.scalar(
            select(func.count(KgEntity.id)).where(
                KgEntity.knowledge_base_id == self.knowledge_base_id
            )
        ) or 0

    def count_relations(self) -> int:
        from sqlalchemy import func
        return self.db.scalar(
            select(func.count(KgRelation.id)).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id
            )
        ) or 0

    def list_corrections(self) -> list[KgGraphCorrection]:
        return list(self.db.scalars(
            select(KgGraphCorrection)
            .where(KgGraphCorrection.knowledge_base_id == self.knowledge_base_id)
            .order_by(KgGraphCorrection.created_at.desc())
        ))

    def create_correction(
        self,
        *,
        action: str,
        target_id: str | None,
        payload: dict,
        reason: str,
        created_by: str,
    ) -> KgGraphCorrection:
        from rag.knowledge_graph.schemas import VALID_PREDICATES

        target_type = "entity" if action == "update_entity" else "relation"
        if action == "add_relation":
            subject_id = payload.get("subject_entity_id")
            object_id = payload.get("object_entity_id")
            if subject_id == object_id:
                raise ValueError("关系两端不能是同一个实体")
            if self.get_entity(subject_id) is None or self.get_entity(object_id) is None:
                raise ValueError("关系实体不存在或不属于当前知识库")
            if payload.get("predicate") not in VALID_PREDICATES:
                raise ValueError("关系类型不在关系字典中")
            confidence = float(payload.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ValueError("置信度必须在 0 到 1 之间")
            payload = {**payload, "confidence": confidence}
        elif action in {"hide_relation", "update_relation"}:
            relation = self.db.scalar(
                select(KgRelation).where(
                    KgRelation.id == target_id,
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                )
            )
            if relation is None:
                raise ValueError("关系不存在或不属于当前知识库")
            if payload.get("predicate") and payload["predicate"] not in VALID_PREDICATES:
                raise ValueError("关系类型不在关系字典中")
            if "confidence" in payload and not 0 <= float(payload["confidence"]) <= 1:
                raise ValueError("置信度必须在 0 到 1 之间")
        elif action == "update_entity":
            if self.get_entity(target_id or "") is None:
                raise ValueError("实体不存在或不属于当前知识库")
            if not str(payload.get("canonical_name", "")).strip() and "canonical_name" in payload:
                raise ValueError("实体标准名称不能为空")

        correction = KgGraphCorrection(
            knowledge_base_id=self.knowledge_base_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload_json=payload,
            reason=reason.strip(),
            status="active",
            created_by=created_by,
            checked_at=datetime.now(timezone.utc),
        )
        self.db.add(correction)
        self.db.commit()
        self.db.refresh(correction)
        return correction

    def revert_correction(
        self,
        correction_id: str,
        reverted_by: str,
    ) -> KgGraphCorrection | None:
        correction = self.db.scalar(
            select(KgGraphCorrection).where(
                KgGraphCorrection.id == correction_id,
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
            )
        )
        if correction is None:
            return None
        if correction.status != "reverted":
            correction.status = "reverted"
            correction.reverted_by = reverted_by
            correction.reverted_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(correction)
        return correction

    def reconcile_corrections(self) -> dict:
        """Mark overlays whose base targets disappeared after graph rebuilding."""
        corrections = list(self.db.scalars(
            select(KgGraphCorrection).where(
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                KgGraphCorrection.status.in_(["active", "conflict"]),
            )
        ))
        active = 0
        conflicts = 0
        now = datetime.now(timezone.utc)
        for correction in corrections:
            reason = self._correction_conflict_reason(correction)
            correction.checked_at = now
            correction.conflict_reason = reason
            correction.status = "conflict" if reason else "active"
            if reason:
                conflicts += 1
            else:
                active += 1
        self.db.commit()
        return {
            "checked": len(corrections),
            "active": active,
            "conflicts": conflicts,
        }

    def _correction_conflict_reason(
        self,
        correction: KgGraphCorrection,
    ) -> str | None:
        payload = correction.payload_json or {}
        if correction.action == "add_relation":
            subject = self.get_entity(payload.get("subject_entity_id", ""))
            obj = self.get_entity(payload.get("object_entity_id", ""))
            if subject is None or obj is None:
                return "人工关系引用的实体已不存在"
            document_id = payload.get("document_id")
            if document_id and self.db.scalar(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                )
            ) is None:
                return "人工关系引用的文档已不存在"
            return None
        if correction.action in {"hide_relation", "update_relation"}:
            relation_exists = self.db.scalar(
                select(KgRelation.id).where(
                    KgRelation.id == correction.target_id,
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                )
            )
            return None if relation_exists else "机器生成的目标关系已不存在"
        if correction.action == "update_entity":
            return (
                None
                if self.get_entity(correction.target_id or "") is not None
                else "机器生成的目标实体已不存在"
            )
        return "未知的校正操作"
