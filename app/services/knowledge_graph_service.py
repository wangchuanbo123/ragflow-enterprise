"""知识图谱构建服务。

负责：
- 按批次从 knowledge_chunks 中提取实体关系
- 记录每个 chunk 的图谱处理状态
- 支持断点继续和失败重试
- 清理孤立数据
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import (
    GRAPH_EXTRACTION_BATCH_SIZE,
    GRAPH_EXTRACTION_MAX_CHARS,
    GRAPH_EXTRACTION_MAX_RETRIES,
    GRAPH_MIN_CONFIDENCE,
    GRAPH_SCHEMA_VERSION,
)
from app.models.knowledge_chunk import KnowledgeChunk
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.knowledge_graph.extractor import GraphExtractor
from rag.knowledge_graph.sqlite_store import SqliteGraphStore

logger = logging.getLogger(__name__)


class GraphBuildService:
    def __init__(self, db: Session, extractor: GraphExtractor | None = None):
        self.db = db
        self.store = SqliteGraphStore(db)
        self.knowledge_repo = KnowledgeRepository(db)
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
        chunks = self._get_chunks_to_process(only_pending, retry_failed)
        total = len(chunks)
        succeeded = 0
        failed = 0
        skipped = 0
        errors: list[str] = []

        logger.info("Graph build: %d chunks to process", total)

        batch: list[dict] = []
        batch_chars = 0

        for chunk in chunks:
            if (
                batch
                and (
                    len(batch) >= GRAPH_EXTRACTION_BATCH_SIZE
                    or batch_chars + len(chunk.content) > GRAPH_EXTRACTION_MAX_CHARS
                )
            ):
                result = self._process_batch(batch)
                succeeded += result["succeeded"]
                failed += result["failed"]
                errors.extend(result["errors"])
                batch = []
                batch_chars = 0

            chunk_data = {
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "content": chunk.content,
            }
            batch.append(chunk_data)
            batch_chars += len(chunk.content)

            if len(batch) >= GRAPH_EXTRACTION_BATCH_SIZE:
                result = self._process_batch(batch)
                succeeded += result["succeeded"]
                failed += result["failed"]
                errors.extend(result["errors"])
                batch = []
                batch_chars = 0

        if batch:
            result = self._process_batch(batch)
            succeeded += result["succeeded"]
            failed += result["failed"]
            errors.extend(result["errors"])

        self.store.cleanup_orphans()

        stats = {
            "total_chunks": total,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "errors": errors[:20],
            "graph_stats": self.store.stats(),
        }
        logger.info("Graph build completed: %s", stats)
        return stats

    def _get_chunks_to_process(self, only_pending: bool, retry_failed: bool) -> list[KnowledgeChunk]:
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
                KnowledgeChunk.graph_schema_version != GRAPH_SCHEMA_VERSION,
            )

        return list(
            self.db.scalars(
                select(KnowledgeChunk)
                .where(
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
        succeeded = 0
        failed = 0
        errors: list[str] = []

        chunks_by_id: dict[str, KnowledgeChunk] = {}
        for chunk_data in batch:
            chunk_id = chunk_data["chunk_id"]
            chunk = self.db.get(KnowledgeChunk, chunk_id)
            if not chunk:
                continue
            chunks_by_id[chunk_id] = chunk

            chunk.graph_status = "processing"
        self.db.commit()

        try:
            extractions = self.extractor.extract_batch(batch)
            extractions_by_id = {
                extraction.chunk_id: extraction for extraction in extractions
            }
        except Exception as exc:
            for chunk_id, chunk in chunks_by_id.items():
                chunk.graph_status = "failed"
                chunk.graph_error = str(exc)[:500]
                errors.append(f"{chunk_id}: {str(exc)[:200]}")
            self.db.commit()
            logger.warning("Graph batch extraction failed: %s", exc)
            return {
                "succeeded": 0,
                "failed": len(chunks_by_id),
                "errors": errors,
            }

        for chunk_data in batch:
            chunk_id = chunk_data["chunk_id"]
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            try:
                extraction = extractions_by_id[chunk_id]
                extraction = extraction.model_copy(
                    update={
                        "relations": [
                            relation
                            for relation in extraction.relations
                            if relation.confidence >= GRAPH_MIN_CONFIDENCE
                        ]
                    }
                )

                # Always replace the old result. An empty new extraction must
                # remove graph facts left by an older schema or model.
                self.store.upsert_extraction(chunk_data, extraction)

                chunk.graph_status = "completed"
                chunk.graph_schema_version = GRAPH_SCHEMA_VERSION
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
