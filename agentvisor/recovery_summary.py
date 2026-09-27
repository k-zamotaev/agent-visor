"""Small prompt projections of the complete recovery audit kept in the store."""
import json
import math
import re


MAX_CONTEXT = 6000
_NUMBERS = ('goal_version', 'iteration', 'exit_code', 'attempts', 'repeated_failure_count',
            'step_index', 'stalled_iterations', 'no_result_sessions', 'rotations')
_REFERENCES = ('event_id', 'first_event_id', 'last_event_id', 'review_cursor', 'event_cursor',
               'call_id', 'process_id', 'fingerprint')
_BOUNDARY_COUNTS = ('context_limit', 'output_reserve', 'safety_reserve', 'input_limit',
                    'estimated_tokens', 'static_tokens', 'raw_estimated_tokens', 'prompt_bytes',
                    'static_bytes', 'message_count', 'tool_count', 'calibration', 'headroom_tokens',
                    'static_headroom_tokens', 'image_tokens_estimate', 'uncertain_images',
                    'seconds_without_result', 'operations', 'repeats')
_SECRET = re.compile(
    r'(?i)(\b(?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret|token)'
    r'[\"\x27]?\s*[:=]\s*)(?:[\"\x27][^\"\x27\r\n]*[\"\x27]|[^\s,;}]+)')


