"""Offline, model-free migration of referenced Chroma collections to pgvector.

The original directory is copied and never opened by Chroma. Application state
must already be migrated to PostgreSQL; collection ownership is taken only from
its document/entity/relation/code index-state rows, never guessed from names.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile

from sqlalchemy import create_engine, select

from rag.providers.interfaces import VectorIndexRef
from rag.providers.pgvector_store import PgVectorStoreProvider, vector_records_hash


def collection_owners(engine):
    from app.models.index_job import KnowledgeIndexState
    from app.models.code_graph import EntityVectorIndexState, RelationVectorIndexState, CodeVectorIndexState
    owners = {}
    with engine.connect() as connection:
        for model, name in ((KnowledgeIndexState, "active_collection_name"), (EntityVectorIndexState, "index_name"),
                            (RelationVectorIndexState, "index_name"), (CodeVectorIndexState, "index_name")):
            field = getattr(model, name)
            for kb, collection in connection.execute(select(model.knowledge_base_id, field).where(field.is_not(None))):
                if not collection:
                    continue
                if collection in owners and owners[collection] != kb:
                    raise ValueError("Collection is referenced by multiple knowledge bases; refusing ambiguous ownership")
                owners[collection] = kb
    return owners


def _source_files(source):
    files = {}
    for directory, folders, names in os.walk(source, followlinks=False):
        for name in folders + names:
            path = Path(directory) / name
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError("Chroma source must not contain symlinks")
        for name in names:
            path = Path(directory) / name
            stat = path.stat()
            files[path.relative_to(source).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return files


@contextmanager
def source_snapshot(source_dir):
    source = Path(source_dir).expanduser().resolve()
    if not source.is_dir() or not (source / "chroma.sqlite3").is_file():
        raise ValueError("Chroma source directory must contain chroma.sqlite3")
    before = _source_files(source)
    with tempfile.TemporaryDirectory(prefix="rag-vector-migration-") as directory:
        root = Path(directory).resolve()
        snapshot = (root / "chroma").resolve()
        if not snapshot.is_relative_to(root) or snapshot == source:
            raise ValueError("Invalid migration snapshot destination")
        shutil.copytree(source, snapshot)
        if _source_files(source) != before:
            raise ValueError("Chroma source changed while copying; stop the legacy application before migration")
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(path=str(snapshot), settings=Settings(anonymized_telemetry=False))
        try:
            yield client
        finally:
            # Chroma 0.4 has no public close method. Stop only this temporary
            # system so its SQLite/file handles can be released on Windows.
            client._system.stop()
            for component in client._system.components():
                for segment in getattr(component, "_instances", {}).values():
                    close = getattr(segment, "close_persistent_index", None)
                    if callable(close):
                        close()


def source_records(collection, identifiers, batch_size):
    for start in range(0, len(identifiers), batch_size):
        wanted = identifiers[start:start + batch_size]
        result = collection.get(ids=wanted, include=["documents", "metadatas", "embeddings"])
        fields = [result.get(key) for key in ("ids", "documents", "metadatas", "embeddings")]
        if any(value is None or len(value) != len(wanted) for value in fields):
            raise ValueError("Source collection has missing IDs, documents, metadata or vectors")
        rows = {identifier: {"id": identifier, "document": document, "metadata": metadata or {}, "embedding": embedding}
            for identifier, document, metadata, embedding in zip(*fields)}
        if set(rows) != set(wanted):
            raise ValueError("Source collection IDs changed during export")
        for identifier in wanted:
            yield rows[identifier]


def migrate_collections(source_client, provider, *, active_only=False, batch_size=500):
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5000:
        raise ValueError("Batch size must be 1–5000")
    owners = collection_owners(provider.engine)
    names = sorted(item if isinstance(item, str) else item.name for item in source_client.list_collections())
    missing = set(owners) - set(names)
    if missing:
        raise ValueError("Referenced source vector collections are missing: " + ", ".join(sorted(missing)))
    unknown = set(names) - set(owners)
    if unknown and not active_only:
        raise ValueError("Historical collections have no authoritative index-state ownership; preserved in source: " + ", ".join(sorted(unknown)))
    report = {"status": "completed", "active_only": active_only, "collections": [], "models_called": False}
    for name in names:
        collection = source_client.get_collection(name, embedding_function=None)
        count = collection.count()
        if name not in owners:
            report["collections"].append({"name": name, "status": "preserved_in_source", "count": count,
                "reason": "No migrated index-state reference; original source is retained"})
            continue
        identifiers = sorted(collection.get(include=[])["ids"])
        if len(identifiers) != count or len(set(identifiers)) != count:
            raise ValueError("Source collection count and IDs differ")
        digest = vector_records_hash(source_records(collection, identifiers, batch_size))
        ref = VectorIndexRef(name=name, knowledge_base_id=owners[name])
        result = provider.import_records(ref, source_records(collection, identifiers, batch_size),
            expected_count=count, expected_hash=digest)
        report["collections"].append({"name": name, "knowledge_base_id": owners[name], **result})
    report["imported_collections"] = sum(item["status"] == "imported" for item in report["collections"])
    report["verified_collections"] = sum(item["status"] == "verified_existing" for item in report["collections"])
    report["preserved_collections"] = sum(item["status"] == "preserved_in_source" for item in report["collections"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Legacy Chroma directory (default VECTOR_DB_DIR)")
    parser.add_argument("--database-url", help="Target PostgreSQL URL (default DATABASE_URL)")
    parser.add_argument("--active-only", action="store_true", help="Migrate only state-referenced collections; report retained history")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--report", type=Path, help="Write a JSON report in addition to stdout")
    args = parser.parse_args(argv)
    from app.core.config import DATABASE_URL, VECTOR_DB_DIR
    url = args.database_url or DATABASE_URL
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        parser.error("Target DATABASE_URL must be PostgreSQL; migrate application state first")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with source_snapshot(args.source or VECTOR_DB_DIR) as source:
            report = migrate_collections(source, PgVectorStoreProvider(engine, batch_size=args.batch_size),
                active_only=args.active_only, batch_size=args.batch_size)
        report["source_preserved"] = str(Path(args.source or VECTOR_DB_DIR).resolve())
    except Exception as exc:
        # Driver exception text can include credentials/SQL; only domain
        # validation messages are included in operator-facing reports.
        report = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc) if isinstance(exc, ValueError)
            else "Migration failed; inspect local database and migration configuration", "source_preserved": True}
    finally:
        engine.dispose()
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(args.report)
    print(payload)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
