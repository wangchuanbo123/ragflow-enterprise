"""Actual ASGI/ORM contracts for the unified graph UI; only compilation is faked."""

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest
from tests import conftest as fixtures


class Compiler:
    def __init__(self):
        self.calls = []

    def run(self, request, **kwargs):
        from app.agents.contracts import AgentResult
        assert request.task == "knowledge_organization" and request.context["mode"] == "graph_filter"
        self.calls.append(request)
        items = []
        for index, principle in enumerate(request.context["principles"]):
            if "不支持" in principle:
                items.append({"index": index, "status": "unsupported", "summary": "无法按此原则过滤",
                    "alternatives": [], "reason": "只能筛选已存在的知识事实"})
            else:
                condition = {"target": "node", "field": "name", "operator": "contains_any", "values": ["不存在的实体"]} if "空结果" in principle else {
                    "target": "node", "field": "entity_type", "operator": "in", "values": ["system"]}
                items.append({"index": index, "status": "applied", "summary": "只保留系统实体",
                    "alternatives": [{"conditions": [condition]}]})
        return AgentResult(output={"version": 1, "summary": "已逐条理解原则", "principles": items}, backend="controlled", model="contract")


def verify():
    from app.agents import factory
    from app.core import database
    from app.core.config import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_graph import KgEntity, KgEntityMention, KgRelation, KgRelationEvidence

    with pytest.MonkeyPatch.context() as patch:
        engine = fixtures.db_engine.__wrapped__(patch)
        clients = fixtures.client.__wrapped__(engine, patch)
        client = next(clients)
        compiler = Compiler()
        patch.setattr(factory, "get_agent_backend", lambda **kwargs: compiler)
        try:
            assert client.post('/api/v1/auth/login', json={'username': INITIAL_ADMIN_USERNAME, 'password': INITIAL_ADMIN_PASSWORD}).status_code == 200
            kb = client.get('/api/v1/knowledge-bases').json()[0]['id']
            base = f'/api/v1/knowledge-bases/{kb}/knowledge-graph'
            with database.SessionLocal() as db:
                db.add(KnowledgeDocument(id='filter-doc', knowledge_base_id=kb, source_path='filter.txt', original_filename='filter.txt',
                    file_hash='filter-hash', file_size=100, status='ready', chunk_count=1))
                db.add(KnowledgeChunk(id='filter-chunk', knowledge_base_id=kb, document_id='filter-doc', file_hash='filter-hash',
                    chunk_index=0, content='订单服务调用支付服务。', content_hash='filter-content'))
                db.add_all([KgEntity(id=identifier, knowledge_base_id=kb, entity_type='system', canonical_name=name, normalized_name=name)
                    for identifier, name in [('filter-a', '订单服务'), ('filter-b', '支付服务')]])
                db.add_all([KgEntityMention(knowledge_base_id=kb, entity_id=identifier, document_id='filter-doc', chunk_id='filter-chunk', mention=name, confidence=1)
                    for identifier, name in [('filter-a', '订单服务'), ('filter-b', '支付服务')]])
                db.add(KgRelation(id='filter-relation', knowledge_base_id=kb, subject_entity_id='filter-a', object_entity_id='filter-b', predicate='CALLS', confidence=1))
                db.add(KgRelationEvidence(knowledge_base_id=kb, relation_id='filter-relation', document_id='filter-doc', chunk_id='filter-chunk',
                    quote='订单服务调用支付服务', quote_hash='filter-quote', confidence=1))
                db.commit()
            whole = client.post(base + '/filter-preview', json={'principles': [], 'max_nodes': 200, 'max_edges': 400})
            assert whole.status_code == 200, whole.text
            assert len(whole.json()['nodes']) == 2 and len(whole.json()['edges']) == 1 and not compiler.calls
            bounded = client.post(base + '/filter-preview', json={'principles': [], 'max_nodes': 1, 'max_edges': 0}).json()
            assert bounded['stats']['truncated'] is True and bounded['stats']['returned_nodes'] == 1
            filtered = client.post(base + '/filter-preview', json={'principles': ['只看系统'], 'max_nodes': 200, 'max_edges': 400})
            assert filtered.status_code == 200, filtered.text
            result = filtered.json()
            assert result['plan']['principles'][0]['status'] == 'applied' and result['summary']
            assert result['edges'][0]['evidence'][0]['chunk_id'] == 'filter-chunk'
            assert result['schema_fingerprint'] and result['graph_fingerprint']
            empty = client.post(base + '/filter-preview', json={'principles': ['空结果']})
            assert empty.status_code == 200 and not empty.json()['nodes'] and empty.json()['stats']['matched_nodes'] == 0
            failed = client.post(base + '/filter-preview', json={'principles': ['不支持的原则']})
            assert failed.status_code == 422, failed.text
            assert failed.json()['error']['code'] == 'GRAPH_FILTER_UNSUPPORTED'
            assert failed.json()['error']['details']['unapplied_principles'][0]['reason']
            assert 'nodes' not in failed.json()
            owner_view = client.post(base + '/filters', json={'name': '管理员私有', 'principles': ['只看系统']})
            assert owner_view.status_code in {200, 201}, owner_view.text
            owner_id = owner_view.json()['id']
            assert any(item['id'] == owner_id for item in client.get(base + '/filters').json())
            user = client.post('/api/v1/users', json={'username': 'graph_viewer', 'password': 'GraphViewerPassword123', 'role': 'user'})
            assert user.status_code == 201, user.text
            assert client.put(f'/api/v1/knowledge-bases/{kb}/members', json={'username': 'graph_viewer', 'role': 'viewer'}).status_code == 200
            client.post('/api/v1/auth/logout')
            assert client.post('/api/v1/auth/login', json={'username': 'graph_viewer', 'password': 'GraphViewerPassword123'}).status_code == 200
            assert client.get(base + '/filters').json() == []
            assert client.delete(base + '/filters/' + owner_id).status_code == 404
            assert client.post(base + '/filters/' + owner_id + '/preview', json={'max_nodes': 200, 'max_edges': 400}).status_code == 404
            mine = client.post(base + '/filters', json={'name': '我的系统视图', 'principles': ['只看系统']})
            assert mine.status_code in {200, 201}, mine.text
            identifier = mine.json()['id']
            assert client.get(base + '/filters').json()[0]['name'] == '我的系统视图'
            before = len(compiler.calls)
            replay = client.post(base + '/filters/' + identifier + '/preview', json={'max_nodes': 200, 'max_edges': 400})
            assert replay.status_code == 200 and len(compiler.calls) == before + 1
            assert client.delete(base + '/filters/' + identifier).status_code == 204
            assert client.get(base + '/filters').json() == []
            print('Graph-filter ASGI contracts passed: whole/scope/truncation, compiled principles, empty matches, 422 details, viewer personal persistence/isolation, recompile and delete 204')
        finally:
            client.close()
            clients.close()
            engine.dispose()


if __name__ == '__main__':
    verify()
