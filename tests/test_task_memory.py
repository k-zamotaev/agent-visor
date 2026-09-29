import json
from pathlib import Path

from agentvisor.store import Store
from agentvisor.task_memory import initialize_memory, memory_prompt, remember_iteration
from agentvisor.tasks import prepare_documents, write_document
from test_supervisor import make, finish


def test_memory_survives_restart_and_uses_observations_not_notes(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    write_document(task, 'PROGRESS.md', '- [x] Setup\n- [ ] Frontend\n')
    write_document(task, 'MEMORY.md', 'goal_version: 1\nHypothesis: use a different port\n')
    store.event(task['id'], 'command_finished', 'python --version', data={
        'status': 'completed', 'exit_code': 0, 'output': 'Python 3.13',
        'input': {'command': 'python --version', 'cwd': task['workspace']}})
    task = remember_iteration(store, task, {'failed': False, 'exit_code': 0})
    task = Store(tmp_path / 'data').get(task['id'])
    memory = task['task_memory']
    assert memory['next_step'] == 'Frontend'
    assert memory['observations'][0]['output'] == 'Python 3.13'
    assert memory['observations'][0]['event_id'] > 0
    assert 'working_notes' not in memory
    assert 'Hypothesis' not in memory_prompt(task)
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384})
    assert 'TASK HANDOFF MEMORY' in prompt
    assert 'not independently verified facts' in prompt
    assert 'Processes from previous sessions have been stopped' in prompt


