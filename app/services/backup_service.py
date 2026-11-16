"""Portable, versioned backups for a knowledge base or one document graph."""

from __future__ import annotations

import io
import hashlib
import json
import logging
import uuid
import zipfile
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.backup import KnowledgeBackup
from app.models.code_graph import CodeFile, CodeRelation, CodeSnapshot, CodeSymbol, CodeVectorIndexState, EntityVectorIndexState, RelationVectorIndexState
from app.models.document_management import DocumentTag, DocumentTagLink, DocumentVersion, KnowledgeFolder
from app.models.graph_governance import (
    GraphCommunity,
    GraphCommunityMember,
    GraphCommunityState,
    GraphSchemaDefinition,
    KgEntityMerge,
)
from app.models.knowledge_base import KnowledgeBase, KnowledgeDataSource
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.index_job import IndexJob
from app.services.knowledge_mutation import serialized_knowledge_mutation

logger = logging.getLogger(__name__)
from app.models.knowledge_graph import (
    KgAlias,
    KgEntity,
    KgEntityMention,
    KgGraphCorrection,
    KgRelation,
    KgRelationEvidence,
)


WHOLE_MODELS = [
    KnowledgeDataSource,
    KnowledgeFolder,
    DocumentTag,
    KnowledgeDocument,
    DocumentTagLink,
    DocumentVersion,
    KnowledgeChunk,
    GraphSchemaDefinition,
    KgEntity,
    KgAlias,
    KgEntityMention,
    KgRelation,
    KgRelationEvidence,
    KgGraphCorrection,
    KgEntityMerge,
    CodeSnapshot,
    CodeFile,
    CodeSymbol,
    CodeRelation,
]


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"__datetime__": value.isoformat()}
    return value


def _row_dict(item) -> dict[str, Any]:
    return {column.name: _json_value(getattr(item, column.name)) for column in item.__table__.columns}


def _restore_value(column, value: Any) -> Any:
    if isinstance(value, dict) and "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    return value


def _restore_row(model, row: dict[str, Any]) -> dict[str, Any]:
    columns = {column.name: column for column in model.__table__.columns}
    return {name: _restore_value(columns[name], value) for name, value in row.items() if name in columns}


