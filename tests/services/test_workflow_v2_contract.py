"""Exercise the actual workflow and V2 adapter together at the HTTP boundary."""

from datetime import datetime
import json

import httpx
import pytest

from app.agents.opencode import OpenCodeAgentBackend
from app.agents.settings import AgentSettings, OPENCODE_VERSION
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.schemas.agent_run import AgentRunCreate
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.knowledge_workflows.catalog import CATALOG
from tests.services.test_agent_workflows import seed


@pytest.mark.parametrize("task", list(CATALOG))
def test_workflow_reaches_native_v2_with_parent_deadline_and_unknown_totals(db_engine, monkeypatch, tmp_path, task):
    from app.core.database import SessionLocal

    sessions, prompts, calls = {}, [], []
    evidence = [{"chunk_id": "chunk-workflow", "document_id": "doc-workflow", "quote": "系统A依赖系统B"}]

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": OPENCODE_VERSION})
        if request.url.path == "/api/session":
            identifier = f"session-{len(sessions)}"
            sessions[identifier] = None
            return httpx.Response(200, json={"data": {"id": identifier}})
        if request.url.path == "/api/mcp":
            return httpx.Response(200, json={"data": [{"name": "knowledge", "status": {"status": "connected"}}]})
        if request.url.path.endswith("/prompt"):
            payload = json.loads(body["text"])
            assert datetime.fromisoformat(payload["context"]["deadline_at"]).utcoffset().total_seconds() == 0
            prompts.append(payload)
            output = {"answer": "系统A依赖系统B[chunk-workflow]", "evidence": evidence} if payload["context"]["mode"] == "answer_comparison" else {
                "summary": "已核对系统依赖", "items": [{"finding": "系统A依赖系统B"}],
                "evidence": evidence, "actions": [], "diagnostics": [], "metrics": {}}
            sessions[request.url.path.split("/")[3]] = output
        if request.method == "GET" and request.url.path.endswith("/message"):
            identifier = request.url.path.split("/")[3]
            return httpx.Response(200, json={"data": [{"id": "answer", "type": "assistant", "finish": "stop",
                "time": {"completed": 1}, "model": {"providerID": "rag", "id": "test"},
                "tokens": {"input": 10, "output": 4, "reasoning": 2, "cache": {"read": 3, "write": 1}}, "cost": 0,
                "content": [{"id": "tool", "type": "tool", "name": "knowledge_get_schema", "state": {"status": "completed"}},
                            {"type": "text", "text": json.dumps(sessions[identifier])}]},
                {"id": "idle", "type": "idle", "outcome": "succeeded"}], "cursor": {}})
        return httpx.Response(200, json=True)

    backend = OpenCodeAgentBackend(AgentSettings(workspace_root=tmp_path, model="rag/test", poll_seconds=0.01),
                                   transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile_name=None: backend)
    monkeypatch.setattr("app.agents.factory.list_backend_profiles", lambda: ["default"])
    monkeypatch.setattr("app.services.rag_service.inspect_retrieval", lambda *args, **kwargs: {
        "sources": [{"chunk_id": "chunk-workflow", "document_id": "doc-workflow"}]})
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        options = {"apply": False, "allowed_actions": [], "max_runtime_seconds": 60}
        if task == "evaluation":
            options["questions"] = ["系统A依赖谁？"]
        run = service.create(AgentRunCreate(task=task, document_ids=["doc-workflow"], options=options), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "completed", service.serialize(run)
        detail = service.serialize(run)
        assert detail["status"] == "succeeded" and detail["backend"] == "opencode"
        assert detail["usage"]["total_tokens"] is None and detail["usage"]["total_tokens_coverage"] == "unknown"
        assert detail["usage"]["cost"] is None and detail["usage"]["cost_coverage"] == "unknown"
        assert detail["usage"]["reasoning_tokens"] == (4 if task == "evaluation" else 2)
        assert {event["type"] for event in detail["events"]} >= {"tool", "step", "usage"}
        assert all(prompt["context"]["deadline_at"] == prompts[0]["context"]["deadline_at"] for prompt in prompts)
    assert any(method == "DELETE" and path.startswith("/api/session/") for method, path in calls)
    assert not list(tmp_path.iterdir())
