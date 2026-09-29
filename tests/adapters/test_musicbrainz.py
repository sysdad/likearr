"""Tests for the MusicBrainz adapter: queries, the SQLite cache, and outage behaviour."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from likearr.adapters.musicbrainz import (
    POSITIVE_TTL_JITTER,
    MusicBrainzLookup,
    RateLimiter,
    build_user_agent,
    jittered_max_age,
)
from likearr.config import MusicBrainzConfig
from likearr.models import PrimaryType, SecondaryType
from likearr.ports import CatalogueTooLarge, MetadataError, MetadataLookup
from tests.clock import FakeClock

from .conftest import MB_URL

RG_MBID = "00000000-0000-4000-8000-000000000001"
RG2_MBID = "00000000-0000-4000-8000-000000000002"
ARTIST_MBID = "00000000-0000-4000-8000-0000000000a1"
UPC = "0000000000001"
ISRC = "XX0000000001"

ARTIST_CREDIT = [{"name": "Fake Band", "artist": {"id": ARTIST_MBID, "name": "Fake Band"}}]

RG_JSON = {
    "id": RG_MBID,
    "title": "Fake Album",
    "primary-type": "Album",
    "secondary-types": [],
    "first-release-date": "2021-03",
    "artist-credit": ARTIST_CREDIT,
}


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


# ---------------------------------------------------------------------------- rate limiter


def test_rate_limiter_spaces_calls_out(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert clock.slept == [1.0, 1.0]


def test_rate_limiter_does_not_wait_if_enough_time_passed(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    limiter.acquire()
    clock.advance(5.0)
    limiter.acquire()
    assert clock.slept == []


def test_user_agent_carries_the_contact() -> None:
    assert build_user_agent("likearr@example.test") == build_user_agent("likearr@example.test")
    assert "likearr@example.test" in build_user_agent("likearr@example.test")
    assert build_user_agent("x").startswith("likearr/")


# ---------------------------------------------------------------------------- barcode


@respx.mock
def test_release_groups_by_barcode_takes_the_exact_official_match(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    {"barcode": "9999999999999", "status": "Official", "release-group": {"id": "wrong-barcode"}},
                    {
                        "barcode": UPC,
                        "status": "Bootleg",
                        "release-group": {**RG_JSON, "id": RG2_MBID, "title": "Bootleg Pressing"},
                    },
                    {"barcode": UPC, "status": "Official", "release-group": RG_JSON},
                ]
            },
        )
    )
    matches = lookup.release_groups_by_barcode(UPC)

    assert [(m.release_group.mbid, m.official) for m in matches] == [(RG_MBID, True), (RG2_MBID, False)]
    group = matches[0].release_group
    assert group.mbid == RG_MBID
    assert group.title == "Fake Album"
    assert group.artist_mbid == ARTIST_MBID
    assert group.artist_name == "Fake Band"
    assert group.primary_type is PrimaryType.ALBUM
    assert group.secondary_types == frozenset()
    assert group.first_release_date == date(2021, 3, 1)
    assert route.calls[0].request.url.params["fmt"] == "json"
    assert route.calls[0].request.headers["User-Agent"] == build_user_agent("likearr@example.test")


@respx.mock
def test_release_groups_by_barcode_rejects_a_fuzzy_match(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json={"releases": [{"barcode": "9999999999999", "release-group": RG_JSON}]})
    )
    assert lookup.release_groups_by_barcode(UPC) == ()


def _barcode_payload(*releases: tuple[str, str, dict]) -> dict:
    """A barcode search body: one release per (barcode, status, release-group) triple, each release
    credited too. The release groups passed here carry their own credit, which is the one used."""
    return {
        "releases": [
            {"barcode": code, "status": status, "artist-credit": ARTIST_CREDIT, "release-group": group}
            for code, status, group in releases
        ]
    }


@respx.mock
def test_release_groups_by_barcode_ignores_leading_zeros_on_either_side(lookup: MusicBrainzLookup) -> None:
    """Spotify sends 00888072328433, MusicBrainz stores 888072328433. The same GTIN."""
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json=_barcode_payload(("888072328433", "Official", RG_JSON)))
    )
    matches = lookup.release_groups_by_barcode("00888072328433")
    assert [m.release_group.mbid for m in matches] == [RG_MBID]
    assert matches[0].release_group.artist_mbid == ARTIST_MBID, "credited from the release group"


@respx.mock
def test_release_groups_by_barcode_matches_a_13_digit_ean_against_a_14_digit_upc(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json=_barcode_payload(("0044003170988", "Official", RG_JSON)))
    )
    assert [m.release_group.mbid for m in lookup.release_groups_by_barcode("00044003170988")] == [RG_MBID]


@respx.mock
def test_release_groups_by_barcode_never_matches_an_all_zero_barcode(lookup: MusicBrainzLookup) -> None:
    """Nothing is left once the zeros go, so nothing is asked either: no route is mocked here."""
    assert lookup.release_groups_by_barcode("0000000000000") == ()


@respx.mock
def test_an_all_zero_barcode_in_musicbrainz_matches_nothing(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json=_barcode_payload(("000000000000", "Official", RG_JSON)))
    )
    assert lookup.release_groups_by_barcode("0000000000001") == ()


@respx.mock
def test_release_groups_by_barcode_still_refuses_a_different_number(lookup: MusicBrainzLookup) -> None:
    """Only leading zeros are dropped: a trailing zero, or any other digit, is still a mismatch."""
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json=_barcode_payload(("8880723284330", "Official", RG_JSON)))
    )
    assert lookup.release_groups_by_barcode("00888072328433") == ()


@respx.mock
def test_release_groups_by_barcode_returns_every_release_group_once(lookup: MusicBrainzLookup) -> None:
    """The "Tease Me" shape: the artist's compilation listed first, then four pressings of the album.
    Every distinct release group comes back, once each, for the resolver to choose between."""
    compilation = {**RG_JSON, "id": RG2_MBID, "title": "All She Wrote", "secondary-types": ["Compilation"]}
    album = {**RG_JSON, "title": "Tease Me"}
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json=_barcode_payload(
                ("731451884825", "Official", compilation),
                *(("731451884825", "Official", album) for _ in range(4)),
            ),
        )
    )
    matches = lookup.release_groups_by_barcode("00731451884825")
    assert [m.release_group.title for m in matches] == ["All She Wrote", "Tease Me"]
    assert all(m.official for m in matches)


# At the Jazz Corner of the World. Spotify's UPC is the 1994 XW digital release, credited to
# Art Blakey alone; its release group is credited only to Art Blakey & The Jazz Messengers.
JAZZ_CORNER_UPC = "00724382888857"
JAZZ_CORNER_RG = "d5f8521c-58a6-3c56-af99-a1a1c353925d"  # gitleaks:allow (a MusicBrainz id)
ART_BLAKEY = "601e7466-eaf5-4a91-9909-ffd770b7e04a"  # gitleaks:allow (a MusicBrainz id)
JAZZ_MESSENGERS = "209ddf15-ee0a-41a1-a1f5-6f4c0409d2ee"  # gitleaks:allow (a MusicBrainz id)
_JAZZ_CORNER_TITLE = "At the Jazz Corner of the World"


def _jazz_corner_search() -> dict:
    """The release search's answer: the release credited to the person, its release group bare."""
    return {
        "releases": [
            {
                "barcode": "724382888857",
                "status": "Official",
                "artist-credit": [{"name": "Art Blakey", "artist": {"id": ART_BLAKEY, "name": "Art Blakey"}}],
                "release-group": {"id": JAZZ_CORNER_RG, "title": _JAZZ_CORNER_TITLE, "primary-type": "Album"},
            }
        ]
    }


