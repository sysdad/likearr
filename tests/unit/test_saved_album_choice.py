"""A saved album prefers the Album among its artist's same-titled release groups (RESOLVER_VERSION 6).

Real, public cases a v5 dry run got wrong, with the candidates exactly as cached MusicBrainz
searches list them (same artist, same title, album against single / EP / demo, different
years). #23 made the earliest first-release date pick
among them, so every one of these went to an earlier single, EP or demo once v5 re-resolved it.

Jimmy Eat World's search was not cached; its fixture is built from the v5 dry run's report
(album 2aad83d4, 2004-10-11; single ef722ce4, 2004).
"""

from __future__ import annotations

import pytest

from likearr.core.resolver import resolve_album, resolve_track
from likearr.models import PrimaryType, ReleaseGroup, ResolutionStatus, SecondaryType
from tests.unit.fakes import NOW, FakeLookup, album_intent, rg, spotify_album, track_intent

YELLOWCARD = "3630fff3-52fc-4e97-ab01-d68fd88e4135"
JIMMY_EAT_WORLD = "bbc5b66b-d037-4f26-aecf-0b129e7f876a"
JOHN_WILLIAMS = "53b106e7-0cc6-42cc-ac95-ed8d30a3a98e"
SUBLIME = "95f5b748-d370-47fe-85bd-0af2dc450bc0"
GROUPLOVE = "d8e41375-bd8d-4e39-9da7-f0c171e97086"
CHAKA_DEMUS = "449f264b-1a94-4ef8-b601-2ffccbbedefa"

Single, Ep, Album = PrimaryType.SINGLE, PrimaryType.EP, PrimaryType.ALBUM


def _rg(mbid: str, title: str, artist: str, name: str, primary, released, *secondary: SecondaryType) -> ReleaseGroup:
    return rg(
        mbid, title, artist_mbid=artist, artist_name=name, primary=primary, secondary=secondary, released=released
    )


