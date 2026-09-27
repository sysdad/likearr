"""No credential reaches stderr, even at debug level.

The web UI renders a job's stderr (`log.txt`) in the browser, so this is the "debug-level run plus a
grep" the design asks for before the log tail ships, made repeatable: every path that handles a
token or a key is driven at DEBUG with obviously fake credentials - including upstream errors that
echo them back - and the captured stderr is searched for each one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from likearr.adapters.lidarr import LidarrClient
from likearr.adapters.spotify import API_BASE, SpotifyAuth, authorized_request
from likearr.config import LidarrConfig, SpotifyConfig
from likearr.logging_setup import setup_logging
from likearr.ports import LidarrError, SourceError

from .conftest import FAKE_API_KEY, LIDARR_URL, FakeClock

TOKEN_URL = "https://accounts.spotify.com/api/token"
OLD_ACCESS = "fake-access-OLD-0123456789abcdef"
OLD_REFRESH = "fake-refresh-OLD-0123456789abcdef"
NEW_ACCESS = "fake-access-NEW-0123456789abcdef"
NEW_REFRESH = "fake-refresh-NEW-0123456789abcdef"
_MARKER = "capture check"
SECRETS = (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, FAKE_API_KEY)


@pytest.fixture
def debug_logging(capsys: pytest.CaptureFixture[str], preserve_root_logging: None) -> Iterator[None]:
    """DEBUG logging onto the `capsys` stderr - the handler binds `sys.stderr` when it is made, so
    it must be made after `capsys` has swapped it in, or nothing would be captured at all."""
    setup_logging(verbose=True)
    logging.getLogger("likearr.test").debug(_MARKER)
    yield


def _assert_clean(text: str) -> None:
    assert _MARKER in text, "stderr was not captured, so this test would prove nothing"
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"credentials reached stderr: {leaked}"


@respx.mock
def test_a_spotify_refresh_and_retry_logs_no_token(
    spotify_config: SpotifyConfig,
    client: httpx.Client,
    clock: FakeClock,
    debug_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spotify_config.token_file.write_text(
        json.dumps({"access_token": OLD_ACCESS, "refresh_token": OLD_REFRESH, "expires_at": 0, "scope": ""})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 3600}
        )
    )
    me = respx.get(f"{API_BASE}/me").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"message": f"bad token {NEW_ACCESS}"}}),
            httpx.Response(200, json={"id": "me"}),
        ]
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)

    authorized_request(client, auth, "GET", f"{API_BASE}/me", context="current user", sleep=clock.sleep)

    assert me.call_count == 2
    _assert_clean(capsys.readouterr().err)


@respx.mock
def test_a_refused_refresh_that_echoes_the_token_logs_no_token(
    spotify_config: SpotifyConfig,
    client: httpx.Client,
    clock: FakeClock,
    debug_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spotify_config.token_file.write_text(
        json.dumps({"access_token": OLD_ACCESS, "refresh_token": OLD_REFRESH, "expires_at": 0, "scope": ""})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": f"refresh_token={OLD_REFRESH} revoked"}
        )
    )
    auth = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep)

    with pytest.raises(SourceError) as excinfo:
        auth.access_token()
    logging.getLogger("likearr.shell.run").error("source read failed: %s", excinfo.value)

    _assert_clean(capsys.readouterr().err)


@respx.mock
def test_a_lidarr_error_that_echoes_the_key_logs_no_key(
    lidarr_config: LidarrConfig,
    client: httpx.Client,
    clock: FakeClock,
    debug_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{LIDARR_URL}/api/v1/system/status").mock(
        return_value=httpx.Response(401, text=f"Unauthorized: X-Api-Key {FAKE_API_KEY} apikey={FAKE_API_KEY}")
    )
    lidarr = LidarrClient(lidarr_config, client, api_key=FAKE_API_KEY, sleep=clock.sleep, monotonic=clock.monotonic)

    with pytest.raises(LidarrError) as excinfo:
        lidarr.check_version()
    logging.getLogger("likearr.shell.run").error("%s", excinfo.value)

    _assert_clean(capsys.readouterr().err)
