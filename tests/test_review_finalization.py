import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentvisor.inference import InferenceGateway
from agentvisor.review_finalization import budgets, dossier, finalize
from agentvisor.step_acceptance import observed_evidence, validate_review
from agentvisor.tasks import read_document
from test_review_tools import evidence, review_session
from test_step_acceptance import result


@pytest.fixture
def verdict_server():
    state = {'requests': [], 'responses': [], 'delay': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(body)
            time.sleep(state['delay'])
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            arguments = state['responses'].pop(0) if state['responses'] else {}
            parts = [
                {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'report', 'function': {
                    'name': 'agentvisor_process_submit_review',
                    'arguments': json.dumps(arguments)}}]}}]},
                {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}],
                 'usage': {'completion_tokens': 30}},
            ]
            try:
                for part in parts:
                    self.wfile.write(('data: ' + json.dumps(part) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def setup(tmp_path, server):
    store, task, tools = review_session(tmp_path)
    tools.close()
    task = dict(task, review_began=time.monotonic())
    cancel = threading.Event()
    gateway = InferenceGateway(store, task, dict(task['profile'], base_url=server, reasoning=None), cancel)
    return store, task, gateway, cancel


@pytest.mark.parametrize('reason', ['review_handoff', None, 'timeout'])
def test_inspection_handoff_submits_real_report_without_repeating_checks(tmp_path, verdict_server, reason):
    state, url = verdict_server
    store, task, gateway, cancel = setup(tmp_path, url)
    event_id = evidence(store, task)
    failed_id = evidence(store, task, process_id='redis', exit_code=1, output='Redis unavailable')
    state['responses'] = [{'passed': True, 'summary': 'Unit criterion passed',
                           'evidence': [{'event_id': event_id, 'finding': '5 passed'}]}]
    with gateway:
        outcome = finalize(store, task, gateway, 'same-loaded-model', cancel,
                           result(reason=reason, failed=reason == 'timeout'), time.monotonic() + 10)
    assert not outcome['failed'] and outcome['reason'] == 'review_submitted'
    assert len(state['requests']) == 1
    sent = state['requests'][0]
    assert sent['model'] == 'same-loaded-model'
    assert sent['tool_choice'] == 'required'  # LM Studio rejects the object form.
    assert 'reasoning_effort' not in sent  # Legacy profiles can store null.
    assert [t['function']['name'] for t in sent['tools']] == ['agentvisor_process_submit_review']
    content = json.dumps(sent['messages'], ensure_ascii=False)
    assert 'Redis unavailable' in content and str(failed_id) in content
    report = json.loads(read_document(task, 'STEP_REVIEW.json'))
    assert report['review_id'] == task['review_request']['id']
    assert report['steps'][0]['evidence'][0]['event_id'] == event_id
    assert validate_review(task, task['review_request'], observed_evidence(store, task, task['review_cursor']))[1] == ''


def test_finalizer_corrects_forged_evidence_without_auto_accepting(tmp_path, verdict_server):
    state, url = verdict_server
    store, task, gateway, cancel = setup(tmp_path, url)
    state['responses'] = [
        {'passed': True, 'summary': 'Claim', 'evidence': [{'event_id': 9999, 'finding': 'invented'}]},
        {'passed': False, 'summary': 'Required live Redis check is missing', 'evidence': []},
    ]
    with gateway:
        outcome = finalize(store, task, gateway, 'same-model', cancel, result(), time.monotonic() + 10)
    assert not outcome['failed']  # Transport succeeded; acceptance must still reject the criterion.
    assert 'was not observed' in state['requests'][1]['messages'][-1]['content']
    accepted, error = validate_review(task, task['review_request'], {})
    assert not accepted and error.startswith('Milestone rejected:')


@pytest.mark.parametrize('changed', ['cancelled', 'context', 'goal', 'deadline'])
def test_finalizer_preserves_scope_cancellation_and_deadline(tmp_path, verdict_server, changed):
    state, url = verdict_server
    store, task, gateway, cancel = setup(tmp_path, url)
    if changed == 'context':
        store.add_context(task['id'], 'Changed criterion')
    elif changed == 'goal':
        store.update(task['id'], goal_version=2)
    elif changed == 'cancelled':
        cancel.set()
    with gateway:
        outcome = finalize(store, task, gateway, 'same-model', cancel,
                           result(failed=True, reason='timeout'),
                           time.monotonic() + (-1 if changed == 'deadline' else 10))
    assert outcome['failed'] and not read_document(task, 'STEP_REVIEW.json')
    assert not state['requests']


def test_cancel_inflight_verdict_does_not_wait_for_model(tmp_path, verdict_server):
    state, url = verdict_server
    state['delay'] = 3
    store, task, gateway, cancel = setup(tmp_path, url)
    # Wait for the request itself before cancelling, not gateway preparation.
    def stop():
        deadline = time.monotonic() + 5
        while not state['requests'] and time.monotonic() < deadline:
            time.sleep(.02)
        cancel.set()
    thread = threading.Thread(target=stop)
    thread.start()
    began = time.monotonic()
    with gateway, pytest.raises(InterruptedError):
        finalize(store, task, gateway, 'same-model', cancel, result(), time.monotonic() + 10)
    thread.join(timeout=1)
    assert time.monotonic() - began < 3
    assert not read_document(task, 'STEP_REVIEW.json')


def test_dossier_excludes_history_and_reports_bounded_output(tmp_path):
    store, task, tools = review_session(tmp_path)
    tools.close()
    stale_id = evidence(store, task, output='OLD RESULT')
    task['review_cursor'] = stale_id
    for index in range(65):
        evidence(store, task, process_id=str(index), output='A' * 10000 + '2 skipped')
    packet = dossier(store, task)
    assert packet['omitted_observations'] == 5
    assert all(i['output_truncated'] and '2 skipped' in i['output'] for i in packet['observations'])
    assert 'OLD RESULT' not in json.dumps(packet)
    assert len(json.dumps(packet)) < 80000


def test_budgets_reserve_time_without_extending_user_limits():
    assert budgets({'timeout_seconds': 3600, 'max_hours': 48, 'elapsed': 0}) == (300, 420)
    assert budgets({'timeout_seconds': 100, 'max_hours': 1, 'elapsed': 0}) == (70, 100)
    assert budgets({'timeout_seconds': 3600, 'max_hours': 1, 'elapsed': 3550}) == (35, 50)
    assert budgets({'timeout_seconds': 3600, 'max_hours': 1, 'elapsed': 3600}) == (0, 0)


@pytest.mark.parametrize('reason', ['review_handoff', None])
def test_supervisor_finishes_inspection_without_report_on_same_evidence(tmp_path, verdict_server, monkeypatch, reason):
    from test_supervisor import make
    from agentvisor.tasks import write_document
    state, url = verdict_server
    store, engine, task = make(tmp_path, step_acceptance=True)
    write_document(task, 'PROGRESS.md', '- [x] Exact artifact\n')
    engine.command_builder = None  # Exercise the real gateway and finalization integration.
    engine.command = lambda *_: ['inspection-placeholder']
    monkeypatch.setattr('agentvisor.supervisor.resolve_command_policy', lambda *_: {'allowed': False})

    def inspection(store, current, *args, **kwargs):
        assert current['review_handoff']
        assert current['timeout_seconds'] <= current['review_inspection_seconds']
        event_id = evidence(store, current)
        state['responses'].append({'passed': True, 'summary': 'Fresh tests passed',
                                   'evidence': [{'event_id': event_id, 'finding': '5 passed'}]})
        return result(reason=reason)

    monkeypatch.setattr('agentvisor.supervisor.execute', inspection)
    profile = dict(task['profile'], base_url=url)
    engine.review_step(task, {'instance': 'same-model', 'context': profile['context']}, profile)
    current = store.get(task['id'])
    assert len(current['step_reviews']['accepted']) == 1
    assert current['elapsed'] > 0
    assert not current.get('review_retry')
    assert len(state['requests']) == 1


@pytest.mark.parametrize('close_streams', [False, True])
def test_inspection_deadline_hands_off_instead_of_failing(tmp_path, close_streams):
    import sys
    from agentvisor.execution import execute
    from test_supervisor import make
    store, engine, task = make(tmp_path, timeout_seconds=.8, review_handoff=True)
    script = 'import time, os\nprint("CHECKS_COLLECTED", flush=True)\n'
    if close_streams:
        script += 'os.close(1)\nos.close(2)\n'
    script += 'time.sleep(30)\n'
    outcome = execute(store, task, [sys.executable, '-c', script], engine.cancel)
    assert not outcome['failed'] and outcome['reason'] == 'review_handoff'
    assert 'CHECKS_COLLECTED' in outcome['output_tail']
    assert store.get(task['id'])['pid'] is None
