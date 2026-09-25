"""Identify the exact local source served by an existing instance."""
import hashlib
from pathlib import Path


def build_id():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for file in sorted(root.rglob('*')):
        if file.suffix in {'.py', '.js', '.css', '.html', '.svg', '.json'}:
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()[:16]
