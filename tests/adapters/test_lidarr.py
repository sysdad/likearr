"""Tests for the Lidarr adapter: every method, batching, version gate and command polling."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest
import respx

from likearr.adapters.http import build_client
from likearr.adapters.lidarr import LidarrClient
from likearr.config import LidarrConfig
from likearr.models import LidarrArtist, PrimaryType, Profile, SecondaryType
from likearr.ports import LidarrArtistExists, LidarrArtistUnknown, LidarrError, LidarrMetadataError, LidarrPort

from .conftest import FAKE_API_KEY, LIDARR_URL, FakeClock

V1 = f"{LIDARR_URL}/api/v1"

ARTIST_MBID = "00000000-0000-4000-8000-0000000000a1"
RG_MBID = "00000000-0000-4000-8000-000000000001"

ARTIST_JSON = {
    "id": 7,
    "foreignArtistId": ARTIST_MBID,
    "artistName": "Fake Band",
    "monitored": True,
    "monitorNewItems": "none",
    "metadataProfileId": 1,
    "qualityProfileId": 2,
    "tags": [3],
    "path": "/music/Fake Band",
}

ALBUM_JSON = {
    "id": 42,
    "foreignAlbumId": RG_MBID,
    "artistId": 7,
    "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Fake Band"},
    "title": "Fake Album",
    "monitored": False,
    "albumType": "Album",
    "secondaryTypes": ["Live"],
    "releaseDate": "2021-03-04T00:00:00Z",
    "statistics": {"trackFileCount": 11, "sizeOnDisk": 123456},
}

ARTIST = LidarrArtist(
    id=7,
    mbid=ARTIST_MBID,
    name="Fake Band",
    monitored=True,
    monitor_new_items="none",
    metadata_profile_id=1,
    quality_profile_id=2,
    tags=frozenset({3}),
)


@pytest.fixture
def lidarr(lidarr_config: LidarrConfig, client: httpx.Client, clock: FakeClock) -> LidarrClient:
    return LidarrClient(lidarr_config, client, api_key=FAKE_API_KEY, sleep=clock.sleep, monotonic=clock.monotonic)


def body_of(route: respx.Route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content.decode())


# ---------------------------------------------------------------------------- version gate


@respx.mock
@pytest.mark.parametrize("version", ["2.6.4.4402", "3.0.0.1"])
def test_check_version_accepts_supported_majors(lidarr: LidarrClient, version: str) -> None:
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": version}))
    assert lidarr.check_version() == version


@respx.mock
def test_check_version_refuses_an_unknown_major(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "4.0.0.1"}))
    with pytest.raises(LidarrError, match="major version 4"):
        lidarr.check_version()


@respx.mock
def test_check_version_refuses_an_unparseable_version(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "nightly"}))
    with pytest.raises(LidarrError, match="unparseable version"):
        lidarr.check_version()


@respx.mock
def test_version_is_fetched_once(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "2.6.4.4402"}))
    lidarr.version()
    lidarr.version()
    assert route.call_count == 1


@respx.mock
def test_api_key_header_is_sent(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "2.6.4.4402"}))
    lidarr.version()
    assert route.calls[0].request.headers["X-Api-Key"] == FAKE_API_KEY


@respx.mock
def test_an_error_never_leaks_the_api_key(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/system/status").mock(
        return_value=httpx.Response(401, text=f'{{"message": "Invalid API Key {FAKE_API_KEY}"}}')
    )
    with pytest.raises(LidarrError) as excinfo:
        lidarr.version()
    assert FAKE_API_KEY not in str(excinfo.value)


# ---------------------------------------------------------------------------- reads


@respx.mock
def test_load_view_loads_albums_only_for_the_requested_artists(lidarr: LidarrClient) -> None:
    other = {**ARTIST_JSON, "id": 8, "foreignArtistId": "00000000-0000-4000-8000-0000000000a2"}
    respx.get(f"{V1}/artist").mock(return_value=httpx.Response(200, json=[ARTIST_JSON, other]))
    albums = respx.get(f"{V1}/album").mock(return_value=httpx.Response(200, json=[ALBUM_JSON]))
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[{"id": 1, "name": "Lean"}]))
    respx.get(f"{V1}/qualityprofile").mock(return_value=httpx.Response(200, json=[{"id": 2, "name": "Standard"}]))
    respx.get(f"{V1}/tag").mock(return_value=httpx.Response(200, json=[{"id": 3, "label": "likearr"}]))
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "2.6.4.4402"}))

    view = lidarr.load_view([ARTIST_MBID])

    assert set(view.artists) == {ARTIST_MBID, "00000000-0000-4000-8000-0000000000a2"}
    assert set(view.albums) == {ARTIST_MBID}
    assert view.metadata_profiles == {"Lean": 1}
    assert view.quality_profiles == {"Standard": 2}
    assert view.tags == {"likearr": 3}
    assert view.version == "2.6.4.4402"
    assert albums.call_count == 1
    assert albums.calls[0].request.url.params["artistId"] == "7"


@respx.mock
def test_load_view_without_artists_loads_no_albums(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/artist").mock(return_value=httpx.Response(200, json=[ARTIST_JSON]))
    albums = respx.get(f"{V1}/album").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/qualityprofile").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/tag").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, json={"version": "2.6.4.4402"}))

    view = lidarr.load_view()
    assert view.albums == {}
    assert albums.call_count == 0


@respx.mock
def test_load_albums_maps_the_resource(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album").mock(return_value=httpx.Response(200, json=[ALBUM_JSON]))
    albums = lidarr.load_albums(ARTIST)

    album = albums[RG_MBID]
    assert album.id == 42
    assert album.artist_mbid == ARTIST_MBID
    assert album.primary_type is PrimaryType.ALBUM
    assert album.secondary_types == frozenset({SecondaryType.LIVE})
    assert album.release_date == date(2021, 3, 4)
    assert album.track_file_count == 11
    assert album.size_on_disk == 123456
    assert album.has_files


@respx.mock
def test_root_folders(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/music", "accessible": True}])
    )
    assert lidarr.root_folders()[0]["path"] == "/music"


@respx.mock
def test_import_lists_and_the_busy_part_of_the_command_queue(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/importlist").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "name": "Last.fm", "enableAutomaticAdd": True}])
    )
    respx.get(f"{V1}/command").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "RescanFolders", "status": "started"},
                {"name": "RefreshArtist", "status": "queued"},
                {"name": "RefreshArtist", "status": "completed"},
            ],
        )
    )

    assert lidarr.import_lists()[0]["enableAutomaticAdd"] is True
    assert [c["status"] for c in lidarr.command_queue()] == ["started", "queued"]


# ---------------------------------------------------------------------------- metadata lookup


@respx.mock
def test_lookup_release_group_uses_the_lidarr_prefix(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"foreignAlbumId": "some-other-id", "title": "Wrong"},
                {
                    "foreignAlbumId": RG_MBID,
                    "title": "Fake Album",
                    "albumType": "Album",
                    "secondaryTypes": [],
                    "releaseDate": "2021-03-04T00:00:00Z",
                    "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Fake Band"},
                },
            ],
        )
    )
    group = lidarr.lookup_release_group(RG_MBID)

    assert route.calls[0].request.url.params["term"] == f"lidarr:{RG_MBID}"
    assert group is not None
    assert group.mbid == RG_MBID
    assert group.artist_mbid == ARTIST_MBID
    assert group.primary_type is PrimaryType.ALBUM
    assert group.first_release_date == date(2021, 3, 4)


@respx.mock
def test_lookup_release_group_returns_none_when_absent(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(return_value=httpx.Response(200, json=[]))
    assert lidarr.lookup_release_group(RG_MBID) is None


@respx.mock
def test_lookup_release_group_maps_a_5xx_to_a_metadata_error(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(return_value=httpx.Response(503, text="skyhook is down"))
    with pytest.raises(LidarrMetadataError, match=r"api\.lidarr\.audio"):
        lidarr.lookup_release_group(RG_MBID)


@respx.mock
def test_lookup_release_group_maps_a_timeout_to_a_metadata_error(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LidarrMetadataError):
        lidarr.lookup_release_group(RG_MBID)


@respx.mock
def test_lookup_release_group_keeps_a_4xx_as_a_plain_lidarr_error(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(return_value=httpx.Response(400, text="bad term"))
    with pytest.raises(LidarrError) as excinfo:
        lidarr.lookup_release_group(RG_MBID)
    assert not isinstance(excinfo.value, LidarrMetadataError)


@respx.mock
def test_search_release_group_is_conservative(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "foreignAlbumId": RG_MBID,
                    "title": "Fake Album (Deluxe)",
                    "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Fake Band"},
                },
                {
                    "foreignAlbumId": "tribute",
                    "title": "Fake Album",
                    "artist": {"foreignArtistId": "x", "artistName": "A Tribute Band"},
                },
            ],
        )
    )
    group = lidarr.search_release_group("Fake Band", "Fake Album")
    assert route.calls[0].request.url.params["term"] == "Fake Band Fake Album"
    assert group is not None and group.mbid == RG_MBID


@respx.mock
def test_search_release_group_rejects_everything_doubtful(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "foreignAlbumId": "nope",
                    "title": "Something Else",
                    "artist": {"foreignArtistId": "x", "artistName": "Fake Band"},
                }
            ],
        )
    )
    assert lidarr.search_release_group("Fake Band", "Fake Album") is None
    assert lidarr.search_release_group("", "Fake Album") is None


def _lookup_hit(rg: str, title: str, artist_mbid: str, artist: str = "Jungle") -> dict:
    return {"foreignAlbumId": rg, "title": title, "artist": {"foreignArtistId": artist_mbid, "artistName": artist}}


@respx.mock
def test_search_candidates_keep_two_same_named_artists_apart(lidarr: LidarrClient) -> None:
    """Issue #42: the first name match used to win, which is a guess between two artists called
    "Jungle". Every artist whose name and title match comes back, each with its first hit."""
    respx.get(f"{V1}/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                _lookup_hit("rg-us-1969", "Jungle", "artist-us"),
                _lookup_hit("rg-london-2014", "Jungle", "artist-london"),
                _lookup_hit("rg-us-reissue", "Jungle", "artist-us"),
                _lookup_hit("rg-other", "Something Else", "artist-london"),
            ],
        )
    )

    found = lidarr.search_release_group_candidates("Jungle", "Jungle")

    assert [(g.mbid, g.artist_mbid) for g in found] == [
        ("rg-us-1969", "artist-us"),
        ("rg-london-2014", "artist-london"),
    ]


@respx.mock
def test_search_candidates_for_one_artist_are_exactly_the_old_answer(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                _lookup_hit(RG_MBID, "Fake Album", ARTIST_MBID, "Fake Band"),
                _lookup_hit("rg-later", "Fake Album", ARTIST_MBID, "Fake Band"),
            ],
        )
    )

    found = lidarr.search_release_group_candidates("Fake Band", "Fake Album")
    single = lidarr.search_release_group("Fake Band", "Fake Album")

    assert [g.mbid for g in found] == [RG_MBID]
    assert single is not None and single.mbid == RG_MBID
    assert lidarr.search_release_group_candidates("", "Fake Album") == ()


# ---------------------------------------------------------------------------- add artist


@respx.mock
def test_add_artist_payload(lidarr: LidarrClient) -> None:
    route = respx.post(f"{V1}/artist").mock(return_value=httpx.Response(201, json=ARTIST_JSON))
    artist = lidarr.add_artist(
        ARTIST_MBID,
        "Fake Band",
        root_folder="/music",
        quality_profile_id=2,
        metadata_profile_id=1,
        tag_ids=[3],
    )
    assert artist.id == 7
    assert body_of(route) == {
        "foreignArtistId": ARTIST_MBID,
        "artistName": "Fake Band",
        "qualityProfileId": 2,
        "metadataProfileId": 1,
        "rootFolderPath": "/music",
        "monitored": True,
        "monitorNewItems": "none",
        "tags": [3],
        "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
    }


@respx.mock
def test_add_artist_handles_already_exists(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/artist").mock(
        return_value=httpx.Response(
            400, json=[{"errorMessage": "This artist has already been added", "propertyName": "ForeignArtistId"}]
        )
    )
    listing = respx.get(f"{V1}/artist").mock(return_value=httpx.Response(200, json=[ARTIST_JSON]))
    with pytest.raises(LidarrArtistExists) as caught:
        lidarr.add_artist(
            ARTIST_MBID, "Fake Band", root_folder="/music", quality_profile_id=2, metadata_profile_id=1, tag_ids=[]
        )
    assert caught.value.artist.id == 7, "carries the artist Lidarr holds, for apply to judge by its tag (#4)"
    assert not isinstance(caught.value, LidarrMetadataError), "not an outage"
    assert listing.call_count == 1


@respx.mock
def test_add_artist_reraises_other_400s(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/artist").mock(
        return_value=httpx.Response(400, json=[{"errorMessage": "Root folder does not exist"}])
    )
    with pytest.raises(LidarrError, match="Root folder") as caught:
        lidarr.add_artist(
            ARTIST_MBID, "Fake Band", root_folder="/nope", quality_profile_id=2, metadata_profile_id=1, tag_ids=[]
        )
    assert not isinstance(caught.value, LidarrMetadataError), "a bad request of ours must stop the apply, not skip"


@respx.mock
@pytest.mark.parametrize("status", [500, 503])
def test_add_artist_maps_a_server_side_error_to_a_metadata_failure(lidarr: LidarrClient, status: int) -> None:
    """Issue #173: Lidarr answers a SkyHook outage on POST /artist with a 5xx. That skips the artist."""
    respx.post(f"{V1}/artist").mock(return_value=httpx.Response(status, text="skyhook is down"))
    with pytest.raises(LidarrMetadataError) as caught:
        lidarr.add_artist(
            ARTIST_MBID, "Fake Band", root_folder="/music", quality_profile_id=2, metadata_profile_id=1, tag_ids=[]
        )
    assert not isinstance(caught.value, LidarrArtistUnknown), "an outage is not an unknown artist"


