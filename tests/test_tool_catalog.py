from copy import deepcopy

import pytest

from agentvisor.context_budget import ContextBudget
from agentvisor.tool_catalog import START, ToolCatalog, compact_skills


def tool(name, description='Tool help'):
    return {'type': 'function', 'function': {'name': name, 'description': description,
                                          'parameters': {'type': 'object', 'properties': {}}}}


def body():
    return {'messages': [{'role': 'system', 'content': 'Preserve project AGENTS.md exactly.'},
                         {'role': 'user', 'content': 'Design a good export interface.'}],
            'tools': [tool('read'), tool('write'), tool('skill'), tool('list_mcp_resources'),
                      tool('agentvisor_process_exec'), tool('agentvisor_process_select_toolset'),
                      tool('browseros-neo_snapshot'), tool('browseros-neo_act'),
                      tool('blender_execute_blender_code'), tool('blender_get_objects_summary')]}


def names(request):
    return [item['function']['name'] for item in request['tools']]


def skill_metadata(name='impeccable'):
    return ('<skill><name>' + name + '</name><description>' + 'Detailed discovery metadata. ' * 40 +
            '</description><location>C:/skills/' + name + '/SKILL.md</location></skill>')


def test_optional_groups_are_discoverable_without_their_large_schemas():
    catalog = ToolCatalog()
    request = body()
    original_messages = deepcopy(request['messages'])
    metrics = catalog.shape(request)
    assert metrics['tools_before'] == 10
    assert metrics['tools_after'] == 6
    assert names(request) == ['read', 'write', 'skill', 'list_mcp_resources',
                              'agentvisor_process_exec', 'agentvisor_process_select_toolset']
    assert [group['name'] for group in catalog.catalogue] == ['blender', 'browseros-neo']
    assert 'browseros-neo' in request['messages'][0]['content']
    assert request['messages'][0]['content'].endswith(original_messages[0]['content'])
    assert request['messages'][1:] == original_messages[1:]


def test_selecting_browser_then_blender_keeps_at_most_one_group():
    catalog = ToolCatalog()
    catalog.shape(body())
    selected = catalog.select('browseros-neo')
    assert selected['active_toolset'] == 'browseros-neo'
    request = body()
    catalog.shape(request)
    assert 'browseros-neo_act' in names(request)
    assert 'blender_execute_blender_code' not in names(request)
    catalog.select('blender')
    request = body()
    catalog.shape(request)
    assert 'browseros-neo_act' not in names(request)
    assert 'blender_execute_blender_code' in names(request)
    catalog.select('none')
    request = body()
    assert catalog.shape(request)['tools_after'] == 6


def test_large_toolset_exposes_starter_tools_and_can_select_any_member():
    catalog = ToolCatalog()
    request = body()
    request['tools'] += [tool('browseros-neo_' + str(index)) for index in range(20)]
    catalog.shape(request)
    selected = catalog.select('browseros-neo')
    assert len(selected['tools']) == 4
    assert len(selected['available_tools']) == 22
    assert 'browseros-neo_snapshot' in selected['tools']
    request = body()
    request['tools'] += [tool('browseros-neo_' + str(index)) for index in range(20)]
    catalog.shape(request)
    assert len([name for name in names(request) if name.startswith('browseros-neo_')]) == 4
    selected = catalog.select('browseros-neo_19')
    assert selected['tools'] == ['browseros-neo_19']
    request = body()
    request['tools'] += [tool('browseros-neo_' + str(index)) for index in range(20)]
    catalog.shape(request)
    assert names(request)[-1] == 'browseros-neo_19'


def test_budget_fit_releases_optional_schemas_without_losing_core_or_selection():
    catalog = ToolCatalog()
    request = body()
    request['tools'] += [tool('browseros-neo_' + str(index), 'schema ' * 15000)
                         for index in range(10)]
    catalog.shape(request)
    catalog.select('browseros-neo')
    request = body()
    request['tools'] += [tool('browseros-neo_' + str(index), 'schema ' * 15000)
                         for index in range(10)]
    catalog.shape(request)
    before = names(request)
    decision = catalog.fit_budget(request, ContextBudget(32768))
    assert decision.action == 'allow'
    assert len(names(request)) < len(before)
    assert names(request)[:6] == before[:6]
    assert len([name for name in names(request) if name.startswith('browseros-neo_')]) >= 1


def test_unknown_toolset_does_not_change_current_selection():
    catalog = ToolCatalog()
    catalog.shape(body())
    catalog.select('browseros-neo')
    with pytest.raises(ValueError, match='Unknown toolset'):
        catalog.select('imaginary')
    assert next(item for item in catalog.catalogue if item['active'])['name'] == 'browseros-neo'


def test_explicit_tool_choice_activates_required_group_even_after_another_selection():
    catalog = ToolCatalog()
    catalog.shape(body())
    catalog.select('blender')
    request = body()
    request['tool_choice'] = {'type': 'function', 'function': {'name': 'browseros-neo_act'}}
    original = deepcopy(request['tool_choice'])
    catalog.shape(request)
    assert 'browseros-neo_act' in names(request)
    assert 'blender_execute_blender_code' not in names(request)
    assert request['tool_choice'] == original


