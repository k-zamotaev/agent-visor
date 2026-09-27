import copy
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.command_mcp import CommandMCP
from agentvisor.inference import InferenceGateway
from agentvisor.progress_plan import initialize
from agentvisor.step_acceptance import pending_steps, review_prompt
from agentvisor.store import Store
from agentvisor.tasks import write_document
from agentvisor.user_instructions import apply, gate_request, pending, statuses
from test_inference import model_server
from test_progress_protocol import fixture, update
from test_supervisor import make


def arguments(task, versions, steps=None, ids=None):
    return dict(goal_version=task['goal_version'], expected_revision=task['progress_plan']['revision'],
                context_versions=versions, steps=steps or [], existing_step_ids=ids or [])


def add(store, task, text='Make the interface attractive and easy to use', **kwargs):
    return store.add_context(task['id'], text, **kwargs)


def test_repeated_requests_get_one_durable_stage_and_original_review_criteria(tmp_path):
    store, _, task = fixture(tmp_path)
    add(store, task)
    current = add(store, task, 'IMPORTANT: improve the design and usability too')
    saved = apply(store, task, arguments(current, [1, 2], ['Polish layout, navigation and controls; verify usability in a browser']))
    ids = [item['step_ids'] for item in saved['user_instructions']]
    assert ids[0] == ids[1] and len(ids[0]) == 1
    assert [item['state'] for item in saved['user_instructions']] == ['planned', 'planned']
    restarted = Store(store.directory).get(task['id'])
    assert len(restarted['progress_plan']['steps']) == 4
    assert restarted['progress_plan']['steps'][:3] == task['progress_plan']['steps']
    assert restarted['step_reviews'] == task['step_reviews']
    update(store, task, 'claim', step_id=ids[0][0], note='Browser flow checked')
    current = store.get(task['id'])
    assert statuses(current)[0]['state'] == 'review_pending'
    review = {'id': 'new-review', 'steps': pending_steps(current)[-1:]}
    assert [item['version'] for item in review['steps'][0]['user_instructions']] == [1, 2]
    assert 'IMPORTANT: improve the design and usability too' in review_prompt(current, review, 'state')
    receipts = copy.deepcopy(current['step_reviews'])
    receipts['accepted'][ids[0][0]] = {'review_id': 'new-review'}
    current = store.update(task['id'], step_reviews=receipts)
    assert [item['state'] for item in statuses(current)] == ['verified', 'verified']
    current = add(store, current, 'Recheck design', recheck=True)
    assert statuses(current)[0]['state'] == 'review_pending'


def test_delivered_instruction_cannot_complete_even_with_old_done_file(tmp_path):
    store, engine, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [x] Original task\n')
    task = initialize(store, task)
    task = store.update(task['id'], applied_goal_version=1, status='running')
    write_document(task, 'DONE.md', 'goal_version: 1\nDone\n')
    add(store, task)
    store.mark_context_delivered(task['id'], 1)
    assert engine.complete(store.get(task['id'])) is False
    assert statuses(store.get(task['id']))[0]['state'] == 'pending'


def test_reference_is_explicit_and_does_not_create_a_milestone(tmp_path):
    store, engine, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [x] Original task\n')
    initialize(store, task)
    task = store.update(task['id'], applied_goal_version=1, status='running')
    write_document(task, 'DONE.md', 'goal_version: 1\nDone\n')
    current = add(store, task, 'Browser endpoint: localhost', kind='reference')
    assert statuses(current)[0]['state'] == 'queued' and not pending(current)
    store.mark_context_delivered(task['id'], 1)
    assert statuses(store.get(task['id']))[0]['state'] == 'delivered'
    assert engine.complete(store.get(task['id'])) is True


