"""Tests for `explain`: the reason chain a user reads when they disagree with the tool."""

from __future__ import annotations

import pytest

from likearr.core.explain import deniable, deny_note, explain
from likearr.models import (
    ArtistResolution,
    PrimaryType,
    Reason,
    ReasonKind,
    Resolution,
    ResolutionStatus,
)
from tests.unit.fakes import (
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    owned,
    reason,
    rg,
)
from tests.unit.test_diff import desired_state

ARTIST = "artist-1"
ALBUM = rg("rg-album", "After Hours", artist_name="The Weeknd", released="2020-03-20")
SINGLE = rg("rg-single", "Blinding Lights", artist_name="The Weeknd", primary=PrimaryType.SINGLE)
LIKED = reason(ReasonKind.LIKED, "t1")
FOLLOWED = reason(ReasonKind.FOLLOWED, "ar1")

RESOLUTION = Resolution(
    intent_key=LIKED.key,
    status=ResolutionStatus.RESOLVED,
    release_group=ALBUM,
    step="track:isrc->album",
    detail="ISRC USUG11904206 puts the song on studio Album 'After Hours'",
    source_release_group=SINGLE,
)


def _explain(query, *, desired=None, owned_map=None, view=None, resolutions=None, artists=None) -> str:
    return explain(
        query,
        desired=desired if desired is not None else desired_state((ALBUM, [LIKED])),
        owned=owned_map or {},
        view=view if view is not None else lidarr_view(),
        resolutions=resolutions if resolutions is not None else {LIKED.key: RESOLUTION},
        artist_resolutions=artists or {},
    )


def test_a_release_title_matches_and_prints_the_reason_chain() -> None:
    out = _explain("After Hours")
    assert "release: The Weeknd - After Hours (rg-album)" in out
    assert "wanted because:" in out
    assert "you liked a song - found as test [liked:t1]" in out
    assert "ISRC USUG11904206" in out


def test_matching_is_case_and_punctuation_insensitive() -> None:
    assert "After Hours" in _explain("after   hours!")
    assert "After Hours" in _explain("AFTER HOURS")


def test_a_release_group_mbid_matches_exactly() -> None:
    assert "After Hours" in _explain("rg-album")


def test_an_artist_name_matches_the_artist_and_their_releases() -> None:
    out = _explain("The Weeknd")
    assert "artist: The Weeknd (artist-1)" in out
    assert "release: The Weeknd - After Hours" in out


def test_lidarr_state_is_reported_for_a_known_album() -> None:
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, id=7, name="The Weeknd")],
        albums=[lidarr_album(ALBUM, id=55, monitored=True, files=14)],
    )
    assert "in Lidarr: monitored, album id 55, 14 file(s)" in _explain("After Hours", view=view)
    assert "in Lidarr: yes (id 7," in _explain("The Weeknd", view=view)


def test_an_album_lidarr_does_not_have_is_said_so() -> None:
    assert "no album with this release group" in _explain("After Hours")


def test_ownership_is_reported_both_ways() -> None:
    assert "owned by likearr: no" in _explain("After Hours")
    key, record = owned(ALBUM, LIKED, step="track:isrc->album")
    assert "owned by likearr: yes since" in _explain("After Hours", owned_map={key: record})


def test_a_followed_artist_says_how_many_releases_they_bring() -> None:
    desired = desired_state((ALBUM, [FOLLOWED]), followed={ARTIST}, followed_counts={ARTIST: 4})
    artists = {
        FOLLOWED.key: ArtistResolution(
            intent_key=FOLLOWED.key,
            status=ResolutionStatus.RESOLVED,
            artist_mbid=ARTIST,
            artist_name="The Weeknd",
            step="artist:search",
            detail="'The Weeknd' matched MusicBrainz artist 'The Weeknd' by name",
        )
    }
    out = _explain("The Weeknd", desired=desired, artists=artists)
    assert "followed on Spotify: yes - 4 studio album(s)/EP(s) wanted" in out
    assert "matched as a followed artist, by name" in out


def test_an_unfollowed_artist_says_so() -> None:
    assert "followed on Spotify: no" in _explain("The Weeknd")


