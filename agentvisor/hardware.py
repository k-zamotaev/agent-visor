import csv
import io
import os
import platform
import threading
import time
from pathlib import Path

import psutil

from .processes import capture, executable

_cached = None
_lock = threading.Lock()


def hardware(force=False):
    global _cached
    with _lock:
        if not force and _cached and time.time() - _cached['measured_at'] < 8:
            return _cached
        ram = psutil.virtual_memory()
        result = {'os': platform.system(), 'cpu': platform.processor() or platform.machine(),
                  'cpu_count': os.cpu_count(), 'cpu_percent': psutil.cpu_percent(),
                  'ram_total': ram.total, 'ram_available': ram.available, 'ram_used': ram.used,
                  'gpus': [], 'gpu_error': None, 'measured_at': time.time(),
                  'container': Path('/.dockerenv').exists(),
                  'opencode': executable('opencode'), 'lms': executable('lms'),
                  'ollama': executable('ollama')}
        cli = executable('nvidia-smi')
        if cli:
            try:
                out = capture([cli, '--query-gpu=name,memory.total,memory.free,utilization.gpu',
                               '--format=csv,noheader,nounits'], timeout=5)
                for row in csv.reader(io.StringIO(out)):
                    if len(row) == 4:
                        result['gpus'].append({'name': row[0].strip(), 'total': int(row[1]) * 1024**2,
                                               'free': int(row[2]) * 1024**2,
                                               'utilization': float(row[3])})
            except (ValueError, OSError, RuntimeError, TimeoutError) as error:
                result['gpu_error'] = str(error)
        _cached = result
        return result


def recommend(models, machine):
    """Conservative starting profile; never label memory estimates as benchmarks."""
    gpu_free = max((g['free'] for g in machine['gpus']), default=0)
    available = gpu_free or machine['ram_available'] * 0.6
    candidates = [m for m in models if m.get('tool_use') is not False]
    fits = [m for m in candidates if m.get('size', 0) and m['size'] * 1.2 < available]
    ranked = sorted(fits, key=lambda m: (m.get('tool_use') is True, m['size']), reverse=True)
    model = ranked[0] if ranked else (candidates[0] if candidates else None)
    if not model:
        return {'model': None, 'context': 8192, 'confidence': 'estimate',
                'reason': 'Нет локальных моделей для инструментов. Добавьте модель в выбранный runtime.'}
    room = available - model.get('size', 0) * 1.1
    context = 32768 if room > 4 * 1024**3 else 16384 if room > 2 * 1024**3 else 8192
    context = min(context, model.get('max_context') or context)
    return {'model': model['id'], 'context': context, 'output_limit': min(4096, context // 4),
            'gpu': 'auto', 'confidence': 'estimate', 'fits_estimate': bool(ranked),
            'reason': 'Стартовый профиль по доступной памяти и размеру весов. '
                      'Размещение выбирает runtime; скорость и запас KV-кэша требуют калибровки.'}
