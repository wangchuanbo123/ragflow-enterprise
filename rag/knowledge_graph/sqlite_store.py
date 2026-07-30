"""SQLite 图谱存储实现。"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgRelation,
    KgRelationEvidence,
)
from rag.knowledge_graph.normalizer import normalize_alias, normalize_name
from rag.knowledge_graph.schemas import ChunkExtraction

logger = logging.getLogger(__name__)


def _text_match_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if candidate in query:
        coverage = len(candidate) / max(len(query), 1)
        return 0.8 + 0.2 * coverage
    if query in candidate:
        coverage = len(query) / max(len(candidate), 1)
        return 0.5 + 0.3 * coverage
    return 0.0


class SqliteGraphStore:
    """基于 SQLAlchemy Session 的知识图谱存储。"""

    def __init__(self, db: Session):
        self.db = db

    def upsert_extraction(
        self,
        chunk: dict,
        extraction: ChunkExtraction,
    ) -> None:
        """保存一个 chunk 的抽取结果。

        chunk: {"chunk_id":..., "document_id":..., "content":...}
        extraction: 校验后的 ChunkExtraction
        """
        chunk_id = extraction.chunk_id
        document_id = chunk["document_id"]

        # First delete existing data for this chunk
        self._delete_chunk_graph(chunk_id)

        local_id_to_entity_id: dict[str, str] = {}

        # Upsert entities
        for ent in extraction.entities:
            norm = normalize_name(ent.name)
            if not norm:
                continue

            existing = self.db.scalar(
                select(KgEntity).where(
                    KgEntity.entity_type == ent.type,
                    KgEntity.normalized_name == norm,
                )
            )

            if existing:
                entity = existing
                if ent.description and not entity.description:
                    entity.description = ent.description
            else:
                entity = KgEntity(
                    id=str(uuid.uuid4()),
                    entity_type=ent.type,
                    canonical_name=ent.name,
                    normalized_name=norm,
                    description=ent.description,
                )
                self.db.add(entity)

            self.db.flush()
            local_id_to_entity_id[ent.local_id] = entity.id

            # Upsert aliases
            for alias in ent.aliases:
                norm_alias = normalize_alias(alias)
                if not norm_alias:
                    continue
                exists = self.db.scalar(
                    select(KgAlias).where(
                        KgAlias.entity_id == entity.id,
                        KgAlias.normalized_alias == norm_alias,
                    )
                )
                if not exists:
                    self.db.add(KgAlias(
                        id=str(uuid.uuid4()),
                        entity_id=entity.id,
                        alias=alias,
                        normalized_alias=norm_alias,
                    ))

            # Upsert mention
            mention_exists = self.db.scalar(
                select(KgEntityMention).where(
                    KgEntityMention.entity_id == entity.id,
                    KgEntityMention.chunk_id == chunk_id,
                )
            )
            if not mention_exists:
                self.db.add(KgEntityMention(
                    id=str(uuid.uuid4()),
                    entity_id=entity.id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    mention=ent.name,
                    confidence=1.0,
                ))

        # Upsert relations
        for rel in extraction.relations:
            subj_id = local_id_to_entity_id.get(rel.subject)
            obj_id = local_id_to_entity_id.get(rel.object)
            if not subj_id or not obj_id or subj_id == obj_id:
                continue

            existing = self.db.scalar(
                select(KgRelation).where(
                    KgRelation.subject_entity_id == subj_id,
                    KgRelation.predicate == rel.predicate,
                    KgRelation.object_entity_id == obj_id,
                )
            )

            if existing:
                relation = existing
                if rel.confidence > relation.confidence:
                    relation.confidence = rel.confidence
            else:
                relation = KgRelation(
                    id=str(uuid.uuid4()),
                    subject_entity_id=subj_id,
                    predicate=rel.predicate,
                    object_entity_id=obj_id,
                    confidence=rel.confidence,
                )
                self.db.add(relation)

            self.db.flush()

            # Upsert evidence
            quote_hash = hashlib.sha256(rel.evidence.encode("utf-8")).hexdigest()[:64]
            ev_exists = self.db.scalar(
                select(KgRelationEvidence).where(
                    KgRelationEvidence.relation_id == relation.id,
                    KgRelationEvidence.chunk_id == chunk_id,
                    KgRelationEvidence.quote_hash == quote_hash,
                )
            )
            if not ev_exists:
                self.db.add(KgRelationEvidence(
                    id=str(uuid.uuid4()),
                    relation_id=relation.id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    quote=rel.evidence,
                    quote_hash=quote_hash,
                    confidence=rel.confidence,
                ))

        self.db.commit()

    def _delete_chunk_graph(self, chunk_id: str) -> None:
        """删除 chunk 关联的提及和证据。"""
        self.db.execute(delete(KgEntityMention).where(KgEntityMention.chunk_id == chunk_id))
        self.db.execute(delete(KgRelationEvidence).where(KgRelationEvidence.chunk_id == chunk_id))
        self.db.commit()

    def delete_document_graph(self, document_id: str) -> None:
        """删除文档关联的全部图谱数据。"""
        self.db.execute(delete(KgEntityMention).where(KgEntityMention.document_id == document_id))
        self.db.execute(delete(KgRelationEvidence).where(KgRelationEvidence.document_id == document_id))
        self.db.commit()
        self.cleanup_orphans()

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        """匹配实体名或别名，支持“问题中出现实体名”。

        例如实体“任务调度模块”可以命中“任务调度模块依赖什么”。
        """
        norm = normalize_name(query)
        if not norm:
            return []

        candidate_limit = max(limit * 4, limit)
        entities = list(self.db.scalars(
            select(KgEntity).where(
                or_(
                    KgEntity.normalized_name == norm,
                    KgEntity.normalized_name.contains(norm),
                    literal(norm).contains(KgEntity.normalized_name),
                )
            ).limit(candidate_limit)
        ))
        alias_matches = list(self.db.scalars(
            select(KgAlias).where(
                or_(
                    KgAlias.normalized_alias == norm,
                    KgAlias.normalized_alias.contains(norm),
                    literal(norm).contains(KgAlias.normalized_alias),
                )
            ).limit(candidate_limit)
        ))

        entities_by_id = {entity.id: entity for entity in entities}
        for alias in alias_matches:
            if alias.entity_id not in entities_by_id:
                entity = self.db.get(KgEntity, alias.entity_id)
                if entity is not None:
                    entities_by_id[entity.id] = entity

        alias_scores: dict[str, float] = {}
        for alias in alias_matches:
            alias_scores[alias.entity_id] = max(
                alias_scores.get(alias.entity_id, 0.0),
                _text_match_score(norm, alias.normalized_alias) - 0.25,
            )

        ranked_entities = sorted(
            entities_by_id.values(),
            key=lambda entity: (
                -max(
                    _text_match_score(norm, entity.normalized_name),
                    alias_scores.get(entity.id, 0.0),
                ),
                len(entity.normalized_name),
                entity.canonical_name,
            ),
        )[:limit]

        result = []
        for ent in ranked_entities:
            aliases = list(self.db.scalars(
                select(KgAlias).where(KgAlias.entity_id == ent.id)
            ))
            match_score = max(
                _text_match_score(norm, ent.normalized_name),
                alias_scores.get(ent.id, 0.0),
            )
            result.append({
                "id": ent.id,
                "entity_type": ent.entity_type,
                "canonical_name": ent.canonical_name,
                "normalized_name": ent.normalized_name,
                "description": ent.description,
                "aliases": [a.alias for a in aliases],
                "match_score": match_score,
            })
        return result

    def get_neighbors(
        self, entity_ids: list[str], max_hops: int = 2, limit: int = 50
    ) -> list[dict]:
        """获取实体邻居关系（BFS 遍历）。"""
        if not entity_ids:
            return []

        visited: set[str] = set()
        current = set(entity_ids)
        relations_result: list[dict] = []
        count = 0

        for hop in range(1, max_hops + 1):
            if not current or count >= limit:
                break

            next_layer: set[str] = set()

            rels_as_subj = self.db.scalars(
                select(KgRelation).where(
                    KgRelation.subject_entity_id.in_(list(current)),
                    KgRelation.object_entity_id.notin_(list(visited | current)),
                ).limit(limit - count)
            ).all()

            rels_as_obj = self.db.scalars(
                select(KgRelation).where(
                    KgRelation.object_entity_id.in_(list(current)),
                    KgRelation.subject_entity_id.notin_(list(visited | current)),
                ).limit(limit - count)
            ).all()

            for rel in list(rels_as_subj) + list(rels_as_obj):
                if count >= limit:
                    break
                subj = self.db.get(KgEntity, rel.subject_entity_id)
                obj = self.db.get(KgEntity, rel.object_entity_id)
                if not subj or not obj:
                    continue

                relations_result.append({
                    "relation_id": rel.id,
                    "hop": hop,
                    "subject_id": subj.id,
                    "subject_name": subj.canonical_name,
                    "subject_type": subj.entity_type,
                    "predicate": rel.predicate,
                    "object_id": obj.id,
                    "object_name": obj.canonical_name,
                    "object_type": obj.entity_type,
                    "confidence": rel.confidence,
                })

                next_layer.add(rel.subject_entity_id)
                next_layer.add(rel.object_entity_id)
                count += 1

            visited.update(current)
            current = next_layer - visited

        return relations_result

    def get_relation_evidence(self, relation_ids: list[str]) -> list[dict]:
        """获取关系的原文证据。"""
        if not relation_ids:
            return []
        evidence = self.db.scalars(
            select(KgRelationEvidence).where(
                KgRelationEvidence.relation_id.in_(relation_ids)
            )
        ).all()
        return [
            {
                "relation_id": ev.relation_id,
                "document_id": ev.document_id,
                "chunk_id": ev.chunk_id,
                "quote": ev.quote,
                "confidence": ev.confidence,
            }
            for ev in evidence
        ]

    def cleanup_orphans(self) -> None:
        """清理无提及、无关系的孤立实体和无证据关系。"""
        # Delete relations without evidence
        relations_with_evidence = self.db.scalars(
            select(KgRelationEvidence.relation_id).distinct()
        ).all()
        if relations_with_evidence:
            self.db.execute(
                delete(KgRelation).where(
                    KgRelation.id.notin_(relations_with_evidence)
                )
            )
        else:
            self.db.execute(delete(KgRelation))

        # Delete entities without mentions and without relations
        entities_with_mentions = self.db.scalars(
            select(KgEntityMention.entity_id).distinct()
        ).all()
        entities_in_relations = set(
            list(self.db.scalars(select(KgRelation.subject_entity_id).distinct())) +
            list(self.db.scalars(select(KgRelation.object_entity_id).distinct()))
        )
        keep_ids = set(entities_with_mentions) | entities_in_relations

        all_entities = self.db.scalars(select(KgEntity)).all()
        for ent in all_entities:
            if ent.id not in keep_ids:
                self.db.delete(ent)

        self.db.commit()

    def stats(self) -> dict:
        return {
            "entities": self.db.scalar(select(func.count(KgEntity.id))) or 0,
            "aliases": self.db.scalar(select(func.count(KgAlias.id))) or 0,
            "mentions": self.db.scalar(select(func.count(KgEntityMention.id))) or 0,
            "relations": self.db.scalar(select(func.count(KgRelation.id))) or 0,
            "evidence": self.db.scalar(select(func.count(KgRelationEvidence.id))) or 0,
        }
