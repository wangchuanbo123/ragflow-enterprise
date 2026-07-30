"""应用初始化工具。

启动时执行已提交的 Alembic 迁移，并在用户表为空时按环境变量创建初始管理员。
初始化必须幂等。
"""

from sqlalchemy.orm import Session

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User


def run_migrations() -> None:
    """执行 Alembic 迁移到最新版本。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


def ensure_initial_admin(db: Session) -> bool:
    """当用户表为空时创建初始管理员账号，返回是否创建。"""
    if db.query(User).count() > 0:
        return False

    admin = User(
        username=INITIAL_ADMIN_USERNAME,
        password_hash=hash_password(INITIAL_ADMIN_PASSWORD),
        display_name=INITIAL_ADMIN_USERNAME,
        role="admin",
        is_active=True,
    )
    db.add(admin)
    db.commit()
    return True


def init_app() -> None:
    """应用启动时调用：迁移数据库并按需创建初始管理员。"""
    run_migrations()
    with SessionLocal() as db:
        ensure_initial_admin(db)

    # Start index worker
    from app.services.index_worker import start_worker
    start_worker()
