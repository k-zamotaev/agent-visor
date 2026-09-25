"""Bounded profile selection using runtime estimates and measured generation speed."""
import hashlib
import json
import os
import re
import time

import httpx

from .hardware import hardware
from .lmstudio import check_cancel
from .processes import executable

GIB = 1024**3


def parameter_count(model):
    match = re.search(r'([\d.]+)\s*([BM])', model.get('params') or '', re.I)
    return float(match[1]) * (1000 if match[2].upper() == 'B' else 1) if match else model.get('size', 0) / 1e6


def reasoning_setting(model, profile):
    capability = model.get('reasoning') or {}
    allowed = capability.get('allowed_options', [])
    requested = profile.get('reasoning', 'auto')
    if requested != 'auto':
        if requested not in allowed:
            raise RuntimeError('Модель не подтверждает выбранный режим рассуждения')
        return requested
    order = {'quality': ['xhigh', 'high', 'on', 'medium', 'low'],
             'balanced': ['medium', 'on', 'high', 'low', 'xhigh'],
             'speed': ['low', 'off', 'medium', 'on']}[profile.get('priority', 'quality')]
    return next((value for value in order if value in allowed), 'auto')


def select_profile(runtime, requested, cancel=None, report=None, measure=True):
    def notify(message):
        check_cancel(cancel)
        runtime.service.notify(message, report)

    runtime.prepare(requested, cancel, report)
    info = runtime.inventory(requested)
    if not info['online']:
        raise RuntimeError(info.get('error') or 'Сервер модели недоступен')
    pinned = requested.get('model')
    models = [model for model in info['models'] if (model['id'] == pinned if pinned else model.get('tool_use') is not False)]
    if not models:
        raise RuntimeError('Нет локальных моделей для инструментов. Добавьте модель в выбранный runtime.')
    if not pinned and requested.get('reasoning', 'auto') != 'auto':
        models = [model for model in models if requested['reasoning'] in (model.get('reasoning') or {}).get('allowed_options', [])]
        if not models:
            raise RuntimeError('Модель не подтверждает выбранный режим рассуждения')
    automatic = requested.get('profile_mode', 'manual') == 'auto'
    priority = requested.get('priority', 'quality')
    models.sort(key=lambda model: (model.get('tool_use') is not True,
                parameter_count(model) * (1 if priority == 'speed' else -1)))
    models = models[:3]
    machine = hardware() if runtime.local(requested) else None
    gpu_free = max((gpu['free'] for gpu in machine['gpus']), default=0) if machine else 0
    # Our resident instances will be reused or replaced, so their weights are
    # reclaimable. Foreign instances remain excluded from the available budget.
    owned_bytes = sum(model.get('size', 0) for model in info['models'] if any(
        runtime.owns_instance(requested, instance['id']) for instance in model.get('instances', [])))
    gpu_capacity = max((gpu['total'] for gpu in machine['gpus']), default=0) if machine else 0
    available_gpu = min(gpu_capacity, gpu_free + owned_bytes)
    gpu_budget = max(0, available_gpu - max(1.5 * GIB, available_gpu * 0.08))
    ram_budget = machine['ram_available'] * 0.75 if machine else None
    can_estimate = requested['runtime'] == 'lmstudio' and machine and executable('lms') and runtime.service.matches(requested)
    can_configure = requested['runtime'] == 'lmstudio' and machine and requested['base_url'].startswith('http:') and not os.environ.get('AGENTVISOR_MODEL_TOKEN')
    plans = []
    for model in models:
        maximum = model.get('max_context') or 32768
        if not automatic and requested['context'] > maximum:
            raise RuntimeError('Контекст превышает предел выбранной модели')
        target = 65536 if priority == 'quality' else 32768
        contexts = sorted({min(maximum, target), min(maximum, 32768)}, reverse=True) if automatic else [requested['context']]
        for context in contexts:
            check_cancel(cancel)
            profile = dict(requested, model=model['id'], context=context,
                           reasoning=reasoning_setting(model, requested))
            if automatic:
                profile['output_limit'] = min(8192 if priority == 'quality' else 4096 if priority == 'balanced' else 2048, context // 4)
            estimate = {}
            if can_estimate:
                notify('Оценка памяти для контекста ' + str(context))
                estimate = runtime.memory_estimate(profile, cancel)
            if automatic and can_configure and model.get('format', 'gguf') == 'gguf':
                if profile.get('flash_attention', 'auto') == 'auto':
                    profile['flash_attention'] = 'on'
                # Q8 is the automatic memory-saving alternative. Q4 is only
                # available by explicit user choice because accuracy may change.
                cache = 'q8_0' if gpu_capacity and estimate.get('gpu_bytes', model.get('size', 0) * 1.25) > gpu_budget else 'f16'
                for field in ('cache_type_k', 'cache_type_v'):
                    if profile.get(field, 'auto') == 'auto':
                        profile[field] = cache if profile.get('flash_attention') != 'off' or field == 'cache_type_k' else 'f16'
                if estimate:
                    estimate['defaults_only'] = True
            total = estimate.get('total_bytes')
            gpu = estimate.get('gpu_bytes')
            comfortable = gpu is not None and gpu <= gpu_budget if gpu_free and profile.get('gpu') != 'off' else total is not None and ram_budget and total <= ram_budget
            # Current free memory excludes resident models. Reusing the exact owned
            # instance does not allocate a second copy; do not penalize that case.
            reusable = any(instance['id'] == runtime.identifier(profile) and
                           instance.get('config', {}).get('context_length') == context
                           for instance in model.get('instances', []))
            possible = total is None or not machine or total <= ram_budget + gpu_budget or reusable
            if possible or not automatic:
                plans.append({'profile': profile, 'estimate': estimate, 'fits_gpu': bool(comfortable or reusable),
                              'model': model, 'gpu_budget': int(gpu_budget), 'ram_budget': int(ram_budget or 0)})
            if comfortable or reusable or not automatic:
                break
    if not plans:
        raise RuntimeError('По оценке runtime модель не помещается с запасом памяти. Выберите меньшую модель.')
    # Prefer full GPU residency, then model capacity, then context; RAM fallback
    # uses the smallest context that still leaves room for an agent prompt.
    plans.sort(key=lambda plan: (plan['fits_gpu'] or plan['profile'].get('cache_type_k') == 'q8_0',
               parameter_count(plan['model']) * (-1 if priority == 'speed' else 1),
               plan['profile']['context'] if plan['fits_gpu'] or plan['profile'].get('cache_type_k') == 'q8_0' else -plan['estimate'].get('total_bytes', plan['profile']['context'])), reverse=True)
    if automatic and plans[0]['profile'].get('flash_attention') == 'on' and all(requested.get(key, 'auto') == 'auto' for key in ('flash_attention', 'cache_type_k', 'cache_type_v')):
        fallback = dict(plans[0], profile=dict(plans[0]['profile'], context=min(plans[0]['profile']['context'], 32768),
                                              flash_attention='off', cache_type_k='f16', cache_type_v='f16'))
        fallback['estimate'] = {}
        # Leave a compatibility fallback in the bounded shortlist when the engine
        # rejects Flash Attention or quantized cache for this architecture.
        plans.insert(min(3, len(plans)), fallback)
    selected = plans[0]
    profile = selected['profile']
    warnings = []
    if not can_estimate:
        warnings.append('Точная оценка памяти недоступна. Использован ограниченный стартовый контекст.')
    samples = []
    fingerprint = {'cpu': machine.get('cpu'), 'ram': machine.get('ram_total'),
                   'gpus': [(gpu['name'], gpu['total']) for gpu in machine['gpus']]} if machine else {'remote': requested['base_url']}
    inventory_key = [(model['id'], model.get('size'), model.get('format'), model.get('max_context'), model.get('reasoning')) for model in models]
    cache_request = {key: value for key, value in requested.items()
                     if not (automatic and key in {'context', 'output_limit'})}
    key = hashlib.sha256(json.dumps([cache_request, fingerprint, inventory_key], sort_keys=True).encode()).hexdigest()
    cache_path = runtime.service.directory / 'calibration.json'
    try:
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        cached = {}
    if automatic and measure:
        cached_plan = next((plan for plan in plans if plan['profile'] == cached.get('profile')), None)
        if cached.get('key') == key and time.time() - cached.get('time', 0) < 86400 and cached_plan:
            selected, profile = cached_plan, cached_plan['profile']
            samples = cached['samples']
        else:
            # A calibration run loads at most four plans. It never unloads foreign
            # instances and can be cancelled before any following agent session.
            for candidate in plans[:4]:
                notify('Проверка скорости выбранного профиля...')
                try:
                    sample = runtime.benchmark(candidate['profile'], cancel)
                    check_cancel(cancel)
                except InterruptedError:
                    raise
                except (RuntimeError, TimeoutError, httpx.HTTPError) as error:
                    warnings.append(str(error))
                    continue
                samples.append(sample)
                if machine:
                    observed = hardware(force=True)
                    sample['memory_after'] = {'ram_available': observed['ram_available'],
                                              'gpu_free': max((gpu['free'] for gpu in observed['gpus']), default=0)}
                    if observed['ram_available'] < 2 * GIB or (gpu_free and candidate['profile'].get('gpu') != 'off' and sample['memory_after']['gpu_free'] < GIB):
                        sample['memory_pressure'] = True
                        warnings.append('После загрузки недостаточно запаса памяти; проверяется более лёгкий профиль.')
                        continue
                speed = sample.get('generation_tps') or sample.get('request_tps') or 0
                if speed >= requested.get('target_tps', 20):
                    selected, profile = candidate, candidate['profile']
                    break
            else:
                safe = [sample for sample in samples if not sample.get('memory_pressure')]
                if safe:
                    fastest = max(safe, key=lambda sample: sample.get('generation_tps') or sample.get('request_tps') or 0)
                    profile = fastest['profile']
                    selected = next(plan for plan in plans if plan['profile'] == profile)
                warnings.append('Целевая скорость не достигнута. Для ускорения выберите меньшую модель или снизьте рассуждение.')
            if not any(not sample.get('memory_pressure') for sample in samples):
                raise RuntimeError('Ни один профиль не прошёл проверку загрузки и генерации')
            runtime.service.directory.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'key': key, 'time': time.time(), 'profile': profile, 'samples': samples}), encoding='utf-8')
            temporary.replace(cache_path)
    if not selected['fits_gpu']:
        warnings.append('Полное размещение в быстрой памяти не подтверждено; возможна работа через RAM/CPU.')
    if selected['estimate'].get('defaults_only'):
        warnings.append('Оценка CLI использует настройки runtime по умолчанию; Flash Attention и KV-кэш проверяются фактической загрузкой.')
    if profile.get('cache_type_k', 'auto').startswith('q') or profile.get('cache_type_v', 'auto').startswith('q'):
        warnings.append('Квантование KV-кэша экономит память, но может влиять на точность. Автоматически используется максимум Q8; Q4 выбирается вручную.')
    if samples and max((sample.get('generation_tps') or sample.get('request_tps') or 0) for sample in samples) < requested.get('target_tps', 20):
        warnings.append('Целевая скорость не достигнута. Для ускорения выберите меньшую модель или снизьте рассуждение.')
    reason = 'Профиль выбран по памяти runtime и измеренной скорости.' if samples else 'Профиль выбран по доступной памяти и ограничениям модели.'
    return {'profile': profile, 'reason': reason, 'warnings': list(dict.fromkeys(warnings)),
            'estimate': selected['estimate'], 'samples': samples, 'hardware': fingerprint,
            'max_context': selected['model'].get('max_context') or 32768,
            'gpu_budget': selected['gpu_budget'], 'ram_budget': selected['ram_budget']}
