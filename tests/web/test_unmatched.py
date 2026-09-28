"""The Unmatched page's rows: one per release, grouped by what happened and why."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from likearr.core.desire import CATALOGUE_TOO_LARGE_STEP
from likearr.core.diff import CATALOGUE_GAP_STEP, RECENT_GAP_STEP
from likearr.core.explain import LOOKUP_FAILED_STEP
from likearr.core.resolver import (
    AMBIGUOUS_SAME_NAME_STEP,
    ARTIST_AMBIGUOUS_STEP,
    EXCLUDED_COMPILATION_STEP,
    EXCLUDED_DENIED_STEP,
    EXCLUDED_REMIX_STEP,
    UNAVAILABLE_STEP,
)
from likearr.models import (
    ArtistResolution,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SourceSnapshot,
)
from likearr.shell.last_run import LastRun
from likearr.web.unmatched import (
    PAGE_SIZE,
    REASONS,
    Filters,
    build_rows,
    cards,
    filters_from,
    part,
    reason_for,
    sections,
    source_options,
)
from tests.unit.fakes import (
    NOW,
    album_intent,
    artist_intent,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    reason,
    rg,
    spotify_album,
    track_intent,
)
from tests.unit.test_diff import desired_state

MAYER = "b1b1b1b1-0000-4000-8000-000000000001"
TRY = spotify_album("TRY! - Live In Concert", spotify_id="sp-try", artists=("John Mayer",), released="2005-11-22")
ROAD_TRIP = "37i9dQZF1DXroadtrip0000"


def _unmapped(key: str, step: str, **kw) -> Resolution:
    status = ResolutionStatus.PENDING_ALBUM if step == "track:pending" else ResolutionStatus.UNMAPPED
    return Resolution(key, status, step=step, **kw)


def _last(*, tracks=(), albums=(), artists=(), resolutions=None, artist_resolutions=None, **kw) -> LastRun:
    return LastRun(
        ran_at=kw.pop("ran_at", NOW),
        kind="dry run",
        snapshot=SourceSnapshot(
            fetched_at=NOW, artists=tuple(artists), albums=tuple(albums), tracks=tuple(tracks), counts={}
        ),
        resolutions=resolutions or {},
        artist_resolutions=artist_resolutions or {},
        desired=kw.pop("desired", desired_state()),
        view=kw.pop("view", lidarr_view()),
        collisions=(),
        monitor=kw.pop("monitor", frozenset()),
    )


def _try_songs(n: int = 5, *, playlist_id: str | None = None) -> list:
    return [
        track_intent(f"Song {i}", TRY, spotify_id=f"sp-try-{i}", artists=("John Mayer Trio",), playlist_id=playlist_id)
        for i in range(n)
    ]


# ---------------------------------------------------------------- one row per release


def test_several_liked_songs_from_one_album_are_one_row() -> None:
    songs = _try_songs()
    last = _last(tracks=songs, resolutions={t.reason.key: _unmapped(t.reason.key, "track:album:search") for t in songs})

    [row] = build_rows(last)

    assert row.headline == "John Mayer - TRY! - Live In Concert"
    assert row.what == "5 liked songs"
    assert row.songs == [f"Song {i}" for i in range(5)]
    assert (row.group, row.reason.code, row.items) == ("unmatched", "no-release", 5)
    assert row.reason.label == "MusicBrainz has no release by this artist with this title"
    assert row.source_text == "Liked song"


def test_the_same_album_for_different_reasons_is_two_rows() -> None:
    a, b = _try_songs(2)
    last = _last(
        tracks=[a, b],
        resolutions={
            a.reason.key: _unmapped(a.reason.key, "track:album:search"),
            b.reason.key: _unmapped(b.reason.key, EXCLUDED_REMIX_STEP),
        },
    )

    rows = sorted(build_rows(last), key=lambda r: r.group)

    assert [(r.group, r.reason.code) for r in rows] == [("excluded", "remix"), ("unmatched", "no-release")]
    assert rows[0].setting == "rules.allow_remix_releases"


def test_a_saved_album_and_a_followed_artist_are_rows_of_their_own_kind() -> None:
    saved = album_intent(spotify_album("Lost", spotify_id="sp-lost", artists=("Nobody",)))
    follow = artist_intent("Ghost Band", spotify_id="sp-ghost")
    last = _last(
        albums=[saved],
        artists=[follow],
        resolutions={saved.reason.key: _unmapped(saved.reason.key, "album:search")},
        artist_resolutions={
            follow.reason.key: ArtistResolution(follow.reason.key, ResolutionStatus.UNMAPPED, step="artist:search")
        },
    )

    rows = {r.kind: r for r in build_rows(last)}

    assert (rows["release"].what, rows["release"].source_text) == ("", "Saved album")
    assert (rows["artist"].headline, rows["artist"].what) == ("Ghost Band", "Followed artist")
    assert rows["artist"].reason.code == "no-artist"
    label, url = rows["artist"].musicbrainz
    assert label == "Search MusicBrainz" and parse_qs(urlparse(url).query)["type"] == ["artist"]


def test_a_playlist_source_is_named_and_the_id_never_shown() -> None:
    songs = _try_songs(2, playlist_id=ROAD_TRIP)
    last = _last(tracks=songs, resolutions={t.reason.key: _unmapped(t.reason.key, "track:album:search") for t in songs})

    [named] = build_rows(last, playlist_names={ROAD_TRIP: "Road trip"})
    [unnamed] = build_rows(last)

    assert named.what == "2 songs"
    assert named.source_text == 'Playlist "Road trip"'
    assert unnamed.source_text == "A playlist"


def test_the_musicbrainz_search_quotes_the_title_and_artist_safely() -> None:
    song = track_intent("x", spotify_album('Say "Hi" \\ now', artists=("A",)), spotify_id="sp-q")
    [row] = build_rows(
        _last(tracks=[song], resolutions={song.reason.key: _unmapped(song.reason.key, "track:album:search")})
    )

    _label, url = row.musicbrainz
    query = parse_qs(urlparse(url).query)["query"][0]

    assert url.startswith("https://musicbrainz.org/search?")
    assert query == 'releasegroup:"Say  Hi    now" AND artist:"A"'


def test_each_outcome_lands_in_its_own_group() -> None:
    songs = [track_intent(f"S{i}", spotify_album(f"A{i}", spotify_id=f"sp-{i}"), spotify_id=f"t{i}") for i in range(5)]
    steps = ["track:album:search", AMBIGUOUS_SAME_NAME_STEP, EXCLUDED_COMPILATION_STEP, "track:pending"]
    steps.append(LOOKUP_FAILED_STEP)
    resolutions = {t.reason.key: _unmapped(t.reason.key, s) for t, s in zip(songs, steps, strict=True)}
    last = _last(tracks=songs, resolutions=resolutions)

    by_title = {r.title: (r.group, r.reason.code) for r in build_rows(last)}

    assert by_title == {
        "A0": ("unmatched", "no-release"),
        "A1": ("ambiguous", "same-name"),
        "A2": ("excluded", "compilation"),
        "A3": ("pending", "waiting"),
        "A4": ("failed", "lookup-failed"),
    }


# ---------------------------------------------------------------- the reasons


@pytest.mark.parametrize(
    ("step", "group", "code"),
    [
        ("track:album:search", "unmatched", "no-release"),
        ("track:album:isrc", "unmatched", "no-release"),
        ("album:search", "unmatched", "no-release"),
        ("track:various-artists", "unmatched", "various-artists"),
        ("album:various-artists", "unmatched", "various-artists"),
        ("artist:search", "unmatched", "no-artist"),
        (ARTIST_AMBIGUOUS_STEP, "unmatched", "artist-link"),
        (CATALOGUE_TOO_LARGE_STEP, "unmatched", "too-large"),
        ("lidarr:missing-release-group", "unmatched", "missing-in-lidarr"),
        (CATALOGUE_GAP_STEP, "unmatched", "not-in-catalogue"),
        (RECENT_GAP_STEP, "unmatched", "catalogue-new"),
        (AMBIGUOUS_SAME_NAME_STEP, "ambiguous", "same-name"),
        (EXCLUDED_REMIX_STEP, "excluded", "remix"),
        (EXCLUDED_COMPILATION_STEP, "excluded", "compilation"),
        (EXCLUDED_DENIED_STEP, "excluded", "denied"),
        ("track:excluded:something-new", "excluded", "left-out"),
        ("track:pending", "pending", "waiting"),
        (LOOKUP_FAILED_STEP, "failed", "lookup-failed"),
    ],
)
def test_every_step_the_resolver_and_the_plan_write_has_a_plain_reason(step: str, group: str, code: str) -> None:
    assert reason_for(step, group).code == code


@pytest.mark.parametrize("step", ["", "track:brand-new-step", "zzz", "error:something"])
def test_an_unknown_step_gets_the_generic_reason_never_a_crash(step: str) -> None:
    assert reason_for(step, "unmatched").label == "likearr couldn't match it"


def test_every_reason_says_why_and_what_to_do() -> None:
    assert len({r.code for r in REASONS}) == len(REASONS)
    assert all(r.label and r.guidance for r in REASONS)


# ---------------------------------------------------------------- Lidarr's catalogue gaps


def _gap_run(
    *, monitor: frozenset[ReleaseKey] | None = frozenset(), released: str = "2010-01-01", liked: bool = False
) -> LastRun:
    held = rg(
        "c0c0c0c0-0000-4000-8000-00000000000a",
        "Promo Sampler",
        artist_mbid=MAYER,
        artist_name="John Mayer",
        released=released,
    )
    song = track_intent("Gravity", TRY, spotify_id="sp-gravity")
    why = song.reason if liked else reason(ReasonKind.FOLLOWED, "sp-mayer")
    matched = Resolution(song.reason.key, ResolutionStatus.RESOLVED, release_group=held, step="track:album")
    return _last(
        tracks=[song] if liked else [],
        resolutions={song.reason.key: matched} if liked else None,
        desired=desired_state((held, [why])),
        view=lidarr_view(artists=[lidarr_artist(MAYER, name="John Mayer")]),
        monitor=monitor,
    )


def test_a_followed_artists_release_lidarr_does_not_list_is_a_catalogue_gap() -> None:
    [row] = build_rows(_gap_run())

    assert (row.group, row.reason.code, row.what) == ("unmatched", "not-in-catalogue", "")
    assert row.source_text == "Followed artist"
    assert row.lidarr_path == f"/artist/{MAYER}"
    assert row.musicbrainz[1] == "https://musicbrainz.org/release-group/c0c0c0c0-0000-4000-8000-00000000000a"


def test_a_new_release_is_the_catalogue_catching_up() -> None:
    last = _gap_run(released=(NOW - timedelta(days=10)).date().isoformat())

    [row] = build_rows(last, recent_release_days=60)

    assert row.reason.code == "catalogue-new"


def test_a_liked_songs_release_lidarr_lacks_is_missing_in_lidarr() -> None:
    [row] = build_rows(_gap_run(liked=True))

    assert (row.reason.code, row.songs, row.source_text) == ("missing-in-lidarr", ["Gravity"], "Liked song")


def test_a_gap_counts_every_reason_behind_it() -> None:
    last = _gap_run(liked=True)
    key = next(iter(last.desired.releases))
    release = last.desired.releases[key]
    both = desired_state((release.release_group, [*release.reasons, reason(ReasonKind.SAVED, "sp-s")]))

    [row] = build_rows(_last(tracks=last.snapshot.tracks, resolutions=last.resolutions, desired=both, view=last.view))

    assert (row.items, row.source_text) == (2, "Liked song, Saved album")


def test_a_release_whose_artist_is_not_in_lidarr_is_not_a_gap() -> None:
    """The plan adds the artist first; until then the release is not Lidarr's to lack."""
    last = _gap_run()

    assert build_rows(_last(desired=last.desired, view=lidarr_view(), monitor=frozenset())) == []


