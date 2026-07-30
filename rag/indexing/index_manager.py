"""索引管理器：影子 collection 构建、校验、切换、回滚。

使用 Chroma 的 collection 机制实现安全重建：
1. 创建新 collection（如 documents_build_{job_id}）
2. 写入向量
3. 校验数量与元数据一致性
4. 切换活动 collection 指针
5. 保留旧 collection 以便回滚
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from app.core.config import (
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    INDEX_SCHEMA_VERSION,
    INGEST_BATCH_SIZE,
    VECTOR_DB_DIR,
)
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.indexing.chunker import split_documents
from rag.loaders.document_loader import is_supported_document, load_document
from rag.utils.file_hash import file_hash

logger = logging.getLogger(__name__)


@dataclass
class PreparedDocument:
    document_id: str
    source_path: str
    original_filename: str
    file_hash: str
    file_size: int
    vector_documents: list[Document]
    chunk_records: list[Any]


def _corpus_fingerprint(doc_dir: Path) -> str:
    files = sorted([p for p in doc_dir.rglob("*") if is_supported_document(p)])
    hasher = hashlib.sha256()
    for f in files:
        h = file_hash(f)
        rel = f.relative_to(doc_dir).as_posix()
        hasher.update(f"{rel}:{h}".encode())
    return hasher.hexdigest()[:16]


def _embedding_fingerprint() -> str:
    return f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}"


class IndexManager:
    def __init__(self, db, embedding=None):
        self.db = db
        self.repo = KnowledgeRepository(db)
        self._embedding = embedding
        self._chroma_client = None

    @property
    def embedding(self):
        if self._embedding is None:
            from rag.providers.factory import get_embedding_provider
            self._embedding = get_embedding_provider().get_model()
        return self._embedding

    @property
    def chroma_client(self):
        if self._chroma_client is None:
            import chromadb
            self._chroma_client = chromadb.PersistentClient(path=str(VECTOR_DB_DIR))
        return self._chroma_client

    def _make_chroma_store(self, collection_name: str):
        from rag.providers.factory import get_vector_store_provider
        return get_vector_store_provider().load(
            embedding=self.embedding,
            persist_dir=str(VECTOR_DB_DIR),
            collection_name=collection_name,
        )

    def _active_collection(self) -> str | None:
        state = self.repo.get_index_state()
        return state.active_collection_name

    # --- Sync classification ---

    def classify_files(self, doc_dir: Path) -> dict[str, list[dict]]:
        """把 data/docs 下的文件分为 added/modified/deleted/unchanged。"""
        result: dict[str, list[dict]] = {
            "added": [],
            "modified": [],
            "deleted": [],
            "unchanged": [],
        }

        existing_docs = {d.source_path: d for d in self.repo.list_documents()}
        files_on_disk = {}
        for path in sorted(doc_dir.rglob("*")):
            if not is_supported_document(path):
                continue
            rel = path.relative_to(doc_dir).as_posix()
            files_on_disk[rel] = path

        emb_fp = _embedding_fingerprint()

        for rel, path in files_on_disk.items():
            fh = file_hash(path)
            doc = existing_docs.get(rel)

            if doc is None:
                result["added"].append({"source_path": rel, "path": path, "file_hash": fh})
            elif (
                doc.status != "ready"
                or bool(doc.error_message)
                or doc.file_hash != fh
                or doc.index_schema_version != INDEX_SCHEMA_VERSION
                or doc.embedding_provider != EMBEDDING_PROVIDER
                or doc.embedding_model != EMBEDDING_MODEL
            ):
                result["modified"].append({
                    "source_path": rel, "path": path, "file_hash": fh, "document_id": doc.id
                })
            else:
                result["unchanged"].append({
                    "source_path": rel,
                    "path": path,
                    "file_hash": fh,
                    "document_id": doc.id,
                })

        for rel, doc in existing_docs.items():
            if rel not in files_on_disk:
                result["deleted"].append({
                    "source_path": rel, "document_id": doc.id, "file_hash": doc.file_hash
                })

        return result

    # --- Build single document ---

    def index_document(
        self,
        source_path: str,
        file_path: Path,
        file_hash_val: str,
        collection_name: str,
        document_id: str | None = None,
    ) -> int:
        """安全替换单个文档，失败时保留旧 Chunk 和旧向量。"""
        existing = self.repo.get_by_source_path(source_path)
        resolved_id = existing.id if existing else (document_id or str(uuid.uuid4()))
        if existing and document_id and existing.id != document_id:
            raise ValueError(
                f"Document ID mismatch for {source_path}: existing={existing.id}, requested={document_id}"
            )

        old_chunk_ids = set(
            self.repo.list_chunk_ids_for_document(resolved_id)
        )

        try:
            prepared = self._prepare_document(
                source_path=source_path,
                file_path=file_path,
                file_hash_val=file_hash_val,
                document_id=resolved_id,
            )
            self._write_prepared_vectors(
                prepared,
                collection_name,
                preserve_ids=old_chunk_ids,
            )
        except Exception as exc:
            self._mark_index_failure(
                source_path,
                file_path,
                file_hash_val,
                resolved_id,
                exc,
                keep_ready=bool(old_chunk_ids),
            )
            raise

        new_chunk_ids = {
            doc.metadata["chunk_id"] for doc in prepared.vector_documents
        }

        try:
            self._persist_prepared_document(prepared)
        except Exception:
            self.db.rollback()
            self._delete_vector_ids(collection_name, new_chunk_ids - old_chunk_ids)
            raise

        try:
            stale_vector_ids = (
                self._list_vector_ids_for_document(
                    collection_name,
                    resolved_id,
                )
                - new_chunk_ids
            )
            self._delete_vector_ids(collection_name, stale_vector_ids)
        except Exception as exc:
            doc = self.repo.get_by_source_path(source_path)
            if doc is not None:
                doc.error_message = (
                    f"Vector cleanup failed; retry indexing: {exc}"
                )[:1000]
                self.db.commit()
            raise

        self.repo.touch_index_state()
        return len(prepared.chunk_records)

    def _prepare_document(
        self,
        source_path: str,
        file_path: Path,
        file_hash_val: str,
        document_id: str,
    ) -> PreparedDocument:
        """解析并切片，但不修改 SQLite 或活动 collection。"""
        from app.models.knowledge_chunk import KnowledgeChunk

        docs = load_document(file_path)
        if not docs:
            raise ValueError(f"No readable content: {source_path}")

        chunk_result = split_documents(
            docs,
            document_id=document_id,
            file_hash_val=file_hash_val,
        )
        if not chunk_result.chunks:
            raise ValueError(f"No chunks generated: {source_path}")

        chunk_records: list[KnowledgeChunk] = []
        for chunk_doc, token_count, chunk_hash in zip(
            chunk_result.chunks,
            chunk_result.token_counts,
            chunk_result.content_hashes,
        ):
            metadata = dict(chunk_doc.metadata)
            metadata.update({
                "document_id": document_id,
                "file_hash": file_hash_val,
                "source": source_path,
                "filename": file_path.name,
            })
            chunk_doc.metadata = metadata
            chunk_records.append(KnowledgeChunk(
                id=metadata["chunk_id"],
                document_id=document_id,
                file_hash=file_hash_val,
                chunk_index=metadata["chunk_index"],
                content=chunk_doc.page_content,
                content_hash=chunk_hash,
                token_count=token_count,
                page=metadata.get("page"),
                section=metadata.get("section"),
                metadata_json={
                    key: value
                    for key, value in metadata.items()
                    if key not in ("chunk_id", "document_id", "file_hash", "chunk_index")
                },
            ))

        return PreparedDocument(
            document_id=document_id,
            source_path=source_path,
            original_filename=file_path.name,
            file_hash=file_hash_val,
            file_size=file_path.stat().st_size,
            vector_documents=chunk_result.chunks,
            chunk_records=chunk_records,
        )

    def _write_prepared_vectors(
        self,
        prepared: PreparedDocument,
        collection_name: str,
        *,
        preserve_ids: set[str] | None = None,
    ) -> None:
        chroma_store = self._make_chroma_store(collection_name)
        written_ids: set[str] = set()
        preserve_ids = preserve_ids or set()
        try:
            for start in range(0, len(prepared.vector_documents), INGEST_BATCH_SIZE):
                batch = prepared.vector_documents[start:start + INGEST_BATCH_SIZE]
                ids = [chunk.metadata["chunk_id"] for chunk in batch]
                chroma_store.add_documents(batch, ids=ids)
                written_ids.update(ids)
        except Exception as write_error:
            cleanup_ids = written_ids - preserve_ids
            if cleanup_ids:
                try:
                    self._delete_vector_ids(collection_name, cleanup_ids)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "Vector write failed and partial-vector cleanup also failed: "
                        f"{cleanup_error}"
                    ) from write_error
            raise

    def _persist_prepared_document(self, prepared: PreparedDocument) -> None:
        """在一个 SQLite 事务中替换文档元数据与 Chunk。"""
        from sqlalchemy import delete

        from app.models.knowledge_chunk import KnowledgeChunk
        from app.models.knowledge_document import KnowledgeDocument

        doc = self.repo.get_by_source_path(prepared.source_path)
        if doc is None:
            doc = KnowledgeDocument(
                id=prepared.document_id,
                source_path=prepared.source_path,
                original_filename=prepared.original_filename,
                file_hash=prepared.file_hash,
                file_size=prepared.file_size,
            )
            self.db.add(doc)
        elif doc.id != prepared.document_id:
            raise ValueError(
                f"Document ID changed while indexing {prepared.source_path}"
            )

        doc.original_filename = prepared.original_filename
        doc.file_hash = prepared.file_hash
        doc.file_size = prepared.file_size
        doc.status = "ready"
        doc.chunk_count = len(prepared.chunk_records)
        doc.index_schema_version = INDEX_SCHEMA_VERSION
        doc.embedding_provider = EMBEDDING_PROVIDER
        doc.embedding_model = EMBEDDING_MODEL
        doc.error_message = None
        doc.indexed_at = datetime.now(timezone.utc)

        self.db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id == prepared.document_id
            )
        )
        self.db.add_all(prepared.chunk_records)
        self.db.commit()

    def _mark_index_failure(
        self,
        source_path: str,
        file_path: Path,
        file_hash_val: str,
        document_id: str,
        error: Exception,
        *,
        keep_ready: bool,
    ) -> None:
        """记录失败，但已有可用索引时继续保持 ready。"""
        from app.models.knowledge_document import KnowledgeDocument

        self.db.rollback()
        doc = self.repo.get_by_source_path(source_path)
        if doc is None:
            doc = KnowledgeDocument(
                id=document_id,
                source_path=source_path,
                original_filename=file_path.name,
                file_hash=file_hash_val,
                file_size=file_path.stat().st_size if file_path.exists() else 0,
            )
            self.db.add(doc)

        doc.status = "ready" if keep_ready else "failed"
        doc.error_message = f"Last indexing attempt failed: {error}"[:1000]
        self.db.commit()

    def _delete_vector_ids(
        self,
        collection_name: str,
        chunk_ids: set[str],
        *,
        max_attempts: int = 3,
    ) -> None:
        if not chunk_ids:
            return
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                collection = self.chroma_client.get_collection(collection_name)
                collection.delete(ids=sorted(chunk_ids))
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Failed to delete %d vectors from %s (attempt %d/%d): %s",
                    len(chunk_ids),
                    collection_name,
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts:
                    time.sleep(0.1 * attempt)
        raise RuntimeError(
            f"Failed to delete {len(chunk_ids)} vectors from {collection_name}"
        ) from last_error

    def _list_vector_ids_for_document(
        self,
        collection_name: str,
        document_id: str,
    ) -> set[str]:
        collection = self.chroma_client.get_collection(collection_name)
        result = collection.get(
            where={"document_id": document_id},
            include=[],
        )
        return set(result.get("ids") or [])

    def delete_document(self, source_path: str, collection_name: str | None = None) -> bool:
        """删除文档及其所有片段和向量。"""
        doc = self.repo.get_by_source_path(source_path)
        if not doc:
            return False

        coll = collection_name or self._active_collection()
        if coll:
            self._delete_doc_from_collection(doc.id, coll)

        for chunk in self.repo.list_chunks_for_document(doc.id):
            self.db.delete(chunk)
        doc.status = "deleted"
        doc.chunk_count = 0
        doc.error_message = None
        self.repo.touch_index_state(commit=False)
        self.db.commit()
        return True

    def _delete_doc_from_collection(self, document_id: str, collection_name: str) -> None:
        coll = self.chroma_client.get_collection(collection_name)
        coll.delete(where={"document_id": document_id})

    # --- Shadow collection rebuild ---

    def rebuild(
        self,
        doc_dir: Path,
        dry_run: bool = False,
        job_id: str | None = None,
    ) -> dict:
        """安全全量重建，任一文件失败都不切换活动索引。"""
        job_id = job_id or str(uuid.uuid4())[:8]
        build_collection = f"documents_build_{job_id}"
        classification = self.classify_files(doc_dir)
        rebuild_items = [
            item
            for category in ("added", "modified", "unchanged")
            for item in classification[category]
        ]

        if dry_run:
            return {
                "dry_run": True,
                "build_collection": build_collection,
                "documents_to_rebuild": len(rebuild_items),
                "classification": {
                    k: [{"source_path": i["source_path"]} for i in v]
                    for k, v in classification.items()
                },
            }

        logger.info("Starting shadow collection rebuild: %s", build_collection)

        if not rebuild_items:
            return {
                "status": "validation_failed",
                "build_collection": build_collection,
                "errors": ["No supported documents found; active index was not changed"],
                "vector_count": 0,
                "chunk_count": 0,
            }

        self._reset_build_collection(build_collection)

        prepared_documents: list[PreparedDocument] = []
        expected_chunk_ids: set[str] = set()
        errors: list[str] = []

        for item in rebuild_items:
            try:
                prepared = self._prepare_document(
                    source_path=item["source_path"],
                    file_path=item["path"],
                    file_hash_val=item["file_hash"],
                    document_id=item.get("document_id") or str(uuid.uuid4()),
                )
                self._write_prepared_vectors(prepared, build_collection)
                prepared_documents.append(prepared)
                expected_chunk_ids.update(
                    chunk.metadata["chunk_id"]
                    for chunk in prepared.vector_documents
                )
            except Exception as exc:
                errors.append(f"{item['source_path']}: {exc}")
                logger.error("Failed to rebuild %s: %s", item["source_path"], exc)

        if errors:
            self._discard_build_collection(build_collection)
            return {
                "status": "failed",
                "build_collection": build_collection,
                "documents_indexed": len(prepared_documents),
                "documents_failed": len(errors),
                "total_chunks": len(expected_chunk_ids),
                "errors": errors[:20],
            }

        collection = self.chroma_client.get_collection(build_collection)
        vector_count = collection.count()
        actual_chunk_ids = set(collection.get(include=[])["ids"])
        validation_errors: list[str] = []

        if vector_count != len(expected_chunk_ids):
            validation_errors.append(
                f"Vector count ({vector_count}) != staged chunk count ({len(expected_chunk_ids)})"
            )
        missing_ids = expected_chunk_ids - actual_chunk_ids
        unexpected_ids = actual_chunk_ids - expected_chunk_ids
        if missing_ids:
            validation_errors.append(
                f"Missing vector IDs: {sorted(missing_ids)[:5]}"
            )
        if unexpected_ids:
            validation_errors.append(
                f"Unexpected vector IDs: {sorted(unexpected_ids)[:5]}"
            )

        if validation_errors:
            logger.error("Validation failed for %s: %s", build_collection, validation_errors)
            self._discard_build_collection(build_collection)
            return {
                "status": "validation_failed",
                "build_collection": build_collection,
                "errors": validation_errors,
                "vector_count": vector_count,
                "chunk_count": len(expected_chunk_ids),
            }

        try:
            old_collection = self._commit_rebuild(
                prepared_documents=prepared_documents,
                deleted_source_paths={
                    item["source_path"] for item in classification["deleted"]
                },
                build_collection=build_collection,
                corpus_fingerprint=_corpus_fingerprint(doc_dir),
            )
        except Exception as exc:
            self.db.rollback()
            self._discard_build_collection(build_collection)
            logger.error("Failed to commit rebuild %s: %s", build_collection, exc)
            return {
                "status": "failed",
                "build_collection": build_collection,
                "documents_indexed": 0,
                "documents_failed": len(prepared_documents),
                "total_chunks": 0,
                "errors": [f"SQLite commit failed: {exc}"],
            }

        logger.info(
            "Switched active collection: %s -> %s (chunks=%d)",
            old_collection, build_collection, len(expected_chunk_ids),
        )

        return {
            "status": "completed",
            "build_collection": build_collection,
            "old_collection": old_collection,
            "documents_indexed": len(prepared_documents),
            "documents_failed": 0,
            "total_chunks": len(expected_chunk_ids),
            "errors": [],
        }

    def _reset_build_collection(self, collection_name: str) -> None:
        active = self._active_collection()
        if collection_name == active:
            raise ValueError(f"Refusing to reset active collection: {collection_name}")
        try:
            self.chroma_client.delete_collection(collection_name)
        except Exception:
            pass
        self._make_chroma_store(collection_name)

    def _discard_build_collection(self, collection_name: str) -> None:
        if collection_name == self._active_collection():
            return
        try:
            self.chroma_client.delete_collection(collection_name)
        except Exception as exc:
            logger.warning(
                "Failed to discard incomplete collection %s: %s",
                collection_name,
                exc,
            )

    def _commit_rebuild(
        self,
        prepared_documents: list[PreparedDocument],
        deleted_source_paths: set[str],
        build_collection: str,
        corpus_fingerprint: str,
    ) -> str | None:
        """原子替换 SQLite Chunk 并切换活动 collection 指针。"""
        from sqlalchemy import delete, func, select

        from app.models.knowledge_chunk import KnowledgeChunk
        from app.models.knowledge_document import KnowledgeDocument

        state = self.repo.get_index_state()
        old_collection = state.active_collection_name
        existing_by_source = {
            doc.source_path: doc for doc in self.repo.list_documents()
        }

        self.db.execute(delete(KnowledgeChunk))

        for prepared in prepared_documents:
            doc = existing_by_source.get(prepared.source_path)
            if doc is None:
                doc = KnowledgeDocument(
                    id=prepared.document_id,
                    source_path=prepared.source_path,
                    original_filename=prepared.original_filename,
                    file_hash=prepared.file_hash,
                    file_size=prepared.file_size,
                )
                self.db.add(doc)
            elif doc.id != prepared.document_id:
                raise ValueError(
                    f"Document ID changed during rebuild: {prepared.source_path}"
                )

            doc.original_filename = prepared.original_filename
            doc.file_hash = prepared.file_hash
            doc.file_size = prepared.file_size
            doc.status = "ready"
            doc.chunk_count = len(prepared.chunk_records)
            doc.index_schema_version = INDEX_SCHEMA_VERSION
            doc.embedding_provider = EMBEDDING_PROVIDER
            doc.embedding_model = EMBEDDING_MODEL
            doc.error_message = None
            doc.indexed_at = datetime.now(timezone.utc)
            self.db.add_all(prepared.chunk_records)

        for source_path in deleted_source_paths:
            doc = existing_by_source.get(source_path)
            if doc is not None:
                doc.status = "deleted"
                doc.chunk_count = 0

        state.active_collection_name = build_collection
        state.index_schema_version = INDEX_SCHEMA_VERSION
        state.embedding_fingerprint = _embedding_fingerprint()
        state.corpus_fingerprint = corpus_fingerprint
        state.updated_at = datetime.now(timezone.utc)

        self.db.flush()
        staged_count = sum(
            len(prepared.chunk_records)
            for prepared in prepared_documents
        )
        persisted_count = self.db.scalar(
            select(func.count(KnowledgeChunk.id))
            .join(
                KnowledgeDocument,
                KnowledgeChunk.document_id == KnowledgeDocument.id,
            )
            .where(KnowledgeDocument.status == "ready")
        ) or 0
        if persisted_count != staged_count:
            raise ValueError(
                f"SQLite chunk count ({persisted_count}) != staged count ({staged_count})"
            )

        self.db.commit()
        return old_collection

    # --- Incremental sync ---

    def sync(self, doc_dir: Path, dry_run: bool = False) -> dict:
        """增量同步：只处理 added/modified/deleted。"""
        classification = self.classify_files(doc_dir)

        if dry_run:
            return {
                "dry_run": True,
                "classification": {
                    k: [{"source_path": i["source_path"]} for i in v]
                    for k, v in classification.items()
                },
            }

        active_coll = self._active_collection()
        if not active_coll:
            return self.rebuild(doc_dir, dry_run=False)

        total_indexed = 0
        succeeded = 0
        failed = 0
        deleted = 0
        errors: list[str] = []

        for category in ("added", "modified"):
            for item in classification[category]:
                try:
                    n = self.index_document(
                        item["source_path"],
                        item["path"],
                        item["file_hash"],
                        active_coll,
                    )
                    total_indexed += n
                    succeeded += 1
                except Exception as exc:
                    failed += 1
                    errors.append(f"{item['source_path']}: {exc}")

        for item in classification["deleted"]:
            try:
                if self.delete_document(item["source_path"], active_coll):
                    deleted += 1
            except Exception as exc:
                errors.append(f"delete {item['source_path']}: {exc}")

        return {
            "status": "completed" if not failed else "partial",
            "indexed": succeeded,
            "failed": failed,
            "deleted": deleted,
            "unchanged": len(classification["unchanged"]),
            "total_chunks_added": total_indexed,
            "errors": errors[:20],
        }

    # --- Collection management ---

    def list_collections(self) -> list[dict]:
        collections = self.chroma_client.list_collections()
        active = self._active_collection()
        result = []
        for coll in collections:
            name = coll.name if hasattr(coll, "name") else str(coll)
            try:
                count = coll.count() if hasattr(coll, "count") else 0
            except Exception:
                count = 0
            result.append({
                "name": name,
                "count": count,
                "is_active": name == active,
            })
        return result

    def rollback_collection(self, collection_name: str) -> dict:
        """切换活动 collection 指针到已有 collection。"""
        try:
            coll = self.chroma_client.get_collection(collection_name)
        except Exception:
            return {"status": "error", "message": f"Collection {collection_name} not found"}

        vector_ids = set(coll.get(include=[])["ids"])
        chunk_ids = {
            chunk.id for chunk in self.repo.list_all_active_chunks()
        }
        if vector_ids != chunk_ids:
            return {
                "status": "error",
                "message": (
                    "Rollback rejected because the collection does not match the "
                    "current SQLite chunks. Rebuild that index generation instead."
                ),
                "vector_count": len(vector_ids),
                "chunk_count": len(chunk_ids),
            }

        self.repo.set_active_collection(collection_name=collection_name)
        return {
            "status": "completed",
            "active_collection": collection_name,
            "vector_count": coll.count(),
        }

    def cleanup_collection(self, collection_name: str) -> dict:
        """删除非活动 collection。"""
        active = self._active_collection()
        if collection_name == active:
            return {"status": "error", "message": "Cannot delete active collection"}

        try:
            self.chroma_client.delete_collection(collection_name)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

        return {"status": "completed", "deleted_collection": collection_name}
