import sys
from pathlib import Path

import psutil
import pytest

from agentvisor.tasks import prepare_documents, state_dir
from test_supervisor import Runtime, finish, make, wait_file


@pytest.mark.parametrize('scenario', ['step_hang', 'eof_hang', 'noisy_hang', 'start_chatter'])
def test_idle_watchdog_stops_agent_even_when_model_is_healthy(tmp_path, scenario):
    store, engine, task = make(tmp_path, scenario, idle_timeout_seconds=0.5,
                              timeout_seconds=30, max_failures=1)
    engine.runtime.health = lambda *args: None
    engine.start(task['id'])
    wait_file(state_dir(task) / 'child.pid')
    child = psutil.Process(int((state_dir(task) / 'child.pid').read_text()))
    finish(engine, seconds=5)
    result = store.get(task['id'])
    assert result['status'] == 'blocked'
    assert result['runtime_health']['ok']
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    event = next(e for e in store.events(task['id']) if e['kind'] == 'agent_idle')
    assert event['data']['last_event'] == 'step_start'
    assert result['recovery_context']['reason'] == 'idle_timeout'


def test_real_agent_events_extend_idle_deadline(tmp_path):
    # Leave room for Windows interpreter startup on a busy host. The fixture
    # emits work for 3 seconds, longer than this budget, so renewal is required.
    store, engine, task = make(tmp_path, 'active', idle_timeout_seconds=2.0)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'completed_unverified'
    assert not any(e['kind'] == 'agent_idle' for e in store.events(task['id']))


def test_idle_recovery_supplies_diagnosis_and_kills_previous_tree(tmp_path):
    store, engine, task = make(tmp_path, idle_timeout_seconds=1.0, max_failures=2)
    prompts, children = [], []

    class HealthyRuntime(Runtime):
        def health(self, *args):
            return None

    engine.runtime = HealthyRuntime()

    def command(current, prompt, ready):
        prompts.append(prompt)
        if current['iteration'] == 2:
            child = int((state_dir(current) / 'child.pid').read_text())
            children.append(not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE)
        return [sys.executable, str(Path(__file__).with_name('fake_agent.py')),
                str(state_dir(current)), 'step_hang' if current['iteration'] == 1 else 'complete']

    engine.command = command
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified'
    assert result['iteration'] == 2 and result['recoveries'] == 1
    assert children == [True]
    assert 'idle_timeout' in prompts[1] and 'step_start' in prompts[1]
    assert 'Do not repeat the same failing approach' in prompts[1]


def test_failed_and_successful_iterations_share_progress_limit(tmp_path):
    store, engine, task = make(tmp_path, stall_limit=3, max_failures=5)
    engine.command_builder = lambda current: [
        sys.executable, str(Path(__file__).with_name('fake_agent.py')),
        str(state_dir(current)), 'failure' if current['iteration'] % 2 else 'stall']
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'blocked'
    assert result['iteration'] == 3
    assert result['progress_watch']['stalls'] == 3
    assert 'Нет новых' in result['reason']


def test_progress_limit_survives_new_supervisor(tmp_path):
    from agentvisor.supervisor import Supervisor
    store, engine, task = make(tmp_path, 'stall', stall_limit=3, max_iterations=2)
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['progress_watch']['stalls'] == 2
    store.update(task['id'], max_iterations=10)
    resumed = Supervisor(store, Runtime(), engine.command_builder)
    resumed.start(task['id'])
    finish(resumed)
    assert store.get(task['id'])['iteration'] == 3
    assert 'Нет новых' in store.get(task['id'])['reason']


@pytest.mark.parametrize('scenario', ['failure', 'stall', 'step_hang'])
def test_autonomous_repair_continues_beyond_local_retry_limits(tmp_path, scenario):
    store, engine, task = make(tmp_path, autonomous_recovery=True, max_failures=2,
                              stall_limit=2, idle_timeout_seconds=0.5, max_iterations=6)
    prompts = []

    def command(current, prompt, ready):
        prompts.append(prompt)
        return [sys.executable, str(Path(__file__).with_name('fake_agent.py')),
                str(state_dir(current)), scenario if current['iteration'] <= 2 else 'complete']

    engine.command = command
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified'
    assert result['iteration'] == 4
    assert 'REPAIR SESSION' in prompts[2]
    assert 'SESSION ROLE: diagnostician' in prompts[2]
    assert 'SESSION ROLE: executor' in prompts[3]
    assert '"attempts": 2' in prompts[2]
    assert result['progress_watch']['stalls'] == 0
    assert result['recovery_context'] is None
    assert not any(event['kind'] == 'blocked' for event in store.events(task['id']))


