"""Two MusicBrainz artists share a name *and* an album title.

A real, public case, as a fixture: a liked "Busy Earnin'" by Jungle, the London modern soul
collective, from their 2014 self-titled album. A release-group search for "Jungle" by "Jungle"
returns that album *and* the 1969 self-titled album by Jungle, a US psychedelic rock band. Both
pass the title and credit gate exactly, and the old tie-break (earliest date, then lowest MBID)
picked the 1969 one every time - both keys favour it. The track's ISRC names only the London
band's recording, and that is what must decide it.

The artist MBIDs are the real ones; the release-group MBIDs keep the real 8-character prefixes
from the issue and are otherwise stand-ins. Every response is served by respx, never MusicBrainz.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from likearr.adapters.musicbrainz import MusicBrainzLookup
from likearr.config import MusicBrainzConfig
from likearr.core.resolver import AMBIGUOUS_SAME_NAME_STEP, resolve_album, resolve_track
from likearr.models import LIKED_TRACK_SCOPE_ALBUM, LIKED_TRACK_SCOPE_SMALLEST, ResolutionStatus
from tests.unit.fakes import NOW, album_intent, spotify_album, track_intent

from .conftest import MB_URL, FakeClock

LONDON = "6bbb3983-ce8a-4971-96e0-7cae73268fc4"
"""Jungle, the London modern soul collective: the band the liked track is by."""
US = "59074e0f-ede4-4ff1-bee2-cbfd3a273095"
"""Jungle, the US psychedelic rock band."""

US_1969 = "297a768d-0000-4000-8000-000000000001"
LONDON_ALBUM = "3c5834e7-0000-4000-8000-000000000002"
LONDON_SINGLE = "b614ca6d-0000-4000-8000-000000000003"
ISRC = "GBBKS1400112"


def _rg_json(mbid: str, artist_mbid: str, released: str, *, primary: str = "Album", title: str = "Jungle") -> dict:
    return {
        "id": mbid,
        "title": title,
        "primary-type": primary,
        "secondary-types": [],
        "first-release-date": released,
        "artist-credit": [{"name": "Jungle", "artist": {"id": artist_mbid, "name": "Jungle"}}],
    }


US_1969_JSON = _rg_json(US_1969, US, "1969")
LONDON_ALBUM_JSON = _rg_json(LONDON_ALBUM, LONDON, "2014-07-14")
LONDON_SINGLE_JSON = _rg_json(LONDON_SINGLE, LONDON, "2014-06-02", primary="Single", title="Busy Earnin'")


@pytest.fixture
def lookup(mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock) -> MusicBrainzLookup:
    return MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def _serve_jungle(*, isrc_hits: bool = True) -> None:
    """MusicBrainz as it answered for the real case: both artists for the name search, and the
    ISRC on the London band's album and single only."""
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [LONDON_ALBUM_JSON, US_1969_JSON]})
    )
    recordings = (
        [
            {
                "isrcs": [ISRC],
                "releases": [{"release-group": {"id": LONDON_ALBUM}}, {"release-group": {"id": LONDON_SINGLE}}],
            }
        ]
        if isrc_hits
        else []
    )
    respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json={"recordings": recordings}))
    respx.get(f"{MB_URL}/release-group/{LONDON_ALBUM}").mock(return_value=httpx.Response(200, json=LONDON_ALBUM_JSON))
    respx.get(f"{MB_URL}/release-group/{LONDON_SINGLE}").mock(return_value=httpx.Response(200, json=LONDON_SINGLE_JSON))


def _busy_earnin(*, isrc: str | None = ISRC):
    return track_intent(
        "Busy Earnin'",
        spotify_album("Jungle", artists=("Jungle",), released="2014-07-14"),
        spotify_id="07tOsOR7E9zW89v2FqzsdG",
        isrc=isrc,
        artists=("Jungle",),
    )


# ---------------------------------------------------------------- the adapter's half