class BackupService:
    def __init__(self, db: Session, knowledge_base_id: str, object_store=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if object_store is None:
            from app.bootstrap.container import get_provider_container

            object_store = get_provider_container().object_store
        self.object_store = object_store

    def list(self, backup_type: str | None = None, document_id: str | None = None):
        query = select(KnowledgeBackup).where(
            KnowledgeBackup.knowledge_base_id == self.knowledge_base_id
        )
        if backup_type:
            query = query.where(KnowledgeBackup.backup_type == backup_type)
        if document_id:
            query = query.where(KnowledgeBackup.document_id == document_id)
        return list(self.db.scalars(query.order_by(KnowledgeBackup.created_at.desc())))

    @serialized_knowledge_mutation
    def create(
        self,
        backup_type: str,
        *,
        document_id: str | None = None,
        label: str | None = None,
        created_by: str | None = None,
        trigger: str = "manual",
        _allow_missing_objects: bool = False,
    ) -> KnowledgeBackup:
        if backup_type not in {"knowledge_base", "document_graph"}:
            raise ValueError("备份类型必须是 knowledge_base 或 document_graph")
        self._assert_idle()
        if backup_type == "document_graph":
            if not document_id:
                raise ValueError("文档图谱备份必须选择文档")
            document = self.db.scalar(select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
            ))
            if document is None:
                raise ValueError("文档不存在")
            scope_key = f"document:{document_id}"
            manifest = self._document_graph_manifest(document)
        else:
            document_id = None
            scope_key = "knowledge-base"
            manifest = self._knowledge_base_manifest(allow_missing_objects=_allow_missing_objects)

        version = (self.db.scalar(select(func.max(KnowledgeBackup.version_no)).where(
            KnowledgeBackup.knowledge_base_id == self.knowledge_base_id,
            KnowledgeBackup.scope_key == scope_key,
        )) or 0) + 1
        payload = self._archive(manifest)
        backup_id = __import__("uuid").uuid4().hex
        object_key = f"backups/{self.knowledge_base_id}/{scope_key.replace(':', '-')}/{version:06d}-{backup_id}.zip"
        ref = self.object_store.put_bytes(object_key, payload)
        backup = KnowledgeBackup(
            id=str(__import__("uuid").UUID(backup_id)),
            knowledge_base_id=self.knowledge_base_id,
            backup_type=backup_type,
            scope_key=scope_key,
            document_id=document_id,
            version_no=version,
            label=(label or "").strip() or None,
            object_key=ref.key,
            content_hash=ref.content_hash,
            file_size=ref.size,
            status="partial" if manifest.get("missing_objects") else "ready",
            trigger=trigger,
            manifest_json={
                "schema_version": manifest["schema_version"],
                "table_counts": {name: len(rows) for name, rows in manifest["tables"].items()},
                "object_count": len(manifest.get("objects", {})),
                "missing_objects": manifest.get("missing_objects", []),
            },
            created_by=created_by,
        )
        self.db.add(backup)
        self.db.commit()
        self.db.refresh(backup)
        return backup

    @serialized_knowledge_mutation
    def delete(self, backup_id: str) -> None:
        backup = self._get(backup_id)
        self.object_store.delete(backup.object_key)
        self.db.delete(backup)
        self.db.commit()

    @serialized_knowledge_mutation
    def restore(self, backup_id: str, restored_by: str | None = None) -> KnowledgeBackup:
        backup = self._get(backup_id)
        self._assert_idle()
        manifest, objects = self._read_archive(backup.object_key, expected_hash=backup.content_hash)
        self._validate_manifest(manifest, objects)
        if manifest.get("backup_type") == "knowledge_base":
            # Older archives may contain five simultaneous active contracts.
            # Normalize an in-memory copy before restoring into the new
            # single-schema constraints; keep the original archive unchanged.
            from app.services.schema_consolidation import normalize_schema_rows
            tables = manifest["tables"]
            manifest = {**manifest, "tables": {**tables,
                "graph_schema_definitions": normalize_schema_rows(tables.get("graph_schema_definitions", [])),
            }}
            self._validate_manifest(manifest, objects)
        if manifest.get("backup_type") != backup.backup_type or (
            backup.backup_type == "document_graph" and manifest.get("document_id") != backup.document_id
        ):
            raise ValueError("备份范围与记录不符")
        # The rollback itself is recoverable even when the user did not create a fresh version.
        self.create(
            backup.backup_type,
            document_id=backup.document_id,
            label=f"回退前自动保护 v{backup.version_no}",
            created_by=restored_by,
            trigger="pre_restore",
            _allow_missing_objects=True,
        )
        staged_index = None
        if backup.backup_type == "knowledge_base":
            # Build only the saved chunks in a fresh collection before changing
            # any live metadata. Re-parsing source files would lose image anchors
            # and could bring post-backup files back into the knowledge base.
            staged_index = self._stage_document_index(manifest["tables"])
            self._restore_whole_atomically(manifest, objects, staged_index)
        else:
            self._restore_document_graph(manifest)
        backup.restored_at = datetime.utcnow()
        backup.restored_by = restored_by
        self.db.commit()
        from app.services.rag_service import invalidate_rag_caches

        invalidate_rag_caches()
        self._queue_derived_rebuilds(restored_by, include_documents=backup.backup_type == "knowledge_base")
        return backup

    def _assert_idle(self) -> None:
        if self.db.scalar(select(IndexJob.id).where(
            IndexJob.knowledge_base_id == self.knowledge_base_id,
            IndexJob.status == "running",
        ).limit(1)):
            raise ValueError("知识库有正在运行的任务，请完成后再备份或恢复")

    @staticmethod
    def _safe_object_key(key: str) -> str:
        path = PurePosixPath(key)
        if not key or key.startswith("/") or "\\" in key or "\x00" in key or any(
            part in {".", ".."} or ":" in part for part in key.split("/")
        ) or path.as_posix() != key:
            raise ValueError("备份含无效对象路径")
        return key

    def _object_keys(self, tables: dict) -> set[str]:
        from app.core.config import IMAGE_URL_PREFIX
        from app.core.knowledge_bases import object_prefix_for_knowledge_base
        from rag.indexing.image_metadata import decode_images

        document_rows = tables.get("knowledge_documents", [])
        keys = {
            row["source_path"] for row in document_rows if row.get("status") != "deleted"
        } | {
            row["object_key"] for row in tables.get("document_versions", []) if row.get("object_key")
        } | {
            row["object_key"] for row in tables.get("code_files", []) if row.get("object_key")
        }
        document_ids = {row["id"] for row in document_rows}
        url_prefix = IMAGE_URL_PREFIX.rstrip("/") + "/"
        for row in tables.get("knowledge_chunks", []):
            if row.get("document_id") not in document_ids:
                raise ValueError("备份图片来源文档不存在")
            for image in decode_images((row.get("metadata_json") or {}).get("images")):
                url = str(image.get("url") or "")
                if not url.startswith(url_prefix):
                    continue
                key = self._safe_object_key(unquote(urlsplit(url[len(url_prefix):]).path))
                owner_prefix = "/".join(filter(None, (
                    object_prefix_for_knowledge_base(self.knowledge_base_id),
                    row["document_id"], "images",
                ))) + "/"
                if not key.startswith(owner_prefix):
                    raise ValueError("备份图片不属于当前文档")
                keys.add(key)
        return {self._safe_object_key(key) for key in keys if key}

    def _validate_manifest(self, manifest: dict, objects: dict) -> None:
        if manifest.get("knowledge_base_id") != self.knowledge_base_id:
            raise ValueError("备份不属于当前知识库")
        for rows in manifest.get("tables", {}).values():
            if any(row.get("knowledge_base_id") != self.knowledge_base_id for row in rows):
                raise ValueError("备份包含其他知识库的数据")
        expected = self._object_keys(manifest["tables"])
        missing = set(manifest.get("missing_objects") or [])
        if expected != set(objects) | missing or missing & set(objects):
            raise ValueError("备份对象不完整或包含未授权对象，无法安全恢复")
        self._check_object_ownership(expected)

    def _check_object_ownership(self, keys: set[str]) -> None:
        for key in keys:
            self._safe_object_key(key)
            if key.startswith("knowledge-bases/") and not key.startswith(f"knowledge-bases/{self.knowledge_base_id}/"):
                raise ValueError("备份对象属于其他知识库")
        for model, column in (
            (KnowledgeDocument, KnowledgeDocument.source_path),
            (DocumentVersion, DocumentVersion.object_key),
            (CodeFile, CodeFile.object_key),
        ):
            if keys and self.db.scalar(select(model.id).where(
                model.knowledge_base_id != self.knowledge_base_id,
                column.in_(keys),
            ).limit(1)):
                raise ValueError("备份对象路径与其他知识库冲突")

    def _stage_document_index(self, tables: dict) -> str:
        from app.bootstrap.container import get_provider_container
        from langchain_core.documents import Document
        from rag.indexing.image_metadata import encode_images_for_vector
        from rag.providers.interfaces import VectorIndexRef
        from app.core.config import INGEST_BATCH_SIZE

        container = get_provider_container()
        name = f"documents_restore_{uuid.uuid4().hex}"
        ref = VectorIndexRef(name=name, knowledge_base_id=self.knowledge_base_id)
        try:
            store = container.vector_store.open(container.embedding.get_model(), ref)
            sources = {row["id"]: row for row in tables.get("knowledge_documents", [])}
            documents = []
            for row in tables.get("knowledge_chunks", []):
                source = sources.get(row["document_id"])
                if source is None or source.get("status") != "ready":
                    continue
                metadata = {**(row.get("metadata_json") or {}), **{
                    key: row.get(key) for key in ("document_id", "file_hash", "chunk_index", "content_hash", "page", "section")
                }, "chunk_id": row["id"], "knowledge_base_id": self.knowledge_base_id,
                    "source": source["source_path"]}
                if "images" in metadata:
                    metadata["images"] = encode_images_for_vector(metadata["images"])
                metadata = {key: value for key, value in metadata.items() if value is not None}
                documents.append(Document(page_content=row["content"], metadata=metadata))
            for start in range(0, len(documents), INGEST_BATCH_SIZE):
                batch = documents[start:start + INGEST_BATCH_SIZE]
                store.add_documents(batch, ids=[item.metadata["chunk_id"] for item in batch])
            if container.vector_store.list_ids(ref) != {item.metadata["chunk_id"] for item in documents}:
                raise ValueError("恢复向量集合校验失败")
            return name
        except Exception:
            try:
                container.vector_store.delete_index(ref)
            except Exception:
                logger.exception("Could not discard failed restore collection")
            raise

    def _restore_whole_atomically(self, manifest: dict, objects: dict, index_name: str) -> None:
        from app.bootstrap.container import get_provider_container
        from app.repositories.knowledge_repository import KnowledgeRepository
        from rag.providers.interfaces import VectorIndexRef

        originals = {}
        objects_changed = False
        try:
            state = KnowledgeRepository(self.db, self.knowledge_base_id).get_index_state()
            saved_paths = {row["source_path"] for row in manifest["tables"].get("knowledge_documents", []) if row.get("status") != "deleted"}
            current_paths = set(self.db.scalars(select(KnowledgeDocument.source_path).where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
            )))
            stale_paths = current_paths - saved_paths
            self._check_object_ownership(stale_paths)
            for key in set(objects) | stale_paths:
                self._safe_object_key(key)
                if self.object_store.exists(key):
                    with self.object_store.materialize(key) as path:
                        originals[key] = path.read_bytes()
                else:
                    originals[key] = None
            objects_changed = True
            for key, payload in objects.items():
                self.object_store.put_bytes(key, payload)
            for key in stale_paths:
                self.object_store.delete(key)
            self._restore_knowledge_base(manifest, objects)
            state.active_collection_name = index_name
            state.updated_at = datetime.utcnow()
            for model in (EntityVectorIndexState, RelationVectorIndexState, CodeVectorIndexState):
                self.db.execute(update(model).where(model.knowledge_base_id == self.knowledge_base_id).values(status="stale"))
            self.db.execute(update(IndexJob).where(
                IndexJob.knowledge_base_id == self.knowledge_base_id,
                IndexJob.status == "pending",
            ).values(status="cancelled", cancel_requested=True, finished_at=datetime.utcnow()))
            self.db.commit()
        except Exception as failure:
            self.db.rollback()
            compensation_errors = []
            if objects_changed:
                for key, payload in originals.items():
                    try:
                        if payload is None:
                            self.object_store.delete(key)
                        else:
                            self.object_store.put_bytes(key, payload)
                    except Exception:
                        compensation_errors.append(key)
                        logger.exception("Could not compensate restored object %s", key)
            try:
                get_provider_container().vector_store.delete_index(VectorIndexRef(name=index_name, knowledge_base_id=self.knowledge_base_id))
            except Exception:
                logger.exception("Could not discard restore collection %s", index_name)
            if compensation_errors:
                raise RuntimeError("恢复失败，部分文件回滚失败；请从自动保护备份重新恢复") from failure
            raise

    def _get(self, backup_id: str) -> KnowledgeBackup:
        backup = self.db.scalar(select(KnowledgeBackup).where(
            KnowledgeBackup.id == backup_id,
            KnowledgeBackup.knowledge_base_id == self.knowledge_base_id,
        ))
        if backup is None:
            raise ValueError("备份不存在")
        return backup

    def _knowledge_base_manifest(self, *, allow_missing_objects: bool = False) -> dict[str, Any]:
        kb = self.db.get(KnowledgeBase, self.knowledge_base_id)
        if kb is None:
            raise ValueError("知识库不存在")
        tables: dict[str, list[dict[str, Any]]] = {}
        for model in WHOLE_MODELS:
            if "knowledge_base_id" not in model.__table__.columns:
                continue
            rows = self.db.scalars(select(model).where(
                model.knowledge_base_id == self.knowledge_base_id
            ))
            tables[model.__tablename__] = [_row_dict(row) for row in rows]
        objects: dict[str, str] = {}
        object_keys = self._object_keys(tables)
        missing_objects = []
        for index, key in enumerate(sorted(object_keys)):
            if not self.object_store.exists(key):
                if allow_missing_objects:
                    missing_objects.append(key)
                    continue
                raise ValueError(f"备份缺少对象：{key}")
            objects[key] = f"objects/{index:06d}.bin"
        return {
            "schema_version": 1,
            "backup_type": "knowledge_base",
            "knowledge_base_id": self.knowledge_base_id,
            "created_at": datetime.utcnow().isoformat(),
            "knowledge_base": _row_dict(kb),
            "tables": tables,
            "objects": objects,
            "missing_objects": missing_objects,
        }

    def _document_graph_manifest(self, document: KnowledgeDocument) -> dict[str, Any]:
        mentions = list(self.db.scalars(select(KgEntityMention).where(
            KgEntityMention.knowledge_base_id == self.knowledge_base_id,
            KgEntityMention.document_id == document.id,
        )))
        evidences = list(self.db.scalars(select(KgRelationEvidence).where(
            KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
            KgRelationEvidence.document_id == document.id,
        )))
        relation_ids = {item.relation_id for item in evidences}
        relations = list(self.db.scalars(select(KgRelation).where(KgRelation.id.in_(relation_ids)))) if relation_ids else []
        entity_ids = {item.entity_id for item in mentions}
        entity_ids.update(item.subject_entity_id for item in relations)
        entity_ids.update(item.object_entity_id for item in relations)
        entities = list(self.db.scalars(select(KgEntity).where(KgEntity.id.in_(entity_ids)))) if entity_ids else []
        aliases = list(self.db.scalars(select(KgAlias).where(KgAlias.entity_id.in_(entity_ids)))) if entity_ids else []
        corrections = list(self.db.scalars(select(KgGraphCorrection).where(
            KgGraphCorrection.knowledge_base_id == self.knowledge_base_id
        )))
        corrections = [item for item in corrections if (
            item.target_id in relation_ids or item.target_id in entity_ids or
            (item.payload_json or {}).get("document_id") == document.id
        )]
        tables = {
            KgEntity.__tablename__: [_row_dict(row) for row in entities],
            KgAlias.__tablename__: [_row_dict(row) for row in aliases],
            KgEntityMention.__tablename__: [_row_dict(row) for row in mentions],
            KgRelation.__tablename__: [_row_dict(row) for row in relations],
            KgRelationEvidence.__tablename__: [_row_dict(row) for row in evidences],
            KgGraphCorrection.__tablename__: [_row_dict(row) for row in corrections],
        }
        return {
            "schema_version": 1,
            "backup_type": "document_graph",
            "knowledge_base_id": self.knowledge_base_id,
            "document_id": document.id,
            "document_name": document.original_filename,
            "created_at": datetime.utcnow().isoformat(),
            "tables": tables,
            "objects": {},
        }

    def _archive(self, manifest: dict[str, Any]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
            for object_key, archive_name in manifest.get("objects", {}).items():
                with self.object_store.materialize(object_key) as path:
                    archive.write(path, archive_name)
        return output.getvalue()

    def _read_archive(self, object_key: str, expected_hash: str | None = None) -> tuple[dict[str, Any], dict[str, bytes]]:
        with self.object_store.materialize(object_key) as path:
            payload = path.read_bytes()
        if expected_hash and hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError("备份校验失败，文件可能已损坏")
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            objects = {
                key: archive.read(name)
                for key, name in manifest.get("objects", {}).items()
            }
        return manifest, objects

    def _restore_knowledge_base(self, manifest: dict[str, Any], objects: dict[str, bytes]) -> None:
        by_name = {model.__tablename__: model for model in WHOLE_MODELS}
        tables = manifest["tables"]
        backup_document_links = {
            item.id: item.document_id
            for item in self.db.scalars(select(KnowledgeBackup).where(
                KnowledgeBackup.knowledge_base_id == self.knowledge_base_id,
                KnowledgeBackup.document_id.is_not(None),
            ))
        }
        # Remove current scoped knowledge content in reverse FK order. Backups,
        # membership and the knowledge-base identity remain untouched.
        self.db.execute(delete(GraphCommunityMember).where(
            GraphCommunityMember.knowledge_base_id == self.knowledge_base_id
        ))
        self.db.execute(delete(GraphCommunity).where(
            GraphCommunity.knowledge_base_id == self.knowledge_base_id
        ))
        self.db.execute(delete(GraphCommunityState).where(
            GraphCommunityState.knowledge_base_id == self.knowledge_base_id
        ))
        for model in reversed(WHOLE_MODELS):
            if model.__tablename__ in tables:
                self.db.execute(delete(model).where(model.knowledge_base_id == self.knowledge_base_id))
        self.db.flush()
        deferred: list[tuple[Any, str, str, str]] = []
        for table_name, rows in tables.items():
            model = by_name.get(table_name)
            if model is None:
                continue
            for raw in rows:
                row = _restore_row(model, raw)
                for self_ref in ("parent_id", "parent_symbol_id"):
                    if row.get(self_ref):
                        deferred.append((model, row["id"], self_ref, row[self_ref]))
                        row[self_ref] = None
                self.db.add(model(**row))
            self.db.flush()
        for model, row_id, field, value in deferred:
            setattr(self.db.get(model, row_id), field, value)
        for backup_id, document_id in backup_document_links.items():
            record = self.db.get(KnowledgeBackup, backup_id)
            if record is not None and self.db.get(KnowledgeDocument, document_id) is not None:
                record.document_id = document_id
        kb_snapshot = manifest.get("knowledge_base", {})
        kb = self.db.get(KnowledgeBase, self.knowledge_base_id)
        for field in ("name", "description", "settings_json"):
            if field in kb_snapshot:
                setattr(kb, field, _restore_value(KnowledgeBase.__table__.columns[field], kb_snapshot[field]))
        # The caller switches the text-vector pointer and commits this metadata
        # together with the restored objects, with compensation on failure.

    def _restore_document_graph(self, manifest: dict[str, Any]) -> None:
        document_id = manifest.get("document_id")
        if not self.db.get(KnowledgeDocument, document_id):
            raise ValueError("目标文档已不存在，无法回退图谱")
        current_evidence = list(self.db.scalars(select(KgRelationEvidence).where(
            KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
            KgRelationEvidence.document_id == document_id,
        )))
        current_relation_ids = {row.relation_id for row in current_evidence}
        current_entity_ids = set(self.db.scalars(select(KgEntityMention.entity_id).where(
            KgEntityMention.knowledge_base_id == self.knowledge_base_id,
            KgEntityMention.document_id == document_id,
        )))
        self.db.execute(delete(KgEntityMention).where(
            KgEntityMention.knowledge_base_id == self.knowledge_base_id,
            KgEntityMention.document_id == document_id,
        ))
        self.db.execute(delete(KgRelationEvidence).where(
            KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
            KgRelationEvidence.document_id == document_id,
        ))
        for correction in self.db.scalars(select(KgGraphCorrection).where(
            KgGraphCorrection.knowledge_base_id == self.knowledge_base_id
        )):
            if (
                correction.target_id in current_relation_ids
                or correction.target_id in current_entity_ids
                or (correction.payload_json or {}).get("document_id") == document_id
            ):
                self.db.delete(correction)
        self.db.flush()
        models = [KgEntity, KgAlias, KgEntityMention, KgRelation, KgRelationEvidence, KgGraphCorrection]
        for model in models:
            for raw in manifest["tables"].get(model.__tablename__, []):
                row = _restore_row(model, raw)
                existing = self.db.get(model, row["id"])
                if existing is not None and existing.knowledge_base_id != self.knowledge_base_id:
                    raise ValueError("图谱备份引用其他知识库的数据")
                if existing is None:
                    self.db.add(model(**row))
                else:
                    for key, value in row.items():
                        setattr(existing, key, value)
            self.db.flush()
        # Relations which lost their final evidence should no longer be part of the base graph.
        for relation_id in current_relation_ids:
            relation = self.db.get(KgRelation, relation_id)
            if relation and not self.db.scalar(select(KgRelationEvidence.id).where(KgRelationEvidence.relation_id == relation_id).limit(1)):
                self.db.delete(relation)
        self.db.flush()
        for entity_id in current_entity_ids:
            entity = self.db.get(KgEntity, entity_id)
            if entity is None:
                continue
            has_mention = self.db.scalar(select(KgEntityMention.id).where(KgEntityMention.entity_id == entity_id).limit(1))
            has_relation = self.db.scalar(select(KgRelation.id).where(
                (KgRelation.subject_entity_id == entity_id) | (KgRelation.object_entity_id == entity_id)
            ).limit(1))
            if not has_mention and not has_relation:
                self.db.delete(entity)
        self.db.commit()

    def _queue_derived_rebuilds(self, requested_by: str | None, *, include_documents: bool) -> None:
        from app.services.code_vector_service import CodeVectorService
        from app.services.entity_vector_service import EntityVectorService
        from app.services.relation_vector_service import RelationVectorService

        EntityVectorService(self.db, self.knowledge_base_id).request_rebuild(requested_by)
        RelationVectorService(self.db, self.knowledge_base_id).request_rebuild(requested_by)
        CodeVectorService(self.db, self.knowledge_base_id).request_rebuild(requested_by)
        from app.services.graph_governance_service import GraphGovernanceService

        GraphGovernanceService(self.db, self.knowledge_base_id).mark_communities_stale()
        # Whole restores already activated a validated collection of saved
        # chunks. Do not reparse originals or schedule a filesystem-wide sync.