@respx.mock
def test_a_barcode_match_takes_the_release_groups_own_credit_not_the_releases(lookup: MusicBrainzLookup) -> None:
    """Lidarr files an album under its release group's artist, so the release's credit is
    never the answer. The group is fetched (cached) as the ISRC path fetches it."""
    group_credit = [
        {
            "name": "Art Blakey & The Jazz Messengers",
            "artist": {"id": JAZZ_MESSENGERS, "name": "Art Blakey & The Jazz Messengers"},
        }
    ]
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=_jazz_corner_search()))
    respx.get(f"{MB_URL}/release-group/{JAZZ_CORNER_RG}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": JAZZ_CORNER_RG,
                "title": _JAZZ_CORNER_TITLE,
                "primary-type": "Album",
                "first-release-date": "1959",
                "artist-credit": group_credit,
            },
        )
    )

    (match,) = lookup.release_groups_by_barcode(JAZZ_CORNER_UPC)

    group = match.release_group
    assert group.mbid == JAZZ_CORNER_RG
    assert group.artist_mbid == JAZZ_MESSENGERS
    assert group.artist_name == "Art Blakey & The Jazz Messengers"
    assert group.main_artist_mbids == (JAZZ_MESSENGERS,)
    assert match.official
    assert lookup.ok


@respx.mock
def test_a_barcode_match_whose_release_group_is_not_found_is_dropped(lookup: MusicBrainzLookup) -> None:
    """No release group, no match - never the release's credit in its place. A wrong artist
    is worse than no barcode answer; the resolver's name search still runs."""
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=_jazz_corner_search()))
    respx.get(f"{MB_URL}/release-group/{JAZZ_CORNER_RG}").mock(return_value=httpx.Response(404))

    assert lookup.release_groups_by_barcode(JAZZ_CORNER_UPC) == ()


@respx.mock
def test_a_failed_release_group_fetch_fails_the_barcode_lookup(lookup: MusicBrainzLookup) -> None:
    """A MusicBrainz error fetching the group is a lookup error like any other - raised, so
    the composite counts it (``mb_ok``) and the answer is provisional - never the release's credit."""
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=_jazz_corner_search()))
    respx.get(f"{MB_URL}/release-group/{JAZZ_CORNER_RG}").mock(return_value=httpx.Response(503))

    with pytest.raises(MetadataError, match="release-group"):
        lookup.release_groups_by_barcode(JAZZ_CORNER_UPC)
    assert not lookup.ok


# ---------------------------------------------------------------------------- isrc


@respx.mock
def test_release_groups_for_isrc_dedupes(lookup: MusicBrainzLookup) -> None:
    """Recording search by ISRC, then one cached release-group lookup per distinct group."""
    respx.get(f"{MB_URL}/recording").mock(
        return_value=httpx.Response(
            200,
            json={
                "recordings": [
                    {
                        "isrcs": [ISRC],
                        "artist-credit": ARTIST_CREDIT,
                        "releases": [
                            {"release-group": {"id": RG_MBID}},
                            {"release-group": {"id": RG_MBID}},
                            {"release-group": {"id": RG2_MBID}},
                        ],
                    },
                    # a fuzzy hit for a different ISRC is ignored
                    {"isrcs": ["XX9999999999"], "releases": [{"release-group": {"id": "ignored"}}]},
                ],
            },
        )
    )
    respx.get(f"{MB_URL}/release-group/{RG_MBID}").mock(return_value=httpx.Response(200, json=RG_JSON))
    respx.get(f"{MB_URL}/release-group/{RG2_MBID}").mock(
        return_value=httpx.Response(
            200, json={**RG_JSON, "id": RG2_MBID, "title": "Fake Single", "primary-type": "Single"}
        )
    )
    groups = lookup.release_groups_for_isrc(ISRC)
    assert [g.mbid for g in groups] == [RG_MBID, RG2_MBID]
    assert groups[1].primary_type is PrimaryType.SINGLE


@respx.mock
def test_recordings_for_isrc_keeps_each_recordings_title_with_its_release_groups(lookup: MusicBrainzLookup) -> None:
    """The Dean Martin shape - one ISRC on two recordings of different songs. Each keeps its
    own title and release groups; a release group on both is fetched once."""
    search = respx.get(f"{MB_URL}/recording").mock(
        return_value=httpx.Response(
            200,
            json={
                "recordings": [
                    {"title": "Good Mornin' Life", "isrcs": [ISRC], "releases": [{"release-group": {"id": RG_MBID}}]},
                    {
                        "title": "Kiss",
                        "isrcs": [ISRC],
                        "releases": [{"release-group": {"id": RG2_MBID}}, {"release-group": {"id": RG_MBID}}],
                    },
                    {"title": "Other", "isrcs": ["XX9999999999"], "releases": [{"release-group": {"id": "ignored"}}]},
                ],
            },
        )
    )
    first = respx.get(f"{MB_URL}/release-group/{RG_MBID}").mock(return_value=httpx.Response(200, json=RG_JSON))
    respx.get(f"{MB_URL}/release-group/{RG2_MBID}").mock(
        return_value=httpx.Response(200, json={**RG_JSON, "id": RG2_MBID, "title": "Kiss", "primary-type": "Single"})
    )
    recordings = lookup.recordings_for_isrc(ISRC)
    assert [(r.title, [g.mbid for g in r.release_groups]) for r in recordings] == [
        ("Good Mornin' Life", [RG_MBID]),
        ("Kiss", [RG2_MBID, RG_MBID]),
    ]
    assert first.call_count == 1
    assert [g.mbid for g in lookup.release_groups_for_isrc(ISRC)] == [RG_MBID, RG2_MBID]
    assert search.call_count == 1, "the second question is answered from the cache"


@respx.mock
def test_isrc_without_hits_is_a_negative_cache_entry_not_an_error(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json={"recordings": []}))
    assert lookup.release_groups_for_isrc(ISRC) == ()
    assert lookup.release_groups_for_isrc(ISRC) == ()
    assert route.call_count == 1
    assert lookup.ok


@respx.mock
def test_negative_cache_expires(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    route = respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json={"recordings": []}))
    lookup = MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    lookup.release_groups_for_isrc(ISRC)
    clock.advance(mb_config.negative_cache_days * (1 + POSITIVE_TTL_JITTER) * 86400 + 1)
    lookup.release_groups_for_isrc(ISRC)
    assert route.call_count == 2


def test_negative_entries_written_together_fall_due_apart(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """Negative rows written in one run used to expire together at exactly
    `negative_cache_days`, and be refetched together every time after. They are jittered by key now,
    as positive rows are: within `negative_cache_days * (1 + POSITIVE_TTL_JITTER)`, and the same key
    always gets the same age."""
    lookup = MusicBrainzLookup(
        mb_config, client, cache_path=tmp_path / "state.db", now=clock.time, monotonic=clock.monotonic
    )
    keys = [f"isrc-search:XX000000000{n}" for n in range(4)]
    for key in keys:
        lookup._cache_put(key, {"recordings": []}, negative=True)
    days = mb_config.negative_cache_days
    ages = [jittered_max_age(days, key) for key in keys]
    assert len(set(ages)) == len(ages)
    assert all(days <= age <= days * (1 + POSITIVE_TTL_JITTER) for age in ages)
    assert ages == [jittered_max_age(days, key) for key in keys]

    clock.advance(sorted(ages)[1] * 86400 + 1)
    stale = [entry.stale for entry in (lookup._cache_get(key) for key in keys) if entry is not None]
    assert stale == [age <= sorted(ages)[1] for age in ages]
    assert sum(stale) == 2
    lookup.close()


@respx.mock
def test_release_group_by_id_is_cached_positively(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/release-group/{RG_MBID}").mock(return_value=httpx.Response(200, json=RG_JSON))
    first = lookup.release_group_by_id(RG_MBID)
    second = lookup.release_group_by_id(RG_MBID)
    assert first is not None and first == second and first.first_release_date == date(2021, 3, 1)
    assert route.call_count == 1


# ---------------------------------------------------------------------------- name searches


@respx.mock
def test_search_release_group_accepts_an_exact_match(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [RG_JSON]})
    )
    group = lookup.search_release_group("Fake Band", "Fake Album")
    assert group is not None and group.mbid == RG_MBID
    query = route.calls[0].request.url.params["query"]
    assert 'releasegroup:"Fake Album"' in query
    assert 'artist:"Fake Band"' in query


