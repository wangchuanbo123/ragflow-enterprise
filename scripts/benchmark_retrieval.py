"""Measure local BM25 capacity using public synthetic chunks and isolated storage.

Run with ``python -m scripts.benchmark_retrieval --chunks 10000 --queries 30
--output eval/results/benchmark_10000.json``. This is a sequential, warm-tokenizer
BM25 benchmark, not a vector, graph, model, HTTP-concurrency or full-RAG benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _percentile(values: list[float], percentile: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * percentile / 100) - 1)]


def _content(number: int) -> str:
    topic = ("订单处理", "库存管理", "支付核对", "任务调度", "文档检索", "客户支持")[number % 6]
    paragraph = (
        f"公共合成知识片段 record_{number:08d}，业务主题：{topic}。"
        f"Synthetic service_{number % 64:02d} handles request validation, audit records, "
        "scheduled operations and retry processing. "
        "系统先验证请求字段，再保存处理结果；遇到超时则记录错误并等待重试。"
        "维护人员可以查看任务状态、检索说明文档和检查依赖服务。"
    )
    return paragraph + "\n" + paragraph


def _query(number: int, chunk_count: int) -> tuple[str, str]:
    if number % 3 == 0:
        return f"record_{(number * 997) % chunk_count:08d}", "specific_record"
    if number % 3 == 1:
        return f"service_{number % 64:02d} retry processing", "service_terms"
    return ("订单处理", "库存管理", "支付核对", "任务调度", "文档检索", "客户支持")[number % 6], "chinese_topic"


def _run(chunk_count: int, query_count: int) -> dict:
    # Do not read .env, generate application credentials or initialize the real
    # application's database. Even the database module's mkdir targets temp.
    with tempfile.TemporaryDirectory(prefix="rag-bm25-benchmark-") as temporary:
        root = Path(temporary)
        database_url = f"sqlite:///{(root / 'metadata.sqlite').as_posix()}"
        environment = {
            "APP_PROFILE": "test",
            "DATABASE_URL": database_url,
            "JWT_SECRET_KEY": "benchmark-isolated-nonproduction-secret",
            "INITIAL_ADMIN_PASSWORD": "benchmark-isolated-nonproduction-password",
            "INDEX_WORKER_ENABLED": "false",
            "CONNECTOR_SCHEDULER_ENABLED": "false",
            "BACKUP_MONTHLY_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch("dotenv.load_dotenv", return_value=False):
            from app.core import config

            config.DATA_DIR = root
            config.DOC_DIR = root / "documents"
            config.VECTOR_DB_DIR = root / "vectors"

            import jieba
            import psutil
            from sqlalchemy import create_engine, insert
            from sqlalchemy.orm import Session
            from app.models import Base
            from app.models.knowledge_base import KnowledgeBase
            from app.models.knowledge_chunk import KnowledgeChunk
            from app.models.knowledge_document import KnowledgeDocument
            from rag.retrievers.bm25_retriever import ChunkBM25Retriever

            engine = create_engine(database_url)
            try:
                Base.metadata.create_all(engine)
                document_count = math.ceil(chunk_count / 100)
                corpus_hash = hashlib.sha256()
                character_count = byte_count = 0
                seed_started = time.perf_counter()
                with Session(engine) as db:
                    db.add(KnowledgeBase(id="benchmark", name="Public synthetic benchmark", slug="benchmark"))
                    db.flush()
                    db.execute(insert(KnowledgeDocument), [{
                        "id": f"benchmark-doc-{number}", "knowledge_base_id": "benchmark",
                        "source_path": f"synthetic/document-{number:06d}.txt",
                        "original_filename": f"document-{number:06d}.txt",
                        "file_hash": "synthetic", "file_size": 0, "status": "ready",
                    } for number in range(document_count)])
                    for start in range(0, chunk_count, 1000):
                        batch = []
                        for number in range(start, min(start + 1000, chunk_count)):
                            content = _content(number)
                            encoded = content.encode("utf-8")
                            corpus_hash.update(encoded)
                            character_count += len(content)
                            byte_count += len(encoded)
                            batch.append({
                                "id": f"benchmark-chunk-{number}", "knowledge_base_id": "benchmark",
                                "document_id": f"benchmark-doc-{number // 100}",
                                "file_hash": "synthetic", "chunk_index": number % 100,
                                "content": content, "content_hash": hashlib.sha256(encoded).hexdigest(),
                                "token_count": 0,
                            })
                        db.execute(insert(KnowledgeChunk), batch)
                    db.commit()
                seed_seconds = time.perf_counter() - seed_started

                # Separate dictionary initialization from corpus-size-sensitive
                # indexing. Use a temporary tokenizer cache, never a user cache.
                jieba.dt.tmp_dir = str(root)
                tokenizer_started = time.perf_counter()
                jieba.initialize()
                tokenizer_seconds = time.perf_counter() - tokenizer_started
                process = psutil.Process()
                baseline_rss = process.memory_info().rss
                peak_rss = [baseline_rss]
                stop = threading.Event()

                def sample_rss():
                    while not stop.wait(0.02):
                        peak_rss[0] = max(peak_rss[0], process.memory_info().rss)

                sampler = threading.Thread(target=sample_rss, daemon=True)
                sampler.start()
                try:
                    retriever = ChunkBM25Retriever("benchmark")
                    with Session(engine) as db:
                        started = time.perf_counter()
                        retriever.build(db)
                        index_seconds = time.perf_counter() - started
                    if retriever.chunk_count != chunk_count:
                        raise RuntimeError("BM25 index build did not load all synthetic chunks")
                    after_index_rss = process.memory_info().rss
                    # One unmeasured query warms the NumPy/scoring path.
                    retriever.get_relevant_documents("任务调度 retry processing", top_k=5)
                    rows = []
                    for number in range(query_count):
                        query, kind = _query(number, chunk_count)
                        started = time.perf_counter()
                        documents = retriever.get_relevant_documents(query, top_k=5)
                        rows.append({
                            "query": query, "kind": kind,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 6),
                            "returned_chunks": len(documents),
                        })
                    after_queries_rss = process.memory_info().rss
                finally:
                    stop.set()
                    sampler.join(timeout=2)
                latencies = [row["elapsed_ms"] for row in rows]
                peak = max(peak_rss[0], after_index_rss, after_queries_rss)
                return {
                    "schema_version": 1,
                    "benchmark": "local_bm25_only",
                    "run_at": datetime.now(timezone.utc).isoformat(),
                    "scope": "Sequential warm-tokenizer BM25L over SQLite chunks; excludes vector/graph retrieval, models, HTTP and concurrency.",
                    "limitations": [
                        "Public synthetic, repeated bilingual paragraphs; not representative business relevance evaluation.",
                        "RSS is an approximate whole-process measurement and varies with allocator, OS and caches.",
                        "One process and one knowledge base; these numbers are not a full-RAG capacity guarantee.",
                        "Imports, schema/data creation and tokenizer startup are excluded from index_seconds; one query warm-up is excluded from latency samples.",
                    ],
                    "environment": {
                        "python": platform.python_version(), "platform": platform.platform(),
                        "logical_cpu_count": psutil.cpu_count(),
                        "physical_cpu_count": psutil.cpu_count(logical=False),
                    },
                    "implementation_sha256": {
                        name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
                        for name in ("rag/retrievers/bm25_retriever.py", "app/repositories/knowledge_repository.py")
                    },
                    "dataset": {
                        "chunks": chunk_count, "documents": document_count,
                        "characters": character_count, "utf8_bytes": byte_count,
                        "average_characters_per_chunk": round(character_count / chunk_count, 2),
                        "average_bm25_terms_per_chunk": round(sum(map(len, retriever._tokenized)) / chunk_count, 2),
                        "sha256": corpus_hash.hexdigest(), "generator_version": 1,
                    },
                    "seed_seconds": round(seed_seconds, 6),
                    "tokenizer_initialization_seconds": round(tokenizer_seconds, 6),
                    "index_seconds": round(index_seconds, 6),
                    "queries": query_count,
                    "top_k": 5,
                    "query_latency_ms": {
                        "mean": round(statistics.mean(latencies), 6),
                        "p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95),
                        "max": max(latencies),
                    },
                    "memory_approximation": {
                        "method": "psutil RSS; 20ms samples plus stage snapshots",
                        "warm_tokenizer_rss_bytes": baseline_rss,
                        "after_index_rss_bytes": after_index_rss,
                        "after_queries_rss_bytes": after_queries_rss,
                        "index_rss_delta_bytes": max(0, after_index_rss - baseline_rss),
                        "peak_sampled_rss_bytes": peak,
                        "peak_rss_delta_bytes": max(0, peak - baseline_rss),
                    },
                    "per_query": rows,
                }
            finally:
                engine.dispose()
                # The module creates a lazy, unused application engine as well.
                from app.core.database import engine as module_engine

                module_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=_positive_int, default=1000)
    parser.add_argument("--queries", type=_positive_int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        output = args.output.resolve()
        if output.suffix.lower() != ".json" or output.is_relative_to(PROJECT_ROOT / "data"):
            parser.error("--output must be a .json file outside the application's data directory")
    result = _run(args.chunks, args.queries)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(json.dumps({
            "benchmark": result["benchmark"], "chunks": args.chunks, "queries": args.queries,
            "index_seconds": result["index_seconds"], "query_latency_ms": result["query_latency_ms"],
            "index_rss_delta_bytes": result["memory_approximation"]["index_rss_delta_bytes"],
            "output": str(output),
        }, ensure_ascii=False))
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
