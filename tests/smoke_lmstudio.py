"""Opt-in real headless lifecycle test; run only in a disposable Docker container."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import psutil

from agentvisor.lmstudio import LMStudioService
from agentvisor.tasks import Profile


def main():
    if os.environ.get('AGENTVISOR_CONTAINER') != '1' or not Path('/.dockerenv').exists():
        raise RuntimeError('Run this test in a disposable AgentVisor Docker container')
    service = LMStudioService()
    initial = service.daemon()
    if initial['status'] == 'running':
        raise RuntimeError('This test requires a stopped LM Studio service')
    server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'agentvisor.app:app',
                               '--host', '127.0.0.1', '--port', '8420', '--no-access-log'])
    profile = Profile(context=4096, output_limit=512).model_dump()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8420', timeout=1000, trust_env=False) as client:
            deadline = time.monotonic() + 20
            while True:
                try:
                    session = client.get('/api/session', timeout=2)
                    session.raise_for_status()
                    break
                except httpx.HTTPError:
                    if time.monotonic() > deadline or server.poll() is not None:
                        raise
                    time.sleep(0.2)
            client.headers['x-agentvisor-token'] = session.json()['token']

            def post(path, body):
                response = client.post(path, json=body)
                assert response.is_success, (response.status_code, response.text)
                return response.json()

            print('Cold start: ' + initial['status'], flush=True)
            post('/api/models/start_service', profile)
            status = client.get('/api/runtime').json()
            assert status['kind'] == 'headless' and status['owned'], status
            print('Owned headless daemon and HTTP API ready', flush=True)
            result = post('/api/download', {
                'profile': profile,
                'model': 'https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF',
                'quantization': 'Q4_K_M',
            })
            print('Download job: ' + result['job_id'], flush=True)
            deadline = time.monotonic() + 600
            previous = None
            while True:
                response = client.get('/api/download')
                response.raise_for_status()
                job = response.json()
                sample = (job['status'], round(job.get('downloaded_bytes', 0) / 1024**2))
                if sample != previous:
                    print('Download: ' + str(sample), flush=True)
                    previous = sample
                if job['status'] in {'completed', 'failed'}:
                    break
                assert time.monotonic() < deadline, 'Download timed out'
                time.sleep(2)
            assert job['status'] == 'completed', job
            models = client.get('/api/models').json()['models']
            profile['model'] = next(model['id'] for model in models if 'smollm2' in model['id'].lower())
            ready = post('/api/models/load', profile)
            assert ready['context'] == 4096 and ready['instance'].startswith('agentvisor-'), ready
            generated = client.post('http://127.0.0.1:1234/v1/chat/completions', json={
                'model': ready['instance'], 'messages': [{'role': 'user', 'content': 'Say hello.'}],
                'max_tokens': 12, 'stream': False,
            }, timeout=120)
            generated.raise_for_status()
            assert generated.json()['usage']['completion_tokens'] > 0
            print('Model loaded; real inference returned tokens', flush=True)
            post('/api/models/unload', profile)
            assert not any(model['loaded'] for model in client.get('/api/models').json()['models'])
            daemon = service.daemon()
            assert service.owned(daemon)
            # Fault injection affects only the daemon created by this isolated test.
            process = psutil.Process(daemon['pid'])
            process.kill()
            psutil.wait_procs([process], timeout=10)
            post('/api/models/load', profile)
            assert service.owned(service.daemon())
            print('Recovered after daemon termination and reloaded the model', flush=True)
            post('/api/models/unload', profile)
            post('/api/models/stop_service', profile)
            assert service.daemon()['status'] != 'running'
            assert client.get('/api/download').json()['status'] == 'completed'
            print(json.dumps({'status': 'passed', 'initial': initial['status'],
                              'model': profile['model'], 'download_bytes': job.get('downloaded_bytes')}), flush=True)
    finally:
        server.terminate()
        server.wait(timeout=20)


if __name__ == '__main__':
    main()