def test_general_required_tool_choice_does_not_load_optional_tools():
    request = body()
    request['tool_choice'] = 'required'
    ToolCatalog().shape(request)
    assert len(names(request)) == 6


def test_shape_is_idempotent_and_does_not_duplicate_markers():
    catalog = ToolCatalog()
    request = body()
    request['messages'][0]['content'] += '<available_skills>' + skill_metadata() + '</available_skills>'
    catalog.shape(request)
    once = deepcopy(request)
    catalog.shape(request)
    assert request == once
    assert request['messages'][0]['content'].count(START) == 1


def test_no_tools_auxiliary_request_is_untouched():
    catalog = ToolCatalog()
    catalog.shape(body())
    request = {'messages': [{'role': 'user', 'content': 'Summarize this conversation.'}]}
    original = deepcopy(request)
    catalog.shape(request)
    assert request == original


def test_previous_catalogue_cannot_bypass_a_tools_gate():
    catalog = ToolCatalog()
    catalog.shape(body())
    catalog.select('browseros-neo')
    request = body()
    request['tools'] = [tool('agentvisor_process_apply_user_instructions')]
    catalog.shape(request)
    assert names(request) == ['agentvisor_process_apply_user_instructions']


def test_standard_mcp_prefix_and_unknown_native_tools_are_handled_conservatively():
    request = body()
    request['tools'] = [tool('mcp__github__list_issues'), tool('question'), tool('custom'),
                        tool('mcp__agentvisor_process__get_progress')]
    catalog = ToolCatalog()
    catalog.shape(request)
    assert names(request) == ['question', 'custom', 'mcp__agentvisor_process__get_progress']
    assert catalog.catalogue[0]['name'] == 'github'


def test_project_instructions_quoting_catalogue_tags_are_not_erased():
    request = body()
    text = '<agentvisor-tool-catalogue>\nPreserve this explicit project rule.\n</agentvisor-tool-catalogue>\n\n'
    request['messages'][0]['content'] = text
    ToolCatalog().shape(request)
    assert request['messages'][0]['content'].endswith(text)


def test_only_known_catalogue_metadata_is_compacted_and_all_names_survive():
    before = 'AGENTS: never delete data.\n'
    after = '\nExplicit instruction: respect every acceptance criterion.'
    text = before + '<available_skills>' + skill_metadata('alpha') + skill_metadata('beta') + '</available_skills>' + after
    compact = compact_skills(text)
    assert compact.startswith(before) and compact.endswith(after)
    assert len(compact) < len(text)
    assert '"name":"alpha"' in compact and '"name":"beta"' in compact
    assert 'complete instructions' in compact
    assert 'C:/skills/' not in compact


@pytest.mark.parametrize('inside', [
    'Always obey this instruction.' + skill_metadata(),
    skill_metadata() + '<instructions>Never modify user files</instructions>',
    '<skill><name>broken</name><description>missing location</description></skill>',
    '<skill><name>broken</name><description>unterminated',
])
def test_unrecognized_catalogue_or_embedded_instructions_are_preserved(inside):
    text = '<available_skills>' + inside + '</available_skills>'
    assert compact_skills(text) == text


def test_user_and_tool_skill_instructions_are_never_compacted():
    request = body()
    content = '<available_skills>' + skill_metadata() + '</available_skills>'
    request['messages'] += [{'role': 'user', 'content': content},
                            {'role': 'tool', 'tool_call_id': 'skill-1', 'content': content}]
    original = deepcopy(request['messages'][1:])
    ToolCatalog().shape(request)
    assert request['messages'][1:] == original


def test_system_content_parts_keep_nontext_parts_and_project_instructions():
    request = body()
    image = {'type': 'image_url', 'image_url': {'url': 'https://example.test/image'}}
    request['messages'][0]['content'] = [{'type': 'text', 'text': 'Project rules unchanged.'}, image]
    ToolCatalog().shape(request)
    assert request['messages'][0]['content'][1:] == [
        {'type': 'text', 'text': 'Project rules unchanged.'}, image]


def test_lazy_tools_release_context_for_useful_work():
    request = body()
    request['tools'] += [tool('blender_large_schema', 'schema ' * 10000),
                         tool('browseros-neo_large_schema', 'schema ' * 10000)]
    request['messages'][0]['content'] += '<available_skills>' + skill_metadata() * 20 + '</available_skills>'
    budget = ContextBudget(65536)
    before = budget.assess(request)
    catalog = ToolCatalog()
    metrics = catalog.shape(request)
    after = budget.assess(request)
    assert before.metrics['static_tokens'] > after.metrics['static_tokens'] + 35000
    assert metrics['skills_chars_saved'] > 15000
    assert after.action == 'allow'
