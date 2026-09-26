import json
import sys
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agentvisor.execution import execute
from agentvisor.inference import InferenceGateway, StreamMetrics
from agentvisor.tool_trace import ToolTrace


class RecordingStore:
    def __init__(self):
        self.events = []
        self.updates = []

    def event(self, task_id, kind, message, level='info', data=None):
        self.events.append({'kind': kind, 'message': message, 'level': level,
                            'data': deepcopy(data)})

    def update(self, task_id, **fields):
        self.updates.append(fields)


def trace_for(**task_options):
    store = RecordingStore()
    task = {'id': 'test', 'idle_timeout_seconds': 300, **task_options}
    return ToolTrace(store, task), store


def test_split_tool_names_and_arguments_reconstruct_before_start():
    metrics = StreamMetrics()
    metrics.accept({'choices': [{'index': 0, 'delta': {'tool_calls': [
        {'index': 0, 'id': 'call-', 'function': {'name': 'ba', 'arguments': '{"command":"Write-'}}]}}]}, 1)
    metrics.accept({'choices': [{'index': 0, 'delta': {'tool_calls': [
        {'index': 0, 'id': '1', 'function': {'name': 'sh', 'arguments': 'Output 1","timeout":1000}'}}]}}]}, 2)
    trace, store = trace_for()
    gateway = SimpleNamespace(trace=trace)
    InferenceGateway.publish_tools(gateway, metrics)
    InferenceGateway.publish_tools(gateway, metrics)
    assert len(store.events) == 1
    pending = trace.snapshot()['pending_tools']
    assert len(pending) == 1
    assert pending[0]['call_id'] == 'call-1'
    assert pending[0]['tool'] == 'bash'
    assert pending[0]['input'] == {'command': 'Write-Output 1', 'timeout': 1000}


def test_non_stream_parallel_tool_calls_without_indices_stay_distinct():
    metrics = StreamMetrics()
    metrics.accept({'choices': [{'message': {'tool_calls': [
        {'id': 'read-1', 'function': {'name': 'read', 'arguments': '{"path":"first.txt"}'}},
        {'id': 'read-2', 'function': {'name': 'read', 'arguments': '{"path":"second.txt"}'}},
    ]}}]}, 1)
    trace, _ = trace_for()
    InferenceGateway.publish_tools(SimpleNamespace(trace=trace), metrics)
    pending = trace.snapshot()['pending_tools']
    assert [(item['call_id'], item['input']) for item in pending] == [
        ('read-1', {'path': 'first.txt'}), ('read-2', {'path': 'second.txt'})]


def test_completed_tool_message_clears_only_matching_pending_call():
    trace, _ = trace_for()
    trace.start('old', 'write', {'path': 'done.txt'})
    trace.start('pending', 'bash', {'command': 'npm run dev'})
    trace.observe_messages([
        {'role': 'tool', 'tool_call_id': 'unrelated', 'content': 'unrelated history'},
        {'role': 'tool', 'tool_call_id': 'old', 'content': 'File written'},
    ])
    pending = trace.snapshot()['pending_tools']
    assert len(pending) == 1 and pending[0]['call_id'] == 'pending'
    assert pending[0]['input']['command'] == 'npm run dev'
    assert not trace.snapshot()['tool_failures']


@pytest.mark.parametrize('status,metadata,error,expected', [
    ('completed', {'exit': 1}, '', 'Command exited with code 1'),
    ('error', {}, 'PowerShell parser error', 'PowerShell parser error'),
])
def test_cli_error_metadata_and_output_survive_completed_tool_state(status, metadata, error, expected):
    trace, store = trace_for()
    trace.start('cmd', 'bash', {'command': 'broken command'})
    trace.observe_event({'callID': 'cmd', 'tool': 'bash', 'state': {
        'status': status, 'input': {'command': 'broken command'}, 'metadata': metadata,
        'error': error, 'output': 'Unexpected token at line 1'}})
    result = trace.snapshot()
    assert result['pending_tools'] == []
    failure = result['tool_failures'][0]
    assert failure['input']['command'] == 'broken command'
    assert failure['error'] == expected
    assert failure['output'] == 'Unexpected token at line 1'
    assert store.events[-1]['level'] == 'warning'


def test_cli_failure_can_enrich_result_observed_first_in_model_request():
    trace, _ = trace_for()
    trace.start('cmd', 'bash', {'command': 'npm test'})
    trace.observe_messages([{'role': 'tool', 'tool_call_id': 'cmd', 'content': 'Tests failed'}])
    trace.observe_event({'callID': 'cmd', 'tool': 'bash', 'state': {
        'status': 'completed', 'metadata': {'exit': 2}, 'output': 'Tests failed'}})
    failures = trace.snapshot()['tool_failures']
    assert len(failures) == 1
    assert failures[0]['error'] == 'Command exited with code 2'
    assert failures[0]['input'] == {'command': 'npm test'}


def test_deadline_reports_pending_command_instead_of_previous_write(monkeypatch):
    trace, _ = trace_for(idle_timeout_seconds=30)
    monkeypatch.setattr('agentvisor.tool_trace.time.time', lambda: 100)
    trace.start('done', 'write', {'path': 'previous.md'})
    trace.finish('done', 'File written')
    trace.start('current', 'bash', {'command': 'npm run dev', 'timeout': 1000})
    monkeypatch.setattr('agentvisor.tool_trace.time.time', lambda: 110)
    assert trace.problem() is None
    monkeypatch.setattr('agentvisor.tool_trace.time.time', lambda: 112)
    problem = trace.problem()
    assert 'npm run dev' in problem and '11s' in problem
    assert 'previous.md' not in problem


def test_tool_deadline_is_enforced_even_while_model_activity_continues(tmp_path):
    trace, store = trace_for(workspace=str(tmp_path), timeout_seconds=5,
                             max_hours=1, idle_timeout_seconds=0.25)
    trace.start('stuck', 'bash', {'command': 'never returns'})
    inference = SimpleNamespace(trace=trace, reasoning_seen=False, activity=time.monotonic)
    result = execute(store, trace.task, [sys.executable, '-c', 'import time; time.sleep(10)'],
                     threading.Event(), inference=inference)
    assert result['failed'] and result['reason'] == 'tool_timeout'
    assert result['duration'] < 2
    assert result['pending_tools'][0]['input']['command'] == 'never returns'
    assert 'never returns' in result['error_detail']


def test_snapshot_bounds_large_arguments_and_failed_output(monkeypatch):
    trace, _ = trace_for()
    monkeypatch.setattr('agentvisor.tool_trace.time.time', lambda: 100)
    for index in range(12):
        trace.start(f'pending-{index}', 'write', {'path': f'{index}.txt', 'content': 'x' * 100000})
        trace.start(f'failed-{index}', 'bash', {'command': 'z' * 100000})
        trace.finish(f'failed-{index}', output='o' * 100000, error='e' * 100000)
    snapshot = trace.snapshot()
    assert len(snapshot['pending_tools']) == len(snapshot['tool_failures']) == 8
    assert len(json.dumps(snapshot)) < 180000
    assert all(len(entry['output']) <= 4000 and len(entry['error']) <= 2000
               for entry in snapshot['tool_failures'])
    monkeypatch.setattr('agentvisor.tool_trace.time.time', lambda: 1000)
    problem = trace.problem()
    assert problem and 'write' in problem and len(problem) <= 1700
