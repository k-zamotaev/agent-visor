from types import SimpleNamespace

import pytest

from agentvisor.tasks import Profile, prepare_documents, NewTask
from agentvisor.store import Store
from agentvisor.tuning import select_profile, GIB
from agentvisor.models import ModelRuntime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.tuning.hardware', lambda **kwargs: {
        'cpu': 'test', 'ram_total': 64 * GIB, 'ram_available': 48 * GIB,
        'gpus': [{'name': 'test gpu', 'free': 24 * GIB, 'total': 24 * GIB}]})
    monkeypatch.setattr('agentvisor.tuning.executable', lambda name: '/fake/lms')
    models = [{'id': 'large', 'params': '27B', 'size': 18 * GIB, 'tool_use': True,
               'max_context': 131072, 'reasoning': {'allowed_options': ['off', 'medium', 'xhigh']}, 'instances': []},
              {'id': 'small', 'params': '7B', 'size': 5 * GIB, 'tool_use': True,
               'max_context': 32768, 'instances': []}]

    def estimate(profile, cancel):
        amount = (18 if profile['model'] == 'large' else 5) * GIB + profile['context'] / 8192 * GIB
        return {'gpu_bytes': int(amount), 'total_bytes': int(amount)}

    return SimpleNamespace(service=SimpleNamespace(directory=tmp_path, notify=lambda *a: None, matches=lambda p: True),
                           prepare=lambda *args: None, local=lambda p: True, identifier=ModelRuntime.identifier,
                           owns_instance=lambda profile, identifier: identifier.startswith('agentvisor-'),
                           inventory=lambda p: {'online': True, 'models': models}, memory_estimate=estimate,
                           benchmark=lambda p, cancel: {'profile': p, 'generation_tps': 10 if p['model'] == 'large' else 40})


def test_auto_prefers_capacity_with_memory_reserve_and_model_context_limit(runtime):
    plan = select_profile(runtime, Profile().model_dump(), measure=False)
    assert plan['profile']['model'] == 'large'
    assert plan['profile']['context'] == 65536
    assert plan['profile']['flash_attention'] == 'on'
    assert plan['profile']['cache_type_k'] == plan['profile']['cache_type_v'] == 'q8_0'
    assert plan['estimate']['defaults_only'] is True
    assert plan['profile']['reasoning'] == 'xhigh'
    assert plan['gpu_budget'] < 24 * GIB
    small = select_profile(runtime, Profile(model='small').model_dump(), measure=False)
    assert small['profile']['context'] == 32768


def test_calibration_checks_speed_before_choosing_smaller_model(runtime):
    plan = select_profile(runtime, Profile().model_dump())
    assert plan['profile']['model'] == 'small'
    assert [sample['generation_tps'] for sample in plan['samples']] == [10, 10, 40]


def test_manual_settings_survive_selection_and_unsupported_reasoning_is_rejected(runtime):
    profile = Profile(model='large', profile_mode='manual', context=8192, output_limit=1024,
                      temperature=0.8, top_p=0.9, top_k=30, reasoning='medium').model_dump()
    plan = select_profile(runtime, profile)
    assert plan['profile'] == profile
    assert plan['samples'] == []
    with pytest.raises(RuntimeError, match='рассуждения'):
        select_profile(runtime, dict(profile, reasoning='high'))


def test_remote_profile_does_not_assume_controller_gpu(runtime):
    runtime.local = lambda profile: False
    runtime.memory_estimate = lambda *a: pytest.fail('No local estimate for remote server')
    plan = select_profile(runtime, Profile(model='large', base_url='http://remote:1234').model_dump(), measure=False)
    assert plan['hardware'] == {'remote': 'http://remote:1234'}
    assert plan['gpu_budget'] == 0
    assert plan['warnings']


