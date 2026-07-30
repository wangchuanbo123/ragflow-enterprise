"""评估工具测试。"""

import json
from pathlib import Path

from scripts.evaluate_rag import (
    compute_mrr_at_k,
    compute_ndcg_at_k,
    compute_recall_at_k,
    compute_source_hit_rate,
    compute_answer_keyword_recall,
    load_questions,
    question_set_hash,
    _normalize_source,
)


def test_recall_at_k():
    assert compute_recall_at_k(["a.txt", "b.txt"], ["a.txt"], 5) == 1.0
    assert compute_recall_at_k(["b.txt", "c.txt"], ["a.txt"], 5) == 0.0
    assert compute_recall_at_k(["b.txt", "a.txt"], ["a.txt"], 1) == 0.0


def test_mrr_at_k():
    assert compute_mrr_at_k(["a.txt", "b.txt"], ["a.txt"], 10) == 1.0
    assert compute_mrr_at_k(["b.txt", "a.txt"], ["a.txt"], 10) == 0.5
    assert compute_mrr_at_k(["b.txt", "c.txt"], ["a.txt"], 10) == 0.0


def test_ndcg_at_k():
    assert compute_ndcg_at_k(["a.txt", "b.txt"], ["a.txt"], 10) == 1.0
    assert 0 < compute_ndcg_at_k(["b.txt", "a.txt"], ["a.txt"], 10) < 1.0


def test_source_hit_rate():
    assert compute_source_hit_rate(["a.txt"], ["a.txt"]) == 1.0
    assert compute_source_hit_rate(["b.txt"], ["a.txt"]) == 0.0


def test_answer_keyword_recall():
    assert compute_answer_keyword_recall("这是关于飞机的回答", ["飞机"]) == 1.0
    assert compute_answer_keyword_recall("无关文本", ["飞机"]) == 0.0


def test_normalize_source():
    assert _normalize_source("data/docs/a/b.txt") == "a/b.txt"
    assert _normalize_source("docs/a.txt") == "a.txt"
    assert _normalize_source("a.txt") == "a.txt"
    assert (
        _normalize_source(
            r"D:\MyCode\ragflow-enterprise\data\docs\a\b.txt"
        )
        == "a/b.txt"
    )


def test_load_questions_validates():
    questions = load_questions()
    assert len(questions) >= 24

    # Check categories
    categories = set(q["category"] for q in questions)
    assert "exact_term" in categories
    assert "semantic" in categories
    assert "unanswerable" in categories

    # Check splits
    splits = set(q["split"] for q in questions)
    assert "tuning" in splits
    assert "holdout" in splits

    # Unanswerable questions have empty relevant_sources
    for q in questions:
        if q.get("unanswerable"):
            assert q["relevant_sources"] == []
        else:
            assert q["ground_truth"]
            assert q["answer_keywords"]


def test_question_set_hash_stable():
    questions = load_questions()
    h1 = question_set_hash(questions)
    h2 = question_set_hash(questions)
    assert h1 == h2
    assert len(h1) == 16
