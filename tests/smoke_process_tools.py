"""Opt-in installed-OpenCode/MCP smoke; uses a fake model, no LM Studio or user tasks."""
import json
import os
import shlex
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentvisor.execution import agent_environment
from agentvisor.command_policy import resolve_command_policy
from agentvisor.inference import InferenceGateway
from agentvisor.processes import capture, executable
from agentvisor.store import Store
from agentvisor.tasks import NewTask, Profile, prepare_documents


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(content_text(part) for part in content)
    if isinstance(content, dict):
        return content_text(content.get('text', content.get('content', '')))
    return ''


def python_command(script):
    if os.name == 'nt':
        return "& '" + sys.executable.replace("'", "''") + "' '" + script + "'"
    return shlex.quote(sys.executable) + ' ' + shlex.quote(script)


class Scenario:
    def __init__(self):
        self.requests, self.errors, self.results = [], [], []
        self.expected = None
        self.phase = 'finite_start'
        self.process = self.server_process = None
        self.names = {}
        self.bash_enabled = False

    def tool(self, name, arguments):
        self.expected = f'call_wire_{len(self.requests)}'
        return {'role': 'assistant', 'tool_calls': [{
            'index': 0, 'id': self.expected, 'type': 'function',
            'function': {'name': self.names[name], 'arguments': json.dumps(arguments)}}]}, 'tool_calls'

    def next(self, body):
        self.requests.append(body)
        assert len(self.requests) <= 30, 'Tool scenario exceeded its request bound'
        names = [tool['function']['name'] for tool in body.get('tools', [])]
        if not names:
            return {'role': 'assistant', 'content': 'Process tool verification'}, 'stop'
        for suffix in ('exec', 'poll', 'stop'):
            self.names[suffix] = next((name for name in names if name.endswith('_' + suffix)
                                     and 'agentvisor_process' in name), None)
            assert self.names[suffix], f'Missing MCP tool {suffix}; received {names}'
        self.bash_enabled = self.bash_enabled or 'bash' in names
        result = None
        if self.expected:
            message = next((message for message in reversed(body['messages'])
                            if message.get('role') == 'tool' and
                            message.get('tool_call_id') == self.expected), None)
            assert message, f'Missing result for {self.expected}'
            text = content_text(message['content'])
            result = json.loads(text)
            assert 'status' in result and 'process_id' in result, text
            self.results.append(result)
        if self.phase == 'finite_start':
            self.phase = 'finite_poll'
            command = ("$wireValue='visor-$-literal'; Write-Output $wireValue" if os.name == 'nt'
                       else "wire_value='visor-$-literal'; printf '%s\\n' \"$wire_value\"")
            return self.tool('exec', {'command': command, 'timeout_ms': 10000, 'yield_ms': 0})
        if self.phase == 'finite_poll':
            if result['status'] == 'running':
                return self.tool('poll', {'process_id': result['process_id'], 'yield_ms': 1000})
            assert result['status'] == 'completed' and result['exit_code'] == 0, result
            assert 'visor-$-literal' in result['output'], result
            self.phase = 'server_poll'
            return self.tool('exec', {'command': python_command('wire_server.py'),
                                     'background': True, 'timeout_ms': 30000, 'yield_ms': 0})
        if self.phase == 'server_poll':
            self.server_process = result['process_id']
            assert result['status'] == 'running', result
            if 'WIRE_READY' not in result['output']:
                return self.tool('poll', {'process_id': self.server_process, 'yield_ms': 1000})
            self.phase = 'probe_poll'
            return self.tool('exec', {'command': python_command('wire_probe.py'),
                                     'timeout_ms': 10000, 'yield_ms': 0})
        if self.phase == 'probe_poll':
            if result['status'] == 'running':
                return self.tool('poll', {'process_id': result['process_id'], 'yield_ms': 1000})
            assert result['status'] == 'completed' and result['exit_code'] == 0, result
            assert 'WIRE_HTTP_OK' in result['output'], result
            self.phase = 'server_stop'
            return self.tool('stop', {'process_id': self.server_process})
        assert self.phase == 'server_stop' and result['status'] == 'stopped', result
        self.phase = 'done'
        self.expected = None
        return {'role': 'assistant', 'content': 'PROCESS_TOOLS_WIRE_PASSED'}, 'stop'


def main():
    scenario = Scenario()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                delta, finish = scenario.next(body)
            except Exception as error:
                scenario.errors.append(str(error))
                delta, finish = {'role': 'assistant', 'content': 'WIRE_FAILURE: ' + str(error)}, 'stop'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for fragment, reason in ((delta, None), ({}, finish)):
                chunk = {'id': 'process-wire', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'wire', 'choices': [{'index': 0, 'delta': fragment, 'finish_reason': reason}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='agentvisor-process-wire-') as directory:
            root = Path(directory)
            (root / 'wire_server.py').write_text(
                'from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler\n'
                'from pathlib import Path\n'
                'server = ThreadingHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)\n'
                'Path("wire_port.txt").write_text(str(server.server_port))\n'
                'print("WIRE_READY", flush=True)\nserver.serve_forever()\n', encoding='utf-8')
            (root / 'wire_probe.py').write_text(
                'from pathlib import Path\nfrom urllib.request import urlopen\n'
                'port = int(Path("wire_port.txt").read_text())\n'
                'with urlopen(f"http://127.0.0.1:{port}/wire_port.txt", timeout=3) as response:\n'
                '    assert response.status == 200\n'
                'print("WIRE_HTTP_OK", flush=True)\n', encoding='utf-8')
            profile = Profile(model='wire', base_url=f'http://127.0.0.1:{server.server_port}',
                              profile_mode='manual', context=32768, output_limit=2048)
            store = Store(root / 'data')
            task = store.create(NewTask(name='Process wire check', workspace=directory,
                goal='Verify owned command execution, HTTP server probe and shutdown',
                profile=profile, timeout_seconds=90).model_dump())
            task = dict(task, command_policy=resolve_command_policy(task))
            assert task['command_policy']['allowed'], task['command_policy']['reason']
            with InferenceGateway(store, task, profile.model_dump(), threading.Event()) as gateway:
                prepare_documents(task, {'instance': 'wire', 'context': profile.context,
                    'api_base_url': gateway.base_url, 'command_mcp_url': gateway.mcp_url})
                output = capture([executable('opencode'), 'run', '--format', 'json', '--dir', directory,
                                  '--auto', '--model', 'agentvisor/wire',
                                  'Use the process tools to verify raw shell variables and an HTTP server.'],
                                 env=agent_environment(task), timeout=90, include_stderr=True)
                assert not scenario.errors, '\n'.join(scenario.errors) + '\n' + output[-5000:]
                assert scenario.phase == 'done' and 'PROCESS_TOOLS_WIRE_PASSED' in output, output[-6000:]
                assert all(entry['process'].poll() is not None
                           for entry in gateway.commands.runner.sessions.values())
                assert not scenario.bash_enabled, 'Process tools worked, but inherited-pipe Bash tool is still enabled'
            print(json.dumps({'passed': True, 'model_requests': len(scenario.requests),
                              'tool_results': len(scenario.results), 'tools': scenario.names,
                              'checked': ['raw shell variables', 'command polling', 'background HTTP server',
                                          'HTTP probe', 'owned server stop', 'automatic permissions']}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
