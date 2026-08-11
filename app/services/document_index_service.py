"""文档索引服务。

API、CLI 和 Worker 共用此服务。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.adapters.interfaces import ObjectRef, ObjectStoreProvider, TaskQueueProvider
from app.core.config import (
    INDEX_JOB_MAX_ATTEMPTS,
    MAX_BATCH_UPLOAD_FILES,
    MAX_DOCUMENT_SIZE_MB,
)
from app.core.knowledge_bases import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    object_prefix_for_knowledge_base,
)
from app.models.document_management import DocumentVersion
from app.models.index_job import IndexJob, IndexJobItem
from app.repositories.document_management_repository import DocumentManagementRepository
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
    def __init__(
        self,
        db: Session,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        object_store: ObjectStoreProvider | None = None,
        task_queue: TaskQueueProvider | None = None,
    ):
        if object_store is None or task_queue is None:
            from app.bootstrap.container import get_provider_container

            container = get_provider_container()
            object_store = object_store or container.object_store
            task_queue = task_queue or container.task_queue
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.job_repo = IndexJobRepository(db, knowledge_base_id)
        self.knowledge_repo = KnowledgeRepository(db, knowledge_base_id)
        self.management_repo = DocumentManagementRepository(db, knowledge_base_id)
        self.object_store = object_store
        self.task_queue = task_queue

    def _signal_job(self, job: IndexJob) -> IndexJob:
        self.task_queue.enqueue(job.id)
        return job

    @staticmethod
    def _version_object_key(
        canonical_key: str,
        version_no: int,
        filename: str,
    ) -> str:
        canonical = PurePosixPath(canonical_key)
        return (
            canonical.parent / ".versions" / str(version_no) / filename
        ).as_posix()

    def _activate_document_version(
        self,
        document,
        filename: str,
        file_data: bytes,
        requested_by: str | None,
    ) -> DocumentVersion:
        """Persist an immutable version and move the canonical object pointer."""
        versions = self.management_repo.list_versions(document.id)
        if versions:
            oldest = versions[-1]
            if oldest.object_key == document.source_path:
                with self.object_store.materialize(document.source_path) as old_path:
                    archived_ref = self.object_store.put_bytes(
                        self._version_object_key(
                            document.source_path,
                            oldest.version_no,
                            document.original_filename,
                        ),
                        old_path.read_bytes(),
                    )
                oldest.object_key = archived_ref.key
                self.db.commit()

        version_no = self.management_repo.next_version_number(document.id)
        immutable_ref = self.object_store.put_bytes(
            self._version_object_key(document.source_path, version_no, filename),
            file_data,
        )
        canonical_ref = self.object_store.put_bytes(document.source_path, file_data)
        version = self.management_repo.add_version(
            DocumentVersion(
                knowledge_base_id=self.knowledge_base_id,
                document_id=document.id,
                version_no=version_no,
                object_key=immutable_ref.key,
                file_hash=immutable_ref.content_hash,
                file_size=immutable_ref.size,
                mime_type=document.mime_type,
                status="active",
                created_by=requested_by,
            )
        )

        document.original_filename = filename
        document.file_hash = canonical_ref.content_hash
        document.file_size = canonical_ref.size
        document.current_version_no = version_no
        document.status = "pending"
        document.error_message = None
        self.db.commit()
        return version

    def list_documents(self, **filters):
        return self.knowledge_repo.list_documents(**filters)

    def get_document(self, document_id: str):
        return self.knowledge_repo.get_by_id(document_id)

    def request_sync_job(self, requested_by: str | None = None) -> IndexJob:
        """创建增量同步任务。"""
        job = IndexJob(
            job_type="sync",
            status="pending",
            requested_by=requested_by,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        return self._signal_job(self.job_repo.create(job))

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
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=doc.source_path,
            action="update",
        )
        self.job_repo.add_items([item])
        return self._signal_job(created)

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
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=doc.source_path,
            action="delete",
        )
        self.job_repo.add_items([item])
        return self._signal_job(created)

    def register_upload(
        self,
        filename: str,
        file_data: bytes,
        requested_by: str | None = None,
    ) -> tuple[str, ObjectRef]:
        """Register an uploaded file and return its logical object reference."""
        del requested_by
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

        # Reuse the canonical object key for duplicate content.
        if existing is not None:
            object_key = existing.source_path
        else:
            prefix = object_prefix_for_knowledge_base(self.knowledge_base_id)
            object_key = "/".join(
                part for part in (prefix, document_id, safe_name) if part
            )

        return document_id, self.object_store.put_bytes(object_key, file_data)

    def create_upload_job(
        self, filename: str, file_data: bytes, requested_by: str | None = None
    ) -> tuple[IndexJob, str]:
        """上传文件并创建索引任务。返回 (job, document_id)。"""
        document_id, object_ref = self.register_upload(filename, file_data, requested_by)

        source_path = object_ref.key
        existing_doc = self.knowledge_repo.get_by_id(document_id)
        if existing_doc is not None:
            source_path = existing_doc.source_path
        else:
            from app.models.knowledge_document import KnowledgeDocument

            existing_doc = KnowledgeDocument(
                id=document_id,
                knowledge_base_id=self.knowledge_base_id,
                source_path=source_path,
                original_filename=Path(source_path).name,
                file_hash=object_ref.content_hash,
                file_size=object_ref.size,
                status="pending",
            )
            self.knowledge_repo.upsert_document(existing_doc)
            version_ref = self.object_store.put_bytes(
                self._version_object_key(
                    source_path,
                    1,
                    existing_doc.original_filename,
                ),
                file_data,
            )
            self.management_repo.add_version(
                DocumentVersion(
                    knowledge_base_id=self.knowledge_base_id,
                    document_id=document_id,
                    version_no=1,
                    object_key=version_ref.key,
                    file_hash=version_ref.content_hash,
                    file_size=version_ref.size,
                    mime_type=existing_doc.mime_type,
                    status="active",
                    created_by=requested_by,
                )
            )

        job = IndexJob(
            job_type="file",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        created = self.job_repo.create(job)

        item = IndexJobItem(
            job_id=created.id,
            document_id=document_id,
            source_path=source_path,
            action="update" if existing_doc.status == "ready" else "add",
        )
        self.job_repo.add_items([item])
        self._signal_job(created)
        return created, document_id

    def create_batch_upload_job(
        self,
        files: list[tuple[str, bytes]],
        requested_by: str | None = None,
    ) -> tuple[IndexJob, list[str], list[dict]]:
        """Upload many files into one job while reporting rejected files."""
        if not files:
            raise DocumentValidationError("At least one file is required")
        if len(files) > MAX_BATCH_UPLOAD_FILES:
            raise DocumentValidationError(
                f"A batch can contain at most {MAX_BATCH_UPLOAD_FILES} files"
            )

        accepted: dict[str, tuple[str, str]] = {}
        rejected: list[dict] = []
        for filename, file_data in files:
            try:
                document_id, object_ref = self.register_upload(
                    filename,
                    file_data,
                    requested_by,
                )
                source_path = object_ref.key
                document = self.knowledge_repo.get_by_id(document_id)
                if document is None:
                    from app.models.knowledge_document import KnowledgeDocument

                    document = KnowledgeDocument(
                        id=document_id,
                        knowledge_base_id=self.knowledge_base_id,
                        source_path=source_path,
                        original_filename=Path(source_path).name,
                        file_hash=object_ref.content_hash,
                        file_size=object_ref.size,
                        status="pending",
                    )
                    self.knowledge_repo.upsert_document(document)
                    version_ref = self.object_store.put_bytes(
                        self._version_object_key(
                            source_path,
                            1,
                            document.original_filename,
                        ),
                        file_data,
                    )
                    self.management_repo.add_version(
                        DocumentVersion(
                            knowledge_base_id=self.knowledge_base_id,
                            document_id=document_id,
                            version_no=1,
                            object_key=version_ref.key,
                            file_hash=version_ref.content_hash,
                            file_size=version_ref.size,
                            mime_type=document.mime_type,
                            status="active",
                            created_by=requested_by,
                        )
                    )
                else:
                    source_path = document.source_path
                accepted[document_id] = (
                    source_path,
                    "update" if document.status == "ready" else "add",
                )
            except Exception as exc:
                if not isinstance(exc, (DocumentValidationError, DocumentTooLargeError)):
                    raise
                rejected.append({"filename": filename, "error": str(exc)})

        if not accepted:
            raise DocumentValidationError("No valid files were accepted")

        job = self.job_repo.create(
            IndexJob(
                job_type="file",
                status="pending",
                requested_by=requested_by,
                total_items=len(accepted),
                max_attempts=INDEX_JOB_MAX_ATTEMPTS,
                options_json={"rejected_files": rejected} if rejected else None,
            )
        )
        self.job_repo.add_items(
            [
                IndexJobItem(
                    job_id=job.id,
                    document_id=document_id,
                    source_path=source_path,
                    action=action,
                )
                for document_id, (source_path, action) in accepted.items()
            ]
        )
        self._signal_job(job)
        return job, list(accepted), rejected

    def create_document_version(
        self,
        document_id: str,
        filename: str,
        file_data: bytes,
        requested_by: str | None = None,
    ) -> tuple[IndexJob, DocumentVersion]:
        document = self.knowledge_repo.get_by_id(document_id)
        if document is None:
            raise DocumentValidationError(f"Document not found: {document_id}")
        safe_name = safe_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise DocumentValidationError(f"Unsupported document type: {suffix}")
        if suffix != Path(document.source_path).suffix.lower():
            raise DocumentValidationError(
                "A new version must keep the current document extension"
            )
        if len(file_data) > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
            raise DocumentTooLargeError(
                f"File too large: {len(file_data)} bytes (max {MAX_DOCUMENT_SIZE_MB}MB)"
            )

        version = self._activate_document_version(
            document,
            safe_name,
            file_data,
            requested_by,
        )

        job = IndexJob(
            job_type="file",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        created = self.job_repo.create(job)
        self.job_repo.add_items(
            [
                IndexJobItem(
                    job_id=created.id,
                    document_id=document.id,
                    source_path=document.source_path,
                    action="update",
                )
            ]
        )
        self._signal_job(created)
        return created, version

    def restore_document_version(
        self,
        document_id: str,
        version_id: str,
        requested_by: str | None = None,
    ) -> tuple[IndexJob, DocumentVersion]:
        """Restore by creating a new immutable version from historical content."""
        document = self.knowledge_repo.get_by_id(document_id)
        version = self.management_repo.get_version(document_id, version_id)
        if document is None or version is None:
            raise DocumentValidationError("Document version not found")
        with self.object_store.materialize(version.object_key) as version_path:
            file_data = version_path.read_bytes()
        return self.create_document_version(
            document_id,
            document.original_filename,
            file_data,
            requested_by,
        )

    def create_data_source_sync_job(
        self,
        source_id: str,
        files: list[tuple[str, bytes]],
        requested_by: str | None = None,
        rejected: list[dict] | None = None,
        *,
        source_type: str = "local_directory",
        deletion_policy: str = "ignore",
        sync_cursor: dict | None = None,
    ) -> tuple[IndexJob, list[str], list[str], list[dict]]:
        """Stage an incremental connector snapshot as one auditable index job."""
        import hashlib
        import uuid

        rejected = list(rejected or [])
        existing_by_relative_path = {
            str((document.metadata_json or {}).get("source_relative_path")): document
            for document in self.knowledge_repo.list_documents()
            if (document.metadata_json or {}).get("data_source_id") == source_id
            and (document.metadata_json or {}).get("source_relative_path")
            and document.status != "deleted"
        }
        staged: list[tuple[str, str, str]] = []
        skipped: list[str] = []
        incoming_paths = {relative_path for relative_path, _ in files}
        existing_by_hash: dict[str, list] = {}
        for existing_path, existing_document in existing_by_relative_path.items():
            if existing_path not in incoming_paths:
                existing_by_hash.setdefault(existing_document.file_hash, []).append(existing_document)
        renamed_from: set[str] = set()

        for relative_path, file_data in files:
            try:
                filename = safe_filename(Path(relative_path).name)
                suffix = Path(filename).suffix.lower()
                if suffix not in SUPPORTED_EXTENSIONS:
                    raise DocumentValidationError(f"Unsupported document type: {suffix}")
                if len(file_data) > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
                    raise DocumentTooLargeError(
                        f"File too large: {len(file_data)} bytes (max {MAX_DOCUMENT_SIZE_MB}MB)"
                    )
                file_hash = hashlib.sha256(file_data).hexdigest()
                document = existing_by_relative_path.get(relative_path)
                if document is not None and document.file_hash == file_hash:
                    skipped.append(relative_path)
                    continue

                # A unique same-content disappearance/addition pair is a rename,
                # so keep document/version identity and avoid needless embedding.
                rename_candidates = existing_by_hash.get(file_hash, []) if document is None else []
                if len(rename_candidates) == 1:
                    document = rename_candidates[0]
                    old_path = str((document.metadata_json or {}).get("source_relative_path"))
                    metadata = dict(document.metadata_json or {})
                    metadata.update({
                        "data_source_id": source_id,
                        "source_relative_path": relative_path,
                        "source_type": source_type,
                    })
                    document.metadata_json = metadata
                    document.original_filename = filename
                    self.db.commit()
                    renamed_from.add(old_path)
                    existing_by_hash[file_hash] = []
                    skipped.append(relative_path)
                    continue

                if document is None:
                    from app.models.knowledge_document import KnowledgeDocument

                    document_id = str(uuid.uuid4())
                    prefix = object_prefix_for_knowledge_base(self.knowledge_base_id)
                    source_path = "/".join(
                        part for part in (prefix, document_id, filename) if part
                    )
                    canonical_ref = self.object_store.put_bytes(source_path, file_data)
                    document = KnowledgeDocument(
                        id=document_id,
                        knowledge_base_id=self.knowledge_base_id,
                        source_path=source_path,
                        original_filename=filename,
                        file_hash=canonical_ref.content_hash,
                        file_size=canonical_ref.size,
                        status="pending",
                        metadata_json={
                            "data_source_id": source_id,
                            "source_relative_path": relative_path,
                            "source_type": source_type,
                        },
                    )
                    self.knowledge_repo.upsert_document(document)
                    version_ref = self.object_store.put_bytes(
                        self._version_object_key(source_path, 1, filename),
                        file_data,
                    )
                    self.management_repo.add_version(
                        DocumentVersion(
                            knowledge_base_id=self.knowledge_base_id,
                            document_id=document.id,
                            version_no=1,
                            object_key=version_ref.key,
                            file_hash=version_ref.content_hash,
                            file_size=version_ref.size,
                            mime_type=document.mime_type,
                            status="active",
                            created_by=requested_by,
                        )
                    )
                    action = "add"
                else:
                    self._activate_document_version(
                        document,
                        filename,
                        file_data,
                        requested_by,
                    )
                    metadata = dict(document.metadata_json or {})
                    metadata.update({
                        "data_source_id": source_id,
                        "source_relative_path": relative_path,
                        "source_type": source_type,
                    })
                    document.metadata_json = metadata
                    self.db.commit()
                    action = "update"
                staged.append((document.id, document.source_path, action))
            except (DocumentValidationError, DocumentTooLargeError, OSError) as exc:
                rejected.append({"path": relative_path, "error": str(exc)})

        removed_paths = sorted(set(existing_by_relative_path) - incoming_paths - renamed_from)
        if deletion_policy == "delete":
            for relative_path in removed_paths:
                document = existing_by_relative_path[relative_path]
                document.status = "pending_delete"
                staged.append((document.id, document.source_path, "delete"))
            if removed_paths:
                self.db.commit()

        options = {
            "data_source_id": source_id,
            "source_type": source_type,
            "deletion_policy": deletion_policy,
            "removed_paths": removed_paths,
            "sync_cursor": sync_cursor or {},
            "skipped_paths": skipped,
            "rejected_files": rejected,
        }
        job = self.job_repo.create(
            IndexJob(
                job_type="file",
                status="pending",
                requested_by=requested_by,
                total_items=len(staged),
                max_attempts=INDEX_JOB_MAX_ATTEMPTS,
                options_json=options,
            )
        )
        if staged:
            self.job_repo.add_items([
                IndexJobItem(
                    job_id=job.id,
                    document_id=document_id,
                    source_path=source_path,
                    action=action,
                )
                for document_id, source_path, action in staged
            ])
            self._signal_job(job)
        else:
            self.job_repo.update(job, status="completed")
        return job, [item[0] for item in staged], skipped, rejected

    def retry_job(self, job_id: str) -> IndexJob:
        job = self.job_repo.retry(job_id)
        if job is None:
            raise DocumentValidationError(f"Index job not found: {job_id}")
        return self._signal_job(job)

    def request_batch(
        self,
        document_ids: list[str],
        action: str,
        *,
        requested_by: str | None = None,
        tag_ids: list[str] | None = None,
        folder_id: str | None = None,
    ):
        unique_ids = list(dict.fromkeys(document_ids))
        if not unique_ids:
            raise DocumentValidationError("At least one document is required")
        documents = [self.knowledge_repo.get_by_id(item) for item in unique_ids]
        if any(document is None for document in documents):
            raise DocumentValidationError("One or more documents were not found")

        if action == "tag":
            return {
                "action": action,
                "updated": self.management_repo.set_document_tags(
                    unique_ids,
                    tag_ids or [],
                ),
            }
        if action == "move":
            return {
                "action": action,
                "updated": self.management_repo.move_documents(
                    unique_ids,
                    folder_id,
                ),
            }
        if action not in {"delete", "reindex"}:
            raise DocumentValidationError(f"Unsupported batch action: {action}")

        job = IndexJob(
            job_type="delete" if action == "delete" else "file",
            status="pending",
            requested_by=requested_by,
            total_items=len(unique_ids),
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        )
        created = self.job_repo.create(job)
        items = []
        for document in documents:
            if action == "delete":
                document.status = "pending_delete"
            items.append(
                IndexJobItem(
                    job_id=created.id,
                    document_id=document.id,
                    source_path=document.source_path,
                    action="delete" if action == "delete" else "update",
                )
            )
        self.db.commit()
        self.job_repo.add_items(items)
        self._signal_job(created)
        return created
