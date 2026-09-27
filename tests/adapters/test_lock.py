from __future__ import annotations

from pathlib import Path

import pytest

from likearr.adapters.lock import LockHeld, run_lock


def test_run_lock_allows_single_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "likearr.lock"

    with run_lock(lock_path):
        pass  # acquired and released cleanly


def test_run_lock_creates_parent_dirs(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "dir" / "likearr.lock"

    with run_lock(lock_path):
        pass

    assert lock_path.exists()


def test_run_lock_raises_when_already_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "likearr.lock"

    # A second, independent open of the same path (simulating a second process) must fail fast
    # rather than block.
    with run_lock(lock_path), pytest.raises(LockHeld), run_lock(lock_path):
        pass  # pragma: no cover - must not be reached


def test_run_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "likearr.lock"

    with run_lock(lock_path):
        pass

    with run_lock(lock_path):
        pass  # the first holder released it, so this must succeed


def test_run_lock_releases_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "likearr.lock"

    class Boom(Exception):
        pass

    with pytest.raises(Boom), run_lock(lock_path):
        raise Boom("boom")

    # Lock must have been released despite the exception.
    with run_lock(lock_path):
        pass
