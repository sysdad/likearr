"""Tests for the prune report, with the protection rule as the centrepiece."""

from __future__ import annotations

import pytest

from likearr.core.desire import CATALOGUE_UNREAD_STEPS
from likearr.core.prune import Protection, build_prune_report, split_track_key
from likearr.core.resolver import METADATA_ERROR_STEP
from likearr.models import (
    ArtistResolution,
    PrimaryType,
    ReasonKind,
    Resolution,
    ResolutionStatus,
    SecondaryType,
)
from tests.unit.fakes import (
    NOW,
    album_intent,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    owned,
    reason,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)
from tests.unit.test_diff import desired_state

ARTIST = "artist-1"
SAVED = reason(ReasonKind.SAVED, "al1")
LIKED = reason(ReasonKind.LIKED, "t1")

SINGLE = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, released="2019-11-29")
ALBUM = rg("rg-album", "After Hours", released="2020-03-20")


def _view(*albums):
    return lidarr_view(artists=[lidarr_artist(ARTIST, path="/music/Test Artist")], albums=list(albums))


def _resolution(status=ResolutionStatus.RESOLVED, *, target=ALBUM, source=SINGLE, key="liked:t1"):
    return Resolution(
        intent_key=key,
        status=status,
        release_group=target if status == ResolutionStatus.RESOLVED else None,
        step="track:isrc->album",
        detail="test",
        single_release_group=source if status == ResolutionStatus.PENDING_ALBUM else None,
        source_release_group=source,
    )


# --------------------------------------------------------------------------- candidates


def test_an_album_with_files_that_nothing_wants_is_a_candidate() -> None:
    album = rg("rg-1", "Record")
    view = _view(lidarr_album(album, files=10, size=500))
    report = build_prune_report(desired_state(), view, {}, [], now=NOW)
    assert [r.rg_mbid for r in report.candidates] == ["rg-1"]
    assert report.total_bytes == 500
    assert report.by_artist == {"Test Artist": 1}
    assert report.bytes_by_artist == {"Test Artist": 500}
    assert report.candidates[0].path == "/music/Test Artist"


def test_a_row_says_whether_its_artist_is_followed_on_spotify() -> None:
    """From the desired state `run` builds - the same read, no new Spotify call."""
    compilation = rg("rg-1", "Greatest Hits", secondary=frozenset({SecondaryType.COMPILATION}))
    view = _view(lidarr_album(compilation, files=10))
    followed = desired_state(followed={ARTIST})

    assert build_prune_report(followed, view, {}, [], now=NOW).candidates[0].artist_followed is True
    assert build_prune_report(desired_state(), view, {}, [], now=NOW).candidates[0].artist_followed is False


def test_when_follows_are_not_read_nobody_can_say_who_is_followed() -> None:
    view = _view(lidarr_album(rg("rg-1", "Record"), files=10))

    row = build_prune_report(desired_state(), view, {}, [], now=NOW, followed_read=False).candidates[0]

    assert row.artist_followed is None and row.follow_unmatched is False


def test_a_spotify_follow_that_never_matched_is_flagged_by_name() -> None:
    view = _view(lidarr_album(rg("rg-1", "Record"), files=10))

    row = build_prune_report(
        desired_state(), view, {}, [], now=NOW, unmatched_follows=frozenset({"test artist"})
    ).candidates[0]

    assert row.artist_followed is False and row.follow_unmatched is True


def test_an_album_with_no_files_is_not_a_candidate() -> None:
    album = rg("rg-1", "Record")
    report = build_prune_report(desired_state(), _view(lidarr_album(album, files=0)), {}, [], now=NOW)
    assert not report.candidates


def test_a_wanted_album_is_not_a_candidate() -> None:
    album = rg("rg-1", "Record")
    view = _view(lidarr_album(album, files=10))
    report = build_prune_report(desired_state((album, [SAVED])), view, {}, [], now=NOW)
    assert not report.candidates


def test_an_owned_album_is_not_a_candidate() -> None:
    """Unmonitoring an owned release is a monitoring decision, not a deletion decision."""
    album = rg("rg-1", "Record")
    key, record = owned(album, SAVED)
    report = build_prune_report(desired_state(), _view(lidarr_album(album, files=10)), {key: record}, [], now=NOW)
    assert not report.candidates


