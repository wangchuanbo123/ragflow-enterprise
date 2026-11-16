"""Measure exact scoped pgvector retrieval using synthetic vectors in a temporary schema."""

import argparse
import json
import os
from pathlib import Path
import platform
import time
import uuid

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateSchema, DropSchema

from rag.providers.interfaces import VectorIndexRef
from rag.providers.pgvector_store import PGVECTOR_METADATA, PgVectorStoreProvider


def benchmark(url, *, counts=(10000, 50000), dimensions=1024, queries=10):
    if not counts or any(not isinstance(count, int) or not 10 <= count <= 1000000 for count in counts):
        raise ValueError("Benchmark counts must be 10–1000000")
    if not 1 <= dimensions <= 16000 or not 1 <= queries <= 100:
        raise ValueError("Invalid benchmark dimensions/query count")
    schema = "bench_pgvector_" + uuid.uuid4().hex
    admin = create_engine(url, pool_pre_ping=True)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(CreateSchema(schema))
        server_version = connection.scalar(text("SHOW server_version"))
        extension_version = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    report = {"status": "completed", "method": "exact_cosine", "dimensions": dimensions, "queries_per_scope": queries,
        "postgresql": server_version, "pgvector_extension": extension_version, "platform": platform.platform(),
        "python": platform.python_version(), "logical_cpus": os.cpu_count(), "models_called": False, "samples": []}
    try:
        PGVECTOR_METADATA.create_all(engine)
        provider = PgVectorStoreProvider(engine)
        ref = VectorIndexRef("benchmark", "benchmark-kb")
        store = provider.open(None, ref)
        random = np.random.default_rng(20261105)
        vectors = random.standard_normal((max(counts), dimensions), dtype=np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        written = 0
        for count in sorted(set(counts)):
            start = time.perf_counter()
            for offset in range(written, count, 500):
                stop = min(offset + 500, count)
                store.add_embeddings(((f"Synthetic vector {index}", vectors[index]) for index in range(offset, stop)),
                    ids=[f"vector-{index:08}" for index in range(offset, stop)],
                    metadatas=[{"document_id": f"doc-{index % 100}", "position": index} for index in range(offset, stop)])
            elapsed = time.perf_counter() - start
            with engine.begin() as connection:
                connection.execute(text("ANALYZE pgvector_embeddings"))
            sample = {"rows": count, "inserted_rows": count - written, "insert_seconds": round(elapsed, 3), "scopes": []}
            for filtered in (False, True):
                selected = np.arange(count)
                where = None
                if filtered:
                    selected = selected[selected % 100 < 5]
                    where = {"document_id": {"$in": [f"doc-{index}" for index in range(5)]}}
                latencies, recalls = [], []
                for index in range(queries):
                    query = vectors[index]
                    expected = set(selected[np.argsort(-(vectors[selected] @ query))[:10]].tolist())
                    started = time.perf_counter()
                    rows = store.similarity_search_by_vector_with_relevance_scores(query, k=10, **({"filter": where} if where else {}))
                    latencies.append((time.perf_counter() - started) * 1000)
                    actual = {doc.metadata["position"] for doc, score in rows}
                    recalls.append(len(actual & expected) / len(expected))
                    assert len(actual) == min(10, len(selected)) and actual.issubset(set(selected.tolist()))
                sample["scopes"].append({"filter": "5_percent_documents" if filtered else "entire_collection", "eligible_rows": len(selected),
                    "p50_ms": round(float(np.percentile(latencies, 50)), 3), "p95_ms": round(float(np.percentile(latencies, 95)), 3),
                    "min_ms": round(min(latencies), 3), "max_ms": round(max(latencies), 3), "recall_at_10": round(float(np.mean(recalls)), 6)})
            report["samples"].append(sample)
            written = count
        assert all(scope["recall_at_10"] == 1 for sample in report["samples"] for scope in sample["scopes"])
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
    report["temporary_schema_removed"] = True
    report["limits"] = "Synthetic single-client local database benchmark; no ANN/concurrency/production capacity claim."
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL"))
    parser.add_argument("--counts", nargs="+", type=int, default=[10000, 50000])
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("Provide --database-url or TEST_POSTGRES_URL for the isolated benchmark")
    report = benchmark(args.database_url, counts=args.counts, dimensions=args.dimensions, queries=args.queries)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