def test_optional_tool_output_is_checkpointed_without_becoming_completion(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    store.event(task['id'], 'tool_finished', 'external_inspect', data={
        'tool': 'external_inspect', 'status': 'completed',
        'input': {'resource': 'module-1'}, 'output': 'Lesson A; token=private-value'})
    store.event(task['id'], 'tool_finished', 'external_inspect', data={
        'tool': 'external_inspect', 'status': 'completed',
        'input': {'resource': 'module-1'}, 'output': 'Lesson A; token=another-value'})
    task = remember_iteration(store, task)
    task = Store(tmp_path / 'data').get(task['id'])
    results = task['task_memory']['tool_results']
    assert len(results) == 2
    assert results[0]['event_id'] > 0
    assert 'private-value' not in memory_prompt(task)
    assert 'another-value' not in memory_prompt(task)
    payload = prompt_payload(task)
    assert payload['next_action']['operation'] == 'continue_from_tool_result'
    assert payload['accepted_count'] == 0


def test_tool_handoff_prompt_is_compact_while_checkpoint_keeps_output(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for index in range(3):
        store.event(task['id'], 'tool_finished', 'external_inspect', data={
            'tool': 'external_inspect', 'status': 'completed',
            'input': {'resource': f'page-{index}'}, 'output': f'page-{index}: ' + 'content ' * 450})
    task = remember_iteration(store, task)
    assert len(task['task_memory']['tool_results']) == 3
    assert len(task['task_memory']['tool_results'][-1]['excerpt']) > 1700
    prompt = memory_prompt(task)
    assert len(prompt) < 4500
    payload = prompt_payload(task)
    assert len(payload['tool_results']) == 3
    assert 'excerpt' not in payload['tool_results'][0]
    assert len(payload['tool_results'][-1]['excerpt']) <= 700


def test_new_goal_excludes_old_events_and_old_notes(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    write_document(task, 'MEMORY.md', 'goal_version: 1\nOld goal fact\n')
    store.event(task['id'], 'command_finished', 'old command', data={'exit_code': 0, 'status': 'completed'})
    task = initialize_memory(store, store.update(task['id'], goal_version=2))
    task = remember_iteration(store, task, {'failed': False})
    assert task['task_memory']['observations'] == []
    assert 'working_notes' not in task['task_memory']


def test_latest_result_replaces_stale_success_and_storage_is_bounded(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for iteration in range(10):
        store.event(task['id'], 'command_finished', f'pytest tests/check_{iteration % 6}.py', data={
            'exit_code': iteration % 2, 'status': 'completed', 'output': '\\' * 9000})
        task = remember_iteration(store, store.update(task['id'], iteration=iteration),
                                  {'failed': bool(iteration % 2), 'output_tail': 'x' * 9000})
    memory = task['task_memory']
    assert len(memory['checks']) == 6 and len(memory['attempts']) == 4
    assert memory['checks'][-1]['exit_code'] == 1
    assert len(json.dumps(memory)) < 10000
    assert len(memory_prompt(task)) < 11000
    task = remember_iteration(store, task, {'failed': True})
    assert len(task['task_memory']['checks']) == 6


def test_running_and_cancelled_commands_are_not_success_evidence(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for status in ('running', 'cancelled', 'stopped'):
        store.event(task['id'], 'command_finished', 'server', data={'status': status})
    task = remember_iteration(store, task, {})
    assert not task['task_memory']['observations']


def test_supervisor_captures_handoff_before_next_session(tmp_path):
    store, engine, task = make(tmp_path, 'stall', max_iterations=2)
    prompts = []
    command = engine.command
    def capture(current, prompt, ready):
        prompts.append(prompt)
        return command(current, prompt, ready)
    engine.command = capture
    engine.start(task['id'])
    finish(engine)
    assert '"iteration": 1' in prompts[1]
    assert store.get(task['id'])['task_memory']['iteration'] == 2


def test_long_commands_with_same_prefix_retain_distinct_results(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for suffix in ('first', 'second'):
        store.event(task['id'], 'command_finished', 'x' * 350 + suffix,
                    data={'status': 'completed', 'exit_code': 0})
    task = remember_iteration(store, task, {})
    assert len(task['task_memory']['observations']) == 2


def test_goal_changed_during_loading_keeps_first_new_handoff(tmp_path):
    store, engine, task = make(tmp_path)
    original = engine.runtime.ensure
    def ensure(*args):
        store.update(task['id'], goal_version=2)
        return original(*args)
    engine.runtime.ensure = ensure
    engine.start(task['id'])
    finish(engine)
    memory = store.get(task['id'])['task_memory']
    assert memory['goal_version'] == 2 and memory['iteration'] == 1


def prompt_payload(task):
    text = memory_prompt(task).split(':\n', 1)[1]
    payload, _ = json.JSONDecoder().raw_decode(text)
    def unwrap(value):
        if isinstance(value, dict):
            if set(value) == {'text_chunks'}:
                return ''.join(value['text_chunks'])
            return {key: unwrap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [unwrap(item) for item in value]
        return value
    return unwrap(payload)


def record_file(store, task, path, operation='write', **extra):
    store.event(task['id'], 'tool_finished', operation, data={
        'tool': operation, 'status': 'completed', 'input': {'filePath': str(path)}, **extra})


def test_stale_same_goal_prose_never_overrides_current_step(tmp_path):
    from agentvisor.progress_plan import initialize
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [x] Dashboard\n- [ ] Export\n')
    write_document(task, 'MEMORY.md', 'goal_version: 1\nHandoff iteration106\nNext: Dashboard\n')
    task = initialize(store, task)
    identity = task['progress_plan']['steps'][0]['id']
    receipts = {'goal_version': 1, 'accepted': {identity: {'evidence': 'retained'}}}
    task = store.update(task['id'], iteration=117, step_reviews=receipts, task_memory={
        'goal_version': 1, 'working_notes': 'Handoff iteration106. Next: Dashboard', 'event_cursor': 0})
    # Safe even if a restart prepares a prompt before migrating legacy memory.
    assert 'Handoff iteration106' not in memory_prompt(task)
    task = remember_iteration(store, task)
    memory = task['task_memory']
    assert memory['current_step']['id'] == task['progress_plan']['steps'][1]['id']
    assert memory['current_step']['text'] == 'Export'
    assert memory['iteration'] == 117 and memory['accepted_count'] == 1
    assert task['step_reviews'] == receipts
    assert memory['attempts'] == []


def test_check_and_write_survive_many_reads_and_commands(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    product = Path(task['workspace']) / 'export.py'
    product.write_text('def export(): return 1', encoding='utf-8')
    record_file(store, task, product)
    store.event(task['id'], 'command_finished', 'pytest tests/test_export.py', data={
        'exit_code': 0, 'status': 'completed', 'output': '3 passed in 0.2s'})
    for index in range(70):
        record_file(store, task, product, 'read')
        store.event(task['id'], 'command_finished', f'Get-Item file{index}', data={
            'exit_code': 0, 'status': 'completed', 'output': 'file metadata'})
    task = remember_iteration(store, task)
    memory = task['task_memory']
    assert memory['checks'][0]['output'] == '3 passed in 0.2s'
    assert memory['checks'][0]['outcome'] == 'succeeded'
    assert len(memory['observations']) == 6
    assert memory['changed_files'][0]['path'] == 'export.py'
    assert memory['changed_files'][0]['sha256']
    assert memory['omitted_events'] > 0
    assert prompt_payload(task)['next_action']['operation'] == 'assess_step_completion'
    assert 'def export' not in memory_prompt(task)


def test_latest_failure_replaces_success_even_across_rotation(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for code in (0, 1):
        store.event(task['id'], 'command_finished', 'pytest tests/test_export.py', data={
            'exit_code': code, 'status': 'completed', 'output': '1 failed' if code else '1 passed'})
        task = remember_iteration(store, task)
    memory = task['task_memory']
    assert len(memory['checks']) == 1 and memory['checks'][0]['outcome'] == 'failed'
    assert memory['attempts'] == []
    assert prompt_payload(task)['next_action']['operation'] == 'inspect_failed_check'


def test_commands_need_exit_zero_and_ignore_cancelled_checks(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    store.event(task['id'], 'command_finished', 'pytest unknown', data={'status': 'completed'})
    store.event(task['id'], 'command_finished', 'pytest cancelled', data={'status': 'cancelled', 'exit_code': 0})
    task = remember_iteration(store, task)
    assert len(task['task_memory']['checks']) == 1
    assert task['task_memory']['checks'][0]['outcome'] == 'unknown'


def test_file_facts_are_scoped_observed_and_not_claims(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    product = Path(task['workspace']) / 'export.py'
    product.write_text('pass', encoding='utf-8')
    record_file(store, task, product, call_id='a', inferred=True)
    record_file(store, task, product, call_id='a', status='error', error='Edit failed')
    record_file(store, task, tmp_path / 'outside.py')
    record_file(store, task, Path(task['workspace']) / '.agentvisor/tasks/id/MEMORY.md')
    task = remember_iteration(store, task)
    assert task['task_memory']['changed_files'] == []
    record_file(store, task, product, call_id='b')
    task = remember_iteration(store, task)
    assert prompt_payload(task)['next_action']['operation'] == 'verify_changed_files'
    before = task['task_memory']['changed_files'][0]['sha256']
    product.write_text('changed', encoding='utf-8')
    task = remember_iteration(store, task)
    assert task['task_memory']['changed_files'][0]['sha256'] != before
    assert task['task_memory']['accepted_count'] == 0


def test_prompt_refreshes_plan_and_user_instructions_without_llm(tmp_path):
    from agentvisor.progress_plan import initialize
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n- [ ] Design\n')
    task = initialize(store, task)
    task = remember_iteration(store, initialize_memory(store, task))
    task = store.add_context(task['id'], 'Design must be usable with keyboard')
    payload = prompt_payload(task)
    assert payload['context_version'] == 1 and payload['pending_instruction_versions'] == [1]
    assert payload['next_action'] == {'operation': 'apply_user_instructions', 'versions': [1]}
    # No stale executor next step and no reuse of a past command as review proof.
    scope = task['progress_plan']['steps'][1]
    payload = prompt_payload(dict(task, review_phase=True, review_request={'steps': [scope]}))
    assert payload['current_step']['id'] == scope['id']
    assert payload['current_step']['state'] == 'review_requested'
    assert payload['next_action'] is None


def test_handoff_excludes_file_dump_and_redacts_credentials(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    store.event(task['id'], 'command_finished', 'pytest --token=private-credential --password "other secret"', data={
        'exit_code': 1, 'status': 'completed', 'output':
        'contents of MEMORY.md: user confidential\nERROR password=private-password\n1 failed'})
    task = remember_iteration(store, task, {'failed': True, 'output_tail': 'private session contents'})
    prompt = memory_prompt(task)
    assert 'private-credential' not in prompt and 'private-password' not in prompt
    assert 'other secret' not in prompt
    assert 'private session contents' not in prompt and 'user confidential' not in prompt
    assert '1 failed' in prompt


def test_full_handoff_is_bounded_without_losing_current_requirement(tmp_path):
    from agentvisor.progress_plan import initialize
    store, _, task = make(tmp_path)
    criterion = 'E' + '\\' * 1999
    write_document(task, 'PROGRESS.md', '- [ ] ' + criterion + '\n')
    task = initialize(store, task)
    task = initialize_memory(store, task)
    for index in range(12):
        store.event(task['id'], 'command_finished', 'pytest ' + 'x' * 800 + str(index), data={
            'exit_code': 1, 'output': 'ERROR ' + '\\' * 9000})
    task = remember_iteration(store, task)
    assert len(json.dumps(task['task_memory'], ensure_ascii=False)) <= 9500
    payload = prompt_payload(task)
    assert payload['current_step']['text'] == criterion
    assert len(json.dumps(payload, ensure_ascii=False)) <= 9500


def test_diagnostic_proposal_requires_fresh_observed_write_and_exact_scope(tmp_path):
    from agentvisor.progress_plan import initialize
    from agentvisor.tasks import document_path
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n')
    task = initialize(store, task)
    task = initialize_memory(store, task)
    identity = task['progress_plan']['steps'][0]['id']
    task = store.update(task['id'], iteration=7, active_role={'name': 'diagnostician'})
    notes = ('goal_version: 1\niteration: 7\ncontext_version: 0\nreview_revision: 0\n'
             f'current_step_id: {identity}\nHypothesis: serializer imports the wrong package.\n')
    write_document(task, 'MEMORY.md', notes)
    task = remember_iteration(store, task)
    assert 'diagnostic_proposal' not in task['task_memory']
    record_file(store, task, document_path(task, 'MEMORY.md'))
    task = remember_iteration(store, task)
    proposal = task['task_memory']['diagnostic_proposal']
    assert proposal['iteration'] == 7 and 'Hypothesis' in proposal['hypothesis']
    assert prompt_payload(task)['diagnostic_proposal'] == proposal
    # Reviewer gets neither the diagnostic suggestion nor a replayed verdict.
    assert 'diagnostic_proposal' not in prompt_payload(dict(task, review_phase=True))
    assert 'diagnostic_proposal' not in prompt_payload(dict(task, context_version=1))
    assert 'diagnostic_proposal' not in prompt_payload(dict(task, review_revision=1))
    task = store.update(task['id'], iteration=8, active_role={'name': 'executor'})
    task = remember_iteration(store, task)
    assert task['task_memory']['diagnostic_proposal'] == proposal
    assert prompt_payload(task)['diagnostic_proposal'] == proposal
    task = remember_iteration(store, store.update(task['id'], iteration=16))
    assert 'diagnostic_proposal' not in task['task_memory']


def test_diagnostic_old_headers_do_not_become_new_handoff(tmp_path):
    from agentvisor.tasks import document_path
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    task = store.update(task['id'], iteration=117, active_role={'name': 'diagnostician'})
    write_document(task, 'MEMORY.md', 'goal_version: 1\niteration: 106\ncurrent_step_id: none\n'
                   'context_version: 0\nreview_revision: 0\nNext: Dashboard\n')
    record_file(store, task, document_path(task, 'MEMORY.md'))
    task = remember_iteration(store, task)
    assert 'diagnostic_proposal' not in task['task_memory']
    assert 'Dashboard' not in memory_prompt(task)


def test_old_step_failure_does_not_direct_work_on_new_step(tmp_path):
    from agentvisor.progress_plan import initialize
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n- [ ] Design\n')
    task = initialize(store, task)
    task = initialize_memory(store, task)
    store.event(task['id'], 'command_finished', 'pytest tests/test_export.py',
                data={'exit_code': 1, 'status': 'completed', 'output': '1 failed'})
    task = remember_iteration(store, task)
    assert prompt_payload(task)['next_action']['operation'] == 'inspect_failed_check'
    plan = task['progress_plan']
    plan['steps'][0]['done'] = True
    task = store.update(task['id'], progress_plan=plan)
    assert prompt_payload(task)['current_step']['text'] == 'Design'
    assert prompt_payload(task)['next_action'] is None


def test_directory_read_never_becomes_check_or_next_diagnostic_action(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for command in ('Get-ChildItem backend/tests', 'Get-Content tests/check.py', 'echo pytest test'):
        store.event(task['id'], 'command_finished', command, data={
            'exit_code': 1, 'status': 'completed', 'output': 'ERROR path not found'})
    task = remember_iteration(store, task)
    assert task['task_memory']['checks'] == []
    assert len(task['task_memory']['observations']) == 3
    assert all(not item['is_verification_result'] for item in task['task_memory']['observations'])
    assert prompt_payload(task)['next_action'] is None


def test_diagnosis_survives_executor_rotations_but_not_requirement_change(tmp_path):
    from agentvisor.progress_plan import initialize
    from agentvisor.tasks import document_path
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n')
    task = initialize_memory(store, initialize(store, task))
    identity = task['progress_plan']['steps'][0]['id']
    task = store.update(task['id'], iteration=133, active_role={'name': 'diagnostician'})
    notes = ('goal_version: 1\niteration: 133\ncontext_version: 0\nreview_revision: 0\n'
             f'current_step_id: {identity}\nBackend exists. Check route shadowing, add export tests and UI.\n')
    write_document(task, 'MEMORY.md', notes)
    record_file(store, task, document_path(task, 'MEMORY.md'))
    task = remember_iteration(store, task)
    for iteration in (134, 135, 136):
        task = remember_iteration(store, store.update(task['id'], iteration=iteration,
                                  active_role={'name': 'executor'}), {'reason': 'context_handoff'})
        assert prompt_payload(task)['diagnostic_proposal']['hypothesis'] == notes
    task = remember_iteration(store, store.update(task['id'], context_version=1))
    assert 'diagnostic_proposal' not in task['task_memory']


def test_memory_read_preserves_ranges_symbols_and_checkpoint_not_full_file(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    product = Path(task['workspace']) / 'export.py'
    product.write_text('def export_csv():\n    return 1\n', encoding='utf-8')
    record_file(store, task, product, 'read', output='<content>\n10: def export_csv():\n11: secret body\n</content>')
    task = remember_iteration(store, task)
    entry = task['task_memory']['inspected_files'][0]
    assert entry['observed_ranges'] == [[10, 11]]
    assert entry['sha256'] and entry['excerpt'] == '10: def export_csv():'
    assert 'secret body' not in memory_prompt(task)
    record_file(store, task, product, 'read', output='<content>\n20: def export_xlsx():\n21: pass\n</content>')
    task = remember_iteration(store, task)
    assert task['task_memory']['inspected_files'][0]['observed_ranges'] == [[10, 11], [20, 21]]
    product.write_text('changed', encoding='utf-8')
    record_file(store, task, product, 'read', output='<content>\n1: changed\n</content>')
    task = remember_iteration(store, task)
    assert task['task_memory']['inspected_files'][0]['observed_ranges'] == [[1, 1]]
    assert not task['task_memory']['checks'] and not task['task_memory']['changed_files']


def test_read_excerpts_redact_secrets_and_skip_sensitive_files(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for name in ('settings.py', '.env', 'credentials.json', 'MEMORY.md'):
        path = Path(task['workspace']) / name
        path.write_text('password="dont-repeat"', encoding='utf-8')
        record_file(store, task, path, 'read', output='1: password="dont-repeat"\n2: unrelated prose')
    task = remember_iteration(store, task)
    assert 'dont-repeat' not in memory_prompt(task)
    files = {entry['path']: entry for entry in task['task_memory']['inspected_files']}
    assert '[redacted]' in files['settings.py']['excerpt']
    assert all('excerpt' not in files[name] for name in ('.env', 'credentials.json', 'MEMORY.md'))


def test_native_reader_line_cap_cannot_hide_current_action_or_diagnosis(tmp_path):
    from agentvisor.progress_plan import initialize
    from agentvisor.tasks import document_path
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] ' + 'Export ' + '\\' * 1900 + '\n')
    task = initialize_memory(store, initialize(store, task))
    task = store.update(task['id'], iteration=133, active_role={'name': 'diagnostician'})
    identity = task['progress_plan']['steps'][0]['id']
    notes = ('goal_version: 1\niteration: 133\ncontext_version: 0\nreview_revision: 0\n'
             f'current_step_id: {identity}\n' + 'Check export paths. ' * 90)
    write_document(task, 'MEMORY.md', notes)
    record_file(store, task, document_path(task, 'MEMORY.md'))
    task = remember_iteration(store, task)
    prompt = memory_prompt(task)
    assert max(map(len, prompt.splitlines())) < 2000
    # Replay the actual native reader's 2,000-char line cap.
    replayed = '\n'.join(line[:2000] for line in prompt.splitlines())
    assert replayed == prompt.rstrip('\n')
    payload = prompt_payload(task)
    assert payload['current_step']['id'] == identity
    assert payload['diagnostic_proposal']['hypothesis'] == notes
    assert prompt.index('"current_step"') < prompt.index('"observations"')
    assert prompt.index('"diagnostic_proposal"') < prompt.index('"observations"')


def test_recent_lost_report_recovers_only_from_matching_scoped_diagnostic_write(tmp_path):
    from agentvisor.progress_plan import initialize
    from agentvisor.tasks import document_path
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n')
    task = initialize_memory(store, initialize(store, task))
    identity = task['progress_plan']['steps'][0]['id']
    notes = ('goal_version: 1\niteration: 133\ncontext_version: 0\nreview_revision: 0\n'
             f'current_step_id: {identity}\nVerify route shadowing and add tests.\n')
    task = store.update(task['id'], iteration=135, active_role={'name': 'executor'})
    write_document(task, 'MEMORY.md', notes)
    store.event(task['id'], 'session_role', '', data={'name': 'diagnostician'})
    store.event(task['id'], 'tool_finished', 'write', data={
        'tool': 'write', 'status': 'completed', 'input': {
            'filePath': str(document_path(task, 'MEMORY.md')), 'content': notes}})
    store.event(task['id'], 'iteration_finished', '', data={'iteration': 133})
    # Existing cursor has already passed these events, like the old build.
    with store.connect() as db:
        cursor = db.execute('SELECT MAX(id) FROM events').fetchone()[0]
    memory = dict(task['task_memory'], event_cursor=cursor)
    task = store.update(task['id'], task_memory=memory)
    task = remember_iteration(store, task)
    assert task['task_memory']['diagnostic_proposal']['hypothesis'] == notes
    assert task['task_memory']['diagnostic_proposal']['iteration'] == 133
    memory = dict(task['task_memory'])
    memory.pop('diagnostic_proposal')
    task = store.update(task['id'], task_memory=memory)
    write_document(task, 'MEMORY.md', notes + 'Unobserved addition.')
    task = remember_iteration(store, task)
    assert 'diagnostic_proposal' not in task['task_memory']
    # A same-looking old file cannot restore the report outside its age bound.
    write_document(task, 'MEMORY.md', notes)
    task = remember_iteration(store, store.update(task['id'], iteration=142))
    assert 'diagnostic_proposal' not in task['task_memory']


def test_later_write_removes_earlier_read_excerpt_of_same_file(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    path = Path(task['workspace']) / 'export.py'
    path.write_text('def old(): pass', encoding='utf-8')
    record_file(store, task, path, 'read', output='1: def old(): pass')
    record_file(store, task, path)
    task = remember_iteration(store, task)
    assert not task['task_memory']['inspected_files']
    assert task['task_memory']['changed_files'][0]['path'] == 'export.py'


def test_successful_current_checks_direct_completion_assessment_without_accepting(tmp_path):
    from agentvisor.progress_plan import initialize
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Export\n')
    task = initialize_memory(store, initialize(store, task))
    path = Path(task['workspace']) / 'export.py'
    path.write_text('pass', encoding='utf-8')
    record_file(store, task, path)
    task = remember_iteration(store, task)
    assert prompt_payload(task)['next_action']['operation'] == 'verify_changed_files'
    store.event(task['id'], 'command_finished', 'pytest tests/test_export.py', data={
        'status': 'completed', 'exit_code': 0, 'output': '34 passed in 0.8s'})
    task = remember_iteration(store, task)
    action = prompt_payload(task)['next_action']
    assert action['operation'] == 'assess_step_completion' and action['check_event_ids']
    assert task['task_memory']['accepted_count'] == 0
    assert task['progress_plan']['steps'][0]['done'] is False
    record_file(store, task, path)
    task = remember_iteration(store, task)
    assert prompt_payload(task)['next_action']['operation'] == 'verify_changed_files'


def test_powershell_security_error_exit_zero_is_inconclusive_not_success(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    output = ('TSC_EXIT=\n[stderr]\n#< CLIXML\n'
              '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
              '<S S="Error">CategoryInfo : PSSecurityException_x000D__x000A_'
              'FullyQualifiedErrorId : UnauthorizedAccess_x000D__x000A_</S></Objs>')
    store.event(task['id'], 'command_finished', 'npx tsc --noEmit; Write-Host $LASTEXITCODE', data={
        'status': 'completed', 'exit_code': 0, 'output': output})
    task = remember_iteration(store, task)
    check = task['task_memory']['checks'][0]
    assert check['exit_code'] == 0 and check['outcome'] == 'unknown'
    assert 'PSSecurityException' in check['output'] and 'UnauthorizedAccess' in check['output']
    assert '#< CLIXML' not in check['output'] and check['result_warning']
    assert prompt_payload(task)['next_action']['operation'] == 'inspect_inconclusive_check'
