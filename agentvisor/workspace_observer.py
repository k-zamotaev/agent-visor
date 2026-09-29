"""Bounded workspace change detection across one working session."""
import os
import time
from pathlib import Path


SKIP_DIRS = {'.agentvisor', '.git', '.venv', 'venv', 'node_modules', '__pycache__'}
SKIP_SUFFIXES = {'.log', '.tmp', '.pid', '.lock', '.pyc'}


def snapshot(root, *, max_files=20000, max_seconds=3, clock=time.monotonic):
    """Return file metadata only; never follow symlinks or read file content."""
    root = Path(root).resolve()
    deadline = clock() + max_seconds
    files = {}
    pending = [root]
    while pending:
        if clock() >= deadline:
            return {'files': files, 'complete': False}
        directory = pending.pop()
        try:
            with os.scandir(directory) as listing:
                entries = sorted(list(listing), key=lambda item: item.name.lower())
        except OSError:
            return {'files': files, 'complete': False}
        for entry in entries:
            if clock() >= deadline or len(files) >= max_files:
                return {'files': files, 'complete': False}
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in SKIP_DIRS:
                        pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if Path(entry.name).suffix.lower() in SKIP_SUFFIXES:
                    continue
                stat = entry.stat(follow_symlinks=False)
                relative = Path(entry.path).relative_to(root).as_posix()
                files[relative] = (stat.st_size, stat.st_mtime_ns)
            except (OSError, ValueError):
                continue
    return {'files': files, 'complete': True}


def changes(before, after, *, limit=16):
    """Return only observed metadata changes, newest first for a bounded handoff."""
    if not before['complete'] or not after['complete']:
        return []
    old, new = before['files'], after['files']
    paths = [path for path in old.keys() | new.keys() if old.get(path) != new.get(path)]
    paths.sort(key=lambda path: new.get(path, old.get(path, (0, 0)))[1], reverse=True)
    return paths[:limit]
