"""Tests for the shared HTTP layer - above all, that nothing leaks."""

from __future__ import annotations

import httpx
import pytest
import respx

from likearr import __version__
from likearr.adapters.http import (
    DEFAULT_USER_AGENT,
    HttpError,
    RedirectRefused,
    build_client,
    default_retry_on,
    redact,
    redact_literals,
    request_with_retries,
    safe_url,
    sent_secrets,
)

SECRET = "sup3rs3cr3tvalue"
URL = "https://api.test/thing"


# ---------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "text",
    [
        f"X-Api-Key: {SECRET}",
        f"x-api-key={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"authorization: Basic {SECRET}",
        f'{{"access_token": "{SECRET}"}}',
        f'{{"refresh_token":"{SECRET}"}}',
        f"https://api.test/x?apikey={SECRET}&y=1",
        f"grant_type=authorization_code&code={SECRET}&redirect_uri=x",
        f"?code={SECRET}",
        f'{{"code": "{SECRET}"}}',
        f"client_secret={SECRET}",
        f"code_verifier={SECRET}",
        f"Bearer {SECRET}",
        f"api_key: {SECRET}",
    ],
)
def test_redact_removes_every_secret_shape(text: str) -> None:
    out = redact(text)
    assert SECRET not in out
    assert "REDACTED" in out


def test_redact_keeps_useful_context() -> None:
    out = redact(f'{{"error": "invalid_grant", "access_token": "{SECRET}"}}')
    assert "invalid_grant" in out
    assert SECRET not in out


def test_redact_leaves_an_ordinary_status_code_alone() -> None:
    assert redact("upstream returned status code: 404") == "upstream returned status code: 404"


def test_redact_replaces_literals_it_was_given() -> None:
    """Patterns cannot catch a key echoed back in prose; the literal layer must."""
    out = redact(f"upstream says: invalid key {SECRET}, try again", literals=[SECRET])
    assert SECRET not in out
    assert "invalid key REDACTED" in out


def test_redact_ignores_short_literals() -> None:
    assert redact("status ok", literals=["ok"]) == "status ok"


@pytest.mark.parametrize("ending", ["\n", "\r", "\r\n"])
def test_redact_literals_also_catches_the_repr_escaped_form(ending: str) -> None:
    """H11 refuses a header value ending in CR or LF, and its message quotes the value's
    repr, where the line ending is two characters (a backslash and a letter), not a real one."""
    literal = SECRET + ending
    escaped = repr(literal.encode())[2:-1]
    assert escaped != literal

    out = redact_literals(f"LocalProtocolError: Illegal header value b'{escaped}'", [literal])

    assert SECRET not in out
    assert out == "LocalProtocolError: Illegal header value b'REDACTED'"


def test_redact_literals_still_replaces_the_raw_form_of_an_escaped_literal() -> None:
    literal = SECRET + "\n"
    assert redact_literals(f"raw {literal} end", [literal]) == "raw REDACTED end"


def test_sent_secrets_collects_header_and_field_values() -> None:
    found = sent_secrets({"X-Api-Key": SECRET, "Accept": "application/json"}, {"code": "abc12345"})
    assert SECRET in found
    assert "abc12345" in found
    assert "application/json" not in found


def test_sent_secrets_strips_the_bearer_prefix() -> None:
    found = sent_secrets({"Authorization": f"Bearer {SECRET}"})
    assert SECRET in found


def test_safe_url_drops_the_query_string() -> None:
    assert safe_url(f"https://api.test/v1/me?apikey={SECRET}&limit=50") == "https://api.test/v1/me"


# ---------------------------------------------------------------------------- client


def test_build_client_sets_timeouts_and_user_agent() -> None:
    with build_client() as client:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 60.0
        assert client.headers["User-Agent"].startswith("likearr/")


