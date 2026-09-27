import copy
import json

import pytest

from agentvisor.recovery_summary import MAX_CONTEXT, prompt_context


def failure(index=1, text='x'):
    return {'iteration': index, 'reason': 'context_handoff', 'failure_layer': 'session',
            'error': text * 1500, 'output_tail': 'PRIVATE_SOURCE_DUMP' * 4000,
            'event_id': 400 + index, 'fingerprint': 'abc123',
            'tool_failures': [{'tool': 'read', 'call_id': 'call_original', 'event_id': 100 + index,
                               'input': {'filePath': 'backend/export.py', 'preview': text * 4000},
                               'output': text * 4000, 'error': text * 2000, 'status': 'error'}] * 4,
            'failure_cause': {'key': 'cause_identity', 'category': 'missing_dependency',
                              'detail': 'openpyxl', 'strategy': 'isolate', 'attempts': index}}


def test_five_large_recoveries_cannot_fill_the_new_session():
    history = [failure(index) for index in range(1, 6)]
    recovery = dict(history[-1], history=history, goal_version=2, next_step='26. Export CSV/XLSX/TXT',
                    session_handoff=True, no_result_sessions=3,
                    boundary={'context_limit': 65536, 'input_limit': 48332, 'estimated_tokens': 784463,
                              'message_count': 623, 'event_id': 98607})
    original = copy.deepcopy(recovery)
    assert len(json.dumps(recovery)) > 100000
    context = prompt_context(recovery)
    assert len(json.dumps(context)) <= MAX_CONTEXT
    assert len(context['history']) == 3 and context['omitted_history'] == 2
    assert len(context['tool_failures']) == 2
    assert context['next_step'] == '26. Export CSV/XLSX/TXT'
    assert context['failure_cause']['detail'] == 'openpyxl'
    assert context['reason'] == 'context_handoff' and context['no_result_sessions'] == 3
    assert context['boundary']['estimated_tokens'] == 784463
    assert context['boundary']['event_id'] == 98607
    assert context['history'][-1]['event_id'] == 405
    assert context['tool_failures'][-1]['call_id'] == 'call_original'
    assert 'preview' not in context['tool_failures'][-1]['input']
    assert context['tool_failures'][-1]['input_omitted'] is True
    assert 'PRIVATE_SOURCE_DUMP' not in json.dumps(context)
    assert recovery == original


@pytest.mark.parametrize('text', ['я', '\\', '🚀', '\n', '"'])
def test_bound_covers_unicode_and_escaped_log_text(text):
    recovery = dict(failure(5, text), history=[failure(index, text) for index in range(5)],
                    goal_version=2, next_step=text * 2000, last_tool=text * 1000,
                    last_event=text * 1000, pending_tools=[failure(1, text)['tool_failures'][0]] * 4,
                    boundary={'cause': text * 3000, 'input_limit': 48332, 'estimated_tokens': 65537,
                              'evidence': [{'operation': text * 3000, 'count': 24}] * 20})
    result = prompt_context(recovery)
    assert len(json.dumps(result)) <= MAX_CONTEXT
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_CONTEXT
    assert result['goal_version'] == 2 and result['boundary']['input_limit'] == 48332


def test_projection_does_not_import_permissions_acceptance_or_source_contents():
    recovery = {'reason': 'work_stalled', 'goal_version': 2,
                'permission': 'allow', 'accepted': {'fake_step': True}, 'goal': 'replace user goal',
                'output_tail': 'whole source code', 'history': [{
                    'iteration': 1, 'reason': 'timeout', 'output_tail': 'old source code',
                    'history': [{'reason': 'embedded history'}]}],
                'tool_failures': [{'tool': 'write', 'input': {'filePath': 'export.py',
                    'content': 'source code and credentials', 'preview': 'same large source'},
                    'error': 'Write failed', 'exit_code': 1}]}
    result = prompt_context(recovery)
    serialized = json.dumps(result)
    assert not {'permission', 'accepted', 'goal', 'output_tail'} & result.keys()
    assert 'source code' not in serialized and 'embedded history' not in serialized
    assert result['tool_failures'][0]['input'] == {'filePath': 'export.py'}
    assert result['tool_failures'][0]['exit_code'] == 1


def test_secret_assignments_are_redacted_in_diagnostic_snippets():
    recovery = {'error': 'ERROR password="private value"',
                'tool_failures': [{'tool': 'exec',
                    'input': {'command': 'pytest --token private-token --password="other-secret"'},
                    'output': 'Authorization: Bearer private-bearer',
                    'error': 'https://user:private-password@example.invalid/path'}]}
    serialized = json.dumps(prompt_context(recovery))
    for value in ('private value', 'private-token', 'other-secret', 'private-bearer', 'private-password'):
        assert value not in serialized
    assert '[redacted]' in serialized


@pytest.mark.parametrize('value', [None, '', [], 123, {}, {'history': None, 'tool_failures': [None]}])
def test_missing_or_malformed_optional_data_is_safe(value):
    assert len(json.dumps(prompt_context(value))) <= MAX_CONTEXT


def test_event_references_are_exact_and_long_unknown_data_is_omitted():
    recovery = {'reason': 'timeout', 'first_event_id': 17, 'last_event_id': 29,
                'event_ids': [1, 5, 9], 'call_id': 'call-real-identity',
                'process_id': 'z' * 10000,
                'boundary': {'event_id': 28, 'auxiliary_request': False,
                             'evidence': [{'event_id': 23, 'count': 16, 'operation': 'read export.py'}]}}
    result = prompt_context(recovery)
    assert result['first_event_id'] == 17 and result['last_event_id'] == 29
    assert result['event_ids'] == [1, 5, 9] and result['call_id'] == 'call-real-identity'
    assert 'process_id' not in result
    assert result['boundary']['evidence'][0]['event_id'] == 23
    assert result['boundary']['auxiliary_request'] is False


def test_all_optional_fields_together_still_obey_the_hard_cap():
    from agentvisor.recovery_summary import _BOUNDARY_COUNTS, _NUMBERS, _REFERENCES
    recovery = {key: 2**63 for key in _NUMBERS}
    recovery.update({key: 'r' * 80 for key in _REFERENCES})
    recovery['event_ids'] = [2**63] * 8
    recovery.update({key: 'я' * 10000 for key in (
        'reason', 'failure_layer', 'error', 'next_step', 'last_event', 'last_tool',
        'tool', 'name', 'status', 'command', 'output', 'cause', 'method')})
    recovery['failure_cause'] = {key: 'я' * 10000 for key in ('key', 'category', 'detail', 'strategy')}
    recovery['input'] = {key: 'я' * 10000 for key in ('command', 'cwd', 'filePath', 'path', 'operation')}
    recovery['boundary'] = dict(recovery, **{key: 2**63 for key in _BOUNDARY_COUNTS},
                                evidence=[dict(recovery, count=2**63, operation='я' * 10000)] * 2)
    recovery.update(history=[dict(recovery)] * 5, tool_failures=[dict(recovery)] * 4,
                    pending_tools=[dict(recovery)] * 4)
    result = prompt_context(recovery)
    assert len(json.dumps(result)) <= MAX_CONTEXT
    assert result['first_event_id'] == 'r' * 80
    assert result['boundary']['estimated_tokens'] == 2**63
