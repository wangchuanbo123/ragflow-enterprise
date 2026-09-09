"""知识图谱仓储。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
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
from app.models.knowledge_chunk import KnowledgeChunk
from rag.knowledge_graph.normalizer import normalize_alias, normalize_name


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

    def list_relation_evidence(
        self, relation_ids: list[str] | None = None
    ) -> list[KgRelationEvidence]:
        statement = select(KgRelationEvidence).where(
            KgRelationEvidence.knowledge_base_id == self.knowledge_base_id
        )
        if relation_ids is not None:
            if not relation_ids:
                return []
            statement = statement.where(
                KgRelationEvidence.relation_id.in_(relation_ids)
            )
        return list(self.db.scalars(
            statement.order_by(
                KgRelationEvidence.relation_id,
                KgRelationEvidence.id,
            )
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
        from app.models.graph_governance import GraphSchemaDefinition
        from rag.knowledge_graph.schemas import VALID_ENTITY_TYPES, VALID_PREDICATES

        if action not in {
            "add_entity",
            "add_relation", "hide_relation", "update_relation",
            "update_entity", "merge_relation",
        }:
            raise ValueError("不支持的图谱校正操作")
        active_schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1))
        allowed_predicates = set(
            active_schema.relation_types_json if active_schema else VALID_PREDICATES
        )
        allowed_entity_types = set(
            active_schema.entity_types_json if active_schema else VALID_ENTITY_TYPES
        )

        target_type = "entity" if action in {"add_entity", "update_entity"} else "relation"
        if action == "add_entity":
            name = str(payload.get("canonical_name", "")).strip()
            entity_type = str(payload.get("entity_type", "")).strip().upper()
            if not name:
                raise ValueError("实体标准名称不能为空")
            if allowed_entity_types and entity_type not in allowed_entity_types:
                raise ValueError("实体类型不在当前 Schema 中")
            chunk = self._validate_source(payload)
            normalized = normalize_name(name)
            if not normalized:
                raise ValueError("实体标准名称无有效字符")
            existing = self.db.scalar(select(KgEntity).where(
                KgEntity.knowledge_base_id == self.knowledge_base_id,
                KgEntity.entity_type == entity_type,
                KgEntity.normalized_name == normalized,
            ))
            if existing is not None:
                raise ValueError("同类型实体已存在，请为现有实体补充提及或进行校正")
            entity = KgEntity(
                id=str(uuid.uuid4()),
                knowledge_base_id=self.knowledge_base_id,
                entity_type=entity_type,
                canonical_name=name,
                normalized_name=normalized,
                description=str(payload.get("description", "")).strip() or None,
            )
            self.db.add(entity)
            self.db.flush()
            mention = KgEntityMention(
                id=str(uuid.uuid4()),
                knowledge_base_id=self.knowledge_base_id,
                entity_id=entity.id,
                document_id=chunk.document_id,
                chunk_id=chunk.id,
                mention=str(payload.get("evidence", "")).strip(),
                confidence=1.0,
            )
            self.db.add(mention)
            for alias in payload.get("aliases") or []:
                alias_text = str(alias).strip()
                normalized_alias = normalize_alias(alias_text)
                if alias_text and normalized_alias and normalized_alias != normalized:
                    self.db.add(KgAlias(
                        id=str(uuid.uuid4()),
                        knowledge_base_id=self.knowledge_base_id,
                        entity_id=entity.id,
                        alias=alias_text,
                        normalized_alias=normalized_alias,
                    ))
            target_id = entity.id
            payload = {
                **payload,
                "canonical_name": name,
                "entity_type": entity_type,
                "document_id": chunk.document_id,
                "chunk_id": chunk.id,
                "mention_id": mention.id,
            }
        elif action == "add_relation":
            subject_id = payload.get("subject_entity_id")
            object_id = payload.get("object_entity_id")
            if subject_id == object_id:
                raise ValueError("关系两端不能是同一个实体")
            subject = self.get_entity(subject_id)
            object_entity = self.get_entity(object_id)
            if subject is None or object_entity is None:
                raise ValueError("关系实体不存在或不属于当前知识库")
            predicate = str(payload.get("predicate") or "").strip().upper()
            if predicate not in allowed_predicates:
                raise ValueError("关系类型不在关系字典中")
            endpoint_rules = []
            for profile in (active_schema.graph_profiles_json or {}).values() if active_schema else []:
                endpoint_rules.extend(
                    item for item in profile.get("relation_types", [])
                    if str(item.get("name") or "").upper() == predicate
                )
            if endpoint_rules and not any(
                (not item.get("source_types") or subject.entity_type in item["source_types"])
                and (not item.get("target_types") or object_entity.entity_type in item["target_types"])
                for item in endpoint_rules
            ):
                raise ValueError(
                    f"关系端点不符合当前 Schema：{subject.entity_type} —{predicate}→ {object_entity.entity_type}"
                )
            chunk = self._validate_source(payload)
            confidence = float(payload.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ValueError("置信度必须在 0 到 1 之间")
            payload = {
                **payload,
                "predicate": predicate,
                "document_id": chunk.document_id,
                "chunk_id": chunk.id,
                "confidence": confidence,
            }
        elif action in {"hide_relation", "update_relation"}:
            if not self._relation_target_exists(target_id):
                raise ValueError("关系不存在或不属于当前知识库")
            if payload.get("predicate") and payload["predicate"] not in allowed_predicates:
                raise ValueError("关系类型不在关系字典中")
            if "confidence" in payload and not 0 <= float(payload["confidence"]) <= 1:
                raise ValueError("置信度必须在 0 到 1 之间")
        elif action == "update_entity":
            if self.get_entity(target_id or "") is None:
                raise ValueError("实体不存在或不属于当前知识库")
            if not str(payload.get("canonical_name", "")).strip() and "canonical_name" in payload:
                raise ValueError("实体标准名称不能为空")
        elif action == "merge_relation":
            canonical_id = str(payload.get("canonical_relation_id", ""))
            if not canonical_id or canonical_id == target_id:
                raise ValueError("合并关系必须不同")
            relation_ids = list(self.db.scalars(select(KgRelation.id).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                KgRelation.id.in_([canonical_id, target_id]),
            )))
            if len(relation_ids) != 2:
                raise ValueError("合并关系不存在或不属于当前知识库")
            existing_merge = self.db.scalar(select(KgGraphCorrection.id).where(
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                KgGraphCorrection.action == "merge_relation",
                KgGraphCorrection.target_id == target_id,
                KgGraphCorrection.status == "active",
            ))
            if existing_merge:
                raise ValueError("该关系已经被合并")
            reverse = self.db.scalar(select(KgGraphCorrection.id).where(
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                KgGraphCorrection.action == "merge_relation",
                KgGraphCorrection.target_id == canonical_id,
                KgGraphCorrection.status == "active",
            ))
            if reverse:
                raise ValueError("关系合并会形成循环")

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

    def _validate_source(self, payload: dict) -> KnowledgeChunk:
        document_id = str(payload.get("document_id", "")).strip()
        chunk_id = str(payload.get("chunk_id", "")).strip()
        evidence = str(payload.get("evidence", "")).strip()
        if not document_id or not chunk_id or not evidence:
            raise ValueError("人工实体或关系必须选择来源文档、Chunk 并填写证据")
        chunk = self.db.scalar(select(KnowledgeChunk).where(
            KnowledgeChunk.id == chunk_id,
            KnowledgeChunk.document_id == document_id,
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
        ))
        if chunk is None:
            raise ValueError("来源 Chunk 不存在，或不属于所选文档/知识库")
        if evidence not in chunk.content:
            raise ValueError("证据必须是所选 Chunk 中的原文片段")
        return chunk

    def _relation_target_exists(self, target_id: str | None) -> bool:
        if not target_id:
            return False
        if target_id.startswith("manual:"):
            correction_id = target_id.split(":", 1)[1]
            return self.db.scalar(select(KgGraphCorrection.id).where(
                KgGraphCorrection.id == correction_id,
                KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
                KgGraphCorrection.action == "add_relation",
                KgGraphCorrection.status == "active",
            )) is not None
        return self.db.scalar(select(KgRelation.id).where(
            KgRelation.id == target_id,
            KgRelation.knowledge_base_id == self.knowledge_base_id,
        )) is not None

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
            if correction.action == "add_entity" and correction.target_id:
                mention_id = str((correction.payload_json or {}).get("mention_id", ""))
                if mention_id:
                    self.db.execute(delete(KgEntityMention).where(
                        KgEntityMention.id == mention_id,
                        KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                    ))
                has_mentions = self.db.scalar(select(func.count(KgEntityMention.id)).where(
                    KgEntityMention.entity_id == correction.target_id,
                    KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                )) or 0
                has_base_relations = self.db.scalar(select(func.count(KgRelation.id)).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    or_(
                        KgRelation.subject_entity_id == correction.target_id,
                        KgRelation.object_entity_id == correction.target_id,
                    ),
                )) or 0
                active_manual_relation = any(
                    correction.target_id in {
                        (item.payload_json or {}).get("subject_entity_id"),
                        (item.payload_json or {}).get("object_entity_id"),
                    }
                    for item in self._active_manual_relations()
                )
                if not has_mentions and not has_base_relations and not active_manual_relation:
                    self.db.execute(delete(KgAlias).where(KgAlias.entity_id == correction.target_id))
                    self.db.execute(delete(KgEntity).where(
                        KgEntity.id == correction.target_id,
                        KgEntity.knowledge_base_id == self.knowledge_base_id,
                    ))
            correction.status = "reverted"
            correction.reverted_by = reverted_by
            correction.reverted_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(correction)
        return correction

    def _active_manual_relations(self) -> list[KgGraphCorrection]:
        return list(self.db.scalars(select(KgGraphCorrection).where(
            KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
            KgGraphCorrection.action == "add_relation",
            KgGraphCorrection.status == "active",
        )))

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
        if correction.action == "add_entity":
            entity = self.get_entity(correction.target_id or "")
            return None if entity is not None else "人工新增的实体已不存在"
        if correction.action == "add_relation":
            subject = self.get_entity(payload.get("subject_entity_id", ""))
            obj = self.get_entity(payload.get("object_entity_id", ""))
            if subject is None or obj is None:
                return "人工关系引用的实体已不存在"
            try:
                self._validate_source(payload)
            except ValueError as exc:
                return str(exc)
            return None
        if correction.action in {"hide_relation", "update_relation"}:
            return None if self._relation_target_exists(correction.target_id) else "目标关系已不存在"
        if correction.action == "merge_relation":
            merged_exists = self.db.scalar(select(KgRelation.id).where(
                KgRelation.id == correction.target_id,
                KgRelation.knowledge_base_id == self.knowledge_base_id,
            ))
            canonical_exists = self.db.scalar(select(KgRelation.id).where(
                KgRelation.id == (correction.payload_json or {}).get("canonical_relation_id"),
                KgRelation.knowledge_base_id == self.knowledge_base_id,
            ))
            return None if merged_exists and canonical_exists else "关系合并引用的关系已不存在"
        if correction.action == "update_entity":
            return (
                None
                if self.get_entity(correction.target_id or "") is not None
                else "机器生成的目标实体已不存在"
            )
        return "未知的校正操作"
