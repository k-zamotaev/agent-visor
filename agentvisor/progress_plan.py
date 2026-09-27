"""Supervisor-owned plan. Markdown is a recoverable view, never a write API."""
import copy
import hashlib
import re

from .step_acceptance import accepted_steps, step_id

ROWS = re.compile(r'^\s*(?:[-*]|\d+[.)])\s+\[([ xX])\]\s+(.+)$', re.M)


def parse_legacy(content):
    return [{'id': step_id(index, match[2]), 'text': match[2].strip(),
             'done': match[1].lower() == 'x', 'note': ''}
            for index, match in enumerate(ROWS.finditer(content))]


def initialize(store, task):
    """One-time import before the first controlled session, never after file edits."""
    from .tasks import read_document
    def import_plan(current):
        if current.get('progress_plan') is not None:
            return current['progress_plan']
        content = read_document(current, 'PROGRESS.md')
        steps = parse_legacy(content)
        if not steps and accepted_steps(current):
            raise ValueError('Saved milestone receipts exist but the legacy checklist is missing. '
                             'Restore the last full plan before migration; do not infer acceptance from prose.')
        accepted = accepted_steps(current)
        for step in steps:
            step['done'] = step['done'] or step['id'] in accepted
        # The exact import remains in revision 1 even after notes are updated.
        return {'goal_version': current['goal_version'], 'steps': steps,
                'notes': '', 'imported_document': content}
    task = store.change_progress(task['id'], import_plan, 'legacy_import')
    sync_document(store, task)
    return task


def items(task):
    plan = task.get('progress_plan')
    if plan is None:
        return None
    current = plan['goal_version'] == task['goal_version']
    accepted = accepted_steps(task) if current else {}
    return [dict(step, done=current and (step['done'] or step['id'] in accepted))
            for step in plan['steps']]


def view(task):
    from .user_instructions import statuses
    plan = task['progress_plan']
    accepted = accepted_steps(task)
    return {'goal_version': task['goal_version'], 'plan_goal_version': plan['goal_version'],
            'revision': plan['revision'], 'steps': [dict(step, review_status=(
                'accepted' if step['id'] in accepted else 'pending') if step['done'] else 'open')
                for step in items(task)], 'notes': plan.get('notes', ''), 'user_instructions': statuses(task)}


def render(task):
    from .user_instructions import statuses
    plan = task['progress_plan']
    lines = ['# Progress', '', f'goal_version: {plan["goal_version"]}',
             f'plan_revision: {plan["revision"]}', '',
             'Supervisor-managed plan. Use agentvisor_process_get_progress and '
             'agentvisor_process_update_progress; direct file edits are discarded.', '']
    if plan['goal_version'] != task['goal_version']:
        lines += ['The user changed the goal. Initialize a plan for the new goal version.', '']
    for step in items(task):
        lines += [f'- [{"x" if step["done"] else " "}] {step["text"]}', f'  <!-- step_id: {step["id"]} -->']
        if step.get('note'):
            lines += ['  > ' + line for line in step['note'].splitlines()]
    if plan.get('notes'):
        lines += ['', '## Notes'] + ['> ' + line for line in plan['notes'].splitlines()]
    instructions = statuses(task)
    if instructions:
        lines += ['', '## User instructions and reference context']
        for item in instructions:
            lines += [f'### #{item["version"]}: {item["state"]}',
                      'Steps: ' + (', '.join(item['step_ids']) or '—')]
            lines += ['> ' + line for line in item['text'].splitlines()]
    if plan.get('imported_document'):
        lines += ['', 'Previous document preserved in progress-history/ and the first database revision.']
    return '\n'.join(lines) + '\n'


def sync_document(store, task):
    """Repair projections on read/session boundaries without importing worker edits."""
    from .tasks import read_document, state_dir, write_document
    with store.progress_lock:
        current = store.get(task['id'])
        if current.get('progress_plan') is None:
            return current
        expected = render(current)
        actual = read_document(current, 'PROGRESS.md')
        if actual == expected:
            return current
        archived = None
        if actual:
            digest = hashlib.sha256(actual.encode('utf-8')).hexdigest()[:20]
            archive = state_dir(current) / 'progress-history'
            if archive.is_symlink():
                raise ValueError('Progress history directory must not be a symbolic link')
            archive.mkdir(exist_ok=True)
            target = archive / f'{digest}.md'
            if target.is_symlink():
                raise ValueError('Progress history file must not be a symbolic link')
            if not target.exists():
                target.write_text(actual, encoding='utf-8')
            archived = str(target)
        write_document(current, 'PROGRESS.md', expected)
        store.event(current['id'], 'progress_file_restored', 'Файл плана восстановлен из сохранённого состояния',
                    data={'revision': current['progress_plan']['revision'], 'previous_document': archived})
        return current