LIDARR_NOT_FOUND = [
    {
        "propertyName": "MusicbrainzId",
        "errorMessage": "An artist with this ID was not found",
        "attemptedValue": ARTIST_MBID,
        "severity": "error",
    }
]
"""Lidarr 3.1's 400 for an MBID its metadata server (SkyHook) does not know yet (rig B, #173)."""


@respx.mock
def test_add_artist_maps_lidarr_not_knowing_the_artist_to_artist_unknown(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/artist").mock(return_value=httpx.Response(400, json=LIDARR_NOT_FOUND))
    listing = respx.get(f"{V1}/artist").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(LidarrArtistUnknown, match="does not know this artist yet"):
        lidarr.add_artist(
            ARTIST_MBID, "Fake Band", root_folder="/music", quality_profile_id=2, metadata_profile_id=1, tag_ids=[]
        )
    assert listing.call_count == 0, "not an 'already exists', so there is nothing to look up"


@respx.mock
def test_add_artist_does_not_read_other_not_found_400s_as_an_unknown_artist(lidarr: LidarrClient) -> None:
    """Only Lidarr's own "artist with this ID was not found" is skipped. A 400 about something
    likearr sent (here a quality profile) is likearr's own fault and must still stop the apply."""
    respx.post(f"{V1}/artist").mock(
        return_value=httpx.Response(
            400, json=[{"propertyName": "QualityProfileId", "errorMessage": "Quality profile was not found"}]
        )
    )
    with pytest.raises(LidarrError, match="Quality profile") as caught:
        lidarr.add_artist(
            ARTIST_MBID, "Fake Band", root_folder="/music", quality_profile_id=99, metadata_profile_id=1, tag_ids=[]
        )
    assert not isinstance(caught.value, LidarrMetadataError)


# ---------------------------------------------------------------------------- refresh polling


@respx.mock
def test_refresh_artist_polls_until_completed(lidarr: LidarrClient, clock: FakeClock) -> None:
    command = respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 99, "status": "queued"}))
    status = respx.get(f"{V1}/command/99").mock(
        side_effect=[
            httpx.Response(200, json={"id": 99, "status": "queued"}),
            httpx.Response(200, json={"id": 99, "status": "started"}),
            httpx.Response(200, json={"id": 99, "status": "completed"}),
        ]
    )
    lidarr.refresh_artist(ARTIST, timeout_s=300)

    assert body_of(command) == {"name": "RefreshArtist", "artistId": 7, "isNewArtist": True}
    assert status.call_count == 3
    assert clock.slept == [2.0, 2.0]


