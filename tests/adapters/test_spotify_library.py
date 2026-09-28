"""`SpotifyLibrary`: the endpoints, the documented batch limits, the cache and the budget.

The endpoints are the interesting assertions here, because the published reference does not
describe what this app can actually do. Measured against a Development Mode app: the documented
`PUT /me/following` and `PUT /me/albums` are both **403**, and `PUT /me/library?uris=` is what
works for both a follow and a save. The two `/contains` endpoints are 403 as well, so membership
is read by PAGING `GET /me/following` and `GET /me/albums`.

Several tests below exist specifically to stop someone "correcting" this code back to the
documented endpoints.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
import respx

from likearr.adapters.spotify import ALL_SCOPES, SpotifyAuth
from likearr.adapters.spotify_library import (
    _MAX_LIBRARY_PAGES,
    LIBRARY_BATCH,
    SEARCH_LIMIT,
    OwnedPlaylist,
    PlaylistEntry,
    SpotifyLibrary,
)
from likearr.config import SpotifyConfig
from likearr.ports import QuotaExceeded, SchemaError, SearchBudgetExceeded, SourceError

from .conftest import FakeClock

API = "https://api.spotify.com/v1"


def make_library(
    config: SpotifyConfig,
    client: httpx.Client,
    clock: FakeClock,
    *,
    cache_path: Path | None = None,
    scope: str = ALL_SCOPES,
    max_searches: int = 600,
) -> SpotifyLibrary:
    config.token_file.write_text(
        json.dumps(
            {
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "expires_at": 9_999_999_999.0,
                "scope": scope,
                "token_type": "Bearer",
                "user_id": "fake-user",
            }
        )
    )
    auth = SpotifyAuth(config, client, now=clock.time, sleep=clock.sleep)
    return SpotifyLibrary(
        auth,
        client,
        cache_path=cache_path,
        min_interval_s=0.0,
        max_searches=max_searches,
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def ids(count: int, prefix: str = "id") -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(count)]


# ---------------------------------------------------------------------------- scopes


def test_granted_scopes_come_from_the_token_file(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    library = make_library(spotify_config, client, clock)
    assert "user-follow-modify" in library.granted_scopes()
    assert "user-library-modify" in library.granted_scopes()


def test_an_old_token_without_recorded_scopes_reports_none(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    library = make_library(spotify_config, client, clock, scope="")
    assert library.granted_scopes() == frozenset()


# ---------------------------------------------------------------------------- writes
#
# Both writes go through PUT /me/library?uris=, which is not in the public reference. The
# documented PUT /me/following and PUT /me/albums are 403 for a Development Mode app (measured).
# These tests exist to stop anyone "correcting" the code back to the documented endpoints.


def sent_uris(call: object) -> list[str]:
    """The URIs one write actually sent, read off the query string."""
    return dict(httpx.URL(str(call.request.url)).params)["uris"].split(",")  # type: ignore[attr-defined]


@respx.mock
def test_following_an_artist_writes_an_artist_uri_to_me_library(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).follow_artists(["4Z8W4fKeB5YxbusRsdQVPb"])

    assert route.call_count == 1
    assert sent_uris(route.calls[0]) == ["spotify:artist:4Z8W4fKeB5YxbusRsdQVPb"]


@respx.mock
def test_saving_an_album_writes_an_album_uri_to_me_library(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).save_albums(["6dVIqQ8qmQ5GBnJ9shOYGE"])

    assert sent_uris(route.calls[0]) == ["spotify:album:6dVIqQ8qmQ5GBnJ9shOYGE"]


@respx.mock
def test_the_uris_go_in_the_query_string_not_a_body(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """A body-only call answers 400 "Missing required field: uris", so likearr must not send one."""
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).save_albums(["a" * 22])

    request = route.calls[0].request
    assert "uris=" in str(request.url)
    assert not request.content, "no JSON body: the endpoint reads uris from the query string only"


@respx.mock
def test_the_documented_endpoints_are_never_called(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """`PUT /me/following` and `PUT /me/albums` are 403 for this app. Do not go back to them."""
    following = respx.put(f"{API}/me/following").mock(return_value=httpx.Response(403))
    albums = respx.put(f"{API}/me/albums").mock(return_value=httpx.Response(403))
    library = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))

    client_under_test = make_library(spotify_config, client, clock)
    client_under_test.follow_artists(["a" * 22])
    client_under_test.save_albums(["b" * 22])

    assert following.call_count == 0 and albums.call_count == 0
    assert library.call_count == 2


@respx.mock
def test_writes_batch_at_twenty_uris_with_a_remainder(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """20 in one call is the largest figure verified live; the endpoint has no published max."""
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).save_albums(ids(45))

    assert route.call_count == 3
    assert [len(sent_uris(c)) for c in route.calls] == [LIBRARY_BATCH, LIBRARY_BATCH, 5]
    assert sent_uris(route.calls[2]) == [f"spotify:album:{i}" for i in ids(45)[40:]]


@respx.mock
def test_exactly_one_batch_is_not_split(spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock) -> None:
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).save_albums(ids(LIBRARY_BATCH))

    assert route.call_count == 1


@respx.mock
def test_writes_drop_duplicate_and_empty_ids(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).follow_artists(["a", "a", "", "b"])

    assert sent_uris(route.calls[0]) == ["spotify:artist:a", "spotify:artist:b"]


@respx.mock
def test_writing_nothing_makes_no_request(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.put(f"{API}/me/library").mock(return_value=httpx.Response(200))
    make_library(spotify_config, client, clock).save_albums([])
    assert route.call_count == 0


@respx.mock
def test_a_403_on_a_write_is_a_clear_error_not_a_silent_skip(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """If this endpoint is ever restricted too, the run must say so, not quietly write nothing."""
    respx.put(f"{API}/me/library").mock(
        return_value=httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
    )
    with pytest.raises(SourceError, match="save albums") as caught:
        make_library(spotify_config, client, clock).save_albums(["a" * 22])

    assert "403" in str(caught.value)


# ---------------------------------------------------------------------------- what is already there
#
# Membership is answered by paging the list endpoints, never by `/contains` - those two are 403
# on a Development Mode account while these list calls answer 200 on the very same token.


def following_page(item_ids: list[str], next_url: str | None) -> dict:
    """A `GET /me/following` page: nested under `artists`, cursor-paged."""
    return {
        "artists": {
            "href": f"{API}/me/following?type=artist&limit=50",
            "limit": 50,
            "next": next_url,
            "cursors": {"after": item_ids[-1] if item_ids else None},
            "total": len(item_ids),
            "items": [{"id": i, "name": f"Artist {i}", "type": "artist"} for i in item_ids],
        }
    }


def albums_page(item_ids: list[str], next_url: str | None, offset: int = 0) -> dict:
    """A `GET /me/albums` page: top level, offset-paged, album nested under `items[].album`."""
    return {
        "href": f"{API}/me/albums?limit=50",
        "limit": 50,
        "next": next_url,
        "offset": offset,
        "previous": None,
        "total": len(item_ids),
        "items": [{"added_at": "2026-01-01T00:00:00Z", "album": {"id": i, "name": f"Album {i}"}} for i in item_ids],
    }


@respx.mock
def test_followed_artists_are_read_by_following_the_cursor_pages(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    page_two = f"{API}/me/following?type=artist&limit=50&after=id0049"
    respx.get(f"{API}/me/following", params__eq={"type": "artist", "limit": "50"}).mock(
        return_value=httpx.Response(200, json=following_page(ids(50), page_two))
    )
    respx.get(page_two).mock(return_value=httpx.Response(200, json=following_page(ids(60)[50:], None)))

    followed = make_library(spotify_config, client, clock).followed_artist_ids()

    assert len(followed) == 60, "60 followed artists is two pages; the second must be followed"
    assert "id0000" in followed and "id0059" in followed


@respx.mock
def test_saved_albums_are_read_by_following_the_offset_pages(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    page_two = f"{API}/me/albums?limit=50&offset=50"
    respx.get(f"{API}/me/albums", params__eq={"limit": "50"}).mock(
        return_value=httpx.Response(200, json=albums_page(ids(50), page_two))
    )
    respx.get(page_two).mock(return_value=httpx.Response(200, json=albums_page(ids(83)[50:], None, offset=50)))

    saved = make_library(spotify_config, client, clock).saved_album_ids()

    assert len(saved) == 83
    assert "id0082" in saved, "the id comes from items[].album.id, not from the saving itself"


@respx.mock
def test_a_library_that_keeps_paging_is_walked_to_the_end(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Five pages, so "page properly" is tested rather than "handle exactly two"."""
    pages = 5

    def answer(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("offset", 0)) // 50
        batch = [f"id{page * 50 + i:04d}" for i in range(50)]
        nxt = f"{API}/me/albums?limit=50&offset={(page + 1) * 50}" if page + 1 < pages else None
        return httpx.Response(200, json=albums_page(batch, nxt, offset=page * 50))

    route = respx.get(url__startswith=f"{API}/me/albums").mock(side_effect=answer)
    saved = make_library(spotify_config, client, clock).saved_album_ids()

    assert route.call_count == pages
    assert len(saved) == pages * 50


