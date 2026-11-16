"""
FastAPI接口
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.legacy_scope import require_default_knowledge_base
from app.core.database import get_db
from app.models.user import User
from app.schemas.rag import AskRequest, AskResponse
from app.services.rag_service import ask_question


router = APIRouter()


@router.post("/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    require_default_knowledge_base(db, current_user)
    result = ask_question(request.query, retrieval_options=request.retrieval)

    return result
