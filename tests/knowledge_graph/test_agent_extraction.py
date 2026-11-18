from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from app.agents.contracts import AgentCancelledError, AgentOutputError, AgentResult, AgentUnavailableError
from rag.knowledge_graph.agent_extractor import AgentGraphExtractor


TEXT = "订单服务调用支付服务创建业务订单，并记录全部业务处理结果。"


def extracted(chunk_id="c1", *, evidence=TEXT):
    return {
        "chunk_id": chunk_id,
        "entities": [
            {"local_id": "a", "name": "订单服务", "type": "MODULE", "aliases": []},
            {"local_id": "b", "name": "支付服务", "type": "MODULE", "aliases": []},
        ],
        "relations": [{"subject": "a", "predicate": "CALLS", "object": "b", "evidence": evidence, "confidence": 0.9}],
        "review": {"context_resolved": True, "coverage_checked": True, "conflicts_checked": True, "issues": []},
    }


class Backend:
    def __init__(self, outputs=None):
        self.outputs = outputs
        self.requests = []
        self.version = "test-v1"

    def identity(self):
        return {"backend": "test-agent", "model": "model-a", "version": self.version}

    def run(self, request, *, cancelled=None, on_event=None):
        if cancelled and cancelled():
            raise AgentCancelledError("cancelled")
        self.requests.append(request)
        if on_event:
            on_event({"type": "agent_round", "round": len(self.requests)})
        if isinstance(self.outputs, Exception):
            raise self.outputs
        if callable(self.outputs):
            output = self.outputs(request)
        elif self.outputs is not None:
            output = self.outputs[min(len(self.requests) - 1, len(self.outputs) - 1)]
        else:
            output = {"chunks": [extracted(chunk["chunk_id"]) for chunk in request.context["chunks"]]}
        return AgentResult(output=deepcopy(output), backend="test-agent", model="model-a")


def test_agent_extracts_then_verifies_and_forwards_controlled_execution():
    backend = Backend()
    extractor = AgentGraphExtractor("kb", backend=backend)
    events = []
    extractor.configure_execution({"run_id": "run-1", "tool_token": "secret-tool-token", "deadline_at": "2026-11-05T12:30:00+00:00"}, on_event=events.append)
    result = extractor.extract_from_chunk("c1", TEXT)

    assert result.relations[0].predicate == "CALLS"
    assert [request.context["mode"] for request in backend.requests] == ["extract", "verify_and_repair"]
    assert backend.requests[1].context["previous_output"]["chunks"]
    assert backend.requests[1].context["feedback"]
    assert all(request.context["deadline_at"] == "2026-11-05T12:30:00+00:00" for request in backend.requests)
    assert all(request.task == "extraction" and request.run_id == "run-1" and request.tool_token == "secret-tool-token" for request in backend.requests)
    assert "secret-tool-token" not in repr(backend.requests[0])
    assert "只读" in backend.requests[0].instructions
    assert events and backend.requests[0].output_schema["$defs"]["ExtractedRelation"]["properties"]["predicate"]["enum"]


def test_invalid_evidence_and_missing_batch_chunks_are_repaired_with_feedback():
    first = {"chunks": [extracted("c1", evidence="编造的证据")]}
    final = {"chunks": [extracted("c1"), extracted("c2")]}
    backend = Backend([first, final])
    extractor = AgentGraphExtractor("kb", backend=backend)
    result = extractor.extract_batch([{"chunk_id": "c1", "content": TEXT}, {"chunk_id": "c2", "content": TEXT}])
    assert [item.chunk_id for item in result] == ["c1", "c2"]
    assert "chunk_id" in backend.requests[1].context["feedback"][0]


@pytest.mark.parametrize("corrupt", [
    lambda item: item["entities"][0].update(name="原文不存在的实体"),
    lambda item: item["entities"][0].update(type="UNRECOGNIZED"),
    lambda item: item["entities"][1].update(local_id="a"),
    lambda item: item["relations"][0].update(object="not-an-entity"),
    lambda item: item["relations"][0].update(evidence="不是原文"),
    lambda item: item["relations"].append(deepcopy(item["relations"][0])),
    lambda item: item["review"].update(issues=["存在未解决的冲突"]),
    lambda item: item.update(chunk_id="outside-scope"),
])
def test_agent_rejects_unverifiable_facts_after_bounded_rounds(corrupt):
    item = extracted()
    corrupt(item)
    backend = Backend([{"chunks": [item]}])
    with pytest.raises(AgentOutputError):
        AgentGraphExtractor("kb", backend=backend, max_retries=99).extract_from_chunk("c1", TEXT)
    assert len(backend.requests) == 4


def test_context_disambiguation_requires_current_mentions_and_original_support():
    content = "该系统调用支付服务完成业务处理。"
    item = extracted(evidence=content)
    item["entities"][0]["aliases"] = ["该系统"]
    backend = Backend([{"chunks": [item]}])
    result = AgentGraphExtractor("kb", backend=backend).extract_from_chunk("c1", content, {"before": "订单服务负责接收业务请求。"})
    assert result.entities[0].name == "订单服务"
    with pytest.raises(AgentOutputError):
        AgentGraphExtractor("kb", backend=backend).extract_from_chunk("c1", content)


def test_agent_unavailability_never_uses_a_legacy_llm(monkeypatch):
    def forbidden():
        raise AssertionError("legacy LLM fallback was called")
    monkeypatch.setattr("rag.providers.factory.get_graph_extraction_llm_provider", forbidden)
    backend = Backend(AgentUnavailableError("codecode offline"))
    with pytest.raises(AgentUnavailableError, match="offline"):
        AgentGraphExtractor("kb", backend=backend).extract_from_chunk("c1", TEXT)
    assert len(backend.requests) == 1