# (Spotify artist, Spotify album title, Spotify release date, the search that finds them, candidates, the album)
CASES = {
    "yellowcard-lights-and-sounds": (
        "Yellowcard",
        "Lights And Sounds",
        "2006-01-24",
        "Lights And Sounds",
        [
            _rg(
                "44df6977-48ec-3a91-aa3b-62eb282be39e",
                "Lights and Sounds",
                YELLOWCARD,
                "Yellowcard",
                Album,
                "2006-01-18",
            ),
            _rg(
                "30ead5f9-cfb6-4d57-87ea-1a9b1bb6009f",
                "Lights and Sounds",
                YELLOWCARD,
                "Yellowcard",
                Single,
                "2005-11-15",
            ),
        ],
        "44df6977-48ec-3a91-aa3b-62eb282be39e",
    ),
    "jimmy-eat-world-futures": (
        "Jimmy Eat World",
        "Futures (Deluxe Edition)",
        "2004-10-19",
        "Futures",
        [
            _rg(
                "2aad83d4-eee8-3616-84d0-d2afa569cc9e",
                "Futures",
                JIMMY_EAT_WORLD,
                "Jimmy Eat World",
                Album,
                "2004-10-11",
            ),
            _rg("ef722ce4-48c1-4d61-aa4d-ea8c91f5d8ce", "Futures", JIMMY_EAT_WORLD, "Jimmy Eat World", Single, "2004"),
        ],
        "2aad83d4-eee8-3616-84d0-d2afa569cc9e",
    ),
    "john-williams-summon-the-heroes": (
        "John Williams",
        "Summon the Heroes",
        "1996-01-01",
        "Summon the Heroes",
        [
            _rg(
                "3deeb90d-1566-377e-a046-6ae1469e9fb4",
                "Summon the Heroes",
                JOHN_WILLIAMS,
                "John Williams",
                Album,
                "1996-04-30",
            ),
            _rg(
                "43a30e4c-a683-4e1a-953a-0bb864054178",
                "Summon the Heroes",
                JOHN_WILLIAMS,
                "John Williams",
                Single,
                "1996",
            ),
        ],
        "3deeb90d-1566-377e-a046-6ae1469e9fb4",
    ),
    "sublime-sublime": (
        "Sublime",
        "Sublime",
        "1996-07-30",
        "Sublime",
        [
            _rg("0430d8c5-7417-3c4c-9f2a-b7d02ef7d164", "Sublime", SUBLIME, "Sublime", Album, "1996-07-30"),
            _rg(
                "80673f08-dda6-31e6-88f6-05fd7e312e23",
                "Sublime",
                SUBLIME,
                "Sublime",
                Album,
                "1995",
                SecondaryType.COMPILATION,
            ),
            _rg("81fe95fc-a0f1-4468-9267-2f503217ab0b", "Sublime", SUBLIME, "Sublime", Ep, "1990", SecondaryType.DEMO),
            _rg(
                "80904995-37ab-4186-af53-ee213884f81d", "Sublime", SUBLIME, "Sublime", None, "1988", SecondaryType.DEMO
            ),
        ],
        "0430d8c5-7417-3c4c-9f2a-b7d02ef7d164",
    ),
    # No Album exists. The EP is the record the two liked "Live from the Seesaw Tour" tracks resolve
    # to, and an EP fits a saved album better than a single does.
    "grouplove-im-with-you": (
        "GROUPLOVE",
        "I'm with You",
        "2014-03-04",
        "I'm with You",
        [
            _rg("258dda10-bb3a-47e7-884d-05f63e996ddd", "I'm With You", GROUPLOVE, "Grouplove", Ep, "2014-03-04"),
            _rg("493064d7-ad48-45db-97d6-c59c087fa8a2", "I'm With You", GROUPLOVE, "Grouplove", Single, "2014-06-24"),
        ],
        "258dda10-bb3a-47e7-884d-05f63e996ddd",
    ),
    # MusicBrainz's only Album titled "Tease Me" by them, over the same-titled single.
    "chaka-demus-tease-me": (
        "Chaka Demus & Pliers",
        "Tease Me",
        "1993-01-01",
        "Tease Me",
        [
            _rg("78f6013b-9c6c-3d69-9957-47a7110c7cf6", "Tease Me", CHAKA_DEMUS, "Chaka Demus & Pliers", Album, "1992"),
            _rg(
                "214fcd9f-8e18-3385-b145-74f28156be17", "Tease Me", CHAKA_DEMUS, "Chaka Demus & Pliers", Single, "1993"
            ),
        ],
        "78f6013b-9c6c-3d69-9957-47a7110c7cf6",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_saved_album_resolves_to_the_album_not_a_same_titled_earlier_single_ep_or_demo(case: str) -> None:
    artist, title, released, searched, candidates, expected = CASES[case]
    lookup = FakeLookup(candidate_searches={(artist, searched): [c.mbid for c in candidates]}).add(*candidates)
    if searched != title:
        lookup.searches[(artist, title)] = None  # the decorated title finds nothing; the stripped one does

    result = resolve_album(album_intent(spotify_album(title, artists=(artist,), released=released)), lookup)

    assert result.status is ResolutionStatus.RESOLVED
    assert result.release_group is not None
    assert result.release_group.mbid == expected, result.detail
    assert result.step == "album:search"


def test_a_saved_ep_beats_the_same_artists_live_album_of_the_same_name() -> None:
    """#166, R6: "X (Live)" folds to "X" and an Album outranks an EP, so the live album won. A
    studio release titled exactly as Spotify prints it now comes before any live, demo or
    compilation release, and the type rank decides only what is left."""
    ep = rg("rg-ep", "Seesaw", primary=Ep, released="2014")
    live = rg("rg-live", "Seesaw (Live)", secondary=[SecondaryType.LIVE], released="2015")
    lookup = FakeLookup(candidate_searches={("Test Artist", "Seesaw"): ["rg-ep", "rg-live"]}).add(ep, live)
    result = resolve_album(album_intent(spotify_album("Seesaw", released="2014-01-01")), lookup)
    assert (result.step, result.release_group) == ("album:search", ep)


def test_a_studio_title_match_does_not_outrank_a_bigger_studio_release() -> None:
    """Only secondary-typed releases give way: between studio releases the type rank still decides,
    so an exact-titled single never beats the EP MusicBrainz calls "Kangaroo EP"."""
    ep = rg("rg-ep", "Kangaroo EP", primary=Ep)
    single = rg("rg-single", "Kangaroo", primary=Single)
    live = rg("rg-live", "Kangaroo", secondary=[SecondaryType.LIVE])
    lookup = FakeLookup(candidate_searches={("Test Artist", "Kangaroo"): ["rg-ep", "rg-single", "rg-live"]}).add(
        ep, single, live
    )
    result = resolve_album(album_intent(spotify_album("Kangaroo")), lookup)
    assert result.release_group == ep


def test_the_spotify_year_breaks_a_tie_between_two_studio_albums() -> None:
    """A remastered reissue is its own release group; the year Spotify gives picks the one it means."""
    original = rg("rg-1969", "The Meters", artist_mbid="meters", artist_name="The Meters", released="1969-01-01")
    reissue = rg("rg-1993", "The Meters", artist_mbid="meters", artist_name="The Meters", released="1993-05-05")
    lookup = FakeLookup().add(original, reissue)

    for year, expected in (("1969-01-01", original), ("1993-06-01", reissue)):
        album = spotify_album("The Meters", artists=("The Meters",), released=year)
        assert resolve_album(album_intent(album), lookup).release_group == expected


def test_without_a_spotify_year_the_earliest_album_wins() -> None:
    original = rg("rg-1969", "The Meters", artist_mbid="meters", artist_name="The Meters", released="1969-01-01")
    reissue = rg("rg-1993", "The Meters", artist_mbid="meters", artist_name="The Meters", released="1993-05-05")
    album = spotify_album("The Meters", artists=("The Meters",), released=None)
    assert resolve_album(album_intent(album), FakeLookup().add(original, reissue)).release_group == original


def test_a_liked_track_still_starts_from_the_earliest_same_titled_release() -> None:
    """The track paths are unchanged: a song liked on the "Lights and Sounds" single maps to the
    single (earliest, 2005), the answer a v5 install already holds for it."""
    album, single = CASES["yellowcard-lights-and-sounds"][4]
    lookup = FakeLookup(candidate_searches={("Yellowcard", "Lights And Sounds"): [album.mbid, single.mbid]}).add(
        album, single
    )
    liked = track_intent(
        "Lights And Sounds",
        spotify_album("Lights And Sounds", artists=("Yellowcard",), album_type="single", released="2005-11-15"),
        artists=("Yellowcard",),
    )

    result = resolve_track(liked, lookup, now=NOW, pending_since=None, fallback_days=180, scope="smallest")

    assert result.source_release_group == single
