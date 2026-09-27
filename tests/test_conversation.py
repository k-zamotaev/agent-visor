import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.conversation import conversation
from agentvisor.store import Store
from agentvisor.tasks import NewTask
from agentvisor.user_instructions import apply
from test_progress_protocol import fixture, update
from test_user_instructions import arguments


def make(tmp_path):
    store = Store(tmp_path / 'state')
    task = store.create(NewTask(name='Task', workspace=str(tmp_path), goal='Build an app').model_dump())
    return store, task


def all_pages(store, identity, limit=3):
    pages, before = [], None
    while True:
        page = conversation(store, identity, before=before, limit=limit)
        pages.append(page['items'])
        if not page['has_more']:
            assert page['next_before'] is None
            break
        before = page['next_before']
        assert before
        assert len(pages) < 100
    return [item for page in reversed(pages) for item in page]


def test_pagination_merges_messages_events_and_keeps_all_notes_reachable(tmp_path):
    store, task = make(tmp_path)
    for index in range(12):
        store.add_context(task['id'], f'User message {index}')
        store.event(task['id'], 'text', f'Model answer {index}')
        for _ in range(30):
            store.event(task['id'], 'generation_sample', 'Hidden telemetry')
    entries = all_pages(store, task['id'])
    assert len(entries) == 25
    assert len({item['id'] for item in entries}) == len(entries)
    assert [item['text'] for item in entries if item['role'] == 'user'] == [f'User message {i}' for i in range(12)]
    assert [item['text'] for item in entries if item['kind'] == 'text'] == [f'Model answer {i}' for i in range(12)]
    assert [item['time'] for item in entries] == sorted(item['time'] for item in entries)


def test_grouped_reasoning_and_tool_trace_do_not_flood_timeline(tmp_path):
    store, task = make(tmp_path)
    store.event(task['id'], 'session_role', 'Role', data={'name': 'reviewer'})
    store.event(task['id'], 'reasoning', 'Start ', data={'request_id': 'req-1'})
    store.event(task['id'], 'tool_started', 'read', data={'call_id': 'call-1', 'tool': 'read', 'input': {'path': 'x'}})
    for index in range(240):
        store.event(task['id'], 'reasoning', f'{index} ', data={'request_id': 'req-1'})
    store.event(task['id'], 'tool_finished', 'read', data={'call_id': 'call-1', 'tool': 'read', 'status': 'completed', 'output': 'result'})
    store.event(task['id'], 'tool', 'Read x', data={'call_id': 'call-1', 'status': 'completed'})
    store.event(task['id'], 'text', 'Checked')
    entries = all_pages(store, task['id'], 2)
    assert len(entries) == 4
    reasoning = next(item for item in entries if item['kind'] == 'reasoning')
    assert reasoning['actor'] == 'reviewer'
    assert reasoning['text'] == 'Start ' + ''.join(f'{i} ' for i in range(240))
    assert reasoning['details']['fragment_count'] == 241
    assert reasoning['details']['collapsed'] and not reasoning['details']['truncated']
    tool = next(item for item in entries if item['kind'] == 'tool')
    assert tool['details']['input'] == {'path': 'x'}
    assert tool['details']['output'] == 'result' and tool['state'] == 'completed'
    assert tool['actor'] == 'reviewer'


def test_unidentified_messages_are_not_accidentally_filtered_out(tmp_path):
    store, task = make(tmp_path)
    store.event(task['id'], 'reasoning', 'CLI reasoning without request id')
    store.event(task['id'], 'tool', 'Legacy tool without call id')
    store.event(task['id'], 'text', '<script>this remains plain text</script>')
    entries = conversation(store, task['id'])['items']
    assert len(entries) == 4
    assert entries[-1]['text'] == '<script>this remains plain text</script>'


