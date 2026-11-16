"""Version-one lossless consolidation of legacy per-view graph schemas.

This module has no model/provider imports. Alembic, startup, and ZIP restore use
the same row transformation; original rows and complete definitions stay in the
audit trail. New migrations must not change this version-one transformation.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import uuid


CONSOLIDATION_VERSION = 1


def _audit(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _audit(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_audit(item) for item in value]
    return value


def _unique(values):
    seen, result = set(), []
    for value in values:
        key = json.dumps(_audit(value), sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(value))
    return result


def _names(values):
    return sorted({value for value in (values or []) if isinstance(value, str) and value})


def _details(row, category):
    summary = row.get("generation_summary_json") or {}
    details = [deepcopy(item) for item in summary.get(category + "_type_details", []) if isinstance(item, dict)]
    profiles = row.get("graph_profiles_json") or {}
    for profile in profiles.values():
        if isinstance(profile, dict):
            details.extend(deepcopy(item) for item in profile.get(category + "_types", []) if isinstance(item, dict))
    return details


def _definitions(rows, category, names):
    definitions, conflicts = [], []
    for name in names:
        originals = []
        for row in rows:
            for detail in _details(row, category):
                if detail.get("name") == name:
                    originals.append({"schema_id": row["id"], "graph_kind": row.get("graph_kind", "general"),
                                      "version": row["version"], "definition": detail})
        descriptions = _unique([item["definition"].get("description") or item["definition"].get("label")
                                for item in originals if item["definition"].get("description") or item["definition"].get("label")])
        definition = {"name": name, "graph_kind": "general", "description": "\n".join(descriptions),
                      "legacy_definitions": _audit(_unique(originals))}
        if len(descriptions) > 1:
            conflicts.append({"kind": category + "_definition", "name": name, "descriptions": descriptions})
        if category == "entity":
            for key in ("aliases", "exclusions"):
                definition[key] = _unique([value for item in originals for value in item["definition"].get(key, [])])
        else:
            variants = []
            for original in originals:
                detail = original["definition"]
                candidates = detail.get("endpoint_variants")
                if not isinstance(candidates, list):
                    candidates = [detail]
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    variants.append({"source_types": _names(item.get("source_types")),
                                     "target_types": _names(item.get("target_types")),
                                     "direction": str(item.get("direction") or detail.get("direction") or "directed")})
            variants = _unique(variants)
            definition["endpoint_variants"] = variants
            # The original paired alternatives are authoritative. Flattened
            # fields remain useful to older display clients, never to authorize
            # a new Cartesian combination of two independent endpoint unions.
            definition["source_types"] = [] if any(not item["source_types"] for item in variants) else _names(
                [value for item in variants for value in item["source_types"]])
            definition["target_types"] = [] if any(not item["target_types"] for item in variants) else _names(
                [value for item in variants for value in item["target_types"]])
            definition["direction"] = "\n".join(_unique([item["direction"] for item in variants])) or "directed"
            definition["aliases"] = _unique([value for item in originals for value in item["definition"].get("aliases", [])])
            if len(variants) > 1:
                conflicts.append({"kind": "relation_endpoints", "name": name, "endpoint_variants": variants})
        definitions.append(definition)
    return definitions, conflicts


def normalize_schema_rows(rows: list[dict], *, consolidated_at: datetime | None = None) -> list[dict]:
    """Return all historical rows plus at most one consolidated active per KB.

    Use database column names. Both decoded DB datetime values and ISO strings
    exported by historical ZIP backups are accepted. The input is not mutated.
    Calling this again on its own output is idempotent.
    """
    result = deepcopy(rows)
    groups = {}
    for row in result:
        groups.setdefault(row["knowledge_base_id"], []).append(row)
    for knowledge_base_id, historical in groups.items():
        active = sorted([row for row in historical if row.get("status") == "active"],
                        key=lambda row: (row.get("graph_kind", "general") != "general", int(row["version"])))
        if not active:
            continue
        if (len(active) == 1 and active[0].get("graph_kind", "general") == "general"
            and not active[0].get("graph_profiles_json")):
            continue
        original = deepcopy(active)
        entity_types = _names([value for row in active for value in row.get("entity_types_json") or []])
        relation_types = _names([value for row in active for value in row.get("relation_types_json") or []])
        entity_details, entity_conflicts = _definitions(original, "entity", entity_types)
        relation_details, relation_conflicts = _definitions(original, "relation", relation_types)
        preferred = next((row for row in active if row.get("graph_kind", "general") == "general"), active[-1])
        rules = deepcopy(preferred.get("normalization_rules_json") or {})
        rule_sources = [{"schema_id": row["id"], "rules": deepcopy(row.get("normalization_rules_json") or {})} for row in original]
        conflicts = entity_conflicts + relation_conflicts
        if len(_unique([item["rules"] for item in rule_sources])) > 1:
            conflicts.append({"kind": "normalization", "sources": rule_sources, "selected_schema_id": preferred["id"]})
        prompts = [{"schema_id": row["id"], "version": row["version"], "legacy_graph_kind": row.get("graph_kind", "general"),
                    "prompt": row.get("extraction_prompt")} for row in original if row.get("extraction_prompt")]
        prompt = (
            "本知识库只有一个覆盖全部文档的总图谱和总Schema。以下历史规则是同一Schema的类型与关系定义，"
            "仅在对应类型和原文上下文中适用；其中“仅限本视图”不得排除其他已声明类型。"
            "端点遵守结构化endpoint_variants的成对备选，不得交叉组合。冲突无法从原文消解时报告issues，禁止编造。\n\n"
            + "\n\n".join(f"【历史规则 {item['schema_id']} / v{item['version']} / {item['legacy_graph_kind']}】\n{item['prompt']}" for item in prompts)
        )
        version = max(int(row["version"]) for row in historical) + 1
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "rag:total-schema:v1:" + knowledge_base_id + ":"
                                   + ",".join(sorted(row["id"] for row in original)) + ":" + str(version)))
        instant = consolidated_at or datetime.utcnow()
        created_at = instant.isoformat() if any(isinstance(row.get("created_at"), str) for row in original) else instant
        compatible = sorted({0, *[int(row["version"]) for row in historical],
                             *[int(value) for row in historical for value in (row.get("generation_summary_json") or {}).get("compatible_extraction_versions", []) if isinstance(value, int)]})
        merged = {
            "id": identifier, "knowledge_base_id": knowledge_base_id, "graph_kind": "general", "version": version,
            "status": "active", "entity_types_json": entity_types, "relation_types_json": relation_types,
            "graph_profiles_json": None, "normalization_rules_json": rules, "extraction_prompt": prompt,
            "generation_method": "legacy_consolidation",
            "source_document_ids_json": _names([value for row in original for value in row.get("source_document_ids_json") or []]),
            "generation_summary_json": {
                "consolidation_version": CONSOLIDATION_VERSION, "graph_kind": "general",
                "consolidated_schema_ids": [row["id"] for row in original], "legacy_schemas": _audit(original),
                "entity_type_details": entity_details, "relation_type_details": relation_details,
                "normalization_rule_sources": _audit(rule_sources), "legacy_extraction_prompts": prompts,
                "consolidation_conflicts": conflicts, "compatible_extraction_versions": compatible,
                "preserved_legacy_extractions": True,
            },
            "created_by": None, "created_at": created_at, "activated_at": created_at,
        }
        for row in active:
            row["status"] = "superseded"
        result.append(merged)
    return result


def consolidate_connection(connection, knowledge_base_id: str | None = None) -> int:
    """Normalize existing rows inside the caller's transaction, without commits."""
    from sqlalchemy import MetaData, Table, select, update

    table = Table("graph_schema_definitions", MetaData(), autoload_with=connection)
    query = select(table)
    if knowledge_base_id is not None:
        query = query.where(table.c.knowledge_base_id == knowledge_base_id)
    original = [dict(row) for row in connection.execute(query).mappings()]
    normalized = normalize_schema_rows(original)
    existing = {row["id"]: row for row in original}
    added = 0
    # Demote old active rows before inserting their replacement: the partial
    # unique constraint is immediate on PostgreSQL and SQLite.
    for row in normalized:
        old = existing.get(row["id"])
        if old is not None and old["status"] != row["status"]:
            connection.execute(update(table).where(table.c.id == row["id"]).values(status=row["status"]))
    for row in normalized:
        if row["id"] not in existing:
            connection.execute(table.insert().values(**row))
            added += 1
    return added