def test_a_pending_track_explains_what_it_is_waiting_for() -> None:
    pending = Resolution(
        intent_key=LIKED.key,
        status=ResolutionStatus.PENDING_ALBUM,
        step="track:pending",
        detail="waiting for an album",
        single_release_group=SINGLE,
        single_release_date=SINGLE.first_release_date,
    )
    out = _explain("Blinding Lights", desired=desired_state(), resolutions={LIKED.key: pending})
    assert "pending album: liked:t1" in out
    assert "waiting on: 'Blinding Lights' (rg-single)" in out


def test_an_unmapped_intent_explains_why() -> None:
    unmapped = Resolution(
        intent_key=LIKED.key,
        status=ResolutionStatus.UNMAPPED,
        step="track:album:search",
        detail="no release group found for 'Nobody' - 'Nothing'",
    )
    out = _explain("Nobody", desired=desired_state(), resolutions={LIKED.key: unmapped})
    assert "unmapped: liked:t1" in out
    assert "looked for as: track:album:search" in out


def test_an_unmapped_artist_explains_why() -> None:
    unmapped = ArtistResolution(
        intent_key=FOLLOWED.key,
        status=ResolutionStatus.UNMAPPED,
        artist_name="Radioheads",
        step="artist:search",
        detail="closest MusicBrainz match is 'Radiohead', which is not an exact name match",
    )
    out = _explain("Radioheads", desired=desired_state(), resolutions={}, artists={FOLLOWED.key: unmapped})
    assert "unmapped: followed:ar1" in out
    assert "not an exact name match" in out


def test_an_owned_release_no_source_wants_any_more_is_explained() -> None:
    key, record = owned(ALBUM, LIKED, step="track:isrc->album")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, name="The Weeknd")],
        albums=[lidarr_album(ALBUM, monitored=True, files=1)],
    )
    out = _explain("After Hours", desired=desired_state(), owned_map={key: record}, view=view, resolutions={})
    assert "wanted because: nothing - no source asks for it any more" in out
    assert "you liked a song [lost]" in out


def test_a_manual_keep_says_it_will_never_be_unmonitored() -> None:
    key, record = owned(ALBUM, reason(ReasonKind.MANUAL, "adopt"), step="adopt:keep")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, name="The Weeknd")],
        albums=[lidarr_album(ALBUM, monitored=True)],
    )
    out = _explain("After Hours", desired=desired_state(), owned_map={key: record}, view=view, resolutions={})
    assert out.count("likearr never unmonitors it") == 2  # the summary and the detail agree


def test_no_match_gives_a_useful_message() -> None:
    out = _explain("Something That Is Not There", desired=desired_state(), resolutions={})
    assert "nothing matches" in out
    assert "release MBID" in out or "release group MBID" in out


def test_output_is_deterministic() -> None:
    assert _explain("The Weeknd") == _explain("The Weeknd")


def test_output_ends_with_exactly_one_newline() -> None:
    out = _explain("After Hours")
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_a_reason_recorded_earlier_but_no_longer_resolving_is_flagged() -> None:
    stale = reason(ReasonKind.SAVED, "gone")
    key, record = owned(ALBUM, LIKED, stale, step="track:isrc->album")
    out = _explain("After Hours", owned_map={key: record})
    assert "you saved an album - recorded earlier, no longer resolving here" in out


def test_the_resolver_step_is_always_visible() -> None:
    """A user disagreeing with the tool needs the step name to report a bug against."""
    key, record = owned(ALBUM, LIKED, step="track:isrc->album")
    out = _explain("After Hours", owned_map={key: record})
    assert "track:isrc->album" in out


# ---------------------------------------------------------------- the plain-language summary
#
# A real, public case: a liked "Busy Earnin'" by Jungle, the London modern soul collective, from their
# 2014 album "Jungle". likearr matched the song to "Jungle" (1969) by Jungle, a US psychedelic rock
# band, and the name-collision guard then skipped that artist because Lidarr already holds the
# London band (id 9003). Explain used to say "likearr would add it" and never said why nothing
# downloaded.

import datetime as _dt  # noqa: E402

from likearr.core.explain import explain_report, render_report  # noqa: E402
from likearr.models import ArtistIntent, NameCollision, SourceSnapshot, TrackIntent  # noqa: E402
from tests.unit.fakes import spotify_album  # noqa: E402

