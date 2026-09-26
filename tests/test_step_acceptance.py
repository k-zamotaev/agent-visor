import json

import pytest

from agentvisor.step_acceptance import observed_evidence, pending_steps, validate_review
from agentvisor.tasks import checklist, read_document, write_document
from test_supervisor import make


def result(**overrides):
    return dict({'failed': False, 'reason': None, 'exit_code': 0,
                 'output_tokens': 7, 'duration': 0.01}, **overrides)


def report(store, task, *, passed=True, fresh=True, stale=False):
    review = next(event['data'] for event in reversed(store.events(task['id']))
                  if event['kind'] == 'step_review_started')
    if fresh:
        store.event(task['id'], 'command_finished', 'python -m pytest', data={
            'process_id': 'review-check', 'status': 'completed', 'exit_code': 0,
            'input': {'command': 'python -m pytest'}, 'output': '1 passed'})
    write_document(task, 'STEP_REVIEW.json', json.dumps({
        'review_id': 'old' if stale else review['review_id'], 'goal_version': task['goal_version'],
        'steps': [{'id': step['id'], 'passed': passed, 'summary': 'Verified behavior' if passed else 'Expected UI missing',
                   'evidence': [{'kind': 'command', 'value': 'python -m pytest', 'finding': '1 passed'}]}
                  for step in review['steps']]}))


@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'missing_evidence', 'stale', 'process_failed', 'cancelled', 'context_changed'])
def test_supervisor_reviews_fresh_session_before_counting_progress(tmp_path, monkeypatch, outcome):
    store, engine, task = make(tmp_path, step_acceptance=True, max_failures=1, max_iterations=1)
    phases = []
    def execute(store, current, *args, **kwargs):
        phases.append('review' if current.get('review_phase') else 'work')
        if not current.get('review_phase'):
            write_document(current, 'PROGRESS.md', '- [x] Implement behavior\n')
            write_document(current, 'DONE.md', 'goal_version: 1\nDone\n')
            # Successful evidence from the worker must not qualify for review.
            store.event(task['id'], 'command_finished', 'python -m pytest', data={
                'status': 'completed', 'exit_code': 0})
        else:
            assert not checklist(current)[0]['done']
            report(store, current, passed=outcome != 'rejected', fresh=outcome != 'missing_evidence',
                   stale=outcome == 'stale')
            if outcome == 'cancelled':
                engine.cancel.set()
            if outcome == 'context_changed':
                store.add_context(current['id'], 'Also check keyboard navigation')
            if outcome == 'process_failed':
                return result(failed=True, reason='timeout')
        return result()
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert phases == ['work', 'review']
    assert current['output_tokens'] == 14
    if outcome == 'accepted':
        assert current['status'] == 'completed_unverified'
        assert checklist(current)[0]['done']
        assert not pending_steps(current)
        accepted = list(current['step_reviews']['accepted'].values())
        assert accepted[0]['evidence_events']
        assert current['progress_watch']['completed'] == 1
    else:
        assert current['status'] == ('paused' if outcome == 'cancelled' else 'blocked')
        assert not checklist(current)[0]['done']
        assert current['progress_watch']['completed'] == 0
        if outcome == 'rejected':
            assert 'Expected UI missing' in current['recovery_context']['error']


def test_failed_worker_cannot_advance_with_unreviewed_checkmarks(tmp_path, monkeypatch):
    store, engine, task = make(tmp_path, step_acceptance=True, max_failures=1)
    def execute(*args, **kwargs):
        write_document(task, 'PROGRESS.md', '- [x] Unverified claim\n')
        return result(failed=True, exit_code=1)
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    assert not checklist(task)[0]['done']
    assert store.get(task['id'])['progress_watch']['completed'] == 0


def test_evidence_ignores_running_failed_provisional_and_self_reports(tmp_path):
    store, _, task = make(tmp_path)
    cursor = store.events(task['id'])[-1]['id']
    for status, code in [('running', None), ('completed', 1), ('stopped', 0)]:
        store.event(task['id'], 'command_finished', 'bad', data={'status': status, 'exit_code': code})
    for inferred, code in [(True, 0), (False, None), (False, 1)]:
        store.event(task['id'], 'tool_finished', 'bash', data={'tool': 'bash', 'status': 'completed',
            'inferred': inferred, 'exit_code': code, 'input': {'command': 'bad'}})
    store.event(task['id'], 'tool_finished', 'read', data={'tool': 'read', 'status': 'completed',
        'input': {'filePath': 'D:\\project\\.AGENTVISOR\\tasks\\STEP_REVIEW.json'}})
    store.event(task['id'], 'tool_finished', 'read', data={'tool': 'read', 'status': 'completed',
        'inferred': True, 'output': 'Error: File not found', 'input': {'filePath': 'missing.md'}})
    assert observed_evidence(store, task, cursor) == {}
    store.event(task['id'], 'tool_finished', 'read', data={'tool': 'read', 'status': 'completed',
        'call_id': 'read1', 'input': {'filePath': 'README.md'}})
    assert ('read', 'README.md') in observed_evidence(store, task, cursor)


