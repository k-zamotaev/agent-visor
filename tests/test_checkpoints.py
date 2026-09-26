import json
import os
from pathlib import Path
import subprocess

import pytest

from agentvisor import checkpoints as cp


def git(workspace, *args):
    return subprocess.run(['git', '-C', str(workspace), *args], check=True, capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).stdout


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    git(workspace, 'init', '-q')
    (workspace / '.gitignore').write_text('.agentvisor/\n*.ignored\nnode_modules/\n')
    (workspace / 'app.py').write_text('original\n')
    (workspace / 'deleted.txt').write_text('tracked deletion\n')
    git(workspace, 'add', '.')
    git(workspace, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
        'commit', '-qm', 'Initial fixture')
    (workspace / 'app.py').write_text('staged user change\n')
    git(workspace, 'add', 'app.py')
    (workspace / 'app.py').write_text('unstaged user change\n')
    (workspace / 'deleted.txt').unlink()
    (workspace / 'src').mkdir()
    (workspace / 'src' / 'new.py').write_text('untracked source\n')
    (workspace / 'secret.ignored').write_text('ignored secret\n')
    (workspace / 'node_modules').mkdir()
    (workspace / 'node_modules' / 'package.js').write_text('ignored dependency\n')
    return {'id': 'checkpoint-test', 'workspace': str(workspace), 'goal_version': 1}


def snapshot_dirs(task):
    return [path for path in cp._root(task).iterdir() if cp.SNAPSHOT.fullmatch(path.name)]


def experiments(task):
    return [path for path in cp._root(task).iterdir() if cp.EXPERIMENT.fullmatch(path.name)]


def test_dirty_and_untracked_copy_never_changes_original_files_branch_or_index(project):
    workspace = Path(project['workspace'])
    index = (workspace / '.git' / 'index').read_bytes()
    head = git(workspace, 'rev-parse', 'HEAD')
    staged = git(workspace, 'diff', '--cached', '--binary')
    checkpoint = cp.save_checkpoint(project, 'before experiment')
    (workspace / 'app.py').write_text('newer original change\n')
    copy = Path(cp.fork_checkpoint(project, checkpoint))
    assert (copy / 'app.py').read_text() == 'unstaged user change\n'
    assert (copy / 'src' / 'new.py').read_text() == 'untracked source\n'
    assert not any((copy / path).exists() for path in
                   ('deleted.txt', '.git', '.agentvisor', 'secret.ignored', 'node_modules'))
    (copy / 'app.py').write_text('isolated alternative\n')
    assert (workspace / 'app.py').read_text() == 'newer original change\n'
    assert (workspace / 'secret.ignored').read_text() == 'ignored secret\n'
    assert (workspace / '.git' / 'index').read_bytes() == index
    assert git(workspace, 'rev-parse', 'HEAD') == head
    assert git(workspace, 'diff', '--cached', '--binary') == staged


def test_repeated_snapshot_reuses_content_and_writes_each_blob_once(project, monkeypatch):
    workspace = Path(project['workspace'])
    (workspace / 'duplicate.py').write_bytes((workspace / 'app.py').read_bytes())
    calls = []
    original = Path.write_bytes
    def record(path, data):
        if cp.BLOB.fullmatch(path.name):
            calls.append(path.name)
        return original(path, data)
    monkeypatch.setattr(Path, 'write_bytes', record)
    first = cp.save_checkpoint(project, 'first')
    second = cp.save_checkpoint(project, 'second')
    assert first['id'] == second['id']
    manifest = json.loads((Path(first['path']) / 'manifest.json').read_text())
    assert len(calls) == len(set(item['sha256'] for item in manifest.values()))
    assert len(snapshot_dirs(project)) == 1


def test_snapshot_pruning_preserves_unknown_directories_and_experiments(project):
    root = cp._root(project)
    unknown = root / 'user-saved-copy'
    unknown.mkdir()
    (unknown / 'manifest.json').write_text('{}')
    (unknown / ('a' * 64)).write_bytes(b'keep')
    manual = root / ('experiment-' + 'b' * 12)
    manual.mkdir()
    (manual / 'user.txt').write_text('keep experiment')
    for index in range(5):
        Path(project['workspace'], 'app.py').write_text(f'change {index}')
        last = cp.save_checkpoint(project, str(index))
    assert len(snapshot_dirs(project)) == 3
    assert Path(last['path']).exists()
    assert (unknown / ('a' * 64)).read_bytes() == b'keep'
    assert (manual / 'user.txt').read_text() == 'keep experiment'


