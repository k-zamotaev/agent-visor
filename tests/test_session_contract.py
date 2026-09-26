from copy import deepcopy

import httpx
import pytest

from agentvisor.inference import InferenceGateway
from agentvisor.session_contract import START, apply_session_contract, session_contract
from test_inference import model_server
from test_supervisor import make


@pytest.mark.parametrize('system', [None, 'Original system', [{'type': 'text', 'text': 'Original system'}]])
def test_contract_is_idempotent_and_preserves_tool_exchange(tmp_path, system):
    _, _, task = make(tmp_path)
    exchange = [{'role': 'assistant', 'tool_calls': [{'id': 'one'}]},
                {'role': 'tool', 'tool_call_id': 'one', 'content': 'Unrelated root plan'}]
    body = {'messages': ([{'role': 'system', 'content': system}] if system is not None else []) + deepcopy(exchange)}
    contract = session_contract(task)
    apply_session_contract(body, contract)
    once = deepcopy(body)
    apply_session_contract(body, contract)
    assert body == once
    assert body['messages'][-2:] == exchange
    assert str(body).count(START.strip()) == 1
    assert 'Original system' in str(body) if system is not None else True


@pytest.mark.parametrize('reviewer', [False, True])
def test_gateway_restores_contract_after_compaction_without_manual_context(tmp_path, model_server, reviewer):
    url, requests = model_server
    store, engine, task = make(tmp_path)
    task = dict(task, review_phase=reviewer)
    if reviewer:
        task['review_request'] = {'id': 'review-unique', 'steps': [{'id': 'db-step', 'text': 'Database'}]}
    history = [{'role': 'user', 'content': 'Compacted summary: continue Blender in root PROGRESS.md'},
               {'role': 'assistant', 'tool_calls': [{'id': 'read-one', 'type': 'function',
                 'function': {'name': 'read', 'arguments': '{}'}}]},
               {'role': 'tool', 'tool_call_id': 'read-one', 'content': 'Root plan: Blender'}]
    body = {'model': 'test', 'messages': history, 'stream': True,
            'tools': [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]}
    with InferenceGateway(store, task, dict(task['profile'], base_url=url), engine.cancel) as gateway:
        with httpx.Client(trust_env=False) as client:
            for _ in range(2):
                assert client.post(gateway.base_url + '/chat/completions', json=body).status_code == 200
            auxiliary = {'model': 'test', 'messages': history, 'stream': True}
            assert client.post(gateway.base_url + '/chat/completions', json=auxiliary).status_code == 200
    for request in requests[:2]:
        sent = request['body']['messages']
        assert sent[1:] == history
        assert sent[0]['role'] == 'system'
        assert task['goal'] in sent[0]['content']
        assert f"/tasks/{task['id']}/PROGRESS.md" in sent[0]['content']
        if reviewer:
            assert 'review-unique' in sent[0]['content']
            assert 'db-step' in sent[0]['content']
            assert 'report_path' in sent[0]['content']
    assert requests[2]['body']['messages'] == history
    current = store.get(task['id'])
    assert not current.get('context_additions')
    assert current.get('context_version', 0) == 0
    assert current.get('applied_context_version', 0) == 0
