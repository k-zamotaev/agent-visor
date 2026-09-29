import copy

from agentvisor.session_continuation import continue_session, note_completed_session
from agentvisor.step_acceptance import pending_steps
from agentvisor.store import Store
from agentvisor.task_memory import remember_iteration
from agentvisor.tasks import read_document
from test_progress_protocol import fixture, update


def setup(tmp_path):
    store, _, task = fixture(tmp_path)
    task = store.update(task['id'], failure_streak=2, elapsed=500, iteration=10,
                        active_role={'name': 'executor'})
    task = remember_iteration(store, task)
    return store, task


def save_diagnosis(store, task):
    memory = copy.deepcopy(task['task_memory'])
    memory['diagnostic_proposal'] = {'goal_version': task['goal_version'],
        'context_version': task.get('context_version', 0), 'review_revision': task.get('review_revision', 0),
        'current_step_id': (memory.get('current_step') or {}).get('id', 'none'),
        'iteration': task['iteration'], 'event_id': 1, 'hypothesis': 'Check one failed export case.'}
    return store.update(task['id'], task_memory=memory)


def rotate(store, task, reason='context_handoff', role='executor', report=True, **extra):
    task = store.update(task['id'], iteration=task['iteration'] + 1,
                        active_role={'name': role})
    if role == 'diagnostician' and report:
        task = save_diagnosis(store, task)
    result = {'reason': reason, 'failed': False, 'exit_code': 1,
              'session_handoff': {'reason': reason, 'evidence': {'input_limit': 48000}}, **extra}
    return continue_session(store, task, result)


def test_controlled_handoff_preserves_accepted_and_pending_claims_and_limits(tmp_path):
    store, task = setup(tmp_path)
    preserved = {key: copy.deepcopy(task.get(key)) for key in (
        'progress_plan', 'step_reviews', 'failure_streak', 'elapsed', 'max_hours',
        'max_iterations', 'timeout_seconds', 'context_additions')}
    progress = read_document(task, 'PROGRESS.md')
    task, action = rotate(store, task)
    assert action == 'continue'
    assert {key: task.get(key) for key in preserved} == preserved
    assert read_document(task, 'PROGRESS.md') == progress
    assert len(pending_steps(task)) == 1
    assert task['recovery_context']['session_handoff'] is True
    assert task['recovery_context']['failure_layer'] == 'session'


def test_three_empty_handoffs_get_one_diagnosis_and_executor_then_block(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)  # Establish the existing receipt baseline.
    for index in range(3):
        task, action = rotate(store, task)
        assert action == ('diagnose' if index == 2 else 'continue')
    assert task['session_continuation']['without_result'] == 3
    assert task['recovery_context']['repair'] is True
    task, action = rotate(store, task, role='diagnostician')
    assert action == 'continue' and task['session_continuation']['diagnosed'] is True
    assert task['recovery_context']['repair'] is False
    task, action = rotate(store, task)
    assert action == 'blocked'
    assert task['session_continuation']['without_result'] == 5


