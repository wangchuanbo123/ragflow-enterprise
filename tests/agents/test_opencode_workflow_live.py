"""Canonical V2 + local model + real ASGI MCP/JWT/DB + an authorized write.

Only the model answers are synthetic. The shared public fixtures provide an
isolated SQLite database; neither cloud models nor the user's data are used.
"""

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import select

from app.agents.opencode import OpenCodeAgentBackend
from app.agents.runtime import project_runtime_executable, runtime_environment
from app.agents.settings import AgentSettings, OPENCODE_VERSION
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.models.document_management import DocumentTag, DocumentTagLink
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.services.agent_workflow_service import AgentWorkflowService


def test_real_v2_workflow_uses_scoped_gateway_and_applies_verified_tag(client, tmp_path, monkeypatch):
    try:
        executable = project_runtime_executable()
    except FileNotFoundError:
        pytest.skip("Project source-built OpenCode runtime is not prepared")
    from app.core import config
    from app.core.database import SessionLocal

    document_id, chunk_id, foreign_id = "v2-live-document", "v2-live-chunk", "v2-out-of-scope"
    quote, tag_name = "关键器件由单一供应商提供。", "V2全链路验证"
    with SessionLocal() as db:
        db.add_all([
            KnowledgeDocument(id=document_id, knowledge_base_id=KB, source_path="live/供应协议.md",
                original_filename="供应协议.md", file_hash="live-hash", file_size=100, status="ready", chunk_count=1),
            KnowledgeDocument(id=foreign_id, knowledge_base_id=KB, source_path="live/范围外.md",
                original_filename="范围外.md", file_hash="foreign-hash", file_size=100, status="ready", chunk_count=1),
            KnowledgeChunk(id=chunk_id, knowledge_base_id=KB, document_id=document_id, file_hash="live-hash",
                chunk_index=0, content=quote, content_hash="live-content"),
            KnowledgeChunk(id="v2-foreign-chunk", knowledge_base_id=KB, document_id=foreign_id, file_hash="foreign-hash",
                chunk_index=0, content="范围外机密文本绝不能发送给模型。", content_hash="foreign-content"),
        ])
        db.commit()

    model_calls, gateway_calls, failures, tokens = [], [], [], []
    scope_denials = []
    output = {"summary": "已核对原文，为授权文档添加供应风险分类。", "items": [{"finding": "单一供应来源风险"}],
        "evidence": [{"chunk_id": chunk_id, "document_id": document_id, "quote": quote}],
        "actions": [{"kind": "tag_documents", "document_ids": [document_id], "params": {"tags": [tag_name]},
                     "reason": "依据已核对的原文进行分类"}], "diagnostics": [], "metrics": {"checked_chunks": 1}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, status=200, mime="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.send_error(405)

        def do_POST(self):
            try:
                self.handle_post()
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                self.respond(b'{"error":{"message":"synthetic fixture failed"}}', 500)

        def handle_post(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/mcp":
                authorization = self.headers.get("Authorization", "")
                assert authorization.startswith("Bearer eyJ")
                token = authorization.removeprefix("Bearer ")
                if token not in tokens:
                    tokens.append(token)
                response = client.post("/api/v1/agent-tools", json=data, headers={"Authorization": authorization})
                gateway_calls.append({"method": data.get("method"), "params": data.get("params"),
                                      "status": response.status_code, "result": response.json() if response.content else None})
                assert response.status_code in {200, 204}, response.text
                if data.get("method") == "tools/call" and data["params"]["name"] == "read_chunks":
                    denied = client.post("/api/v1/agent-tools", json={"jsonrpc": "2.0", "id": "outside", "method": "tools/call",
                        "params": {"name": "read_chunks", "arguments": {"document_id": foreign_id}}},
                        headers={"Authorization": authorization})
                    assert denied.status_code == 200 and denied.json()["error"]["code"] == -32602
                    invalid = client.post("/api/v1/agent-tools", json={"jsonrpc": "2.0", "id": "invalid", "method": "tools/list"},
                        headers={"Authorization": authorization + "tampered"})
                    assert invalid.status_code == 401
                    scope_denials.append(True)
                self.respond(response.content, response.status_code)
                return

            assert self.path == "/v1/chat/completions"
            assert self.headers.get("Authorization") == "Bearer synthetic"
            assert all(tool["function"]["name"].startswith("knowledge_") for tool in data.get("tools", []))
            model_calls.append(data)
            tool_results = [message["content"] for message in data.get("messages", []) if message.get("role") == "tool"]
            assert "范围外机密文本" not in json.dumps(data, ensure_ascii=False)
            selected = "knowledge_get_schema" if not tool_results else "knowledge_read_chunks" if len(tool_results) == 1 else None
            if selected:
                arguments = {} if selected == "knowledge_get_schema" else {"document_id": document_id, "limit": 10}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call-{len(tool_results)}", "type": "function",
                    "function": {"name": selected, "arguments": json.dumps(arguments)}}]}
            else:
                assert chunk_id in json.dumps(tool_results, ensure_ascii=False)
                assert quote in json.dumps(tool_results, ensure_ascii=False)
                delta = {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)}
            usage = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
            finish = "tool_calls" if selected else "stop"
            if data.get("stream"):
                chunks = [{"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "test", "usage": usage,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
                payload = ("".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n").encode()
                self.respond(payload, mime="text/event-stream")
            else:
                for call in delta.get("tool_calls", []):
                    call.pop("index", None)
                self.respond(json.dumps({"id": "synthetic", "object": "chat.completion", "created": 1, "model": "test",
                    "choices": [{"index": 0, "message": delta, "finish_reason": finish}], "usage": usage}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    monkeypatch.setattr(config, "ANSWER_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(config, "ANSWER_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(config, "ANSWER_LLM_API_KEY", "synthetic")
    monkeypatch.setattr(config, "ANSWER_LLM_MODEL", "test")
    monkeypatch.setenv("OPENCODE_MODEL", "rag/test")
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", "{}")
    env = runtime_environment(tmp_path / "runtime", "synthetic-server-password")
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    backend = OpenCodeAgentBackend(AgentSettings(base_url=f"http://127.0.0.1:{port}", password="synthetic-server-password",
        model="rag/test", callback_url=f"http://127.0.0.1:{server.server_port}/mcp", workspace_root=workspace, timeout_seconds=45))
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile_name=None: backend)
    process = None
    try:
        with (tmp_path / "opencode.log").open("wb") as log:
            process = subprocess.Popen([executable, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=tmp_path / "runtime" / "home", env=env, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            if backend.health()["status"] == "ok":
                break
            time.sleep(0.2)
        assert backend.health()["status"] == "ok", (tmp_path / "opencode.log").read_text(encoding="utf-8", errors="replace")[-5000:]
        assert backend.identity()["version"] == OPENCODE_VERSION == "2.2.5"
        assert client.post("/api/v1/auth/login", json={"username": config.INITIAL_ADMIN_USERNAME,
            "password": config.INITIAL_ADMIN_PASSWORD}).status_code == 200
        base = f"/api/v1/knowledge-bases/{KB}/agent-runs"
        response = client.post(base, json={"task": "knowledge_organization", "instructions": "核对原文后只给所选文档添加分类。",
            "document_ids": [document_id], "options": {"apply": True, "allowed_actions": ["tag_documents"],
                "max_actions": 1, "max_runtime_seconds": 60, "max_agent_calls": 2}})
        assert response.status_code == 202, response.text
        created = response.json()
        with SessionLocal() as db:
            result = AgentWorkflowService(db, KB).execute_for_job(created["job_id"])
            assert result["status"] == "completed", {"result": result, "fixture_errors": failures,
                "run": AgentWorkflowService(db, KB).serialize(AgentWorkflowService(db, KB).get(created["id"]))}
            tag = db.scalar(select(DocumentTag).where(DocumentTag.knowledge_base_id == KB, DocumentTag.name == tag_name))
            assert tag is not None
            assert list(db.scalars(select(DocumentTagLink.document_id).where(DocumentTagLink.tag_id == tag.id))) == [document_id]
        detail = client.get(base + "/" + created["id"]).json()
        assert detail["status"] == "succeeded" and detail["backend"] == "opencode"
        assert detail["output"]["actions"][0]["status"] == "applied"
        assert detail["output"]["evidence"][0]["quote"] == quote
        assert detail["usage"]["input_tokens"] == 30 and detail["usage"]["output_tokens"] == 12
        assert detail["usage"]["total_tokens"] is None and detail["usage"]["cost"] is None
        assert len(detail["usage"]["sessions"]) == 1
        assert {event["type"] for event in detail["events"]} >= {"tool", "step", "usage"}
        assert len(model_calls) == 3 and not failures
        assert [item["params"]["name"] for item in gateway_calls if item["method"] == "tools/call"] == ["get_schema", "read_chunks"]
        assert scope_denials and len(tokens) == 1
        assert tokens[0] not in json.dumps(detail, ensure_ascii=False)
        assert client.post("/api/v1/agent-tools", json={"jsonrpc": "2.0", "id": "ended", "method": "tools/list"},
            headers={"Authorization": "Bearer " + tokens[0]}).status_code == 401
        with httpx.Client(base_url=backend.settings.base_url, auth=("opencode", "synthetic-server-password"), trust_env=False) as probe:
            assert probe.get("/api/session/" + detail["session_id"]).status_code == 404
            assert not any(str(workspace) in str(location) for location in probe.get("/api/debug/location").json())
        assert not list(workspace.iterdir())
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
