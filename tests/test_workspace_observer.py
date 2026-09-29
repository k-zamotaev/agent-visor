from agentvisor.workspace_observer import changes, snapshot


def test_detects_created_modified_and_deleted_product_files(tmp_path):
    before = snapshot(tmp_path)
    target = tmp_path / 'nested' / 'result.txt'
    target.parent.mkdir()
    target.write_text('first', encoding='utf-8')
    after_create = snapshot(tmp_path)
    assert changes(before, after_create) == ['nested/result.txt']
    target.write_text('second version', encoding='utf-8')
    after_edit = snapshot(tmp_path)
    assert changes(after_create, after_edit) == ['nested/result.txt']
    target.unlink()
    assert changes(after_edit, snapshot(tmp_path)) == ['nested/result.txt']


def test_ignores_supervisor_state_and_never_claims_incomplete_scan(tmp_path):
    state = tmp_path / '.agentvisor' / 'tasks' / 'task'
    state.mkdir(parents=True)
    before = snapshot(tmp_path)
    (state / 'RUN_PROMPT.md').write_text('updated', encoding='utf-8')
    (tmp_path / 'agent.log').write_text('volatile', encoding='utf-8')
    assert changes(before, snapshot(tmp_path)) == []
    (tmp_path / 'result.txt').write_text('product', encoding='utf-8')
    limited = snapshot(tmp_path, max_files=0)
    assert limited['complete'] is False
    assert changes(before, limited) == []
