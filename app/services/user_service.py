"""Administrator account management, including last-administrator protection."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import bad_request, not_found
from app.core.security import hash_password
from app.models.user import User
from app.schemas.auth import UserCreate, UserUpdate
from app.services.auth_service import revoke_user_sessions, validate_new_password


def create_user(db: Session, payload: UserCreate) -> User:
    validate_new_password(payload.password)
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise bad_request("用户名已存在")
    user = User(
        username=payload.username, password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip() or payload.username,
        role=payload.role, is_active=payload.is_active,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise bad_request("用户名已存在")
    db.refresh(user)
    return user


def update_user(db: Session, user_id: str, payload: UserUpdate) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise not_found("用户不存在")
    values = payload.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise bad_request("用户字段不能为 null")
    loses_admin = user.role == "admin" and user.is_active and (
        values.get("role", user.role) != "admin" or not values.get("is_active", user.is_active)
    )
    if loses_admin:
        # Every administrator removal shares one transaction lock, including
        # removals targeting different rows on PostgreSQL.
        from sqlalchemy import text, update

        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(724091830221)"))
            db.refresh(user)
        else:
            db.execute(update(User).where(User.id == user_id).values(is_active=User.is_active))
        active_admins = db.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active.is_(True)
        ))
        if active_admins <= 1:
            db.rollback()
            raise bad_request("至少保留一个启用的管理员")
    password = values.pop("password", None)
    if password is not None:
        validate_new_password(password)
        user.password_hash = hash_password(password)
    security_changed = password is not None or any(
        key in values and values[key] != getattr(user, key)
        for key in ("role", "is_active")
    )
    for key, value in values.items():
        setattr(user, key, value.strip() if key == "display_name" else value)
    if security_changed:
        revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return user