@pytest.mark.parametrize('case', ['unknown', 'reference', 'accepted', 'stale', 'empty', 'extra', 'reviewer', 'old_goal', 'old_scope'])
def test_model_cannot_acknowledge_dismiss_or_reuse_accepted_work(tmp_path, case):
    store, _, task = fixture(tmp_path)
    current = add(store, task, kind='reference' if case == 'reference' else 'instruction')
    args = arguments(current, [1], ['Review new design in browser'])
    session = task
    if case == 'unknown':
        args['context_versions'] = [99]
    elif case == 'accepted':
        args.update(steps=[], existing_step_ids=[task['progress_plan']['steps'][0]['id']])
    elif case == 'stale':
        args['expected_revision'] = 99
    elif case == 'empty':
        args['steps'] = []
    elif case == 'extra':
        args['kind'] = 'reference'
    elif case == 'reviewer':
        session = dict(task, review_phase=True)
    elif case == 'old_goal':
        store.update(task['id'], goal_version=2)
    elif case == 'old_scope':
        store.update(task['id'], review_revision=1)
    with pytest.raises(ValueError):
        apply(store, session, args)
    assert store.get(task['id'])['progress_plan'] == task['progress_plan']


def test_linking_unaccepted_claim_requires_fresh_claim_and_allows_diagnostic_planning(tmp_path):
    store, _, task = fixture(tmp_path)
    current = add(store, task)
    session = dict(task, recovery_context={'goal_version': 1, 'repair': 'Investigate failure'})
    identity = task['progress_plan']['steps'][1]['id']
    saved = apply(store, session, arguments(current, [1], ids=[identity]))
    assert saved['steps'][1]['done'] is False
    assert saved['user_instructions'][0]['state'] == 'planned'
    with pytest.raises(ValueError, match='Only the executor'):
        update(store, session, 'claim', step_id=identity, note='Diagnosis')


def test_new_input_during_integration_stays_pending_and_cas_prevents_duplicate_stages(tmp_path):
    store, _, task = fixture(tmp_path)
    current = add(store, task)
    args = arguments(current, [1], ['Improve design and test it'])
    add(store, task, 'Also support keyboard navigation')
    def attempt(_):
        try:
            apply(store, task, args)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    current = store.get(task['id'])
    assert len(current['progress_plan']['steps']) == 4
    assert [item['version'] for item in pending(current)] == [2]
    current = store.update(task['id'], goal_version=2)
    assert [item['version'] for item in pending(current)] == [1, 2]


def test_pending_instruction_blocks_commands_and_claims_from_old_session(tmp_path):
    store, engine, task = fixture(tmp_path)
    add(store, task)
    mcp = CommandMCP(store, task, engine.cancel)
    try:
        with pytest.raises(ValueError, match='Apply pending user instructions'):
            mcp.call('exec', {'command': 'echo ignored', 'shell': 'powershell'})
        with pytest.raises(ValueError, match='Apply pending user instructions'):
            update(store, task, 'claim', step_id=task['progress_plan']['steps'][2]['id'], note='old session')
        result = mcp.call('get_progress', {})
        assert result['user_instructions'][0]['state'] == 'pending'
    finally:
        mcp.close()


def test_initial_plan_can_be_created_before_integrating_pending_instructions(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize(store, task)
    add(store, task)
    session = dict(task, recovery_context={'goal_version': 1, 'repair': 'Inspect initial failure'})
    update(store, session, 'initialize', steps=['Implement original application'])
    current = store.get(task['id'])
    assert len(pending(current)) == 1
    apply(store, task, arguments(current, [1], ['Design and verify the UI']))
    assert not pending(store.get(task['id']))


def primary_tools():
    return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}} for name in (
        'read', 'agentvisor_process_exec', 'agentvisor_process_get_progress',
        'agentvisor_process_update_progress', 'agentvisor_process_apply_user_instructions')]


def test_every_gateway_request_enforces_fresh_directives_after_compaction(tmp_path, model_server):
    url, requests = model_server
    store, engine, task = fixture(tmp_path)
    profile = dict(task['profile'], base_url=url)
    body = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Compacted history: continue export'}],
            'stream': True, 'tools': primary_tools()}
    with InferenceGateway(store, task, profile, engine.cancel) as gateway, httpx.Client(trust_env=False) as client:
        endpoint = gateway.base_url + '/chat/completions'
        assert client.post(endpoint, json=body).status_code == 200
        current = add(store, task, 'Original design instruction')
        assert client.post(endpoint, json=body).status_code == 200
        sent = requests[-1]['body']
        assert {tool['function']['name'] for tool in sent['tools']} == {
            'agentvisor_process_get_progress', 'agentvisor_process_apply_user_instructions'}
        assert sent['tool_choice'] == 'required'
        assert 'Original design instruction' in sent['messages'][0]['content']
        assert pending(store.get(task['id']))  # successful HTTP delivery did not resolve it
        apply(store, task, arguments(current, [1], ['Design the interface and check usability']))
        assert client.post(endpoint, json=body).status_code == 200
        assert requests[-1]['body']['tools'] == body['tools']
        assert '"state": "planned"' in requests[-1]['body']['messages'][0]['content']
        add(store, task, 'Another instruction')
        assert client.post(endpoint, json=body).status_code == 200
        assert requests[-1]['body']['tool_choice'] == 'required'


