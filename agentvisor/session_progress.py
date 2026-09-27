"""Bounded evidence of repeated investigation inside one agent session.

This is not a timer for reasoning or an acceptance verifier. It only requests a
fresh session when completed operations demonstrate a repeat loop. Callers must
defer recovery while a tool or finite command is still running.
"""
import hashlib
import json
import re
import threading
import time
from collections import Counter, OrderedDict, deque


_READS = {'read', 'glob', 'grep', 'get_progress'}
_WRITES = {'write', 'edit', 'apply_patch'}
_NOTES = {'memory.md', 'progress.md', 'goal.md', 'run_prompt.md', 'done.md',
          'step_review.json', 'agents.md'}
_VERIFY = re.compile(
    r'^(?:[\w./\\:\-]+[/\\])?(?:pytest(?:\.exe)?\b|tsc(?:\.cmd)?\b|'
    r'(?:python(?:\d+(?:\.\d+)*)?(?:\.exe)?|py)\s+-m\s+'
    r'(?:pytest|unittest|compileall|py_compile)\b|'
    r'(?:npm|pnpm|yarn)(?:\.cmd)?\s+(?:test\b|(?:run\s+)?(?:build|typecheck|lint|test)\b)|'
    r'cargo\s+(?:test|check)\b|go\s+test\b|dotnet\s+(?:test|build)\b|ruff\s+check\b)', re.I)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()[:24]


def _name(tool):
    return tool.removeprefix('agentvisor_process_')


def _path(arguments):
    return str(next((arguments[key] for key in ('filePath', 'file_path', 'path')
                     if arguments.get(key)), '')).replace('\\', '/')


def _product_write(tool, arguments):
    path = _path(arguments)
    if tool == 'apply_patch':
        patch = str(arguments.get('patchText') or arguments.get('patch') or '')
        paths = re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', patch, re.M)
        return bool(paths) and any(_product_write('write', {'path': item}) for item in paths)
    return bool(path) and path.rsplit('/', 1)[-1].lower() not in _NOTES and '.agentvisor/' not in path.lower()


def is_verification_command(command):
    # Accommodate the actual Windows command shape without classifying a file
    # read merely because its output or a quoted string mentions pytest.
    command = str(command or '').strip()
    location = re.match(r'^(?:cd|Set-Location)\s+(?:"[^"]*"|\x27[^\x27]*\x27|[^;]+);\s*', command, re.I)
    if location:
        command = command[location.end():]
    command = re.sub(r'^&\s*', '', command)
    executable = re.match(r'^("[^"]+"|\x27[^\x27]+\x27|\S+)(.*)$', command, re.S)
    if executable:
        command = executable[1].strip('\"\x27').replace('\\', '/').rsplit('/', 1)[-1] + executable[2]
    command = re.sub(r'^(?:npx|pnpm exec)(?:\.cmd)?\s+', '', command, flags=re.I)
    return bool(_VERIFY.match(command))


