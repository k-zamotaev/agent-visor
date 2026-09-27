"""Exercise real process cleanup at a controlled model-session boundary."""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentvisor.command_mcp import CommandMCP
from agentvisor.execution import execute
from agentvisor.task_memory import initialize_memory, remember_iteration
from agentvisor.tool_trace import ToolTrace
from test_command_sessions import python_command
from test_supervisor import make


@pytest.fixture
def session(tmp_path, monkeypatch):
    store, _, task = make(tmp_path)
    task = store.update(task['id'], timeout_seconds=8, idle_timeout_seconds=5)
    task = initialize_memory(store, task)
    cancel = threading.Event()
    commands = CommandMCP(store, task, cancel)
    trace = ToolTrace(store, task)
    commands.progress = trace.progress
    gateway = SimpleNamespace(commands=commands, trace=trace, session_stop=None,
                              reasoning_seen=False, activity=time.monotonic)
    monkeypatch.setattr('agentvisor.command_mcp.check_command_policy',
                        lambda *args, **kwargs: {'allowed': True})
    yield store, task, cancel, gateway
    cancel.set()
    commands.close()


def handoff(gateway):
    gateway.session_stop = {'reason': 'context_handoff', 'blocked': False,
                            'evidence': {'input_limit': 48000, 'estimated_tokens': 51000}}


def cli(store, task, cancel, gateway, source='import time; time.sleep(30)'):
    return execute(store, task, [sys.executable, '-c', source], cancel, inference=gateway)


def test_cli_409_error_and_exit_still_produce_controlled_handoff(session, monkeypatch):
    store, task, cancel, gateway = session
    event = store.event

    def observe(task_id, kind, message, *args, **kwargs):
        value = event(task_id, kind, message, *args, **kwargs)
        if kind == 'agent_error':
            handoff(gateway)
        return value

    monkeypatch.setattr(store, 'event', observe)
    source = ('import json,sys; print(json.dumps({"type":"error",'
              '"error":{"status":409,"message":"supervisor_session_boundary"}}),flush=True);sys.exit(2)')
    result = cli(store, task, cancel, gateway, source)
    assert result['reason'] == 'context_handoff' and result['failed'] is False
    assert result['session_handoff']['evidence']['input_limit'] == 48000
    assert store.get(task['id'])['pid'] is None
    assert any(item['kind'] == 'agent_error' for item in store.events(task['id']))


def test_finite_command_finishes_and_its_actual_result_is_retained(session):
    store, task, cancel, gateway = session
    command = gateway.commands.call('exec', {
        'command': python_command('import time; time.sleep(1); print("export check finished",flush=True)'),
        'timeout_ms': 5000, 'yield_ms': 0})
    assert command['status'] == 'running'
    handoff(gateway)
    result = cli(store, task, cancel, gateway)
    assert result['reason'] == 'context_handoff' and not result['failed']
    finished = [event['data'] for event in store.events(task['id'])
                if event['kind'] == 'command_finished']
    assert len(finished) == 1
    assert finished[0]['status'] == 'completed' and finished[0]['exit_code'] == 0
    assert 'export check finished' in finished[0]['output']
    assert finished[0]['process_id'] == command['process_id']
    assert not gateway.commands.has_running_foreground()


def test_user_cancel_interrupts_finite_command_drain(session, monkeypatch):
    store, task, cancel, gateway = session
    gateway.commands.call('exec', {'command': python_command('import time; time.sleep(30)'),
                                  'timeout_ms': 7000, 'yield_ms': 0})
    draining = threading.Event()
    original = gateway.commands.has_running_foreground

    def observe():
        running = original()
        if running:
            draining.set()
        return running

    monkeypatch.setattr(gateway.commands, 'has_running_foreground', observe)
    canceller = threading.Thread(target=lambda: (draining.wait(3), cancel.set()), daemon=True)
    canceller.start()
    handoff(gateway)
    began = time.monotonic()
    result = cli(store, task, cancel, gateway)
    canceller.join(timeout=1)
    assert draining.is_set()
    assert result['reason'] == 'cancelled'
    assert time.monotonic() - began < 3
    assert store.get(task['id'])['pid'] is None


def test_background_service_does_not_delay_handoff(session):
    store, task, cancel, gateway = session
    command = gateway.commands.call('exec', {
        'command': python_command('import time; time.sleep(30)'),
        'background': True, 'timeout_ms': 7000, 'yield_ms': 0})
    handoff(gateway)
    began = time.monotonic()
    result = cli(store, task, cancel, gateway)
    assert result['reason'] == 'context_handoff'
    assert time.monotonic() - began < 3
    current = gateway.commands.runner.poll({'process_id': command['process_id'], 'yield_ms': 0})
    assert current['status'] == 'running' and current['background'] is True
    assert not gateway.commands.has_running_foreground()


def test_command_drain_cannot_extend_original_task_iteration_budget(session):
    store, task, cancel, gateway = session
    task = dict(task, timeout_seconds=.6)
    gateway.commands.runner.task = task
    command = gateway.commands.call('exec', {
        'command': python_command('import time; time.sleep(30)'),
        'timeout_ms': 60000, 'yield_ms': 0})
    assert command['timeout_ms'] <= 600
    handoff(gateway)
    began = time.monotonic()
    result = cli(store, task, cancel, gateway)
    assert result['reason'] == 'context_handoff'
    assert time.monotonic() - began < 3
    failures = result['tool_failures']
    assert failures and failures[0]['status'] == 'timed_out'
    assert any(event['kind'] == 'command_finished' and event['level'] == 'warning'
               for event in store.events(task['id']))


def test_queued_write_completion_survives_handoff_and_enters_next_memory(session, monkeypatch):
    store, task, cancel, gateway = session
    path = Path(task['workspace']) / 'export.py'
    ready = Path(task['workspace']) / 'write-ready'
    content = 'def export():\n    return "csv"\n'
    arguments = {'filePath': str(path), 'content': content}
    gateway.trace.start('write-export', 'write', arguments)
    # The next model request saw output before OpenCode emitted terminal metadata.
    gateway.trace.finish('write-export', 'File written', inferred=True)
    payload = {'type': 'tool_use', 'part': {'callID': 'write-export', 'tool': 'write',
               'state': {'status': 'completed', 'input': arguments, 'output': 'File written'}}}
    source = (f'import json,time; from pathlib import Path; Path({str(path)!r}).write_text({content!r}); '
              f'print(json.dumps({payload!r}),flush=True); Path({str(ready)!r}).write_text("ready"); '
              'time.sleep(30)')
    from agentvisor.execution import spawn

    def started_with_queued_event(*args, **kwargs):
        process = spawn(*args, **kwargs)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        return process

    monkeypatch.setattr('agentvisor.execution.spawn', started_with_queued_event)
    handoff(gateway)
    result = cli(store, task, cancel, gateway, source)
    assert result['reason'] == 'context_handoff'
    records = [event['data'] for event in store.events(task['id'])
               if event['kind'] == 'tool_finished' and event['data']['call_id'] == 'write-export']
    assert records[-1]['inferred'] is False and records[-1]['status'] == 'completed'
    remembered = remember_iteration(store, store.get(task['id']), result)
    changed = remembered['task_memory']['changed_files']
    assert len(changed) == 1 and changed[0]['path'] == 'export.py'
    assert changed[0]['exists'] is True and changed[0]['sha256']
