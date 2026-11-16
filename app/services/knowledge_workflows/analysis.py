"""Deterministic graph artifacts and bounded model/prompt comparison experiments."""

import re
import time
import hashlib
from collections import defaultdict, deque
from datetime import date
from sqlalchemy import select
from app.models.knowledge_chunk import KnowledgeChunk
from app.schemas.agent_run import WorkflowEvidence, GeneratedEvaluationCase
from pydantic import BaseModel, Field


class ComparisonAnswer(BaseModel):
    answer: str
    evidence: list[WorkflowEvidence] = Field(default_factory=list, max_length=50)


def validate_generated_cases(db, run, items):
    if not 1 <= len(items) <= run.options_json.get("max_generated_cases", 5):
        raise ValueError("生成样例数量超出本次范围")
    result = []
    seen = set()
    for item in items:
        parsed = GeneratedEvaluationCase.model_validate(item)
        if parsed.question in seen:
            raise ValueError("生成样例包含重复问题")
        seen.add(parsed.question)
        chunks = {chunk.id: chunk for chunk in db.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
            KnowledgeChunk.document_id.in_(run.document_ids_json), KnowledgeChunk.id.in_(parsed.expected_chunk_ids)))}
        if set(chunks) != set(parsed.expected_chunk_ids) or {chunk.document_id for chunk in chunks.values()} != set(parsed.expected_document_ids):
            raise ValueError("生成样例的期望文档或片段超出范围")
        cited = set()
        for evidence in parsed.evidence:
            chunk = chunks.get(evidence.chunk_id)
            if chunk is None or evidence.quote not in chunk.content or (evidence.document_id and evidence.document_id != chunk.document_id):
                raise ValueError("生成样例的引文不能通过原文核验")
            evidence.document_id = chunk.document_id
            cited.add(chunk.id)
        if cited != set(chunks):
            raise ValueError("生成样例须为每个期望片段提供逐字证据")
        data = parsed.model_dump()
        identifier = hashlib.sha256((run.id + json_stable(data)).encode()).hexdigest()[:24]
        result.append({**data, "id": "case-" + identifier, "provenance": "agent_generated", "review_status": "candidate",
            "human_review_required": True, "source_run_id": run.id, "validation": "scope_and_exact_quotes_only"})
    return result


def json_stable(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def graph_artifact(db, run):
    from app.bootstrap.container import get_provider_container
    store = get_provider_container().graph_store(db, run.knowledge_base_id)
    nodes, edges = {}, {}
    for document_id in run.document_ids_json[:100]:
        graph = store.get_document_subgraph(document_id, limit=100)
        nodes.update({node["id"]: node for node in graph.get("nodes", [])})
        for edge in graph.get("edges", []):
            item = edges.setdefault(edge["id"], {**edge, "evidence": []})
            for evidence in edge.get("evidence", []):
                if evidence.get("document_id", document_id) in run.document_ids_json:
                    item["evidence"].append(evidence)
        if len(edges) >= 300:
            break
    adjacency = defaultdict(list)
    for edge in list(edges.values())[:300]:
        adjacency[edge["source"]].append((edge["target"], edge["id"]))
    paths = []
    cycles = []
    for seed in list(nodes)[:30]:
        queue = deque([(seed, [seed], [])])
        visited = {seed}
        while queue and len(paths) < 100:
            node, node_path, relation_path = queue.popleft()
            if len(relation_path) >= 3:
                continue
            for target, relation_id in adjacency[node][:20]:
                if target == seed:
                    cycles.append({"entity_ids": node_path + [target], "relation_ids": relation_path + [relation_id]})
                if target in visited:
                    continue
                visited.add(target)
                path = {"entity_ids": node_path + [target], "relation_ids": relation_path + [relation_id],
                        "evidence": [evidence for identifier in relation_path + [relation_id] for evidence in edges[identifier]["evidence"][:3]]}
                paths.append(path)
                queue.append((target, path["entity_ids"], path["relation_ids"]))
    timeline = []
    chunks = list(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
                                KnowledgeChunk.document_id.in_(run.document_ids_json)).limit(300)))
    for chunk in chunks:
        for match in re.finditer(r"(?<!\d)(?P<year>(?:19|20)\d{2})[-/年](?P<month>1[0-2]|0?[1-9])(?:(?:[-/月])(?P<day>3[01]|[12]\d|0?[1-9])日?|月)?(?!\d)(?![-/月]\d)", chunk.content):
            try:
                parsed = date(int(match["year"]), int(match["month"]), int(match["day"] or "1"))
            except ValueError:
                continue
            timeline.append({"date_text": match.group(), "sort_key": parsed.isoformat(),
                             "normalized_date": parsed.isoformat() if match["day"] else parsed.strftime("%Y-%m"),
                             "precision": "day" if match["day"] else "month",
                             "document_id": chunk.document_id, "chunk_id": chunk.id,
                             "quote": chunk.content[max(0, match.start()-50):match.end()+100]})
            if len(timeline) >= 100:
                break
    return {"kind": "evidence_backed_business_graph", "nodes": list(nodes.values())[:300], "dependencies": list(edges.values())[:300],
            "impact_paths": paths, "cycles": cycles[:30], "timeline": sorted(timeline[:100], key=lambda row: row["sort_key"]),
            "limits": {"documents": 100, "edges": 300, "path_hops": 3, "timeline_chunks": 300},
            "interpretation": "路径由已生效图谱确定；日期只摘录原文，不推断业务事件时间。关系类型含义由原关系标签说明。"}