def test_group_append_does_not_move_cursor_or_duplicate_group(tmp_path):
    store, task = make(tmp_path)
    store.event(task['id'], 'reasoning', 'Old ', data={'request_id': 'old'})
    for index in range(5):
        store.event(task['id'], 'text', str(index))
    latest = conversation(store, task['id'], limit=3)
    store.event(task['id'], 'reasoning', 'updated', data={'request_id': 'old'})
    older = conversation(store, task['id'], before=latest['next_before'], limit=3)
    assert not {item['id'] for item in latest['items']} & {item['id'] for item in older['items']}
    assert next(item['text'] for item in older['items'] if item['kind'] == 'reasoning') == 'Old updated'


def test_same_timestamp_uses_stable_cursor_order(tmp_path):
    store, task = make(tmp_path)
    for index in range(14):
        store.event(task['id'], 'text', str(index))
    store.add_context(task['id'], 'Same timestamp note')
    current = store.get(task['id'])
    current['context_additions'][0]['created'] = 123.0
    store.update(task['id'], context_additions=current['context_additions'])
    with store.connect() as db:
        db.execute('UPDATE events SET time=123 WHERE task_id=?', (task['id'],))
    entries = all_pages(store, task['id'], 2)
    assert len(entries) == 16 and len({item['id'] for item in entries}) == 16
    assert entries[-1]['role'] == 'user'
    assert [item['text'] for item in entries if item['kind'] == 'text'] == [str(i) for i in range(14)]


@pytest.mark.parametrize('cursor', ['bad!', 'e30', 'bnVsbA', 'W10', 'x' * 1001])
def test_invalid_cursors_are_rejected(tmp_path, cursor):
    store, task = make(tmp_path)
    with pytest.raises(ValueError, match='Invalid conversation cursor'):
        conversation(store, task['id'], before=cursor)


def test_cursor_cannot_cross_task_boundaries(tmp_path):
    store, first = make(tmp_path)
    second = store.create(NewTask(name='Second', workspace=str(tmp_path), goal='Other app').model_dump())
    store.event(first['id'], 'text', 'Answer')
    cursor = conversation(store, first['id'], limit=1)['next_before']
    with pytest.raises(ValueError, match='Invalid conversation cursor'):
        conversation(store, second['id'], before=cursor)
    for timestamp in (float('nan'), float('inf')):
        bad = base64.urlsafe_b64encode(json.dumps([1, first['id'], timestamp, 'E:00000000000000000001']).encode()).decode().rstrip('=')
        with pytest.raises(ValueError):
            conversation(store, first['id'], before=bad)


def test_context_retry_is_atomic_and_mismatch_does_not_mutate(tmp_path):
    store, task = make(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.add_context(task['id'], 'Keep user data', recheck=True,
                                                 client_message_id='request-1'), range(20)))
    saved = store.get(task['id'])
    assert saved['context_version'] == 1 and saved['review_revision'] == 1
    assert len(saved['context_additions']) == 1
    assert len([event for event in store.events(task['id']) if event['kind'] == 'context_added']) == 1
    for values in ({'text': 'Other text', 'recheck': True}, {'text': 'Keep user data', 'recheck': False},
                   {'text': 'Keep user data', 'recheck': True, 'kind': 'reference'}):
        with pytest.raises(ValueError, match='different message'):
            store.add_context(task['id'], **values, client_message_id='request-1')
        assert store.get(task['id']) == saved


def test_retry_still_succeeds_at_context_capacity(tmp_path):
    store, task = make(tmp_path)
    for index in range(40):
        store.add_context(task['id'], str(index), client_message_id=f'msg-{index}')
    saved = store.get(task['id'])
    assert store.add_context(task['id'], '39', client_message_id='msg-39') == saved
    assert conversation(store, task['id'])['composer']['can_send'] is False
    assert conversation(store, task['id'])['composer']['messages_remaining'] == 0