def test_two_releases_that_share_an_artist_name_and_a_title_stay_two_rows() -> None:
    other = "b2b2b2b2-0000-4000-8000-000000000002"
    live_a = rg("f0000000-0000-4000-8000-00000000000a", "Live", artist_mbid=MAYER, artist_name="John Mayer")
    live_b = rg("f0000000-0000-4000-8000-00000000000b", "Live", artist_mbid=MAYER, artist_name="John Mayer")
    namesake = rg("f0000000-0000-4000-8000-00000000000c", "Live", artist_mbid=other, artist_name="John Mayer")
    follows = [reason(ReasonKind.FOLLOWED, "sp-mayer")]
    last = _last(
        desired=desired_state((live_a, follows), (live_b, follows), (namesake, follows)),
        view=lidarr_view(artists=[lidarr_artist(MAYER, name="John Mayer"), lidarr_artist(other, name="John Mayer")]),
    )

    rows = build_rows(last)

    assert sorted(r.release_group for r in rows) == [live_a.mbid, live_b.mbid, namesake.mbid]
    assert {r.lidarr_path for r in rows} == {f"/artist/{MAYER}", f"/artist/{other}"}


def test_two_songs_with_the_same_name_are_two_songs() -> None:
    first = track_intent("Interlude", TRY, spotify_id="sp-i1")
    second = track_intent("Interlude", TRY, spotify_id="sp-i2")
    last = _last(
        tracks=[first, second],
        resolutions={t.reason.key: _unmapped(t.reason.key, "track:album:search") for t in (first, second)},
    )

    [row] = build_rows(last)

    assert (row.what, row.items, row.songs) == ("2 liked songs", 2, ["Interlude", "Interlude"])


