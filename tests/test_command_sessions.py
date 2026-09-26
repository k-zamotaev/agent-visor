"""Real process tests for owned command sessions and inherited output handles."""
import os
import base64
from pathlib import Path
import shlex
import sys
import threading
import time

import psutil
import pytest

from agentvisor.command_sessions import CommandSessions


@pytest.fixture
def sessions(tmp_path):
    task = {'id': 'command-test', 'workspace': str(tmp_path),
            'timeout_seconds': 30, 'max_hours': 1, 'elapsed': 0}
    runner = CommandSessions(task, threading.Event())
    yield runner
    runner.close()


def python_command(source):
    if os.name == 'nt':
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        # Windows PowerShell 5 rebuilds argv for native programs. Avoid embedded
        # double quotes in this test helper, separately from shell interpolation.
        encoded = base64.b64encode(source.encode()).decode()
        source = 'import base64;exec(base64.b64decode(' + repr(encoded) + '))'
        return '& ' + quote(sys.executable) + ' -c ' + quote(source)
    return shlex.join([sys.executable, '-c', source])


def wait_result(runner, identifier, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = runner.poll({'process_id': identifier, 'yield_ms': 100})
        if result['status'] != 'running':
            return result
    pytest.fail('Command did not finish in time')


def child_command():
    return python_command('import subprocess,sys; '
                          'p=subprocess.Popen([sys.executable,"-c",'
                          '"import time; time.sleep(20)"]); '
                          'print("CHILD="+str(p.pid),flush=True)')


def child_pid(runner, identifier):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = runner.poll({'process_id': identifier, 'yield_ms': 100})
        if 'CHILD=' in result['output']:
            return int(result['output'].split('CHILD=')[1].splitlines()[0])
        time.sleep(.05)
    pytest.fail('Child process did not start')


def wait_gone(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(.05)
    pytest.fail('Owned child survived cleanup')


def test_inherited_output_does_not_wait_for_child_and_close_cleans_it(sessions):
    result = sessions.start({'command': child_command(), 'yield_ms': 0})
    identifier = result['process_id']
    pid = child_pid(sessions, identifier)
    finished = wait_result(sessions, identifier)
    assert finished['status'] == 'completed'
    assert finished['exit_code'] == 0
    assert psutil.pid_exists(pid)
    assert Path(finished['stdout_log']).is_file()
    sessions.close()
    wait_gone(pid)


def test_background_children_survive_shell_exit_until_explicit_stop(sessions):
    result = sessions.start({'command': child_command(), 'background': True, 'yield_ms': 0})
    pid = child_pid(sessions, result['process_id'])
    time.sleep(.15)
    live = sessions.poll({'process_id': result['process_id']})
    assert live['status'] == 'running'
    stopped = sessions.stop({'process_id': result['process_id']})
    assert stopped['status'] == 'stopped'
    wait_gone(pid)


def test_deadline_stops_descendants_without_polling(sessions):
    result = sessions.start({'command': child_command(), 'background': True,
                             'timeout_ms': 2500, 'yield_ms': 0})
    pid = child_pid(sessions, result['process_id'])
    # Once output is available, no polling is needed to enforce the deadline.
    time.sleep(2.7)
    wait_gone(pid)
    assert sessions.poll({'process_id': result['process_id']})['status'] == 'timed_out'


def test_cancel_stops_owned_tree(sessions):
    result = sessions.start({'command': child_command(), 'background': True, 'yield_ms': 0})
    pid = child_pid(sessions, result['process_id'])
    sessions.cancel.set()
    wait_gone(pid)
    assert sessions.poll({'process_id': result['process_id']})['status'] == 'cancelled'
    with pytest.raises(InterruptedError):
        sessions.start({'command': python_command('print(1)')})


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PowerShell')
def test_native_powershell_preserves_variables(sessions):
    result = sessions.start({'command': "$name='literal $value'; Write-Output $name", 'yield_ms': 0})
    result = wait_result(sessions, result['process_id'])
    assert result['exit_code'] == 0
    assert result['output'].strip() == 'literal $value'


def test_poll_preserves_logs_and_bounds_output(sessions):
    result = sessions.start({'command': python_command('print("x"*16000)'), 'yield_ms': 0})
    result = wait_result(sessions, result['process_id'])
    assert len(result['output']) == 12000
    assert Path(result['stdout_log']).stat().st_size > 12000
    with pytest.raises(ValueError, match='Unknown owned'):
        sessions.stop({'process_id': str(os.getpid())})


def test_task_budget_caps_requested_timeout(sessions):
    sessions.task['timeout_seconds'] = .5
    result = sessions.start({'command': python_command('import time; time.sleep(20)'),
                             'timeout_ms': 120000, 'yield_ms': 0})
    assert 1 <= result['timeout_ms'] <= 500
    assert wait_result(sessions, result['process_id'])['status'] == 'timed_out'


@pytest.mark.skipif(os.name != 'nt', reason='Windows Start-Process regression')
def test_start_process_server_returns_and_remains_owned(sessions, tmp_path):
    server = tmp_path / 'server.py'
    server.write_text('import time\nprint("READY", flush=True)\ntime.sleep(20)\n')
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = ('$p = Start-Process -FilePath ' + quote(sys.executable)
               + ' -ArgumentList ' + quote(str(server)) + ' -WindowStyle Hidden -PassThru'
               + ' -RedirectStandardOutput ' + quote(tmp_path / 'server.out')
               + ' -RedirectStandardError ' + quote(tmp_path / 'server.err')
               + '; Write-Output "CHILD=$($p.Id)"')
    result = sessions.start({'command': command, 'yield_ms': 0, 'background': True})
    pid = child_pid(sessions, result['process_id'])
    assert sessions.poll({'process_id': result['process_id']})['status'] == 'running'
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (tmp_path / 'server.out').read_text().strip():
        time.sleep(.05)
    assert 'READY' in (tmp_path / 'server.out').read_text()
    sessions.close()
    wait_gone(pid)


def test_invalid_yield_does_not_launch_command(sessions):
    with pytest.raises(ValueError, match='yield_ms'):
        sessions.start({'command': python_command('print(1)'), 'yield_ms': 1001})
    assert not sessions.sessions


def test_command_failure_is_reported(sessions):
    result = sessions.start({'command': python_command('import sys; sys.exit(7)'), 'yield_ms': 0})
    result = wait_result(sessions, result['process_id'])
    assert result['exit_code'] != 0


def test_remaining_iteration_budget_is_not_reset(sessions):
    sessions.task['timeout_seconds'] = 10
    sessions.started -= 9
    result = sessions.start({'command': python_command('import time; time.sleep(20)'),
                             'timeout_ms': 120000, 'yield_ms': 0})
    assert 1 <= result['timeout_ms'] <= 1000
    assert wait_result(sessions, result['process_id'])['status'] == 'timed_out'


def test_runaway_output_is_stopped_without_poll(sessions):
    sessions.MAX_LOG_BYTES = 16000
    source = 'import sys,time\nwhile True:\n sys.stdout.write("x"*16000); sys.stdout.flush(); time.sleep(.01)'
    result = sessions.start({'command': python_command(source), 'yield_ms': 0})
    process = sessions.sessions[result['process_id']]['process']
    deadline = time.monotonic() + 5
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    snapshot = sessions.snapshots()[0]
    assert snapshot['process_id'] == result['process_id']
    assert snapshot['status'] == 'output_limit'
    assert len(snapshot['output']) == 12000
    assert Path(snapshot['stdout_log']).stat().st_size < 1000000


def test_total_session_limit(sessions):
    sessions.MAX_SESSIONS = 1
    result = sessions.start({'command': python_command('print(1)'), 'yield_ms': 0})
    wait_result(sessions, result['process_id'])
    with pytest.raises(ValueError, match='session limit'):
        sessions.start({'command': python_command('print(2)')})


def test_active_session_limit(sessions):
    sessions.MAX_ACTIVE = 1
    result = sessions.start({'command': python_command('import time; time.sleep(20)'), 'yield_ms': 0})
    with pytest.raises(ValueError, match='Too many active'):
        sessions.start({'command': python_command('print(1)')})
    sessions.stop({'process_id': result['process_id']})


def test_relative_working_directory_is_relative_to_workspace(sessions, tmp_path):
    (tmp_path / 'child').mkdir()
    result = sessions.start({'command': python_command('import os; print(os.getcwd())'),
                             'cwd': 'child', 'yield_ms': 0})
    result = wait_result(sessions, result['process_id'])
    assert result['output'].strip() == str(tmp_path / 'child')


def test_missing_log_cannot_kill_deadline_monitor(sessions):
    result = sessions.start({'command': python_command('print(1)'), 'yield_ms': 0})
    result = wait_result(sessions, result['process_id'])
    Path(result['stdout_log']).unlink()
    time.sleep(.15)
    damaged = sessions.poll({'process_id': result['process_id']})
    assert damaged['status'] == 'failed'
    assert damaged['error']
    assert sessions.monitor.is_alive()
