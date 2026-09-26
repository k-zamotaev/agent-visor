"""Optional source recovery artifacts; a snapshot failure never stops task execution."""
import subprocess
from .checkpoints import fork_checkpoint, save_checkpoint


def checkpoint_after_acceptance(store, task, label='accepted milestone'):
    if not task.get('checkpoints', True) or task['mode'] == 'demo':
        return store.get(task['id'])
    try:
        checkpoint = save_checkpoint(task, label)
        latest = store.get(task['id'])
        if latest['goal_version'] != task['goal_version']:
            return latest
        latest = store.update(task['id'], checkpoint=checkpoint, checkpoint_notice=None,
                              experiment_workspace=None, experiment_error=None)
        store.event(task['id'], 'checkpoint_saved', 'Сохранена контрольная точка исходников', data=checkpoint)
        return latest
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
        store.event(task['id'], 'checkpoint_skipped', 'Контрольная точка недоступна; работа продолжается',
                    'warning', data={'error': str(error)[:500]})
        return store.update(task['id'], checkpoint_notice={'goal_version': task['goal_version'], 'error': str(error)[:500]})


def prepare_checkpoint(store, task):
    if not task.get('checkpoints', True) or task['mode'] == 'demo':
        return task
    if ((task.get('checkpoint') or {}).get('goal_version') != task['goal_version'] and
            (task.get('checkpoint_notice') or {}).get('goal_version') != task['goal_version']):
        task = checkpoint_after_acceptance(store, task, 'initial working tree, not verified')
    recovery = task.get('recovery_context') or {}
    checkpoint = task.get('checkpoint') or {}
    if (checkpoint.get('goal_version') == task['goal_version'] and not task.get('experiment_workspace') and
            recovery.get('goal_version') == task['goal_version'] and
            (recovery.get('failure_cause') or {}).get('strategy') == 'alternative' and
            not task.get('experiment_error')):
        try:
            path = fork_checkpoint(task, checkpoint)
            task = store.update(task['id'], experiment_workspace=path)
            store.event(task['id'], 'checkpoint_experiment', 'Подготовлена отдельная копия для альтернативного решения',
                        data={'path': path, 'checkpoint_id': checkpoint['id']})
        except (OSError, ValueError) as error:
            task = store.update(task['id'], experiment_error=str(error)[:500])
            store.event(task['id'], 'checkpoint_skipped', 'Контрольная точка недоступна; работа продолжается',
                        'warning', data={'error': str(error)[:500]})
    return task
