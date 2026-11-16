"""Read-only, run-scoped MCP capabilities. No filesystem or arbitrary SQL access."""

from datetime import datetime, timedelta, timezone
import json
import jwt
from sqlalchemy import select

from app.core import config
from app.models.agent_run import AgentRun
from app.models.user import User
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import KgEntity, KgEntityMention, KgRelation, KgRelationEvidence
from app.models.index_job import IndexJob
from app.models.graph_governance import KgEntityMerge
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

TOOL_SCHEMAS = {
    "get_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "list_documents": {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "additionalProperties": False},
    "read_chunks": {"type": "object", "properties": {"document_id": {"type": "string"}, "chunk_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "additionalProperties": False},
    "search_entities": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 255}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"], "additionalProperties": False},
    "graph_neighbors": {"type": "object", "properties": {"entity_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["entity_id"], "additionalProperties": False},
    "job_status": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False},
}


def create_tool_token(run: AgentRun, lifetime_seconds: int = 360) -> str:
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=min(max(lifetime_seconds, 60), 360))
    if run.started_at:
        expiry = min(expiry, run.started_at.replace(tzinfo=timezone.utc) + timedelta(seconds=int(run.options_json.get("max_runtime_seconds", 900)) + 60))
    if run.context_json.get("deadline_at"):
        expiry = min(expiry, datetime.fromisoformat(run.context_json["deadline_at"]).replace(tzinfo=timezone.utc) + timedelta(seconds=60))
    return jwt.encode({"aud": "agent-tools", "purpose": "agent_tool", "sub": run.requested_by,
                       "run_id": run.id, "kb_id": run.knowledge_base_id, "attempt": run.attempt,
                       "iat": now, "exp": expiry},
                      config.JWT_SECRET_KEY, algorithm="HS256")


def authorize_tool_token(db, token: str) -> AgentRun:
    try:
        claims = jwt.decode(token, config.JWT_SECRET_KEY, algorithms=["HS256"], audience="agent-tools",
                            options={"require": ["aud", "exp", "sub", "run_id", "kb_id", "purpose", "attempt"]})
    except jwt.PyJWTError as exc:
        raise PermissionError("工具令牌无效或已过期") from exc
    run = db.get(AgentRun, claims["run_id"])
    user = db.get(User, claims["sub"])
    if (claims["purpose"] != "agent_tool" or run is None or user is None or not user.is_active
        or run.status != "running" or run.cancel_requested or run.attempt != claims["attempt"]
        or run.progress_json.get("stage") not in {"agent_planning", "applying"}
        or run.knowledge_base_id != claims["kb_id"] or run.requested_by != user.id
        or not KnowledgeBaseRepository(db).can_edit(run.knowledge_base_id, user.id, is_admin=user.role == "admin")):
        raise PermissionError("任务已结束、已取消或授权已撤销")
    owner = run
    for _ in range(10):
        if not (owner.context_json or {}).get("attached_index_job") or not owner.parent_run_id:
            break
        parent = db.get(AgentRun, owner.parent_run_id)
        if parent is None or parent.knowledge_base_id != run.knowledge_base_id:
            raise PermissionError("父任务授权已失效")
        owner = parent
    if (owner.cancel_requested or owner.status not in {"queued", "running"} or (owner.context_json or {}).get("budget_exhausted")
        or (owner.started_at and datetime.utcnow() >= owner.started_at + timedelta(seconds=int(owner.options_json.get("max_runtime_seconds", 900))))):
        raise PermissionError("工作流已取消或达到总预算")
    return run


def tool_catalog():
    return [{"name": name, "description": "Read authorized knowledge within this agent run only: " + name,
             "inputSchema": schema, "annotations": {"readOnlyHint": True, "destructiveHint": False}}
            for name, schema in TOOL_SCHEMAS.items()]


def call_tool(db, run: AgentRun, name: str, arguments: dict):
    if name not in TOOL_SCHEMAS:
        raise ValueError("未知工具")
    import jsonschema
    jsonschema.validate(arguments, TOOL_SCHEMAS[name])
    scope = list(run.document_ids_json)
    limit = arguments.get("limit", 20)
    offset = arguments.get("offset", 0)
    if name == "get_schema":
        from app.models.graph_governance import GraphSchemaDefinition
        rows = db.scalars(select(GraphSchemaDefinition).where(GraphSchemaDefinition.knowledge_base_id == run.knowledge_base_id,
                                                              GraphSchemaDefinition.status == "active",
                                                              GraphSchemaDefinition.graph_kind == "general"))
        return [{"id": row.id, "version": row.version, "graph_kind": row.graph_kind,
                 "entity_types": row.entity_types_json, "relation_types": row.relation_types_json} for row in rows]
    if name == "list_documents":
        rows = db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == run.knowledge_base_id,
                              KnowledgeDocument.id.in_(scope)).order_by(KnowledgeDocument.id).offset(offset).limit(limit))
        return [{"id": row.id, "filename": row.original_filename, "version": row.current_version_no, "status": row.status} for row in rows]
    if name == "read_chunks":
        query = select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
                                            KnowledgeChunk.document_id.in_(scope))
        if arguments.get("document_id"):
            if arguments["document_id"] not in scope:
                raise PermissionError("文档超出任务范围")
            query = query.where(KnowledgeChunk.document_id == arguments["document_id"])
        if arguments.get("chunk_ids"):
            query = query.where(KnowledgeChunk.id.in_(arguments["chunk_ids"]))
            found = set(db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.knowledge_base_id == run.knowledge_base_id,
                KnowledgeChunk.document_id.in_(scope), KnowledgeChunk.id.in_(arguments["chunk_ids"]))))
            if found != set(arguments["chunk_ids"]):
                raise PermissionError("片段超出任务范围")
        rows = db.scalars(query.order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index).offset(offset).limit(limit))
        return [{"id": row.id, "document_id": row.document_id, "chunk_index": row.chunk_index, "content": row.content[:6000],
                 "content_hash": row.content_hash, "graph_status": row.graph_status} for row in rows]
    allowed_entities = set(db.scalars(select(KgEntityMention.entity_id).where(KgEntityMention.knowledge_base_id == run.knowledge_base_id,
                                                             KgEntityMention.document_id.in_(scope))))
    merges = {row.merged_entity_id: row.canonical_entity_id for row in db.scalars(select(KgEntityMerge).where(
        KgEntityMerge.knowledge_base_id == run.knowledge_base_id, KgEntityMerge.status == "active"))}
    def canonical(identifier):
        seen = set()
        while identifier in merges and identifier not in seen:
            seen.add(identifier)
            identifier = merges[identifier]
        return identifier
    canonical_ids = {canonical(identifier) for identifier in allowed_entities}
    if name in {"search_entities", "graph_neighbors"}:
        from app.bootstrap.container import get_provider_container
        store = get_provider_container().graph_store(db, run.knowledge_base_id)
    if name == "search_entities":
        rows = store.search_entities(arguments["query"], max(50, limit * 4))
        return [{"id": row["id"], "name": row["canonical_name"], "entity_type": row["entity_type"]}
                for row in rows if row["id"] in canonical_ids][:limit]
    if name == "graph_neighbors":
        identifier = canonical(arguments["entity_id"])
        if identifier not in canonical_ids:
            raise PermissionError("实体超出任务范围")
        sources = [value for value in allowed_entities if canonical(value) == identifier]
        documents = list(db.scalars(select(KgEntityMention.document_id).where(KgEntityMention.knowledge_base_id == run.knowledge_base_id,
            KgEntityMention.document_id.in_(scope), KgEntityMention.entity_id.in_(sources)).distinct().limit(100)))
        result = {}
        for document_id in documents:
            for edge in store.get_document_subgraph(document_id, limit=200).get("edges", []):
                if identifier not in {edge["source"], edge["target"]}:
                    continue
                evidence = [item for item in edge.get("evidence", []) if item.get("document_id", document_id) in scope]
                if evidence:
                    first = evidence[0]
                    result[edge["id"]] = {"id": edge["id"], "subject_id": edge["source"], "predicate": edge["predicate"],
                        "object_id": edge["target"], "chunk_id": first.get("chunk_id"), "document_id": first.get("document_id", document_id),
                        "quote": first.get("quote", "")}
                if len(result) >= limit:
                    return list(result.values())
        return list(result.values())
    allowed_jobs = {run.job_id, *(run.options_json.get("job_ids") or [])}
    allowed_jobs.update(run.output_json.get("followup_job_ids") or [])
    allowed_jobs.update(item.get("job_id") for item in (run.output_json.get("actions") or []))
    if arguments["job_id"] not in allowed_jobs:
        raise PermissionError("任务编号超出授权范围")
    job = db.scalar(select(IndexJob).where(IndexJob.id == arguments["job_id"], IndexJob.knowledge_base_id == run.knowledge_base_id))
    if job is None:
        raise ValueError("任务不存在")
    return {"id": job.id, "status": job.status, "processed_items": job.processed_items, "total_items": job.total_items}
