import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.tasks import NewTask, read_document, state_dir, write_document
from test_supervisor import finish, make


def test_exhausted_task_can_extend_budget_and_resume_without_reset(tmp_path):
    store, engine, task = make(tmp_path)
    task = store.update(task['id'], status='blocked', elapsed=43232.45, iteration=45,
                        max_iterations=100, max_hours=12)
    with pytest.raises(ValueError, match='Увеличьте общий предел'):
        engine.start(task['id'])
    assert engine.worker is None
    assert store.get(task['id']) == task
    state_dir(task)
    write_document(task, 'PROGRESS.md', '# Progress\n- [x] Existing milestone\n')
    app = create_app(store.directory)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        app.state.engine = engine
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        path = f'/api/tasks/{task["id"]}'
        response = client.patch(path, json={'max_hours': 24})
        assert response.status_code == 200
        updated = response.json()
        for field in ('elapsed', 'iteration', 'goal', 'goal_version', 'status'):
            assert updated[field] == task[field]
        assert 'Existing milestone' in read_document(updated, 'PROGRESS.md')
        assert store.events(task['id'])[-1]['data']['changes']['max_hours'] == {'before': 12, 'after': 24}
        assert client.post(path + '/start').status_code == 200
        finish(engine)
        resumed = store.get(task['id'])
        assert resumed['elapsed'] > task['elapsed']
        assert resumed['iteration'] == 46
        assert resumed['goal_version'] == task['goal_version']
        assert resumed['status'] == 'completed_unverified'


def test_budget_patch_validation_and_unchanged_goal(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        task = client.post('/api/tasks', json=NewTask(name='Budget', workspace=str(tmp_path),
                           goal='Original goal').model_dump()).json()
        path = f'/api/tasks/{task["id"]}'
        app.state.store.update(task['id'], status='running', iteration=3, elapsed=100)
        for value in (0, -1, 169):
            assert client.patch(path, json={'max_hours': value}).status_code == 422
        for value in (0, 1001, 1.5):
            assert client.patch(path, json={'max_iterations': value}).status_code == 422
        changed = client.patch(path, json={'goal': task['goal'], 'max_hours': 24}).json()
        assert changed['goal_version'] == 1
        assert changed['elapsed'] == 100 and changed['iteration'] == 3 and changed['status'] == 'running'
        event_count = len(app.state.store.events(task['id']))
        assert client.patch(path, json={'max_hours': 24}).status_code == 200
        assert len(app.state.store.events(task['id'])) == event_count
        assert client.patch(path, json={'goal': 'New objective'}).json()['goal_version'] == 2
        app.state.store.update(task['id'], status='succeeded')
        assert client.patch(path, json={'max_hours': 48}).status_code == 400


def test_exhausted_iterations_require_extension_before_start(tmp_path):
    store, engine, task = make(tmp_path, max_iterations=5)
    store.update(task['id'], iteration=5, status='blocked')
    with pytest.raises(ValueError, match='Лимит итераций исчерпан'):
        engine.start(task['id'])
    assert not engine.busy
    store.update(task['id'], max_iterations=6)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['iteration'] == 6


def test_grouping_restores_full_response_beyond_raw_tail_and_preserves_order(tmp_path):
    store, _, task = make(tmp_path)
    fragments = ['First sentence.\n', *[f'word{i} ' for i in range(230)], 'Last sentence.']
    for index, fragment in enumerate(fragments):
        store.event(task['id'], 'reasoning', fragment, data={'request_id': 'first', 'source': 'model_stream'})
        if index == 25:
            store.event(task['id'], 'tool_started', 'Tool remains a separate entry')
    store.event(task['id'], 'reasoning', 'Other response', data={'request_id': 'second'})
    store.event(task['id'], 'reasoning', 'CLI response without request identity')
    store.event(task['id'], 'reasoning', 'Another CLI response without identity')
    raw = store.events(task['id'])
    assert len(raw) == 200
    assert raw[0]['message'] != fragments[0]
    groups = store.event_groups(task['id'])
    thought = next(event for event in groups if event['data'].get('request_id') == 'first')
    assert thought['message'] == ''.join(fragments)
    assert thought['fragment_count'] == len(fragments)
    assert thought['first_event_id'] == thought['id'] < thought['last_event_id']
    assert len(groups) == 6  # task_created, tool, two identified and two CLI responses
    assert [event['last_event_id'] for event in groups] == sorted(event['last_event_id'] for event in groups)
    assert store.events(task['id']) == raw
    store.event(task['id'], 'reasoning', ' appended', data={'request_id': 'first'})
    updated = store.event_groups(task['id'], limit=1)[0]
    assert updated['id'] == thought['id']
    assert updated['message'] == ''.join(fragments) + ' appended'
    assert updated['fragment_count'] == len(fragments) + 1


def test_grouped_api_keeps_task_boundaries_raw_cursor_and_language(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        store = app.state.store
        first = store.create(NewTask(name='First', workspace=str(tmp_path), goal='Some goal').model_dump())
        second = store.create(NewTask(name='Second', workspace=str(tmp_path), goal='Another goal').model_dump())
        store.event(first['id'], 'reasoning', 'текст ', data={'request_id': 'same'})
        store.event(second['id'], 'reasoning', 'foreign', data={'request_id': 'same'})
        store.event(first['id'], 'reasoning', '<script>safe text</script>', data={'request_id': 'same'})
        path = f'/api/tasks/{first["id"]}/events'
        raw = client.get(path).json()
        grouped = client.get(path + '?grouped=true', headers={'accept-language': 'en'}).json()
        assert len(grouped) == 2
        assert grouped[0]['message'] == 'Task created'
        assert grouped[1]['message'] == 'текст <script>safe text</script>'
        assert grouped[1]['fragment_count'] == 2
        assert client.get(path + f'?after={raw[-2]["id"]}').json() == raw[-1:]
        assert client.get(path + '?grouped=true&after=1').status_code == 400
        assert client.get('/api/tasks/missing/events?grouped=true').status_code == 404
