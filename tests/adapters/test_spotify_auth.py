"""Tests for the Spotify PKCE flow and the token file."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import stat
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from likearr.adapters.spotify import (
    ALL_SCOPES,
    READ_SCOPES,
    REFRESH_TOKEN_LIFETIME_MONTHS,
    SpotifyAuth,
    TokenSet,
    _loopback_answer,
    asks_for_write_scopes,
    can_read_collaborative,
    read_authorized_at,
    read_granted_scopes,
    reauth_due,
)
from likearr.config import SpotifyConfig
from likearr.ports import SourceError

from .conftest import FakeClock

TOKEN_URL = "https://accounts.spotify.com/api/token"


def write_tokens(config: SpotifyConfig, **overrides: object) -> None:
    data = {
        "access_token": "fake-access-old",
        "refresh_token": "fake-refresh-old",
        "expires_at": 9_999_999_999.0,
        "scope": ALL_SCOPES,
        "token_type": "Bearer",
    }
    data.update(overrides)
    config.token_file.write_text(json.dumps(data))


# ---------------------------------------------------------------------------- authorize URL


def test_build_authorize_url_shape(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    auth = SpotifyAuth(spotify_config, client)
    url, verifier, state = auth.build_authorize_url()

    parsed = urllib.parse.urlparse(url)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    assert parsed.netloc == "accounts.spotify.com"
    assert parsed.path == "/authorize"
    assert params["response_type"] == "code"
    assert params["client_id"] == "fake-client-id"
    assert params["code_challenge_method"] == "S256"
    assert params["state"] == state
    assert params["scope"] == ("user-follow-read user-library-read playlist-read-private playlist-read-collaborative")
    assert params["scope"] == READ_SCOPES
    assert params["redirect_uri"] == spotify_config.redirect_uri
    assert "client_secret" not in params

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert params["code_challenge"] == expected
    assert 43 <= len(verifier) <= 128


def test_the_write_scopes_are_asked_for_only_on_request(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    """#161: read-only by default; `include_write=True` (promote-save's opt-in) adds the two modify scopes."""
    auth = SpotifyAuth(spotify_config, client)

    read_only, _, _ = auth.build_authorize_url()
    with_write, _, _ = auth.build_authorize_url(include_write=True)

    def scope(url: str) -> list[str]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0].split()

    assert not any(name.endswith("-modify") for name in scope(read_only))
    assert scope(with_write) == [*scope(read_only), "user-follow-modify", "user-library-modify"]
    assert " ".join(scope(with_write)) == ALL_SCOPES


def test_a_token_with_the_write_scopes_keeps_them_on_re_authorization(spotify_config: SpotifyConfig) -> None:
    """#161 decision (a), keep what you have: a plain re-authorization asks for the write scopes
    again only when the stored token already has both."""
    write_tokens(spotify_config, scope=ALL_SCOPES)
    assert asks_for_write_scopes(spotify_config.token_file) is True

    write_tokens(spotify_config, scope=READ_SCOPES)
    assert asks_for_write_scopes(spotify_config.token_file) is False

    write_tokens(spotify_config, scope=f"{READ_SCOPES} user-library-modify")  # half of it is not "has them"
    assert asks_for_write_scopes(spotify_config.token_file) is False

    write_tokens(spotify_config, scope=READ_SCOPES)
    assert asks_for_write_scopes(spotify_config.token_file, promote_save=True) is True


def test_a_token_from_before_the_collaborative_scope_keeps_its_write_access_and_gains_it(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    """#103 item 3 with #161: a token granted read and write before likearr asked for
    playlist-read-collaborative still counts as having write access, so its next re-authorization
    asks for everything - the new read scope and the write scopes it already had."""
    old = "user-follow-read user-library-read playlist-read-private user-follow-modify user-library-modify"
    write_tokens(spotify_config, scope=old)
    include_write = asks_for_write_scopes(spotify_config.token_file)
    url, _, _ = SpotifyAuth(spotify_config, client).build_authorize_url(include_write=include_write)

    asked = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0].split()
    assert include_write is True
    assert set(old.split()) | {"playlist-read-collaborative"} == set(asked)
    assert " ".join(asked) == ALL_SCOPES


