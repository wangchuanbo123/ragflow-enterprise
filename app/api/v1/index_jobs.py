"""索引任务 API。"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import forbidden
from app.core.database import get_db
from app.models.user import User
from app.schemas.document import IndexJobOut
from app.services.document_index_service import DocumentIndexService

router = APIRouter(prefix="/index-jobs", tags=["index-jobs"])


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise forbidden("仅管理员可执行此操作")


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def request_sync(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    svc = DocumentIndexService(db)
    job = svc.request_sync_job(current_user.id)
    return {"job_id": job.id, "status": "accepted"}


@router.get("", response_model=list[IndexJobOut])
def list_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.repositories.index_job_repository import IndexJobRepository
    repo = IndexJobRepository(db)
    return repo.list_jobs()


@router.get("/{job_id}", response_model=IndexJobOut)
def get_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.repositories.index_job_repository import IndexJobRepository
    repo = IndexJobRepository(db)
    job = repo.get(job_id)
    if not job:
        from app.api.errors import not_found
        raise not_found("任务不存在")
    return job