def test_opencode_config_contains_explicit_inference_options(tmp_path):
    import json
    from agentvisor.tasks import document_path
    profile = Profile(model='coding', temperature=0.8, top_p=0.93, top_k=30, reasoning='medium').model_dump()
    task = Store(tmp_path / 'state').create(NewTask(name='test', goal='Test parameters', workspace=str(tmp_path), profile=profile).model_dump())
    prepare_documents(task, {'instance': 'coding', 'context': profile['context']})
    config = json.loads(document_path(task, 'opencode.json').read_text(encoding='utf-8'))
    model = config['provider']['agentvisor']['models']['coding']
    assert model['options'] == {'temperature': 0.8, 'top_p': 0.93, 'top_k': 30, 'reasoningEffort': 'medium'}
    assert model['limit']['output'] == profile['output_limit']


def test_calibration_reuses_selected_fallback(runtime):
    profile = Profile().model_dump()
    first = select_profile(runtime, profile)
    runtime.benchmark = lambda *a: pytest.fail('Cached profile must not repeat calibration')
    second = select_profile(runtime, dict(profile, context=32768, output_limit=8192))
    assert second['profile'] == first['profile']
    assert second['samples'] == first['samples']


def test_calibration_continues_after_http_error(runtime):
    import httpx

    def benchmark(profile, cancel):
        if profile['model'] == 'large':
            raise httpx.ReadTimeout('load failed')
        return {'profile': profile, 'generation_tps': 30}

    runtime.benchmark = benchmark
    assert select_profile(runtime, Profile().model_dump())['profile']['model'] == 'small'


def test_unsupported_flash_attention_uses_compatible_fallback(runtime):
    def benchmark(profile, cancel):
        if profile['flash_attention'] == 'on':
            raise RuntimeError('unsupported flash attention')
        return {'profile': profile, 'generation_tps': 30}

    runtime.benchmark = benchmark
    plan = select_profile(runtime, Profile(model='large').model_dump())
    assert plan['profile']['flash_attention'] == 'off'
    assert plan['profile']['cache_type_v'] == 'f16'


def test_explicit_cache_settings_are_verified_and_v_quantization_requires_flash():
    from agentvisor.load_config import sdk_config, verify_config
    profile = Profile(flash_attention='on', cache_type_k='q8_0', cache_type_v='q8_0').model_dump()
    config = sdk_config(profile)
    assert config['llamaKCacheQuantizationType'] == config['llamaVCacheQuantizationType'] == 'q8_0'
    verify_config(profile, config)
    with pytest.raises(RuntimeError, match='llamaVCacheQuantizationType'):
        verify_config(profile, dict(config, llamaVCacheQuantizationType='f16'))
    with pytest.raises(ValueError, match='Flash Attention'):
        Profile(flash_attention='off', cache_type_v='q4_0')
    other = dict(profile, cache_type_k='q4_0')
    assert ModelRuntime.identifier(profile) != ModelRuntime.identifier(other)