def test_restart_retains_handoff_budget(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    task, _ = rotate(store, task)
    restarted = Store(store.directory)
    task = restarted.get(task['id'])
    task, action = rotate(restarted, task)
    assert action == 'continue'
    assert task['session_continuation']['without_result'] == 2
    task, action = rotate(restarted, task)
    assert action == 'diagnose'


def test_ordinary_diagnostic_return_is_not_repeated_after_next_handoff(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    task = store.update(task['id'], active_role={'name': 'diagnostician'})
    task = save_diagnosis(store, task)
    task = note_completed_session(store, task, {'failed': False, 'exit_code': 0})
    assert task['session_continuation']['diagnosed'] is True
    task, action = rotate(store, task)
    assert action == 'continue' and task['recovery_context']['repair'] is False
    task, action = rotate(store, task)
    assert action == 'blocked'


def test_normal_session_new_result_clears_previous_stagnation(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    memory = copy.deepcopy(task['task_memory'])
    memory['checks'] = [{'key': 'new-export-check', 'command': 'pytest tests/test_export.py',
                         'outcome': 'succeeded', 'exit_code': 0}]
    task = store.update(task['id'], task_memory=memory)
    task = note_completed_session(store, task, {'failed': False, 'exit_code': 0})
    assert task['session_continuation']['without_result'] == 0
    assert task['session_continuation']['diagnosed'] is False
    assert task['session_continuation']['diagnosis_requested'] is False


def test_new_file_content_resets_budget_but_file_timestamp_does_not(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    memory = copy.deepcopy(task['task_memory'])
    memory['changed_files'] = [{'path': 'backend/export.py', 'sha256': 'new-code',
                                'exists': True, 'mtime_ns': 1, 'event_id': 1}]
    task = store.update(task['id'], task_memory=memory)
    task, action = rotate(store, task)
    assert action == 'continue'
    assert task['session_continuation']['without_result'] == 0
    assert task['session_continuation']['diagnosis_requested'] is False
    memory['changed_files'][0].update(mtime_ns=2, event_id=2)
    task = store.update(task['id'], task_memory=memory)
    task, _ = rotate(store, task)
    assert task['session_continuation']['without_result'] == 1


def test_new_browser_observation_resets_handoff_stagnation_once(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    observation = {'session_progress': {'result_evidence': [
        {'operation': 'observe browseros-neo_navigate', 'fingerprint': 'course-page-1'}]}}
    task, action = rotate(store, task, **observation)
    assert action == 'continue'
    assert task['session_continuation']['without_result'] == 0
    task, _ = rotate(store, task, **observation)
    assert task['session_continuation']['without_result'] == 1
    observation['session_progress']['result_evidence'][0]['fingerprint'] = 'course-page-2'
    task, action = rotate(store, task, **observation)
    assert action == 'continue'
    assert task['session_continuation']['without_result'] == 0


def test_same_check_output_timing_is_not_new_evidence_but_changed_result_is(tmp_path):
    store, task = setup(tmp_path)
    memory = copy.deepcopy(task['task_memory'])
    check = {'key': 'export-tests', 'command': 'pytest tests/test_export.py',
             'outcome': 'failed', 'exit_code': 1,
             'output': 'failed in 1 second', 'event_id': 1}
    memory['checks'] = [check]
    task = store.update(task['id'], task_memory=memory)
    task, _ = rotate(store, task)
    check.update(output='failed in 2 seconds', event_id=2)
    task = store.update(task['id'], task_memory=memory)
    task, _ = rotate(store, task)
    assert task['session_continuation']['without_result'] == 1
    check.update(outcome='succeeded', exit_code=0)
    task = store.update(task['id'], task_memory=memory)
    task, _ = rotate(store, task)
    assert task['session_continuation']['without_result'] == 0


def test_claim_and_model_write_assertions_alone_are_not_new_acceptance(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    update(store, task, 'claim', step_id=task['progress_plan']['steps'][2]['id'], note='I finished')
    task = store.get(task['id'])
    task, _ = rotate(store, task, session_progress={
        'new_progress_count': 100, 'result_evidence': [
            {'operation': 'write fake.py', 'fingerprint': 'unverified-write'}]})
    assert task['session_continuation']['without_result'] == 1
    assert len(task['step_reviews']['accepted']) == 1


def test_new_user_direction_starts_a_new_scope_without_removing_old_receipts(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    receipts = copy.deepcopy(task['step_reviews'])
    task = store.add_context(task['id'], 'Add modern accessible interface design')
    task, action = rotate(store, task)
    assert action == 'continue'
    assert task['session_continuation']['without_result'] == 0
    assert task['session_continuation']['scope'][1] == task['context_version']
    assert task['step_reviews'] == receipts


def test_static_context_overflow_blocks_immediately_without_reopening_claims(tmp_path):
    store, task = setup(tmp_path)
    plan = copy.deepcopy(task['progress_plan'])
    task, action = rotate(store, task, reason='context_blocked')
    assert action == 'blocked'
    assert task['progress_plan'] == plan
    assert len(task['step_reviews']['accepted']) == len(pending_steps(task)) == 1


def test_note_churn_cannot_keep_session_rotating_forever(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for index in range(3):
        memory = copy.deepcopy(task['task_memory'])
        memory['changed_files'] = [{'path': 'MEMORY.md', 'exists': True,
                                    'sha256': f'new-promise-{index}'}]
        task = store.update(task['id'], task_memory=memory)
        task, action = rotate(store, task)
    assert action == 'diagnose'
    assert task['session_continuation']['without_result'] == 3


def test_searching_test_directories_is_not_a_completed_verification(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for index in range(3):
        memory = copy.deepcopy(task['task_memory'])
        memory['checks'] = [{'key': f'list-tests-{index}',
                             'command': f'Get-ChildItem backend/tests/subdir{index}',
                             'outcome': 'succeeded', 'exit_code': 0}]
        task = store.update(task['id'], task_memory=memory)
        task, action = rotate(store, task)
    assert action == 'diagnose'
    assert task['session_continuation']['without_result'] == 3


def test_interrupted_diagnosis_returns_to_executor_after_two_missing_reports(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    task, action = rotate(store, task, role='diagnostician', report=False)
    assert action == 'diagnose'
    assert task['session_continuation']['diagnosed'] is False
    assert task['session_continuation']['diagnostic_attempts'] == 1
    task, action = rotate(store, task, role='diagnostician', report=False)
    assert action == 'continue'
    assert task['session_continuation']['diagnosed'] is False
    assert task['recovery_context']['diagnostic_report_missing'] is True
    assert task['recovery_context']['repair'] is False
    assert task['session_continuation']['diagnosis_requested'] is False
    task, action = rotate(store, task)
    assert action == 'continue'
    assert task['session_continuation']['diagnostic_attempts'] == 2
    assert len([event for event in store.events(task['id'])
                if event['kind'] == 'diagnostic_fallback']) == 1


def test_stale_report_is_not_a_completed_new_diagnostic_attempt(tmp_path):
    store, task = setup(tmp_path)
    task = save_diagnosis(store, task)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    task, action = rotate(store, task, role='diagnostician', report=False)
    assert action == 'diagnose'
    assert task['session_continuation']['diagnosed'] is False


def test_ordinary_diagnostic_exit_without_report_counts_failed_delivery(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    task = store.update(task['id'], active_role={'name': 'diagnostician'})
    task = note_completed_session(store, task, {'failed': False, 'exit_code': 0})
    assert task['session_continuation']['diagnosed'] is False
    assert task['session_continuation']['diagnostic_attempts'] == 1
    assert task['session_continuation']['diagnostic_report_missing'] is True


def test_second_diagnostic_report_gets_executor_attempt_before_blocking(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    for _ in range(3):
        task, _ = rotate(store, task)
    task, _ = rotate(store, task, role='diagnostician', report=False)
    task, action = rotate(store, task, role='diagnostician')
    assert action == 'continue' and task['session_continuation']['diagnosed'] is True
    assert task['session_continuation']['executor_attempts_after_diagnosis'] == 0
    task, action = rotate(store, task)
    assert action == 'blocked'


def test_supervisor_continues_after_missing_ordinary_diagnostic_report(tmp_path, monkeypatch):
    from test_supervisor import make, finish
    from agentvisor.task_memory import initialize_memory
    from agentvisor.recovery import initialize_progress
    store, engine, task = make(tmp_path, max_iterations=8, autonomous_recovery=True)
    task = initialize_memory(store, initialize_progress(store, task))
    store.update(task['id'], session_continuation={
        'scope': [task['goal_version'], 0, None], 'without_result': 4,
        'diagnosis_requested': True, 'diagnosed': False, 'diagnostic_attempts': 1, 'seen': []},
        recovery_context={'goal_version': task['goal_version'], 'repair': True})
    monkeypatch.setattr('agentvisor.supervisor.execute', lambda *args, **kwargs: {
        'reason': '', 'failed': False, 'exit_code': 0, 'output_tokens': 0, 'duration': .01})
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'blocked' and result['iteration'] == 8
    assert 'лимит итераций' in result['reason']
    assert result['session_continuation']['diagnosed'] is False
    assert any(event['kind'] == 'diagnostic_fallback' for event in store.events(task['id']))


def test_legacy_diagnosed_flag_without_report_is_not_success_and_keeps_budget(tmp_path):
    store, task = setup(tmp_path)
    task, _ = rotate(store, task)
    state = dict(task['session_continuation'], without_result=5, diagnosed=True, diagnosis_requested=True)
    task = store.update(task['id'], session_continuation=state)
    task, action = rotate(store, task)
    assert action == 'diagnose'
    assert task['session_continuation']['without_result'] == 6
    assert task['session_continuation']['seen'] == state['seen']
    assert task['session_continuation']['diagnosed'] is False


def test_role_retries_missing_report_once_then_returns_to_executor(tmp_path):
    from agentvisor.session_roles import session_role
    store, task = setup(tmp_path)
    task = store.update(task['id'], last_session_role={'name': 'diagnostician', 'goal_version': task['goal_version']},
        recovery_context={'goal_version': task['goal_version'], 'repair': True},
        session_continuation={'scope': [task['goal_version'], task.get('context_version', 0), None],
                              'diagnostic_report_missing': True, 'diagnostic_attempts': 1})
    assert session_role(task)['name'] == 'diagnostician'
    task['session_continuation']['diagnostic_attempts'] = 2
    assert session_role(task)['name'] == 'executor'
