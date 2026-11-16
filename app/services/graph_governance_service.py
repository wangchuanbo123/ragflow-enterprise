"""Schema registry, graph quality gates, review queue, merges, and hierarchy."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import (
    GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS,
    GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS,
    GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS,
    GRAPH_AUTO_BUILD_PROFILE,
    GRAPH_EXTRACTION_BATCH_SIZE,
    GRAPH_LOW_CONFIDENCE_REVIEW_THRESHOLD,
    GRAPH_ENTITY_MERGE_SIMILARITY,
    GRAPH_AUTO_BUILD_ENABLED,
    GRAPH_MERGE_CANDIDATE_LIMIT,
    GRAPH_PARALLEL_WORKERS,
    GRAPH_RELATED_TO_MAX_RATIO,
    GRAPH_SCHEMA_AUTO_GENERATE_ENABLED,
    GRAPH_SCHEMA_VERSION,
    INDEX_JOB_MAX_ATTEMPTS,
)
from app.models.graph_governance import (
    GraphCommunity,
    GraphCommunityMember,
    GraphCommunityState,
    GraphExtractionCache,
    GraphQualityReport,
    GraphReviewItem,
    GraphSchemaDefinition,
    KgEntityMerge,
)
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import KgAlias, KgEntity, KgEntityMention, KgRelation, KgRelationEvidence
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
from app.services.knowledge_mutation import knowledge_mutation_lock, serialized_knowledge_mutation
from rag.knowledge_graph.schemas import ChunkExtraction, VALID_ENTITY_TYPES, VALID_PREDICATES
from rag.knowledge_graph.profiles import (
    profile_catalog,
)

GRAPH_KIND_ORDER = ("general",)
VALID_GRAPH_KINDS = set(GRAPH_KIND_ORDER)


class GraphGovernanceService:
    def __init__(self, db: Session, knowledge_base_id: str, task_queue=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if task_queue is None:
            from app.bootstrap.container import get_provider_container
            task_queue = get_provider_container().task_queue
        self.task_queue = task_queue

    def create_correction(
        self, *, action: str, target_id: str | None, payload: dict,
        reason: str, created_by: str,
    ):
        """Persist a governance edit, then refresh every derived retrieval view."""
        from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

        correction = KnowledgeGraphRepository(
            self.db, self.knowledge_base_id
        ).create_correction(
            action=action,
            target_id=target_id,
            payload=payload,
            reason=reason,
            created_by=created_by,
        )
        self._queue_derived_refresh(created_by)
        return correction

    def revert_correction(self, correction_id: str, reverted_by: str):
        from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

        correction = KnowledgeGraphRepository(
            self.db, self.knowledge_base_id
        ).revert_correction(correction_id, reverted_by)
        if correction is not None:
            self._queue_derived_refresh(reverted_by)
        return correction

    def _queue_derived_refresh(self, requested_by: str | None) -> None:
        """Keep vector search and communities consistent without blocking the API."""
        from app.services.entity_vector_service import EntityVectorService
        from app.services.relation_vector_service import RelationVectorService

        self.mark_communities_stale()
        if not callable(getattr(self.task_queue, "enqueue", None)):
            return
        EntityVectorService(self.db, self.knowledge_base_id).request_rebuild(
            requested_by, task_queue=self.task_queue
        )
        RelationVectorService(self.db, self.knowledge_base_id).request_rebuild(
            requested_by, task_queue=self.task_queue
        )
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        maintenance = repository.find_open("graph_maintenance")
        if maintenance is None or maintenance.status == "running":
            job = repository.create(IndexJob(
                job_type="graph_maintenance",
                status="pending",
                requested_by=requested_by,
                total_items=1,
                max_attempts=INDEX_JOB_MAX_ATTEMPTS,
                options_json={"trigger": "graph_governance_edit"},
            ))
            signal_persisted_job(self.task_queue, job.id)

    def list_schemas(self, graph_kind: str | None = None, *, include_legacy: bool = False) -> list[GraphSchemaDefinition]:
        stmt = select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id
        )
        if graph_kind:
            if graph_kind != "general" and not include_legacy:
                self._validate_graph_kind(graph_kind)
            stmt = stmt.where(GraphSchemaDefinition.graph_kind == graph_kind)
        elif not include_legacy:
            stmt = stmt.where(GraphSchemaDefinition.graph_kind == "general")
        return list(self.db.scalars(stmt.order_by(GraphSchemaDefinition.version.desc())))

    def list_graph_profiles(self) -> list[dict]:
        """Return profile contracts plus live graph and active-Schema coverage."""
        result: list[dict] = []
        for profile in profile_catalog():
            kind = profile["key"]
            active = self.active_schema(graph_kind=kind, create_default=False)
            active_entities = set(active.entity_types_json or []) if active else set()
            active_relations = set(active.relation_types_json or []) if active else set()
            if active:
                from rag.knowledge_graph.profiles import profiles_for_schema
                summary = active.generation_summary_json or {}
                profile = profiles_for_schema(active.entity_types_json, active.relation_types_json, summary.get("entity_type_details"), summary.get("relation_type_details"))["general"]
            entity_types = {item["name"] for item in profile["entity_types"]}
            relation_types = {item["name"] for item in profile["relation_types"]}
            entity_filters = [KgEntity.knowledge_base_id == self.knowledge_base_id]
            relation_filters = [KgRelation.knowledge_base_id == self.knowledge_base_id]
            if kind != "general":
                entity_filters.append(KgEntity.entity_type.in_(entity_types))
                relation_filters.append(KgRelation.predicate.in_(relation_types))
            entity_count = self.db.scalar(select(func.count(KgEntity.id)).where(*entity_filters)) or 0
            relation_count = self.db.scalar(select(func.count(KgRelation.id)).where(*relation_filters)) or 0
            result.append({
                **profile,
                "entity_count": int(entity_count),
                "relation_count": int(relation_count),
                "schema_entity_coverage": (
                    len(active_entities) if kind == "general" else len(entity_types & active_entities)
                ),
                "schema_relation_coverage": (
                    len(active_relations) if kind == "general" else len(relation_types & active_relations)
                ),
            })
        return result

    @staticmethod
    def _validate_graph_kind(graph_kind: str) -> str:
        if graph_kind not in VALID_GRAPH_KINDS:
            raise ValueError("仅支持知识库总图谱 general，独立专业图谱已停用")
        return graph_kind

    def _next_schema_version(self) -> int:
        latest = self.db.scalar(select(func.max(GraphSchemaDefinition.version)).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id
        )) or 0
        return max(int(latest) + 1, GRAPH_SCHEMA_VERSION)

    @staticmethod
    def _validate_schema_types(entity_types: list[str], relation_types: list[str]) -> tuple[list[str], list[str]]:
        pattern = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
        entities = sorted({item.strip().upper() for item in entity_types if item.strip()})
        relations = sorted({item.strip().upper() for item in relation_types if item.strip()})
        if not entities or not relations:
            raise ValueError("Schema 至少需要一个实体类型和一个关系类型")
        invalid = [item for item in [*entities, *relations] if not pattern.fullmatch(item)]
        if invalid:
            raise ValueError(f"Schema 类型必须是 1-32 位大写字母、数字或下划线：{invalid}")
        if len(entities) > 500 or len(relations) > 500:
            raise ValueError("实体类型和关系类型分别不能超过 500 个")
        return entities, relations

    def active_schema(
        self, *, graph_kind: str = "general", create_default: bool = True
    ) -> GraphSchemaDefinition | None:
        self._validate_graph_kind(graph_kind)
        schema = self._read_active_schema(graph_kind)
        # MCP and startup must be able to read existing contracts while a
        # worker owns this KB's write lock across its agent/tool round trips.
        if schema is not None or not create_default:
            return schema
        with knowledge_mutation_lock(self.knowledge_base_id):
            # Another API/startup may have initialized this kind while we
            # waited. Allocate a version only after checking under the lock.
            schema = self._read_active_schema(graph_kind)
            if schema is not None:
                return schema
            return self._create_default_schema(graph_kind)

    def _read_active_schema(self, graph_kind: str) -> GraphSchemaDefinition | None:
        return self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.graph_kind == graph_kind,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1).execution_options(populate_existing=True))

    def _create_default_schema(self, graph_kind: str) -> GraphSchemaDefinition:
        """Called only inside the KB write lock, after the second existence check."""
        contract = {
            "entity_types": sorted(VALID_ENTITY_TYPES),
            "relation_types": sorted(VALID_PREDICATES),
            "graph_profiles": None,
            "extraction_prompt": None,
        }
        schema = GraphSchemaDefinition(
            knowledge_base_id=self.knowledge_base_id,
            graph_kind=graph_kind,
            version=self._next_schema_version(),
            status="active",
            entity_types_json=contract["entity_types"],
            relation_types_json=contract["relation_types"],
            graph_profiles_json=contract["graph_profiles"],
            extraction_prompt=contract["extraction_prompt"],
            generation_method="manual",
            normalization_rules_json={
                "unicode": "NFKC", "case": "lower_ascii", "trim_punctuation": True,
            },
            activated_at=datetime.utcnow(),
        )
        self.db.add(schema)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            schema = self._read_active_schema(graph_kind)
            if schema is None:
                raise
        self.db.refresh(schema)
        return schema

    def ensure_total_schema(self):
        """Repair legacy seed/restore rows only when necessary, under the KB lock."""
        rows = list(self.db.scalars(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.status == "active")))
        if len(rows) == 1 and rows[0].graph_kind == "general" and not rows[0].graph_profiles_json:
            return rows[0]
        if rows:
            from app.services.schema_consolidation import consolidate_connection
            with knowledge_mutation_lock(self.knowledge_base_id):
                consolidate_connection(self.db.connection(), self.knowledge_base_id)
                self.db.commit()
                self.db.expire_all()
        return self.active_schema()

    def extraction_schema(self) -> SimpleNamespace:
        """Detached snapshot of the sole active total Schema, safe for worker threads."""
        schema = self.active_schema()
        return SimpleNamespace(**{name: deepcopy(getattr(schema, name)) for name in (
            "id", "version", "entity_types_json", "relation_types_json", "normalization_rules_json",
            "extraction_prompt", "graph_profiles_json", "generation_summary_json")})

    @serialized_knowledge_mutation
    def create_schema(
        self, *, entity_types: list[str], relation_types: list[str],
        normalization_rules: dict, extraction_prompt: str | None, created_by: str | None,
        generation_method: str = "manual",
        source_document_ids: list[str] | None = None,
        generation_summary: dict | None = None,
        graph_profiles: dict | None = None,
        graph_kind: str = "general",
    ) -> GraphSchemaDefinition:
        self._validate_graph_kind(graph_kind)
        if graph_profiles:
            raise ValueError("独立专业图谱定义已停用，请在总Schema中定义类型与关系")
        entity_types, relation_types = self._validate_schema_types(
            entity_types, relation_types
        )
        schema = GraphSchemaDefinition(
            knowledge_base_id=self.knowledge_base_id,
            graph_kind=graph_kind,
            version=self._next_schema_version(),
            status="draft",
            entity_types_json=entity_types,
            relation_types_json=relation_types,
            graph_profiles_json=graph_profiles or None,
            normalization_rules_json=normalization_rules,
            extraction_prompt=extraction_prompt,
            created_by=created_by,
            generation_method=generation_method,
            source_document_ids_json=source_document_ids,
            generation_summary_json=generation_summary,
        )
        self.db.add(schema)
        self.db.commit()
        self.db.refresh(schema)
        return schema

    @serialized_knowledge_mutation
    def update_schema(
        self, schema_id: str, *, entity_types: list[str], relation_types: list[str],
        normalization_rules: dict, extraction_prompt: str | None,
        graph_profiles: dict | None = None,
    ) -> GraphSchemaDefinition | None:
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.id == schema_id,
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
        ).execution_options(populate_existing=True))
        if schema is None:
            return None
        self._validate_graph_kind(schema.graph_kind)
        if graph_profiles:
            raise ValueError("独立专业图谱定义已停用")
        if schema.status != "draft":
            raise ValueError("仅草稿 Schema 可以直接修改；请复制活动版本创建新草稿")
        entities, relations = self._validate_schema_types(entity_types, relation_types)
        schema.entity_types_json = entities
        schema.relation_types_json = relations
        if graph_profiles is not None:
            schema.graph_profiles_json = graph_profiles or None
        schema.normalization_rules_json = normalization_rules
        schema.extraction_prompt = extraction_prompt
        self.db.commit()
        self.db.refresh(schema)
        return schema

    def automation_settings(self) -> dict:
        knowledge_base = self.db.get(KnowledgeBase, self.knowledge_base_id)
        settings = (knowledge_base.settings_json or {}) if knowledge_base else {}
        return {
            "auto_build_enabled": bool(
                settings.get("graph_auto_build_enabled", GRAPH_AUTO_BUILD_ENABLED)
            ),
            "auto_schema_generation_enabled": bool(
                settings.get(
                    "graph_schema_auto_generate_enabled",
                    GRAPH_SCHEMA_AUTO_GENERATE_ENABLED,
                )
            ),
        }

    def update_automation_settings(
        self,
        *,
        auto_build_enabled: bool,
        auto_schema_generation_enabled: bool | None = None,
    ) -> dict:
        knowledge_base = self.db.get(KnowledgeBase, self.knowledge_base_id)
        if knowledge_base is None:
            raise ValueError("知识库不存在")
        settings = dict(knowledge_base.settings_json or {})
        settings["graph_auto_build_enabled"] = auto_build_enabled
        if auto_schema_generation_enabled is None:
            auto_schema_generation_enabled = bool(
                settings.get(
                    "graph_schema_auto_generate_enabled",
                    GRAPH_SCHEMA_AUTO_GENERATE_ENABLED,
                )
            )
        settings["graph_schema_auto_generate_enabled"] = auto_schema_generation_enabled
        knowledge_base.settings_json = settings
        self.db.commit()
        return {
            "auto_build_enabled": auto_build_enabled,
            "auto_schema_generation_enabled": auto_schema_generation_enabled,
        }

    @staticmethod
    def schema_change_kind(
        previous: GraphSchemaDefinition | None, schema: GraphSchemaDefinition
    ) -> str:
        """Classify a schema activation so rebuild cost matches the actual change."""
        if previous is None:
            return "initial"
        old_entities = set(previous.entity_types_json or [])
        new_entities = set(schema.entity_types_json or [])
        old_relations = set(previous.relation_types_json or [])
        new_relations = set(schema.relation_types_json or [])
        removed = (old_entities - new_entities) | (old_relations - new_relations)
        added = (new_entities - old_entities) | (new_relations - old_relations)
        if (previous.normalization_rules_json or {}) != (schema.normalization_rules_json or {}):
            return "normalization_change"
        if old_entities or old_relations:
            overlap = (
                len((old_entities & new_entities) | (old_relations & new_relations))
                / len(old_entities | old_relations)
            )
            if overlap <= 0.5:
                return "domain_change"
        if removed and added:
            return "domain_change"
        if removed:
            return "type_removal"
        if added:
            return "type_addition"
        if (previous.extraction_prompt or "") != (schema.extraction_prompt or ""):
            return "prompt_change"
        if (previous.graph_profiles_json or {}) != (schema.graph_profiles_json or {}):
            return "prompt_change"
        return "unchanged"

    def create_profile_schema(self, *, graph_kind: str, created_by: str | None):
        raise ValueError("独立专业图谱模板已停用，请生成或编辑知识库总Schema")

    def _new_type_tokens(self, schema: GraphSchemaDefinition) -> list[str]:
        previous = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.graph_kind == schema.graph_kind,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1))
        if previous is None:
            return []
        added_entities = set(schema.entity_types_json or []) - set(previous.entity_types_json or [])
        added_relations = set(schema.relation_types_json or []) - set(previous.relation_types_json or [])
        tokens: set[str] = set()
        for item in added_entities | added_relations:
            lowered = item.lower()
            tokens.add(lowered)
            tokens.update(part for part in lowered.split("_") if len(part) >= 3)
        return sorted(tokens)

    def _chunks_matching_tokens(self, tokens: list[str]) -> tuple[int, int]:
        """Cheap content match for chunks that may contain the new types."""
        if not tokens:
            return 0, 0
        rows = self.db.execute(
            select(KnowledgeChunk.document_id, KnowledgeChunk.id).where(
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                or_(*[
                    KnowledgeChunk.content.ilike(f"%{token}%")
                    for token in tokens
                ]),
            )
        ).all()
        return len({row[0] for row in rows}), len(rows)

    def estimate_schema_rebuild(self, schema_id: str) -> dict | None:
        """Predict the rebuild impact of activating a draft schema."""
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.id == schema_id,
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
        ))
        if schema is None:
            return None
        previous = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.graph_kind == schema.graph_kind,
            GraphSchemaDefinition.status == "active",
        ).order_by(GraphSchemaDefinition.version.desc()).limit(1))
        kind = self.schema_change_kind(previous, schema)
        total_documents = int(self.db.scalar(select(func.count(KnowledgeDocument.id)).where(
            KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
        )) or 0)
        total_chunks = int(self.db.scalar(select(func.count(KnowledgeChunk.id)).where(
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
        )) or 0)
        if kind in {"normalization_change", "domain_change", "initial"}:
            affected_documents, affected_chunks = total_documents, total_chunks
        elif kind == "type_addition":
            affected_documents, affected_chunks = self._chunks_matching_tokens(
                self._new_type_tokens(schema)
            )
        elif kind == "type_removal" and previous is not None:
            removed_entities = set(previous.entity_types_json or []) - set(schema.entity_types_json or [])
            removed_relations = set(previous.relation_types_json or []) - set(schema.relation_types_json or [])
            return {
                "change_kind": kind,
                "affected_documents": 0,
                "affected_chunks": 0,
                "estimated_llm_requests": 0,
                "estimated_minutes": 0,
                "removed_entity_types": sorted(removed_entities),
                "removed_relation_types": sorted(removed_relations),
            }
        else:
            affected_documents, affected_chunks = 0, 0
        batches = math.ceil(affected_chunks / GRAPH_EXTRACTION_BATCH_SIZE) if affected_chunks else 0
        estimated_minutes = round(
            batches * 2.0 / max(GRAPH_PARALLEL_WORKERS, 1) / 60.0, 1
        )
        return {
            "change_kind": kind,
            "affected_documents": affected_documents,
            "affected_chunks": affected_chunks,
            "estimated_llm_requests": batches,
            "estimated_minutes": estimated_minutes,
        }

    def _prune_removed_schema_types(
        self, removed_entity_types: set[str], removed_relation_types: set[str]
    ) -> dict:
        """Database-only cleanup for removed schema types; no LLM involved."""
        pruned_relations = 0
        pruned_entities = 0
        if removed_relation_types:
            relation_ids = list(self.db.scalars(select(KgRelation.id).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                KgRelation.predicate.in_(sorted(removed_relation_types)),
            )))
            if relation_ids:
                self.db.execute(delete(KgRelationEvidence).where(
                    KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                    KgRelationEvidence.relation_id.in_(relation_ids),
                ))
                self.db.execute(delete(KgRelation).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    KgRelation.id.in_(relation_ids),
                ))
                pruned_relations += len(relation_ids)
        if removed_entity_types:
            entity_ids = list(self.db.scalars(select(KgEntity.id).where(
                KgEntity.knowledge_base_id == self.knowledge_base_id,
                KgEntity.entity_type.in_(sorted(removed_entity_types)),
            )))
            if entity_ids:
                touching = list(self.db.scalars(select(KgRelation.id).where(
                    KgRelation.knowledge_base_id == self.knowledge_base_id,
                    or_(
                        KgRelation.subject_entity_id.in_(entity_ids),
                        KgRelation.object_entity_id.in_(entity_ids),
                    ),
                )))
                if touching:
                    self.db.execute(delete(KgRelationEvidence).where(
                        KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                        KgRelationEvidence.relation_id.in_(touching),
                    ))
                    self.db.execute(delete(KgRelation).where(
                        KgRelation.knowledge_base_id == self.knowledge_base_id,
                        KgRelation.id.in_(touching),
                    ))
                    pruned_relations += len(touching)
                self.db.execute(delete(KgEntityMention).where(
                    KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                    KgEntityMention.entity_id.in_(entity_ids),
                ))
                self.db.execute(delete(KgAlias).where(
                    KgAlias.knowledge_base_id == self.knowledge_base_id,
                    KgAlias.entity_id.in_(entity_ids),
                ))
                self.db.execute(delete(KgEntity).where(
                    KgEntity.knowledge_base_id == self.knowledge_base_id,
                    KgEntity.id.in_(entity_ids),
                ))
                pruned_entities += len(entity_ids)
        return {"pruned_entities": pruned_entities, "pruned_relations": pruned_relations}

    @serialized_knowledge_mutation
    def activate_schema(
        self,
        schema_id: str,
        *,
        reprocess: str = "auto",
    ) -> GraphSchemaDefinition | None:
        if reprocess not in {"auto", "incremental", "full"}:
            raise ValueError("无效的重处理策略")
        schema = self.db.scalar(select(GraphSchemaDefinition).where(
            GraphSchemaDefinition.id == schema_id,
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
        ).execution_options(populate_existing=True))
        if schema is None:
            return None
        self._validate_graph_kind(schema.graph_kind)
        previous = self._read_active_schema(schema.graph_kind)
        kind = self.schema_change_kind(previous, schema)
        # 差异必须在切换 active 状态之前计算，否则会与新 Schema 自身求差。
        addition_tokens = (
            self._new_type_tokens(schema) if kind == "type_addition" else []
        )

        self.db.execute(update(GraphSchemaDefinition).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.graph_kind == schema.graph_kind,
            GraphSchemaDefinition.status == "active",
        ).values(status="superseded"))
        schema.status = "active"
        schema.activated_at = datetime.utcnow()

        chunk_scope = update(KnowledgeChunk).where(
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id
        )
        if kind in {"normalization_change", "domain_change", "initial"} or reprocess == "full":
            # Raw names are needed again: re-extract everything.
            self.db.execute(chunk_scope.where(
                KnowledgeChunk.graph_schema_version != schema.version
            ).values(graph_status="pending"))
        elif kind == "type_removal":
            removed_entities = set(previous.entity_types_json or []) - set(schema.entity_types_json or [])
            removed_relations = set(previous.relation_types_json or []) - set(schema.relation_types_json or [])
            self._prune_removed_schema_types(removed_entities, removed_relations)
            # Remaining extractions stay valid under the narrower schema.
            self.db.execute(chunk_scope.values(graph_schema_version=schema.version))
        elif kind == "type_addition":
            if addition_tokens:
                affected_ids = list(self.db.scalars(select(KnowledgeChunk.id).where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    or_(*[
                        KnowledgeChunk.content.ilike(f"%{token}%")
                        for token in addition_tokens
                    ]),
                )))
                if affected_ids:
                    self.db.execute(chunk_scope.where(
                        KnowledgeChunk.id.in_(affected_ids)
                    ).values(graph_status="pending"))
            self.db.execute(chunk_scope.where(
                KnowledgeChunk.graph_status != "pending"
            ).values(graph_schema_version=schema.version))
        else:
            # prompt_change or incremental choice: keep facts, adopt the version.
            self.db.execute(chunk_scope.where(
                KnowledgeChunk.graph_status != "pending"
            ).values(graph_schema_version=schema.version))
        self.db.commit()
        self.db.refresh(schema)

        self._after_schema_activation(kind)
        return schema

    def request_schema_generation(
        self,
        *,
        requested_by: str | None,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
        target_schema_id: str | None = None,
        graph_kind: str = "general",
    ) -> IndexJob:
        """Queue one deduplicated, auditable Schema induction job."""
        self._validate_graph_kind(graph_kind)
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        if target_schema_id:
            target = self.db.scalar(select(GraphSchemaDefinition).where(
                GraphSchemaDefinition.id == target_schema_id,
                GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            ))
            if target is None or target.status != "draft":
                raise ValueError("只能覆盖当前知识库中的 Schema 草稿")
            if target.graph_kind != graph_kind:
                raise ValueError("只能覆盖同一图谱类型的 Schema 草稿")
        existing = repository.find_open("schema_generation")
        if existing is not None:
            return existing
        job = IndexJob(
            job_type="schema_generation",
            status="pending",
            requested_by=requested_by,
            total_items=len(document_ids or []) or 1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            options_json={
                "document_ids": list(dict.fromkeys(document_ids or []))[:100],
                "instructions": (instructions or "").strip()[:2000],
                "target_schema_id": target_schema_id,
                "graph_kind": graph_kind,
            },
        )
        repository.create(job)
        signal_persisted_job(self.task_queue, job.id)
        return job

    def request_automatic_schema_generation(
        self, requested_by: str | None, source_job_id: str
    ) -> IndexJob | None:
        """Generate the first document-derived draft after successful indexing."""
        if not self.automation_settings()["auto_schema_generation_enabled"]:
            return None
        # Establish the immutable default active baseline before a generated
        # draft receives its version number.
        self.active_schema(graph_kind="general")
        existing_schema = self.db.scalar(select(GraphSchemaDefinition.id).where(
            GraphSchemaDefinition.knowledge_base_id == self.knowledge_base_id,
            GraphSchemaDefinition.generation_method.in_(["document_llm", "document_agent"]),
            GraphSchemaDefinition.graph_kind == "general",
        ).limit(1))
        if existing_schema is not None:
            return None
        job = self.request_schema_generation(requested_by=requested_by)
        options = dict(job.options_json or {})
        options.update({
            "trigger": "document_index_completed",
            "source_job_id": source_job_id,
        })
        job.options_json = options
        self.db.commit()
        return job

    def request_schema_drift_check(
        self,
        *,
        requested_by: str | None,
        document_ids: list[str] | None = None,
        instructions: str | None = None,
        graph_kind: str = "general",
    ) -> IndexJob:
        """Queue a drift check that creates a draft only for new type concepts."""
        self._validate_graph_kind(graph_kind)
        self.active_schema(graph_kind=graph_kind)
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("schema_drift")
        if existing is not None:
            return existing
        job = IndexJob(
            job_type="schema_drift",
            status="pending",
            requested_by=requested_by,
            total_items=len(document_ids or []) or 1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            options_json={
                "document_ids": list(dict.fromkeys(document_ids or []))[:100],
                "instructions": (instructions or "").strip()[:2000],
                "graph_kind": graph_kind,
            },
        )
        repository.create(job)
        signal_persisted_job(self.task_queue, job.id)
        return job

    def _after_schema_activation(self, kind: str) -> None:
        """Queue the follow-up work that matches the change kind."""
        if kind == "type_removal":
            self.mark_communities_stale()
            from app.services.entity_vector_service import EntityVectorService
            from app.services.relation_vector_service import RelationVectorService

            EntityVectorService(self.db, self.knowledge_base_id).request_rebuild(
                None, task_queue=self.task_queue
            )
            RelationVectorService(self.db, self.knowledge_base_id).request_rebuild(
                None, task_queue=self.task_queue
            )
            return
        pending = self._pending_chunk_count()
        if pending:
            self.request_build(requested_by=None, profile="standard")

    def request_build(
        self,
        mode: str = "pending",
        requested_by: str | None = None,
        options: dict | None = None,
        *,
        profile: str | None = None,
        vector_update: str | None = None,
        community_update: str | None = None,
        visible_at: datetime | None = None,
    ) -> IndexJob:
        """Create one deduplicated graph_rebuild job.

        ``profile`` wins over explicit ``mode``/``vector_update``/``community_update``;
        the legacy boolean ``options`` map to the same fields for old callers.
        ``visible_at`` delays worker pickup for debounced automatic builds.
        """
        from app.services.graph_build_profiles import (
            GRAPH_BUILD_PROFILES,
            VALID_COMMUNITY_UPDATES,
            VALID_VECTOR_UPDATES,
        )

        if mode not in {"pending", "retry_failed", "full"}:
            raise ValueError("无效的图谱构建模式")
        if profile is not None:
            if profile not in GRAPH_BUILD_PROFILES:
                raise ValueError("无效的图谱构建策略")
            resolved = dict(GRAPH_BUILD_PROFILES[profile])
            resolved["profile"] = profile
        else:
            legacy = options or {}
            resolved = {
                "mode": mode,
                "vector_update": vector_update or (
                    "full" if legacy.get("rebuild_entity_vectors", True) else "none"
                ),
                "community_update": community_update or (
                    "full" if legacy.get("rebuild_communities", True) else "defer"
                ),
            }
        if resolved["vector_update"] not in VALID_VECTOR_UPDATES:
            raise ValueError("无效的向量更新策略")
        if resolved["community_update"] not in VALID_COMMUNITY_UPDATES:
            raise ValueError("无效的社区更新策略")

        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("graph_rebuild")
        if existing is not None:
            return existing
        job = IndexJob(
            job_type="graph_rebuild",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            visible_at=visible_at,
            options_json={**resolved, **(options or {})},
        )
        repository.create(job)
        signal_persisted_job(self.task_queue, job.id)
        return job

    def _pending_chunk_count(self) -> int:
        return int(self.db.scalar(select(func.count(KnowledgeChunk.id)).where(
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
            KnowledgeChunk.graph_status == "pending",
        )) or 0)

    def request_automatic_build(
        self, requested_by: str | None, source_job_id: str
    ) -> IndexJob | None:
        """Debounced automatic build: merge nearby uploads into one job.

        - pending < GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS waits GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS;
        - reaching the threshold pulls a waiting job forward immediately;
        - a waiting job never waits longer than GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS.
        """
        if not self.automation_settings()["auto_build_enabled"]:
            return None
        pending = self._pending_chunk_count()
        if pending == 0:
            return None

        now = datetime.utcnow()
        repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = repository.find_open("graph_rebuild")
        if existing is not None:
            waiting = (
                existing.status == "pending"
                and existing.visible_at is not None
                and existing.visible_at > now
            )
            if waiting:
                created = existing.created_at or now
                threshold_met = pending >= GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS
                max_wait_elapsed = (
                    now - created
                ).total_seconds() >= GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS
                if threshold_met or max_wait_elapsed:
                    existing.visible_at = now
                    self.db.commit()
                    signal_persisted_job(self.task_queue, existing.id)
                else:
                    # Sliding debounce: each new completed document postpones the
                    # build, but never beyond the hard deadline from job creation.
                    hard_deadline = created + timedelta(
                        seconds=GRAPH_AUTO_BUILD_MAX_WAIT_SECONDS
                    )
                    existing.visible_at = min(
                        now + timedelta(seconds=GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS),
                        hard_deadline,
                    )
                    options = dict(existing.options_json or {})
                    sources = list(options.get("source_job_ids") or [])
                    if not sources and options.get("source_job_id"):
                        sources.append(options["source_job_id"])
                    if source_job_id not in sources:
                        sources.append(source_job_id)
                    options["source_job_ids"] = sources[-100:]
                    options["pending_chunks_at_enqueue"] = pending
                    existing.options_json = options
                    self.db.commit()
            return existing

        immediate = (
            GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS <= 0
            or pending >= GRAPH_AUTO_BUILD_MIN_PENDING_CHUNKS
        )
        return self.request_build(
            requested_by=requested_by,
            profile=GRAPH_AUTO_BUILD_PROFILE,
            visible_at=None if immediate else now + timedelta(
                seconds=GRAPH_AUTO_BUILD_DEBOUNCE_SECONDS
            ),
            options={
                "trigger": "document_index_completed",
                "source_job_id": source_job_id,
                "pending_chunks_at_enqueue": pending,
            },
        )

    def latest_quality(self) -> GraphQualityReport | None:
        return self.db.scalar(select(GraphQualityReport).where(
            GraphQualityReport.knowledge_base_id == self.knowledge_base_id
        ).order_by(GraphQualityReport.created_at.desc()).limit(1))

    def document_coverage(self, document_id: str | None = None) -> dict:
        """Return document-level processing and graph-fact coverage metrics."""
        document_stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == self.knowledge_base_id
        )
        if document_id:
            document_stmt = document_stmt.where(KnowledgeDocument.id == document_id)
        documents = list(self.db.scalars(
            document_stmt.order_by(KnowledgeDocument.updated_at.desc())
        ))
        document_ids = [document.id for document in documents]
        if not document_ids:
            return {
                "summary": {
                    "documents": 0,
                    "chunks": 0,
                    "processed_chunks": 0,
                    "fact_chunks": 0,
                    "processing_rate": 0.0,
                    "fact_coverage_rate": 0.0,
                },
                "documents": [],
            }

        chunks = list(self.db.execute(select(
            KnowledgeChunk.id,
            KnowledgeChunk.document_id,
            KnowledgeChunk.graph_status,
            KnowledgeChunk.graph_schema_version,
            KnowledgeChunk.graph_error,
        ).where(
            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
            KnowledgeChunk.document_id.in_(document_ids),
        )))
        mention_rows = list(self.db.execute(select(
            KgEntityMention.document_id,
            KgEntityMention.chunk_id,
            KgEntityMention.entity_id,
        ).where(
            KgEntityMention.knowledge_base_id == self.knowledge_base_id,
            KgEntityMention.document_id.in_(document_ids),
        )))
        evidence_rows = list(self.db.execute(select(
            KgRelationEvidence.document_id,
            KgRelationEvidence.chunk_id,
            KgRelationEvidence.relation_id,
        ).where(
            KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
            KgRelationEvidence.document_id.in_(document_ids),
        )))

        stats = defaultdict(lambda: {
            "chunks": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "processing": 0,
            "schema_versions": set(),
            "errors": [],
            "fact_chunks": set(),
            "entities": set(),
            "relations": set(),
            "mentions": 0,
            "evidence": 0,
        })
        for chunk in chunks:
            item = stats[chunk.document_id]
            item["chunks"] += 1
            status = chunk.graph_status if chunk.graph_status in {
                "completed", "failed", "pending", "processing"
            } else "pending"
            item[status] += 1
            if chunk.graph_schema_version:
                item["schema_versions"].add(chunk.graph_schema_version)
            if chunk.graph_error and len(item["errors"]) < 3:
                item["errors"].append(chunk.graph_error)
        for row in mention_rows:
            item = stats[row.document_id]
            item["mentions"] += 1
            item["fact_chunks"].add(row.chunk_id)
            item["entities"].add(row.entity_id)
        for row in evidence_rows:
            item = stats[row.document_id]
            item["evidence"] += 1
            item["fact_chunks"].add(row.chunk_id)
            item["relations"].add(row.relation_id)

        rows = []
        for document in documents:
            item = stats[document.id]
            total = int(item["chunks"])
            processed = int(item["completed"] + item["failed"])
            fact_chunks = len(item["fact_chunks"])
            if total == 0:
                coverage_status = "no_chunks"
            elif item["failed"]:
                coverage_status = "attention"
            elif processed < total:
                coverage_status = "processing"
            elif fact_chunks == 0:
                coverage_status = "no_facts"
            else:
                coverage_status = "covered"
            rows.append({
                "document_id": document.id,
                "filename": document.original_filename,
                "document_status": document.status,
                "coverage_status": coverage_status,
                "total_chunks": total,
                "completed_chunks": int(item["completed"]),
                "failed_chunks": int(item["failed"]),
                "pending_chunks": int(item["pending"] + item["processing"]),
                "fact_chunks": fact_chunks,
                "entity_mentions": int(item["mentions"]),
                "unique_entities": len(item["entities"]),
                "relation_evidence": int(item["evidence"]),
                "unique_relations": len(item["relations"]),
                "processing_rate": round(processed / total, 4) if total else 0.0,
                "fact_coverage_rate": round(fact_chunks / total, 4) if total else 0.0,
                "schema_versions": sorted(item["schema_versions"]),
                "errors": item["errors"],
            })

        total_chunks = sum(row["total_chunks"] for row in rows)
        processed_chunks = sum(
            row["completed_chunks"] + row["failed_chunks"] for row in rows
        )
        fact_chunks = sum(row["fact_chunks"] for row in rows)
        return {
            "summary": {
                "documents": len(rows),
                "chunks": total_chunks,
                "processed_chunks": processed_chunks,
                "fact_chunks": fact_chunks,
                "processing_rate": round(processed_chunks / total_chunks, 4)
                if total_chunks else 0.0,
                "fact_coverage_rate": round(fact_chunks / total_chunks, 4)
                if total_chunks else 0.0,
            },
            "documents": rows,
        }

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
        connected_entity_ids: set[str] = set()
        degrees: dict[str, int] = defaultdict(int)
        for relation in relations:
            connected_entity_ids.update(
                (relation.subject_entity_id, relation.object_entity_id)
            )
            degrees[relation.subject_entity_id] += 1
            degrees[relation.object_entity_id] += 1
        mentioned_entity_ids = set(self.db.scalars(
            select(KgEntityMention.entity_id)
            .where(KgEntityMention.knowledge_base_id == self.knowledge_base_id)
            .distinct()
        ))
        orphan_entities = [
            entity for entity in entities
            if entity.id not in connected_entity_ids
            and entity.id not in mentioned_entity_ids
        ]
        high_degree_threshold = max(12, int(max(len(relations), 1) ** 0.5 * 3))
        high_degree_entities = [
            entity_id for entity_id, degree in degrees.items()
            if degree >= high_degree_threshold
        ]
        status_counts = dict(self.db.execute(
            select(KnowledgeChunk.graph_status, func.count(KnowledgeChunk.id))
            .where(KnowledgeChunk.knowledge_base_id == self.knowledge_base_id)
            .group_by(KnowledgeChunk.graph_status)
        ).all())
        relation_count = len(relations)
        source_chunk_count = sum(int(value) for value in status_counts.values())
        evidence_coverage = evidence_backed / relation_count if relation_count else 1.0
        related_ratio = related_to / relation_count if relation_count else 0.0
        metrics = {
            "entities": len(entities), "relations": relation_count,
            "evidence_coverage": round(evidence_coverage, 6),
            "relations_without_evidence": relation_count - evidence_backed,
            "low_confidence_relations": len(low_confidence),
            "related_to_ratio": round(related_ratio, 6),
            "duplicate_entity_groups": len(duplicate_groups),
            "orphan_entities": len(orphan_entities),
            "max_entity_degree": max(degrees.values(), default=0),
            "high_degree_entities": len(high_degree_entities),
            "high_degree_threshold": high_degree_threshold,
            "cross_knowledge_base_relations": cross_kb,
            "chunk_status": status_counts,
        }
        non_empty_graph = (
            source_chunk_count == 0 or (len(entities) > 0 and relation_count > 0)
        )
        extraction_complete = (
            status_counts.get("pending", 0) == 0
            and status_counts.get("processing", 0) == 0
        )
        gates = {
            "non_empty_graph": {
                "passed": non_empty_graph,
                "minimum_entities": 1 if source_chunk_count else 0,
                "minimum_relations": 1 if source_chunk_count else 0,
            },
            "extraction_complete": {
                "passed": extraction_complete,
                "pending_or_processing": int(status_counts.get("pending", 0))
                + int(status_counts.get("processing", 0)),
                "maximum": 0,
            },
            "failed_chunks": {
                "passed": status_counts.get("failed", 0) == 0,
                "failed": int(status_counts.get("failed", 0)),
                "maximum": 0,
            },
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

    def scan_relation_merge_candidates(self) -> dict:
        """Find relations that become duplicate after active entity disambiguation."""
        relations = list(self.db.scalars(select(KgRelation).where(
            KgRelation.knowledge_base_id == self.knowledge_base_id
        )))
        entity_map = {
            item.merged_entity_id: item.canonical_entity_id
            for item in self.db.scalars(select(KgEntityMerge).where(
                KgEntityMerge.knowledge_base_id == self.knowledge_base_id,
                KgEntityMerge.status == "active",
            ))
        }
        existing = set(self.db.scalars(select(GraphReviewItem.target_id).where(
            GraphReviewItem.knowledge_base_id == self.knowledge_base_id,
            GraphReviewItem.status == "pending",
        )))
        grouped: dict[tuple[str, str, str], list[KgRelation]] = defaultdict(list)
        for relation in relations:
            subject = entity_map.get(relation.subject_entity_id, relation.subject_entity_id)
            obj = entity_map.get(relation.object_entity_id, relation.object_entity_id)
            grouped[(subject, relation.predicate, obj)].append(relation)
        created = 0
        for triple, group in grouped.items():
            if len(group) < 2:
                continue
            relation_ids = sorted(item.id for item in group)
            target = "relation-merge:" + ":".join(relation_ids)
            if target in existing:
                continue
            self.db.add(GraphReviewItem(
                knowledge_base_id=self.knowledge_base_id,
                item_type="relation_merge_candidate",
                target_id=target,
                payload_json={"relation_ids": relation_ids, "resolved_triple": list(triple)},
                reason="实体消歧后关系指向同一三元组，建议人工确认合并",
                confidence=min(float(item.confidence) for item in group),
            ))
            existing.add(target)
            created += 1
        self.db.commit()
        return {"scanned": len(relations), "candidates": created}

    def list_relation_merges(self):
        from app.models.knowledge_graph import KgGraphCorrection
        return list(self.db.scalars(select(KgGraphCorrection).where(
            KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
            KgGraphCorrection.action == "merge_relation",
        ).order_by(KgGraphCorrection.created_at.desc())))

    def merge_relations(self, canonical_id: str, merged_id: str, reason: str, created_by: str):
        record = self.create_correction(
            action="merge_relation",
            target_id=merged_id,
            payload={"canonical_relation_id": canonical_id},
            reason=reason,
            created_by=created_by,
        )
        return record

    def revert_relation_merge(self, merge_id: str, reverted_by: str):
        from app.models.knowledge_graph import KgGraphCorrection
        record = self.db.scalar(select(KgGraphCorrection).where(
            KgGraphCorrection.id == merge_id,
            KgGraphCorrection.knowledge_base_id == self.knowledge_base_id,
            KgGraphCorrection.action == "merge_relation",
        ))
        if record is None:
            return None
        return self.revert_correction(merge_id, reverted_by)

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
        self._queue_derived_refresh(created_by)
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
        self._queue_derived_refresh(reverted_by)
        return record

    def community_state(self, *, create: bool = True) -> GraphCommunityState | None:
        state = self.db.scalar(select(GraphCommunityState).where(
            GraphCommunityState.knowledge_base_id == self.knowledge_base_id
        ))
        if state is None and create:
            state = GraphCommunityState(
                knowledge_base_id=self.knowledge_base_id,
                status="empty",
            )
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        return state

    def mark_communities_stale(self) -> GraphCommunityState:
        """Flag communities for deferred rebuild instead of blocking the graph build."""
        state = self.community_state()
        if state.status != "stale":
            state.status = "stale"
            self.db.commit()
        return state

    def _load_graph_adjacency(self) -> tuple[dict[str, object], dict, list]:
        from rag.knowledge_graph.sqlite_store import SqliteGraphStore

        store = SqliteGraphStore(self.db, self.knowledge_base_id)
        effective_entities = store.list_effective_entities()
        relations = store.list_effective_relations()
        entities = [SimpleNamespace(**item) for item in effective_entities]
        adjacency: dict[str, set[str]] = {entity.id: set() for entity in entities}
        relation_by_pair: dict[frozenset[str], list[str]] = defaultdict(list)
        for relation in relations:
            subject_id = relation["subject_id"]
            object_id = relation["object_id"]
            if subject_id not in adjacency or object_id not in adjacency:
                continue
            adjacency[subject_id].add(object_id)
            adjacency[object_id].add(subject_id)
            relation_by_pair[frozenset((subject_id, object_id))].append(
                relation["relation_id"]
            )
        return adjacency, relation_by_pair, entities

    @staticmethod
    def _components_from_adjacency(adjacency: dict[str, set[str]]) -> list[list[str]]:
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
        return components

    def rebuild_communities(self, changed_entity_ids: list[str] | None = None) -> dict:
        """Rebuild communities; pass changed ids to touch only affected components."""
        if changed_entity_ids is not None and not changed_entity_ids:
            state = self.community_state(create=False)
            return {
                "communities": int(state.communities if state else 0),
                "entities": int(state.entities if state else 0),
                "unchanged": True,
            }
        adjacency, relation_by_pair, entities = self._load_graph_adjacency()
        components = self._components_from_adjacency(adjacency)
        by_id = {entity.id: entity for entity in entities}
        affected_rebuild = changed_entity_ids is not None
        affected = set(changed_entity_ids or [])

        if affected_rebuild:
            affected_components = [
                component for component in components if affected & set(component)
            ]
            affected_entities = {
                entity_id for component in affected_components
                for entity_id in component
            }
            if affected_entities:
                stale_community_ids = list(self.db.scalars(
                    select(GraphCommunityMember.community_id).where(
                        GraphCommunityMember.knowledge_base_id == self.knowledge_base_id,
                        GraphCommunityMember.entity_id.in_(sorted(affected_entities)),
                    ).distinct()
                ))
            else:
                stale_community_ids = []
            if stale_community_ids:
                self.db.execute(delete(GraphCommunityMember).where(
                    GraphCommunityMember.knowledge_base_id == self.knowledge_base_id,
                    GraphCommunityMember.community_id.in_(stale_community_ids),
                ))
                self.db.execute(delete(GraphCommunity).where(
                    GraphCommunity.knowledge_base_id == self.knowledge_base_id,
                    GraphCommunity.id.in_(stale_community_ids),
                ))
                self.db.flush()
            targets = affected_components
        else:
            self.db.execute(delete(GraphCommunityMember).where(
                GraphCommunityMember.knowledge_base_id == self.knowledge_base_id
            ))
            self.db.execute(delete(GraphCommunity).where(
                GraphCommunity.knowledge_base_id == self.knowledge_base_id
            ))
            self.db.flush()
            targets = components

        root = self.db.scalar(select(GraphCommunity).where(
            GraphCommunity.knowledge_base_id == self.knowledge_base_id,
            GraphCommunity.level == 1,
        ).limit(1))
        if not affected_rebuild and not targets:
            root = None
        if targets and root is None:
            root = GraphCommunity(
                knowledge_base_id=self.knowledge_base_id,
                level=1,
                title="知识库全局图谱",
                summary="",
                evidence_json=[],
            )
            self.db.add(root)
            self.db.flush()

        for index, component in enumerate(sorted(
            targets, key=lambda values: (-len(values), values[0])
        ), start=1):
            names = [by_id[item].canonical_name for item in component if item in by_id]
            evidence = sorted({
                relation_id
                for entity_id in component
                for neighbor in adjacency.get(entity_id, ())
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

        if not targets and root is not None and affected_rebuild:
            # Graph emptied; remove the stale global root as well.
            self.db.execute(delete(GraphCommunityMember).where(
                GraphCommunityMember.community_id == root.id
            ))
            self.db.delete(root)
            root = None
        if root is not None:
            relation_count = int(self.db.scalar(select(func.count(
                KgRelation.id
            )).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id
            )) or 0)
            community_count = int(self.db.scalar(select(func.count(
                GraphCommunity.id
            )).where(
                GraphCommunity.knowledge_base_id == self.knowledge_base_id,
                GraphCommunity.level == 0,
            )) or 0)
            root.summary = (
                f"包含 {len(entities)} 个实体、{relation_count} 条关系、"
                f"{community_count} 个主题子图。"
            )
            root.evidence_json = [
                relation_id
                for pair in relation_by_pair
                for relation_id in relation_by_pair[pair]
            ][:100]

        state = self.community_state()
        state.status = "ready"
        state.communities = int(self.db.scalar(select(func.count(
            GraphCommunity.id
        )).where(
            GraphCommunity.knowledge_base_id == self.knowledge_base_id,
            GraphCommunity.level == 0,
        )) or 0)
        state.entities = len(entities)
        state.built_at = datetime.utcnow()
        self.db.commit()
        return {"communities": state.communities, "entities": len(entities)}

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
