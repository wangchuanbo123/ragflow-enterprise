"""Lifecycle of a dedicated codecode process; never uses personal agent config."""

import json
import logging
import os
import secrets
import hashlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.core.branding import public_text
from app.agents.settings import codecode_env, normalized_backend, CODECODE_VERSION, CODECODE_COMMIT, default_settings, managed_endpoint, same_service_endpoint, configured_profiles, profile_settings

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_process = None
_managed_executable = None
_log_thread = None
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def runtime_version(output: str) -> str | None:
    """Parse the standalone CLI banner without accepting additional output."""
    match = re.fullmatch(r"(?:(?:codecode|opencode)\s+)?v?(\d+\.\d+\.\d+)", output.strip())
    return match.group(1) if match else None


def _within_project(path: Path) -> Path:
    lexical_root = Path(os.path.abspath(PROJECT_ROOT))
    candidate = Path(os.path.abspath(path if path.is_absolute() else lexical_root / path))
    if not candidate.is_relative_to(lexical_root):
        raise ValueError("codecode executable must be inside this project")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(lexical_root.resolve()):
        raise ValueError("codecode executable or metadata resolves outside this project")
    return resolved


def project_runtime_path() -> Path:
    return PROJECT_ROOT / "integrations" / "codecode" / "runtime" / ("codecode.exe" if os.name == "nt" else "codecode")


def project_runtime_executable(value: str | None = None) -> str:
    """Use only a project-contained native binary from the pinned source build."""
    configured = value if value is not None else codecode_env("EXECUTABLE").strip()
    if configured:
        supplied = Path(configured)
        if not supplied.is_absolute() and "/" not in str(configured) and "\\" not in str(configured):
            raise ValueError("codecode executable requires an explicit project-local path; PATH lookup is disabled")
        path = _within_project(supplied)
    else:
        path = _within_project(project_runtime_path())
    if path.suffix.lower() in {".cmd", ".bat", ".ps1", ".sh", ".js", ".ts", ".py"}:
        raise ValueError("codecode executable must be a project-built native binary, not a command shim")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise ValueError("codecode executable must be a project-built native .exe")
    if not path.is_file():
        raise FileNotFoundError("Project codecode executable has not been built")
    manifest_path = _within_project(project_runtime_path().parent / "build.json")
    if not manifest_path.is_file():
        raise FileNotFoundError("Project codecode source-build metadata is missing")
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("Project codecode build metadata is invalid") from exc
    if (not isinstance(metadata, dict) or metadata.get("method") != "source"
        or metadata.get("source_commit") != CODECODE_COMMIT
        or metadata.get("source_tag") != f"v{CODECODE_VERSION}"
        or metadata.get("version") != CODECODE_VERSION):
        raise ValueError("codecode executable must match the pinned project source build")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        signature = stream.read(4)
        digest.update(signature)
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    valid_magic = signature[:2] == b"MZ" if os.name == "nt" else signature in {
        b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    }
    if not valid_magic or metadata.get("executable_sha256") != digest.hexdigest():
        raise ValueError("Project codecode executable is not the verified source-built artifact")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ValueError("Project codecode executable does not have execute permission")
    return str(path)


def resolve_runtime_executable(value: str) -> str:
    """Compatibility entry point, with no global or npm-shim resolution."""
    return project_runtime_executable(value)


