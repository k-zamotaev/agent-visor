from copy import deepcopy

import pytest

from agentvisor.session_roles import role_prompt, session_role


def task(**changes):
    value = {'goal_version': 2, 'iteration': 7, 'max_iterations': 10,
             'max_hours': 1, 'elapsed': 300, 'timeout_seconds': 60,
             'profile': {'model': 'local-model', 'context': 16384},
             'resolved_profile': {'model': 'loaded-model', 'context': 32768},
             'runtime_instance': 'one-loaded-instance'}
    value.update(changes)
    return value


@pytest.mark.parametrize('recovery', [None, {'goal_version': 2, 'repair': False},
    {'goal_version': 2, 'failure_cause': {'attempts': 2}},
    {'goal_version': 1, 'repair': True, 'failure_cause': {'attempts': 8}}])
def test_normal_work_and_stale_recovery_use_executor(recovery):
    assert session_role(task(recovery_context=recovery))['name'] == 'executor'


@pytest.mark.parametrize('recovery', [
    {'goal_version': 2, 'repair': True},
    {'goal_version': 2, 'failure_cause': {'attempts': 3}},
])
def test_current_repair_or_repeated_cause_gets_a_diagnostic_session(recovery):
    role = session_role(task(recovery_context=recovery))
    assert role['name'] == 'diagnostician'
    prompt = role_prompt(role, '.agentvisor/tasks/example', 2)
    assert 'confirm or disprove' in prompt
    assert 'Never write DONE.md' in prompt
    assert 'goal_version: 2' in prompt
    assert 'check that will decide whether that repair works' in prompt


def test_sticky_recovery_alternates_diagnosis_and_actual_repair():
    current = task(recovery_context={'goal_version': 2, 'repair': True})
    names = []
    for iteration in range(5):
        role = session_role(current)
        names.append(role['name'])
        current['last_session_role'] = {'name': role['name'], 'goal_version': 2}
        current['iteration'] += 1
    assert names == ['diagnostician', 'executor', 'diagnostician', 'executor', 'diagnostician']


def test_new_goal_does_not_skip_diagnosis_due_to_old_role():
    current = task(recovery_context={'goal_version': 2, 'repair': True},
                   last_session_role={'name': 'diagnostician', 'goal_version': 1})
    assert session_role(current)['name'] == 'diagnostician'


def test_explicit_review_keeps_worker_turn_and_profile_unchanged():
    current = task(recovery_context={'goal_version': 2, 'repair': True},
                   last_session_role={'name': 'diagnostician', 'goal_version': 2})
    original = deepcopy(current)
    review = session_role(current, review=True)
    assert review['name'] == 'reviewer'
    assert 'Do not implement repairs' in role_prompt(review, '.agentvisor/tasks/example', 2)
    assert current == original
    assert session_role(current)['name'] == 'executor'
    assert session_role(current)['after_diagnosis']


def test_role_selection_cannot_add_budget_or_route_the_loaded_model():
    current = task(max_iterations=7, max_hours=0.01,
                   recovery_context={'goal_version': 2, 'repair': True})
    original = deepcopy(current)
    role = session_role(current)
    assert role['execution'] == 'sequential_same_loaded_model'
    assert role['uses_existing_task_budgets']
    assert current == original
    assert 'do not start another model' in role_prompt(role, '.agentvisor/tasks/example', 2)


def test_contract_metadata_is_isolated_between_sessions():
    first = session_role(task())
    first['inputs'].append('untrusted prior state')
    first['outputs'].clear()
    following = session_role(task())
    assert 'untrusted prior state' not in following['inputs']
    assert following['outputs']


def test_unknown_role_cannot_silently_become_executor():
    with pytest.raises(ValueError, match='Unknown session role'):
        role_prompt({'name': 'another-model'}, '.agentvisor/tasks/example', 2)
