"""Schema registry, graph quality gates, review queue, merges, and hierarchy."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import (
    GRAPH_LOW_CONFIDENCE_REVIEW_THRESHOLD,
    GRAPH_ENTITY_MERGE_SIMILARITY,
    GRAPH_MERGE_CANDIDATE_LIMIT,
    GRAPH_RELATED_TO_MAX_RATIO,
    GRAPH_SCHEMA_VERSION,
    INDEX_JOB_MAX_ATTEMPTS,
)
from app.models.graph_governance import (
    GraphCommunity,
    GraphCommunityMember,
    GraphExtractionCache,
    GraphQualityReport,
    GraphReviewItem,
    GraphSchemaDefinition,
    KgEntityMerge,
)
from app.models.index_job import IndexJob
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_graph import KgAlias, KgEntity, KgEntityMention, KgRelation, KgRelationEvidence
from app.repositories.index_job_repository import IndexJobRepository
from rag.knowledge_graph.schemas import ChunkExtraction, VALID_ENTITY_TYPES, VALID_PREDICATES


class GraphGovernanceService:
    def __init__(self, db: Session, knowledge_base_id: str, task_queue=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if task_queue is None:
            from app.bootstrap.container import get_provider_container
            task_queue = get_provider_container().task_queue
        self.task_queue = task_queue

    def list_schemas(self) -> list[GraphSchemaDefinition]:
        return list(self.db.scalars(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id
        ).order_by(GraphSchemaDefinition.version.desc())))

    def active_schema(self, *, create_default: bool = True) -> GraphSchemaDefinition | None:
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1))
        if schema is None and create_default:
            schema = GraphSchemaDefinition(
                knowledge_base_id=self.knowledge_base_id,
                version=GRAPH_SCHEMA_VERSION,
                status="active",
                entity_types_json=sorted(VALID_ENTITY_TYPES),
                relation_types_json=sorted(VALID_PREDICATES),
                normalization_rules_json={
                    "unicode": "NFKC", "case": "lower_ascii", "trim_punctuation": True,
                },
                activated_at=datetime.utcnow(),
            )
            self.db.add(schema)
            self.db.commit()
            self.db.refresh(schema)
        return schema

    def create_schema(
        self, *, entity_types: list[str], relation_types: list[str],
        normalization_rules: dict, extraction_prompt: str | None, created_by: str,
    ) -> GraphSchemaDefinition:
        unknown_entities = set(entity_types) - set(VALID_ENTITY_TYPES)
        unknown_relations = set(relation_types) - set(VALID_PREDICATES)
        if unknown_entities or unknown_relations:
            raise ValueError(
                f"Schema 包含未注册类型：{sorted(unknown_entities | unknown_relations)}"
            )
        latest = self.db.scalar(select(func.max(GraphSchemaDefinition.version)).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id
        )) or 0
        schema = GraphSchemaDefinition(
            knowledge_base_id=self.knowledge_base_id,
            version=max(int(latest) + 1, GRAPH_SCHEMA_VERSION),
            status="draft",
            entity_types_json=sorted(set(entity_types)),
            relation_types_json=sorted(set(relation_types)),
            normalization_rules_json=normalization_rules,
            extraction_prompt=extraction_prompt,
            created_by=created_by,
        )
        self.db.add(schema)
        self.db.commit()
        self.db.refresh(schema)
        return schema

    def activate_schema(self, schema_id: str) -> GraphSchemaDefinition | None:
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.id == schema_id,
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
        ))
        if schema is None:
            return None
        self.db.execute(update(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.status == "active",
        ).values(status="superseded"))
        schema.status = "active"
        schema.activated_at = datetime.utcnow()
        self.db.execute(update(KnowledgeChunk).where(
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
            KnowledgeChunk.graph_schema_version != schema.version,
        ).values(graph_status="pending"))
        self.db.commit()
        self.db.refresh(schema)
        return schema

    def request_build(self, mode: str, requested_by: str | None, options: dict | None = None) -> IndexJob:
        if mode not in {"pending", "retry_failed", "full"}:
            raise ValueError("无效的图谱构建模式")
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("graph_rebuild")
        if existing is not None:
            return existing
        job = repository.create(IndexJob(
            job_type="graph_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            options_json={"mode": mode, **(options or {})},
        ))
        self.task_queue.enqueue(job.id)
        return job

    def latest_quality(self) -> GraphQualityReport | None:
        return self.db.scalar(select(GraphQualityReport).where(
            GraphQualityReport.knowledge_base_id == self.knowledge_base_id
        ).order_by(GraphQualityReport.created_at.desc()).limit(1))

    def assess_quality(self, created_by: str | None = None) -> GraphQualityReport:
        entities = list(self.db.scalars(select(KgEntity).where(
            KgEntity.knowledge_base_id == self.knowledge_base_id
        )))
        relations = list(self.db.scalars(select(KgRelation).where(
            KgRelation.knowledge_base_id == self.knowledge_base_id
        )))
        evidence_counts = dict(self.db.execute(
            select(KgRelationEvidence.relation_id, func.count(KgRelationEvidence.id))
            .where(KgRelationEvidence.knowledge_base_id == self.knowledge_base_id)
            .group_by(KgRelationEvidence.relation_id)
        ).all())
        evidence_backed = sum(1 for relation in relations if evidence_counts.get(relation.id, 0))
        low_confidence = [
            relation for relation in relations
            if relation.confidence < GRAPH_LOW_CONFIDENCE_REVIEW_THRESHOLD
        ]
        related_to = sum(1 for relation in relations if relation.predicate == "RELATED_TO")
        cross_kb = int(self.db.scalar(select(func.count(KgRelation.id)).join(
            KgEntity, KgEntity.id == KgRelation.subject_entity_id
        ).where(
            KgRelation.knowledge_base_id == self.knowledge_base_id,
            KgEntity.knowledge_base_id != self.knowledge_base_id,
        )) or 0)
        by_normalized: dict[str, list[KgEntity]] = defaultdict(list)
        for entity in entities:
            by_normalized[entity.normalized_name].append(entity)
        duplicate_groups = [items for items in by_normalized.values() if len(items) > 1]
        status_counts = dict(self.db.execute(
            select(KnowledgeChunk.graph_status, func.count(KnowledgeChunk.id))
            .where(KnowledgeChunk.knowledge_base_id == self.knowledge_base_id)
            .group_by(KnowledgeChunk.graph_status)
        ).all())
        relation_count = len(relations)
        evidence_coverage = evidence_backed / relation_count if relation_count else 1.0
        related_ratio = related_to / relation_count if relation_count else 0.0
        metrics = {
            "entities": len(entities), "relations": relation_count,
            "evidence_coverage": round(evidence_coverage, 6),
            "relations_without_evidence": relation_count - evidence_backed,
            "low_confidence_relations": len(low_confidence),
            "related_to_ratio": round(related_ratio, 6),
            "duplicate_entity_groups": len(duplicate_groups),
            "cross_knowledge_base_relations": cross_kb,
            "chunk_status": status_counts,
        }
        gates = {
            "evidence_coverage": {"passed": evidence_coverage >= 0.95, "minimum": 0.95},
            "related_to_ratio": {"passed": related_ratio <= GRAPH_RELATED_TO_MAX_RATIO, "maximum": GRAPH_RELATED_TO_MAX_RATIO},
            "cross_knowledge_base": {"passed": cross_kb == 0, "maximum": 0},
            "stale_processing": {"passed": status_counts.get("processing", 0) == 0, "maximum": 0},
        }
        status = "ready" if all(item["passed"] for item in gates.values()) else "warning"
        graph_version = hashlib.sha256("\n".join(
            f"{relation.id}:{relation.updated_at}:{relation.confidence}"
            for relation in relations
        ).encode()).hexdigest()
        report = GraphQualityReport(
            knowledge_base_id=self.knowledge_base_id,
            graph_version=graph_version,
            status=status,
            metrics_json=metrics,
            gate_results_json=gates,
            created_by=created_by,
        )
        self.db.add(report)
        self._populate_review_queue(low_confidence, duplicate_groups)
        self.db.commit()
        self.db.refresh(report)
        return report

    def _populate_review_queue(self, low_confidence, duplicate_groups) -> None:
        existing = set(self.db.scalars(select(GraphReviewItem.target_id).where(
            GraphReviewItem.knowledge_base_id == self.knowledge_base_id,
            GraphReviewItem.status == "pending",
        )))
        for relation in low_confidence:
            if relation.id in existing:
                continue
            self.db.add(GraphReviewItem(
                knowledge_base_id=self.knowledge_base_id,
                item_type="low_confidence_relation",
                target_id=relation.id,
                payload_json={
                    "subject_entity_id": relation.subject_entity_id,
                    "predicate": relation.predicate,
                    "object_entity_id": relation.object_entity_id,
                },
                reason="关系置信度低于人工复核阈值",
                confidence=relation.confidence,
            ))
        for group in duplicate_groups:
            pair = sorted(entity.id for entity in group)[:2]
            key = "merge:" + ":".join(pair)
            if key in existing:
                continue
            self.db.add(GraphReviewItem(
                knowledge_base_id=self.knowledge_base_id,
                item_type="entity_merge_candidate",
                target_id=key,
                payload_json={"entity_ids": pair, "normalized_name": group[0].normalized_name},
                reason="标准化名称相同，实体类型不同",
                confidence=0.95,
            ))

    def list_reviews(self, status: str = "pending") -> list[GraphReviewItem]:
        return list(self.db.scalars(select(GraphReviewItem).where(
            GraphReviewItem.knowledge_base_id == self.knowledge_base_id,
            GraphReviewItem.status == status,
        ).order_by(GraphReviewItem.created_at.desc())))

    def resolve_review(self, review_id: str, decision: str, reviewed_by: str) -> GraphReviewItem | None:
        item = self.db.scalar(select(GraphReviewItem).where(
            GraphReviewItem.id == review_id,
            GraphReviewItem.knowledge_base_id == self.knowledge_base_id,
        ))
        if item is None:
            return None
        item.status = decision
        item.reviewed_by = reviewed_by
        item.reviewed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(item)
        return item

    def scan_semantic_merge_candidates(self) -> dict:
        """Use the active entity-vector index as a candidate filter, never as an auto-merge."""
        from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
        from app.services.entity_vector_service import EntityVectorService

        repository = KnowledgeGraphRepository(self.db, self.knowledge_base_id)
        entities = repository.list_entities(limit=GRAPH_MERGE_CANDIDATE_LIMIT)
        vector_service = EntityVectorService(self.db, self.knowledge_base_id)
        existing = set(self.db.scalars(select(GraphReviewItem.target_id).where(
            GraphReviewItem.knowledge_base_id == self.knowledge_base_id,
            GraphReviewItem.status == "pending",
        )))
        created = 0
        considered: set[tuple[str, str]] = set()
        for entity in entities:
            for candidate in vector_service.semantic_search(entity.canonical_name, limit=5):
                candidate_id = str(candidate.get("entity_id", ""))
                score = float(candidate.get("match_score", 0))
                if not candidate_id or candidate_id == entity.id or score < GRAPH_ENTITY_MERGE_SIMILARITY:
                    continue
                other = repository.get_entity(candidate_id)
                if other is None or other.entity_type != entity.entity_type:
                    continue
                # Version numbers and explicit identifiers are conservative blockers.
                if any(char.isdigit() for char in entity.canonical_name + other.canonical_name) and entity.normalized_name != other.normalized_name:
                    continue
                pair = tuple(sorted((entity.id, candidate_id)))
                if pair in considered:
                    continue
                considered.add(pair)
                target = "merge:" + ":".join(pair)
                if target in existing:
                    continue
                self.db.add(GraphReviewItem(
                    knowledge_base_id=self.knowledge_base_id,
                    item_type="semantic_entity_merge_candidate",
                    target_id=target,
                    payload_json={
                        "entity_ids": list(pair),
                        "names": [entity.canonical_name, other.canonical_name],
                        "entity_type": entity.entity_type,
                        "similarity": score,
                    },
                    reason="实体向量相似度达到候选阈值，需人工确认",
                    confidence=score,
                ))
                existing.add(target)
                created += 1
        self.db.commit()
        return {"scanned": len(entities), "candidates": created, "threshold": GRAPH_ENTITY_MERGE_SIMILARITY}

    def list_merges(self) -> list[KgEntityMerge]:
        return list(self.db.scalars(select(KgEntityMerge).where(
            KgEntityMerge.knowledge_base_id == self.knowledge_base_id
        ).order_by(KgEntityMerge.created_at.desc())))

    def merge_entities(self, canonical_id: str, merged_id: str, reason: str, created_by: str) -> KgEntityMerge:
        if canonical_id == merged_id:
            raise ValueError("实体不能与自身合并")
        entities = list(self.db.scalars(select(KgEntity).where(
            KgEntity.knowledge_base_id == self.knowledge_base_id,
            KgEntity.id.in_([canonical_id, merged_id]),
        )))
        if len(entities) != 2:
            raise ValueError("合并实体不存在或不属于当前知识库")
        if self.db.scalar(select(KgEntityMerge.id).where(
            KgEntityMerge.knowledge_base_id == self.knowledge_base_id,
            KgEntityMerge.canonical_entity_id == merged_id,
            KgEntityMerge.merged_entity_id == canonical_id,
            KgEntityMerge.status == "active",
        )):
            raise ValueError("合并会形成循环")
        record = self.db.scalar(select(KgEntityMerge).where(
            KgEntityMerge.knowledge_base_id == self.knowledge_base_id,
            KgEntityMerge.merged_entity_id == merged_id,
        ))
        if record is None:
            record = KgEntityMerge(
                knowledge_base_id=self.knowledge_base_id,
                canonical_entity_id=canonical_id,
                merged_entity_id=merged_id,
                reason=reason.strip(),
                status="active",
                created_by=created_by,
            )
            self.db.add(record)
        else:
            record.canonical_entity_id = canonical_id
            record.reason = reason.strip()
            record.status = "active"
            record.created_by = created_by
            record.reverted_by = None
            record.reverted_at = None
        self.db.commit()
        self.db.refresh(record)
        return record

    def revert_merge(self, merge_id: str, reverted_by: str) -> KgEntityMerge | None:
        record = self.db.scalar(select(KgEntityMerge).where(
            KgEntityMerge.id == merge_id,
            KgEntityMerge.knowledge_base_id == self.knowledge_base_id,
        ))
        if record is None:
            return None
        record.status = "reverted"
        record.reverted_by = reverted_by
        record.reverted_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record

    def rebuild_communities(self) -> dict:
        entities = list(self.db.scalars(select(KgEntity).where(
            KgEntity.knowledge_base_id == self.knowledge_base_id
        )))
        relations = list(self.db.scalars(select(KgRelation).where(
            KgRelation.knowledge_base_id == self.knowledge_base_id
        )))
        adjacency: dict[str, set[str]] = {entity.id: set() for entity in entities}
        relation_by_pair: dict[frozenset[str], list[str]] = defaultdict(list)
        for relation in relations:
            adjacency.setdefault(relation.subject_entity_id, set()).add(relation.object_entity_id)
            adjacency.setdefault(relation.object_entity_id, set()).add(relation.subject_entity_id)
            relation_by_pair[frozenset((relation.subject_entity_id, relation.object_entity_id))].append(relation.id)
        components: list[list[str]] = []
        remaining = set(adjacency)
        while remaining:
            seed = min(remaining)
            queue = deque([seed])
            component = []
            remaining.remove(seed)
            while queue:
                current = queue.popleft()
                component.append(current)
                for neighbor in sorted(adjacency[current] & remaining):
                    remaining.remove(neighbor)
                    queue.append(neighbor)
            components.append(component)
        self.db.execute(delete(GraphCommunityMember).where(
            GraphCommunityMember.knowledge_base_id == self.knowledge_base_id
        ))
        self.db.execute(delete(GraphCommunity).where(
            GraphCommunity.knowledge_base_id == self.knowledge_base_id
        ))
        self.db.flush()
        by_id = {entity.id: entity for entity in entities}
        root = None
        if components:
            root = GraphCommunity(
                knowledge_base_id=self.knowledge_base_id,
                level=1,
                title="知识库全局图谱",
                summary=f"包含 {len(entities)} 个实体、{len(relations)} 条关系、{len(components)} 个主题子图。",
                evidence_json=[relation.id for relation in relations[:100]],
            )
            self.db.add(root)
            self.db.flush()
        for index, component in enumerate(sorted(components, key=lambda values: (-len(values), values[0])), start=1):
            names = [by_id[item].canonical_name for item in component if item in by_id]
            evidence = sorted({
                relation_id
                for entity_id in component
                for neighbor in adjacency[entity_id]
                for relation_id in relation_by_pair[frozenset((entity_id, neighbor))]
            })[:100]
            community = GraphCommunity(
                knowledge_base_id=self.knowledge_base_id,
                parent_id=root.id if root else None,
                level=0,
                title=" / ".join(names[:3]) or f"主题 {index}",
                summary=f"包含 {len(component)} 个实体：" + "、".join(names[:20]),
                evidence_json=evidence,
            )
            self.db.add(community)
            self.db.flush()
            self.db.add_all([
                GraphCommunityMember(
                    knowledge_base_id=self.knowledge_base_id,
                    community_id=community.id,
                    entity_id=entity_id,
                ) for entity_id in component
            ])
        self.db.commit()
        return {"communities": len(components), "entities": len(entities)}

    def list_communities(self) -> list[dict]:
        communities = list(self.db.scalars(select(GraphCommunity).where(
            GraphCommunity.knowledge_base_id == self.knowledge_base_id
        ).order_by(GraphCommunity.level.desc(), GraphCommunity.title)))
        result = []
        for community in communities:
            members = list(self.db.execute(
                select(KgEntity.id, KgEntity.canonical_name, KgEntity.entity_type)
                .join(GraphCommunityMember, GraphCommunityMember.entity_id == KgEntity.id)
                .where(GraphCommunityMember.community_id == community.id)
                .order_by(KgEntity.canonical_name)
            ).all())
            result.append({
                "id": community.id, "parent_id": community.parent_id,
                "level": community.level, "title": community.title,
                "summary": community.summary, "evidence_json": community.evidence_json,
                "members": [
                    {"id": entity_id, "name": name, "entity_type": entity_type}
                    for entity_id, name, entity_type in members
                ],
            })
        return result

    @staticmethod
    def extraction_cache_key(batch: list[dict], schema_version: int, model_fingerprint: str) -> str:
        payload = {
            "schema": schema_version, "model": model_fingerprint,
            "chunks": [{"id": item["chunk_id"], "content": item["content"]} for item in batch],
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def get_cached_extractions(self, cache_key: str) -> list[ChunkExtraction] | None:
        item = self.db.scalar(select(GraphExtractionCache).where(
            GraphExtractionCache.knowledge_base_id == self.knowledge_base_id,
            GraphExtractionCache.cache_key == cache_key,
        ))
        if item is None:
            return None
        item.hit_count += 1
        item.last_used_at = datetime.utcnow()
        self.db.commit()
        return [ChunkExtraction.model_validate(value) for value in item.payload_json.get("chunks", [])]

    def put_cached_extractions(
        self, cache_key: str, schema_version: int, model_fingerprint: str,
        extractions: list[ChunkExtraction],
    ) -> None:
        item = GraphExtractionCache(
            knowledge_base_id=self.knowledge_base_id,
            cache_key=cache_key,
            schema_version=schema_version,
            model_fingerprint=model_fingerprint,
            payload_json={"chunks": [value.model_dump() for value in extractions]},
        )
        self.db.add(item)
        self.db.commit()