def test_a_long_title_is_cut_to_what_look_up_takes() -> None:
    song = track_intent("x", spotify_album("T" * 500, spotify_id="sp-long"), spotify_id="sp-l")
    [row] = build_rows(_last(tracks=[song], resolutions={song.reason.key: _unmapped(song.reason.key, "album:search")}))

    assert len(row.lookup) == 200


def test_the_cards_count_exactly_what_status_counts_for_the_same_run() -> None:
    """Each card's number is Status's line for it - a song counted in one place is
    counted in the other. A key with no resolution is a miss on both; a Lidarr catalogue gap is a
    match on Status, so it sits beside the card's number, never in it."""
    from likearr.web.status import coverage

    songs = [track_intent(f"S{i}", spotify_album(f"A{i}", spotify_id=f"sp-{i}"), spotify_id=f"t{i}") for i in range(7)]
    steps = ["track:album:search", "track:album:search", AMBIGUOUS_SAME_NAME_STEP, EXCLUDED_REMIX_STEP]
    steps += ["track:pending", LOOKUP_FAILED_STEP]
    resolutions = {t.reason.key: _unmapped(t.reason.key, step) for t, step in zip(songs, steps, strict=False)}
    # songs[6] has no resolution at all: a miss on both pages. A stray resolution Spotify no longer
    # lists is on neither.
    resolutions["liked:gone"] = _unmapped("liked:gone", "track:album:search")
    gap = _gap_run()
    last = _last(tracks=songs, resolutions=resolutions, desired=gap.desired, view=gap.view)

    c = coverage(last)
    by_group = {card.group.code: card for card in cards(build_rows(last))}

    assert {code: card.items for code, card in by_group.items()} == {
        "unmatched": c.unmatched,
        "ambiguous": c.ambiguous,
        "excluded": c.excluded,
        "pending": c.pending,
        "failed": c.lookup_failed,
    }
    assert (c.unmatched, by_group["unmatched"].releases, by_group["unmatched"].gaps) == (3, 3, 1)


