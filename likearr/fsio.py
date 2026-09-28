"""Atomic, durable file writes.

Every state and plan file likearr writes goes through `write_atomic`: a temp file beside the
target, fsynced, renamed over it, then the directory fsynced. A reader sees the old file or the
new one, never half of one, and after a power cut the new one is there with its data, not an empty
file the rename reached before the bytes did.

The temp file is created with the caller's mode, not `tempfile.mkstemp`'s fixed 0600, so a file
that was written with a plain `Path.write_text` keeps the mode the umask gave it, and a 0600 file
(the Spotify token) is never readable by anyone else, not even while its bytes land.
"""

from __future__ import annotations

import errno
import os
import secrets
from pathlib import Path

__all__ = ["write_atomic"]

_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_ATTEMPTS = 100


def write_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace `path` with `text` (UTF-8) atomically and durably.

    The temp file is `.{path.name}.<random>.tmp` beside `path`, the name
    `shell.last_run.remove_stale_temps` sweeps. With `mode`, the file has exactly that mode from
    before its first byte; without, it has what the umask gives a new file, as `open()` would. The
    directory must exist: the caller makes it when it should. On any failure, the temp file is
    removed, `path` is left as it was, and the exception propagates.
    """
    fd, tmp = _create_temp(path, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _create_temp(path: Path, mode: int) -> tuple[int, Path]:
    for _ in range(_ATTEMPTS):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            return os.open(tmp, _CREATE, mode), tmp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no free temporary file name", str(path))


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable. Best effort: some filesystems refuse a directory fsync."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
