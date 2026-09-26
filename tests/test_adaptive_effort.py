from copy import deepcopy

import pytest

from agentvisor.adaptive_effort import effort_plan, effort_prompt, provider_options


def task_with_recovery(repeats=0, goal_version=1, **fields):
    return {'goal_version': 1, 'recovery_context': {
        'goal_version': goal_version, 'repeated_failure_count': repeats, **fields}}


def profile(**fields):
    return {'reasoning': 'auto', 'context': 32768, 'output_limit': 8192,
            'model': 'already-loaded', 'runtime': 'lmstudio',
            'base_url': 'http://127.0.0.1:1234', 'gpu': 'max', **fields}


def test_routine_session_has_small_response_and_working_horizon_without_native_guess():
    plan = effort_plan({'goal_version': 1}, {'name': 'executor'}, profile())
    assert plan['level'] == 'routine'
    assert plan['output_limit'] == 2048 and plan['hypothesis_limit'] == 1
    assert plan['working_horizon'] == 'current assigned step or review scope'
    assert provider_options(plan) == {}
    prompt = effort_prompt(plan)
    assert 'one concrete next action' in prompt
    assert 'preserve required acceptance checks' in prompt
    assert 'already loaded model' in prompt


@pytest.mark.parametrize(('task', 'role', 'expected', 'cap'), [
    ({'goal_version': 1}, 'reviewer', 'focused', 4096),
    (task_with_recovery(1), 'executor', 'focused', 4096),
    ({'goal_version': 1}, {'name': 'executor', 'after_diagnosis': True}, 'focused', 4096),
    ({'goal_version': 1}, 'diagnostician', 'deep', 8192),
    (task_with_recovery(3), 'executor', 'deep', 8192),
    (task_with_recovery(1, failure_cause={'attempts': 4}), 'executor', 'deep', 8192),
    (task_with_recovery(4), 'reviewer', 'deep', 8192),
])
def test_effort_uses_role_and_persisted_current_goal_repeats(task, role, expected, cap):
    plan = effort_plan(task, role, profile())
    assert plan['level'] == expected and plan['output_limit'] == cap
    assert 'assigned role' in effort_prompt(plan)


def test_old_goal_recovery_does_not_escalate_new_work():
    plan = effort_plan(task_with_recovery(99, goal_version=0, repair=True), 'executor', profile())
    assert plan['level'] == 'routine'
    assert plan['repeated_failure_count'] == 0


def test_profile_role_task_and_limits_are_immutable():
    task = task_with_recovery(4, failure_cause={'attempts': 6})
    role = {'name': 'diagnostician', 'inputs': ['observed error']}
    current_profile = profile(capabilities={'reasoning_effort_values': ['low', 'high']})
    before = deepcopy((task, role, current_profile))
    plan = effort_plan(task, role, current_profile)
    effort_prompt(plan)
    provider_options(plan)
    assert (task, role, current_profile) == before


@pytest.mark.parametrize(('context', 'output'), [(4096, 256), (4096, 10000), (4, 1),
                                               (8192, 2048), (32768, 8192), (262144, 32768)])