@respx.mock
def test_search_release_group_is_conservative(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200, json={"release-groups": [{**RG_JSON, "title": "Fake Album (Deluxe Reissue 2020)"}]}
        )
    )
    # The bracketed qualifier is stripped, so this one still matches...
    assert lookup.search_release_group("Fake Band", "Fake Album") is not None


@respx.mock
def test_search_release_group_rejects_a_different_title(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [{**RG_JSON, "title": "Some Other Record"}]})
    )
    assert lookup.search_release_group("Fake Band", "Fake Album") is None


@respx.mock
def test_search_release_group_rejects_a_different_artist(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {
                        **RG_JSON,
                        "artist-credit": [{"artist": {"id": "other", "name": "A Tribute Band"}}],
                    }
                ]
            },
        )
    )
    assert lookup.search_release_group("Fake Band", "Fake Album") is None


# ------------------------------------------------------- title-comparison normalisation


@respx.mock
def test_search_release_group_matches_a_bare_trailing_ep_musicbrainz_carries(lookup: MusicBrainzLookup) -> None:
    """Kyle Andrews - 'Kangaroo', MusicBrainz 'Kangaroo EP': Spotify's side has nothing to strip.

    Stripping only Spotify's (already bare) title could never close this gap - the qualifier is
    on MusicBrainz's side, undecorated, so `strip_release_qualifiers` has to run on both.
    """
    kyle_andrews = [{"artist": {"id": ARTIST_MBID, "name": "Kyle Andrews"}}]
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200, json={"release-groups": [{**RG_JSON, "title": "Kangaroo EP", "artist-credit": kyle_andrews}]}
        )
    )
    group = lookup.search_release_group("Kyle Andrews", "Kangaroo")
    assert group is not None and group.title == "Kangaroo EP"


@pytest.mark.parametrize("result_order", [("album", "ep"), ("ep", "album")])
@respx.mock
def test_search_release_group_prefers_an_exact_title_match_over_a_qualifier_stripped_one(
    lookup: MusicBrainzLookup, result_order: tuple[str, str]
) -> None:
    """A real "Kangaroo" album and an unrelated "Kangaroo EP" both exist in MusicBrainz.

    Spotify's "Kangaroo" must resolve to the exact-titled album, never the EP, and that must
    hold whichever order MusicBrainz's own relevance ranking happens to list them in - the
    qualifier-stripped pass exists to *rescue* a match when nothing matches exactly,
    not to outrank a real one that MusicBrainz simply ranked second.
    """
    kyle_andrews = [{"artist": {"id": ARTIST_MBID, "name": "Kyle Andrews"}}]
    album = {**RG_JSON, "id": RG_MBID, "title": "Kangaroo", "artist-credit": kyle_andrews}
    ep = {**RG_JSON, "id": RG2_MBID, "title": "Kangaroo EP", "artist-credit": kyle_andrews}
    ordered = [album, ep] if result_order == ("album", "ep") else [ep, album]
    respx.get(f"{MB_URL}/release-group").mock(return_value=httpx.Response(200, json={"release-groups": ordered}))
    group = lookup.search_release_group("Kyle Andrews", "Kangaroo")
    assert group is not None and group.mbid == RG_MBID and group.title == "Kangaroo"


@respx.mock
def test_search_release_group_matches_a_colon_where_spotify_has_brackets(lookup: MusicBrainzLookup) -> None:
    """Elf: Spotify's bracketed subtitle and MusicBrainz's colon-joined one fold to the same words."""
    various = [{"artist": {"id": ARTIST_MBID, "name": "Various Artists"}}]
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {**RG_JSON, "title": "Elf: Music From the Major Motion Picture", "artist-credit": various}
                ]
            },
        )
    )
    group = lookup.search_release_group("Various Artists", "Elf (Music from the Major Motion Picture)")
    assert group is not None


@respx.mock
def test_search_release_group_matches_after_stripping_a_bracketed_soundtrack_qualifier(
    lookup: MusicBrainzLookup,
) -> None:
    """Noelle: MusicBrainz's plain title only surfaces once the bracketed qualifier is stripped
    from Spotify's side - and 'Original Motion Picture Soundtrack' is now in that vocabulary."""
    clyde_lawrence = [{"artist": {"id": ARTIST_MBID, "name": "Clyde Lawrence"}}]
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200, json={"release-groups": [{**RG_JSON, "title": "Noelle", "artist-credit": clyde_lawrence}]}
        )
    )
    group = lookup.search_release_group("Clyde Lawrence", "Noelle (Original Motion Picture Soundtrack)")
    assert group is not None and group.title == "Noelle"


@respx.mock
def test_search_release_group_still_rejects_an_unrelated_release_with_the_same_title(
    lookup: MusicBrainzLookup,
) -> None:
    """A same-titled album by a wholly unrelated artist, score 100.

    None of the title-side loosening may accept this - only the credit gate, which
    is exact equality, refuses it. Invented names: 'Pellucid Varnish' - 'Harbour Lights' also names
    an album credited to Ondine Karsk.
    """
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {
                        **RG_JSON,
                        "title": "Harbour Lights",
                        "artist-credit": [{"artist": {"id": "ondine-karsk", "name": "Ondine Karsk"}}],
                    }
                ]
            },
        )
    )
    assert lookup.search_release_group("Pellucid Varnish", "Harbour Lights") is None


# ------------------------------------------------------- unquoted retry on an empty search


@respx.mock
def test_search_release_group_retries_unquoted_when_the_quoted_query_finds_nothing(
    lookup: MusicBrainzLookup,
) -> None:
    """A punctuation-sensitive quoted phrase finds nothing; an unquoted retry finds the release."""
    route = respx.get(f"{MB_URL}/release-group")
    route.side_effect = [
        httpx.Response(200, json={"release-groups": []}),
        httpx.Response(200, json={"release-groups": [RG_JSON]}),
    ]
    group = lookup.search_release_group("Fake Band", "Fake Album")
    assert group is not None and group.mbid == RG_MBID
    assert route.call_count == 2
    first_query = route.calls[0].request.url.params["query"]
    second_query = route.calls[1].request.url.params["query"]
    assert 'releasegroup:"Fake Album"' in first_query
    assert 'releasegroup:"Fake Album"' not in second_query
    assert "releasegroup:(Fake Album)" in second_query


@respx.mock
def test_search_release_group_does_not_retry_when_the_quoted_query_found_candidates(
    lookup: MusicBrainzLookup,
) -> None:
    """Candidates that fail the equality gate are a real refusal, not a query problem - no retry."""
    route = respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(200, json={"release-groups": [{**RG_JSON, "title": "Some Other Record"}]})
    )
    assert lookup.search_release_group("Fake Band", "Fake Album") is None
    assert route.call_count == 1


