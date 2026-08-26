"""知识图谱构建服务。

负责：
- 按批次从 knowledge_chunks 中提取实体关系
- 记录每个 chunk 的图谱处理状态
- 支持断点继续和失败重试
- 清理孤立数据
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import (
    GRAPH_ESCALATION_ENABLED,
    GRAPH_ESCALATION_MIN_CONFIDENCE,
    GRAPH_EXTRACTION_BATCH_SIZE,
    GRAPH_EXTRACTION_MAX_CHARS,
    GRAPH_EXTRACTION_MAX_RETRIES,
    GRAPH_MIN_CONFIDENCE,
    GRAPH_CACHE_ENABLED,
    GRAPH_PARALLEL_WORKERS,
    GRAPH_PREFILTER_ENABLED,
    GRAPH_PREFILTER_MIN_CHARS,
    GRAPH_SCHEMA_VERSION,
)
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_graph import KgEntity, KgRelation
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.knowledge_graph.extractor import GraphExtractor
from rag.knowledge_graph.interfaces import GraphStore
from rag.knowledge_graph.prefilter import has_graph_signal
from rag.knowledge_graph.schemas import ChunkExtraction

logger = logging.getLogger(__name__)


class GraphBuildService:
    def __init__(
        self,
        db: Session,
        extractor: GraphExtractor | None = None,
        store: GraphStore | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if store is None:
            from app.bootstrap.container import get_provider_container

            store = get_provider_container().graph_store(db, knowledge_base_id)
        self.store = store
        self.knowledge_repo = KnowledgeRepository(db, knowledge_base_id)
        self._extractor = extractor
        self._escalation_provider = None
        self._escalation_loaded = False

    @property
    def extractor(self) -> GraphExtractor:
        if self._extractor is None:
            self._extractor = GraphExtractor(
                max_retries=GRAPH_EXTRACTION_MAX_RETRIES,
                max_content_chars=GRAPH_EXTRACTION_MAX_CHARS,
            )
        return self._extractor

    def build_graph(
        self,
        only_pending: bool = True,
        retry_failed: bool = False,
    ) -> dict:
        """为所有 ready 文档的 chunk 构建知识图谱。

        Args:
            only_pending: 只处理 pending 状态的 chunk
            retry_failed: 重试 failed 状态的 chunk

        Returns:
            统计信息，含 ``changes`` 变化集供增量向量/社区更新使用
        """
        recovered_processing = self.recover_stale_processing()
        from app.services.graph_governance_service import GraphGovernanceService

        schema = GraphGovernanceService(
            self.db, self.knowledge_base_id
        ).active_schema()
        schema_version = schema.version if schema else GRAPH_SCHEMA_VERSION
        chunks = self._get_chunks_to_process(only_pending, retry_failed)
        total = len(chunks)
        succeeded = 0
        failed = 0
        skipped = 0
        errors: list[str] = []

        logger.info("Graph build: %d chunks to process", total)

        chunk_ids = [chunk.id for chunk in chunks]
        before_snapshot = self._snapshot_chunk_touch(chunk_ids)

        chunks, prefiltered = self._apply_prefilter(
            chunks, schema_version, before_snapshot[0]
        )
        skipped += prefiltered

        batches = self._make_batches(chunks)
        for chunk in chunks:
            chunk.graph_status = "processing"
        if chunks:
            self.db.commit()

        worker_count = 1 if self._extractor is not None else GRAPH_PARALLEL_WORKERS
        extracted: dict[int, tuple[list | None, Exception | None]] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._extract_batch_cached, batch, schema): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    extracted[index] = (future.result(), None)
                except Exception as exc:
                    extracted[index] = (None, exc)

        extracted = self._escalate_low_signal(batches, extracted, schema)

        for index, batch in enumerate(batches):
            extractions, extraction_error = extracted[index]
            result = self._persist_batch(
                batch, extractions, extraction_error, schema_version, schema
            )
            succeeded += result["succeeded"]
            failed += result["failed"]
            errors.extend(result["errors"])

        self.store.cleanup_orphans()
        from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

        correction_reconciliation = KnowledgeGraphRepository(
            self.db,
            self.knowledge_base_id,
        ).reconcile_corrections()

        changes = self._compute_changeset(chunk_ids, before_snapshot)
        stats = {
            "recovered_processing": recovered_processing,
            "total_chunks": total,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "prefiltered_chunks": prefiltered,
            "errors": errors[:20],
            "graph_stats": self.store.stats(),
            "correction_reconciliation": correction_reconciliation,
            "changes": changes,
        }
        logger.info("Graph build completed: %s", stats)
        return stats

    def _apply_prefilter(
        self,
        chunks: list[KnowledgeChunk],
        schema_version: int,
        before_touch: dict[str, tuple[set, set]],
    ) -> tuple[list[KnowledgeChunk], int]:
        """规则预过滤：无图谱信号的 Chunk 直接标记完成，不调用 LLM。"""
        if not GRAPH_PREFILTER_ENABLED or not chunks:
            return chunks, 0
        keep: list[KnowledgeChunk] = []
        filtered: list[KnowledgeChunk] = []
        for chunk in chunks:
            if has_graph_signal(chunk.content or "", min_chars=GRAPH_PREFILTER_MIN_CHARS):
                keep.append(chunk)
            else:
                filtered.append(chunk)
        for chunk in filtered:
            had_facts = bool(before_touch.get(chunk.id, ({}, {}))[1])
            if had_facts:
                # 清掉旧 Schema/旧模型留下的图谱数据，等价于空抽取替换。
                self.store.upsert_extraction(
                    {
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "content": chunk.content,
                    },
                    ChunkExtraction(chunk_id=chunk.id, entities=[], relations=[]),
                )
            chunk.graph_status = "completed"
            chunk.graph_schema_version = schema_version
            chunk.graph_extracted_at = datetime.now(timezone.utc)
            chunk.graph_error = None
        if filtered:
            self.db.commit()
            logger.info("Graph prefilter skipped %d chunks", len(filtered))
        return keep, len(filtered)

    def _snapshot_chunk_touch(
        self, chunk_ids: list[str]
    ) -> tuple[dict[str, tuple[set, set]], dict[str, tuple[str, str]]]:
        """记录每个 chunk 当前关联的实体/关系，以及这些关系的端点。"""
        touch: dict[str, tuple[set, set]] = {
            chunk_id: (set(), set()) for chunk_id in chunk_ids
        }
        if not chunk_ids:
            return touch, {}
        from app.models.knowledge_graph import KgEntityMention, KgRelationEvidence

        for chunk_id, entity_id in self.db.execute(
            select(KgEntityMention.chunk_id, KgEntityMention.entity_id).where(
                KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                KgEntityMention.chunk_id.in_(chunk_ids),
            )
        ):
            touch.setdefault(chunk_id, (set(), set()))[0].add(entity_id)
        before_relation_ids: set[str] = set()
        for chunk_id, relation_id in self.db.execute(
            select(KgRelationEvidence.chunk_id, KgRelationEvidence.relation_id).where(
                KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                KgRelationEvidence.chunk_id.in_(chunk_ids),
            )
        ):
            touch.setdefault(chunk_id, (set(), set()))[1].add(relation_id)
            before_relation_ids.add(relation_id)

        relation_endpoints: dict[str, tuple[str, str]] = {}
        if before_relation_ids:
            for relation in self.db.scalars(select(KgRelation).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                KgRelation.id.in_(sorted(before_relation_ids)),
            )):
                relation_endpoints[relation.id] = (
                    relation.subject_entity_id,
                    relation.object_entity_id,
                )
        return touch, relation_endpoints

    def _compute_changeset(
        self,
        chunk_ids: list[str],
        before: tuple[dict[str, tuple[set, set]], dict[str, tuple[str, str]]],
    ) -> dict:
        """构建后 diff 出受影响的实体/关系，驱动增量向量与社区更新。"""
        before_touch, before_endpoints = before
        before_entities: set[str] = set()
        before_relations: set[str] = set()
        for entity_ids, relation_ids in before_touch.values():
            before_entities.update(entity_ids)
            before_relations.update(relation_ids)
        after_touch, _ = self._snapshot_chunk_touch(chunk_ids)
        after_entities: set[str] = set()
        after_relations: set[str] = set()
        for entity_ids, relation_ids in after_touch.values():
            after_entities.update(entity_ids)
            after_relations.update(relation_ids)

        changed_entity_ids = before_entities | after_entities
        changed_relation_ids = before_relations | after_relations

        # 变化实体关联的关系也要刷新（向量内容包含实体名）
        if changed_entity_ids:
            cascaded = list(self.db.scalars(select(KgRelation.id).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                or_(
                    KgRelation.subject_entity_id.in_(sorted(changed_entity_ids)),
                    KgRelation.object_entity_id.in_(sorted(changed_entity_ids)),
                ),
            )))
            changed_relation_ids.update(cascaded)

        deleted_entity_ids = {
            entity_id
            for entity_id in changed_entity_ids
            if self.db.get(KgEntity, entity_id) is None
        }
        deleted_relation_ids = {
            relation_id
            for relation_id in changed_relation_ids
            if self.db.get(KgRelation, relation_id) is None
        }

        # 社区影响集 = 变化实体 + 变化/删除关系的端点
        community_entities = set(changed_entity_ids)
        surviving_ids = [
            relation_id
            for relation_id in changed_relation_ids
            if relation_id not in deleted_relation_ids
        ]
        if surviving_ids:
            for relation in self.db.scalars(select(KgRelation).where(
                KgRelation.knowledge_base_id == self.knowledge_base_id,
                KgRelation.id.in_(surviving_ids),
            )):
                community_entities.add(relation.subject_entity_id)
                community_entities.add(relation.object_entity_id)
        for relation_id in deleted_relation_ids:
            endpoints = before_endpoints.get(relation_id)
            if endpoints:
                community_entities.update(endpoints)

        return {
            "changed_entity_ids": sorted(changed_entity_ids - deleted_entity_ids),
            "deleted_entity_ids": sorted(deleted_entity_ids),
            "changed_relation_ids": sorted(changed_relation_ids - deleted_relation_ids),
            "deleted_relation_ids": sorted(deleted_relation_ids),
            "community_entity_ids": sorted(community_entities),
        }

    def _escalation_llm(self):
        """懒加载升级模型；与抽取模型相同时视为未配置。"""
        if not self._escalation_loaded:
            self._escalation_loaded = True
            self._escalation_provider = None
            if GRAPH_ESCALATION_ENABLED and self._extractor is None:
                try:
                    from rag.providers.factory import (
                        get_graph_escalation_llm_provider,
                        get_graph_extraction_llm_provider,
                    )

                    escalation = get_graph_escalation_llm_provider()
                    if (
                        escalation.identity().as_dict()
                        != get_graph_extraction_llm_provider().identity().as_dict()
                    ):
                        self._escalation_provider = escalation
                except Exception as exc:
                    logger.debug("Graph escalation model unavailable: %s", exc)
        return self._escalation_provider

    def _escalate_low_signal(
        self,
        batches: list[list[dict]],
        extracted: dict[int, tuple[list | None, Exception | None]],
        schema,
    ) -> dict[int, tuple[list | None, Exception | None]]:
        """两阶段抽取：空结果/低置信度/解析失败的批次交给更强模型重试。"""
        escalation_llm = self._escalation_llm()
        if escalation_llm is None:
            return extracted

        candidates: list[tuple[int, dict]] = []
        for index, batch in enumerate(batches):
            extractions, error = extracted[index]
            if error is not None:
                # 解析失败：整批升级重试
                candidates.extend((index, item) for item in batch)
                continue
            by_id = {item.chunk_id: item for item in (extractions or [])}
            for item in batch:
                extraction = by_id.get(item["chunk_id"])
                if extraction is None:
                    candidates.append((index, item))
                    continue
                has_facts = bool(extraction.entities or extraction.relations)
                top_confidence = max(
                    (relation.confidence for relation in extraction.relations),
                    default=0.0,
                )
                if has_facts and top_confidence >= GRAPH_ESCALATION_MIN_CONFIDENCE:
                    continue
                candidates.append((index, item))
        if not candidates:
            return extracted

        logger.info("Graph escalation retry for %d chunks", len(candidates))
        escalated = 0
        for start in range(0, len(candidates), GRAPH_EXTRACTION_BATCH_SIZE):
            slice_ = candidates[start:start + GRAPH_EXTRACTION_BATCH_SIZE]
            payload = [item for _, item in slice_]
            try:
                results = self._extract_batch_cached(
                    payload, schema, llm_override=escalation_llm
                )
            except Exception as exc:
                logger.warning("Graph escalation batch failed: %s", exc)
                continue
            result_by_id = {item.chunk_id: item for item in results or []}
            for index, item in slice_:
                upgraded = result_by_id.get(item["chunk_id"])
                if upgraded is None:
                    continue
                if not upgraded.entities and not upgraded.relations:
                    continue
                base, _error = extracted[index]
                rows = list(base or [])
                merged = []
                replaced = False
                for row in rows:
                    if row.chunk_id == item["chunk_id"]:
                        merged.append(upgraded)
                        replaced = True
                    else:
                        merged.append(row)
                if not replaced:
                    merged.append(upgraded)
                extracted[index] = (merged, None)
                escalated += 1
        logger.info("Graph escalation upgraded %d chunks", escalated)
        return extracted

    @staticmethod
    def _make_batches(chunks: list[KnowledgeChunk]) -> list[list[dict]]:
        batches: list[list[dict]] = []
        batch: list[dict] = []
        batch_chars = 0
        for chunk in chunks:
            if batch and (
                len(batch) >= GRAPH_EXTRACTION_BATCH_SIZE
                or batch_chars + len(chunk.content) > GRAPH_EXTRACTION_MAX_CHARS
            ):
                batches.append(batch)
                batch = []
                batch_chars = 0
            batch.append({
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "content": chunk.content,
            })
            batch_chars += len(chunk.content)
        if batch:
            batches.append(batch)
        return batches

    def _extract_batch_cached(
        self, batch: list[dict], schema, llm_override=None
    ) -> list:
        configure_schema = getattr(self.extractor, "configure_schema", None)
        if callable(configure_schema) and schema is not None:
            configure_schema(
                entity_types=schema.entity_types_json,
                relation_types=schema.relation_types_json,
                prompt_prefix=schema.extraction_prompt,
            )
        if not GRAPH_CACHE_ENABLED or self._extractor is not None:
            if llm_override is not None:
                return self.extractor.extract_batch(batch, llm=llm_override)
            return self.extractor.extract_batch(batch)
        active_llm = llm_override or self.extractor.llm
        model_identity = getattr(active_llm, "identity", None)
        model_fingerprint = str(model_identity().as_dict()) if callable(model_identity) else type(active_llm).__name__
        schema_version = schema.version if schema else GRAPH_SCHEMA_VERSION
        from app.bootstrap.container import get_provider_container
        from app.services.graph_governance_service import GraphGovernanceService

        cache_key = GraphGovernanceService.extraction_cache_key(
            batch, schema_version, model_fingerprint
        )
        container = get_provider_container()
        with container.metadata.session() as cache_db:
            governance = GraphGovernanceService(
                cache_db, self.knowledge_base_id, task_queue=container.task_queue
            )
            cached = governance.get_cached_extractions(cache_key)
        if cached is not None:
            return cached
        extractions = self.extractor.extract_batch(
            batch, llm=llm_override
        ) if llm_override is not None else self.extractor.extract_batch(batch)
        try:
            with container.metadata.session() as cache_db:
                GraphGovernanceService(
                    cache_db, self.knowledge_base_id, task_queue=container.task_queue
                ).put_cached_extractions(
                    cache_key, schema_version, model_fingerprint, extractions
                )
        except Exception as exc:
            logger.debug("Graph extraction cache write skipped: %s", exc)
        return extractions

    def recover_stale_processing(self) -> int:
        """Return interrupted local graph work to the retryable pending state.

        Graph extraction currently has no concurrent distributed worker or lease,
        so any ``processing`` row found before a new build is stale by definition.
        The lease-aware implementation in the distributed-task phase will replace
        this local recovery rule.
        """
        chunks = list(
            self.db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.graph_status == "processing"
                )
            )
        )
        for chunk in chunks:
            chunk.graph_status = "pending"
            chunk.graph_error = "Recovered from interrupted graph extraction"
        if chunks:
            self.db.commit()
            logger.warning("Recovered %d stale graph chunks", len(chunks))
        return len(chunks)

    def _get_chunks_to_process(self, only_pending: bool, retry_failed: bool) -> list[KnowledgeChunk]:
        from app.services.graph_governance_service import GraphGovernanceService

        schema = GraphGovernanceService(self.db, self.knowledge_base_id).active_schema()
        schema_version = schema.version if schema else GRAPH_SCHEMA_VERSION
        statuses = ["pending"]
        if retry_failed:
            statuses.append("failed")
        elif not only_pending:
            statuses = ["pending", "failed", "completed"]

        ready_doc_ids = [d.id for d in self.knowledge_repo.list_ready_documents()]
        if not ready_doc_ids:
            return []

        status_filter = KnowledgeChunk.graph_status.in_(statuses)
        if only_pending:
            status_filter = or_(
                status_filter,
                KnowledgeChunk.graph_schema_version != schema_version,
            )

        return list(
            self.db.scalars(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.document_id.in_(ready_doc_ids),
                    status_filter,
                )
                .order_by(
                    KnowledgeChunk.document_id,
                    KnowledgeChunk.chunk_index,
                )
            )
        )

    def _process_batch(self, batch: list[dict]) -> dict:
        from app.services.graph_governance_service import GraphGovernanceService
        schema = GraphGovernanceService(self.db, self.knowledge_base_id).active_schema()
        schema_version = schema.version if schema else GRAPH_SCHEMA_VERSION
        for chunk_data in batch:
            chunk_id = chunk_data["chunk_id"]
            chunk = self.db.get(KnowledgeChunk, chunk_id)
            if not chunk:
                continue
            chunk.graph_status = "processing"
        self.db.commit()
        try:
            extractions = self._extract_batch_cached(batch, schema)
            error = None
        except Exception as exc:
            extractions, error = None, exc
        return self._persist_batch(batch, extractions, error, schema_version, schema)

    def _persist_batch(self, batch, extractions, extraction_error, schema_version, schema) -> dict:
        succeeded = 0
        failed = 0
        errors: list[str] = []
        chunks_by_id = {
            item["chunk_id"]: self.db.get(KnowledgeChunk, item["chunk_id"])
            for item in batch
        }
        chunks_by_id = {key: value for key, value in chunks_by_id.items() if value is not None}
        if extraction_error is not None:
            for chunk_id, chunk in chunks_by_id.items():
                chunk.graph_status = "failed"
                chunk.graph_error = str(extraction_error)[:500]
                errors.append(f"{chunk_id}: {str(extraction_error)[:200]}")
            self.db.commit()
            return {"succeeded": 0, "failed": len(chunks_by_id), "errors": errors}
        extractions_by_id = {extraction.chunk_id: extraction for extraction in (extractions or [])}
        allowed_entities = set(schema.entity_types_json) if schema else None
        allowed_relations = set(schema.relation_types_json) if schema else None

        for chunk_data in batch:
            chunk_id = chunk_data["chunk_id"]
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            try:
                extraction = extractions_by_id[chunk_id]
                entities = [
                    entity for entity in extraction.entities
                    if allowed_entities is None or entity.type in allowed_entities
                ]
                local_ids = {entity.local_id for entity in entities}
                extraction = extraction.model_copy(
                    update={
                        "entities": entities,
                        "relations": [
                            relation
                            for relation in extraction.relations
                            if relation.confidence >= GRAPH_MIN_CONFIDENCE
                            and relation.subject in local_ids
                            and relation.object in local_ids
                            and (allowed_relations is None or relation.predicate in allowed_relations)
                        ]
                    }
                )

                # Always replace the old result. An empty new extraction must
                # remove graph facts left by an older schema or model.
                self.store.upsert_extraction(chunk_data, extraction)

                chunk.graph_status = "completed"
                chunk.graph_schema_version = schema_version
                chunk.graph_extracted_at = datetime.now(timezone.utc)
                chunk.graph_error = None
                succeeded += 1
            except Exception as exc:
                chunk.graph_status = "failed"
                chunk.graph_error = str(exc)[:500]
                failed += 1
                errors.append(f"{chunk_id}: {str(exc)[:200]}")
                logger.warning("Graph extraction failed for %s: %s", chunk_id, exc)

            self.db.commit()

        return {"succeeded": succeeded, "failed": failed, "errors": errors}
