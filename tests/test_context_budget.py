"""Regressions for overflow that a runtime hides behind a successful response."""
from copy import deepcopy

import pytest

from agentvisor.context_budget import ContextBudget, static_body


def request(history='', *, system='Follow the goal.', tools=None):
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Implement export.'}]
    if history:
        messages += [{'role': 'assistant', 'tool_calls': [
            {'id': 'read-1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-1', 'content': history}]
    return {'messages': messages, 'tools': tools or [], 'max_tokens': 4096}


def test_complete_history_is_rejected_before_hidden_runtime_truncation():
    controller = ContextBudget(65536)
    # Equivalent order of magnitude to the incident's 2.67 MB / 784k tokens.
    body = request('source and old results\n' * 120000)
    untouched = deepcopy(body)
    decision = controller.assess(body)
    assert decision.action == 'rotate'
    assert decision.reason == 'context_budget_exhausted'
    assert decision.metrics['estimated_tokens'] > 65536
    assert decision.metrics['static_tokens'] < decision.metrics['input_limit']
    assert body == untouched  # No rolling-window or middle truncation.


def test_single_large_tool_result_cannot_bypass_context_guard():
    decision = ContextBudget(8192).assess(request('log\n' * 20000))
    assert decision.action == 'rotate'
    assert decision.metrics['message_count'] == 4


def test_schema_growth_and_full_initial_instructions_are_counted():
    # Synthetic dimensions of the actual static prompt; no private log text.
    system = 'a' * 60000 + 'я' * 13919
    tools = [{'type': 'function', 'function': {
        'name': f'tool_{index}', 'description': 'schema description ' * 56,
        'parameters': {'type': 'object', 'properties': {}}}} for index in range(65)]
    controller = ContextBudget(65536)
    body = request(system=system, tools=tools)
    static = controller.assess(body)
    assert static.action == 'allow'
    assert static.metrics['static_tokens'] > 38000
    assert static.metrics['tool_count'] == 65
    expanded = controller.assess(request('read result\n' * 5000, system=system, tools=tools))
    assert expanded.action == 'rotate'
    assert expanded.metrics['static_tokens'] == static.metrics['static_tokens']


def test_oversized_static_prompt_is_not_a_restart_request():
    body = request(system='irreducible instructions\n' * 30000)
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'blocked'
    assert decision.reason == 'static_context_overflow'
    assert ContextBudget(65536).assess(body).action == 'blocked'


def test_oversized_tools_alone_are_static_failure():
    body = request(tools=[{'type': 'function', 'function': {
        'name': 'all_schemas', 'description': 'Huge schema ' * 30000}}])
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'blocked'
    assert decision.metrics['static_tokens'] > decision.metrics['input_limit']


def test_summary_and_title_requests_without_tools_are_still_guarded():
    body = {'messages': [{'role': 'user', 'content': 'summarize\n' * 30000}], 'max_tokens': 2048}
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'blocked'
    assert decision.metrics['tool_count'] == 0
    body['messages'] = request('history\n' * 30000)['messages']
    assert ContextBudget(65536).assess(body).action == 'rotate'


def test_unicode_cost_uses_utf8_not_only_character_count():
    controller = ContextBudget(65536)
    ascii_size = controller.assess(request('x' * 10000)).metrics['estimated_tokens']
    russian_size = controller.assess(request('я' * 10000)).metrics['estimated_tokens']
    chinese_size = controller.assess(request('界' * 10000)).metrics['estimated_tokens']
    emoji_size = controller.assess(request('🚀' * 10000)).metrics['estimated_tokens']
    assert ascii_size < russian_size < chinese_size < emoji_size


@pytest.mark.parametrize('context,output', [(0, 1), (True, 1), (None, 1), (512, 4096), (8192, 0)])
def test_invalid_or_exhausted_configuration_is_not_retried(context, output):
    body = {'messages': [{'role': 'user', 'content': 'go'}]}
    assert ContextBudget(context, output).assess(body).reason == 'invalid_context_budget'


def test_minimum_real_context_works_with_a_small_response_reserve():
    result = ContextBudget(1024, 128).assess({'messages': [{'role': 'user', 'content': 'go'}]})
    assert result.action == 'allow'
    assert result.metrics['output_reserve'] == 128
    assert result.metrics['safety_reserve'] == 205


def test_reserves_both_output_token_spellings_conservatively():
    body = request()
    body['max_completion_tokens'] = 8000
    result = ContextBudget(8192).assess(body)
    assert result.action == 'blocked'
    assert result.metrics['output_reserve'] == 8000


def test_legacy_functions_and_structured_output_schemas_are_not_free():
    body = request()
    body['functions'] = [{'name': 'legacy', 'description': 'schema ' * 5000}]
    body['response_format'] = {'type': 'json_schema', 'json_schema': {'description': 'schema ' * 5000}}
    result = ContextBudget(16384).assess(body)
    assert result.action == 'blocked'
    assert result.metrics['tool_count'] == 1


def test_low_usage_after_truncation_never_relaxes_preflight():
    controller = ContextBudget(65536)
    body = request('history\n' * 5000)
    before = controller.assess(body)
    assert before.action == 'allow'
    controller.observe(before, 2000)
    after = controller.assess(body)
    assert after.metrics['estimated_tokens'] == before.metrics['estimated_tokens']
    assert after.metrics['calibration'] == 1


def test_observed_usage_corrects_underestimation_only_upward_and_survives_handoff():
    controller = ContextBudget(65536)
    body = request('opaque input\n' * 1000)
    before = controller.assess(body)
    observed = before.metrics['estimated_tokens'] * 2
    feedback = controller.observe(before, observed)
    assert feedback['underestimated']
    after = controller.assess(body)
    assert after.metrics['estimated_tokens'] >= observed
    controller.observe(after, 10)
    controller.restore_calibration(0.1)
    assert controller.assess(body).metrics['estimated_tokens'] == after.metrics['estimated_tokens']
    fresh = ContextBudget(65536)
    fresh.restore_calibration(feedback['calibration'])
    assert fresh.assess(body).metrics['estimated_tokens'] >= observed


@pytest.mark.parametrize('usage', [None, 0, -1, True, '2000', 3.5])
def test_invalid_usage_cannot_poison_calibration(usage):
    controller = ContextBudget(65536)
    decision = controller.assess(request())
    assert controller.observe(decision, usage) is None
    assert controller.assess(request()).metrics['calibration'] == 1


def test_provider_count_can_resolve_a_false_positive_estimate():
    body = request(system='highly compressible prompt ' * 12000)
    controller = ContextBudget(65536)
    assert controller.assess(body).action == 'blocked'
    decision = controller.assess(body, prompt_tokens=30000, static_prompt_tokens=30000)
    assert decision.action == 'allow'
    assert decision.metrics['method'] == 'provider_count'
    assert decision.metrics['estimated_tokens'] == 30000


def test_provider_counts_do_not_waive_output_or_safety_reserve():
    result = ContextBudget(65536).assess(request('history'), prompt_tokens=60000, static_prompt_tokens=30000)
    assert result.action == 'rotate'


def test_static_projection_keeps_late_system_and_all_initial_user_instructions():
    body = request('tool result')
    body['messages'].insert(2, {'role': 'user', 'content': 'Also respect this requirement.'})
    body['messages'] += [{'role': 'system', 'content': 'New supervisor rule'},
                         {'role': 'user', 'content': 'Conversation turn'}]
    untouched = deepcopy(body)
    projection = static_body(body)
    assert [m['content'] for m in projection['messages']] == [
        'Follow the goal.', 'Implement export.', 'Also respect this requirement.', 'New supervisor rule']
    assert body == untouched


def test_media_is_not_counted_as_only_the_url_length():
    body = {'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'https://example.test/large.png'}}]}]}
    controller = ContextBudget(65536)
    decision = controller.assess(body)
    assert decision.action == 'allow'
    assert decision.metrics['image_tokens_estimate'] == 8192
    assert decision.metrics['uncertain_images'] == 1
    assert decision.metrics['estimated_tokens'] > 8192
    counted = controller.assess(body, prompt_tokens=10000)
    assert counted.action == 'allow'
    assert counted.metrics['uncertain_images'] == 0


def test_inline_image_bytes_are_not_treated_as_language_tokens():
    body = {'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'a' * 2000000}}]}]}
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'allow'
    assert decision.metrics['prompt_bytes'] > 2000000
    assert decision.metrics['estimated_tokens'] < 10000


def test_many_screenshots_in_history_trigger_handoff():
    body = request()
    body['messages'] += [{'role': 'assistant', 'content': 'Inspect screenshots.'},
                         {'role': 'tool', 'content': [
                             {'type': 'image_url', 'image_url': {'url': 'https://example.test/screen.png'}}
                             for _ in range(8)]}]
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'rotate'
    assert decision.metrics['image_tokens_estimate'] == 8 * 8192


def test_unbounded_audio_or_video_requires_provider_count():
    body = {'messages': [{'role': 'user', 'content': [{'type': 'input_audio', 'data': 'encoded'}]}]}
    assert ContextBudget(65536).assess(body).reason == 'unsupported_media_requires_token_count'
    assert ContextBudget(65536).assess(body, prompt_tokens=10000).action == 'allow'


def test_media_types_in_tool_schema_remain_ordinary_schema_text():
    body = request(tools=[{'type': 'function', 'function': {'name': 'upload', 'parameters': {
        'type': 'object', 'properties': {'file': {'type': 'file'}, 'image': {'type': 'image_url'}}}}}])
    decision = ContextBudget(65536).assess(body)
    assert decision.action == 'allow'
    assert decision.metrics['uncertain_images'] == 0
