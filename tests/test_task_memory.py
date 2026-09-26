import json

from agentvisor.store import Store
from agentvisor.task_memory import initialize_memory, memory_prompt, remember_iteration
from agentvisor.tasks import prepare_documents, write_document
from test_supervisor import make, finish


def test_memory_survives_restart_and_distinguishes_notes_from_observations(tmp_path):
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
    assert 'Hypothesis' in memory['working_notes']
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384})
    assert 'TASK HANDOFF MEMORY' in prompt
    assert 'not independently verified facts' in prompt
    assert 'Processes from previous sessions have been stopped' in prompt


def test_new_goal_excludes_old_events_and_old_notes(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    write_document(task, 'MEMORY.md', 'goal_version: 1\nOld goal fact\n')
    store.event(task['id'], 'command_finished', 'old command', data={'exit_code': 0, 'status': 'completed'})
    task = initialize_memory(store, store.update(task['id'], goal_version=2))
    task = remember_iteration(store, task, {'failed': False})
    assert task['task_memory']['observations'] == []
    assert task['task_memory']['working_notes'] == ''


def test_latest_result_replaces_stale_success_and_storage_is_bounded(tmp_path):
    store, _, task = make(tmp_path)
    task = initialize_memory(store, task)
    for iteration in range(10):
        store.event(task['id'], 'command_finished', f'check {iteration % 6}', data={
            'exit_code': iteration % 2, 'status': 'completed', 'output': '\\' * 9000})
        task = remember_iteration(store, store.update(task['id'], iteration=iteration),
                                  {'failed': bool(iteration % 2), 'output_tail': 'x' * 9000})
    memory = task['task_memory']
    assert len(memory['observations']) == 5 and len(memory['attempts']) == 4
    assert memory['observations'][-1]['exit_code'] == 1
    assert len(json.dumps(memory)) < 10000
    assert len(memory_prompt(task)) < 11000
    task = remember_iteration(store, task, {'failed': True})
    assert len(task['task_memory']['observations']) == 5


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
