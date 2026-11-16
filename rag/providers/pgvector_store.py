"""PostgreSQL vector storage with tenant-scoped, versioned collections.

Schema and extension creation belong to Alembic, never to request handlers.
All metadata operators are compiled as bound SQLAlchemy expressions. A logical
collection can contain one embedding dimension; versions can use different ones.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import hashlib
import math
from numbers import Real
import uuid
import struct
import time

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, MetaData,
    Numeric, String, Table, Text, UniqueConstraint, and_, case, cast, delete,
    false, func, select, update, text, literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert

from rag.providers.interfaces import ProviderHealth, ProviderIdentity, VectorIndexInfo, VectorIndexRef


PGVECTOR_METADATA = MetaData()
COLLECTIONS = Table(
    "pgvector_collections", PGVECTOR_METADATA,
    Column("id", UUID(as_uuid=False), primary_key=True),
    Column("knowledge_base_id", String(36), nullable=False),
    Column("name", String(255), nullable=False),
    Column("index_version", String(255)),
    Column("embedding_profile_id", String(255)),
    Column("dimension", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("knowledge_base_id", "name", name="uq_pgvector_collection_kb_name"),
    CheckConstraint("dimension IS NULL OR (dimension >= 1 AND dimension <= 16000)", name="ck_pgvector_collection_dimension"),
)
EMBEDDINGS = Table(
    "pgvector_embeddings", PGVECTOR_METADATA,
    Column("collection_id", UUID(as_uuid=False), ForeignKey("pgvector_collections.id", ondelete="CASCADE"), primary_key=True),
    Column("id", String(512), primary_key=True),
    Column("document", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("embedding", VECTOR(), nullable=False),
)
Index("ix_pgvector_embeddings_metadata", EMBEDDINGS.c.metadata, postgresql_using="gin")


def _scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Metadata filters require finite JSON scalar values")


def metadata_filter(where, *, column=None, depth=0):
    """Compile Chroma-style filters, rejecting unsupported/empty expressions."""
    column = EMBEDDINGS.c.metadata if column is None else column
    if not isinstance(where, Mapping) or not where or depth > 12 or len(where) > 100:
        raise ValueError("Metadata filter must be a nonempty bounded object")
    predicates = []
    for key, expression in where.items():
        if key in {"$and", "$or"}:
            if not isinstance(expression, list) or not 2 <= len(expression) <= 100:
                raise ValueError("Logical filters require 2–100 operands")
            parts = [metadata_filter(item, column=column, depth=depth + 1) for item in expression]
            if key == "$and":
                predicates.append(and_(*parts))
            else:
                from sqlalchemy import or_
                predicates.append(or_(*parts))
            continue
        if not isinstance(key, str) or not key or key.startswith("$") or len(key) > 256 or "\x00" in key:
            raise ValueError("Invalid metadata field")
        field = column[key]
        operators = expression if isinstance(expression, Mapping) else {"$eq": expression}
        if not operators or len(operators) > 8:
            raise ValueError("Invalid metadata comparison")
        for operator, value in operators.items():
            if operator in {"$eq", "$ne"}:
                scalar = _scalar(value)
                equality = column.contains({key: scalar})
                predicates.append(equality if operator == "$eq" else and_(column.has_key(key), ~equality))
            elif operator in {"$in", "$nin"}:
                if not isinstance(value, (list, tuple)) or len(value) > 10000:
                    raise ValueError("Metadata membership requires a bounded list")
                values = [_scalar(item) for item in value]
                # JSONB comparison preserves numbers, booleans and strings.
                from sqlalchemy import or_
                matches = or_(*(column.contains({key: item}) for item in values)) if values else false()
                predicates.append(matches if operator == "$in" else and_(column.has_key(key), ~matches))
            elif operator in {"$gt", "$gte", "$lt", "$lte"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Ordered metadata filters require finite numbers")
                # CASE protects numeric casts even if the planner reorders WHERE.
                number = case((func.jsonb_typeof(field) == "number", cast(field.astext, Numeric)), else_=None)
                predicates.append({"$gt": number > value, "$gte": number >= value,
                                   "$lt": number < value, "$lte": number <= value}[operator])
            else:
                raise ValueError(f"Unsupported metadata operator: {operator}")
    return and_(*predicates)


def _vector(value):
    if isinstance(value, (str, bytes)):
        raise ValueError("Embedding must be a numeric vector")
    try:
        values = list(value)
    except TypeError:
        raise ValueError("Embedding must be a numeric vector") from None
    if not 1 <= len(values) <= 16000:
        raise ValueError("Embedding dimension must be 1–16000")
    if any(isinstance(item, bool) or not isinstance(item, Real) or not math.isfinite(item)
           or abs(item) > 3.4028234663852886e38 for item in values):
        raise ValueError("Embedding values must be finite float32 numbers")
    values = list(struct.unpack(f"!{len(values)}f", struct.pack(f"!{len(values)}f", *values)))
    if not any(values):
        raise ValueError("Zero vectors have no cosine similarity")
    return values


def vector_records_hash(records):
    """Hash ID/text/metadata and original float32 vectors in sorted ID order."""
    digest, previous = hashlib.sha256(), None
    for record in records:
        identifier = record["id"]
        if previous is not None and identifier <= previous:
            raise ValueError("Vector records must have unique, ascending IDs")
        vector = [struct.unpack("!f", struct.pack("!f", value))[0] for value in _vector(record["embedding"])]
        payload = json.dumps([identifier, record["document"], record["metadata"], vector],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        previous = identifier
    return digest.hexdigest()


class PgVectorStoreProvider:
    def __init__(self, engine, *, batch_size=500, search_mode=None, ann_min_rows=None, ann_ef_search=None):
        if engine.dialect.name != "postgresql":
            raise ValueError("pgvector requires a PostgreSQL engine")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5000:
            raise ValueError("Invalid vector batch size")
        self.engine, self.batch_size = engine, batch_size
        from app.core import config
        self.search_mode = search_mode or getattr(config, "PGVECTOR_SEARCH_MODE", "exact")
        self.ann_min_rows = ann_min_rows if ann_min_rows is not None else getattr(config, "PGVECTOR_ANN_MIN_ROWS", 100000)
        self.ann_ef_search = ann_ef_search if ann_ef_search is not None else getattr(config, "PGVECTOR_ANN_EF_SEARCH", 200)
        if self.search_mode not in {"auto", "exact"} or not 1 <= self.ann_min_rows <= 10000000 or not 10 <= self.ann_ef_search <= 1000:
            raise ValueError("Invalid vector search settings")

    @staticmethod
    def search_index_name(collection):
        identifier = uuid.UUID(str(collection["id"])).hex
        dimension = int(collection["dimension"])
        if not 1 <= dimension <= 2000:
            raise ValueError("HNSW vector index supports dimensions 1–2000; retain exact search for this collection")
        return f"ix_pgv_hnsw_{identifier}_{dimension}"

    def maintain_search_index(self, index_ref, *, apply=False, min_rows=None):
        """Explicit operator maintenance; request handlers never run DDL.

        A collection-specific expression/partial index supports mixed embedding
        dimensions without casting unrelated rows. Concurrent creation preserves
        availability. Filtered searches deliberately keep their exact path.
        """
        with self.engine.connect() as connection:
            collection, scope = self._rows_scope(connection, index_ref)
            count = connection.scalar(select(func.count()).select_from(EMBEDDINGS).where(scope))
            schema = connection.scalar(text("SELECT current_schema()"))
        if not collection["dimension"]:
            return {"status": "empty", "rows": count}
        name = self.search_index_name(collection)
        minimum = self.ann_min_rows if min_rows is None else min_rows
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ValueError("Minimum vector rows must be positive")
        result = {"status": "planned", "index": name, "rows": count, "dimension": collection["dimension"],
                  "filtered_search": "exact", "knowledge_base_id": index_ref.knowledge_base_id}
        if not apply or count < minimum:
            return {**result, "status": "planned" if count >= minimum else "below_threshold"}
        quote = self.engine.dialect.identifier_preparer.quote
        qualified = f"{quote(schema)}.{quote(name)}"
        table = f"{quote(schema)}.pgvector_embeddings"
        identifier = str(uuid.UUID(str(collection["id"])))
        dimension = int(collection["dimension"])
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            from app.core import config
            memory_mb = getattr(config, "PGVECTOR_INDEX_MEMORY_MB", 256)
            if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or not 32 <= memory_mb <= 1024:
                raise ValueError("Vector index build memory must be 32–1024 MB")
            connection.exec_driver_sql(f"SET maintenance_work_mem='{memory_mb}MB'")
            # A bounded single builder avoids Docker /dev/shm exhaustion from
            # parallel maintenance allocations. This setting is session-local.
            connection.exec_driver_sql("SET max_parallel_maintenance_workers=0")
            try:
                existing = connection.execute(text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:name"),
                    {"schema": schema, "name": name}).first()
                if existing is not None and not existing[0]:
                    connection.exec_driver_sql(f"DROP INDEX CONCURRENTLY {qualified}")
                connection.exec_driver_sql(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {quote(name)} ON {table} "
                    f"USING hnsw ((embedding::vector({dimension})) vector_cosine_ops) "
                    f"WITH (m=16, ef_construction=128) WHERE collection_id='{identifier}'::uuid")
                connection.exec_driver_sql(f"ANALYZE {table}")
            finally:
                # Return a clean session to the pool even when building fails.
                try:
                    connection.exec_driver_sql("RESET maintenance_work_mem")
                    connection.exec_driver_sql("RESET max_parallel_maintenance_workers")
                except Exception:
                    connection.invalidate()
        return {**result, "status": "ready"}

    def _ann_available(self, connection, collection):
        if self.search_mode == "exact" or not collection["dimension"] or collection["dimension"] > 2000:
            return False
        name = self.search_index_name(collection)
        gate = connection.execute(text("SELECT i.indisvalid, obj_description(c.oid, 'pg_class') AS quality FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname=:name"), {"name": name})
        gate = gate.mappings().first()
        if not gate or not gate["indisvalid"]:
            return False
        try:
            quality = json.loads(gate["quality"] or "{}")
        except (ValueError, TypeError):
            return False
        if (quality.get("format") != "rag-ann-quality-v1" or not quality.get("accepted")
                or quality.get("embedding_profile_id") != collection["embedding_profile_id"]
                or quality.get("index_version") != collection["index_version"]
                or quality.get("ef_search") != self.ann_ef_search):
            return False
        bounded = select(EMBEDDINGS.c.id).where(EMBEDDINGS.c.collection_id == collection["id"]).limit(self.ann_min_rows).subquery()
        return connection.scalar(select(func.count()).select_from(bounded)) >= self.ann_min_rows

    def validate_search_index(self, index_ref, *, apply=False, samples=20, k=10,
                              min_recall=0.95, max_p95_ms=1000):
        """Operator-only ANN acceptance against exact search using stored vectors.

        No model calls or source edits. The persisted gate is tied to the index,
        embedding/pipeline identity and ef_search. New/rebuilt indexes remain
        exact until an operator accepts a passing result. This is ANN recall,
        not an answer-quality or concurrent-load benchmark.
        """
        if (isinstance(samples, bool) or not isinstance(samples, int) or not 10 <= samples <= 100
                or isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100
                or not 0.9 <= min_recall <= 1 or not 1 <= max_p95_ms <= 60000):
            raise ValueError("Invalid ANN acceptance bounds")
        with self.engine.begin() as connection:
            collection, scope = self._rows_scope(connection, index_ref, lock=True)
            name = self.search_index_name(collection)
            index_oid = connection.scalar(text("SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_index i ON i.indexrelid=c.oid WHERE n.nspname=current_schema() AND c.relname=:name AND i.indisvalid"), {"name": name})
            if not index_oid:
                raise ValueError("Build a valid HNSW index before quality validation")
            connection.execute(text("SELECT set_config('statement_timeout', '30000', true)"))
            vectors = list(connection.scalars(select(EMBEDDINGS.c.embedding).where(scope)
                .order_by(func.md5(EMBEDDINGS.c.id)).limit(samples)))
            if len(vectors) < 10:
                raise ValueError("ANN acceptance needs at least 10 stored query vectors")
            identifier = str(uuid.UUID(str(collection["id"])))
            ann_scope = EMBEDDINGS.c.collection_id == literal_column(f"'{identifier}'::uuid")
            recalls, timings, used = [], [], True
            for vector in vectors:
                distance = EMBEDDINGS.c.embedding.cosine_distance(vector)
                expected = set(connection.scalars(select(EMBEDDINGS.c.id).where(scope)
                    .order_by(distance, EMBEDDINGS.c.id).limit(k)))
                distance = cast(EMBEDDINGS.c.embedding, VECTOR(collection["dimension"])).cosine_distance(vector)
                statement = select(EMBEDDINGS.c.id).where(ann_scope).order_by(distance).limit(k)
                connection.execute(text("SELECT set_config('hnsw.ef_search', :value, true)"), {"value": str(max(k, self.ann_ef_search))})
                connection.execute(text("SELECT set_config('enable_seqscan', 'off', true)"))
                connection.execute(text("SELECT set_config('enable_sort', 'off', true)"))
                # EXPLAIN is inspection only; ensure a reported recall did use ANN.
                compiled = statement.compile(dialect=self.engine.dialect, compile_kwargs={"literal_binds": True})
                plan = connection.exec_driver_sql("EXPLAIN " + str(compiled)).scalars().all()
                used = used and any(name in line for line in plan)
                started = time.perf_counter()
                actual = set(connection.scalars(statement))
                timings.append((time.perf_counter() - started) * 1000)
                recalls.append(len(actual & expected) / len(expected) if expected else 0)
                connection.execute(text("SELECT set_config('enable_seqscan', 'on', true)"))
                connection.execute(text("SELECT set_config('enable_sort', 'on', true)"))
            p95 = sorted(timings)[max(0, math.ceil(len(timings) * .95) - 1)]
            passed = used and min(recalls) >= min_recall and p95 <= max_p95_ms
            report = {"format": "rag-ann-quality-v1", "accepted": bool(apply and passed),
                "passed": passed, "samples": len(vectors), "k": k, "min_recall": min(recalls),
                "mean_recall": sum(recalls) / len(recalls), "p95_ms": round(p95, 3),
                "required_recall": min_recall, "max_p95_ms": max_p95_ms, "index_used": used,
                "embedding_profile_id": collection["embedding_profile_id"], "index_version": collection["index_version"],
                "ef_search": self.ann_ef_search, "models_called": False,
                "limits": "Stored-vector probes only; validate business retrieval and concurrent load separately"}
            if apply:
                schema = connection.scalar(text("SELECT current_schema()"))
                quote = self.engine.dialect.identifier_preparer.quote
                # Only bounded generated metadata is written; no document content.
                value = json.dumps(report, separators=(",", ":")).replace("'", "''")
                connection.exec_driver_sql(f"COMMENT ON INDEX {quote(schema)}.{quote(name)} IS '{value}'")
            return report

    def identity(self):
        return ProviderIdentity(kind="vector_store", provider="pgvector",
            capabilities=("metadata_filter", "versioned_collection", "cosine", "transactional_upsert", "knowledge_base_isolation"))

    @staticmethod
    def _scope(ref, *, require_name=True):
        if not isinstance(ref.knowledge_base_id, str) or not 1 <= len(ref.knowledge_base_id) <= 36:
            raise ValueError("Vector operations require an explicit knowledge base")
        if require_name and (not isinstance(ref.name, str) or not 1 <= len(ref.name) <= 255):
            raise ValueError("Vector index name is required")
        for value in (ref.index_version, ref.embedding_profile_id):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 255):
                raise ValueError("Invalid vector index version/profile")
        return and_(COLLECTIONS.c.knowledge_base_id == ref.knowledge_base_id, COLLECTIONS.c.name == ref.name)

    def _collection(self, connection, ref, *, lock=False):
        statement = select(COLLECTIONS).where(self._scope(ref))
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise ValueError("Vector collection does not exist in this knowledge base")
        for key in ("index_version", "embedding_profile_id"):
            requested = getattr(ref, key)
            if requested and row[key] and requested != row[key]:
                raise ValueError(f"Vector collection {key} does not match")
            if requested and not row[key]:
                populated = connection.scalar(select(EMBEDDINGS.c.id).where(
                    EMBEDDINGS.c.collection_id == row["id"]).limit(1)) is not None
                if populated:
                    raise ValueError("Legacy vector identity is unverified; rebuild into a new shadow collection before use")
        return row

    def _rows_scope(self, connection, ref, *, lock=False):
        collection = self._collection(connection, ref, lock=lock)
        return collection, EMBEDDINGS.c.collection_id == collection["id"]

    def open(self, embedding, index_ref):
        self._scope(index_ref, require_name=False)
        if index_ref.name is not None:
            self._scope(index_ref)
            with self.engine.begin() as connection:
                connection.execute(insert(COLLECTIONS).values(id=str(uuid.uuid4()), knowledge_base_id=index_ref.knowledge_base_id,
                    name=index_ref.name, index_version=index_ref.index_version, embedding_profile_id=index_ref.embedding_profile_id)
                    .on_conflict_do_nothing(index_elements=[COLLECTIONS.c.knowledge_base_id, COLLECTIONS.c.name]))
                row = self._collection(connection, index_ref, lock=True)
                missing = {key: getattr(index_ref, key) for key in ("index_version", "embedding_profile_id") if getattr(index_ref, key) and row[key] is None}
                if missing:
                    connection.execute(update(COLLECTIONS).where(COLLECTIONS.c.id == row["id"]).values(**missing))
        return PgVectorStore(self, embedding, index_ref)

    def probe(self):
        """Check connectivity/schema with bounded reads, independent of corpus size."""
        try:
            with self.engine.connect() as connection:
                connection.execute(select(literal_column("1")).select_from(COLLECTIONS).limit(1))
                connection.execute(select(literal_column("1")).select_from(EMBEDDINGS).limit(1))
            return ProviderHealth.ok(provider="pgvector", schema_available=True)
        except Exception as exc:
            return ProviderHealth.fail(f"PostgreSQL vector store unavailable ({type(exc).__name__}); check migrations and connectivity")

    def health(self, index_ref, expected_ids=None):
        try:
            with self.engine.connect() as connection:
                collection, scope = self._rows_scope(connection, index_ref)
                vector_count = connection.scalar(select(func.count()).select_from(EMBEDDINGS).where(scope))
                identifiers = set(connection.scalars(select(EMBEDDINGS.c.id).where(scope))) if expected_ids is not None else None
            details = {"provider": "pgvector", "collection": index_ref.name, "knowledge_base_id": index_ref.knowledge_base_id,
                       "vector_count": vector_count, "dimension": collection["dimension"], "distance": "cosine"}
            if expected_ids is not None and identifiers != expected_ids:
                return ProviderHealth.fail("Vector IDs do not match active IDs", chunk_count=len(expected_ids),
                    missing=sorted(expected_ids - identifiers)[:5], unexpected=sorted(identifiers - expected_ids)[:5], **details)
            return ProviderHealth.ok(**details)
        except ValueError as exc:
            return ProviderHealth.fail(str(exc))
        except Exception as exc:
            # SQL/DSNs and metadata may contain private data; do not echo them.
            return ProviderHealth.fail(f"PostgreSQL vector store unavailable ({type(exc).__name__}); check migrations and connectivity")

    def list_indexes(self, *, knowledge_base_id=None):
        statement = select(COLLECTIONS, func.count(EMBEDDINGS.c.id).label("vector_count")).outerjoin(
            EMBEDDINGS, EMBEDDINGS.c.collection_id == COLLECTIONS.c.id).group_by(COLLECTIONS.c.id).order_by(COLLECTIONS.c.name)
        if knowledge_base_id is not None:
            statement = statement.where(COLLECTIONS.c.knowledge_base_id == knowledge_base_id)
        with self.engine.connect() as connection:
            return [VectorIndexInfo(ref=VectorIndexRef(name=row["name"], knowledge_base_id=row["knowledge_base_id"],
                index_version=row["index_version"], embedding_profile_id=row["embedding_profile_id"]), count=row["vector_count"])
                for row in connection.execute(statement).mappings()]

    def delete_index(self, index_ref):
        with self.engine.begin() as connection:
            connection.execute(delete(COLLECTIONS).where(self._scope(index_ref)))

    def count(self, index_ref):
        with self.engine.connect() as connection:
            _, scope = self._rows_scope(connection, index_ref)
            return connection.scalar(select(func.count()).select_from(EMBEDDINGS).where(scope))

    def list_ids(self, index_ref, where=None):
        predicate = metadata_filter(where) if where is not None else None
        with self.engine.connect() as connection:
            _, scope = self._rows_scope(connection, index_ref)
            return set(connection.scalars(select(EMBEDDINGS.c.id).where(scope, *([predicate] if predicate is not None else []))))

    def delete_vectors(self, index_ref, *, ids=None, where=None):
        if ids is None and where is None:
            raise ValueError("Vector deletion requires IDs or a nonempty metadata filter")
        identifiers = PgVectorStore._ids(ids) if ids is not None else None
        predicate = metadata_filter(where) if where is not None else None
        if identifiers == []:
            return
        with self.engine.begin() as connection:
            _, scope = self._rows_scope(connection, index_ref, lock=True)
            statement = delete(EMBEDDINGS).where(scope)
            if identifiers is not None:
                statement = statement.where(EMBEDDINGS.c.id.in_(identifiers))
            if predicate is not None:
                statement = statement.where(predicate)
            connection.execute(statement)

    def import_records(self, index_ref, records, *, expected_count, expected_hash):
        """Offline atomic import; never overwrite a populated, different index.

        The iterator supplies sorted {id, document, metadata, embedding} rows.
        Its expected digest must be computed from the immutable source snapshot.
        A failed batch or digest comparison rolls back the entire collection.
        """
        self._scope(index_ref)
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
            raise ValueError("Invalid expected vector count")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("Expected vector SHA-256 is required")
        with self.engine.begin() as connection:
            connection.execute(insert(COLLECTIONS).values(id=str(uuid.uuid4()), knowledge_base_id=index_ref.knowledge_base_id,
                name=index_ref.name, index_version=index_ref.index_version, embedding_profile_id=index_ref.embedding_profile_id)
                .on_conflict_do_nothing(index_elements=[COLLECTIONS.c.knowledge_base_id, COLLECTIONS.c.name]))
            collection, scope = self._rows_scope(connection, index_ref, lock=True)
            existing_count = connection.scalar(select(func.count()).select_from(EMBEDDINGS).where(scope))
            def current_hash():
                rows = connection.execute(select(EMBEDDINGS.c.id, EMBEDDINGS.c.document, EMBEDDINGS.c.metadata, EMBEDDINGS.c.embedding)
                    .where(scope).order_by(EMBEDDINGS.c.id.collate("C")).execution_options(stream_results=True, yield_per=self.batch_size)).mappings()
                return vector_records_hash(rows)
            if existing_count:
                if existing_count == expected_count and current_hash() == expected_hash:
                    return {"status": "verified_existing", "count": existing_count, "sha256": expected_hash}
                raise ValueError("Target vector collection is nonempty and differs from source; refusing overwrite")
            store = PgVectorStore(self, None, index_ref)
            batch, count, dimension, previous = [], 0, collection["dimension"], None
            for record in records:
                if previous is not None and record["id"] <= previous:
                    raise ValueError("Vector records must have unique, ascending IDs")
                previous = record["id"]
                row = store._payload([record["document"]], [record["metadata"]], [record["id"]])[0]
                vector = _vector(record["embedding"])
                if dimension is not None and len(vector) != dimension:
                    raise ValueError("Imported vector dimension does not match collection")
                dimension = len(vector)
                batch.append({**row, "collection_id": collection["id"], "embedding": vector})
                count += 1
                if len(batch) >= self.batch_size:
                    connection.execute(insert(EMBEDDINGS), batch)
                    batch.clear()
            if batch:
                connection.execute(insert(EMBEDDINGS), batch)
            if count != expected_count or current_hash() != expected_hash:
                raise ValueError("Imported vector count/content digest verification failed")
            connection.execute(update(COLLECTIONS).where(COLLECTIONS.c.id == collection["id"]).values(dimension=dimension))
            return {"status": "imported", "count": count, "sha256": expected_hash}


class PgVectorStore(VectorStore):
    """LangChain-compatible operations; cosine distance and [0,1] relevance."""

    def __init__(self, provider, embedding, index_ref):
        self.provider, self._embedding, self.index_ref = provider, embedding, index_ref

    @property
    def embeddings(self):
        return self._embedding

    @staticmethod
    def _ids(ids):
        if isinstance(ids, (str, bytes)):
            raise ValueError("Vector IDs must be a sequence")
        values = list(ids)
        if any(not isinstance(value, str) or not 1 <= len(value) <= 512 or "\x00" in value for value in values):
            raise ValueError("Invalid vector ID")
        if len(values) != len(set(values)):
            raise ValueError("Duplicate vector IDs in a batch")
        return values

    def _payload(self, texts, metadatas, ids):
        if isinstance(texts, (str, bytes)):
            raise ValueError("Texts must be an iterable of documents")
        texts = list(texts)
        metadata = list(metadatas) if metadatas is not None else [{} for _ in texts]
        identifiers = self._ids(ids) if ids is not None else [str(uuid.uuid4()) for _ in texts]
        if len(texts) != len(metadata) or len(texts) != len(identifiers):
            raise ValueError("Texts, metadata and IDs must have equal lengths")
        rows = []
        for text, item, identifier in zip(texts, metadata, identifiers):
            if not isinstance(text, str) or not isinstance(item, Mapping):
                raise ValueError("Documents must contain text and metadata objects")
            if item.get("knowledge_base_id", self.index_ref.knowledge_base_id) != self.index_ref.knowledge_base_id:
                raise ValueError("Document metadata belongs to another knowledge base")
            try:
                item = json.loads(json.dumps(dict(item), allow_nan=False))
            except (TypeError, ValueError):
                raise ValueError("Document metadata must be finite JSON") from None
            rows.append({"id": identifier, "document": text, "metadata": item})
        return rows

    def _write(self, rows, embeddings):
        if self.index_ref.name is None:
            raise ValueError("No active vector index configured")
        vectors = [_vector(value) for value in embeddings]
        if len(vectors) != len(rows) or len({len(value) for value in vectors}) > 1:
            raise ValueError("Embedding batch count or dimensions do not match documents")
        if not rows:
            return []
        dimension = len(vectors[0])
        with self.provider.engine.begin() as connection:
            collection = self.provider._collection(connection, self.index_ref, lock=True)
            if collection["dimension"] is not None and collection["dimension"] != dimension:
                raise ValueError("Embedding dimension differs from collection; create a new version")
            if collection["dimension"] is None:
                connection.execute(update(COLLECTIONS).where(COLLECTIONS.c.id == collection["id"]).values(dimension=dimension))
            values = [{**row, "collection_id": collection["id"], "embedding": vector} for row, vector in zip(rows, vectors)]
            for start in range(0, len(values), self.provider.batch_size):
                statement = insert(EMBEDDINGS).values(values[start:start + self.provider.batch_size])
                connection.execute(statement.on_conflict_do_update(index_elements=[EMBEDDINGS.c.collection_id, EMBEDDINGS.c.id],
                    set_={"document": statement.excluded.document, "metadata": statement.excluded.metadata, "embedding": statement.excluded.embedding}))
        return [row["id"] for row in rows]

    def add_texts(self, texts: Iterable[str], metadatas=None, ids=None, **kwargs):
        if kwargs:
            raise ValueError("Unsupported vector write options")
        rows = self._payload(texts, metadatas, ids)
        if not rows:
            return []
        if self.index_ref.name is None:
            raise ValueError("No active vector index configured")
        vectors = self._embedding.embed_documents([row["document"] for row in rows])
        return self._write(rows, vectors)

    def add_documents(self, documents, **kwargs):
        documents = list(documents)
        return self.add_texts([doc.page_content for doc in documents], metadatas=[doc.metadata for doc in documents], **kwargs)

    def add_embeddings(self, text_embeddings, metadatas=None, ids=None, **kwargs):
        if kwargs:
            raise ValueError("Unsupported vector write options")
        pairs = list(text_embeddings)
        return self._write(self._payload([item[0] for item in pairs], metadatas, ids), [item[1] for item in pairs])

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, **kwargs):
        provider, ref = kwargs.pop("provider", None), kwargs.pop("index_ref", None)
        if provider is None or ref is None:
            raise ValueError("from_texts requires an explicit provider and knowledge-base index_ref")
        store = provider.open(embedding, ref)
        store.add_texts(texts, metadatas=metadatas, **kwargs)
        return store

    def delete(self, ids=None, **kwargs):
        if "where" in kwargs and "filter" in kwargs:
            raise ValueError("Use one metadata filter")
        where = kwargs.pop("where", kwargs.pop("filter", None))
        if kwargs:
            raise ValueError("Unsupported vector deletion options")
        self.provider.delete_vectors(self.index_ref, ids=ids, where=where)
        return True

    def _search(self, embedding, k, filter=None, **kwargs):
        if kwargs:
            raise ValueError("Unsupported vector search options")
        if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k <= 10000:
            raise ValueError("Search limit must be 0–10000")
        predicate = metadata_filter(filter) if filter is not None else None
        if not k or self.index_ref.name is None:
            return []
        vector = _vector(embedding)
        with self.provider.engine.connect() as connection:
            collection, scope = self.provider._rows_scope(connection, self.index_ref)
            if collection["dimension"] is None:
                return []
            if collection["dimension"] != len(vector):
                raise ValueError("Query embedding dimension does not match collection")
            if predicate is None and k <= 1000 and self.provider._ann_available(connection, collection):
                # Validated UUID literal lets PostgreSQL prove the partial-index
                # predicate even after the driver switches to prepared queries.
                identifier = str(uuid.UUID(str(collection["id"])))
                ann_scope = EMBEDDINGS.c.collection_id == literal_column(f"'{identifier}'::uuid")
                distance = cast(EMBEDDINGS.c.embedding, VECTOR(collection["dimension"])).cosine_distance(vector).label("distance")
                connection.execute(text("SELECT set_config('hnsw.ef_search', :value, true)"),
                                   {"value": str(max(k, self.provider.ann_ef_search))})
                statement = select(EMBEDDINGS.c.document, EMBEDDINGS.c.metadata, distance).where(ann_scope).order_by(distance).limit(k)
                rows = list(connection.execute(statement).mappings())
                if len(rows) >= k:
                    return [(Document(page_content=row["document"], metadata=row["metadata"]), float(row["distance"])) for row in rows]
                # A small/deleted collection or restrictive index scan must not
                # silently underfill results. Exact execution remains available.
            distance = EMBEDDINGS.c.embedding.cosine_distance(vector).label("distance")
            statement = select(EMBEDDINGS.c.document, EMBEDDINGS.c.metadata, distance).where(scope)
            if predicate is not None:
                statement = statement.where(predicate)
            rows = connection.execute(statement.order_by(distance, EMBEDDINGS.c.id).limit(k)).mappings()
            return [(Document(page_content=row["document"], metadata=row["metadata"]), float(row["distance"])) for row in rows]

    def similarity_search_with_score(self, query, k=4, filter=None, **kwargs):
        if self.index_ref.name is None:
            return self._search([], k, filter=filter, **kwargs)
        if filter is not None:
            metadata_filter(filter)
        return self._search(self._embedding.embed_query(query), k, filter=filter, **kwargs)

    def similarity_search_by_vector_with_score(self, embedding, k=4, **kwargs):
        return self._search(embedding, k, **kwargs)

    @staticmethod
    def _relevance(rows, threshold=None):
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1):
            raise ValueError("Relevance threshold must be 0–1")
        values = [(doc, max(0.0, min(1.0, 1 - distance / 2))) for doc, distance in rows]
        return [(doc, score) for doc, score in values if threshold is None or score >= threshold]

    def similarity_search_with_relevance_scores(self, query, k=4, **kwargs):
        threshold = kwargs.pop("score_threshold", None)
        return self._relevance(self.similarity_search_with_score(query, k=k, **kwargs), threshold)

    def similarity_search_by_vector_with_relevance_scores(self, embedding, k=4, **kwargs):
        threshold = kwargs.pop("score_threshold", None)
        return self._relevance(self._search(embedding, k, **kwargs), threshold)

    def similarity_search(self, query, k=4, **kwargs):
        return [doc for doc, _ in self.similarity_search_with_score(query, k=k, **kwargs)]

    def similarity_search_by_vector(self, embedding, k=4, **kwargs):
        return [doc for doc, _ in self._search(embedding, k, **kwargs)]

    def get(self, ids=None, where=None, limit=None, offset=0, include=None):
        """Export original vectors for offline migration/verification, without re-embedding."""
        include = ["documents", "metadatas"] if include is None else list(include)
        if set(include) - {"documents", "metadatas", "embeddings"} or offset < 0 or (limit is not None and limit < 0):
            raise ValueError("Invalid vector export options")
        predicate = metadata_filter(where) if where is not None else None
        identifiers = self._ids(ids) if ids is not None else None
        result = {"ids": [], **{key: [] for key in include}}
        if self.index_ref.name is None or identifiers == []:
            return result
        with self.provider.engine.connect() as connection:
            _, scope = self.provider._rows_scope(connection, self.index_ref)
            statement = select(EMBEDDINGS).where(scope).order_by(EMBEDDINGS.c.id.collate("C")).offset(offset)
            if identifiers is not None:
                statement = statement.where(EMBEDDINGS.c.id.in_(identifiers))
            if predicate is not None:
                statement = statement.where(predicate)
            if limit is not None:
                statement = statement.limit(limit)
            for row in connection.execute(statement).mappings():
                result["ids"].append(row["id"])
                for key, field in (("documents", "document"), ("metadatas", "metadata"), ("embeddings", "embedding")):
                    if key in include:
                        result[key].append(list(row[field]) if key == "embeddings" else row[field])
        return result