def ensure_project_runtime() -> str:
    """Build missing project artifacts; never fall back to an installed CLI."""
    try:
        return project_runtime_executable()
    except FileNotFoundError:
        pass
    logger.info("Preparing the project's pinned codecode source and native runtime")
    root = _within_project(PROJECT_ROOT / "data" / "agents" / "build-environment")
    root.mkdir(parents=True, exist_ok=True)
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "SYSTEMDRIVE",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"HOME": str(root), "USERPROFILE": str(root), "APPDATA": str(root / "config"),
                "LOCALAPPDATA": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    prepared = subprocess.run([sys.executable, "-m", "scripts.prepare_codecode", "--install-runtime"],
        cwd=str(PROJECT_ROOT), env=env, timeout=1800, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for output in (getattr(prepared, "stdout", ""), getattr(prepared, "stderr", "")):
        if output:
            logger.info("%s", public_text(output.rstrip()))
    if prepared.returncode:
        raise RuntimeError("Project codecode source preparation failed")
    return project_runtime_executable()


def runtime_config() -> dict:
    from app.core import config as cfg
    from app.agents.codecode import SYSTEM_PROMPT

    remote = bool(cfg.ANSWER_LLM_API_KEY and cfg.ANSWER_LLM_BASE_URL)
    anthropic = cfg.ANSWER_LLM_PROVIDER in {"anthropic", "anthropic_compatible"} and remote
    model = cfg.ANSWER_LLM_MODEL if remote else cfg.OLLAMA_LLM_MODEL
    base_url = cfg.ANSWER_LLM_BASE_URL if remote else cfg.OLLAMA_BASE_URL.rstrip("/") + "/v1"
    if anthropic:
        # Existing application configuration accepts a host, /v1, or the full
        # /v1/messages endpoint. The native provider appends /messages.
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1/messages"):
            base_url = base_url[:-len("/messages")]
        elif not base_url.endswith("/v1"):
            base_url += "/v1"
    provider = {
        "package": "@opencode/ai/providers/anthropic" if anthropic else "@opencode/ai/providers/openai-compatible",
        "name": "RAG configured model",
        "settings": {
            "baseURL": base_url,
            "apiKey": "{env:RAG_AGENT_API_KEY}", "timeout": 60000,
        },
        "models": {model: {"name": model, "capabilities": {"tools": True}, "limit": {"context": 32768, "output": 8192}}},
    }
    if anthropic:
        provider["headers"] = {"Authorization": "Bearer {env:RAG_AGENT_API_KEY}"}
    settings = default_settings()
    max_steps = settings.max_steps
    # A managed server has a private provider registry. Populate only explicit
    # server profiles using this endpoint; external profiles configure their own
    # providers. A model reference alone cannot define a native reasoning variant.
    for profile in [settings, *(profile_settings(name) for name in configured_profiles())]:
        if profile.backend != "codecode" or not same_service_endpoint(profile.base_url, settings.base_url):
            continue
        provider_id, separator, identifier = profile.model.partition("/")
        if not separator or provider_id != "rag" or not identifier or "#" in identifier:
            raise ValueError("Managed codecode profiles require rag/model without a variant; use an external backend for configured native variants")
        provider["models"].setdefault(identifier, {"name": identifier, "capabilities": {"tools": True},
                                                "limit": {"context": 32768, "output": 8192}})
        max_steps = max(max_steps, profile.max_steps)
    permissions = [{"action": "*", "resource": "*", "effect": "deny"},
                   {"action": "knowledge_*", "resource": "*", "effect": "allow"}]
    policies = [{"action": action, "resource": "*", "effect": "deny"} for action in (
        "provider.use", "tool.use", "integration.connect", "integration.use", "mcp.use", "skill.use", "plugin.use")]
    policies.extend([
        {"action": "provider.use", "resource": "rag", "effect": "allow"},
        {"action": "integration.use", "resource": "rag", "effect": "allow"},
        {"action": "tool.use", "resource": "knowledge_*:*", "effect": "allow"},
        {"action": "mcp.use", "resource": "knowledge", "effect": "allow"},
    ])
    return {
        "providers": {"rag": provider},
        "model": codecode_env("MODEL") or f"rag/{model}", "default_agent": "knowledge",
        "share": "disabled", "update": "disable", "snapshots": False, "warming": False,
        "formatter": False, "lsp": False, "skills": [], "instructions": [],
        "permissions": permissions, "policies": policies,
        "agents": {"knowledge": {"description": "Scoped knowledge lifecycle agent", "mode": "primary",
                   "system": SYSTEM_PROMPT, "steps": max_steps, "permissions": permissions},
                   "title": {"disabled": True}, "summary": {"disabled": True}},
        "mcp": {"servers": {}},
    }


def runtime_environment(root, password: str) -> dict:
    from app.core import config as cfg

    root = Path(root).resolve()
    # V2's CLI exposes project discovery control but no switch to skip platform
    # managed config. Windows uses our private ProgramData. On other platforms
    # refuse ambient managed sources rather than claim to ignore them.
    if os.name != "nt":
        system = Path("/Library/Application Support/opencode" if sys.platform == "darwin" else "/etc/opencode")
        mdm = Path("/Library/Managed Preferences")
        if system.exists() or (sys.platform == "darwin" and any(mdm.glob("**/ai.opencode.managed.plist"))):
            raise ValueError("Host-managed codecode configuration cannot be isolated by this CLI; use an explicitly managed external backend")
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
               "SYSTEMDRIVE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("config", "data", "cache", "state", "tmp", "system"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(root / "config"),
        "LOCALAPPDATA": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"), "TMPDIR": str(root / "tmp"),
        "ProgramData": str(root / "system"),
        "OPENCODE_CONFIG_DIR": str(root / "config"),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_FILEWATCHER_DISABLE": "true", "OPENCODE_DISABLE_FFF": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_PASSWORD": password,
        "OPENCODE_CONFIG_CONTENT": json.dumps(runtime_config(), ensure_ascii=False),
        "RAG_AGENT_API_KEY": cfg.ANSWER_LLM_API_KEY if cfg.ANSWER_LLM_BASE_URL and cfg.ANSWER_LLM_API_KEY else "ollama",
    })
    return env


