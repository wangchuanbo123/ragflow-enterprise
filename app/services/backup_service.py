"""Portable, versioned backups for a knowledge base or one document graph."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.backup import KnowledgeBackup
from app.models.code_graph import CodeFile, CodeRelation, CodeSnapshot, CodeSymbol
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

    def create(
        self,
        backup_type: str,
        *,
        document_id: str | None = None,
        label: str | None = None,
        created_by: str | None = None,
        trigger: str = "manual",
    ) -> KnowledgeBackup:
        if backup_type not in {"knowledge_base", "document_graph"}:
            raise ValueError("备份类型必须是 knowledge_base 或 document_graph")
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
            manifest = self._knowledge_base_manifest()

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
            status="ready",
            trigger=trigger,
            manifest_json={
                "schema_version": manifest["schema_version"],
                "table_counts": {name: len(rows) for name, rows in manifest["tables"].items()},
                "object_count": len(manifest.get("objects", {})),
            },
            created_by=created_by,
        )
        self.db.add(backup)
        self.db.commit()
        self.db.refresh(backup)
        return backup

    def delete(self, backup_id: str) -> None:
        backup = self._get(backup_id)
        self.object_store.delete(backup.object_key)
        self.db.delete(backup)
        self.db.commit()

    def restore(self, backup_id: str, restored_by: str | None = None) -> KnowledgeBackup:
        backup = self._get(backup_id)
        # The rollback itself is recoverable even when the user did not create a fresh version.
        self.create(
            backup.backup_type,
            document_id=backup.document_id,
            label=f"回退前自动保护 v{backup.version_no}",
            created_by=restored_by,
            trigger="pre_restore",
        )
        manifest, objects = self._read_archive(backup.object_key)
        if manifest.get("knowledge_base_id") != self.knowledge_base_id:
            raise ValueError("备份不属于当前知识库")
        if backup.backup_type == "knowledge_base":
            self._restore_knowledge_base(manifest, objects)
        else:
            self._restore_document_graph(manifest)
        backup.restored_at = datetime.utcnow()
        backup.restored_by = restored_by
        self.db.commit()
        self._queue_derived_rebuilds(restored_by, include_documents=backup.backup_type == "knowledge_base")
        return backup

    def _get(self, backup_id: str) -> KnowledgeBackup:
        backup = self.db.scalar(select(KnowledgeBackup).where(
            KnowledgeBackup.id == backup_id,
            KnowledgeBackup.knowledge_base_id == self.knowledge_base_id,
        ))
        if backup is None:
            raise ValueError("备份不存在")
        return backup

    def _knowledge_base_manifest(self) -> dict[str, Any]:
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
        object_keys = {
            row["source_path"] for row in tables.get("knowledge_documents", [])
        } | {
            row["object_key"] for row in tables.get("document_versions", [])
        } | {
            row["object_key"] for row in tables.get("code_files", []) if row.get("object_key")
        }
        for index, key in enumerate(sorted(object_keys)):
            if key and self.object_store.exists(key):
                objects[key] = f"objects/{index:06d}.bin"
        return {
            "schema_version": 1,
            "backup_type": "knowledge_base",
            "knowledge_base_id": self.knowledge_base_id,
            "created_at": datetime.utcnow().isoformat(),
            "knowledge_base": _row_dict(kb),
            "tables": tables,
            "objects": objects,
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

    def _read_archive(self, object_key: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        with self.object_store.materialize(object_key) as path:
            payload = path.read_bytes()
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
        for key, payload in objects.items():
            self.object_store.put_bytes(key, payload)
        self.db.commit()

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
        if include_documents:
            from app.services.document_index_service import DocumentIndexService

            service = DocumentIndexService(self.db, self.knowledge_base_id)
            for document_id in self.db.scalars(select(KnowledgeDocument.id).where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id
            )):
                service.request_reindex(document_id, requested_by)