WANTED = "59074e0f-ede4-4ff1-bee2-cbfd3a273095"
EXISTING = "6bbb3983-ce8a-4971-96e0-7cae73268fc4"
JUNGLE_1969 = rg(
    "297a768d-1111-2222-3333-444444444444", "Jungle", artist_mbid=WANTED, artist_name="Jungle", released="1969-01-01"
)
BUSY = reason(ReasonKind.LIKED, "busy-earnin")
JUNGLE_RESOLUTION = Resolution(
    intent_key=BUSY.key,
    status=ResolutionStatus.RESOLVED,
    release_group=JUNGLE_1969,
    step="track:smallest:album",
    detail="Spotify filed 'Busy Earnin'' on album 'Jungle'; 1 candidate(s) considered",
)
JUNGLE_SNAPSHOT = SourceSnapshot(
    fetched_at=_dt.datetime(2026, 9, 23, tzinfo=_dt.UTC),
    artists=(),
    albums=(),
    tracks=(
        TrackIntent(
            spotify_id="busy-earnin",
            name="Busy Earnin'",
            isrc=None,
            artist_names=("Jungle",),
            album=spotify_album("Jungle", artists=("Jungle",), released="2014-07-14"),
            added_at=None,
            reason=BUSY,
        ),
    ),
    counts={"liked_tracks": 1},
)
JUNGLE_COLLISION = NameCollision(
    name="Jungle",
    wanted_mbid=WANTED,
    existing_mbid=EXISTING,
    existing_lidarr_id=9003,
    existing_name="Jungle",
    wanted_disambiguation="US psychedelic rock",
    existing_disambiguation="London modern soul collective",
    dropped_releases=1,
)


def _jungle_desired():
    desired = desired_state((JUNGLE_1969, [BUSY]))
    for release in desired.releases.values():
        release.steps[BUSY.key] = "track:smallest:album"
    return desired


def _jungle_inputs() -> dict[str, object]:
    return dict(
        desired=_jungle_desired(),
        owned={},
        view=lidarr_view(artists=[lidarr_artist(EXISTING, id=9003, name="Jungle")]),
        resolutions={BUSY.key: JUNGLE_RESOLUTION},
        artist_resolutions={},
        snapshot=JUNGLE_SNAPSHOT,
        collisions=[JUNGLE_COLLISION],
        disambiguation={WANTED: "US psychedelic rock", EXISTING: "London modern soul collective"}.get,
    )


def _jungle(**overrides: object):
    return explain_report("Busy Earnin'", **{**_jungle_inputs(), **overrides})  # type: ignore[arg-type]


def test_the_jungle_case_reads_as_what_happened_and_what_is_wrong() -> None:
    report = _jungle()

    (item,) = report.summary
    assert 'You liked "Busy Earnin\'" by Jungle (on Spotify: the album "Jungle", 2014).' in item.text
    assert 'likearr matched it to "Jungle" (1969) by Jungle (US psychedelic rock).' in item.text
    assert "Lidarr already has a different Jungle (London modern soul collective, Lidarr id 9003)" in item.text
    assert "can't hold two artists with the same name" in item.text
    assert "nothing was downloaded" in item.text
    assert item.wrong_match
    assert "looks like a wrong match" in item.text
    assert "2014" in item.text and "1969" in item.text
    assert (
        "the matched release on MusicBrainz",
        "https://musicbrainz.org/release-group/297a768d-1111-2222-3333-444444444444",
    ) in item.links
    assert ("the matched artist on MusicBrainz", f"https://musicbrainz.org/artist/{WANTED}") in item.links


def test_the_artist_is_never_said_to_be_added_when_the_collision_guard_blocks_it() -> None:
    report = explain_report(
        "Jungle",
        desired=desired_state((JUNGLE_1969, [BUSY])),
        owned={},
        view=lidarr_view(artists=[lidarr_artist(EXISTING, id=9003, name="Jungle")]),
        resolutions={BUSY.key: JUNGLE_RESOLUTION},
        artist_resolutions={},
        snapshot=JUNGLE_SNAPSHOT,
        collisions=[JUNGLE_COLLISION],
        disambiguation={WANTED: "US psychedelic rock"}.get,
    )

    text = "\n".join(i.text for i in report.summary) + report.details
    assert "likearr would add it" not in text
    assert "won't be added" in text
    assert "Lidarr already has a different artist named Jungle (London modern soul collective, Lidarr id 9003)" in text