def test_a_release_the_plan_monitors_is_not_a_gap() -> None:
    held = next(iter(_gap_run().desired.releases))

    assert build_rows(_gap_run(monitor=frozenset({held}))) == []


def test_a_file_from_before_the_monitors_were_kept_lists_no_gaps() -> None:
    assert build_rows(_gap_run(monitor=None)) == []


def test_a_release_lidarr_holds_is_not_a_gap() -> None:
    last = _gap_run()
    key: ReleaseKey = next(iter(last.desired.releases))
    held = last.desired.releases[key].release_group
    view = lidarr_view(artists=[lidarr_artist(MAYER, name="John Mayer")], albums=[lidarr_album(held)])

    assert build_rows(_last(desired=last.desired, view=view)) == []


# ---------------------------------------------------------------- filters, sort, pages, cards


def _many() -> list:
    """40 albums with one liked song each, plus Try! with 5 and a playlist song."""
    tracks = [
        track_intent(
            f"Track {i:02d}",
            spotify_album(f"Album {i:02d}", spotify_id=f"sp-a{i}", artists=(f"Artist {i:02d}",)),
            spotify_id=f"sp-t{i}",
            artists=(f"Artist {i:02d}",),
        )
        for i in range(40)
    ]
    tracks += _try_songs()
    tracks.append(track_intent("Road Song", TRY, spotify_id="sp-road", playlist_id=ROAD_TRIP))
    tracks.append(track_intent("Failed One", spotify_album("Flaky", artists=("Zed",)), spotify_id="sp-f"))
    resolutions = {t.reason.key: _unmapped(t.reason.key, "track:album:search") for t in tracks}
    resolutions["liked:sp-f"] = _unmapped("liked:sp-f", LOOKUP_FAILED_STEP)
    return build_rows(_last(tracks=tracks, resolutions=resolutions), playlist_names={ROAD_TRIP: "Road trip"})


