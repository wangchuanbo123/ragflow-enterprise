"""Durable workflows exercise real application writes with a controlled agent."""

from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.agents.contracts import AgentResult
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.models.agent_run import AgentRun
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import KgEntity, KgEntityMention, KgRelation, KgRelationEvidence
from app.models.user import User
from app.models.graph_governance import GraphReviewItem, GraphSchemaDefinition
from app.models.document_management import DocumentTagLink
from app.schemas.agent_run import AgentRunCreate
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.knowledge_workflows.tools import authorize_tool_token, call_tool, create_tool_token


def seed(db):
    db.add_all([KnowledgeBase.default(), User(id="workflow-user", username="workflow-user", role="admin", password_hash="unused", is_active=True),
        KnowledgeDocument(id="doc-workflow", knowledge_base_id=KB, source_path="doc-workflow/guide.txt", original_filename="guide.txt", file_hash="v1", file_size=10, status="ready", current_version_no=1),
        KnowledgeChunk(id="chunk-workflow", knowledge_base_id=KB, document_id="doc-workflow", file_hash="v1", chunk_index=0,
                       content="系统A依赖系统B。2026年11月5日开始维护。", content_hash="chunk-hash", graph_status="pending"),
        KgEntity(id="entity-a", knowledge_base_id=KB, entity_type="system", canonical_name="系统A", normalized_name="a"),
        KgEntity(id="entity-b", knowledge_base_id=KB, entity_type="system", canonical_name="系统B", normalized_name="b"),
        KgEntityMention(knowledge_base_id=KB, entity_id="entity-a", document_id="doc-workflow", chunk_id="chunk-workflow", mention="系统A", confidence=1),
        KgEntityMention(knowledge_base_id=KB, entity_id="entity-b", document_id="doc-workflow", chunk_id="chunk-workflow", mention="系统B", confidence=1),
        KgRelation(id="relation-ab", knowledge_base_id=KB, subject_entity_id="entity-a", object_entity_id="entity-b", predicate="depends_on", confidence=1),
        KgRelationEvidence(knowledge_base_id=KB, relation_id="relation-ab", document_id="doc-workflow", chunk_id="chunk-workflow",
                           quote="系统A依赖系统B", quote_hash="quote-hash", confidence=1)])
    db.commit()


class FakeAgent:
    def __init__(self, task=None, action=None):
        self.task = task
        self.action = action
        self.requests = []
    def run(self, request, *, cancelled=None, on_event=None):
        self.requests.append(request)
        if request.context.get("mode") == "answer_comparison":
            return AgentResult(output={"answer": "系统A依赖系统B[chunk-workflow]", "evidence": [
                {"chunk_id": "chunk-workflow", "document_id": "doc-workflow", "quote": "系统A依赖系统B"}]}, backend="fake", model="test", usage={"cost": 0.01})
        if on_event:
            on_event({"type": "planning", "message": "controlled planning", "tool_token": request.tool_token})
        actions = [self.action] if self.action else []
        return AgentResult(output={"summary": "已依据文档完成规划", "items": [{"finding": "系统依赖"}],
            "evidence": [{"chunk_id": "chunk-workflow", "quote": "系统A依赖系统B", "document_id": "doc-workflow"}],
            "actions": actions, "diagnostics": [], "metrics": {}}, backend="fake", session_id="test-session", model="test", usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})


def setup_agent(monkeypatch, agent):
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile_name=None: agent)
    monkeypatch.setattr("app.agents.factory.list_backend_profiles", lambda: ["default", "backup"])


def finish_children(db, service, run):
    identifiers = run.output_json.get("followup_job_ids", [])
    if identifiers:
        db.execute(update(IndexJob).where(IndexJob.id.in_(identifiers)).values(status="completed"))
        db.commit()
    return service.get(run.id)


