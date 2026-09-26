from copy import deepcopy
import threading

import pytest

from agentvisor.process_wait import wait_any
from test_command_sessions import python_command, sessions, wait_result


class FakeRunner:
    def __init__(self, results):
        self.results = results
        self.cancel = threading.Event()
        self.closing = threading.Event()
        self.calls = []

    def poll(self, payload):
        self.calls.append(payload)
        identifier = payload['process_id']
        if identifier not in self.results:
            raise ValueError('Unknown owned process_id')
        return deepcopy(self.results[identifier])


class ClockEvent:
    def __init__(self, clock, on_wait=None):
        self.clock, self.on_wait = clock, on_wait
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        if self.on_wait:
            self.on_wait()
        return self.stopped


def result(identifier, status='running', exit_code=None, output=''):
    return {'process_id': identifier, 'status': status, 'exit_code': exit_code,
            'output': output, 'stdout_log': 'stdout.log', 'stderr_log': 'stderr.log'}


def test_reports_all_ready_dependencies_and_only_selected_running_ids():
    runner = FakeRunner({'slow': result('slow'), 'ok': result('ok', 'completed', 0, 'useful output'),
                         'bad': result('bad', 'completed', 7, 'compiler error'),
                         'unrelated': result('unrelated')})
    response = wait_any(runner, {'process_ids': ['slow', 'bad', 'ok'], 'yield_ms': 1000})
    assert response['running'] == ['slow']
    assert response['ready'] == [runner.results['bad'], runner.results['ok']]
    assert not response['timed_out']
    assert runner.calls == [{'process_id': key, 'yield_ms': 0} for key in ('slow', 'bad', 'ok')]


@pytest.mark.parametrize('status', ['timed_out', 'failed', 'output_limit', 'stopped', 'cancelled'])
def test_terminal_process_states_are_ready_without_being_reexecuted(status):
    runner = FakeRunner({'one': result('one', status, 1, 'diagnostic')})
    response = wait_any(runner, {'process_ids': ['one']})
    assert response['ready'][0]['status'] == status
    assert response['ready'][0]['output'] == 'diagnostic'
    assert response['running'] == [] and not response['timed_out']
    assert len(runner.calls) == 1


def test_wait_returns_as_soon_as_one_dependency_changes(monkeypatch):
    clock = [100.0]
    runner = FakeRunner({'first': result('first'), 'second': result('second')})

    def complete_first():
        runner.results['first'] = result('first', 'completed', 0, 'ready')

    runner.cancel = ClockEvent(clock, complete_first)
    monkeypatch.setattr('agentvisor.process_wait.time.monotonic', lambda: clock[0])
    response = wait_any(runner, {'process_ids': ['first', 'second'], 'yield_ms': 1000})
    assert response['waited_ms'] == 50
    assert response['ready'] == [runner.results['first']]
    assert response['running'] == ['second']
    assert runner.cancel.waits == [0.05]


@pytest.mark.parametrize('field', ['yield_ms', 'timeout_ms'])
def test_wait_budget_is_shared_across_all_processes_and_does_not_busy_spin(monkeypatch, field):
    clock = [0.0]
    identifiers = [str(index) for index in range(16)]
    runner = FakeRunner({identifier: result(identifier) for identifier in identifiers})
    runner.cancel = ClockEvent(clock)
    monkeypatch.setattr('agentvisor.process_wait.time.monotonic', lambda: clock[0])
    response = wait_any(runner, {'process_ids': identifiers, field: 125})
    assert response == {'ready': [], 'running': identifiers, 'timed_out': True, 'waited_ms': 125}
    assert runner.cancel.waits == pytest.approx([0.05, 0.05, 0.025])
    assert len(runner.calls) == 4 * 16
    assert all(call['yield_ms'] == 0 for call in runner.calls)


def test_zero_wait_is_one_nonblocking_snapshot(monkeypatch):
    runner = FakeRunner({'one': result('one')})

    def unexpected_wait(*_):
        pytest.fail('Zero wait must not sleep')

    monkeypatch.setattr(runner.cancel, 'wait', unexpected_wait)
    response = wait_any(runner, {'process_ids': ['one'], 'yield_ms': 0})
    assert response['ready'] == [] and response['running'] == ['one'] and response['timed_out']
    assert len(runner.calls) == 1


@pytest.mark.parametrize('payload', [
    None, [], {}, {'process_ids': []}, {'process_ids': 'one'}, {'process_ids': ['one', 'one']},
    {'process_ids': ['']}, {'process_ids': ['  ']}, {'process_ids': [1]},
    {'process_ids': [str(index) for index in range(17)]},
    {'process_ids': ['one'], 'yield_ms': -1}, {'process_ids': ['one'], 'yield_ms': 1001},
    {'process_ids': ['one'], 'yield_ms': True}, {'process_ids': ['one'], 'yield_ms': '100'},
    {'process_ids': ['one'], 'yield_ms': float('nan')},
    {'process_ids': ['one'], 'timeout_ms': float('inf')},
    {'process_ids': ['one'], 'yield_ms': 0, 'timeout_ms': True},
    {'process_ids': ['one'], 'yield_ms': 10, 'timeout_ms': 20},
])
def test_invalid_input_does_not_touch_processes(payload):
    runner = FakeRunner({'one': result('one')})
    with pytest.raises(ValueError):
        wait_any(runner, payload)
    assert runner.calls == []


def test_unknown_id_is_not_hidden_by_another_ready_dependency():
    runner = FakeRunner({'one': result('one', 'completed', 0)})
    with pytest.raises(ValueError, match='Unknown owned process_id'):
        wait_any(runner, {'process_ids': ['one', 'unknown']})


@pytest.mark.parametrize('event_name', ['cancel', 'closing', 'explicit'])
def test_cancellation_before_wait_never_polls(event_name):
    runner = FakeRunner({'one': result('one')})
    explicit = threading.Event()
    event = explicit if event_name == 'explicit' else getattr(runner, event_name)
    event.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        wait_any(runner, {'process_ids': ['one']}, explicit)
    assert runner.calls == []


def test_cancellation_interrupts_a_wait_before_full_budget(monkeypatch):
    clock = [0.0]
    runner = FakeRunner({'one': result('one')})
    explicit = ClockEvent(clock, runner.cancel.set)
    monkeypatch.setattr('agentvisor.process_wait.time.monotonic', lambda: clock[0])
    with pytest.raises(InterruptedError, match='cancelled'):
        wait_any(runner, {'process_ids': ['one'], 'yield_ms': 1000}, explicit)
    assert clock[0] == 0.05 and len(runner.calls) == 1


def test_real_owned_process_results_are_preserved_without_starting_new_commands(sessions, monkeypatch):
    finite = sessions.start({'command': python_command("print('dependency-ready', flush=True)"), 'yield_ms': 0})
    completed = wait_result(sessions, finite['process_id'])
    slow = sessions.start({'command': python_command('import time; time.sleep(10)'), 'yield_ms': 0})
    identifiers = [slow['process_id'], finite['process_id']]
    owned = set(sessions.sessions)

    def never_start(*_):
        pytest.fail('wait_any must not start or repeat commands')

    monkeypatch.setattr(sessions, 'start', never_start)
    response = wait_any(sessions, {'process_ids': identifiers})
    assert response['ready'] == [completed]
    assert 'dependency-ready' in response['ready'][0]['output']
    assert response['running'] == [slow['process_id']]
    assert set(sessions.sessions) == owned