@respx.mock
def test_each_list_is_read_once_and_reused(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Every membership test in a run asks the same question of the same list."""
    route = respx.get(url__startswith=f"{API}/me/following").mock(
        return_value=httpx.Response(200, json=following_page(ids(3), None))
    )
    library = make_library(spotify_config, client, clock)

    assert library.followed_artist_ids() == library.followed_artist_ids()
    assert route.call_count == 1


@respx.mock
def test_a_page_missing_its_pagination_fields_is_a_schema_error(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(url__startswith=f"{API}/me/albums").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(SchemaError, match="'next' is missing"):
        make_library(spotify_config, client, clock).saved_album_ids()


@respx.mock
def test_a_next_that_never_ends_is_refused_rather_than_looped_on(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(url__startswith=f"{API}/me/albums").mock(
        return_value=httpx.Response(200, json=albums_page(ids(50), f"{API}/me/albums?limit=50&offset=50"))
    )
    with pytest.raises(SourceError, match="refusing to page further"):
        make_library(spotify_config, client, clock).saved_album_ids()


# ---------------------------------------------------------------------------- search


@respx.mock
def test_search_albums_sends_the_documented_filters_and_limit(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json={"albums": {"items": [_album_json("sp-1", "In Rainbows")]}})
    )
    hits = make_library(spotify_config, client, clock).search_albums("Radiohead", "In Rainbows")

    params = dict(httpx.URL(str(route.calls[0].request.url)).params)
    assert params["q"] == 'album:"In Rainbows" artist:"Radiohead"'
    assert params["type"] == "album"
    assert params["limit"] == str(SEARCH_LIMIT)
    assert [h.spotify_id for h in hits] == ["sp-1"]
    assert hits[0].artist_names == ("Radiohead",)


@respx.mock
def test_search_by_upc_uses_the_upc_filter(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json={"albums": {"items": []}}))
    make_library(spotify_config, client, clock).search_albums_by_upc("0634904032524")

    assert dict(httpx.URL(str(route.calls[0].request.url)).params)["q"] == "upc:0634904032524"


@respx.mock
def test_search_artists_reads_the_artists_block(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json={"artists": {"items": [{"id": "sp-a", "name": "Radiohead"}]}})
    )
    hits = make_library(spotify_config, client, clock).search_artists("Radiohead")
    assert [(h.spotify_id, h.name) for h in hits] == [("sp-a", "Radiohead")]


@respx.mock
def test_an_identical_search_is_answered_from_the_cache(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock, tmp_path: Path
) -> None:
    """This is what makes a re-plan after a quota error free for work already done."""
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json={"albums": {"items": [_album_json("sp-1", "Kid A")]}})
    )
    cache = tmp_path / "state.sqlite"
    first = make_library(spotify_config, client, clock, cache_path=cache)
    first.search_albums("Radiohead", "Kid A")
    first.close()

    second = make_library(spotify_config, client, clock, cache_path=cache)
    hits = second.search_albums("Radiohead", "Kid A")
    second.close()

    assert route.call_count == 1, "a second process must not repeat the call"
    assert [h.spotify_id for h in hits] == ["sp-1"]
    assert second.searches == 0, "a cache hit does not spend the budget"


@respx.mock
def test_the_search_budget_stops_instead_of_burning_the_quota(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json={"artists": {"items": []}}))
    library = make_library(spotify_config, client, clock, max_searches=2)

    library.search_artists("One")
    library.search_artists("Two")
    with pytest.raises(SearchBudgetExceeded, match="budget of 2"):
        library.search_artists("Three")

    assert route.call_count == 2, "the third search is never sent"


@respx.mock
def test_a_quota_rejection_is_not_retried(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(429, json={"error": {"status": 429, "message": "QUOTA_EXCEEDED"}})
    )
    with pytest.raises(SourceError, match="QUOTA_EXCEEDED"):
        make_library(spotify_config, client, clock).search_artists("Radiohead")

    assert route.call_count == 1, "a hard quota rejection will not improve with a retry"


@respx.mock
def test_a_quota_rejection_says_when_spotify_will_take_requests_again(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/search").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "86400"},
            json={"error": {"status": 429, "message": "Too many requests", "reason": "QUOTA_EXCEEDED"}},
        )
    )
    with pytest.raises(QuotaExceeded) as excinfo:
        make_library(spotify_config, client, clock).search_artists("Radiohead")

    assert excinfo.value.retry_after == 86400.0
    assert "retry after 86400 s (about 24 h)" in str(excinfo.value)
    assert "zero unmonitors" in str(excinfo.value)


@respx.mock
def test_a_plain_429_over_the_backoff_cap_names_the_wait(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """No QUOTA_EXCEEDED marker, but Retry-After is long enough that the shared helper stops
    instead of retrying early - the message should be as readable as the quota one."""
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": {"status": 429}})
    )
    with pytest.raises(SourceError, match="retry after 3600 s \\(about 60 min\\)"):
        make_library(spotify_config, client, clock).search_artists("Radiohead")

    assert route.call_count == 1


@respx.mock
def test_a_429_with_retry_after_is_honoured(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    route = respx.get(f"{API}/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"artists": {"items": []}}),
        ]
    )
    make_library(spotify_config, client, clock).search_artists("Radiohead")

    assert route.call_count == 2
    assert max(clock.slept) >= 7.0, "Retry-After is a floor"


def _album_json(spotify_id: str, name: str) -> dict:
    return {
        "id": spotify_id,
        "name": name,
        "album_type": "album",
        "release_date": "2007-10-10",
        "release_date_precision": "day",
        "artists": [{"id": "sp-artist", "name": "Radiohead"}],
    }


# ---------------------------------------------------------------------------- owned playlists
#
# The web UI's playlist picker. Only playlists the user OWNS are offered, because Development
# Mode returns zero items for anyone else's playlist (see `SpotifySource._read_playlist`).

ME = "fake-user-me"


def playlist(
    playlist_id: str,
    name: str,
    *,
    owner: str = ME,
    tracks_total: int | None = None,
    items_total: int | None = None,
    collaborative: bool | None = None,
) -> dict:
    """One `GET /me/playlists` item, carrying its count as `tracks.total`, `items.total`, or neither."""
    entry: dict = {"id": playlist_id, "name": name, "owner": {"id": owner, "display_name": owner}}
    if collaborative is not None:
        entry["collaborative"] = collaborative
    if tracks_total is not None:
        entry["tracks"] = {"href": f"{API}/playlists/{playlist_id}/tracks", "total": tracks_total}
    if items_total is not None:
        entry["items"] = {"href": f"{API}/playlists/{playlist_id}/items", "total": items_total}
    return entry


def playlists_page(entries: Sequence[dict | None], next_url: str | None) -> dict:
    return {"href": f"{API}/me/playlists", "items": list(entries), "next": next_url, "limit": 50, "total": len(entries)}


def mock_me(user_id: str = ME) -> respx.Route:
    return respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json={"id": user_id, "display_name": "Me"}))


@respx.mock
def test_owned_playlists_keeps_only_the_users_own(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    mock_me()
    route = respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200,
            json=playlists_page(
                [
                    playlist("pl-mine", "Mine", tracks_total=12),
                    playlist("pl-followed", "Someone Else's", owner="another-user", tracks_total=40),
                    playlist("pl-spotify", "Discover Weekly", owner="spotify", tracks_total=30),
                ],
                None,
            ),
        )
    )
    owned = make_library(spotify_config, client, clock).owned_playlists()

    assert owned == [OwnedPlaylist(id="pl-mine", name="Mine", track_count=12)]
    assert dict(route.calls[0].request.url.params)["limit"] == "50"


@respx.mock
def test_all_playlists_lists_every_entry_marked_owned_or_not(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """The picker's listing: nothing is dropped, each row says whether
    Development Mode will read its items."""
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200,
            json=playlists_page(
                [
                    playlist("pl-mine", "Mine", tracks_total=12),
                    playlist("pl-followed", "Someone Else's", owner="another-user", tracks_total=40),
                    playlist("pl-spotify", "Discover Weekly", owner="spotify", tracks_total=30),
                ],
                None,
            ),
        )
    )
    entries = make_library(spotify_config, client, clock).all_playlists()

    assert entries == [
        PlaylistEntry(id="pl-spotify", name="Discover Weekly", track_count=30, owned=False, collaborative_scope=True),
        PlaylistEntry(id="pl-mine", name="Mine", track_count=12, owned=True, collaborative_scope=True),
        PlaylistEntry(id="pl-followed", name="Someone Else's", track_count=40, owned=False, collaborative_scope=True),
    ]
    assert [p.readable for p in entries] == [False, True, False]


OLD_SCOPES = "user-follow-read user-library-read playlist-read-private user-follow-modify user-library-modify"
"""What a token granted before the collaborative scope carries: read and write, no playlist-read-collaborative."""


def _collaborative_listing() -> None:
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200,
            json=playlists_page(
                [
                    playlist("pl-mine", "Mine", tracks_total=12),
                    playlist("pl-mine-collab", "Mine, Shared", tracks_total=4, collaborative=True),
                    playlist("pl-collab", "Road Trip", owner="a-friend", tracks_total=20, collaborative=True),
                    playlist(
                        "pl-followed", "Someone Else's", owner="another-user", tracks_total=40, collaborative=False
                    ),
                ],
                None,
            ),
        )
    )


@respx.mock
def test_a_collaborative_playlist_someone_else_owns_is_readable_with_the_scope(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """With playlist-read-collaborative granted, a playlist you collaborate on is a
    source like one you own - listed as readable, and among `owned_playlists` (the picker's
    selectable set). A followed playlist stays unreadable."""
    _collaborative_listing()
    library = make_library(spotify_config, client, clock)

    by_id = {p.id: p for p in library.all_playlists()}
    assert by_id["pl-collab"].readable and not by_id["pl-collab"].owned and not by_id["pl-collab"].needs_reauth
    assert by_id["pl-mine-collab"].readable and by_id["pl-mine-collab"].owned
    assert not by_id["pl-followed"].readable and not by_id["pl-followed"].needs_reauth
    assert [p.id for p in library.owned_playlists()] == ["pl-mine", "pl-mine-collab", "pl-collab"]


@respx.mock
def test_before_a_reauth_a_collaborative_playlist_is_listed_but_not_offered(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """A token granted before likearr asked for playlist-read-collaborative: the playlist is not
    offered (a run could not read it), and is marked as needing a re-auth rather than a copy."""
    _collaborative_listing()
    library = make_library(spotify_config, client, clock, scope=OLD_SCOPES)

    by_id = {p.id: p for p in library.all_playlists()}
    assert not by_id["pl-collab"].readable and by_id["pl-collab"].needs_reauth
    assert by_id["pl-mine-collab"].readable and not by_id["pl-mine-collab"].needs_reauth
    assert [p.id for p in library.owned_playlists()] == ["pl-mine", "pl-mine-collab"]


@respx.mock
def test_all_playlists_treats_a_missing_owner_as_not_owned(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """An entry with an id but no readable `owner` is listed, greyed out, not dropped - only a
    `null` entry or one with no id at all is skipped (that is Spotify saying "will never show
    you this one", which `owned_playlists` already covered)."""
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(200, json=playlists_page([{"id": "pl-x", "name": "No Owner"}], None))
    )
    entries = make_library(spotify_config, client, clock).all_playlists()

    assert entries == [PlaylistEntry(id="pl-x", name="No Owner", track_count=0, owned=False, collaborative_scope=True)]


@respx.mock
def test_owned_playlists_reads_either_total_shape(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """`tracks.total` is the classic shape; the 2026 Dev Mode API may carry it as `items.total`."""
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200,
            json=playlists_page(
                [
                    playlist("pl-a", "A Tracks", tracks_total=5),
                    playlist("pl-b", "B Items", items_total=7),
                    playlist("pl-c", "C Both", tracks_total=3, items_total=99),
                    playlist("pl-d", "D Neither"),
                ],
                None,
            ),
        )
    )
    owned = make_library(spotify_config, client, clock).owned_playlists()

    assert [(p.id, p.track_count) for p in owned] == [("pl-a", 5), ("pl-b", 7), ("pl-c", 3), ("pl-d", 0)]