def test_two_experiment_limit_preserves_both_copies(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    first = Path(cp.fork_checkpoint(project, checkpoint))
    second = Path(cp.fork_checkpoint(project, checkpoint))
    (first / 'user-change.txt').write_text('do not remove')
    with pytest.raises(ValueError, match='Two isolated'):
        cp.fork_checkpoint(project, checkpoint)
    assert len(experiments(project)) == 2 and second.exists()
    assert (first / 'user-change.txt').read_text() == 'do not remove'


def test_partial_snapshot_and_staging_cache_are_rebuilt_safely(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    folder = Path(checkpoint['path'])
    (folder / 'manifest.json').unlink()
    pending = cp._root(project) / ('.pending-' + 'a' * 32)
    pending.mkdir()
    (pending / ('b' * 64)).write_bytes(b'partial blob')
    again = cp.save_checkpoint(project, 'recovered')
    assert again['id'] == checkpoint['id']
    assert not pending.exists()
    assert Path(cp.fork_checkpoint(project, again), 'app.py').exists()


def test_unknown_content_in_partial_cache_is_not_deleted(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    folder = Path(checkpoint['path'])
    (folder / 'manifest.json').unlink()
    (folder / 'user.txt').write_text('preserve')
    with pytest.raises(ValueError, match='unknown entries'):
        cp.save_checkpoint(project, 'retry')
    assert (folder / 'user.txt').read_text() == 'preserve'


def test_failed_snapshot_write_publishes_nothing_and_removes_own_partial_files(project, monkeypatch):
    original = Path.write_bytes
    def fail(path, data):
        if path.name == 'manifest.json':
            raise OSError('simulated full disk')
        return original(path, data)
    monkeypatch.setattr(Path, 'write_bytes', fail)
    with pytest.raises(OSError, match='full disk'):
        cp.save_checkpoint(project, 'interrupted')
    assert list(cp._root(project).iterdir()) == []


def test_corrupt_blob_is_rejected_before_creating_experiment(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    folder = Path(checkpoint['path'])
    manifest = json.loads((folder / 'manifest.json').read_text())
    digest = next(iter(manifest.values()))['sha256']
    (folder / digest).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='checksum'):
        cp.fork_checkpoint(project, checkpoint)
    with pytest.raises(ValueError, match='checksum'):
        cp.save_checkpoint(project, 'reuse')
    assert not experiments(project)


@pytest.mark.parametrize('path', ['../outside.txt', '/absolute.txt', 'C:/outside.txt',
                                  'folder/../../outside', '.git/config', '.agentvisor/task',
                                  'folder\\outside', 'name:stream', 'a//b'])
def test_manifest_traversal_and_reserved_paths_are_rejected(project, path):
    checkpoint = cp.save_checkpoint(project, 'source')
    folder = Path(checkpoint['path'])
    manifest = json.loads((folder / 'manifest.json').read_text())
    manifest[path] = next(iter(manifest.values()))
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='file path'):
        cp.fork_checkpoint(project, checkpoint)
    assert not experiments(project)


@pytest.mark.parametrize('limit,value', [('MAX_FILES', 1), ('MAX_FILE_BYTES', 3), ('MAX_BYTES', 8)])
def test_snapshot_source_limits_fail_before_publication(project, monkeypatch, limit, value):
    monkeypatch.setattr(cp, limit, value)
    with pytest.raises(ValueError, match='limit exceeded'):
        cp.save_checkpoint(project, 'too large')
    assert list(cp._root(project).iterdir()) == []


def test_manifest_and_duplicate_blob_total_are_bounded_when_forking(project, monkeypatch):
    checkpoint = cp.save_checkpoint(project, 'source')
    monkeypatch.setattr(cp, 'MAX_MANIFEST_BYTES', 32)
    with pytest.raises(ValueError, match='size limit'):
        cp.fork_checkpoint(project, checkpoint)
    monkeypatch.setattr(cp, 'MAX_MANIFEST_BYTES', 2 * 1024 * 1024)
    monkeypatch.setattr(cp, 'MAX_BYTES', 8)
    with pytest.raises(ValueError, match='size limit'):
        cp.fork_checkpoint(project, checkpoint)
    assert not experiments(project)


def test_non_git_workspace_fails_without_changing_user_content(tmp_path):
    (tmp_path / 'user.txt').write_text('original')
    task = {'id': 'task', 'workspace': str(tmp_path), 'goal_version': 1}
    with pytest.raises(ValueError, match='Git workspace'):
        cp.save_checkpoint(task, 'unsupported')
    assert (tmp_path / 'user.txt').read_text() == 'original'


@pytest.mark.skipif(os.name == 'nt', reason='Executable file mode requires a POSIX filesystem')
def test_posix_executable_bit_changes_identity_and_is_restored_in_copy(project):
    script = Path(project['workspace'], 'run.sh')
    script.write_text('#!/bin/sh\necho ready\n')
    script.chmod(0o644)
    first = cp.save_checkpoint(project, 'ordinary')
    script.chmod(0o755)
    second = cp.save_checkpoint(project, 'executable')
    assert first['id'] != second['id']
    copy = Path(cp.fork_checkpoint(project, second))
    assert (copy / 'run.sh').stat().st_mode & 0o111 == 0o111


def symlink_or_skip(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f'Symbolic links unavailable: {error}')


def test_source_symlink_is_not_followed(project, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('private outside file')
    symlink_or_skip(Path(project['workspace'], 'linked.py'), outside)
    with pytest.raises(ValueError, match='leaves|symbolic links'):
        cp.save_checkpoint(project, 'linked')
    assert outside.read_text() == 'private outside file'


def test_manifest_blob_symlink_is_not_followed(project, tmp_path):
    checkpoint = cp.save_checkpoint(project, 'source')
    folder = Path(checkpoint['path'])
    manifest = json.loads((folder / 'manifest.json').read_text())
    blob = folder / next(iter(manifest.values()))['sha256']
    outside = tmp_path / 'outside.bin'
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    symlink_or_skip(blob, outside)
    with pytest.raises(ValueError, match='leaves|symbolic links'):
        cp.fork_checkpoint(project, checkpoint)
    assert not experiments(project)


def test_state_directory_symlink_cannot_create_outside_task_directories(project, tmp_path):
    outside = tmp_path / 'outside-state'
    outside.mkdir()
    symlink_or_skip(Path(project['workspace'], '.agentvisor'), outside, directory=True)
    with pytest.raises(ValueError, match='leaves|symbolic links'):
        cp.save_checkpoint(project, 'linked-state')
    assert list(outside.iterdir()) == []


def test_failed_fork_write_removes_only_the_new_attempt(project, monkeypatch):
    checkpoint = cp.save_checkpoint(project, 'source')
    original_copy = Path(cp.fork_checkpoint(project, checkpoint))
    (original_copy / 'user.txt').write_text('existing experiment')
    original_open = Path.open
    def fail(path, mode='r', *args, **kwargs):
        if mode == 'xb' and path.name == 'new.py':
            raise OSError('simulated copy interruption')
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(OSError, match='copy interruption'):
        cp.fork_checkpoint(project, checkpoint)
    assert experiments(project) == [original_copy]
    assert (original_copy / 'user.txt').read_text() == 'existing experiment'
    assert Path(project['workspace'], 'src', 'new.py').read_text() == 'untracked source\n'


@pytest.mark.parametrize('manifest', [[], {'name': 'a' * 64},
    {'name': {'sha256': 'a' * 64, 'mode': True}},
    {'name': {'sha256': '../outside', 'mode': 0o644}}])
def test_malformed_manifest_is_rejected_before_copying(project, manifest):
    checkpoint = cp.save_checkpoint(project, 'source')
    Path(checkpoint['path'], 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        cp.fork_checkpoint(project, checkpoint)
    assert not experiments(project)


def test_duplicate_json_keys_and_file_directory_collision_are_rejected(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    manifest_path = Path(checkpoint['path'], 'manifest.json')
    manifest = json.loads(manifest_path.read_text())
    entry = json.dumps(next(iter(manifest.values())))
    manifest_path.write_text('{"name":' + entry + ',"name":' + entry + '}')
    with pytest.raises(ValueError, match='Duplicate'):
        cp.fork_checkpoint(project, checkpoint)
    manifest_path.write_text('{"name":' + entry + ',"name/child":' + entry + '}')
    with pytest.raises(ValueError, match='paths conflict'):
        cp.fork_checkpoint(project, checkpoint)
    assert not experiments(project)


def test_prompt_does_not_claim_a_separate_git_worktree(project):
    checkpoint = cp.save_checkpoint(project, 'source')
    copy = cp.fork_checkpoint(project, checkpoint)
    prompt = cp.checkpoint_prompt(dict(project, checkpoint=checkpoint, experiment_workspace=copy))
    assert 'ancestor is still the original Git repository' in prompt
    assert 'Do not run Git mutations' in prompt
    assert 'Never copy the entire directory back' in prompt
