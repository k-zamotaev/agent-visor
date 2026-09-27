import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentvisor.command_mcp import CommandMCP
from agentvisor.progress_plan import change, initialize, sync_document, view
from agentvisor.step_acceptance import mark_steps, pending_steps
from agentvisor.store import Store
from agentvisor.tasks import checklist, document_path, read_document, write_document
from test_supervisor import make


def fixture(tmp_path):
    store, engine, task = make(tmp_path, step_acceptance=True)
    write_document(task, 'PROGRESS.md', '# Progress\n- [x] Database\n- [x] UI\n- [ ] Export\n')
    task = initialize(store, task)
    first = task['progress_plan']['steps'][0]['id']
    task = store.update(task['id'], step_reviews={'goal_version': 1, 'accepted': {first: {'review_id': 'proved'}}})
    return store, engine, task


def update(store, task, operation, **kwargs):
    current = store.get(task['id'])
    return change(store, task, dict(goal_version=current['goal_version'],
                  expected_revision=current['progress_plan']['revision'], operation=operation, **kwargs))


@pytest.mark.parametrize('bad', ['', 'Steps 1-23 [x] DONE', '- [ ] Database\n',
                                 '- [x] Entire task complete\n', '- [ ] Export\n- [x] UI\n- [x] Database\n'])
def test_file_overwrite_cannot_delete_reorder_reopen_or_complete_plan(tmp_path, bad):
    store, _, task = fixture(tmp_path)
    before = checklist(task)
    write_document(task, 'PROGRESS.md', bad)
    # UI/planning stay correct even before repair or after a crash.
    assert checklist(Store(store.directory).get(task['id'])) == before
    current = sync_document(store, task)
    assert checklist(current) == before
    assert len(pending_steps(current)) == 1
    assert len(current['step_reviews']['accepted']) == 1
    assert '- [x] Database' in read_document(task, 'PROGRESS.md')
    assert '- [ ] Export' in read_document(task, 'PROGRESS.md')
    assert current['progress_plan']['revision'] == task['progress_plan']['revision']


def test_deleted_file_restores_without_importing_summary(tmp_path):
    store, _, task = fixture(tmp_path)
    document_path(task, 'PROGRESS.md').unlink()
    restored = initialize(Store(store.directory), task)
    assert len(checklist(restored)) == 3
    assert 'Database' in read_document(task, 'PROGRESS.md')


def test_old_task_migration_preserves_claims_receipts_and_original(tmp_path):
    store, _, task = fixture(tmp_path)
    assert [step['review_status'] for step in checklist(task)] == ['accepted', 'pending', 'open']
    assert task['progress_plan']['imported_document'].startswith('# Progress\n- [x]')
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT body FROM progress_revisions').fetchone()[0])
    assert saved['imported_document'] == task['progress_plan']['imported_document']


def test_missing_legacy_plan_does_not_guess_completion_from_receipts(tmp_path):
    store, _, task = make(tmp_path)
    task = store.update(task['id'], step_reviews={'goal_version': 1, 'accepted': {'old': {}}})
    write_document(task, 'PROGRESS.md', 'Steps 1-23 done')
    with pytest.raises(ValueError, match='legacy checklist is missing'):
        initialize(store, task)
    assert 'progress_plan' not in store.get(task['id'])


def test_claim_is_pending_until_review_and_rejection_can_reopen(tmp_path):
    store, _, task = fixture(tmp_path)
    export = task['progress_plan']['steps'][2]
    claimed = update(store, task, 'claim', step_id=export['id'], note='pytest: 5 passed')
    assert claimed['steps'][2]['review_status'] == 'pending'
    current = store.get(task['id'])
    assert mark_steps(current, [export], False, store)
    assert checklist(store.get(task['id']))[2]['review_status'] == 'open'
    assert len(store.get(task['id'])['step_reviews']['accepted']) == 1


@pytest.mark.parametrize('operation', ['claim', 'reopen'])
def test_model_cannot_change_accepted_milestone(tmp_path, operation):
    store, _, task = fixture(tmp_path)
    first = task['progress_plan']['steps'][0]
    with pytest.raises(ValueError, match='accepted step is immutable'):
        update(store, task, operation, step_id=first['id'], note='I changed my mind')
    assert store.get(task['id'])['progress_plan'] == task['progress_plan']


@pytest.mark.parametrize('operation', ['delete', 'replace', 'rename', 'reorder', 'accept'])
def test_destructive_or_acceptance_operations_rejected(tmp_path, operation):
    store, _, task = fixture(tmp_path)
    with pytest.raises(ValueError, match='forbidden'):
        update(store, task, operation)


def test_existing_plan_cannot_be_reinitialized_or_renamed_by_append(tmp_path):
    store, _, task = fixture(tmp_path)
    with pytest.raises(ValueError, match='already exists'):
        update(store, task, 'initialize', steps=['Everything done'])
    with pytest.raises(ValueError, match='unique'):
        update(store, task, 'append', steps=['New', 'Database'])
    assert len(checklist(store.get(task['id']))) == 3


def test_append_keeps_ids_receipts_and_history(tmp_path):
    store, _, task = fixture(tmp_path)
    before = view(task)['steps']
    after = update(store, task, 'append', steps=['Polish the interface'])
    assert after['steps'][:3] == before
    assert after['steps'][3]['review_status'] == 'open'
    with store.connect() as db:
        rows = db.execute('SELECT revision,source FROM progress_revisions ORDER BY revision').fetchall()
    assert [tuple(row) for row in rows] == [(1, 'legacy_import'), (2, 'append')]