@respx.mock
def test_owned_playlists_follows_the_pages_and_sorts_by_name(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    mock_me()
    pages = [
        [playlist("pl-2", "beta"), playlist("pl-3", "Alpha")],
        [playlist("pl-1", "alpha"), playlist("pl-5", "Other's", owner="another-user")],
        [playlist("pl-4", "Gamma")],
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("offset", 0)) // 50
        nxt = f"{API}/me/playlists?offset={(page + 1) * 50}&limit=50" if page + 1 < len(pages) else None
        return httpx.Response(200, json=playlists_page(pages[page], nxt))

    route = respx.get(url__startswith=f"{API}/me/playlists").mock(side_effect=respond)
    owned = make_library(spotify_config, client, clock).owned_playlists()

    assert route.call_count == len(pages)
    assert [p.id for p in owned] == ["pl-1", "pl-3", "pl-2", "pl-4"], "name casefolded, then id"


@respx.mock
def test_owned_playlists_skips_malformed_entries(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Spotify sends `null` for a playlist it cannot show; that is not a reason to fail the list."""
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200,
            json=playlists_page(
                [
                    None,
                    {"name": "no id", "owner": {"id": ME}},
                    {"id": "pl-x", "name": "No Owner"},
                    playlist("pl-ok", "Ok"),
                ],
                None,
            ),
        )
    )
    assert [p.id for p in make_library(spotify_config, client, clock).owned_playlists()] == ["pl-ok"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"next": None}, "'items' is missing"),
        ({"items": []}, "'next' is missing"),
    ],
)
@respx.mock
def test_owned_playlists_page_missing_its_pagination_fields_is_a_schema_error(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock, body: dict, match: str
) -> None:
    mock_me()
    respx.get(url__startswith=f"{API}/me/playlists").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(SchemaError, match=match):
        make_library(spotify_config, client, clock).owned_playlists()


@respx.mock
def test_owned_playlists_without_a_user_id_is_a_schema_error(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json={"display_name": "Me"}))
    with pytest.raises(SchemaError, match="'id'"):
        make_library(spotify_config, client, clock).owned_playlists()


@respx.mock
def test_owned_playlists_next_that_never_ends_is_refused(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    mock_me()
    route = respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(
            200, json=playlists_page([playlist("pl-1", "Loop")], f"{API}/me/playlists?offset=50&limit=50")
        )
    )
    with pytest.raises(SourceError, match="refusing to page further"):
        make_library(spotify_config, client, clock).owned_playlists()
    assert route.call_count == _MAX_LIBRARY_PAGES


@respx.mock
def test_owned_playlists_refreshes_once_on_a_401(
    spotify_config: SpotifyConfig, client: httpx.Client, clock: FakeClock
) -> None:
    """Every call goes through `authorized_request`, so a 401 takes the locked refresh path."""
    token = respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fake-access-new", "expires_in": 3600})
    )
    respx.get(f"{API}/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"id": ME})],
    )
    respx.get(url__startswith=f"{API}/me/playlists").mock(
        return_value=httpx.Response(200, json=playlists_page([playlist("pl-1", "One")], None))
    )
    assert [p.id for p in make_library(spotify_config, client, clock).owned_playlists()] == ["pl-1"]
    assert token.call_count == 1
