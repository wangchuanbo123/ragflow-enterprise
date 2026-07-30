"""健康检查接口：存活 + 就绪。"""

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_current_user
from app.services.readiness_service import check_readiness

router = APIRouter()


@router.get("/health")
def health():
    """轻量存活检查：只证明进程存活。"""
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response):
    """就绪检查：验证数据库、向量库、Ollama 等组件状态。"""
    result = check_readiness()
    if result["status"] != "ready":
        response.status_code = 503
    return result
