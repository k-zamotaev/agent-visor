"""Opt-in wire check against the installed OpenCode, with a local fake model API."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading

from agentvisor.execution import agent_environment
from agentvisor.processes import capture, executable
from agentvisor.store import Store
from agentvisor.tasks import NewTask, Profile, prepare_documents


def main():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for delta, reason in [({'role': 'assistant', 'content': 'AgentVisor ready'}, None), ({}, 'stop')]:
                chunk = {'id': 'wire-check', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'wire', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='agentvisor-wire-') as directory:
            root = Path(directory)
            profile = Profile(model='wire', base_url=f'http://127.0.0.1:{server.server_port}',
                              profile_mode='manual', context=32768, output_limit=2048,
                              temperature=0.8, top_p=0.93, top_k=30, reasoning='medium')
            task = Store(root / 'data').create(NewTask(name='Wire check', workspace=directory,
                          goal='Say ready without using tools', profile=profile).model_dump())
            prepare_documents(task, {'instance': 'wire', 'context': profile.context})
            output = capture([executable('opencode'), 'run', '--format', 'json', '--dir', directory,
                              '--model', 'agentvisor/wire', 'Say ready without using tools.'],
                             env=agent_environment(task), timeout=90, include_stderr=True)
            assert requests, output[-4000:]
            body = next(request for request in requests if request.get('tools'))
            fields = {key: body.get(key) for key in ('temperature', 'top_p', 'top_k', 'reasoning_effort', 'max_tokens')}
            print(json.dumps(fields))
            assert fields == {'temperature': 0.8, 'top_p': 0.93, 'top_k': 30,
                              'reasoning_effort': 'medium', 'max_tokens': 2048}, fields
            assert 'AgentVisor ready' in output, output[-4000:]
            print('OpenCode inference parameters reached the model API.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
