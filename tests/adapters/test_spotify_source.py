"""Tests for SpotifySource: pagination, the schema canary, and every all-or-nothing failure."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from likearr.adapters.spotify import ALL_SCOPES, SpotifyAuth, SpotifySource
from likearr.config import SpotifyConfig
from likearr.models import ReasonKind, SourceKind
from likearr.ports import SchemaError, SourceError, SourcePort
from tests.clock import FakeClock

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
FETCHED_AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

PLAYLIST_ID = "fakeplaylist0000000001"


def make_source(
    config: SpotifyConfig,
    client: httpx.Client,
    clock: FakeClock,
    **overrides: object,
) -> SpotifySource:
    """A source wired to a valid, unexpired fake token, with sources disabled unless asked for."""
    defaults: dict[str, object] = {"followed_artists": False, "saved_albums": False, "liked_tracks": False}
    defaults.update(overrides)
    config = replace(config, **defaults)  # type: ignore[arg-type]
    config.token_file.write_text(
        json.dumps(
            {
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "expires_at": 9_999_999_999.0,
                "scope": ALL_SCOPES,
                "token_type": "Bearer",
                "user_id": "fake-user",
            }
        )
    )
    auth = SpotifyAuth(config, client, now=clock.time, sleep=clock.sleep)
    return SpotifySource(config, auth, client, now=lambda: FETCHED_AT, sleep=clock.sleep)


def album_json(album_id: str, name: str, *, upc: str | None = "0000000000001", **extra: object) -> dict:
    payload: dict = {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "release_date": "2021-03-04",
        "release_date_precision": "day",
        "artists": [{"id": "fakeartist0000000001", "name": "Fake Band"}],
    }
    if upc is not None:
        payload["external_ids"] = {"upc": upc}
    payload.update(extra)
    return payload


def track_json(track_id: str, name: str, *, isrc: str | None = "XX0000000001", **extra: object) -> dict:
    payload: dict = {
        "id": track_id,
        "name": name,
        "type": "track",
        "artists": [{"id": "fakeartist0000000001", "name": "Fake Band"}],
        "album": album_json("fakealbum0000000001", "Fake Album"),
    }
    if isrc is not None:
        payload["external_ids"] = {"isrc": isrc}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------- followed artists


@respx.mock
def test_followed_artists_follow_the_cursor(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/following").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [{"id": "fakeartist0000000001", "name": "Fake Band"}],
                        "cursors": {"after": "fakecursor1"},
                        "next": f"{API}/me/following?type=artist&after=fakecursor1&limit=50",
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [{"id": "fakeartist0000000002", "name": "Second Fake Band"}],
                        "cursors": {"after": None},
                        "next": None,
                    }
                },
            ),
        ]
    )
    snapshot = make_source(spotify_config, client, clock, followed_artists=True).read()

    assert [a.spotify_id for a in snapshot.artists] == ["fakeartist0000000001", "fakeartist0000000002"]
    assert snapshot.artists[0].reason.kind is ReasonKind.FOLLOWED
    assert snapshot.artists[0].reason.source_id == "fakeartist0000000001"
    assert snapshot.counts == {str(SourceKind.FOLLOWED_ARTISTS): 2}
    assert snapshot.fetched_at == FETCHED_AT
    assert snapshot.schema_ok


@respx.mock
def test_followed_artists_canary_missing_cursors(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/following").mock(
        return_value=httpx.Response(200, json={"artists": {"items": [], "next": None}})
    )
    with pytest.raises(SchemaError, match="cursors"):
        make_source(spotify_config, client, clock, followed_artists=True).read()


@respx.mock
def test_followed_artists_canary_missing_artists_block(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/following").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(SchemaError, match="no 'artists' object"):
        make_source(spotify_config, client, clock, followed_artists=True).read()


# ---------------------------------------------------------------------------- saved albums


@respx.mock
def test_saved_albums_paginate_and_map(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    respx.get(f"{API}/me/albums").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [{"album": album_json("fakealbum0000000001", "First Fake")}],
                    "next": f"{API}/me/albums?offset=50&limit=50",
                },
            ),
            httpx.Response(
                200,
                json={"items": [{"album": album_json("fakealbum0000000002", "Second Fake")}], "next": None},
            ),
        ]
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()

    assert [a.album.spotify_id for a in snapshot.albums] == ["fakealbum0000000001", "fakealbum0000000002"]
    first = snapshot.albums[0]
    assert first.album.upc == "0000000000001"
    assert first.album.release_date == date(2021, 3, 4)
    assert first.album.artist_names == ("Fake Band",)
    assert first.reason.kind is ReasonKind.SAVED
    assert snapshot.counts[str(SourceKind.SAVED_ALBUMS)] == 2


@respx.mock
def test_saved_albums_canary_missing_next(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(SchemaError, match="'next' is missing"):
        make_source(spotify_config, client, clock, saved_albums=True).read()


@respx.mock
def test_saved_albums_canary_missing_album_id(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(200, json={"items": [{"album": {"name": "No Id"}}], "next": None})
    )
    with pytest.raises(SchemaError, match="no 'id'"):
        make_source(spotify_config, client, clock, saved_albums=True).read()


@respx.mock
def test_missing_external_ids_degrades_but_does_not_fail(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"album": album_json("fakealbum0000000001", "No UPC", upc=None)}], "next": None},
        )
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()

    assert not snapshot.schema_ok
    assert snapshot.schema_warnings == ("saved_albums: external_ids absent (Spotify Dev Mode field removal?)",)
    assert snapshot.albums[0].album.upc is None


@respx.mock
def test_one_album_with_external_ids_is_enough(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"album": album_json("fakealbum0000000001", "No UPC", upc=None)},
                    {"album": album_json("fakealbum0000000002", "Has UPC")},
                ],
                "next": None,
            },
        )
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()
    assert snapshot.schema_ok


@pytest.mark.parametrize(
    ("raw", "precision", "expected"),
    [
        ("2021", "year", date(2021, 1, 1)),
        ("2021-07", "month", date(2021, 7, 1)),
        ("2021-07-09", "day", date(2021, 7, 9)),
        ("0000", "year", None),
        ("", "day", None),
    ],
)
@respx.mock
def test_release_date_precision(
    spotify_config: SpotifyConfig,
    client: httpx.Client,
    clock: FakeClock,
    raw: str,
    precision: str,
    expected: date | None,
) -> None:
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "album": album_json(
                            "fakealbum0000000001",
                            "Dated",
                            release_date=raw,
                            release_date_precision=precision,
                        )
                    }
                ],
                "next": None,
            },
        )
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()
    assert snapshot.albums[0].album.release_date == expected


# ---------------------------------------------------------------------------- liked tracks


@respx.mock
def test_liked_tracks_skip_locals_and_episodes(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"added_at": "2026-01-02T03:04:05Z", "track": track_json("faketrack0000000001", "Real")},
                    {"added_at": "2026-01-03T00:00:00Z", "track": {"id": None, "name": "Local", "is_local": True}},
                    {
                        "added_at": "2026-01-04T00:00:00Z",
                        "track": {"id": "fakeepisode000000001", "name": "Ep", "type": "episode"},
                    },
                ],
                "next": None,
            },
        )
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()

    assert [t.spotify_id for t in snapshot.tracks] == ["faketrack0000000001"]
    track = snapshot.tracks[0]
    assert track.isrc == "XX0000000001"
    assert track.album.spotify_id == "fakealbum0000000001"
    assert track.added_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert track.reason.kind is ReasonKind.LIKED
    assert track.reason.playlist_id is None
    assert snapshot.counts[str(SourceKind.LIKED_TRACKS)] == 1


@respx.mock
def test_liked_tracks_canary_requires_album_id(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    broken = track_json("faketrack0000000001", "No Album Id")
    broken["album"] = {"name": "Album with no id"}
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": [{"track": broken}], "next": None})
    )
    with pytest.raises(SchemaError, match="'album' has no 'id'"):
        make_source(spotify_config, client, clock, liked_tracks=True).read()


@respx.mock
def test_liked_tracks_without_isrc_warn(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"track": track_json("faketrack0000000001", "No ISRC", isrc=None)}], "next": None},
        )
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()
    assert not snapshot.schema_ok
    assert "liked_tracks: external_ids absent" in snapshot.schema_warnings[0]


# ---------------------------------------------------------------------------- playlists


@respx.mock
def test_playlist_items_use_the_item_field(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"added_at": "2026-02-01T00:00:00Z", "item": track_json("faketrack0000000002", "From item")},
                ],
                "next": None,
            },
        )
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()

    track = snapshot.tracks[0]
    assert track.spotify_id == "faketrack0000000002"
    assert track.reason.kind is ReasonKind.PLAYLIST
    assert track.reason.playlist_id == PLAYLIST_ID
    assert snapshot.counts == {f"playlist:{PLAYLIST_ID}": 1}


@respx.mock
def test_playlist_items_fall_back_to_the_track_field(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(
            200, json={"items": [{"track": track_json("faketrack0000000003", "Legacy")}], "next": None}
        )
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()
    assert snapshot.tracks[0].spotify_id == "faketrack0000000003"


@respx.mock
def test_non_owned_playlist_is_detected(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": [], "next": None})
    )
    meta = respx.get(f"{API}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(
            200,
            json={"owner": {"id": "someone-else"}, "name": "Their Playlist", "tracks": {"total": 42}},
        )
    )
    with pytest.raises(SourceError, match="not owned by you"):
        make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()
    # No `fields` filter: naming the wrong nested key would break this very check.
    assert "fields" not in meta.calls[0].request.url.params


@respx.mock
def test_non_owned_playlist_is_detected_via_the_items_total(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Dev Mode may report the collection as `items` rather than `tracks`; either total counts."""
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": [], "next": None})
    )
    respx.get(f"{API}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(200, json={"owner": {"id": "someone-else"}, "items": {"total": 7}})
    )
    with pytest.raises(SourceError, match="7 tracks"):
        make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()


