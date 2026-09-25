import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import threading

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.lmstudio import LMStudioService
from agentvisor.models import ModelRuntime
from agentvisor.processes import capture
from agentvisor.tasks import Profile


def offline(*args, **kwargs):
    raise httpx.ConnectError('offline')


def test_service_child_survives_successful_cli_exit():
    script = ('import subprocess, sys; p=subprocess.Popen([sys.executable, "-c", '
              '"import time; time.sleep(30)"], stdin=subprocess.DEVNULL, '
              'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)')
    pid = int(capture([sys.executable, '-c', script], service=True))
    child = psutil.Process(pid)
    try:
        assert child.is_running() and child.status() != psutil.STATUS_ZOMBIE
    finally:
        child.kill()
        psutil.wait_procs([child], timeout=5)


def test_cold_start_waits_for_api_and_records_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.lmstudio.executable', lambda name: '/fake/lms')
    service = LMStudioService(tmp_path)
    commands, messages, remembered = [], [], []
    service.daemon = lambda: {'status': 'not-running'}
    service.remember = remembered.append
    ready = iter([False, False, True])
    service.probe = lambda *args: next(ready)

    def command(*args, **kwargs):
        commands.append(args)
        if args[:2] == ('daemon', 'up'):
            return json.dumps({'status': 'running', 'pid': 42, 'isDaemon': True})
        return json.dumps({'running': False})

    service.command = command
    service.ensure(Profile().model_dump(), offline, report=messages.append)
    assert commands == [('daemon', 'up', '--json'), ('server', 'status', '--json'),
                        ('server', 'start', '--port', '1234', '--bind', '127.0.0.1')]
    assert remembered[0]['pid'] == 42
    assert messages[-1] == 'Сервер LM Studio готов'


def test_missing_cli_is_installed_only_when_start_is_needed(tmp_path, monkeypatch):
    service = LMStudioService(tmp_path)
    monkeypatch.setattr('agentvisor.lmstudio.executable', lambda name: None)
    installed = []
    service.install = lambda *args: installed.append(True)
    service.daemon = lambda: {'status': 'running', 'isDaemon': True}
    service.command = lambda *args, **kwargs: '{"running": false}'
    ready = iter([False, True])
    service.probe = lambda *args: next(ready)
    service.ensure(Profile().model_dump(), offline)
    assert installed == [True]
    service.probe = lambda *args: True
    service.ensure(Profile().model_dump(), offline)
    assert installed == [True]