def test_rows_carry_the_lidarr_and_musicbrainz_identity() -> None:
    album = rg("rg-1", "Record", secondary=[SecondaryType.LIVE], released="1999-05-04")
    view = _view(lidarr_album(album, id=321, files=3, size=42))
    row = build_prune_report(desired_state(), view, {}, [], now=NOW).candidates[0]
    assert row.artist_mbid == ARTIST
    assert row.artist_name == "Test Artist"
    assert row.lidarr_artist_id == 1
    assert row.lidarr_album_id == 321
    assert row.title == "Record"
    assert row.primary_type is PrimaryType.ALBUM
    assert row.secondary_types == frozenset({SecondaryType.LIVE})
    assert row.release_date is not None
    assert row.release_date.isoformat() == "1999-05-04"
    assert row.track_file_count == 3
    assert row.size_on_disk == 42


# --------------------------------------------------------------------------- the protection rule


def test_the_single_a_liked_track_came_from_is_protected_while_its_album_has_no_files() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=0),
    )
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [_resolution()], now=NOW)
    assert not report.candidates
    assert [r.rg_mbid for r in report.protected] == ["rg-single"]
    assert report.protected[0].protected_reason is not None
    assert "only copy" in report.protected[0].protected_reason
    assert report.total_bytes == 0


def test_the_single_stops_being_protected_once_the_album_is_downloaded() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=14),
    )
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [_resolution()], now=NOW)
    assert [r.rg_mbid for r in report.candidates] == ["rg-single"]
    assert not report.protected


def test_a_pending_tracks_single_is_always_protected() -> None:
    """A pending track resolved to nothing, so the single is definitionally the only copy."""
    view = _view(lidarr_album(SINGLE, id=101, files=1, size=10))
    pending = _resolution(ResolutionStatus.PENDING_ALBUM)
    report = build_prune_report(desired_state(), view, {}, [pending], now=NOW)
    assert not report.candidates
    assert [r.rg_mbid for r in report.protected] == ["rg-single"]
    assert report.protected[0].protected_reason is not None
    assert "waiting for an album" in report.protected[0].protected_reason


def test_a_saved_albums_resolution_does_not_protect_anything() -> None:
    """Protection is about liked and playlist tracks; a saved album is its own target."""
    saved = Resolution(
        intent_key="saved:al1",
        status=ResolutionStatus.RESOLVED,
        release_group=SINGLE,
        step="album:upc",
        source_release_group=SINGLE,
    )
    view = _view(lidarr_album(SINGLE, id=101, files=1, size=10))
    report = build_prune_report(desired_state(), view, {}, [saved], now=NOW)
    assert [r.rg_mbid for r in report.candidates] == ["rg-single"]


def test_a_playlist_tracks_single_is_protected_like_a_liked_one() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=0),
    )
    listed = _resolution(key="playlist:pl-1:t1")
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [listed], now=NOW)
    assert [r.rg_mbid for r in report.protected] == ["rg-single"]


def test_a_release_that_is_its_own_resolution_target_follows_the_ordinary_rules() -> None:
    """A liked track resolved straight to its album protects nothing; the album is just wanted."""
    direct = Resolution(
        intent_key="liked:t1",
        status=ResolutionStatus.RESOLVED,
        release_group=ALBUM,
        step="track:album",
        source_release_group=ALBUM,
    )
    view = _view(lidarr_album(ALBUM, id=102, files=14, size=99))
    report = build_prune_report(desired_state(), view, {}, [direct], now=NOW)
    assert [r.rg_mbid for r in report.candidates] == ["rg-album"]


def test_protection_survives_the_album_being_absent_from_lidarr_entirely() -> None:
    view = _view(lidarr_album(SINGLE, id=101, files=1, size=10))
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [_resolution()], now=NOW)
    assert not report.candidates
    assert [r.rg_mbid for r in report.protected] == ["rg-single"]


# --------------------------------------------------------------------------- why, as data

THINK = track_intent("Think", spotify_album("Aretha Now"), spotify_id="t1", artists=("Aretha Franklin",))


def test_a_protected_row_says_why_as_data_with_the_songs_title() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=0),
    )
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [_resolution()], now=NOW, tracks=[THINK])

    assert report.protected[0].protection == Protection(
        kind="album_not_downloaded",
        intent_key="liked:t1",
        song="Think",
        song_artists=("Aretha Franklin",),
        album="After Hours",
        album_mbid="rg-album",
    )
    assert Protection("pending_album", "liked:t1").source == "liked"
    # The terminal's line is what it always was.
    assert report.protected[0].protected_reason == (
        "holds a liked track (liked:t1) whose album 'After Hours' (rg-album) has no files yet; "
        "this is the only copy on disk"
    )
    assert report.candidates == []