def _hops(*answers: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[len(seen) - 1]

    return httpx.MockTransport(handler), seen


def test_a_default_client_still_follows_a_cross_origin_redirect() -> None:
    """MusicBrainz and Spotify keep httpx's default; only Lidarr's client refuses."""
    transport, seen = _hops(
        httpx.Response(302, headers={"Location": "https://mirror.test/ws/2"}), httpx.Response(200, json={})
    )
    with build_client(transport=transport) as client:
        assert request_with_retries(client, "GET", "https://musicbrainz.test/ws/2").status_code == 200
    assert [r.url.host for r in seen] == ["musicbrainz.test", "mirror.test"]


def test_httpx_runs_a_request_hook_on_every_redirect_hop() -> None:
    """What `pinned_origin` relies on: the request hook sees each hop's real URL before it is sent.
    If a later httpx stopped doing this, the Lidarr key could follow a redirect unchecked."""
    transport, seen = _hops(
        httpx.Response(301, headers={"Location": "/y"}),
        httpx.Response(302, headers={"Location": "//b.test/z"}),
        httpx.Response(200, json={}),
    )
    hooked: list[str] = []
    with httpx.Client(
        transport=transport, follow_redirects=True, event_hooks={"request": [lambda r: hooked.append(str(r.url))]}
    ) as client:
        client.get("http://a.test/x")
    assert hooked == [str(r.url) for r in seen] == ["http://a.test/x", "http://a.test/y", "http://b.test/z"]


def test_a_pinned_client_refuses_a_cross_origin_hop_later_in_the_chain() -> None:
    transport, seen = _hops(
        httpx.Response(301, headers={"Location": "https://a.test/x"}),
        httpx.Response(302, headers={"Location": "https://b.test/y?token=abc"}),
    )
    with (
        build_client(transport=transport, pinned_origin="http://a.test") as client,
        pytest.raises(RedirectRefused) as excinfo,
    ):
        request_with_retries(client, "GET", "http://a.test/x", headers={"X-Api-Key": SECRET})
    assert excinfo.value.origin == "https://b.test"
    assert "abc" not in str(excinfo.value) and "/y" not in str(excinfo.value) and SECRET not in str(excinfo.value)
    assert [str(r.url) for r in seen] == ["http://a.test/x", "https://a.test/x"]


def test_a_pinned_client_refuses_another_origin_before_sending_anything() -> None:
    transport, seen = _hops(httpx.Response(200, json={}))
    with (
        build_client(transport=transport, pinned_origin="http://a.test:8686/lidarr") as client,
        pytest.raises(RedirectRefused, match=r"refused: not http://a\.test:8686"),
    ):
        request_with_retries(client, "GET", "http://a.test:9999/x")
    assert seen == []


def test_a_redirect_to_a_malformed_idna_host_is_never_sent() -> None:
    """httpx fails on the host while building the redirect, before any hook or second request."""
    transport, seen = _hops(httpx.Response(302, headers={"Location": "http://xn--lidarr-.lan/x"}))
    with build_client(transport=transport, pinned_origin="http://a.test") as client, pytest.raises(UnicodeError):
        request_with_retries(client, "GET", "http://a.test/x")
    assert len(seen) == 1


def test_a_malformed_pinned_url_fails_only_when_a_request_is_made() -> None:
    """build_context builds the Lidarr client for every command; a bad [lidarr] url must still
    only fail the commands that call Lidarr."""
    transport, seen = _hops(httpx.Response(200, json={}))
    with build_client(transport=transport, pinned_origin="http://[::1") as client, pytest.raises(httpx.InvalidURL):
        request_with_retries(client, "GET", "http://a.test/x")
    assert seen == []


def test_a_pinned_client_passes_a_plain_response_and_a_malformed_location_through() -> None:
    transport, _ = _hops(httpx.Response(200, json={}), httpx.Response(302, headers={"Location": "http://[::1"}))
    with build_client(transport=transport, pinned_origin="http://a.test") as client:
        assert request_with_retries(client, "GET", "http://a.test/x").status_code == 200
        # httpx itself refuses a Location it cannot parse, before any second request.
        with pytest.raises(HttpError) as excinfo:
            request_with_retries(client, "GET", "http://a.test/x", max_attempts=1)
    assert not isinstance(excinfo.value, RedirectRefused)


def test_default_user_agent_names_the_real_version_and_repo() -> None:
    """The URL once pointed at a repository that is not this one."""
    assert f"likearr/{__version__} (+https://github.com/sysdad/likearr)" == DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.endswith("(+https://github.com/sysdad/likearr)")


# ---------------------------------------------------------------------------- retries


@respx.mock
def test_retries_5xx_then_succeeds(client: httpx.Client) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    slept: list[float] = []
    response = request_with_retries(client, "GET", URL, sleep=slept.append)
    assert response.json() == {"ok": True}
    assert route.call_count == 3
    assert slept == [1.0, 2.0]


@respx.mock
def test_honours_retry_after(client: httpx.Client) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    slept: list[float] = []
    request_with_retries(client, "GET", URL, sleep=slept.append)
    assert slept == [7.0]


@respx.mock
def test_retry_after_is_capped(client: httpx.Client) -> None:
    """A `Retry-After` longer than `max_backoff` ends the request instead of retrying early."""
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "9999"}), httpx.Response(200, json={})]
    )
    slept: list[float] = []
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(client, "GET", URL, sleep=slept.append, max_backoff=30.0)
    assert route.call_count == 1
    assert slept == []
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 9999.0