def test_review_and_auxiliary_requests_do_not_enter_the_planning_phase(tmp_path):
    store, _, task = fixture(tmp_path)
    current = add(store, task)
    review = {'tools': primary_tools()}
    auxiliary = {'messages': [{'role': 'user', 'content': 'Summarize'}]}
    before = copy.deepcopy((review, auxiliary))
    gate_request(review, current, reviewer=True)
    gate_request(auxiliary, current)
    assert (review, auxiliary) == before


def test_api_defaults_to_enforced_instruction_and_exposes_pending_state(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        task = client.post('/api/tasks', json=dict(name='Task', workspace=str(tmp_path), goal='Build an app')).json()
        endpoint = f'/api/tasks/{task["id"]}'
        assert client.post(endpoint + '/context', json={'text': 'Improve the UI'}).status_code == 200
        assert client.get(endpoint).json()['user_instructions'][0]['state'] == 'pending'
        assert client.post(endpoint + '/context', json={'text': 'Docs link', 'kind': 'reference'}).status_code == 200
        assert client.get(endpoint).json()['user_instructions'][1]['state'] == 'queued'
        assert client.post(endpoint + '/context', json={'text': 'Skip me', 'kind': 'dismissed'}).status_code == 422


def test_new_instruction_during_final_verification_prevents_completion_even_after_delivery(tmp_path, monkeypatch):
    store, engine, task = make(tmp_path, verification='verify')
    write_document(task, 'PROGRESS.md', '- [x] Original task\n')
    task = initialize(store, task)
    task = store.update(task['id'], applied_goal_version=1, status='running')
    write_document(task, 'DONE.md', 'goal_version: 1\nDone\n')
    def verify(*args, **kwargs):
        current = add(store, task)
        apply(store, task, arguments(current, [1], ['Design the UI and check it in a browser']))
        store.mark_context_delivered(task['id'], 1)
        return {'failed': False}
    monkeypatch.setattr('agentvisor.supervisor.execute', verify)
    assert engine.complete(task) is False
    assert store.get(task['id'])['status'] != 'succeeded'


def test_instruction_spanning_multiple_steps_requires_all_receipts_and_scoped_review(tmp_path):
    store, _, task = fixture(tmp_path)
    current = add(store, task, 'Design and implement an accessible UI; verify in browser')
    result = apply(store, task, arguments(current, [1], ['Design and implement the UI', 'Verify accessibility in browser']))
    ids = result['user_instructions'][0]['step_ids']
    update(store, task, 'claim', step_id=ids[0], note='UI implementation checked')
    current = store.get(task['id'])
    request = pending_steps(current)[-1]
    assert [step['id'] for step in request['user_instructions'][0]['related_steps']] == ids
    receipts = copy.deepcopy(current['step_reviews'])
    receipts['accepted'][ids[0]] = {'review_id': 'first-only'}
    current = store.update(task['id'], step_reviews=receipts)
    assert statuses(current)[0]['state'] == 'planned'
    update(store, task, 'claim', step_id=ids[1], note='Browser check passed')
    assert statuses(store.get(task['id']))[0]['state'] == 'review_pending'
    receipts['accepted'][ids[1]] = {'review_id': 'browser-check'}
    current = store.update(task['id'], step_reviews=receipts)
    assert statuses(current)[0]['state'] == 'verified'


def test_new_directive_is_processed_before_draining_old_pending_reviews(tmp_path, monkeypatch):
    store, engine, task = fixture(tmp_path)
    current = add(store, task)
    calls = []
    monkeypatch.setattr(engine, 'review_step', lambda *args: calls.append(args))
    engine.review_steps(current, {}, {})
    assert calls == []