@respx.mock
def test_refresh_artist_raises_on_failure_with_the_message(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 99}))
    respx.get(f"{V1}/command/99").mock(
        return_value=httpx.Response(200, json={"id": 99, "status": "failed", "message": "skyhook unavailable"})
    )
    with pytest.raises(LidarrMetadataError, match="skyhook unavailable"):
        lidarr.refresh_artist(ARTIST)


@respx.mock
@pytest.mark.parametrize("state", ["aborted", "cancelled"])
def test_refresh_artist_raises_on_other_terminal_states(lidarr: LidarrClient, state: str) -> None:
    respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 99}))
    respx.get(f"{V1}/command/99").mock(return_value=httpx.Response(200, json={"id": 99, "status": state}))
    with pytest.raises(LidarrMetadataError, match=state):
        lidarr.refresh_artist(ARTIST)


@respx.mock
def test_refresh_artist_times_out(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 99}))
    respx.get(f"{V1}/command/99").mock(return_value=httpx.Response(200, json={"id": 99, "status": "started"}))
    with pytest.raises(LidarrMetadataError, match="did not finish within"):
        lidarr.refresh_artist(ARTIST, timeout_s=6)


@respx.mock
def test_refresh_artist_needs_a_command_id(lidarr: LidarrClient) -> None:
    respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"status": "queued"}))
    with pytest.raises(LidarrError, match="no command id"):
        lidarr.refresh_artist(ARTIST)