def test_calibration_rejects_fast_profile_without_memory_reserve(runtime, monkeypatch):
    from agentvisor.tuning import hardware
    initial = hardware()
    low = dict(initial, gpus=[dict(initial['gpus'][0], free=GIB // 2)])
    observations = iter([low, initial])
    monkeypatch.setattr('agentvisor.tuning.hardware', lambda force=False: next(observations) if force else initial)
    runtime.benchmark = lambda profile, cancel: {'profile': profile, 'generation_tps': 40}
    plan = select_profile(runtime, Profile(model='large').model_dump())
    assert plan['profile']['context'] == 32768
    assert plan['samples'][0]['memory_pressure'] is True
    assert len(plan['samples']) == 2


def test_requested_reasoning_filters_unpinned_model_candidates(runtime):
    models = runtime.inventory({})['models']
    models[0]['reasoning'] = {'allowed_options': ['on', 'off']}
    models[1]['reasoning'] = {'allowed_options': ['medium']}
    plan = select_profile(runtime, Profile(reasoning='medium').model_dump(), measure=False)
    assert plan['profile']['model'] == 'small'


def test_profile_api_distinguishes_preview_and_tune_and_translates_plan(tmp_path):
    from fastapi.testclient import TestClient
    from agentvisor.app import create_app
    app = create_app(tmp_path)
    with TestClient(app, client=('127.0.0.1', 12345)) as client:
        runtime = app.state.engine.runtime
        calls = []
        selected = Profile(model='chosen').model_dump()
        def resolve(values, measure):
            calls.append(('resolve', measure))
            return {'profile': selected, 'reason': 'Профиль выбран по памяти runtime и измеренной скорости.',
                    'warnings': ['Точная оценка памяти недоступна. Использован ограниченный стартовый контекст.']}
        runtime.resolve_profile = resolve
        runtime.ensure = lambda profile: (calls.append(('load', profile['model'])) or {'instance': 'test', 'context': profile['context']})
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        client.headers['Accept-Language'] = 'en'
        preview = client.post('/api/models/recommend', json=Profile().model_dump())
        assert preview.status_code == 200
        assert calls == [('resolve', False)]
        assert preview.json()['reason'].startswith('The profile was selected')
        assert preview.json()['warnings'][0].startswith('An exact memory')
        tuned = client.post('/api/models/tune', json=Profile().model_dump())
        assert tuned.status_code == 200 and tuned.json()['loaded']['instance'] == 'test'
        assert calls[-2:] == [('resolve', True), ('load', 'chosen')]
        app.state.store.save_setting('profile', {'runtime': 'lmstudio', 'model': 'old',
                                               'base_url': 'http://127.0.0.1:1234', 'context': 32768, 'output_limit': 4096})
        legacy = client.get('/api/profile').json()
        assert legacy['profile_mode'] == 'manual' and legacy['context'] == 32768


def test_task_pause_cancels_blocked_calibration_http_request(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    arrived, release, cancel = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            arrived.set()
            release.wait(8)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runtime = ModelRuntime(tmp_path)
    runtime.ensure = lambda profile, cancel: {'instance': 'test'}
    profile = Profile(model='test', base_url=f'http://127.0.0.1:{server.server_port}').model_dump()
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(runtime.benchmark, profile, cancel)
            assert arrived.wait(5)
            cancel.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=3)
    finally:
        release.set()
        cancel.set()
        server.shutdown()
        server.server_close()


def test_sdk_configuration_uses_requested_endpoint_without_cli_status(tmp_path):
    runtime = ModelRuntime(tmp_path)
    profile = Profile(model='test', base_url='http://127.0.0.1:5678',
                      flash_attention='on', cache_type_k='q8_0', cache_type_v='q8_0').model_dump()
    calls = []
    runtime.prepare = lambda *args: None
    runtime.service.matches = lambda *args: pytest.fail('SDK must address the requested endpoint directly')
    runtime.service.command = lambda *args, **kwargs: pytest.fail('Must not mutate another CLI server')
    def request(profile, path, body=None, **kwargs):
        calls.append((path, body))
        return {'models': [{'key': 'test', 'loaded_instances': [{'id': 'foreign'}, {'id': 'agentvisor-old'}]}]}
    runtime.request = request
    runtime.load_settings = lambda p, identifier, cancel, action: {'instance': identifier, 'context': p['context']}
    assert runtime.ensure(profile)['instance'] == runtime.identifier(profile)
    assert calls == [('/api/v1/models', None), ('/api/v1/models/unload', {'instance_id': 'agentvisor-old'})]


def test_sdk_read_retries_timeout_but_never_repeats_load(tmp_path, monkeypatch):
    runtime = ModelRuntime(tmp_path)
    profile = Profile(model='test').model_dump()
    calls = []
    def capture(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('timed out')
        return '{"context": 16384}'
    monkeypatch.setattr('agentvisor.models.capture', capture)
    monkeypatch.setattr('agentvisor.models.time.sleep', lambda value: None)
    assert runtime.load_settings(profile, 'test')['context'] == 16384
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(RuntimeError, match='timed out'):
        runtime.load_settings(profile, 'test', action='load')
    assert len(calls) == 1
