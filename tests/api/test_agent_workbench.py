"""User authorization, run lifecycle and MCP transport boundaries."""

from datetime import datetime
import pytest
from sqlalchemy import select

from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.core.security import hash_password
from app.models.agent_run import AgentRun
from app.models.user import User
from app.models.knowledge_base import KnowledgeBaseMember
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_chunk import KnowledgeChunk
from app.services.knowledge_workflows.tools import create_tool_token


def login(client, username=INITIAL_ADMIN_USERNAME, password=INITIAL_ADMIN_PASSWORD):
    assert client.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code == 200


def test_api_role_scope_and_cancel(client):
    from app.core.database import SessionLocal
    login(client)
    base = f"/api/v1/knowledge-bases/{KB}"
    catalog = client.get(base + "/agent-task-catalog")
    assert catalog.status_code == 200 and len(catalog.json()) == 9
    assert "runtime_profiles" in catalog.json()[0]["options"]
    assert all(item["instructions"] for item in catalog.json())
    assert client.post(base + "/agent-runs", json={"task": "operations", "options": {"profile": "deep"}}).status_code == 422
    created = client.post(base + "/agent-runs", json={"task": "operations", "instructions": "诊断"})
    assert created.status_code == 202
    run = created.json()
    assert run["status"] == "queued" and run["events"][0]["type"] == "queued"
    assert client.post(base + f"/agent-runs/{run['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(base + f"/agent-runs/{run['id']}/retry").status_code == 202
    with SessionLocal() as db:
        viewer = User(username="agent-viewer", password_hash=hash_password("Agent-viewer-123"), display_name="Viewer", role="user")
        db.add(viewer)
        db.flush()
        db.add(KnowledgeBaseMember(knowledge_base_id=KB, user_id=viewer.id, role="viewer"))
        db.commit()
    login(client, "agent-viewer", "Agent-viewer-123")
    assert client.get(base + "/agent-runs").status_code == 200
    assert client.get(base + f"/agent-runs/{run['id']}").status_code == 200
    assert client.post(base + "/agent-runs", json={"task": "operations"}).status_code == 403
    assert client.post(base + f"/agent-runs/{run['id']}/cancel").status_code == 403


def test_agent_run_filters_apply_before_pagination(client):
    from app.core.database import SessionLocal
    login(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == INITIAL_ADMIN_USERNAME))
        db.add_all([AgentRun(id=f'page-{index:03}', knowledge_base_id=KB, requested_by=user.id,
                            task='business_analysis' if index % 2 else 'operations',
                            status='failed' if index % 3 else 'succeeded', instructions='',
                            document_ids_json=[], options_json={}, context_json={}, output_json={},
                            usage_json={}, progress_json={}, created_at=datetime(2026, 1, 1))
                    for index in range(125)])
        db.commit()
    base = f'/api/v1/knowledge-bases/{KB}/agent-runs/page'
    response = client.get(base, params={'task': 'business_analysis', 'status': 'failed', 'offset': 10, 'limit': 7})
    assert response.status_code == 200
    body = response.json()
    expected = [f'page-{index:03}' for index in range(125) if index % 2 and index % 3]
    assert body['total'] == len(expected)
    assert [run['id'] for run in body['items']] == expected[10:17]
    assert body['offset'] == 10 and body['limit'] == 7
    assert all(run['task'] == 'business_analysis' and run['status'] == 'failed' for run in body['items'])
    assert client.get(base, params={'status': 'invented'}).status_code == 422
    assert client.get(base, params={'task': 'invented'}).status_code == 422


def test_mcp_initialize_read_scope_and_token_invalidation(client):
    from app.core.database import SessionLocal
    login(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == INITIAL_ADMIN_USERNAME))
        db.add_all([KnowledgeDocument(id="mcp-doc", knowledge_base_id=KB, source_path="mcp-doc/doc.txt", original_filename="doc.txt", file_hash="h", file_size=1),
                    KnowledgeChunk(id="mcp-chunk", knowledge_base_id=KB, document_id="mcp-doc", file_hash="h", chunk_index=0, content="allowed evidence", content_hash="hash")])
        run = AgentRun(knowledge_base_id=KB, requested_by=user.id, task="business_analysis", status="running", instructions="",
            document_ids_json=["mcp-doc"], options_json={}, context_json={}, output_json={}, usage_json={},
            progress_json={"stage": "agent_planning"}, started_at=datetime.utcnow())
        db.add(run)
        db.commit()
        run_id, token = run.id, create_tool_token(run)
    assert client.post("/api/v1/agent-tools", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).status_code == 401
    headers = {"Authorization": "Bearer " + token}
    result = client.post("/api/v1/agent-tools", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert result.status_code == 200 and result.json()["result"]["capabilities"]["tools"]
    tool_list = client.post("/api/v1/agent-tools", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    assert all(tool["annotations"]["readOnlyHint"] for tool in tool_list["result"]["tools"])
    args = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_chunks", "arguments": {"document_id": "mcp-doc"}}}
    assert "allowed evidence" in client.post("/api/v1/agent-tools", headers=headers, json=args).text
    args["params"]["arguments"]["document_id"] = "foreign-doc"
    denied = client.post("/api/v1/agent-tools", headers=headers, json=args).json()
    assert denied["error"]["code"] == -32602
    with SessionLocal() as db:
        db.get(AgentRun, run_id).status = "succeeded"
        db.commit()
    assert client.post("/api/v1/agent-tools", headers=headers, json=args).status_code == 401
