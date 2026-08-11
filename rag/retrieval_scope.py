"""Resolve request-level document scope against knowledge-base metadata."""

from __future__ import annotations

from app.core.database import SessionLocal
from app.repositories.knowledge_repository import KnowledgeRepository


def resolve_scope_document_ids(
    knowledge_base_id: str,
    scope: dict | None,
) -> set[str] | None:
    scope = scope or {}
    document_ids = set(scope.get("document_ids") or [])
    has_metadata_scope = any(
        (
            scope.get("tag_ids"),
            scope.get("folder_id"),
            scope.get("metadata"),
        )
    )
    if not document_ids and not has_metadata_scope:
        return None

    with SessionLocal() as db:
        repository = KnowledgeRepository(db, knowledge_base_id)
        candidates = repository.list_documents(
            folder_id=scope.get("folder_id"),
            tag_ids=scope.get("tag_ids") or None,
            metadata_filters=scope.get("metadata") or None,
        )
    candidate_ids = {document.id for document in candidates}
    if document_ids:
        return document_ids & candidate_ids if has_metadata_scope else document_ids
    return candidate_ids


def filter_documents_by_scope(documents, allowed_document_ids: set[str] | None):
    if allowed_document_ids is None:
        return list(documents)
    return [
        document
        for document in documents
        if document.metadata.get("document_id") in allowed_document_ids
    ]
