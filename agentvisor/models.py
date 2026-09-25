import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx

from .hardware import hardware, recommend
from .processes import capture, executable
from .lmstudio import LMStudioService, local, parse_cli_json
from .load_config import explicit_config, sdk_config

DEFAULT_PROFILE = {'runtime': 'lmstudio', 'base_url': 'http://127.0.0.1:1234',
                   'model': '', 'context': 16384, 'output_limit': 4096, 'manage_runtime': True,
                   'profile_mode': 'auto', 'priority': 'quality', 'target_tps': 20,
                   'gpu': 'auto', 'reasoning': 'auto', 'watchdog': True}


class ModelRuntime:
    def __init__(self, directory=None):
        self.lock = threading.Lock()
        self.service = LMStudioService(directory)

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
                                   'params': row.get('params_string'), 'reasoning': row.get('capabilities', {}).get('reasoning'),
                                   'format': row.get('format'),
                                   'quantization': row.get('quantization', {}).get('name'),
                                   'tool_use': row.get('capabilities', {}).get('trained_for_tool_use'),
                                   'loaded': bool(row.get('loaded_instances')), 'instances': row.get('loaded_instances', [])})
            online = True
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            error = str(exc)
            if profile['runtime'] == 'lmstudio' and self.local(profile) and executable('lms'):
                try:
                    # Polling the page must not wake a stopped service.
                    rows = (parse_cli_json(self.service.command('ls', '--llm', '--json', timeout=25))
                            if self.service.daemon().get('status') == 'running' else [])
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
        return {'online': online, 'models': models, 'error': error, 'recommendation': recommendation,
                'service': self.service.status(profile, online)}

    @staticmethod
    def local(profile):
        return local(profile)

    def prepare(self, profile, cancel=None, report=None):
        self.service.ensure(profile, self.request, cancel, report)

    def resolve_profile(self, profile, cancel=None, report=None, measure=True):
        from .tuning import select_profile
        return select_profile(self, profile, cancel, report, measure)

    def health(self, profile, instance):
        try:
            if profile['runtime'] == 'ollama':
                rows = self.request(profile, '/api/ps', timeout=2)['models']
                if any(row.get('name') == instance or row.get('model') == instance for row in rows):
                    return None
            else:
                rows = self.request(profile, '/api/v1/models', timeout=2)['models']
                for row in rows:
                    for loaded in row.get('loaded_instances', []):
                        if loaded['id'] == instance:
                            if loaded.get('config', {}).get('context_length') != profile['context']:
                                return 'Контекст загруженной модели отличается от профиля'
                            return None
            return 'Рабочий экземпляр модели выгружен из памяти'
        except (httpx.HTTPError, ValueError, KeyError) as error:
            return 'Сервер модели недоступен: ' + str(error)

    def instances(self):
        path = self.service.directory / 'model-instances.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            return {}

    def remember_instance(self, profile, identifier):
        with self.lock:
            instances = self.instances()
            endpoint = profile['base_url'].rstrip('/')
            instances.setdefault(endpoint, [])
            if identifier not in instances[endpoint]:
                instances[endpoint].append(identifier)
            self.service.directory.mkdir(parents=True, exist_ok=True)
            path = self.service.directory / 'model-instances.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(instances), encoding='utf-8')
            temporary.replace(path)

    def owns_instance(self, profile, identifier):
        return (identifier.startswith('agentvisor-') or
                identifier in self.instances().get(profile['base_url'].rstrip('/'), []))

    @staticmethod
    def identifier(profile):
        identity = f"{profile['model']}:{profile['context']}"
        if profile.get('gpu', 'auto') != 'auto':
            identity += ':' + profile['gpu']
        if explicit_config(profile):
            identity += ':' + json.dumps(sdk_config(profile), sort_keys=True)
        return 'agentvisor-' + hashlib.sha256(identity.encode()).hexdigest()[:12]

    def load_settings(self, profile, identifier, cancel=None, action='inspect'):
        if not self.local(profile) or profile['base_url'].startswith('https:'):
            raise RuntimeError('Настройки Flash Attention и KV-кэша доступны через локальный LM Studio SDK')
        for attempt in range(2):
            try:
                output = capture([sys.executable, '-m', 'agentvisor.load_config', action,
                                  json.dumps(profile), identifier], timeout=310 if action == 'load' else 20,
                                 cancel=cancel, include_stderr=True, service=True, cwd=Path(__file__).parent.parent)
                return parse_cli_json(output)
            except (RuntimeError, TimeoutError) as error:
                # A read can be retried without allocating another model. Do not
                # repeat a load or hide rejected settings/authentication errors.
                if action != 'inspect' or attempt or not (isinstance(error, TimeoutError) or 'timed out' in str(error).lower()):
                    raise
                if cancel:
                    if cancel.wait(0.5):
                        raise InterruptedError('Операция отменена')
                else:
                    time.sleep(0.5)

    def start_service(self, profile):
        if profile['runtime'] != 'lmstudio' or not self.local(profile):
            raise RuntimeError('Запуск сервиса доступен только для локального LM Studio')
        self.service.ensure(profile, self.request, force=True)
        return {'text': 'Сервер LM Studio готов', 'service': self.service.status(profile, True)}

    def stop_service(self, profile):
        return self.service.stop(profile, self.request)

    def ensure(self, profile, cancel=None):
        if cancel and cancel.is_set():
            raise InterruptedError()
        if not profile.get('model'):
            raise RuntimeError('Выберите локальную модель в разделе «Модели»')
        self.prepare(profile, cancel)
        if profile['runtime'] == 'ollama':
            if explicit_config(profile):
                raise RuntimeError('Настройки Flash Attention и KV-кэша доступны через локальный LM Studio SDK')
            if profile.get('gpu', 'auto') != 'auto':
                raise RuntimeError('Ручное размещение GPU/CPU доступно только через локальный LM Studio CLI')
            if profile.get('_reload'):
                self.request(profile, '/api/generate', {'model': profile['model'], 'prompt': '',
                             'stream': False, 'keep_alive': 0}, timeout=30)
            result = self.request(profile, '/api/generate', {'model': profile['model'], 'prompt': '',
                                  'stream': False, 'keep_alive': '30m',
                                  'options': {'num_ctx': profile['context']}}, timeout=180)
            return {'instance': result.get('model', profile['model']), 'context': profile['context']}
        if explicit_config(profile):
            if not self.local(profile) or not profile['base_url'].startswith('http:'):
                raise RuntimeError('Настройки Flash Attention и KV-кэша доступны через локальный LM Studio SDK')
            identifier = self.identifier(profile)
            existing = self.request(profile, '/api/v1/models')['models']
            for row in existing:
                for instance in row.get('loaded_instances', []):
                    if instance['id'] == identifier and not profile.get('_reload'):
                        return self.load_settings(profile, identifier, cancel)
            for row in existing:
                for instance in row.get('loaded_instances', []):
                    if self.owns_instance(profile, instance['id']):
                        self.request(profile, '/api/v1/models/unload', {'instance_id': instance['id']}, timeout=60)
            self.service.notify('Загрузка модели в память...')
            result = self.load_settings(profile, identifier, cancel, action='load')
            self.service.notify('Модель готова к работе')
            return result
        if self.local(profile) and executable('lms') and self.service.matches(profile):
            identifier = self.identifier(profile)
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
                        self.service.command('unload', instance['id'], timeout=60, cancel=cancel)
            self.service.notify('Загрузка модели в память...')
            placement = ['--gpu', profile['gpu']] if profile.get('gpu', 'auto') != 'auto' else []
            self.service.command('load', profile['model'], '--identifier', identifier,
                                 '--context-length', str(profile['context']), '--parallel', '1', *placement, '-y',
                                 timeout=300, cancel=cancel)
            rows = self.request(profile, '/api/v1/models')['models']
            for row in rows:
                for instance in row.get('loaded_instances', []):
                    if instance['id'] == identifier and instance['config']['context_length'] == profile['context']:
                        self.service.notify('Модель готова к работе')
                        return {'instance': identifier, 'context': profile['context']}
            raise RuntimeError('Runtime не подтвердил загрузку с выбранным контекстом')
        existing = self.request(profile, '/api/v1/models')['models']
        if profile.get('gpu', 'auto') != 'auto':
            raise RuntimeError('Ручное размещение GPU/CPU доступно только через локальный LM Studio CLI')
        for row in existing:
            if row.get('key') == profile['model']:
                for instance in row.get('loaded_instances', []):
                    if instance.get('config', {}).get('context_length') == profile['context']:
                        return {'instance': instance['id'], 'context': profile['context']}
        loaded = self.request(profile, '/api/v1/models/load',
                              {'model': profile['model'], 'context_length': profile['context'],
                               'echo_load_config': True}, timeout=300)
        self.remember_instance(profile, loaded['instance_id'])
        context = loaded.get('load_config', {}).get('context_length')
        if context != profile['context']:
            raise RuntimeError('Сервер не подтвердил запрошенный размер контекста')
        return {'instance': loaded['instance_id'], 'context': context}

    def estimate(self, profile):
        self.prepare(profile)
        if profile['runtime'] == 'lmstudio' and self.local(profile) and executable('lms') and self.service.matches(profile):
            return self.memory_estimate(profile)
        return {'kind': 'unavailable', 'text': 'Оценка без загрузки доступна для локального LM Studio CLI.'}

    def memory_estimate(self, profile, cancel=None):
        placement = ['--gpu', profile['gpu']] if profile.get('gpu', 'auto') != 'auto' else []
        output = capture([executable('lms'), 'load', profile['model'], '--estimate-only',
                          '--context-length', str(profile['context']), '--parallel', '1', *placement],
                         timeout=60, cancel=cancel, include_stderr=True, service=True)
        values = {}
        for name, field in [('GPU', 'gpu_bytes'), ('Total', 'total_bytes')]:
            match = re.search(r'Estimated ' + name + r' Memory:\s*([\d.]+)\s*(GiB|MiB|GB|MB)', output)
            if match:
                unit = {'GiB': 1024**3, 'MiB': 1024**2, 'GB': 10**9, 'MB': 10**6}[match[2]]
                values[field] = int(float(match[1]) * unit)
        return {'kind': 'runtime_estimate', 'text': output[-8000:],
                'defaults_only': explicit_config(profile), **values}

    def unload(self, profile):
        if profile['runtime'] == 'ollama':
            self.request(profile, '/api/generate', {'model': profile['model'], 'prompt': '',
                         'stream': False, 'keep_alive': 0}, timeout=60)
        else:
            rows = self.request(profile, '/api/v1/models')['models']
            for row in rows:
                if row.get('key') != profile['model']:
                    continue
                for instance in row.get('loaded_instances', []):
                    if self.owns_instance(profile, instance['id']):
                        self.request(profile, '/api/v1/models/unload', {'instance_id': instance['id']}, timeout=60)
        return {'text': 'Экземпляры модели AgentVisor выгружены из памяти'}

    def download(self, profile, model, quantization=''):
        if profile['runtime'] != 'lmstudio':
            raise RuntimeError('Скачивание из панели доступно для LM Studio')
        self.prepare(profile)
        body = {'model': model}
        if quantization:
            body['quantization'] = quantization
        return self.request(profile, '/api/v1/models/download', body, timeout=60)

    def benchmark(self, profile, cancel=None):
        ready = self.ensure(profile, cancel)
        started = time.perf_counter()
        def predict(path, body):
            if cancel is None:
                return self.request(profile, path, body, timeout=120)
            output = capture([sys.executable, '-m', 'agentvisor.prediction_probe',
                              profile['base_url'].rstrip('/') + path, json.dumps(body)],
                             timeout=125, cancel=cancel, cwd=Path(__file__).parent.parent)
            return json.loads(output)
        if profile['runtime'] == 'ollama':
            options = {'num_ctx': profile['context'], 'num_predict': 180}
            options.update({key: profile[key] for key in ('temperature', 'top_p', 'top_k') if profile.get(key) is not None})
            result = predict('/api/generate', {'model': ready['instance'],
                                  'prompt': 'List integers from 1 to 60 separated by spaces.',
                                  'stream': False, 'keep_alive': '30m',
                                  'options': options})
            tokens = result.get('eval_count', 0)
            duration = result.get('eval_duration', 0) / 1e9
        else:
            body = {'model': ready['instance'], 'input': 'List integers from 1 to 100 separated by spaces.',
                    'max_output_tokens': 180, 'stream': False, 'store': False}
            for field in ('temperature', 'top_p', 'top_k'):
                if profile.get(field) is not None:
                    body[field] = profile[field]
            if profile.get('reasoning', 'auto') != 'auto':
                body['reasoning'] = profile['reasoning']
            result = predict('/api/v1/chat', body)
            stats = result.get('stats', {})
            tokens = stats.get('total_output_tokens', 0)
            speed = stats.get('tokens_per_second', 0)
            duration = tokens / speed if speed and tokens else None
        wall = time.perf_counter() - started
        return {'time': time.time(), 'profile': profile, 'output_tokens': tokens,
                'load_config': ready.get('load_config'),
                'request_seconds': round(wall, 2),
                'generation_tps': round(tokens / duration, 2) if duration else None,
                'request_tps': round(tokens / wall, 2) if wall > 0 else None,
                'note': 'Один короткий замер. Скорость запроса включает обработку входа; '
                        'качество работы агента и устойчивость за ночь этим не проверяются.'}
