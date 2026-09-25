import json

from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.network import COOKIE, read_network


def local_client(app):
    return TestClient(app, client=('127.0.0.1', 50000))


def control_headers(client):
    return {'x-agentvisor-token': client.get('/api/session').json()['token']}


def test_remote_requests_require_code_even_with_forwarded_loopback(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, client=('192.0.2.50', 50000)) as client:
        for path in ('/api/session', '/api/health', '/api/tasks', '/api/network', '/api/system'):
            response = client.get(path, headers={'X-Forwarded-For': '127.0.0.1', 'Accept-Language': 'en'})
            assert response.status_code == 401
            assert response.json()['detail'] == 'Enter the AgentVisor access code'
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert app.state.network['access_code'] not in client.get('/').text


def test_remote_login_keeps_csrf_and_origin_checks(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, client=('192.0.2.50', 50000)) as client:
        code = app.state.network['access_code']
        assert client.post('/api/auth/login', json={'code': 'Неверный код'}).status_code == 401
        assert client.post('/api/auth/login', json={'code': code},
                           headers={'Origin': 'http://foreign.example'}).status_code == 403
        response = client.post('/api/auth/login', json={'code': code})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'SameSite=strict' in response.headers['set-cookie']
        assert client.cookies.get(COOKIE) == code
        assert client.get('/api/network').json()['authentication_required'] is True
        assert client.put('/api/network', json={'host': '0.0.0.0'}).status_code == 403
        headers = control_headers(client)
        assert client.put('/api/network', json={'host': '0.0.0.0'},
                          headers=dict(headers, Origin='http://foreign.example')).status_code == 403
        assert client.put('/api/network', json={'host': '0.0.0.0'}, headers=headers).status_code == 200
        assert client.post('/api/auth/logout', headers=headers).status_code == 200
        assert client.get('/api/tasks').status_code == 401
        assert client.get('/api/tasks', headers={'Authorization': 'Bearer ' + code}).status_code == 200


def test_network_settings_and_code_survive_restart(tmp_path):
    with local_client(create_app(tmp_path)) as client:
        initial = client.get('/api/network').json()
        assert initial['host'] == initial['active_host'] == '127.0.0.1'
        assert initial['authentication_required'] is False
        assert len(initial['access_code']) >= 24
        response = client.put('/api/network', json={'host': '0.0.0.0'}, headers=control_headers(client))
        assert response.status_code == 200
        assert response.json()['restart_required'] is True
        assert response.json()['active_host'] == '127.0.0.1'
        assert client.put('/api/network', json={'host': 'evil.example'},
                          headers=control_headers(client)).status_code == 422
        assert client.get('/api/network', headers={'Host': 'evil.example'}).status_code == 400
    with local_client(create_app(tmp_path)) as client:
        restored = client.get('/api/network').json()
        assert restored['host'] == '0.0.0.0'
        assert restored['access_code'] == initial['access_code']
    assert read_network(tmp_path)['host'] == '0.0.0.0'
    assert json.loads((tmp_path / 'network.json').read_text())['access_code'] == initial['access_code']


def test_restart_is_blocked_while_task_or_model_operation_is_active(tmp_path):
    app = create_app(tmp_path)
    calls = []
    app.state.request_restart = lambda: calls.append('restart')
    with local_client(app) as client:
        headers = control_headers(client)
        with app.state.engine.lock:
            assert client.post('/api/restart', headers=headers).status_code == 409
        payload = {'name': 'Restart test', 'goal': 'Run a demo', 'workspace': '.', 'mode': 'demo'}
        task = client.post('/api/tasks', json=payload, headers=headers).json()
        assert client.post(f"/api/tasks/{task['id']}/start", headers=headers).status_code == 200
        assert client.post('/api/restart', headers=headers).status_code == 409
        assert calls == [] and app.state.restarting is False
        app.state.engine.shutdown()
        assert client.post('/api/restart', headers=headers).status_code == 200
        assert calls == ['restart'] and app.state.restarting is True
        assert client.post(f"/api/tasks/{task['id']}/start", headers=headers).status_code == 503


def test_unmanaged_server_and_external_binding_are_explained(tmp_path):
    app = create_app(tmp_path)
    app.state.bind_host = '0.0.0.0'
    app.state.bind_override = True
    with local_client(app) as client:
        headers = control_headers(client)
        network = client.get('/api/network').json()
        assert network['host'] == '0.0.0.0' and network['bind_override']
        assert not network['managed']
        assert client.put('/api/network', json={'host': '127.0.0.1'}, headers=headers).status_code == 409
        assert client.post('/api/restart', headers=headers).status_code == 409
