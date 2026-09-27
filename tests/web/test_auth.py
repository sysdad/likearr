"""The pure halves of the web UI's access control: the login limiter and the cross-origin rule."""

from __future__ import annotations

import pytest

from likearr.web.auth import (
    AllowedHostMiddleware,
    LoginLimiter,
    cross_origin_allowed,
    password_matches,
    refused_host_message,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- password


def test_the_password_must_match_exactly() -> None:
    assert password_matches("correct horse", "correct horse")
    assert not password_matches("correct hors", "correct horse")
    assert not password_matches("", "correct horse")
    assert password_matches("pässwörd", "pässwörd")


# ---------------------------------------------------------------- limiter


def test_five_failures_in_a_minute_pause_that_address_for_sixty_seconds() -> None:
    clock = Clock()
    limiter = LoginLimiter(now=clock)
    for _ in range(5):
        assert limiter.blocked_for("10.0.0.5") == 0
        limiter.failed("10.0.0.5")
        clock.t += 5

    assert limiter.blocked_for("10.0.0.5") > 0
    assert limiter.blocked_for("10.0.0.6") == 0

    # The pause runs from the fifth failure (t=1020), so it ends at t=1080.
    clock.t = 1079
    assert limiter.blocked_for("10.0.0.5") > 0
    clock.t = 1081
    assert limiter.blocked_for("10.0.0.5") == 0


def test_failures_spread_over_more_than_a_minute_do_not_pause() -> None:
    clock = Clock()
    limiter = LoginLimiter(now=clock)
    for _ in range(10):
        limiter.failed("10.0.0.5")
        clock.t += 15

    assert limiter.blocked_for("10.0.0.5") == 0


def test_a_success_clears_the_failures() -> None:
    clock = Clock()
    limiter = LoginLimiter(now=clock)
    for _ in range(4):
        limiter.failed("10.0.0.5")
    limiter.succeeded("10.0.0.5")
    limiter.failed("10.0.0.5")

    assert limiter.blocked_for("10.0.0.5") == 0


def test_the_limiter_forgets_idle_addresses() -> None:
    clock = Clock()
    limiter = LoginLimiter(now=clock)
    for i in range(100):
        limiter.failed(f"10.0.1.{i}")
    clock.t += 3600
    limiter.failed("10.0.0.5")

    assert len(limiter) == 1


# ---------------------------------------------------------------- cross-origin


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_never_blocked(method: str) -> None:
    assert cross_origin_allowed(method, {"sec-fetch-site": "cross-site", "origin": "https://evil.example", "host": "x"})


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        # Plain http, which is how the LAN reaches the app directly: no Sec-Fetch-Site at all, so the
        # Origin-vs-Host comparison is the check that actually runs.
        ({"origin": "http://192.168.1.20:8770", "host": "192.168.1.20:8770"}, True),
        ({"origin": "http://evil.example", "host": "192.168.1.20:8770"}, False),
        ({"origin": "http://192.168.1.20:9999", "host": "192.168.1.20:8770"}, False),
        ({"origin": "null", "host": "192.168.1.20:8770"}, False),
        ({"origin": "http://LIKEARR.example.org", "host": "likearr.example.org"}, True),
        # Neither header: not a browser, so not a cross-site request. The login still applies.
        ({"host": "192.168.1.20:8770"}, True),
        # https through the proxy: the browser's own verdict wins.
        ({"sec-fetch-site": "same-origin", "origin": "https://likearr.example.org", "host": "x"}, True),
        ({"sec-fetch-site": "none", "host": "likearr.example.org"}, True),
        ({"sec-fetch-site": "same-site", "origin": "https://other.example.org", "host": "likearr.example.org"}, False),
        (
            {"sec-fetch-site": "cross-site", "origin": "https://likearr.example.org", "host": "likearr.example.org"},
            False,
        ),
    ],
)
def test_unsafe_methods_need_a_same_origin_request(headers: dict[str, str], allowed: bool) -> None:
    assert cross_origin_allowed("POST", headers) is allowed


# ---------------------------------------------------------------- refused-host message (#169)


def test_the_message_names_the_host_and_the_setting_with_no_port() -> None:
    message = refused_host_message("192.168.1.50:8770")

    assert '"192.168.1.50"' in message
    assert "8770" not in message
    assert "LIKEARR_ALLOWED_HOSTS" in message
    assert "environment" in message
    assert "config.toml" not in message


def test_a_bare_host_with_no_port_is_named_the_same_way() -> None:
    message = refused_host_message("nas.local")

    assert '"nas.local"' in message


