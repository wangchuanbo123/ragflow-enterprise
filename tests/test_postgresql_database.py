"""Portable schema, startup serialization, and non-destructive SQLite cutover."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base, create_database_engine, normalize_database_url
from app.core.init_db import database_startup_lock, ensure_initial_admin, run_migrations
from scripts import migrate_to_postgres as migrate


def test_postgresql_url_explicitly_uses_psycopg3_without_showing_credentials():
    for value in ("postgres://person:secret@localhost/test", "postgresql://person:secret@localhost/test"):
        normalized = normalize_database_url(value)
        assert normalized.drivername == "postgresql+psycopg"
        assert "secret" not in str(normalized)
    assert normalize_database_url("sqlite://").drivername == "sqlite"
    with pytest.raises(ValueError):
        normalize_database_url("mysql://localhost/test")


def test_sqlite_snapshot_is_read_only_and_includes_committed_wal_data(tmp_path):
    source = tmp_path / "source.sqlite"
    original = sqlite3.connect(source)
    try:
        original.execute("PRAGMA journal_mode=WAL")
        original.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        original.execute("INSERT INTO records VALUES (1, 'committed')")
        original.commit()
        before = source.read_bytes()
        snapshot = tmp_path / "snapshot.sqlite"
        assert len(migrate.snapshot_sqlite(source, snapshot)) == 64
        assert source.read_bytes() == before
        with sqlite3.connect(snapshot) as copied:
            assert copied.execute("SELECT value FROM records").fetchall() == [("committed",)]
    finally:
        original.close()


def test_migration_command_never_prints_database_exception_rows_or_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ISOLATED_TARGET_URL", "postgresql://person:never-print-this@localhost/test")

    def fail(*args, **kwargs):
        raise migrate.MigrationError("Database migration failed (IntegrityError; SQLSTATE 23503); source is unchanged")

    monkeypatch.setattr(migrate, "migrate_sqlite_to_postgres", fail)
    assert migrate.main(["--source-sqlite", str(tmp_path / "explicit.sqlite"), "--target-env", "ISOLATED_TARGET_URL"]) == 1
    output = capsys.readouterr()
    assert "23503" in output.err and "never-print-this" not in output.err + output.out


@pytest.fixture
def postgres_url():
    value = os.getenv("TEST_POSTGRES_URL")
    if not value:
        pytest.skip("Set TEST_POSTGRES_URL to an isolated PostgreSQL database with pgvector")
    base_url = normalize_database_url(value)
    root = create_database_engine(base_url)
    schema = "test_database_" + uuid.uuid4().hex
    with root.begin() as connection:
        connection.execute(text("CREATE SCHEMA " + schema))
    url = base_url.set(query={**base_url.query, "options": "-csearch_path=" + schema + ",public"})
    try:
        yield url
    finally:
        with root.begin() as connection:
            connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        root.dispose()


@pytest.fixture
def sqlite_source(tmp_path):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.user import User
    from app.models.knowledge_base import KnowledgeBase
    from app.models.document_management import KnowledgeFolder
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_graph import KgEntity

    source = tmp_path / "application.sqlite"
    engine = create_database_engine("sqlite:///" + source.as_posix())
    run_migrations(target_engine=engine)
    with Session(engine) as db:
        user = User(id="user-source", username="source-user", password_hash="synthetic-password-hash", display_name="迁移用户", role="admin")
        db.add(user)
        db.commit()
        db.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID).owner_id = user.id
        db.add_all([
            KnowledgeFolder(id="folder-child", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, name="子目录", path="/父/子", parent_id="folder-parent"),
            KnowledgeFolder(id="folder-parent", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, name="父目录", path="/父"),
        ])
        db.add(KnowledgeDocument(id="document-source", source_path="docs/测试.md", original_filename="测试.md", file_hash="a" * 64,
                                 file_size=123, status="ready", chunk_count=1, folder_id="folder-child",
                                 metadata_json={"language": "中文", "nested": {"boolean": True, "empty": None}}))
        db.commit()
        db.add(KnowledgeChunk(id="chunk-source", document_id="document-source", file_hash="a" * 64, chunk_index=0,
                              content="系统甲依赖系统乙。", content_hash="b" * 64, graph_status="completed"))
        db.add(KgEntity(id="entity-source", entity_type="SYSTEM", canonical_name="系统甲", normalized_name="系统甲"))
        # Exercise PostgreSQL's integer sequence reset as well as UUID/string keys.
        state = Base.metadata.tables["knowledge_index_state"]
        db.execute(state.insert().values(id=41, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, active_collection_name="source-version",
                                        index_schema_version=2, embedding_fingerprint="synthetic-profile", corpus_fingerprint="c" * 64))
        db.commit()
    engine.dispose()
    return source


def test_postgresql_fresh_migrations_match_models_and_partial_schema_uniqueness(postgres_url):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.graph_governance import GraphSchemaDefinition

    engine = create_database_engine(postgres_url)
    try:
        run_migrations(target_engine=engine)
        run_migrations(target_engine=engine)
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.check(config)
        with Session(engine) as db:
            for version, status in ((1, "draft"), (2, "draft"), (3, "active")):
                db.add(GraphSchemaDefinition(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, version=version, status=status,
                                             entity_types_json=["SYSTEM"], relation_types_json=["DEPENDS_ON"]))
            db.commit()
            db.add(GraphSchemaDefinition(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, version=4, status="active",
                                         entity_types_json=["SYSTEM"], relation_types_json=["DEPENDS_ON"]))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
    finally:
        engine.dispose()


def test_sqlite_cutover_verifies_every_table_self_references_and_sequences(sqlite_source, postgres_url):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.user import User

    # Deployment identity belongs to the target installation, even when an old
    # SQLite source happens to contain its own deployment-local state.
    with sqlite3.connect(sqlite_source) as connection:
        connection.execute("INSERT INTO server_deployment_state (key,value) VALUES ('deployment_id','legacy-deployment')")
    target = create_database_engine(postgres_url)
    try:
        run_migrations(target_engine=target)
        with target.begin() as connection:
            connection.execute(text("INSERT INTO server_deployment_state (key,value) VALUES ('deployment_id','target-deployment')"))
    finally:
        target.dispose()
    before = hashlib.sha256(sqlite_source.read_bytes()).hexdigest()
    report = migrate.migrate_sqlite_to_postgres(sqlite_source, postgres_url, batch_size=1)
    assert report["status"] == "migrated" and report["tables"]["users"]["rows"] == 1
    assert report["tables"]["knowledge_chunks"]["rows"] == 1
    assert report["tables"]["knowledge_graph_revisions"]["rows"] == 1
    assert set(report["tables"]) == set(Base.metadata.tables) - migrate.DEPLOYMENT_LOCAL_TABLES
    assert hashlib.sha256(sqlite_source.read_bytes()).hexdigest() == before
    checked = migrate.migrate_sqlite_to_postgres(sqlite_source, postgres_url, verify_only=True)
    assert checked["tables"] == report["tables"]
    with pytest.raises(migrate.MigrationError, match="Target contains data"):
        migrate.migrate_sqlite_to_postgres(sqlite_source, postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        with engine.connect() as connection:
            sequence = connection.scalar(text("SELECT pg_get_serial_sequence('knowledge_index_state', 'id')"))
            assert connection.scalar(text("SELECT nextval(CAST(:sequence AS regclass))"), {"sequence": sequence}) == 42
            assert connection.scalar(select(User.username)) == "source-user"
            assert connection.scalar(text("SELECT parent_id FROM knowledge_folders WHERE id='folder-child'")) == "folder-parent"
            assert connection.scalar(text("SELECT value FROM server_deployment_state WHERE key='deployment_id'")) == "target-deployment"
            # The exact source incarnation/revision survives cutover, then the
            # re-enabled statement trigger increments once for a normal write.
            before_revision = connection.execute(text("SELECT incarnation, revision FROM knowledge_graph_revisions")).one()
            with sqlite3.connect(sqlite_source) as source:
                assert tuple(before_revision) == source.execute("SELECT incarnation, revision FROM knowledge_graph_revisions").fetchone()
            connection.execute(text("UPDATE kg_entities SET description='post-cutover' WHERE id='entity-source'"))
            assert connection.scalar(text("SELECT revision FROM knowledge_graph_revisions")) == before_revision.revision + 1
            connection.rollback()
    finally:
        engine.dispose()


def test_copy_failure_rolls_back_target_and_preserves_original(sqlite_source, postgres_url, monkeypatch):
    before = hashlib.sha256(sqlite_source.read_bytes()).hexdigest()
    original = migrate._copy_table

    def fail(source, target, table, batch_size):
        original(source, target, table, batch_size)
        if table.name == "users":
            raise RuntimeError("never print source content or credentials")

    monkeypatch.setattr(migrate, "_copy_table", fail)
    with pytest.raises(migrate.MigrationError) as caught:
        migrate.migrate_sqlite_to_postgres(sqlite_source, postgres_url)
    assert "never print" not in str(caught.value)
    assert hashlib.sha256(sqlite_source.read_bytes()).hexdigest() == before
    engine = create_database_engine(postgres_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM users")) == 0
            assert connection.scalar(text("SELECT count(*) FROM knowledge_bases")) == 1
            assert connection.scalar(text("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND NOT t.tgisinternal AND t.tgname LIKE 'graph_revision_%' AND t.tgenabled <> 'O'")) == 0
    finally:
        engine.dispose()


def test_concurrent_startups_serialize_migrations_and_admin_seed(postgres_url):
    from app.models.user import User

    engine = create_database_engine(postgres_url)
    barrier = threading.Barrier(2)

    def boot(index):
        barrier.wait(timeout=5)
        with database_startup_lock(engine) as connection:
            run_migrations(connection=connection)
            with Session(bind=connection) as db:
                created = ensure_initial_admin(db)
                time.sleep(0.03)
                return created

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(boot, [0, 1])) == [False, True]
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(User)) == 1
    finally:
        engine.dispose()


def test_two_real_processes_boot_one_database_without_duplicate_seed(postgres_url):
    env = dict(os.environ, DATABASE_URL=postgres_url.render_as_string(hide_password=False), APP_PROFILE="test", APP_ROLE="api")
    code = "from app.core.init_db import init_app; init_app(start_services=False); print('booted')"
    processes = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(2)]
    try:
        for process in processes:
            output, error = process.communicate(timeout=60)
            assert process.returncode == 0, error
            assert "booted" in output
        engine = create_database_engine(postgres_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM users")) == 1
                assert connection.scalar(text("SELECT count(*) FROM knowledge_base_members")) == 1
        finally:
            engine.dispose()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_old_sqlite_revision_is_upgraded_only_on_the_snapshot(tmp_path, postgres_url):
    source = tmp_path / "historical.sqlite"
    engine = create_database_engine("sqlite:///" + source.as_posix())
    try:
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "7c307ebb1e8b")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id, username, password_hash, display_name, role, is_active) "
                                    "VALUES ('old-user', 'historical-user', 'synthetic-hash', '历史用户', 'admin', 1)"))
    finally:
        engine.dispose()
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    report = migrate.migrate_sqlite_to_postgres(source, postgres_url)
    assert report["tables"]["users"]["rows"] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "7c307ebb1e8b"


def test_postgresql_last_administrator_cannot_be_removed_concurrently(postgres_url):
    from app.api.errors import APIError
    from app.models.user import User
    from app.schemas.auth import UserUpdate
    from app.services.user_service import update_user

    engine = create_database_engine(postgres_url)
    run_migrations(target_engine=engine)
    with Session(engine) as db:
        db.add_all([User(id=f"administrator-{index}", username=f"administrator-{index}", password_hash="synthetic-hash",
                         role="admin", is_active=True) for index in range(2)])
        db.commit()
    barrier = threading.Barrier(2)

    def disable(index):
        with Session(engine) as db:
            db.get(User, f"administrator-{index}")
            barrier.wait(timeout=5)
            try:
                update_user(db, f"administrator-{index}", UserUpdate(is_active=False))
                return "changed"
            except APIError as exc:
                assert exc.message == "至少保留一个启用的管理员"
                return "protected"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(disable, [0, 1])) == ["changed", "protected"]
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) == 1
    finally:
        engine.dispose()


def test_postgresql_graph_queries_and_membership_keep_knowledge_base_scope(sqlite_source, postgres_url):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseMember
    from app.models.knowledge_graph import KgEntity
    from app.models.user import User
    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
    from rag.knowledge_graph.schemas import ChunkExtraction
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    migrate.migrate_sqlite_to_postgres(sqlite_source, postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        with Session(engine) as db:
            store = SqliteGraphStore(db, DEFAULT_KNOWLEDGE_BASE_ID)
            extraction = ChunkExtraction.model_validate({"chunk_id": "chunk-source", "entities": [
                {"local_id": "a", "name": "系统甲", "type": "SYSTEM", "aliases": ["甲系统"]},
                {"local_id": "b", "name": "系统乙", "type": "SYSTEM"},
            ], "relations": [{"subject": "a", "predicate": "DEPENDS_ON", "object": "b", "evidence": "系统甲依赖系统乙。", "confidence": 0.95}]})
            store.upsert_extraction({"document_id": "document-source"}, extraction)
            assert store.search_entities("甲系统")
            assert store.get_neighbors(["entity-source"])
            db.add(KnowledgeBase(id="other-kb", name="另一个库", slug="other-kb"))
            db.add(User(id="viewer-user", username="viewer-user", password_hash="synthetic-hash"))
            db.commit()
            db.add(KgEntity(id="other-entity", knowledge_base_id="other-kb", entity_type="SYSTEM", canonical_name="系统甲", normalized_name="系统甲"))
            db.add(KnowledgeBaseMember(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, user_id="viewer-user", role="viewer"))
            db.commit()
            assert {item["id"] for item in SqliteGraphStore(db, "other-kb").search_entities("系统甲")} == {"other-entity"}
            assert "other-entity" not in {item["id"] for item in store.search_entities("系统甲")}
            repository = KnowledgeBaseRepository(db)
            assert repository.can_read(DEFAULT_KNOWLEDGE_BASE_ID, "viewer-user")
            assert not repository.can_edit(DEFAULT_KNOWLEDGE_BASE_ID, "viewer-user")
            assert not repository.can_read("other-kb", "viewer-user")
            repository.archive(db.get(KnowledgeBase, "other-kb"))
            assert not repository.can_read("other-kb", "viewer-user", is_admin=True)
    finally:
        engine.dispose()
