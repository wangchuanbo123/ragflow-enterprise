"""文档管理 API。"""

from fastapi import APIRouter, Depends, UploadFile, File, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.legacy_scope import require_default_knowledge_base
from app.api.errors import bad_request, forbidden, not_found
from app.core.config import MAX_DOCUMENT_SIZE_MB
from app.core.database import get_db
from app.models.user import User
from app.schemas.document import DocumentOut, DocumentReindexRequest
from app.services.document_index_service import DocumentIndexService
from app.services.upload_staging import stage_uploads, upload_db_operation

router = APIRouter(prefix="/documents", tags=["documents"])
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise forbidden("仅管理员可执行此操作")


@router.get("", response_model=list[DocumentOut])
def list_documents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_default_knowledge_base(db, current_user)
    svc = DocumentIndexService(db)
    return svc.list_documents()


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_default_knowledge_base(db, current_user)
    svc = DocumentIndexService(db)
    doc = svc.get_document(document_id)
    if not doc:
        raise not_found("文档不存在")
    return doc


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    user_id = current_user.id

    try:
        async with stage_uploads([file]) as (uploads, _):
            _, filename, path = uploads[0]
            def register(owned_db):
                job, document_id = DocumentIndexService(owned_db).create_upload_job(filename, path, user_id)
                return job.id, document_id
            job_id, document_id = await upload_db_operation(db, register)
    except Exception as exc:
        from rag.errors import DocumentTooLargeError, DocumentValidationError
        if isinstance(exc, (DocumentValidationError, DocumentTooLargeError)):
            raise bad_request(str(exc))
        raise

    return {"job_id": job_id, "document_id": document_id, "status": "accepted"}


@router.delete("/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    svc = DocumentIndexService(db)
    job = svc.request_delete(document_id, current_user.id)
    return {"job_id": job.id, "status": "accepted"}


@router.post("/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    payload: DocumentReindexRequest | None = None,
):
    _require_admin(current_user)
    svc = DocumentIndexService(db)
    job = svc.request_reindex(document_id, current_user.id,
        parse_options=payload.model_dump() if payload is not None else None)
    return {"job_id": job.id, "status": "accepted"}
