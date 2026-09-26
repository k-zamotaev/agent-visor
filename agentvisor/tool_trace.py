"""Observe tool calls at the model boundary, including calls the CLI never finishes."""
import hashlib
import json
import threading
import time
from copy import deepcopy


def fingerprint(tool, arguments):
    return hashlib.sha256(json.dumps([tool, arguments], sort_keys=True,
                         ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def bounded_input(arguments):
    serialized = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(serialized) <= 4000:
        return deepcopy(arguments)
    result = {'preview': serialized[:4000], 'truncated': True}
    if isinstance(arguments, dict) and isinstance(arguments.get('timeout'), (int, float)):
        result['timeout'] = arguments['timeout']
    return result


class ToolTrace:
    def __init__(self, store, task):
        self.store, self.task = store, task
        self.pending, self.finished = {}, set()
        self.observed_results = {}
        self.failures = []
        self.lock = threading.RLock()

    def start(self, call_id, tool, arguments):
        if not call_id:
            return
        with self.lock:
            if call_id in self.pending or call_id in self.finished:
                return
            entry = {'call_id': call_id, 'tool': tool, 'input': bounded_input(arguments),
                     'status': 'pending', 'started': time.time()}
            self.pending[call_id] = entry
            self.store.event(self.task['id'], 'tool_started', tool, data=entry)

    def finish(self, call_id, output='', error='', status='completed', arguments=None, tool='', inferred=False,
               exit_code=None):
        if not call_id:
            return
        with self.lock:
            if call_id in self.finished and (inferred or call_id not in self.observed_results):
                return
            entry = self.pending.pop(call_id, None) or self.observed_results.pop(call_id, None)
            if entry is None:
                entry = {'call_id': call_id, 'tool': tool, 'input': bounded_input(arguments or {})}
            entry.update(status=status, output=str(output)[-4000:], error=str(error)[-2000:],
                         inferred=inferred, exit_code=exit_code)
            self.finished.add(call_id)
            if inferred:
                # A next model request can arrive before the CLI emits exit/error
                # metadata. Preserve enough state for that authoritative update.
                self.observed_results[call_id] = entry
                while len(self.observed_results) > 64:
                    self.observed_results.pop(next(iter(self.observed_results)))
            if error or status in {'error', 'timed_out'}:
                self.failures.append(entry)
                self.failures = self.failures[-8:]
            self.store.event(self.task['id'], 'tool_finished', entry['tool'],
                             'warning' if error else 'info', data=entry)

    def observe_messages(self, messages):
        # Process historical calls only to resolve this gateway's pending calls.
        for message in messages:
            if message.get('role') == 'tool' and message.get('tool_call_id') in self.pending:
                content = message.get('content', '')
                self.finish(message['tool_call_id'], content, inferred=True)

    def observe_event(self, part):
        state = part.get('state') or {}
        call_id, tool = part.get('callID'), part.get('tool', '')
        status = state.get('status')
        if status in {'pending', 'running'}:
            self.start(call_id, tool, state.get('input') or {})
        elif status in {'completed', 'error'}:
            code = (state.get('metadata') or {}).get('exit')
            error = state.get('error') or (f'Command exited with code {code}' if code else '')
            self.finish(call_id, state.get('output', ''), error, status,
                        state.get('input'), tool, exit_code=code)

    def snapshot(self):
        with self.lock:
            return deepcopy({'pending_tools': list(self.pending.values())[-8:],
                             'tool_failures': list(self.failures)})

    def problem(self):
        # A deadline belongs to the operation, not to unrelated model tokens.
        with self.lock:
            for entry in self.pending.values():
                args = entry.get('input') or {}
                requested = args.get('timeout') if isinstance(args, dict) else None
                budget = self.task.get('idle_timeout_seconds', 300)
                if isinstance(requested, (float, int)) and requested > 0:
                    budget = min(budget, requested / 1000 + 10)
                if time.time() - entry['started'] >= budget:
                    return f"Tool did not return within {budget:g}s: {entry['tool']} {json.dumps(args, ensure_ascii=False)[:1500]}"
        return None
