import sys
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.models import parse_cli_json
from agentvisor.store import Store
from agentvisor.supervisor import Supervisor
from agentvisor.tasks import NewTask, Profile, read_document, state_dir


class Runtime:
    def ensure(self, profile, cancel=None):
        return {'instance': 'fake', 'context': profile['context']}


def make(tmp_path, scenario='complete', **overrides):
    workspace = tmp_path / 'project'
    workspace.mkdir(exist_ok=True)
    store = Store(tmp_path / 'data')
    values = NewTask(name='Test', workspace=str(workspace), goal='Complete a test',
                     profile=Profile(model='fake'), backoff_seconds=0.1,
                     autonomous_recovery=False, step_acceptance=False, checkpoints=False).model_dump()
    values.update(overrides)
    task = store.create(values)
    engine = Supervisor(store, Runtime(), lambda t: [sys.executable,
                        str(Path(__file__).with_name('fake_agent.py')), str(state_dir(t)), scenario])
    return store, engine, task


def finish(engine, seconds=10):
    engine.worker.join(timeout=seconds)
    if engine.busy:
        engine.shutdown()
        pytest.fail('Supervisor did not finish within its bound')


def wait_file(file):
    end = time.monotonic() + 5
    while not file.exists() and time.monotonic() < end:
        time.sleep(0.02)
    assert file.exists()


def test_done_is_not_verified_success(tmp_path):
    store, engine, task = make(tmp_path)
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified'
    assert result['output_tokens'] == 17
    assert result['pid'] is None


def test_independent_verification_accepts_success(tmp_path):
    store, engine, task = make(tmp_path, verification=[sys.executable, '-c', 'assert 2 + 2 == 4'])
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'succeeded'
    assert any(e['kind'] == 'verification_finished' for e in store.events(task['id']))


def test_failing_verification_never_succeeds(tmp_path):
    store, engine, task = make(tmp_path, max_iterations=40, max_failures=2,
                              verification=[sys.executable, '-c', 'raise SystemExit(2)'])
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'blocked'
    assert store.get(task['id'])['iteration'] == 2
    assert 'Исчерпаны' in store.get(task['id'])['reason']


@pytest.mark.parametrize('scenario', ['error', 'failure'])
def test_failures_are_bounded_even_with_exit_zero(tmp_path, scenario):
    store, engine, task = make(tmp_path, scenario, max_failures=2)
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'blocked'
    assert result['iteration'] == 2
    assert result['recoveries'] == 1


def test_changing_prose_does_not_hide_stall(tmp_path):
    store, engine, task = make(tmp_path, 'stall', stall_limit=2)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['iteration'] == 2
    assert 'Нет новых' in store.get(task['id'])['reason']


def test_stale_done_does_not_finish_new_goal(tmp_path):
    store, engine, task = make(tmp_path, 'stale', max_iterations=2)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'blocked'


def test_timeout_kills_descendants(tmp_path):
    store, engine, task = make(tmp_path, 'hang', timeout_seconds=0.6, max_failures=1)
    engine.start(task['id'])
    child_file = state_dir(task) / 'child.pid'
    wait_file(child_file)
    child = psutil.Process(int(child_file.read_text()))
    finish(engine)
    assert store.get(task['id'])['status'] == 'blocked'
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE


def test_pause_kills_tree_and_prevents_next_iteration(tmp_path):
    store, engine, task = make(tmp_path, 'hang')
    engine.start(task['id'])
    child_file = state_dir(task) / 'child.pid'
    wait_file(child_file)
    child = psutil.Process(int(child_file.read_text()))
    engine.control(task['id'], 'pause')
    finish(engine)
    assert store.get(task['id'])['status'] == 'paused'
    assert store.get(task['id'])['iteration'] == 1
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE


def test_goal_change_during_iteration_waits_for_fresh_session(tmp_path):
    store, engine, task = make(tmp_path, 'slow')
    engine.start(task['id'])
    wait_file(state_dir(task) / 'started')
    store.update(task['id'], goal='An updated goal', goal_version=2)
    finish(engine)
    result = store.get(task['id'])
    assert result['iteration'] == 2
    assert result['applied_goal_version'] == 2
    assert 'goal_version: 2' in read_document(result, 'DONE.md')


def test_no_parallel_launches(tmp_path):
    store, engine, task = make(tmp_path, 'hang')
    engine.start(task['id'])
    try:
        with pytest.raises(ValueError, match='Уже выполняется'):
            engine.start(task['id'])
    finally:
        engine.shutdown()


def test_restart_preserves_goal_and_pauses_interrupted_task(tmp_path):
    store, engine, task = make(tmp_path)
    store.update(task['id'], status='running', pid=None)
    restored = Store(tmp_path / 'data')
    Supervisor(restored, Runtime())
    assert restored.get(task['id'])['status'] == 'paused'
    assert restored.get(task['id'])['goal'] == task['goal']


def test_api_requires_token_and_rejects_foreign_origin(tmp_path):
    with TestClient(create_app(tmp_path / 'api'), client=('127.0.0.1', 50000)) as client:
        token = client.get('/api/session').json()['token']
        payload = NewTask(name='Example', workspace=str(tmp_path), goal='A clear goal').model_dump()
        assert client.post('/api/tasks', json=payload).status_code == 403
        headers = {'x-agentvisor-token': token, 'Origin': 'http://foreign.example'}
        assert client.post('/api/tasks', json=payload, headers=headers).status_code == 403
        headers.pop('Origin')
        response = client.post('/api/tasks', json=payload, headers=headers)
        assert response.status_code == 201
        task_id = response.json()['id']
        response = client.patch(f'/api/tasks/{task_id}', json={'goal': 'Changed goal'}, headers=headers)
        assert response.json()['goal_version'] == 2
        assert client.get('/api/tasks/absent').status_code == 404