@respx.mock
def test_retry_after_under_the_cap_still_retries(client: httpx.Client) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200, json={})])
    slept: list[float] = []
    response = request_with_retries(client, "GET", URL, sleep=slept.append, max_backoff=60.0)
    assert response.status_code == 200
    assert slept == [30.0]


@respx.mock
def test_4xx_is_not_retried_and_carries_status_and_excerpt(client: httpx.Client) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404, json={"error": "no such thing"}))
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(client, "GET", URL, sleep=lambda _s: None)
    assert route.call_count == 1
    assert excinfo.value.status_code == 404
    assert "no such thing" in excinfo.value.body_excerpt
    assert "HTTP 404" in str(excinfo.value)
    assert excinfo.value.retry_after is None, "no Retry-After header, nothing invented"


@respx.mock
def test_the_final_error_carries_retry_after(client: httpx.Client) -> None:
    """So a caller that will not retry (a spent quota) can still say when to try again."""
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(client, "GET", URL, retry_on=lambda _r: False, sleep=lambda _s: None)
    assert excinfo.value.retry_after == 3600.0


@respx.mock
@pytest.mark.parametrize("header", ["inf", "1e400", "nan", "Mon, 01 Jan 2026 00:00:00 +99999999999999999999999"])
def test_a_non_finite_retry_after_is_ignored(client: httpx.Client, header: str) -> None:
    """`float()` takes the numbers, and an infinite wait would overflow whatever formats it; the
    date's offset overflows `parsedate_to_datetime` itself. Neither may turn into an OverflowError."""
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": header}))
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(client, "GET", URL, retry_on=lambda _r: False, sleep=lambda _s: None)
    assert excinfo.value.retry_after is None


@respx.mock
def test_error_never_contains_the_api_key(client: httpx.Client) -> None:
    respx.get(URL).mock(return_value=httpx.Response(401, text=f'{{"message":"bad key {SECRET}"}}'))
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(
            client, "GET", URL, params={"apikey": SECRET}, headers={"X-Api-Key": SECRET}, sleep=lambda _s: None
        )
    assert SECRET not in str(excinfo.value)
    assert SECRET not in excinfo.value.body_excerpt
    assert SECRET not in excinfo.value.url


@respx.mock
def test_allow_status_returns_the_response(client: httpx.Client) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404, json={}))
    response = request_with_retries(client, "GET", URL, allow_status=(404,), sleep=lambda _s: None)
    assert response.status_code == 404


@respx.mock
def test_transport_errors_are_retried_then_raise(client: httpx.Client) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    slept: list[float] = []
    with pytest.raises(HttpError) as excinfo:
        request_with_retries(client, "GET", URL, max_attempts=3, sleep=slept.append)
    assert route.call_count == 3
    assert slept == [1.0, 2.0]
    assert excinfo.value.status_code is None
    assert excinfo.value.is_server_side


@respx.mock
def test_retry_on_can_veto(client: httpx.Client) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429, json={"reason": "QUOTA_EXCEEDED"}))
    with pytest.raises(HttpError):
        request_with_retries(client, "GET", URL, retry_on=lambda _r: False, sleep=lambda _s: None)
    assert route.call_count == 1


def test_default_retry_on() -> None:
    assert default_retry_on(httpx.Response(429))
    assert default_retry_on(httpx.Response(503))
    assert not default_retry_on(httpx.Response(404))


def test_retry_after_zero_is_a_floor_not_a_ceiling() -> None:
    """MusicBrainz answers 503 with `Retry-After: 0`; likearr must still back off exponentially."""
    import httpx
    import respx

    from likearr.adapters.http import request_with_retries

    waits: list[float] = []
    with respx.mock(base_url="https://mb.example") as mock:
        mock.get("/ws").mock(
            side_effect=[
                httpx.Response(503, headers={"Retry-After": "0"}),
                httpx.Response(503, headers={"Retry-After": "0"}),
                httpx.Response(200, json={}),
            ]
        )
        with httpx.Client(base_url="https://mb.example") as client:
            request_with_retries(client, "GET", "/ws", sleep=waits.append, base_backoff=1.0)
    assert waits == [1.0, 2.0]


def test_before_attempt_runs_before_every_attempt_including_retries() -> None:
    import httpx
    import respx

    from likearr.adapters.http import request_with_retries

    calls: list[str] = []
    with respx.mock(base_url="https://mb.example") as mock:
        mock.get("/ws").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
        with httpx.Client(base_url="https://mb.example") as client:
            request_with_retries(
                client, "GET", "/ws", sleep=lambda _s: None, before_attempt=lambda: calls.append("acquire")
            )
    assert calls == ["acquire", "acquire"]
