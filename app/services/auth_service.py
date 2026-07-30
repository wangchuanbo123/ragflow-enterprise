"""鉴权服务。"""

from sqlalchemy.orm import Session

from app.api.errors import bad_request, unauthorized
from app.core.security import (
    clear_auth_cookie,
    create_access_token,
    set_auth_cookie,
    verify_password,
)
from app.models.user import User
from app.repositories.user_repository import UserRepository


def login(db: Session, username: str, password: str) -> tuple[User, str]:
    repo = UserRepository(db)
    user = repo.get_by_username(username)
    if not user or not verify_password(password, user.password_hash):
        raise bad_request("用户名或密码错误")
    if not user.is_active:
        raise unauthorized("用户已禁用")
    token = create_access_token(user.id, {"username": user.username, "role": user.role})
    return user, token


def attach_token_cookie(response, token: str) -> None:
    set_auth_cookie(response, token)


def logout(response) -> None:
    clear_auth_cookie(response)