def test_the_cards_count_songs_and_the_releases_they_are_on() -> None:
    by_group = {c.group.code: c for c in cards(_many())}

    assert (by_group["unmatched"].items, by_group["unmatched"].releases) == (46, 41)
    assert (by_group["failed"].items, by_group["ambiguous"].items) == (1, 0)


def test_the_first_page_holds_25_and_the_next_the_rest() -> None:
    rows = _many()

    [(_group, [first])] = [s for s in sections(rows, Filters()) if s[0].code == "unmatched"]
    second = part(rows, Filters(), "unmatched", "no-release", 2)

    assert (len(first.rows), first.page, first.pages, first.matched) == (PAGE_SIZE, 1, 2, 41)
    assert second is not None and (len(second.rows), second.page) == (16, 2)
    assert part(rows, Filters(), "unmatched", "no-release", 99).page == 2  # type: ignore[union-attr]
    assert part(rows, Filters(), "nope", "no-release", 1) is None


def test_search_finds_an_artist_an_album_or_a_song() -> None:
    rows = _many()

    for query in ("john mayer", "try!", "song 3", "ROAD SONG"):
        found = [r for r in rows if Filters(query=query).keeps(r)]
        assert [r.title for r in found] == ["TRY! - Live In Concert"], query


def test_the_group_filter_takes_a_group_or_a_reason() -> None:
    rows = _many()

    assert {r.group for r in rows if Filters(group="failed").keeps(r)} == {"failed"}
    assert len([r for r in rows if Filters(group="no-release").keeps(r)]) == 41


def test_the_source_filter_takes_a_kind_or_one_playlist() -> None:
    rows = _many()

    assert [r.title for r in rows if Filters(source=f"playlist:{ROAD_TRIP}").keeps(r)] == ["TRY! - Live In Concert"]
    assert [r.title for r in rows if Filters(source="playlist").keeps(r)] == ["TRY! - Live In Concert"]
    assert len([r for r in rows if Filters(source="liked").keeps(r)]) == 42
    assert source_options(rows) == [
        ("liked", "Liked songs"),
        ("playlist", "Any playlist"),
        (f"playlist:{ROAD_TRIP}", 'Playlist "Road trip"'),
    ]


def test_most_liked_songs_first_puts_try_on_top() -> None:
    rows = _many()

    first = part(rows, Filters(sort="songs"), "unmatched", "no-release", 1)
    by_name = part(rows, Filters(), "unmatched", "no-release", 1)

    assert first is not None and first.rows[0].title == "TRY! - Live In Concert"
    assert by_name is not None and by_name.rows[0].artist == "Artist 00"


def test_filters_from_a_request_drop_anything_the_page_does_not_offer() -> None:
    rows = _many()

    f = filters_from({"q": "x" * 500, "show": "<script>", "source": "playlist:someone-elses", "sort": "size"}, rows)

    assert (len(f.query), f.group, f.source, f.sort) == (200, "", "", "artist")
    ok = filters_from({"show": "no-release", "source": f"playlist:{ROAD_TRIP}", "sort": "songs"}, rows)
    assert (ok.group, ok.source, ok.sort) == ("no-release", f"playlist:{ROAD_TRIP}", "songs")


