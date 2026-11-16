r"""Actual ASGI + database contracts for the knowledge-agent workbench.

Run from web/: ..\venv\Scripts\python.exe tests/contracts/verify_agent_contracts.py
Only the replaceable agent and retrieval boundary are faked. API authorization,
task options, persistence, output/evidence validation and tag writes are real.
"""

from pathlib import Path
from datetime import datetime
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest
from tests import conftest as fixtures


class ContractAgent:
    def run(self, request, *, cancelled=None, on_event=None):
        from app.agents.contracts import AgentResult
        assert request.context.get("deadline_at"), "All workflow requests must retain the parent deadline"
        assert datetime.fromisoformat(request.context["deadline_at"]).utcoffset().total_seconds() == 0
        evidence = [{"document_id": "contract-doc", "chunk_id": "contract-chunk", "quote": "关键器件由单一供应商提供。"}]
        if request.context.get("mode") == "answer_comparison":
            return AgentResult(output={"answer": "关键器件存在单一来源风险[contract-chunk]。", "evidence": evidence},
                               backend="contract", model="contract/model", usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": None,
                                   "reasoning_tokens": 2, "cache_read_tokens": 10, "cache_write_tokens": 0, "cost": None, "reported_cost": 0})
        actions = []
        items = [{"finding": "单一供应来源"}]
        if request.task == "evaluation" and request.context.get("options", {}).get("generate_cases"):
            items = [{"kind": "evaluation_case", "question": "关键器件由谁提供？", "reference_answer": "关键器件由单一供应商提供。",
                      "expected_document_ids": ["contract-doc"], "expected_chunk_ids": ["contract-chunk"], "evidence": evidence}]
        if request.task == "schema_governance":
            actions = [{"kind": "create_schema_draft", "document_ids": ["contract-doc"], "params": {
                "entity_types": ["SUPPLIER", "COMPONENT"], "relation_types": ["SUPPLIES"]}, "reason": "结构草案待审阅"}]
        elif request.task == "knowledge_organization":
            actions = [{"kind": "tag_documents", "document_ids": ["contract-doc"],
                        "params": {"tags": ["智能体契约标签"]}, "reason": "按原文标记供应风险"}]
        if on_event:
            on_event({"type": "tool", "message": "已核对文档与证据", "evidence_count": 1})
        return AgentResult(output={"summary": f"{request.task}真实应用契约完成", "items": items,
                                   "evidence": evidence, "actions": actions, "metrics": {"checked_documents": 1},
                                   "diagnostics": [{"status": "ok", "message": "契约后端运行正常"}]},
                           backend="contract", model="contract/model", session_id="contract-session",
                           usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": None,
                               "reasoning_tokens": 3, "cache_read_tokens": 40, "cache_write_tokens": 0, "cost": None, "reported_cost": 0})