def test_atomic_revision_check_prevents_concurrent_lost_updates(tmp_path):
    store, _, task = fixture(tmp_path)
    barrier = threading.Barrier(2)
    def worker(name):
        barrier.wait()
        try:
            return change(store, task, dict(goal_version=1, expected_revision=1, operation='append', steps=[name]))
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker, ['Extra A', 'Extra B']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any('Stale plan revision' in str(result) for result in results)
    current = Store(store.directory).get(task['id'])
    assert len(checklist(current)) == 4
    assert current['progress_plan']['revision'] == 2


def test_new_user_goal_can_initialize_new_plan_but_stale_session_cannot(tmp_path):
    store, _, task = fixture(tmp_path)
    changed = store.update(task['id'], goal_version=2, goal='A different application')
    assert len(checklist(changed)) == 3 and not any(step['done'] for step in checklist(changed))
    with pytest.raises(ValueError, match='Goal or review scope changed'):
        update(store, task, 'initialize', steps=['New criterion'])
    current = update(store, changed, 'initialize', steps=['New criterion'])
    assert len(current['steps']) == 1 and current['steps'][0]['review_status'] == 'open'
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM progress_revisions').fetchone()[0] == 2


def test_only_explicit_user_recheck_invalidates_acceptance(tmp_path):
    store, _, task = fixture(tmp_path)
    changed = store.add_context(task['id'], 'Use port 8080')
    assert checklist(changed)[0]['review_status'] == 'accepted'
    changed = store.add_context(task['id'], 'Review behavior again', recheck=True)
    assert checklist(changed)[0]['review_status'] == 'pending'
    with pytest.raises(ValueError, match='scope changed'):
        update(store, task, 'claim', step_id=task['progress_plan']['steps'][0]['id'], note='Old session')


@pytest.mark.parametrize('role', ['reviewer', 'diagnostician'])
def test_non_executor_cannot_update_progress_through_mcp(tmp_path, role):
    store, engine, task = fixture(tmp_path)
    if role == 'reviewer':
        task = dict(task, review_phase=True)
    else:
        task = dict(task, recovery_context={'goal_version': 1, 'repair': True})
    tools = CommandMCP(store, task, engine.cancel)
    names = [s['name'] for s in tools.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']]
    assert 'get_progress' in names and 'update_progress' in names
    assert tools.call('get_progress', {})['steps'][0]['review_status'] == 'accepted'
    result = tools.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
        'name': 'update_progress', 'arguments': {'goal_version': 1, 'expected_revision': 1,
                                               'operation': 'append', 'steps': ['Illicit']}}})['result']
    assert result['isError'] and 'Only the executor' in result['content'][0]['text']
    tools.close()


def test_progress_tools_are_available_without_shell_permission(tmp_path):
    store, engine, task = make(tmp_path)
    task = initialize(store, task)
    tools = CommandMCP(store, task, engine.cancel)
    state = tools.call('get_progress', {})
    result = tools.call('update_progress', {'operation': 'initialize', 'goal_version': 1,
                       'expected_revision': state['revision'], 'steps': ['Implement', 'Verify']})
    assert len(result['steps']) == 2
    with pytest.raises(ValueError, match='permissions'):
        tools.call('exec', {'command': 'echo test'})
    tools.close()


@pytest.mark.parametrize('attempt', ['file_only', 'typed_claim', 'failed_claim'])
def test_real_supervisor_path_consumes_only_protocol_claims(tmp_path, monkeypatch, attempt):
    from test_step_acceptance import report, result
    store, engine, task = make(tmp_path, step_acceptance=True, max_iterations=1, max_failures=1)
    engine.command_builder = None
    engine.command = lambda *args: ['unused-fixture-command']
    monkeypatch.setattr('agentvisor.supervisor.resolve_command_policy', lambda *args: {
        'allowed': False, 'permission': 'deny', 'reason': 'Fixture disables shell execution'})
    phases = []
    def execute(store, current, *args, **kwargs):
        phases.append('review' if current.get('review_phase') else 'work')
        if current.get('review_phase'):
            report(store, current)
            return result()
        state = update(store, current, 'initialize', steps=['Implement behavior'])
        if attempt != 'file_only':
            update(store, current, 'claim', step_id=state['steps'][0]['id'], note='Verification passed')
        write_document(current, 'PROGRESS.md', '- [x] Everything complete\n')
        write_document(current, 'DONE.md', 'goal_version: 1\nDone\n')
        return result(failed=attempt == 'failed_claim', reason='timeout' if attempt == 'failed_claim' else None)
    monkeypatch.setattr('agentvisor.supervisor.execute', execute)
    monkeypatch.setattr('agentvisor.supervisor.finalize_review', lambda *args: args[-2])
    engine.run(task['id'])
    current = store.get(task['id'])
    assert len(checklist(current)) == 1 and checklist(current)[0]['text'] == 'Implement behavior'
    if attempt == 'typed_claim':
        assert phases == ['work', 'review']
        assert checklist(current)[0]['review_status'] == 'accepted'
        assert current['status'] == 'completed_unverified'
    else:
        assert phases == ['work']
        assert checklist(current)[0]['review_status'] == 'open'
        assert current['status'] == 'blocked'
    assert 'Everything complete' not in read_document(current, 'PROGRESS.md')
