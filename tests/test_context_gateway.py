"""Exercise admission, handoff and lazy tools through the actual HTTP gateway."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from agentvisor.context_budget import ContextBudget
from agentvisor.inference import InferenceGateway
from agentvisor.progress_plan import initialize
from agentvisor.tasks import write_document
from test_inference import model_server
from test_supervisor import make


def tool(name, description=''):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': {}}}}


def request(history='', **overrides):
    messages = [{'role': 'user', 'content': 'Implement CSV export and verify the result.'}]
    if history:
        messages += [{'role': 'assistant', 'tool_calls': [
            {'id': 'read-1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-1', 'content': history}]
    return {'model': 'fake', 'messages': messages, 'tools': [tool('read')],
            'stream': True, 'max_tokens': 512, **overrides}


def profile_for(task, url, **overrides):
    return dict(task['profile'], base_url=url, context=65536, **overrides)


def test_large_history_never_reaches_upstream_and_handoff_is_sticky(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            response = client.post(gateway.base_url + '/chat/completions', json=request('read output\n' * 50000))
            assert response.status_code == 409
            assert response.json()['error']['type'] == 'supervisor_session_boundary'
            assert gateway.session_stop['reason'] == 'context_handoff'
            assert gateway.session_stop['blocked'] is False
            assert gateway.session_stop['evidence']['message_count'] == 4
            # A client retry must not sneak past the requested process boundary.
            assert client.post(gateway.base_url + '/chat/completions', json=request()).status_code == 409
    assert upstream == []
    assert len([event for event in store.events(task['id']) if event['kind'] == 'session_handoff']) == 1


def test_verified_provider_count_reduces_reserve_for_same_profile_only(tmp_path, model_server):
    url, _ = model_server
    store, engine, task = make(tmp_path)
    profile = dict(task['profile'], base_url=url, context=32768)
    identity = [profile.get(key) for key in ('runtime', 'base_url', 'model', 'context')]
    task = store.update(task['id'], context_calibration={
        'identity': identity, 'observed_prompt_tokens': 19286,
        'raw_estimated_tokens': 20192, 'calibration': 1.0})
    with InferenceGateway(store, task, profile, engine.cancel) as gateway:
        assert gateway.context_budget.safety_fraction == .10
        metrics = gateway.context_budget.assess(request()).metrics
        assert metrics['safety_reserve'] == 3277
        assert metrics['input_limit'] == 28979
    other_profile = dict(profile, context=65536)
    with InferenceGateway(store, task, other_profile, engine.cancel) as gateway:
        assert gateway.context_budget.safety_fraction == .20


def test_oversized_initial_instructions_are_nonretryable_static_failure(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    body = request(messages=[{'role': 'system', 'content': 'Required system instructions\n' * 15000},
                             {'role': 'user', 'content': 'Follow the goal.'}])
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 409
        assert gateway.session_stop['reason'] == 'context_blocked'
        assert gateway.session_stop['blocked'] is True
        assert gateway.session_stop['evidence']['cause'] == 'static_context_overflow'
    assert upstream == []


def test_oversized_auxiliary_summary_rotates_instead_of_blocking_the_task(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    body = {'model': 'fake', 'messages': [{'role': 'user', 'content': 'Summarize history\n' * 30000}]}
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 409
        assert gateway.session_stop['reason'] == 'context_handoff'
        assert gateway.session_stop['blocked'] is False
        assert gateway.session_stop['evidence']['auxiliary_request'] is True
    assert upstream == []


def test_supervisor_and_user_context_are_added_before_admission(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    # All four additions fit the real 20k-character store limit.
    for _ in range(4):
        task = store.add_context(task['id'], 'Сохранить данные пользователя. ' * 130)
    profile = dict(task['profile'], base_url=url, context=16384)
    body = request()
    with InferenceGateway(store, task, profile, engine.cancel) as gateway:
        assert gateway.context_budget.assess(body).action == 'allow'
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 409
        assert gateway.session_stop['blocked']
        assert gateway.session_stop['evidence']['static_tokens'] > gateway.session_stop['evidence']['input_limit']
    assert upstream == []
    assert store.get(task['id']).get('applied_context_version', 0) == 0


def test_mcp_selection_changes_next_request_without_losing_directives(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    directive = 'Добавить дизайн интерфейса и сохранить существующие пользовательские данные.'
    task = store.add_context(task['id'], directive)
    body = request(tools=[tool('read'), tool('agentvisor_process_select_toolset'),
                          tool('browseros-neo_snapshot'), tool('blender_get_objects_summary')])
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            endpoint = gateway.base_url + '/chat/completions'
            assert client.post(endpoint, json=body).status_code == 200
            for group in ('browseros-neo', 'blender'):
                selected = client.post(gateway.mcp_url, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'select_toolset', 'arguments': {'name': group}}}).json()['result']
                assert selected['isError'] is False
                assert json.loads(selected['content'][0]['text'])['active_toolset'] == group
                assert client.post(endpoint, json=body).status_code == 200
    names = [{tool['function']['name'] for tool in item['body']['tools']} for item in upstream]
    assert names[0] == {'read', 'agentvisor_process_select_toolset'}
    assert names[1] == names[0] | {'browseros-neo_snapshot'}
    assert names[2] == names[0] | {'blender_get_objects_summary'}
    for item in upstream:
        assert directive in next(message['content'] for message in item['body']['messages'] if message['role'] == 'user')
        assert 'Implement CSV export and verify the result.' in json.dumps(item['body'], ensure_ascii=False)


def test_current_step_external_tool_is_preselected_on_new_gateway(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    task = store.update(task['id'], task_memory={'current_step': {'id': 'step-1'},
        'tool_results': [{'tool': 'browseros-neo_snapshot', 'step_id': 'step-1', 'status': 'completed'}]})
    body = request(tools=[tool('read'), tool('agentvisor_process_select_toolset'),
                          tool('browseros-neo_snapshot'), tool('blender_get_objects_summary')])
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 200
    visible = {item['function']['name'] for item in upstream[0]['body']['tools']}
    assert 'browseros-neo_snapshot' in visible
    assert 'blender_get_objects_summary' not in visible


def test_browser_toolset_stays_usable_in_32k_context(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    tools = [tool('read'), tool('agentvisor_process_select_toolset')]
    tools += [tool('browseros-neo_' + str(index), 'browser schema ' * 240)
              for index in range(20)]
    tools += [tool(name, 'browser schema ' * 240) for name in (
        'browseros-neo_tabs', 'browseros-neo_snapshot',
        'browseros-neo_navigate', 'browseros-neo_act')]
    body = request(messages=[{'role': 'system', 'content': 'Preserve these instructions. ' * 1700},
                             {'role': 'user', 'content': 'Inspect the page.'}], tools=tools)
    assert ContextBudget(32768).assess(body).action == 'blocked'
    with InferenceGateway(store, task, dict(task['profile'], base_url=url, context=32768), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            endpoint = gateway.base_url + '/chat/completions'
            assert client.post(endpoint, json=body).status_code == 200
            result = client.post(gateway.mcp_url, json={'jsonrpc': '2.0', 'id': 1,
                'method': 'tools/call', 'params': {'name': 'select_toolset',
                                                   'arguments': {'name': 'browseros-neo'}}}).json()['result']
            assert result['isError'] is False
            assert client.post(endpoint, json=body).status_code == 200
        assert gateway.session_stop is None
    visible = {item['function']['name'] for item in upstream[-1]['body']['tools']}
    assert visible == {'read', 'agentvisor_process_select_toolset',
                       'browseros-neo_tabs', 'browseros-neo_snapshot',
                       'browseros-neo_navigate', 'browseros-neo_act'}


def test_pending_directives_override_selected_browser_toolset(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Implement export\n')
    task = initialize(store, task)
    task = store.add_context(task['id'], 'Add interface design and browser verification.')
    body = request(tools=[tool('agentvisor_process_get_progress'), tool('agentvisor_process_apply_user_instructions'),
                          tool('agentvisor_process_select_toolset'), tool('browseros-neo_snapshot')],
                   tool_choice={'type': 'function', 'function': {'name': 'browseros-neo_snapshot'}})
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 200
    forwarded = upstream[0]['body']
    assert {item['function']['name'] for item in forwarded['tools']} == {
        'agentvisor_process_get_progress', 'agentvisor_process_apply_user_instructions'}
    assert forwarded['tool_choice'] == 'required'


@pytest.fixture
def usage_server():
    state = {'fixed': None, 'multiplier': 2, 'requests': [], 'error': None,
             'error_stream': False, 'error_status': 400}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(body)
            if state['error']:
                data = json.dumps({'error': {'message': state['error']}}).encode()
                if state['error_stream']:
                    data = b'data: ' + data + b'\n\ndata: [DONE]\n\n'
                self.send_response(state['error_status'])
                self.send_header('Content-Type', 'text/event-stream' if state['error_stream'] else 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            estimated = ContextBudget(65536).assess(body).metrics['raw_estimated_tokens']
            usage = state['fixed'] if state['fixed'] is not None else estimated * state['multiplier']
            data = json.dumps({'choices': [{'message': {'content': 'Done'}, 'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': usage, 'completion_tokens': 1}}).encode()
            is_stream = body.get('stream', False)
            if is_stream:
                data = b'data: ' + data + b'\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if is_stream else 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize('stream', [False, True])
def test_usage_calibration_survives_gateway_handoff_and_low_usage_cannot_relax_it(tmp_path, usage_server, stream):
    url, upstream = usage_server
    store, engine, task = make(tmp_path)
    profile = profile_for(task, url)
    with httpx.Client(trust_env=False) as client:
        with InferenceGateway(store, task, profile, engine.cancel) as gateway:
            assert client.post(gateway.base_url + '/chat/completions', json=request(stream=stream)).status_code == 200
        task = store.get(task['id'])
        assert task['context_calibration']['calibration'] == 2
        upstream['fixed'] = 1  # Emulate usage reported after server-side truncation.
        with InferenceGateway(store, task, profile, engine.cancel) as gateway:
            assert gateway.context_budget.assess(request()).metrics['calibration'] == 2
            assert client.post(gateway.base_url + '/chat/completions', json=request(stream=stream)).status_code == 200
        assert store.get(task['id'])['context_calibration']['calibration'] == 2
        with InferenceGateway(store, store.get(task['id']), dict(profile, model='different-model'), engine.cancel) as gateway:
            assert gateway.context_budget.assess(request()).metrics['calibration'] == 1


def test_review_finalization_clears_boundary_and_accepts_only_a_fresh_small_dossier(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    task = dict(task, review_phase=True)
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            endpoint = gateway.base_url + '/chat/completions'
            assert client.post(endpoint, json=request('old reviewer history\n' * 20000)).status_code == 409
            assert gateway.session_stop is not None
            gateway.begin_finalization()
            assert gateway.session_stop is None and gateway.finalizing
            assert client.post(endpoint, json=request()).status_code == 200
            assert len(upstream) == 1
            oversized_dossier = {'messages': [{'role': 'user', 'content': 'evidence\n' * 50000}]}
            assert client.post(endpoint, json=oversized_dossier).status_code == 409
            assert gateway.session_stop['blocked'] is True
    assert len(upstream) == 1


@pytest.mark.parametrize('error,counted', [
    ('The request (69933 tokens) exceeds the context length of 65536 tokens.', 69933),
    ('context_length_exceeded: maximum context length is 65536 tokens.', None),
])
def test_provider_context_refusal_uses_sticky_handoff_instead_of_runtime_recovery(tmp_path, usage_server, error, counted):
    url, upstream = usage_server
    upstream['error'] = error
    store, engine, task = make(tmp_path)
    profile = profile_for(task, url)
    body = request()
    with InferenceGateway(store, task, profile, engine.cancel) as gateway:
        assert gateway.context_budget.assess(body).action == 'allow'
        with httpx.Client(trust_env=False) as client:
            endpoint = gateway.base_url + '/chat/completions'
            # The provider's status was already forwarded before the JSON error
            # arrived. Later requests receive the supervisor's boundary response.
            first = client.post(endpoint, json=body)
            assert first.status_code == 400
            assert first.json()['error']['message'] == error
            assert gateway.session_stop['reason'] == 'context_handoff'
            assert gateway.session_stop['blocked'] is False
            assert gateway.session_stop['evidence']['cause'] == 'provider_context_refusal'
            assert client.post(endpoint, json=body).status_code == 409
    assert len(upstream['requests']) == 1
    if counted:
        calibration = store.get(task['id'])['context_calibration']
        assert calibration['observed_prompt_tokens'] == counted
        assert calibration['calibration'] > 1
    else:
        assert not store.get(task['id']).get('context_calibration')
    handoffs = [event for event in store.events(task['id']) if event['kind'] == 'session_handoff']
    assert len(handoffs) == 1


@pytest.mark.parametrize('stream', [False, True])
def test_unrelated_provider_error_is_not_misclassified_as_context_handoff(tmp_path, usage_server, stream):
    url, upstream = usage_server
    upstream['error'] = 'The model is unavailable.'
    upstream['error_stream'] = stream
    store, engine, task = make(tmp_path)
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=request()).status_code == 400
        assert gateway.session_stop is None
        assert 'model is unavailable' in gateway.inference_error


@pytest.mark.parametrize('status', [200, 400])
@pytest.mark.parametrize('error,counted', [
    ('The request (69933 tokens) exceeds the context length of 65536 tokens.', 69933),
    ('context_length_exceeded: maximum context length is 65536 tokens.', None),
])
def test_streamed_context_refusal_becomes_handoff_even_after_http_200(tmp_path, usage_server, status, error, counted):
    url, upstream = usage_server
    upstream.update(error=error, error_stream=True, error_status=status)
    store, engine, task = make(tmp_path)
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            endpoint = gateway.base_url + '/chat/completions'
            assert gateway.context_budget.assess(request()).action == 'allow'
            first = client.post(endpoint, json=request())
            assert first.status_code == status  # Headers may already be sent.
            assert error in first.text
            assert first.text.endswith('\n\n')  # Complete SSE error event.
            assert '[DONE]' not in first.text  # An error cannot become success.
            assert gateway.session_stop['reason'] == 'context_handoff'
            assert gateway.session_stop['evidence']['cause'] == 'provider_context_refusal'
            assert client.post(endpoint, json=request()).status_code == 409
    assert len(upstream['requests']) == 1
    current = store.get(task['id'])
    assert not current.get('generation_activity', {}).get('active')
    assert not current.get('generation_sample')
    if counted:
        assert current['context_calibration']['observed_prompt_tokens'] == counted
        assert current['context_calibration']['calibration'] > 1
    else:
        assert not current.get('context_calibration')


def test_huge_unused_tool_schemas_do_not_false_block_initial_backend_work(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    body = request(tools=[tool('read'), tool('agentvisor_process_select_toolset'),
                          tool('browseros-neo_snapshot', 'Browser documentation ' * 15000),
                          tool('blender_get_objects_summary', 'Blender documentation ' * 15000)])
    with InferenceGateway(store, task, profile_for(task, url), engine.cancel) as gateway:
        assert gateway.context_budget.assess(body).action == 'blocked'
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 200
        assert gateway.session_stop is None
    assert len(upstream[0]['body']['tools']) == 2


def test_browser_screenshot_does_not_false_block_review(tmp_path, model_server):
    url, upstream = model_server
    store, engine, task = make(tmp_path)
    body = request(messages=[{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Verify the export interface visually.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'a' * 250000}}]}])
    with InferenceGateway(store, dict(task, review_phase=True), profile_for(task, url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 200
        assert gateway.session_stop is None
    assert len(upstream) == 1