def text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Expected nonempty text of at most {limit} characters')
    return value.strip()


def change(store, session, arguments):
    from .session_roles import session_role
    from .user_instructions import pending
    current = store.get(session['id'])
    plan = current.get('progress_plan') or {}
    instruction_initialization = (isinstance(arguments, dict) and arguments.get('operation') == 'initialize'
                                  and pending(current) and
                                  (not plan.get('steps') or plan.get('goal_version') != current['goal_version']))
    if session.get('review_phase') or (session_role(session)['name'] != 'executor' and not instruction_initialization):
        raise ValueError('Only the executor may update progress. Use MEMORY.md for diagnostic notes.')
    if not isinstance(arguments, dict):
        raise ValueError('Progress arguments must be an object')
    operation = arguments.get('operation')
    fields = {'initialize': {'steps'}, 'append': {'steps'}, 'claim': {'step_id', 'note'},
              'reopen': {'step_id', 'note'}, 'note': {'note'}}
    if operation not in fields:
        raise ValueError('Use initialize, append, claim, reopen or note. Deletion, renaming and reordering are forbidden.')
    required = {'operation', 'goal_version', 'expected_revision'} | fields[operation]
    if set(arguments) != required:
        raise ValueError('Expected exactly these fields: ' + ', '.join(sorted(required)))

    def update(current):
        plan = copy.deepcopy(current['progress_plan'])
        if (type(arguments['goal_version']) is not int or
                arguments['goal_version'] != current['goal_version'] or
                session['goal_version'] != current['goal_version'] or
                session.get('review_revision', 0) != current.get('review_revision', 0)):
            raise ValueError('Goal or review scope changed. End this session; do not update the old plan.')
        if type(arguments['expected_revision']) is not int or arguments['expected_revision'] != plan['revision']:
            raise ValueError('Stale plan revision. Call get_progress, then retry against its revision.')
        if operation == 'initialize':
            if plan['goal_version'] == current['goal_version'] and plan['steps']:
                raise ValueError('Plan already exists. You may append steps, not replace it.')
            plan = dict(plan, goal_version=current['goal_version'], steps=[], notes='', context_links={})
        elif plan['goal_version'] != current['goal_version']:
            raise ValueError('Initialize a plan for the new goal version first.')
        elif pending(current):
            raise ValueError('Apply pending user instructions with apply_user_instructions before ordinary progress updates')
        if operation in {'initialize', 'append'}:
            values = arguments['steps']
            if not isinstance(values, list) or not 1 <= len(values) <= 100 or len(plan['steps']) + len(values) > 200:
                raise ValueError('Supply 1 to 100 steps, at most 200 in the whole plan.')
            existing = {step['text'] for step in plan['steps']}
            for value in values:
                value = text(value)
                if '\n' in value or '\r' in value or value in existing or ROWS.match(value):
                    raise ValueError('Step text must be unique, one line, without a checkbox prefix.')
                plan['steps'].append({'id': step_id(len(plan['steps']), value), 'text': value, 'done': False, 'note': ''})
                existing.add(value)
        elif operation in {'claim', 'reopen'}:
            selected = next((step for step in plan['steps'] if step['id'] == arguments['step_id']), None)
            if selected is None:
                raise ValueError('Unknown step_id. Call get_progress for the current IDs.')
            if selected['id'] in accepted_steps(current):
                raise ValueError('An accepted step is immutable. Only the user may request a new review scope.')
            selected.update(done=operation == 'claim', note=text(arguments['note'], 8000))
        else:
            plan['notes'] = text(arguments['note'], 16000)
        plan.pop('imported_document', None)
        return plan
    current = store.change_progress(session['id'], update, operation)
    return view(sync_document(store, current))


def mark(store, task, steps, done):
    """Supervisor-only claim rollback after failure or an explicit rejection."""
    selected = {step['id'] for step in steps}
    def update(current):
        if (current['goal_version'] != task['goal_version'] or
                current.get('review_revision', 0) != task.get('review_revision', 0)):
            raise ValueError('Review scope changed')
        plan = copy.deepcopy(current['progress_plan'])
        if plan['goal_version'] != task['goal_version'] or not selected.issubset({s['id'] for s in plan['steps']}):
            raise ValueError('Milestone identities changed')
        accepted = accepted_steps(current)
        for step in plan['steps']:
            if step['id'] in selected:
                step['done'] = done or step['id'] in accepted
        return plan
    try:
        current = store.change_progress(task['id'], update, 'supervisor_mark')
    except ValueError:
        return False
    task.update(progress_plan=current['progress_plan'])
    sync_document(store, current)
    return True
