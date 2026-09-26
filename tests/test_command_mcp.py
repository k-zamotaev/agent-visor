import json
import os
import time
from pathlib import Path

import httpx
import pytest

from agentvisor.command_mcp import CommandMCP
from agentvisor.inference import InferenceGateway
from agentvisor.tasks import prepare_documents, read_document
from agentvisor.tool_trace import fingerprint
from test_supervisor import make as make_task


def make(*args, **kwargs):
    kwargs.setdefault('command_policy', {'allowed': True, 'permission': 'allow',
                                        'denied_patterns': [], 'external_directory': 'allow'})
    return make_task(*args, **kwargs)


def finish_command(commands, result):
    deadline = time.monotonic() + 8
    while result['status'] == 'running' and time.monotonic() < deadline:
        result = commands.call('poll', {'process_id': result['process_id'], 'yield_ms': 1000})
    assert result['status'] != 'running'
    return result


def test_failed_command_memory_survives_session_and_requires_repair(tmp_path):
    store, engine, task = make(tmp_path, timeout_seconds=60)
    command = {'command': 'echo failure-evidence; exit 7', 'yield_ms': 1000}
    for _ in range(2):
        commands = CommandMCP(store, store.get(task['id']), engine.cancel)
        try:
            result = finish_command(commands, commands.call('exec', command))
            assert result['exit_code'] == 7
            commands.call('poll', {'process_id': result['process_id']})
        finally:
            commands.close()
    commands = CommandMCP(store, store.get(task['id']), engine.cancel)
    try:
        with pytest.raises(ValueError, match='already failed twice'):
            commands.call('exec', command)
        assert len(commands.runner.sessions) == 0
        prompt = prepare_documents(store.get(task['id']), {'instance': 'fake', 'context': 16384})
        assert 'failure-evidence' in prompt and 'FAILED COMMAND MEMORY' in prompt
        result = finish_command(commands, commands.call('exec', dict(command, repair_note='Repaired dependency; rechecking')))
        assert result['exit_code'] == 7
        assert commands.memory()[0]['attempts'] == 3
    finally:
        commands.close()


def test_duplicate_running_command_returns_existing_process(tmp_path):
    store, engine, task = make(tmp_path)
    commands = CommandMCP(store, task, engine.cancel)
    try:
        payload = {'command': 'Start-Sleep 10' if os.name == 'nt' else 'sleep 10', 'yield_ms': 0}
        first = commands.call('exec', payload)
        second = commands.call('exec', payload)
        assert second['reused'] and first['process_id'] == second['process_id']
        assert len(commands.runner.sessions) == 1
        commands.call('stop', {'process_id': first['process_id']})
        assert commands.memory() == []
    finally:
        commands.close()


def test_verified_repair_clears_stale_command_failure(tmp_path):
    store, engine, task = make(tmp_path, timeout_seconds=60)
    commands = CommandMCP(store, task, engine.cancel)
    code = 'if (-not (Test-Path repaired.txt)) { exit 3 }; Write-Output ok' if os.name == 'nt' else 'test -f repaired.txt'
    try:
        for _ in range(2):
            assert finish_command(commands, commands.call('exec', {'command': code}))['exit_code']
        (Path(task['workspace']) / 'repaired.txt').write_text('done')
        assert finish_command(commands, commands.call('exec', {'command': code, 'repair_note': 'Created required input'}))['exit_code'] == 0
        assert commands.memory() == []
        assert finish_command(commands, commands.call('exec', {'command': code}))['exit_code'] == 0
    finally:
        commands.close()


def test_unpolled_timeout_is_saved_for_next_attempt(tmp_path):
    store, engine, task = make(tmp_path)
    commands = CommandMCP(store, task, engine.cancel)
    try:
        payload = {'command': 'Start-Sleep 10' if os.name == 'nt' else 'sleep 10',
                   'yield_ms': 0, 'timeout_ms': 300}
        commands.call('exec', payload)
        time.sleep(.7)
        failures = commands.snapshot()['tool_failures']
        assert len(failures) == 1 and failures[0]['status'] == 'timed_out'
        assert failures[0]['input']['command'] == payload['command']
        assert commands.memory()[0]['attempts'] == 1
    finally:
        commands.close()


def test_new_goal_does_not_inherit_command_blacklist(tmp_path):
    store, engine, task = make(tmp_path)
    store.update(task['id'], command_failures={'goal_version': 1, 'items': [{'attempts': 9}]}, goal_version=2)
    commands = CommandMCP(store, store.get(task['id']), engine.cancel)
    try:
        assert commands.memory() == []
    finally:
        commands.close()


