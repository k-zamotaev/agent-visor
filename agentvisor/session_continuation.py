"""Persist controlled session boundaries without resetting work or retry budgets."""
import hashlib
import json
from pathlib import PurePosixPath

from .recovery import record_recovery
from .step_acceptance import accepted_steps
from .session_progress import is_verification_command
from .task_memory import diagnostic_proposal


HANDOFFS = {'context_handoff', 'context_blocked', 'work_stalled'}
MAX_DIAGNOSTIC_ATTEMPTS = 2


def _diagnostic_result(task, state):
    proposal = diagnostic_proposal(task)
    received = bool(proposal and proposal.get('iteration') == task['iteration'])
    return {'diagnosed': received, 'diagnostic_attempts': state.get('diagnostic_attempts', 0) + 1,
            'diagnostic_report_missing': not received, 'executor_attempts_after_diagnosis': 0}


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
    if state.get('diagnosed') and not diagnostic_proposal(task):
        state.update(diagnosed=False, diagnostic_report_missing=True)
    facts = _facts(task, result)
    seen = set(state.get('seen', []))
    if facts - seen:
        state.update(without_result=0, diagnosed=False, diagnosis_requested=False,
                     diagnostic_attempts=0, diagnostic_report_missing=False,
                     executor_attempts_after_diagnosis=0, diagnostic_fallback=False,
                     seen=(list(state.get('seen', [])) + sorted(facts - seen))[-128:])
    elif state.get('diagnosis_requested') and (task.get('active_role') or {}).get('name') == 'diagnostician':
        state.update(_diagnostic_result(task, state))
    return store.update(task['id'], session_continuation=state)


def release_missing_diagnosis(store, task):
    """Return control to the executor without claiming a diagnosis exists."""
    state = dict(task.get('session_continuation') or {})
    first_fallback = not state.get('diagnostic_fallback', False)
    state.update(diagnosis_requested=False, diagnosed=False,
                 diagnostic_report_missing=True, diagnostic_fallback=True)
    recovery = dict(task.get('recovery_context') or {})
    recovery.update(goal_version=task['goal_version'], repair=False,
                    reason='diagnostic_report_missing', failure_layer='session',
                    error='Diagnostic report was not saved. Continue from recorded tool and event evidence.')
    recovery.pop('failure_cause', None)
    task = store.update(task['id'], session_continuation=state, recovery_context=recovery)
    if first_fallback:
        store.event(task['id'], 'diagnostic_fallback',
                    'Диагностический отчёт не сохранён; исполнитель продолжит по фактам журнала.',
                    'warning', data={'attempts': state.get('diagnostic_attempts', 0)})
    return task


def continue_session(store, task, result, *, context_adjusted=False):
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
    elif previous.get('diagnosed') and not diagnostic_proposal(task):
        previous = dict(previous, diagnosed=False, diagnostic_report_missing=True)
    seen = set(previous.get('seen', []))
    facts = _facts(task, result)
    advanced = bool(facts - seen)
    stagnant = 0 if advanced else previous.get('without_result', 0) + 1
    diagnostic = ({'diagnosed': False, 'diagnostic_attempts': 0, 'diagnostic_report_missing': False,
                   'executor_attempts_after_diagnosis': 0}
                  if advanced else {key: previous.get(key, default) for key, default in
                  (('diagnosed', False), ('diagnostic_attempts', 0), ('diagnostic_report_missing', False),
                   ('executor_attempts_after_diagnosis', 0))})
    role = (task.get('active_role') or {}).get('name')
    if role == 'diagnostician' and previous.get('diagnosis_requested'):
        diagnostic.update(_diagnostic_result(task, previous))
    elif role == 'executor' and diagnostic['diagnosed']:
        diagnostic['executor_attempts_after_diagnosis'] += 1
    missing = not diagnostic['diagnosed'] and diagnostic['diagnostic_attempts'] >= MAX_DIAGNOSTIC_ATTEMPTS
    blocked = result['reason'] == 'context_blocked' and not context_adjusted
    diagnose = not blocked and not missing and stagnant >= 3 and not diagnostic['diagnosed']
    state = {'scope': scope, 'without_result': stagnant,
             'diagnosis_requested': diagnose or previous.get('diagnosis_requested', False) and not advanced,
             **diagnostic, 'last_reason': result['reason'],
             'diagnostic_fallback': previous.get('diagnostic_fallback', False) and not advanced,
             'rotations': previous.get('rotations', 0) + 1,
             'seen': (list(previous.get('seen', [])) + sorted(facts - seen))[-128:]}
    task = store.update(task['id'], session_continuation=state)
    evidence = (result.get('session_handoff') or {}).get('evidence', {})
    message = ('Context budget was increased for the next session; retry at the new size.'
               if context_adjusted else
               'Initial instructions/tools exceed the input budget; reduce the connected tool/schema '
               'overhead or use a supported context size. Retrying unchanged cannot help.'
               if result['reason'] == 'context_blocked' else
               'Continue from TASK HANDOFF MEMORY: reuse observed paths and checks, make one bounded '
               'implementation or targeted check. Do not repeat the whole-project survey. '
               'Read large files in small relevant ranges and verify the edited path before expanding scope.')
    if stagnant >= 5 and not blocked:
        message = ('The current step has not advanced across repeated sessions. Use the saved tool results '
                   'and event IDs; do not repeat the same inputs or survey. Choose a different bounded '
                   'operation that produces a durable task artifact or an acceptance check. If the latest '
                   'tool output is incomplete, inspect only the missing part and save the finding before '
                   'another context handoff. Record the exact obstacle and attempted alternatives.')
    task = record_recovery(store, task, result, message, repair=diagnose, layer='session')
    recovery = dict(task['recovery_context'], session_handoff=True, boundary=evidence,
                    repair=diagnose, no_result_sessions=stagnant,
                    diagnostic_report_missing=missing)
    task = store.update(task['id'], recovery_context=recovery)
    if missing:
        task = release_missing_diagnosis(store, task)
    action = 'blocked' if blocked else 'diagnose' if diagnose else 'continue'
    store.event(task['id'], 'session_continued',
                'Состояние сессии сохранено; продолжение учитывает предыдущие результаты.',
                data={'action': action, 'new_evidence': advanced, 'without_result': stagnant,
                      **diagnostic,
                      'reason': result['reason'], 'rotations': state['rotations']})
    return task, action
