"""会话 CRUD 接口（严格的用户隔离）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import not_found
from app.core.database import get_db
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _ensure_access(db: Session, conv, current_user: User):
    if not conv or not KnowledgeBaseRepository(db).can_read(
        conv.knowledge_base_id, current_user.id, is_admin=current_user.role == "admin"
    ):
        raise not_found("会话不存在")
    return conv


def _repo(db: Session) -> ConversationRepository:
    return ConversationRepository(db)


@router.post("", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb_repo = KnowledgeBaseRepository(db)
    if not kb_repo.can_read(
        payload.knowledge_base_id,
        current_user.id,
        is_admin=current_user.role == "admin",
    ):
        raise not_found("知识库不存在")
    return _repo(db).create(
        current_user.id,
        payload.title,
        payload.knowledge_base_id,
    )


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    knowledge_base_ids = {
        item.id for item in KnowledgeBaseRepository(db).list_for_user(
            current_user.id, is_admin=current_user.role == "admin"
        )
    }
    return _repo(db).list_by_user(current_user.id, knowledge_base_ids)


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = _repo(db).get_detail_for_user(conversation_id, current_user.id)
    _ensure_access(db, conv, current_user)
    return conv


@router.patch("/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = _repo(db).get_for_user(conversation_id, current_user.id)
    _ensure_access(db, conv, current_user)
    return _repo(db).rename(conv, payload.title)


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = _repo(db).get_for_user(conversation_id, current_user.id)
    _ensure_access(db, conv, current_user)
    _repo(db).delete(conv)
    return None