# ---------------------------------------------------------------------------- monitoring writes


@respx.mock
def test_set_albums_monitored_batches(lidarr: LidarrClient) -> None:
    from likearr.adapters.lidarr import BATCH_SIZE

    route = respx.put(f"{V1}/album/monitor").mock(return_value=httpx.Response(202, json={}))
    n = BATCH_SIZE * 2 + BATCH_SIZE // 2
    lidarr.set_albums_monitored(list(range(n)), True)

    assert route.call_count == 3
    assert body_of(route, 0) == {"albumIds": list(range(BATCH_SIZE)), "monitored": True}
    assert body_of(route, 2) == {"albumIds": list(range(BATCH_SIZE * 2, n)), "monitored": True}


@respx.mock
def test_set_albums_monitored_does_nothing_for_an_empty_list(lidarr: LidarrClient) -> None:
    route = respx.put(f"{V1}/album/monitor").mock(return_value=httpx.Response(202, json={}))
    lidarr.set_albums_monitored([], False)
    assert route.call_count == 0


@respx.mock
def test_set_artist_profile(lidarr: LidarrClient) -> None:
    route = respx.put(f"{V1}/artist/editor").mock(return_value=httpx.Response(202, json=[]))
    lidarr.set_artist_profile(ARTIST, 5)
    assert body_of(route) == {"artistIds": [7], "metadataProfileId": 5}


