import json
from dataclasses import replace

import httpx
import pytest

from app.agents.contracts import AgentRequest, AgentOutputError, AgentCancelledError, AgentUnavailableError
from app.agents.opencode import OpenCodeAgentBackend
from app.agents.settings import AgentSettings, OPENCODE_VERSION, profile_settings


SCHEMA = {"type": "object", "properties": {"verified": {"type": "boolean"}}, "required": ["verified"], "additionalProperties": False}


def request(**kwargs):
    fields = dict(task="quality_review", knowledge_base_id="kb-test", instructions="Review evidence", context={}, output_schema=SCHEMA)
    return AgentRequest(**{**fields, **kwargs})


def adapter(tmp_path, output=None, version=OPENCODE_VERSION, error=None):
    calls = []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        if req.url.path == "/api/info":
            data = {"version": version}
        elif req.url.path == "/api/session":
            data = {"data": {"id": "ses_test"}}
        elif req.url.path == "/api/mcp":
            data = {"location": {"directory": req.url.params["location[directory]"]},
                    "data": [{"name": "knowledge", "status": {"status": "connected"}}]}
        elif req.method == "GET" and req.url.path.endswith("/message"):
            info = {"id": "msg_assistant1", "type": "assistant", "time": {"completed": 1}, "finish": "stop",
                    "model": {"providerID": "rag", "id": "test"},
                    "tokens": {"input": 10, "output": 4}, "cost": 0.01,
                    "content": [{"type": "tool", "id": "tool-1", "name": "knowledge_get_schema", "state": {"status": "completed"}},
                                {"type": "text", "text": json.dumps({"verified": True} if output is None else output)}]}
            if error:
                info["error"] = {"type": error, "status": 503, "message": "never expose raw upstream secret",
                                 "response": {"body": "never expose response body"}}
            data = {"data": [info, {"id": "msg_idle", "type": "idle", "outcome": "succeeded"}], "cursor": {}}
        else:
            data = True
        return httpx.Response(200, json=data)

    instance = OpenCodeAgentBackend(AgentSettings(workspace_root=tmp_path, model="rag/test", poll_seconds=0.01), transport=httpx.MockTransport(handler))
    return instance, calls


def test_adapter_validates_structured_output_and_scopes_permissions(tmp_path):
    instance, calls = adapter(tmp_path)
    events = []
    result = instance.run(request(tool_token="synthetic-token"), on_event=events.append)
    assert result.output == {"verified": True}
    assert result.backend == "opencode" and result.usage["input_tokens"] == 10
    create = next(body for method, path, body in calls if method == "POST" and path == "/api/session")
    assert create["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "knowledge_*", "resource": "*", "effect": "allow"},
    ]
    assert create["model"] == {"providerID": "rag", "id": "test"}
    assert any(path == "/api/experimental/mcp/knowledge" and method == "PUT" and body["config"]["oauth"] is False
               and body["config"]["codemode"] is False for method, path, body in calls)
    assert any(path.endswith("/prompt") and json.loads(body["text"])["output_schema"] == SCHEMA for _, path, body in calls)
    assert any(path == "/api/experimental/mcp/knowledge" and method == "DELETE" for method, path, _ in calls)
    assert all(path.startswith("/api/") for _, path, _ in calls)
    assert any(method == "DELETE" for method, _, _ in calls)
    assert not list(tmp_path.iterdir())


def test_rejects_invalid_output_and_aborts(tmp_path):
    instance, calls = adapter(tmp_path, output={"verified": "not a boolean"})
    with pytest.raises(AgentOutputError):
        instance.run(request())
    assert any(path.endswith("/interrupt") for _, path, _ in calls)


def test_failed_abort_still_disconnects_and_disposes(tmp_path):
    instance, calls = adapter(tmp_path, output={"verified": "invalid"})
    original = instance._request

    def fail_abort(client, method, path, **kwargs):
        if path.endswith("/interrupt"):
            raise AgentUnavailableError("synthetic abort failure")
        return original(client, method, path, **kwargs)

    instance._request = fail_abort
    with pytest.raises(AgentOutputError):
        instance.run(request(tool_token="synthetic-token"))
    assert any(path == "/api/experimental/mcp/knowledge" and method == "DELETE" for method, path, _ in calls)
    assert any(method == "DELETE" for method, _, _ in calls)
    assert any(path == "/api/debug/location" for _, path, _ in calls)
    assert not list(tmp_path.iterdir())


def test_cancellation_does_not_submit_model_request(tmp_path):
    instance, calls = adapter(tmp_path)
    with pytest.raises(AgentCancelledError):
        instance.run(request(), cancelled=lambda: True)
    assert calls == []