def test_statuses_refresh_from_canonical_plan_without_writing(tmp_path):
    store, _, task = fixture(tmp_path)
    current = store.add_context(task['id'], 'Improve interface', client_message_id='design')
    store.add_context(task['id'], 'Browser endpoint', kind='reference')
    assert [item['state'] for item in conversation(store, task['id'])['items'] if item['role'] == 'user'] == ['pending', 'queued']
    applied = apply(store, task, arguments(current, [1], ['Polish layout and check browser usability']))
    identity = applied['user_instructions'][0]['step_ids'][0]
    update(store, task, 'claim', step_id=identity, note='Browser checked')
    receipts = copy.deepcopy(store.get(task['id'])['step_reviews'])
    receipts['accepted'][identity] = {'review_id': 'design-review'}
    store.update(task['id'], step_reviews=receipts, status='paused')
    store.mark_context_delivered(task['id'], 2)
    before, events = store.get(task['id']), store.events(task['id'])
    result = conversation(store, task['id'])
    assert [item['state'] for item in result['items'] if item['role'] == 'user'] == ['verified', 'delivered']
    assert result['composer']['paused'] and result['composer']['can_send']
    assert store.get(task['id']) == before and store.events(task['id']) == events


def test_reasoning_payload_is_bounded_and_honest(tmp_path):
    store, task = make(tmp_path)
    for _ in range(400):
        store.event(task['id'], 'reasoning', 'x' * 5000, data={'request_id': 'large'})
    item = conversation(store, task['id'])['items'][-1]
    assert len(item['text']) == 16000
    assert item['details']['truncated'] and item['details']['fragment_count'] == 400


