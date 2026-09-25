from agentvisor.hardware import recommend
from agentvisor.models import ModelRuntime
from agentvisor.tasks import Profile


def test_recommendation_excludes_non_tool_models_and_reserves_memory():
    machine = {'gpus': [{'free': 24 * 1024**3}], 'ram_available': 50 * 1024**3}
    models = [{'id': 'roleplay', 'size': 12 * 1024**3, 'tool_use': False},
              {'id': 'too-large', 'size': 23 * 1024**3, 'tool_use': True},
              {'id': 'coding', 'size': 16 * 1024**3, 'tool_use': True}]
    selected = recommend(models, machine)
    assert selected['model'] == 'coding'
    assert selected['confidence'] == 'estimate'
    assert selected['output_limit'] < selected['context'] // 2


def test_remote_lmstudio_reuses_matching_loaded_instance():
    runtime = ModelRuntime()
    calls = []

    def request(profile, path, body=None, **kwargs):
        calls.append(path)
        return {'models': [{'key': 'coding', 'loaded_instances': [
            {'id': 'loaded-model', 'config': {'context_length': 16384}}]}]}

    runtime.request = request
    ready = runtime.ensure(Profile(model='coding', base_url='http://runtime:1234').model_dump())
    assert ready == {'instance': 'loaded-model', 'context': 16384}
    assert calls == ['/api/v1/models']


def test_remote_lmstudio_refuses_unconfirmed_context(tmp_path):
    import pytest
    runtime = ModelRuntime(tmp_path)
    runtime.request = lambda profile, path, body=None, **kw: (
        {'models': []} if path == '/api/v1/models' else
        {'instance_id': 'loaded-model', 'load_config': {'context_length': 4096}})
    with pytest.raises(RuntimeError, match='контекста'):
        runtime.ensure(Profile(model='coding', base_url='http://runtime:1234').model_dump())


def test_ollama_benchmark_distinguishes_generation_and_request_time():
    runtime = ModelRuntime()
    calls = []

    def request(profile, path, body=None, **kwargs):
        calls.append(body)
        return {'model': 'coding', 'eval_count': 100, 'eval_duration': 2_000_000_000}

    runtime.request = request
    result = runtime.benchmark(Profile(runtime='ollama', model='coding').model_dump())
    assert result['generation_tps'] == 50
    assert calls[0]['options']['num_ctx'] == 16384
    assert calls[1]['options']['num_predict'] == 180
    assert result['request_tps'] != result['generation_tps']
