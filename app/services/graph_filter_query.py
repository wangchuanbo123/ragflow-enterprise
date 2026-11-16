"""Load and filter complete effective facts. No model-generated query is executed."""

from dataclasses import dataclass, field
import hashlib
import json
import math
import time
from collections import defaultdict

from sqlalchemy import func, select

from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_graph import KgAlias, KgEntity, KgEntityMention, KgRelation, KgRelationEvidence, KgGraphCorrection
from app.models.graph_governance import GraphSchemaDefinition
from rag.knowledge_graph.sqlite_store import SqliteGraphStore


class FilterQueryError(RuntimeError):
    def __init__(self, message, *, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("图谱过滤超时，请缩小知识库范围后重试")


def checked_items(items, deadline):
    for position, item in enumerate(items):
        if position % 32 == 0:
            check_deadline(deadline)
        yield item


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def fold(value):
    return str(value).strip().casefold()


def confidence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise FilterQueryError("现有图谱包含无效置信度，请先校正数据")
    return float(value)


@dataclass
class GraphSnapshot:
    nodes: dict
    edges: dict
    documents: dict
    entity_aliases: dict
    relation_aliases: dict
    entity_types: list
    relation_types: list
    schema_fingerprint: str
    graph_fingerprint: str
    warnings: list
    query_indexes: dict = field(default_factory=dict)


def load_snapshot(db, knowledge_base_id, deadline, *, max_records=200000):
    """Budget rejection is explicit; never substitute a first-page sample."""
    count = 0
    for model in (KnowledgeDocument, KgEntity, KgAlias, KgRelation, KgEntityMention, KgRelationEvidence, KgGraphCorrection):
        count += db.scalar(select(func.count()).select_from(model).where(model.knowledge_base_id == knowledge_base_id)) or 0
        check_deadline(deadline)
        if count > max_records:
            raise FilterQueryError("完整图谱超过当前过滤资源预算，未应用任何部分筛选；请缩小知识库范围或由管理员提高预算")
    store = SqliteGraphStore(db, knowledge_base_id)
    entities = store.list_effective_entities(check_cancelled=lambda: check_deadline(deadline))
    check_deadline(deadline)
    relations = store.list_effective_relations(check_cancelled=lambda: check_deadline(deadline))
    check_deadline(deadline)
    merges = store._merge_map(check_cancelled=lambda: check_deadline(deadline))
    corrections = store._active_corrections()
    updates = {row.target_id: row.payload_json or {} for row in corrections if row.action == "update_relation"}
    relation_merges = {row.target_id: str((row.payload_json or {}).get("canonical_relation_id", ""))
                       for row in corrections if row.action == "merge_relation"}
    hidden = {row.target_id for row in corrections if row.action == "hide_relation"}
    documents = {row.id: {"id": row.id, "name": row.original_filename, "path": row.source_path,
                         "file_hash": row.file_hash, "version": row.current_version_no}
                 for row in db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                                                                      KnowledgeDocument.status != "deleted"))}
    nodes = {row["id"]: {**row, "aliases": sorted(set(row.get("aliases", []))), "label": row["canonical_name"], "source_documents": set(), "evidence": [],
                         "confidence": None, "mention_count": 0} for row in entities}
    entity_aliases = {identifier: identifier for identifier in nodes}
    entity_aliases.update({old: new for old, new in merges.items() if new in nodes})
    edges, keys = {}, {}
    dropped = 0
    raw_relations = {row.id: row for row in db.scalars(select(KgRelation).where(KgRelation.knowledge_base_id == knowledge_base_id))}
    correction_by_id = {"manual:" + row.id: row for row in corrections if row.action == "add_relation"}
    for row in checked_items(relations, deadline):
        if row["subject_id"] not in nodes or row["object_id"] not in nodes:
            dropped += 1
            continue
        identifier = row["relation_id"]
        original = raw_relations.get(identifier)
        payload = updates.get(identifier, {})
        description = payload.get("description", original.description if original is not None else None)
        edges[identifier] = {"id": identifier, "source": row["subject_id"], "target": row["object_id"],
            "predicate": row["predicate"], "confidence": confidence(row["confidence"]), "description": description,
            "evidence": [], "source_documents": set(), "manual": row.get("manual", False), "correction_id": row.get("correction_id")}
        keys[(row["subject_id"], row["predicate"], row["object_id"])] = identifier

    relation_aliases = {identifier: identifier for identifier in edges}
    for identifier, row in checked_items(raw_relations.items(), deadline):
        current, visited = identifier, set()
        while current in relation_merges and current not in visited:
            visited.add(current); current = relation_merges[current]
        if current in edges:
            relation_aliases[identifier] = current
        elif identifier not in hidden and identifier not in relation_merges:
            key = (merges.get(row.subject_entity_id, row.subject_entity_id), updates.get(identifier, {}).get("predicate", row.predicate),
                   merges.get(row.object_entity_id, row.object_entity_id))
            if key in keys:
                relation_aliases[identifier] = keys[key]

    mentions = list(db.scalars(select(KgEntityMention).where(KgEntityMention.knowledge_base_id == knowledge_base_id)))
    proofs = list(db.scalars(select(KgRelationEvidence).where(KgRelationEvidence.knowledge_base_id == knowledge_base_id)))
    chunks_needed = {row.chunk_id for row in [*mentions, *proofs]}
    chunks_needed.update(str((row.payload_json or {}).get("chunk_id", "")) for row in correction_by_id.values())
    chunks = {}
    identifiers = sorted(chunks_needed - {""})
    for start in range(0, len(identifiers), 500):
        check_deadline(deadline)
        for row in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                                                          KnowledgeChunk.id.in_(identifiers[start:start + 500]))):
            if row.document_id in documents:
                chunks[row.id] = row

    def evidence(chunk_id, document_id, quote, score):
        chunk = chunks.get(chunk_id)
        if chunk is None or chunk.document_id != document_id or not quote or quote not in chunk.content:
            return None
        return {"chunk_id": chunk_id, "document_id": document_id, "quote": quote, "confidence": confidence(score)}

    for row in checked_items(mentions, deadline):
        node = nodes.get(merges.get(row.entity_id, row.entity_id))
        proof = evidence(row.chunk_id, row.document_id, row.mention, row.confidence)
        if node is not None and proof:
            node["mention_count"] += 1
            node["evidence"].append(proof)
    for row in checked_items(proofs, deadline):
        edge = edges.get(relation_aliases.get(row.relation_id, ""))
        proof = evidence(row.chunk_id, row.document_id, row.quote, row.confidence)
        if edge is not None and proof:
            edge["evidence"].append(proof)
    for identifier, row in checked_items(correction_by_id.items(), deadline):
        edge = edges.get(identifier)
        payload = row.payload_json or {}
        proof = evidence(payload.get("chunk_id"), payload.get("document_id"), payload.get("evidence"), payload.get("confidence", 1.0))
        if edge is not None and proof:
            edge["evidence"].append(proof)
    for edge in checked_items(edges.values(), deadline):
        unique = {}
        for proof in edge["evidence"]:
            key = (proof["chunk_id"], proof["quote"])
            if key not in unique or proof["confidence"] > unique[key]["confidence"]:
                unique[key] = proof
        edge["evidence"] = list(unique.values())
        edge["source_documents"] = {proof["document_id"] for proof in edge["evidence"]}
        for identifier in (edge["source"], edge["target"]):
            nodes[identifier]["evidence"].extend(edge["evidence"])
    for node in checked_items(nodes.values(), deadline):
        unique = {}
        for proof in node["evidence"]:
            key = (proof["chunk_id"], proof["quote"])
            if key not in unique or proof["confidence"] > unique[key]["confidence"]:
                unique[key] = proof
        node["evidence"] = list(unique.values())
        node["source_documents"] = {proof["document_id"] for proof in node["evidence"]}
        node["confidence"] = max((proof["confidence"] for proof in node["evidence"]), default=None)
    schema = list(db.scalars(select(GraphSchemaDefinition).where(GraphSchemaDefinition.knowledge_base_id == knowledge_base_id,
        GraphSchemaDefinition.graph_kind == "general", GraphSchemaDefinition.status == "active").order_by(GraphSchemaDefinition.version)))
    schema_data = [{"id": row.id, "version": row.version, "entities": row.entity_types_json, "relations": row.relation_types_json,
                    "rules": row.normalization_rules_json, "details": row.generation_summary_json,
                    "profiles": row.graph_profiles_json, "prompt": row.extraction_prompt} for row in schema]
    entity_types = sorted({node["entity_type"] for node in nodes.values()} | {value for row in schema for value in row.entity_types_json})
    relation_types = sorted({edge["predicate"] for edge in edges.values()} | {value for row in schema for value in row.relation_types_json})
    # Sets are serialized deterministically; evidence order is database-independent.
    def canonical(items):
        return [{**item, "source_documents": sorted(item["source_documents"]), "evidence": sorted(item["evidence"], key=lambda p: (p["chunk_id"], p["quote"]))}
                for item in sorted(items, key=lambda value: value["id"])]
    check_deadline(deadline)
    snapshot = GraphSnapshot(nodes, edges, documents, entity_aliases, relation_aliases, entity_types, relation_types,
        fingerprint(schema_data), fingerprint({"nodes": canonical(nodes.values()), "edges": canonical(edges.values()), "documents": documents}),
        ["已排除端点不属于当前知识库的损坏关系"] if dropped else [])
    snapshot.query_indexes = build_query_indexes(snapshot, deadline)
    return snapshot


