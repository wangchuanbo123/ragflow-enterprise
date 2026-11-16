from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sys

import pytest

from tests.test_retrieval_consistency import infrastructure
from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
from app.models.index_job import IndexJob


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_agent_worker_does_not_hold_mutation_lock_and_reconciles(infrastructure, monkeypatch, terminal):
    from app.core.database import SessionLocal
    from app.services.index_worker import _process_job
    from app.services.knowledge_mutation import knowledge_mutation_lock

    reconciled = []
    class Workflow:
        def __init__(self, db, knowledge_base_id):
            pass

        def execute_for_job(self, job_id):
            def independent_tool_phase():
                with knowledge_mutation_lock(KB):
                    return True
            with ThreadPoolExecutor(1) as executor:
                assert executor.submit(independent_tool_phase).result(timeout=2)
            return {"status": terminal, "run_id": "test-run"}

    monkeypatch.setitem(sys.modules, "app.services.agent_workflow_service", SimpleNamespace(
        AgentWorkflowService=Workflow,
        reconcile_workflow_followups=lambda db, knowledge_base_id: reconciled.append(knowledge_base_id),
    ))
    with SessionLocal() as db:
        job = IndexJob(knowledge_base_id=KB, job_type="agent_workflow", status="pending")
        db.add(job)
        db.commit()
        identifier = job.id
    assert _process_job(identifier)
    with SessionLocal() as db:
        assert db.get(IndexJob, identifier).status == terminal
    assert reconciled == [KB]


def test_schema_worker_uses_agent_default_instead_of_legacy_llm(infrastructure, monkeypatch):
    from app.core.database import SessionLocal
    from app.services.index_worker import _process_job

    captured = []
    attached_results = []
    class AttachedWorkflow:
        def __init__(self, db, knowledge_base_id):
            pass

        def attach_existing_job(self, job_id, *, task, document_ids):
            assert task == "schema_governance"
            return SimpleNamespace(id="attached", document_ids_json=document_ids), {"run_id": "attached", "tool_token": "scoped-token"}

        def attached_cancelled(self, run_id):
            return False

        def append_attached_event(self, run_id, event):
            pass

        def finish_attached(self, run_id, **kwargs):
            attached_results.append(kwargs)

    class Schema:
        def __init__(self, db, knowledge_base_id, **kwargs):
            captured.append(kwargs)

        def generate_draft(self, **kwargs):
            assert captured[0]["cancelled"]() is False
            return SimpleNamespace(id="agent-schema", version=2, source_document_ids_json=kwargs["document_ids"])

    monkeypatch.setattr("app.services.graph_schema_induction_service.GraphSchemaInductionService", Schema)
    monkeypatch.setattr("app.services.agent_workflow_service.AgentWorkflowService", AttachedWorkflow)
    with SessionLocal() as db:
        job = IndexJob(knowledge_base_id=KB, job_type="schema_generation", status="pending", options_json={"document_ids": ["selected"]})
        db.add(job)
        db.commit()
        identifier = job.id
    assert _process_job(identifier)
    assert "llm" not in captured[0]
    assert captured[0]["agent_context"]["tool_token"] == "scoped-token"
    assert attached_results[0]["status"] == "succeeded"
    with SessionLocal() as db:
        job = db.get(IndexJob, identifier)
        assert job.status == "completed"
        assert job.options_json["generated_schema_id"] == "agent-schema"


def test_followup_links_preserve_existing_run_and_record_shared_child(infrastructure):
    from app.core.database import SessionLocal
    from app.services.index_worker import _link_followup_jobs

    with SessionLocal() as db:
        parent = IndexJob(knowledge_base_id=KB, job_type="file", status="completed", options_json={"agent_run_id": "run-1"})
        child = IndexJob(knowledge_base_id=KB, job_type="graph_rebuild", status="pending")
        shared = IndexJob(knowledge_base_id=KB, job_type="code_index", status="pending", options_json={"agent_run_id": "older-run"})
        db.add_all([parent, child, shared])
        db.commit()
        _link_followup_jobs(db, parent, [child, None, shared])
        assert set(parent.options_json["followup_job_ids"]) == {child.id, shared.id}
        assert child.options_json["agent_run_id"] == "run-1"
        assert child.options_json["parent_job_id"] == parent.id
        assert shared.options_json["agent_run_id"] == "older-run"