@respx.mock
def test_the_search_keeps_both_same_name_artists_instead_of_picking_the_earliest(lookup: MusicBrainzLookup) -> None:
    _serve_jungle()

    found = lookup.search_release_group_candidates("Jungle", "Jungle")

    assert [g.mbid for g in found] == [US_1969, LONDON_ALBUM], "one per artist, in the old tie-break order"
    assert {g.artist_mbid for g in found} == {US, LONDON}
    assert lookup.search_release_group("Jungle", "Jungle") is None, "two artists is doubt, and doubt is None"


@respx.mock
def test_one_artists_same_titled_releases_all_come_back_in_date_then_mbid_order(lookup: MusicBrainzLookup) -> None:
    """The adapter no longer chooses within an artist (the resolver does, knowing what Spotify
    asked for); it returns them all, deterministically ordered. The plain `search_release_group`
    still answers with the earliest, as before."""
    later = _rg_json("00000000-0000-4000-8000-00000000000a", LONDON, "2016")
    same_day_higher = _rg_json("00000000-0000-4000-8000-00000000000c", LONDON, "2014-07-14")
    same_day_lower = _rg_json("00000000-0000-4000-8000-00000000000b", LONDON, "2014-07-14")
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [later, same_day_higher, same_day_lower]})
    )

    found = lookup.search_release_group_candidates("Jungle", "Jungle")

    assert [g.mbid[-2:] for g in found] == ["0b", "0c", "0a"]
    chosen = lookup.search_release_group("Jungle", "Jungle")
    assert chosen is not None and chosen.mbid == "00000000-0000-4000-8000-00000000000b"


@respx.mock
def test_an_exact_title_from_one_artist_still_beats_a_stripped_one_from_another(lookup: MusicBrainzLookup) -> None:
    """Tiers first, artists second: a qualifier-stripped match from a second artist is not a tie."""
    exact = _rg_json(LONDON_ALBUM, LONDON, "2014-07-14", title="Kangaroo")
    stripped = _rg_json(US_1969, US, "1969", title="Kangaroo EP")
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [stripped, exact]})
    )

    assert [g.mbid for g in lookup.search_release_group_candidates("Jungle", "Kangaroo")] == [LONDON_ALBUM]


A_MBID = "00000000-0000-4000-8000-0000000000a1"
B_MBID = "00000000-0000-4000-8000-0000000000b2"


def _credited(mbid: str, *artists: tuple[str, str], released: str = "2010", title: str = "Duets") -> dict:
    """A release group with a multi-artist credit, joined the way MusicBrainz joins one."""
    credit = [
        {"name": name, "joinphrase": " & " if i < len(artists) - 1 else "", "artist": {"id": aid, "name": name}}
        for i, (aid, name) in enumerate(artists)
    ]
    return {
        "id": mbid,
        "title": title,
        "primary-type": "Album",
        "first-release-date": released,
        "artist-credit": credit,
    }


@respx.mock
def test_a_collaboration_credited_both_ways_round_is_not_a_second_artist(lookup: MusicBrainzLookup) -> None:
    """ "A & B" and "B & A" are the same two people. The candidate's artist is its *first* credit,
    which the credit gate compares with Spotify's first artist, so the "B & A" release is refused on
    the credit rather than surfacing as a same-named second artist."""
    a_and_b = _credited("00000000-0000-4000-8000-000000000011", (A_MBID, "Alpha"), (B_MBID, "Beta"))
    b_and_a = _credited("00000000-0000-4000-8000-000000000012", (B_MBID, "Beta"), (A_MBID, "Alpha"), released="2001")
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [b_and_a, a_and_b]})
    )

    found = lookup.search_release_group_candidates("Alpha", "Duets")

    assert [g.artist_mbid for g in found] == [A_MBID]
    chosen = lookup.search_release_group("Alpha", "Duets")
    assert chosen is not None and chosen.mbid == "00000000-0000-4000-8000-000000000011"