def test_loopback_mcp_transport_and_config(tmp_path):
    store, engine, task = make(tmp_path)
    with InferenceGateway(store, task, task['profile'], engine.cancel) as gateway:
        with httpx.Client(trust_env=False, timeout=5) as client:
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
            assert client.post(gateway.mcp_url, json=request, headers={'Origin': 'https://example.org'}).status_code == 403
            assert client.post(gateway.mcp_url.replace('/v1/mcp', '/wrong/mcp'), json=request).status_code == 404
            assert client.get(gateway.mcp_url).status_code == 405
            response = client.post(gateway.mcp_url, json=request).json()
            assert response['result']['protocolVersion'] == '2025-03-26'
            assert client.post(gateway.mcp_url, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}).status_code == 202
            listing = client.post(gateway.mcp_url, json=dict(request, method='tools/list')).json()
            assert {tool['name'] for tool in listing['result']['tools']} == {'exec', 'poll', 'stop'}
            unknown = dict(request, method='tools/call', params={'name': 'stop', 'arguments': {'process_id': '1234'}})
            assert client.post(gateway.mcp_url, json=unknown).json()['result']['isError']
        prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384, 'command_mcp_url': gateway.mcp_url})
        config = json.loads(read_document(task, 'opencode.json'))
        assert config['permission'] == {'bash': 'deny', 'agentvisor_process_exec': 'allow'}
        assert config['mcp']['agentvisor_process']['url'] == gateway.mcp_url
        assert 'agentvisor_process_exec' in prompt and 'background=true' in prompt
        assert 'Start-Process -WindowStyle Hidden' not in prompt


def test_equivalent_relative_and_absolute_cwd_reuses_running_process(tmp_path):
    store, engine, task = make(tmp_path)
    working = Path(task['workspace']) / 'backend'
    working.mkdir()
    commands = CommandMCP(store, task, engine.cancel)
    try:
        payload = {'command': 'Start-Sleep 10' if os.name == 'nt' else 'sleep 10',
                   'yield_ms': 0, 'cwd': 'backend'}
        first = commands.call('exec', payload)
        second = commands.call('exec', dict(payload, cwd=str(working.resolve())))
        assert second['reused'] and second['process_id'] == first['process_id']
        assert len(commands.runner.sessions) == 1
        assert commands.commands[first['process_id']]['cwd'] == str(working.resolve())
    finally:
        commands.close()


def test_same_failed_repair_note_does_not_authorize_another_identical_attempt(tmp_path):
    store, engine, task = make(tmp_path, timeout_seconds=60)
    commands = CommandMCP(store, task, engine.cancel)
    payload = {'command': 'echo repeat-evidence; exit 7', 'yield_ms': 1000}
    try:
        for _ in range(2):
            assert finish_command(commands, commands.call('exec', payload))['exit_code'] == 7
        note = 'Reinstalled the missing dependency'
        repaired = commands.call('exec', dict(payload, repair_note=note))
        assert finish_command(commands, repaired)['exit_code'] == 7
        with pytest.raises(ValueError, match='already failed twice'):
            commands.call('exec', dict(payload, repair_note='  ' + note + '  '))
        assert len(commands.runner.sessions) == 3
        assert commands.memory()[0]['attempts'] == 3
        assert commands.memory()[0]['repair_note'] == note
    finally:
        commands.close()


def test_gateway_cleanup_survives_failure_while_recording_final_snapshot(tmp_path, monkeypatch):
    store, engine, task = make(tmp_path)
    gateway = InferenceGateway(store, task, task['profile'], engine.cancel)
    process = None

    def broken_snapshot():
        raise RuntimeError('Diagnostic store unavailable')

    try:
        with pytest.raises(RuntimeError, match='Diagnostic store unavailable'):
            with gateway:
                result = gateway.commands.call('exec', {
                    'command': 'Start-Sleep 10' if os.name == 'nt' else 'sleep 10', 'yield_ms': 0})
                process = gateway.commands.runner.sessions[result['process_id']]['process']
                assert process.poll() is None
                monkeypatch.setattr(gateway.commands, 'snapshot', broken_snapshot)
        assert process.poll() is not None
        assert not gateway.commands.runner.monitor.is_alive()
        assert not gateway.thread.is_alive()
        assert gateway.server.fileno() == -1
    finally:
        gateway.commands.runner.close()
        gateway.close_transport()


def test_failed_command_memory_bounds_scripts_but_keeps_exact_fingerprint(tmp_path):
    store, engine, task = make(tmp_path)
    commands = CommandMCP(store, task, engine.cancel)
    try:
        for index in range(12):
            command = {'command': 'large-script-' + 'x' * 10000 + str(index),
                       'shell': 'powershell', 'cwd': str(tmp_path)}
            signature = fingerprint('exec', command)
            commands.commands[str(index)] = dict(command, fingerprint=signature,
                                                 repair_note='repair-' + 'r' * 2000)
            commands.record({'process_id': str(index), 'status': 'completed',
                             'exit_code': 7, 'output': 'diagnostic ' + 'o' * 10000})
        memory = commands.memory()
        assert len(memory) == 8
        assert all(len(item['command']) <= 1500 and len(item['repair_note']) <= 500 and
                   len(item['output']) <= 1000 for item in memory)
        assert memory[-1]['fingerprint'] == signature
        assert len(json.dumps(memory)) < 30000
        prompt = prepare_documents(store.get(task['id']), {'instance': 'fake', 'context': 16384})
        assert 'FAILED COMMAND MEMORY' in prompt and len(prompt) < 35000
    finally:
        commands.close()