def normalize_pending_schema_jobs(connection) -> None:
    """Convert only unfinished legacy work; completed audit logs stay intact."""
    from sqlalchemy import MetaData, Table, inspect, select, update

    metadata = MetaData()
    names = set(inspect(connection).get_table_names())
    schemas = Table("graph_schema_definitions", metadata, autoload_with=connection)
    kinds = {row[0]: row[1] for row in connection.execute(select(schemas.c.id, schemas.c.graph_kind))}
    for name, column in (("index_jobs", "options_json"), ("agent_runs", "options_json")):
        if name not in names:
            continue
        table = Table(name, metadata, autoload_with=connection)
        query = select(table.c.id, table.c[column]).where(table.c.status.in_(
            ["pending", "queued", "running", "retry", "retrying", "waiting_retry"]))
        if name == "index_jobs":
            query = query.where(table.c.job_type.in_(["schema_generation", "schema_drift"]))
        for identifier, raw in connection.execute(query):
            options = deepcopy(raw or {})
            changed = False
            kind = options.get("graph_kind")
            if kind and kind != "general":
                options["legacy_graph_kind"] = kind
                options["graph_kind"] = "general"
                changed = True
            target = options.get("target_schema_id")
            if target and kinds.get(target, "general") != "general":
                options["legacy_target_schema_id"] = target
                options.pop("target_schema_id", None)
                changed = True
            if changed:
                connection.execute(update(table).where(table.c.id == identifier).values({column: options}))