@pytest.mark.parametrize("task,action", [
    ("extraction", None),
    ("ingestion", None),
    ("schema_governance", {"kind": "create_schema_draft", "params": {"entity_types": ["system"], "relation_types": ["depends_on"], "normalization_rules": {}}}),
    ("quality_review", {"kind": "propose_correction", "params": {"action": "hide_relation", "target_id": "relation-ab", "reason": "原文需进一步人工复核", "payload": {}}}),
    ("incremental_maintenance", None),
    ("knowledge_organization", {"kind": "tag_documents", "document_ids": ["doc-workflow"], "params": {"tags": ["系统依赖"]}}),
    ("evaluation", None),
    ("business_analysis", None),
    ("operations", {"kind": "adjust_batch", "params": {"batch_size": 5}}),
])
def test_all_nine_tasks_reach_real_business_results(db_engine, monkeypatch, task, action):
    from app.core.database import SessionLocal
    agent = FakeAgent(task, action)
    setup_agent(monkeypatch, agent)
    built = []
    def build_graph(self, **kwargs):
        built.append(kwargs)
        assert kwargs["document_ids"] == ["doc-workflow"]
        assert kwargs["agent_context"]["tool_token"]
        self.db.execute(update(KnowledgeChunk).where(KnowledgeChunk.id == "chunk-workflow").values(graph_status="completed"))
        self.db.commit()
        return {"succeeded": 1, "failed": 0}
    monkeypatch.setattr("app.services.knowledge_graph_service.GraphBuildService.build_graph", build_graph)
    monkeypatch.setattr("app.services.rag_service.inspect_retrieval", lambda *args, **kwargs: {"sources": [{"chunk_id": "chunk-workflow", "document_id": "doc-workflow"}]})
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        options = {"apply": True}
        if task == "evaluation":
            options.update({"gold_cases": [{"question": "系统A依赖谁", "expected_document_ids": ["doc-workflow"]}],
                            "comparison_profiles": ["default", "backup"], "prompt_variants": [{"name": "p1", "instructions": "cite sources"}]})
        run = service.create(AgentRunCreate(task=task, document_ids=["doc-workflow"], options=options), "workflow-user")
        result = service.execute_for_job(run.job_id)
        assert result["status"] == "completed", service.serialize(run)
        if run.output_json.get("followup_job_ids"):
            assert run.status == "running" and run.progress_json["stage"] == "waiting_for_jobs"
        run = finish_children(db, service, run)
        assert run.status == "succeeded", service.serialize(run)
        assert agent.requests and run.output_json["summary"]
        assert all(request.context.get("deadline_at") == agent.requests[0].context["deadline_at"] for request in agent.requests)
        assert datetime.fromisoformat(agent.requests[0].context["deadline_at"]).utcoffset().total_seconds() == 0
        if task in {"extraction", "incremental_maintenance"}:
            assert built and db.get(KnowledgeChunk, "chunk-workflow").graph_status == "completed"
        elif task == "ingestion":
            assert run.output_json["actions"][0]["result"]["job_ids"]
        elif task == "schema_governance":
            assert db.scalar(select(GraphSchemaDefinition).where(GraphSchemaDefinition.generation_method == "agent_workflow")).status == "draft"
        elif task == "quality_review":
            assert db.scalar(select(GraphReviewItem).where(GraphReviewItem.item_type == "agent_correction_candidate")).status == "pending"
        elif task == "knowledge_organization":
            assert db.scalar(select(DocumentTagLink).where(DocumentTagLink.document_id == "doc-workflow"))
        elif task == "evaluation":
            experiments = run.output_json["metrics"]["model_prompt_comparison"]["comparisons"]
            assert len(experiments) == 2 and all(row["precision"] == 1 and row["evidence_coverage"] == 1 for row in experiments)
        elif task == "business_analysis":
            artifact = next(item for item in run.output_json["items"] if item.get("kind") == "evidence_backed_business_graph")
            assert artifact["dependencies"] and artifact["impact_paths"] and artifact["timeline"]
        elif task == "operations":
            assert db.get(KnowledgeBase, KB).settings_json["agent_workflows"]["batch_size"] == 5
        serialized = json.dumps(service.serialize(run), default=str)
        assert agent.requests[0].tool_token not in serialized


