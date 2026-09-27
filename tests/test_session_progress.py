import json

import pytest

from agentvisor.session_progress import SessionProgress


class Clock:
    now = 0

    def __call__(self):
        return self.now


def detector():
    clock = Clock()
    return SessionProgress(clock=clock), clock


def complete(guard, index, tool='read', arguments=None, output='unchanged', **result):
    key = str(index)
    guard.start(key, tool, arguments or {'filePath': 'backend/app/main.py'})
    guard.finish(key, output, **result)


def repeat_reads(guard, count=24, start=0):
    for index in range(start, start + count):
        complete(guard, index, arguments={'filePath': f'backend/app/file{index % 4}.py'})


def test_repeated_investigation_has_bounded_actionable_evidence():
    guard, clock = detector()
    repeat_reads(guard)
    assert guard.problem() is None
    clock.now = 181
    problem = guard.problem()
    assert problem['reason'] == 'repeated_investigation'
    assert problem['operations'] == 24 and problem['repeats'] == 20
    assert problem['evidence'][0]['count'] == 6
    assert 'backend/app/' in problem['evidence'][0]['operation']
    assert len(json.dumps(problem)) < 2500


def test_broad_legitimate_investigation_and_different_file_ranges_do_not_trigger():
    guard, clock = detector()
    for index in range(100):
        complete(guard, index, arguments={'filePath': 'large.py', 'offset': index * 100, 'limit': 100})
    clock.now = 3600
    assert guard.problem() is None


def test_changed_read_results_and_short_rechecks_are_not_a_loop():
    guard, clock = detector()
    for index in range(40):
        complete(guard, index, output=f'changed version {index}')
    clock.now = 3600
    assert guard.problem() is None
    for index in range(40, 45):
        complete(guard, index)
    assert guard.problem() is None


def test_mixed_search_and_repeated_reads_matches_observed_research_cycle():
    guard, clock = detector()
    for index in range(16):
        complete(guard, index, arguments={'filePath': f'src/{index}.py'})
    for index in range(16, 32):
        complete(guard, index, arguments={'filePath': f'src/{index % 4}.py'})
    clock.now = 351
    problem = guard.problem()
    assert problem['reason'] == 'repeated_investigation'
    assert problem['repeats'] == 16 and problem['operations'] == 32


def test_progress_view_timestamps_cannot_disguise_repeated_investigation():
    guard, clock = detector()
    for index in range(24):
        complete(guard, index, tool='agentvisor_process_get_progress',
                 arguments={'scope': 'current'}, output=f'{{"elapsed": {index}}}')
    clock.now = 181
    assert guard.problem()['reason'] == 'repeated_investigation'


def test_reasoning_and_time_alone_never_trigger():
    guard, clock = detector()
    clock.now = 7200
    assert guard.problem() is None


def test_pending_command_defers_recovery_even_after_loop_was_detected():
    guard, clock = detector()
    repeat_reads(guard)
    clock.now = 181
    assert guard.problem(pending=True) is None
    assert guard.problem()['reason'] == 'repeated_investigation'


@pytest.mark.parametrize('path', [
    'MEMORY.md', 'PROGRESS.md', 'GOAL.md', 'DONE.md', 'RUN_PROMPT.md',
    'D:\\OpenSemantico\\.agentvisor\\tasks\\task1\\notes.txt',
])
def test_rewriting_notes_cannot_erase_loop_evidence(path):
    guard, clock = detector()
    repeat_reads(guard)
    complete(guard, 'note', tool='write', arguments={'filePath': path, 'content': 'new promise'})
    clock.now = 181
    assert guard.problem()['reason'] == 'repeated_investigation'
    assert guard.snapshot()['new_progress_count'] == 0


def test_successful_product_edit_starts_new_evidence_window_but_same_edit_does_not():
    guard, clock = detector()
    repeat_reads(guard)
    clock.now = 181
    edit = {'filePath': 'backend/app/main.py', 'oldString': 'old', 'newString': 'new'}
    complete(guard, 'edit1', tool='edit', arguments=edit)
    assert guard.problem() is None
    assert guard.snapshot()['new_progress_count'] == 1
    repeat_reads(guard, start=100)
    clock.now = 400
    complete(guard, 'edit2', tool='edit', arguments=edit)
    assert guard.problem()['reason'] == 'repeated_investigation'
    assert guard.snapshot()['new_progress_count'] == 1


def test_inferred_write_is_not_result_and_authoritative_failure_does_not_reset():
    guard, clock = detector()
    repeat_reads(guard)
    guard.start('edit', 'edit', {'filePath': 'main.py', 'newString': 'fix'})
    guard.finish('edit', 'done', inferred=True)
    guard.finish('edit', 'not found', status='error', error='oldString not found')
    clock.now = 181
    assert guard.problem()
    assert guard.snapshot()['new_progress_count'] == 0


def test_inferred_then_authoritative_read_is_counted_once():
    guard, clock = detector()
    for index in range(12):
        guard.start(str(index), 'read', {'filePath': 'main.py'})
        guard.finish(str(index), 'same', inferred=True)
        guard.finish(str(index), 'same')
    clock.now = 181
    assert guard.snapshot()['operations'] == 12
    assert guard.problem() is None