def test_a_missing_host_gets_the_generic_line() -> None:
    message = refused_host_message("")

    assert "LIKEARR_ALLOWED_HOSTS" in message
    assert '""' not in message


def test_a_host_that_is_only_a_port_gets_the_generic_line() -> None:
    # ":8770" is empty before its first ":" - nothing safe to name.
    message = refused_host_message(":8770")

    assert "LIKEARR_ALLOWED_HOSTS" in message
    assert "8770" not in message


def test_an_ipv6_literal_is_refused_without_being_offered_as_the_fix() -> None:
    message = refused_host_message("[fd00::1]:8770")

    assert "fd00" not in message
    assert "IPv6" in message
    assert "LIKEARR_ALLOWED_HOSTS" in message


def test_control_characters_are_stripped_before_the_host_is_echoed() -> None:
    message = refused_host_message("evil\x07\x00.example:8770")

    assert "\x07" not in message
    assert "\x00" not in message
    assert '"evil.example"' in message


def test_a_very_long_host_is_truncated_to_253_characters() -> None:
    message = refused_host_message("a" * 400)

    assert "a" * 254 not in message
    assert "a" * 253 in message


def test_the_message_never_orders_the_reader_to_add_the_host() -> None:
    # Conditional wording: a rebinding page might be the one reading this, not the person who
    # typed the address on purpose.
    message = refused_host_message("rebound.evil.example")

    assert "if you reached likearr at this address on purpose" in message.lower()


def test_only_the_first_host_header_counts_as_starlette_did() -> None:
    """Two Host headers: the check must judge the one the app will use (the first), or a second,
    allowed one would let a request through under a name the check never saw."""
    import anyio

    reached: list[bool] = []

    async def app(scope: object, receive: object, send: object) -> None:
        reached.append(True)

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    middleware = AllowedHostMiddleware(app, allowed_hosts=["likearr.lan"])  # type: ignore[arg-type]
    scope = {"type": "http", "headers": [(b"host", b"evil.example"), (b"host", b"likearr.lan")]}
    anyio.run(middleware, scope, receive, send)  # type: ignore[arg-type]

    assert reached == []
    assert sent[0]["status"] == 400


# ---------------------------------------------------------------- hosts with LIKEARR_ALLOWED_HOSTS unset (#3)


def _host_status(middleware_hosts: list[str], host: str, *, any_ipv4: bool) -> int:
    """The status `AllowedHostMiddleware` answers `host` with: 200 when it reached the app."""
    import anyio

    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        sent.append({"status": 200})

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            sent.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    middleware = AllowedHostMiddleware(app, allowed_hosts=middleware_hosts, any_ipv4=any_ipv4)  # type: ignore[arg-type]
    anyio.run(middleware, {"type": "http", "headers": [(b"host", host.encode())]}, receive, send)  # type: ignore[arg-type]
    return int(sent[0]["status"])  # type: ignore[arg-type]


LOOPBACK = ["localhost", "127.0.0.1"]


@pytest.mark.parametrize("host", ["192.168.1.20:8770", "10.0.0.5", "127.0.0.1:8770", "localhost:8770"])
def test_unset_accepts_loopback_and_any_ipv4_address(host: str) -> None:
    assert _host_status(LOOPBACK, host, any_ipv4=True) == 200


@pytest.mark.parametrize(
    "host",
    [
        "likearr.example.org",
        "rebound.evil.example:8770",
        "192.168.1.20.nip.example",
        "1.2.3",  # a name that only looks numeric: not four parts
        "01.2.3.4",  # a leading zero is not a dotted-quad address
        "0x7f.0.0.1",
        "999.1.1.1",
        "[fd00::1]:8770",
        "",
    ],
)
def test_unset_refuses_every_host_name_ipv6_and_anything_not_a_strict_ipv4_address(host: str) -> None:
    assert _host_status(LOOPBACK, host, any_ipv4=True) == 400


def test_set_accepts_a_listed_name() -> None:
    assert _host_status(["likearr.example.org", *LOOPBACK], "likearr.example.org:8770", any_ipv4=False) == 200


def test_set_refuses_an_ipv4_address_it_does_not_list() -> None:
    hosts = ["likearr.example.org", "192.168.1.20", *LOOPBACK]

    assert _host_status(hosts, "192.168.1.21:8770", any_ipv4=False) == 400
    assert _host_status(hosts, "192.168.1.20:8770", any_ipv4=False) == 200


def test_set_still_refuses_ipv6() -> None:
    assert _host_status(["likearr.example.org", *LOOPBACK], "[::1]:8770", any_ipv4=False) == 400