@respx.mock
def test_one_artist_with_different_collaborators_is_still_one_artist(lookup: MusicBrainzLookup) -> None:
    """ "A & B" and "A & C" share their first credit, so they are one artist's two releases."""
    with_b = _credited("00000000-0000-4000-8000-000000000021", (A_MBID, "Alpha"), (B_MBID, "Beta"), released="2012")
    with_c = _credited(
        "00000000-0000-4000-8000-000000000022", (A_MBID, "Alpha"), ("00000000-0000-4000-8000-0000000000c3", "Gamma")
    )
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [with_b, with_c]})
    )

    found = lookup.search_release_group_candidates("Alpha", "Duets")
    assert {g.artist_mbid for g in found} == {A_MBID}
    chosen = lookup.search_release_group("Alpha", "Duets")
    assert chosen is not None and chosen.mbid == "00000000-0000-4000-8000-000000000022"


@respx.mock
def test_various_artists_compilations_sharing_a_title_are_one_artist(lookup: MusicBrainzLookup) -> None:
    """Every compilation is credited to the one Various Artists MBID, so same-titled ones never read
    as two different artists; the earliest wins as it always did."""
    from likearr.models import VARIOUS_ARTISTS_MBID

    va = (VARIOUS_ARTISTS_MBID, "Various Artists")
    older = _credited("00000000-0000-4000-8000-000000000031", va, released="1998", title="Now 1")
    newer = _credited("00000000-0000-4000-8000-000000000032", va, released="2018", title="Now 1")
    respx.get(f"{MB_URL}/release-group").mock(return_value=httpx.Response(200, json={"release-groups": [newer, older]}))

    found = lookup.search_release_group_candidates("Various Artists", "Now 1")

    assert {g.artist_mbid for g in found} == {VARIOUS_ARTISTS_MBID}
    chosen = lookup.search_release_group("Various Artists", "Now 1")
    assert chosen is not None and chosen.mbid == "00000000-0000-4000-8000-000000000031"


# ---------------------------------------------------------------- the whole path, real adapter


@respx.mock
def test_the_jungle_like_resolves_to_the_london_band_by_its_isrc(lookup: MusicBrainzLookup) -> None:
    _serve_jungle()

    result = resolve_track(
        _busy_earnin(), lookup, now=NOW, pending_since=None, fallback_days=180, scope=LIKED_TRACK_SCOPE_SMALLEST
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.release_group is not None
    assert result.release_group.artist_mbid == LONDON
    assert result.release_group.mbid == LONDON_SINGLE, "the smallest scope then works inside the right artist"
    assert result.step == "track:smallest:single"
    assert f"ISRC {ISRC}" in result.detail


@respx.mock
def test_the_jungle_like_under_the_album_scope_lands_on_the_london_album(lookup: MusicBrainzLookup) -> None:
    _serve_jungle()

    result = resolve_track(
        _busy_earnin(), lookup, now=NOW, pending_since=None, fallback_days=180, scope=LIKED_TRACK_SCOPE_ALBUM
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.release_group is not None and result.release_group.mbid == LONDON_ALBUM
    assert result.step == "track:album"


@respx.mock
def test_the_jungle_like_with_no_isrc_is_ambiguous_not_the_1969_album(lookup: MusicBrainzLookup) -> None:
    _serve_jungle()

    result = resolve_track(
        _busy_earnin(isrc=None),
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=180,
        scope=LIKED_TRACK_SCOPE_SMALLEST,
    )

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert result.release_group is None
    assert US_1969 in result.detail and LONDON_ALBUM in result.detail, "both candidates are named"


@respx.mock
def test_the_jungle_like_whose_isrc_musicbrainz_does_not_know_is_ambiguous(lookup: MusicBrainzLookup) -> None:
    _serve_jungle(isrc_hits=False)

    result = resolve_track(
        _busy_earnin(), lookup, now=NOW, pending_since=None, fallback_days=180, scope=LIKED_TRACK_SCOPE_SMALLEST
    )

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP


@respx.mock
def test_a_saved_jungle_album_with_no_barcode_is_ambiguous(lookup: MusicBrainzLookup) -> None:
    """A saved album has no ISRC to consult; its UPC is tried first and, here, there is none."""
    _serve_jungle()

    result = resolve_album(album_intent(spotify_album("Jungle", artists=("Jungle",))), lookup)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