@pytest.mark.parametrize(
    ("granted", "expected"),
    [
        (None, False),
        (frozenset(), False),
        (frozenset({"user-follow-read", "user-library-read", "playlist-read-private"}), False),
        (frozenset(READ_SCOPES.split()), True),
    ],
)
def test_can_read_collaborative_needs_the_scope(granted: frozenset[str] | None, expected: bool) -> None:
    assert can_read_collaborative(granted) is expected


@pytest.mark.parametrize("content", [None, "not json", '{"access_token": "x"}'])
def test_no_readable_token_means_read_only_unless_promote_save_asks(
    spotify_config: SpotifyConfig, content: str | None
) -> None:
    """A first sign-in (no token file), or a token file with no scopes recorded, is a new user."""
    if content is not None:
        spotify_config.token_file.write_text(content)

    assert asks_for_write_scopes(spotify_config.token_file) is False
    assert asks_for_write_scopes(spotify_config.token_file, promote_save=True) is True


def test_authorize_url_verifiers_are_unique(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    auth = SpotifyAuth(spotify_config, client)
    _, v1, s1 = auth.build_authorize_url()
    _, v2, s2 = auth.build_authorize_url()
    assert v1 != v2
    assert s1 != s2


def test_localhost_redirect_is_rejected(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    config = replace(spotify_config, redirect_uri="http://localhost:8765/callback")
    with pytest.raises(SourceError, match="loopback IP literal"):
        SpotifyAuth(config, client).build_authorize_url()


def test_non_loopback_redirect_is_rejected(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    config = replace(spotify_config, redirect_uri="http://192.168.1.21:8765/callback")
    with pytest.raises(SourceError, match="not a loopback address"):
        SpotifyAuth(config, client).build_authorize_url()


def test_redirect_without_a_port_is_rejected(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    config = replace(spotify_config, redirect_uri="http://127.0.0.1/callback")
    with pytest.raises(SourceError, match="explicit port"):
        SpotifyAuth(config, client).build_authorize_url()


# ---------------------------------------------------------------------------- direct-callback mode (#79)


def test_an_explicit_redirect_uri_overrides_the_configured_loopback_one(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    """The web UI's direct-callback mode passes its own https redirect_uri; `[spotify]
    redirect_uri` (the loopback one, still used by the CLI and the web UI's paste-back mode) is
    then not even validated as a loopback address."""
    url, _verifier, _state = SpotifyAuth(spotify_config, client).build_authorize_url(
        redirect_uri="https://likearr.example.org/spotify/callback"
    )
    params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
    assert params["redirect_uri"] == "https://likearr.example.org/spotify/callback"


def test_a_non_https_explicit_redirect_uri_is_rejected(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    with pytest.raises(SourceError, match="https"):
        SpotifyAuth(spotify_config, client).build_authorize_url(redirect_uri="http://likearr.example.org/callback")


def test_an_explicit_redirect_uri_with_no_host_is_rejected(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    with pytest.raises(SourceError, match="no host"):
        SpotifyAuth(spotify_config, client).build_authorize_url(redirect_uri="https:///spotify/callback")


def test_an_explicit_redirect_uri_with_a_query_string_is_rejected(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    with pytest.raises(SourceError, match="query string"):
        SpotifyAuth(spotify_config, client).build_authorize_url(
            redirect_uri="https://likearr.example.org/spotify/callback?x=1"
        )


@respx.mock
def test_exchange_code_sends_the_same_explicit_redirect_uri(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Spotify requires the token exchange's `redirect_uri` to match the authorize step's exactly -
    the direct-callback mode's own value, not the configured loopback default."""
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fake-access-1", "refresh_token": "fake-refresh-1", "expires_in": 3600},
        )
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    auth.exchange_code(
        "fake-code-abc", "fake-verifier-abc", redirect_uri="https://likearr.example.org/spotify/callback"
    )

    body = urllib.parse.parse_qs(route.calls[0].request.content.decode())
    assert body["redirect_uri"] == ["https://likearr.example.org/spotify/callback"]


def test_exchange_code_holds_the_token_lock(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """The web UI's "Connect Spotify" exchange (issue #79) goes through the same locked write as
    `likearr auth` and a scheduled run's refresh: a concurrent holder makes it wait, then give up,
    never interleave a write."""
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    lock_path = spotify_config.token_file.with_name(f"{spotify_config.token_file.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SourceError, match="timed out"):
            auth.exchange_code("fake-code-abc", "fake-verifier-abc")
    assert clock.slept


# ---------------------------------------------------------------------------- parse_redirect_url


def test_parse_redirect_url() -> None:
    code, state = SpotifyAuth.parse_redirect_url(
        "  http://127.0.0.1:8765/callback?code=fake-code-abc&state=fake-state-xyz  "
    )
    assert (code, state) == ("fake-code-abc", "fake-state-xyz")


def test_parse_redirect_url_reports_an_error_param() -> None:
    with pytest.raises(SourceError, match="access_denied"):
        SpotifyAuth.parse_redirect_url("http://127.0.0.1:8765/callback?error=access_denied")


def test_parse_redirect_url_rejects_a_url_without_a_code() -> None:
    with pytest.raises(SourceError, match="no 'code' parameter"):
        SpotifyAuth.parse_redirect_url("http://127.0.0.1:8765/callback")


# ---------------------------------------------------------------------------- loopback callback (#171)


@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("code=fake-code&state=fake-state", (200, {"code": "fake-code", "state": "fake-state"})),
        ("error=access_denied&state=fake-state", (200, {"error": "access_denied"})),
        ("code=fake-code", (400, {})),  # no state: keep waiting
        ("code=fake-code&state=", (400, {})),
        ("code=fake-code&state=other-state", (400, {})),
        ("error=access_denied", (400, {})),  # a stranger cannot cancel the sign-in either
        ("error=access_denied&state=other-state", (400, {})),
        ("", (404, {})),
        ("favicon=1", (404, {})),
    ],
)
def test_the_loopback_server_takes_only_a_callback_with_the_issued_state(
    query: str, answer: tuple[int, dict[str, str]]
) -> None:
    """The loopback server stops at the first callback it accepts, so one carrying no state or
    another state is answered 400 and the wait goes on for the real one."""
    assert _loopback_answer(query, expected_state="fake-state") == answer


# ---------------------------------------------------------------------------- token file


@respx.mock
def test_exchange_code_writes_the_token_file_at_0600(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fake-access-1",
                "refresh_token": "fake-refresh-1",
                "expires_in": 3600,
                "scope": ALL_SCOPES,
                "token_type": "Bearer",
            },
        )
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    tokens = auth.exchange_code("fake-code-abc", "fake-verifier-abc")

    assert tokens.access_token == "fake-access-1"
    assert tokens.expires_at == clock.time() + 3600
    on_disk = json.loads(spotify_config.token_file.read_text())
    assert on_disk["refresh_token"] == "fake-refresh-1"
    mode = stat.S_IMODE(spotify_config.token_file.stat().st_mode)
    assert mode == 0o600

    body = urllib.parse.parse_qs(route.calls[0].request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code_verifier"] == ["fake-verifier-abc"]
    assert "client_secret" not in body


@respx.mock
def test_refresh_persists_a_rotated_refresh_token(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config, expires_at=0.0)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fake-access-2",
                "refresh_token": "fake-refresh-ROTATED",
                "expires_in": 3600,
            },
        )
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    assert auth.access_token() == "fake-access-2"

    on_disk = json.loads(spotify_config.token_file.read_text())
    assert on_disk["refresh_token"] == "fake-refresh-ROTATED"


@respx.mock
def test_refresh_keeps_the_old_refresh_token_when_none_is_returned(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config, expires_at=0.0)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-3", "expires_in": 3600})
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    auth.access_token()
    assert json.loads(spotify_config.token_file.read_text())["refresh_token"] == "fake-refresh-old"


@respx.mock
def test_access_token_does_not_refresh_while_it_is_still_valid(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    assert auth.access_token() == "fake-access-old"
    assert route.call_count == 0


@respx.mock
def test_access_token_refreshes_inside_the_60s_skew(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config, expires_at=clock.time() + 30)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-4", "expires_in": 3600})
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    assert auth.access_token() == "fake-access-4"
    assert route.call_count == 1


def test_missing_token_file_is_a_source_error(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    """#118: names the browser path too, since a new user reads this before ever hearing of the
    CLI - Settings is what the README's quick start actually sends them to."""
    with pytest.raises(SourceError) as excinfo:
        SpotifyAuth(spotify_config, client).access_token()
    assert "Connect Spotify in Settings" in str(excinfo.value)
    assert "likearr auth" in str(excinfo.value)


def test_malformed_token_file_is_a_source_error(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    spotify_config.token_file.write_text("not json at all")
    with pytest.raises(SourceError, match="not valid JSON"):
        SpotifyAuth(spotify_config, client).access_token()


@respx.mock
def test_auth_failure_is_a_source_error_without_leaking_the_code(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    with pytest.raises(SourceError) as excinfo:
        auth.exchange_code("fake-code-SECRETVALUE", "fake-verifier-SECRETVALUE")
    assert "invalid_grant" in str(excinfo.value)
    assert "SECRETVALUE" not in str(excinfo.value)


# ---------------------------------------------------------------------------- cross-process lock


@respx.mock
def test_a_waiting_process_picks_up_the_other_processes_refresh(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """The lost-update race this lock exists to close.

    Two `SpotifyAuth` instances (standing in for two processes, e.g. a `run` and an `explain`)
    share one token file and both load the original refresh token (R1). A's token expires first
    and it refreshes to R2. B still has R1 cached in memory; when B next needs a token, it must
    re-read the token file under the lock and see A's R2 rather than refresh with the
    already-rotated R1 - Spotify would answer that with `invalid_grant`. So B makes zero token
    requests of its own, and the file is left holding R2.
    """
    write_tokens(spotify_config, expires_at=clock.time() + 3600)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fake-access-2", "refresh_token": "fake-refresh-2", "expires_in": 3600},
        )
    )
    a = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    b = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)

    # Both load R1 into their own in-memory cache.
    assert a.access_token() == "fake-access-old"
    assert b.access_token() == "fake-access-old"
    assert route.call_count == 0

    # The token expires. A refreshes and persists R2.
    clock.advance(3600)
    assert a.access_token() == "fake-access-2"
    assert route.call_count == 1

    # B still thinks it holds R1. Its access_token() call must re-read the file under the lock,
    # see that R2 is already valid, and make no token request.
    assert b.access_token() == "fake-access-2"
    assert route.call_count == 1

    on_disk = json.loads(spotify_config.token_file.read_text())
    assert on_disk["refresh_token"] == "fake-refresh-2"


def test_token_lock_times_out_when_another_process_holds_it(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """`flock` conflicts across independent `open()`s of the same path even within one process
    (see `adapters/lock.py`), so holding the lock file open here stands in for another process.
    `access_token()` must wait - through the injected `sleep`, never a real one - and then give up
    with a `SourceError` rather than block forever.
    """
    write_tokens(spotify_config, expires_at=0.0)  # expired, so access_token() must take the lock
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    lock_path = spotify_config.token_file.with_name(f"{spotify_config.token_file.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = clock.time()

    with lock_path.open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SourceError, match="timed out"):
            auth.access_token()

    assert clock.slept, "access_token() must poll (and sleep) rather than fail immediately"
    assert clock.time() - start >= 25.0  # waited out something close to the real ~30s timeout


def test_token_lock_file_is_created_0600(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    write_tokens(spotify_config)  # not expired: access_token() still takes the lock to read it
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    auth.access_token()

    lock_path = spotify_config.token_file.with_name(f"{spotify_config.token_file.name}.lock")
    mode = stat.S_IMODE(lock_path.stat().st_mode)
    assert mode == 0o600


def test_token_set_expiry(tmp_path: Path) -> None:
    del tmp_path
    tokens = TokenSet(access_token="a", refresh_token="r", expires_at=1000.0)
    assert tokens.expired(now=941.0)
    assert not tokens.expired(now=939.0)


# ---------------------------------------------------------------------------- the six-month clock
#
# Since 2026-07-20 a Spotify refresh token expires six months after the user authorized the app,
# and refreshing does not extend it. The token file records when that clock started.

AUTH_RESPONSE = {
    "access_token": "fake-access-1",
    "refresh_token": "fake-refresh-1",
    "expires_in": 3600,
    "scope": ALL_SCOPES,
    "token_type": "Bearer",
}


@respx.mock
def test_exchange_code_records_when_the_user_authorized(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=AUTH_RESPONSE))
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    tokens = auth.exchange_code("fake-code-abc", "fake-verifier-abc")

    assert tokens.authorized_at == clock.time()
    assert json.loads(spotify_config.token_file.read_text())["authorized_at"] == clock.time()


@respx.mock
def test_a_refresh_never_restarts_the_six_month_clock(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config, expires_at=0.0, authorized_at=500.0)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "fake-access-2", "refresh_token": "fake-refresh-2", "expires_in": 3600}
        )
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    auth.access_token()
    assert json.loads(spotify_config.token_file.read_text())["authorized_at"] == 500.0

    auth.refresh()  # the forced refresh `doctor` and a 401 take
    assert json.loads(spotify_config.token_file.read_text())["authorized_at"] == 500.0


@respx.mock
def test_a_refresh_of_a_token_with_no_recorded_date_leaves_it_unrecorded(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """A refresh is not an authorization: it must not invent a start date for the clock."""
    write_tokens(spotify_config, expires_at=0.0)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-2", "expires_in": 3600})
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)
    auth.access_token()
    assert "authorized_at" not in json.loads(spotify_config.token_file.read_text())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1_780_000_000.0, 1_780_000_000.0),
        (1_780_000_000, 1_780_000_000.0),
        (None, None),
        ("2026-04-01", None),
        ("not a number", None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        (-5.0, None),
        ({"nested": 1}, None),
    ],
)
def test_from_mapping_tolerates_a_bad_authorized_at(raw: object, expected: float | None) -> None:
    """A bad date is a missing date, never a token file that fails to load."""
    tokens = TokenSet.from_mapping({"access_token": "a", "refresh_token": "r", "authorized_at": raw})
    assert tokens.authorized_at == expected
    assert tokens.access_token == "a"


def test_from_mapping_without_authorized_at_is_none() -> None:
    assert TokenSet.from_mapping({"access_token": "a"}).authorized_at is None


def test_to_json_omits_an_unknown_authorized_at() -> None:
    assert "authorized_at" not in json.loads(TokenSet("a", "r", 1.0).to_json())
    assert json.loads(TokenSet("a", "r", 1.0, authorized_at=7.0).to_json())["authorized_at"] == 7.0


def test_read_authorized_at_returns_only_the_date(spotify_config: SpotifyConfig) -> None:
    write_tokens(spotify_config, authorized_at=datetime(2026, 4, 1, tzinfo=UTC).timestamp())
    got = read_authorized_at(spotify_config.token_file)
    assert got == datetime(2026, 4, 1, tzinfo=UTC)
    assert got is not None and got.tzinfo is not None


@pytest.mark.parametrize(
    "content",
    [
        None,  # no file at all
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"access_token": "fake-access-old"}),
        json.dumps({"access_token": "fake-access-old", "authorized_at": "yesterday"}),
        json.dumps({"access_token": "fake-access-old", "authorized_at": 1e300}),
    ],
)
def test_read_authorized_at_is_none_on_any_problem(spotify_config: SpotifyConfig, content: str | None) -> None:
    if content is not None:
        spotify_config.token_file.write_text(content)
    assert read_authorized_at(spotify_config.token_file) is None


def test_read_authorized_at_never_takes_the_token_lock(spotify_config: SpotifyConfig) -> None:
    """The web server reads the date while a run may be mid-refresh; it must never wait on the lock."""
    write_tokens(spotify_config, authorized_at=1_780_000_000.0)
    lock_path = spotify_config.token_file.with_name(f"{spotify_config.token_file.name}.lock")
    with lock_path.open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert read_authorized_at(spotify_config.token_file) is not None


@pytest.mark.parametrize(
    ("authorized", "due"),
    [
        (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 7, 20, 13, 45, tzinfo=UTC), datetime(2027, 1, 20, 13, 45, tzinfo=UTC)),
        (datetime(2026, 8, 31, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC)),
        (datetime(2027, 8, 31, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC)),  # leap year
        (datetime(2026, 3, 31, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)),
        (datetime(2026, 12, 15, tzinfo=UTC), datetime(2027, 6, 15, tzinfo=UTC)),
    ],
)
def test_reauth_due_is_six_calendar_months_later_with_the_day_clamped(authorized: datetime, due: datetime) -> None:
    assert REFRESH_TOKEN_LIFETIME_MONTHS == 6
    assert reauth_due(authorized) == due


# ---------------------------------------------------------------------------- stopping mid-refresh


@respx.mock
def test_a_stop_signal_during_a_refresh_waits_until_the_rotated_token_is_saved(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    # Spotify rotates the refresh token on this request. If the process died between the answer and
    # the save, the file would keep a refresh token Spotify has already used, and every later run
    # would fail until `likearr auth --manual`. The web UI's Cancel sends exactly this signal.
    import os
    import signal

    write_tokens(spotify_config, expires_at=0.0)
    seen_on_disk: list[str] = []

    def handler(signum: int, frame: object) -> None:
        seen_on_disk.append(json.loads(spotify_config.token_file.read_text())["refresh_token"])

    def rotate(request: httpx.Request) -> httpx.Response:
        os.kill(os.getpid(), signal.SIGTERM)  # lands while the refresh is in flight
        return httpx.Response(
            200, json={"access_token": "fake-access-new", "refresh_token": "fake-refresh-new", "expires_in": 3600}
        )

    respx.post(TOKEN_URL).mock(side_effect=rotate)
    previous = signal.signal(signal.SIGTERM, handler)
    try:
        SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep).access_token()
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert seen_on_disk == ["fake-refresh-new"]


def test_the_token_request_has_a_short_bounded_worst_case() -> None:
    # SIGTERM is held for the whole token request, so its worst case bounds how long a stopped job
    # may take to go; the job runner waits longer than this before it escalates to SIGKILL.
    from likearr.adapters import spotify

    per_attempt = sum(getattr(spotify._TOKEN_TIMEOUT, phase) for phase in ("connect", "read", "write", "pool"))
    assert spotify._TOKEN_ATTEMPTS * per_attempt + spotify._TOKEN_MAX_BACKOFF_S <= spotify.TOKEN_REQUEST_WORST_CASE_S
    assert spotify.TOKEN_REQUEST_WORST_CASE_S <= 90


@respx.mock
def test_the_token_request_uses_the_bounded_profile(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    write_tokens(spotify_config, expires_at=0.0)
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SourceError):
        SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep).access_token()

    from likearr.adapters import spotify

    assert route.call_count == spotify._TOKEN_ATTEMPTS
    assert sum(clock.slept) <= spotify._TOKEN_MAX_BACKOFF_S


def test_read_granted_scopes_returns_only_scope_names(spotify_config: SpotifyConfig) -> None:
    write_tokens(spotify_config, scope="user-follow-read user-library-modify <script> x" + "y" * 80)

    assert read_granted_scopes(spotify_config.token_file) == frozenset({"user-follow-read", "user-library-modify"})


@pytest.mark.parametrize("content", [None, "not json", "[1]", '{"scope": 7}'])
def test_read_granted_scopes_is_none_for_anything_unreadable(
    spotify_config: SpotifyConfig, content: str | None
) -> None:
    if content is not None:
        spotify_config.token_file.write_text(content)

    assert read_granted_scopes(spotify_config.token_file) is None
