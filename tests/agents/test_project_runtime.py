"""Managed agents never resolve or execute a personal/global codecode CLI."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import runtime
from app.agents.settings import CODECODE_COMMIT, CODECODE_VERSION


def built_runtime(root, monkeypatch):
    monkeypatch.setattr(runtime, "PROJECT_ROOT", root)
    monkeypatch.delenv("CODECODE_EXECUTABLE", raising=False)
    executable = runtime.project_runtime_path()
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes((b"MZ\x00\x00" if os.name == "nt" else b"\x7fELF") + b"synthetic-project-source-build")
    executable.chmod(0o755)
    metadata = {"method": "source", "source_commit": CODECODE_COMMIT, "source_tag": f"v{CODECODE_VERSION}",
                "version": CODECODE_VERSION, "bun_version": "1.3.14", "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    (executable.parent / "build.json").write_text(json.dumps(metadata), encoding="utf-8")
    return executable


def test_only_project_source_build_and_matching_internal_override_are_accepted(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    assert runtime.project_runtime_executable() == str(executable.resolve())
    alternate = runtime.PROJECT_ROOT / "source-build" / executable.name
    alternate.parent.mkdir()
    alternate.write_bytes(executable.read_bytes())
    alternate.chmod(0o755)
    assert runtime.project_runtime_executable(str(alternate)) == str(alternate.resolve())
    assert runtime.project_runtime_executable(alternate.relative_to(runtime.PROJECT_ROOT).as_posix()) == str(alternate.resolve())


def test_external_and_bare_cli_overrides_are_rejected_without_execution(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    outside = tmp_path / executable.name
    outside.write_bytes(executable.read_bytes())
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    for value in (str(outside), "../" + outside.name, "opencode", "opencode.exe"):
        monkeypatch.setenv("CODECODE_EXECUTABLE", value)
        with pytest.raises(ValueError):
            runtime.ensure_project_runtime()
    assert calls == []


def test_project_shims_and_modified_or_unproven_binaries_are_rejected(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    shim = executable.parent / "opencode.cmd"
    shim.write_text("@opencode %*", encoding="utf-8")
    with pytest.raises(ValueError):
        runtime.project_runtime_executable(str(shim))
    executable.write_bytes(executable.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        runtime.project_runtime_executable()
    executable = built_runtime(tmp_path / "project", monkeypatch)
    metadata_path = executable.parent / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["method"] = "npm"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        runtime.project_runtime_executable()


def test_native_path_resolving_outside_project_is_rejected(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    outside = tmp_path / executable.name
    outside.write_bytes(executable.read_bytes())
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        return outside if path == executable else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="outside"):
        runtime.project_runtime_executable()


def test_missing_project_runtime_runs_project_preparation_not_global_cli(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(runtime, "PROJECT_ROOT", root)
    monkeypatch.delenv("CODECODE_EXECUTABLE", raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", "not-for-build")
    commands = []
    def prepare(command, **kwargs):
        commands.append((command, kwargs))
        built_runtime(root, monkeypatch)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime.subprocess, "run", prepare)
    assert runtime.ensure_project_runtime() == str(runtime.project_runtime_path().resolve())
    assert len(commands) == 1
    assert commands[0][0][1:] == ["-m", "scripts.prepare_codecode", "--install-runtime"]
    assert commands[0][1]["cwd"] == str(root)
    assert "UNRELATED_SECRET" not in commands[0][1]["env"]
    assert "JWT_SECRET_KEY" not in commands[0][1]["env"]
    assert commands[0][1]["env"]["HOME"].startswith(str(root))


def test_live_contract_path_does_not_need_path_lookup(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    # Resolving the runtime works even with no global CLI on PATH.
    monkeypatch.setenv("PATH", "")
    assert runtime.project_runtime_executable() == str(executable.resolve())


def test_unproven_existing_process_is_not_considered_the_project_runtime(tmp_path, monkeypatch):
    executable = built_runtime(tmp_path / "project", monkeypatch)
    monkeypatch.setattr(runtime, "_process", SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(runtime, "_managed_executable", None)
    assert not runtime.managed_runtime_running()
    monkeypatch.setattr(runtime, "_managed_executable", str(executable))
    assert runtime.managed_runtime_running()
    monkeypatch.setattr(runtime, "_managed_executable", str(tmp_path / "global-opencode.exe"))
    assert not runtime.managed_runtime_running()


def test_explicit_external_mode_and_replacement_backend_do_not_prepare_or_execute(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_AUTO_START", "false")
    monkeypatch.setattr(runtime, "ensure_project_runtime", lambda: pytest.fail("External service must not prepare a local CLI"))
    assert runtime.start_managed_codecode()["status"] == "disabled"
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setenv("AGENT_BACKEND", "replacement")
    assert runtime.start_managed_codecode()["status"] == "disabled"


@pytest.mark.parametrize("banner", ["2.2.5", "v2.2.5", "opencode v2.2.5\n"])
def test_runtime_parses_the_verified_v2_standalone_banner(banner):
    assert runtime.runtime_version(banner) == "2.2.5"


@pytest.mark.parametrize("banner", ["personal opencode v2.2.5", "opencode v2.2.5\nwarning", "2.2.5-dev", "version 2.2.5"])
def test_runtime_rejects_unknown_version_output(banner):
    assert runtime.runtime_version(banner) is None


def test_v2_environment_isolates_all_config_and_credentials(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "ANSWER_LLM_API_KEY", "synthetic-selected-key")
    monkeypatch.setattr(config, "ANSWER_LLM_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setattr(config, "ANSWER_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENCODE_CONFIG", "host-personal-config.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"permissions":[{"action":"*","resource":"*","effect":"allow"}]}')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unselected-secret")
    monkeypatch.setenv("CODECODE_SERVER_USERNAME", "codecode")
    environment = runtime.runtime_environment(tmp_path, "synthetic-password")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_STATE_HOME", "TEMP", "TMP", "TMPDIR", "ProgramData", "OPENCODE_CONFIG_DIR"):
        assert Path(environment[key]).is_relative_to(tmp_path)
    assert environment["OPENCODE_PASSWORD"] == "synthetic-password"
    assert environment["RAG_AGENT_API_KEY"] == "synthetic-selected-key"
    assert "OPENCODE_CONFIG" not in environment and "ANTHROPIC_API_KEY" not in environment
    assert "OPENCODE_PURE" not in environment and "OPENCODE_SERVER_USERNAME" not in environment
    native = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert not {"provider", "agent", "permission", "small_model", "autoupdate"} & native.keys()
    assert native["update"] == "disable" and native["snapshots"] is False and native["warming"] is False
    assert native["agents"]["knowledge"]["system"]
    provider = native["providers"]["rag"]
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["apiKey"] == "{env:RAG_AGENT_API_KEY}"
    assert provider["models"][config.ANSWER_LLM_MODEL]["capabilities"]["tools"] is True
    def decision(action, resource):
        import fnmatch
        matches = [item["effect"] for item in native["policies"] if item["action"] == action and fnmatch.fnmatchcase(resource, item["resource"])]
        return matches[-1] if matches else None
    assert decision("tool.use", "shell:whoami") == "deny"
    assert decision("tool.use", "read:C:/Users/personal-secret") == "deny"
    assert decision("tool.use", "knowledge_get_schema:*") == "allow"
    assert decision("provider.use", "anthropic") == "deny"
    assert decision("provider.use", "rag") == "allow"
    assert decision("mcp.use", "personal") == "deny"
    assert decision("mcp.use", "knowledge") == "allow"


def test_settings_fixed_v2_username_and_password_precedence(monkeypatch):
    from app.agents.settings import AgentSettings, default_settings
    monkeypatch.setenv("CODECODE_PASSWORD", "synthetic-native-password")
    monkeypatch.setenv("CODECODE_SERVER_PASSWORD", "synthetic-legacy-password")
    monkeypatch.setenv("CODECODE_SERVER_USERNAME", "codecode")
    assert default_settings().password == "synthetic-native-password"
    monkeypatch.delenv("CODECODE_PASSWORD")
    assert default_settings().password == "synthetic-legacy-password"
    with pytest.raises(ValueError, match="username"):
        AgentSettings(username="misleading-username")


def test_remote_default_backend_does_not_require_managed_local_process(monkeypatch):
    from app.agents.factory import get_agent_backend
    from app.core import config
    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_BASE_URL", "https://synthetic.example")
    monkeypatch.setenv("CODECODE_PASSWORD", "synthetic-password")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setattr(runtime, "managed_runtime_running", lambda: pytest.fail("External backend needs no local handle"))
    assert get_agent_backend().settings.base_url == "https://synthetic.example"


def test_managed_profile_models_are_registered_but_external_models_are_not(monkeypatch):
    from app.core import config
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("CODECODE_MODEL", "rag/main")
    monkeypatch.setenv("AGENT_MAX_STEPS", "6")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", json.dumps({
        "backup": {"model": "rag/backup", "base_url": "http://localhost:4096/", "max_steps": 20},
        "remote": {"model": "outside/external-model#native-variant", "base_url": "https://synthetic.example", "password": "synthetic"},
        "replacement": {"backend": "custom", "model": "custom/ignored"},
    }))
    native = runtime.runtime_config()
    assert set(native["providers"]["rag"]["models"]) == {config.ANSWER_LLM_MODEL if config.ANSWER_LLM_BASE_URL and config.ANSWER_LLM_API_KEY else config.OLLAMA_LLM_MODEL, "main", "backup"}
    assert native["agents"]["knowledge"]["steps"] == 20


@pytest.mark.parametrize("reference", ["other/model", "rag/test#high", "unqualified"])
def test_unsupported_managed_profile_references_fail_explicitly(monkeypatch, reference):
    monkeypatch.setenv("CODECODE_MODEL", "rag/main")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", json.dumps({"unsupported": {"model": reference}}))
    with pytest.raises(ValueError, match="Managed codecode profiles"):
        runtime.runtime_config()


def test_loopback_profile_alias_cannot_bypass_managed_runtime_proof(monkeypatch):
    from app.agents.factory import get_agent_backend
    from app.agents.contracts import AgentUnavailableError
    from app.core import config
    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setenv("CODECODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", '{"alias":{"base_url":"http://localhost:4096/"}}')
    monkeypatch.setattr(runtime, "managed_runtime_running", lambda: False)
    with pytest.raises(AgentUnavailableError):
        get_agent_backend("alias")


def test_service_urls_without_port_use_http_default_not_opencode_convention():
    from app.agents.settings import same_service_endpoint
    assert same_service_endpoint("http://127.0.0.1", "http://localhost:80/")
    assert not same_service_endpoint("http://127.0.0.1", "http://localhost:4096")


def test_managed_start_uses_v2_arguments_password_and_private_versioned_data(tmp_path, monkeypatch):
    import socket
    from app.core import config
    from app.agents.codecode import CodeCodeAgentBackend
    executable = built_runtime(tmp_path / "project", monkeypatch)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    monkeypatch.setattr(config, "APP_PROFILE", "local")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CODECODE_PASSWORD", "synthetic-server-password")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setenv("CODECODE_MODEL", "rag/test")
    monkeypatch.setenv("AGENT_BACKEND_PROFILES_JSON", "{}")
    monkeypatch.setattr(runtime, "_process", None)
    monkeypatch.setattr(runtime, "_managed_executable", None)
    monkeypatch.setattr(runtime, "ensure_project_runtime", lambda: str(executable))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="opencode v2.2.5\n"))
    monkeypatch.setattr(CodeCodeAgentBackend, "health", lambda self: {"status": "ok"})
    calls = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(poll=lambda: None, stdout=__import__("io").BytesIO(b"opencode ready\n")))
    assert runtime.start_managed_codecode()["status"] == "ok"
    command, arguments = calls[0]
    assert command == [str(executable), "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    assert arguments["cwd"] == str(tmp_path / "data" / "agents" / "runtime" / "v2.2.5")
    assert arguments["env"]["OPENCODE_PASSWORD"] == "synthetic-server-password"
    assert "--pure" not in command and "OPENCODE_PURE" not in arguments["env"]
    assert runtime.managed_runtime_running()