def test_each_agent_round_obeys_shared_budget_and_refreshes_tool_token():
    backend = Backend()
    checks, tokens = [], []
    def before_request():
        checks.append(True)
    def token_factory():
        token = f"fresh-{len(tokens)}"
        tokens.append(token)
        return token
    extractor = AgentGraphExtractor("kb", backend=backend)
    extractor.configure_execution({"before_request": before_request, "token_factory": token_factory})
    extractor.extract_from_chunk("c1", TEXT)
    assert len(checks) == 2
    assert [request.tool_token for request in backend.requests] == tokens == ["fresh-0", "fresh-1"]

    def exhausted():
        raise AgentOutputError("shared quota exhausted")
    extractor.configure_execution({"before_request": exhausted, "token_factory": token_factory})
    with pytest.raises(AgentOutputError, match="quota exhausted"):
        extractor.extract_from_chunk("c1", TEXT)
    assert len(backend.requests) == 2


def test_backend_usage_events_are_forwarded_once_and_keep_unknown_cost():
    from dataclasses import replace

    class ReportingBackend(Backend):
        def run(self, request, *, cancelled=None, on_event=None):
            result = replace(super().run(request, cancelled=cancelled, on_event=on_event),
                             session_id=f"session-{len(self.requests)}", usage={"input_tokens": 3, "cost": None})
            on_event({"event": "usage", "session_id": result.session_id, "usage": result.usage})
            return result
    backend = ReportingBackend()
    events = []
    extractor = AgentGraphExtractor("kb", backend=backend)
    extractor.configure_execution(on_event=events.append)
    extractor.extract_from_chunk("c1", TEXT)
    usage = [event for event in events if event.get("event") == "usage"]
    assert len(usage) == 2
    assert [event["session_id"] for event in usage] == ["session-1", "session-2"]
    assert all(event["usage"]["cost"] is None for event in usage)


def seed(db, name):
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.knowledge_chunk import KnowledgeChunk

    if db.get(KnowledgeBase, KB) is None:
        db.add(KnowledgeBase.default())
    document = KnowledgeDocument(id=f"doc-{name}", knowledge_base_id=KB, source_path=f"{name}.txt", original_filename=f"{name}.txt", file_hash=name, file_size=50, status="ready")
    chunk = KnowledgeChunk(id=f"chunk-{name}", knowledge_base_id=KB, document_id=document.id, file_hash=name, chunk_index=0, content=TEXT, content_hash=name, graph_status="pending")
    db.add_all([document, chunk])
    db.commit()
    return document, chunk


def test_default_graph_build_uses_agents_and_preserves_document_scope(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    backend = Backend()
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile=None: backend)
    monkeypatch.setattr("app.services.knowledge_graph_service.GRAPH_CACHE_ENABLED", False)
    with SessionLocal() as db:
        first, a = seed(db, "first")
        second, b = seed(db, "second")
        service = GraphBuildService(db, store=SqliteGraphStore(db, KB))
        result = service.build_graph(document_ids=[first.id])
        assert isinstance(service.extractor, AgentGraphExtractor)
        assert result["succeeded"] == 1 and result["failed"] == 0
        assert a.graph_status == "completed" and b.graph_status == "pending"
        assert {chunk["chunk_id"] for request in backend.requests for chunk in request.context["chunks"]} == {a.id}
        assert service.build_graph(document_ids=[])["total_chunks"] == 0


def test_cancelled_graph_build_restores_processing_without_persisting(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    cancelled = Event()
    def output(request):
        cancelled.set()
        return {"chunks": [extracted(chunk["chunk_id"]) for chunk in request.context["chunks"]]}
    backend = Backend(output)
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile=None: backend)
    monkeypatch.setattr("app.services.knowledge_graph_service.GRAPH_CACHE_ENABLED", False)
    with SessionLocal() as db:
        document, chunk = seed(db, "cancelled")
        store = SqliteGraphStore(db, KB)
        with pytest.raises(AgentCancelledError):
            GraphBuildService(db, store=store).build_graph(document_ids=[document.id], cancelled=cancelled.is_set)
        assert chunk.graph_status == "pending"
        assert store.stats()["entities"] == 0


def test_agent_cache_changes_with_backend_prompt_constraints_and_neighbor_context(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.services.knowledge_graph_service import GraphBuildService
    from rag.knowledge_graph.sqlite_store import SqliteGraphStore

    backend = Backend()
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile=None: backend)
    monkeypatch.setattr("app.services.knowledge_graph_service.GRAPH_CACHE_ENABLED", True)
    monkeypatch.setattr("app.bootstrap.container.get_provider_container", lambda: SimpleNamespace(metadata=SimpleNamespace(session=SessionLocal), task_queue=object()))
    with SessionLocal() as db:
        seed(db, "cache")
        service = GraphBuildService(db, store=SqliteGraphStore(db, KB))
        batch = [{"chunk_id": "c1", "content": TEXT, "metadata": {"before": "邻接原文"}}]
        service._extract_batch_cached(batch, None)
        service._extract_batch_cached(batch, None)
        assert len(backend.requests) == 2
        backend.version = "test-v2"
        service._extract_batch_cached(batch, None)
        assert len(backend.requests) == 4
        service.extractor.prompt_prefix = "新增领域边界"
        service._extract_batch_cached(batch, None)
        assert len(backend.requests) == 6
        service.extractor.relation_constraints = {"CALLS": ({"MODULE"}, {"MODULE"})}
        service._extract_batch_cached(batch, None)
        assert len(backend.requests) == 8
        batch[0]["metadata"]["before"] = "修改后的邻接原文"
        service._extract_batch_cached(batch, None)
        assert len(backend.requests) == 10