def test_a_candidate_carries_no_protection() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=14),  # downloaded: the single is not the only copy
    )
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [_resolution()], now=NOW, tracks=[THINK])
    assert [(r.rg_mbid, r.protection, r.protected_reason) for r in report.candidates] == [("rg-single", None, None)]


def test_a_pending_songs_protection_names_no_album() -> None:
    view = _view(lidarr_album(SINGLE, id=101, files=1, size=10))
    report = build_prune_report(
        desired_state(), view, {}, [_resolution(ResolutionStatus.PENDING_ALBUM)], now=NOW, tracks=[THINK]
    )

    protection = report.protected[0].protection
    assert protection is not None
    assert (protection.kind, protection.song, protection.album, protection.album_mbid) == (
        "pending_album",
        "Think",
        "",
        "",
    )
    assert "waiting for an album" in (report.protected[0].protected_reason or "")


def test_a_playlist_tracks_protection_names_its_playlist_and_a_song_not_in_the_snapshot_is_untitled() -> None:
    view = _view(
        lidarr_album(SINGLE, id=101, files=1, size=10),
        lidarr_album(ALBUM, id=102, monitored=True, files=0),
    )
    listed = _resolution(key="playlist:pl-1:t9")
    report = build_prune_report(desired_state((ALBUM, [LIKED])), view, {}, [listed], now=NOW, tracks=[THINK])

    protection = report.protected[0].protection
    assert protection is not None
    assert (protection.source, protection.playlist_id, protection.song) == ("playlist", "pl-1", "")
    assert protection.to_dict() == {
        "kind": "album_not_downloaded",
        "intent_key": "playlist:pl-1:t9",
        "source": "playlist",
        "playlist_id": "pl-1",
        "track_id": "t9",
        "song": None,
        "song_artists": [],
        "album": "After Hours",
        "album_mbid": "rg-album",
    }


def test_an_intent_key_splits_into_its_source_playlist_and_track() -> None:
    assert split_track_key("liked:t1") == ("liked", "", "t1")
    assert split_track_key("playlist:pl1:t1") == ("playlist", "pl1", "t1")
    for other in ("saved:al1", "followed:a1", "liked:", "playlist:pl1", "playlist::t1", "liked:a:b", "", "x"):
        assert split_track_key(other) is None, other


def test_the_report_is_deterministic() -> None:
    groups = [rg(f"rg-{i}", f"Record {i}") for i in (2, 0, 1)]
    view = _view(*[lidarr_album(g, id=100 + i, files=1, size=i) for i, g in enumerate(groups)])
    a = build_prune_report(desired_state(), view, {}, [], now=NOW)
    b = build_prune_report(desired_state(), view, {}, [], now=NOW)
    assert [r.rg_mbid for r in a.candidates] == ["rg-0", "rg-1", "rg-2"]
    assert a.candidates == b.candidates


def test_totals_add_up_across_several_artists() -> None:
    one = rg("rg-1", "One", artist_mbid="a1", artist_name="One Artist")
    two = rg("rg-2", "Two", artist_mbid="a2", artist_name="Two Artist")
    three = rg("rg-3", "Three", artist_mbid="a2", artist_name="Two Artist")
    view = lidarr_view(
        artists=[lidarr_artist("a1", id=1, name="One Artist"), lidarr_artist("a2", id=2, name="Two Artist")],
        albums=[
            lidarr_album(one, id=101, files=1, size=100),
            lidarr_album(two, id=102, files=1, size=200),
            lidarr_album(three, id=103, files=1, size=300),
        ],
    )
    report = build_prune_report(desired_state(), view, {}, [], now=NOW)
    assert report.total_candidates == 3
    assert report.by_artist == {"One Artist": 1, "Two Artist": 2}
    assert report.bytes_by_artist == {"One Artist": 100, "Two Artist": 500}
    assert report.total_bytes == 600


# --------------------------------------------------------------------------- unread catalogues


def _unread(step: str = METADATA_ERROR_STEP) -> ArtistResolution:
    return ArtistResolution(
        intent_key="followed:sp-1",
        status=ResolutionStatus.UNMAPPED,
        artist_mbid=ARTIST,
        artist_name="Test Artist",
        step=step,
    )


