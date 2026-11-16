from copy import deepcopy

import pytest

from app.agents.contracts import AgentOutputError, AgentResult, AgentUnavailableError
from rag.code_graph.agent_summary import AgentCodeSummarizer


class Backend:
    def __init__(self, *, unavailable=False, invalid=False):
        self.requests = []
        self.version = "v1"
        self.unavailable = unavailable
        self.invalid = invalid

    def identity(self):
        return {"backend": "test-agent", "model": "test-code-model", "version": self.version}

    def run(self, request, *, cancelled=None, on_event=None):
        self.requests.append(request)
        if self.unavailable:
            raise AgentUnavailableError("synthetic offline")
        assert request.task == "knowledge_organization" and request.context["mode"] == "code_summary"
        summaries = []
        for symbol in request.context["symbols"]:
            field = "source_excerpt" if symbol.get("source_excerpt") else "path"
            summaries.append({"stable_key": "outside-scope" if self.invalid else symbol["stable_key"],
                              "summary": f"{symbol['qualified_name']} 执行给定源码定义的逻辑。",
                              "evidence": [{"field": field, "quote": symbol[field]}]})
        return AgentResult(output={"summaries": summaries,
            "review": {"coverage_checked": True, "conflicts_checked": True, "issues": []}},
            backend="test-agent", model="test-code-model", session_id=f"code-{len(self.requests)}", usage={"input_tokens": 3, "cost": None})


def test_code_summary_is_verified_scoped_and_budgeted():
    backend = Backend()
    calls, events = [], []
    def reserve():
        calls.append(True)
    service = AgentCodeSummarizer(backend, "kb", agent_context={"run_id": "parent", "before_request": reserve,
        "token_factory": lambda: f"token-{len(calls)}", "deadline_at": "2026-11-05T12:30:00+00:00"}, on_event=events.append)
    items = [{"stable_key": "k1", "qualified_name": "calculate", "path": "a.py", "source_excerpt": "def calculate():\n    return 1", "signature": "calculate()", "relations": []}]
    result = service.summarize(items, "snapshot-1")
    assert result["k1"]["evidence"][0]["quote"] == items[0]["source_excerpt"]
    assert len(calls) == len(backend.requests) == 2
    assert [request.tool_token for request in backend.requests] == ["token-1", "token-2"]
    assert all(request.context["deadline_at"] == "2026-11-05T12:30:00+00:00" for request in backend.requests)
    assert len([event for event in events if event.get("event") == "usage"]) == 2
    backend.invalid = True
    with pytest.raises(AgentOutputError):
        service.summarize(items, "snapshot-1")
    assert len(backend.requests) == 5


def indexed_service(db, tmp_path, backend):
    from app.adapters.local import LocalObjectStore
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.services.code_index_service import CodeIndexService
    from app.services.code_vector_service import CodeVectorService
    from tests.test_relation_and_code_vector_retrieval import _container, _FakeTaskQueue

    source = tmp_path / "source"
    source.mkdir()
    (source / "calculate.py").write_text("def calculate(value):\n    return value + 1\n", encoding="utf-8")
    CodeIndexService(db, KB, LocalObjectStore(tmp_path / "objects"), _FakeTaskQueue()).index_directory(source)
    container = _container()
    # This is deliberately present: production must not use a container's old
    # extraction_llm unless it was explicitly passed as summary_llm.
    container.extraction_llm = type("ForbiddenLLM", (), {"generate": lambda self, prompt: (_ for _ in ()).throw(AssertionError("legacy fallback"))})()
    return CodeVectorService(db, KB, container, agent_backend=backend)


def test_code_summary_agent_cache_and_model_change_rebuild_vectors(db_engine, tmp_path):
    from app.core.database import SessionLocal

    backend = Backend()
    with SessionLocal() as db:
        service = indexed_service(db, tmp_path, backend)
        first = service.rebuild()
        assert first["summary_method"] == "agent" and first["summary_agent_failures"] == 0
        assert first["summaries_generated"] == first["symbol_count"]
        assert len(backend.requests) == 2
        service.rebuild()
        assert len(backend.requests) == 2
        backend.version = "v2"
        changed = service.rebuild()
        assert len(backend.requests) == 4
        assert changed["index_name"] != first["index_name"]
        assert all(symbol["metadata"]["summary_method"] == "agent" for symbol in service.repository.list_symbols())


def test_unavailable_agent_keeps_code_retrieval_but_marks_deterministic_diagnostic(db_engine, tmp_path):
    from app.core.database import SessionLocal

    backend = Backend(unavailable=True)
    with SessionLocal() as db:
        service = indexed_service(db, tmp_path, backend)
        result = service.rebuild()
        assert result["status"] == "ready"
        assert result["summaries_generated"] == 0
        assert result["summary_agent_failures"] == result["symbol_count"]
        assert result["summary_method"] == "deterministic"
        assert "AGENT_UNAVAILABLE" in result["summary_diagnostics"]
        assert service.hybrid_search("calculate", 5)
        assert all(symbol["metadata"]["functional_summary_origin"] == "fallback" for symbol in service.repository.list_symbols())
        assert len(backend.requests) == 1


def test_code_summary_does_not_send_repository_symbols_outside_attached_scope(db_engine, tmp_path):
    from app.core.database import SessionLocal

    backend = Backend()
    with SessionLocal() as db:
        service = indexed_service(db, tmp_path, backend)
        service._agent_context = {"scope_document_ids": [], "allow_repository_symbols": False}
        result = service.rebuild()
        assert not backend.requests
        assert result["summary_scope_skipped"] == result["symbol_count"]
        assert result["summary_agent_failures"] == 0
        assert "outside_agent_scope" in result["summary_diagnostics"]
