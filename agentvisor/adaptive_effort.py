"""Adapt a session's working style and response budget on the same loaded model."""


LEVELS = {
    'routine': {'output_cap': 2048, 'hypothesis_limit': 1, 'target_effort': 'low'},
    'focused': {'output_cap': 4096, 'hypothesis_limit': 2, 'target_effort': 'medium'},
    'deep': {'output_cap': None, 'hypothesis_limit': 3, 'target_effort': 'high'},
}
NATIVE_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')


def _count(value):
    return min(value, 1000) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _native_effort(profile, level):
    """Use only normalized, explicitly advertised reasoning_effort API values.

    The caller may supply capabilities.reasoning_effort_values from trustworthy
    provider metadata. A reasoning boolean or LM Studio's native on/off toggle
    does not establish support for the OpenAI-compatible reasoning_effort field.
    """
    capabilities = profile.get('capabilities')
    if not isinstance(capabilities, dict):
        return None
    values = capabilities.get('reasoning_effort_values')
    if (not isinstance(values, (list, tuple)) or not values or
            any(not isinstance(value, str) or value not in NATIVE_EFFORTS for value in values)):
        return None
    target = NATIVE_EFFORTS.index(LEVELS[level]['target_effort'])
    return min(set(values), key=lambda value: (abs(NATIVE_EFFORTS.index(value) - target),
                                              NATIVE_EFFORTS.index(value)))


def effort_plan(task, role, profile):
    """Return request guidance; never mutate profiles, route, load, or tune a model.

    A response cap is bounded by the configured output ceiling and half the
    configured context. It is not a measurement of free context for a request.
    Explicit user reasoning also preserves its configured response ceiling.
    """
    name = role.get('name') if isinstance(role, dict) else role
    if name not in {'executor', 'diagnostician', 'reviewer'}:
        raise ValueError('Unknown session role')
    output = profile.get('output_limit', 4096)
    context = profile.get('context', 16384)
    if (isinstance(output, bool) or not isinstance(output, int) or output <= 0 or
            isinstance(context, bool) or not isinstance(context, int) or context < 4):
        raise ValueError('Positive output_limit and usable context are required')
    ceiling = min(output, context // 2 - 1)
    recovery = task.get('recovery_context') or {}
    if not isinstance(recovery, dict) or recovery.get('goal_version') != task.get('goal_version'):
        recovery = {}
    cause = recovery.get('failure_cause') or {}
    repeats = max(_count(recovery.get('repeated_failure_count')),
                  _count(cause.get('attempts')) if isinstance(cause, dict) else 0)
    after_diagnosis = isinstance(role, dict) and bool(role.get('after_diagnosis'))
    if name == 'diagnostician' or repeats >= 3:
        level, reason = 'deep', 'Investigate a blocker or repeated failure with bounded evidence checks.'
    elif name == 'reviewer' or recovery or after_diagnosis:
        level, reason = 'focused', 'Check a claimed result or implement the next evidence-based repair.'
    else:
        level, reason = 'routine', 'Take the next small action and inspect its result.'
    manual = profile.get('reasoning', 'auto') != 'auto'
    cap = LEVELS[level]['output_cap']
    return {
        'level': level, 'role': name, 'goal_version': task.get('goal_version'),
        'repeated_failure_count': repeats, 'reason': reason,
        'working_horizon': 'current assigned step or review scope',
        'hypothesis_limit': LEVELS[level]['hypothesis_limit'],
        'output_limit': ceiling if manual or cap is None else min(ceiling, cap),
        'configured_output_ceiling': ceiling,
        'reasoning_policy': 'preserve_user' if manual else 'advertised_values_only',
        'native_reasoning_effort': None if manual else _native_effort(profile, level),
        'limitation': 'Workflow and response size change; the underlying model capability is unchanged.',
    }


def effort_prompt(plan):
    """Bound deliberation without replacing the assigned role or required checks."""
    level = plan['level']
    if level not in LEVELS:
        raise ValueError('Unknown effort level')
    common = (
        f'\nSESSION EFFORT: {level}. Stay within the assigned role, current step and requested review scope. '
        'Keep decisions concise, act on observed evidence, and preserve required acceptance checks. '
        f'Keep each response within {plan["output_limit"]} output tokens by splitting large edits and reports. '
        'Use the already loaded model; do not load, route to, or run another model. '
    )
    if level == 'routine':
        return common + (
            'Choose one concrete next action, perform it and inspect its result before planning further. '
            'If it fails, preserve the exact evidence instead of repeating an unchanged action.\n')
    if level == 'focused':
        return common + (
            'Compare at most two plausible approaches for the current uncertainty, then select the '
            'smallest useful check. Verify the result and record the next action or unresolved obstacle.\n')
    conclusion = (
        'After checking the assumptions, implement the chosen repair and verify the assigned step. '
        if plan.get('role') == 'executor' else
        'Finish with the requested evidence-based acceptance verdict. '
        if plan.get('role') == 'reviewer' else
        'End this investigation when a repair is supported or the remaining uncertainty is identified; '
        'save that evidence for the executor. '
    )
    return common + (
        'Separate observed facts from hypotheses. Compare at most three explanations for the blocker, '
        'choose bounded checks that distinguish them, and inspect their results before another attempt. '
        + conclusion + 'Do not expand to the whole project or skip required checks.\n')


def provider_options(plan):
    """Return only a confirmed native hint, never a model identifier or user override."""
    value = plan.get('native_reasoning_effort')
    if plan.get('reasoning_policy') != 'advertised_values_only' or value not in NATIVE_EFFORTS:
        return {}
    # OpenCode/AI SDK uses camelCase and serializes reasoning_effort on the wire.
    return {'reasoningEffort': value}


def session_effort(task, ready, review=False):
    from .session_roles import session_role
    if not task.get('adaptive_effort', True) or not task.get('auto_tune', True):
        return None
    profile = dict(task.get('resolved_profile') or task['profile'])
    profile['context'] = min(profile['context'], ready.get('context', profile['context']))
    return effort_plan(task, session_role(task, review=review), profile)