@respx.mock
def test_a_403_on_playlist_items_is_the_same_not_owned_message(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """The Get Playlist Items reference documents 403 for a non-owner, non-collaborator.
    It must read the same as the 200-with-zero-items path, not the generic
    "Spotify refused the request" every other 403 gets - that phrasing looks like a token
    problem, not "this playlist isn't yours"."""
    items = respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(return_value=httpx.Response(403))
    meta = respx.get(f"{API}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(200, json={"owner": {"id": "someone-else"}, "tracks": {"total": 42}})
    )
    with pytest.raises(SourceError, match="not owned by you") as excinfo:
        make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()

    assert "Spotify refused the request (HTTP 403)" not in str(excinfo.value)
    assert meta.calls == [], "a 403 is conclusive on its own - no metadata round trip needed"
    assert items.called


@respx.mock
@pytest.mark.parametrize(
    ("scope", "hint"),
    [
        ("user-follow-read user-library-read playlist-read-private", True),
        (ALL_SCOPES, False),
    ],
)
def test_an_unreadable_playlist_says_to_re_authorize_only_when_the_token_predates_collaboration(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock, scope: str, hint: bool
) -> None:
    """A token granted before likearr asked for playlist-read-collaborative cannot read
    a playlist you collaborate on either, so the one message that failure gets also says a
    re-authorization fixes that case. With the scope granted, the hint would be wrong."""
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(return_value=httpx.Response(403))
    source = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,))
    token = json.loads(spotify_config.token_file.read_text())
    spotify_config.token_file.write_text(json.dumps({**token, "scope": scope}))

    with pytest.raises(SourceError, match="not owned by you") as excinfo:
        source.read()

    assert ("If you collaborate on it, re-authorize Spotify" in str(excinfo.value)) is hint


