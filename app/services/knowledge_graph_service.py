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
    GRAPH_EXTRACTION_BATCH_SIZE,
    GRAPH_EXTRACTION_MAX_CHARS,
    GRAPH_EXTRACTION_MAX_RETRIES,
    GRAPH_MIN_CONFIDENCE,
    GRAPH_CACHE_ENABLED,
    GRAPH_PARALLEL_WORKERS,
    GRAPH_SCHEMA_VERSION,
)
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.knowledge_chunk import KnowledgeChunk
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.knowledge_graph.extractor import GraphExtractor
from rag.knowledge_graph.interfaces import GraphStore

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
            统计信息
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

        stats = {
            "recovered_processing": recovered_processing,
            "total_chunks": total,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "errors": errors[:20],
            "graph_stats": self.store.stats(),
            "correction_reconciliation": correction_reconciliation,
        }
        logger.info("Graph build completed: %s", stats)
        return stats

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

    def _extract_batch_cached(self, batch: list[dict], schema) -> list:
        configure_schema = getattr(self.extractor, "configure_schema", None)
        if callable(configure_schema) and schema is not None:
            configure_schema(
                entity_types=schema.entity_types_json,
                relation_types=schema.relation_types_json,
                prompt_prefix=schema.extraction_prompt,
            )
        if not GRAPH_CACHE_ENABLED or self._extractor is not None:
            return self.extractor.extract_batch(batch)
        model_identity = getattr(self.extractor.llm, "identity", None)
        model_fingerprint = str(model_identity().as_dict()) if callable(model_identity) else type(self.extractor.llm).__name__
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
        extractions = self.extractor.extract_batch(batch)
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
