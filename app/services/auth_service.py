"""鉴权服务。"""

from datetime import datetime
from functools import lru_cache

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.api.errors import APIError, bad_request, unauthorized
from app.core.security import (
    clear_auth_cookie,
    create_access_token,
    decode_access_token,
    hash_password,
    set_auth_cookie,
    verify_password,
)
from app.models.user import User
from app.models.auth_session import AuthSession
from app.repositories.user_repository import UserRepository


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password("unknown-account-timing-check")


def login(db: Session, username: str, password: str, address: str = "local") -> tuple[User, str]:
    from app.services.login_throttle import check_login_attempt, clear_account_failures

    check_login_attempt(username, address)
    repo = UserRepository(db)
    user = repo.get_by_username(username)
    observed_hash = user.password_hash if user else _dummy_hash()
    valid = verify_password(password, observed_hash)
    if not user or not valid:
        raise bad_request("用户名或密码错误")
    if user.registration_status == "pending":
        raise APIError("REGISTRATION_PENDING", "注册申请正在等待管理员审核，暂时无法登录", 403)
    if user.registration_status == "rejected":
        raise APIError("REGISTRATION_REJECTED", "注册申请未通过审核，请联系管理员", 403)
    if not user.is_active:
        raise unauthorized("用户已禁用")
    # Serialize session issuance with password resets and account changes. A
    # reset after verification must not be followed by a new old-password login.
    locked = db.execute(update(User).where(
        User.id == user.id, User.password_hash == observed_hash, User.is_active.is_(True),
        User.registration_status == "approved",
    ).values(password_hash=User.password_hash))
    if locked.rowcount != 1:
        db.rollback()
        raise unauthorized("账号状态已变化，请重新登录")
    db.refresh(user)
    token = create_access_token(user.id, {"username": user.username, "role": user.role})
    payload = decode_access_token(token)
    db.execute(delete(AuthSession).where(AuthSession.expires_at < datetime.utcnow()))
    db.add(AuthSession(
        id=payload["jti"], user_id=user.id,
        expires_at=datetime.utcfromtimestamp(payload["exp"]),
    ))
    db.commit()
    clear_account_failures(username, address)
    return user, token


def attach_token_cookie(response, token: str) -> None:
    set_auth_cookie(response, token)


def logout(response, db: Session | None = None, token: str | None = None) -> None:
    if db is not None and token:
        from fastapi import HTTPException

        try:
            payload = decode_access_token(token)
        except HTTPException:
            payload = {}
        if payload.get("jti"):
            db.execute(update(AuthSession).where(AuthSession.id == payload["jti"]).values(revoked_at=datetime.utcnow()))
            db.commit()
    clear_auth_cookie(response)


def revoke_user_sessions(db: Session, user_id: str) -> None:
    db.execute(update(AuthSession).where(
        AuthSession.user_id == user_id,
        AuthSession.revoked_at.is_(None),
    ).values(revoked_at=datetime.utcnow()))


def validate_new_password(password: str) -> None:
    if not 12 <= len(password.encode("utf-8")) <= 72:
        raise bad_request("密码须为 12–72 个 UTF-8 字节")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise bad_request("密码须同时包含字母和数字")


def change_password(db: Session, user: User, current_password: str, new_password: str) -> None:
    observed_hash = user.password_hash
    if not verify_password(current_password, observed_hash):
        raise bad_request("当前密码错误")
    validate_new_password(new_password)
    if verify_password(new_password, user.password_hash):
        raise bad_request("新密码不能与当前密码相同")
    changed = db.execute(update(User).where(
        User.id == user.id, User.password_hash == observed_hash, User.is_active.is_(True),
        User.registration_status == "approved",
    ).values(password_hash=hash_password(new_password)))
    if changed.rowcount != 1:
        db.rollback()
        raise unauthorized("账号状态已变化，请重新登录")
    revoke_user_sessions(db, user.id)
    db.commit()
