"""Persist progress and bounded diagnostics across retries and app restarts."""
import hashlib
import json
import re

from .tasks import checklist
from .loop_detection import detect_loop


def failure_layer(result=None, error='', preparing=False):
    """Recover the failing component, not every failure by reloading the model."""
    result = result or {}
    if result.get('kind') == 'verify' or result.get('reason') == 'verification_failed':
        return 'verification'
    reason = result.get('reason') or ''
    if reason == 'runtime_unavailable':
        return 'runtime'
    if result.get('pending_tools') or reason in {'tool_timeout', 'tool_failure', 'tool_loop'}:
        return 'tool'
    detail = str(error or result.get('error_detail') or '')
    if re.search(r'exceeds.*context|exceed_context|context_length_exceeded', detail, re.I):
        return 'context'
    if preparing or reason in {'inference_error', 'inference_timeout', 'inference_failed', 'model_error'}:
        return 'runtime'
    if result.get('tool_failures'):
        return 'tool'
    if re.search(r'out of memory|insufficient memory|\boom\b', detail, re.I):
        return 'runtime'
    return 'agent'


def bounded_tools(values):
    """Keep the actionable input and output, without unbounded provider metadata."""
    def bound(value, limit=2000, depth=0):
        if isinstance(value, str):
            return value[-limit:]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 3:
            return str(value)[-limit:]
        if isinstance(value, dict):
            return {str(key)[:100]: bound(item, limit=min(limit, 1000), depth=depth + 1)
                    for key, item in list(value.items())[:12]}
        if isinstance(value, list):
            return [bound(item, limit=min(limit, 1000), depth=depth + 1) for item in value[-4:]]
        return str(value)[-limit:]

    if not isinstance(values, list):
        return []
    fields = ('id', 'call_id', 'tool', 'name', 'command', 'input', 'output', 'error', 'status',
              'exit_code', 'timeout', 'duration', 'source')
    return [{key: bound(item[key]) for key in fields if key in item}
            for item in values[-4:] if isinstance(item, dict)]


def observe_progress(store, task):
    completed = sum(item['done'] for item in checklist(task))
    previous = task.get('progress_watch') or {}
    if previous.get('goal_version') != task['goal_version']:
        previous = {'goal_version': task['goal_version'], 'completed': completed, 'stalls': 0}
    advanced = completed > previous['completed']
    watch = dict(previous, completed=max(completed, previous['completed']),
                 stalls=0 if advanced else previous['stalls'] + 1)
    updates = {'progress_watch': watch}
    if advanced:
        updates['recovery_context'] = None
    return store.update(task['id'], **updates)


def initialize_progress(store, task):
    previous = task.get('progress_watch') or {}
    if previous.get('goal_version') == task['goal_version']:
        return task
    return store.update(task['id'], failure_streak=0, recovery_context=None, progress_watch={
        'goal_version': task['goal_version'],
        'completed': sum(item['done'] for item in checklist(task)), 'stalls': 0})


def record_recovery(store, task, result=None, error='', repair=False, layer=None):
    previous = task.get('recovery_context') or {}
    if previous.get('goal_version') != task['goal_version']:
        previous = {}
    result = result or {}
    history = list(previous.get('history', []))
    entry = {
        'iteration': task['iteration'],
        'reason': result.get('reason') or ('iteration_error' if error else 'no_progress'),
        'error': str(error or result.get('error_detail') or '')[:1500],
        'last_event': result.get('last_event', ''),
        'last_tool': result.get('last_tool', '')[:1000],
        'failure_layer': layer or failure_layer(result, error),
        'exit_code': result.get('exit_code'),
        'output_tail': str(result.get('output_tail') or '')[-4000:],
        'pending_tools': bounded_tools(result.get('pending_tools')),
        'tool_failures': bounded_tools(result.get('tool_failures')),
    }
    signature = {key: entry[key] for key in ('reason', 'failure_layer', 'error', 'last_tool')}
    signature['commands'] = [tool.get('command') or tool.get('input') or tool.get('tool')
                             for tool in entry['pending_tools'] + entry['tool_failures']]
    entry['fingerprint'] = hashlib.sha256(json.dumps(signature, sort_keys=True,
                                                     ensure_ascii=False).encode()).hexdigest()[:16]
    matching = [item for item in history if item.get('fingerprint') == entry['fingerprint']]
    entry['repeated_failure_count'] = 1 + max((item.get('repeated_failure_count', 1)
                                             for item in matching), default=0)
    step = next((item['text'][:1500] for item in checklist(task) if not item['done']), '')
    cause = detect_loop(entry, history, step)
    if cause:
        entry['failure_cause'] = cause
        if cause['attempts'] >= 3:
            repair = True
            store.event(task['id'], 'strategy_change',
                        'Повторяется причина сбоя. Требуется смена подхода.', 'warning', data=cause)
    history.append(entry)
    context = dict(entry, goal_version=task['goal_version'],
                   attempts=previous.get('attempts', 0) + 1,
                   repair=repair or previous.get('repair', False), history=history[-5:],
                   next_step=step,
                   stalled_iterations=task.get('progress_watch', {}).get('stalls', 0))
    return store.update(task['id'], recovery_context=context)