def test_jargon_in_the_details_reads_as_words() -> None:
    details = _jungle().details

    assert 'you liked "Busy Earnin\'"' in details
    assert "the smallest release holding a liked song" in details
    assert "1 possible release looked at" in details
    assert "candidate(s)" not in details


def test_a_match_close_in_time_is_not_flagged() -> None:
    near = rg("rg-near", "Jungle", artist_mbid=WANTED, artist_name="Jungle", released="2013-06-01")
    report = _jungle(
        desired=desired_state((near, [BUSY])),
        resolutions={
            BUSY.key: Resolution(
                intent_key=BUSY.key, status=ResolutionStatus.RESOLVED, release_group=near, step="track:album"
            )
        },
        collisions=[],
    )

    (item,) = report.summary
    assert not item.wrong_match


def test_other_songs_by_the_same_spotify_artist_matching_someone_else_is_flagged() -> None:
    other = reason(ReasonKind.LIKED, "casio")
    london_album = rg(
        "rg-london", "Loving in Stereo", artist_mbid=EXISTING, artist_name="Jungle", released="2021-08-13"
    )
    snapshot = SourceSnapshot(
        fetched_at=JUNGLE_SNAPSHOT.fetched_at,
        artists=(),
        albums=(),
        tracks=(
            *JUNGLE_SNAPSHOT.tracks,
            TrackIntent(
                spotify_id="casio",
                name="Casio",
                isrc=None,
                artist_names=("Jungle",),
                album=spotify_album("Loving in Stereo", artists=("Jungle",), released="2021-08-13"),
                added_at=None,
                reason=other,
            ),
        ),
        counts={"liked_tracks": 2},
    )
    near = rg("rg-near", "Jungle", artist_mbid=WANTED, artist_name="Jungle", released="2014-07-14")
    report = _jungle(
        desired=desired_state((near, [BUSY]), (london_album, [other])),
        resolutions={
            BUSY.key: Resolution(
                intent_key=BUSY.key, status=ResolutionStatus.RESOLVED, release_group=near, step="track:album"
            ),
            other.key: Resolution(
                intent_key=other.key, status=ResolutionStatus.RESOLVED, release_group=london_album, step="track:album"
            ),
        },
        snapshot=snapshot,
        collisions=[],
    )

    busy = next(i for i in report.summary if "Busy Earnin" in i.text)
    assert busy.wrong_match
    assert "Other songs by Jungle on Spotify matched a different artist" in busy.text


def _in_playlist(playlist_id: str, names: dict[str, str]):
    in_playlist = reason(ReasonKind.PLAYLIST, "busy-earnin", playlist_id=playlist_id)
    snapshot = SourceSnapshot(
        fetched_at=JUNGLE_SNAPSHOT.fetched_at,
        artists=(),
        albums=(),
        tracks=(
            TrackIntent(
                spotify_id="busy-earnin",
                name="Busy Earnin'",
                isrc=None,
                artist_names=("Jungle",),
                album=spotify_album("Jungle", artists=("Jungle",), released="2014-07-14"),
                added_at=None,
                reason=in_playlist,
            ),
        ),
        counts={f"playlist:{playlist_id}": 1},
    )
    return _jungle(
        desired=desired_state((JUNGLE_1969, [in_playlist])),
        resolutions={in_playlist.key: JUNGLE_RESOLUTION},
        snapshot=snapshot,
        playlist_names=names,
    )


def test_a_playlist_song_names_its_playlist() -> None:
    report = _in_playlist("pl1", {"pl1": "Road trip"})

    assert '"Busy Earnin\'" by Jungle is in your playlist "Road trip"' in report.summary[0].text
    assert not any("open.spotify.com" in url for _, url in report.summary[0].links)


def test_a_playlist_with_no_known_name_is_never_its_raw_id_and_links_to_spotify() -> None:
    report = _in_playlist("pl1", {})

    assert "is in one of your playlists" in report.summary[0].text
    assert "pl1" not in report.summary[0].text  # never a raw id
    assert ("the playlist on Spotify", "https://open.spotify.com/playlist/pl1") in report.summary[0].links


def test_the_text_form_leads_with_the_summary() -> None:
    from likearr.core.explain import render_report

    text = render_report(_jungle())

    assert text.index("In short:") < text.index("Details:")
    assert "looks like a wrong match" in text


# ---------------------------------------------------------------- a followed artist (Lawrence)

