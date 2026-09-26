from agentvisor.recovery import record_recovery
from agentvisor.store import Store
from agentvisor.tasks import prepare_documents, write_document
from test_supervisor import make


def failure(command, module='fastapi'):
    return {'reason': 'tool_failure', 'tool_failures': [
        {'tool': 'bash', 'input': {'command': command},
         'output': f"ModuleNotFoundError: No module named '{module}'", 'status': 'error'}]}


def test_changed_commands_trigger_diagnostic_strategy_and_survive_restart(tmp_path):
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Start backend\n')
    for number, command in enumerate(['python app.py', 'python -m app', 'uv run app.py'], 1):
        task = store.update(task['id'], iteration=number)
        task = record_recovery(store, task, failure(command))
    task = Store(tmp_path / 'data').get(task['id'])
    recovery = task['recovery_context']
    assert recovery['failure_cause']['attempts'] == 3
    assert recovery['failure_cause']['category'] == 'missing_dependency'
    assert recovery['repeated_failure_count'] == 1
    assert recovery['repair']
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384})
    assert 'STRATEGY CHANGE REQUIRED' in prompt
    assert 'smallest bounded probe' in prompt
    assert sum(e['kind'] == 'strategy_change' for e in store.events(task['id'])) == 1
    for number in (4, 5):
        task = record_recovery(store, store.update(task['id'], iteration=number), failure(str(number)))
    assert task['recovery_context']['failure_cause']['strategy'] == 'alternative'


def test_distinct_dependency_step_and_goal_do_not_share_counter(tmp_path):
    store, _, task = make(tmp_path)
    write_document(task, 'PROGRESS.md', '- [ ] Backend\n')
    task = record_recovery(store, task, failure('one'))
    task = record_recovery(store, task, failure('two', 'pytest'))
    assert task['recovery_context']['failure_cause']['attempts'] == 1
    write_document(task, 'PROGRESS.md', '- [x] Backend\n- [ ] Frontend\n')
    task = record_recovery(store, task, failure('three', 'pytest'))
    assert task['recovery_context']['failure_cause']['attempts'] == 1
    task = store.update(task['id'], goal_version=2)
    task = record_recovery(store, task, failure('four', 'pytest'))
    assert task['recovery_context']['failure_cause']['attempts'] == 1


def test_duplicate_tool_observations_count_once_per_attempt(tmp_path):
    store, _, task = make(tmp_path)
    result = failure('python app.py')
    result['tool_failures'] *= 4
    task = record_recovery(store, task, result)
    assert task['recovery_context']['failure_cause']['attempts'] == 1


def test_no_evidence_does_not_invent_a_cause(tmp_path):
    store, _, task = make(tmp_path)
    task = record_recovery(store, task, {'reason': 'no_progress'})
    assert 'failure_cause' not in task['recovery_context']


def test_timeout_and_exit_code_alone_are_not_a_shared_cause(tmp_path):
    store, _, task = make(tmp_path)
    task = record_recovery(store, task, {'reason': 'tool_timeout',
        'pending_tools': [{'tool': 'bash', 'input': {'command': 'npm test'}}],
        'error_detail': 'Command exited with code 1'})
    assert 'failure_cause' not in task['recovery_context']


def test_cwd_is_part_of_scope_and_same_iteration_is_not_counted_twice(tmp_path):
    store, _, task = make(tmp_path)
    result = failure('python app.py')
    result['tool_failures'][0]['input']['cwd'] = '/project/backend'
    task = record_recovery(store, task, result)
    task = record_recovery(store, task, result)
    assert task['recovery_context']['failure_cause']['attempts'] == 1
    result['tool_failures'][0]['input']['cwd'] = '/project/other'
    task = record_recovery(store, store.update(task['id'], iteration=1), result)
    assert task['recovery_context']['failure_cause']['attempts'] == 1


def test_step_annotations_cannot_reset_loop_counter(tmp_path):
    store, _, task = make(tmp_path)
    for iteration in range(1, 4):
        write_document(task, 'PROGRESS.md', f'- [ ] Start backend (attempt {iteration})\n')
        task = record_recovery(store, store.update(task['id'], iteration=iteration), failure(str(iteration)))
    assert task['recovery_context']['failure_cause']['attempts'] == 3


def test_bash_missing_command_and_case_sensitive_paths():
    from agentvisor.loop_detection import failure_cause
    def cause(output):
        return failure_cause({'tool_failures': [{'output': output}]}, 'step')
    assert cause('/bin/bash: line 1: pnpm: command not found')['detail'] == 'pnpm'
    assert cause("Cannot find module './Foo'")['key'] != cause("Cannot find module './foo'")['key']