def build_query_indexes(snapshot, deadline):
    """Deterministic inverted indexes prune candidates before costly predicates.

    These index the complete effective graph, including aliases/corrections and
    verified evidence. Restricting raw SQL before those overlays would drop
    valid merged/manual facts; this equivalent prefilter preserves semantics.
    """
    indexes = {"node": defaultdict(lambda: defaultdict(set)), "edge": defaultdict(lambda: defaultdict(set))}
    for target, items in (("node", snapshot.nodes), ("edge", snapshot.edges)):
        for identifier, item in checked_items(items.items(), deadline):
            values = {"id": [identifier], "source_document": item["source_documents"],
                      "has_evidence": [bool(item["evidence"])]}
            if target == "node":
                values.update(entity_type=[item["entity_type"]], name=[item["label"], *item.get("aliases", [])])
            else:
                source, destination = snapshot.nodes[item["source"]], snapshot.nodes[item["target"]]
                values.update(predicate=[item["predicate"]], source_type=[source["entity_type"]],
                    target_type=[destination["entity_type"]], endpoint_type=[source["entity_type"], destination["entity_type"]],
                    source_name=[source["label"], *source.get("aliases", [])],
                    target_name=[destination["label"], *destination.get("aliases", [])])
            for name, entries in values.items():
                for value in entries:
                    key = value if name in {"source_document", "has_evidence"} else fold(value)
                    indexes[target][name][key].add(identifier)
    return indexes


