"""Per-session loopback MCP tools backed by supervisor-owned processes."""
import json
import os
import threading
from pathlib import Path

from .command_sessions import CommandSessions
from .command_policy import check_command_policy
from .tool_trace import fingerprint


def schemas():
    process = {'process_id': {'type': 'string'}, 'yield_ms': {'type': 'integer', 'minimum': 0, 'maximum': 1000}}
    return [
        {'name': 'exec', 'description':
         'Execute a command with a native shell and independent deadline. Returns within 1 second. '
         'If status=running use poll with process_id; do not start duplicates. '
         'On Windows default shell is PowerShell: pass raw PowerShell, never wrap it in bash. '
         'Start servers directly with background=true (no nohup, &, or Start-Process). '
         'All owned processes stop when this agent session ends.',
         'inputSchema': {'type': 'object', 'properties': {
             'command': {'type': 'string'}, 'shell': {'type': 'string', 'enum': ['powershell', 'bash']},
             'cwd': {'type': 'string'}, 'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 21600000},
             'yield_ms': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
             'background': {'type': 'boolean'},
             'repair_note': {'type': 'string', 'description': 'What changed since the same command failed twice; inspect evidence before retrying.'}},
                         'required': ['command'], 'additionalProperties': False}},
        {'name': 'poll', 'description': 'Read the state and bounded output of an owned command. Running is not success. '
         'Use other tools while a server is running; poll a finite command until completed.',
         'inputSchema': {'type': 'object', 'properties': process, 'required': ['process_id'], 'additionalProperties': False}},
        {'name': 'stop', 'description': 'Stop only the process tree identified by an exec process_id.',
         'inputSchema': {'type': 'object', 'properties': {'process_id': {'type': 'string'}},
                         'required': ['process_id'], 'additionalProperties': False}},
    ]


class CommandMCP:
    def __init__(self, store, task, cancel):
        self.store, self.task, self.cancel = store, task, cancel
        self.runner = CommandSessions(task, cancel)
        self.policy = task.get('command_policy') or {
            'allowed': False, 'permission': 'deny', 'reason': 'Command permissions have not been verified'}
        self.lock = threading.RLock()
        self.commands, self.recorded = {}, set()

    def close(self):
        try:
            self.snapshot()
        finally:
            self.runner.close()

    def snapshot(self):
        with self.lock:
            results = self.runner.snapshots()
            for result in results:
                self.record(result)
            return {'tool_failures': [dict(result, tool='agentvisor_process_exec',
                    input=self.commands.get(result['process_id'], {})) for result in results
                    if result['status'] not in {'stopped', 'cancelled'} and (
                        result['status'] in {'timed_out', 'output_limit', 'failed'} or result.get('exit_code'))][-8:]}

    def memory(self):
        memory = self.store.get(self.task['id']).get('command_failures') or {}
        return memory.get('items', []) if memory.get('goal_version') == self.task['goal_version'] else []

    def record(self, result):
        key = result.get('process_id')
        if key not in self.commands or key in self.recorded or result.get('status') == 'running':
            return
        self.recorded.add(key)
        command = self.commands[key]
        failed = result.get('status') not in {'stopped', 'cancelled'} and (
            result.get('status') in {'timed_out', 'output_limit', 'failed'} or bool(result.get('exit_code')))
        self.store.event(self.task['id'], 'command_finished', command['command'],
                         'warning' if failed else 'info', data=dict(result, input={
                             'command': command['command'][:1500], 'cwd': command['cwd'][:500],
                             'command_hash': fingerprint('command', command['command']),
                             'fingerprint': command['fingerprint']}))
        if not failed or self.cancel.is_set():
            if result['status'] == 'completed' and result.get('exit_code') == 0:
                items = self.memory()
                remaining = [item for item in items if item['fingerprint'] != command['fingerprint']]
                if len(remaining) != len(items):
                    self.store.update(self.task['id'], command_failures={
                        'goal_version': self.task['goal_version'], 'items': remaining})
            return
        items = self.memory()
        previous = next((item for item in items if item['fingerprint'] == command['fingerprint']), {})
        entry = dict(command, command=str(command['command'])[:1500], cwd=command['cwd'][:500],
                     repair_note=command.get('repair_note', '')[:500], attempts=previous.get('attempts', 0) + 1,
                     status=result['status'], exit_code=result.get('exit_code'), output=result.get('output', '')[-1000:])
        items = [item for item in items if item['fingerprint'] != entry['fingerprint']][-7:] + [entry]
        self.store.update(self.task['id'], command_failures={'goal_version': self.task['goal_version'], 'items': items})

    def call(self, name, arguments):
        if self.cancel.is_set():
            raise ValueError('Task cancelled')
        with self.lock:
            if name == 'exec':
                cwd = Path(arguments.get('cwd') or self.task['workspace'])
                if not cwd.is_absolute():
                    cwd = Path(self.task['workspace']) / cwd
                command = {'command': arguments.get('command'),
                           'shell': arguments.get('shell') or ('powershell' if os.name == 'nt' else 'bash'),
                           'cwd': str(cwd.resolve())}
                permission = check_command_policy(self.policy, command['command'], command['shell'],
                                                  command['cwd'], self.task['workspace'], self.cancel)
                if not permission['allowed']:
                    raise ValueError(permission['reason'])
                signature = fingerprint('exec', command)
                for existing in self.runner.snapshots():
                    self.record(existing)
                    if (existing['status'] == 'running' and
                            self.commands.get(existing['process_id'], {}).get('fingerprint') == signature):
                        return dict(existing, reused=True)
                previous = next((item for item in self.memory() if item['fingerprint'] == signature), None)
                repair_note = str(arguments.get('repair_note', '')).strip()[:500]
                if previous and previous['attempts'] >= 2 and (
                        not repair_note or repair_note == previous.get('repair_note')):
                    raise ValueError('This command already failed twice. Inspect its error, change the approach, '
                                     'or supply repair_note describing the concrete repair before retrying. '
                                     + json.dumps(previous, ensure_ascii=False))
                result = self.runner.start(arguments)
                self.commands[result['process_id']] = dict(command, fingerprint=signature,
                                                          repair_note=repair_note)
                self.store.event(self.task['id'], 'command_started', str(arguments.get('command', '')),
                                 data=dict(result, input=dict(command, command=str(command['command'])[:4000])))
            elif name == 'poll':
                result = self.runner.poll(arguments)
            elif name == 'stop':
                result = self.runner.stop(arguments)
            else:
                raise ValueError('Unknown tool')
            self.record(result)
            return result

    def dispatch(self, body):
        if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
        if 'id' not in body:
            return None
        method, params = body.get('method'), body.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'AgentVisor process supervisor', 'version': '1.0'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': schemas()}
        elif method == 'tools/call':
            try:
                data = self.call(params.get('name'), params.get('arguments') or {})
                failed = data.get('status') not in {'stopped', 'cancelled'} and (
                    data.get('status') in {'timed_out', 'output_limit', 'failed'} or bool(data.get('exit_code')))
                result = {'isError': failed, 'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
            except (ValueError, OSError, TypeError, InterruptedError) as error:
                result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)[:6000]}]}
        else:
            return {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