@pytest.mark.parametrize("step", sorted(CATALOGUE_UNREAD_STEPS))
def test_a_followed_artist_whose_catalogue_was_not_read_keeps_their_studio_albums_and_eps(step: str) -> None:
    studio, ep = rg("rg-1", "Record"), rg("rg-2", "Short", primary=PrimaryType.EP)
    live = rg("rg-3", "Live Record", secondary=[SecondaryType.LIVE])
    view = _view(lidarr_album(studio, files=10), lidarr_album(ep, files=4), lidarr_album(live, files=8))
    desired = desired_state(followed={ARTIST}, unmapped=[_unread(step)])

    report = build_prune_report(desired, view, {}, [], now=NOW)

    assert [r.rg_mbid for r in report.protected] == ["rg-1", "rg-2"]
    assert {r.protection for r in report.protected} == {Protection(kind="catalogue_unread", intent_key="followed:sp-1")}
    assert "catalogue could not be read" in (report.protected[0].protected_reason or "")
    assert [r.rg_mbid for r in report.candidates] == ["rg-3"], "a follow never brings a live album"


def test_an_artist_unmapped_for_another_reason_is_not_protected() -> None:
    view = _view(lidarr_album(rg("rg-1", "Record"), files=10))
    desired = desired_state(followed={ARTIST}, unmapped=[_unread("artist:no-match")])

    report = build_prune_report(desired, view, {}, [], now=NOW)

    assert [r.rg_mbid for r in report.candidates] == ["rg-1"] and not report.protected


def test_a_song_s_only_copy_is_named_before_an_unread_catalogue() -> None:
    view = _view(lidarr_album(SINGLE, files=1), lidarr_album(ALBUM, files=0))
    desired = desired_state(followed={ARTIST}, unmapped=[_unread()])

    report = build_prune_report(desired, view, {}, [_resolution()], now=NOW)

    assert [r.protection.kind for r in report.protected if r.protection] == ["album_not_downloaded"]


# --------------------------------------------------------------------------- failed lookups


def _artist_failed(name: str = "Test Artist", step: str = METADATA_ERROR_STEP) -> ArtistResolution:
    return ArtistResolution(
        intent_key="followed:sp-9", status=ResolutionStatus.UNMAPPED, artist_mbid="", artist_name=name, step=step
    )


def _item_failed(intent_key: str, step: str = METADATA_ERROR_STEP) -> Resolution:
    return Resolution(intent_key=intent_key, status=ResolutionStatus.UNMAPPED, step=step)


def test_a_followed_artist_whose_own_lookup_failed_keeps_their_studio_albums_by_name() -> None:
    """No MusicBrainz id this run, so the Lidarr artist is matched by name, as adopt does; a live
    album and a compilation are not what a follow brings, and stay candidates."""
    studio, ep = rg("rg-1", "Record"), rg("rg-2", "Short", primary=PrimaryType.EP)
    live = rg("rg-3", "Live Record", secondary=[SecondaryType.LIVE])
    hits = rg("rg-4", "Greatest Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, name="The Test Artist")],
        albums=[lidarr_album(a, files=3) for a in (studio, ep, live, hits)],
    )
    desired = desired_state(unmapped=[_artist_failed()])

    report = build_prune_report(desired, view, {}, [], now=NOW)

    assert [r.rg_mbid for r in report.protected] == ["rg-1", "rg-2"]
    assert {r.protection for r in report.protected} == {Protection(kind="catalogue_unread", intent_key="followed:sp-9")}
    assert [r.rg_mbid for r in report.candidates] == ["rg-3", "rg-4"]


def test_a_followed_artist_mb_failed_during_keeps_their_albums_too() -> None:
    """Unmapped at a plain not-found step, but MusicBrainz failed during it (`lookup_failed`)."""
    view = _view(lidarr_album(rg("rg-1", "Record"), files=3))
    desired = desired_state(unmapped=[_artist_failed(step="artist:search")])

    report = build_prune_report(desired, view, {}, [], now=NOW, lookup_failed=frozenset({"followed:sp-9"}))

    assert [r.rg_mbid for r in report.protected] == ["rg-1"]