def test_long_assistant_answers_preserve_storage_and_api_text(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        store = app.state.store
        task = store.create(NewTask(name='Task', workspace=str(tmp_path), goal='Build an app').model_dump())
        store.event(task['id'], 'session_role', 'Previous role', data={'name': 'executor'})
        answer = 'Ответ модели с подробным результатом.\n' * 900
        store.event(task['id'], 'text', answer, data={'actor': 'reviewer', 'iteration': 4, 'part_id': 'part-1'})
        event = store.events(task['id'])[-1]
        assert 16000 < len(answer) < 64000 and event['message'] == answer
        assert not event['data'].get('truncated')
        path = f'/api/tasks/{task["id"]}'
        item = client.get(path + '/conversation').json()['items'][-1]
        assert item['text'] == answer and item['actor'] == 'reviewer'
        assert item['details']['iteration'] == 4 and item['details']['part_id'] == 'part-1'
        assert client.get(path + '/events').json()[-1]['message'] == answer
        oversized = 'Ю' * 65000
        store.event(task['id'], 'text', oversized)
        stored = store.events(task['id'])[-1]
        assert stored['message'] == oversized[:64000]
        assert stored['data']['truncated'] and stored['data']['original_chars'] == 65000
        item = client.get(path + '/conversation').json()['items'][-1]
        assert item['text'] == oversized[:64000]
        assert item['details']['truncated'] and item['details']['original_chars'] == 65000
        store.event(task['id'], 'output', oversized)
        assert len(store.events(task['id'])[-1]['message']) == 6000


def test_api_message_id_readonly_conversation_language_and_validation(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        task = client.post('/api/tasks', json={'name': 'App', 'workspace': str(tmp_path), 'goal': 'Build an app'}).json()
        path = f'/api/tasks/{task["id"]}'
        message = {'text': '  Improve design  ', 'client_message_id': 'unique-message'}
        assert client.post(path + '/context', json=message).status_code == 200
        assert client.post(path + '/context', json=message).status_code == 200
        store = app.state.store
        before = store.get(task['id'])
        monkeypatch.setattr('agentvisor.progress_plan.sync_document', lambda *_: pytest.fail('Conversation must not sync or rewrite documents'))
        result = client.get(path + '/conversation', headers={'accept-language': 'en'}).json()
        assert result['items'][0]['text'] == 'Task created'
        assert len([item for item in result['items'] if item['role'] == 'user']) == 1
        assert result['composer']['messages_remaining'] == 39
        assert result['composer']['chars_remaining'] == 20000 - len('Improve design')
        assert store.get(task['id']) == before and before['status'] == 'draft'
        assert client.get(path + '/conversation?before=invalid!').status_code == 400
        assert client.get(path + '/conversation?limit=101').status_code == 400
        assert client.get('/api/tasks/missing/conversation').status_code == 404
        assert client.post(path + '/context', json={**message, 'text': 'Other'}).status_code == 400
        assert client.post(path + '/context', json={**message, 'client_message_id': 'bad value'}).status_code == 422
        store.update(task['id'], status='succeeded')
        assert client.get(path + '/conversation').json()['composer']['can_send'] is False
        completed = store.get(task['id'])
        assert client.post(path + '/context', json=message).status_code == 200
        assert store.get(task['id']) == completed
        assert client.post(path + '/context', json={'text': 'New requirement'}).status_code == 400


def test_original_event_api_reads_old_groups_without_leaking_tasks_or_mutation(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        store = app.state.store
        first = store.create(NewTask(name='Task', workspace=str(tmp_path), goal='Build an app').model_dump())
        second = store.create(NewTask(name='Other', workspace=str(tmp_path), goal='Other app').model_dump())
        store.event(first['id'], 'reasoning', 'Start. ', data={'request_id': 'old-response'})
        identity = store.events(first['id'])[-1]['id']
        fragments = ['Start. ']
        for index in range(30):
            fragments.append(f'{index}: ' + 'Long reasoning. ' * 100)
            store.event(first['id'], 'reasoning', fragments[-1], data={'request_id': 'old-response'})
        store.event(second['id'], 'reasoning', 'Foreign secret', data={'request_id': 'old-response'})
        foreign_id = store.events(second['id'])[-1]['id']
        for index in range(250):
            store.event(first['id'], 'text', f'Later reply {index}')
        before, events = store.get(first['id']), store.events(first['id'])
        path = f'/api/tasks/{first["id"]}/events'
        assert not any(event['id'] == identity for event in client.get(path + '?grouped=true').json())
        response = client.get(path + f'/{identity}')
        assert response.status_code == 200
        item = response.json()
        assert item['id'] == identity and item['kind'] == 'reasoning'
        assert item['message'] == ''.join(fragments) and not item['truncated']
        assert item['fragment_count'] == 31 and 'Foreign secret' not in item['message']
        assert client.get(path + f'/{foreign_id}').status_code == 404
        assert client.get(path + '/99999999').status_code == 404
        assert client.get(f'/api/tasks/missing/events/{identity}').status_code == 404
        assert store.get(first['id']) == before and store.events(first['id']) == events


def test_original_event_api_bounds_large_groups_and_preserves_assistant_text(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        store = app.state.store
        task = store.create(NewTask(name='Task', workspace=str(tmp_path), goal='Build an app').model_dump())
        path = f'/api/tasks/{task["id"]}/events'
        for _ in range(30):
            store.event(task['id'], 'reasoning', 'r' * 5000, data={'request_id': 'huge'})
        identity = store.events(task['id'])[-1]['id']
        item = client.get(path + f'/{identity}').json()
        assert item['message'] == 'r' * 128000
        assert item['truncated'] and item['data']['original_chars'] == 150000
        store.event(task['id'], 'text', 'a' * 64000)
        identity = store.events(task['id'])[-1]['id']
        item = client.get(path + f'/{identity}').json()
        assert item['message'] == 'a' * 64000 and not item['truncated']
        store.event(task['id'], 'tool_started', 'read', data={'tool': 'read', 'call_id': 'tool-1', 'input': {'path': 'x'}})
        identity = store.events(task['id'])[-1]['id']
        store.event(task['id'], 'tool_finished', 'read', data={'tool': 'read', 'call_id': 'tool-1', 'status': 'completed', 'output': 'File content'})
        item = client.get(path + f'/{identity}').json()
        assert item['data']['status'] == 'completed' and item['data']['output'] == 'File content'
        assert item['data']['input'] == {'path': 'x'}
