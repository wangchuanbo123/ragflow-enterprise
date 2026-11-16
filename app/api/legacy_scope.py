"""Authorization shared by legacy endpoints bound to the default knowledge base."""

from sqlalchemy.orm import Session

from app.api.errors import not_found
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository


def require_default_knowledge_base(db: Session, user: User) -> None:
    if not KnowledgeBaseRepository(db).can_read(
        DEFAULT_KNOWLEDGE_BASE_ID, user.id, is_admin=user.role == "admin",
    ):
        raise not_found("知识库不存在")