@pytest.mark.parametrize(
    ("name", "step"),
    [("Test Artist", "artist:search"), ("Someone Else", METADATA_ERROR_STEP)],
    ids=["lookup-succeeded-no-match", "another-name"],
)
def test_a_followed_artist_whose_lookup_did_not_fail_keeps_nothing(name: str, step: str) -> None:
    view = _view(lidarr_album(rg("rg-1", "Record"), files=3))
    desired = desired_state(unmapped=[_artist_failed(name, step)])

    report = build_prune_report(desired, view, {}, [], now=NOW)

    assert [r.rg_mbid for r in report.candidates] == ["rg-1"] and not report.protected


def test_a_liked_song_and_a_saved_album_whose_lookups_failed_keep_their_albums() -> None:
    """Matched by title and artist name as adopt does, whatever the album's type; an album nothing
    names stays a candidate."""
    liked = track_intent("Song", spotify_album("Record (Deluxe Edition)", spotify_id="sp-1"), spotify_id="sp-t")
    saved = album_intent(spotify_album("Live Record", spotify_id="sp-2"))
    view = _view(
        lidarr_album(rg("rg-1", "Record"), files=3),
        lidarr_album(rg("rg-2", "Live Record", secondary=[SecondaryType.LIVE]), files=3),
        lidarr_album(rg("rg-3", "Unnamed"), files=3),
    )
    desired = desired_state(unmapped=[_item_failed(liked.reason.key), _item_failed(saved.reason.key)])
    source = snapshot(albums=[saved], tracks=[liked])

    report = build_prune_report(desired, view, {}, [], now=NOW, tracks=source.tracks, snapshot=source)

    assert [(r.rg_mbid, r.protection.kind if r.protection else "") for r in report.protected] == [
        ("rg-1", "lookup_failed"),
        ("rg-2", "lookup_failed"),
    ]
    song = report.protected[0].protection
    assert song is not None and song.intent_key == liked.reason.key and song.song == "Song"
    assert song.this_run_only
    assert "could not be looked up" in (report.protected[1].protected_reason or "")
    assert [r.rg_mbid for r in report.candidates] == ["rg-3"]


def test_a_saved_album_mb_failed_during_keeps_its_album() -> None:
    saved = album_intent(spotify_album("Record", spotify_id="sp-1"))
    desired = desired_state(unmapped=[_item_failed(saved.reason.key, "search:no-match")])
    view = _view(lidarr_album(rg("rg-1", "Record"), files=3))

    report = build_prune_report(
        desired, view, {}, [], now=NOW, snapshot=snapshot(albums=[saved]), lookup_failed={saved.reason.key}
    )

    assert [r.rg_mbid for r in report.protected] == ["rg-1"]


@pytest.mark.parametrize(
    ("step", "artists", "title", "with_snapshot"),
    [
        ("search:no-match", ("Test Artist",), "Record", True),
        (METADATA_ERROR_STEP, ("Someone Else",), "Record", True),
        (METADATA_ERROR_STEP, ("Test Artist",), "Another Record", True),
        (METADATA_ERROR_STEP, ("Test Artist",), "Record", False),
    ],
    ids=["lookup-succeeded-no-match", "another-artist", "another-title", "no-snapshot"],
)
def test_a_lookup_that_did_not_fail_or_names_another_album_keeps_nothing(
    step: str, artists: tuple[str, ...], title: str, with_snapshot: bool
) -> None:
    saved = album_intent(spotify_album(title, spotify_id="sp-1", artists=artists))
    desired = desired_state(unmapped=[_item_failed(saved.reason.key, step)])
    view = _view(lidarr_album(rg("rg-1", "Record"), files=3))

    report = build_prune_report(
        desired, view, {}, [], now=NOW, snapshot=snapshot(albums=[saved]) if with_snapshot else None
    )

    assert [r.rg_mbid for r in report.candidates] == ["rg-1"] and not report.protected


def test_a_lookup_that_succeeded_leaves_the_report_as_it_was() -> None:
    """Control: a resolved song whose album is downloaded protects nothing new."""
    liked = track_intent("Song", spotify_album("Record", spotify_id="sp-1"), spotify_id="t1")
    view = _view(lidarr_album(rg("rg-1", "Record"), files=3))
    source = snapshot(tracks=[liked])

    report = build_prune_report(desired_state(), view, {}, [], now=NOW, tracks=source.tracks, snapshot=source)

    assert [r.rg_mbid for r in report.candidates] == ["rg-1"] and not report.protected
