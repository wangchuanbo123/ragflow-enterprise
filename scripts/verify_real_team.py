"""Opt-in real HTTP/model acceptance in disposable PostgreSQL/Redis namespaces.

Example (prefer the application container, which includes OCR dependencies)::

    TEST_POSTGRES_URL=... TEST_REDIS_URL=... python -m scripts.verify_real_team \
        --model-env .env.server --report /tmp/real-team-report.json --with-ocr

Only explicitly named model settings are read from --model-env. The application
.env and ambient application settings are disabled in child processes. Three
synthetic users and tiny documents test correctness; this is not a capacity test.
The loopback forwarding proxy sends requests to the real configured providers,
counts retries too, and enforces a shared limit of 12 generation HTTP requests.
No provider is replaced with a fake, and no production document is read.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
GENERATION_LIMIT = 12
EMBEDDING_LIMIT = 30
MODEL_FIELDS = {"PROVIDER", "MODEL", "API_KEY", "BASE_URL"}
MODEL_KEYS = {f"{prefix}_{suffix}" for prefix in (
    "LLM", "ANSWER_LLM", "REWRITE_LLM", "ROUTER_LLM", "EMBEDDING"
) for suffix in MODEL_FIELDS} | {"ZHIPU_API_KEY", "ZHIPU_BASE_URL", "OLLAMA_BASE_URL", "OLLAMA_LLM_MODEL"}
OS_KEYS = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "LANG", "LC_ALL",
    "TESSDATA_PREFIX", "OMP_NUM_THREADS"}
GENERATION_PROVIDERS = {"openai", "openai_compatible", "zhipu", "zai", "glm", "anthropic", "anthropic_compatible"}
SCHEMA_PATTERN = re.compile(r"verify_real_[0-9a-f]{32}\Z")
PREFIX_PATTERN = re.compile(r"verify-real:[0-9a-f]{32}\Z")


class AcceptanceError(RuntimeError):
    """Only fixed, secret-free error codes are exposed in reports."""


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def load_model_settings(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    require(path.is_file(), "model_env_missing")
    # Do not interpolate ambient environment variables into an explicit file.
    values = dotenv_values(path, interpolate=False)
    return {key: str(value) for key, value in values.items() if key in MODEL_KEYS and value is not None}


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(default="", repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        require(parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and not any((parsed.username, parsed.password, parsed.query, parsed.fragment)), "invalid_model_endpoint")
        require(bool(self.model.strip()), "model_name_missing")


def model_endpoints(settings: dict[str, str]) -> dict[str, ModelEndpoint]:
    legacy = {
        "PROVIDER": settings.get("LLM_PROVIDER", "zhipu"),
        "MODEL": settings.get("LLM_MODEL", "glm-5.2"),
        "API_KEY": settings.get("LLM_API_KEY") or settings.get("ZHIPU_API_KEY", ""),
        "BASE_URL": settings.get("LLM_BASE_URL") or settings.get("ZHIPU_BASE_URL", ""),
    }
    endpoints = {}
    answer = {field: settings.get("ANSWER_LLM_" + field) or value for field, value in legacy.items()}
    for purpose in ("answer", "rewrite", "router"):
        values = answer if purpose == "answer" else {
            field: settings.get(purpose.upper() + "_LLM_" + field) or value for field, value in answer.items()}
        provider = values["PROVIDER"].lower()
        require(provider in GENERATION_PROVIDERS, "remote_generation_provider_required")
        require(bool(values["API_KEY"] and values["BASE_URL"]), "explicit_generation_credentials_required")
        endpoints[purpose] = ModelEndpoint(provider, values["MODEL"], values["BASE_URL"].rstrip("/"), values["API_KEY"])
    embedding_provider = settings.get("EMBEDDING_PROVIDER", "ollama").lower()
    require(embedding_provider in {"ollama", "openai", "openai_compatible"}, "unsupported_embedding_provider")
    if embedding_provider == "ollama":
        endpoints["embedding"] = ModelEndpoint("ollama", settings.get("EMBEDDING_MODEL", "nomic-embed-text-v2-moe"),
            settings.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"))
    else:
        key, base = settings.get("EMBEDDING_API_KEY") or legacy["API_KEY"], settings.get("EMBEDDING_BASE_URL") or legacy["BASE_URL"]
        require(bool(key and base), "explicit_embedding_credentials_required")
        endpoints["embedding"] = ModelEndpoint(embedding_provider, settings.get("EMBEDDING_MODEL", "nomic-embed-text-v2-moe"), base.rstrip("/"), key)
    return endpoints


class RequestBudget:
    def __init__(self, generation_limit=GENERATION_LIMIT, embedding_limit=EMBEDDING_LIMIT):
        self.limits = {"generation": generation_limit, "embedding": embedding_limit}
        self.counts = {"generation": 0, "embedding": 0}
        self.rejected = {"generation": 0, "embedding": 0}
        self.metrics = []
        self.lock = threading.Lock()

    def claim(self, kind: str) -> bool:
        with self.lock:
            if self.counts[kind] >= self.limits[kind]:
                self.rejected[kind] += 1
                return False
            self.counts[kind] += 1
            return True

    def add_metric(self, value):
        with self.lock:
            self.metrics.append(value)

    def snapshot(self):
        with self.lock:
            return {"limits": dict(self.limits), "requests": dict(self.counts),
                "budget_rejections": dict(self.rejected), "calls": list(self.metrics),
                "cost": None, "cost_status": "unknown; no configured authoritative price or billing result"}


def reported_usage(payload) -> dict[str, int | float]:
    """Keep numeric provider-reported token counts, never text or inferred cost."""
    if not isinstance(payload, dict):
        return {}
    message = payload.get("message")
    usage = payload.get("usage") or (message.get("usage") if isinstance(message, dict) else None) or {}
    if not isinstance(usage, dict):
        return {}
    keys = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens"}
    return {key: value for key, value in usage.items() if key in keys
        and isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**12}


class UsageCollector:
    def __init__(self):
        self.buffer = bytearray()
        self.usage = {}

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            self._line(line)
        # The response itself is streamed unchanged; an oversized line is not
        # retained for telemetry and cannot exhaust the verification process.
        if len(self.buffer) > 1024 * 1024:
            self.buffer.clear()

    def _line(self, line):
        line = line.strip()
        if line.startswith(b"data:"):
            line = line[5:].strip()
        try:
            self.usage.update(reported_usage(json.loads(line)))
        except (ValueError, TypeError, AttributeError):
            pass

    def finish(self):
        self._line(bytes(self.buffer))
        return self.usage


class ModelProxy:
    """Loopback-only, fixed-destination pass-through; no prompts are logged."""

    def __init__(self, endpoints, *, timeout=180):
        self.endpoints, self.timeout = endpoints, timeout
        self.token, self.route_nonce = secrets.token_urlsafe(32), secrets.token_hex(20)
        self.budget = RequestBudget()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                owner.forward(self)

            def do_GET(self):
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="real-model-budget-proxy")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def base(self, purpose):
        return f"http://127.0.0.1:{self.server.server_port}/{self.route_nonce}/{purpose}"

    def forward(self, handler):
        import httpx
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 4 * 1024 * 1024:
            handler.send_error(413)
            return
        # Consume the bounded body before rejecting a request. Windows can
        # reset a socket on close while unread request bytes remain buffered.
        body = handler.rfile.read(length)
        path = urlsplit(handler.path)
        parts = path.path.split("/", 3)
        if path.query or len(parts) != 4 or parts[1] != self.route_nonce or parts[2] not in self.endpoints:
            handler.send_error(404)
            return
        purpose, suffix = parts[2], "/" + parts[3]
        endpoint = self.endpoints[purpose]
        allowed = {"/api/embed"} if purpose == "embedding" and endpoint.provider == "ollama" else (
            {"/embeddings"} if purpose == "embedding" else {"/chat/completions", "/responses", "/v1/messages", "/messages"})
        if suffix not in allowed:
            handler.send_error(404)
            return
        # The nonce protects Ollama's keyless API too. Other clients must send
        # the per-run synthetic credential; upstream secrets never reach them.
        if endpoint.provider != "ollama" and handler.headers.get("Authorization") != "Bearer " + self.token and handler.headers.get("x-api-key") != self.token:
            handler.send_error(401)
            return
        kind = "embedding" if purpose == "embedding" else "generation"
        if not self.budget.claim(kind):
            handler.send_error(429, "Verification request budget exceeded")
            return
        base = endpoint.base_url
        if endpoint.provider in {"anthropic", "anthropic_compatible"}:
            # Application accepts host, /v1 or /v1/messages; OpenCode adds its
            # own /v1/messages suffix to our isolated proxy base URL.
            base = base.removesuffix("/messages") if base.endswith("/v1/messages") else base
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        else:
            url = base + suffix
        headers = {"Content-Type": handler.headers.get("Content-Type", "application/json"), "Accept-Encoding": "identity"}
        if endpoint.api_key:
            headers["Authorization"] = "Bearer " + endpoint.api_key
            if handler.headers.get("x-api-key"):
                headers["x-api-key"] = endpoint.api_key
        for key in ("anthropic-version", "anthropic-beta"):
            if handler.headers.get(key):
                headers[key] = handler.headers[key]
        started, status, collector, headers_sent = time.monotonic(), None, UsageCollector(), False
        try:
            # Environment proxies are intentionally ignored: the destination is
            # exactly the explicitly configured real model URL.
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                with client.stream("POST", url, headers=headers, content=body) as response:
                    status = response.status_code
                    handler.send_response(status)
                    handler.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    headers_sent = True
                    handler.close_connection = True
                    for chunk in response.iter_bytes():
                        collector.feed(chunk)
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            if not headers_sent:
                handler.send_error(502, "Real provider request failed")
        finally:
            self.budget.add_metric({"purpose": purpose, "kind": kind, "http_status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "reported_usage": collector.finish()})


def free_ports(number):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(number)]
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]


def isolated_environment(*, workspace: Path, role: str, database_url: str, redis_url: str,
        prefix: str, api_port: int, opencode_port: int, proxy: ModelProxy, admin_password: str):
    env = {key: value for key, value in os.environ.items() if key.upper() in OS_KEYS}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "APP_PROFILE": "server", "APP_ROLE": role, "APP_PORT": str(api_port), "DATA_DIR": str(workspace / role),
        "DATABASE_URL": database_url, "REDIS_URL": redis_url, "REDIS_KEY_PREFIX": prefix,
        "METADATA_STORE_PROVIDER": "postgresql", "VECTOR_STORE_PROVIDER": "pgvector", "GRAPH_STORE_PROVIDER": "sqlalchemy",
        "TASK_QUEUE_PROVIDER": "redis", "CACHE_PROVIDER": "redis", "OBJECT_STORE_PROVIDER": "local",
        "LOCAL_OBJECT_STORE_DIR": str(workspace / "objects"), "LOCAL_SOURCE_ALLOWED_ROOTS": str(workspace / "objects"),
        "JWT_SECRET_KEY": secrets.token_urlsafe(48), "INITIAL_ADMIN_USERNAME": "verify-admin",
        "INITIAL_ADMIN_PASSWORD": admin_password, "COOKIE_SECURE": "false", "RERANKER_PROVIDER": "none",
        "INDEX_WORKER_ENABLED": "false", "INDEX_MAX_CONCURRENCY": "1", "INDEX_JOB_MAX_ATTEMPTS": "1",
        "CONNECTOR_SCHEDULER_ENABLED": "false", "BACKUP_MONTHLY_ENABLED": "false",
        "GRAPH_COMMUNITY_MAINTENANCE_ENABLED": "false", "CONTEXT_JSON_EXPORT_ENABLED": "false",
        "GRAPH_AUTO_BUILD_ENABLED": "false", "GRAPH_SCHEMA_AUTO_GENERATE_ENABLED": "false",
        "GRAPH_ENTITY_VECTOR_ENABLED": "false", "GRAPH_RELATION_VECTOR_ENABLED": "false",
        "GRAPH_EXTRACTION_MAX_RETRIES": "0", "GRAPH_ESCALATION_PROVIDER": "current", "GRAPH_ESCALATION_MODEL": "",
        "GRAPH_EXTRACTION_BATCH_SIZE": "6", "GRAPH_EXTRACTION_MAX_CHARS": "6000", "GRAPH_PARALLEL_WORKERS": "1",
        "GRAPH_PREFILTER_ENABLED": "false", "DOCUMENT_CODE_GRAPH_ENABLED": "false", "CODE_GRAPH_ENABLED": "false",
        "RETRIEVAL_ROUTER_MODE": "rules", "ROUTER_LLM_ENABLED": "false", "QUERY_REWRITE_CONFIDENCE_THRESHOLD": "0",
        "CHUNK_STRATEGY": "recursive", "MIN_CHUNK_SIZE": "40", "CHUNK_SIZE": "700", "CHUNK_OVERLAP": "30",
        "MAX_CONTEXT_CHUNKS": "8", "MAX_CHUNKS_PER_SOURCE": "4", "RAG_MAX_CONCURRENCY": "4",
        "MODEL_MAX_CONCURRENCY": "4", "MODEL_BACKGROUND_CONCURRENCY": "1", "MODEL_QUEUE_TIMEOUT_SECONDS": "60",
        "PROVIDER_TIMEOUT_SECONDS": "180", "AGENT_BACKEND": "opencode", "OPENCODE_AUTO_START": "true",
        "OPENCODE_BASE_URL": f"http://127.0.0.1:{opencode_port}", "OPENCODE_MODEL": "rag/" + proxy.endpoints["answer"].model,
        "OPENCODE_PASSWORD": secrets.token_urlsafe(32), "AGENT_WORKSPACE_DIR": str(workspace / role / "agents" / "workspaces"),
        "AGENT_TOOL_URL": f"http://127.0.0.1:{api_port}/api/v1/agent-tools", "AGENT_TIMEOUT_SECONDS": "180",
        "AGENT_MAX_STEPS": "3", "AGENT_BACKEND_PROFILES_JSON": "{}", "ANONYMIZED_TELEMETRY": "false"})
    for purpose in ("answer", "rewrite", "router"):
        endpoint = proxy.endpoints[purpose]
        for suffix, value in {"PROVIDER": endpoint.provider, "MODEL": endpoint.model, "API_KEY": proxy.token, "BASE_URL": proxy.base(purpose)}.items():
            env[purpose.upper() + "_LLM_" + suffix] = value
            if purpose == "answer":
                env["LLM_" + suffix] = value
    embedding = proxy.endpoints["embedding"]
    env.update({"EMBEDDING_PROVIDER": embedding.provider, "EMBEDDING_MODEL": embedding.model,
        "EMBEDDING_API_KEY": proxy.token, "EMBEDDING_BASE_URL": proxy.base("embedding"), "OLLAMA_BASE_URL": proxy.base("embedding")})
    return env


def child_command(module, *args):
    # This alters configuration loading only; actual application/provider code
    # and migrations run unchanged in a fresh interpreter.
    bootstrap = "import dotenv,runpy; dotenv.load_dotenv=lambda *a,**k: False; runpy.run_module(" + repr(module) + ",run_name='__main__')"
    return [sys.executable, "-c", bootstrap, *args]


def start_child(command, env, log_path):
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        return subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt")


def stop_children(processes):
    import psutil
    targets = {}
    for process in processes:
        try:
            parent = psutil.Process(process.pid)
            targets.update({child.pid: child for child in parent.children(recursive=True)})
            targets[parent.pid] = parent
        except psutil.NoSuchProcess:
            pass
    for child in reversed(list(targets.values())):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(list(targets.values()), timeout=8)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(alive, timeout=5)
    for process in processes:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return not alive


def wait_api(base, processes, *, timeout):
    import httpx
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=base, timeout=3, trust_env=False) as client:
        while time.monotonic() < deadline:
            require(all(process.poll() is None for process in processes), "application_process_exited_during_boot")
            try:
                if client.get("/api/v1/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise AcceptanceError("api_startup_timeout")


def request_json(client, method, path, *, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    require(response.status_code == expected, "http_" + str(response.status_code))
    return response.json()


def wait_job(client, knowledge_base_id, job_id, processes, *, timeout=480):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require(all(process.poll() is None for process in processes), "application_process_exited_during_job")
        job = request_json(client, "GET", f"/api/v1/knowledge-bases/{knowledge_base_id}/index-jobs/{job_id}")
        if job["status"] in {"completed", "partial", "failed", "cancelled"}:
            return {key: job.get(key) for key in ("id", "job_type", "status", "total_items", "processed_items", "succeeded_items", "failed_items", "attempt_count")}
        time.sleep(0.5)
    raise AcceptanceError("job_completion_timeout")


def iter_sse(lines):
    event, data = "message", []
    for line in lines:
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, json.loads("\n".join(data))


def check_answer(case, answer, sources, expected_document_ids):
    normalized = re.sub(r"\s+", "", answer)
    source_ids = {source.get("document_id") for source in sources}
    in_scope = bool(source_ids) and source_ids <= set(expected_document_ids)
    hits = len(source_ids & set(expected_document_ids))
    if case == "formal_facts":
        checks = {"owner": "林青" in normalized, "formal_date": bool(re.search(r"2026(?:-|年|/)12(?:-|月|/)18", normalized)),
            "no_draft_date": not bool(re.search(r"2026(?:-|年|/)12(?:-|月|/)28", normalized)), "expected_evidence": hits >= 1}
    elif case == "conflicting_dates":
        gap = r"(?:(?!2026|正式|草案)[^\n。；]){0,100}"
        def attributed(label, day):
            date = r"2026(?:-|年|/)12(?:-|月|/)" + day
            return bool(re.search(label + gap + date, answer) or re.search(date + gap + label, answer))
        checks = {"both_dates": all(re.search(r"2026(?:-|年|/)12(?:-|月|/)" + day, normalized) for day in ("18", "28")),
            "conflict_explicit": any(word in normalized for word in ("冲突", "矛盾", "不一致", "不同", "有差异")),
            "both_documents_evidenced": hits == 2,
            "dates_attributed_to_correct_documents": attributed("正式", "18") and attributed("草案", "28")}
    elif case == "unanswerable":
        checks = {"acknowledges_missing_information": any(word in normalized for word in (
            "未提供", "没有提供", "未记载", "未提及", "未说明", "未列出", "未记录", "未包含", "没有记载", "无法确认", "无法确定", "资料不足", "不知道", "没有相关")),
            "does_not_invent_address": not bool(re.search(r"[省市区县路街道]\d+号|位于.{2,30}[省市区县路街道]|地址[为是：:].{2,30}[省市区县路街道]", normalized))}
    else:
        raise AcceptanceError("unknown_answer_case")
    # Retrieval returning no evidence is allowed for abstention. Any evidence
    # that is returned must remain within the requested document scope.
    checks["source_scope_respected"] = in_scope if case != "unanswerable" else source_ids <= set(expected_document_ids)
    return {"checks": checks, "expected_source_document_hits": hits, "source_count": len(sources),
        "source_document_count": len(source_ids), "passed": all(checks.values())}


def stream_case(client, case, kb, document_ids, query, barrier):
    conversation = request_json(client, "POST", "/api/v1/conversations", expected=201,
        json={"title": "合成验收 " + case, "knowledge_base_id": kb})
    barrier.wait(timeout=30)
    started, first, answer, sources, complete, errored = time.monotonic(), None, [], [], False, False
    with client.stream("POST", f"/api/v1/conversations/{conversation['id']}/messages/stream", json={"content": query,
            "retrieval": {"strategy": "fast", "channels": ["text"], "scope": {"knowledge_base_ids": [kb], "document_ids": document_ids}}}) as response:
        require(response.status_code == 200, "chat_http_" + str(response.status_code))
        for event, data in iter_sse(response.iter_lines()):
            if event == "delta" and data.get("content"):
                if first is None:
                    first = (time.monotonic() - started) * 1000
                answer.append(data["content"])
            elif event == "sources":
                sources = data.get("sources", [])
            elif event == "answer":
                answer[:] = [data.get("content", "")]
            elif event == "done":
                complete = data.get("status") == "completed"
            elif event == "error":
                errored = True
    result = check_answer(case, "".join(answer), sources, document_ids)
    result.update({"case": case, "first_token_ms": round(first, 2) if first is not None else None,
        "total_ms": round((time.monotonic() - started) * 1000, 2), "stream_completed": complete, "stream_error": errored,
        "answer_characters": sum(map(len, answer))})
    result["passed"] = result["passed"] and complete and not errored and first is not None
    return result


def synthetic_documents():
    return [
        ("晨星正式方案.txt", "text/plain", ("合成验收资料，不对应真实客户。晨星项目正式方案，版本状态为正式发布。\n"
            "晨星项目的负责人是林青。晨星项目的正式上线日期是2026-12-18。晨星项目的审批额度为8000元。\n"
            "星河订单服务调用北辰支付服务。星河订单服务负责创建订单，北辰支付服务负责支付确认。\n"
            "正式方案中的负责人、上线日期和审批额度均已经过项目组确认。验收标记为SYNTHETIC_FORMAL_ORBIT。\n"
            "此资料没有记录晨星项目总部的详细地址，不能从服务名称或人员姓名推断地址。")),
        ("晨星草案.md", "text/markdown", ("# 晨星项目草案\n\n此文档属于合成验收资料，不对应真实客户，状态为尚未批准的草案。\n\n"
            "草案中晨星项目的建议上线日期是2026-12-28，与《晨星正式方案.txt》中的2026-12-18存在日期冲突。\n\n"
            "草案建议另行确认上线计划，不代表已经批准的变更。晨星项目负责人仍为林青。\n\n"
            "星河订单服务调用北辰支付服务。草案没有给出项目总部的详细地址。验收标记为SYNTHETIC_DRAFT_ORBIT。")),
    ]


def expected_service_relationship(nodes, edges):
    return any(nodes.get(edge["source"]) == "星河订单服务" and nodes.get(edge["target"]) == "北辰支付服务"
        and edge["predicate"] == "CALLS" and any("星河订单服务调用北辰支付服务" in re.sub(r"\s+", "", evidence["quote"])
            for evidence in edge["evidence"]) for edge in edges)


def scanned_pdf(workspace):
    if not shutil.which("tesseract") or not shutil.which("pdftoppm"):
        return None, "OCR or Poppler executable unavailable"
    try:
        from PIL import Image, ImageDraw, ImageFont
        font = None
        for name in ("DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                font = ImageFont.truetype(name, 44)
                break
            except OSError:
                pass
        if font is None:
            return None, "No suitable image font available"
        page = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(page)
        for index, line in enumerate(("SYNTHETIC OCR ACCEPTANCE", "ORBITVERIFYMARKER", "The service owner is Lin Qing.",
                "The approved launch date is 2026-12-18.", "This page contains only synthetic test facts.")):
            draw.text((90, 160 + index * 100), line, fill="black", font=font)
        path = workspace / "synthetic-scan.pdf"
        page.save(path, "PDF", resolution=150)
        return path.read_bytes(), None
    except ImportError:
        return None, "Pillow PDF image support unavailable"


def require_cleanup_names(schema, prefix):
    require(bool(SCHEMA_PATTERN.fullmatch(schema)), "unsafe_schema_cleanup_target")
    require(bool(PREFIX_PATTERN.fullmatch(prefix)), "unsafe_redis_cleanup_target")


def require_prepared_runtime():
    # Missing binaries would make application startup build into the checkout.
    # Acceptance requires a prepared project artifact; the application then
    # verifies its pinned commit, native format and SHA-256 before execution.
    runtime = ROOT / "integrations" / "opencode" / "runtime"
    require((runtime / ("opencode.exe" if os.name == "nt" else "opencode")).is_file()
        and (runtime / "build.json").is_file(), "project_opencode_runtime_must_be_prepared")


def cleanup_redis(client, prefix):
    require(bool(PREFIX_PATTERN.fullmatch(prefix)), "unsafe_redis_cleanup_target")
    # No FLUSHDB/FLUSHALL; bound deletion to keys owned by this invocation.
    for key in client.scan_iter(match=prefix + ":*", count=100):
        client.delete(key)
    return next(client.scan_iter(match=prefix + ":*", count=100), None) is None


def verify(postgres_url, redis_url, model_env, *, with_ocr=False, startup_timeout=180, job_timeout=480):
    """Run real acceptance without importing application configuration locally."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    import httpx
    import redis

    require(bool(postgres_url and redis_url), "explicit_test_service_urls_required")
    settings = load_model_settings(Path(model_env))
    endpoints = model_endpoints(settings)
    url = make_url(postgres_url)
    require(url.get_backend_name() == "postgresql", "test_postgresql_required")
    # Inherited libpq options could change search_path or inject a service file.
    require(not any(key in url.query for key in ("options", "service", "servicefile")), "test_url_options_not_allowed")
    url = url.set(drivername="postgresql+psycopg")
    schema, prefix = "verify_real_" + uuid.uuid4().hex, "verify-real:" + uuid.uuid4().hex
    require_cleanup_names(schema, prefix)
    target = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    admin_engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 10})
    redis_client = redis.Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    report = {"status": "failed", "test_kind": "real_http_real_configured_models", "members": 3, "concurrent_clients": 3,
        "synthetic_data": True, "schema": schema, "redis_namespace": prefix,
        "models": {purpose: {"provider": value.provider, "model": value.model} for purpose, value in endpoints.items()},
        "limits": ["Three concurrent synthetic users and tiny documents do not prove production team capacity.",
            "Automatic Schema generation, reranking, query rewriting, community summaries and graph vector rebuilds are disabled for this bounded correctness run.",
            "Answer quality checks are deterministic rules, not a human or independent model evaluation.",
            "The default knowledge-base Schema is used; this run does not evaluate Schema generation quality."],
        "cleanup": {"processes_stopped": True, "schema_removed": False, "redis_namespace_removed": False, "private_logs_removed": True}}
    processes, created_schema, proxy, stage, private_workspace = [], False, None, "preflight", None
    started = time.monotonic()
    try:
        require_prepared_runtime()
        with admin_engine.begin() as connection:
            # Alembic and application tables are not schema-qualified. A public
            # relation could shadow a missing isolated relation. Require a test
            # database with no public user tables before migrations can run.
            collision = connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT EXISTS "
                "(SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e'))"))
            require(not collision, "dedicated_test_database_required_public_tables_present")
            require(connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")), "pgvector_extension_must_be_preinstalled")
            connection.execute(CreateSchema(schema))
        created_schema = True
        require(redis_client.ping(), "test_redis_unavailable")
        with tempfile.TemporaryDirectory(prefix="rag-real-team-") as directory:
            workspace = Path(directory)
            private_workspace = workspace
            workspace.chmod(0o700)
            with ModelProxy(endpoints) as proxy, ExitStack() as clients:
                api_port, api_agent_port, worker_agent_port = free_ports(3)
                admin_password = secrets.token_urlsafe(24)
                common = dict(workspace=workspace, database_url=target.render_as_string(hide_password=False), redis_url=redis_url,
                    prefix=prefix, api_port=api_port, proxy=proxy, admin_password=admin_password)
                api_env = isolated_environment(role="api", opencode_port=api_agent_port, **common)
                worker_env = isolated_environment(role="worker", opencode_port=worker_agent_port, **common)
                # All replicas must share the synthetic authentication secret.
                worker_env["JWT_SECRET_KEY"] = api_env["JWT_SECRET_KEY"]
                stage = "process_startup"
                try:
                    processes.append(start_child(child_command("uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port), "--workers", "1"), api_env, workspace / "api.log"))
                    processes.append(start_child(child_command("scripts.run_worker"), worker_env, workspace / "worker.log"))
                    base = f"http://127.0.0.1:{api_port}"
                    wait_api(base, processes, timeout=startup_timeout)
                    admin = clients.enter_context(httpx.Client(base_url=base, timeout=210, trust_env=False))
                    request_json(admin, "POST", "/api/v1/auth/login", json={"username": "verify-admin", "password": admin_password})
                    stage = "synthetic_members"
                    kb = request_json(admin, "POST", "/api/v1/knowledge-bases", expected=201,
                        json={"name": "合成真实模型验收", "slug": "verify-real-" + uuid.uuid4().hex[:12]})["id"]
                    kb_path = "/api/v1/knowledge-bases/" + kb
                    request_json(admin, "PUT", kb_path + "/knowledge-graph/automation", json={"auto_build_enabled": False, "auto_schema_generation_enabled": False})
                    members = []
                    for number in range(3):
                        username, password = f"verify-member-{number}", secrets.token_urlsafe(24)
                        request_json(admin, "POST", "/api/v1/users", expected=201, json={"username": username,
                            "password": password, "display_name": "合成成员" + str(number), "role": "user", "is_active": True})
                        request_json(admin, "PUT", kb_path + "/members", json={"username": username, "role": "viewer"})
                        client = clients.enter_context(httpx.Client(base_url=base, timeout=210, trust_env=False))
                        request_json(client, "POST", "/api/v1/auth/login", json={"username": username, "password": password})
                        members.append(client)
                    stage = "real_upload_parse_embedding"
                    files = [("files", (name, content.encode("utf-8"), mime)) for name, mime, content in synthetic_documents()]
                    uploaded = request_json(admin, "POST", kb_path + "/documents/batch-upload", expected=202, files=files)
                    require(uploaded["accepted"] == 2 and not uploaded["rejected"], "synthetic_upload_rejected")
                    report["ingestion"] = wait_job(admin, kb, uploaded["job_id"], processes, timeout=job_timeout)
                    require(report["ingestion"]["status"] == "completed", "synthetic_ingestion_not_completed")
                    page = request_json(admin, "GET", kb_path + "/documents/page", params={"offset": 0, "limit": 10})
                    documents = {document["original_filename"]: document for document in page["items"]}
                    require(all(document["status"] == "ready" and document["chunk_count"] > 0 for document in documents.values()), "document_or_chunks_not_ready")
                    formal, draft = (documents[name]["id"] for name in ("晨星正式方案.txt", "晨星草案.md"))
                    report["ingestion"].update({"documents": len(documents), "chunks": sum(document["chunk_count"] for document in documents.values())})
                    # OCR is independent of generation availability. Check it
                    # before questions so an unavailable LLM cannot hide it.
                    stage = "optional_scanned_pdf"
                    report["ocr"] = {"status": "skipped", "reason": "--with-ocr was not requested"}
                    if with_ocr:
                        pdf, skip = scanned_pdf(workspace)
                        if pdf is None:
                            report["ocr"] = {"status": "skipped", "reason": skip}
                        else:
                            # Keep OCR facts separate from the two-document
                            # graph and evidence assertions below.
                            ocr_kb = request_json(admin, "POST", "/api/v1/knowledge-bases", expected=201,
                                json={"name": "合成扫描PDF验收", "slug": "verify-ocr-" + uuid.uuid4().hex[:12]})["id"]
                            ocr_path = "/api/v1/knowledge-bases/" + ocr_kb
                            request_json(admin, "PUT", ocr_path + "/knowledge-graph/automation", json={"auto_build_enabled": False, "auto_schema_generation_enabled": False})
                            uploaded_scan = request_json(admin, "POST", ocr_path + "/documents/batch-upload", expected=202,
                                files=[("files", ("synthetic-scan.pdf", pdf, "application/pdf"))])
                            require(uploaded_scan["accepted"] == 1, "scanned_pdf_upload_rejected")
                            scan_job = wait_job(admin, ocr_kb, uploaded_scan["job_id"], processes, timeout=job_timeout)
                            chunks = request_json(admin, "GET", ocr_path + "/documents/" + uploaded_scan["document_ids"][0] + "/chunks")
                            marker = "ORBITVERIFYMARKER" in re.sub(r"\s+", "", "".join(chunk["content"] for chunk in chunks)).upper()
                            report["ocr"] = {"status": "passed" if marker and scan_job["status"] == "completed" else "failed",
                                "image_only_pdf": True, "marker_found": marker, "chunks": len(chunks), "job": scan_job}
                    stage = "three_concurrent_real_questions"
                    cases = [("formal_facts", [formal], "请仅根据晨星正式方案回答：负责人是谁，正式上线日期是哪一天？不要引用草案。"),
                        ("conflicting_dates", [formal, draft], "请比较晨星正式方案与草案中的上线日期，是否存在冲突？明确列出两个日期并注明各自来源。"),
                        ("unanswerable", [formal, draft], "根据这些晨星资料，项目总部的详细街道地址是什么？如果没有记录请明确说明，不要猜测。")]
                    barrier = threading.Barrier(3)
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        futures = [pool.submit(stream_case, client, case, kb, ids, query, barrier)
                            for client, (case, ids, query) in zip(members, cases)]
                        report["chat_cases"] = [future.result() for future in futures]
                    stage = "real_opencode_graph_extraction"
                    build = request_json(admin, "POST", kb_path + "/knowledge-graph/build", expected=202, json={"mode": "pending", "graph_kind": "general",
                        "rebuild_entity_vectors": False, "rebuild_relation_vectors": False, "rebuild_communities": False})
                    report["graph_job"] = wait_job(admin, kb, build["id"], processes, timeout=job_timeout)
                    stats = request_json(admin, "GET", kb_path + "/knowledge-graph/stats")
                    service_nodes = request_json(admin, "GET", kb_path + "/knowledge-graph/entities", params={"query": "服务", "limit": 30})
                    names = {item["canonical_name"] for item in service_nodes}
                    report["graph"] = {**stats, "expected_service_entities": all(name in names for name in ("星河订单服务", "北辰支付服务"))}
                    report["graph"]["passed"] = report["graph_job"]["status"] == "completed" and stats["entities"] >= 2 and stats["relations"] >= 1 and stats["evidence"] >= 1 and report["graph"]["expected_service_entities"]
                    stage = "spoken_graph_filter_and_evidence"
                    filtered = request_json(members[0], "POST", kb_path + "/knowledge-graph/filter-preview",
                        json={"principles": ["只显示有原文证据的关系"], "max_nodes": 200, "max_edges": 400})
                    edges, nodes = filtered["edges"], {node["id"]: node["label"] for node in filtered["nodes"]}
                    expected_edge = expected_service_relationship(nodes, edges)
                    evidence_validated, evidence_checks = 0, []
                    chunk_cache = {}
                    for edge in edges:
                        for evidence in edge["evidence"]:
                            chunk_id = evidence["chunk_id"]
                            if chunk_id not in chunk_cache:
                                chunk_cache[chunk_id] = request_json(admin, "GET", kb_path + "/chunks/" + chunk_id)
                            chunk = chunk_cache[chunk_id]
                            valid = evidence["document_id"] == chunk["document_id"] and evidence["document_id"] in {formal, draft} and bool(evidence["quote"].strip()) and re.sub(r"\s+", "", evidence["quote"]) in re.sub(r"\s+", "", chunk["content"])
                            evidence_checks.append(valid)
                            evidence_validated += int(valid)
                    report["filter"] = {"nodes": len(nodes), "edges": len(edges), "all_edges_have_evidence": bool(edges) and all(edge["evidence"] for edge in edges),
                        "expected_service_relationship": bool(expected_edge), "evidence_validated": evidence_validated,
                        "all_evidence_quotes_in_original_chunks": bool(evidence_checks) and all(evidence_checks),
                        "all_principles_applied": len(filtered["plan"]["principles"]) == 1 and all(rule["status"] == "applied" for rule in filtered["plan"]["principles"]),
                        "per_principle_counts": filtered.get("per_principle_counts", [])}
                    report["filter"]["passed"] = all(report["filter"][key] for key in ("all_edges_have_evidence", "expected_service_relationship", "all_evidence_quotes_in_original_chunks", "all_principles_applied"))
                    stage = "assertions"
                    require(all(item["passed"] for item in report["chat_cases"]), "answer_acceptance_failed")
                    require(report["graph"]["passed"], "graph_acceptance_failed")
                    require(report["filter"]["passed"], "filter_evidence_acceptance_failed")
                    require(report["ocr"]["status"] != "failed", "ocr_acceptance_failed")
                    require(proxy.budget.counts["generation"] > 0 and proxy.budget.counts["embedding"] > 0, "real_models_not_called")
                    require(not any(proxy.budget.rejected.values()), "model_request_budget_exceeded")
                    report["status"] = "completed"
                finally:
                    report["cleanup"]["processes_stopped"] = stop_children(processes)
                    processes.clear()
            report["cleanup"]["private_logs_removed"] = True
    except Exception as exc:
        # Exception messages and tracebacks can contain upstream URL/API keys.
        # Only our fixed codes and exception class names leave this process.
        report["status"] = "failed"
        report["error"] = {"stage": stage, "type": type(exc).__name__,
            "code": str(exc) if isinstance(exc, AcceptanceError) else "verification_step_failed"}
    finally:
        report["cleanup"]["private_logs_removed"] = private_workspace is None or not private_workspace.exists()
        if processes:
            report["cleanup"]["processes_stopped"] = stop_children(processes)
        if proxy is not None:
            report["model_requests"] = proxy.budget.snapshot()
        try:
            report["cleanup"]["redis_namespace_removed"] = cleanup_redis(redis_client, prefix)
        except Exception:
            report["cleanup"]["redis_namespace_removed"] = False
        try:
            if created_schema:
                require_cleanup_names(schema, prefix)
                with admin_engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
                    report["cleanup"]["schema_removed"] = not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:name)"), {"name": schema})
            else:
                report["cleanup"]["schema_removed"] = True
        except Exception:
            report["cleanup"]["schema_removed"] = False
        admin_engine.dispose()
        redis_client.close()
        if not all(report["cleanup"].values()):
            report["status"] = "failed"
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-env", required=True, type=Path, help="Read only model settings from this file; never database, Redis, agent or application secrets")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--with-ocr", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=180)
    parser.add_argument("--job-timeout", type=int, default=480)
    args = parser.parse_args(argv)
    try:
        require(10 <= args.startup_timeout <= 600 and 30 <= args.job_timeout <= 1200, "invalid_timeout")
        report = verify(os.getenv("TEST_POSTGRES_URL"), os.getenv("TEST_REDIS_URL"), args.model_env,
            with_ocr=args.with_ocr, startup_timeout=args.startup_timeout, job_timeout=args.job_timeout)
    except Exception as exc:
        report = {"status": "failed", "error": {"stage": "configuration", "type": type(exc).__name__,
            "code": str(exc) if isinstance(exc, AcceptanceError) else "verification_configuration_failed"}}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    # The report has no URLs, credentials, prompts, responses or private logs.
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.report), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
