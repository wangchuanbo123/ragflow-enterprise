"""Compare exact/HNSW and cold/warm graph reads in a disposable PostgreSQL schema.

All documents/vectors are synthetic. This is a single-client component benchmark,
not a production capacity promise or a substitute for model/HTTP load tests.
"""

import argparse
import json
import os
from pathlib import Path
import time
import uuid

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.orm import Session


def benchmark(url, *, rows=10000, dimensions=256, queries=10, ann_ef_search=400):
    from app.models import Base
    from rag.providers.interfaces import VectorIndexRef
    from rag.providers.pgvector_store import PGVECTOR_METADATA, PgVectorStoreProvider
    from app.services.graph_filter_service import GraphFilterService
    from app.schemas.graph_filter import GraphFilterPreviewRequest
    from tests.test_graph_filters import seed
    if not 100 <= rows <= 100000 or not 2 <= dimensions <= 2000 or not 1 <= queries <= 100:
        raise ValueError("Invalid bounded benchmark parameters")
    namespace = "search_bench_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(CreateSchema(namespace))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={namespace},public"})
    report = {"rows": rows, "dimensions": dimensions, "queries": queries, "ann_ef_search": ann_ef_search,
              "warmup_queries": 2, "query_kind": "independent held-out random unit vectors", "seed": 20261106,
              "cloud_model_calls": 0}
    try:
        Base.metadata.create_all(engine)
        PGVECTOR_METADATA.create_all(engine)
        with engine.connect() as connection:
            report["pgvector_version"] = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
            report["postgresql_version"] = connection.scalar(text("SHOW server_version"))
        provider = PgVectorStoreProvider(engine, search_mode="auto", ann_min_rows=100, ann_ef_search=ann_ef_search)
        ref = VectorIndexRef("synthetic-v1", "benchmark-kb")
        store = provider.open(None, ref)
        vectors = np.random.default_rng(20261106).normal(size=(rows, dimensions)).astype("float32")
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        query_vectors = np.random.default_rng(61).normal(size=(queries + 2, dimensions)).astype("float32")
        query_vectors /= np.linalg.norm(query_vectors, axis=1, keepdims=True)
        for offset in range(0, rows, 500):
            stop = min(offset + 500, rows)
            store.add_embeddings([(f"Synthetic vector {number}", vectors[number]) for number in range(offset, stop)],
                ids=[f"vector-{number:08}" for number in range(offset, stop)],
                metadatas=[{"position": number, "document_id": f"doc-{number % 100}"} for number in range(offset, stop)])
        exact = PgVectorStoreProvider(engine, search_mode="exact").open(None, ref)
        started = time.perf_counter()
        index = provider.maintain_search_index(ref, apply=True, min_rows=100)
        report["index_build_seconds"] = round(time.perf_counter() - started, 3)
        samples = []
        for filtered in (False, True):
            where = {"document_id": {"$in": ["doc-0", "doc-1", "doc-2", "doc-3", "doc-4"]}} if filtered else None
            for name, source in (("exact", exact), ("auto_hnsw", store)):
                latencies, recalls = [], []
                eligible = np.array([number for number in range(rows) if not filtered or number % 100 < 5])
                for warmup in range(2):
                    source.similarity_search_by_vector(query_vectors[warmup], k=10, **({"filter": where} if where else {}))
                for query in range(queries):
                    query_vector = query_vectors[query + 2]
                    expected = set(eligible[np.argsort(-(vectors[eligible] @ query_vector))[:10]])
                    started = time.perf_counter()
                    result = source.similarity_search_by_vector(query_vector, k=10, **({"filter": where} if where else {}))
                    latencies.append((time.perf_counter() - started) * 1000)
                    actual = {document.metadata["position"] for document in result}
                    assert len(actual) == 10 and actual <= set(eligible)
                    recalls.append(len(expected & actual) / 10)
                samples.append({"mode": name, "filtered": filtered, "p50_ms": round(float(np.percentile(latencies, 50)), 3),
                    "p95_ms": round(float(np.percentile(latencies, 95)), 3), "recall_at_10": round(float(np.mean(recalls)), 5)})
        assert all(sample["recall_at_10"] == 1 for sample in samples if sample["filtered"] or sample["mode"] == "exact")
        report["vector_samples"] = samples
        ann = next(sample for sample in samples if not sample["filtered"] and sample["mode"] == "auto_hnsw")
        baseline = next(sample for sample in samples if not sample["filtered"] and sample["mode"] == "exact")
        report["ann_recommended_for_this_sample"] = ann["recall_at_10"] >= .98 and ann["p95_ms"] < baseline["p95_ms"] * .9
        report["recommendation"] = "ANN requires representative recall and latency acceptance; retain exact mode when this gate fails."
        with Session(engine) as db:
            seed(db, extra=5000)
        graph_times = []
        for number in range(5):
            with Session(engine) as db:
                started = time.perf_counter()
                result = GraphFilterService(db, "filter-kb", "reader").preview(GraphFilterPreviewRequest(principles=[]))
                graph_times.append((time.perf_counter() - started) * 1000)
                assert result.stats.total_nodes == 5006
        report["graph"] = {"effective_nodes": 5006, "cold_ms": round(graph_times[0], 3),
                           "warm_p50_ms": round(float(np.median(graph_times[1:])), 3),
                           "warm_p95_ms": round(float(np.percentile(graph_times[1:], 95)), 3)}
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(namespace, cascade=True))
        admin.dispose()
    report["temporary_schema_removed"] = True
    report["limits"] = "Synthetic single-client local component benchmark, no external models or production capacity claim."
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=256)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--ann-ef-search", type=int, default=400)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        parser.error("TEST_POSTGRES_URL must select disposable test infrastructure")
    result = benchmark(url, rows=args.rows, dimensions=args.dimensions, queries=args.queries, ann_ef_search=args.ann_ef_search)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