LAWRENCE = "8e1a9a4e-5a1c-4c1b-9d7c-2b3f7c6a1e11"
FOLLOWS_LAWRENCE = reason(ReasonKind.FOLLOWED, "sp-lawrence")
LIVING_ROOM = rg("rg-l1", "Living Room", artist_mbid=LAWRENCE, artist_name="Lawrence", released="2016-06-17")
HOTEL_TV = rg("rg-l2", "Hotel TV", artist_mbid=LAWRENCE, artist_name="Lawrence", released="2020-08-21")
FAMILY = rg("rg-l3", "Family Business", artist_mbid=LAWRENCE, artist_name="Lawrence", released="2025-03-28")


def _lawrence_inputs() -> dict[str, object]:
    return dict(
        desired=desired_state(
            (LIVING_ROOM, [FOLLOWS_LAWRENCE]),
            (HOTEL_TV, [FOLLOWS_LAWRENCE]),
            (FAMILY, [FOLLOWS_LAWRENCE]),
            followed={LAWRENCE},
            followed_counts={LAWRENCE: 3},
        ),
        owned={},
        view=lidarr_view(
            artists=[lidarr_artist(LAWRENCE, id=9004, name="Lawrence")],
            albums=[
                lidarr_album(LIVING_ROOM, id=101, artist_id=9004, monitored=True, files=10),
                lidarr_album(HOTEL_TV, id=102, artist_id=9004, monitored=True),
                lidarr_album(FAMILY, id=103, artist_id=9004),
            ],
        ),
        resolutions={},
        artist_resolutions={},
        snapshot=SourceSnapshot(
            fetched_at=JUNGLE_SNAPSHOT.fetched_at,
            artists=(ArtistIntent(spotify_id="sp-lawrence", name="Lawrence", reason=FOLLOWS_LAWRENCE),),
            albums=(),
            tracks=(),
            counts={"followed_artists": 1},
        ),
        disambiguation={LAWRENCE: "Clyde and Gracie Lawrence"}.get,
    )


def _lawrence(**overrides: object):
    return explain_report("Lawrence", **{**_lawrence_inputs(), **overrides})  # type: ignore[arg-type]


def test_a_followed_artist_explains_the_setting_in_plain_words() -> None:
    report = _lawrence()

    (item,) = report.summary  # one answer for the artist, not one per album
    assert (
        "You follow Lawrence (Clyde and Gracie Lawrence) on Spotify, so likearr wants their studio albums "
        "and EPs - 3 of them - on the Lean profile (the followed_artists setting is on)."
    ) in item.text
    assert "In Lidarr as artist id 9004: of the 3, 2 monitored, 1 with files on disk." in item.text
    assert "Monitored: Living Room (2016), Hotel TV (2020)." in item.text
    assert "Not monitored yet: Family Business (2025); the next apply monitors it." in item.text
    assert ("the artist on MusicBrainz", f"https://musicbrainz.org/artist/{LAWRENCE}") in item.links
    # The detail still lists every release.
    assert "Family Business" in report.details and "Living Room" in report.details


def test_a_followed_artist_song_you_also_liked_keeps_its_own_answer() -> None:
    liked = reason(ReasonKind.LIKED, "sp-song")
    desired = desired_state(
        (LIVING_ROOM, [FOLLOWS_LAWRENCE, liked]),
        (HOTEL_TV, [FOLLOWS_LAWRENCE]),
        followed={LAWRENCE},
        followed_counts={LAWRENCE: 2},
    )

    report = _lawrence(desired=desired)

    assert len(report.summary) == 2
    assert "Living Room" in report.summary[1].text


def test_a_limit_keeps_the_first_answers_and_counts_the_rest() -> None:
    releases = [rg(f"rg-{i}", f"Record {i}") for i in range(5)]
    desired = desired_state(*[(r, [reason(ReasonKind.LIKED, f"t{i}")]) for i, r in enumerate(releases)])

    report = explain_report(
        "Record", desired=desired, owned={}, view=lidarr_view(), resolutions={}, artist_resolutions={}, limit=3
    )

    assert len(report.summary) == 3
    assert report.left_out == 2
    assert report.more_note == "2 more matches not shown: make the search more specific."
    assert "- 2 more matches not shown" in render_report(report)  # the CLI still says so
    assert report.as_dict()["left_out"] == 2
    assert "Record 4" not in report.details


# ---------------------------------------------------------------- a release nothing wants any more

