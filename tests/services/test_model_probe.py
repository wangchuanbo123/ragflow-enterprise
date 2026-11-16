from types import SimpleNamespace

import httpx
import pytest

from app.services import model_probe_service as probes
from rag.providers.interfaces import ProviderIdentity


class FakeProvider:
    def __init__(self, kind, failure=False):
        self.kind = kind
        self.failure = failure
        self.base_url = "https://models.example"
        self.api_key = "synthetic-secret"
        self.calls = []

    def identity(self):
        return ProviderIdentity(kind=self.kind, provider="test", model="test-model")

    def get_model(self):
        return self

    def generate(self, prompt):
        self.calls.append(prompt)
        if self.failure:
            raise httpx.HTTPStatusError("secret-in-upstream-error", request=httpx.Request("POST", self.base_url), response=httpx.Response(503))
        return "OK"

    def embed_query(self, text):
        self.calls.append(text)
        return [0.1, 0.2]

    def rerank(self, query, documents):
        self.calls.append(query)
        return documents


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setattr(probes, "_cached", None)
    monkeypatch.setattr(probes, "_cached_key", None)
    monkeypatch.setattr(probes, "_expires_at", 0)
    return SimpleNamespace(llm=FakeProvider("llm"), embedding=FakeProvider("embedding"), reranker=FakeProvider("reranker"))


def test_probe_uses_small_inputs_and_caches_results(container):
    first = probes.probe_models(container)
    assert first["status"] == "verified"
    assert all(row["status"] == "ok" for row in first["profiles"].values())
    assert probes.probe_models(container) == first
    assert len(container.llm.calls) == 1
    assert len(container.embedding.calls) == 1
    first["profiles"].clear()
    assert probes.last_model_probe(container)["profiles"]


def test_endpoint_and_credential_rotation_invalidates_probe(container):
    probes.probe_models(container)
    container.llm.base_url = "https://different.example"
    assert probes.last_model_probe(container) is None
    probes.probe_models(container)
    container.llm.api_key = "rotated-synthetic-secret"
    assert probes.last_model_probe(container) is None


def test_probe_sanitizes_provider_error(container):
    container.llm.failure = True
    result = probes.probe_models(container)
    assert result["status"] == "failed"
    assert result["profiles"]["answer"]["http_status"] == 503
    assert "secret-in-upstream-error" not in str(result)
    assert "synthetic-secret" not in str(result)


def test_probe_requires_admin(client, monkeypatch):
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME

    assert client.post("/api/v1/model-probe").status_code == 401
    client.post("/api/v1/auth/login", json={"username": INITIAL_ADMIN_USERNAME, "password": INITIAL_ADMIN_PASSWORD})
    monkeypatch.setattr(probes, "probe_models", lambda: {"status": "verified", "profiles": {}, "checked_at": "test"})
    assert client.post("/api/v1/model-probe").json()["status"] == "verified"


def test_openai_probe_disables_sdk_retries_and_closes_owned_client(monkeypatch):
    from rag.providers import llm

    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": {"message": "upstream unavailable"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    original = llm.ChatOpenAI

    def model_factory(**kwargs):
        return original(**kwargs, http_client=client)

    monkeypatch.setattr(llm, "ChatOpenAI", model_factory)
    provider = llm.OpenAICompatibleLLMProvider(
        model="test-model", api_key="synthetic-key", base_url="https://models.example/v1"
    )
    result = probes._probe_one("answer", provider)
    assert result["status"] == "fail"
    assert result["http_status"] == 503
    assert len(attempts) == 1
    assert client.is_closed
    assert "_model" not in vars(provider)