def _text(value, budget=200):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', value)
    value = _SECRET.sub(r'\1[redacted]', value)
    value = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', value)
    value = re.sub(r'(?i)(--?(?:api[_-]?key|access[_-]?token|password|passwd|secret|token)\s+)'
                   r'(?:"[^"\r\n]*"|\x27[^\x27\r\n]*\x27|\S+)', r'\1[redacted]', value)
    value = value[:budget]
    # Bound escaped JSON too: Cyrillic, controls and backslashes are not free.
    while len(json.dumps(value)) > budget:
        value = value[:len(value) * 3 // 4]
    return value


def _numbers(source, names):
    return {key: source[key] for key in names if key in source and (
        type(source[key]) is int and abs(source[key]) <= 2**63 or
        type(source[key]) is float and math.isfinite(source[key]) and abs(source[key]) <= 2**63)}


def _references(source):
    result = _numbers(source, _REFERENCES)
    for key in _REFERENCES:
        value = source.get(key)
        # Reference identifiers must remain exact or be omitted, never shortened.
        if isinstance(value, str) and re.fullmatch(r'[\w:./-]{1,80}', value, re.ASCII):
            result[key] = value
    values = source.get('event_ids')
    if isinstance(values, list):
        result['event_ids'] = [value for value in values[-8:] if type(value) is int and 0 < value <= 2**63]
    return result


def _cause(value):
    if not isinstance(value, dict):
        return {}
    result = _numbers(value, ('attempts', 'iteration'))
    for key, size in (('key', 80), ('category', 80), ('detail', 350), ('strategy', 50)):
        if isinstance(value.get(key), str):
            result[key] = _text(value[key], size)
    return result


def _history(value):
    if not isinstance(value, list):
        return [], 0
    selected = [item for item in value if isinstance(item, dict)]
    result = []
    for item in selected[-3:]:
        entry = dict(_numbers(item, ('iteration', 'exit_code', 'repeated_failure_count')),
                     **_references(item))
        for key, budget in (('reason', 80), ('failure_layer', 40), ('error', 120)):
            if isinstance(item.get(key), str):
                entry[key] = _text(item[key], budget)
        cause = item.get('failure_cause')
        if isinstance(cause, dict):
            entry['cause_category'] = _text(cause.get('category'), 60)
        result.append(entry)
    return result, max(0, len(selected) - len(result))


def _tools(value, *, pending=False):
    if not isinstance(value, list):
        return []
    result = []
    for item in [entry for entry in value if isinstance(entry, dict)][-2:]:
        entry = dict(_references(item), **_numbers(item, ('exit_code', 'timeout', 'duration')))
        for key, budget in (('tool', 80), ('name', 80), ('status', 40), ('command', 200),
                            ('error', 180), ('output', 180)):
            if key in item and not (pending and key in {'error', 'output'}):
                entry[key] = _text(item[key], budget)
        arguments = item.get('input')
        if isinstance(arguments, dict):
            entry['input'] = {key: _text(arguments[key], budget) for key, budget in (
                ('command', 200), ('cwd', 120), ('filePath', 180), ('path', 180), ('operation', 40))
                if isinstance(arguments.get(key), str)}
            if set(arguments) - set(entry['input']):
                entry['input_omitted'] = True
        elif arguments is not None:
            entry['input_omitted'] = True
        result.append(entry)
    return result


def _boundary(value):
    if not isinstance(value, dict):
        return {}
    result = dict(_numbers(value, _BOUNDARY_COUNTS), **_references(value))
    for key in ('cause', 'reason', 'method'):
        if key in value:
            result[key] = _text(value[key], 100)
    if type(value.get('auxiliary_request')) is bool:
        result['auxiliary_request'] = value['auxiliary_request']
    evidence = value.get('evidence')
    if isinstance(evidence, list):
        result['evidence'] = [dict(_references(item), **_numbers(item, ('count',)),
                                   operation=_text(item.get('operation'), 120))
                              for item in evidence[:2] if isinstance(item, dict)]
    return result


def prompt_context(recovery):
    """Return <=6000 JSON characters without mutating the durable recovery audit.

    This is diagnostic data, not instructions, authorization or acceptance.
    Full logs, arbitrary previews, source dumps and nested histories stay in DB.
    """
    if not isinstance(recovery, dict) or not recovery:
        return {}
    result = dict(_numbers(recovery, _NUMBERS), **_references(recovery), summary_only=True)
    for key, budget in (('reason', 120), ('failure_layer', 40), ('error', 500),
                        ('next_step', 600), ('last_event', 80), ('last_tool', 150)):
        if key in recovery:
            result[key] = _text(recovery[key], budget)
    for key in ('session_handoff', 'repair'):
        if type(recovery.get(key)) is bool:
            result[key] = recovery[key]
    if recovery.get('failure_cause'):
        result['failure_cause'] = _cause(recovery['failure_cause'])
    if recovery.get('boundary'):
        result['boundary'] = _boundary(recovery['boundary'])
    result['history'], result['omitted_history'] = _history(recovery.get('history'))
    result['tool_failures'] = _tools(recovery.get('tool_failures'))
    result['pending_tools'] = _tools(recovery.get('pending_tools'), pending=True)
    result['full_details'] = 'Original recovery_context and referenced events remain in the task store.'
    # Fields have individual caps; this final cap also covers pathological
    # combinations of populated optional fields and escaped identifiers.
    while len(json.dumps(result)) > MAX_CONTEXT:
        if result['pending_tools']:
            result['pending_tools'].pop(0)
        elif len(result['tool_failures']) > 1:
            result['tool_failures'].pop(0)
        elif result['history']:
            result['history'].pop(0)
            result['omitted_history'] += 1
        elif result['tool_failures']:
            result['tool_failures'].pop(0)
        elif (result.get('boundary') or {}).get('evidence'):
            result['boundary']['evidence'].pop(0)
            result['omitted_boundary_evidence'] = result.get('omitted_boundary_evidence', 0) + 1
        else:
            # Only bounded canonical descriptors remain. Retain them while
            # shortening verbose text, never IDs or numerical observations.
            for key in ('error', 'next_step', 'last_tool'):
                if isinstance(result.get(key), str):
                    result[key] = _text(result[key], 100)
            if isinstance(result.get('failure_cause'), dict):
                result['failure_cause']['detail'] = _text(result['failure_cause'].get('detail'), 100)
            boundary = result.get('boundary') or {}
            result['boundary'] = {key: boundary[key] for key in (
                *_REFERENCES, 'event_ids', 'context_limit', 'input_limit', 'estimated_tokens',
                'operations', 'repeats', 'seconds_without_result') if key in boundary}
            break
    return result
