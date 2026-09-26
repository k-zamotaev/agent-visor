"""Opt-in real OpenCode milestone review through MCP; no real model or user tasks."""
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentvisor.command_policy import resolve_command_policy
from agentvisor.step_acceptance import pending_steps
from agentvisor.store import Store
from agentvisor.supervisor import Supervisor
from agentvisor.tasks import NewTask, Profile, checklist, document_path, prepare_documents, write_document
from tests.smoke_process_tools import content_text, python_command


class Runtime:
    def health(self, *_):
        return None


class ReviewScenario:
    def __init__(self, store, task):
        self.store, self.task = store, task
        self.requests, self.errors, self.tool_results = [], [], []
        self.names = {}
        self.expected = None
        self.phase = 'start'
        self.command = python_command('verify_artifact.py')
        self.process_id = None
        self.report = None

    def call(self, tool, arguments):
        self.expected = f'call_review_{len(self.requests)}'
        return {'role': 'assistant', 'tool_calls': [{
            'index': 0, 'id': self.expected, 'type': 'function', 'function': {
                'name': self.names[tool], 'arguments': json.dumps(arguments)}}]}, 'tool_calls'

    def next(self, body):
        self.requests.append(body)
        assert len(self.requests) <= 20, 'Review exceeded its model request bound'
        names = [tool['function']['name'] for tool in body.get('tools', [])]
        if not names:
            return {'role': 'assistant', 'content': 'Independent review wire check'}, 'stop'
        for suffix in ('exec', 'wait_any'):
            self.names[suffix] = next((name for name in names if name.endswith('_' + suffix)
                                     and 'agentvisor_process' in name), None)
            assert self.names[suffix], f'Missing process tool {suffix}'
        for name in ('read', 'write'):
            assert name in names, f'Missing OpenCode built-in {name} tool'
            self.names[name] = name
        assert 'bash' not in names, 'Original Bash transport is unexpectedly exposed'
        text = ''
        if self.expected:
            message = next((message for message in reversed(body['messages'])
                            if message.get('role') == 'tool' and
                            message.get('tool_call_id') == self.expected), None)
            assert message, f'Missing result for {self.expected}'
            text = content_text(message['content'])
            self.tool_results.append(text)
        if self.phase == 'start':
            context = '\n'.join(content_text(message.get('content', '')) for message in body['messages'])
            assert 'independent milestone reviewer in a NEW session' in context
            assert 'Do not implement the next step' in context
            assert checklist(self.task)[0]['review_status'] == 'pending', 'Claim must remain pending during review'
            self.phase = 'wait_start'
            return self.call('exec', {'command': self.command, 'timeout_ms': 10000, 'yield_ms': 0})
        if self.phase == 'wait_start':
            result = json.loads(text)
            assert result['status'] in {'running', 'completed'}, result
            self.process_id = result['process_id']
            self.phase = 'wait_result'
            return self.call('wait_any', {'process_ids': [self.process_id], 'yield_ms': 1000})
        if self.phase == 'wait_result':
            result = json.loads(text)
            if not result['ready']:
                assert result['running'] == [self.process_id], result
                return self.call('wait_any', {'process_ids': [self.process_id], 'yield_ms': 1000})
            verified = next(item for item in result['ready'] if item['process_id'] == self.process_id)
            assert verified['status'] == 'completed' and verified['exit_code'] == 0, verified
            assert 'ARTIFACT_VERIFIED' in verified['output'], verified
            review = next(event['data'] for event in reversed(self.store.events(self.task['id']))
                          if event['kind'] == 'step_review_started')
            self.report = {'review_id': review['review_id'], 'goal_version': self.task['goal_version'],
                'steps': [{'id': step['id'], 'passed': True,
                           'summary': 'Actual artifact contents verified by a fresh process',
                           'evidence': [{'kind': 'command', 'value': self.command,
                                         'finding': 'Exit 0 and ARTIFACT_VERIFIED'}]}
                          for step in review['steps']]}
            self.phase = 'read_report'
            return self.call('read', {'filePath': str(document_path(self.task, 'STEP_REVIEW.json'))})
        if self.phase == 'read_report':
            self.phase = 'write_report'
            return self.call('write', {'filePath': str(document_path(self.task, 'STEP_REVIEW.json')),
                                      'content': json.dumps(self.report, indent=2)})
        assert self.phase == 'write_report', self.phase
        self.phase, self.expected = 'done', None
        return {'role': 'assistant', 'content': 'STEP_REVIEW_WIRE_PASSED'}, 'stop'


