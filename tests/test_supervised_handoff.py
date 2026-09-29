"""Run supervisor state transitions without an external model."""
import copy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentvisor.recovery import initialize_progress
from agentvisor.step_acceptance import accepted_steps, pending_steps
from agentvisor.tasks import checklist, write_document
from test_progress_protocol import fixture, update
from test_step_acceptance import report, result
from test_supervisor import finish


def boundary(reason='context_handoff'):
    return result(reason=reason, exit_code=2,
                  session_handoff={'reason': reason, 'blocked': False,
                                   'evidence': {'input_limit': 48000}})


def setup(tmp_path, **changes):
    store, engine, task = fixture(tmp_path)
    task = initialize_progress(store, task)
    task = store.update(task['id'], max_iterations=30, max_failures=1, auto_tune=True,
                        autonomous_recovery=True, failure_streak=2, **changes)
    profiles = []
    ensure = engine.runtime.ensure

    def record(profile, cancel):
        profiles.append(copy.deepcopy(profile))
        return ensure(profile, cancel)

    engine.runtime.ensure = record
    return store, engine, task, profiles


def test_next_work_session_and_independent_reviews_finish_after_handoff(tmp_path, monkeypatch):
    store, engine, task, profiles = setup(tmp_path)
    old_receipts = copy.deepcopy(task['step_reviews']['accepted'])
    phases = []
    working = 0

    def execute(store, current, *args, **kwargs):
        nonlocal working
        if current.get('review_phase'):
            phases.append('review')
            # Handoff did not unclaim the UI; it still needs real acceptance.
            assert pending_steps(current)
            assert all(current['step_reviews']['accepted'].get(key) == value
                       for key, value in old_receipts.items())
            report(store, current)
            return result()
        working += 1
        phases.append('work')
        assert current['failure_streak'] == 2
        if working == 1:
            return boundary()
        assert current['iteration'] == 2
        assert len(current['step_reviews']['accepted']) == 2
        export = current['progress_plan']['steps'][2]
        update(store, current, 'claim', step_id=export['id'], note='Export implemented and checked')
        write_document(current, 'DONE.md', 'goal_version: 1\nDone\n')
        return result()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert phases == ['work', 'review', 'work', 'review']
    assert current['status'] == 'completed_unverified'
    assert len(current['step_reviews']['accepted']) == 3 and not pending_steps(current)
    assert all(current['step_reviews']['accepted'][key] == value for key, value in old_receipts.items())
    assert current['iteration'] == 2
    assert current['max_hours'] == task['max_hours'] and current['max_iterations'] == task['max_iterations']
    assert len(profiles) == 2
    assert all(profile['context'] == task['profile']['context'] and not profile.get('_reload') for profile in profiles)
    assert not any(event['kind'] in {'context_increased', 'iteration_error', 'recovering'}
                   for event in store.events(task['id']))


@pytest.mark.parametrize('reason', ['context_handoff', 'work_stalled'])
def test_repeated_empty_handoffs_return_to_executor_until_task_limit(tmp_path, monkeypatch, reason):
    store, engine, task, profiles = setup(tmp_path, step_acceptance=False)
    plan, receipts = copy.deepcopy(task['progress_plan']), copy.deepcopy(task['step_reviews'])
    roles = []

    def execute(store, current, *args, **kwargs):
        roles.append(current['active_role']['name'])
        return boundary(reason)

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert current['status'] == 'blocked' and current['iteration'] == task['max_iterations']
    assert roles[:6] == ['executor'] * 4 + ['diagnostician', 'diagnostician']
    assert all(role == 'executor' for role in roles[6:])
    assert current['session_continuation']['diagnosed'] is False
    assert current['session_continuation']['diagnostic_attempts'] == 2
    assert 'лимит итераций' in current['reason']
    assert len([event for event in store.events(task['id']) if event['kind'] == 'diagnostic_fallback']) == 1
    assert current['failure_streak'] == 2
    assert current['progress_plan'] == plan and current['step_reviews'] == receipts
    assert current['recoveries'] == 0
    assert all(profile['context'] == task['profile']['context'] for profile in profiles)
    assert not any(event['kind'] == 'iteration_error' for event in store.events(task['id']))


