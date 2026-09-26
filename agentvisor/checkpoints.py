"""Bounded source snapshots and isolated experiment copies; never overwrite a project."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

MAX_FILES = 1500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BYTES = 24 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
SNAPSHOT = re.compile(r'[0-9a-f]{20}')
BLOB = re.compile(r'[0-9a-f]{64}')
PENDING = re.compile(r'\.pending-[0-9a-f]{32}')
EXPERIMENT = re.compile(r'experiment-[0-9a-f]{12}')


def _linked(path):
    try:
        # Windows directory junctions are reparse points, not always symlinks.
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def _inside(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Checkpoint path leaves its directory')
    current = path
    while current != root:
        if _linked(current):
            raise ValueError('Checkpoints do not follow symbolic links or junctions')
        current = current.parent


def _root(task):
    workspace = Path(task['workspace']).resolve()
    task_id = str(task['id'])
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
        raise ValueError('Invalid checkpoint task identity')
    root = workspace
    for part in ('.agentvisor', 'tasks', task_id, 'checkpoints'):
        root = root / part
        _inside(root, workspace)
        root.mkdir(exist_ok=True)
    return root


def _name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024 or
            any(char in name for char in ('\\', ':', '\0')) or
            any(part in {'', '.', '..'} or part.lower() in {'.git', '.agentvisor'}
                for part in name.split('/'))):
        raise ValueError('Invalid checkpoint file path')
    return name


def _read(path, root, limit):
    _inside(path, root)
    if not path.is_file():
        raise ValueError('Checkpoint entry is not a regular file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Source checkpoint size limit exceeded')
    return data


def _files(task):
    workspace = Path(task['workspace']).resolve()
    result = subprocess.run(['git', '-C', str(workspace), 'ls-files', '--cached', '--others',
                             '--exclude-standard', '-z'], capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise ValueError('Source checkpoints require an existing Git workspace')
    if len(result.stdout) > MAX_MANIFEST_BYTES:
        raise ValueError('Source checkpoint file list limit exceeded')
    names = sorted(set(result.stdout.decode('utf-8', errors='strict').split('\0')) - {''})
    names = [name for name in names if not {'.git', '.agentvisor'}.intersection(
        part.lower() for part in name.replace('\\', '/').split('/'))]
    if len(names) > MAX_FILES:
        raise ValueError('Source checkpoint file limit exceeded')
    manifest, blobs, total = {}, {}, 0
    for name in names:
        path = workspace / _name(name)
        _inside(path, workspace)
        if not path.exists():
            continue  # A tracked deletion is represented by absence in the copy.
        data = _read(path, workspace, MAX_FILE_BYTES)
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError('Source checkpoint size limit exceeded')
        digest = hashlib.sha256(data).hexdigest()
        manifest[name] = {'sha256': digest, 'mode': 0o755 if path.stat().st_mode & 0o111 else 0o644}
        blobs[digest] = data
    return manifest, blobs, total


def _encoded(manifest):
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _identity(manifest):
    return hashlib.sha256(_encoded(manifest)).hexdigest()[:20]


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate checkpoint manifest key')
        value[key] = item
    return value


def _load(source, root):
    _inside(source, root)
    manifest = json.loads(_read(source / 'manifest.json', root, MAX_MANIFEST_BYTES), object_pairs_hook=_pairs)
    if not isinstance(manifest, dict) or len(manifest) > MAX_FILES:
        raise ValueError('Invalid or oversized checkpoint manifest')
    names, blobs, total = set(), {}, 0
    for name, item in manifest.items():
        _name(name)
        normalized = os.path.normcase(name)
        if normalized in names:
            raise ValueError('Checkpoint paths collide on this filesystem')
        names.add(normalized)
        if (not isinstance(item, dict) or set(item) != {'sha256', 'mode'} or
                not isinstance(item['sha256'], str) or not BLOB.fullmatch(item['sha256']) or
                type(item['mode']) is not int or item['mode'] not in {0o644, 0o755}):
            raise ValueError('Invalid checkpoint entry')
        digest = item['sha256']
        if digest not in blobs:
            data = _read(source / digest, root, MAX_FILE_BYTES)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Snapshot checksum mismatch')
            blobs[digest] = data
        total += len(blobs[digest])
        if total > MAX_BYTES:
            raise ValueError('Source checkpoint size limit exceeded')
    for name in names:
        parts = name.replace('\\', '/').split('/')
        if any(os.path.normcase('/'.join(parts[:end])) in names for end in range(1, len(parts))):
            raise ValueError('Checkpoint file and directory paths conflict')
    if _identity(manifest) != source.name:
        raise ValueError('Snapshot manifest identity mismatch')
    return manifest, blobs


def _discard_flat(folder, root):
    """Remove only our flat cache layout; preserve unknown files and directories."""
    _inside(folder, root)
    children = list(folder.iterdir())
    if not all(not _linked(child) and child.is_file() and
               (child.name == 'manifest.json' or BLOB.fullmatch(child.name)) for child in children):
        return False
    for child in children:
        _inside(child, root)
        child.unlink()
    folder.rmdir()
    return True


def save_checkpoint(task, label):
    root, (manifest, blobs, total) = _root(task), _files(task)
    if len(_encoded(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError('Source checkpoint manifest size limit exceeded')
    identity = _identity(manifest)
    folder = root / identity
    _inside(folder, root)
    for path in root.iterdir():
        if PENDING.fullmatch(path.name) and path.is_dir() and not _linked(path):
            _discard_flat(path, root)
    if folder.exists() and not (folder / 'manifest.json').exists():
        if not folder.is_dir() or not _discard_flat(folder, root):
            raise ValueError('Incomplete checkpoint contains unknown entries')
    if folder.exists():
        _load(folder, root)
    else:
        temporary = root / ('.pending-' + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            for digest, data in blobs.items():
                (temporary / digest).write_bytes(data)
            (temporary / 'manifest.json').write_bytes(_encoded(manifest))
            temporary.rename(folder)
        except BaseException:
            if temporary.exists():
                _discard_flat(temporary, root)
            raise
    snapshots = sorted([path for path in root.iterdir() if SNAPSHOT.fullmatch(path.name)
                        and path.is_dir() and not _linked(path) and path != folder
                        and not _linked(path / 'manifest.json') and (path / 'manifest.json').is_file()],
                       key=lambda path: path.stat().st_mtime, reverse=True)
    for old in snapshots[2:]:
        _discard_flat(old, root)
    return {'id': identity, 'path': str(folder), 'label': str(label)[:500],
            'goal_version': task['goal_version'], 'files': len(manifest), 'bytes': total}


def fork_checkpoint(task, checkpoint):
    root = _root(task)
    identity = checkpoint.get('id', '')
    if not isinstance(identity, str) or not SNAPSHOT.fullmatch(identity):
        raise ValueError('Invalid checkpoint identity')
    experiments = [path for path in root.iterdir() if EXPERIMENT.fullmatch(path.name)]
    if len(experiments) >= 2:
        raise ValueError('Two isolated experiment copies already exist; preserve or remove one explicitly')
    manifest, blobs = _load(root / identity, root)
    # Validate the complete source first; never overwrite the project or Git index.
    destination = root / ('experiment-' + uuid.uuid4().hex[:12])
    destination.mkdir()
    created_files, created_dirs = [], [destination]
    try:
        for name, item in manifest.items():
            target = destination / name
            _inside(target, destination)
            parent = destination
            for component in name.split('/')[:-1]:
                parent = parent / component
                if not parent.exists():
                    parent.mkdir()
                    created_dirs.append(parent)
                _inside(parent, destination)
            created_files.append(target)
            with target.open('xb') as stream:
                stream.write(blobs[item['sha256']])
            if os.name != 'nt':
                target.chmod(item['mode'])
    except BaseException:
        for path in reversed(created_files):
            try:
                _inside(path, destination)
                if path.is_file():
                    path.unlink()
            except (OSError, ValueError):
                pass
        for path in reversed(created_dirs):
            try:
                _inside(path, root)
                path.rmdir()
            except (OSError, ValueError):
                pass
        raise
    return str(destination)


def checkpoint_prompt(task):
    checkpoint = task.get('checkpoint') or {}
    if checkpoint.get('goal_version') != task['goal_version']:
        return ''
    message = ('\nSOURCE CHECKPOINT (historical snapshot, not proof of current correctness): '
               + json.dumps(checkpoint, ensure_ascii=False) + '. ')
    if task.get('experiment_workspace'):
        message += ('An isolated source copy is available at ' + task['experiment_workspace'] +
                    '. Explore an alternative there with existing permitted tools. It excludes ignored dependencies '
                    'and Git metadata, but its ancestor is still the original Git repository. Do not run Git '
                    'mutations in this copy: they could change the original index or branch. '
                    'Validate the alternative, then integrate only the relevant verified changes '
                    'into the original workspace, preserving user changes. Never copy the entire directory back. ')
    return message
