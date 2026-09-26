import sys
import threading

import pytest

from agentvisor.recovery import failure_layer, record_recovery
from agentvisor.store import Store
from test_supervisor import finish, make


class RecordedWait(threading.Event):
    def __init__(self):
        super().__init__()
        self.delays = []

    def wait(self, timeout=None):
        self.delays.append(timeout)
        return self.is_set()


@pytest.mark.parametrize(('reason', 'details', 'layer', 'reload', 'delays'), [
    ('idle_timeout', {'pending_tools': [{'tool': 'bash', 'command': 'npm run dev',
                                       'status': 'running'}]}, 'tool', False, [0.1, 0.1, 0.1]),
    ('tool_timeout', {}, 'tool', False, [0.1, 0.1, 0.1]),
    ('tool_loop', {}, 'tool', False, [0.1, 0.1, 0.1]),
    ('idle_timeout', {}, 'agent', False, [0.1, 0.1, 0.1]),
    ('runtime_unavailable', {}, 'runtime', True, [0.1, 0.2, 0.4]),
    ('inference_error', {'error_detail': 'connection reset'}, 'runtime', True, [0.1, 0.2, 0.4]),
    (None, {'error_detail': 'out of memory'}, 'runtime', True, [0.1, 0.2, 0.4]),
])
def test_supervisor_recovers_only_the_failing_component(tmp_path, monkeypatch, reason,
                                                       details, layer, reload, delays):
    store, engine, task = make(tmp_path, autonomous_recovery=True, max_iterations=3,
                              max_failures=1)
    profiles = []

    def ensure(profile, cancel=None):
        profiles.append(dict(profile))
        return {'instance': 'fake', 'context': profile['context']}

    engine.runtime.ensure = ensure
    engine.cancel = RecordedWait()
    monkeypatch.setattr('agentvisor.supervisor.execute', lambda *args, **kwargs: dict(
        {'failed': True, 'reason': reason, 'exit_code': -1, 'output_tokens': 0,
         'duration': 0.01, 'error_detail': ''}, **details))
    engine.run(task['id'])
    current = store.get(task['id'])
    assert current['iteration'] == 3
    assert current['recovery_context']['failure_layer'] == layer
    assert engine.cancel.delays == delays
    assert [bool(item.get('_reload')) for item in profiles] == [False, reload, reload]
    events = store.events(task['id'])
    assert sum(item['kind'] == 'model_reload_requested' for item in events) == (3 if reload else 0)


def test_verification_keeps_its_own_failure_and_output(tmp_path):
    store, engine, task = make(tmp_path, max_failures=2, verification=[
        sys.executable, '-c', 'print("acceptance: expected index missing", flush=True); raise SystemExit(7)'])
    seen = []
    command = engine.command

    def capture(current, prompt, ready):
        seen.append(prompt)
        return command(current, prompt, ready)

    engine.command = capture
    engine.start(task['id'])
    finish(engine)
    current = store.get(task['id'])
    recovery = current['recovery_context']
    assert current['status'] == 'blocked'
    assert recovery['failure_layer'] == 'verification'
    assert recovery['exit_code'] == 7
    assert 'acceptance: expected index missing' in recovery['output_tail']
    assert 'acceptance: expected index missing' in seen[1]
    assert not any(item['kind'] == 'model_reload_requested' for item in store.events(task['id']))


def test_runtime_preparation_failure_reloads_and_preserves_backoff(tmp_path):
    store, engine, task = make(tmp_path, max_failures=3)
    profiles = []

    def ensure(profile, cancel=None):
        profiles.append(dict(profile))
        if len(profiles) < 3:
            raise RuntimeError('model runtime is unavailable')
        return {'instance': 'fake', 'context': profile['context']}

    engine.runtime.ensure = ensure
    engine.cancel = RecordedWait()
    engine.run(task['id'])
    assert store.get(task['id'])['status'] == 'completed_unverified'
    assert [bool(item.get('_reload')) for item in profiles] == [False, True, True]
    assert engine.cancel.delays == [0.1, 0.2]


def test_tool_diagnostics_and_repeat_count_survive_restart_bounded(tmp_path):
    store, _, task = make(tmp_path)
    tool = {'id': 'call_1', 'tool': 'bash', 'status': 'error', 'command': 'npm test',
            'input': {'command': 'npm test', 'timeout': 1000},
            'output': 'x' * 9000 + 'specific failure', 'error': 'exit 2', 'exit_code': 2,
            'provider_metadata': 'must not retain'}
    result = {'reason': 'tool_failure', 'pending_tools': [tool] * 20,
              'tool_failures': [tool] * 20, 'output_tail': 'x' * 9000 + 'process failed'}
    for iteration in range(1, 8):
        task = store.update(task['id'], iteration=iteration)
        task = record_recovery(store, task, result)
    restored = Store(tmp_path / 'data').get(task['id'])['recovery_context']
    assert len(restored['history']) == 5
    assert restored['repeated_failure_count'] == 7
    assert restored['failure_layer'] == 'tool'
    assert len(restored['pending_tools']) == len(restored['tool_failures']) == 4
    assert restored['tool_failures'][0]['input']['command'] == 'npm test'
    assert restored['tool_failures'][0]['output'].endswith('specific failure')
    assert len(restored['tool_failures'][0]['output']) == 2000
    assert 'provider_metadata' not in restored['tool_failures'][0]
    assert len(restored['output_tail']) == 4000


def test_verification_and_tool_oom_do_not_change_model_profile():
    assert failure_layer({'kind': 'verify'}, 'out of memory', preparing=True) == 'verification'
    assert failure_layer({'reason': 'tool_failure'}, 'out of memory') == 'tool'
    assert failure_layer({'pending_tools': [{'command': 'npm test'}]}, 'out of memory') == 'tool'
    assert failure_layer({'tool_failures': [{'command': 'npm test'}]}, 'out of memory') == 'tool'
    assert failure_layer({}, 'request (20581 tokens) exceeds context size', preparing=True) == 'context'
