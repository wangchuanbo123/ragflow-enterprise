"""Safety/measurement helpers plus an explicit opt-in real-model acceptance."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

from scripts.verify_real_team import (
    AcceptanceError,
    ModelEndpoint,
    ModelProxy,
    RequestBudget,
    UsageCollector,
    check_answer,
    expected_service_relationship,
    child_command,
    cleanup_redis,
    isolated_environment,
    iter_sse,
    load_model_settings,
    model_endpoints,
    require_cleanup_names,
    wait_job,
)


def endpoints(base="http://127.0.0.1:9/v2"):
    return {purpose: ModelEndpoint("openai_compatible", "synthetic-chat", base, "synthetic-upstream-secret")
        for purpose in ("answer", "rewrite", "router", "embedding")}


def test_model_file_whitelist_ignores_app_agent_database_and_ambient_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("DO_NOT_IMPORT", "ambient-secret")
    path = tmp_path / "models.env"
    path.write_text("LLM_PROVIDER=openai_compatible\nLLM_API_KEY=synthetic-key\n"
        "LLM_MODEL=test-model\nLLM_BASE_URL=http://127.0.0.1:9/v2\n"
        "DATABASE_URL=postgresql://production\nREDIS_URL=redis://production\n"
        "JWT_SECRET_KEY=production-secret\nOPENCODE_BASE_URL=https://production-agent\n"
        "AGENT_BACKEND_PROFILES_JSON={}\nUNKNOWN=${DO_NOT_IMPORT}\n"
        "REWRITE_LLM_API_KEY=${DO_NOT_IMPORT}\n", encoding="utf-8")
    values = load_model_settings(path)
    assert set(values) == {"LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "REWRITE_LLM_API_KEY"}
    assert values["REWRITE_LLM_API_KEY"] == "${DO_NOT_IMPORT}"
    assert "production" not in repr(values) and "ambient-secret" not in repr(values)


def test_model_profiles_follow_application_fallbacks_and_secrets_are_not_in_repr():
    result = model_endpoints({"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "configured-chat",
        "LLM_API_KEY": "private-key", "LLM_BASE_URL": "https://models.invalid/v2",
        "EMBEDDING_PROVIDER": "ollama", "EMBEDDING_MODEL": "configured-embedding",
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434"})
    assert result["answer"].model == result["rewrite"].model == result["router"].model == "configured-chat"
    assert result["embedding"].model == "configured-embedding"
    assert "private-key" not in repr(result) and "models.invalid" not in repr(result)
    with pytest.raises(AcceptanceError, match="remote_generation_provider_required"):
        model_endpoints({"LLM_PROVIDER": "ollama"})
    with pytest.raises(AcceptanceError, match="invalid_model_endpoint"):
        ModelEndpoint("openai", "model", "https://user:secret@models.invalid/v1")


def test_child_process_environments_use_private_state_and_proxy_credentials(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "REDIS_URL", "OPENCODE_EXECUTABLE", "AGENT_BACKEND_PROFILES_JSON", "ANSWER_LLM_API_KEY"):
        monkeypatch.setenv(key, "must-not-inherit")
    with ModelProxy(endpoints()) as proxy:
        common = dict(workspace=tmp_path, database_url="postgresql://test/schema", redis_url="redis://test/3",
            prefix="verify-real:" + "a" * 32, api_port=12345, proxy=proxy, admin_password="synthetic-admin-password")
        api = isolated_environment(role="api", opencode_port=12346, **common)
        worker = isolated_environment(role="worker", opencode_port=12347, **common)
        assert api["DATA_DIR"] != worker["DATA_DIR"]
        assert api["AGENT_WORKSPACE_DIR"] != worker["AGENT_WORKSPACE_DIR"]
        assert api["OPENCODE_BASE_URL"] != worker["OPENCODE_BASE_URL"]
        assert api["LOCAL_OBJECT_STORE_DIR"] == worker["LOCAL_OBJECT_STORE_DIR"]
        assert api["ANSWER_LLM_API_KEY"] == proxy.token
        assert "synthetic-upstream-secret" not in repr(api)
        assert "must-not-inherit" not in repr(api)
        assert "OPENCODE_EXECUTABLE" not in api
        assert api["AGENT_BACKEND_PROFILES_JSON"] == "{}"
        assert api["GRAPH_AUTO_BUILD_ENABLED"] == api["GRAPH_SCHEMA_AUTO_GENERATE_ENABLED"] == "false"
        assert "dotenv.load_dotenv=lambda" in child_command("scripts.run_worker")[2]


def test_atomic_budget_counts_upstream_attempts_and_never_exceeds_global_limit():
    budget = RequestBudget(generation_limit=12)
    with ThreadPoolExecutor(max_workers=24) as pool:
        accepted = list(pool.map(lambda _index: budget.claim("generation"), range(100)))
    assert sum(accepted) == 12
    assert budget.snapshot()["requests"]["generation"] == 12
    assert budget.snapshot()["budget_rejections"]["generation"] == 88
    assert budget.snapshot()["cost"] is None


def test_forwarding_proxy_preserves_real_payload_auth_stream_and_budget():
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            received.append({"path": self.path, "key": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            content = (b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
                b'data: {"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n'
                b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ModelProxy(endpoints(f"http://127.0.0.1:{server.server_port}/v2")) as proxy:
            proxy.budget = RequestBudget(generation_limit=1)
            payload = {"model": "synthetic-chat", "messages": [{"role": "user", "content": "synthetic prompt"}], "stream": True}
            headers = {"Authorization": "Bearer " + proxy.token}
            with httpx.Client(trust_env=False, timeout=10) as client:
                assert client.post(proxy.base("answer") + "/chat/completions", json=payload).status_code == 401
                first = client.post(proxy.base("answer") + "/chat/completions", json=payload, headers=headers)
                assert first.status_code == 200 and "first" in first.text and "[DONE]" in first.text
                assert client.post(proxy.base("answer") + "/chat/completions", json=payload, headers=headers).status_code == 429
            assert received == [{"path": "/v2/chat/completions", "key": "Bearer synthetic-upstream-secret", "body": payload}]
            metrics = proxy.budget.snapshot()
            assert metrics["requests"]["generation"] == 1
            assert metrics["budget_rejections"]["generation"] == 1
            assert metrics["calls"][0]["reported_usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
            assert "synthetic-upstream-secret" not in repr(metrics) and "synthetic prompt" not in repr(metrics)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_usage_collector_handles_chunk_boundaries_and_keeps_only_numeric_usage():
    collector = UsageCollector()
    collector.feed(b'data: {"message":{"usage":{"input_to')
    collector.feed(b'kens":25,"output_tokens":false,"api_key":"secret"}}}\n\n')
    collector.feed(b'data: {"usage":{"output_tokens":3,"total_tokens":28}}\n\n')
    assert collector.finish() == {"input_tokens": 25, "output_tokens": 3, "total_tokens": 28}
    assert len(UsageCollector().buffer) == 0


@pytest.mark.parametrize("schema,prefix", [
    ("public", "verify-real:" + "a" * 32),
    ("verify_real_" + "a" * 32, "rag-enterprise"),
    ("verify_real_" + "a" * 32 + ";drop schema public", "verify-real:" + "b" * 32),
    ("verify_real_" + "a" * 32, "verify-real:*"),
])
def test_cleanup_rejects_non_owned_or_wildcard_targets(schema, prefix):
    with pytest.raises(AcceptanceError):
        require_cleanup_names(schema, prefix)


def test_redis_cleanup_deletes_only_the_invocation_prefix():
    owned = "verify-real:" + "a" * 32

    class Client:
        def __init__(self):
            self.keys = {owned + ":jobs", owned + ":cache", "rag-enterprise:jobs", "verify-real:" + "b" * 32 + ":jobs"}

        def scan_iter(self, *, match, count):
            return iter([key for key in self.keys if key.startswith(match[:-1])])

        def delete(self, key):
            self.keys.remove(key)

    client = Client()
    assert cleanup_redis(client, owned)
    assert client.keys == {"rag-enterprise:jobs", "verify-real:" + "b" * 32 + ":jobs"}
    with pytest.raises(AcceptanceError):
        cleanup_redis(client, "*")


def test_answer_checks_require_source_scope_dates_conflicts_and_abstention():
    formal, both = [{"document_id": "formal"}], [{"document_id": "formal"}, {"document_id": "draft"}]
    assert check_answer("formal_facts", "林青，2026年12月18日。", formal, ["formal"])["passed"]
    assert not check_answer("formal_facts", "林青，2026-12-28。", formal, ["formal"])["passed"]
    assert not check_answer("formal_facts", "林青，2026-12-18。", both, ["formal"])["passed"]
    assert check_answer("conflicting_dates", "日期冲突：正式2026-12-18，草案2026-12-28。", both, ["formal", "draft"])["passed"]
    assert not check_answer("conflicting_dates", "2026-12-18与2026-12-28不一致。", formal, ["formal", "draft"])["passed"]
    assert not check_answer("conflicting_dates", "日期冲突：正式2026-12-28，草案2026-12-18。", both, ["formal", "draft"])["passed"]
    assert check_answer("unanswerable", "资料未记载详细地址，无法确定。", [], ["formal", "draft"])["passed"]
    assert not check_answer("unanswerable", "未提供，但总部地址是上海市假想路10号。", both, ["formal", "draft"])["passed"]


def test_expected_relation_requires_correct_predicate_direction_and_supporting_quote():
    nodes = {"order": "星河订单服务", "payment": "北辰支付服务"}
    valid = {"source": "order", "target": "payment", "predicate": "CALLS",
        "evidence": [{"quote": "合成资料：星河订单服务 调用 北辰支付服务。"}]}
    assert expected_service_relationship(nodes, [valid])
    assert not expected_service_relationship(nodes, [{**valid, "predicate": "OWNED_BY"}])
    assert not expected_service_relationship(nodes, [{**valid, "source": "payment", "target": "order"}])
    assert not expected_service_relationship(nodes, [{**valid, "evidence": [{"quote": "项目负责人是林青。"}]}])


def test_sse_parser_handles_comments_multiline_and_last_event_without_blank_line():
    events = list(iter_sse([": heartbeat", "event: delta", 'data: {"content":', 'data: "hello"}', "", "event: done", 'data: {"status":"completed"}']))
    assert events == [("delta", {"content": "hello"}), ("done", {"status": "completed"})]


def test_wait_job_queries_actual_nondefault_knowledge_base_route(client, db_engine):
    from sqlalchemy.orm import Session
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD
    from app.models.index_job import IndexJob
    assert client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD}).status_code == 200
    response = client.post("/api/v1/knowledge-bases", json={"name": "隔离路由验收", "slug": "real-team-route-check"})
    assert response.status_code == 201
    kb = response.json()["id"]
    with Session(db_engine) as db:
        job = IndexJob(knowledge_base_id=kb, job_type="file", status="completed", total_items=1,
            processed_items=1, succeeded_items=1, failed_items=0)
        db.add(job)
        db.commit()
        identifier = job.id
    # The legacy route correctly scopes itself to the default knowledge base.
    assert client.get("/api/v1/index-jobs/" + identifier).status_code == 404
    result = wait_job(client, kb, identifier, [], timeout=5)
    assert result["id"] == identifier and result["status"] == "completed"


def test_acceptance_paths_match_actual_api_routes_and_never_static_fallback():
    from app.main import app
    from starlette.routing import Match
    contracts = [
        ("POST", "/api/v1/auth/login", "login"),
        ("POST", "/api/v1/users", "create_user"),
        ("POST", "/api/v1/knowledge-bases", "create_knowledge_base"),
        ("PUT", "/api/v1/knowledge-bases/verify-kb/members", "upsert_knowledge_base_member"),
        ("PUT", "/api/v1/knowledge-bases/verify-kb/knowledge-graph/automation", "update_graph_automation_settings"),
        ("POST", "/api/v1/knowledge-bases/verify-kb/documents/batch-upload", "batch_upload_documents"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/documents/page", "paged_documents"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/index-jobs/verify-job", "get_index_job"),
        ("POST", "/api/v1/conversations", "create_conversation"),
        ("POST", "/api/v1/conversations/verify-conversation/messages/stream", "stream_message"),
        ("POST", "/api/v1/knowledge-bases/verify-kb/knowledge-graph/build", "request_graph_build"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/knowledge-graph/stats", "graph_stats"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/knowledge-graph/entities", "search_graph_entities"),
        ("POST", "/api/v1/knowledge-bases/verify-kb/knowledge-graph/filter-preview", "preview_graph_filter"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/chunks/verify-chunk", "get_chunk_detail"),
        ("GET", "/api/v1/knowledge-bases/verify-kb/documents/verify-document/chunks", "list_document_chunks"),
    ]
    def effective_routes(routes):
        for route in routes:
            # New FastAPI releases preserve include prefixes in lazy router
            # branches; inspect the same effective candidates routing uses.
            candidates = getattr(route, "effective_candidates", None)
            if callable(candidates):
                yield from effective_routes(candidates())
            else:
                yield route
    routes = list(effective_routes(app.routes))
    for method, path, expected_name in contracts:
        matches = [route for route in routes if route.matches({"type": "http", "method": method,
            "path": path, "root_path": ""})[0] == Match.FULL]
        names = [route.name for route in matches]
        assert names and names[0] == expected_name, (method, path, names)


def test_real_http_models_three_members_graph_and_original_evidence(tmp_path):
    postgres, redis_url = os.getenv("TEST_POSTGRES_URL"), os.getenv("TEST_REDIS_URL")
    model_env = os.getenv("TEST_REAL_MODEL_ENV")
    if os.getenv("RUN_REAL_MODEL_ACCEPTANCE") != "1" or not all((postgres, redis_url, model_env)):
        pytest.skip("Opt-in requires RUN_REAL_MODEL_ACCEPTANCE=1, TEST_POSTGRES_URL, TEST_REDIS_URL and TEST_REAL_MODEL_ENV")
    report_file = tmp_path / "real-team-report.json"
    command = [sys.executable, "-m", "scripts.verify_real_team", "--model-env", model_env, "--report", str(report_file)]
    if os.getenv("TEST_REAL_MODEL_OCR") == "1":
        command.append("--with-ocr")
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=1500)
    # The subprocess's console contains only a safe status and report path.
    assert result.returncode == 0, result.stdout
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["status"] == "completed" and report["concurrent_clients"] == 3
    assert all(item["passed"] and item["first_token_ms"] > 0 and item["total_ms"] >= item["first_token_ms"] for item in report["chat_cases"])
    assert report["graph"]["passed"] and report["filter"]["passed"]
    assert report["filter"]["evidence_validated"] > 0
    assert 0 < report["model_requests"]["requests"]["generation"] <= 12
    assert report["model_requests"]["requests"]["embedding"] > 0
    assert report["model_requests"]["cost"] is None
    assert all(report["cleanup"].values())
