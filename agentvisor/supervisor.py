import ctypes
import os
import re
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse

from .execution import agent_environment, execute
from .inference import InferenceGateway
from .processes import executable, recover_process
from .recovery import initialize_progress, observe_progress, record_recovery
from .store import ACTIVE
from .tasks import checklist, prepare_documents, read_document, state_dir


class Supervisor:
    def __init__(self, store, runtime, command_builder=None):
        self.store, self.runtime, self.command_builder = store, runtime, command_builder
        self.lock = threading.RLock()
        self.worker = None
        self.current = None
        self.cancel = threading.Event()
        self.requested = 'paused'
        resume = None
        for task in store.list():
            if task['status'] in ACTIVE:
                recover_process(task)
                interrupted = task['status'] not in {'pausing', 'stopping'}
                status = 'stopped' if task['status'] == 'stopping' else 'paused'
                task = store.update(task['id'], status=status, pid=None, pid_created=None,
                                    reason='Приложение перезапущено. Проверьте последний шаг перед продолжением.')
                if interrupted and task.get('autonomous_recovery', True) and resume is None:
                    task = initialize_progress(store, task)
                    record_recovery(store, task, {'reason': 'service_interrupted'},
                                    'Приложение прервано во время работы. Проверьте результат последней операции.', repair=True)
                    store.update(task['id'], recoveries=task['recoveries'] + 1)
                    resume = task['id']
                    store.event(task['id'], 'service_restarted',
                                'Приложение перезапущено. Прерванная задача продолжится автоматически.', 'warning')
                else:
                    store.event(task['id'], 'service_restarted', 'Прерванный запуск переведён в паузу', 'warning')
        if resume:
            try:
                self.start(resume)
            except ValueError as error:
                self.transition(resume, 'blocked', str(error), 'error')

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def start(self, task_id):
        with self.lock:
            if self.busy:
                raise ValueError('Уже выполняется задача. Сначала поставьте её на паузу.')
            task = self.store.get(task_id)
            if task['status'] in {'succeeded', 'completed_unverified'}:
                raise ValueError('Запуск завершён. Для новой цели создайте задачу.')
            if not Path(task['workspace']).is_dir():
                raise ValueError('Рабочий каталог недоступен')
            if task['mode'] == 'opencode' and not executable('opencode') and not self.command_builder:
                raise ValueError('OpenCode не найден. Установите opencode-ai и перезапустите приложение.')
            self.current = task_id
            self.cancel = threading.Event()
            self.requested = 'paused'
            self.store.update(task_id, status='preparing', reason='', started=task.get('started') or time.time())
            self.worker = threading.Thread(target=self.run, args=(task_id,), name='agentvisor-worker', daemon=True)
            self.worker.start()
            return self.store.get(task_id)

    def control(self, task_id, action):
        with self.lock:
            task = self.store.get(task_id)
            if action == 'start':
                return self.start(task_id)
            if action not in {'pause', 'stop'}:
                raise ValueError('Неизвестное действие')
            status = 'paused' if action == 'pause' else 'stopped'
            if self.busy and self.current == task_id:
                self.requested = status
                self.store.update(task_id, status='pausing' if action == 'pause' else 'stopping',
                                  reason='Завершается текущая операция')
                self.cancel.set()
            elif task['status'] not in {'succeeded', 'completed_unverified'}:
                self.store.update(task_id, status=status, reason='По запросу пользователя')
            return self.store.get(task_id)

    def shutdown(self):
        self.requested = 'paused'
        self.cancel.set()
        if self.worker:
            self.worker.join(timeout=10)

    def transition(self, task_id, status, message, level='info'):
        self.store.update(task_id, status=status, reason=message)
        self.store.event(task_id, status, message, level)

    def command(self, task, prompt, ready):
        if self.command_builder:
            return self.command_builder(task)
        if task['mode'] == 'demo':
            return [sys.executable, str(Path(__file__).with_name('demo_agent.py')), str(state_dir(task))]
        argv = [executable('opencode'), 'run', '--format', 'json', '--dir', task['workspace'],
                '--thinking',
                '--title', f'AgentVisor {task["id"]} / {task["iteration"]}',
                '--model', 'agentvisor/' + ready['instance']]
        if task['auto_permissions']:
            argv.append('--auto')
        return argv + [prompt]

    def run(self, task_id):
        started = time.monotonic()
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        base_elapsed = self.store.get(task_id)['elapsed']
        failures = 0
        try:
            while not self.cancel.is_set():
                task = initialize_progress(self.store, self.store.get(task_id))
                failures = task.get('failure_streak', 0)
                if task['iteration'] >= task['max_iterations']:
                    self.transition(task_id, 'blocked', 'Достигнут лимит итераций', 'warning')
                    break
                if base_elapsed + time.monotonic() - started >= task['max_hours'] * 3600:
                    self.transition(task_id, 'blocked', 'Достигнут лимит времени запуска', 'warning')
                    break
                self.transition(task_id, 'preparing', 'Подготовка модели и контекста')
                result = None
                try:
                    profile = task.get('resolved_profile') or task['profile'].copy()
                    if task['mode'] == 'demo':
                        ready = {'instance': 'demo', 'context': profile['context']}
                    else:
                        if hasattr(self.runtime, 'prepare'):
                            self.runtime.prepare(profile, self.cancel, lambda message:
                                self.store.event(task_id, 'runtime_preparation', message))
                        if not task.get('resolved_profile') and hasattr(self.runtime, 'resolve_profile'):
                            plan = self.runtime.resolve_profile(profile, self.cancel, lambda message:
                                self.store.event(task_id, 'profile_preparation', message))
                            profile = plan['profile']
                            self.store.update(task_id, profile_plan=plan)
                            self.store.event(task_id, 'profile_selected', plan['reason'], data=plan)
                        elif not profile['model']:
                            recommendation = self.runtime.inventory(profile)['recommendation']
                            if not recommendation['model']:
                                raise RuntimeError(recommendation['reason'])
                            profile.update({k: recommendation[k] for k in ('context', 'output_limit', 'model')})
                            self.store.event(task_id, 'profile_selected', recommendation['reason'], data=profile)
                        self.store.update(task_id, resolved_profile=profile)
                        ready = self.runtime.ensure(profile, self.cancel)
                        profile.pop('_reload', None)
                    self.store.update(task_id, resolved_profile=profile, runtime_instance=ready['instance'])
                    # Read goal again after loading: it may have changed in the UI.
                    task = self.store.get(task_id)
                    gateway = (InferenceGateway(self.store, task, profile, self.cancel)
                               if task['mode'] == 'opencode' and not self.command_builder else nullcontext())
                    with gateway as inference:
                        if inference:
                            ready = dict(ready, api_base_url=inference.base_url)
                        prompt = prepare_documents(task, ready)
                        task = self.store.update(task_id, iteration=task['iteration'] + 1,
                                                 applied_goal_version=task['goal_version'],
                                                 elapsed=base_elapsed + time.monotonic() - started)
                        if self.cancel.is_set():
                            break
                        self.transition(task_id, 'running', f'Итерация {task["iteration"]}: следующий шаг')
                        health_check = (lambda: self.runtime.health(profile, ready['instance'])) if (
                            task['mode'] != 'demo' and profile.get('watchdog', True) and hasattr(self.runtime, 'health')) else None
                        result = execute(self.store, task, self.command(task, prompt, ready), self.cancel,
                                         agent_environment(task), health_check=health_check, inference=inference)
                    task = self.store.update(task_id, elapsed=base_elapsed + time.monotonic() - started,
                                             output_tokens=task['output_tokens'] + result['output_tokens'],
                                             agent_seconds=task.get('agent_seconds', 0) + result['duration'])
                    self.store.event(task_id, 'iteration_finished', f'Итерация {task["iteration"]} завершена',
                                     data=dict(result, iteration=task['iteration']))
                    if self.cancel.is_set():
                        break
                    task = observe_progress(self.store, task)
                    if result['failed']:
                        raise RuntimeError(f'Ошибка итерации: {result.get("error_detail") or result["reason"] or "exit " + str(result["exit_code"])}')
                    if self.complete(task):
                        break
                    failures = 0
                    task = self.store.update(task_id, failure_streak=0)
                    stalls = task['progress_watch']['stalls']
                    if stalls:
                        task = record_recovery(self.store, task, result, repair=stalls >= task['stall_limit'])
                    if stalls >= task['stall_limit']:
                        if not task.get('autonomous_recovery', True):
                            self.transition(task_id, 'blocked', 'Нет новых завершённых шагов. Проверьте план и журнал.', 'warning')
                            break
                        self.store.update(task_id, recoveries=task['recoveries'] + 1)
                        self.transition(task_id, 'recovering',
                                        'Нет новых завершённых шагов. Следующая сессия устранит блокировщик.', 'warning')
                    self.store.event(task_id, 'next_iteration', 'Контекст сохранён; подготовка следующей сессии')
                    self.cancel.wait(task['backoff_seconds'])
                except InterruptedError:
                    break
                except Exception as error:
                    if self.cancel.is_set():
                        break
                    failures += 1
                    task = self.store.update(task_id, failure_streak=failures)
                    message = str(error)[:2500]
                    stalled = task.get('progress_watch', {}).get('stalls', 0) >= task['stall_limit']
                    repair = stalled or failures >= task['max_failures']
                    task = record_recovery(self.store, task, result, message, repair=repair)
                    if not task.get('autonomous_recovery', True):
                        if stalled:
                            self.transition(task_id, 'blocked', 'Нет новых завершённых шагов. Проверьте план и журнал.', 'warning')
                            break
                        if failures >= task['max_failures']:
                            self.transition(task_id, 'blocked', 'Исчерпаны попытки: ' + message, 'error')
                            break
                    profile = (task.get('resolved_profile') or task['profile']).copy()
                    if result and result['failed'] and (profile['runtime'] == 'ollama' or
                            urlparse(profile['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}):
                        profile['_reload'] = True
                        task = self.store.update(task_id, resolved_profile=profile)
                        self.store.event(task_id, 'model_reload_requested', 'Следующая попытка перезагрузит экземпляр модели AgentVisor', 'warning')
                    if task['auto_tune'] and re.search(r'exceeds.*context|exceed_context|context_length_exceeded', message, re.I):
                        profile = (task.get('resolved_profile') or task['profile']).copy()
                        request_size = re.search(r'request \((\d+) tokens\)', message, re.I)
                        needed = int(request_size[1]) + profile['output_limit'] + 4096 if request_size else profile['context'] * 2
                        maximum = task.get('profile_plan', {}).get('max_context', 262144)
                        context = min(maximum, max(profile['context'] * 2, ((needed + 8191) // 8192) * 8192))
                        if context > profile['context']:
                            profile['context'] = context
                            self.store.update(task_id, resolved_profile=profile, context_floor=context)
                            self.store.event(task_id, 'context_increased', f'Контекст увеличен до {context}: запрос OpenCode не помещался', 'warning')
                    if task['auto_tune'] and re.search(r'out of memory|insufficient memory|oom', message, re.I):
                        profile = (task.get('resolved_profile') or task['profile']).copy()
                        smaller = max(8192, profile['context'] // 2)
                        if smaller >= task.get('context_floor', 8192) and smaller < profile['context']:
                            profile['context'] = smaller
                            profile['output_limit'] = min(profile['output_limit'], profile['context'] // 4)
                            self.store.update(task_id, resolved_profile=profile)
                            self.store.event(task_id, 'context_reduced', f'Контекст снижен до {profile["context"]} после ошибки памяти', 'warning')
                    self.store.update(task_id, recoveries=task['recoveries'] + 1)
                    reason = (f'Автовосстановление: попытка {failures}. {message}' if repair else
                              f'Повтор {failures}/{task["max_failures"]}: {message}')
                    self.transition(task_id, 'recovering', reason, 'warning')
                    self.cancel.wait(min(task['backoff_seconds'] * (2 ** min(failures - 1, 12)), 300))
        except Exception as error:
            self.transition(task_id, 'failed', str(error)[:2500], 'error')
        finally:
            if self.cancel.is_set():
                self.transition(task_id, self.requested, 'По запросу пользователя или при остановке приложения')
            self.store.update(task_id, elapsed=base_elapsed + time.monotonic() - started, pid=None, pid_created=None)
            if os.name == 'nt':
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

    def complete(self, task):
        if task.get('context_version', 0) > task.get('applied_context_version', 0):
            return False
        done = read_document(task, 'DONE.md')
        declared = re.search(r'^goal_version:\s*(\d+)\s*$', done, re.M)
        if not declared or int(declared[1]) != task['goal_version'] or task['applied_goal_version'] != task['goal_version']:
            return False
        items = checklist(task)
        if not items or not all(item['done'] for item in items):
            self.store.event(task['id'], 'done_rejected', 'DONE.md найден, но в плане остались незавершённые шаги', 'warning')
            return False
        if not task['verification']:
            with self.lock:
                latest = self.store.get(task['id'])
                if (self.cancel.is_set() or latest['goal_version'] != task['goal_version'] or
                        latest.get('context_version', 0) > latest.get('applied_context_version', 0)):
                    return False
                self.transition(task['id'], 'completed_unverified', 'Агент заявил о завершении. Независимая проверка не настроена.')
                return True
        self.transition(task['id'], 'verifying', 'Запуск независимой проверки результата')
        result = execute(self.store, task, task['verification'], self.cancel, kind='verify')
        with self.lock:
            latest = self.store.get(task['id'])
            if (self.cancel.is_set() or latest['goal_version'] != task['goal_version'] or
                    latest.get('context_version', 0) > latest.get('applied_context_version', 0)):
                return False
            self.store.event(task['id'], 'verification_finished', 'Результат независимой проверки', data=result)
            if result['failed']:
                raise RuntimeError('Независимая проверка завершилась ошибкой; результат не принят')
            self.transition(task['id'], 'succeeded', 'Все шаги завершены; заданная проверка результата пройдена')
            return True