def test_a_v7_shaped_run_opens_with_every_group_and_try_as_one_row() -> None:
    """Unmapped intents shaped like a real plan: the resolver's misses, left-outs,
    and Lidarr's catalogue gaps, which are worked out from the wanted releases."""
    tracks, resolutions = [], {}

    def miss(i: int, step: str, album=None) -> None:
        t = track_intent(
            f"T{i}",
            album or spotify_album(f"A{i}", spotify_id=f"sp-a{i}", artists=(f"R{i}",)),
            spotify_id=f"sp-{i}",
            artists=(f"R{i}",),
        )
        tracks.append(t)
        resolutions[t.reason.key] = _unmapped(t.reason.key, step)

    for i in range(5):
        miss(i, "track:album:search", TRY)
    for i in range(5, 105):
        miss(i, "track:album:search")
    for i in range(105, 115):
        miss(i, "track:various-artists")
    for i in range(115, 117):
        miss(i, EXCLUDED_REMIX_STEP)
    saved = [album_intent(spotify_album(f"S{i}", spotify_id=f"sp-s{i}", artists=(f"R{i}",))) for i in range(6)]
    resolutions.update({a.reason.key: _unmapped(a.reason.key, "album:search") for a in saved})
    follows = [artist_intent(f"F{i}", spotify_id=f"sp-f{i}") for i in range(2)]
    artist_res = {
        f.reason.key: ArtistResolution(f.reason.key, ResolutionStatus.UNMAPPED, step="artist:search") for f in follows
    }
    followed = [
        (
            rg(f"d0000000-0000-4000-8000-{i:012d}", f"Promo {i}", artist_mbid=MAYER, released="2001-01-01"),
            [reason(ReasonKind.FOLLOWED, "sp-mayer")],
        )
        for i in range(50)
    ]
    liked_gaps = [
        (
            rg(f"e0000000-0000-4000-8000-{i:012d}", f"Gap {i}", artist_mbid=MAYER),
            [reason(ReasonKind.LIKED, f"sp-gap-{i}")],
        )
        for i in range(20)
    ]
    last = _last(
        tracks=tracks,
        albums=saved,
        artists=follows,
        resolutions=resolutions,
        artist_resolutions=artist_res,
        desired=desired_state(*followed, *liked_gaps),
        view=lidarr_view(artists=[lidarr_artist(MAYER, name="John Mayer")]),
    )

    rows = build_rows(last)
    intents = sum(r.items for r in rows)
    shown = sections(rows, Filters())

    assert intents == 195
    assert [g.code for g, _ in shown] == ["unmatched", "excluded"]
    parts = {p.reason.code: p for p in shown[0][1]}
    assert {k: p.matched for k, p in parts.items()} == {
        "no-release": 101 + 6,
        "missing-in-lidarr": 20,
        "not-in-catalogue": 50,
        "various-artists": 10,
        "no-artist": 2,
    }
    assert [r.what for r in rows if r.title == "TRY! - Live In Concert"] == ["5 liked songs"]
    assert all(len(p.rows) <= PAGE_SIZE for p in parts.values())


def test_a_part_of_followed_artists_counts_artists_not_releases() -> None:
    follow = artist_intent("Ghost Band", spotify_id="sp-ghost")
    last = _last(
        artists=[follow],
        artist_resolutions={
            follow.reason.key: ArtistResolution(follow.reason.key, ResolutionStatus.UNMAPPED, step="artist:search")
        },
    )

    found = part(build_rows(last), Filters(), "unmatched", "no-artist", 1)

    assert found is not None and (found.matched, found.unit_one, found.unit) == (1, "artist", "artists")


# ---------------------------------------------------------------- a track Spotify no longer serves


def test_a_track_spotify_no_longer_serves_has_its_own_reason_and_is_labelled_by_its_id() -> None:
    """Spotify returns such a track with an empty name, artist and "Various Artists" album. The row
    read "Various Artists - " and advised adding the album to MusicBrainz, which cannot help."""
    gone = track_intent("", spotify_album("", artists=("",)), spotify_id="0gone0", artists=("",))
    last = _last(tracks=[gone], resolutions={gone.reason.key: _unmapped(gone.reason.key, UNAVAILABLE_STEP)})

    [row] = build_rows(last)

    assert (row.group, row.reason.code) == ("unmatched", "unavailable")
    assert row.reason.label == "Spotify no longer serves this track"
    assert "MusicBrainz" not in row.reason.guidance
    assert row.headline == "Spotify track 0gone0"
    assert reason_for(UNAVAILABLE_STEP, "unmatched").code == "unavailable"