def main():
    scenario = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                delta, finish = scenario.next(body)
            except Exception as error:
                scenario.errors.append(str(error))
                delta, finish = {'role': 'assistant', 'content': 'REVIEW_WIRE_FAILURE: ' + str(error)}, 'stop'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for fragment, reason in ((delta, None), ({}, finish)):
                chunk = {'id': 'review-wire', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'wire', 'choices': [{'index': 0, 'delta': fragment, 'finish_reason': reason}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='agentvisor-review-wire-') as directory:
            root = Path(directory)
            (root / 'artifact.txt').write_text('verified artifact\n', encoding='utf-8')
            (root / 'verify_artifact.py').write_text(
                'from pathlib import Path\nimport time\ntime.sleep(0.2)\n'
                'assert Path("artifact.txt").read_text(encoding="utf-8") == "verified artifact\\n"\n'
                'print("ARTIFACT_VERIFIED", flush=True)\n', encoding='utf-8')
            profile = Profile(model='wire', base_url=f'http://127.0.0.1:{server.server_port}',
                              profile_mode='manual', context=32768, output_limit=2048)
            store = Store(root / 'data')
            task = store.create(NewTask(name='Review wire check', workspace=directory,
                goal='Verify artifact.txt contains the requested exact text', profile=profile,
                timeout_seconds=90, auto_permissions=True, checkpoints=False).model_dump())
            task = store.update(task['id'], iteration=1, applied_goal_version=1,
                                resolved_profile=profile.model_dump())
            original_policy = resolve_command_policy(task)
            assert original_policy['allowed'], original_policy['reason']
            ready = {'instance': 'wire', 'context': profile.context}
            prepare_documents(task, ready)
            write_document(task, 'PROGRESS.md', '- [x] Exact artifact contents\n')
            scenario = ReviewScenario(store, task)
            engine = Supervisor(store, Runtime())
            try:
                engine.review_steps(task, ready, profile.model_dump())
            except Exception as error:
                events = store.events(task['id'])
                diagnostics = [{'kind': event['kind'], 'message': event['message']} for event in events[-8:]]
                raise AssertionError(json.dumps({'errors': scenario.errors, 'events': diagnostics},
                                               ensure_ascii=False)) from error
            current = store.get(task['id'])
            events = store.events(task['id'])
            assert not scenario.errors, scenario.errors
            assert scenario.phase == 'done'
            reviews = current['step_reviews']
            assert reviews['goal_version'] == current['goal_version'] == 1
            accepted = list(reviews['accepted'].values())
            assert len(accepted) == 1 and not pending_steps(current)
            commands = [event for event in events if event['kind'] == 'command_finished'
                        and event['data'].get('status') == 'completed' and event['data'].get('exit_code') == 0]
            assert len(commands) == 1
            assert commands[0]['id'] in accepted[0]['evidence_events']
            began = next(event for event in events if event['kind'] == 'step_review_started')
            passed = next(event for event in events if event['kind'] == 'step_review_accepted')
            assert began['id'] < commands[0]['id'] < passed['id']
            assert checklist(current)[0]['done'] and current['pid'] is None
            assert (root / 'artifact.txt').read_text(encoding='utf-8') == 'verified artifact\n'
            print(json.dumps({'passed': True, 'model_requests': len(scenario.requests),
                'tool_results': len(scenario.tool_results), 'accepted_milestones': len(accepted),
                'successful_command_events': len(commands), 'checked': [
                    'new review session', 'native verification command', 'wait_any',
                    'real report write tool', 'authoritative evidence', 'accepted receipt']}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