def verify():
    from app.agents import factory
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.agent_run import AgentRun  # Register before creating the isolated schema.
    from app.services.agent_workflow_service import AgentWorkflowService
    from app.services import rag_service
    from app.services.login_throttle import reset_login_limits
    from app.core import database
    del AgentRun

    reset_login_limits()
    with pytest.MonkeyPatch.context() as patch:
        engine = fixtures.db_engine.__wrapped__(patch)
        clients = fixtures.client.__wrapped__(engine, patch)
        client = next(clients)
        patch.setattr(factory, "get_agent_backend", lambda **kwargs: ContractAgent())
        patch.setattr(factory, "list_backend_profiles", lambda: ["default", "contract"])
        patch.setattr(rag_service, "inspect_retrieval", lambda *args, **kwargs: {"sources": [{
            "chunk_id": "contract-chunk", "document_id": "contract-doc", "source": "供应协议.md",
            "preview": "关键器件由单一供应商提供。"}]})
        patch.setattr(rag_service, "ask", lambda *args, **kwargs: {"answer": "关键器件由单一供应商提供。"})
        try:
            assert client.post('/api/v1/auth/login', json={'username': INITIAL_ADMIN_USERNAME, 'password': INITIAL_ADMIN_PASSWORD}).status_code == 200
            kb = client.get('/api/v1/knowledge-bases').json()[0]['id']
            base = f'/api/v1/knowledge-bases/{kb}'
            with database.SessionLocal() as db:
                db.add(KnowledgeDocument(id='contract-doc', knowledge_base_id=kb, source_path='contract/供应协议.md',
                    original_filename='供应协议.md', file_hash='contract-hash', file_size=100, status='ready', chunk_count=1))
                db.add(KnowledgeChunk(id='contract-chunk', knowledge_base_id=kb, document_id='contract-doc',
                    file_hash='contract-hash', chunk_index=0, content='关键器件由单一供应商提供。', content_hash='contract-content', token_count=12))
                db.commit()
            catalog_response = client.get(base + '/agent-task-catalog')
            assert catalog_response.status_code == 200, catalog_response.text
            catalog = catalog_response.json()
            expected_tasks = {'extraction', 'ingestion', 'schema_governance', 'quality_review', 'incremental_maintenance',
                              'knowledge_organization', 'evaluation', 'business_analysis', 'operations'}
            assert {item['task'] for item in catalog} == expected_tasks
            assert all('default' in item['options']['runtime_profiles'] for item in catalog)

            runs = []
            for task in sorted(expected_tasks):
                allowed = ['create_schema_draft'] if task == 'schema_governance' else ['tag_documents'] if task == 'knowledge_organization' else []
                options = {'apply': False, 'allowed_actions': allowed, 'runtime_profile': 'contract',
                           'graph_kind': 'general', 'mode': 'pending', 'max_actions': 10}
                if task == 'evaluation':
                    options.update({'gold_cases': [{'question': '有哪些供应风险？', 'expected_document_ids': ['contract-doc']}],
                                    'comparison_profiles': ['contract'], 'prompt_variants': [{'name': '证据优先', 'instructions': '使用原文回答'}]})
                response = client.post(base + '/agent-runs', json={'task': task, 'instructions': '核对指定文档和原文证据',
                    'document_ids': ['contract-doc'], 'options': options})
                assert response.status_code == 202, (task, response.text)
                run = response.json()
                assert run['status'] == 'queued' and run['options']['apply'] is False
                with database.SessionLocal() as db:
                    AgentWorkflowService(db, kb).execute_for_job(run['job_id'])
                detail = client.get(base + '/agent-runs/' + run['id']).json()
                assert detail['status'] == 'succeeded', (task, detail.get('error_message'), detail.get('events'))
                assert detail['output']['summary'] and detail['output']['items'] and detail['output']['evidence']
                assert detail['events'] and detail['usage']['input_tokens'] == (25 if task == 'evaluation' else 20)
                assert detail['usage']['total_tokens'] is None and detail['usage']['total_tokens_coverage'] == 'unknown'
                assert detail['usage']['reasoning_tokens'] == (5 if task == 'evaluation' else 3)
                assert detail['usage']['cache_read_tokens'] == (50 if task == 'evaluation' else 40)
                assert detail['usage']['cost'] is None and detail['usage']['cost_coverage'] == 'unknown'
                assert len(detail['usage']['sessions']) == (2 if task == 'evaluation' else 1)
                assert detail['backend'] == 'contract' and detail['session_id'] == 'contract-session'
                assert detail['progress']['current'] == detail['progress']['total']
                assert 'followup_jobs' in detail
                artifact = client.get(base + '/agent-runs/' + run['id'] + '/artifact')
                assert artifact.status_code == 200 and artifact.json()['output'] == detail['output']
                assert 'attachment' in artifact.headers.get('content-disposition', '')
                assert detail['options']['max_runtime_seconds'] == 900 and detail['options']['max_agent_calls'] == 40
                runs.append(detail)

            # Generate candidates without inventing benchmark scores, then use
            # an explicitly reviewed reference while retaining machine origin.
            generated_response = client.post(base + '/agent-runs', json={'task': 'evaluation', 'instructions': '从原文构建待审阅样例',
                'document_ids': ['contract-doc'], 'options': {'generate_cases': True, 'max_generated_cases': 3,
                    'runtime_profile': 'contract', 'allowed_actions': []}})
            assert generated_response.status_code == 202, generated_response.text
            generated_run = generated_response.json()
            with database.SessionLocal() as db:
                AgentWorkflowService(db, kb).execute_for_job(generated_run['job_id'])
            generation = client.get(base + '/agent-runs/' + generated_run['id']).json()
            assert generation['status'] == 'succeeded', generation
            candidate = generation['output']['generated_cases'][0]
            assert candidate['provenance'] == 'agent_generated' and candidate['human_review_required'] is True
            assert candidate['review_status'] == 'candidate' and candidate['source_run_id'] == generated_run['id']
            generation_metrics = generation['output']['metrics']['evaluation']
            assert generation_metrics['mode'] == 'case_generation_only' and generation_metrics['case_count'] == 0
            assert generation_metrics['precision'] is None and generation_metrics['recall'] is None
            reused_response = client.post(base + '/agent-runs', json={'task': 'evaluation', 'document_ids': ['contract-doc'],
                'options': {'runtime_profile': 'contract', 'allowed_actions': [], 'gold_cases': [{
                    'question': candidate['question'], 'reference_answer': candidate['reference_answer'],
                    'expected_document_ids': candidate['expected_document_ids'], 'expected_chunk_ids': candidate['expected_chunk_ids'],
                    'provenance': 'agent_generated', 'source_run_id': generated_run['id'], 'candidate_id': candidate['id']}]}})
            assert reused_response.status_code == 202, reused_response.text
            reused = reused_response.json()
            with database.SessionLocal() as db:
                AgentWorkflowService(db, kb).execute_for_job(reused['job_id'])
            reused_detail = client.get(base + '/agent-runs/' + reused['id']).json()
            assert reused_detail['status'] == 'succeeded', reused_detail
            assert reused_detail['output']['metrics']['evaluation']['reference_set'] == 'contains_agent_generated_candidates'

            # Demonstrate an actual controlled write, rather than replacing the
            # workflow service or returning a canned successful HTTP response.
            response = client.post(base + '/agent-runs', json={'task': 'knowledge_organization',
                'instructions': '为已核验文档追加风险标签', 'document_ids': ['contract-doc'],
                'options': {'apply': True, 'allowed_actions': ['tag_documents'], 'runtime_profile': 'contract'}})
            assert response.status_code == 202, response.text
            applied = response.json()
            with database.SessionLocal() as db:
                AgentWorkflowService(db, kb).execute_for_job(applied['job_id'])
            detail = client.get(base + '/agent-runs/' + applied['id']).json()
            assert detail['status'] == 'succeeded' and detail['output']['actions'][0]['status'] == 'applied', detail
            assert any(tag['name'] == '智能体契约标签' for tag in client.get(base + '/tags').json())
            assert client.get(base + '/documents/tag-map').json()['contract-doc']

            response = client.post(base + '/agent-runs', json={'task': 'extraction', 'document_ids': ['foreign-document']})
            assert response.status_code == 400
            pending = client.post(base + '/agent-runs', json={'task': 'operations', 'document_ids': ['contract-doc'],
                'options': {'apply': False, 'allowed_actions': []}}).json()
            assert client.post(base + '/agent-runs/' + pending['id'] + '/cancel').json()['status'] == 'cancelled'
            retried = client.post(base + '/agent-runs/' + pending['id'] + '/retry')
            assert retried.status_code == 202 and retried.json()['parent_run_id'] == pending['id']
            assert retried.json()['attempt'] == 2

            created = client.post('/api/v1/users', json={'username': 'agent_viewer', 'password': 'AgentViewer123', 'role': 'user'}).json()
            assert created.get('id'), created
            assert client.put(base + '/members', json={'username': 'agent_viewer', 'role': 'viewer'}).status_code == 200
            client.post('/api/v1/auth/logout')
            assert client.post('/api/v1/auth/login', json={'username': 'agent_viewer', 'password': 'AgentViewer123'}).status_code == 200
            assert client.get(base + '/agent-runs').status_code == 200
            assert client.get(base + '/agent-runs/' + runs[0]['id']).status_code == 200
            assert client.post(base + '/agent-runs', json={'task': 'operations'}).status_code == 403
            assert client.post(base + '/agent-runs/' + retried.json()['id'] + '/cancel').status_code == 403
            assert client.post(base + '/agent-runs/' + pending['id'] + '/retry').status_code == 403
            print('Agent ASGI contracts passed: nine real workflows, structured results/evidence/usage/events, scoped write, cancel/retry, viewer restrictions')
        finally:
            client.close(); clients.close(); engine.dispose(); reset_login_limits()


if __name__ == '__main__':
    verify()
