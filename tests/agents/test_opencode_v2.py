"""Native V2 envelopes, multi-turn tools, scoped cleanup and execution budgets."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import threading

import httpx
import pytest

from app.agents.contracts import AgentCancelledError, AgentOutputError, AgentRequest, AgentUnavailableError
from app.agents.codecode import CodeCodeAgentBackend
from app.agents.settings import AgentSettings, CODECODE_VERSION


SCHEMA = {"type": "object", "properties": {"verified": {"type": "boolean"}}, "required": ["verified"], "additionalProperties": False}


def task(**values):
    return AgentRequest(**{**dict(task="quality_review", knowledge_base_id="kb-test", instructions="Review evidence", context={}, output_schema=SCHEMA), **values})


def assistant(index, *, tool=None, completed=True, text=None, cost=0):
    return {"id": f"msg_{index}", "type": "assistant", "model": {"providerID": "rag", "id": "test"},
            "time": {"created": index, **({"completed": index + 10} if completed else {})},
            "finish": "tool-calls" if tool else "stop", "cost": cost,
            "tokens": {"input": 10 * index, "output": index, "reasoning": index + 1, "cache": {"read": index + 2, "write": index + 3}},
            "content": ([{"type": "tool", "id": f"tool_{index}", "name": tool,
                          "state": {"status": "completed" if completed else "running"}}] if tool else [])
                       + ([{"type": "text", "text": text}] if text is not None else [])}


IDLE = {"id": "msg_idle", "type": "idle", "outcome": "succeeded"}


def native_backend(tmp_path, snapshots, *, max_steps=12, intercept=None):
    calls, poll = [], 0

    def handler(req):
        nonlocal poll
        body = json.loads(req.content) if req.content else None
        calls.append((req, body))
        if intercept:
            response = intercept(req, body)
            if response is not None:
                return response
        if req.url.path == "/api/info":
            return httpx.Response(200, json={"version": CODECODE_VERSION})
        if req.method == "POST" and req.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_native"}})
        if req.method == "GET" and req.url.path == "/api/mcp":
            return httpx.Response(200, json={"data": [{"name": "knowledge", "status": {"status": "connected"}}]})
        if req.method == "GET" and req.url.path.endswith("/message"):
            data = snapshots[min(poll, len(snapshots) - 1)]
            poll += 1
            return httpx.Response(200, json={"data": data, "cursor": {}})
        return httpx.Response(204)

    backend = CodeCodeAgentBackend(AgentSettings(workspace_root=tmp_path, model="rag/test", poll_seconds=0.01,
                                                max_steps=max_steps), transport=httpx.MockTransport(handler))
    return backend, calls


def test_two_tool_rounds_wait_for_native_idle_and_aggregate_usage_without_double_counting(tmp_path):
    first = assistant(1, tool="knowledge_get_schema")
    second = assistant(2, tool="knowledge_read_chunks")
    final = assistant(3, text='{"verified":true}')
    backend, calls = native_backend(tmp_path, [
        [assistant(1, tool="knowledge_get_schema", completed=False)],
        [first, assistant(2, tool="knowledge_read_chunks", completed=False)],
        [first, second, final],
        [first, second, final, IDLE],
    ])
    events = []
    result = backend.run(task(tool_token="scoped-token"), on_event=events.append)
    assert result.output == {"verified": True}
    assert result.usage["input_tokens"] == 60
    assert result.usage["output_tokens"] == 6
    assert result.usage["reasoning_tokens"] == 9
    assert result.usage["cache_read_tokens"] == 12
    assert result.usage["cache_write_tokens"] == 15
    assert result.usage["total_tokens"] is None and result.usage["cost"] is None
    assert result.usage["reported_cost"] == 0
    assert len([event for event in events if event["type"] == "usage"]) == 1
    assert len([event for event in events if event["type"] == "step"]) == 3
    assert len([event for event in events if event["type"] == "tool"]) == 4
    assert len([req for req, _ in calls if req.method == "GET" and req.url.path.endswith("/message")]) == 4
    create = next(body for req, body in calls if req.method == "POST" and req.url.path == "/api/session")
    directory = create["location"]["directory"]
    for req, _ in calls:
        if "/mcp" in req.url.path or req.url.path == "/api/debug/location":
            assert req.url.params["location[directory]"] == directory
            assert "directory" not in req.url.params
        assert req.url.path.startswith("/api/")
    assert not list(tmp_path.iterdir())


def test_idle_projection_waits_for_complete_assistant_before_accepting_json(tmp_path):
    pending = assistant(1, completed=False, text='{"verified":true}')
    pending.pop("finish")
    backend, calls = native_backend(tmp_path, [[pending, IDLE], [assistant(1, text='{"verified":true}'), IDLE]])
    assert backend.run(task()).output == {"verified": True}
    assert len([req for req, _ in calls if req.method == "GET" and req.url.path.endswith("/message")]) == 2


def test_permanently_incomplete_idle_fails_within_budget_and_cleans_up(tmp_path):
    pending = assistant(1, completed=False, text='{"verified":true}')
    pending.pop("finish")
    backend, calls = native_backend(tmp_path, [[pending, IDLE]])
    backend.settings = replace(backend.settings, timeout_seconds=1)
    with pytest.raises(AgentOutputError, match="终态消息"):
        backend.run(task())
    assert any(req.url.path.endswith("/interrupt") for req, _ in calls)
    assert any(req.method == "DELETE" and req.url.path == "/api/debug/location" for req, _ in calls)
    assert not list(tmp_path.iterdir())


def test_mcp_initial_discovery_precedes_session_and_failed_session_releases_location(tmp_path):
    def reject_session(req, body):
        if req.method == "POST" and req.url.path == "/api/session":
            return httpx.Response(503)
    backend, calls = native_backend(tmp_path, [], intercept=reject_session)
    with pytest.raises(AgentUnavailableError):
        backend.run(task(tool_token="scoped-token"))
    paths = [(req.method, req.url.path) for req, _ in calls]
    assert paths.index(("GET", "/api/mcp")) < paths.index(("POST", "/api/session"))
    assert ("DELETE", "/api/experimental/mcp/knowledge") in paths
    assert ("DELETE", "/api/debug/location") in paths
    assert not any(path.endswith("/prompt") for _, path in paths)
    assert not list(tmp_path.iterdir())


def test_partial_usage_survives_cancellation_and_each_cleanup_is_independent(tmp_path):
    backend, calls = native_backend(tmp_path, [[assistant(1, tool="knowledge_get_schema")]])
    cancelled = False
    events = []

    def on_event(event):
        nonlocal cancelled
        events.append(event)
        if event["type"] == "step":
            cancelled = True

    original = backend._request

    def request(client, method, path, **kwargs):
        if path.endswith("/interrupt"):
            raise AgentUnavailableError("synthetic interrupt failure")
        return original(client, method, path, **kwargs)

    backend._request = request
    with pytest.raises(AgentCancelledError):
        backend.run(task(tool_token="scoped-token"), cancelled=lambda: cancelled, on_event=on_event)
    assert any(req.method == "DELETE" and req.url.path == "/api/experimental/mcp/knowledge" for req, _ in calls)
    assert any(req.method == "DELETE" and req.url.path == "/api/session/ses_native" for req, _ in calls)
    assert any(req.method == "DELETE" and req.url.path == "/api/debug/location" for req, _ in calls)
    assert next(event for event in events if event["type"] == "usage")["usage"]["input_tokens"] == 10


def test_parent_deadline_shortens_every_http_timeout_and_expired_request_has_no_side_effects(tmp_path):
    backend, calls = native_backend(tmp_path, [[assistant(1, text='{"verified":true}'), IDLE]])
    deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
    backend.run(task(context={"deadline_at": deadline.isoformat()}))
    assert all(0 < req.extensions["timeout"]["read"] <= 3 for req, _ in calls)
    calls.clear()
    with pytest.raises(AgentUnavailableError, match="时间预算"):
        backend.run(task(context={"deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}))
    assert calls == [] and not list(tmp_path.iterdir())


def test_step_budget_allows_one_inflight_step_but_rejects_extra_final_step(tmp_path):
    backend, _ = native_backend(tmp_path, [[assistant(1, completed=False)], [assistant(1, text='{"verified":true}'), IDLE]], max_steps=1)
    assert backend.run(task()).output == {"verified": True}
    backend, calls = native_backend(tmp_path, [[assistant(1, tool="knowledge_get_schema"), assistant(2, text='{"verified":true}'), IDLE]], max_steps=1)
    with pytest.raises(AgentOutputError, match="步骤预算"):
        backend.run(task())
    assert any(req.url.path.endswith("/interrupt") for req, _ in calls)


def test_legacy_message_envelope_is_rejected_without_fallback(tmp_path):
    def intercept(req, body):
        if req.method == "GET" and req.url.path.endswith("/message"):
            return httpx.Response(200, json=[{"info": {"role": "assistant", "structured": {"verified": True}}}])

    backend, calls = native_backend(tmp_path, [[]], intercept=intercept)
    with pytest.raises(AgentUnavailableError, match="V2消息"):
        backend.run(task())
    assert not any(not req.url.path.startswith("/api/") for req, _ in calls)


def test_native_message_pagination_uses_cursor_without_order_and_deduplicates_messages():
    queries = []

    def send(method, path, **kwargs):
        queries.append(kwargs["params"])
        return {"data": [assistant(1)], "cursor": {"next": "next-page"}} if len(queries) == 1 else {"data": [assistant(1), IDLE], "cursor": {}}

    messages = CodeCodeAgentBackend._messages(send, "/api/session/ses_native")
    assert len(messages) == 2
    assert queries == [{"limit": 200, "order": "asc"}, {"limit": 200, "cursor": "next-page"}]


def test_native_model_variant_and_fixed_basic_username(tmp_path):
    backend, calls = native_backend(tmp_path, [[assistant(1, text='{"verified":true}'), IDLE]])
    from dataclasses import replace
    backend.settings = replace(backend.settings, model="rag/team/model#careful", password="scoped-password")
    backend.run(task())
    create = next(body for req, body in calls if req.method == "POST" and req.url.path == "/api/session")
    assert create["model"] == {"providerID": "rag", "id": "team/model", "variant": "careful"}
    assert calls[0][0].headers["Authorization"] == "Basic b3BlbmNvZGU6c2NvcGVkLXBhc3N3b3Jk"


def test_budget_is_checked_between_http_response_chunks_and_stream_is_closed():
    state = {"chunks": 0, "checks": 0, "closed": False}

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b'{"data":', b'[]', b'}'):
                state["chunks"] += 1
                yield chunk

        def close(self):
            state["closed"] = True

    def check():
        state["checks"] += 1
        if state["checks"] == 2:
            raise AgentCancelledError("parent task cancelled")

    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream()))) as client:
        with pytest.raises(AgentCancelledError):
            CodeCodeAgentBackend._request(client, "GET", "http://localhost/api/info", _budget=check)
    assert state == {"chunks": 2, "checks": 2, "closed": True}


def test_pending_mcp_cancellation_never_submits_a_model_prompt(tmp_path):
    pending = False

    def intercept(req, body):
        nonlocal pending
        if req.method == "GET" and req.url.path == "/api/mcp":
            pending = True
            return httpx.Response(200, json={"data": [{"name": "knowledge", "status": {"status": "pending"}}]})

    backend, calls = native_backend(tmp_path, [[]], intercept=intercept)
    with pytest.raises(AgentCancelledError):
        backend.run(task(tool_token="scoped-token"), cancelled=lambda: pending)
    assert not any(req.url.path.endswith("/prompt") for req, _ in calls)
    assert any(req.method == "DELETE" and req.url.path == "/api/experimental/mcp/knowledge" for req, _ in calls)
    assert any(req.method == "DELETE" and req.url.path == "/api/debug/location" for req, _ in calls)


def test_concurrent_runs_never_share_mcp_locations_or_credentials(tmp_path):
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    sessions, registered, removed, evicted = {}, {}, [], []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        if req.url.path == "/api/info":
            return httpx.Response(200, json={"version": CODECODE_VERSION})
        if req.method == "POST" and req.url.path == "/api/session":
            with lock:
                sid = f"ses_{len(sessions)}"
                sessions[sid] = body["location"]["directory"]
            return httpx.Response(200, json={"data": {"id": sid}})
        if req.method == "PUT" and req.url.path == "/api/experimental/mcp/knowledge":
            with lock:
                registered[req.url.params["location[directory]"]] = body["config"]["headers"]["Authorization"]
            barrier.wait(timeout=5)
        if req.method == "GET" and req.url.path == "/api/mcp":
            return httpx.Response(200, json={"data": [{"name": "knowledge", "status": {"status": "connected"}}]})
        if req.method == "GET" and req.url.path.endswith("/message"):
            return httpx.Response(200, json={"data": [assistant(1, text='{"verified":true}'), IDLE], "cursor": {}})
        if req.method == "DELETE" and req.url.path == "/api/experimental/mcp/knowledge":
            with lock:
                removed.append(req.url.params["location[directory]"])
        if req.method == "DELETE" and req.url.path == "/api/debug/location":
            with lock:
                evicted.append(req.url.params["location[directory]"])
        return httpx.Response(204)

    backend = CodeCodeAgentBackend(AgentSettings(workspace_root=tmp_path, model="rag/test"), transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda token: backend.run(task(tool_token=token)), ["first-token", "second-token"]))
    assert len({result.session_id for result in results}) == 2
    assert len(registered) == 2 and set(registered.values()) == {"Bearer first-token", "Bearer second-token"}
    assert set(registered) == set(removed) == set(evicted) == set(sessions.values())
    assert not list(tmp_path.iterdir())