@respx.mock
def test_set_artists_new_items_none_batches(lidarr: LidarrClient) -> None:
    route = respx.put(f"{V1}/artist/editor").mock(return_value=httpx.Response(202, json=[]))
    from likearr.adapters.lidarr import BATCH_SIZE

    n = BATCH_SIZE + BATCH_SIZE // 2
    lidarr.set_artists_new_items_none(list(range(n)))
    assert route.call_count == 2
    assert body_of(route, 0)["monitorNewItems"] == "none"
    assert body_of(route, 1)["artistIds"] == list(range(BATCH_SIZE, n))


# ---------------------------------------------------------------------------- tags and profiles


@respx.mock
def test_ensure_tag_returns_an_existing_tag(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/tag").mock(return_value=httpx.Response(200, json=[{"id": 3, "label": "likearr"}]))
    create = respx.post(f"{V1}/tag").mock(return_value=httpx.Response(201, json={"id": 9}))
    assert lidarr.ensure_tag("likearr") == 3
    assert create.call_count == 0


@respx.mock
def test_ensure_tag_creates_a_missing_tag(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/tag").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{V1}/tag").mock(return_value=httpx.Response(201, json={"id": 9, "label": "likearr"}))
    assert lidarr.ensure_tag("likearr") == 9
    assert body_of(create) == {"label": "likearr"}


def _schema() -> dict:
    return {
        "id": 0,
        "name": "",
        "primaryAlbumTypes": [
            {"albumType": {"id": 1, "name": "Album"}, "allowed": False},
            {"albumType": {"id": 2, "name": "Single"}, "allowed": False},
            {"albumType": {"id": 3, "name": "EP"}, "allowed": False},
            {"albumType": {"id": 4, "name": "Broadcast"}, "allowed": False},
            {"albumType": {"id": 5, "name": "Other"}, "allowed": False},
        ],
        "secondaryAlbumTypes": [
            {"albumType": {"id": 0, "name": "Studio"}, "allowed": False},
            {"albumType": {"id": 1, "name": "Compilation"}, "allowed": False},
            {"albumType": {"id": 2, "name": "Soundtrack"}, "allowed": False},
            {"albumType": {"id": 6, "name": "Live"}, "allowed": False},
            {"albumType": {"id": 8, "name": "Remix"}, "allowed": False},
            {"albumType": {"id": 9, "name": "DJ-mix"}, "allowed": False},
            {"albumType": {"id": 10, "name": "Mixtape/Street"}, "allowed": False},
            {"albumType": {"id": 11, "name": "Demo"}, "allowed": False},
        ],
        "releaseStatuses": [
            {"releaseStatus": {"id": 1, "name": "Official"}, "allowed": False},
            {"releaseStatus": {"id": 2, "name": "Promotion"}, "allowed": False},
            {"releaseStatus": {"id": 3, "name": "Bootleg"}, "allowed": False},
        ],
    }


def _allowed(entries: list[dict], key: str) -> set[str]:
    return {e[key]["name"] for e in entries if e["allowed"]}


@respx.mock
def test_ensure_metadata_profile_returns_an_existing_profile(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[{"id": 4, "name": "Lean"}]))
    create = respx.post(f"{V1}/metadataprofile").mock(return_value=httpx.Response(201, json={"id": 9}))
    assert lidarr.ensure_metadata_profile(Profile.LEAN, "Lean") == 4
    assert create.call_count == 0


@respx.mock
def test_ensure_metadata_profile_creates_lean(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/metadataprofile/schema").mock(return_value=httpx.Response(200, json=_schema()))
    create = respx.post(f"{V1}/metadataprofile").mock(return_value=httpx.Response(201, json={"id": 11}))

    assert lidarr.ensure_metadata_profile(Profile.LEAN, "Lean") == 11
    body = body_of(create)
    assert body["name"] == "Lean"
    assert _allowed(body["primaryAlbumTypes"], "albumType") == {"Album", "EP"}
    assert _allowed(body["secondaryAlbumTypes"], "albumType") == {"Studio"}
    assert _allowed(body["releaseStatuses"], "releaseStatus") == {"Official"}


@respx.mock
def test_ensure_metadata_profile_creates_full_without_remix_or_djmix(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/metadataprofile/schema").mock(return_value=httpx.Response(200, json=_schema()))
    create = respx.post(f"{V1}/metadataprofile").mock(return_value=httpx.Response(201, json={"id": 12}))

    assert lidarr.ensure_metadata_profile(Profile.FULL, "Full") == 12
    body = body_of(create)
    assert _allowed(body["primaryAlbumTypes"], "albumType") == {"Album", "EP", "Single"}
    secondary = _allowed(body["secondaryAlbumTypes"], "albumType")
    assert secondary == {"Studio", "Compilation", "Soundtrack", "Live"}
    assert not secondary & {"Remix", "DJ-mix", "Mixtape/Street", "Demo"}


# ---------------------------------------------------------------------------- root folder defaults


@respx.mock
def test_set_root_folder_defaults(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/rootfolder").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "path": "/music",
                    "defaultMonitorOption": "all",
                    "defaultNewItemMonitorOption": "all",
                    "defaultQualityProfileId": 2,
                    "defaultMetadataProfileId": 1,
                }
            ],
        )
    )
    route = respx.put(f"{V1}/rootfolder/1").mock(return_value=httpx.Response(202, json={}))
    lidarr.set_root_folder_defaults("/music")

    body = body_of(route)
    assert body["defaultMonitorOption"] == "none"
    assert body["defaultNewItemMonitorOption"] == "none"
    assert body["path"] == "/music"
    assert body["defaultQualityProfileId"] == 2


