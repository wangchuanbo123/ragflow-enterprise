"""SQLAlchemy database engines shared by PostgreSQL and SQLite profiles."""

from collections.abc import Generator
import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import DATABASE_URL, DATA_DIR

def normalize_database_url(value: str | URL) -> URL:
    """Use psycopg 3 explicitly, including common hosted postgres:// URLs."""
    url = make_url(value)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("Database provider must be PostgreSQL or SQLite")
    return url


def create_database_engine(value: str | URL, **overrides):
    """Create a pooled engine without connecting or logging credentials."""
    url = normalize_database_url(value)
    options = {"future": True, "pool_pre_ping": True, "hide_parameters": True}
    connect_args = {}
    if url.get_backend_name() == "sqlite":
        connect_args.update(check_same_thread=False, timeout=30)
        if not url.database or url.database == ":memory:":
            options["poolclass"] = StaticPool
    else:
        if "poolclass" not in overrides:
            from app.services.observability import ObservedQueuePool
            options["poolclass"] = ObservedQueuePool
            options.update(pool_size=int(os.getenv("DATABASE_POOL_SIZE", "10")),
                           max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "20")),
                           pool_timeout=float(os.getenv("DATABASE_POOL_TIMEOUT", "30")),
                           pool_recycle=1800)
        connect_args.update(connect_timeout=10, application_name="rag-enterprise")
    connect_args.update(overrides.pop("connect_args", {}))
    options.update(overrides)
    result = create_engine(url, connect_args=connect_args, **options)
    from app.services.observability import install_database_metrics
    install_database_metrics(result)
    if url.get_backend_name() == "sqlite":
        @event.listens_for(result, "connect")
        def sqlite_busy_timeout(connection, record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return result


DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_database_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供一个数据库会话并在请求结束后关闭。"""
    from app.bootstrap.container import get_provider_container

    with get_provider_container().metadata.session() as db:
        yield db