class SessionProgress:
    """Observe full tool arguments/results before their diagnostic truncation.

    ``inferred`` read results are usable evidence; inferred writes are not proof
    of a successful edit. Feed authoritative CLI completion afterwards. For
    supervisor-owned commands, use ``command_result`` only on actual completion.
    """
    def __init__(self, *, clock=time.monotonic, min_seconds=180, min_operations=24,
                 min_repeats=16, repeat_ratio=.5, window=32):
        self.clock, self.min_seconds = clock, min_seconds
        self.min_operations, self.min_repeats = min_operations, min_repeats
        self.repeat_ratio = repeat_ratio
        self.window = deque(maxlen=window)
        self.pending, self.finished = OrderedDict(), OrderedDict()
        self.results = OrderedDict()
        self.timeouts = OrderedDict()
        self.epoch, self.since = 0, clock()
        self.last_result = None
        self.new_progress_count = 0
        self.lock = threading.RLock()

    @staticmethod
    def _remember(mapping, key, value, limit=256):
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > limit:
            mapping.popitem(last=False)

    def start(self, call_id, tool, arguments, *, authoritative=False):
        if not call_id:
            return
        with self.lock:
            previous = self.pending.get(call_id)
            if (previous is not None and not authoritative or
                    previous is None and call_id in self.finished):
                return
            arguments = arguments if isinstance(arguments, dict) else {}
            name = _name(tool)
            identity = dict(arguments)
            if name == 'task':
                identity.pop('description', None)  # A new title is not a new investigation.
            self._remember(self.pending, call_id, {
                'tool': name, 'signature': _hash([name, identity]),
                'epoch': previous['epoch'] if previous else self.epoch,
                'label': (name + ' ' + (_path(arguments) or str(arguments.get('pattern') or
                         arguments.get('prompt') or arguments.get('command') or '')))[:300],
                'write': name in _WRITES and _product_write(name, arguments),
                'explore': name == 'task' and arguments.get('subagent_type') == 'explore',
                'command': str(arguments.get('command') or '') if name in {'bash', 'exec'} else '',
            }, limit=128)

    def _result(self, signature, label):
        if signature in self.results:
            return
        self._remember(self.results, signature, {'fingerprint': signature, 'operation': label[:300]})
        self.new_progress_count += 1
        self.epoch += 1
        self.since = self.clock()
        self.window.clear()
        self.timeouts.clear()
        self.last_result = label[:300]

    def finish(self, call_id, output='', error='', status='completed', exit_code=None, inferred=False):
        with self.lock:
            entry = self.pending.get(call_id)
            if entry is None:
                return
            # Keep a provisional read for deduplication, and writes until the CLI
            # supplies authoritative status. Never trust an inferred success.
            if inferred and entry['tool'] not in _READS:
                return
            duplicate = call_id in self.finished
            self._remember(self.finished, call_id, True)
            if not inferred:
                self.pending.pop(call_id, None)
            if duplicate:
                if not inferred and entry['tool'] in _READS and entry['epoch'] == self.epoch:
                    # A provisional result may precede complete CLI arguments.
                    # Correct its identity/status in place, never count it twice.
                    updated = self._read_result(entry, call_id, output, error, status)
                    for index, item in enumerate(self.window):
                        if item[2] == call_id:
                            self.window[index] = updated
                            break
                return
            failed = bool(error) or status in {'error', 'failed', 'timed_out'} or bool(exit_code)
            if entry['write'] and not failed and not inferred and status == 'completed':
                self._result(entry['signature'], entry['label'])
            elif entry['command'] and not inferred:
                self.command_result(call_id, entry['command'], status=status, exit_code=exit_code)
            elif entry['epoch'] == self.epoch and entry['tool'] in _READS:
                self.window.append(self._read_result(entry, call_id, output, error, status))
            elif entry['epoch'] == self.epoch and entry['explore'] and (
                    status == 'timed_out' or re.search(r'time[ -]?out|timed out', str(error), re.I)):
                key = (entry['signature'], entry['label'])
                self._remember(self.timeouts, key, self.timeouts.get(key, 0) + 1, limit=64)

    @staticmethod
    def _read_result(entry, call_id, output, error, status):
        # get_progress may contain volatile runtime timestamps. Its completion
        # is never an implementation or acceptance result.
        content = None if entry['tool'] == 'get_progress' else str(output)
        signature = _hash([entry['signature'], content, str(error), status])
        return signature, entry['label'], call_id

    def command_result(self, process_id, command, *, status, exit_code=None, cwd=''):
        """A completed new check is evidence; running/polling and reads are not.

        Repeating the same check with the same exit status cannot buy more time.
        Its elapsed-time text is deliberately absent from the result identity.
        """
        with self.lock:
            if status not in {'completed', 'failed'} or type(exit_code) is not int:
                return
            command = str(command).strip()
            if not is_verification_command(command):
                return
            signature = _hash(['check', command, cwd, status, exit_code])
            self._result(signature, 'check ' + command)

    def problem(self, *, pending=False):
        """Return bounded recovery evidence; do not interrupt an active operation."""
        with self.lock:
            if pending or self.clock() - self.since < self.min_seconds:
                return None
            elapsed = round(self.clock() - self.since, 1)
            repeated_timeouts = [(label, count) for (_, label), count in self.timeouts.items() if count >= 2]
            if repeated_timeouts:
                return {'reason': 'repeated_explore_timeout', 'seconds_without_result': elapsed,
                        'evidence': [{'operation': label, 'count': count}
                                     for label, count in repeated_timeouts[:4]],
                        'next_action': 'Use the saved findings and a direct bounded check; do not repeat the same exploration.'}
            counts = Counter(item[0] for item in self.window)
            repeats = sum(count - 1 for count in counts.values())
            if (len(self.window) < self.min_operations or repeats < self.min_repeats or
                    repeats / len(self.window) < self.repeat_ratio or max(counts.values(), default=0) < 4):
                return None
            labels = {item[0]: item[1] for item in self.window}
            return {'reason': 'repeated_investigation', 'seconds_without_result': elapsed,
                    'operations': len(self.window), 'repeats': repeats,
                    'last_result': self.last_result,
                    'evidence': [{'operation': labels[key], 'count': count}
                                 for key, count in counts.most_common(4) if count > 1],
                    'next_action': 'Continue from verified findings in a fresh session; make one concrete edit or targeted check before repeating these reads.'}

    def snapshot(self):
        with self.lock:
            return {'operations': len(self.window), 'last_result': self.last_result,
                    'new_progress_count': self.new_progress_count,
                    'result_evidence': list(self.results.values())[-8:],
                    'seconds_without_result': round(self.clock() - self.since, 1),
                    'problem': self.problem()}
