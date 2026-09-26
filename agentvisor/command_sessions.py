"""Owned, bounded command sessions without inherited output-pipe waits."""
import base64
import ctypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time

from .processes import WindowsJob, stop_tree


def _job_pids(job):
    """Query the job even after its original process has exited."""
    if not job or not job.handle:
        return []
    from ctypes import wintypes as w
    query = job.api.QueryInformationJobObject
    query.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    query.restype = w.BOOL
    buffer = ctypes.create_string_buffer(8 + 4096 * ctypes.sizeof(ctypes.c_size_t))
    if not query(job.handle, 3, buffer, len(buffer), None):
        raise OSError(ctypes.get_last_error(), 'Cannot query owned process job')
    count = ctypes.c_uint32.from_buffer(buffer, 4).value
    return list((ctypes.c_size_t * count).from_buffer(buffer, 8))


class CommandSessions:
    MAX_SESSIONS = 128
    MAX_ACTIVE = 16
    MAX_LOG_BYTES = 8 * 1024 * 1024

    def __init__(self, task, cancel):
        self.task, self.cancel = task, cancel
        self.workspace = Path(task['workspace']).resolve()
        self.root = self.workspace / '.agentvisor' / 'tasks' / task['id'] / 'commands'
        if not self.root.resolve().is_relative_to(self.workspace):
            raise ValueError('Command log directory leaves the workspace')
        self.started = time.monotonic()
        self.sessions = {}
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def _budget(self, payload):
        requested = payload.get('timeout_ms', 120000)
        if not isinstance(requested, (int, float)) or not 1 <= requested <= 21600000:
            raise ValueError('timeout_ms must be between 1 and 21600000')
        elapsed = time.monotonic() - self.started
        remaining = self.task.get('max_hours', 12) * 3600 - self.task.get('elapsed', 0) - elapsed
        budget = min(requested / 1000, self.task.get('timeout_seconds', 1800) - elapsed, remaining)
        if budget <= 0:
            raise ValueError('Task execution budget is exhausted')
        return budget

    def start(self, payload):
        command = payload.get('command')
        if not isinstance(command, str) or not command.strip() or len(command) > 100000:
            raise ValueError('A nonempty command of at most 100000 characters is required')
        self._yield(payload)
        shell = payload.get('shell') or ('powershell' if os.name == 'nt' else 'bash')
        if shell not in {'powershell', 'bash'}:
            raise ValueError('shell must be powershell or bash')
        cwd = Path(payload.get('cwd') or self.workspace)
        cwd = (self.workspace / cwd).resolve() if not cwd.is_absolute() else cwd.resolve()
        if not cwd.is_dir():
            raise ValueError('Command working directory does not exist')
        budget = self._budget(payload)
        if shell == 'powershell':
            executable = shutil.which('powershell.exe' if os.name == 'nt' else 'pwsh')
            script = ("$ProgressPreference='SilentlyContinue'; "
                      '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); '
                      '$OutputEncoding = [Console]::OutputEncoding;\n' + command)
            encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
            argv = [executable, '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text',
                    '-EncodedCommand', encoded]
        else:
            executable = shutil.which('bash')
            argv = [executable, '-lc', command]
        if not executable:
            raise ValueError('Requested shell is not installed')
        with self.lock:
            if self.closing.is_set() or self.cancel.is_set():
                raise InterruptedError('Command execution cancelled')
            if len(self.sessions) >= self.MAX_SESSIONS:
                raise ValueError('Command session limit reached; end this iteration and continue in a new session')
            active = sum(not entry['cleaned'] and self._alive(entry) for entry in self.sessions.values())
            if active >= self.MAX_ACTIVE:
                raise ValueError('Too many active command sessions; stop an owned process before starting another')
            identifier = secrets.token_hex(12)
            folder = self.root / identifier
            folder.mkdir(parents=True)
            output, error = folder / 'stdout.log', folder / 'stderr.log'
            kwargs = ({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
                      else {'start_new_session': True})
            with output.open('wb') as stdout, error.open('wb') as stderr:
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, **kwargs)
            try:
                process.visor_job = WindowsJob(process.pid) if os.name == 'nt' else None
            except OSError:
                stop_tree(process)
                raise
            entry = {'process': process, 'deadline': time.monotonic() + budget,
                     'status': 'running', 'stdout_log': output, 'stderr_log': error,
                     'timeout_ms': round(budget * 1000), 'shell': shell,
                     'background': bool(payload.get('background')), 'cleaned': False}
            self.sessions[identifier] = entry
        return self.poll({'process_id': identifier, 'yield_ms': payload.get('yield_ms', 1000)})

    def _alive(self, entry):
        process = entry['process']
        if process.poll() is None:
            return True
        if os.name == 'nt':
            return bool(_job_pids(process.visor_job))
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _terminate(self, entry, status):
        if not entry['cleaned']:
            process = entry['process']
            if os.name == 'nt':
                # The handle owns the tree even after the original PID exits.
                # Do not look up that numeric PID again: it may have been reused.
                process.visor_job.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            else:
                stop_tree(process)
            entry['cleaned'] = True
        entry['status'] = status

    def _refresh(self, entry):
        try:
            self._refresh_state(entry)
        except OSError as error:
            entry['error'] = str(error)[:1000]
            self._terminate(entry, 'failed')

    def _refresh_state(self, entry):
        if entry['cleaned']:
            return
        process = entry['process']
        if self.cancel.is_set():
            self._terminate(entry, 'cancelled')
        elif any(entry[key].stat().st_size >= self.MAX_LOG_BYTES
                 for key in ('stdout_log', 'stderr_log')):
            self._terminate(entry, 'output_limit')
        elif time.monotonic() >= entry['deadline']:
            alive = self._alive(entry)
            self._terminate(entry, 'timed_out' if alive and entry['status'] == 'running'
                            else 'completed')
        elif process.poll() is not None and (not entry['background'] or not self._alive(entry)):
            entry['status'] = 'completed'

    @staticmethod
    def _tail(path):
        try:
            with path.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 16000))
                return stream.read().decode('utf-8', errors='replace')[-12000:]
        except OSError as error:
            return '[Command log unavailable: ' + str(error)[:500] + ']'

    def _result(self, identifier, entry):
        stdout, stderr = self._tail(entry['stdout_log']), self._tail(entry['stderr_log'])
        output = stdout + ('\n[stderr]\n' + stderr if stderr else '')
        return {'process_id': identifier, 'status': entry['status'],
                'exit_code': entry['process'].poll(), 'output': output[-12000:],
                'stdout_log': str(entry['stdout_log']), 'stderr_log': str(entry['stderr_log']),
                'timeout_ms': entry['timeout_ms'], 'shell': entry['shell'],
                'error': entry.get('error', '')}

    def snapshots(self):
        with self.lock:
            for entry in self.sessions.values():
                self._refresh(entry)
            return [self._result(identifier, entry) for identifier, entry in self.sessions.items()]

    def poll(self, payload):
        identifier = payload.get('process_id')
        milliseconds = self._yield(payload)
        deadline = time.monotonic() + milliseconds / 1000
        while True:
            with self.lock:
                entry = self.sessions.get(identifier)
                if entry is None:
                    raise ValueError('Unknown owned process_id')
                self._refresh(entry)
                if entry['status'] != 'running' or time.monotonic() >= deadline:
                    return self._result(identifier, entry)
            self.closing.wait(min(.05, max(0, deadline - time.monotonic())))

    @staticmethod
    def _yield(payload):
        milliseconds = payload.get('yield_ms', 0)
        if not isinstance(milliseconds, (int, float)) or not 0 <= milliseconds <= 1000:
            raise ValueError('yield_ms must be between 0 and 1000')
        return milliseconds

    def stop(self, payload):
        identifier = payload.get('process_id')
        with self.lock:
            entry = self.sessions.get(identifier)
            if entry is None:
                raise ValueError('Unknown owned process_id')
            self._terminate(entry, 'stopped')
            return self._result(identifier, entry)

    def _monitor(self):
        while not self.closing.wait(.05):
            with self.lock:
                for entry in self.sessions.values():
                    self._refresh(entry)

    def close(self):
        self.closing.set()
        self.monitor.join(timeout=2)
        with self.lock:
            for entry in self.sessions.values():
                if not entry['cleaned']:
                    self._terminate(entry, 'stopped' if entry['status'] == 'running' else entry['status'])
