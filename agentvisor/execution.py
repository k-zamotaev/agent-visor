"""Bounded process execution and OpenCode JSON event decoding."""
import json
import os
import queue
import threading
import time

import psutil

from .processes import spawn, stop_tree
from .i18n import translate


def execute(store, task, argv, cancel, env=None, kind='agent', health_check=None, health_interval=5,
            inference=None):
    started = time.monotonic()
    process = spawn(argv, cwd=task['workspace'], env=env)
    messages = queue.Queue(maxsize=1024)
    store.update(task['id'], pid=process.pid, pid_created=psutil.Process(process.pid).create_time())
    streams_done = 0
    output_tokens, failed, reason = 0, False, None
    error_detail = ''
    heartbeat = started
    last_activity = started
    last_event, last_tool = '', ''
    # OpenCode JSON is not token streaming: a stalled tool can leave the last
    # visible event at step_start while /models remains perfectly healthy.
    idle_budget = task.get('idle_timeout_seconds', 300) if kind == 'agent' else None
    next_health_check, unhealthy = started, 0
    budget = min(task['timeout_seconds'], task['max_hours'] * 3600 - task.get('elapsed', 0))

    def idle_problem():
        activity = max(last_activity, inference.activity()) if inference else last_activity
        silent = time.monotonic() - activity
        if idle_budget is None or silent < idle_budget:
            return None
        message = f'Нет событий агента {int(silent)} с. Сессия будет перезапущена с диагностикой.'
        store.event(task['id'], 'agent_idle', message, 'warning', data={
            'idle_seconds': round(silent, 1), 'idle_timeout_seconds': idle_budget,
            'last_event': last_event, 'last_tool': last_tool})
        return message

    def check_runtime():
        nonlocal next_health_check, unhealthy
        if not health_check or time.monotonic() < next_health_check or process.poll() is not None:
            return None
        try:
            problem = health_check()
        except Exception as error:
            problem = str(error)
        next_health_check = time.monotonic() + health_interval
        unhealthy = unhealthy + 1 if problem else 0
        store.update(task['id'], runtime_health={'checked_at': time.time(),
                     'ok': not bool(problem), 'message': problem or '', 'failures': unhealthy})
        if unhealthy == 1:
            store.event(task['id'], 'runtime_health_warning',
                        'Проверка модели не пройдена: ' + problem, 'warning')
        if unhealthy >= 2:
            store.event(task['id'], 'runtime_lost',
                        'Модель недоступна. Текущая сессия будет завершена для восстановления.', 'warning')
            return problem
        return None

    def read(stream, channel):
        try:
            while True:
                line = stream.readline(65536)
                if not line:
                    break
                while not cancel.is_set() and not readers_stop.is_set():
                    try:
                        messages.put((channel, line), timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            stream.close()
            while True:
                try:
                    messages.put((channel, None), timeout=0.2)
                    break
                except queue.Full:
                    if cancel.is_set() or readers_stop.is_set():
                        break

    readers_stop = threading.Event()
    readers = [threading.Thread(target=read, args=(process.stdout, 'stdout'), daemon=True),
               threading.Thread(target=read, args=(process.stderr, 'stderr'), daemon=True)]
    for thread in readers:
        thread.start()
    try:
        while streams_done < 2:
            duration = time.monotonic() - started
            if cancel.is_set():
                reason = 'cancelled'
                break
            if time.monotonic() - heartbeat >= 1:
                store.update(task['id'], elapsed=task.get('elapsed', 0) + duration)
                heartbeat = time.monotonic()
            if duration >= budget:
                reason, failed = 'timeout', True
                store.event(task['id'], 'timeout', 'Превышено время итерации или запуска', 'warning')
                break
            problem = idle_problem()
            if problem:
                reason, failed, error_detail = 'idle_timeout', True, problem
                break
            problem = check_runtime()
            if problem:
                reason, failed, error_detail = 'runtime_unavailable', True, problem
                break
            try:
                channel, line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                streams_done += 1
                continue
            line = line.strip()
            if not line:
                continue
            event = None
            if channel == 'stdout' and kind == 'agent':
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    pass
            if isinstance(event, dict):
                part = event.get('part') or {}
                event_type = event.get('type', 'output')
                last_event = event_type
                if (event_type in {'step_finish', 'tool_use', 'tool_result'} or
                        event_type in {'text', 'reasoning'} and part.get('text')):
                    last_activity = time.monotonic()
                # Health probes, stderr chatter and repeated step_start events
                # are not evidence of agent work and cannot extend this deadline.
                if event_type == 'step_finish':
                    tokens = part.get('tokens') or {}
                    output_tokens += int(tokens.get('output') or 0)
                    finish = part.get('reason', '')
                    store.event(task['id'], 'model_step', f'Ответ модели завершён: {finish or "готово"}',
                                data={'output_tokens': tokens.get('output'), 'finish': finish})
                elif event_type == 'error':
                    failed = True
                    error = event.get('error', {})
                    error_detail = json.dumps(error, ensure_ascii=False)[:2500]
                    store.event(task['id'], 'agent_error', error_detail, 'error')
                elif event_type in {'tool_use', 'tool_result'}:
                    state = part.get('state') or {}
                    message = state.get('title') or part.get('tool') or translate('Инструмент', task.get('language', 'ru'))
                    last_tool = str(message)[:1000]
                    store.event(task['id'], 'tool', message, data={'status': state.get('status')})
                elif event_type in {'text', 'reasoning'}:
                    if event_type != 'reasoning' or not inference or not inference.reasoning_seen:
                        store.event(task['id'], event_type, part.get('text', '')[:6000])
                else:
                    store.event(task['id'], 'agent_event', event_type)
            else:
                store.event(task['id'], 'verification_output' if kind == 'verify' else 'output',
                            line, 'warning' if channel == 'stderr' else 'info')
        if reason is None:
            # EOF may precede process exit; keep the timeout and cancellation effective.
            while process.poll() is None:
                if cancel.wait(0.1):
                    reason = 'cancelled'
                    break
                if time.monotonic() - started >= budget:
                    reason, failed = 'timeout', True
                    break
                problem = idle_problem()
                if problem:
                    reason, failed, error_detail = 'idle_timeout', True, problem
                    break
                problem = check_runtime()
                if problem:
                    reason, failed, error_detail = 'runtime_unavailable', True, problem
                    break
    finally:
        readers_stop.set()
        stop_tree(process)
        for thread in readers:
            thread.join(timeout=1)
        store.update(task['id'], pid=None, pid_created=None)
    return {'exit_code': process.returncode, 'failed': failed or (reason is None and process.returncode != 0),
            'reason': reason, 'error_detail': error_detail, 'duration': time.monotonic() - started,
            'output_tokens': output_tokens, 'last_event': last_event, 'last_tool': last_tool}


def agent_environment(task):
    from .tasks import document_path
    environment = os.environ.copy()
    environment['AGENTVISOR_LANGUAGE'] = task.get('language', 'ru')
    environment['OPENCODE_CONFIG'] = str(document_path(task, 'opencode.json'))
    environment['OPENCODE_DISABLE_AUTOUPDATE'] = 'true'
    token = os.environ.get('AGENTVISOR_MODEL_TOKEN')
    if token:
        environment['OPENCODE_CONFIG_CONTENT'] = json.dumps({'provider': {'agentvisor': {'options': {'apiKey': token}}}})
    return environment