def candidate_ids(snapshot, target, conditions, deadline):
    universe = snapshot.nodes if target == "node" else snapshot.edges
    candidates = None
    indexes = snapshot.query_indexes.get(target, {})
    for condition in conditions:
        check_deadline(deadline)
        index = indexes.get(condition.field)
        if index is None or condition.operator not in {"in", "not_in", "eq"}:
            continue
        values = [condition.boolean] if condition.field == "has_evidence" else condition.values
        selected = set()
        for value in values:
            key = value if condition.field in {"source_document", "has_evidence"} else fold(value)
            selected.update(index.get(key, ()))
        if condition.operator == "not_in":
            selected = set(universe) - selected
        candidates = selected if candidates is None else candidates & selected
    return universe.keys() if candidates is None else candidates


def resolve_values(snapshot, condition, deadline=None):
    def check():
        if deadline is not None:
            check_deadline(deadline)
    field = condition.field
    values = condition.values
    if field in {"entity_type", "source_type", "target_type", "endpoint_type", "predicate"}:
        vocabulary = snapshot.relation_types if field == "predicate" else snapshot.entity_types
        choices = {fold(value): value for value in vocabulary}
        if any(fold(value) not in choices for value in values):
            raise FilterQueryError("指定类型不在当前知识库已有类型或有效Schema中")
        condition.values = [choices[fold(value)] for value in values]
    elif field == "source_document":
        resolved = []
        for value in values:
            check()
            found = [value] if value in snapshot.documents else []
            if not found:
                for position, (identifier, doc) in enumerate(snapshot.documents.items()):
                    if position % 32 == 0:
                        check()
                    if fold(value) in {fold(doc["name"]), fold(doc["path"])}:
                        found.append(identifier)
            if len(found) != 1:
                raise FilterQueryError("来源文档未在当前知识库唯一匹配，请使用文档ID或完整来源路径",
                    candidates=[{"id": identifier, "label": snapshot.documents[identifier]["name"],
                                 "value": identifier, "description": snapshot.documents[identifier]["path"]}
                                for identifier in found[:10]])
            resolved.extend(found)
        condition.values = list(dict.fromkeys(resolved))
    elif field == "id":
        aliases = snapshot.entity_aliases if condition.target == "node" else snapshot.relation_aliases
        if any(value not in aliases for value in values):
            raise FilterQueryError("指定实体或关系不属于当前知识库的有效事实")
        condition.values = list(dict.fromkeys(aliases[value] for value in values))
    elif field in {"name", "source_name", "target_name"} and condition.operator in {"in", "not_in"}:
        for value in values:
            found = False
            for position, node in enumerate(snapshot.nodes.values()):
                if position % 32 == 0:
                    check()
                if fold(value) in {fold(node["label"]), *map(fold, node.get("aliases", []))}:
                    found = True
                    break
            if not found:
                raise FilterQueryError("指定实体名称未在当前知识库匹配，请改写为现有名称或关键词条件")


