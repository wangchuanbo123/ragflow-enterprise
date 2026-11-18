"""应用初始化工具。

启动时执行已提交的 Alembic 迁移，并在用户表为空时按环境变量创建初始管理员。
初始化必须幂等。
"""

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import BASE_DIR, INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User

_STARTUP_LOCK_NAMESPACE = 1380009777
_STARTUP_LOCK_KEY = 1
_local_lock = threading.RLock()


@contextmanager
def database_startup_lock(target_engine=None):
    """Serialize schema changes and seed data across processes, not just threads."""
    from app.core import database

    target_engine = target_engine or database.engine
    if target_engine.dialect.name == "postgresql":
        with target_engine.connect() as connection:
            try:
                connection.execute(text("SELECT pg_advisory_lock(:namespace, :key)"),
                                   {"namespace": _STARTUP_LOCK_NAMESPACE, "key": _STARTUP_LOCK_KEY})
                connection.commit()
                yield connection
            finally:
                if connection.in_transaction():
                    connection.rollback()
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:namespace, :key)"),
                                       {"namespace": _STARTUP_LOCK_NAMESPACE, "key": _STARTUP_LOCK_KEY})
                    connection.commit()
                except Exception:
                    # Do not return a possibly still-locked session to the pool.
                    connection.invalidate()
        return
    with _local_lock:
        name = target_engine.url.database
        handle = None
        try:
            if name and name != ":memory:":
                lock_path = Path(name).resolve().with_suffix(Path(name).suffix + ".startup.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+b")
                if lock_path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                until = time.monotonic() + 180
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= until:
                            raise RuntimeError("Database startup lock timed out") from None
                        time.sleep(0.05)
            with target_engine.connect() as connection:
                yield connection
        finally:
            if handle is not None:
                handle.close()  # Closing releases the OS lock on both platforms.


def run_migrations(*, connection=None, target_engine=None) -> None:
    """执行 Alembic 迁移到最新版本。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    if connection is not None:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        return
    with database_startup_lock(target_engine) as locked_connection:
        cfg.attributes["connection"] = locked_connection
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


def init_app(start_services: bool = True) -> None:
    """应用启动时调用：迁移数据库并按需创建初始管理员。"""
    from app.core import config
    role = getattr(config, "APP_ROLE", "all")
    with database_startup_lock() as connection:
        run_migrations(connection=connection)
        # The advisory/file lock remains held across commits in the seed
        # services, so concurrent API/worker boots cannot create two admins.
        with Session(bind=connection, autoflush=False) as db:
            ensure_initial_admin(db)
            if config.APP_PROFILE == "server":
                from app.services.deployment_identity import ensure_deployment_identity
                ensure_deployment_identity(db)
            if role == "all":
                recover_interrupted_messages(db)
            admin = db.query(User).filter(User.role == "admin").order_by(User.created_at).first()
            from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

            KnowledgeBaseRepository(db).ensure_default(admin.id if admin else None)
            from sqlalchemy import select
            from app.models.knowledge_base import KnowledgeBase
            from app.services.graph_governance_service import GraphGovernanceService

            for knowledge_base_id in db.scalars(select(KnowledgeBase.id)):
                GraphGovernanceService(db, knowledge_base_id, task_queue=object()).ensure_total_schema()

    if not start_services:
        return

    # Start index worker
    if role in {"all", "api", "worker"}:
        from app.agents.runtime import start_managed_codecode
        start_managed_codecode()
    if role in {"all", "worker"}:
        from app.services.index_worker import start_worker
        start_worker()
    if role == "all":
        from app.services.connector_scheduler import start_connector_scheduler
        start_connector_scheduler()
        from app.services.graph_maintenance import start_graph_maintenance_scheduler
        start_graph_maintenance_scheduler()
        from app.services.backup_scheduler import start_backup_scheduler
        start_backup_scheduler()


def recover_interrupted_messages(db: Session) -> int:
    """Recover the last persisted partial output after a process interruption."""
    from sqlalchemy import update
    from app.models.message import Message

    result = db.execute(update(Message).where(Message.status == "streaming").values(
        status="interrupted", error_message="服务已重启，回答生成中断"
    ))
    db.commit()
    return result.rowcount
