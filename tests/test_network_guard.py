"""The no-network guard itself: every other test in the suite already relies on it
being active, so this is the one place that proves it actually blocks something."""

from __future__ import annotations

import subprocess
import sys

import httpx
import pytest

import tests._network_guard as guard


def test_a_real_request_is_refused() -> None:
    # httpx wraps the guard's `OSError` in its own `ConnectError`; the message survives the wrap.
    with pytest.raises(httpx.ConnectError, match="network disabled in tests"):
        httpx.get("https://example.com", timeout=2)


def test_a_real_request_from_a_spawned_subprocess_is_also_refused() -> None:
    # The `PYTHONPATH` this needs is set for the whole session by `tests/conftest.py`'s
    # `_network_guard_session` fixture; a bare `subprocess.run` with no `env=` inherits it, same
    # as `likearr/web/jobs.py`'s `_child_env` does for a job's real child process.
    probe = "import httpx; httpx.get('https://example.com', timeout=2)"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "network disabled in tests" in result.stderr


@pytest.mark.parametrize("host", ["localhost", "testserver", "127.0.0.1", "127.5.6.7", "::1"])
def test_loopback_hosts_are_never_refused(host: str) -> None:
    assert guard._is_loopback(host)


@pytest.mark.parametrize("host", ["example.com", "8.8.8.8", "accounts.spotify.com"])
def test_non_loopback_hosts_are_refused(host: str) -> None:
    assert not guard._is_loopback(host)


def test_the_guard_is_enabled_by_default() -> None:
    assert guard._enabled is True


def test_disabled_toggles_the_flag_for_its_block_only() -> None:
    assert guard._enabled is True
    with guard.disabled():
        assert guard._enabled is False
    assert guard._enabled is True


@pytest.mark.integration
def test_the_integration_marker_disables_the_guard_for_the_test() -> None:
    # `tests/conftest.py`'s per-test fixture wraps `integration`-marked tests in `disabled()`
    # automatically, precisely so a real Lidarr call (`LIKEARR_TEST_LIDARR_URL`) is not refused.
    assert guard._enabled is False
