"""Which Spotify account the token file belongs to (#40): recorded on connect, kept on refresh."""

from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest
import respx

from likearr.adapters.spotify import (
    AccountRefused,
    SpotifyAccount,
    SpotifyAuth,
    TokenSet,
    fetch_account,
    read_account,
)
from likearr.config import SpotifyConfig
from likearr.ports import SourceError

from .conftest import FakeClock

TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"


def _write(config: SpotifyConfig, **fields: object) -> None:
    data: dict[str, object] = {
        "access_token": "fake-access-old",
        "refresh_token": "fake-refresh-old",
        "expires_at": 1.0,
        "scope": "user-library-read",
    }
    data.update(fields)
    config.token_file.write_text(json.dumps(data))


def test_the_authorize_url_always_asks_spotify_to_show_its_page(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    url, _, _ = SpotifyAuth(spotify_config, client).build_authorize_url()

    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["show_dialog"] == ["true"]


def test_the_account_round_trips_through_the_token_file() -> None:
    tokens = TokenSet("a", "r", 1.0, user_id="alex-1", display_name="Alex")

    again = TokenSet.from_mapping(json.loads(tokens.to_json()))

    assert (again.user_id, again.display_name) == ("alex-1", "Alex")


def test_a_token_file_without_an_account_still_round_trips_unchanged() -> None:
    raw = {"access_token": "a", "refresh_token": "r", "expires_at": 1.0, "scope": "", "token_type": "Bearer"}

    assert json.loads(TokenSet.from_mapping(raw).to_json()) == raw


def test_read_account_reads_the_recorded_account(spotify_config: SpotifyConfig) -> None:
    _write(spotify_config, user_id="alex-1", display_name="Alex")

    assert read_account(spotify_config.token_file) == SpotifyAccount("alex-1", "Alex")


@pytest.mark.parametrize("fields", [{}, {"user_id": ""}, {"user_id": 7}], ids=["none", "empty", "not-text"])
def test_read_account_is_none_when_nothing_usable_is_recorded(
    spotify_config: SpotifyConfig, fields: dict[str, object]
) -> None:
    _write(spotify_config, **fields)

    assert read_account(spotify_config.token_file) is None


def test_read_account_is_none_without_a_token_file(spotify_config: SpotifyConfig) -> None:
    assert read_account(spotify_config.token_file) is None


def test_an_account_without_a_display_name_is_labelled_by_its_id() -> None:
    assert SpotifyAccount("alex-1", "").label == "alex-1"
    assert SpotifyAccount("alex-1", "Alex").label == "Alex"


@respx.mock
def test_fetch_account_asks_me_with_the_given_token(client: httpx.Client) -> None:
    route = respx.get(ME_URL).mock(return_value=httpx.Response(200, json={"id": "alex-1", "display_name": "Alex"}))

    account = fetch_account(client, "fake-access-new")

    assert account == SpotifyAccount("alex-1", "Alex")
    assert route.calls[0].request.headers["authorization"] == "Bearer fake-access-new"


@respx.mock
def test_fetch_account_says_a_403_is_an_account_the_app_cannot_serve(client: httpx.Client) -> None:
    respx.get(ME_URL).mock(return_value=httpx.Response(403, json={"error": {"status": 403}}))

    with pytest.raises(AccountRefused):
        fetch_account(client, "fake-access-new")


@respx.mock
def test_fetch_account_refuses_an_answer_with_no_id(client: httpx.Client) -> None:
    respx.get(ME_URL).mock(return_value=httpx.Response(200, json={"display_name": "Alex"}))

    with pytest.raises(SourceError):
        fetch_account(client, "fake-access-new")


@respx.mock
def test_requesting_code_tokens_writes_nothing(spotify_config: SpotifyConfig, client: httpx.Client) -> None:
    """The web callback checks the account before anything is saved."""
    _write(spotify_config)
    before = spotify_config.token_file.read_text()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )

    tokens = SpotifyAuth(spotify_config, client).request_code_tokens("code", "verifier")

    assert tokens.access_token == "fake-access-new"
    assert tokens.authorized_at is not None
    assert spotify_config.token_file.read_text() == before


def test_save_authorization_writes_the_tokens_with_their_account(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    tokens = TokenSet("fake-access-new", "fake-refresh-new", 1.0, user_id="alex-1", display_name="Alex")

    SpotifyAuth(spotify_config, client).save_authorization(tokens)

    assert json.loads(spotify_config.token_file.read_text())["user_id"] == "alex-1"


@respx.mock
def test_a_refresh_keeps_the_recorded_account_without_asking_again(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    _write(spotify_config, user_id="alex-1", display_name="Alex")
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    me = respx.get(ME_URL)

    SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep).access_token()

    on_disk = json.loads(spotify_config.token_file.read_text())
    assert (on_disk["user_id"], on_disk["display_name"]) == ("alex-1", "Alex")
    assert not me.called


@respx.mock
def test_a_refresh_records_the_account_when_none_is_recorded(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """A token file from before accounts were recorded gets one on its next refresh, silently."""
    _write(spotify_config)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    respx.get(ME_URL).mock(return_value=httpx.Response(200, json={"id": "alex-1", "display_name": "Alex"}))

    token = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep).access_token()

    assert token == "fake-access-new"
    assert read_account(spotify_config.token_file) == SpotifyAccount("alex-1", "Alex")


@respx.mock
def test_a_refresh_still_works_when_the_account_cannot_be_asked(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    _write(spotify_config)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    respx.get(ME_URL).mock(return_value=httpx.Response(403))

    token = SpotifyAuth(spotify_config, client, now=clock.time, sleep=clock.sleep).access_token()

    assert token == "fake-access-new"
    assert read_account(spotify_config.token_file) is None
    assert json.loads(spotify_config.token_file.read_text())["access_token"] == "fake-access-new"


def test_record_account_adds_the_account_to_the_stored_token(
    spotify_config: SpotifyConfig, client: httpx.Client
) -> None:
    """`likearr auth` asks /me after the token is written, then records who it was."""
    _write(spotify_config, expires_at=9_999_999_999.0)

    SpotifyAuth(spotify_config, client).record_account(SpotifyAccount("alex-1", "Alex"))

    on_disk = json.loads(spotify_config.token_file.read_text())
    assert on_disk["access_token"] == "fake-access-old"
    assert (on_disk["user_id"], on_disk["display_name"]) == ("alex-1", "Alex")
