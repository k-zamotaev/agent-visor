import json

import pytest

from agentvisor.command_mcp import CommandMCP
from agentvisor.step_acceptance import observed_evidence, pending_steps, validate_review
from agentvisor.tasks import read_document, write_document
from test_supervisor import make


def review_session(tmp_path):
    store, engine, task = make(tmp_path, step_acceptance=True)
    write_document(task, 'PROGRESS.md', '- [x] Database\n')
    cursor = store.events(task['id'])[-1]['id']
    task = dict(task, review_phase=True, review_cursor=cursor,
                review_request={'id': 'current-review', 'steps': pending_steps(task)})
    return store, task, CommandMCP(store, task, engine.cancel)


def evidence(store, task, **changes):
    data = dict(process_id='check', status='completed', exit_code=0,
                input={'command': 'python -m pytest'}, output='5 passed')
    data.update(changes)
    store.event(task['id'], 'command_finished', 'pytest', data=data)
    return store.events(task['id'])[-1]['id']


def test_submission_uses_real_evidence_and_writes_exact_report(tmp_path):
    store, task, tools = review_session(tmp_path)
    try:
        event_id = evidence(store, task)
        assert tools.call('review_evidence', {})['evidence'][0]['event_id'] == event_id
        result = tools.call('submit_review', {'passed': True, 'summary': 'Tests passed',
                           'evidence': [{'event_id': event_id, 'finding': '5 passed'}]})
        assert result['status'] == 'submitted'
        report = json.loads(read_document(task, 'STEP_REVIEW.json'))
        assert report['review_id'] == task['review_request']['id']
        accepted, error = validate_review(task, task['review_request'], observed_evidence(store, task, task['review_cursor']))
        assert not error
        assert next(iter(accepted.values()))['evidence_events'] == [event_id]
    finally:
        tools.close()


@pytest.mark.parametrize('bad', ['fabricated', 'failed', 'running', 'stale', 'self_report', 'changed_context'])
def test_submission_rejects_invalid_evidence_without_creating_report(tmp_path, bad):
    store, task, tools = review_session(tmp_path)
    try:
        event_id = evidence(store, task, exit_code=1 if bad == 'failed' else 0,
                            status='running' if bad == 'running' else 'completed')
        if bad == 'stale':
            task['review_cursor'] = event_id
        if bad == 'fabricated':
            event_id += 100
        if bad == 'self_report':
            store.event(task['id'], 'tool_finished', 'read', data={'tool': 'read', 'status': 'completed',
                'input': {'filePath': task['workspace'] + '/.agentvisor/tasks/id/STEP_REVIEW.json'}})
            event_id = store.events(task['id'])[-1]['id']
        if bad == 'changed_context':
            store.add_context(task['id'], 'New requirement')
        with pytest.raises(ValueError):
            tools.call('submit_review', {'passed': True, 'summary': 'Claimed pass',
                       'evidence': [{'event_id': event_id, 'finding': 'OK'}]})
        assert not read_document(task, 'STEP_REVIEW.json')
    finally:
        tools.close()


def test_negative_verdict_needs_no_fabricated_evidence(tmp_path):
    _, task, tools = review_session(tmp_path)
    try:
        result = tools.call('submit_review', {'passed': False, 'summary': 'Expected API missing', 'evidence': []})
        assert result['status'] == 'submitted' and not result['passed']
        assert json.loads(read_document(task, 'STEP_REVIEW.json'))['steps'][0]['passed'] is False
    finally:
        tools.close()


def test_evidence_does_not_expire_after_200_later_tools(tmp_path):
    store, task, tools = review_session(tmp_path)
    try:
        event_id = evidence(store, task)
        for index in range(205):
            store.event(task['id'], 'tool_finished', 'write', data={
                'call_id': str(index), 'tool': 'write', 'status': 'completed'})
        result = tools.call('submit_review', {'passed': True, 'summary': 'Still fresh in this review',
                           'evidence': [{'event_id': event_id, 'finding': '5 passed'}]})
        assert result['status'] == 'submitted'
    finally:
        tools.close()
