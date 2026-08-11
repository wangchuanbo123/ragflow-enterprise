"""知识文档与片段仓储。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.index_job import KnowledgeIndexState
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument


class KnowledgeRepository:
    def __init__(
        self,
        db: Session,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ):
        self.db = db
        self.knowledge_base_id = knowledge_base_id

    # --- Documents ---

    def get_by_source_path(self, source_path: str) -> KnowledgeDocument | None:
        return self.db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                KnowledgeDocument.source_path == source_path,
            )
        )

    def get_by_id(self, document_id: str) -> KnowledgeDocument | None:
        return self.db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
            )
        )

    def list_documents(
        self,
        *,
        folder_id: str | None = None,
        status: str | None = None,
        tag_ids: list[str] | None = None,
        filename_contains: str | None = None,
        metadata_filters: dict[str, str] | None = None,
    ) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == self.knowledge_base_id
        )
        if folder_id is not None:
            stmt = stmt.where(KnowledgeDocument.folder_id == folder_id)
        if status is not None:
            stmt = stmt.where(KnowledgeDocument.status == status)
        if filename_contains:
            stmt = stmt.where(
                KnowledgeDocument.original_filename.contains(filename_contains)
            )
        if tag_ids:
            from app.models.document_management import DocumentTagLink

            stmt = (
                stmt.join(
                    DocumentTagLink,
                    DocumentTagLink.document_id == KnowledgeDocument.id,
                )
                .where(
                    DocumentTagLink.knowledge_base_id == self.knowledge_base_id,
                    DocumentTagLink.tag_id.in_(tag_ids),
                )
                .distinct()
            )
        documents = list(
            self.db.scalars(stmt.order_by(KnowledgeDocument.created_at.desc()))
        )
        if metadata_filters:
            documents = [
                document
                for document in documents
                if all(
                    str((document.metadata_json or {}).get(key)) == value
                    for key, value in metadata_filters.items()
                )
            ]
        return documents

    def list_ready_documents(self) -> list[KnowledgeDocument]:
        return list(self.db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                KnowledgeDocument.status == "ready",
            )
        ))

    def upsert_document(self, doc: KnowledgeDocument) -> KnowledgeDocument:
        if doc.knowledge_base_id not in {None, self.knowledge_base_id}:
            raise ValueError("Document belongs to another knowledge base")
        doc.knowledge_base_id = self.knowledge_base_id
        self.db.add(doc)
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def delete_document(self, doc: KnowledgeDocument) -> None:
        if doc.knowledge_base_id != self.knowledge_base_id:
            raise ValueError("Document belongs to another knowledge base")
        self.db.delete(doc)
        self.db.commit()

    # --- Chunks ---

    def list_chunks_for_document(self, document_id: str) -> list[KnowledgeChunk]:
        return list(self.db.scalars(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                KnowledgeChunk.document_id == document_id,
            )
            .order_by(KnowledgeChunk.chunk_index)
        ))

    def list_chunk_ids_for_document(self, document_id: str) -> list[str]:
        return list(self.db.scalars(
            select(KnowledgeChunk.id)
            .where(
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                KnowledgeChunk.document_id == document_id,
            )
        ))

    def list_all_active_chunks(self) -> list[KnowledgeChunk]:
        return list(self.db.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                KnowledgeDocument.status == "ready",
            )
            .order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index)
        ))

    def delete_chunks_for_document(self, document_id: str) -> int:
        chunks = self.list_chunks_for_document(document_id)
        for c in chunks:
            self.db.delete(c)
        self.db.commit()
        return len(chunks)

    def add_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        for c in chunks:
            if c.knowledge_base_id not in {None, self.knowledge_base_id}:
                raise ValueError("Chunk belongs to another knowledge base")
            c.knowledge_base_id = self.knowledge_base_id
            self.db.add(c)
        self.db.commit()

    def count_chunks(self) -> int:
        return self.db.scalar(
            select(func.count(KnowledgeChunk.id))
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.knowledge_base_id == self.knowledge_base_id,
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                KnowledgeDocument.status == "ready",
            )
        ) or 0

    # --- Index state ---

    def get_index_state(self) -> KnowledgeIndexState:
        state = self.db.scalar(
            select(KnowledgeIndexState).where(
                KnowledgeIndexState.knowledge_base_id == self.knowledge_base_id
            )
        )
        if state is None:
            state = KnowledgeIndexState(
                knowledge_base_id=self.knowledge_base_id,
                active_collection_name=None,
                index_schema_version=2,
                embedding_fingerprint="",
                corpus_fingerprint="",
            )
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        return state

    def set_active_collection(
        self,
        collection_name: str | None,
        index_schema_version: int | None = None,
        embedding_fingerprint: str | None = None,
        corpus_fingerprint: str | None = None,
    ) -> KnowledgeIndexState:
        state = self.get_index_state()
        state.active_collection_name = collection_name
        if index_schema_version is not None:
            state.index_schema_version = index_schema_version
        if embedding_fingerprint is not None:
            state.embedding_fingerprint = embedding_fingerprint
        if corpus_fingerprint is not None:
            state.corpus_fingerprint = corpus_fingerprint
        self.db.commit()
        self.db.refresh(state)
        return state

    def touch_index_state(self, *, commit: bool = True) -> KnowledgeIndexState:
        """Advance the cross-process index revision timestamp."""
        state = self.get_index_state()
        state.updated_at = datetime.now(timezone.utc)
        if commit:
            self.db.commit()
            self.db.refresh(state)
        return state

    def get_index_signature(self) -> str:
        """Return a stable signature used to detect index changes."""
        state = self.get_index_state()
        updated_at = state.updated_at.isoformat() if state.updated_at else ""
        return "|".join(
            [
                state.active_collection_name or "",
                str(state.index_schema_version),
                state.embedding_fingerprint,
                state.corpus_fingerprint,
                updated_at,
            ]
        )
