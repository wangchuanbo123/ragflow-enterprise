"""Alembic 迁移环境。

直接复用项目的数据库引擎与模型元数据，避免在 alembic.ini 中硬编码连接串。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.core.config import DATABASE_URL
from app.core.database import Base, create_database_engine, normalize_database_url
from app.models import *  # noqa: F401,F403  确保所有模型注册到 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = normalize_database_url(config.attributes.get("database_url", DATABASE_URL))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.get_backend_name() == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate_connection(connection) -> None:
    metadata = target_metadata
    if connection.dialect.name == "postgresql":
        from rag.providers.pgvector_store import PGVECTOR_METADATA
        metadata = [target_metadata, PGVECTOR_METADATA]
    context.configure(connection=connection, target_metadata=metadata,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        migrate_connection(supplied)
        return
    options = {"poolclass": pool.NullPool}
    value = config.attributes.get("database_url", DATABASE_URL)
    if normalize_database_url(value).get_backend_name() == "postgresql":
        # NullPool does not accept QueuePool's sizing options.
        from sqlalchemy import create_engine
        connectable = create_engine(normalize_database_url(value), poolclass=pool.NullPool, hide_parameters=True)
    else:
        connectable = create_database_engine(value, **options)
    try:
        from app.core.init_db import database_startup_lock
        with database_startup_lock(connectable) as connection:
            migrate_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
