"""Knowledge backup, version and rollback APIs."""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import bad_request, forbidden, not_found
from app.core.config import BACKUP_MONTHLY_ENABLED, BACKUP_MONTHLY_INTERVAL_DAYS, BACKUP_RETENTION_VERSIONS
from app.core.database import get_db
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.backup import BackupCreate, BackupOut, BackupSettingsOut
from app.services.backup_service import BackupService

router = APIRouter(prefix="/knowledge-bases", tags=["backups"])


def _require(db: Session, knowledge_base_id: str, user: User, *, edit: bool = False) -> None:
    repository = KnowledgeBaseRepository(db)
    allowed = (
        repository.can_edit(knowledge_base_id, user.id, is_admin=user.role == "admin")
        if edit else repository.can_read(knowledge_base_id, user.id, is_admin=user.role == "admin")
    )
    if not allowed:
        if repository.get(knowledge_base_id) is None:
            raise not_found("知识库不存在")
        raise forbidden("没有该知识库的访问权限")


@router.get("/{knowledge_base_id}/backups", response_model=list[BackupOut])
def list_backups(
    knowledge_base_id: str,
    backup_type: str | None = Query(default=None),
    document_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return BackupService(db, knowledge_base_id).list(backup_type, document_id)


@router.get("/{knowledge_base_id}/backup-settings", response_model=BackupSettingsOut)
def backup_settings(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return BackupSettingsOut(
        monthly_enabled=BACKUP_MONTHLY_ENABLED,
        interval_days=BACKUP_MONTHLY_INTERVAL_DAYS,
        retention_versions=BACKUP_RETENTION_VERSIONS,
    )


@router.post("/{knowledge_base_id}/backups", response_model=BackupOut, status_code=status.HTTP_201_CREATED)
def create_backup(
    knowledge_base_id: str,
    payload: BackupCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return BackupService(db, knowledge_base_id).create(
            payload.backup_type,
            document_id=payload.document_id,
            label=payload.label,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post("/{knowledge_base_id}/backups/{backup_id}/restore", response_model=BackupOut)
def restore_backup(
    knowledge_base_id: str,
    backup_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return BackupService(db, knowledge_base_id).restore(backup_id, current_user.id)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.delete("/{knowledge_base_id}/backups/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backup(
    knowledge_base_id: str,
    backup_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        BackupService(db, knowledge_base_id).delete(backup_id)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
