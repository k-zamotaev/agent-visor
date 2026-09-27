"""Persist controlled session boundaries without resetting work or retry budgets."""
import hashlib
import json
from pathlib import PurePosixPath

from .recovery import record_recovery
from .step_acceptance import accepted_steps
from .session_progress import is_verification_command


HANDOFFS = {'context_handoff', 'context_blocked', 'work_stalled'}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _facts(task, result):
    memory = task.get('task_memory') or {}
    facts = {_fingerprint(['accepted', key]) for key in accepted_steps(task)}
    for entry in memory.get('changed_files', []):
        if PurePosixPath(entry['path'].replace('\\', '/')).name.lower() in {
                'memory.md', 'progress.md', 'goal.md', 'run_prompt.md', 'done.md', 'step_review.json'}:
            continue
        facts.add(_fingerprint(['file', entry['path'], entry.get('exists'),
                               entry.get('sha256') or entry.get('size')]))
    for entry in memory.get('checks', []):
        if not entry.get('is_verification_result') and not is_verification_command(entry.get('command', '')):
            continue
        # Durations and volatile stdout do not turn a repeated check into progress.
        facts.add(_fingerprint(['check', entry.get('key'), entry.get('outcome'), entry.get('exit_code')]))
    for entry in (result.get('session_progress') or {}).get('result_evidence', []):
        if entry.get('operation', '').startswith('check '):
            facts.add(entry['fingerprint'])
    return facts


def note_completed_session(store, task, result):
    """Keep the bounded recovery cycle across an ordinary diagnostic return."""
    state = task.get('session_continuation')
    if not state or result.get('reason') in HANDOFFS:
        return task
    state = dict(state)
    facts = _facts(task, result)
    seen = set(state.get('seen', []))
    if facts - seen:
        state.update(without_result=0, diagnosed=False, diagnosis_requested=False,
                     seen=(list(state.get('seen', [])) + sorted(facts - seen))[-128:])
    elif state.get('diagnosis_requested') and (task.get('active_role') or {}).get('name') == 'diagnostician':
        state['diagnosed'] = True
    return store.update(task['id'], session_continuation=state)


def continue_session(store, task, result):
    """Return (fresh task, action): continue, diagnose, or blocked.

    Context rotation is not a failed milestone. Only repeated rotations with no
    new artifact/check/acceptance evidence escalate. One diagnostic session and
    an executor attempt follow before another unchanged cycle can be blocked.
    """
    previous = task.get('session_continuation') or {}
    memory = task.get('task_memory') or {}
    scope = [task['goal_version'], task.get('context_version', 0),
             (memory.get('current_step') or {}).get('id')]
    if previous.get('scope') != scope:
        previous = {}
    seen = set(previous.get('seen', []))
    facts = _facts(task, result)
    advanced = bool(facts - seen)
    stagnant = 0 if advanced else previous.get('without_result', 0) + 1
    diagnosed = False if advanced else previous.get('diagnosed', False)
    role = (task.get('active_role') or {}).get('name')
    if role == 'diagnostician' and previous.get('diagnosis_requested'):
        diagnosed = True
    blocked = result['reason'] == 'context_blocked' or (diagnosed and stagnant >= 5)
    diagnose = not blocked and stagnant >= 3 and not diagnosed
    state = {'scope': scope, 'without_result': stagnant,
             'diagnosis_requested': diagnose or previous.get('diagnosis_requested', False) and not advanced,
             'diagnosed': diagnosed, 'last_reason': result['reason'],
             'rotations': previous.get('rotations', 0) + 1,
             'seen': (list(previous.get('seen', [])) + sorted(facts - seen))[-128:]}
    task = store.update(task['id'], session_continuation=state)
    evidence = (result.get('session_handoff') or {}).get('evidence', {})
    message = ('Initial instructions/tools exceed the input budget; reduce the connected tool/schema '
               'overhead or use a supported context size. Retrying unchanged cannot help.'
               if result['reason'] == 'context_blocked' else
               'Continue from TASK HANDOFF MEMORY: reuse observed paths and checks, make one bounded '
               'implementation or targeted check. Do not repeat the whole-project survey. '
               'Read large files in small relevant ranges and verify the edited path before expanding scope.')
    task = record_recovery(store, task, result, message, repair=diagnose, layer='session')
    recovery = dict(task['recovery_context'], session_handoff=True, boundary=evidence,
                    repair=diagnose, no_result_sessions=stagnant)
    task = store.update(task['id'], recovery_context=recovery)
    action = 'blocked' if blocked else 'diagnose' if diagnose else 'continue'
    store.event(task['id'], 'session_continued',
                'Состояние сессии сохранено; продолжение учитывает предыдущие результаты.',
                data={'action': action, 'new_evidence': advanced, 'without_result': stagnant,
                      'reason': result['reason'], 'rotations': state['rotations']})
    return task, action