ORPHAN_VIEW = lidarr_view(
    artists=[lidarr_artist(ARTIST, name="The Weeknd")], albums=[lidarr_album(ALBUM, monitored=True, files=1)]
)


def _orphan(**overrides: object):
    key, record = owned(ALBUM, LIKED, step="track:isrc->album")
    args: dict[str, object] = dict(
        desired=desired_state(),
        owned={key: record},
        view=ORPHAN_VIEW,
        resolutions={},
        artist_resolutions={},
    )
    args.update(overrides)
    return key, explain_report("After Hours", **args)  # type: ignore[arg-type]


def test_an_orphan_the_plan_unmonitors_says_so() -> None:
    key, _ = _orphan()
    _, report = _orphan(unmonitor={key})

    assert "so the next apply unmonitors it" in report.summary[0].text
    assert "so the next apply unmonitors it" in report.details


def test_an_orphan_a_guard_held_back_says_which_guard() -> None:
    from likearr.models import Guard

    guard = Guard("scheduled-cap", "12 unmonitors on a scheduled run, over the cap of 10", blocked_unmonitors=12)
    _, report = _orphan(unmonitor=set(), guards=[guard])

    assert "likearr would unmonitor it, but a guard held that back: 12 unmonitors" in report.summary[0].text
    assert "unmonitors it (files" not in report.summary[0].text


def test_an_orphan_with_no_plan_facts_hedges() -> None:
    _, report = _orphan()

    assert "unless a guard holds it back" in report.summary[0].text


def test_an_orphan_whose_song_is_still_liked_but_unmatched_is_kept() -> None:
    liked = SourceSnapshot(
        fetched_at=JUNGLE_SNAPSHOT.fetched_at,
        artists=(),
        albums=(),
        tracks=(
            TrackIntent(
                spotify_id="t1",
                name="Blinding Lights",
                isrc=None,
                artist_names=("The Weeknd",),
                album=spotify_album("After Hours"),
                added_at=None,
                reason=LIKED,
            ),
        ),
        counts={},
    )
    unmapped = Resolution(LIKED.key, ResolutionStatus.UNMAPPED, detail="MusicBrainz did not answer")

    _, report = _orphan(snapshot=liked, resolutions={LIKED.key: unmapped}, unmonitor=set())

    assert "is still on Spotify but didn't match anything this run" in report.summary[0].text
    assert "keeps it monitored" in report.details


def test_an_orphan_not_monitored_in_lidarr_needs_nothing() -> None:
    view = lidarr_view(artists=[lidarr_artist(ARTIST, name="The Weeknd")], albums=[lidarr_album(ALBUM)])

    _, report = _orphan(view=view)

    assert "there is nothing to do" in report.summary[0].text


def test_a_followed_artists_numbers_add_up_when_a_liked_song_adds_a_release() -> None:
    liked = reason(ReasonKind.LIKED, "sp-song")
    single = rg("rg-l4", "A Single", artist_mbid=LAWRENCE, artist_name="Lawrence", primary=PrimaryType.SINGLE)
    desired = desired_state(
        (LIVING_ROOM, [FOLLOWS_LAWRENCE]),
        (HOTEL_TV, [FOLLOWS_LAWRENCE]),
        (FAMILY, [FOLLOWS_LAWRENCE]),
        (single, [liked]),
        followed={LAWRENCE},
        followed_counts={LAWRENCE: 3},
    )

    item = _lawrence(desired=desired).summary[0]

    assert "- 3 of them -" in item.text
    assert (
        "of the 4 releases likearr wants from them (the 3 above, plus 1 for songs or albums you liked, "
        "saved or keep in playlists)" in item.text
    )


def test_the_jungle_artist_answer_carries_the_wrong_match_flag() -> None:
    report = explain_report("Jungle", **{**_jungle_inputs(), "desired": _jungle_desired()})  # type: ignore[arg-type]

    artist = report.summary[0]
    assert "won't be added" in artist.text
    assert artist.wrong_match
    assert "The only release likearr wants from Jungle looks like a wrong match" in artist.text


# ---------------------------------------------------------------- ambiguity is said, not guessed

