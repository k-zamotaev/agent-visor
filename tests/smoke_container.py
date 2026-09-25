"""Opt-in HTTP smoke test inside the built image, without downloading a model."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def main():
    version = subprocess.check_output(['opencode', '--version'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='agentvisor-smoke-') as directory:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, AGENTVISOR_DATA=directory)
        log = (Path(directory) / 'server.log').open('w+')
        server = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'agentvisor.app:app',
             '--host', '127.0.0.1', '--port', str(port)],
            env=env, stdout=log, stderr=log,
        )
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        health = client.get('/api/health')
                        health.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if server.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise RuntimeError(log.read())
                        time.sleep(0.1)
                assert client.get('/').status_code == 200
                assert client.get('/static/app.js').status_code == 200
                body = dict(name='Container HTTP smoke', workspace=directory,
                            goal='Complete the four demonstration steps', mode='demo')
                assert client.post('/api/tasks', json=body).status_code == 403
                client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
                created = client.post('/api/tasks', json=body)
                created.raise_for_status()
                task_id = created.json()['id']
                started = client.post(f'/api/tasks/{task_id}/start')
                started.raise_for_status()
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    task = client.get(f'/api/tasks/{task_id}').json()
                    if task['status'] in {'completed_unverified', 'failed', 'blocked'}:
                        break
                    time.sleep(0.2)
                assert task['status'] == 'completed_unverified', task
                assert len(task['checklist']) == 4
                assert all(step['done'] for step in task['checklist'])
                events = client.get(f'/api/tasks/{task_id}/events').json()
                assert any(event['kind'] == 'completed_unverified' for event in events)
                print(json.dumps(dict(status='passed', opencode=version,
                                      build_id=health.json()['build_id'],
                                      task_status=task['status'], iterations=task['iteration'],
                                      checklist_items=len(task['checklist']), events=len(events))))
        finally:
            server.send_signal(signal.SIGINT)
            server.wait(timeout=20)
            log.close()


if __name__ == '__main__':
    main()
