"""Typed model interface to the versioned task plan."""

PROTOCOL = (
    'PROGRESS PROTOCOL: the supervisor owns the plan in its database. PROGRESS.md is a generated '
    'read-only view, not a handoff file. Never edit, delete, summarize or rewrite it with file/shell tools; '
    'those changes are discarded and cannot update progress. Call agentvisor_process_get_progress '
    'to get goal_version, revision and stable step IDs. Only the executor may call '
    'agentvisor_process_update_progress, with goal_version and expected_revision from that response. '
    'Operations: initialize(steps=[plain one-line criterion texts]) only when there is no plan or '
    'the USER changed the goal version; append(steps=[texts]) adds criteria at the end; '
    'claim(step_id,note) records actual verification and requests independent review; '
    'reopen(step_id,note) withdraws an unaccepted claim; note(note) saves current blockers/next action. '
    'Never send the entire plan to update one step. Existing criteria and IDs cannot be deleted, '
    'renamed, reordered or weakened. Accepted steps cannot be changed by the model. On a stale '
    'revision, get_progress and retry; do not fall back to file edits. A claim is not acceptance. '
    'Diagnostic handoffs belong in MEMORY.md; the reviewer only submits its report. '
)


def schemas():
    return [
        {'name': 'get_progress', 'description':
         'Read the authoritative plan with revision, stable step IDs and review status. '
         'Use it instead of inferring progress from notes or a summary.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'update_progress', 'description': PROTOCOL,
         'inputSchema': {'type': 'object', 'properties': {
             'goal_version': {'type': 'integer', 'minimum': 1},
             'expected_revision': {'type': 'integer', 'minimum': 1},
             'operation': {'type': 'string', 'enum': ['initialize', 'append', 'claim', 'reopen', 'note']},
             'steps': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                       'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
             'step_id': {'type': 'string'}, 'note': {'type': 'string', 'minLength': 1, 'maxLength': 16000}},
             'required': ['goal_version', 'expected_revision', 'operation'], 'additionalProperties': False}},
    ]


def call(store, session, name, arguments):
    from .progress_plan import change, initialize, sync_document, view
    current = store.get(session['id'])
    if current.get('progress_plan') is None:
        current = initialize(store, current)
    if name == 'get_progress':
        if arguments:
            raise ValueError('get_progress takes no arguments')
        return view(sync_document(store, current))
    return change(store, session, arguments)