JUNGLE_AMBIGUOUS = Resolution(
    intent_key=BUSY.key,
    status=ResolutionStatus.UNMAPPED,
    step="ambiguous:same-name-artists",
    detail=(
        "2 different artists named 'Jungle' each have a release titled 'Jungle': 'Jungle' (297a768d, 1969) "
        "by Jungle (59074e0f); 'Jungle' (3c5834e7, 2014) by Jungle (6bbb3983); the track has no ISRC to tell "
        "them apart"
    ),
)


def test_an_ambiguous_match_says_two_artists_share_the_name_and_which_was_meant_is_unknown() -> None:
    from likearr.core.explain import describe_step, resolution_outcome

    assert resolution_outcome(JUNGLE_AMBIGUOUS) == "ambiguous"

    report = _jungle(desired=desired_state(), resolutions={BUSY.key: JUNGLE_AMBIGUOUS}, collisions=[])

    (item,) = report.summary
    assert item.text.startswith('You liked "Busy Earnin\'" by Jungle, but two different artists share this name')
    assert (
        "two different artists share this name and album title; likearr couldn't tell which one you meant" in item.text
    )
    assert "likearr monitors nothing new for it" in item.text
    assert "couldn't match it to MusicBrainz" not in item.text, "it did find a match - two of them"
    assert "297a768d" in item.text and "3c5834e7" in item.text, "the candidates are named"
    assert "looked for as: " + describe_step(JUNGLE_AMBIGUOUS.step) in report.details
    assert "ambiguous:same-name-artists" not in describe_step(JUNGLE_AMBIGUOUS.step), "said in words"


def test_an_ambiguous_match_says_the_release_it_held_before_stays_monitored() -> None:
    """A release v4 monitored on a guess is kept (`core.diff`'s still_live), and the summary says so."""
    from tests.unit.fakes import lidarr_album, owned

    key, record = owned(JUNGLE_1969, BUSY, step="track:smallest:album")
    report = _jungle(
        desired=desired_state(),
        resolutions={BUSY.key: JUNGLE_AMBIGUOUS},
        collisions=[],
        owned={key: record},
        view=lidarr_view(
            artists=[lidarr_artist(WANTED, id=2000, name="Jungle")],
            albums=[lidarr_album(JUNGLE_1969, artist_id=2000, monitored=True)],
        ),
    )

    (item,) = report.summary
    assert 'likearr keeps "Jungle" monitored from before until it can tell which artist you meant' in item.text
    assert "monitors nothing new" not in item.text


# ---------------------------------------------------------------- cards: what the Look up page shows


def test_a_release_card_leads_with_what_you_did_and_says_the_rest_as_facts() -> None:
    (item,) = _jungle().summary

    assert item.headline == (
        'You liked "Busy Earnin\'" by Jungle (on Spotify: the album "Jungle", 2014). '
        'likearr matched it to "Jungle" (1969) by Jungle (US psychedelic rock).'
    )
    assert item.status == "skipped"
    assert dict(item.facts) == {
        "On Spotify": 'You liked "Busy Earnin\'"',
        "Matched to": "Jungle (album, 1969) by Jungle (US psychedelic rock)",
        "In Lidarr": "Not added: Lidarr already has a different Jungle (London modern soul collective)",
        "Why it may be wrong": "This looks like a wrong match: Spotify's album is from 2014, the one likearr "
        "matched from 1969. If it is, use Not this one to stop likearr choosing it, or check the match on MusicBrainz.",
    }
    assert item.release == JUNGLE_1969.mbid
    assert item.lidarr_path == ""  # the artist is not in Lidarr: nothing to open
    assert item.detail.startswith("release: Jungle - Jungle (297a768d-1111-2222-3333-444444444444)")


def test_a_followed_artist_card_says_what_likearr_wants_and_holds_its_albums_detail() -> None:
    (item,) = _lawrence().summary

    assert item.headline == (
        "You follow Lawrence (Clyde and Gracie Lawrence) on Spotify, so likearr wants their studio albums and EPs."
    )
    assert item.status == "monitored"
    assert dict(item.facts) == {
        "On Spotify": "You follow them",
        "Matched to": "Lawrence (Clyde and Gracie Lawrence)",
        "In Lidarr": "2 of 3 monitored, 1 with files on disk",
        "What likearr wants": "Studio albums and EPs (3), Lean profile",
    }
    assert item.lidarr_path == f"/artist/{LAWRENCE}"
    assert item.release == ""  # an artist is not refused with "Not this one"
    # The albums folded into the artist's card keep their detail there.
    assert "artist: Lawrence" in item.detail and "release: Lawrence - Family Business" in item.detail


