"""A simple run lock: one likearr run at a time per lock file.

Uses `fcntl.flock`, which is per-open-file-description, not per-process - two separate opens of
the same path from the same process contend exactly like two processes would, which is what makes
this module easy to unit test.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockHeld(Exception):
    """Raised when another run already holds the lock file."""


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on `path` for the duration of the `with` block.

    Raises `LockHeld` immediately if another run already holds it, rather than blocking.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise LockHeld(f"another run already holds the lock: {path}") from e
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
