from copy import deepcopy

import pytest

from app.agents.contracts import AgentOutputError, AgentUnavailableError
from app.services.graph_schema_induction_service import GraphSchemaInductionService
from tests.knowledge_graph.test_agent_extraction import Backend
from tests.test_graph_schema_induction_and_coverage import _seed_document


def schema_output():
    return {
        "entity_types": [{"name": "MODULE", "description": "业务服务模块", "aliases": ["服务"], "exclusions": ["无明确指代"], "graph_kind": "general"}],
        "relation_types": [{"name": "CALLS", "description": "显式调用", "source_types": ["MODULE"], "target_types": ["MODULE"], "direction": "调用方到被调用方", "graph_kind": "general"}],
        "extraction_prompt": "只抽取明确的服务调用和逐字原文证据。",
        "rationale": "文档明确包含订单服务调用支付服务。",
        "supporting_evidence": [{"chunk_id": "schema-one-0", "quote": "订单服务调用支付服务创建支付单。", "type_names": ["MODULE", "CALLS"]}],
        "review": {"coverage_checked": True, "conflicts_checked": True, "issues": []},
    }


def test_schema_induction_uses_agent_review_rounds_and_preserves_draft_only(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB

    backend = Backend([schema_output()])
    reservations, tokens = [], []
    def reserve():
        reservations.append(True)
    def token_factory():
        tokens.append(f"controlled-token-{len(tokens)}")
        return tokens[-1]
    with SessionLocal() as db:
        document, _ = _seed_document(db)
        service = GraphSchemaInductionService(db, KB, backend=backend, agent_context={"run_id": "schema-run", "before_request": reserve, "token_factory": token_factory, "deadline_at": "2026-11-05T12:30:00+00:00"})
        draft = service.generate_draft(created_by=None, document_ids=[document.id])
        assert draft.status == "draft"
        assert draft.generation_method == "document_agent"
        assert draft.generation_summary_json["agent_backend"]["version"] == "test-v1"
    assert len(backend.requests) == 2
    assert all(request.task == "schema_governance" and request.context["mode"] == "induction" for request in backend.requests)
    assert backend.requests[1].context["phase"] == "verify_and_repair"
    assert backend.requests[1].context["previous_output"]
    assert backend.requests[0].run_id == "schema-run"
    assert all(request.context["deadline_at"] == "2026-11-05T12:30:00+00:00" for request in backend.requests)
    assert len(reservations) == 2
    assert [request.tool_token for request in backend.requests] == tokens == ["controlled-token-0", "controlled-token-1"]


def test_schema_agent_repairs_unknown_endpoints_and_invalid_evidence(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB

    invalid = schema_output()
    invalid["relation_types"][0]["target_types"] = ["UNKNOWN"]
    backend = Backend([invalid, schema_output()])
    with SessionLocal() as db:
        document, _ = _seed_document(db)
        result = GraphSchemaInductionService(db, KB, backend=backend)._induce(document_ids=[document.id])
        assert result["relation_types"] == ["CALLS"]
    assert "端点" in backend.requests[1].context["feedback"][0]

    invalid = schema_output()
    invalid["supporting_evidence"][0]["quote"] = "样本没有这样的文字"
    backend = Backend([invalid])
    with SessionLocal() as db:
        with pytest.raises(AgentOutputError):
            GraphSchemaInductionService(db, KB, backend=backend, max_retries=0)._induce(document_ids=[document.id])
    assert len(backend.requests) == 2


def test_schema_agent_unavailable_fails_without_raw_llm_fallback(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB

    backend = Backend(AgentUnavailableError("offline"))
    monkeypatch.setattr("app.agents.factory.get_agent_backend", lambda profile=None: backend)
    with SessionLocal() as db:
        document, _ = _seed_document(db)
        with pytest.raises(AgentUnavailableError):
            GraphSchemaInductionService(db, KB)._induce(document_ids=[document.id])
        with pytest.raises(ValueError, match="没有可用于"):
            GraphSchemaInductionService(db, KB)._induce(document_ids=[])

