"""文档索引服务。

API、CLI 和 Worker 共用此服务。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import DOC_DIR, MAX_DOCUMENT_SIZE_MB
from app.models.index_job import IndexJob, IndexJobItem
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.errors import DocumentTooLargeError, DocumentValidationError
from rag.loaders.document_loader import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    """清理文件名，阻止路径穿越。"""
    if not name or "\x00" in name or "/" in name or "\\" in name or ".." in name:
        raise DocumentValidationError(f"Unsafe filename: {name}")
    name = os.path.basename(name)
    name = name.strip()
    if not name:
        raise DocumentValidationError("Empty filename")
    if Path(name).stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise DocumentValidationError(f"Reserved filename: {name}")
    return name


class DocumentIndexService:
    def __init__(self, db: Session):
        self.db = db
        self.job_repo = IndexJobRepository(db)
        self.knowledge_repo = KnowledgeRepository(db)

    def list_documents(self):
        return self.knowledge_repo.list_documents()

    def get_document(self, document_id: str):
        return self.knowledge_repo.get_by_id(document_id)

    def request_sync_job(self, requested_by: str | None = None) -> IndexJob:
        """创建增量同步任务。"""
        job = IndexJob(
            job_type="sync",
            status="pending",
            requested_by=requested_by,
        )
        return self.job_repo.create(job)

    def request_reindex(self, document_id: str, requested_by: str | None = None) -> IndexJob:
        """请求重新索引单个文档。"""
        doc = self.knowledge_repo.get_by_id(document_id)
        if not doc:
            raise DocumentValidationError(f"Document not found: {document_id}")

        job = IndexJob(
            job_type="file",
            status="pending",
            requested_by=requested_by,
            total_items=1,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=doc.source_path,
            action="update",
        )
        self.job_repo.add_items([item])
        return created

    def request_delete(self, document_id: str, requested_by: str | None = None) -> IndexJob:
        """请求删除文档。"""
        doc = self.knowledge_repo.get_by_id(document_id)
        if not doc:
            raise DocumentValidationError(f"Document not found: {document_id}")

        doc.status = "pending_delete"
        self.knowledge_repo.upsert_document(doc)

        job = IndexJob(
            job_type="delete",
            status="pending",
            requested_by=requested_by,
            total_items=1,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=doc.source_path,
            action="delete",
        )
        self.job_repo.add_items([item])
        return created

    def register_upload(self, filename: str, file_data: bytes, requested_by: str | None = None) -> tuple[str, Path]:
        """注册上传文件。返回 (document_id, saved_path)。"""
        safe_name = safe_filename(filename)
        if Path(safe_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise DocumentValidationError(
                f"Unsupported document type. Supported extensions: {supported}"
            )

        if len(file_data) > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
            raise DocumentTooLargeError(
                f"File too large: {len(file_data)} bytes (max {MAX_DOCUMENT_SIZE_MB}MB)"
            )

        import hashlib
        file_hash_val = hashlib.sha256(file_data).hexdigest()

        # Check if document already exists by hash
        existing = None
        for doc in self.knowledge_repo.list_documents():
            if doc.file_hash == file_hash_val and doc.status != "deleted":
                existing = doc
                break

        import uuid
        document_id = existing.id if existing else str(uuid.uuid4())

        # Reuse the canonical path for duplicate content instead of leaving an
        # unindexed duplicate file behind.
        if existing is not None:
            target = DOC_DIR / existing.source_path
        else:
            target = DOC_DIR / document_id / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.tmp"

        with open(tmp, "wb") as f:
            f.write(file_data)
        os.replace(str(tmp), str(target))

        return document_id, target

    def create_upload_job(
        self, filename: str, file_data: bytes, requested_by: str | None = None
    ) -> tuple[IndexJob, str]:
        """上传文件并创建索引任务。返回 (job, document_id)。"""
        document_id, target_path = self.register_upload(filename, file_data, requested_by)

        source_path = target_path.relative_to(DOC_DIR).as_posix()
        existing_doc = self.knowledge_repo.get_by_id(document_id)
        if existing_doc is not None:
            source_path = existing_doc.source_path
        else:
            from app.models.knowledge_document import KnowledgeDocument
            from rag.utils.file_hash import file_hash

            existing_doc = KnowledgeDocument(
                id=document_id,
                source_path=source_path,
                original_filename=target_path.name,
                file_hash=file_hash(target_path),
                file_size=target_path.stat().st_size,
                status="pending",
            )
            self.knowledge_repo.upsert_document(existing_doc)

        job = IndexJob(
            job_type="file",
            status="pending",
            requested_by=requested_by,
            total_items=1,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=source_path,
            action="update" if existing_doc.status == "ready" else "add",
        )
        self.job_repo.add_items([item])
        return created, document_id
