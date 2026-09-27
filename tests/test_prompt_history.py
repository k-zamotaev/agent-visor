from copy import deepcopy
import json

from agentvisor.prompt_history import failure_prompt, recipe_prompt
from agentvisor.tasks import prepare_documents
from agentvisor.progress_tools import PROTOCOL
from agentvisor.session_contract import session_contract
from test_supervisor import make


def test_failure_prompt_keeps_recent_failure_without_clixml_or_old_history():
    xml = ('#< CLIXML\n<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04">'
           '<S S="Error">Get-Content: missing conftest.py_x000D__x000A_</S>'
           '<Obj><S N="Computer">HOST-SERIALIZATION-NOISE</S></Obj></Objs>')
    task = {'goal_version': 2, 'command_failures': {'goal_version': 2, 'items': [
        {'fingerprint': 'obsolete', 'command': 'old unrelated survey', 'output': 'old failure'},
        {'fingerprint': 'latest', 'command': 'Get-Content conftest.py', 'output': xml,
         'exit_code': 1, 'attempts': 2, 'status': 'completed'}]},
        'recovery_context': {'goal_version': 2, 'tool_failures': [
            {'input': {'fingerprint': 'latest'}}]}}
    original = deepcopy(task)
    prompt = failure_prompt(task)
    assert 'missing conftest.py' in prompt and '"fingerprint": "latest"' in prompt
    assert 'old unrelated survey' not in prompt and 'HOST-SERIALIZATION-NOISE' not in prompt
    assert 'CLIXML' not in prompt and '<Objs' not in prompt
    assert '"omitted_items": 1' in prompt
    assert task == original


def test_failure_history_is_bounded_and_readable_without_hiding_omission():
    task = {'goal_version': 1, 'command_failures': {'goal_version': 1, 'items': [
        {'fingerprint': f'failure-{i}', 'command': 'x' * 50000,
         'repair_note': 'r' * 30000, 'cwd': 'd' * 30000, 'output': 'o' * 50000,
         'status': 'completed', 'exit_code': 9} for i in range(8)]}}
    prompt = failure_prompt(task)
    assert len(prompt) < 3000
    assert max(map(len, prompt.splitlines())) < 2000
    assert 'failure-7' in prompt and 'failure-0' not in prompt
    assert 'omitted_items' in prompt and 'full_details' in prompt
    task['goal_version'] = 2
    assert failure_prompt(task) == ''


def test_recipe_prompt_excludes_other_steps_tasks_and_goals():
    entry = {'provenance': {'task_id': 'task', 'goal_version': 2, 'step_id': 'current'},
             'commands': [{'command': 'pytest test_export.py', 'cwd_relative': 'backend',
                           'shell': 'powershell', 'output_tail': '7 passed'}]}
    task = {'id': 'task', 'goal_version': 2, 'task_memory': {'current_step': {'id': 'current'}}}
    variants = [deepcopy(entry) for _ in range(4)]
    variants[0]['provenance']['task_id'] = 'other'
    variants[1]['provenance']['goal_version'] = 1
    variants[2]['provenance']['step_id'] = 'old-step'
    for index in range(3):
        variants[index]['commands'][0]['command'] = f'UNRELATED-{index}'
    task['recipe_context'] = 'WORKSPACE RECIPE LIBRARY:\n' + json.dumps(variants) + '\nInstructions'
    original = deepcopy(task)
    prompt = recipe_prompt(task)
    assert 'pytest test_export.py' in prompt and 'UNRELATED' not in prompt
    assert len(prompt) < 1500 and max(map(len, prompt.splitlines())) < 2000
    assert task == original
    task['recipe_context'] = 'WORKSPACE RECIPE LIBRARY:\n' + json.dumps(variants[:3])
    assert recipe_prompt(task) == ''


def test_runtime_prompt_uses_single_protocol_and_readable_recovery_json(tmp_path):
    store, _, task = make(tmp_path)
    task = store.update(task['id'], recovery_context={
        'goal_version': task['goal_version'], 'session_handoff': True,
        'reason': 'context_handoff', 'error': 'Use the inspected paths',
        'history': [{'iteration': i, 'error': 'Long historical error ' * 40} for i in range(8)],
        'boundary': {'context_limit': 65536, 'input_limit': 48332, 'estimated_tokens': 49000}})
    prompt = prepare_documents(task, {'instance': 'fake', 'context': 65536,
                                     'command_mcp_url': 'http://127.0.0.1/fake'})
    # The full immutable protocol is reattached by the gateway every request;
    # the bootstrap file should not carry another large copy.
    assert PROTOCOL not in prompt and PROTOCOL in session_contract(task)
    assert '\n  "reason": "context_handoff"' in prompt
    assert max(map(len, prompt.splitlines())) < 2000