def test_iteration_limit_is_preserved_across_controlled_handoffs(tmp_path, monkeypatch):
    store, engine, task, _ = setup(tmp_path, step_acceptance=False)
    task = store.update(task['id'], max_iterations=2)
    monkeypatch.setattr('agentvisor.supervisor.execute', lambda *args, **kwargs: boundary())
    engine.run(task['id'])
    current = store.get(task['id'])
    assert current['status'] == 'blocked' and current['iteration'] == 2
    assert current['max_iterations'] == 2 and 'лимит итераций' in current['reason'].lower()
    assert current['failure_streak'] == 2


def test_shell_style_workspace_write_is_observed_at_session_boundary(tmp_path, monkeypatch):
    store, engine, task, _ = setup(tmp_path, step_acceptance=False)
    task = store.update(task['id'], max_iterations=1)

    def execute(store, current, *args, **kwargs):
        target = Path(current['workspace']) / '_work' / 'inventory.txt'
        target.parent.mkdir(exist_ok=True)
        target.write_text('module 1', encoding='utf-8')
        return boundary()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert current['task_memory']['changed_files'][0]['path'] == '_work/inventory.txt'
    assert current['session_continuation']['without_result'] == 0
    assert current['task_memory']['accepted_count'] == len(accepted_steps(task))


def test_auto_tune_recovers_static_context_overflow_before_blocking(tmp_path, monkeypatch):
    store, engine, task, profiles = setup(tmp_path, step_acceptance=False)
    task = store.update(task['id'], max_iterations=2)
    calls = 0

    def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return result(reason='context_blocked', exit_code=0,
                          session_handoff={'reason': 'context_blocked', 'blocked': True,
                                           'evidence': {'cause': 'static_context_overflow',
                                                        'context_limit': task['profile']['context'],
                                                        'safety_reserve': 3277, 'static_tokens': 20000,
                                                        'output_reserve': 4096}})
        return boundary()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert calls == 2
    assert current['status'] == 'blocked' and 'лимит итераций' in current['reason']
    assert current['resolved_profile']['context'] > task['profile']['context']
    assert len(profiles) == 2 and profiles[1]['context'] > profiles[0]['context']
    assert len([event for event in store.events(task['id']) if event['kind'] == 'context_increased']) == 1


def test_final_claim_at_handoff_is_reviewed_and_completes_without_another_worker(tmp_path, monkeypatch):
    store, engine, task, _ = setup(tmp_path)
    phases = []

    def execute(store, current, *args, **kwargs):
        if current.get('review_phase'):
            phases.append('review')
            report(store, current)
            return result()
        phases.append('work')
        export = current['progress_plan']['steps'][2]
        update(store, current, 'claim', step_id=export['id'], note='Export checked')
        write_document(current, 'DONE.md', 'goal_version: 1\nDone\n')
        return boundary()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert phases == ['work', 'review', 'review']
    assert current['status'] == 'completed_unverified' and current['iteration'] == 1
    assert len(current['step_reviews']['accepted']) == 3


def test_total_time_budget_is_not_restarted_by_context_handoff(tmp_path, monkeypatch):
    store, engine, task, _ = setup(tmp_path, step_acceptance=False)
    task = store.update(task['id'], max_hours=1 / 3600)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr('agentvisor.supervisor.time',
                        SimpleNamespace(monotonic=lambda: clock.now, time=time.time))

    def execute(*args, **kwargs):
        clock.now += 2
        return boundary()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert current['status'] == 'blocked' and current['iteration'] == 1
    assert 'лимит времени' in current['reason'].lower()
    assert current['max_hours'] == 1 / 3600 and current['elapsed'] == 2
    assert current['failure_streak'] == 2


def test_pause_after_handoff_does_not_start_another_session(tmp_path, monkeypatch):
    store, engine, task, _ = setup(tmp_path, step_acceptance=False)
    calls = []
    event = store.event

    def observe(task_id, kind, message, *args, **kwargs):
        value = event(task_id, kind, message, *args, **kwargs)
        if kind == 'next_iteration':
            engine.control(task_id, 'pause')
        return value

    def execute(*args, **kwargs):
        calls.append(True)
        return boundary()

    monkeypatch.setattr(store, 'event', observe)
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.start(task['id'])
    finish(engine)
    current = store.get(task['id'])
    assert current['status'] == 'paused' and len(calls) == 1
    assert current['iteration'] == 1 and current['failure_streak'] == 2
    assert checklist(current) == checklist(task)
    assert current['step_reviews'] == task['step_reviews']
