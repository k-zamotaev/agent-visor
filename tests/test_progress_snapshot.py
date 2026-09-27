"""Model snapshots must not replay the whole completed plan every session."""
import copy
import json

import pytest

from agentvisor.command_mcp import CommandMCP
from agentvisor.progress_plan import view
from agentvisor.progress_tools import schemas, snapshot
from test_progress_protocol import fixture, update


def test_default_focus_preserves_exact_criteria_and_pending_acceptance(tmp_path):
    _, _, task = fixture(tmp_path)
    state = view(task)
    original = copy.deepcopy(state)
    result = snapshot(state)
    assert result['scope'] == 'focus'
    assert result['counts'] == {'accepted': 1, 'pending': 1, 'open': 1, 'total': 3}
    assert result['steps'] == state['steps'][1:]
    assert result['current_step_id'] == state['steps'][1]['id']
    assert result['next_step_id'] == state['steps'][2]['id']
    assert result['omitted_steps'] == 1
    assert result['revision'] == state['revision']
    assert result['goal_version'] == state['goal_version']
    assert state == original


def test_long_notes_are_explicitly_bounded_and_retrievable_without_loss(tmp_path):
    store, _, task = fixture(tmp_path)
    identity = task['progress_plan']['steps'][2]['id']
    update(store, task, 'claim', step_id=identity, note='Evidence ' * 800)
    update(store, task, 'note', note='Next action ' * 1000)
    state = view(store.get(task['id']))
    focus = snapshot(state)
    assert focus['steps'][1]['note_truncated'] is True
    assert len(focus['steps'][1]['note']) == 1000
    assert focus['notes_truncated'] is True
    assert len(focus['notes']) == 1500
    selected = snapshot(state, {'step_ids': [identity]})
    assert selected['scope'] == 'selected'
    assert selected['steps'] == [state['steps'][2]]
    assert selected['notes'] == state['notes']
    full = snapshot(state, {'full': True})
    for key, value in state.items():
        assert full[key] == value
    assert full['omitted_steps'] == 0


def test_all_user_instructions_and_reference_constraints_survive_default(tmp_path):
    store, _, task = fixture(tmp_path)
    store.add_context(task['id'], 'Use the existing API contract unchanged.', kind='instruction')
    store.add_context(task['id'], 'Browser endpoint and project restrictions.', kind='reference')
    state = view(store.get(task['id']))
    assert snapshot(state)['user_instructions'] == state['user_instructions']
    assert len(snapshot(state)['user_instructions']) == 2


def test_new_goal_does_not_select_old_plan_as_current(tmp_path):
    _, _, task = fixture(tmp_path)
    state = view(dict(task, goal_version=2))
    result = snapshot(state)
    assert result['needs_initialization'] is True
    assert result['steps'] == []
    assert result['current_step_id'] is None and result['next_step_id'] is None
    assert snapshot(state, {'full': True})['steps'] == state['steps']


def test_complete_plan_has_no_next_step_or_reinitialization(tmp_path):
    _, _, task = fixture(tmp_path)
    state = view(task)
    for step in state['steps']:
        step.update(done=True, review_status='accepted')
    result = snapshot(state)
    assert result['steps'] == []
    assert result['current_step_id'] is None and result['next_step_id'] is None
    assert result['needs_initialization'] is False
    assert result['counts']['accepted'] == 3


def test_pending_review_is_current_even_when_an_earlier_step_is_open(tmp_path):
    _, _, task = fixture(tmp_path)
    state = view(task)
    state['steps'][1].update(done=False, review_status='open')
    state['steps'][2].update(done=True, review_status='pending')
    result = snapshot(state)
    assert result['steps'] == [state['steps'][2], state['steps'][1]]
    assert result['current_step_id'] == result['steps'][0]['id']
    assert result['next_step_id'] == result['steps'][1]['id']


@pytest.mark.parametrize('arguments', [[], {'extra': 1}, {'full': 1}, {'step_ids': []},
    {'step_ids': 'one'}, {'step_ids': ['']}, {'step_ids': [1]}, {'step_ids': ['one', 'one']},
    {'step_ids': ['missing']}, {'full': True, 'step_ids': ['one']}])
def test_invalid_scope_cannot_silently_fall_back_to_full_plan(tmp_path, arguments):
    _, _, task = fixture(tmp_path)
    with pytest.raises(ValueError):
        snapshot(view(task), arguments)


def test_mcp_updates_return_compact_revision_and_exact_changed_ids(tmp_path):
    store, engine, task = fixture(tmp_path)
    tools = CommandMCP(store, task, engine.cancel)
    try:
        before = tools.call('get_progress', {})
        result = tools.call('update_progress', {'operation': 'append', 'goal_version': 1,
            'expected_revision': before['revision'], 'steps': ['Verify export accessibility']})
        assert result['revision'] == before['revision'] + 1
        assert result['scope'] == 'focus' and len(result['steps']) == 2
        assert len(result['changed_step_ids']) == 1
        selected = tools.call('get_progress', {'step_ids': result['changed_step_ids']})
        assert selected['steps'][0]['text'] == 'Verify export accessibility'
        assert selected['steps'][0]['review_status'] == 'open'
        with pytest.raises(ValueError, match='Stale plan revision'):
            tools.call('update_progress', {'operation': 'append', 'goal_version': 1,
                'expected_revision': before['revision'], 'steps': ['Stale write']})
        full = tools.call('get_progress', {'full': True})
        assert len(full['steps']) == 4
        assert full['steps'][0]['review_status'] == 'accepted'
    finally:
        tools.close()


def test_mcp_instruction_linking_preserves_receipts_and_returns_created_ids(tmp_path):
    store, engine, task = fixture(tmp_path)
    task = store.add_context(task['id'], 'Improve the design and verify keyboard operation.')
    tools = CommandMCP(store, task, engine.cancel)
    try:
        before = tools.call('get_progress', {})
        result = tools.call('apply_user_instructions', {'goal_version': 1,
            'expected_revision': before['revision'], 'context_versions': [1],
            'steps': ['Implement accessible design and browser verification'], 'existing_step_ids': []})
        assert result['scope'] == 'focus'
        assert result['user_instructions'][0]['text'] == task['context_additions'][0]['text']
        assert result['user_instructions'][0]['state'] == 'planned'
        assert result['changed_step_ids'] == result['user_instructions'][0]['step_ids']
        assert result['counts']['accepted'] == 1
    finally:
        tools.close()


def test_completed_criteria_do_not_scale_up_routine_model_payload(tmp_path):
    _, _, task = fixture(tmp_path)
    state = view(task)
    completed = dict(state['steps'][0])
    state['steps'] = [dict(completed, id=f'accepted-{index}',
                           text=f'{index}: ' + 'Exact accepted acceptance criterion ' * 30,
                           note='Stored verification evidence ' * 80)
                      for index in range(35)] + state['steps'][1:]
    full = json.dumps(snapshot(state, {'full': True}), ensure_ascii=False)
    compact = json.dumps(snapshot(state), ensure_ascii=False)
    assert len(compact) < len(full) / 20
    assert snapshot(state)['steps'] == state['steps'][-2:]
    assert snapshot(state)['counts']['total'] == 37


def test_schema_advertises_detail_scope_and_forbids_unknown_fields():
    schema = next(tool for tool in schemas() if tool['name'] == 'get_progress')['inputSchema']
    assert set(schema['properties']) == {'full', 'step_ids'}
    assert schema['additionalProperties'] is False
