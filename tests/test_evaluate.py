"""评估工具测试。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
            r"D:\MyCode\ragflow\data\docs\a\b.txt"
        )
        == "a/b.txt"
    )
    assert _normalize_source("alpha/docs/a.txt") == "alpha/docs/a.txt"
    assert compute_recall_at_k(["beta/docs/a.txt"], ["alpha/docs/a.txt"], 5) == 0


def test_load_questions_validates():
    questions = load_questions(Path("eval/questions.example.jsonl"))
    assert len(questions) == 6

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
    questions = load_questions(Path("eval/questions.example.jsonl"))
    h1 = question_set_hash(questions)
    h2 = question_set_hash(questions)
    assert h1 == h2
    assert len(h1) == 16


def test_failed_questions_reduce_reported_recall():
    from scripts.evaluate_rag import _summarize

    questions = [{"id": "ok"}, {"id": "failed"}]
    result = _summarize([
        {"id": "ok", "status": "ok", "recall@5": 1},
        {"id": "failed", "status": "error", "recall@5": 0},
    ], questions, "retrieval")
    assert result["summary"]["metrics"]["recall@5"] == 0.5
    assert result["summary"]["failed_count"] == 1


def test_citation_validity_checks_actual_source_ids():
    from scripts.evaluate_rag import citation_validity

    assert citation_validity("结论[来源1]，另一个结论[来源2]", [{"citation_id": "来源1"}]) == 0.5


def test_full_acceptance_rejects_wrong_uncited_answer():
    from scripts.evaluate_rag import evaluate_acceptance

    result = evaluate_acceptance({
        "failed_count": 0, "total_time": {"p95": 1},
        "metrics": {"recall@5": 1, "source_hit_rate": 1, "answer_keyword_recall": 0, "citation_validity": 0},
    }, {"recall@5": 0.8, "answer_keyword_recall": 0.8, "citation_validity": 0.95})
    assert result["passed"] is False
    assert result["failures"] == ["answer_keyword_recall", "citation_validity"]


def _evaluation_question(source="answer.txt"):
    return {
        "id": "q1", "split": "tuning", "category": "exact_term", "question": "检索答案",
        "ground_truth": "答案", "answer_keywords": ["答案"],
        "relevant_sources": [{"source": source}],
        "retrieval": {"channels": ["text"]},
    }


@pytest.mark.parametrize("mode", ["retrieval", "full"])
def test_evaluation_keeps_knowledge_base_selection_isolated(monkeypatch, mode):
    from app.services import rag_service
    from scripts import evaluate_rag

    calls = []

    def prepare(query, history, *, retrieval_options=None, knowledge_base_id=None):
        calls.append((knowledge_base_id, retrieval_options))
        source = f"{knowledge_base_id}/answer.txt"
        return {
            "docs": [SimpleNamespace(metadata={"source": source})],
            "sources": [{"source": source, "citation_id": "来源1"}],
            "context": f"[来源1] {knowledge_base_id} 的答案",
            "route": {"evidence_status": "sufficient", "knowledge_base_id": knowledge_base_id},
        }

    monkeypatch.setattr(rag_service, "prepare_context", prepare)
    monkeypatch.setattr(rag_service, "generate", lambda *args, **kwargs: "答案[来源1]")
    evaluate = getattr(evaluate_rag, f"run_{mode}_eval")
    for base in ("kb-a", "kb-b"):
        question = _evaluation_question(f"{base}/answer.txt")
        result = evaluate([question], knowledge_base_id=base)
        record = result["per_question"][0]
        assert result["knowledge_base_id"] == base
        assert record["status"] == "ok"
        assert record["retrieved_sources"] == [f"{base}/answer.txt"]
        assert record["route"]["knowledge_base_id"] == base
        assert record["recall@5"] == 1
    assert calls == [("kb-a", {"channels": ["text"]}), ("kb-b", {"channels": ["text"]})]


@pytest.mark.parametrize("mode", ["retrieval", "full"])
def test_evaluation_preserves_default_and_positional_limit(monkeypatch, mode):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID
    from app.services import rag_service
    from scripts import evaluate_rag

    calls = []

    def prepare(query, history, **kwargs):
        calls.append(kwargs["knowledge_base_id"])
        return {"docs": [], "sources": [], "context": "", "route": {}}

    monkeypatch.setattr(rag_service, "prepare_context", prepare)
    result = getattr(evaluate_rag, f"run_{mode}_eval")([_evaluation_question()] * 2, 1)
    assert result["summary"]["total_questions"] == 1
    assert result["knowledge_base_id"] == DEFAULT_KNOWLEDGE_BASE_ID
    assert calls == [DEFAULT_KNOWLEDGE_BASE_ID]


def test_full_evaluation_empty_evidence_refuses_without_model_call(monkeypatch):
    from app.services import rag_service
    from rag.answer_validation import NO_EVIDENCE_ANSWER
    from scripts.evaluate_rag import run_full_eval

    monkeypatch.setattr(rag_service, "prepare_context", lambda *args, **kwargs: {
        "context": "", "sources": [], "route": {"evidence_status": "insufficient"},
    })

    def no_model(*args, **kwargs):
        pytest.fail("Empty evidence must not create a runtime or call a model")

    monkeypatch.setattr(rag_service, "_active_runtime_for_knowledge_base", no_model)
    question = {**_evaluation_question(), "unanswerable": True, "relevant_sources": []}
    record = run_full_eval([question], knowledge_base_id="empty-kb")["per_question"][0]
    assert record["status"] == "ok"
    assert record["answer_preview"] == NO_EVIDENCE_ANSWER
    assert record["refusal_phrase_detected"] is True
    assert record["route"]["citation_validation"]["support_status"] == "no_evidence"


def test_full_evaluation_retains_generation_citation_diagnostics(monkeypatch):
    from app.services import rag_service
    from scripts.evaluate_rag import run_full_eval

    sources = [{"source": "answer.txt", "citation_id": "来源1"}]
    monkeypatch.setattr(rag_service, "prepare_context", lambda *args, **kwargs: {
        "context": "[来源1] 答案", "sources": sources, "route": {"evidence_status": "sufficient"},
    })
    model_calls = []

    def generate(prompt):
        model_calls.append(prompt)
        return "答案[来源1]，错误引用[来源9]"

    runtime = SimpleNamespace(llm=SimpleNamespace(generate=generate))
    monkeypatch.setattr(rag_service, "_active_runtime_for_knowledge_base", lambda *_: runtime)
    record = run_full_eval([_evaluation_question()])["per_question"][0]
    assert record["status"] == "ok"
    assert len(model_calls) == 1
    assert "[来源9]" not in record["answer_preview"]
    assert record["route"]["citation_validation"]["status"] == "invalid"
    assert record["route"]["citation_validation"]["invalid_ids"] == ["来源9"]
    assert record["citation_validity"] == 0
    from scripts.evaluate_rag import _summarize, evaluate_acceptance

    summary = _summarize([record], [_evaluation_question()], "full")["summary"]
    assert evaluate_acceptance(summary, {"citation_validity": 0.95})["failures"] == ["citation_validity"]


@pytest.mark.parametrize("mode", ["retrieval", "full"])
def test_evaluation_cli_forwards_knowledge_base(monkeypatch, tmp_path, mode):
    from scripts import evaluate_rag

    question = _evaluation_question()
    questions_path = tmp_path / "questions.jsonl"
    questions_path.write_text(json.dumps(question, ensure_ascii=False) + "\n", encoding="utf-8")
    observed = []

    def evaluate(questions, *, knowledge_base_id):
        observed.append(knowledge_base_id)
        result = evaluate_rag._summarize([{
            "id": question["id"], "status": "ok", "recall@5": 1, "source_hit": 1,
            "answer_keyword_recall": 1, "citation_validity": 1,
        }], questions, mode)
        result["knowledge_base_id"] = knowledge_base_id
        return result

    saved = []
    monkeypatch.setattr(evaluate_rag, f"run_{mode}_eval", evaluate)
    monkeypatch.setattr(evaluate_rag, "save_result", lambda result, *args: saved.append(result))
    monkeypatch.setattr("sys.argv", [
        "evaluate_rag", "--mode", mode, "--questions", str(questions_path),
        "--knowledge-base", "server-kb", "--allow-unannotated-facts",
    ])
    assert evaluate_rag.main() == 0
    assert observed == ["server-kb"]
    assert saved[0]["knowledge_base_id"] == "server-kb"
