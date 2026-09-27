import ctypes
import os
import re
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse

from .execution import agent_environment, execute
from .command_policy import resolve_command_policy
from .inference import InferenceGateway
from .processes import executable, recover_process
from .recovery import failure_layer, initialize_progress, observe_progress, record_recovery
from .store import ACTIVE
from .task_memory import initialize_memory, remember_iteration
from .tool_trace import fingerprint
from .step_acceptance import (accepted_steps, claimed_steps, mark_steps, observed_evidence,
                              pending_steps, restore_accepted_claims, validate_review)
from .review_finalization import budgets as review_budgets, finalize as finalize_review
from .session_roles import session_role
from .checkpoint_flow import checkpoint_after_acceptance, prepare_checkpoint
from .skill_library import record_skills, skill_prompt
from .adaptive_effort import session_effort
from .tasks import checklist, prepare_documents, read_document, state_dir, write_document
from .progress_plan import initialize as initialize_plan, sync_document as sync_progress
from .session_continuation import HANDOFFS, continue_session, note_completed_session


class VerificationFailure(RuntimeError):
    def __init__(self, result):
        detail = str(result.get('error_detail') or '')[:1500]
        super().__init__('Независимая проверка завершилась ошибкой; результат не принят' +
                         (': ' + detail if detail else ''))
        self.result = dict(result, kind='verify', reason=result.get('reason') or 'verification_failed')


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
            if task['elapsed'] >= task['max_hours'] * 3600:
                raise ValueError('Лимит времени исчерпан. Увеличьте общий предел часов в лимитах задачи.')
            if task['iteration'] >= task['max_iterations']:
                raise ValueError('Лимит итераций исчерпан. Увеличьте его в лимитах задачи.')
            if not Path(task['workspace']).is_dir():
                raise ValueError('Рабочий каталог недоступен')
            if task['mode'] == 'opencode' and not executable('opencode') and not self.command_builder:
                raise ValueError('OpenCode не найден. Установите opencode-ai и перезапустите приложение.')
            self.current = task_id
            self.cancel = threading.Event()
            self.requested = 'paused'
            if task['status'] == 'blocked' and (task.get('review_retry') or {}).get('protocol_failures', 0) >= 3:
                self.store.update(task_id, review_retry=dict(task['review_retry'], protocol_failures=0))
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
        if len(prompt.encode('utf-16-le')) > 12000:
            write_document(task, 'RUN_PROMPT.md', prompt)
            prompt = ('Read ' + str(state_dir(task) / 'RUN_PROMPT.md') +
                      ' first. It contains the supervisor instructions for this session; follow them.')
        return argv + [prompt]

    def run(self, task_id):
        started = time.monotonic()
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        base_elapsed = self.store.get(task_id)['elapsed']
        failures = 0
        try:
            while not self.cancel.is_set():
                task = self.store.get(task_id)
                if task['mode'] == 'opencode' and not self.command_builder:
                    try:
                        task = initialize_plan(self.store, task)
                    except (ValueError, OSError) as error:
                        self.transition(task_id, 'blocked', 'Не удалось восстановить план: ' + str(error), 'error')
                        break
                restored = restore_accepted_claims(task, self.store)
                if restored:
                    self.store.event(task_id, 'accepted_claims_restored', 'Восстановлены отметки принятых этапов',
                                     data={'steps': restored})
                task = initialize_progress(self.store, task)
                task = initialize_memory(self.store, task)
                # Reconcile observed state before the next prompt as well as
                # after execution, including a validated diagnostic lost by an
                # older build. This does not invent another attempt or verdict.
                task = remember_iteration(self.store, task)
                failures = task.get('failure_streak', 0)
                if task['iteration'] >= task['max_iterations']:
                    self.transition(task_id, 'blocked', 'Достигнут лимит итераций', 'warning')
                    break
                if base_elapsed + time.monotonic() - started >= task['max_hours'] * 3600:
                    self.transition(task_id, 'blocked', 'Достигнут лимит времени запуска', 'warning')
                    break
                self.transition(task_id, 'preparing', 'Подготовка модели и контекста')
                result = None
                preparing_runtime = task['mode'] != 'demo'
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
                    preparing_runtime = False
                    # Read goal again after loading: it may have changed in the UI.
                    task = initialize_memory(self.store, self.store.get(task_id))
                    retry = task.get('review_retry') or {}
                    if (retry.get('goal_version') == task['goal_version'] and
                            retry.get('step_id') in {s['id'] for s in pending_steps(task)}):
                        if retry.get('protocol_failures', 0) >= 3:
                            self.transition(task_id, 'blocked',
                                'Рецензент трижды не сдал корректный отчёт. Прогресс сохранён. ' + retry.get('error', ''), 'error')
                            break
                        task = self.store.update(task_id, iteration=task['iteration'] + 1,
                                                 elapsed=base_elapsed + time.monotonic() - started)
                        self.review_steps(task, ready, profile)
                        task = observe_progress(self.store, self.store.get(task_id))
                        self.store.update(task_id, failure_streak=0, review_retry=None)
                        if self.complete(task):
                            break
                        self.cancel.wait(task['backoff_seconds'])
                        continue
                    task = prepare_checkpoint(self.store, task)
                    task = dict(task, recipe_context=skill_prompt(self.store, task))
                    effort = session_effort(task, ready)
                    task = dict(task, active_effort=effort)
                    if task['mode'] == 'opencode' and not self.command_builder:
                        policy = resolve_command_policy(task, self.cancel)
                        task = dict(task, command_policy=policy)
                        if not policy['allowed']:
                            self.store.event(task_id, 'command_policy', policy['reason'], 'warning')
                    gateway = (InferenceGateway(self.store, task, dict(profile, context=ready['context']), self.cancel)
                               if task['mode'] == 'opencode' and not self.command_builder else nullcontext())
                    with gateway as inference:
                        if inference:
                            ready = dict(ready, api_base_url=inference.base_url)
                            ready['command_mcp_url'] = inference.mcp_url
                        role = session_role(task)
                        prompt = prepare_documents(task, ready)
                        task = self.store.update(task_id, iteration=task['iteration'] + 1,
                                                 active_role=role, last_session_role=role, active_effort=effort,
                                                 applied_goal_version=task['goal_version'],
                                                 elapsed=base_elapsed + time.monotonic() - started)
                        if self.cancel.is_set():
                            break
                        self.transition(task_id, 'running', f'Итерация {task["iteration"]}: следующий шаг')
                        self.store.event(task_id, 'session_role', 'Роль текущей сессии', data=role)
                        if effort:
                            self.store.event(task_id, 'effort_selected', 'Выбран режим работы текущей модели', data=effort)
                        health_check = (lambda: self.runtime.health(profile, ready['instance'])) if (
                            task['mode'] != 'demo' and profile.get('watchdog', True) and hasattr(self.runtime, 'health')) else None
                        prior_claims = {s['id'] for s in claimed_steps(task)}
                        claim_goal_version = task['goal_version']
                        result = execute(self.store, task, self.command(task, prompt, ready), self.cancel,
                                         agent_environment(task), health_check=health_check, inference=inference)
                    task = self.store.update(task_id, elapsed=base_elapsed + time.monotonic() - started,
                                             output_tokens=task['output_tokens'] + result['output_tokens'],
                                             agent_seconds=task.get('agent_seconds', 0) + result['duration'])
                    self.store.event(task_id, 'iteration_finished', f'Итерация {task["iteration"]} завершена',
                                     data=dict(result, iteration=task['iteration']))
                    task = remember_iteration(self.store, task, result)
                    if task.get('progress_plan') is not None:
                        task = sync_progress(self.store, task)
                    if self.cancel.is_set():
                        break
                    restore_accepted_claims(task, self.store)
                    task = note_completed_session(self.store, task, result)
                    continuation = task.get('session_continuation') or {}
                    missing_report = ('Две диагностические сессии не сохранили отчёт. '
                                      'Прогресс сохранён; подробности в журнале.')
                    if (result.get('reason') not in HANDOFFS and role['name'] == 'diagnostician'
                            and continuation.get('diagnostic_attempts', 0) >= 2
                            and not continuation.get('diagnosed')):
                        self.transition(task_id, 'blocked', missing_report, 'warning')
                        break
                    if result.get('reason') in HANDOFFS:
                        # A session boundary must not uncheck claims, reload the
                        # model, grow its context, or inflate failure_streak.
                        task, action = continue_session(self.store, task, result)
                        if action == 'blocked':
                            message = ('Начальный контекст не помещается в бюджет модели. Прогресс сохранён.'
                                       if result['reason'] == 'context_blocked' else
                                       missing_report if (task.get('recovery_context') or {}).get('diagnostic_report_missing') else
                                       'Повторные сессии и диагностика не дали нового результата. Прогресс сохранён; '
                                       'причина и последние проверки записаны в журнале.')
                            self.transition(task_id, 'blocked', message, 'warning')
                            break
                        if role['name'] == 'executor' and pending_steps(task) and task.get('step_acceptance', True):
                            self.review_steps(task, ready, profile)
                        if role['name'] == 'executor' and self.complete(self.store.get(task_id)):
                            break
                        self.store.event(task_id, 'next_iteration', 'Контекст сохранён; подготовка следующей сессии')
                        continue
                    if role['name'] == 'diagnostician' and task['goal_version'] == claim_goal_version:
                        mark_steps(task, [s for s in claimed_steps(task) if s['id'] not in prior_claims], False, self.store)
                    if result['failed']:
                        if (task.get('step_acceptance', True) and task['mode'] != 'demo' and
                                task['goal_version'] == claim_goal_version):
                            mark_steps(task, [s for s in claimed_steps(task) if s['id'] not in prior_claims], False, self.store)
                        task = observe_progress(self.store, task)
                        raise RuntimeError(f'Ошибка итерации: {result.get("error_detail") or result["reason"] or "exit " + str(result["exit_code"])}')
                    if role['name'] == 'executor' and task.get('step_acceptance', True) and task['mode'] != 'demo':
                        self.review_steps(task, ready, profile)
                        task = self.store.get(task_id)
                    task = observe_progress(self.store, task)
                    if role['name'] == 'executor' and self.complete(task):
                        break
                    failures = 0
                    task = self.store.update(task_id, failure_streak=0)
                    stalls = task['progress_watch']['stalls']
                    if stalls:
                        task = record_recovery(self.store, task, result, repair=stalls >= task['stall_limit'])
                        task = remember_iteration(self.store, task, result)
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
                    if isinstance(error, VerificationFailure):
                        result = error.result
                    message = str(error)[:2500]
                    layer = failure_layer(result, message, preparing=preparing_runtime)
                    stalled = task.get('progress_watch', {}).get('stalls', 0) >= task['stall_limit']
                    repair = stalled or failures >= task['max_failures']
                    task = record_recovery(self.store, task, result, message, repair=repair, layer=layer)
                    task = remember_iteration(self.store, task, result or {'failed': True, 'error_detail': message})
                    if not task.get('autonomous_recovery', True):
                        if stalled:
                            self.transition(task_id, 'blocked', 'Нет новых завершённых шагов. Проверьте план и журнал.', 'warning')
                            break
                        if failures >= task['max_failures']:
                            self.transition(task_id, 'blocked', 'Исчерпаны попытки: ' + message, 'error')
                            break
                    profile = (task.get('resolved_profile') or task['profile']).copy()
                    if layer == 'runtime' and (profile['runtime'] == 'ollama' or
                            urlparse(profile['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}):
                        profile['_reload'] = True
                        task = self.store.update(task_id, resolved_profile=profile)
                        self.store.event(task_id, 'model_reload_requested', 'Следующая попытка перезагрузит экземпляр модели AgentVisor', 'warning')
                    if task['auto_tune'] and layer == 'context':
                        profile = (task.get('resolved_profile') or task['profile']).copy()
                        request_size = re.search(r'request \((\d+) tokens\)', message, re.I)
                        needed = int(request_size[1]) + profile['output_limit'] + 4096 if request_size else profile['context'] * 2
                        maximum = task.get('profile_plan', {}).get('max_context', 262144)
                        context = min(maximum, max(profile['context'] * 2, ((needed + 8191) // 8192) * 8192))
                        if context > profile['context']:
                            profile['context'] = context
                            self.store.update(task_id, resolved_profile=profile, context_floor=context)
                            self.store.event(task_id, 'context_increased', f'Контекст увеличен до {context}: запрос OpenCode не помещался', 'warning')
                    if task['auto_tune'] and layer == 'runtime' and re.search(r'out of memory|insufficient memory|\boom\b', message, re.I):
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
                    delay = (min(task['backoff_seconds'] * (2 ** min(failures - 1, 12)), 300)
                             if layer == 'runtime' else task['backoff_seconds'])
                    self.cancel.wait(delay)
        except Exception as error:
            self.transition(task_id, 'failed', str(error)[:2500], 'error')
        finally:
            if self.cancel.is_set():
                self.transition(task_id, self.requested, 'По запросу пользователя или при остановке приложения')
            self.store.update(task_id, elapsed=base_elapsed + time.monotonic() - started, pid=None, pid_created=None)
            if os.name == 'nt':
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

    def complete(self, task):
        from .user_instructions import pending
        if pending(task):
            return False
        if task.get('step_acceptance', True) and task['mode'] != 'demo' and pending_steps(task):
            return False
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
                        pending(latest) or
                        latest.get('context_version', 0) != task.get('context_version', 0) or
                        (latest.get('progress_plan') or {}).get('revision') != (task.get('progress_plan') or {}).get('revision') or
                        latest.get('context_version', 0) > latest.get('applied_context_version', 0)):
                    return False
                message = ('Этапы прошли приёмку. Итоговая проверка всей задачи не настроена.'
                           if task.get('step_acceptance', True) and task['mode'] != 'demo' else
                           'Агент заявил о завершении. Независимая проверка не настроена.')
                self.transition(task['id'], 'completed_unverified', message)
                return True
        self.transition(task['id'], 'verifying', 'Запуск независимой проверки результата')
        result = execute(self.store, task, task['verification'], self.cancel, kind='verify')
        with self.lock:
            latest = self.store.get(task['id'])
            if (self.cancel.is_set() or latest['goal_version'] != task['goal_version'] or
                    pending(latest) or
                    latest.get('context_version', 0) != task.get('context_version', 0) or
                    (latest.get('progress_plan') or {}).get('revision') != (task.get('progress_plan') or {}).get('revision') or
                    latest.get('context_version', 0) > latest.get('applied_context_version', 0)):
                return False
            self.store.event(task['id'], 'verification_finished', 'Результат независимой проверки',
                             data=dict(result, input={'command': str(task['verification'])[:1500],
                                                     'fingerprint': fingerprint('verify', task['verification'])}))
            remember_iteration(self.store, task, result)
            if result['failed']:
                raise VerificationFailure(result)
            self.transition(task['id'], 'succeeded', 'Все шаги завершены; заданная проверка результата пройдена')
            return True

    def review_steps(self, task, ready, profile):
        from .user_instructions import pending
        if pending(task):
            return  # Let the next working session incorporate the new request first.
        # Review individually so one malformed report cannot invalidate other milestones.
        while pending_steps(task):
            self.review_step(task, ready, profile)
            latest = self.store.get(task['id'])
            if (latest['goal_version'] != task['goal_version'] or
                    latest.get('context_version', 0) != task.get('context_version', 0)):
                return
            task = latest

    def review_step(self, task, ready, profile):
        began = time.monotonic()
        base_elapsed = task['elapsed']
        steps = pending_steps(task)[:1]
        if not steps:
            return
        review = {'id': uuid.uuid4().hex, 'steps': steps}
        with self.lock:
            latest = self.store.get(task['id'])
            if self.cancel.is_set():
                raise InterruptedError()
            if latest['goal_version'] != task['goal_version']:
                raise VerificationFailure({'failed': True, 'error_detail': 'Goal changed before review'})
            if not mark_steps(task, steps, True, self.store):
                raise VerificationFailure({'failed': True, 'error_detail': 'Plan changed before milestone review'})
            write_document(task, 'STEP_REVIEW.json', '')
        self.transition(task['id'], 'verifying', 'Независимая приёмка этапа')
        role = session_role(task, review=True)
        effort = session_effort(task, ready, review=True)
        self.store.update(task['id'], active_role=role, active_effort=effort)
        self.store.event(task['id'], 'session_role', 'Роль текущей сессии', data=role)
        if effort:
            self.store.event(task['id'], 'effort_selected', 'Выбран режим работы текущей модели', data=effort)
        self.store.event(task['id'], 'step_review_started', 'Проверка отмеченных этапов',
                         data={'review_id': review['id'], 'steps': steps})
        with self.store.connect() as db:
            cursor = db.execute('SELECT MAX(id) FROM events WHERE task_id=?', (task['id'],)).fetchone()[0]
        inspection_budget, review_budget = review_budgets(task)
        task = dict(task, review_phase=True, review_request=review, review_cursor=cursor,
                    active_effort=effort, review_began=began,
                    review_inspection_seconds=inspection_budget, timeout_seconds=review_budget)
        if task['mode'] == 'opencode' and not self.command_builder:
            task['command_policy'] = resolve_command_policy(task, self.cancel)
        gateway = (InferenceGateway(self.store, task, dict(profile, context=ready['context']), self.cancel)
                   if task['mode'] == 'opencode' and not self.command_builder else nullcontext())
        ready = {key: value for key, value in ready.items() if key not in {'api_base_url', 'command_mcp_url'}}
        with gateway as inference:
            if inference:
                ready['api_base_url'] = inference.base_url
                ready['command_mcp_url'] = inference.mcp_url
            prompt = prepare_documents(task, ready, review=review)
            health_check = (lambda: self.runtime.health(profile, ready['instance'])) if (
                profile.get('watchdog', True) and hasattr(self.runtime, 'health')) else None
            task = dict(task, elapsed=base_elapsed + time.monotonic() - began)
            inspection = dict(task, timeout_seconds=max(0, inspection_budget - (time.monotonic() - began)),
                              review_handoff=bool(inference))
            result = execute(self.store, inspection, self.command(task, prompt, ready), self.cancel,
                             agent_environment(task), inference=inference, health_check=health_check)
            if inference:
                result = finalize_review(self.store, dict(task, elapsed=base_elapsed), inference,
                                         ready['instance'], self.cancel, result, began + review_budget)
        latest = self.store.get(task['id'])
        self.store.update(task['id'], output_tokens=latest['output_tokens'] + result['output_tokens'],
                          agent_seconds=latest.get('agent_seconds', 0) + result['duration'],
                          elapsed=base_elapsed + time.monotonic() - began)
        with self.lock:
            latest = self.store.get(task['id'])
            if self.cancel.is_set():
                raise InterruptedError()
            if (latest['goal_version'] != task['goal_version'] or
                    latest.get('context_version', 0) != task.get('context_version', 0)):
                raise VerificationFailure({'failed': True, 'error_detail': 'Goal or user context changed during review'})
            accepted, error = validate_review(task, review, observed_evidence(self.store, task, cursor))
            if result['failed']:
                error = result.get('error_detail') or result.get('reason') or 'Reviewer process failed'
            if not error and not mark_steps(task, steps, True, self.store):
                error = 'Plan changed during milestone review'
            if error:
                # A broken report/process is not evidence that implementation failed.
                if not result['failed'] and error.startswith('Milestone rejected:'):
                    mark_steps(task, steps, False, self.store)
                    self.store.update(task['id'], review_retry=None)
                else:
                    # A report transport/schema problem belongs to the reviewer, not product repair.
                    previous_retry = latest.get('review_retry') or {}
                    attempts = previous_retry.get('attempts', 0) if previous_retry.get('step_id') == steps[0]['id'] else 0
                    self.store.update(task['id'], review_retry={
                        'goal_version': task['goal_version'], 'step_id': steps[0]['id'],
                        'error': error[:1500], 'attempts': attempts + 1,
                        'protocol_failures': (previous_retry.get('protocol_failures', 0) + 1
                                              if result.get('reason') not in {
                                                  'runtime_unavailable', 'inference_error', 'inference_timeout',
                                                  'inference_failed', 'model_error', 'cancelled'} else 0)})
                self.store.event(task['id'], 'step_review_rejected', 'Этап не прошёл приёмку', 'warning',
                                 data={'review_id': review['id'], 'error': error})
                raise VerificationFailure(dict(result, failed=True, error_detail=error))
            records = dict(accepted_steps(latest))
            records.update(accepted)
            accepted_task = self.store.update(task['id'], review_retry=None, step_reviews={'goal_version': task['goal_version'],
                                             'review_revision': task.get('review_revision', 0),
                                             'context_version': task.get('context_version', 0), 'accepted': records})
            self.store.event(task['id'], 'step_review_accepted', 'Этап подтверждён проверкой',
                             data={'review_id': review['id'], 'accepted': accepted})
        # Optional disk work must not prevent control() from setting cancellation.
        # Retain the accepted version even if a user edits the goal after the lock.
        if not self.cancel.is_set():
            saved = record_skills(self.store, accepted_task, accepted)
            if saved:
                self.store.event(task['id'], 'skills_saved', 'Сохранены проверенные сценарии для этого проекта',
                                 data={'recipe_ids': saved})
            checkpoint_after_acceptance(self.store, accepted_task)
        remember_iteration(self.store, task, result)
