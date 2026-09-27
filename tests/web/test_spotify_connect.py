"""`web.spotify_connect.PendingSpotifyAuthStore`: single-use, expiring, server-side PKCE state."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from likearr.adapters.spotify import ALL_SCOPES, READ_SCOPES
from likearr.config import SpotifyConfig
from likearr.web.spotify_connect import PendingSpotifyAuthStore, build_authorize, one_click_form_action


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def test_a_started_attempt_can_be_consumed_once() -> None:
    clock = FakeClock()
    store = PendingSpotifyAuthStore(now=clock.time)
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    pending = store.consume("s1")

    assert pending is not None
    assert (pending.state, pending.verifier, pending.mode) == ("s1", "v1", "paste")


def test_consuming_twice_fails_the_second_time() -> None:
    store = PendingSpotifyAuthStore()
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    assert store.consume("s1") is not None
    assert store.consume("s1") is None


def test_an_unknown_state_is_refused() -> None:
    store = PendingSpotifyAuthStore()

    assert store.consume("never-started") is None


def test_an_expired_attempt_is_refused() -> None:
    clock = FakeClock()
    store = PendingSpotifyAuthStore(now=clock.time, ttl_s=600.0)
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    clock.now += 601.0

    assert store.consume("s1") is None


def test_an_attempt_just_under_the_ttl_still_works() -> None:
    clock = FakeClock()
    store = PendingSpotifyAuthStore(now=clock.time, ttl_s=600.0)
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    clock.now += 599.0

    assert store.consume("s1") is not None


def test_starting_again_with_the_same_state_replaces_the_pending_entry() -> None:
    store = PendingSpotifyAuthStore()
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")
    store.start(state="s1", verifier="v2", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    pending = store.consume("s1")

    assert pending is not None
    assert pending.verifier == "v2"


def test_len_reports_only_live_entries() -> None:
    clock = FakeClock()
    store = PendingSpotifyAuthStore(now=clock.time, ttl_s=600.0)
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")
    store.start(state="s2", verifier="v2", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")

    clock.now += 601.0

    assert len(store) == 0


def test_the_write_scope_choice_is_kept_server_side_with_the_attempt() -> None:
    """#161: whether this attempt asked for the write scopes lives in the pending entry, beside the
    verifier - never in anything the callback's query string could change."""
    store = PendingSpotifyAuthStore()
    store.start(state="s1", verifier="v1", redirect_uri="http://127.0.0.1:8765/callback", mode="paste")
    store.start(
        state="s2", verifier="v2", redirect_uri="http://127.0.0.1:8765/callback", mode="paste", include_write=True
    )

    plain, with_write = store.consume("s1"), store.consume("s2")

    assert plain is not None and plain.include_write is False
    assert with_write is not None and with_write.include_write is True


def test_build_authorize_asks_for_the_write_scopes_only_when_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    config = SpotifyConfig(token_file=tmp_path / "spotify-token.json")

    def scope(url: str) -> str:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0]

    plain, _, _ = build_authorize(config, redirect_uri=None)
    with_write, _, _ = build_authorize(config, redirect_uri=None, include_write=True)

    assert scope(plain) == READ_SCOPES
    assert scope(with_write) == ALL_SCOPES


# ---------------------------------------------------------------- one-click Connect (#11)

_PUBLIC = "https://likearr.example.org"
_ALLOWED = ("https://accounts.spotify.com", "https://likearr.example.org")


@pytest.mark.parametrize(
    "host", ["likearr.example.org", "LIKEARR.Example.org", "likearr.example.org:443"], ids=["exact", "case", "443"]
)
def test_one_click_applies_when_the_host_is_the_public_url(host: str) -> None:
    assert one_click_form_action(host, _PUBLIC) == _ALLOWED


@pytest.mark.parametrize(
    "host",
    ["192.168.1.20:8080", "likearr.example.org:8080", "likearr:8080", "other.example.org", ""],
    ids=["lan-ip", "other-port", "container-name", "other-host", "no-host"],
)
def test_one_click_does_not_apply_at_any_other_address(host: str) -> None:
    assert one_click_form_action(host, _PUBLIC) == ()


def test_one_click_does_not_apply_without_a_public_url() -> None:
    assert one_click_form_action("likearr.example.org", "") == ()


def test_a_public_url_port_is_part_of_its_origin() -> None:
    public = "https://likearr.example.org:8443"
    assert one_click_form_action("likearr.example.org:8443", public) == (
        "https://accounts.spotify.com",
        "https://likearr.example.org:8443",
    )
    assert one_click_form_action("likearr.example.org", public) == ()


@pytest.mark.parametrize("public", ["https://a;b.example.org", "https://a b.example.org", "https://a,b.example.org"])
def test_a_public_url_that_would_not_make_a_clean_csp_source_never_applies(public: str) -> None:
    """The origin goes into a response header: anything beyond a plain host name keeps the link."""
    host = urllib.parse.urlsplit(public).netloc
    assert one_click_form_action(host, public) == ()
