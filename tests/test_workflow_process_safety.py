"""Workflow concurrency uses real PostgreSQL with independent coordinator locks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_distributed_coordination import postgres_service
from app.adapters.local import InlineTaskQueue
from app.models.agent_run import AgentRun, AgentRunEvent
from app.models.knowledge_base import KnowledgeBase, KnowledgeDataSource
from app.models.user import User
from app.schemas.agent_run import WorkflowOptions
from app.services.agent_workflow_service import AgentWorkflowService


def seed_agent(engine, *, maximum=40, children=0, status="running"):
    with Session(engine) as db:
        db.add(User(id="coord-user", username="coord-admin", password_hash="synthetic", role="admin", is_active=True))
        db.flush()
        db.add(KnowledgeBase(id="agent-kb", slug="agent-coord", name="Agent tests", owner_id="coord-user"))
        db.flush()
        parent = AgentRun(knowledge_base_id="agent-kb", requested_by="coord-user", task="knowledge_organization", status=status,
            options_json=WorkflowOptions(max_agent_calls=maximum).model_dump(), context_json={}, output_json={}, usage_json={},
            progress_json={"stage": "applying"}, started_at=datetime.utcnow())
        db.add(parent); db.flush()
        identifiers = []
        for _ in range(children):
            child = AgentRun(knowledge_base_id="agent-kb", requested_by="coord-user", task="extraction", status="running",
                options_json=WorkflowOptions(max_agent_calls=maximum).model_dump(), parent_run_id=parent.id,
                context_json={"attached_index_job": True}, output_json={}, usage_json={},
                progress_json={"stage": "applying"}, started_at=datetime.utcnow())
            db.add(child); db.flush(); identifiers.append(child.id)
        db.commit()
        return parent.id, identifiers


def service_for(db):
    return AgentWorkflowService(db, "agent-kb", task_queue=InlineTaskQueue())


def disable_process_local_lock(monkeypatch):
    # Separate server processes have separate RLocks. Remove their incidental
    # protection in this threaded test so only the real database can serialize.
    monkeypatch.setattr("app.services.agent_workflow_service._EVENT_LOCK", nullcontext())


def test_real_postgres_parent_budget_is_atomic_across_independent_workers(postgres_service, monkeypatch):
    from app.agents.contracts import AgentCancelledError
    engine, _, _ = postgres_service
    parent, children = seed_agent(engine, maximum=7, children=2)
    disable_process_local_lock(monkeypatch)
    def reserve(number):
        with Session(engine) as db:
            try:
                service_for(db).reserve_agent_call(children[number % 2])
                return True
            except AgentCancelledError:
                return False
    with ThreadPoolExecutor(12) as executor:
        outcomes = list(executor.map(reserve, range(32)))
    assert sum(outcomes) == 7
    with Session(engine) as db:
        context = db.get(AgentRun, parent).context_json
        assert context["agent_budget"]["calls"] == 7
        assert context["budget_exhausted"] == "agent_calls"


def test_real_postgres_usage_and_event_sequences_do_not_lose_parallel_updates(postgres_service, monkeypatch):
    from app.core import config
    engine, _, _ = postgres_service
    parent, children = seed_agent(engine, children=2)
    disable_process_local_lock(monkeypatch)
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    def append(number):
        event = {"type": "usage", "session_id": f"session-{number % 16}", "backend": "synthetic", "model": "test",
                 "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": None, "cost": None}}
        with Session(engine) as db:
            service = service_for(db)
            service.append_attached_event(children[number % 2], event)
            assert not service._pending_events
    with ThreadPoolExecutor(12) as executor:
        list(executor.map(append, range(32)))
    with Session(engine) as db:
        usage = db.get(AgentRun, parent).usage_json
        assert len(usage["sessions"]) == 16
        assert usage["input_tokens"] == 32 and usage["output_tokens"] == 48
        assert usage["cost"] is None and usage["total_tokens"] is None
        for identifier in children:
            sequences = list(db.scalars(select(AgentRunEvent.sequence).where(AgentRunEvent.run_id == identifier).order_by(AgentRunEvent.sequence)))
            assert sequences == list(range(1, 17))


def test_real_postgres_token_refresh_preserves_parallel_budget_json(postgres_service, monkeypatch):
    engine, _, _ = postgres_service
    parent, _ = seed_agent(engine, maximum=100)
    disable_process_local_lock(monkeypatch)
    def update(number):
        with Session(engine) as db:
            service = service_for(db)
            if number % 2:
                assert service._fresh_tool_token(parent)
            else:
                service.reserve_agent_call(parent)
    with ThreadPoolExecutor(12) as executor:
        list(executor.map(update, range(32)))
    with Session(engine) as db:
        context = db.get(AgentRun, parent).context_json
        assert context["agent_budget"]["calls"] == 16 and context["deadline_at"]


def test_real_postgres_retry_requests_create_one_child(postgres_service, monkeypatch):
    engine, _, _ = postgres_service
    parent, _ = seed_agent(engine, status="failed")
    monkeypatch.setattr("app.agents.factory.list_backend_profiles", lambda: ["default"])
    def retry(_):
        with Session(engine) as db:
            return service_for(db).retry(parent, "coord-user").id
    with ThreadPoolExecutor(8) as executor:
        children = list(executor.map(retry, range(8)))
    assert len(set(children)) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.parent_run_id == parent)) == 1
        assert db.get(AgentRun, children[0]).attempt == 2


def test_real_server_local_directory_sync_is_queued_and_deduplicated(postgres_service, monkeypatch):
    from app.core import config
    from app.services.data_source_service import DataSourceService
    from app.models.index_job import IndexJob
    engine, _, _ = postgres_service
    seed_agent(engine)
    with Session(engine) as db:
        source = KnowledgeDataSource(knowledge_base_id="agent-kb", name="synthetic-source", source_type="local_directory",
                                    config_json={"location": "/not-scanned-by-api"}, status="active")
        db.add(source); db.commit(); identifier = source.id
    monkeypatch.setattr(config, "APP_PROFILE", "server")
    monkeypatch.setattr(DataSourceService, "execute_sync", lambda *args, **kwargs: pytest.fail("Server API must never scan directories"))
    def enqueue(_):
        with Session(engine) as db:
            return DataSourceService(db, "agent-kb", task_queue=InlineTaskQueue()).request_sync(identifier, "coord-user")["job_id"]
    with ThreadPoolExecutor(6) as executor:
        jobs = list(executor.map(enqueue, range(6)))
    assert len(set(jobs)) == 1
    with Session(engine) as db:
        job = db.get(IndexJob, jobs[0])
        assert job.job_type == "connector_sync" and job.status == "pending"


def test_duplicate_model_actions_write_once(db_engine, monkeypatch):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.schemas.agent_run import AgentRunCreate
    from app.models.graph_governance import GraphReviewItem
    from tests.services.test_agent_workflows import seed, FakeAgent, setup_agent
    action = {"kind": "propose_correction", "params": {"action": "hide_relation", "target_id": "relation-ab", "reason": "待核验", "payload": {}}}
    class DuplicateAgent(FakeAgent):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result.output["actions"] = [action, action]
            return result
    setup_agent(monkeypatch, DuplicateAgent())
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="quality_review", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "completed"
        assert [row["status"] for row in run.output_json["actions"]] == ["applied", "already_applied"]
        assert db.scalar(select(func.count()).select_from(GraphReviewItem)) == 1


def test_committed_action_without_acknowledgement_is_not_blindly_retried(db_engine, monkeypatch):
    from app.api.errors import APIError
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.schemas.agent_run import AgentRunCreate
    from app.services.knowledge_workflows.execution import WorkflowExecutor
    from app.models.graph_governance import GraphReviewItem
    from tests.services.test_agent_workflows import seed, FakeAgent, setup_agent
    action = {"kind": "propose_correction", "params": {"action": "hide_relation", "target_id": "relation-ab", "reason": "待核验", "payload": {}}}
    setup_agent(monkeypatch, FakeAgent(action=action))
    original = WorkflowExecutor.execute_action
    def interrupted(self, value):
        original(self, value)
        raise RuntimeError("synthetic crash after domain commit")
    monkeypatch.setattr(WorkflowExecutor, "execute_action", interrupted)
    with SessionLocal() as db:
        seed(db)
        service = AgentWorkflowService(db, KB)
        run = service.create(AgentRunCreate(task="quality_review", options={"apply": True}), "workflow-user")
        assert service.execute_for_job(run.job_id)["status"] == "failed"
        assert run.output_json["actions"][0]["status"] == "applying"
        assert db.scalar(select(func.count()).select_from(GraphReviewItem)) == 1
        with pytest.raises(APIError) as caught:
            service.retry(run.id, "workflow-user")
        assert "待核验" in caught.value.message