def test_reads_started_before_edit_do_not_pollute_new_window():
    guard, _ = detector()
    guard.start('old-read', 'read', {'filePath': 'main.py'})
    complete(guard, 'edit', tool='write', arguments={'filePath': 'main.py', 'content': 'fixed'})
    guard.finish('old-read', 'old result')
    assert guard.snapshot()['operations'] == 0


def test_enriching_partial_arguments_preserves_the_original_read_epoch():
    guard, _ = detector()
    guard.start('read', 'read', {})
    complete(guard, 'edit', tool='write', arguments={'filePath': 'main.py', 'content': 'fixed'})
    guard.start('read', 'read', {'filePath': 'main.py'}, authoritative=True)
    guard.finish('read', 'old result')
    assert guard.snapshot()['operations'] == 0
    assert guard.snapshot()['new_progress_count'] == 1


def test_partial_write_enrichment_uses_actual_content_for_progress_fingerprint():
    guard, _ = detector()
    for index, content in enumerate(('first', 'second', 'second')):
        identity = f'write-{index}'
        guard.start(identity, 'write', {'filePath': 'export.py'})
        guard.start(identity, 'write', {'filePath': 'export.py', 'content': content}, authoritative=True)
        guard.finish(identity, 'File written')
    assert guard.snapshot()['new_progress_count'] == 2


def test_note_only_patch_is_not_result_but_product_patch_is():
    guard, _ = detector()
    complete(guard, 'note', tool='apply_patch', arguments={
        'patchText': '*** Begin Patch\n*** Update File: MEMORY.md\n+promise\n*** End Patch'})
    assert guard.snapshot()['new_progress_count'] == 0
    complete(guard, 'code', tool='apply_patch', arguments={
        'patchText': '*** Begin Patch\n*** Update File: backend/main.py\n+fix\n*** End Patch'})
    assert guard.snapshot()['new_progress_count'] == 1


def test_new_check_result_resets_but_repeat_same_exit_status_does_not():
    guard, clock = detector()
    repeat_reads(guard)
    clock.now = 181
    guard.command_result('test1', '.venv/Scripts/python.exe -m pytest tests/test_export.py',
                         status='completed', exit_code=1)
    assert guard.problem() is None
    repeat_reads(guard, start=100)
    clock.now = 400
    guard.command_result('test2', '.venv/Scripts/python.exe -m pytest tests/test_export.py',
                         status='completed', exit_code=1)
    assert guard.problem()
    guard.command_result('test3', '.venv/Scripts/python.exe -m pytest tests/test_export.py',
                         status='completed', exit_code=0)
    assert guard.problem() is None
    assert guard.snapshot()['new_progress_count'] == 2


def test_running_checks_successful_reads_and_shell_output_are_not_progress():
    guard, clock = detector()
    repeat_reads(guard)
    guard.command_result('test', 'pytest', status='running', exit_code=None)
    guard.command_result('read', 'Get-Content main.py', status='completed', exit_code=0)
    guard.command_result('echo', 'echo pytest', status='completed', exit_code=0)
    clock.now = 181
    assert guard.problem()
    assert guard.snapshot()['new_progress_count'] == 0


@pytest.mark.parametrize('command', [
    'cd D:\\OpenSemantico\\backend; python -m pytest -q 2>&1 | Select-Object -Last 45',
    'Set-Location D:\\OpenSemantico\\frontend; npx.cmd tsc --noEmit',
    'cd "D:\\Project folder"; npm.cmd run build',
    '& "D:\\Project folder\\.venv\\Scripts\\python.exe" -m pytest',
])
def test_completed_real_windows_checks_are_observed(command):
    guard, _ = detector()
    guard.command_result('check', command, status='completed', exit_code=0)
    assert guard.snapshot()['new_progress_count'] == 1


def test_quoted_test_name_in_arbitrary_shell_command_is_not_a_check():
    guard, _ = detector()
    guard.command_result('read', "Write-Output '; pytest tests'", status='completed', exit_code=0)
    assert guard.snapshot()['new_progress_count'] == 0


def test_same_exploration_retried_after_timeout_ignores_title_changes():
    guard, clock = detector()
    for index in range(2):
        complete(guard, index, tool='task', arguments={
            'subagent_type': 'explore', 'prompt': 'Locate export structure',
            'description': f'Explore export attempt {index}'}, status='error', error='Tool timed out')
    clock.now = 601
    assert guard.problem()['reason'] == 'repeated_explore_timeout'
    assert guard.problem()['evidence'][0]['count'] == 2


def test_distinct_explorations_or_success_do_not_count_as_repeated_timeout():
    guard, clock = detector()
    for index in range(4):
        complete(guard, index, tool='task', arguments={
            'subagent_type': 'explore', 'prompt': f'Investigate separate question {index}'},
            status='error', error='Tool timed out')
    clock.now = 601
    assert guard.problem() is None


def test_every_history_is_bounded():
    guard, _ = detector()
    for index in range(300):
        complete(guard, index, tool='write', arguments={'filePath': f'src/{index}.py', 'content': 'x'})
        complete(guard, f'explore{index}', tool='task', arguments={
            'subagent_type': 'explore', 'prompt': str(index)}, status='timed_out')
        guard.start(f'pending{index}', 'read', {'filePath': str(index)})
    assert len(guard.pending) <= 128
    assert len(guard.finished) <= 256 and len(guard.results) <= 256
    assert len(guard.timeouts) <= 64
    assert len(guard.snapshot()['result_evidence']) == 8
