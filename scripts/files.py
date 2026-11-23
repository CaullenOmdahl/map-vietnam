"""Small file helpers shared by the map library (library.py) and the record sources (records.py).

A lock that every process on this computer honours, JSON files that are never seen half-written,
and deleting or sizing a file or folder. Standard library only, so start.py can import records.py
(which imports this) before .venv exists. Import it; there is nothing to run.
"""

import contextlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

try:
    import fcntl  # macOS and Linux
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt


def path_bytes(path):
    """Size on disk of a file or a folder's files; 0 if it does not exist. A file deleted while
    this counts (the viewer lists sizes every second while a map or source is being removed)
    counts as nothing, never as an error."""
    path = Path(path)
    total = 0
    try:
        if path.is_dir():
            for p in path.rglob("*"):
                with contextlib.suppress(OSError):
                    if p.is_file():
                        total += p.stat().st_size
        else:
            total = path.stat().st_size
    except OSError:
        pass
    return total


class StillInUse(PermissionError):
    """Something could not be deleted because another program holds it open (on Windows, a
    file in use cannot be removed). str() is plain English, ready to show the user."""

    def __init__(self, message=None):
        super().__init__(message or "Some files are still in use; close other programs and try Remove again")


def delete(path, attempts=10):
    """Remove a file or folder if it is there. Windows refuses to delete a file that is open, and
    the server opens a map's .mbtiles for each tile it serves, so a refused delete is retried
    for a couple of seconds before giving up. A folder is emptied as far as possible each time
    round; if anything is still there after the last try, StillInUse is raised, so a removal
    never looks done while gigabytes remain."""
    path = Path(path)
    refused = []
    for attempt in range(attempts):
        try:
            if path.is_dir() and not path.is_symlink():
                refused.clear()
                shutil.rmtree(path, onexc=lambda func, p, exc: refused.append(exc))
                if path.exists():
                    raise StillInUse from (refused[0] if refused else None)
            else:
                path.unlink(missing_ok=True)
            return
        except PermissionError as e:  # includes StillInUse from the folder branch
            if attempt == attempts - 1:
                if isinstance(e, StillInUse):
                    raise
                raise StillInUse() from e
            time.sleep(0.2)


def write_json(path, data):
    """Write JSON so a reader in another process sees the old file or the new one, never half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False
    ) as f:
        try:
            json.dump(data, f)
        except BaseException:  # not JSON, or the disk is full: leave no half file behind
            f.close()
            Path(f.name).unlink(missing_ok=True)
            raise
    os.replace(f.name, path)


def read_json(path, default=None):
    """The parsed JSON file at `path`, or `default` when it is missing or not JSON."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def lock_file(f, blocking=True):
    """Take an exclusive lock on the open file `f`, shared by every process and thread on this
    computer. Returns False instead of waiting when blocking is False and someone else has it."""
    if fcntl:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True
    f.seek(0)  # pragma: no cover - Windows: lock the first byte, retrying while it is taken
    while True:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(0.2)


def unlock_file(f):
    """Release lock_file's lock."""
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
