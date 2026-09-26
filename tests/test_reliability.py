import json

import pytest

from agentvisor.recovery import initialize_progress, observe_progress
from agentvisor.step_acceptance import claimed_steps, pending_steps, restore_accepted_claims
from agentvisor.tasks import checklist, write_document
from test_supervisor import make


def accepted_task(store, task):
    write_document(task, 'PROGRESS.md', '- [x] First\n- [x] Second\n- [ ] New\n')
    records = {s['id']: {'text': s['text'], 'evidence_events': [1]} for s in claimed_steps(task)}
    return store.update(task['id'], step_reviews={'goal_version': task['goal_version'],
                        'context_version': 0, 'accepted': records})


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('recheck', [False, True])
def test_context_during_diagnosis_never_clears_old_claims(tmp_path, monkeypatch, failed, recheck):
    store, engine, task = make(tmp_path, step_acceptance=True, max_iterations=1, max_failures=1)
    task = initialize_progress(store, accepted_task(store, task))
    store.update(task['id'], recovery_context={'goal_version': task['goal_version'], 'repair': True})

    def execute(store, current, *args, **kwargs):
        assert not current.get('review_phase')
        store.add_context(task['id'], 'Use a different port', recheck=recheck)
        write_document(task, 'PROGRESS.md', '- [x] First\n- [x] Second\n- [x] New\n')
        return {'failed': failed, 'reason': 'test' if failed else None, 'exit_code': int(failed),
                'output_tokens': 0, 'duration': 0}

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    # Rechecking all old claims is a separate session; the invariant here is rollback.
    monkeypatch.setattr(engine, 'review_steps', lambda *args: None)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert [i['done'] for i in checklist(current)] == [True, True, False]
    assert len(current['step_reviews']['accepted']) == 2
    assert len(pending_steps(current)) == (2 if recheck else 0)


def test_context_invalidation_is_explicit_and_preserves_receipts(tmp_path):
    store, _, task = make(tmp_path, step_acceptance=True)
    task = accepted_task(store, task)
    receipts = task['step_reviews']
    current = store.add_context(task['id'], 'Browser endpoint is available')
    assert not pending_steps(current)
    current = store.add_context(task['id'], 'All previous API responses must now be paginated', recheck=True)
    assert len(pending_steps(current)) == 2
    assert current['step_reviews'] == receipts
    assert [i['done'] for i in checklist(current)] == [True, True, False]


def test_broken_review_retries_reviewer_without_product_diagnosis(tmp_path, monkeypatch):
    from test_step_acceptance import report, result
    store, engine, task = make(tmp_path, step_acceptance=True, autonomous_recovery=True,
                               max_iterations=2, max_failures=1)
    phases = []

    def execute(store, current, *args, **kwargs):
        if not current.get('review_phase'):
            phases.append('work')
            write_document(task, 'PROGRESS.md', '- [x] Implement\n')
            write_document(task, 'DONE.md', 'goal_version: 1\nDone\n')
        else:
            phases.append('review')
            assert current['timeout_seconds'] <= 300
            if len(phases) == 3:
                assert 'did not write' in current['review_retry']['error']
                report(store, current)
        return result()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert phases == ['work', 'review', 'review']
    assert current['status'] == 'completed_unverified'
    assert not current.get('review_retry')
    assert not current.get('recovery_context')


def test_acceptance_is_progress_even_when_checkmark_was_already_set(tmp_path):
    store, _, task = make(tmp_path, step_acceptance=True)
    write_document(task, 'PROGRESS.md', '- [x] First\n')
    task = initialize_progress(store, task)
    task = store.update(task['id'], recovery_context={'goal_version': 1, 'repair': True})
    task = observe_progress(store, task)
    assert task['progress_watch']['stalls'] == 1
    step = claimed_steps(task)[0]
    task = store.update(task['id'], step_reviews={'goal_version': 1, 'accepted': {step['id']: {}}})
    task = observe_progress(store, task)
    assert task['progress_watch']['stalls'] == 0
    assert task['recovery_context'] is None


def test_worker_cannot_erase_valid_acceptance_but_changed_requirements_can(tmp_path):
    store, _, task = make(tmp_path, step_acceptance=True)
    task = accepted_task(store, task)
    write_document(task, 'PROGRESS.md', '- [ ] First\n- [ ] Second\n- [ ] New\n')
    assert len(restore_accepted_claims(task)) == 2
    assert [s['done'] for s in checklist(task)] == [True, True, False]
    task = store.add_context(task['id'], 'New acceptance criteria', recheck=True)
    write_document(task, 'PROGRESS.md', '- [ ] First\n- [ ] Second\n- [ ] New\n')
    assert not restore_accepted_claims(task)
    assert not any(s['done'] for s in checklist(task))


def test_reviewer_protocol_failure_cannot_burn_all_iterations(tmp_path, monkeypatch):
    from test_step_acceptance import result
    store, engine, task = make(tmp_path, step_acceptance=True, autonomous_recovery=True, max_iterations=20)
    phases = []

    def execute(store, current, *args, **kwargs):
        phases.append('review' if current.get('review_phase') else 'work')
        if not current.get('review_phase'):
            write_document(task, 'PROGRESS.md', '- [x] Implement\n')
        return result()

    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    engine.run(task['id'])
    current = store.get(task['id'])
    assert phases == ['work', 'review', 'review', 'review']
    assert current['status'] == 'blocked'
    assert 'STEP_REVIEW.json' in current['reason']
    assert checklist(current)[0]['done']
    assert len(pending_steps(current)) == 1