def resolve_seed(snapshot, value, deadline=None):
    if value in snapshot.entity_aliases:
        return snapshot.entity_aliases[value]
    found = []
    for position, node in enumerate(snapshot.nodes.values()):
        if deadline is not None and position % 32 == 0:
            check_deadline(deadline)
        if fold(value) in {fold(node["label"]), *map(fold, node.get("aliases", []))}:
            found.append(node["id"])
    if len(found) != 1:
        raise FilterQueryError("中心实体未在当前知识库唯一匹配，请使用实体ID或明确名称",
            candidates=[{"id": identifier, "label": snapshot.nodes[identifier]["label"], "value": identifier,
                         "description": snapshot.nodes[identifier]["entity_type"]} for identifier in found[:10]])
    return found[0]


def matches(snapshot, item, condition):
    field, operator = condition.field, condition.operator
    if field == "confidence":
        value = item["confidence"]
        return value is not None and (value >= condition.number if operator == "gte" else value <= condition.number)
    if field == "has_evidence":
        return bool(item["evidence"]) is condition.boolean
    if field == "source_document":
        found = bool(item["source_documents"] & set(condition.values))
    else:
        if field == "name":
            candidates = [item["label"], *item.get("aliases", [])]
        elif field == "keyword":
            candidates = [item.get("label", ""), *item.get("aliases", []), item.get("description") or "", item.get("predicate", "")]
            if condition.target == "edge":
                candidates += [snapshot.nodes[item[key]]["label"] for key in ("source", "target")]
        elif field in {"source_type", "target_type", "endpoint_type", "source_name", "target_name"}:
            endpoints = ("source", "target") if field == "endpoint_type" else (field.split("_")[0],)
            candidates = [snapshot.nodes[item[key]]["entity_type" if field.endswith("type") else "label"] for key in endpoints]
            if field.endswith("name"):
                candidates += [alias for key in endpoints for alias in snapshot.nodes[item[key]].get("aliases", [])]
        else:
            candidates = [item[field]]
        values, candidates = list(map(fold, condition.values)), list(map(fold, candidates))
        found = any(value in candidate if "contains" in operator else value == candidate for value in values for candidate in candidates)
    return not found if operator in {"not_in", "not_contains_any"} else found


