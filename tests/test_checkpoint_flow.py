import subprocess

from agentvisor.checkpoint_flow import checkpoint_after_acceptance, prepare_checkpoint
from test_supervisor import make


def test_unavailable_snapshot_does_not_stop_execution_or_retry_every_iteration(tmp_path, monkeypatch):
    store, _, task = make(tmp_path, checkpoints=True)
    calls = []
    def fail(*args):
        calls.append(1)
        raise subprocess.TimeoutExpired('git', 10)
    monkeypatch.setattr('agentvisor.checkpoint_flow.save_checkpoint', fail)
    task = prepare_checkpoint(store, task)
    assert task['checkpoint_notice']['goal_version'] == 1
    prepare_checkpoint(store, task)
    assert len(calls) == 1
    assert store.events(task['id'])[-1]['kind'] == 'checkpoint_skipped'


def test_checkpoint_does_not_attach_to_goal_changed_during_copy(tmp_path, monkeypatch):
    store, _, task = make(tmp_path, checkpoints=True)
    def save(*args):
        store.update(task['id'], goal_version=2)
        return {'id': 'old', 'goal_version': 1}
    monkeypatch.setattr('agentvisor.checkpoint_flow.save_checkpoint', save)
    current = checkpoint_after_acceptance(store, task)
    assert current['goal_version'] == 2 and not current.get('checkpoint')


def test_alternative_copy_is_created_once_from_current_checkpoint(tmp_path, monkeypatch):
    store, _, task = make(tmp_path, checkpoints=True)
    task = store.update(task['id'], checkpoint={'id': 'known', 'goal_version': 1},
                        recovery_context={'goal_version': 1, 'failure_cause': {'strategy': 'alternative'}})
    calls = []
    def fork(*args):
        calls.append(1)
        return str(tmp_path / 'experiment')
    monkeypatch.setattr('agentvisor.checkpoint_flow.fork_checkpoint', fork)
    task = prepare_checkpoint(store, task)
    prepare_checkpoint(store, task)
    assert len(calls) == 1
    assert task['experiment_workspace'].endswith('experiment')