@respx.mock
def test_set_root_folder_defaults_rejects_an_unknown_path(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/rootfolder").mock(return_value=httpx.Response(200, json=[{"id": 1, "path": "/music"}]))
    with pytest.raises(LidarrError, match="no root folder"):
        lidarr.set_root_folder_defaults("/elsewhere")


# ---------------------------------------------------------------------------- shape guards


@respx.mock
def test_a_non_array_listing_is_an_error(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/tag").mock(return_value=httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(LidarrError, match="expected a JSON array"):
        lidarr.ensure_tag("likearr")


@respx.mock
def test_a_non_json_body_is_an_error(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/system/status").mock(return_value=httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(LidarrError, match="not valid JSON"):
        lidarr.version()


# ---------------------------------------------------------------------------- port conformance


def test_client_satisfies_lidarr_port(lidarr: LidarrClient) -> None:
    """Statically checked by pyright: the shell must be able to take this as a LidarrPort."""
    port: LidarrPort = lidarr
    assert callable(port.load_view)


# ---------------------------------------------------------------------------- shell-only endpoints
#
# track_files, add_root_folder, delete_artist and rescan_artist are not part of `LidarrPort`;
# they exist for `prune-stage` and `setup-profiles`. See `LidarrShell` in likearr/shell/context.py.


@respx.mock
def test_track_files_is_always_filtered_by_album(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/trackfile").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/music/Fake Band/Album/01.flac", "size": 123}])
    )
    files = lidarr.track_files(42)

    assert route.calls[0].request.url.params["albumId"] == "42"
    assert files == [{"id": 1, "path": "/music/Fake Band/Album/01.flac", "size": 123}]


@respx.mock
def test_track_files_of_an_album_with_nothing_on_disk(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/trackfile").mock(return_value=httpx.Response(200, json=[]))
    assert lidarr.track_files(42) == []


@respx.mock
def test_add_root_folder_creates_one_defaulting_to_monitor_none(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/rootfolder").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{V1}/qualityprofile").mock(return_value=httpx.Response(200, json=[{"id": 3, "name": "Any"}]))
    respx.get(f"{V1}/metadataprofile").mock(return_value=httpx.Response(200, json=[{"id": 5, "name": "Standard"}]))
    route = respx.post(f"{V1}/rootfolder").mock(return_value=httpx.Response(201, json={"id": 1, "path": "/music"}))

    created = lidarr.add_root_folder("/music")

    body = body_of(route)
    assert created["path"] == "/music"
    assert body["path"] == "/music"
    assert body["defaultMonitorOption"] == "none"
    assert body["defaultNewItemMonitorOption"] == "none"
    assert body["defaultQualityProfileId"] == 3
    assert body["defaultMetadataProfileId"] == 5


@respx.mock
def test_add_root_folder_is_idempotent(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/rootfolder").mock(return_value=httpx.Response(200, json=[{"id": 1, "path": "/music/"}]))
    post = respx.post(f"{V1}/rootfolder").mock(return_value=httpx.Response(500))

    assert lidarr.add_root_folder("/music")["id"] == 1
    assert not post.called


@respx.mock
def test_delete_artist_never_deletes_files(lidarr: LidarrClient) -> None:
    route = respx.delete(f"{V1}/artist/7").mock(return_value=httpx.Response(200))
    lidarr.delete_artist(7)

    assert route.calls[0].request.url.params["deleteFiles"] == "false"
    assert route.calls[0].request.url.params["addImportListExclusion"] == "false"


@respx.mock
def test_delete_artist_refuses_delete_files(lidarr: LidarrClient) -> None:
    route = respx.delete(f"{V1}/artist/7").mock(return_value=httpx.Response(200))
    with pytest.raises(LidarrError, match="never deletes files"):
        lidarr.delete_artist(7, delete_files=True)
    assert not route.called, "the refusal must happen before the request"


@respx.mock
def test_rescan_artist_scans_only_the_artist_folder_without_polling(lidarr: LidarrClient) -> None:
    """Lidarr has no RescanArtist command; a RescanFolders without folders walks every root."""
    respx.get(f"{V1}/artist/7").mock(return_value=httpx.Response(200, json={"id": 7, "path": "/music/Seven"}))
    route = respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 9, "status": "queued"}))
    lidarr.rescan_artist(7)

    assert body_of(route) == {
        "name": "RescanFolders",
        "folders": ["/music/Seven"],
        "addNewArtists": False,
        "artistIds": [7],
    }