@respx.mock
def test_search_release_group_unquoted_retry_still_rejects_an_unrelated_artist(
    lookup: MusicBrainzLookup,
) -> None:
    """The query got looser (fault 2); the credit gate it feeds must not have."""
    route = respx.get(f"{MB_URL}/release-group")
    route.side_effect = [
        httpx.Response(200, json={"release-groups": []}),
        httpx.Response(
            200,
            json={
                "release-groups": [
                    {
                        **RG_JSON,
                        "title": "Stacy's Mom",
                        "artist-credit": [{"artist": {"id": "ralph", "name": "Ralph"}}],
                    }
                ]
            },
        ),
    ]
    assert lookup.search_release_group("stories", "Stacy's Mom") is None
    assert route.call_count == 2


# ------------------------------------------------------- artist-credit fold


@respx.mock
def test_search_release_group_matches_credit_differing_only_by_a_leading_the(
    lookup: MusicBrainzLookup,
) -> None:
    """Branford Marsalis Quartet / The Branford Marsalis Quartet - `normalize_name` drops "the"."""
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {
                        **RG_JSON,
                        "artist-credit": [{"artist": {"id": ARTIST_MBID, "name": "The Branford Marsalis Quartet"}}],
                    }
                ]
            },
        )
    )
    group = lookup.search_release_group("Branford Marsalis Quartet", "Fake Album")
    assert group is not None


@respx.mock
def test_search_release_group_matches_a_spotify_featuring_decoration_on_the_credit(
    lookup: MusicBrainzLookup,
) -> None:
    """The Marty Paich Quartet featuring Art Pepper -> The Marty Paich Quartet."""
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {
                        **RG_JSON,
                        "artist-credit": [{"artist": {"id": ARTIST_MBID, "name": "The Marty Paich Quartet"}}],
                    }
                ]
            },
        )
    )
    group = lookup.search_release_group("The Marty Paich Quartet featuring Art Pepper", "Fake Album")
    assert group is not None


@respx.mock
def test_search_release_group_still_rejects_a_credit_containing_the_spotify_one(
    lookup: MusicBrainzLookup,
) -> None:
    """John Mayer / John Mayer Trio: containment, not equality - a different identity question,
    not this one's. The credit fold must not accidentally start accepting it."""
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {**RG_JSON, "artist-credit": [{"artist": {"id": ARTIST_MBID, "name": "John Mayer Trio"}}]}
                ]
            },
        )
    )
    assert lookup.search_release_group("John Mayer", "Fake Album") is None


@respx.mock
def test_a_release_group_keeps_every_credited_artist(lookup: MusicBrainzLookup) -> None:
    """A classical credit is composer first, then the performers. The first stays the
    release group's artist; the whole credit is kept beside it, in order."""
    credit = [
        {"name": "Jean Sibelius", "joinphrase": "; ", "artist": {"id": "sibelius", "name": "Jean Sibelius"}},
        {"name": "London Philharmonic Orchestra", "joinphrase": ", ", "artist": {"id": "lpo", "name": "LPO"}},
        {"name": "Paavo Berglund", "artist": {"id": "berglund", "name": "Paavo Berglund"}},
    ]
    respx.get(f"{MB_URL}/release-group/{RG_MBID}").mock(
        return_value=httpx.Response(200, json={**RG_JSON, "artist-credit": credit})
    )
    group = lookup.release_group_by_id(RG_MBID)
    assert group is not None
    assert (group.artist_mbid, group.artist_name) == ("sibelius", "Jean Sibelius")
    assert group.main_artist_mbids == ("sibelius", "lpo", "berglund")


def _credit(*entries: tuple[str, str]) -> list[dict]:
    """An artist credit from (mbid, joinphrase) pairs; the joinphrase joins an entry to the next."""
    return [{"name": m, "joinphrase": jp, "artist": {"id": m, "name": m}} for m, jp in entries]


@pytest.mark.parametrize(
    ("credit", "main"),
    [
        (_credit(("dirty", " feat. "), ("dawn", "")), ("dirty",)),
        (_credit(("francis", " feat. "), ("chance", "")), ("francis",)),
        (_credit(("batiste", " feat. "), ("jid", ", "), ("newjeans", " & "), ("camilo", "")), ("batiste",)),
        (_credit(("scary", " ft. "), ("stacey", "")), ("scary",)),
        (_credit(("sammy", " feat "), ("will", "")), ("sammy",)),
        (_credit(("getz", " / "), ("gilberto", " featuring "), ("jobim", "")), ("getz", "gilberto")),
        (_credit(("dylan", " Featuring "), ("petty", "")), ("dylan",)),
        (_credit(("mingus", " (feat. "), ("dolphy", ")")), ("mingus",)),
        # "with" is co-billing as often as a guest, so it stays a main credit
        (_credit(("getz", " With "), ("fiedler", "")), ("getz", "fiedler")),
        (_credit(("sibelius", "; "), ("orchestra", " & "), ("conductor", "")), ("sibelius", "orchestra", "conductor")),
        (_credit(("solo", "")), ("solo",)),
    ],
)
def test_featured_guests_are_left_out_of_the_main_artists(
    lookup: MusicBrainzLookup, credit: list[dict], main: tuple[str, ...]
) -> None:
    """A join phrase joins a credit to the next, so everything after "feat."/"ft."/"featuring"
    (any case, spaces and punctuation aside) is a guest."""
    with respx.mock:
        respx.get(f"{MB_URL}/release-group/{RG_MBID}").mock(
            return_value=httpx.Response(200, json={**RG_JSON, "artist-credit": credit})
        )
        group = lookup.release_group_by_id(RG_MBID)
    assert group is not None
    assert group.main_artist_mbids == main
    assert group.artist_mbid == credit[0]["artist"]["id"]


@respx.mock
def test_search_artist_prefers_the_perfect_score(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/artist").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": [
                    {"id": "low-score", "name": "Fake Band", "score": 62},
                    {"id": ARTIST_MBID, "name": "Fake Band", "score": 100},
                ]
            },
        )
    )
    assert lookup.search_artist("Fake Band") == (ARTIST_MBID, "Fake Band")


@respx.mock
def test_search_artist_rejects_a_near_miss(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/artist").mock(
        return_value=httpx.Response(200, json={"artists": [{"id": ARTIST_MBID, "name": "Fake Bands", "score": 98}]})
    )
    assert lookup.search_artist("Fake Band") is None


@respx.mock
def test_search_artist_candidates_returns_every_exact_name_match_best_scored_first(lookup: MusicBrainzLookup) -> None:
    """From the recorded "Evangeline" search: five artists share the name, and the one the
    user listens to scores third. A near miss is not a candidate; one search answers both methods."""
    route = respx.get(f"{MB_URL}/artist").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": [
                    {"id": "seattle", "name": "Evangeline", "score": 100},
                    {"id": "new-orleans", "name": "Evangeline", "score": 98},
                    {"id": "near-miss", "name": "Evangelines", "score": 97},
                    {"id": "la", "name": "Evangeline", "score": 97},
                    {"id": "by-sort-name", "name": "EVANGELINE!", "sort-name": "Evangeline", "score": 95},
                ]
            },
        )
    )
    assert lookup.search_artist_candidates("Evangeline") == (
        ("seattle", "Evangeline"),
        ("new-orleans", "Evangeline"),
        ("la", "Evangeline"),
        ("by-sort-name", "EVANGELINE!"),
    )
    assert lookup.search_artist("Evangeline") == ("seattle", "Evangeline")
    assert route.call_count == 1
    assert lookup.search_artist_candidates("") == ()


# ---------------------------------------------------------------------------- browse