def compare_answers(db, run, cancelled, on_event=None, before_request=None, token_factory=None):
    from app.agents.contracts import AgentRequest
    from app.agents.factory import get_agent_backend
    from app.services import rag_service
    from app.services.knowledge_workflows.tools import create_tool_token
    profiles = run.options_json.get("comparison_profiles") or [run.options_json.get("runtime_profile") or "default"]
    variants = run.options_json.get("prompt_variants") or [{"name": "baseline", "instructions": "仅根据证据回答，并在每个事实句后使用[chunk_id]引用。"}]
    cases = list(run.options_json.get("gold_cases") or [])
    cases.extend({"question": question} for question in run.options_json.get("questions") or [])
    rows = []
    for case in cases:
        if cancelled():
            break
        retrieved = rag_service.inspect_retrieval(case["question"], knowledge_base_id=run.knowledge_base_id,
            retrieval_options={"scope": {"knowledge_base_ids": [run.knowledge_base_id], "document_ids": run.document_ids_json}})
        identifiers = [source.get("chunk_id") for source in retrieved.get("sources", [])]
        chunks = list(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(identifiers),
                            KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
                            KnowledgeChunk.document_id.in_(run.document_ids_json))))
        for profile in profiles:
            for variant in variants:
                if len(rows) >= 20 or cancelled():
                    break
                started = time.perf_counter()
                row = {"question": case["question"], "profile": profile, "prompt_variant": variant["name"],
                       "case_provenance": case.get("provenance", "unlabeled_query"), "source_run_id": case.get("source_run_id")}
                try:
                    if before_request:
                        before_request()
                    response = get_agent_backend(profile_name=profile).run(AgentRequest(task="evaluation",
                        knowledge_base_id=run.knowledge_base_id, instructions=variant["instructions"],
                        context={"mode": "answer_comparison", "question": case["question"], "chunks": [
                            {"id": chunk.id, "document_id": chunk.document_id, "content": chunk.content[:3000]} for chunk in chunks],
                            "scope_document_ids": run.document_ids_json,
                            "deadline_at": run.context_json.get("deadline_at")}, output_schema=ComparisonAnswer.model_json_schema(),
                        run_id=run.id, tool_token=token_factory() if token_factory else create_tool_token(run)), cancelled=cancelled, on_event=on_event)
                    if on_event:
                        on_event({"type": "usage", "session_id": response.session_id or f"compare:{profile}:{variant['name']}:{hashlib.sha256(case['question'].encode()).hexdigest()}",
                                  "backend": response.backend, "model": response.model, "usage": response.usage})
                    output = ComparisonAnswer.model_validate(response.output)
                    cited = set()
                    for evidence in output.evidence:
                        chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == evidence.chunk_id,
                            KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
                            KnowledgeChunk.document_id.in_(run.document_ids_json)))
                        if chunk is None or evidence.quote not in chunk.content:
                            raise ValueError("比较回答包含无效引用")
                        cited.add(chunk.id)
                    statements = [line for line in re.split(r"[。！？\n]", output.answer) if line.strip()]
                    covered = sum(any(f"[{identifier}]" in statement for identifier in cited) for statement in statements)
                    expected = set(case.get("expected_chunk_ids") or [])
                    selected = cited
                    if not expected and case.get("expected_document_ids"):
                        expected = set(case["expected_document_ids"])
                        selected = {e.document_id or db.get(KnowledgeChunk, e.chunk_id).document_id for e in output.evidence}
                    costs = response.usage.get("cost", response.usage.get("total_cost"))
                    row.update({"status": "completed", "answer": output.answer,
                        "evidence": [e.model_dump() for e in output.evidence], "usage": response.usage,
                        "backend": response.backend, "model": response.model, "cost": costs,
                        "reported_cost": response.usage.get("reported_cost"),
                        "precision": len(selected & expected)/len(selected) if expected and selected else (0 if expected else None),
                        "recall": len(selected & expected)/len(expected) if expected else None,
                        "evidence_coverage": covered/len(statements) if statements else 0,
                        "coverage_metric": "proportion_of_answer_statements_with_valid_source_markers; not_entailment"})
                    if case.get("reference_answer"):
                        reference = set(re.findall(r"\w", case["reference_answer"].lower()))
                        actual = set(re.findall(r"\w", output.answer.lower()))
                        row["reference_character_recall"] = len(reference & actual) / len(reference) if reference else None
                        row["reference_metric"] = "character_overlap; not_semantic_correctness"
                except Exception as exc:
                    row.update({"status": "failed", "error_type": type(exc).__name__, "cost": None})
                row["elapsed_ms"] = round((time.perf_counter()-started)*1000, 2)
                rows.append(row)
    return {"comparisons": rows, "limit": 20, "profiles": profiles, "prompt_variants": [v["name"] for v in variants],
            "cost_note": "Only provider-reported inference cost is counted; missing cost remains null"}