def test_a_release_card_status_follows_lidarr() -> None:
    from likearr.core.explain import explain_report

    def status(*, monitored: bool, files: int = 0) -> tuple[str, str]:
        view = lidarr_view(
            artists=[lidarr_artist(ARTIST, id=7, name="The Weeknd")],
            albums=[lidarr_album(ALBUM, monitored=monitored, files=files)],
        )
        (item,) = explain_report(
            "After Hours",
            desired=desired_state((ALBUM, [LIKED])),
            owned={},
            view=view,
            resolutions={LIKED.key: RESOLUTION},
            artist_resolutions={},
        ).summary
        return item.status, dict(item.facts)["In Lidarr"]

    assert status(monitored=True, files=14) == ("downloaded", "Monitored, 14 files on disk")
    assert status(monitored=True) == ("waiting", "Monitored, nothing downloaded yet")
    assert status(monitored=False) == ("not-monitored", "Not monitored yet: the next apply monitors it")


def test_an_unmatched_card_keeps_the_resolver_detail_out_of_its_headline() -> None:
    from likearr.core.explain import explain_report

    unmapped = Resolution(BUSY.key, ResolutionStatus.UNMAPPED, step="track:none", detail="3 candidate(s) considered")
    (item,) = explain_report(
        "Busy",
        desired=desired_state(),
        owned={},
        view=lidarr_view(),
        resolutions={BUSY.key: unmapped},
        artist_resolutions={},
        snapshot=JUNGLE_SNAPSHOT,
    ).summary

    assert item.status == "unmatched"
    assert item.headline.endswith("so nothing is monitored for it.")
    assert "3 possible releases looked at" in item.text  # the text form keeps it...
    assert (
        dict(item.facts)["Matched to"] == "Nothing on MusicBrainz (3 possible releases looked at)"
    )  # ...as does a fact


def test_the_json_report_carries_the_cards() -> None:
    data = _jungle().as_dict()

    (item,) = data["summary"]  # type: ignore[misc]
    assert item["status"] == "skipped" and item["release"] == JUNGLE_1969.mbid
    assert ["In Lidarr", "Not added: Lidarr already has a different Jungle (London modern soul collective)"] in item[
        "facts"
    ]
    assert data["left_out"] == 0


# ---------------------------------------------------------------- "Not this one" only where it works


def _why(*kinds: ReasonKind) -> frozenset[Reason]:
    return frozenset(
        Reason(kind, f"sp-{i}", "pl-1" if kind is ReasonKind.PLAYLIST else None) for i, kind in enumerate(kinds)
    )


@pytest.mark.parametrize(
    ("kinds", "can_deny"),
    [
        ((ReasonKind.LIKED,), True),
        ((ReasonKind.PLAYLIST,), True),
        ((ReasonKind.FOLLOWED,), True),
        ((ReasonKind.LIKED, ReasonKind.PLAYLIST, ReasonKind.FOLLOWED), True),
        ((ReasonKind.SAVED,), False),
        ((ReasonKind.MANUAL,), False),
        ((ReasonKind.LIKED, ReasonKind.SAVED), False),
        ((ReasonKind.FOLLOWED, ReasonKind.SAVED), False),
        ((ReasonKind.LIKED, ReasonKind.MANUAL), False),
        ((), False),
    ],
)
def test_deniable(kinds: tuple[ReasonKind, ...], can_deny: bool) -> None:
    """A deny changes liked and playlist songs and (option B) followed catalogues; a saved album
    overrides it and a hand-kept release is never dropped."""
    assert deniable(_why(*kinds)) is can_deny
    assert bool(deny_note(_why(*kinds))) is (not can_deny and bool(kinds))


def test_deny_notes_say_where_the_choice_lives() -> None:
    assert deny_note(_why(ReasonKind.SAVED)) == "Unsave the album on Spotify to stop this."
    assert deny_note(_why(ReasonKind.FOLLOWED, ReasonKind.SAVED)) == "Unsave the album on Spotify to stop this."
    assert deny_note(_why(ReasonKind.LIKED, ReasonKind.SAVED)) == (
        "Your saved album still wants this; refusing it only moves the song."
    )
    assert "by hand" in deny_note(_why(ReasonKind.MANUAL))
