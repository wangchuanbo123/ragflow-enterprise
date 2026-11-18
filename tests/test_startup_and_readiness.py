import io
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import start


def test_startup_resolves_windows_command_shim(monkeypatch):
    commands = []
    monkeypatch.setattr(start.shutil, "which", lambda command: "C:/Node/npm.CMD")
    monkeypatch.setattr(start.subprocess, "run", lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0))
    assert start._run(["npm", "ci"]) == 0
    assert commands == [["C:/Node/npm.CMD", "ci"]]


def test_startup_loads_dotenv_before_port_selection(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("APP_PORT=9137\n", encoding="utf-8")
    monkeypatch.setattr(start, "ENV_FILE", path)
    monkeypatch.delenv("APP_PORT", raising=False)
    start._load_environment()
    assert start.os.getenv("APP_PORT") == "9137"
    monkeypatch.delenv("APP_PORT")


def test_existing_web_build_is_refreshed_when_sources_change(tmp_path, monkeypatch):
    web = tmp_path / "web"
    dist = web / "dist"
    dist.mkdir(parents=True)
    (web / "src").mkdir()
    source = web / "src" / "App.vue"
    source.write_text("first", encoding="utf-8")
    (web / "package.json").write_text("{}", encoding="utf-8")
    (dist / "index.html").write_text("old", encoding="utf-8")
    monkeypatch.setattr(start, "WEB_DIR", web)
    monkeypatch.setattr(start, "WEB_DIST_DIR", dist)
    monkeypatch.setattr(start.shutil, "which", lambda _: "npm.CMD")
    commands = []
    monkeypatch.setattr(start, "_run", lambda command, **kwargs: commands.append(command) or 0)
    assert start._ensure_web_dist()
    assert len(commands) == 2
    assert start._ensure_web_dist()
    assert len(commands) == 2
    source.write_text("second", encoding="utf-8")
    assert start._ensure_web_dist()
    assert len(commands) == 4


def test_diagnostics_keep_http_503_body(monkeypatch):
    error = HTTPError("http://test/ready", 503, "unavailable", {}, io.BytesIO(b'{"status":"not_ready"}'))

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(start.urllib.request, "urlopen", unavailable)
    assert start._http_get_json("http://test/ready", 1) == (503, {"status": "not_ready"})


def test_readiness_rejects_wrong_model_tag(monkeypatch):
    import httpx
    from app.services.readiness_service import _check_ollama

    provider = SimpleNamespace(identity=lambda: SimpleNamespace(provider="ollama", model="model:4b"))
    container = SimpleNamespace(**{name: provider for name in ("llm", "rewrite_llm", "router_llm", "extraction_llm", "embedding")})
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"models": [{"name": "model:8b"}]}
    ))
    assert _check_ollama(container)["status"] == "fail"


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_invalid_ports_fail_before_socket_operations(monkeypatch, port):
    monkeypatch.setenv("APP_PORT", port)
    with pytest.raises(SystemExit):
        start._pick_port()


@pytest.mark.parametrize("prefix", ["", "codecode v"])
def test_startup_agent_runtime_uses_shared_project_resolver_without_path_lookup(tmp_path, monkeypatch, prefix):
    from app.agents import runtime
    from app.agents.settings import CODECODE_VERSION
    executable = tmp_path / "project-runtime.exe"
    calls = []
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setattr(runtime, "ensure_project_runtime", lambda: str(executable))
    monkeypatch.setattr(start.shutil, "which", lambda *args: pytest.fail("Agent startup must not look up a global CLI"))
    monkeypatch.setattr(start.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout=prefix + CODECODE_VERSION))
    assert start._ensure_agent_runtime()
    assert calls == [[str(executable), "--version"]]


def test_startup_external_executable_never_falls_back_to_global_or_npm(monkeypatch):
    from app.agents import runtime
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    def reject():
        raise ValueError("external executable")
    monkeypatch.setattr(runtime, "ensure_project_runtime", reject)
    monkeypatch.setattr(start, "_run", lambda *args, **kwargs: pytest.fail("No fallback installer or global command"))
    assert not start._ensure_agent_runtime()


def test_remote_agent_startup_does_not_prepare_local_runtime(monkeypatch):
    from app.agents import runtime
    monkeypatch.setenv("AGENT_BACKEND", "codecode")
    monkeypatch.setenv("CODECODE_AUTO_START", "true")
    monkeypatch.setenv("CODECODE_BASE_URL", "https://synthetic.example")
    monkeypatch.setenv("CODECODE_PASSWORD", "synthetic-password")
    monkeypatch.setattr(runtime, "ensure_project_runtime", lambda: pytest.fail("Remote codecode has no local runtime dependency"))
    assert start._ensure_agent_runtime()
