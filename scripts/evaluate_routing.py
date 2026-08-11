"""Deterministic retrieval-router evaluation; never calls an LLM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag.routing import route_query


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def evaluate_cases(cases: list[dict]) -> dict:
    rows = []
    exact = 0
    unavailable_exact = 0
    true_positive = false_positive = false_negative = 0
    for case in cases:
        decision = route_query(
            case["query"],
            explicit_channels=case.get("explicit"),
            graph_available=case.get("graph_available", False),
            code_available=case.get("code_available", False),
        )
        actual = set(decision["selected_channels"])
        expected = set(case["expected"])
        actual_unavailable = set(decision["unavailable_channels"])
        expected_unavailable = set(case.get("expected_unavailable", []))
        is_exact = actual == expected
        unavailable_match = actual_unavailable == expected_unavailable
        exact += int(is_exact)
        unavailable_exact += int(unavailable_match)
        true_positive += len(actual & expected)
        false_positive += len(actual - expected)
        false_negative += len(expected - actual)
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "expected": sorted(expected),
            "actual": decision["selected_channels"],
            "expected_unavailable": sorted(expected_unavailable),
            "actual_unavailable": decision["unavailable_channels"],
            "pass": is_exact and unavailable_match,
            "reasons": decision["reasons"],
        })
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    return {
        "case_count": len(cases),
        "exact_match": exact / max(len(cases), 1),
        "unavailable_exact_match": unavailable_exact / max(len(cases), 1),
        "channel_precision": precision,
        "channel_recall": recall,
        "passed": sum(1 for row in rows if row["pass"]),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval/data/routing_cases.jsonl"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-exact-match", type=float, default=1.0)
    args = parser.parse_args()
    result = evaluate_cases(load_cases(args.cases))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["exact_match"] >= args.min_exact_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
