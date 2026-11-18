"""Public branding stays distinct from the private native protocol bridge."""

import io
import json

import httpx
import pytest

from app.agents import runtime
from app.agents.codecode import CodeCodeAgentBackend
from app.agents.settings import AgentSettings, codecode_env, default_settings


def test_public_configuration_overrides_legacy_including_explicit_empty(monkeypatch):
    monkeypatch.setenv("OPENCODE_MODEL", "rag/legacy")
    monkeypatch.setenv("CODECODE_MODEL", "rag/current")
    assert codecode_env("MODEL") == "rag/current"
    monkeypatch.setenv("CODECODE_MODEL", "")
    assert codecode_env("MODEL") == ""
    monkeypatch.delenv("CODECODE_MODEL")
    assert codecode_env("MODEL") == "rag/legacy"


def test_legacy_configuration_normalizes_public_identity(monkeypatch):
    for name in ("BASE_URL", "SERVER_USERNAME", "PASSWORD", "SERVER_PASSWORD", "MODEL"):
        monkeypatch.delenv("CODECODE_" + name, raising=False)
    monkeypatch.setenv("AGENT_BACKEND", "opencode")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://synthetic.example")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setenv("OPENCODE_PASSWORD", "synthetic-password")
    monkeypatch.setenv("OPENCODE_MODEL", "rag/legacy")
    settings = default_settings()
    assert settings.backend == settings.username == "codecode"
    assert settings.password == "synthetic-password"
    identity = CodeCodeAgentBackend(settings).identity()
    assert identity["backend"] == identity["provider"] == "codecode"
    assert "opencode" not in json.dumps(identity).lower()


def test_public_config_uses_native_authentication_only_at_wire_boundary():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"version": "2.2.5"})

    backend = CodeCodeAgentBackend(
        AgentSettings(password="scoped-password"), transport=httpx.MockTransport(handler)
    )
    assert backend.health()["status"] == "ok"
    assert requests[0].headers["authorization"] == "Basic b3BlbmNvZGU6c2NvcGVkLXBhc3N3b3Jk"
    with pytest.raises(Exception) as caught:
        backend._model_ref("invalid")
    assert "CODECODE_MODEL" in str(caught.value)
    assert "opencode" not in str(caught.value).lower()


def test_runtime_log_rebrands_names_split_across_pipe_reads(tmp_path):
    class Fragmented(io.BytesIO):
        def read1(self, size=-1):
            return self.read(3)

    original = "OpenCode started\nOPENCODE_MODEL unset\nopencode final 中文\n"
    path = tmp_path / "server.log"
    stream = Fragmented(original.encode())
    assert runtime._drain_runtime_log(stream, path)
    assert stream.closed
    assert path.read_text(encoding="utf-8") == "codecode started\nCODECODE_MODEL unset\ncodecode final 中文\n"


def test_existing_runtime_log_is_normalized_before_append(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("OpenCode v2.2.5\nOPENCODE_PASSWORD configured\n", encoding="utf-8")
    runtime._normalize_runtime_log(path)
    runtime._drain_runtime_log(io.BytesIO(b"opencode ready\n"), path)
    assert path.read_text(encoding="utf-8") == "codecode v2.2.5\nCODECODE_PASSWORD configured\ncodecode ready\n"
    assert list(tmp_path.iterdir()) == [path]


def test_legacy_imports_and_version_banner_remain_internal_compatible():
    from app.agents.opencode import OpenCodeAgentBackend

    assert OpenCodeAgentBackend is CodeCodeAgentBackend
    assert runtime.start_managed_opencode is runtime.start_managed_codecode
    assert runtime.stop_managed_opencode is runtime.stop_managed_codecode
    assert runtime.runtime_version("codecode v2.2.5") == "2.2.5"
    assert runtime.runtime_version("opencode v2.2.5") == "2.2.5"