@respx.mock
def test_rescan_artist_refuses_without_a_path(lidarr: LidarrClient) -> None:
    respx.get(f"{V1}/artist/7").mock(return_value=httpx.Response(200, json={"id": 7, "path": ""}))
    route = respx.post(f"{V1}/command").mock(return_value=httpx.Response(201, json={"id": 9}))
    with pytest.raises(LidarrError, match="root-wide"):
        lidarr.rescan_artist(7)
    assert not route.called


@respx.mock
def test_artist_track_file_records_counts_the_records_not_the_statistic(lidarr: LidarrClient) -> None:
    route = respx.get(f"{V1}/trackfile", params={"artistId": "7"}).mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/m/a.flac"}, {"id": 2, "path": "/m/b.cue.flac"}])
    )
    statistic = respx.get(f"{V1}/artist/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "statistics": {"trackFileCount": 118}})
    )

    assert lidarr.artist_track_file_records(7) == 2
    assert route.called and not statistic.called


@respx.mock
def test_add_artist_re_monitors_the_artist_when_lidarr_unmonitors_it(lidarr: LidarrClient) -> None:
    """`addOptions.monitor: none` makes Lidarr return the artist with monitored=false; fix it up."""
    created = {
        "id": 42,
        "foreignArtistId": "artist-mbid",
        "artistName": "A",
        "monitored": False,
        "monitorNewItems": "none",
        "metadataProfileId": 3,
        "qualityProfileId": 5,
        "tags": [],
        "path": "/m/A",
    }
    respx.post(f"{V1}/artist").mock(return_value=httpx.Response(201, json=created))
    editor = respx.put(f"{V1}/artist/editor").mock(return_value=httpx.Response(202, json=[]))
    artist = lidarr.add_artist(
        "artist-mbid", "A", root_folder="/m", quality_profile_id=5, metadata_profile_id=3, tag_ids=[]
    )
    assert artist.monitored is True
    assert editor.call_count == 1
    assert body_of(editor, 0) == {"artistIds": [42], "monitored": True}


@respx.mock
def test_set_artists_monitored_batches(lidarr: LidarrClient) -> None:
    from likearr.adapters.lidarr import BATCH_SIZE

    route = respx.put(f"{V1}/artist/editor").mock(return_value=httpx.Response(202, json=[]))
    n = BATCH_SIZE + 5
    lidarr.set_artists_monitored(list(range(n)))
    assert route.call_count == 2
    assert body_of(route, 0) == {"artistIds": list(range(BATCH_SIZE)), "monitored": True}
    assert body_of(route, 1) == {"artistIds": list(range(BATCH_SIZE, n)), "monitored": True}


def test_likearr_never_posts_a_lidarr_search_command() -> None:
    """likearr monitors; it never searches (#146). A grep, not a claim: Lidarr's own AlbumSearch,
    MissingAlbumSearch and ArtistSearch command names must never appear anywhere under `likearr/`.
    The only command names posted today are RefreshArtist (`refresh_artist`, above) and
    RescanFolders (`rescan_artist`), and `add_artist`'s `addOptions.searchForMissingAlbums` is
    always `False`.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "likearr"
    hits = [
        f"{path.relative_to(root)}: {name}"
        for path in root.rglob("*.py")
        for name in ("AlbumSearch", "MissingAlbumSearch", "ArtistSearch")
        if name in path.read_text()
    ]
    assert hits == []


# ---------------------------------------------------------------------------- redirects (#171)


def _redirecting_lidarr(
    url: str, answer: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request]
) -> LidarrClient:
    """A LidarrClient on the kind of client `build_context` builds for Lidarr, over a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    client = build_client(transport=httpx.MockTransport(handler), pinned_origin=url)
    config = LidarrConfig(url=url, root_folder="/music", quality_profile="Standard")
    return LidarrClient(config, client, api_key=FAKE_API_KEY, sleep=FakeClock().sleep)


