"""Who Spotify is connected as, always going to Spotify's page, and guarding an account switch."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from likearr.adapters.state_sqlite import SqliteState
from likearr.models import RunStatus
from tests.web.app_support import _PUBLIC_URL, API_KEY_SENTINEL, CONFIG, NOW, _app, _login, _record

CLIENT_ID = "spotify-client-id-SENTINEL"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"
ALEX = {"id": "alex-1", "display_name": "Alex"}
SAM = {"id": "sam-2", "display_name": "Sam"}


def _token_file(data_dir: Path) -> Path:
    return data_dir / "spotify-token.json"


def _record_account(data_dir: Path, account: dict[str, str] | None) -> None:
    data = json.loads(_token_file(data_dir).read_text())
    if account is not None:
        data.update(user_id=account["id"], display_name=account["display_name"])
    _token_file(data_dir).write_text(json.dumps(data))


@contextmanager
def _spotify(me: httpx.Response) -> Iterator[respx.Route]:
    """Spotify's token endpoint grants a new token; `GET /me` answers `me`."""
    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-new",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                    "scope": "user-library-read",
                },
            )
        )
        yield respx.get(ME_URL).mock(return_value=me)


def _state(text: str) -> str:
    found = re.search(r"state=([^&\"]+)", html.unescape(text))
    assert found is not None
    return found[1]


def _paste_back(client: TestClient, me: httpx.Response) -> Any:
    """Connect in paste-back mode and paste back a redirect whose code Spotify accepts."""
    state = _state(client.post("/settings/spotify/connect", follow_redirects=False).text)
    url = f"http://127.0.0.1:8765/callback?code=some-code&state={state}"
    with _spotify(me):
        return client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)