@respx.mock
def test_artist_release_groups_paginate(lookup: MusicBrainzLookup) -> None:
    page1 = [{**RG_JSON, "id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(100)]
    page2 = [{**RG_JSON, "id": "00000000-0000-4000-8000-0000000000ff"}]
    respx.get(f"{MB_URL}/release-group").mock(
        side_effect=[
            httpx.Response(200, json={"release-groups": page1, "release-group-count": 101}),
            httpx.Response(200, json={"release-groups": page2, "release-group-count": 101}),
        ]
    )
    groups = lookup.artist_release_groups(ARTIST_MBID)
    assert len(groups) == 101


def _next_run(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> MusicBrainzLookup:
    """A fresh lookup on the same cache file: what the next scheduled run sees."""
    return MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@respx.mock
def test_artist_release_groups_are_browsed_once_per_run_but_fresh_every_run(
    lookup: MusicBrainzLookup, mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """A followed artist exists to catch new releases, so every RUN browses again - but an artist
    with several liked singles is browsed only once within a run."""
    page = {"release-groups": [RG_JSON], "release-group-count": 1}
    route = respx.get(f"{MB_URL}/release-group").mock(return_value=httpx.Response(200, json=page))

    assert len(lookup.artist_release_groups(ARTIST_MBID)) == 1
    assert len(lookup.artist_release_groups(ARTIST_MBID)) == 1
    assert route.call_count == 1  # memoised within the run
    assert len(_next_run(mb_config, client, tmp_path, clock).artist_release_groups(ARTIST_MBID)) == 1
    assert route.call_count == 2  # never served from the disk cache on a healthy run


@respx.mock
def test_artist_release_groups_still_fall_back_to_the_cache_on_an_outage(
    lookup: MusicBrainzLookup, mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        side_effect=[
            httpx.Response(200, json={"release-groups": [RG_JSON], "release-group-count": 1}),
            *[httpx.Response(503)] * 4,
        ]
    )
    assert len(lookup.artist_release_groups(ARTIST_MBID)) == 1
    later = _next_run(mb_config, client, tmp_path, clock)
    assert len(later.artist_release_groups(ARTIST_MBID)) == 1  # outage: last known catalogue
    assert not later.ok


def test_various_artists_is_never_browsed(lookup: MusicBrainzLookup) -> None:
    from likearr.models import VARIOUS_ARTISTS_MBID

    with pytest.raises(MetadataError, match="Various Artists"):
        lookup.artist_release_groups(VARIOUS_ARTISTS_MBID)


@respx.mock
def test_a_runaway_catalogue_stops_at_the_page_cap(lookup: MusicBrainzLookup) -> None:
    from likearr.adapters import musicbrainz as mb

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        rgs = [{**RG_JSON, "id": f"00000000-0000-4000-8000-{offset + i:012d}"} for i in range(100)]
        return httpx.Response(200, json={"release-groups": rgs, "release-group-count": 10_000_000})

    route = respx.get(f"{MB_URL}/release-group").mock(side_effect=page)
    with pytest.raises(CatalogueTooLarge, match="not browsing further"):
        lookup.artist_release_groups(ARTIST_MBID)
    assert route.call_count == mb._MAX_CATALOGUE_PAGES


@respx.mock
def test_a_runaway_catalogue_is_crawled_once_per_run(lookup: MusicBrainzLookup) -> None:
    """Several liked tracks by one composer each asked for the whole 30-page crawl again. The
    failure is remembered for the life of the lookup (one run), and every ask raises it."""
    from likearr.adapters import musicbrainz as mb

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        rgs = [{**RG_JSON, "id": f"00000000-0000-4000-8000-{offset + i:012d}"} for i in range(100)]
        return httpx.Response(200, json={"release-groups": rgs, "release-group-count": 10_000_000})

    route = respx.get(f"{MB_URL}/release-group").mock(side_effect=page)
    for _ in range(2):
        with pytest.raises(CatalogueTooLarge, match="not browsing further"):
            lookup.artist_release_groups(ARTIST_MBID)
    assert route.call_count == mb._MAX_CATALOGUE_PAGES


# ---------------------------------------------------------------------------- outward links

SP_ARTIST = "4Z8W4fKeB5YxbusRsdQVPb"
SP_ALBUM = "6dVIqQ8qmQ5GBnJ9shOYGE"


def url_rel(resource: str, rel_type: str = "free streaming") -> dict:
    return {"type": rel_type, "target-type": "url", "url": {"resource": resource}}


def digital(barcode: str, *resources: str) -> dict:
    return {
        "status": "Official",
        "barcode": barcode,
        "media": [{"format": "Digital Media"}],
        "relations": [url_rel(r) for r in resources],
    }


@respx.mock
def test_spotify_artist_id_comes_from_the_url_relationship(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/artist/{ARTIST_MBID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": ARTIST_MBID,
                "relations": [
                    url_rel("https://www.discogs.com/artist/3840", "discogs"),
                    url_rel(f"https://open.spotify.com/artist/{SP_ARTIST}"),
                ],
            },
        )
    )
    assert lookup.spotify_artist_id(ARTIST_MBID) == SP_ARTIST
    assert dict(httpx.URL(str(route.calls[0].request.url)).params)["inc"] == "url-rels"


SP_LAWRENCE = "5rwUYLyUq8gBsVaOUcUxpE"


def artist_rel(mbid: str, name: str, disambiguation: str = "") -> dict:
    return {"type": "free streaming", "artist": {"id": mbid, "name": name, "disambiguation": disambiguation}}


@respx.mock
def test_a_spotify_artist_url_resolves_to_its_musicbrainz_artists(lookup: MusicBrainzLookup) -> None:
    """The authoritative direction. A name search picked a German DJ for this exact page."""
    route = respx.get(f"{MB_URL}/url").mock(
        return_value=httpx.Response(
            200,
            json={
                "relations": [
                    artist_rel("b6e422c0", "Lawrence", "Clyde Lawrence and Gracie Lawrence"),
                    artist_rel("cf8e5830", "Lawrence", "eurobeat artist"),
                ]
            },
        )
    )
    found = lookup.artists_for_spotify_artist(SP_LAWRENCE)

    assert [c.mbid for c in found] == ["b6e422c0", "cf8e5830"]
    assert found[0].disambiguation == "Clyde Lawrence and Gracie Lawrence"
    params = dict(httpx.URL(str(route.calls[0].request.url)).params)
    assert params["resource"] == f"https://open.spotify.com/artist/{SP_LAWRENCE}"
    assert params["inc"] == "artist-rels"


@respx.mock
def test_an_unlinked_spotify_artist_is_empty_not_an_error(lookup: MusicBrainzLookup) -> None:
    """MusicBrainz 404s a URL it has never seen; the resolver then falls back to the name search."""
    respx.get(f"{MB_URL}/url").mock(return_value=httpx.Response(404, json={"error": "Not Found"}))
    assert lookup.artists_for_spotify_artist(SP_LAWRENCE) == ()


@respx.mock
def test_the_spotify_artist_url_lookup_is_cached(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/url").mock(
        return_value=httpx.Response(200, json={"relations": [artist_rel("b6e422c0", "Lawrence")]})
    )
    assert lookup.artists_for_spotify_artist(SP_LAWRENCE)
    assert lookup.artists_for_spotify_artist(SP_LAWRENCE)
    assert route.call_count == 1


@respx.mock
def test_relations_without_an_artist_are_ignored(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/url").mock(
        return_value=httpx.Response(
            200, json={"relations": [{"type": "free streaming"}, artist_rel("b6e422c0", "Lawrence")]}
        )
    )
    assert [c.mbid for c in lookup.artists_for_spotify_artist(SP_LAWRENCE)] == ["b6e422c0"]


@respx.mock
def test_artist_disambiguation_is_read_from_the_artist_lookup(lookup: MusicBrainzLookup) -> None:
    """ "Germany DJ & producer" is what makes a name-collision report actionable."""
    respx.get(f"{MB_URL}/artist/{ARTIST_MBID}").mock(
        return_value=httpx.Response(
            200, json={"id": ARTIST_MBID, "disambiguation": "Germany DJ & producer", "relations": []}
        )
    )
    assert lookup.artist_disambiguation(ARTIST_MBID) == "Germany DJ & producer"


@respx.mock
def test_an_artist_with_no_disambiguation_gives_an_empty_string(lookup: MusicBrainzLookup) -> None:
    """Plenty of artists have none. That is normal, not an error."""
    respx.get(f"{MB_URL}/artist/{ARTIST_MBID}").mock(
        return_value=httpx.Response(200, json={"id": ARTIST_MBID, "relations": []})
    )
    assert lookup.artist_disambiguation(ARTIST_MBID) == ""


@respx.mock
def test_the_disambiguation_shares_the_spotify_link_lookups_cache(lookup: MusicBrainzLookup) -> None:
    """Both read one `artist/<mbid>?inc=url-rels`, so asking for both costs one request."""
    route = respx.get(f"{MB_URL}/artist/{ARTIST_MBID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": ARTIST_MBID,
                "disambiguation": "Germany DJ & producer",
                "relations": [url_rel(f"https://open.spotify.com/artist/{SP_ARTIST}")],
            },
        )
    )
    assert lookup.spotify_artist_id(ARTIST_MBID) == SP_ARTIST
    assert lookup.artist_disambiguation(ARTIST_MBID) == "Germany DJ & producer"
    assert route.call_count == 1


@respx.mock
def test_spotify_artist_id_is_none_when_nothing_links_there(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/artist/{ARTIST_MBID}").mock(
        return_value=httpx.Response(200, json={"id": ARTIST_MBID, "relations": [url_rel("https://last.fm/x", "x")]})
    )
    assert lookup.spotify_artist_id(ARTIST_MBID) is None


@respx.mock
def test_spotify_album_id_is_read_off_the_groups_digital_release(lookup: MusicBrainzLookup) -> None:
    """Spotify album links live on releases, never on the release group - checked against the API."""
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    {"status": "Official", "barcode": "0724385522925", "media": [{"format": "CD"}]},
                    digital("0634904078164", f"https://open.spotify.com/album/{SP_ALBUM}"),
                ]
            },
        )
    )
    assert lookup.spotify_album_id(RG_MBID) == SP_ALBUM
    params = dict(httpx.URL(str(route.calls[0].request.url)).params)
    assert params["inc"] == "url-rels media"
    assert params["limit"] == "100", "the digital release is rarely in the first few of an unsorted browse"