@pytest.mark.parametrize('role', ['executor', 'reviewer', 'diagnostician'])
def test_response_limit_never_exceeds_user_limit_or_context_budget(context, output, role):
    plan = effort_plan({'goal_version': 1}, role, profile(context=context, output_limit=output))
    assert 0 < plan['output_limit'] <= output
    assert plan['output_limit'] < context // 2
    assert plan['configured_output_ceiling'] == min(output, context // 2 - 1)


@pytest.mark.parametrize('reasoning', ['off', 'on', 'low', 'medium', 'high', 'xhigh'])
def test_explicit_reasoning_preserves_user_parameter_and_response_ceiling(reasoning):
    current_profile = profile(reasoning=reasoning,
                              capabilities={'reasoning_effort_values': ['none', 'low', 'medium', 'high']})
    plan = effort_plan({'goal_version': 1}, 'executor', current_profile)
    assert plan['reasoning_policy'] == 'preserve_user'
    assert plan['native_reasoning_effort'] is None
    assert plan['output_limit'] == current_profile['output_limit']
    assert provider_options(plan) == {}
    assert current_profile['reasoning'] == reasoning


@pytest.mark.parametrize(('role', 'native'), [('executor', 'low'), ('reviewer', 'medium'),
                                            ('diagnostician', 'high')])
def test_native_hint_requires_explicit_supported_api_values(role, native):
    current_profile = profile(capabilities={'reasoning_effort_values': ['none', 'low', 'medium', 'high']})
    plan = effort_plan({'goal_version': 1}, role, current_profile)
    assert provider_options(plan) == {'reasoningEffort': native}
    assert native in current_profile['capabilities']['reasoning_effort_values']


@pytest.mark.parametrize('capabilities', [None, True, {}, {'reasoning': True},
    {'reasoning_effort_values': ['on', 'off']}, {'reasoning_effort_values': 'high'},
    {'reasoning_effort_values': []}, {'reasoning_effort_values': ['high', True]},
    {'reasoning_effort_values': ['future-value']}, {'reasoning_effort_values': {'high': True}}])
def test_unknown_or_native_toggle_metadata_does_not_invent_provider_parameters(capabilities):
    plan = effort_plan(task_with_recovery(4), 'diagnostician', profile(capabilities=capabilities))
    assert plan['level'] == 'deep' and plan['native_reasoning_effort'] is None
    assert provider_options(plan) == {}


def test_native_selection_never_returns_an_unadvertised_value():
    plan = effort_plan(task_with_recovery(4), 'diagnostician',
                       profile(capabilities={'reasoning_effort_values': ['none', 'low']}))
    assert provider_options(plan) == {'reasoningEffort': 'low'}


def test_deep_prompt_requires_evidence_and_acknowledges_capability_limit():
    plan = effort_plan(task_with_recovery(4), 'diagnostician', profile())
    prompt = effort_prompt(plan)
    assert 'at most three explanations' in prompt and 'bounded checks' in prompt
    assert 'observed facts from hypotheses' in prompt
    assert 'underlying model capability is unchanged' in plan['limitation']


def test_plan_and_options_cannot_route_load_or_resize_a_model():
    plan = effort_plan(task_with_recovery(4), 'diagnostician', profile())
    forbidden = {'model', 'runtime', 'base_url', 'context', 'gpu', 'load', '_reload',
                 'api_base_url', 'fallback', 'route', 'context_floor', 'output_limit_override'}
    assert not forbidden.intersection(plan)
    assert not forbidden.intersection(provider_options(plan))
    assert 'already-loaded' not in str(plan)
    assert '127.0.0.1' not in str(plan)


@pytest.mark.parametrize('fields', [{'output_limit': 0}, {'output_limit': True},
                                   {'output_limit': 1.5}, {'context': 0}, {'context': True}])
def test_invalid_budgets_fail_without_silently_raising_user_limits(fields):
    with pytest.raises(ValueError, match='output_limit'):
        effort_plan({'goal_version': 1}, 'executor', profile(**fields))


def test_deep_executor_still_repairs_instead_of_only_diagnosing():
    plan = effort_plan(task_with_recovery(4), 'executor', profile())
    assert 'implement the chosen repair' in effort_prompt(plan)
    assert 'save that evidence for the executor' not in effort_prompt(plan)


def test_real_session_options_preserve_model_and_manual_settings(tmp_path):
    import json
    from agentvisor.tasks import prepare_documents, read_document
    from test_supervisor import make
    store, _, task = make(tmp_path)
    ready = {'instance': 'already-loaded', 'context': 16384}
    prompt = prepare_documents(task, ready)
    config = json.loads(read_document(task, 'opencode.json'))
    model = config['provider']['agentvisor']['models']['already-loaded']
    assert model['limit']['output'] == 2048
    assert 'reasoningEffort' not in model['options']
    assert 'SESSION EFFORT: routine' in prompt
    task = store.update(task['id'], profile=dict(task['profile'], reasoning='high'))
    prepare_documents(task, ready)
    model = json.loads(read_document(task, 'opencode.json'))['provider']['agentvisor']['models']['already-loaded']
    assert model['limit']['output'] == task['profile']['output_limit']
    assert model['options']['reasoningEffort'] == 'high'


def test_session_effort_respects_disabled_tuning_and_loaded_context():
    from agentvisor.adaptive_effort import session_effort
    current = {'goal_version': 1, 'profile': profile(), 'auto_tune': False}
    assert session_effort(current, {'context': 4096}) is None
    current['auto_tune'] = True
    assert session_effort(current, {'context': 4096})['output_limit'] < 2048
