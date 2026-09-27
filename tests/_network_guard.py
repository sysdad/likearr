"""Refuses any non-loopback DNS lookup or socket connect, in this process and in any subprocess a
test spawns.

Nothing in the suite talks to the network today - every Spotify, MusicBrainz and Lidarr call goes
through a fake or a `respx` mock - but nothing enforced that (issue #137). A test that forgets a
mock should fail loudly with "network disabled in tests", not quietly reach Spotify (whose Dev
Mode quota was exhausted on 2026-09-23), MusicBrainz, or someone's real Lidarr.

Patched at the lowest level (`socket.getaddrinfo`, `socket.socket.connect`) so it catches `httpx`,
`requests`-alikes and anything else built on the stdlib socket API, without depending on any one
HTTP library's internals.

Shared by `tests/conftest.py` (installed directly in the pytest process) and
`tests/_sitecustomize/sitecustomize.py` (installed the same way in every subprocess a test spawns,
via `PYTHONPATH` - see that file). Stdlib only, so both sides can import it without the dev extra.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_ALLOWED_HOSTS = frozenset({"localhost", "testserver", "127.0.0.1", "::1"})

# Whether the guard currently refuses a non-loopback address. Toggled off for the duration of an
# `integration`-marked test (`disabled()`), which talks to a real Lidarr at
# `LIKEARR_TEST_LIDARR_URL` on purpose.
_enabled = True


class NetworkDisabledError(OSError):
    """A DNS lookup or a connect the guard refused: network is disabled in this test."""


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        # None (getaddrinfo's own "any"), or a raw sockaddr for an AF_UNIX/AF_NETLINK socket:
        # nothing this guard is meant to catch, so let the real call decide.
        return True
    return host in _ALLOWED_HOSTS or host.startswith("127.")


def install() -> None:
    """Patch `socket` module-wide. Idempotent: safe to call once per process, however many times
    it is invoked - the parent test process calls it once per session, a spawned child once at
    interpreter start."""
    if getattr(socket, "_likearr_network_guard", False):
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if _enabled and not _is_loopback(host):
            raise NetworkDisabledError(f"network disabled in tests: getaddrinfo({host!r}) refused")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if _enabled and not _is_loopback(host):
            raise NetworkDisabledError(f"network disabled in tests: connect({address!r}) refused")
        real_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket._likearr_network_guard = True  # type: ignore[attr-defined]


@contextmanager
def disabled() -> Iterator[None]:
    """Let real network calls through for the duration of the `with` block (the `integration`
    marker). `install()` must already have run; this only flips the check it consults."""
    global _enabled
    previous, _enabled = _enabled, False
    try:
        yield
    finally:
        _enabled = previous