def test_autonomous_repair_still_respects_total_budget(tmp_path):
    store, engine, task = make(tmp_path, 'failure', autonomous_recovery=True,
                              max_failures=1, max_iterations=3)
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'blocked' and result['iteration'] == 3
    assert result['reason'] == 'Достигнут лимит итераций'


def test_legacy_task_without_recovery_flag_uses_autonomous_mode(tmp_path):
    store, engine, task = make(tmp_path, 'failure', max_failures=1, max_iterations=2)
    with store.connect() as db:
        db.execute("UPDATE tasks SET body=json_remove(body, '$.autonomous_recovery') WHERE id=?", (task['id'],))
    engine.start(task['id'])
    finish(engine)
    result = store.get(task['id'])
    assert result['iteration'] == 2 and result['reason'] == 'Достигнут лимит итераций'


def test_crashed_autonomous_task_resumes_with_repair_context(tmp_path):
    from agentvisor.supervisor import Supervisor
    store, engine, task = make(tmp_path, autonomous_recovery=True)
    store.update(task['id'], status='running', iteration=1)
    seen = []

    def command(current):
        seen.append(current['recovery_context'])
        return engine.command_builder(current)

    resumed = Supervisor(store, Runtime(), command)
    finish(resumed)
    result = store.get(task['id'])
    assert result['status'] == 'completed_unverified' and result['iteration'] == 3
    assert seen[0]['reason'] == 'service_interrupted' and seen[0]['repair']
    assert result['recoveries'] == 1


@pytest.mark.parametrize(('before', 'after'), [('pausing', 'paused'), ('stopping', 'stopped'), ('paused', 'paused')])
def test_restart_never_overrides_user_pause_or_stop(tmp_path, before, after):
    from agentvisor.supervisor import Supervisor
    store, engine, task = make(tmp_path, autonomous_recovery=True)
    store.update(task['id'], status=before)
    resumed = Supervisor(store, Runtime(), engine.command_builder)
    assert not resumed.busy
    assert store.get(task['id'])['status'] == after


def test_pause_cancels_autonomous_repair_without_another_attempt(tmp_path):
    store, engine, task = make(tmp_path, 'step_hang', autonomous_recovery=True,
                              idle_timeout_seconds=0.4, backoff_seconds=1)
    engine.start(task['id'])
    wait_file(state_dir(task) / 'child.pid')
    engine.control(task['id'], 'pause')
    finish(engine)
    result = store.get(task['id'])
    assert result['status'] == 'paused' and result['iteration'] == 1
    assert not result.get('recovery_context')


def test_agent_idle_budget_does_not_apply_to_verification(tmp_path):
    store, engine, task = make(tmp_path, idle_timeout_seconds=0.5,
                              verification=[sys.executable, '-c', 'import time; time.sleep(1)'])
    engine.start(task['id'])
    finish(engine)
    assert store.get(task['id'])['status'] == 'succeeded'


def test_new_goal_drops_old_recovery_advice(tmp_path):
    store, _, task = make(tmp_path)
    task = store.update(task['id'], goal_version=2, recovery_context={
        'goal_version': 1, 'reason': 'stale_failure', 'iteration': 1})
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384})
    assert 'stale_failure' not in prompt


def test_prompt_uses_bounded_commands_and_host_platform(tmp_path):
    store, _, task = make(tmp_path)
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 16384})
    assert 'timeout' in prompt and 'inherited' in prompt
    if sys.platform == 'win32':
        assert 'Windows' in prompt and 'Start-Process' in prompt
        assert 'nohup' in prompt and '-WindowStyle Hidden' in prompt
