"""Optional cost-bearing evaluation for the configured retrieval router model."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _channels(raw: str) -> set[str]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    payload = json.loads(fenced.group(1) if fenced else raw)
    return {item for item in payload.get("channels", []) if item in {"text", "graph", "code"}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path("eval/routing_model_cases.example.jsonl"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-exact-match", type=float, default=0.9)
    args = parser.parse_args()
    from app.bootstrap.container import get_provider_container

    router = get_provider_container().router_llm
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for case in cases:
        prompt = (
            "你是 RAG 检索路由器。仅输出 JSON，格式为 "
            '{"channels":["text","graph","code"]}。普通文档选择 text；'
            "实体关系、多跳路径选择 graph；代码符号、实现、调用选择 code；可以多选。\n"
            f"问题：{case['query']}"
        )
        try:
            actual = _channels(router.generate(prompt))
            error = None
        except Exception as exc:
            actual, error = set(), f"{type(exc).__name__}: {exc}"
        expected = set(case["expected"])
        rows.append({
            "id": case["id"], "query": case["query"],
            "expected": sorted(expected), "actual": sorted(actual),
            "pass": actual == expected, "error": error,
        })
    exact = sum(row["pass"] for row in rows) / max(len(rows), 1)
    result = {
        "router": router.identity().as_dict(), "case_count": len(rows),
        "exact_match": exact, "rows": rows,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if exact >= args.min_exact_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
