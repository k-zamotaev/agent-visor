"""Shell-free child processes and cross-platform tree termination."""
import ctypes
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import psutil


def executable(name):
    found = shutil.which(name)
    if name == 'lms' and not found:
        roots = [Path.home() / '.lmstudio']
        pointer = Path.home() / '.lmstudio-home-pointer'
        if pointer.is_file():
            roots.insert(0, Path(pointer.read_text(encoding='utf-8').strip()))
        if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
            roots.append(Path(os.environ['LOCALAPPDATA']) / 'lm-studio')
        else:
            roots.append(Path.home() / '.cache' / 'lm-studio')
        for root in roots:
            candidate = root / 'bin' / ('lms.exe' if os.name == 'nt' else 'lms')
            if candidate.is_file():
                found = str(candidate)
                break
    if name == 'opencode' and os.name == 'nt':
        roots = [Path(found).parent] if found else []
        roots.append(Path(os.environ.get('APPDATA', '')) / 'npm')
        for root in roots:
            candidate = root / 'node_modules' / 'opencode-ai' / 'bin' / 'opencode.exe'
            if candidate.exists():
                return str(candidate)
    if found and Path(found).suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
        return found
    return None


class WindowsJob:
    """KILL_ON_JOB_CLOSE also cleans up if the supervisor crashes."""
    def __init__(self, pid):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t),
                        ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000
        process = api.OpenProcess(0x0100 | 0x0001, False, pid)
        ok = handle and process and api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        ok = ok and api.AssignProcessToJobObject(handle, process)
        if process:
            api.CloseHandle(process)
        if not ok:
            error = ctypes.get_last_error()
            if handle:
                api.CloseHandle(handle)
            raise OSError(error, 'Не удалось защитить дерево процесса Windows Job Object')
        self.handle, self.api = handle, api

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def spawn(argv, cwd=None, env=None, managed=True):
    environment = dict(os.environ if env is None else env)
    environment.setdefault('PYTHONIOENCODING', 'utf-8')
    environment.setdefault('PYTHONUTF8', '1')
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace', **kwargs)
    try:
        process.visor_job = WindowsJob(process.pid) if managed else None
    except OSError:
        stop_tree(process)
        raise
    return process


def stop_tree(process):
    if os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        job = getattr(process, 'visor_job', None)
        if job:
            job.close()
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def capture(argv, timeout=20, cancel=None, include_stderr=False, service=False, env=None, cwd=None):
    # Service commands may start a detached daemon that must outlive the CLI.
    process = spawn(argv, env=env, cwd=cwd, managed=not service)
    started = time.monotonic()
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError('Операция отменена')
            if time.monotonic() - started > timeout:
                raise TimeoutError('Команда превысила допустимое время')
            try:
                out, err = process.communicate(timeout=0.25)
                if process.returncode:
                    raise RuntimeError((err or out or 'Ошибка команды')[-2500:])
                return out + err if include_stderr else out
            except subprocess.TimeoutExpired:
                continue
    finally:
        if not service or process.poll() is None:
            stop_tree(process)


def recover_process(task):
    """Never kill a reused PID: compare its recorded creation time."""
    if not task.get('pid') or not task.get('pid_created'):
        return
    try:
        process = psutil.Process(task['pid'])
        if abs(process.create_time() - task['pid_created']) > 0.1:
            return
        children = process.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        psutil.wait_procs(children + [process], timeout=5)
    except psutil.NoSuchProcess:
        pass
