import hashlib
import json
import os
import threading
import time
from urllib.parse import urlparse

import httpx

from .hardware import hardware, recommend
from .processes import capture, executable

DEFAULT_PROFILE = {'runtime': 'lmstudio', 'base_url': 'http://127.0.0.1:1234',
                   'model': '', 'context': 16384, 'output_limit': 4096}


def parse_cli_json(text):
    for index, char in enumerate(text):
        if char in '[{':
            try:
                return json.loads(text[index:])
            except json.JSONDecodeError:
                continue
    raise ValueError('CLI не вернул JSON')


class ModelRuntime:
    def __init__(self):
        self.lock = threading.Lock()

    def request(self, profile, path, body=None, timeout=8):
        token = os.environ.get('AGENTVISOR_MODEL_TOKEN', '')
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        with httpx.Client(timeout=timeout, trust_env=False, headers=headers) as client:
            url = profile['base_url'].rstrip('/') + path
            response = client.get(url) if body is None else client.post(url, json=body)
            response.raise_for_status()
            return response.json()

    def inventory(self, profile):
        models, online, error = [], False, None
        try:
            if profile['runtime'] == 'ollama':
                rows = self.request(profile, '/api/tags')['models']
                loaded = {r['name']: r for r in self.request(profile, '/api/ps')['models']}
                for row in rows:
                    models.append({'id': row['name'], 'name': row['name'], 'size': row.get('size', 0),
                                   'max_context': None, 'tool_use': None,
                                   'loaded': row['name'] in loaded, 'instances': []})
            else:
                rows = self.request(profile, '/api/v1/models')['models']
                for row in rows:
                    if row.get('type') != 'llm':
                        continue
                    models.append({'id': row['key'], 'name': row.get('display_name', row['key']),
                                   'size': row.get('size_bytes', 0), 'max_context': row.get('max_context_length'),
                                   'tool_use': row.get('capabilities', {}).get('trained_for_tool_use'),
                                   'loaded': bool(row.get('loaded_instances')), 'instances': row.get('loaded_instances', [])})
            online = True
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            error = str(exc)
            if profile['runtime'] == 'lmstudio' and self.local(profile) and executable('lms'):
                try:
                    rows = parse_cli_json(capture([executable('lms'), 'ls', '--llm', '--json'], timeout=25))
                    for row in rows:
                        models.append({'id': row['modelKey'], 'name': row.get('displayName', row['modelKey']),
                                       'size': row.get('sizeBytes', 0), 'max_context': row.get('maxContextLength'),
                                       'tool_use': row.get('trainedForToolUse'), 'loaded': False, 'instances': []})
                except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
                    error = str(exc)
        recommendation = recommend(models, hardware())
        if not self.local(profile):
            candidates = sorted((m for m in models if m.get('tool_use') is not False),
                                key=lambda m: (m.get('tool_use') is not True, m.get('size') or float('inf')))
            recommendation = {'model': candidates[0]['id'] if candidates else None,
                              'context': 8192, 'output_limit': 2048, 'confidence': 'unknown',
                              'reason': 'Оборудование удалённого сервера модели недоступно. '
                                        'Предложен небольшой стартовый контекст; проверьте память на сервере модели.'}
        return {'online': online, 'models': models, 'error': error, 'recommendation': recommendation}

    @staticmethod
    def local(profile):
        return urlparse(profile['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}

    def ensure(self, profile, cancel=None):
        if cancel and cancel.is_set():
            raise InterruptedError()
        if not profile.get('model'):
            raise RuntimeError('Выберите локальную модель в разделе «Модели»')
        if profile['runtime'] == 'ollama':
            if profile.get('_reload'):
                self.request(profile, '/api/generate', {'model': profile['model'], 'prompt': '',
                             'stream': False, 'keep_alive': 0}, timeout=30)
            result = self.request(profile, '/api/generate', {'model': profile['model'], 'prompt': '',
                                  'stream': False, 'keep_alive': '30m',
                                  'options': {'num_ctx': profile['context']}}, timeout=180)
            return {'instance': result.get('model', profile['model']), 'context': profile['context']}
        if self.local(profile) and executable('lms'):
            try:
                self.request(profile, '/api/v1/models')
            except httpx.HTTPError:
                port = urlparse(profile['base_url']).port or 1234
                capture([executable('lms'), 'server', 'start', '--port', str(port)], timeout=60, cancel=cancel)
            identifier = 'agentvisor-' + hashlib.sha256(
                f"{profile['model']}:{profile['context']}".encode()).hexdigest()[:12]
            existing = self.request(profile, '/api/v1/models')['models']
            for row in existing:
                for instance in row.get('loaded_instances', []):
                    if instance['id'] == identifier and not profile.get('_reload'):
                        if instance['config']['context_length'] != profile['context']:
                            raise RuntimeError('Контекст загруженной модели отличается от профиля')
                        return {'instance': identifier, 'context': profile['context']}
            # Only release instances previously created by this application.
            # LM Studio otherwise keeps the old context resident while loading another.
            for row in existing:
                for instance in row.get('loaded_instances', []):
                    if instance['id'].startswith('agentvisor-'):
                        capture([executable('lms'), 'unload', instance['id']], timeout=60, cancel=cancel)
            capture([executable('lms'), 'load', profile['model'], '--identifier', identifier,
                     '--context-length', str(profile['context']), '--parallel', '1', '-y'],
                    timeout=300, cancel=cancel)
            rows = self.request(profile, '/api/v1/models')['models']
            for row in rows:
                for instance in row.get('loaded_instances', []):
                    if instance['id'] == identifier and instance['config']['context_length'] == profile['context']:
                        return {'instance': identifier, 'context': profile['context']}
            raise RuntimeError('Runtime не подтвердил загрузку с выбранным контекстом')
        existing = self.request(profile, '/api/v1/models')['models']
        for row in existing:
            if row.get('key') == profile['model']:
                for instance in row.get('loaded_instances', []):
                    if instance.get('config', {}).get('context_length') == profile['context']:
                        return {'instance': instance['id'], 'context': profile['context']}
        loaded = self.request(profile, '/api/v1/models/load',
                              {'model': profile['model'], 'context_length': profile['context'],
                               'echo_load_config': True}, timeout=300)
        context = loaded.get('load_config', {}).get('context_length')
        if context != profile['context']:
            raise RuntimeError('Сервер не подтвердил запрошенный размер контекста')
        return {'instance': loaded['instance_id'], 'context': context}

    def estimate(self, profile):
        if profile['runtime'] == 'lmstudio' and self.local(profile) and executable('lms'):
            output = capture([executable('lms'), 'load', profile['model'], '--estimate-only',
                              '--context-length', str(profile['context'])], timeout=60, include_stderr=True)
            return {'kind': 'runtime_estimate', 'text': output[-8000:]}
        return {'kind': 'unavailable', 'text': 'Оценка без загрузки доступна для локального LM Studio CLI.'}

    def benchmark(self, profile):
        ready = self.ensure(profile)
        started = time.perf_counter()
        if profile['runtime'] == 'ollama':
            result = self.request(profile, '/api/generate', {'model': ready['instance'],
                                  'prompt': 'List integers from 1 to 60 separated by spaces.',
                                  'stream': False, 'keep_alive': '30m',
                                  'options': {'num_ctx': profile['context'], 'num_predict': 180}}, timeout=120)
            tokens = result.get('eval_count', 0)
            duration = result.get('eval_duration', 0) / 1e9
        else:
            result = self.request(profile, '/v1/chat/completions', {'model': ready['instance'],
                                  'messages': [{'role': 'user', 'content': 'List integers from 1 to 60 separated by spaces.'}],
                                  'max_tokens': 180, 'stream': False}, timeout=120)
            tokens = result.get('usage', {}).get('completion_tokens', 0)
            duration = None
        wall = time.perf_counter() - started
        return {'time': time.time(), 'profile': profile, 'output_tokens': tokens,
                'request_seconds': round(wall, 2),
                'generation_tps': round(tokens / duration, 2) if duration else None,
                'request_tps': round(tokens / wall, 2) if wall > 0 else None,
                'note': 'Один короткий замер. Скорость запроса включает обработку входа; '
                        'качество работы агента и устойчивость за ночь этим не проверяются.'}