@pytest.fixture
def web(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", CLIENT_ID)
    _login(client)
    return client


# ---------------------------------------------------------------- always show Spotify's page


@pytest.mark.parametrize("data", [{}, {"promote_save": "1"}], ids=["settings", "clean-up-write-access"])
def test_both_buttons_ask_spotify_to_show_its_page(web: TestClient, data: dict[str, str]) -> None:
    page = web.post("/settings/spotify/connect", data=data, follow_redirects=False).text
    url = html.unescape(re.search(r'href="(https://accounts\.spotify\.com/authorize[^"]+)"', page)[1])  # type: ignore[index]

    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["show_dialog"] == ["true"]


# ---------------------------------------------------------------- the callback's account check


def test_the_same_account_saves_the_token(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)

    response = _paste_back(web, httpx.Response(200, json=ALEX))

    assert response.status_code == 303
    on_disk = json.loads(_token_file(data_dir).read_text())
    assert (on_disk["access_token"], on_disk["user_id"]) == ("at-new", "alex-1")
    assert "Connected as Alex" in _visible(_spotify_box(web.get("/settings").text))


def test_no_recorded_account_records_it_without_asking(web: TestClient, data_dir: Path) -> None:
    """An install from before accounts were recorded: its first record is never a switch."""
    response = _paste_back(web, httpx.Response(200, json=SAM))

    assert response.status_code == 303
    assert json.loads(_token_file(data_dir).read_text())["user_id"] == "sam-2"


def test_another_account_saves_nothing_and_asks_first(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)
    before = _token_file(data_dir).read_text()

    response = _paste_back(web, httpx.Response(200, json=SAM))

    assert response.status_code == 200
    text = html.unescape(response.text)
    assert "This switches likearr from Alex to Sam" in text
    assert "the next run plans against Sam's library" in text
    assert 'action="/settings/spotify/switch"' in text
    assert "at-new" not in text and "rt-new" not in text
    assert _token_file(data_dir).read_text() == before


def _switch_form(text: str) -> dict[str, str]:
    key = re.search(r'name="switch" value="([^"]+)"', text)
    assert key is not None
    return {"switch": key[1]}


def test_confirming_the_switch_saves_the_new_token(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)
    form = _switch_form(_paste_back(web, httpx.Response(200, json=SAM)).text)

    response = web.post("/settings/spotify/switch", data={**form, "confirmed": "yes"}, follow_redirects=False)

    assert response.status_code == 303
    on_disk = json.loads(_token_file(data_dir).read_text())
    assert (on_disk["access_token"], on_disk["user_id"], on_disk["display_name"]) == ("at-new", "sam-2", "Sam")
    assert "Spotify connected as Sam" in web.get("/settings").text
    assert "Connected as Sam" in _visible(_spotify_box(web.get("/settings").text))
    # Single use: the same confirm a second time finds nothing to confirm.
    again = web.post("/settings/spotify/switch", data={**form, "confirmed": "yes"}, follow_redirects=False)
    assert "expired" in web.get(again.headers["location"]).text


def test_keeping_the_current_account_saves_nothing(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)
    before = _token_file(data_dir).read_text()
    form = _switch_form(_paste_back(web, httpx.Response(200, json=SAM)).text)

    response = web.post("/settings/spotify/switch", data={**form, "confirmed": "no"}, follow_redirects=False)

    assert response.status_code == 303
    assert _token_file(data_dir).read_text() == before
    assert "Still connected as Alex" in web.get("/settings").text


def test_a_switch_is_refused_when_the_connection_changed_meanwhile(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)
    form = _switch_form(_paste_back(web, httpx.Response(200, json=SAM)).text)
    _record_account(data_dir, {"id": "kim-3", "display_name": "Kim"})
    before = _token_file(data_dir).read_text()

    response = web.post("/settings/spotify/switch", data={**form, "confirmed": "yes"}, follow_redirects=False)

    assert _token_file(data_dir).read_text() == before
    assert "changed since" in web.get(response.headers["location"]).text


def test_confirming_a_switch_needs_the_login(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback needs no login; the confirm that saves another account's token does."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", CLIENT_ID)
    (data_dir / "config.toml").write_text(CONFIG.replace(*_PUBLIC_URL))
    _record_account(data_dir, ALEX)
    app = _app(data_dir, fake_cli)
    with TestClient(app) as logged_in:
        _login(logged_in)
        start = logged_in.post("/settings/spotify/connect", follow_redirects=False)
    with TestClient(app) as anon:
        with _spotify(httpx.Response(200, json=SAM)):
            page = anon.get("/spotify/callback", params={"code": "c", "state": _state(start.text)})
        assert "This switches likearr from Alex to Sam" in html.unescape(page.text)
        refused = anon.post("/settings/spotify/switch", data={**_switch_form(page.text), "confirmed": "yes"})
    assert refused.status_code == 401
    assert json.loads(_token_file(data_dir).read_text())["user_id"] == "alex-1"


def test_an_account_the_app_cannot_serve_keeps_the_old_token(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)
    before = _token_file(data_dir).read_text()

    response = _paste_back(web, httpx.Response(403, json={"error": {"status": 403}}))

    assert _token_file(data_dir).read_text() == before
    page = html.unescape(web.get(response.headers["location"]).text)
    assert "User Management" in page
    assert "The current connection is unchanged" in page


def test_a_failed_account_check_saves_nothing(web: TestClient, data_dir: Path) -> None:
    before = _token_file(data_dir).read_text()

    response = _paste_back(web, httpx.Response(500))

    assert _token_file(data_dir).read_text() == before
    assert "could not check which Spotify account" in html.unescape(web.get(response.headers["location"]).text)


# ---------------------------------------------------------------- the Settings box


def _spotify_box(page: str) -> str:
    start = page.index('id="spotify"')
    return page[start : page.index("</fieldset>", start)]


def _visible(fragment: str) -> str:
    text = re.sub(r"<svg.*?</svg>|<[^>]+>", " ", fragment, flags=re.S)
    return " ".join(html.unescape(text).split())


def test_settings_says_who_is_connected(web: TestClient, data_dir: Path) -> None:
    _record_account(data_dir, ALEX)

    box = _spotify_box(web.get("/settings").text)

    assert "Connected as <strong>Alex</strong>" in box


def test_with_nothing_missing_settings_says_re_authorizing_is_only_to_switch(web: TestClient) -> None:
    box = _visible(_spotify_box(web.get("/settings").text))

    assert "Re-authorize only to switch Spotify accounts." in box


def test_a_missing_scope_is_the_reason_to_re_authorize(web: TestClient, data_dir: Path) -> None:
    data = json.loads(_token_file(data_dir).read_text())
    _token_file(data_dir).write_text(json.dumps({**data, "scope": "user-library-read"}))

    box = _visible(_spotify_box(web.get("/settings").text))

    assert "playlists you collaborate on" in box
    assert "only to switch" not in box


def test_a_revoked_token_is_the_reason_to_re_authorize(web: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(
                ts=int(NOW.timestamp()) - 60,
                status=RunStatus.ERROR,
                exit_code=1,
                spotify_ok=False,
                message='spotify auth: HTTP 400 - body: {"error":"invalid_grant"}',
            ),
            None,
        )

    box = _visible(_spotify_box(web.get("/settings").text))

    assert "Spotify refused the saved authorization on the last run" in box
    assert "only to switch" not in box


BOX_BUDGET = 320
"""Characters of visible text the Spotify box may hold around its button."""


@pytest.mark.parametrize("public_url", [False, True], ids=["paste-back", "callback"])
def test_the_spotify_box_stays_short(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, public_url: bool
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", CLIENT_ID)
    if public_url:
        (data_dir / "config.toml").write_text(CONFIG.replace(*_PUBLIC_URL))
    _record_account(data_dir, ALEX)
    with TestClient(_app(data_dir, fake_cli)) as client:
        _login(client)
        box = _visible(_spotify_box(client.get("/settings").text))

    assert len(box) <= BOX_BUDGET, box


def test_the_clean_up_intro_is_two_sentences_at_most(web: TestClient) -> None:
    page = web.get("/prune").text
    intro = _visible(re.search(r'<section class="card">\s*<p>(.*?)</p>', page, flags=re.S)[1])  # type: ignore[index]

    assert len(re.findall(r"[.!?](\s|$)", intro)) <= 2, intro
    assert len(intro) <= 300, intro
