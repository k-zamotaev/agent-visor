import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import pytest
import httpx

import launch
from agentvisor.instance import InstanceInUseError, lock_instance


@pytest.fixture
def health_server():
    servers = []

    def start(payload):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        servers.append((server, worker))
        return server.server_port

    yield start
    for server, worker in servers:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def unexpected_bootstrap(*args):
    pytest.fail('An existing or locked instance must not launch another server')


def test_repeated_launch_reuses_outdated_build_on_custom_port(tmp_path, monkeypatch, health_server, capsys):
    port = health_server({'status': 'ok', 'build_id': 'old-build', 'data_directory': str(tmp_path)})
    (tmp_path / 'launcher.json').write_text(json.dumps({'port': port}), encoding='utf-8')
    monkeypatch.setenv('AGENTVISOR_DATA', str(tmp_path))
    monkeypatch.setattr(launch, 'DEFAULT_PORTS', [])
    monkeypatch.setattr(launch, 'bootstrap', unexpected_bootstrap)
    opened = []
    monkeypatch.setattr(launch.webbrowser, 'open', opened.append)
    assert launch.main(['--port', '8422', '--language', 'en']) == 0
    output = capsys.readouterr().out
    assert f'AgentVisor is already running: http://127.0.0.1:{port}' in output
    assert 'source files have changed' in output
    assert opened == [f'http://127.0.0.1:{port}']
    assert 'Traceback' not in output and '\x1b[' not in output


@pytest.mark.parametrize('payload', [
    {'status': 'ok', 'build_id': 'same', 'data_directory': '/another-project'},
    {'status': 'ok', 'version': '0.1.0'},
    ['not', 'a', 'health', 'response'],
])
def test_other_or_legacy_server_is_not_reused(tmp_path, health_server, payload):
    port = health_server(payload)
    assert launch.find_existing(tmp_path, [port]) is None


def test_locked_directory_is_reported_without_starting_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('AGENTVISOR_DATA', str(tmp_path))
    monkeypatch.setattr(launch, 'DEFAULT_PORTS', [])
    monkeypatch.setattr(launch, 'bootstrap', unexpected_bootstrap)
    with lock_instance(tmp_path):
        assert launch.main(['--no-browser']) == 1
    output = capsys.readouterr().out
    assert 'уже используется' in output
    assert 'Не удаляйте файл блокировки' in output
    assert 'PermissionError' not in output and 'AgentVisor: http' not in output


def test_lock_excludes_another_process_and_is_released(tmp_path):
    lock_path = tmp_path / 'instance.lock'
    lock_path.write_bytes(b'0')
    script = '''
import sys
from agentvisor.instance import InstanceInUseError, lock_instance
try:
    with lock_instance(sys.argv[1]):
        pass
except InstanceInUseError:
    raise SystemExit(43)
'''
    command = [sys.executable, '-c', script, str(tmp_path)]
    with lock_instance(tmp_path):
        for _ in range(2):
            assert subprocess.run(command, timeout=10).returncode == 43
    assert subprocess.run(command, timeout=10).returncode == 0
    assert lock_path.read_bytes() == b'0'


def test_filesystem_error_is_not_misreported_as_running_instance(tmp_path):
    directory = tmp_path / 'not-a-directory'
    directory.write_text('file')
    with pytest.raises(OSError):
        lock_instance(directory)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_real_startup_then_repeated_launch_uses_same_server(tmp_path, monkeypatch, capsys):
    port = free_port()
    monkeypatch.setenv('AGENTVISOR_DATA', str(tmp_path))
    monkeypatch.setattr(launch, 'DEFAULT_PORTS', [])
    monkeypatch.setattr(launch, 'bootstrap', lambda *args: Path(sys.executable))
    original_popen = subprocess.Popen
    children = []

    def start(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(launch.subprocess, 'Popen', start)
    results = []
    worker = threading.Thread(target=lambda: results.append(launch.main(['--port', str(port), '--no-browser'])))
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / 'launcher.json').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / 'launcher.json').is_file()
        health = launch.read_health(f'http://127.0.0.1:{port}')
        assert launch.owns_directory(health, tmp_path)
        assert launch.main(['--no-browser']) == 0
        assert len(children) == 1
        assert 'agentvisor.server' in children[0].args
        output = capsys.readouterr().out
        assert f'AgentVisor: http://127.0.0.1:{port}' in output
        assert f'AgentVisor уже работает: http://127.0.0.1:{port}' in output
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3) as client:
            headers = {'x-agentvisor-token': client.get('/api/session').json()['token']}
            payload = {'name': 'Preserved task', 'goal': 'Keep history on restart',
                       'workspace': str(tmp_path), 'mode': 'demo'}
            created = client.post('/api/tasks', json=payload, headers=headers)
            created.raise_for_status()
            task_id = created.json()['id']
            code = client.get('/api/network').json()['access_code']
            for host in ('0.0.0.0', '127.0.0.1'):
                headers = {'x-agentvisor-token': client.get('/api/session').json()['token']}
                client.put('/api/network', json={'host': host}, headers=headers).raise_for_status()
                client.post('/api/restart', headers=headers).raise_for_status()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    health = launch.read_health(f'http://127.0.0.1:{port}')
                    if health and health.get('bind_host') == host:
                        break
                    time.sleep(0.1)
                assert health and health.get('bind_host') == host
                assert client.get(f'/api/tasks/{task_id}').json()['status'] == 'draft'
                assert client.get('/api/network').json()['access_code'] == code
            assert len(children) == 3
    finally:
        for child in children:
            if child.poll() is None:
                root = psutil.Process(child.pid)
                owned = root.children(recursive=True)
                for process in reversed(owned):
                    if process.is_running():
                        process.terminate()
                if root.is_running():
                    root.terminate()
                psutil.wait_procs([*owned, root], timeout=10)
        worker.join(timeout=20)
    assert not worker.is_alive()
    assert len(results) == 1
    with lock_instance(tmp_path):
        pass


def test_failed_start_does_not_announce_a_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('AGENTVISOR_DATA', str(tmp_path))
    monkeypatch.setattr(launch, 'DEFAULT_PORTS', [])
    monkeypatch.setattr(launch, 'bootstrap', lambda *args: Path(sys.executable))
    original_popen = subprocess.Popen
    monkeypatch.setattr(launch.subprocess, 'Popen',
                        lambda *args, **kwargs: original_popen([sys.executable, '-c', 'raise SystemExit(3)']))
    assert launch.main(['--port', str(free_port()), '--no-browser']) == 1
    output = capsys.readouterr().out
    assert 'AgentVisor не запустился' in output
    assert 'AgentVisor: http' not in output
    assert not (tmp_path / 'launcher.json').exists()