def test_polling_stopped_service_does_not_install_or_start(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.lmstudio.executable', lambda name: None)
    service = LMStudioService(tmp_path)
    service.install = lambda *args: pytest.fail('Polling must not install software')
    assert service.status(Profile().model_dump())['kind'] == 'not-installed'


def test_installer_reports_insufficient_disk_before_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.lmstudio.shutil.disk_usage', lambda path: SimpleNamespace(free=0))
    with pytest.raises(RuntimeError, match='3 ГБ'):
        LMStudioService(tmp_path).install()


def test_installer_retries_transient_connection_before_running_script(tmp_path, monkeypatch):
    requests, scripts = [], []
    client_type = httpx.Client

    def handler(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            raise httpx.ConnectTimeout('temporary TLS timeout')
        return httpx.Response(200, text='installer fixture')

    monkeypatch.setattr('agentvisor.lmstudio.httpx.Client', lambda **kwargs:
                        client_type(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('agentvisor.lmstudio.time.sleep', lambda seconds: None)
    monkeypatch.setattr('agentvisor.lmstudio.executable', lambda name: 'fake-' + name)

    def capture(argv, **kwargs):
        path = next(Path(arg) for arg in argv if arg.endswith(('.sh', '.ps1')))
        scripts.append(path.read_text())
        assert path.is_relative_to(tmp_path)
        assert kwargs['env']['TMPDIR'] == str(path.parent)

    monkeypatch.setattr('agentvisor.lmstudio.capture', capture)
    LMStudioService(tmp_path).install()
    assert len(requests) == 2 and requests[0].startswith('https://lmstudio.ai/install.')
    assert scripts == ['installer fixture']


@pytest.mark.parametrize('profile', [Profile(base_url='http://remote:1234'), Profile(runtime='ollama')])
def test_external_service_never_starts_local_cli(tmp_path, profile):
    service = LMStudioService(tmp_path)
    service.probe = lambda *args: pytest.fail('External server must not use local startup')
    service.ensure(profile.model_dump(), offline)
    assert not service.status(profile.model_dump())['manageable']


def test_disabled_startup_and_cancel_do_not_start_cli(tmp_path):
    service = LMStudioService(tmp_path)
    service.command = lambda *args, **kwargs: pytest.fail('CLI should not start')
    with pytest.raises(RuntimeError, match='выключен'):
        service.ensure(Profile(manage_runtime=False).model_dump(), offline)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        service.ensure(Profile().model_dump(), offline, cancel)


def test_auth_failure_does_not_restart_existing_server(tmp_path):
    service = LMStudioService(tmp_path)
    service.command = lambda *args, **kwargs: pytest.fail('Authentication failure must not restart LM Studio')

    def denied(*args, **kwargs):
        response = httpx.Response(401, request=httpx.Request('GET', 'http://localhost:1234/api/v1/models'))
        response.raise_for_status()

    with pytest.raises(RuntimeError, match='AGENTVISOR_MODEL_TOKEN'):
        service.ensure(Profile().model_dump(), denied)


def test_desktop_service_is_reused_and_never_owned(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.lmstudio.executable', lambda name: '/fake/lms')
    service = LMStudioService(tmp_path)
    service.daemon = lambda: {'status': 'running', 'isDaemon': False, 'pid': os.getpid()}
    commands = []
    service.command = lambda *args, **kwargs: (commands.append(args) or '{"running": false}')
    ready = iter([False, True])
    service.probe = lambda *args: next(ready)
    service.ensure(Profile().model_dump(), offline)
    assert all(command[0] != 'daemon' for command in commands)
    assert not (tmp_path / 'lmstudio-owner.json').exists()
    with pytest.raises(RuntimeError, match='вне AgentVisor'):
        service.stop(Profile().model_dump(), offline)


def test_ownership_survives_panel_restart_but_checks_process_identity(tmp_path):
    daemon = {'status': 'running', 'isDaemon': True, 'pid': os.getpid()}
    LMStudioService(tmp_path).remember(daemon)
    service = LMStudioService(tmp_path)
    assert service.owned(daemon)
    owner_file = tmp_path / 'lmstudio-owner.json'
    owner = json.loads(owner_file.read_text())
    owner['created'] -= 10
    owner_file.write_text(json.dumps(owner))
    assert not service.owned(daemon)


def test_stop_refuses_foreign_models(tmp_path):
    service = LMStudioService(tmp_path)
    daemon = {'status': 'running', 'isDaemon': True, 'pid': os.getpid()}
    service.remember(daemon)
    service.daemon = lambda: daemon
    def command(*args, **kwargs):
        if args[0] == 'server':
            return '{"running": true, "port": 1234}'
        if args[0] == 'ps':
            return '[{"identifier":"user-model"}]'
        pytest.fail('Foreign model must be left running')
    service.command = command
    with pytest.raises(RuntimeError, match='других приложений'):
        service.stop(Profile().model_dump(), offline)


def test_loading_and_unloading_touch_only_agentvisor_instances(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.models.executable', lambda name: '/fake/lms')
    runtime = ModelRuntime(tmp_path)
    runtime.prepare = lambda *args: None
    runtime.service.matches = lambda *args: True
    rows = [{'key': 'coding', 'loaded_instances': [
        {'id': 'foreign', 'config': {'context_length': 16384}},
        {'id': 'agentvisor-old', 'config': {'context_length': 8192}}]}]
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        if args[0] == 'load':
            rows[0]['loaded_instances'].append({'id': args[3], 'config': {'context_length': 16384}})

    posts = []
    runtime.service.command = command
    runtime.request = lambda profile, path, body=None, **kwargs: (posts.append(body) or {}) if body else {'models': rows}
    profile = Profile(model='coding').model_dump()
    loaded = runtime.ensure(profile)
    assert loaded['instance'].startswith('agentvisor-')
    assert commands[0] == ('unload', 'agentvisor-old')
    assert not any('foreign' in command for command in commands)
    runtime.unload(profile)
    assert all(body['instance_id'].startswith('agentvisor-') for body in posts)


def test_download_and_service_controls_require_token_and_idle_task(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        runtime = app.state.engine.runtime
        calls = []
        runtime.prepare = lambda *args: None
        runtime.start_service = lambda profile: {'text': 'Сервер LM Studio готов'}
        runtime.download = lambda *args: (calls.append(args) or {'job_id': 'job_test'})
        runtime.request = lambda *args, **kwargs: {'job_id': 'job_test', 'status': 'completed', 'downloaded_bytes': 50}
        profile = Profile().model_dump()
        body = {'profile': profile, 'model': 'test/model'}
        assert client.post('/api/download', json=body).status_code == 403
        headers = {'x-agentvisor-token': client.get('/api/session').json()['token'], 'Accept-Language': 'en'}
        assert client.post('/api/models/start_service', json=profile, headers=headers).json()['text'] == 'LM Studio server is ready'
        assert client.post('/api/download', json=body, headers=headers).status_code == 200
        assert client.get('/api/download').json()['model'] == 'test/model'
        assert calls[0][1] == 'test/model'
        runtime.request = offline
        assert client.get('/api/download').json()['status'] == 'completed'
        assert client.post('/api/download', json={**body, 'model': '   '}, headers=headers).status_code == 422
        task = client.post('/api/tasks', json={'name': 'busy', 'goal': 'Demo', 'workspace': '.', 'mode': 'demo'}, headers=headers).json()
        client.post(f"/api/tasks/{task['id']}/start", headers=headers)
        assert client.post('/api/models/stop_service', json=profile, headers=headers).status_code == 409
        assert client.post('/api/download', json=body, headers=headers).status_code == 409


def test_rest_instance_can_be_unloaded_after_panel_restart(tmp_path):
    profile = Profile(model='coding', base_url='http://remote:1234').model_dump()
    runtime = ModelRuntime(tmp_path)
    runtime.request = lambda profile, path, body=None, **kwargs: (
        {'models': []} if body is None else
        {'instance_id': 'rest-instance', 'load_config': {'context_length': 16384}})
    assert runtime.ensure(profile)['instance'] == 'rest-instance'
    runtime = ModelRuntime(tmp_path)
    unloaded = []

    def request(profile, path, body=None, **kwargs):
        if body:
            unloaded.append(body['instance_id'])
            return {}
        return {'models': [{'key': 'coding', 'loaded_instances': [
            {'id': 'rest-instance'}, {'id': 'foreign-instance'}]}]}

    runtime.request = request
    runtime.unload(dict(profile, base_url='http://other:1234'))
    assert unloaded == []
    runtime.unload(profile)
    assert unloaded == ['rest-instance']


def test_local_rest_endpoint_does_not_load_on_other_cli_server(tmp_path, monkeypatch):
    monkeypatch.setattr('agentvisor.models.executable', lambda name: '/fake/lms')
    runtime = ModelRuntime(tmp_path)
    runtime.prepare = lambda *args: None
    runtime.service.daemon = lambda: {'status': 'running'}

    def command(*args, **kwargs):
        assert args == ('server', 'status', '--json')
        return '{"running": true, "port": 1234}'

    runtime.service.command = command
    runtime.request = lambda profile, path, body=None, **kwargs: (
        {'models': []} if body is None else
        {'instance_id': 'on-5678', 'load_config': {'context_length': 16384}})
    result = runtime.ensure(Profile(model='coding', base_url='http://127.0.0.1:5678').model_dump())
    assert result['instance'] == 'on-5678'
