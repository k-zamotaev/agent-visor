"""User directives remain pending until linked atomically to reviewable plan steps."""
import copy


def statuses(task):
    from .step_acceptance import accepted_steps
    plan = task.get('progress_plan') or {}
    current = plan.get('goal_version') == task['goal_version']
    links = plan.get('context_links', {}) if current else {}
    steps = {step['id']: step for step in plan.get('steps', [])} if current else {}
    accepted = accepted_steps(task)
    result = []
    for item in task.get('context_additions', []):
        kind = item.get('kind', 'instruction')
        ids = links.get(str(item['version']), [])
        if kind == 'reference':
            state = 'delivered' if item['version'] <= task.get('applied_context_version', 0) else 'queued'
        elif not ids or any(identity not in steps for identity in ids):
            state = 'pending'
        elif all(identity in accepted for identity in ids):
            state = 'verified'
        elif all(steps[identity]['done'] for identity in ids):
            state = 'review_pending' if task.get('step_acceptance', True) else 'claimed'
        else:
            state = 'planned'
        result.append(dict(item, kind=kind, state=state, step_ids=ids if state != 'pending' else []))
    return result


def pending(task):
    return [item for item in statuses(task) if item['state'] == 'pending']


def requirements_for(task, identity):
    criteria = {step['id']: step['text'] for step in (task.get('progress_plan') or {}).get('steps', [])}
    result = []
    for item in statuses(task):
        if identity in item['step_ids'] and item['kind'] == 'instruction':
            result.append({'version': item['version'], 'text': item['text'],
                           'related_steps': [{'id': key, 'text': criteria[key]} for key in item['step_ids']]})
    return result


PROTOCOL = (
    'USER INSTRUCTIONS: delivery and reasoning are not execution. Before ordinary project work, '
    'resolve pending user directives using agentvisor_process_apply_user_instructions. '
    'Call get_progress for the current revision and exact numbered directives. Translate each '
    'directive into concrete implementation/verification criteria, not a promise to read or '
    'add a plan. For a design request, plan the actual design and usability work and browser '
    'verification, not merely creating a checklist item. Preserve the full intent and constraints. '
    'Combine repeated equivalent directives into one set of criteria with all context_versions. '
    'Use existing_step_ids only when those unaccepted criteria genuinely cover the whole directive. '
    'The supervisor retains the original wording for independent review. You cannot dismiss a '
    'directive as reference data, declare it already fulfilled by an accepted milestone, or '
    'finish the task while directives remain unplanned. Reference-only notes are explicitly '
    'selected by the user; they remain standing context. '
)


def schema():
    return {'name': 'apply_user_instructions', 'description': PROTOCOL,
            'inputSchema': {'type': 'object', 'properties': {
                'goal_version': {'type': 'integer', 'minimum': 1},
                'expected_revision': {'type': 'integer', 'minimum': 1},
                'context_versions': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1},
                                     'minItems': 1, 'maxItems': 40, 'uniqueItems': True},
                'steps': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                          'maxItems': 20},
                'existing_step_ids': {'type': 'array', 'items': {'type': 'string'},
                                     'maxItems': 20, 'uniqueItems': True}},
                'required': ['goal_version', 'expected_revision', 'context_versions', 'steps', 'existing_step_ids'],
                'additionalProperties': False}}


def apply(store, session, arguments):
    from .progress_plan import ROWS, sync_document, text, view
    from .step_acceptance import accepted_steps, step_id
    if session.get('review_phase'):
        raise ValueError('The reviewer cannot integrate new instructions during acceptance')
    fields = set(schema()['inputSchema']['required'])
    if not isinstance(arguments, dict) or set(arguments) != fields:
        raise ValueError('Supply exactly: ' + ', '.join(sorted(fields)))
    versions, values, identities = (arguments[key] for key in ('context_versions', 'steps', 'existing_step_ids'))
    if (not isinstance(versions, list) or not 1 <= len(versions) <= 40 or
            any(type(value) is not int for value in versions) or len(set(versions)) != len(versions)):
        raise ValueError('context_versions must contain unique integer versions from pending instructions')
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError('steps must be a list of at most 20 concrete criteria')
    if (not isinstance(identities, list) or len(identities) > 20 or
            any(not isinstance(identity, str) for identity in identities) or len(set(identities)) != len(identities)):
        raise ValueError('existing_step_ids must be a list of at most 20 unique IDs')
    if not values and not identities:
        raise ValueError('An instruction needs at least one concrete plan step; acknowledgement alone is insufficient')

    def update(current):
        if (type(arguments['goal_version']) is not int or
                session['goal_version'] != current['goal_version'] or
                arguments['goal_version'] != current['goal_version'] or
                session.get('review_revision', 0) != current.get('review_revision', 0)):
            raise ValueError('Goal or review scope changed. End the old session and read the current plan')
        plan = copy.deepcopy(current['progress_plan'])
        if plan['goal_version'] != current['goal_version']:
            raise ValueError('Initialize the plan for the new user goal before integrating instructions')
        if type(arguments['expected_revision']) is not int or arguments['expected_revision'] != plan['revision']:
            raise ValueError('Stale plan revision. Call get_progress and retry')
        available = {item['version'] for item in pending(current)}
        if not set(versions).issubset(available):
            raise ValueError('An instruction is unknown, reference-only or already integrated. Call get_progress')
        steps = {step['id']: step for step in plan['steps']}
        accepted = accepted_steps(current)
        if any(identity not in steps or identity in accepted for identity in identities):
            raise ValueError('Link only existing unaccepted steps; accepted work cannot satisfy a new directive')
        if len(plan['steps']) + len(values) > 200:
            raise ValueError('The plan cannot contain more than 200 steps')
        selected = list(identities)
        known_text = {step['text'] for step in plan['steps']}
        for value in values:
            value = text(value)
            if '\n' in value or '\r' in value or value in known_text or ROWS.match(value):
                raise ValueError('New criteria must be unique plain one-line texts; link existing IDs for duplicates')
            identity = step_id(len(plan['steps']), value)
            plan['steps'].append({'id': identity, 'text': value, 'done': False, 'note': ''})
            known_text.add(value)
            selected.append(identity)
        # Newly attached requirements need a fresh worker claim, even if the
        # old criterion was already pending review when the user clarified it.
        for identity in identities:
            steps[identity]['done'] = False
        for version in versions:
            plan.setdefault('context_links', {})[str(version)] = selected
        plan.pop('imported_document', None)
        return plan

    current = store.change_progress(session['id'], update, 'apply_user_instructions')
    store.event(session['id'], 'instructions_planned', 'Указания пользователя внесены в план',
                data={'versions': versions, 'revision': current['progress_plan']['revision'],
                      'step_ids': current['progress_plan']['context_links'][str(versions[0])]})
    return view(sync_document(store, current))


def gate_request(body, task, *, reviewer=False):
    """Expose only planning tools while a directive has no durable plan effect."""
    if reviewer or not body.get('tools') or task.get('progress_plan') is None or not pending(task):
        return
    plan = task['progress_plan']
    allowed = {'agentvisor_process_get_progress', 'agentvisor_process_apply_user_instructions'}
    # A new goal or a new task must first create its initial plan.
    if not plan['steps'] or plan['goal_version'] != task['goal_version']:
        allowed.add('agentvisor_process_update_progress')
    available = [tool for tool in body['tools'] if tool.get('function', {}).get('name') in allowed]
    if not available:
        raise ValueError('Pending user instructions require the AgentVisor progress MCP tools')
    body['tools'] = available
    body['tool_choice'] = 'required'
