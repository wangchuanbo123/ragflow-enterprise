"""Create private server settings, preserving existing model and auth settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    """Read our one-line dotenv values without expanding secret ${...} text."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def configure_server(root: Path = ROOT) -> Path:
    target = root / ".env.server"
    if target.is_symlink():
        raise ValueError("Server settings must not be a symbolic link")
    if target.exists():
        from scripts.server_backup import _restrict_private
        _restrict_private(target)
        return target
    old = read_env(root / ".env")
    # Import previous private settings into the canonical public namespace.
    for key, value in list(old.items()):
        if key.startswith("OPENCODE_"):
            old.setdefault("CODECODE_" + key.removeprefix("OPENCODE_"), value)
    if old.get("AGENT_BACKEND") == "opencode":
        old["AGENT_BACKEND"] = "codecode"
    settings = read_env(root / "deploy/server.env.example")
    prefixes = ("LLM_", "ANSWER_LLM_", "REWRITE_LLM_", "ROUTER_LLM_", "EMBEDDING_", "RERANKER_", "OLLAMA_", "GRAPH_", "BACKUP_", "CONNECTOR_")
    for key, value in old.items():
        if (key.startswith(prefixes) and key != "GRAPH_STORE_PROVIDER") or key in {"CODECODE_MODEL", "AGENT_TIMEOUT_SECONDS", "AGENT_MAX_STEPS", "AGENT_BACKEND_PROFILES_JSON", "INITIAL_ADMIN_USERNAME"}:
            settings[key] = value
    registry = json.loads(old.get("CONNECTOR_CREDENTIALS_JSON") or "{}")
    for entry in registry.values():
        if isinstance(entry, dict) and str(entry.get("secret_ref", "")).startswith("env://"):
            name = entry["secret_ref"].removeprefix("env://")
            if name in old or name in os.environ:
                settings[name] = old.get(name, os.environ.get(name, ""))
    for key in ("JWT_SECRET_KEY", "INITIAL_ADMIN_PASSWORD"):
        settings[key] = old.get(key) or secrets.token_urlsafe(48)
    if len(settings["INITIAL_ADMIN_PASSWORD"]) < 12:
        # This value seeds only a new empty database. Imported accounts retain
        # their original password hashes and are never reset by configuration.
        settings["INITIAL_ADMIN_PASSWORD"] = secrets.token_urlsafe(48)
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        settings[key] = secrets.token_hex(32)
    for key, value in list(settings.items()):
        if key.endswith("_BASE_URL") and value:
            url = urlsplit(value)
            if url.hostname in {"localhost", "127.0.0.1", "::1"}:
                if url.username or url.password:
                    raise ValueError("Model URL credentials must use the dedicated API key setting")
                host = "host.docker.internal" + (f":{url.port}" if url.port else "")
                settings[key] = urlunsplit((url.scheme, host, url.path, url.query, url.fragment))
    # Preserve the working embedding/reranking model: changing embeddings would
    # invalidate all migrated vectors, even when dimensions happen to match.
    content = "# Private server settings. Keep this file with encrypted backups.\n"
    content += "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in settings.items()) + "\n"
    from scripts.server_backup import _private_write
    _private_write(target, content.encode("utf-8"))
    return target


def main() -> int:
    configure_server()
    print("服务器配置已准备，原模型和登录配置已保留；密钥未显示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