@respx.mock
def test_several_spotify_albums_on_one_group_is_no_answer_at_all(lookup: MusicBrainzLookup) -> None:
    """Regional duplicates. Probably the same record, but the UPC and title tiers can say so."""
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    digital("1", f"https://open.spotify.com/album/{SP_ALBUM}"),
                    digital("2", "https://open.spotify.com/album/7dxKtc08dYeRVHt3p9CZJn"),
                ]
            },
        )
    )
    assert lookup.spotify_album_id(RG_MBID) is None


@respx.mock
def test_the_same_spotify_album_on_several_releases_is_still_one_answer(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    digital("1", f"https://open.spotify.com/album/{SP_ALBUM}"),
                    digital("2", f"https://open.spotify.com/intl-de/album/{SP_ALBUM}"),
                ]
            },
        )
    )
    assert lookup.spotify_album_id(RG_MBID) == SP_ALBUM


@respx.mock
def test_a_link_to_the_wrong_entity_type_is_not_an_album(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200, json={"releases": [digital("1", f"https://open.spotify.com/track/{SP_ALBUM}")]}
        )
    )
    assert lookup.spotify_album_id(RG_MBID) is None


@respx.mock
def test_links_and_barcodes_share_one_request(lookup: MusicBrainzLookup) -> None:
    """Both questions are answered off one cached browse, so an album costs one MusicBrainz call."""
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200, json={"releases": [digital("0634904078164", f"https://open.spotify.com/album/{SP_ALBUM}")]}
        )
    )
    assert lookup.spotify_album_id(RG_MBID) == SP_ALBUM
    assert lookup.release_group_barcodes(RG_MBID) == ("0634904078164",)
    assert route.call_count == 1


@respx.mock
def test_release_group_barcodes_prefers_the_official_digital_pressing(lookup: MusicBrainzLookup) -> None:
    """The digital release's barcode is the one Spotify is most likely to carry."""
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    {"status": "Promotion", "barcode": "0000000000001", "media": [{"format": "CD"}]},
                    {"status": "Official", "barcode": "0724385522925", "media": [{"format": "CD"}]},
                    digital("0634904078164"),
                    {"status": "Official", "barcode": "0724385522925", "media": [{"format": "Vinyl"}]},
                    {"status": "Official", "barcode": "", "media": [{"format": "CD"}]},
                    {"status": "Official", "media": [{"format": "CD"}]},
                ]
            },
        )
    )
    assert lookup.release_group_barcodes(RG_MBID) == ("0634904078164", "0724385522925", "0000000000001")


@respx.mock
def test_release_group_barcodes_is_empty_when_nothing_carries_one(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json={"releases": []}))
    assert lookup.release_group_barcodes(RG_MBID) == ()


@respx.mock
def test_release_group_track_titles_prefers_official(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    {
                        "status": "Promotion",
                        "media": [{"tracks": [{"title": "Promo Only"}]}],
                    },
                    {
                        "status": "Official",
                        "media": [
                            {"tracks": [{"title": "One"}, {"title": "Two"}]},
                            {"tracks": [{"title": "Three"}]},
                        ],
                    },
                ]
            },
        )
    )
    assert lookup.release_group_track_titles(RG_MBID) == ("One", "Two", "Three")


@respx.mock
def test_secondary_types_are_mapped_and_unknowns_ignored(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [{**RG_JSON, "secondary-types": ["Live", "Compilation", "Something New MB Invented"]}]
            },
        )
    )
    group = lookup.search_release_group("Fake Band", "Fake Album")
    assert group is not None
    assert group.secondary_types == frozenset({SecondaryType.LIVE, SecondaryType.COMPILATION})


# ---------------------------------------------------------------------------- caching


@respx.mock
def test_a_cache_hit_avoids_http(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]})
    )
    assert lookup.release_groups_by_barcode(UPC)
    assert lookup.release_groups_by_barcode(UPC)
    assert route.call_count == 1


@respx.mock
def test_live_calls_and_cache_hits_are_counted_separately(lookup: MusicBrainzLookup) -> None:
    """The shell's progress line needs "how much of this run was actually live
    network work" versus "how much was free". The first lookup goes out live; the second is
    answered from the fresh cache entry the first one wrote."""
    respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]})
    )
    assert lookup.live_calls == 0
    assert lookup.cache_hits == 0

    lookup.release_groups_by_barcode(UPC)
    assert lookup.live_calls == 1
    assert lookup.cache_hits == 0

    lookup.release_groups_by_barcode(UPC)
    assert lookup.live_calls == 1
    assert lookup.cache_hits == 1


