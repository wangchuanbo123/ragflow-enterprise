"""Exercise the UI's contracts against the actual ASGI routes, without models.

Run from web/: python tests/contracts/verify_api_contracts.py
All account changes use an in-memory database from the backend test fixtures.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest
from tests import conftest as fixtures


def verify():
    from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
    from app.api.v1 import health
    from app.services import model_probe_service
    from app.services.login_throttle import reset_login_limits

    reset_login_limits()
    with pytest.MonkeyPatch.context() as patch:
        engine = fixtures.db_engine.__wrapped__(patch)
        clients = fixtures.client.__wrapped__(engine, patch)
        client = next(clients)
        try:
            login = client.post('/api/v1/auth/login', json={
                'username': INITIAL_ADMIN_USERNAME, 'password': INITIAL_ADMIN_PASSWORD,
            })
            assert login.status_code == 200, login.text
            assert {'id', 'username', 'display_name', 'role', 'is_active'} <= login.json()['user'].keys()

            kb = client.get('/api/v1/knowledge-bases').json()[0]
            assert kb['current_user_role'] == 'admin'
            assert set(kb['capabilities']) == {'can_read', 'can_edit', 'can_manage_members', 'can_archive'}
            assert kb['capabilities']['can_edit'] is True
            kb_id = kb['id']

            payload = {'username': 'contract_user', 'password': 'ContractPassword123',
                       'display_name': '契约用户', 'role': 'user', 'is_active': True}
            created = client.post('/api/v1/users', json=payload)
            assert created.status_code == 201, created.text
            account = created.json()
            assert 'password' not in account and account['username'] == payload['username']
            updated = client.patch(f"/api/v1/users/{account['id']}", json={'display_name': '更新名称'})
            assert updated.status_code == 200 and updated.json()['display_name'] == '更新名称'
            assert any(item['id'] == account['id'] for item in client.get('/api/v1/users').json())
            member = client.put(f'/api/v1/knowledge-bases/{kb_id}/members', json={
                'username': payload['username'], 'role': 'viewer',
            })
            assert member.status_code == 200, member.text

            failed_probe = {'status': 'failed', 'checked_at': '2026-11-05T02:00:00Z',
                            'profiles': {'answer': {'status': 'fail', 'elapsed_ms': 5,
                                                    'error_type': 'HTTPStatusError', 'http_status': 402}}}
            patch.setattr(model_probe_service, 'probe_models', lambda: failed_probe)
            response = client.post('/api/v1/model-probe')
            assert response.status_code == 200 and response.json() == failed_probe
            ready = {'status': 'not_ready', 'checked_at': failed_probe['checked_at'],
                     'model_inference_verified': False,
                     'components': {'model_probe': {'status': 'fail', **{k: failed_probe[k] for k in ('profiles', 'checked_at')}}}}
            patch.setattr(health, 'check_readiness', lambda: ready)
            response = client.get('/api/v1/ready')
            assert response.status_code == 503 and response.json() == ready

            # Same request bodies used by the browser, now with real validation,
            # cookies and session revocation instead of the Playwright API mock.
            client.post('/api/v1/auth/logout')
            assert client.post('/api/v1/auth/login', json={
                'username': payload['username'], 'password': payload['password'],
            }).status_code == 200
            viewer = client.get(f'/api/v1/knowledge-bases/{kb_id}').json()
            assert viewer['current_user_role'] == 'viewer'
            assert viewer['capabilities'] == {'can_read': True, 'can_edit': False,
                                              'can_manage_members': False, 'can_archive': False}
            assert client.get('/api/v1/users').status_code == 403
            assert client.post('/api/v1/model-probe').status_code == 403
            changed = client.post('/api/v1/auth/password', json={
                'current_password': payload['password'], 'new_password': 'ChangedContract456',
            })
            assert changed.status_code == 204 and changed.content == b'', changed.text
            assert client.get('/api/v1/auth/me').status_code == 401
            assert client.post('/api/v1/auth/login', json={
                'username': payload['username'], 'password': 'ChangedContract456',
            }).status_code == 200
            print('ASGI contracts passed: accounts, password 204/revocation, capabilities, ready 503, admin-only model probe')
        finally:
            client.close()
            clients.close()
            engine.dispose()
            reset_login_limits()


if __name__ == '__main__':
    verify()
