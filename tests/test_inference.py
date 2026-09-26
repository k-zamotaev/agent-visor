import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.inference import InferenceGateway, StreamMetrics
from agentvisor.tasks import NewTask
from test_supervisor import make


def test_generation_rate_uses_usage_and_excludes_waiting():
    metrics = StreamMetrics()
    assert not metrics.accept({'choices': [{'delta': {'role': 'assistant'}}]}, 1)
    metrics.accept({'choices': [{'delta': {'reasoning_content': 'one large fragment'}}]}, 100)
    metrics.accept({'choices': [{'delta': {'tool_calls': [{'function': {'arguments': '{}'}}]}}]}, 102)
    metrics.accept({'choices': [], 'usage': {'completion_tokens': 101}}, 150)
    assert metrics.result() == {'tokens_per_second': 50, 'output_tokens': 101,
                                'generation_seconds': 2, 'source': 'stream'}
    assert metrics.reasoning == 'one large fragment'


def test_missing_usage_never_turns_fragments_into_tokens():
    metrics = StreamMetrics()
    for tick in range(5):
        metrics.accept({'choices': [{'delta': {'content': 'words'}}]}, tick)
    assert metrics.result()['tokens_per_second'] is None
    metrics.accept({'usage': {'completion_tokens': 101}, 'stats': {'tokens_per_second': 75}}, 10)
    assert metrics.result()['tokens_per_second'] == 75
    assert metrics.result()['source'] == 'runtime'


@pytest.fixture
def model_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['content-length'])))
            requests.append({'body': body, 'path': self.path, 'authorization': self.headers.get('Authorization')})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            payloads = [
                {'choices': [{'delta': {'reasoning_content': 'Check the file first. '}}]},
                {'choices': [{'delta': {'content': 'Done'}}]},
                {'usage': {'completion_tokens': 12}, 'stats': {'tokens_per_second': 42}},
            ]
            for payload in payloads:
                self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
                self.wfile.flush()
                time.sleep(0.06)
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_gateway_preserves_stream_records_thoughts_and_delivers_new_context(tmp_path, model_server):
    url, requests = model_server
    store, engine, task = make(tmp_path)
    profile = dict(task['profile'], base_url=url)
    body = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Original goal'}], 'stream': True,
            'tools': [{'type': 'function', 'function': {'name': 'read',
                       'parameters': {'type': 'object', 'properties': {}}}}]}
    with InferenceGateway(store, task, profile, engine.cancel) as gateway, httpx.Client(trust_env=False) as client:
        endpoint = gateway.base_url + '/chat/completions'
        response = client.post(endpoint, json=body, headers={'Authorization': 'Bearer test-only'})
        assert response.status_code == 200 and 'data: [DONE]' in response.text
        task = store.add_context(task['id'], 'Use port 8015')
        response = client.post(endpoint, json=body)
        assert response.status_code == 200
        assert requests[0]['body']['messages'][1:] == body['messages']
        assert requests[0]['body']['messages'][0]['role'] == 'system'
        assert requests[0]['authorization'] == 'Bearer test-only'
        assert requests[0]['body']['stream_options']['include_usage'] is True
        assert 'Use port 8015' in requests[1]['body']['messages'][-1]['content']
        assert all(request['path'] == '/v1/chat/completions' for request in requests)
        assert client.post(gateway.base_url + '/other', json=body).status_code == 404
        assert gateway.reasoning_seen
        task = store.get(task['id'])
        assert task['generation_sample']['tokens_per_second'] == 42
        assert task['applied_context_version'] == task['context_version'] == 1
        assert task['goal_version'] == 1
        assert not task['generation_activity']['active']
        assert any(e['kind'] == 'reasoning' and e['message'] == 'Check the file first. '
                   for e in store.events(task['id']))
    assert not gateway.thread.is_alive()


