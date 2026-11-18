"""Real pinned codecode binary + local synthetic model/MCP, no cloud calls."""

import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.agents.contracts import AgentRequest, AgentCancelledError, AgentError
from app.agents.codecode import CodeCodeAgentBackend
from app.agents.runtime import project_runtime_executable, runtime_environment
from app.agents.settings import AgentSettings


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_real_opencode_structured_output_and_mcp(tmp_path, api, monkeypatch):
    try:
        executable = project_runtime_executable()
    except FileNotFoundError:
        pytest.skip("Project source-built codecode runtime is not prepared")
    tool_calls = []
    model_calls = []
    message_snapshots = []
    tokens = {"synthetic-tool-token": "default", "synthetic-token-a": "scope-a", "synthetic-token-b": "scope-b"}
    concurrent_tools = threading.Barrier(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_error(405)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/mcp":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                assert token in tokens
                method = data.get("method")
                if method == "notifications/initialized":
                    self.send_response(202); self.end_headers(); return
                if method == "initialize":
                    result = {"protocolVersion": data["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "synthetic-knowledge", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "description": "Get authorized knowledge", "inputSchema": {"type": "object", "properties": {}}}
                                        for name in ("get_schema", "read_chunks")]}
                elif method == "tools/call":
                    tool = data["params"]["name"]
                    tool_calls.append({"tool": tool, "scope": tokens[token]})
                    # Both scopes must have a live MCP connection at the same
                    # time; shared directory state would swap their credentials.
                    if tool == "get_schema" and tokens[token] != "default":
                        concurrent_tools.wait(timeout=10)
                    result = {"content": [{"type": "text", "text": json.dumps({"entity_types": ["SYSTEM"], "authorized_scope": tokens[token],
                              **({"chunks": [{"id": "synthetic-chunk", "text": "Public synthetic text"}]} if tool == "read_chunks" else {})})}]}
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": data.get("id"), "result": result}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                return
            model_calls.append(data)
            expected_path = "/v1/messages" if api == "anthropic" else "/v1/chat/completions"
            assert self.path == expected_path
            names = [item["name"] if api == "anthropic" else item["function"]["name"] for item in data.get("tools", [])]
            assert set(names) == {"knowledge_get_schema", "knowledge_read_chunks"}, "Session prompt must await its scoped MCP catalog"
            if api == "anthropic":
                assert self.headers.get("x-api-key") == "synthetic"
            else:
                assert self.headers.get("Authorization") == "Bearer synthetic"
            tool_results, task_payload = [], {}
            for message in data.get("messages", []):
                content = message.get("content")
                if message.get("role") == "tool":
                    tool_results.append(content)
                if isinstance(content, list):
                    tool_results.extend(part.get("content") for part in content if part.get("type") == "tool_result")
                    text_parts = [part.get("text", "") for part in content if part.get("type") == "text"]
                else:
                    text_parts = [content] if isinstance(content, str) else []
                for text in text_parts:
                    try:
                        value = json.loads(text)
                        if isinstance(value, dict) and "task" in value:
                            task_payload = value
                    except ValueError:
                        pass
            scenario = task_payload.get("context", {}).get("scenario")
            if scenario == "provider_error" and tool_results:
                payload = b'{"error":{"type":"invalid_request_error","message":"synthetic-secret-must-not-leak"}}'
                self.send_response(400); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                return
            selected = "knowledge_get_schema" if not tool_results else "knowledge_read_chunks" if len(tool_results) == 1 else None
            scope = "default"
            if tool_results:
                content = tool_results[-1]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
                scope = json.loads(content)["authorized_scope"]
            final_json = json.dumps({"verified": True, **({"scope": scope} if scenario == "concurrent" else {})})
            delta = {"role": "assistant", "content": final_json}
            if selected:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call-{len(tool_results)}", "type": "function", "function": {"name": selected, "arguments": "{}"}}]}
            if api == "anthropic":
                block = {"type": "tool_use", "id": f"call-{len(tool_results)}", "name": selected, "input": {}} if selected else {"type": "text", "text": ""}
                block_delta = {"type": "input_json_delta", "partial_json": "{}"} if selected else {"type": "text_delta", "text": final_json}
                events = [
                    {"type": "message_start", "message": {"id": "synthetic", "type": "message", "role": "assistant", "model": "test", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                    {"type": "content_block_start", "index": 0, "content_block": block},
                    {"type": "content_block_delta", "index": 0, "delta": block_delta},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use" if selected else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}},
                    {"type": "message_stop"},
                ]
                payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                mime = "text/event-stream"
            elif data.get("stream"):
                events = [
                    {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "synthetic", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "synthetic", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if selected else "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
                ]
                payload = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
                mime = "text/event-stream"
            else:
                message = {key: value for key, value in delta.items()}
                for call in message.get("tool_calls", []):
                    call.pop("index", None)
                payload = json.dumps({"id": "synthetic", "object": "chat.completion", "created": 1, "model": "synthetic", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if selected else "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}).encode()
                mime = "application/json"
            self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    from app.core import config
    monkeypatch.setattr(config, "ANSWER_LLM_PROVIDER", "anthropic_compatible" if api == "anthropic" else "openai_compatible")
    monkeypatch.setattr(config, "ANSWER_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(config, "ANSWER_LLM_API_KEY", "synthetic")
    monkeypatch.setattr(config, "ANSWER_LLM_MODEL", "test")
    monkeypatch.setenv("CODECODE_MODEL", "rag/test")
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", "{}")
    env = runtime_environment(tmp_path / "runtime", "synthetic-server-password")
    home = tmp_path / "runtime" / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    # Project and ancestor configuration must not alter the managed runtime.
    (workspace / "opencode.json").write_text(json.dumps({"providers": {"rag": {"settings": {"baseURL": "http://127.0.0.1:1/untrusted"}}},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]}), encoding="utf-8")
    with (tmp_path / "opencode.log").open("wb") as log:
        process = subprocess.Popen([executable, "serve", "--port", str(port), "--hostname", "127.0.0.1"], cwd=home, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    backend = CodeCodeAgentBackend(AgentSettings(base_url=f"http://127.0.0.1:{port}", password="synthetic-server-password", model="rag/test", callback_url=f"http://127.0.0.1:{server.server_port}/mcp", workspace_root=workspace, timeout_seconds=40))
    native_messages = backend._messages
    def messages(send, path):
        rows = native_messages(send, path)
        message_snapshots.append({"session": path, "messages": [{key: item.get(key) for key in ("id", "type", "finish", "time", "outcome")} for item in rows]})
        return rows
    backend._messages = messages
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            if backend.health()["status"] == "ok":
                break
            time.sleep(0.2)
        assert backend.health()["status"] == "ok", (tmp_path / "opencode.log").read_text(encoding="utf-8", errors="replace")[-5000:]
        schema = {"type": "object", "properties": {"verified": {"type": "boolean"}}, "required": ["verified"], "additionalProperties": False}
        def request(scenario="normal", token="synthetic-tool-token"):
            output_schema = schema if scenario != "concurrent" else {**schema, "properties": {**schema["properties"], "scope": {"type": "string"}}, "required": ["verified", "scope"]}
            return AgentRequest(task="quality_review", knowledge_base_id="synthetic-kb", instructions="Call get_schema, then read_chunks, then return verified=true and any requested authorized scope.",
                                context={"scenario": scenario}, output_schema=output_schema, tool_token=token)
        events = []
        result = backend.run(request(), on_event=events.append)
        assert result.output == {"verified": True}
        assert [item["tool"] for item in tool_calls] == ["get_schema", "read_chunks"]
        assert len(model_calls) == 3
        assert result.usage["input_tokens"] == 30 and result.usage["output_tokens"] == 12
        assert result.usage["total_tokens"] is None and result.usage["cost"] is None
        assert len([event for event in events if event["type"] == "usage"]) == 1
        sessions = [result.session_id]
        for round_number in range(3):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(backend.run, request("concurrent", token)) for token in ("synthetic-token-a", "synthetic-token-b")]
                # Includes the adapter's 40s execution deadline and bounded
                # cleanup; this is a harness deadline, not a weaker run budget.
                results = [future.result(timeout=50) for future in futures]
            assert [item.output["scope"] for item in results] == ["scope-a", "scope-b"]
            assert len({item.session_id for item in results}) == 2
            sessions.extend(item.session_id for item in results)
            expected = ["get_schema", "read_chunks"] * (round_number + 1)
            assert [item["tool"] for item in tool_calls if item["scope"] == "scope-a"] == expected
            assert [item["tool"] for item in tool_calls if item["scope"] == "scope-b"] == expected
        failed_events = []
        with pytest.raises(AgentError) as caught:
            backend.run(request("provider_error"), on_event=failed_events.append)
        assert "synthetic-secret-must-not-leak" not in str(caught.value)
        usage = [event for event in failed_events if event["type"] == "usage"]
        assert usage and usage[0]["usage"]["input_tokens"] >= 10
        sessions.extend(event["session_id"] for event in failed_events if event["type"] == "session")
        cancel = threading.Event()
        cancelled_events = []
        def on_cancel_event(event):
            cancelled_events.append(event)
            if event["type"] == "step":
                cancel.set()
        with pytest.raises(AgentCancelledError):
            backend.run(request(), cancelled=cancel.is_set, on_event=on_cancel_event)
        assert any(event["type"] == "usage" for event in cancelled_events)
        sessions.extend(event["session_id"] for event in cancelled_events if event["type"] == "session")
        with httpx.Client(base_url=backend.settings.base_url, auth=("opencode", "synthetic-server-password"), trust_env=False) as client:
            assert all(client.get("/api/session/" + session).status_code == 404 for session in sessions)
            loaded = client.get("/api/debug/location").json()
            assert not any(str(workspace) in str(location) for location in loaded)
        assert [path.name for path in workspace.iterdir()] == ["opencode.json"]
    finally:
        (tmp_path / "synthetic-diagnostics.json").write_text(json.dumps({"tool_calls": tool_calls, "model_calls": model_calls, "message_snapshots": message_snapshots[-200:]}, ensure_ascii=False), encoding="utf-8")
        process.terminate()
        process.wait(timeout=10)
        server.shutdown(); server.server_close(); thread.join(timeout=2)