def execute_plan(snapshot, plan, deadline, *, diagnostics=None):
    matched_nodes, matched_edges = set(snapshot.nodes), set(snapshot.edges)
    connected_required = False
    for principle in plan.principles:
        principle_nodes, principle_edges = set(), set()
        for clause in principle.alternatives:
            check_deadline(deadline)
            node_conditions = [condition for condition in clause.conditions if condition.target == "node"]
            edge_conditions = [condition for condition in clause.conditions if condition.target == "edge"]
            nodes, edges = set(), set()
            for position, identifier in enumerate(candidate_ids(snapshot, "node", node_conditions, deadline)):
                if position % 32 == 0:
                    check_deadline(deadline)
                item = snapshot.nodes[identifier]
                if all(matches(snapshot, item, condition) for condition in node_conditions):
                    nodes.add(identifier)
            for position, identifier in enumerate(candidate_ids(snapshot, "edge", edge_conditions, deadline)):
                if position % 32 == 0:
                    check_deadline(deadline)
                item = snapshot.edges[identifier]
                if all(matches(snapshot, item, condition) for condition in edge_conditions):
                    edges.add(identifier)
            if clause.neighborhood:
                neighborhood = clause.neighborhood
                frontier = set(neighborhood.seeds)
                reached, traversed = set(frontier), set()
                adjacency = defaultdict(list)
                for identifier in edges:
                    edge = snapshot.edges[identifier]
                    if neighborhood.direction in {"outgoing", "both"}:
                        adjacency[edge["source"]].append((edge["target"], identifier))
                    if neighborhood.direction in {"incoming", "both"}:
                        adjacency[edge["target"]].append((edge["source"], identifier))
                for _ in range(neighborhood.hops):
                    check_deadline(deadline)
                    following = set()
                    for identifier in frontier:
                        for neighbor, edge_id in adjacency[identifier]:
                            traversed.add(edge_id); following.add(neighbor)
                    frontier = following - reached
                    reached.update(following)
                nodes &= reached
                edges &= traversed
            edges = {identifier for identifier in edges if snapshot.edges[identifier]["source"] in nodes and snapshot.edges[identifier]["target"] in nodes}
            if edge_conditions or clause.hide_isolated:
                endpoints = {snapshot.edges[identifier][key] for identifier in edges for key in ("source", "target")}
                nodes &= endpoints
            principle_nodes |= nodes
            principle_edges |= edges
        if len(principle.alternatives) > 1 and all(
                not clause.neighborhood and not clause.hide_isolated and all(condition.target == "node" for condition in clause.conditions)
                for clause in principle.alternatives):
            # "A or B nodes" keeps edges between A and B. Closing each OR
            # branch separately would incorrectly erase those connecting edges.
            principle_edges = {identifier for identifier, edge in snapshot.edges.items()
                               if edge["source"] in principle_nodes and edge["target"] in principle_nodes}
        matched_nodes &= principle_nodes
        matched_edges &= principle_edges
        if diagnostics is not None:
            # Cumulative AND counts reuse the already computed candidate sets.
            # No extra rule execution, DB query, or model call is necessary.
            connected_required = connected_required or principle_requires_connected(principle)
            visible_edges = {identifier for identifier in matched_edges if snapshot.edges[identifier]["source"] in matched_nodes
                             and snapshot.edges[identifier]["target"] in matched_nodes}
            visible_nodes = matched_nodes
            if connected_required:
                visible_nodes = matched_nodes & {snapshot.edges[identifier][key] for identifier in visible_edges for key in ("source", "target")}
            diagnostics.append({"index": principle.index, "matched_nodes": len(visible_nodes), "matched_edges": len(visible_edges)})
    matched_edges = {identifier for identifier in matched_edges if snapshot.edges[identifier]["source"] in matched_nodes and snapshot.edges[identifier]["target"] in matched_nodes}
    if requires_connected_nodes(plan):
        # AND principles can remove an edge that formerly prevented isolation.
        endpoints = {snapshot.edges[identifier][key] for identifier in matched_edges for key in ("source", "target")}
        matched_nodes &= endpoints
    check_deadline(deadline)
    return matched_nodes, matched_edges


def requires_connected_nodes(plan):
    return any(principle_requires_connected(principle) for principle in plan.principles)


def principle_requires_connected(principle):
    return all(clause.hide_isolated or any(condition.target == "edge" for condition in clause.conditions)
               for clause in principle.alternatives)