@respx.mock
def test_the_cache_is_persistent_across_instances(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    cache = tmp_path / "state.db"
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]})
    )
    first = MusicBrainzLookup(mb_config, client, cache_path=cache, now=clock.time, sleep=clock.sleep)
    first.release_groups_by_barcode(UPC)
    first.close()

    second = MusicBrainzLookup(mb_config, client, cache_path=cache, now=clock.time, sleep=clock.sleep)
    assert second.release_groups_by_barcode(UPC)
    assert route.call_count == 1
    second.close()

    with sqlite3.connect(cache) as db:
        (count,) = db.execute("SELECT COUNT(*) FROM mb_cache WHERE negative = 0").fetchone()
    assert count == 1


@respx.mock
def test_failure_with_a_cached_value_returns_the_cached_value(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """A MusicBrainz outage must keep the last known mapping, not look like 'not found'."""
    respx.get(f"{MB_URL}/release").mock(
        side_effect=[
            httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]}),
            *[httpx.Response(503)] * 4,
        ]
    )
    # max_age_days=0 expires every positive entry immediately, so the second call must refetch.
    lookup = MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        max_age_days=0,
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert lookup.release_groups_by_barcode(UPC)
    assert lookup.ok

    (match,) = lookup.release_groups_by_barcode(UPC)
    assert match.release_group.mbid == RG_MBID
    assert lookup.errors == 1
    assert not lookup.ok


# ------------------------------------------------- positive entries expire


def _ttl_lookup(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock, days: int
) -> MusicBrainzLookup:
    return MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        max_age_days=days,
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@respx.mock
def test_a_positive_entry_is_refetched_once_it_is_past_its_max_age(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """A corrected Spotify link or a MusicBrainz artist merge was never seen before this."""
    route = respx.get(f"{MB_URL}/release").mock(
        return_value=httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]})
    )
    lookup = _ttl_lookup(mb_config, client, tmp_path, clock, 90)
    lookup.release_groups_by_barcode(UPC)

    clock.advance(89 * 86400)
    lookup.release_groups_by_barcode(UPC)
    assert route.call_count == 1, "still fresh"

    clock.advance(30 * 86400)  # past 90 days plus the widest possible jitter
    lookup.release_groups_by_barcode(UPC)
    assert route.call_count == 2
    assert lookup.ok, "a successful refetch is not an error"
    lookup.close()


def test_the_positive_ttl_is_jittered_per_key_so_the_whole_cache_never_expires_at_once() -> None:
    """A first run writes every entry on one day; expiring them together would blow the 1 req/s budget."""
    from likearr.adapters.musicbrainz import POSITIVE_TTL_JITTER, jittered_max_age

    ages = {jittered_max_age(90.0, f"rg:{n}") for n in range(500)}

    assert min(ages) >= 90.0
    assert max(ages) <= 90.0 * (1.0 + POSITIVE_TTL_JITTER)
    assert len(ages) > 400, "the spread is real, not a handful of buckets"
    assert jittered_max_age(90.0, "rg:1") == jittered_max_age(90.0, "rg:1"), "deterministic"
    assert jittered_max_age(0.0, "rg:1") == 0.0, "a zero TTL still means 'always refetch'"


@respx.mock
def test_a_failed_refetch_keeps_serving_the_stale_entry_and_counts_it(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """The rule is unchanged: a failed lookup never drops a mapping. It is now visible."""
    respx.get(f"{MB_URL}/release").mock(
        side_effect=[
            httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]}),
            *[httpx.Response(503)] * 4,
        ]
    )
    lookup = _ttl_lookup(mb_config, client, tmp_path, clock, 0)
    assert lookup.release_groups_by_barcode(UPC)
    assert lookup.stale_served == 0

    (match,) = lookup.release_groups_by_barcode(UPC)

    assert match.release_group.mbid == RG_MBID
    assert lookup.stale_served == 1
    assert lookup.errors == 1
    lookup.close()


@respx.mock
def test_failure_without_a_cached_value_raises(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(503))
    with pytest.raises(MetadataError, match="musicbrainz"):
        lookup.release_groups_by_barcode(UPC)
    assert lookup.errors == 1
    assert not lookup.ok


@respx.mock
def test_a_non_json_body_is_a_metadata_error(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, text="<html>rate limited</html>"))
    with pytest.raises(MetadataError):
        lookup.release_groups_by_barcode(UPC)
    assert not lookup.ok


@respx.mock
def test_503_is_retried_with_backoff(lookup: MusicBrainzLookup, clock: FakeClock) -> None:
    respx.get(f"{MB_URL}/release").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"releases": [{"barcode": UPC, "release-group": RG_JSON}]}),
        ]
    )
    assert lookup.release_groups_by_barcode(UPC)
    assert 1.0 in clock.slept
    assert lookup.ok


def test_empty_inputs_short_circuit(lookup: MusicBrainzLookup) -> None:
    assert lookup.search_release_group("", "Fake Album") is None
    assert lookup.search_release_group("Fake Band", "  ") is None
    assert lookup.search_artist("") is None


# ---------------------------------------------------------------------------- port conformance


def test_lookup_satisfies_metadata_lookup(lookup: MusicBrainzLookup) -> None:
    """Statically checked by pyright: the resolver must be able to take this as a MetadataLookup."""
    port: MetadataLookup = lookup
    assert callable(port.release_groups_by_barcode)


# ---------------------------------------------------------------- regressions from early versions


def test_normalize_keeps_unicode_hyphen_as_a_separator() -> None:
    """MusicBrainz titles use U+2010; Spotify uses '-'. Both must fold to the same words."""
    from likearr.adapters.musicbrainz import _normalize

    assert _normalize("The All\u2010American Rejects") == _normalize("The All-American Rejects")
    assert _normalize("Don\u2019t Stop") == _normalize("Don't Stop")
    assert _normalize("Sigur Rós") == _normalize("Sigur Ros")
    assert _normalize("ジブリ") != _normalize("ドラゴン")  # non-Latin never collapses to ""


# ---------------------------------------------------------------- disambiguations from the cache alone


def test_cached_disambiguations_read_the_cache_and_ask_nobody(tmp_path: Path) -> None:
    import json

    from likearr.adapters.musicbrainz import CachedDisambiguations

    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE mb_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at INTEGER, negative INTEGER)"
    )
    conn.execute(
        "INSERT INTO mb_cache VALUES (?, ?, 0, 0)",
        ("artist-urls:wanted", json.dumps({"disambiguation": " US psychedelic rock ", "relations": []})),
    )
    conn.execute("INSERT INTO mb_cache VALUES (?, ?, 0, 0)", ("artist-urls:plain", json.dumps({"relations": []})))
    conn.commit()
    conn.close()

    with CachedDisambiguations(db) as disambiguation:
        assert disambiguation("wanted") == "US psychedelic rock"
        assert disambiguation("plain") is None
        assert disambiguation("unknown") is None


def test_cached_disambiguations_without_a_database_know_nothing_and_create_nothing(tmp_path: Path) -> None:
    from likearr.adapters.musicbrainz import CachedDisambiguations

    with CachedDisambiguations(tmp_path / "nope.sqlite") as disambiguation:
        assert disambiguation("wanted") is None

    assert not (tmp_path / "nope.sqlite").exists()


# ---------------------------------------------------------------------------- other-credit search


TRIO_MBID = "00000000-0000-4000-8000-0000000000b1"
MAYER_MBID = "00000000-0000-4000-8000-0000000000b2"
TRIO_CREDIT = [{"name": "John Mayer Trio", "artist": {"id": TRIO_MBID, "name": "John Mayer Trio"}}]
MAYER_CREDIT = [{"name": "John Mayer", "artist": {"id": MAYER_MBID, "name": "John Mayer"}}]