def test_context_updates_are_atomic_and_delivery_does_not_go_backwards(tmp_path):
    store, _, task = make(tmp_path)
    threads = [threading.Thread(target=store.add_context, args=(task['id'], f'Note {i}')) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    result = store.get(task['id'])
    assert result['context_version'] == 6 and len(result['context_additions']) == 6
    store.mark_context_delivered(task['id'], 5)
    store.mark_context_delivered(task['id'], 2)
    assert store.get(task['id'])['applied_context_version'] == 5


def test_gateway_enforces_session_ceiling_only_for_work_requests(tmp_path, model_server):
    url, requests = model_server
    store, engine, task = make(tmp_path)
    task = dict(task, active_effort={'output_limit': 2048})
    with InferenceGateway(store, task, dict(task['profile'], base_url=url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            body = {'model': 'same-model', 'messages': [], 'stream': True, 'tools': [{'type': 'function'}]}
            for fields in ({'max_tokens': 9000}, {'max_completion_tokens': 9000}, {'max_tokens': 1000}, {}):
                assert client.post(gateway.base_url + '/chat/completions', json=dict(body, **fields)).status_code == 200
            client.post(gateway.base_url + '/chat/completions', json={'model': 'same-model', 'messages': [], 'max_tokens': 4000})
    assert requests[0]['body']['max_tokens'] == 2048
    assert requests[1]['body']['max_completion_tokens'] == 2048
    assert requests[2]['body']['max_tokens'] == 1000
    assert requests[3]['body']['max_tokens'] == 2048
    assert requests[4]['body']['max_tokens'] == 4000
    assert all(request['body']['model'] == 'same-model' for request in requests)


def test_context_api_keeps_running_task_and_goal_unchanged(tmp_path):
    app = create_app(tmp_path / 'api')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        task = client.post('/api/tasks', json=NewTask(name='Context', workspace=str(tmp_path),
                           goal='Original goal').model_dump()).json()
        app.state.store.update(task['id'], status='running', pid=None)
        response = client.post(f'/api/tasks/{task["id"]}/context', json={'text': 'Keep all data'})
        assert response.status_code == 200
        result = response.json()
        assert result['status'] == 'running' and result['goal'] == 'Original goal'
        assert result['goal_version'] == 1 and result['context_version'] == 1
        assert client.post(f'/api/tasks/{task["id"]}/context', json={'text': '  '}).status_code == 422
        assert client.post(f'/api/tasks/{task["id"]}/context', json={'text': 'a' * 6001}).status_code == 422
        app.state.store.update(task['id'], status='succeeded')
        assert client.post(f'/api/tasks/{task["id"]}/context', json={'text': 'New'}).status_code == 400


def test_pending_context_prevents_premature_completion(tmp_path):
    from test_supervisor import finish
    store, engine, task = make(tmp_path, max_iterations=1)
    store.add_context(task['id'], 'Verify the new requirement too')
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'blocked'


def test_stream_activity_keeps_silent_cli_alive_but_not_forever(tmp_path):
    import sys
    from pathlib import Path
    from agentvisor.execution import execute
    from agentvisor.tasks import prepare_documents, state_dir
    store, engine, task = make(tmp_path, idle_timeout_seconds=0.3, timeout_seconds=5)
    prepare_documents(task, {'instance': 'fake', 'context': 16384})
    started = time.monotonic()

    class ActiveStream:
        def activity(self):
            return min(time.monotonic(), started + 0.8)

    result = execute(store, task, [sys.executable, str(Path(__file__).with_name('fake_agent.py')),
                     str(state_dir(task)), 'step_hang'], engine.cancel, inference=ActiveStream())
    assert result['reason'] == 'idle_timeout'
    assert 1.0 <= result['duration'] < 3


@pytest.mark.parametrize('headers_first', [True, False])
def test_cancel_closes_gateway_with_silent_upstream(tmp_path, headers_first):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            if headers_first:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
            entered.set()
            release.wait(5)

    model = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    model.daemon_threads = True
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    store, engine, task = make(tmp_path, timeout_seconds=3600)
    gateway = InferenceGateway(store, task, dict(task['profile'], base_url=f'http://127.0.0.1:{model.server_port}'), engine.cancel)
    errors = []

    def request():
        try:
            httpx.post(gateway.base_url + '/chat/completions', json={'stream': True}, timeout=3, trust_env=False)
        except httpx.HTTPError as error:
            errors.append(type(error).__name__)

    try:
        gateway.__enter__()
        reader = threading.Thread(target=request, daemon=True)
        reader.start()
        assert entered.wait(3)
        started = time.monotonic()
        engine.cancel.set()
        gateway.__exit__()
        assert time.monotonic() - started < 1.5
        assert not gateway.thread.is_alive()
        reader.join(timeout=1)
        assert not reader.is_alive() and not gateway.requests
        assert not store.get(task['id']).get('generation_sample')
    finally:
        release.set()
        model.shutdown()
        model.server_close()
        reader.join(timeout=3)
