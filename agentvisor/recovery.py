"""Persist progress and bounded diagnostics across retries and app restarts."""
from .tasks import checklist


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


def record_recovery(store, task, result=None, error='', repair=False):
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
    }
    history.append(entry)
    context = dict(entry, goal_version=task['goal_version'],
                   attempts=previous.get('attempts', 0) + 1,
                   repair=repair or previous.get('repair', False), history=history[-5:],
                   next_step=next((item['text'][:1500] for item in checklist(task) if not item['done']), ''),
                   stalled_iterations=task.get('progress_watch', {}).get('stalls', 0))
    return store.update(task['id'], recovery_context=context)