@respx.mock
def test_other_credits_are_the_title_matches_the_credit_gate_refused(lookup: MusicBrainzLookup) -> None:
    """The same search and the same title tiers; only the credit comparison is inverted. It is
    answered from the name search's own cache entry, so asking both costs one request."""
    route = respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {**RG_JSON, "id": RG_MBID, "title": "Try!", "artist-credit": TRIO_CREDIT},
                    {**RG_JSON, "id": RG2_MBID, "title": "Something Else", "artist-credit": TRIO_CREDIT},
                    {**RG_JSON, "id": "rg-own", "title": "Try!", "artist-credit": MAYER_CREDIT},
                ]
            },
        )
    )

    own = lookup.search_release_group_candidates("John Mayer", "TRY! - Live In Concert")
    others = lookup.release_groups_under_other_credits("John Mayer", "TRY! - Live In Concert")

    assert [g.mbid for g in own] == ["rg-own"]
    assert [(g.mbid, g.artist_name) for g in others] == [(RG_MBID, "John Mayer Trio")]
    assert route.call_count == 1


@respx.mock
def test_other_credits_follow_the_unquoted_retry_too(lookup: MusicBrainzLookup) -> None:
    route = respx.get(f"{MB_URL}/release-group").mock(
        side_effect=[
            httpx.Response(200, json={"release-groups": []}),
            httpx.Response(200, json={"release-groups": [{**RG_JSON, "title": "Try!", "artist-credit": TRIO_CREDIT}]}),
        ]
    )
    assert lookup.search_release_group_candidates("John Mayer", "Try!") == ()
    assert [g.mbid for g in lookup.release_groups_under_other_credits("John Mayer", "Try!")] == [RG_MBID]
    assert route.call_count == 2, "the quoted and the unquoted search, both by the name search"


@respx.mock
def test_other_credits_never_ask_musicbrainz_themselves(lookup: MusicBrainzLookup) -> None:
    """Cache only: a search that was never answered leaves nothing, and asks for nothing."""
    route = respx.get(f"{MB_URL}/release-group").mock(return_value=httpx.Response(503))
    assert lookup.release_groups_under_other_credits("John Mayer", "Try!") == ()
    assert route.call_count == 0
    assert lookup.ok


@respx.mock
def test_other_credits_read_a_stale_entry_without_refetching_it(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """Through an outage the name search serves its stale entry; this reads the same one, and
    does not pay a second failing request for it."""
    route = respx.get(f"{MB_URL}/release-group").mock(
        side_effect=[
            httpx.Response(200, json={"release-groups": [{**RG_JSON, "title": "Try!", "artist-credit": TRIO_CREDIT}]}),
            *[httpx.Response(503)] * 4,
        ]
    )
    lookup = _ttl_lookup(mb_config, client, tmp_path, clock, 0)
    assert lookup.search_release_group_candidates("John Mayer", "Try!") == ()
    clock.advance(86400)
    assert lookup.search_release_group_candidates("John Mayer", "Try!") == (), "stale, refetch failed"
    calls = route.call_count

    assert [g.mbid for g in lookup.release_groups_under_other_credits("John Mayer", "Try!")] == [RG_MBID]
    assert route.call_count == calls, "no request of its own"
    lookup.close()


@respx.mock
def test_other_credits_never_include_a_credit_the_gate_accepts_or_one_without_an_mbid(
    lookup: MusicBrainzLookup,
) -> None:
    respx.get(f"{MB_URL}/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-groups": [
                    {**RG_JSON, "artist-credit": [{"artist": {"id": ARTIST_MBID, "name": "The Fake Band"}}]},
                    {**RG_JSON, "id": RG2_MBID, "artist-credit": [{"name": "No Id Band"}]},
                ]
            },
        )
    )
    assert lookup.release_groups_under_other_credits("Fake Band", "Fake Album") == ()


def _member(mbid: str, name: str, kind: str = "member of band", direction: str = "backward") -> dict:
    return {"type": kind, "direction": direction, "target-type": "artist", "artist": {"id": mbid, "name": name}}


@respx.mock
def test_artist_relations_are_read_from_artist_rels_and_cached(lookup: MusicBrainzLookup) -> None:
    palladino = "00000000-0000-4000-8000-0000000000b3"
    route = respx.get(f"{MB_URL}/artist/{TRIO_MBID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": TRIO_MBID,
                "relations": [
                    _member(MAYER_MBID, "John Mayer"),
                    _member(palladino, "Pino Palladino"),
                    {"type": "member of band", "direction": "backward"},
                ],
            },
        )
    )

    first = lookup.artist_relations(TRIO_MBID)
    second = lookup.artist_relations(TRIO_MBID)

    assert first == second
    assert [(r.relationship, r.artist_mbid, r.artist_name) for r in first] == [
        ("member of band", MAYER_MBID, "John Mayer"),
        ("member of band", palladino, "Pino Palladino"),
    ], "a relation without an artist is ignored"
    assert route.call_count == 1
    assert dict(httpx.URL(str(route.calls[0].request.url)).params)["inc"] == "artist-rels"


@respx.mock
def test_artist_relations_do_not_share_the_url_rels_cache_entry(lookup: MusicBrainzLookup) -> None:
    """`inc=url-rels` carries no artist relationships, so reusing its entry would read as "none"."""
    route = respx.get(f"{MB_URL}/artist/{TRIO_MBID}").mock(
        side_effect=[
            httpx.Response(200, json={"id": TRIO_MBID, "disambiguation": "", "relations": []}),
            httpx.Response(200, json={"id": TRIO_MBID, "relations": [_member(MAYER_MBID, "John Mayer")]}),
        ]
    )
    assert lookup.artist_disambiguation(TRIO_MBID) == ""
    assert [r.artist_name for r in lookup.artist_relations(TRIO_MBID)] == ["John Mayer"]
    assert route.call_count == 2


@respx.mock
def test_an_artist_with_no_relations_is_a_negative_entry_that_expires(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """Sister Sparrow & The Dirty Birds today: none. An editor adding one must be picked up on the
    negative TTL, not the 90-day positive one."""
    route = respx.get(f"{MB_URL}/artist/{TRIO_MBID}").mock(
        side_effect=[
            httpx.Response(200, json={"id": TRIO_MBID, "relations": []}),
            httpx.Response(200, json={"id": TRIO_MBID, "relations": [_member(MAYER_MBID, "John Mayer")]}),
        ]
    )
    lookup = MusicBrainzLookup(
        mb_config,
        client,
        cache_path=tmp_path / "state.db",
        max_age_days=90,
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert lookup.artist_relations(TRIO_MBID) == ()
    assert lookup.artist_relations(TRIO_MBID) == (), "cached"
    clock.advance(mb_config.negative_cache_days * (1 + POSITIVE_TTL_JITTER) * 86400 + 1)
    assert [r.artist_name for r in lookup.artist_relations(TRIO_MBID)] == ["John Mayer"]
    assert route.call_count == 2
    lookup.close()


@respx.mock
def test_an_unknown_artist_has_no_relations_rather_than_an_error(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/artist/{TRIO_MBID}").mock(return_value=httpx.Response(404, json={"error": "Not Found"}))
    assert lookup.artist_relations(TRIO_MBID) == ()
    assert lookup.ok


@respx.mock
def test_a_relationship_lookup_failure_with_nothing_cached_raises(lookup: MusicBrainzLookup) -> None:
    respx.get(f"{MB_URL}/artist/{TRIO_MBID}").mock(return_value=httpx.Response(503))
    with pytest.raises(MetadataError):
        lookup.artist_relations(TRIO_MBID)
    assert not lookup.ok