def test_preview_and_fabricated_evidence_do_not_write(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    agent = FakeAgent(action={"kind": "tag_documents", "params": {"tags": ["preview"]}})
    setup_agent(monkeypatch, agent)
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="knowledge_organization", options={"apply": False}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "completed"
        assert db.scalar(select(DocumentTagLink)) is None
        assert run.output_json["actions"][0]["status"] == "proposed"
        original_run = agent.run
        def fabricate(request, **kwargs):
            result = original_run(request, **kwargs)
            result.output["evidence"][0]["quote"] = "fabricated quote"
            return result
        monkeypatch.setattr(agent, "run", fabricate)
        bad = service.create(AgentRunCreate(task="knowledge_organization", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(bad.job_id)["status"] == "failed"
        assert db.scalar(select(DocumentTagLink)) is None


def test_mcp_token_is_run_scoped_revocable_and_not_a_login_token(db_engine):
    from app.core.database import SessionLocal
    from app.core.security import create_access_token
    with SessionLocal() as db:
        seed(db)
        run = AgentRun(knowledge_base_id=KB, requested_by="workflow-user", task="business_analysis", status="running",
            instructions="", document_ids_json=["doc-workflow"], options_json={}, context_json={}, output_json={}, usage_json={},
            progress_json={"stage": "agent_planning"}, started_at=datetime.utcnow())
        db.add(run)
        db.commit()
        token = create_tool_token(run)
        assert authorize_tool_token(db, token).id == run.id
        assert call_tool(db, run, "read_chunks", {"chunk_ids": ["chunk-workflow"]})[0]["id"] == "chunk-workflow"
        with pytest.raises(PermissionError):
            call_tool(db, run, "read_chunks", {"document_id": "foreign-doc"})
        with pytest.raises(ValueError):
            call_tool(db, run, "shell", {"command": "anything"})
        with pytest.raises(PermissionError):
            authorize_tool_token(db, create_access_token("workflow-user"))
        run.cancel_requested = True
        db.commit()
        with pytest.raises(PermissionError):
            authorize_tool_token(db, token)


def test_invalid_action_scope_and_unregistered_profile_are_blocked(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    setup_agent(monkeypatch, FakeAgent(action={"kind": "tag_documents", "document_ids": ["foreign-doc"], "params": {"tags": ["unsafe"]}}))
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        with pytest.raises(Exception):
            service.create(AgentRunCreate(task="operations", options={"runtime_profile": "https://attacker.invalid"}), "workflow-user")
        run = service.create(AgentRunCreate(task="knowledge_organization", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "failed"
        assert db.scalar(select(DocumentTagLink)) is None


def test_usage_ledger_deduplicates_planning_and_subsessions(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    agent = FakeAgent()
    original = agent.run
    def run(request, **kwargs):
        callback = kwargs["on_event"]
        for identifier, usage in [("child-one", {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5, "cost": 0.2}),
                                  ("child-two", {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9, "cost": None}),
                                  ("child-one", {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5, "cost": 0.2})]:
            callback({"type": "usage", "session_id": identifier, "model": "test", "usage": usage})
        return original(request, **kwargs)
    monkeypatch.setattr(agent, "run", run)
    setup_agent(monkeypatch, agent)
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        current = service.create(AgentRunCreate(task="operations"), "workflow-user")
        assert service.execute_for_job(current.job_id)["status"] == "completed"
        db.expire(current, ["usage_json"])
        assert len(current.usage_json["sessions"]) == 3
        assert current.usage_json["input_tokens"] == 16
        assert current.usage_json["output_tokens"] == 28
        assert current.usage_json["total_tokens"] == 44
        assert current.usage_json["cost"] == 0.2 and current.usage_json["cost_coverage"] == "partial"


def test_parent_budget_is_shared_with_attached_jobs_and_tokens_refresh(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.agents.contracts import AgentCancelledError
    from datetime import timedelta, timezone
    import jwt
    setup_agent(monkeypatch, FakeAgent())
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        parent = service.create(AgentRunCreate(task="extraction", options={"max_agent_calls": 2, "max_runtime_seconds": 60}), "workflow-user")
        parent.status = "running"
        parent.started_at = datetime.utcnow()
        parent.progress_json = {"stage": "applying"}
        child = IndexJob(knowledge_base_id=KB, job_type="graph_rebuild", status="running", requested_by="workflow-user", options_json={"agent_run_id": parent.id})
        db.add(child)
        db.commit()
        attached, context = service.attach_existing_job(child.id, "extraction")
        assert datetime.fromisoformat(context["deadline_at"]) == parent.started_at.replace(tzinfo=timezone.utc) + timedelta(seconds=60)
        claims = jwt.decode(context["token_factory"](), options={"verify_signature": False})
        assert claims["exp"] <= int(parent.started_at.replace(tzinfo=timezone.utc).timestamp()) + 121
        service.reserve_agent_call(parent.id)
        context["before_request"]()
        with pytest.raises(AgentCancelledError):
            context["before_request"]()
        assert service.attached_cancelled(attached.id)
        with pytest.raises(AgentCancelledError):
            context["token_factory"]()
        # An explicit invalid account must never gain the owner's identity.
        invalid = IndexJob(knowledge_base_id=KB, job_type="graph_rebuild", status="running", requested_by="deleted-account", options_json={})
        db.add(invalid)
        db.commit()
        with pytest.raises(Exception):
            service.attach_existing_job(invalid.id, "extraction")


@pytest.mark.parametrize("stopped", ["child_cancel", "parent_cancel", "parent_failed", "permission_revoked"])
def test_stopped_attached_run_cannot_consume_parent_budget(db_engine, monkeypatch, stopped):
    from app.core.database import SessionLocal
    from app.agents.contracts import AgentCancelledError
    setup_agent(monkeypatch, FakeAgent())
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        parent = service.create(AgentRunCreate(task="extraction"), "workflow-user")
        parent.status, parent.started_at = "running", datetime.utcnow()
        child = IndexJob(knowledge_base_id=KB, job_type="graph_rebuild", status="running",
                         requested_by="workflow-user", options_json={"agent_run_id": parent.id})
        db.add(child)
        db.commit()
        attached, context = service.attach_existing_job(child.id, "extraction")
        token = context["tool_token"]
        assert authorize_tool_token(db, token).id == attached.id
        if stopped == "child_cancel":
            attached.cancel_requested = True
        elif stopped == "parent_cancel":
            parent.cancel_requested = True
        elif stopped == "parent_failed":
            parent.status = "failed"
        else:
            db.get(User, "workflow-user").is_active = False
        db.commit()
        with pytest.raises(AgentCancelledError):
            context["before_request"]()
        db.refresh(parent)
        assert not parent.context_json.get("agent_budget")
        assert not parent.context_json.get("budget_exhausted")
        assert service.attached_cancelled(attached.id)
        with pytest.raises(PermissionError):
            authorize_tool_token(db, token)


def test_parent_cancel_while_waiting_preserves_cancelled_status(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    setup_agent(monkeypatch, FakeAgent())
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="ingestion", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "completed"
        assert run.status == "running" and run.progress_json["stage"] == "waiting_for_jobs"
        service.cancel(run.id, "workflow-user")
        db.execute(update(IndexJob).where(IndexJob.id.in_(run.output_json["followup_job_ids"])).values(status="cancelled"))
        db.commit()
        settled = service.get(run.id)
        assert settled.status == "cancelled" and settled.error_message is None


def test_timeline_preserves_two_digit_dates_calendar_validation_and_sort_order(db_engine):
    from app.core.database import SessionLocal
    from app.services.knowledge_workflows.analysis import graph_artifact
    with SessionLocal() as db:
        seed(db)
        chunk = db.get(KnowledgeChunk, "chunk-workflow")
        chunk.content = "2026-10-15发布；2026-2-03评审；2026年11月20日上线；2026-02-30错误日期；2026年3月仅月份。"
        db.commit()
        artifact = graph_artifact(db, SimpleNamespace(knowledge_base_id=KB, document_ids_json=["doc-workflow"]))
        timeline = artifact["timeline"]
        assert [item["normalized_date"] for item in timeline] == ["2026-02-03", "2026-03", "2026-10-15", "2026-11-20"]
        assert timeline[1]["precision"] == "month" and timeline[1]["date_text"] == "2026年3月"
        assert timeline[2]["date_text"] == "2026-10-15"


def test_retry_resumes_only_failed_followups_without_duplicate_index_jobs(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    setup_agent(monkeypatch, FakeAgent())
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        first = service.create(AgentRunCreate(task="ingestion", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(first.job_id)["status"] == "completed"
        child_id = first.output_json["followup_job_ids"][0]
        db.get(IndexJob, child_id).status = "failed"
        db.commit()
        assert service.get(first.id).status == "failed"
        retried = service.retry(first.id, "workflow-user")
        assert service.execute_for_job(retried.job_id)["status"] == "completed"
        assert retried.output_json["actions"][0]["status"] == "already_applied"
        assert retried.output_json["followup_job_ids"] == [child_id]
        assert len(list(db.scalars(select(IndexJob).where(IndexJob.job_type == "file")))) == 1
        assert db.get(IndexJob, child_id).status == "pending"
        assert finish_children(db, service, retried).status == "succeeded"


def test_mcp_neighbors_obey_effective_governance_overlays(db_engine):
    from app.core.database import SessionLocal
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
    with SessionLocal() as db:
        seed(db)
        run = SimpleNamespace(knowledge_base_id=KB, document_ids_json=["doc-workflow"], job_id=None, options_json={}, output_json={})
        assert call_tool(db, run, "graph_neighbors", {"entity_id": "entity-a"})
        KnowledgeGraphRepository(db, KB).create_correction(action="hide_relation", target_id="relation-ab", payload={},
                                                          reason="人工确认不应进入有效图谱", created_by="workflow-user")
        assert call_tool(db, run, "graph_neighbors", {"entity_id": "entity-a"}) == []


def test_unknown_total_tokens_are_not_inferred_from_input_output():
    usage = AgentWorkflowService.aggregate_usage([
        {"session_id": "a", "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": None, "reasoning_tokens": 4,
            "cache_read_tokens": 5, "cache_write_tokens": 6, "cost": None, "reported_cost": 0, "cost_note": "未定价"}},
        {"session_id": "b", "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 40, "reasoning_tokens": 1,
            "cache_read_tokens": 2, "cache_write_tokens": 3, "cost": None, "reported_cost": 0}},
    ])
    assert usage["total_tokens"] is None and usage["known_total_tokens"] == 40
    assert usage["total_tokens_coverage"] == "partial" and usage["reasoning_tokens"] == 5
    assert usage["cache_read_tokens"] == 7 and usage["cache_write_tokens"] == 9
    assert usage["cost"] is None and usage["cost_coverage"] == "unknown"


def test_failed_gold_retrieval_counts_zero_in_mean_recall(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.services.knowledge_workflows.execution import WorkflowExecutor
    def retrieve(question, **kwargs):
        if question == "failed":
            raise RuntimeError("unavailable")
        return {"sources": [{"chunk_id": "chunk-workflow", "document_id": "doc-workflow"}]}
    monkeypatch.setattr("app.services.rag_service.inspect_retrieval", retrieve)
    with SessionLocal() as db:
        seed(db)
        run = SimpleNamespace(knowledge_base_id=KB, document_ids_json=["doc-workflow"], options_json={
            "gold_cases": [{"question": question, "expected_document_ids": ["doc-workflow"]} for question in ("ok", "failed")]})
        result = WorkflowExecutor(db, run).evaluate()
        assert result["mean_recall"] == 0.5
        assert result["cases"][1]["recall"] == 0 and result["cases"][1]["mrr"] == 0


def test_incremental_baselines_ignore_previews_and_merge_actual_document_scopes(db_engine):
    from app.core.database import SessionLocal
    from app.services.knowledge_workflows.execution import WorkflowExecutor
    from datetime import timedelta
    with SessionLocal() as db:
        seed(db)
        def history(name, scope, versions, actions, *, apply=True, age=0):
            row = AgentRun(id=name, knowledge_base_id=KB, requested_by="workflow-user", task="incremental_maintenance",
                status="succeeded", document_ids_json=scope, options_json={"apply": apply}, context_json={"document_versions": versions},
                output_json={"actions": actions}, finished_at=datetime.utcnow() + timedelta(seconds=age))
            db.add(row)
        version_a = {"version": 1, "file_hash": "a1"}
        version_b = {"version": 2, "file_hash": "b2"}
        def action(identifiers, versions):
            return {"kind": "reextract_documents", "status": "applied", "result": {
                "completed_document_ids": identifiers, "document_versions": versions, "graph": {"succeeded": 1, "failed": 0}}}
        history("done-a", ["doc-workflow"], {"doc-workflow": version_a}, [action(["doc-workflow"], {"doc-workflow": version_a})])
        history("done-b", ["doc-b"], {"doc-b": version_b, "doc-workflow": {"version": 99}}, [action(["doc-b"], {"doc-b": version_b})], age=1)
        history("preview-a", ["doc-workflow"], {"doc-workflow": {"version": 99}}, [action(["doc-workflow"], {"doc-workflow": {"version": 99}})], apply=False, age=2)
        history("no-action-a", ["doc-workflow"], {"doc-workflow": {"version": 88}}, [], age=3)
        history("proposed-a", ["doc-workflow"], {"doc-workflow": {"version": 77}}, [{"kind": "reextract_documents", "status": "proposed"}], age=4)
        db.commit()
        run = SimpleNamespace(id="current", knowledge_base_id=KB, task="incremental_maintenance", document_ids_json=["doc-workflow", "doc-b"], options_json={})
        baselines = WorkflowExecutor(db, run).incremental_baselines(run.document_ids_json)
        assert baselines == {"doc-workflow": version_a, "doc-b": version_b}


def test_incremental_unchanged_requires_verified_completion_proof(db_engine):
    from app.core.database import SessionLocal
    from app.services.knowledge_workflows.execution import WorkflowExecutor
    with SessionLocal() as db:
        seed(db)
        version = {"version": 1, "file_hash": "v1"}
        historical = AgentRun(knowledge_base_id=KB, requested_by="workflow-user", task="incremental_maintenance", status="succeeded",
            document_ids_json=["doc-workflow"], options_json={"apply": True}, context_json={"document_versions": {"doc-workflow": version}},
            output_json={"actions": [{"kind": "reextract_documents", "status": "applied", "result": {"unchanged": True, "document_ids": []}}]},
            finished_at=datetime.utcnow())
        db.add(historical)
        db.commit()
        run = SimpleNamespace(id="current", knowledge_base_id=KB, task="incremental_maintenance", document_ids_json=["doc-workflow"], options_json={})
        executor = WorkflowExecutor(db, run)
        assert executor.incremental_baselines(["doc-workflow"]) == {}
        historical.output_json = {"actions": [{"kind": "reextract_documents", "status": "unchanged", "result": {
            "unchanged": True, "verified_document_ids": ["doc-workflow"], "document_versions": {"doc-workflow": version}}}]}
        db.commit()
        assert executor.incremental_baselines(["doc-workflow"]) == {"doc-workflow": version}


def test_generate_evaluation_candidates_without_running_comparison_and_reuse_as_candidates(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from jsonschema import Draft202012Validator
    agent = FakeAgent()
    original = agent.run
    def generate(request, **kwargs):
        result = original(request, **kwargs)
        Draft202012Validator.check_schema(request.output_schema)
        result.output["items"] = [{"kind": "evaluation_case", "question": "系统A依赖哪个系统？", "reference_answer": "系统B",
            "expected_document_ids": ["doc-workflow"], "expected_chunk_ids": ["chunk-workflow"],
            "evidence": [{"chunk_id": "chunk-workflow", "document_id": "doc-workflow", "quote": "系统A依赖系统B"}]}]
        return result
    monkeypatch.setattr(agent, "run", generate)
    setup_agent(monkeypatch, agent)
    def no_evaluation(*args, **kwargs):
        pytest.fail("Case generation without supplied questions must not run retrieval/comparison")
    monkeypatch.setattr("app.services.rag_service.inspect_retrieval", no_evaluation)
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="evaluation", options={"generate_cases": True, "max_generated_cases": 2}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "completed"
        candidate = run.output_json["generated_cases"][0]
        assert candidate["provenance"] == "agent_generated" and candidate["review_status"] == "candidate"
        assert candidate["human_review_required"] and candidate["evidence"][0]["quote"] == "系统A依赖系统B"
        assert run.output_json["metrics"]["evaluation"]["mode"] == "case_generation_only"
        assert run.output_json["metrics"]["evaluation"]["recall"] is None
        reused = service.create(AgentRunCreate(task="evaluation", options={"gold_cases": [{
            "question": candidate["question"], "reference_answer": candidate["reference_answer"],
            "expected_document_ids": candidate["expected_document_ids"], "expected_chunk_ids": candidate["expected_chunk_ids"],
            "provenance": "agent_generated", "source_run_id": run.id, "candidate_id": candidate["id"]}]}), "workflow-user")
        assert reused.options_json["gold_cases"][0]["provenance"] == "agent_generated"


def test_generated_cases_reject_fabricated_evidence_and_count_limits(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AgentRunCreate(task="evaluation", options={"generate_cases": True, "max_generated_cases": 21})
    agent = FakeAgent()
    original = agent.run
    def fabricate(request, **kwargs):
        result = original(request, **kwargs)
        result.output["items"] = [{"kind": "evaluation_case", "question": "不实问题", "reference_answer": "不实答案",
            "expected_document_ids": ["doc-workflow"], "expected_chunk_ids": ["chunk-workflow"],
            "evidence": [{"chunk_id": "chunk-workflow", "quote": "原文中不存在的证据"}]}]
        return result
    monkeypatch.setattr(agent, "run", fabricate)
    setup_agent(monkeypatch, agent)
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="evaluation", options={"generate_cases": True}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "failed"
        assert not run.output_json.get("generated_cases")
