"""Fixtures shared by the whole suite: no-network enforcement and a root-logging save/restore
helper. See issue #137.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._network_guard import disabled, install

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SITECUSTOMIZE_DIR = Path(__file__).resolve().parent / "_sitecustomize"
_GUARD_ENV = "LIKEARR_TEST_NETWORK_GUARD"


@pytest.fixture(scope="session", autouse=True)
def _network_guard_session() -> Iterator[None]:
    """Installs the no-network guard for this process, and arranges for any subprocess a test
    spawns to install it too, via `PYTHONPATH` (`tests/_sitecustomize/sitecustomize.py`).

    `_child_env()` (`likearr/web/jobs.py`) copies `os.environ` wholesale, so a job's fake-CLI
    child inherits this `PYTHONPATH` and the flag like any other environment variable.
    """
    install()
    previous_path = os.environ.get("PYTHONPATH")
    parts = [str(_SITECUSTOMIZE_DIR), str(_REPO_ROOT)]
    if previous_path:
        parts.append(previous_path)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    os.environ[_GUARD_ENV] = "1"
    try:
        yield
    finally:
        if previous_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous_path
        os.environ.pop(_GUARD_ENV, None)


@pytest.fixture(autouse=True)
def _network_guard_per_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Lets an `integration`-marked test (a real Lidarr at `LIKEARR_TEST_LIDARR_URL`) through."""
    if request.node.get_closest_marker("integration") is not None:
        with disabled():
            yield
    else:
        yield


@pytest.fixture
def preserve_root_logging() -> Iterator[None]:
    """Saves the root logger's handlers and level, and restores them after the test.

    `setup_logging` (`likearr.logging_setup`) replaces the root handlers wholesale, binding a
    `StreamHandler` to whatever `sys.stderr` is at call time - capsys's own stream, in a test.
    That stream closes when the test ends; without this, the handler stays installed and every
    later log call - including one from a background thread that outlives the test - writes to a
    closed file, printing "--- Logging error ---" for the rest of the run (issue #137).
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
