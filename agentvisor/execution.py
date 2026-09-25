"""Bounded process execution and OpenCode JSON event decoding."""
import json
import os
import queue
import threading
import time

import psutil

from .processes import spawn, stop_tree
from .i18n import translate


def execute(store, task, argv, cancel, env=None, kind='agent'):
    started = time.monotonic()
    process = spawn(argv, cwd=task['workspace'], env=env)
    messages = queue.Queue(maxsize=1024)
    store.update(task['id'], pid=process.pid, pid_created=psutil.Process(process.pid).create_time())
    streams_done = 0
    output_tokens, failed, reason = 0, False, None
    error_detail = ''
    heartbeat = started
    budget = min(task['timeout_seconds'], task['max_hours'] * 3600 - task.get('elapsed', 0))

    def read(stream, channel):
        try:
            while True:
                line = stream.readline(65536)
                if not line:
                    break
                while not cancel.is_set():
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
                    if cancel.is_set():
                        break

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
                    store.event(task['id'], 'tool', message, data={'status': state.get('status')})
                elif event_type in {'text', 'reasoning'}:
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
    finally:
        stop_tree(process)
        for thread in readers:
            thread.join(timeout=1)
        store.update(task['id'], pid=None, pid_created=None)
    return {'exit_code': process.returncode, 'failed': failed or (reason is None and process.returncode != 0),
            'reason': reason, 'error_detail': error_detail, 'duration': time.monotonic() - started, 'output_tokens': output_tokens}


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