def test_version_mismatch_fails_explicitly(tmp_path):
    instance, calls = adapter(tmp_path, version="0.0.0-incompatible")
    assert instance.health()["status"] == "fail"
    with pytest.raises(AgentUnavailableError):
        instance.run(request())
    assert not any(path == "/api/session" for _, path, _ in calls)


def test_does_not_expose_raw_provider_error(tmp_path):
    instance, calls = adapter(tmp_path, error="ProviderAuthError")
    with pytest.raises(Exception) as caught:
        instance.run(request())
    assert "never expose" not in str(caught.value)
    assert caught.value.status_code == 503


def test_profiles_are_server_managed_and_url_credentials_rejected(monkeypatch):
    monkeypatch.setenv("OPENCODE_MODEL", "")
    monkeypatch.setenv("AGENT_TOOL_URL", "")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", '{"backup":{"model":"rag/backup"}}')
    assert profile_settings("backup").model == "rag/backup"
    with pytest.raises(ValueError):
        profile_settings("unregistered")
    with pytest.raises(ValueError):
        AgentSettings(base_url="http://username:secret@example.com")


def test_runtime_does_not_inherit_application_secrets(tmp_path, monkeypatch):
    from app.agents.runtime import runtime_environment

    monkeypatch.setenv("UNRELATED_SECRET", "never pass this")
    environment = runtime_environment(tmp_path, "synthetic-password")
    assert "UNRELATED_SECRET" not in environment
    assert "JWT_SECRET_KEY" not in environment
    assert environment["OPENCODE_CONFIG_PROJECT_DISABLE"] == "true"
    assert "OPENCODE_PURE" not in environment
    config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert config["permissions"][0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert config["agents"]["knowledge"]["steps"] <= 64


def test_replacement_backend_does_not_start_opencode(monkeypatch):
    from app.agents.runtime import start_managed_opencode
    from app.core import config

    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setenv("AGENT_BACKEND", "replacement")
    monkeypatch.setenv("OPENCODE_EXECUTABLE", "must-not-be-invoked")
    assert start_managed_opencode() == {"status": "disabled"}


def test_bad_executable_reports_failure_without_stopping_application(monkeypatch):
    from app.agents import runtime
    from app.core import config

    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setenv("AGENT_BACKEND", "opencode")
    monkeypatch.setenv("OPENCODE_AUTO_START", "true")
    monkeypatch.setenv("OPENCODE_EXECUTABLE", "synthetic-missing-executable")
    monkeypatch.setattr(runtime, "_process", None)
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("synthetic-missing-executable")
    monkeypatch.setattr(runtime.subprocess, "run", unavailable)
    result = runtime.start_managed_opencode()
    assert result["status"] == "fail" and "executable" in result["detail"]


def test_backend_can_be_replaced_without_business_service_changes(monkeypatch):
    from app.agents import factory

    replacement = object()
    monkeypatch.setattr(factory, "_factories", {})
    monkeypatch.setenv("AGENT_BACKEND", "replacement")
    factory.register_agent_backend("replacement", lambda settings: replacement)
    assert factory.get_agent_backend() is replacement


def test_transport_password_rotation_preserves_model_cache_identity(tmp_path):
    settings = AgentSettings(workspace_root=tmp_path, model="rag/test", password="synthetic-first")
    original = OpenCodeAgentBackend(settings).identity()
    rotated = OpenCodeAgentBackend(replace(settings, password="synthetic-second")).identity()
    other_model = OpenCodeAgentBackend(replace(settings, model="rag/other")).identity()
    assert rotated == original
    assert other_model["fingerprint"] != original["fingerprint"]


def test_runtime_blank_model_and_callback_use_valid_defaults(monkeypatch):
    from app.agents.settings import default_settings
    from app.agents.runtime import runtime_config

    monkeypatch.setenv("OPENCODE_MODEL", "")
    monkeypatch.setenv("AGENT_TOOL_URL", "")
    assert "/" in default_settings().model
    assert default_settings().callback_url.endswith("/api/v1/agent-tools")
    assert "/" in runtime_config()["model"]


@pytest.mark.parametrize("base", ["https://synthetic.example/gateway", "https://synthetic.example/gateway/v1", "https://synthetic.example/gateway/v1/messages/"])
def test_anthropic_runtime_preserves_application_endpoint_contract(monkeypatch, base):
    from app.agents.runtime import runtime_config
    from app.core import config

    monkeypatch.setattr(config, "ANSWER_LLM_PROVIDER", "anthropic_compatible")
    monkeypatch.setattr(config, "ANSWER_LLM_API_KEY", "synthetic-key")
    monkeypatch.setattr(config, "ANSWER_LLM_BASE_URL", base)
    provider = runtime_config()["providers"]["rag"]
    assert provider["settings"]["baseURL"] == "https://synthetic.example/gateway/v1"
    assert provider["package"] == "@opencode/ai/providers/anthropic"
