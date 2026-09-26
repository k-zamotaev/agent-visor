"""Typed review submission using evidence observed in this exact reviewer session."""
import json

from .step_acceptance import observed_evidence, validate_review
from .tasks import document_path, write_document


def schemas():
    return [
        {'name': 'review_evidence', 'description':
         'List successful checks observed in THIS review, with event IDs and output. '
         'Use these IDs in submit_review; old results and task-state files are excluded.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'submit_review', 'description':
         'Finish the current milestone review. The supervisor fills review/goal/step IDs, '
         'validates fresh evidence and writes STEP_REVIEW.json. Fix reported errors before ending. '
         'A negative verdict needs an exact failed criterion. Never claim success without evidence.',
         'inputSchema': {'type': 'object', 'properties': {
             'passed': {'type': 'boolean'},
             'summary': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
             'evidence': {'type': 'array', 'maxItems': 20, 'items': {
                 'type': 'object', 'properties': {
                     'event_id': {'type': 'integer'},
                     'finding': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
                 'required': ['event_id', 'finding'], 'additionalProperties': False}}},
             'required': ['passed', 'summary', 'evidence'], 'additionalProperties': False}},
    ]


def call(store, task, name, arguments):
    review = task.get('review_request')
    if not task.get('review_phase') or not review or len(review['steps']) != 1:
        raise ValueError('Review tools are available only in the active milestone review')
    latest = store.get(task['id'])
    if (latest['goal_version'] != task['goal_version'] or
            latest.get('context_version', 0) != task.get('context_version', 0)):
        raise ValueError('Goal or user context changed during review; a fresh review is required')
    observed = observed_evidence(store, task, task['review_cursor'])
    if name == 'review_evidence':
        ids = sorted(set(observed.values()))[-60:]
        if not ids:
            return {'evidence': [], 'instruction': 'Run a fresh meaningful check first.'}
        with store.connect() as db:
            rows = db.execute('SELECT id,kind,message,data FROM events WHERE task_id=? AND id IN (' +
                              ','.join('?' for _ in ids) + ') ORDER BY id', (task['id'], *ids)).fetchall()
        return {'evidence': [{'event_id': row['id'], 'check': row['message'][:500],
                             'output': str(json.loads(row['data']).get('output') or '')[-1000:]}
                            for row in rows]}
    if name != 'submit_review':
        raise ValueError('Unknown review tool')
    if (type(arguments.get('passed')) is not bool or
            not isinstance(arguments.get('summary'), str) or not arguments['summary'].strip() or
            len(arguments['summary']) > 1000 or not isinstance(arguments.get('evidence'), list) or
            len(arguments['evidence']) > 20):
        raise ValueError('Required: passed boolean, nonempty summary (up to 1000 characters), evidence list (up to 20)')
    report = {'review_id': review['id'], 'goal_version': task['goal_version'], 'steps': [
        dict(arguments, id=review['steps'][0]['id'])]}
    _, error = validate_review(task, review, observed, report=report)
    if error and not error.startswith('Milestone rejected:'):
        raise ValueError(error)
    # File writing and readback belong to the harness, not to shell quoting or the model.
    write_document(task, 'STEP_REVIEW.json', json.dumps(report, ensure_ascii=False, indent=2))
    if json.loads(document_path(task, 'STEP_REVIEW.json').read_text(encoding='utf-8')) != report:
        raise ValueError('Review report readback failed')
    store.event(task['id'], 'review_submitted', 'Отчёт приёмки сохранён', data={
        'review_id': review['id'], 'passed': arguments['passed']})
    return {'status': 'submitted', 'passed': arguments['passed'],
            'report_path': str(document_path(task, 'STEP_REVIEW.json')),
            'instruction': 'Report saved and checked. End this reviewer session now.'}
