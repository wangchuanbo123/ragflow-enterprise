"""Copy an explicit SQLite snapshot into an empty PostgreSQL application schema.

The original SQLite file is opened read-only and never upgraded. Alembic runs
only on a temporary backup and on the requested PostgreSQL target. Row counts
and order-independent SHA-256 fingerprints are checked before committing.
Historical Chroma vectors and object files require their separate migration.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from sqlalchemy import DateTime, Integer, MetaData, func, inspect, select, text


class MigrationError(RuntimeError):
    """A safe error containing no SQL parameters, URLs, or source content."""


DEPLOYMENT_LOCAL_TABLES = {"server_deployment_state"}


def _safe_failure(exc: Exception) -> MigrationError:
    code = getattr(getattr(exc, "orig", exc), "sqlstate", None)
    suffix = f"; SQLSTATE {code}" if isinstance(code, str) and code.isalnum() else ""
    return MigrationError(f"Database migration failed ({type(exc).__name__}{suffix}); source is unchanged")


def snapshot_sqlite(source: Path, snapshot: Path) -> str:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file() or source == snapshot.resolve():
        raise MigrationError("Choose a SQLite source file distinct from the snapshot")
    until = time.monotonic() + 300

    def progress(status, remaining, total):
        if time.monotonic() > until:
            raise MigrationError("SQLite snapshot exceeded its time budget")

    # mode=ro includes committed WAL content, unlike immutable=1. SQLite's
    # backup API yields one consistent snapshot even when readers are active.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10) as original:
        with sqlite3.connect(snapshot) as copied:
            original.backup(copied, pages=1000, progress=progress, sleep=0.05)
    digest = hashlib.sha256()
    with snapshot.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normal(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return {"datetime": value.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, dict):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


def table_fingerprint(connection, table, batch_size=500) -> dict:
    """Streaming multiset digest, independent of database collation and order."""
    count, total, xor = 0, 0, 0
    mask = (1 << 256) - 1
    result = connection.execution_options(stream_results=True).execute(select(table))
    try:
        for row in result.mappings():
            payload = json.dumps(_normal(dict(row)), sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            number = int.from_bytes(hashlib.sha256(payload).digest(), "big")
            total = (total + number) & mask
            xor ^= number
            count += 1
    finally:
        result.close()
    digest = hashlib.sha256(f"{count}:{total:064x}:{xor:064x}".encode()).hexdigest()
    return {"rows": count, "sha256_multiset": digest}


def _row_values(table, row):
    values = dict(row)
    for column in table.columns:
        value = values.get(column.name)
        if isinstance(column.type, DateTime) and isinstance(value, datetime) and value.tzinfo is not None and not column.type.timezone:
            values[column.name] = value.astimezone(timezone.utc).replace(tzinfo=None)
    return values


def _seed_row(table, row) -> bool:
    defaults = {
        "knowledge_bases": {"id": "00000000-0000-0000-0000-000000000001", "name": "默认知识库",
                            "slug": "default", "is_default": True, "is_active": True},
        "knowledge_data_sources": {"id": "00000000-0000-0000-0000-000000000002",
                                   "knowledge_base_id": "00000000-0000-0000-0000-000000000001",
                                   "source_type": "upload", "name": "本地上传", "status": "active",
                                   "deletion_policy": "ignore"},
    }
    expected = defaults.get(table.name)
    if not expected:
        return False
    return all(value == expected.get(key) for key, value in row.items() if key not in {"created_at", "updated_at"})


def _require_empty_target(connection, business_metadata):
    metadata = MetaData()
    metadata.reflect(bind=connection)
    for table in metadata.sorted_tables:
        if table.name == "alembic_version" or table.name in DEPLOYMENT_LOCAL_TABLES:
            continue
        rows = connection.execute(select(table).limit(2)).mappings().all()
        if rows and not (table.name in business_metadata.tables and len(rows) == 1 and _seed_row(table, rows[0])):
            raise MigrationError("Target contains data; refusing to replace an existing application database")


def _copy_table(source, target, table, batch_size):
    self_columns = {
        foreign_key.parent.name for foreign_key in table.foreign_keys if foreign_key.column.table.name == table.name
    }
    if any(not table.c[key].nullable for key in self_columns):
        raise MigrationError("A required self-reference needs an explicit migration")
    pending_updates = []
    result = source.execute(select(table))
    try:
        while batch := result.mappings().fetchmany(batch_size):
            values = [_row_values(table, row) for row in batch]
            if self_columns:
                for row in values:
                    original = {key: row[key] for key in self_columns}
                    if any(value is not None for value in original.values()):
                        # Core UPDATE would otherwise apply ORM onupdate=now()
                        # defaults and change migrated timestamps.
                        original.update({column.name: row[column.name] for column in table.columns if column.onupdate is not None})
                        pending_updates.append(({column.name: row[column.name] for column in table.primary_key}, original))
                        row.update({key: None for key in self_columns})
            target.execute(table.insert(), values)
    finally:
        result.close()
    # Parent UUIDs need not sort before child UUIDs. Restore nullable
    # self-references after all rows in this table exist, with FKs still enabled.
    for primary_key, original in pending_updates:
        target.execute(table.update().where(*[table.c[key] == value for key, value in primary_key.items()]).values(**original))


def _reset_sequences(connection, metadata):
    preparer = connection.dialect.identifier_preparer
    for table in metadata.sorted_tables:
        for column in table.primary_key:
            if not isinstance(column.type, Integer):
                continue
            qualified = preparer.format_table(table)
            sequence = connection.scalar(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                         {"table": qualified, "column": column.name})
            if sequence:
                maximum = connection.scalar(select(func.max(column)))
                connection.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                                   {"sequence": sequence, "value": maximum if maximum is not None else 1,
                                    "called": maximum is not None})


def _set_graph_revision_triggers(connection, metadata, *, enabled):
    """Keep an exact revision snapshot while copying its contributing tables.

    This runs inside the cutover's locked transaction. PostgreSQL rolls back
    these ALTERs along with data if any copy/check fails. Only our application
    revision triggers are suspended; foreign keys and other constraints remain
    active. A successful copy enables every trigger before verification/commit.
    """
    from app.services.graph_revision import GRAPH_TABLES
    preparer = connection.dialect.identifier_preparer
    action = "ENABLE" if enabled else "DISABLE"
    for name in GRAPH_TABLES:
        qualified = preparer.format_table(metadata.tables[name])
        for trigger in ("graph_revision_insert", "graph_revision_update", "graph_revision_delete", "graph_revision_truncated"):
            connection.execute(text(f"ALTER TABLE {qualified} {action} TRIGGER {preparer.quote(trigger)}"))


def migrate_sqlite_to_postgres(source_path, target_url, *, batch_size=500, verify_only=False) -> dict:
    if not 1 <= batch_size <= 5000:
        raise MigrationError("Batch size must be between 1 and 5000")
    from app.core.database import Base, create_database_engine, normalize_database_url
    from app.core.init_db import database_startup_lock, run_migrations
    import app.models  # noqa: F401
    business_tables = [table for table in Base.metadata.sorted_tables if table.name not in DEPLOYMENT_LOCAL_TABLES]

    try:
        target_url = normalize_database_url(target_url)
        if target_url.get_backend_name() != "postgresql":
            raise MigrationError("Migration target must be PostgreSQL")
        with tempfile.TemporaryDirectory(prefix="rag-postgres-migration-") as temporary:
            snapshot = Path(temporary) / "snapshot.sqlite"
            source_snapshot_hash = snapshot_sqlite(Path(source_path), snapshot)
            copied_engine = create_database_engine("sqlite:///" + snapshot.as_posix())
            target_engine = create_database_engine(target_url)
            try:
                # Old SQLite releases are upgraded on the copy, never in place.
                run_migrations(target_engine=copied_engine)
                with copied_engine.connect() as source:
                    if source.exec_driver_sql("PRAGMA foreign_key_check").first():
                        raise MigrationError("Source snapshot violates foreign keys; repair it before migrating")
                    source_revision = source.scalar(text("SELECT version_num FROM alembic_version"))
                    source_tables = set(inspect(source).get_table_names()) - {"alembic_version", "sqlite_sequence"}
                    if source_tables != set(Base.metadata.tables):
                        raise MigrationError("Source has unsupported tables or an incomplete schema")
                    # Hold the source snapshot transaction for every hash/copy.
                    with database_startup_lock(target_engine) as target:
                        if not verify_only:
                            run_migrations(connection=target)
                        target_tables = set(inspect(target).get_table_names())
                        if not set(Base.metadata.tables) <= target_tables:
                            raise MigrationError("Target schema is incomplete; run Alembic migrations first")
                        target_revision = target.scalar(text("SELECT version_num FROM alembic_version"))
                        if source_revision != target_revision:
                            raise MigrationError("Source and target schema revisions differ")
                        if target.in_transaction():
                            target.commit()
                        with target.begin():
                            # Blocks concurrent target DML during the cutover.
                            names = [target.dialect.identifier_preparer.format_table(table) for table in business_tables]
                            target.execute(text("LOCK TABLE " + ", ".join(names) + " IN ACCESS EXCLUSIVE MODE"))
                            if not verify_only:
                                _require_empty_target(target, Base.metadata)
                                _set_graph_revision_triggers(target, Base.metadata, enabled=False)
                                for table in reversed(business_tables):
                                    target.execute(table.delete())
                                for table in business_tables:
                                    try:
                                        _copy_table(source, target, table, batch_size)
                                    except MigrationError:
                                        raise
                                    except Exception as exc:
                                        raise MigrationError(f"Copy failed for application table {table.name}; {_safe_failure(exc)}") from None
                                _set_graph_revision_triggers(target, Base.metadata, enabled=True)
                            checks = {}
                            for table in business_tables:
                                source_check = table_fingerprint(source, table, batch_size)
                                target_check = table_fingerprint(target, table, batch_size)
                                if source_check != target_check:
                                    raise MigrationError(f"Verification failed for application table {table.name}; transaction rolled back")
                                checks[table.name] = source_check
                            if not verify_only:
                                _reset_sequences(target, Base.metadata)
                        return {
                            "status": "verified" if verify_only else "migrated",
                            "schema_revision": source_revision,
                            "source_snapshot_sha256": source_snapshot_hash,
                            "tables": checks, "total_rows": sum(item["rows"] for item in checks.values()),
                            "preserved_target_tables": sorted(DEPLOYMENT_LOCAL_TABLES),
                            "object_files": "not copied; preserve the existing object store and source paths",
                            "vector_indexes": "not copied; migrate Chroma collections separately or rebuild pgvector indexes before serving retrieval",
                        }
            finally:
                copied_engine.dispose()
                target_engine.dispose()
    except MigrationError:
        raise
    except Exception as exc:
        # SQLAlchemy and PostgreSQL error details can echo whole rows and hashes.
        raise _safe_failure(exc) from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sqlite", required=True, type=Path, help="Explicit SQLite source path (read-only)")
    parser.add_argument("--target-env", default="DATABASE_URL", help="Environment variable holding a PostgreSQL URL")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--verify-only", action="store_true", help="Compare an existing target without copying data")
    parser.add_argument("--report", type=Path, help="Optional JSON verification report; no source rows or credentials")
    args = parser.parse_args(argv)
    value = os.getenv(args.target_env)
    if not value:
        print("Set the target URL environment variable before running the migration", file=sys.stderr)
        return 2
    # This command is not an application server. Disable local credential-file
    # generation while importing model metadata/Alembic.
    os.environ["APP_PROFILE"] = "test"
    try:
        report = migrate_sqlite_to_postgres(args.source_sqlite, value, batch_size=args.batch_size, verify_only=args.verify_only)
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            destination = args.report.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix=destination.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(serialized + "\n")
            try:
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        print(serialized)
        return 0
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Migration report could not be written ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

