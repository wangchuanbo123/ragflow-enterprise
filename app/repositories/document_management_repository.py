"""Repositories for document folders, tags, versions, and chunk browsing."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.document_management import (
    DocumentTag,
    DocumentTagLink,
    DocumentVersion,
    KnowledgeFolder,
)
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument


class DocumentManagementRepository:
    def __init__(self, db: Session, knowledge_base_id: str):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    def list_folders(self) -> list[KnowledgeFolder]:
        return list(
            self.db.scalars(
                select(KnowledgeFolder)
                .where(
                    KnowledgeFolder.knowledge_base_id == self.knowledge_base_id
                )
                .order_by(KnowledgeFolder.path)
            )
        )

    def get_folder(self, folder_id: str) -> KnowledgeFolder | None:
        return self.db.scalar(
            select(KnowledgeFolder).where(
                KnowledgeFolder.id == folder_id,
                KnowledgeFolder.knowledge_base_id == self.knowledge_base_id,
            )
        )

    def create_folder(
        self,
        *,
        name: str,
        parent_id: str | None = None,
        description: str | None = None,
    ) -> KnowledgeFolder:
        clean_name = name.strip().strip("/")
        if not clean_name or "/" in clean_name or ".." in clean_name:
            raise ValueError("Invalid folder name")
        parent = self.get_folder(parent_id) if parent_id else None
        if parent_id and parent is None:
            raise ValueError("Parent folder not found")
        path = f"{parent.path}/{clean_name}" if parent else f"/{clean_name}"
        if self.db.scalar(
            select(KnowledgeFolder.id).where(
                KnowledgeFolder.knowledge_base_id == self.knowledge_base_id,
                KnowledgeFolder.path == path,
            )
        ):
            raise ValueError("Folder already exists")
        folder = KnowledgeFolder(
            knowledge_base_id=self.knowledge_base_id,
            parent_id=parent_id,
            name=clean_name,
            path=path,
            description=description,
        )
        self.db.add(folder)
        self.db.commit()
        self.db.refresh(folder)
        return folder

    def list_tags(self) -> list[DocumentTag]:
        return list(
            self.db.scalars(
                select(DocumentTag)
                .where(DocumentTag.knowledge_base_id == self.knowledge_base_id)
                .order_by(DocumentTag.name)
            )
        )

    def get_tag(self, tag_id: str) -> DocumentTag | None:
        return self.db.scalar(
            select(DocumentTag).where(
                DocumentTag.id == tag_id,
                DocumentTag.knowledge_base_id == self.knowledge_base_id,
            )
        )

    def create_tag(self, *, name: str, color: str | None = None) -> DocumentTag:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Tag name is required")
        if self.db.scalar(
            select(DocumentTag.id).where(
                DocumentTag.knowledge_base_id == self.knowledge_base_id,
                DocumentTag.name == clean_name,
            )
        ):
            raise ValueError("Tag already exists")
        tag = DocumentTag(
            knowledge_base_id=self.knowledge_base_id,
            name=clean_name,
            color=color,
        )
        self.db.add(tag)
        self.db.commit()
        self.db.refresh(tag)
        return tag

    def set_document_tags(
        self,
        document_ids: list[str],
        tag_ids: list[str],
    ) -> int:
        documents = list(
            self.db.scalars(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeDocument.id.in_(document_ids),
                )
            )
        )
        if len(documents) != len(set(document_ids)):
            raise ValueError("One or more documents were not found")
        tags = list(
            self.db.scalars(
                select(DocumentTag).where(
                    DocumentTag.knowledge_base_id == self.knowledge_base_id,
                    DocumentTag.id.in_(tag_ids),
                )
            )
        ) if tag_ids else []
        if len(tags) != len(set(tag_ids)):
            raise ValueError("One or more tags were not found")

        existing = list(
            self.db.scalars(
                select(DocumentTagLink).where(
                    DocumentTagLink.knowledge_base_id == self.knowledge_base_id,
                    DocumentTagLink.document_id.in_(document_ids),
                )
            )
        )
        for link in existing:
            self.db.delete(link)
        for document_id in set(document_ids):
            for tag_id in set(tag_ids):
                self.db.add(
                    DocumentTagLink(
                        document_id=document_id,
                        tag_id=tag_id,
                        knowledge_base_id=self.knowledge_base_id,
                    )
                )
        self.db.commit()
        return len(documents)

    def list_tag_ids_for_document(self, document_id: str) -> list[str]:
        return list(
            self.db.scalars(
                select(DocumentTagLink.tag_id).where(
                    DocumentTagLink.knowledge_base_id == self.knowledge_base_id,
                    DocumentTagLink.document_id == document_id,
                )
            )
        )

    def document_tag_map(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for document_id, tag_id in self.db.execute(
            select(DocumentTagLink.document_id, DocumentTagLink.tag_id).where(
                DocumentTagLink.knowledge_base_id == self.knowledge_base_id
            )
        ):
            mapping.setdefault(document_id, []).append(tag_id)
        return mapping

    def move_documents(self, document_ids: list[str], folder_id: str | None) -> int:
        if folder_id and self.get_folder(folder_id) is None:
            raise ValueError("Folder not found")
        result = self.db.execute(
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                KnowledgeDocument.id.in_(document_ids),
            )
            .values(folder_id=folder_id)
        )
        if result.rowcount != len(set(document_ids)):
            self.db.rollback()
            raise ValueError("One or more documents were not found")
        self.db.commit()
        return result.rowcount

    def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return list(
            self.db.scalars(
                select(DocumentVersion)
                .where(
                    DocumentVersion.knowledge_base_id == self.knowledge_base_id,
                    DocumentVersion.document_id == document_id,
                )
                .order_by(DocumentVersion.version_no.desc())
            )
        )

    def get_version(
        self,
        document_id: str,
        version_id: str,
    ) -> DocumentVersion | None:
        return self.db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == version_id,
                DocumentVersion.knowledge_base_id == self.knowledge_base_id,
                DocumentVersion.document_id == document_id,
            )
        )

    def next_version_number(self, document_id: str) -> int:
        current = self.db.scalar(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.knowledge_base_id == self.knowledge_base_id,
                DocumentVersion.document_id == document_id,
            )
        )
        return int(current or 0) + 1

    def add_version(self, version: DocumentVersion) -> DocumentVersion:
        if version.knowledge_base_id != self.knowledge_base_id:
            raise ValueError("Version belongs to another knowledge base")
        self.db.execute(
            update(DocumentVersion)
            .where(
                DocumentVersion.knowledge_base_id == self.knowledge_base_id,
                DocumentVersion.document_id == version.document_id,
                DocumentVersion.status == "active",
            )
            .values(status="superseded")
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def get_chunk_by_id(self, chunk_id: str) -> KnowledgeChunk | None:
        return self.db.get(KnowledgeChunk, chunk_id)

    def list_chunks_around_index(
        self,
        document_id: str,
        chunk_index: int,
        *,
        before: int = 1,
        after: int = 1,
    ) -> list[KnowledgeChunk]:
        return list(
            self.db.scalars(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.document_id == document_id,
                    KnowledgeChunk.chunk_index >= chunk_index - before,
                    KnowledgeChunk.chunk_index <= chunk_index + after,
                )
                .order_by(KnowledgeChunk.chunk_index)
            )
        )

    def list_chunks(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[KnowledgeChunk]:
        return list(
            self.db.scalars(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.document_id == document_id,
                )
                .order_by(KnowledgeChunk.chunk_index)
                .offset(offset)
                .limit(limit)
            )
        )