@respx.mock
def test_genuinely_empty_playlist_is_fine(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": [], "next": None})
    )
    respx.get(f"{API}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(200, json={"owner": {"id": "me"}, "name": "Mine", "tracks": {"total": 0}})
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()
    assert snapshot.tracks == ()
    assert snapshot.counts == {f"playlist:{PLAYLIST_ID}": 0}


# ---------------------------------------------------------------------------- reported totals
#
# Spotify ends a paged read at the first page whose `next` is null. If it ever did that early, the
# read would look complete and the missing likes would look like un-likes. Every page also carries
# the `total` Spotify holds, so a read that falls short of it degrades the run (`schema_ok=False`),
# which holds back every unmonitor while adds still go ahead.


def liked_entries(count: int, *, start: int = 0) -> list[dict]:
    return [
        {"added_at": "2026-01-01T00:00:00Z", "track": track_json(f"faketrack{i:011d}", f"Song {i}")}
        for i in range(start, start + count)
    ]


def saved_entries(count: int) -> list[dict]:
    return [{"album": album_json(f"fakealbum{i:011d}", f"Album {i}")} for i in range(count)]


def playlist_entries(count: int) -> list[dict]:
    return [{"item": track_json(f"faketrack{i:011d}", f"Song {i}")} for i in range(count)]


def artist_entries(count: int) -> list[dict]:
    return [{"id": f"fakeartist{i:010d}", "name": f"Band {i}"} for i in range(count)]


@respx.mock
def test_liked_tracks_short_of_the_reported_total_degrade_the_run(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": liked_entries(50), "total": 120, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()

    assert not snapshot.schema_ok
    assert snapshot.schema_warnings == ("liked_tracks: read 50 of 120 items Spotify reported",)
    assert snapshot.counts[str(SourceKind.LIKED_TRACKS)] == 50


@respx.mock
def test_saved_albums_short_of_the_reported_total_degrade_the_run(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(200, json={"items": saved_entries(50), "total": 120, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()

    assert not snapshot.schema_ok
    assert snapshot.schema_warnings == ("saved_albums: read 50 of 120 items Spotify reported",)


@respx.mock
def test_a_playlist_short_of_the_reported_total_degrades_the_run(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": playlist_entries(50), "total": 120, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()

    assert not snapshot.schema_ok
    assert snapshot.schema_warnings == (f"playlist:{PLAYLIST_ID}: read 50 of 120 items Spotify reported",)


@respx.mock
def test_followed_artists_short_of_the_reported_total_degrade_the_run(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/following").mock(
        return_value=httpx.Response(
            200,
            json={"artists": {"items": artist_entries(50), "total": 120, "cursors": {"after": None}, "next": None}},
        )
    )
    snapshot = make_source(spotify_config, client, clock, followed_artists=True).read()

    assert not snapshot.schema_ok
    assert snapshot.schema_warnings == ("followed_artists: read 50 of 120 items Spotify reported",)


@respx.mock
def test_the_total_is_taken_from_the_first_page_and_checked_after_the_last(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Two full-looking pages that still fall short: the check covers the whole run, not one page."""
    respx.get(f"{API}/me/tracks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"items": liked_entries(50), "total": 200, "next": f"{API}/me/tracks?offset=50&limit=50"},
            ),
            httpx.Response(200, json={"items": liked_entries(50, start=50), "total": 200, "next": None}),
        ]
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()

    assert snapshot.schema_warnings == ("liked_tracks: read 100 of 200 items Spotify reported",)


@respx.mock
def test_a_complete_read_matching_the_total_stays_clean(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/tracks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"items": liked_entries(50), "total": 70, "next": f"{API}/me/tracks?offset=50&limit=50"},
            ),
            httpx.Response(200, json={"items": liked_entries(20, start=50), "total": 70, "next": None}),
        ]
    )
    respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(200, json={"items": saved_entries(3), "total": 3, "next": None})
    )
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": playlist_entries(4), "total": 4, "next": None})
    )
    respx.get(f"{API}/me/following").mock(
        return_value=httpx.Response(
            200,
            json={"artists": {"items": artist_entries(5), "total": 5, "cursors": {"after": None}, "next": None}},
        )
    )
    snapshot = make_source(
        spotify_config,
        client,
        clock,
        liked_tracks=True,
        saved_albums=True,
        followed_artists=True,
        playlists=(PLAYLIST_ID,),
    ).read()

    assert snapshot.schema_ok
    assert snapshot.schema_warnings == ()
    assert len(snapshot.tracks) == 74


@respx.mock
def test_a_playlists_local_and_null_tracks_count_toward_its_total(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Spotify's total counts local files and removed tracks; likearr drops them but read them."""
    items = [
        *playlist_entries(2),
        {"item": {"id": None, "name": "Local", "is_local": True}},
        {"item": None, "track": None},
        {"item": {"id": "fakeepisode000000001", "name": "Ep", "type": "episode"}},
    ]
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": items, "total": 5, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()

    assert snapshot.schema_ok
    assert snapshot.counts == {f"playlist:{PLAYLIST_ID}": 2}


@respx.mock
def test_liked_tracks_count_dropped_entries_toward_the_total(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    items = [*liked_entries(2), {"track": None}, {"track": {"id": None, "name": "Local", "is_local": True}}]
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": items, "total": 4, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()

    assert snapshot.schema_ok
    assert snapshot.counts[str(SourceKind.LIKED_TRACKS)] == 2


@pytest.mark.parametrize(("served", "total"), [(50, 51), (50, 52), (50, 49), (50, 48)])
@respx.mock
def test_a_small_drift_from_the_total_is_tolerated(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock, served: int, total: int
) -> None:
    """A like added or removed mid-read moves `total`, and offset paging can skip or repeat one."""
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": liked_entries(served), "total": total, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()
    assert snapshot.schema_ok


@pytest.mark.parametrize(("served", "total"), [(50, 53), (50, 47)])
@respx.mock
def test_drift_past_the_tolerance_degrades_the_run(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock, served: int, total: int
) -> None:
    """Reading more than Spotify reported means paging is off too, so it is not trusted either."""
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": liked_entries(served), "total": total, "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()
    assert snapshot.schema_warnings == (f"liked_tracks: read {served} of {total} items Spotify reported",)


@respx.mock
def test_an_empty_playlist_whose_page_reports_items_is_still_caught(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """The not-owned check keeps its own error; a page total alone also degrades the run."""
    respx.get(f"{API}/playlists/{PLAYLIST_ID}/items").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 9, "next": None})
    )
    respx.get(f"{API}/playlists/{PLAYLIST_ID}").mock(
        return_value=httpx.Response(200, json={"owner": {"id": "me"}, "name": "Mine", "tracks": {"total": 0}})
    )
    snapshot = make_source(spotify_config, client, clock, playlists=(PLAYLIST_ID,)).read()
    assert snapshot.schema_warnings == (f"playlist:{PLAYLIST_ID}: read 0 of 9 items Spotify reported",)


@respx.mock
def test_a_page_without_a_total_is_not_checked(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """No `total` means nothing to compare against, so the read stands as it did before."""
    respx.get(f"{API}/me/tracks").mock(
        return_value=httpx.Response(200, json={"items": liked_entries(3), "total": "many", "next": None})
    )
    snapshot = make_source(spotify_config, client, clock, liked_tracks=True).read()
    assert snapshot.schema_ok


# ---------------------------------------------------------------------------- failure modes


@respx.mock
def test_quota_exceeded_aborts_without_retrying(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/me/albums").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            json={"error": {"status": 429, "message": "QUOTA_EXCEEDED", "reason": "QUOTA_EXCEEDED"}},
        )
    )
    with pytest.raises(SourceError, match="QUOTA_EXCEEDED"):
        make_source(spotify_config, client, clock, saved_albums=True).read()
    assert route.call_count == 1
    assert clock.slept == []


@respx.mock
def test_server_error_aborts_the_whole_read(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me/albums").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(SourceError):
        make_source(spotify_config, client, clock, saved_albums=True).read()


@respx.mock
def test_non_json_body_aborts_the_read(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    respx.get(f"{API}/me/albums").mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(SourceError, match="not valid JSON"):
        make_source(spotify_config, client, clock, saved_albums=True).read()


@respx.mock
def test_a_401_triggers_one_refresh_then_retries(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    albums = respx.get(f"{API}/me/albums").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}}),
            httpx.Response(
                200, json={"items": [{"album": album_json("fakealbum0000000001", "After refresh")}], "next": None}
            ),
        ]
    )
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    snapshot = make_source(spotify_config, client, clock, saved_albums=True).read()

    assert albums.call_count == 2
    assert token.call_count == 1
    assert snapshot.albums[0].album.name == "After refresh"


@respx.mock
def test_a_persistent_401_aborts(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    respx.get(f"{API}/me/albums").mock(return_value=httpx.Response(401, json={"error": "nope"}))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    with pytest.raises(SourceError, match="still unauthorized"):
        make_source(spotify_config, client, clock, saved_albums=True).read()


@respx.mock
def test_all_sources_disabled_produces_an_empty_snapshot(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    snapshot = make_source(spotify_config, client, clock).read()
    assert snapshot.artists == () and snapshot.albums == () and snapshot.tracks == ()
    assert snapshot.counts == {}
    assert snapshot.schema_ok


# ---------------------------------------------------------------------------- port conformance


def test_spotify_source_satisfies_source_port(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Statically checked by pyright: the core must be able to take this as a SourcePort."""
    port: SourcePort = make_source(spotify_config, client, clock)
    assert callable(port.read)