@pytest.mark.parametrize(
    ("location", "origin"),
    [
        ("https://sso.other-origin.test/login?rd=x&token=secret-token-value#frag", "https://sso.other-origin.test"),
        ("//sso.other-origin.test:9091/login?token=secret-token-value", "http://sso.other-origin.test:9091"),
        ("https://user:pw-secret@sso.other-origin.test/login", "https://sso.other-origin.test"),
    ],
)
def test_a_redirect_to_another_host_is_refused_and_the_key_never_leaves(location: str, origin: str) -> None:
    """The audit's probe: a 302 to a login portal used to take X-Api-Key along with it."""
    seen: list[httpx.Request] = []
    lidarr = _redirecting_lidarr(
        "http://lidarr.example.test:8686", lambda _: httpx.Response(302, headers={"Location": location}), seen
    )

    with pytest.raises(LidarrError) as excinfo:
        lidarr.version()

    message = str(excinfo.value)
    assert message == (
        f"Lidarr at http://lidarr.example.test:8686 redirected to {origin}; set LIKEARR_LIDARR_URL to Lidarr "
        "itself (for example the container address), not a login page"
    )
    assert [r.url.host for r in seen] == ["lidarr.example.test"]


def test_a_refused_redirect_is_not_retried_and_not_a_metadata_outage() -> None:
    seen: list[httpx.Request] = []
    lidarr = _redirecting_lidarr(
        "http://lidarr.example.test:8686",
        lambda _: httpx.Response(307, headers={"Location": "https://sso.other-origin.test/"}),
        seen,
    )

    with pytest.raises(LidarrError) as excinfo:
        lidarr.lookup_release_group("00000000-0000-4000-8000-000000000001")

    assert not isinstance(excinfo.value, LidarrMetadataError)
    assert len(seen) == 1


def test_a_redirect_to_a_malformed_idna_host_is_a_lidarr_error_not_a_crash() -> None:
    seen: list[httpx.Request] = []
    lidarr = _redirecting_lidarr(
        "http://lidarr.example.test:8686",
        lambda _: httpx.Response(302, headers={"Location": "http://xn--lidarr-.lan/x"}),
        seen,
    )

    with pytest.raises(LidarrError, match=r"^lidarr GET /system/status: IDNAError: "):
        lidarr.version()
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("url", "location"),
    [
        ("https://lidarr.example.test", "http://lidarr.example.test/api/v1/system/status"),  # downgrade
        ("http://lidarr.example.test:8686", "http://lidarr.example.test:9999/api/v1/system/status"),  # port
        ("http://lidarr.example.test:8686", "https://lidarr.example.test:8686/api/v1/system/status"),  # not 80->443
    ],
)
def test_a_redirect_to_another_origin_on_the_same_host_is_refused(url: str, location: str) -> None:
    seen: list[httpx.Request] = []
    lidarr = _redirecting_lidarr(url, lambda _: httpx.Response(301, headers={"Location": location}), seen)

    with pytest.raises(LidarrError, match="redirected to"):
        lidarr.version()
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("url", "location", "followed_to"),
    [
        (
            "http://lidarr.example.test",
            "https://lidarr.example.test/api/v1/system/status",
            "https://lidarr.example.test/api/v1/system/status",
        ),
        (
            "http://lidarr.example.test:8686",
            "/lidarr/api/v1/system/status",
            "http://lidarr.example.test:8686/lidarr/api/v1/system/status",
        ),
        (
            "http://lidarr.example.test:8686",
            "http://LIDARR.example.test:8686/lidarr/api/v1/system/status",
            "http://lidarr.example.test:8686/lidarr/api/v1/system/status",
        ),
    ],
)
def test_a_same_origin_redirect_or_https_upgrade_is_still_followed(url: str, location: str, followed_to: str) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if len(seen) == 1:
            return httpx.Response(301, headers={"Location": location})
        return httpx.Response(200, json={"version": "3.1.0.4875"})

    lidarr = _redirecting_lidarr(url, answer, seen)

    assert lidarr.version() == "3.1.0.4875"
    assert str(seen[1].url) == followed_to
    assert seen[1].headers["X-Api-Key"] == FAKE_API_KEY


def test_a_redirected_write_is_refused_and_not_mistaken_for_an_artist_error() -> None:
    """A 307 re-sends the POST body along with the key; the add must stop at the first hop."""
    seen: list[httpx.Request] = []
    lidarr = _redirecting_lidarr(
        "http://lidarr.example.test:8686",
        lambda _: httpx.Response(307, headers={"Location": "https://sso.other-origin.test/api/v1/artist"}),
        seen,
    )

    with pytest.raises(LidarrError, match=r"redirected to https://sso\.other-origin\.test;") as excinfo:
        lidarr.add_artist(
            "00000000-0000-4000-8000-000000000002",
            "Someone",
            root_folder="/music",
            quality_profile_id=1,
            metadata_profile_id=1,
            tag_ids=(),
        )

    assert not isinstance(excinfo.value, (LidarrMetadataError, LidarrArtistUnknown))
    assert [(r.method, r.url.host) for r in seen] == [("POST", "lidarr.example.test")]
