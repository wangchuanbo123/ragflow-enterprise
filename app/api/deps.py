"""FastAPI 公共依赖。"""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token, read_auth_cookie
from app.models.user import User


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """从 HttpOnly Cookie 中解析 JWT 并返回当前用户。

    失败统一返回 401。
    """
    token = read_auth_cookie(request)
    if not token:
        from app.api.errors import unauthorized

        raise unauthorized("未登录")

    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        from app.api.errors import unauthorized

        raise unauthorized("无效的令牌")

    user = db.get(User, user_id)
    if not user or not user.is_active:
        from app.api.errors import unauthorized

        raise unauthorized("用户不存在或已禁用")

    return user