def test_model_profile_budget_and_url_validation():
    with pytest.raises(ValueError):
        Profile(context=4096, output_limit=4096)
    with pytest.raises(ValueError):
        Profile(base_url='file:///etc/passwd')
    with pytest.raises(ValueError):
        Profile(base_url='http://localhost:1234/v1')
    assert parse_cli_json('Waking up service...\n[{"id":"a"}]') == [{'id': 'a'}]


def test_original_project_documents_are_preserved(tmp_path):
    store, engine, task = make(tmp_path)
    original = Path(task['workspace']) / 'GOAL.md'
    original.write_text('Original user goal')
    engine.start(task['id'])
    finish(engine)
    assert original.read_text() == 'Original user goal'


def test_pause_then_resume_keeps_history(tmp_path):
    store, engine, task = make(tmp_path, 'hang')
    engine.start(task['id'])
    wait_file(state_dir(task) / 'child.pid')
    engine.control(task['id'], 'pause')
    finish(engine)
    before = store.get(task['id'])['elapsed']
    engine.command_builder = lambda t: [sys.executable, str(Path(__file__).with_name('fake_agent.py')),
                                        str(state_dir(t)), 'complete']
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['iteration'] == 2
    assert result['elapsed'] > before
    assert result['status'] == 'completed_unverified'
    assert any(e['kind'] == 'paused' for e in store.events(task['id']))


def test_unicode_and_spaces_in_workspace(tmp_path):
    root = tmp_path / 'проект с пробелами'
    root.mkdir()
    store, engine, task = make(root)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'completed_unverified'


def test_context_overflow_recovers_with_larger_profile(tmp_path):
    store, engine, task = make(tmp_path, max_failures=2)
    profiles = []

    class OverflowRuntime:
        def ensure(self, profile, cancel=None):
            profiles.append(profile['context'])
            if profile['context'] < 32768:
                raise RuntimeError('request (20581 tokens) exceeds the available context size (16384 tokens)')
            return {'instance': 'fake', 'context': profile['context']}

    engine.runtime = OverflowRuntime()
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert profiles == [16384, 32768]
    assert result['status'] == 'completed_unverified'
    assert result['context_floor'] == 32768
    assert result['recoveries'] == 1


def test_context_and_memory_failures_do_not_oscillate_forever(tmp_path):
    store, engine, task = make(tmp_path, max_failures=3)
    profiles = []

    class IncompatibleRuntime:
        def ensure(self, profile, cancel=None):
            profiles.append(profile['context'])
            if profile['context'] < 32768:
                raise RuntimeError('request (20581 tokens) exceeds the available context size (16384 tokens)')
            raise RuntimeError('out of memory')

    engine.runtime = IncompatibleRuntime()
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'blocked'
    assert profiles == [16384, 32768, 32768]


def test_process_output_decodes_unicode_and_optional_stderr():
    from agentvisor.processes import capture
    result = capture([sys.executable, '-c',
                      'import sys; print("Привет"); print("Оценка памяти", file=sys.stderr)'],
                     include_stderr=True)
    assert 'Привет' in result
    assert 'Оценка памяти' in result


def test_watchdog_recovers_missing_model_during_hung_agent(tmp_path, monkeypatch):
    from agentvisor.execution import execute as real_execute
    monkeypatch.setattr('agentvisor.supervisor.execute',
                        lambda *args, **kwargs: real_execute(*args, **kwargs, health_interval=0.05))
    store, engine, task = make(tmp_path, max_failures=2)
    attempts = []

    class WatchedRuntime(Runtime):
        def ensure(self, profile, cancel=None):
            attempts.append(True)
            return super().ensure(profile, cancel)

        def health(self, profile, instance):
            return 'Рабочий экземпляр модели выгружен из памяти' if len(attempts) == 1 else None

    engine.runtime = WatchedRuntime()
    engine.command_builder = lambda task: [sys.executable, str(Path(__file__).with_name('fake_agent.py')),
                                           str(state_dir(task)), 'hang' if len(attempts) == 1 else 'complete']
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified'
    assert result['recoveries'] == 1 and result['iteration'] == 2
    assert any(event['kind'] == 'runtime_lost' for event in store.events(task['id']))
    child = int((state_dir(task) / 'child.pid').read_text())
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_watchdog_tolerates_one_failed_probe(tmp_path, monkeypatch):
    from agentvisor.execution import execute as real_execute
    monkeypatch.setattr('agentvisor.supervisor.execute',
                        lambda *args, **kwargs: real_execute(*args, **kwargs, health_interval=0.05))
    store, engine, task = make(tmp_path, 'slow')
    probes = []

    def health(profile, instance):
        probes.append(True)
        return 'temporary timeout' if len(probes) == 1 else None

    engine.runtime.health = health
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified' and result['iteration'] == 1
    assert result['recoveries'] == 0 and len(probes) > 1


def test_pause_stops_watchdog_without_reloading_model(tmp_path):
    store, engine, task = make(tmp_path, 'hang')
    probes = []
    engine.runtime.health = lambda *args: (probes.append(True) or None)
    engine.start(task['id'])
    wait_file(state_dir(task) / 'child.pid')
    engine.control(task['id'], 'pause')
    finish(engine)
    assert store.get(task['id'])['status'] == 'paused'
    assert store.get(task['id'])['recoveries'] == 0
    count = len(probes)
    time.sleep(0.1)
    assert len(probes) == count