def _drain_runtime_log(stream, path: Path) -> bool:
    """Rebrand the private child output before any bytes reach the log file."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    reader = getattr(stream, "read1", stream.read)
    try:
        with path.open("ab") as log:
            while chunk := reader(8192):
                pending += decoder.decode(chunk)
                # Keep a short suffix so a name split between pipe reads is
                # replaced as one word, while keeping memory use bounded.
                boundary = max(0, len(pending) - 7)
                for match in re.finditer("opencode", pending, re.IGNORECASE):
                    if match.start() < boundary < match.end():
                        boundary = match.start()
                        break
                if boundary:
                    log.write(public_text(pending[:boundary]).encode("utf-8"))
                    log.flush()
                    pending = pending[boundary:]
            pending += decoder.decode(b"", final=True)
            log.write(public_text(pending).encode("utf-8"))
        return True
    except OSError:
        logger.warning("codecode runtime log stream could not be written")
        return False
    finally:
        stream.close()


def _normalize_runtime_log(path: Path) -> None:
    """Atomically apply current branding to a previous managed runtime log."""
    if not path.is_file():
        return
    import uuid

    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if not _drain_runtime_log(path.open("rb"), temporary):
            raise OSError("codecode runtime log migration failed")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def start_managed_codecode() -> dict:
    global _process, _managed_executable, _log_thread
    from app.core.config import APP_PROFILE, DATA_DIR
    from app.agents.codecode import CodeCodeAgentBackend

    if (APP_PROFILE == "test" or normalized_backend(os.getenv("AGENT_BACKEND", "codecode")) != "codecode"
        or codecode_env("AUTO_START", "true").lower() not in {"1", "true", "yes"}):
        return {"status": "disabled"}
    with _lock:
        if _process is not None and _process.poll() is None:
            if managed_runtime_running():
                health = CodeCodeAgentBackend(default_settings()).health()
                if health["status"] == "ok":
                    return health
            # A handle retained from older startup code is not proof that it
            # was launched from this project's verified source build.
            stop_managed_codecode()
        settings = default_settings()
        parsed = urlsplit(settings.base_url)
        if not managed_endpoint(settings.base_url):
            return {"status": "external", "detail": "Remote codecode managed externally"}
        try:
            executable = ensure_project_runtime()
            version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "fail", "detail": "codecode executable is unavailable or version check timed out"}
        except (ValueError, RuntimeError):
            return {"status": "fail", "detail": "codecode executable must be the verified project source build; prepare it with scripts.prepare_codecode --install-runtime"}
        if version.returncode or runtime_version(version.stdout) != CODECODE_VERSION:
            return {"status": "fail", "detail": f"codecode {CODECODE_VERSION} required"}
        # Refuse to silently attach to another process occupying the dedicated
        # port. Configure explicit external mode for a separately managed server.
        import socket

        with socket.socket(socket.AF_INET6 if parsed.hostname == "::1" else socket.AF_INET) as probe:
            if probe.connect_ex((parsed.hostname, parsed.port or 80)) == 0:
                return {"status": "fail", "detail": "Dedicated codecode port is occupied; use an unused port or explicit external mode"}
        password = settings.password or secrets.token_urlsafe(32)
        root = DATA_DIR / "agents" / "runtime" / f"v{CODECODE_VERSION}"
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / "server.log"
        try:
            environment = runtime_environment(root, password)
            _normalize_runtime_log(log_path)
            _process = subprocess.Popen(
                [executable, "serve", "--hostname", parsed.hostname, "--port", str(parsed.port or 80)],
                cwd=str(root), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _log_thread = threading.Thread(target=_drain_runtime_log, args=(_process.stdout, log_path),
                                           name="codecode-runtime-log", daemon=True)
            _log_thread.start()
        except (OSError, ValueError) as exc:
            return {"status": "fail", "detail": f"codecode isolated startup configuration failed: {public_text(str(exc))}"}
        os.environ["CODECODE_PASSWORD"] = password
        _managed_executable = executable
        backend = CodeCodeAgentBackend(default_settings())
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and _process.poll() is None:
            health = backend.health()
            if health["status"] == "ok":
                return health
            time.sleep(0.25)
        stop_managed_codecode()
        return {"status": "fail", "detail": f"codecode startup failed; see data/agents/runtime/v{CODECODE_VERSION}/server.log"}


def stop_managed_codecode() -> None:
    global _process, _managed_executable, _log_thread
    process, _process = _process, None
    log_thread, _log_thread = _log_thread, None
    _managed_executable = None
    if process is None or process.poll() is not None:
        if log_thread is not None:
            log_thread.join(timeout=3)
        return
    import psutil

    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if log_thread is not None:
        log_thread.join(timeout=3)


def managed_runtime_running() -> bool:
    if _process is None or _process.poll() is not None or _managed_executable is None:
        return False
    try:
        return _within_project(Path(_managed_executable)).is_file()
    except (ValueError, OSError):
        return False


# Kept for callers upgrading independently of the application configuration.
start_managed_opencode = start_managed_codecode
stop_managed_opencode = stop_managed_codecode
