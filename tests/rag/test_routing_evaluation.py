import json
from pathlib import Path

from scripts.evaluate_routing import evaluate_cases, load_cases


def test_rule_router_passes_versioned_golden_cases():
    cases = load_cases(Path("eval/data/routing_cases.jsonl"))
    result = evaluate_cases(cases)

    assert result["case_count"] >= 10
    assert result["exact_match"] == 1.0, json.dumps(
        [row for row in result["rows"] if not row["pass"]],
        ensure_ascii=False,
    )
    assert result["unavailable_exact_match"] == 1.0
