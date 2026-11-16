"""Server-owned agent configuration; task payloads never select URLs or secrets."""

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import DATA_DIR

OPENCODE_VERSION = "2.2.5"
OPENCODE_COMMIT = "9bbb8ce18c80f099e0e448775f59d965ead40a21"


def managed_endpoint(value: str) -> bool:
    """The project can spawn only a plain HTTP, root-path loopback server."""
    url = urlsplit(value)
    return url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"} and url.path in {"", "/"}


def same_service_endpoint(first: str, second: str) -> bool:
    """Treat root slashes and loopback aliases as the same managed service."""
    if managed_endpoint(first) and managed_endpoint(second):
        return (urlsplit(first).port or 80) == (urlsplit(second).port or 80)
    return first.rstrip("/") == second.rstrip("/")


def safe_service_url(value: str, *, allow_internal_callback: bool = False) -> str:
    url = urlsplit(value)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError("Agent service URL must not contain credentials, query or fragment")
    trusted = {host.strip().lower() for host in os.getenv("AGENT_TRUSTED_HTTP_HOSTS", "").split(",") if host.strip()}
    internal = allow_internal_callback and url.hostname in trusted
    if url.scheme != "https" and not (url.scheme == "http" and (url.hostname in {"localhost", "127.0.0.1", "::1"} or internal)):
        raise ValueError("Agent service requires HTTPS or loopback HTTP")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Agent service port is invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class AgentSettings:
    backend: str = "opencode"
    base_url: str = "http://127.0.0.1:4096"
    username: str = "opencode"
    password: str = field(default="", repr=False)
    model: str = ""
    timeout_seconds: float = 180
    poll_seconds: float = 0.25
    max_steps: int = 12
    callback_url: str = "http://127.0.0.1:8080/api/v1/agent-tools"
    workspace_root: Path = field(default_factory=lambda: DATA_DIR / "agents" / "workspaces")
    expected_version: str = OPENCODE_VERSION
    cleanup_sessions: bool = True

    def __post_init__(self):
        safe_service_url(self.base_url)
        safe_service_url(self.callback_url, allow_internal_callback=True)
        if not 1 <= self.max_steps <= 64 or not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("Invalid agent step/time budget")
        if not 0.01 <= self.poll_seconds <= 10:
            raise ValueError("Invalid agent polling interval")
        if self.backend == "opencode" and self.username != "opencode":
            raise ValueError("OpenCode V2 fixes the authentication username to opencode")
        if self.backend == "opencode" and urlsplit(self.base_url).hostname not in {"localhost", "127.0.0.1", "::1"} and not self.password:
            raise ValueError("Remote OpenCode requires authentication")


def default_settings() -> AgentSettings:
    from app.core.config import ANSWER_LLM_MODEL, ANSWER_LLM_API_KEY, ANSWER_LLM_BASE_URL, OLLAMA_LLM_MODEL
    model = ANSWER_LLM_MODEL if ANSWER_LLM_API_KEY and ANSWER_LLM_BASE_URL else OLLAMA_LLM_MODEL

    return AgentSettings(
        backend=os.getenv("AGENT_BACKEND", "opencode"),
        base_url=os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096"),
        username=os.getenv("OPENCODE_SERVER_USERNAME", "opencode"),
        password=os.getenv("OPENCODE_PASSWORD") or os.getenv("OPENCODE_SERVER_PASSWORD", ""),
        model=os.getenv("OPENCODE_MODEL") or f"rag/{model}",
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "12")),
        callback_url=os.getenv("AGENT_TOOL_URL") or f"http://127.0.0.1:{os.getenv('APP_PORT', '8080')}/api/v1/agent-tools",
        workspace_root=Path(os.getenv("AGENT_WORKSPACE_DIR", str(DATA_DIR / "agents" / "workspaces"))),
    )


def profile_settings(name: str | None = None) -> AgentSettings:
    settings = default_settings()
    if name in {None, "default"}:
        return settings
    profiles = configured_profiles()
    if name not in profiles:
        raise ValueError("Unknown server-managed agent profile")
    allowed = {"backend", "base_url", "username", "password", "model", "timeout_seconds", "max_steps"}
    values = profiles[name]
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("Invalid server-managed agent profile")
    return replace(settings, **values)


def configured_profiles() -> dict:
    profiles = json.loads(os.getenv("AGENT_BACKEND_PROFILES_JSON", "{}"))
    if not isinstance(profiles, dict) or "default" in profiles:
        raise ValueError("Invalid agent profile registry")
    return profiles
