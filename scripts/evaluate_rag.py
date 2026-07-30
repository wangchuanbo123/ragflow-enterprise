"""
确定性 RAG 评估脚本

支持模式：
  --mode retrieval  只测检索、重排和来源命中，不调用生成 LLM
  --mode full       运行完整回答，测答案、引用、拒答和耗时
  --compare         对比两个结果文件的指标差异

用法示例：
  python -m scripts.evaluate_rag --mode retrieval --split tuning --label baseline
  python -m scripts.evaluate_rag --mode full --split tuning --label baseline
  python -m scripts.evaluate_rag --mode full --split holdout --label final
  python -m scripts.evaluate_rag --compare eval/baselines/current_baseline.json eval/results/final.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = PROJECT_ROOT / "eval"
QUESTIONS_PATH = EVAL_DIR / "data" / "questions.jsonl"
BASELINES_DIR = EVAL_DIR / "baselines"
RESULTS_DIR = EVAL_DIR / "results"


# ---------------------------------------------------------------------------
# 评估集加载与校验
# ---------------------------------------------------------------------------

def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    questions: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_num} 行解析失败: {exc}") from exc
            _validate_question(q, line_num)
            questions.append(q)
    return questions


def _validate_question(q: dict, line_num: int) -> None:
    qid = q.get("id", f"line_{line_num}")
    for field in ("id", "split", "category", "question"):
        if field not in q:
            raise ValueError(f"题目 {qid} 缺少字段: {field}")
    if not q.get("unanswerable"):
        if not q.get("ground_truth"):
            raise ValueError(f"题目 {qid} 的 ground_truth 不能为空（非 unanswerable）")
        if not q.get("answer_keywords"):
            raise ValueError(f"题目 {qid} 的 answer_keywords 不能为空（非 unanswerable）")
    else:
        if q.get("relevant_sources"):
            raise ValueError(f"题目 {qid} 标记为 unanswerable 但 relevant_sources 非空")


def filter_by_split(questions: list[dict], split: str) -> list[dict]:
    return [q for q in questions if q["split"] == split]


def question_set_hash(questions: list[dict]) -> str:
    raw = json.dumps(questions, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 环境信息收集
# ---------------------------------------------------------------------------

def _git_info() -> dict:
    info: dict[str, Any] = {}
    for key, args in [("commit", ["rev-parse", "HEAD"]), ("dirty", ["status", "--porcelain"])]:
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True, text=True, timeout=5,
                cwd=str(PROJECT_ROOT),
            )
            if key == "dirty":
                info["git_dirty"] = bool(result.stdout.strip())
            else:
                info[f"git_{key}"] = result.stdout.strip()[:12]
        except Exception:
            info[f"git_{key}"] = "unknown"
    return info


def _provider_info() -> dict:
    from app.core import config as cfg
    return {
        "llm_provider": cfg.LLM_PROVIDER,
        "llm_model": cfg.LLM_MODEL,
        "embedding_provider": cfg.EMBEDDING_PROVIDER,
        "embedding_model": cfg.EMBEDDING_MODEL,
        "reranker_provider": cfg.RERANKER_PROVIDER,
        "reranker_model": cfg.RERANKER_MODEL,
    }


def _retrieval_config() -> dict:
    from app.core import config as cfg
    return {
        "vector_search_k": getattr(cfg, "VECTOR_SEARCH_K", 6),
        "bm25_search_k": getattr(cfg, "BM25_SEARCH_K", 6),
        "hybrid_vector_weight": getattr(cfg, "HYBRID_VECTOR_WEIGHT", 0.7),
        "rerank_top_k": getattr(cfg, "RERANK_TOP_K", 5),
    }


def _chunk_config() -> dict:
    from app.core import config as cfg
    return {
        "chunk_strategy": getattr(cfg, "CHUNK_STRATEGY", "legacy"),
        "chunk_size": getattr(cfg, "CHUNK_SIZE", 300),
        "chunk_overlap": getattr(cfg, "CHUNK_OVERLAP", 50),
    }


def _corpus_fingerprint() -> str:
    from pathlib import Path
    from rag.loaders.document_loader import is_supported_document, load_document
    from rag.utils.file_hash import file_hash

    doc_dir = Path(getattr(__import__("app.core.config", fromlist=["DOC_DIR"]), "DOC_DIR"))
    files = sorted([p for p in doc_dir.rglob("*") if is_supported_document(p)])
    hasher = hashlib.sha256()
    for f in files:
        h = file_hash(f)
        rel = f.relative_to(doc_dir).as_posix()
        hasher.update(f"{rel}:{h}".encode())
    return hasher.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 检索指标计算（确定性）
# ---------------------------------------------------------------------------

def _source_key(source: str) -> str:
    return Path(source).as_posix()


def _normalize_source(source: str | None) -> str:
    if not source:
        return ""
    raw = str(source).strip().replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    lowered = [part.casefold() for part in parts]

    for index in range(len(parts) - 1):
        if lowered[index:index + 2] == ["data", "docs"]:
            return "/".join(parts[index + 2:])

    if "docs" in lowered:
        docs_index = lowered.index("docs")
        return "/".join(parts[docs_index + 1:])

    return "/".join(parts)


def compute_recall_at_k(retrieved_sources: list[str], relevant_sources: list[str], k: int) -> float:
    if not relevant_sources:
        return 0.0
    top_k = retrieved_sources[:k]
    relevant_set = {_normalize_source(s) for s in relevant_sources}
    retrieved_set = {_normalize_source(s) for s in top_k}
    hits = len(relevant_set & retrieved_set)
    return hits / len(relevant_set)


def compute_mrr_at_k(retrieved_sources: list[str], relevant_sources: list[str], k: int) -> float:
    if not relevant_sources:
        return 0.0
    relevant_set = {_normalize_source(s) for s in relevant_sources}
    for i, src in enumerate(retrieved_sources[:k]):
        if _normalize_source(src) in relevant_set:
            return 1.0 / (i + 1)
    return 0.0


def compute_ndcg_at_k(retrieved_sources: list[str], relevant_sources: list[str], k: int) -> float:
    if not relevant_sources:
        return 0.0
    relevant_set = {_normalize_source(s) for s in relevant_sources}
    dcg = 0.0
    for i, src in enumerate(retrieved_sources[:k]):
        if _normalize_source(src) in relevant_set:
            dcg += 1.0 / math.log2(i + 2)
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(j + 2) for j in range(ideal_hits))
    return dcg / idcg if idcg > 0 else 0.0


def compute_source_hit_rate(retrieved_sources: list[str], relevant_sources: list[str]) -> float:
    if not relevant_sources:
        return 0.0
    relevant_set = {_normalize_source(s) for s in relevant_sources}
    retrieved_set = {_normalize_source(s) for s in retrieved_sources}
    return 1.0 if (relevant_set & retrieved_set) else 0.0


def compute_answer_keyword_recall(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    answer_lower = answer.lower()
    hits = sum(1 for kw in keywords if kw.lower() in answer_lower)
    return hits / len(keywords)


# ---------------------------------------------------------------------------
# 评估执行
# ---------------------------------------------------------------------------

def _extract_sources_from_docs(docs: list) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        src = None
        if hasattr(doc, "metadata"):
            src = doc.metadata.get("source")
        elif isinstance(doc, dict):
            src = doc.get("source")
        norm = _normalize_source(src) if src else "unknown"
        if norm not in seen:
            seen.add(norm)
            sources.append(src or "unknown")
    return sources


def _extract_sources_from_result(sources_list: list[dict]) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for s in sources_list:
        src = s.get("source", "unknown")
        norm = _normalize_source(src)
        if norm not in seen:
            seen.add(norm)
            sources.append(src)
    return sources


def run_retrieval_eval(questions: list[dict], limit: int | None = None) -> dict:
    from app.services.rag_service import active_runtime
    from rag.nodes.retrieve_node import create_retrieve_node
    from rag.nodes.rerank_node import create_rerank_node
    from rag.nodes.rewrite_node import create_rewrite_node

    rt = active_runtime()
    rewrite_fn = create_rewrite_node(rt.llm)
    retrieve_fn = create_retrieve_node(rt.retriever)
    rerank_fn = create_rerank_node(rt.reranker)

    results: list[dict] = []
    qs = questions[:limit] if limit else questions

    for q in qs:
        t_start = time.perf_counter()
        record: dict[str, Any] = {"id": q["id"], "category": q["category"], "question": q["question"]}

        try:
            state: dict[str, Any] = {"original_query": q["question"], "history": []}

            t0 = time.perf_counter()
            state.update(rewrite_fn(state))
            t_rewrite = time.perf_counter() - t0

            t0 = time.perf_counter()
            state.update(retrieve_fn(state))
            t_retrieve = time.perf_counter() - t0

            t0 = time.perf_counter()
            state.update(rerank_fn(state))
            t_rerank = time.perf_counter() - t0

            retrieved_sources = _extract_sources_from_docs(state.get("docs", []))
            relevant = [s["source"] for s in q.get("relevant_sources", [])]

            record.update({
                "retrieved_sources": retrieved_sources[:10],
                "recall@5": compute_recall_at_k(retrieved_sources, relevant, 5),
                "recall@10": compute_recall_at_k(retrieved_sources, relevant, 10),
                "mrr@10": compute_mrr_at_k(retrieved_sources, relevant, 10),
                "ndcg@10": compute_ndcg_at_k(retrieved_sources, relevant, 10),
                "source_hit": compute_source_hit_rate(retrieved_sources, relevant),
                "timings": {"rewrite": t_rewrite, "retrieve": t_retrieve, "rerank": t_rerank},
                "status": "ok",
            })
        except Exception as exc:
            record.update({"status": "error", "error": str(exc)[:500], "recall@5": 0, "recall@10": 0,
                           "mrr@10": 0, "ndcg@10": 0, "source_hit": 0, "timings": {}})

        record["total_time"] = time.perf_counter() - t_start
        results.append(record)

    return _summarize(results, questions, "retrieval")


def run_full_eval(questions: list[dict], limit: int | None = None) -> dict:
    from app.services.rag_service import prepare_context, generate

    results: list[dict] = []
    qs = questions[:limit] if limit else questions

    for q in qs:
        t_start = time.perf_counter()
        record: dict[str, Any] = {"id": q["id"], "category": q["category"], "question": q["question"]}

        try:
            history: list[dict] = []
            t0 = time.perf_counter()
            prepared = prepare_context(q["question"], history)
            t_prepare = time.perf_counter() - t0

            retrieved_sources = _extract_sources_from_result(prepared.get("sources", []))
            relevant = [s["source"] for s in q.get("relevant_sources", [])]

            record.update({
                "recall@5": compute_recall_at_k(retrieved_sources, relevant, 5),
                "recall@10": compute_recall_at_k(retrieved_sources, relevant, 10),
                "mrr@10": compute_mrr_at_k(retrieved_sources, relevant, 10),
                "ndcg@10": compute_ndcg_at_k(retrieved_sources, relevant, 10),
                "source_hit": compute_source_hit_rate(retrieved_sources, relevant),
            })

            if q.get("unanswerable"):
                record["expected_unanswerable"] = True

            t0 = time.perf_counter()
            answer = generate(q["question"], prepared["context"], history)
            t_generate = time.perf_counter() - t0

            keywords = q.get("answer_keywords", [])
            record.update({
                "answer_preview": answer[:200],
                "answer_keyword_recall": compute_answer_keyword_recall(answer, keywords),
                "timings": {"prepare": t_prepare, "generate": t_generate},
                "status": "ok",
            })
        except Exception as exc:
            record.update({"status": "error", "error": str(exc)[:500], "recall@5": 0, "recall@10": 0,
                           "mrr@10": 0, "ndcg@10": 0, "source_hit": 0, "answer_keyword_recall": 0,
                           "timings": {}})

        record["total_time"] = time.perf_counter() - t_start
        results.append(record)

    return _summarize(results, questions, "full")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, min(len(sorted_vals) - 1, int(math.ceil(p / 100 * len(sorted_vals))) - 1))
    return sorted_vals[idx]


def _summarize(results: list[dict], questions: list[dict], mode: str) -> dict:
    answerable = [r for r in results if not questions[[q["id"] for q in questions].index(r["id"])].get("unanswerable")]
    unanswerable = [r for r in results if questions[[q["id"] for q in questions].index(r["id"])].get("unanswerable")]

    def avg(key: str, subset=None) -> float:
        pool = subset if subset is not None else answerable
        vals = [r.get(key, 0) for r in pool if r.get("status") != "error"]
        return statistics.mean(vals) if vals else 0.0

    timing_keys: set[str] = set()
    for r in results:
        timing_keys.update(r.get("timings", {}).keys())

    stage_stats: dict[str, dict] = {}
    for stage in sorted(timing_keys):
        vals = [r["timings"].get(stage, 0) for r in results if r.get("timings")]
        stage_stats[stage] = {
            "avg": statistics.mean(vals) if vals else 0,
            "p50": _percentile(vals, 50),
            "p95": _percentile(vals, 95),
        }

    total_times = [r.get("total_time", 0) for r in results]
    failed = [r for r in results if r.get("status") == "error"]

    summary: dict[str, Any] = {
        "mode": mode,
        "total_questions": len(results),
        "failed_count": len(failed),
        "metrics": {
            "recall@5": avg("recall@5"),
            "recall@10": avg("recall@10"),
            "mrr@10": avg("mrr@10"),
            "ndcg@10": avg("ndcg@10"),
            "source_hit_rate": avg("source_hit"),
        },
        "timings": stage_stats,
        "total_time": {
            "p50": _percentile(total_times, 50),
            "p95": _percentile(total_times, 95),
            "avg": statistics.mean(total_times) if total_times else 0,
        },
    }

    if mode == "full":
        summary["metrics"]["answer_keyword_recall"] = avg("answer_keyword_recall")

    if unanswerable:
        wrong_hits = [r for r in unanswerable if r.get("source_hit", 0) > 0 and r.get("status") != "error"]
        summary["metrics"]["unanswerable_wrong_hit_rate"] = len(wrong_hits) / len(unanswerable) if unanswerable else 0
        summary["metrics"]["unanswerable_count"] = len(unanswerable)

    return {"per_question": results, "summary": summary}


# ---------------------------------------------------------------------------
# 结果保存与对比
# ---------------------------------------------------------------------------

def save_result(result: dict, label: str, split: str, output: str | None = None) -> Path:
    meta = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "split": split,
        "question_set_hash": question_set_hash(load_questions()),
        "python_version": platform.python_version(),
        "provider_info": _safe_call(_provider_info, {}),
        "retrieval_config": _safe_call(_retrieval_config, {}),
        "chunk_config": _safe_call(_chunk_config, {}),
        "corpus_fingerprint": _safe_call(_corpus_fingerprint, "unknown"),
        "git_info": _safe_call(_git_info, {}),
    }
    result["meta"] = meta

    if output:
        out_path = Path(output)
    else:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR / f"{split}_{label}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {out_path}")
    return out_path


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


def compare_results(baseline_path: str, final_path: str) -> None:
    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    with open(final_path, "r", encoding="utf-8") as f:
        final = json.load(f)

    b_metrics = baseline.get("summary", {}).get("metrics", {})
    f_metrics = final.get("summary", {}).get("metrics", {})

    print("\n" + "=" * 60)
    print("评估结果对比")
    print("=" * 60)
    print(f"{'指标':<30} {'Baseline':>12} {'Final':>12} {'变化':>12} {'百分比':>10}")
    print("-" * 80)

    all_keys = sorted(set(list(b_metrics.keys()) + list(f_metrics.keys())))
    for key in all_keys:
        b_val = b_metrics.get(key, 0)
        f_val = f_metrics.get(key, 0)
        diff = f_val - b_val
        pct = f"{(diff / b_val * 100):+.1f}%" if b_val != 0 else "N/A"
        print(f"{key:<30} {b_val:>12.4f} {f_val:>12.4f} {diff:>+12.4f} {pct:>10}")

    b_timing = baseline.get("summary", {}).get("total_time", {})
    f_timing = final.get("summary", {}).get("total_time", {})
    print(f"\n{'总耗时 p50 (s)':<30} {b_timing.get('p50', 0):>12.3f} {f_timing.get('p50', 0):>12.3f}")
    print(f"{'总耗时 p95 (s)':<30} {b_timing.get('p95', 0):>12.3f} {f_timing.get('p95', 0):>12.3f}")

    print(f"\nBaseline 失败数: {baseline.get('summary', {}).get('failed_count', '?')}")
    print(f"Final 失败数:    {final.get('summary', {}).get('failed_count', '?')}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RAG 评估工具")
    parser.add_argument("--mode", choices=["retrieval", "full"], default="retrieval")
    parser.add_argument("--split", choices=["tuning", "holdout", "all"], default="tuning")
    parser.add_argument("--label", default="run")
    parser.add_argument("--limit", type=int, default=None, help="快速冒烟测试")
    parser.add_argument("--output", default=None, help="自定义输出路径")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "FINAL"), default=None)
    args = parser.parse_args()

    if args.compare:
        compare_results(args.compare[0], args.compare[1])
        return

    questions = load_questions()
    if args.split == "all":
        qs = questions
    else:
        qs = filter_by_split(questions, args.split)

    print(f"\n评估模式: {args.mode} | split: {args.split} | 题目数: {len(qs)}")
    if args.limit:
        qs = qs[:args.limit]
        print(f"限制为前 {args.limit} 题")

    if args.mode == "retrieval":
        result = run_retrieval_eval(qs)
    else:
        result = run_full_eval(qs)

    summary = result["summary"]
    print(f"\n{'=' * 50}")
    print(f"评估完成 | 失败: {summary['failed_count']}/{summary['total_questions']}")
    print(f"{'=' * 50}")
    for k, v in summary["metrics"].items():
        print(f"  {k:<30} {v:.4f}")
    print(f"\n总耗时 p50: {summary['total_time']['p50']:.3f}s | p95: {summary['total_time']['p95']:.3f}s")

    save_result(result, args.label, args.split, args.output)


if __name__ == "__main__":
    main()
