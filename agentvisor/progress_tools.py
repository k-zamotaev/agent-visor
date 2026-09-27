"""Typed model interface to the versioned task plan."""

PROTOCOL = (
    'PROGRESS PROTOCOL: the supervisor owns the plan in its database. PROGRESS.md is a generated '
    'read-only view, not a handoff file. Never edit, delete, summarize or rewrite it with file/shell tools; '
    'those changes are discarded and cannot update progress. Call agentvisor_process_get_progress '
    'to get goal_version, revision and stable step IDs. Its default snapshot focuses on the current '
    'and next step; request step_ids for other exact criteria/notes, or full=true for the whole plan. '
    'Only the executor may call '
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
    from .user_instructions import schema
    return [
        {'name': 'get_progress', 'description':
         'Read authoritative revision, counts, current/next criteria and all user instructions. '
         'Default omits unrelated steps and bounds notes to save context. Request step_ids for exact '
         'details of selected steps; full=true returns the entire plan and all notes. '
         'Use it instead of inferring progress from a summary.',
         'inputSchema': {'type': 'object', 'properties': {
             'full': {'type': 'boolean', 'description': 'Return the entire plan with unabridged notes.'},
             'step_ids': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'uniqueItems': True,
                          'items': {'type': 'string', 'minLength': 1},
                          'description': 'Return exact selected criteria and notes instead of the default focus.'}},
             'additionalProperties': False}},
        {'name': 'update_progress', 'description': PROTOCOL,
         'inputSchema': {'type': 'object', 'properties': {
             'goal_version': {'type': 'integer', 'minimum': 1},
             'expected_revision': {'type': 'integer', 'minimum': 1},
             'operation': {'type': 'string', 'enum': ['initialize', 'append', 'claim', 'reopen', 'note']},
             'steps': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                       'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
             'step_id': {'type': 'string'}, 'note': {'type': 'string', 'minLength': 1, 'maxLength': 16000}},
             'required': ['goal_version', 'expected_revision', 'operation'], 'additionalProperties': False}},
        schema(),
    ]


def snapshot(state, arguments=None):
    """MCP-only projection: saved plan, UI and acceptance evidence stay intact."""
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, dict) or set(arguments) - {'full', 'step_ids'}:
        raise ValueError('get_progress accepts only full or step_ids')
    if 'full' in arguments and type(arguments['full']) is not bool:
        raise ValueError('full must be a boolean')
    identities = arguments.get('step_ids')
    if 'step_ids' in arguments and (not isinstance(identities, list) or not 1 <= len(identities) <= 200 or
            any(not isinstance(value, str) or not value for value in identities) or
            len(set(identities)) != len(identities)):
        raise ValueError('step_ids must contain 1 to 200 unique stable IDs')
    if arguments.get('full') and identities:
        raise ValueError('Use either full=true or step_ids, not both')
    steps = state['steps']
    if identities and not set(identities).issubset({step['id'] for step in steps}):
        raise ValueError('Unknown step_id. Use get_progress full=true to inspect the current plan IDs')
    counts = {key: sum(step['review_status'] == key for step in steps) for key in ('accepted', 'pending', 'open')}
    counts['total'] = len(steps)
    current_goal = state['goal_version'] == state['plan_goal_version']
    # Claims await independent review before the executor takes another step.
    remaining = ([step for step in steps if step['review_status'] == 'pending'] +
                 [step for step in steps if step['review_status'] == 'open']) if current_goal else []
    current, following = (remaining + [None, None])[:2]
    focus = {step['id'] for step in (current, following) if step}
    scope = 'full' if arguments.get('full') else 'selected' if identities else 'focus'
    selected = set(identities or []) if scope == 'selected' else focus
    result = dict(state, scope=scope, counts=counts, needs_initialization=not current_goal or not steps,
                  current_step_id=current['id'] if current else None,
                  next_step_id=following['id'] if following else None)
    result['steps'] = ([dict(step) for step in remaining[:2]] if scope == 'focus' else
                       [dict(step) for step in steps if scope == 'full' or step['id'] in selected])
    result['omitted_steps'] = len(steps) - len(result['steps'])
    if scope == 'focus':
        for step in result['steps']:
            if len(step.get('note', '')) > 1000:
                step['note'] = step['note'][:1000]
                step['note_truncated'] = True
        if len(result.get('notes', '')) > 1500:
            result['notes'] = result['notes'][:1500]
            result['notes_truncated'] = True
    if scope != 'full':
        result['details_hint'] = 'Use step_ids for exact step notes; full=true for the whole plan and all notes.'
    return result


def call(store, session, name, arguments):
    from .progress_plan import change, initialize, sync_document, view
    current = store.get(session['id'])
    if current.get('progress_plan') is None:
        current = initialize(store, current)
    if name == 'get_progress':
        return snapshot(view(sync_document(store, current)), arguments)
    before = {step['id']: step for step in view(current)['steps']}
    if name == 'apply_user_instructions':
        from .user_instructions import apply
        updated = apply(store, session, arguments)
    else:
        updated = change(store, session, arguments)
    result = snapshot(updated)
    result['changed_step_ids'] = [step['id'] for step in updated['steps'] if before.get(step['id']) != step]
    return result
