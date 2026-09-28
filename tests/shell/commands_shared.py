"""Fakes and data the three command test files share: `test_commands`, `test_setup_commands` and
`test_prune_commands`."""

from __future__ import annotations

import json

import httpx

from likearr.adapters.spotify import SpotifyAuth
from likearr.models import PrimaryType
from likearr.shell.context import Context
from tests.shell.conftest import FakeLidarr, FakeSource
from tests.unit.fakes import FakeLookup, artist_intent, rg, snapshot

ALBUM = rg("rg-1", "First Album")
EP = rg("rg-2", "An EP", primary=PrimaryType.EP)
STRANGER = rg("rg-9", "Something Else", artist_mbid="artist-9", artist_name="A Stranger")


def followed_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    lookup = FakeLookup().add(ALBUM, EP)
    lookup.catalogues["artist-1"] = ["rg-1", "rg-2"]
    source = FakeSource(snapshot(artists=[artist_intent("Test Artist", spotify_id="sp-a1")]))
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    return source, lookup, lidarr


# --------------------------------------------------------------------------- doctor: the Spotify quota

QUOTA_BODY = {"error": {"status": 429, "message": "Too many requests", "reason": "QUOTA_EXCEEDED"}}


class FakeSpotify:
    """Spotify's token endpoint and Web API behind one `httpx.MockTransport`. Nothing leaves it.

    `answers` maps a path fragment to the response every GET on a matching path gets; anything
    else is an empty page. Every request is recorded, so a test can count what doctor spent.
    """

    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.answers = answers or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, response in self.answers.items():
            if fragment in request.url.path:
                return response
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(
                200, json={"access_token": "fake-access-new", "expires_in": 3600, "scope": "user-follow-read"}
            )
        if request.url.path.endswith("/me/following"):
            return httpx.Response(200, json={"artists": {"items": [], "total": 3}})
        return httpx.Response(200, json={"items": [], "total": 5})

    def api_calls(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.host == "api.spotify.com"]


# --------------------------------------------------------------------------- auth: the six-month clock


def token_file_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "access_token": "fake-access-old",
        "refresh_token": "fake-refresh-old",
        "expires_at": 9_999_999_999.0,
        "scope": "user-follow-read",
        "token_type": "Bearer",
        "user_id": "fake-user",  # recorded, so a refresh has no account to ask for
    }
    data.update(overrides)
    return data


def with_real_auth(ctx: Context, *, token: dict[str, object] | None) -> None:
    """Give `ctx` a real `SpotifyAuth` over the tmp token file (no request is ever made)."""
    if token is not None:
        ctx.config.spotify.token_file.write_text(json.dumps(token))
    ctx.auth = SpotifyAuth(ctx.config.spotify, httpx.Client())
