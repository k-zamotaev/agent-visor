from copy import deepcopy

import httpx
import pytest

from agentvisor.inference import CONTEXT_START, InferenceGateway, apply_task_context
from agentvisor.tasks import prepare_documents, write_document
from test_inference import model_server
from test_supervisor import make


PRIMARY_TOOLS = [{'type': 'function', 'function': {'name': 'read',
                 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]


def conversation():
    return [
        {'role': 'system', 'content': 'Follow the user task.'},
        {'role': 'user', 'content': 'Build the application.'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'read-1', 'type': 'function', 'function': {
                'name': 'read', 'arguments': '{"path":"README.md"}'}}]},
        {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'Existing project documentation'},
    ]


def test_standing_context_does_not_become_a_new_turn_after_tool_output():
    history = conversation()
    body = {'messages': deepcopy(history)}
    apply_task_context(body, [{'version': 1, 'text': 'Keep the existing database.'}])
    assert len(body['messages']) == len(history)
    assert body['messages'][0] == history[0]
    assert body['messages'][2:] == history[2:]
    goal = body['messages'][1]
    assert goal['role'] == 'user'
    assert goal['content'].endswith(history[1]['content'])
    assert 'Keep the existing database.' in goal['content']
    assert body['messages'][-1]['role'] == 'tool'


def test_reapplying_or_updating_context_replaces_one_stable_block():
    body = {'messages': conversation()}
    notes = [{'version': 1, 'text': 'Do not migrate data.'}]
    apply_task_context(body, notes)
    once = deepcopy(body)
    apply_task_context(body, notes)
    assert body == once
    notes.append({'version': 2, 'text': 'Use port 8015.'})
    apply_task_context(body, notes)
    goal = body['messages'][1]['content']
    assert goal.count(CONTEXT_START) == 1
    assert goal.count('Do not migrate data.') == 1
    assert goal.count('Use port 8015.') == 1
    assert goal.endswith('Build the application.')
    assert body['messages'][2:] == once['messages'][2:]


def test_empty_context_leaves_complete_history_unchanged():
    body = {'messages': conversation()}
    original = deepcopy(body)
    apply_task_context(body, [])
    assert body == original


def test_multimodal_goal_keeps_images_and_original_text():
    parts = [{'type': 'text', 'text': 'Build this interface.'},
             {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    body = {'messages': [{'role': 'user', 'content': deepcopy(parts)}]}
    notes = [{'version': 1, 'text': 'Use a dark theme.'}]
    apply_task_context(body, notes)
    once = deepcopy(body)
    apply_task_context(body, notes)
    assert body == once
    assert body['messages'][0]['content'][1:] == parts
    assert 'Use a dark theme.' in body['messages'][0]['content'][0]['text']


def test_client_normalized_text_block_does_not_lose_original_goal():
    body = {'messages': [{'role': 'user', 'content': 'Original goal'}]}
    notes = [{'version': 1, 'text': 'A note'}]
    apply_task_context(body, notes)
    normalized = body['messages'][0]['content']
    body['messages'][0]['content'] = [{'type': 'text', 'text': normalized}]
    apply_task_context(body, notes)
    assert body['messages'][0]['content'][1] == {'type': 'text', 'text': 'Original goal'}


@pytest.mark.parametrize('prefix', [[], [{'role': 'system', 'content': 'A system instruction'}]])
def test_compacted_history_gets_context_before_complete_tool_exchange(prefix):
    history = deepcopy(prefix) + conversation()[2:]
    body = {'messages': deepcopy(history)}
    notes = [{'version': 1, 'text': 'Retain the existing files.'}]
    apply_task_context(body, notes)
    assert body['messages'][:len(prefix)] == prefix
    assert body['messages'][len(prefix)]['role'] == 'user'
    assert body['messages'][len(prefix) + 1:] == history[len(prefix):]
    once = deepcopy(body)
    apply_task_context(body, notes)
    assert body == once


def test_gateway_keeps_context_stable_and_delivers_new_version_on_next_request(tmp_path, model_server):
    url, requests = model_server
    store, engine, task = make(tmp_path)
    store.add_context(task['id'], 'Keep the existing database.')
    body = {'model': 'test', 'messages': conversation(), 'stream': True, 'tools': PRIMARY_TOOLS}
    profile = dict(task['profile'], base_url=url)
    with InferenceGateway(store, task, profile, engine.cancel) as gateway, httpx.Client(trust_env=False) as client:
        endpoint = gateway.base_url + '/chat/completions'
        for _ in range(2):
            assert client.post(endpoint, json=body).status_code == 200
        assert requests[0]['body']['messages'] == requests[1]['body']['messages']
        assert store.get(task['id'])['applied_context_version'] == 1
        store.add_context(task['id'], 'Use port 8015.')
        assert client.post(endpoint, json=body).status_code == 200
    sent = requests[2]['body']['messages']
    assert len(sent) == len(body['messages'])
    assert 'Use port 8015.' in sent[1]['content']
    assert sent[2:] == body['messages'][2:]
    assert sent[-1]['role'] == 'tool'
    updated = store.get(task['id'])
    assert updated['applied_context_version'] == updated['context_version'] == 2
    assert updated['goal_version'] == 1


@pytest.mark.parametrize('auxiliary_tools', [None, []])
def test_auxiliary_title_cannot_consume_context_or_allow_task_completion(tmp_path, model_server, auxiliary_tools):
    url, requests = model_server
    store, engine, task = make(tmp_path)
    prepare_documents(task, {'instance': 'fake', 'context': 16384})
    write_document(task, 'PROGRESS.md', '- [x] Finish original task\n')
    write_document(task, 'DONE.md', f'goal_version: {task["goal_version"]}\nOriginal task done\n')
    store.update(task['id'], status='running', applied_goal_version=task['goal_version'])
    primary = {'model': 'test', 'messages': conversation(), 'stream': True, 'tools': PRIMARY_TOOLS}
    auxiliary = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Generate a short task title.'}],
                 'stream': True}
    if auxiliary_tools is not None:
        auxiliary['tools'] = auxiliary_tools
    profile = dict(task['profile'], base_url=url)
    with InferenceGateway(store, task, profile, engine.cancel) as gateway, httpx.Client(trust_env=False) as client:
        endpoint = gateway.base_url + '/chat/completions'
        assert client.post(endpoint, json=primary).status_code == 200
        store.add_context(task['id'], 'Verify the accessibility requirement too.')
        assert client.post(endpoint, json=auxiliary).status_code == 200
        pending = store.get(task['id'])
        assert pending['context_version'] == 1 and pending.get('applied_context_version', 0) == 0
        assert requests[1]['body']['messages'] == auxiliary['messages']
        assert engine.complete(pending) is False
        assert store.get(task['id'])['status'] == 'running'
        assert client.post(endpoint, json=primary).status_code == 200
    delivered = store.get(task['id'])
    assert delivered['applied_context_version'] == delivered['context_version'] == 1
    assert 'Verify the accessibility requirement too.' in requests[2]['body']['messages'][1]['content']
    assert engine.complete(delivered) is True
