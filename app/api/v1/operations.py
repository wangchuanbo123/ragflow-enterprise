"""Administrator-only operational metrics and actionable threshold alerts."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.deps import get_admin_user
from app.core.database import get_db
from app.models.user import User
from app.services.observability import operations_snapshot

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/metrics")
def metrics(current_user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    return operations_snapshot(db)


@router.post("/index-audit")
def index_audit(current_user: User = Depends(get_admin_user)):
    """管理员显式执行全量审计；不参与就绪检查或请求热路径。"""
    from app.services.readiness_service import audit_index_integrity
    return audit_index_integrity()