def test_later_authoritative_tool_failure_invalidates_provisional_success(tmp_path):
    store, _, task = make(tmp_path)
    cursor = store.events(task['id'])[-1]['id']
    for error in ('', 'failed'):
        store.event(task['id'], 'tool_finished', 'bash', data={'tool': 'bash', 'status': 'completed',
            'call_id': 'one', 'exit_code': 0, 'error': error, 'input': {'command': 'check'}})
    assert observed_evidence(store, task, cursor) == {}


def test_review_prompt_does_not_include_worker_instructions(tmp_path):
    from agentvisor.tasks import prepare_documents
    store, _, task = make(tmp_path, step_acceptance=True)
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384}, review={'id': 'new', 'steps': []})
    assert 'independent milestone reviewer' in prompt
    assert 'Take only the NEXT unchecked' not in prompt
    assert 'Do not edit product code' in prompt


def test_invalid_report_is_rejected_and_changed_goal_requires_new_review(tmp_path):
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [x] Step\n')
    steps = pending_steps(task)
    write_document(task, 'STEP_REVIEW.json', '{broken')
    assert validate_review(task, {'id': 'one', 'steps': steps}, {})[1]
    task = store.update(task['id'], step_reviews={'goal_version': 1, 'accepted': {steps[0]['id']: {}}})
    assert not pending_steps(task)
    assert pending_steps(dict(task, context_version=1))
    task = store.update(task['id'], goal_version=2)
    assert pending_steps(task)


def test_full_long_command_matches_by_observed_hash(tmp_path):
    from agentvisor.tool_trace import fingerprint
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [x] Behavior\n')
    steps = pending_steps(task)
    command = 'Write-Output ' + 'x' * 2000
    cursor = store.events(task['id'])[-1]['id']
    store.event(task['id'], 'command_finished', command, data={'status': 'completed', 'exit_code': 0,
        'input': {'command': command[:1500], 'command_hash': fingerprint('command', command)}})
    write_document(task, 'STEP_REVIEW.json', json.dumps({'review_id': 'one', 'goal_version': 1,
        'steps': [{'id': steps[0]['id'], 'passed': True, 'evidence': [
            {'kind': 'command', 'value': command, 'finding': 'Successful actual output'}]}]}))
    accepted, error = validate_review(task, {'id': 'one', 'steps': steps}, observed_evidence(store, task, cursor))
    assert not error and accepted


def test_review_heartbeat_time_is_counted_once(tmp_path, monkeypatch):
    store, engine, task = make(tmp_path, step_acceptance=True)
    task = store.update(task['id'], elapsed=100)
    write_document(task, 'PROGRESS.md', '- [x] Behavior\n')
    clock = {'now': 1000.0}
    monkeypatch.setattr('agentvisor.supervisor.time.monotonic', lambda: clock['now'])
    def execute(store, current, *args, **kwargs):
        clock['now'] += 50
        store.update(current['id'], elapsed=150)
        report(store, current)
        return result(duration=50)
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.review_steps(task, {'instance': 'fake', 'context': 16384}, task['profile'])
    assert store.get(task['id'])['elapsed'] == 150


def test_optional_snapshot_does_not_hold_control_lock_or_relabel_new_goal(tmp_path, monkeypatch):
    import threading
    store, engine, task = make(tmp_path, step_acceptance=True)
    write_document(task, 'PROGRESS.md', '- [x] Behavior\n')
    def execute(store, current, *args, **kwargs):
        report(store, current)
        return result()
    def recipes(store, current, accepted):
        acquired = threading.Event()
        def edit():
            with engine.lock:
                store.update(current['id'], goal_version=2)
                acquired.set()
        worker = threading.Thread(target=edit)
        worker.start()
        try:
            assert acquired.wait(1), 'Optional work is holding the user control lock'
        finally:
            worker.join(timeout=2)
        return []
    seen = []
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    monkeypatch.setattr('agentvisor.supervisor.record_skills', recipes)
    monkeypatch.setattr('agentvisor.supervisor.checkpoint_after_acceptance', lambda store, current: seen.append(current['goal_version']))
    engine.review_steps(task, {'instance': 'fake', 'context': 16384}, task['profile'])
    assert seen == [1] and store.get(task['id'])['goal_version'] == 2
