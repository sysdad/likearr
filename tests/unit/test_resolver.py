"""Unit tests for the resolver, with hand-built lookups.

The golden-corpus tests in `test_resolver_corpus.py` cover the same rules against real recorded
MusicBrainz data; these cover the edges that real data does not conveniently supply.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

import likearr.core.resolver as resolver_module
from likearr.adapters.state_sqlite import SqliteState
from likearr.core.resolver import (
    AMBIGUOUS_SAME_NAME_STEP,
    ARTIST_AMBIGUOUS_NAME_STEP,
    EXCLUDED_COMPILATION_STEP,
    EXCLUDED_DENIED_STEP,
    EXCLUDED_REMIX_STEP,
    JOINING_RELATIONSHIPS,
    REMIX_ONLY_STEP,
    SINGLE_FALLBACK_STEP,
    UNAVAILABLE_STEP,
    is_excluded,
    resolve_album,
    resolve_all,
    resolve_artist,
    resolve_track,
)
from likearr.models import (
    LIKED_TRACK_SCOPE_SMALLEST,
    NO_EXCLUSIONS,
    RESOLVER_VERSION,
    VARIOUS_ARTISTS_MBID,
    AlbumIntent,
    ArtistCandidate,
    ArtistIntent,
    ArtistResolution,
    ExclusionRules,
    IsrcRecording,
    PrimaryType,
    ReleaseGroup,
    Resolution,
    ResolutionStatus,
    SecondaryType,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.ports import CatalogueTooLarge
from tests.unit.fakes import (
    NOW,
    FakeLookup,
    album_intent,
    artist_intent,
    relation,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)

FALLBACK_DAYS = 180


def resolve(
    intent,
    lookup: FakeLookup,
    *,
    now: datetime = NOW,
    pending_since: datetime | None = None,
    rules: ExclusionRules = NO_EXCLUSIONS,
) -> Resolution:
    return resolve_track(intent, lookup, now=now, pending_since=pending_since, fallback_days=FALLBACK_DAYS, rules=rules)


# --------------------------------------------------------------------------- artists


def test_artist_exact_name_match_resolves() -> None:
    lookup = FakeLookup(artists={"mb-radiohead": "Radiohead"})
    result = resolve_artist(artist_intent("Radiohead"), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.artist_mbid == "mb-radiohead"
    assert result.step == "artist:search"
    assert result.detail


def test_artist_match_ignores_case_punctuation_and_leading_the() -> None:
    lookup = FakeLookup(artists={"mb-weeknd": "The Weeknd"})
    assert resolve_artist(artist_intent("the weeknd"), lookup).artist_mbid == "mb-weeknd"


def test_artist_near_miss_is_unmapped_not_guessed() -> None:
    """A wrong artist would pull a whole wrong catalogue in, so near misses are refused."""
    lookup = FakeLookup(artist_searches={"Radiohead": ("mb-radioheads", "Radioheads")})
    result = resolve_artist(artist_intent("Radiohead"), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert "Radioheads" in result.detail
    assert result.artist_mbid is None


def test_artist_not_found_is_unmapped() -> None:
    result = resolve_artist(artist_intent("Nobody At All"), FakeLookup())
    assert result.status == ResolutionStatus.UNMAPPED
    assert "Nobody At All" in result.detail


# --------------------------------------------------------------------------- albums


def test_album_resolves_by_barcode_without_searching() -> None:
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup(barcodes={"111": "rg-1"}).add(album)
    result = resolve_album(album_intent(spotify_album("OK Computer", upc="111", artists=("Radiohead",))), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "album:upc"
    assert result.release_group == album
    assert lookup.calls.get("search_release_group_candidates") is None


def test_album_falls_back_to_name_search_when_the_barcode_is_unknown() -> None:
    """Spotify has dropped external_ids before, so the name search is a first-class path."""
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup().add(album)
    result = resolve_album(album_intent(spotify_album("OK Computer", upc="999", artists=("Radiohead",))), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "album:search"
    assert result.release_group == album


def test_a_zero_padded_spotify_upc_matches_the_barcode_musicbrainz_stores() -> None:
    """#150: Chet Baker "In New York" - MusicBrainz knows 888072328433, Spotify says 00888072328433.
    The one release group holding it is taken without a title check, though its title differs."""
    album = rg("rg-nyc", "Chet Baker in New York", artist_name="Chet Baker")
    lookup = FakeLookup(barcodes={"888072328433": "rg-nyc"}).add(album)
    intent = album_intent(
        spotify_album("In New York [Original Jazz Classics Remasters]", upc="00888072328433", artists=("Chet Baker",))
    )
    result = resolve_album(intent, lookup)
    assert (result.status, result.step, result.release_group) == (ResolutionStatus.RESOLVED, "album:upc", album)
    assert lookup.calls.get("search_release_group_candidates") is None


def _tease_me() -> tuple[FakeLookup, ReleaseGroup, ReleaseGroup]:
    """The cached "Tease Me" barcode: the same artist's compilation listed first, then the album."""
    compilation = rg(
        "rg-all-she-wrote", "All She Wrote", artist_name="Chaka Demus & Pliers", secondary=[SecondaryType.COMPILATION]
    )
    album = rg("rg-tease-me", "Tease Me", artist_name="Chaka Demus & Pliers")
    lookup = FakeLookup(barcodes={"731451884825": ["rg-all-she-wrote", "rg-tease-me"]}).add(compilation, album)
    return lookup, compilation, album


def test_a_barcode_on_several_release_groups_is_chosen_by_title() -> None:
    lookup, _compilation, album = _tease_me()
    intent = album_intent(spotify_album("Tease Me", upc="00731451884825", artists=("Chaka Demus & Pliers",)))
    result = resolve_album(intent, lookup)
    assert (result.status, result.step, result.release_group) == (ResolutionStatus.RESOLVED, "album:upc", album)
    assert "2 release groups" in result.detail


def test_a_barcode_on_several_release_groups_none_titled_falls_through_to_the_name_search() -> None:
    lookup, _compilation, _album = _tease_me()
    other = rg("rg-other", "Something Else", artist_name="Chaka Demus & Pliers")
    lookup.add(other)
    intent = album_intent(spotify_album("Something Else", upc="00731451884825", artists=("Chaka Demus & Pliers",)))
    result = resolve_album(intent, lookup)
    assert (result.status, result.step, result.release_group) == (ResolutionStatus.RESOLVED, "album:search", other)
    assert lookup.calls.get("search_release_group_candidates") == 1


def test_a_barcode_on_several_release_groups_that_all_pass_the_title_check_chooses_none() -> None:
    first = rg("rg-a", "Tease Me", artist_name="Chaka Demus & Pliers")
    second = rg("rg-b", "Tease Me", artist_name="Chaka Demus & Pliers", released="1993-01-01")
    lookup = FakeLookup(barcodes={"731451884825": ["rg-a", "rg-b"]}, searches={("X", "Tease Me"): None}).add(
        first, second
    )
    intent = album_intent(spotify_album("Tease Me", upc="731451884825", artists=("X",)))
    result = resolve_album(intent, lookup)
    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == "album:search"
    assert "731451884825 is on 2 release groups" in result.detail
    assert "unknown to MusicBrainz" not in result.detail


def test_several_titled_release_groups_on_one_barcode_prefer_the_artists_then_an_official_one() -> None:
    theirs = rg("rg-theirs", "Tease Me", artist_name="Chaka Demus & Pliers")
    tribute = rg("rg-tribute", "Tease Me", artist_mbid="artist-2", artist_name="Someone Else")
    bootleg = rg("rg-bootleg", "Tease Me", artist_name="Chaka Demus & Pliers", released="1994-01-01")
    lookup = FakeLookup(
        barcodes={"731451884825": ["rg-tribute", "rg-bootleg", "rg-theirs"]}, unofficial={"rg-bootleg"}
    ).add(theirs, tribute, bootleg)
    intent = album_intent(spotify_album("Tease Me", upc="731451884825", artists=("Chaka Demus & Pliers",)))
    result = resolve_album(intent, lookup)
    assert (result.step, result.release_group) == ("album:upc", theirs)


def test_an_album_whose_barcode_matches_nothing_does_not_blame_musicbrainz() -> None:
    """#150: the barcode miss read "unknown to MusicBrainz", which blamed MusicBrainz for likearr's
    own comparison. It now says only what was found: no release with that barcode."""
    lookup = FakeLookup()
    result = resolve_album(album_intent(spotify_album("Nowhere", upc="00123", artists=("Nobody",))), lookup)
    assert result.status is ResolutionStatus.UNMAPPED
    assert "unknown to MusicBrainz" not in result.detail
    assert "no release with barcode 00123" in result.detail


def test_album_search_requires_exact_normalised_title_equality() -> None:
    other = rg("rg-2", "OK Computer OKNOTOK 1997 2017", artist_name="Radiohead")
    lookup = FakeLookup(searches={("Radiohead", "OK Computer"): "rg-2"}).add(other)
    result = resolve_album(album_intent(spotify_album("OK Computer", artists=("Radiohead",))), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert "not an exact title match" in result.detail


def test_album_credited_to_various_artists_on_spotify_is_unmapped() -> None:
    lookup = FakeLookup()
    intent = album_intent(spotify_album("Awesome Mix Vol. 1", artists=("Various Artists",)))
    result = resolve_album(intent, lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "album:various-artists"
    assert lookup.calls == {}


def test_album_credited_to_various_artists_in_musicbrainz_is_unmapped() -> None:
    va = rg("rg-va", "Awesome Mix Vol. 1", artist_mbid=VARIOUS_ARTISTS_MBID, artist_name="Various Artists")
    lookup = FakeLookup(barcodes={"111": "rg-va"}).add(va)
    result = resolve_album(
        album_intent(spotify_album("Awesome Mix Vol. 1", upc="111", artists=("Blue Swede",))), lookup
    )
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "album:various-artists"


def test_a_saved_single_is_monitored_because_the_user_asked_for_it() -> None:
    """The Singles rule is about liked tracks. An explicitly saved single is honoured."""
    single = rg("rg-s", "Blinding Lights", primary=PrimaryType.SINGLE)
    lookup = FakeLookup(barcodes={"111": "rg-s"}).add(single)
    result = resolve_album(album_intent(spotify_album("Blinding Lights", upc="111")), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == single


# --------------------------------------------------------------------------- albums: qualifier-stripped retry


def test_album_search_retries_without_spotifys_qualifier_and_resolves() -> None:
    """MusicBrainz stores 'The Beatles'; Spotify's lucene search for the decorated title misses it."""
    album = rg("rg-1", "The Beatles", artist_name="The Beatles")
    lookup = FakeLookup(
        searches={
            ("The Beatles", "The Beatles (Remastered)"): None,
            ("The Beatles", "The Beatles"): "rg-1",
        }
    ).add(album)
    result = resolve_album(album_intent(spotify_album("The Beatles (Remastered)", artists=("The Beatles",))), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "album:search"
    assert result.release_group == album
    assert lookup.calls["search_release_group_candidates"] == 2
    assert "after stripping '(Remastered)'" in result.detail


def test_album_search_that_matches_the_raw_title_never_retries() -> None:
    """A title with nothing to strip must cost exactly one lookup, matched or not."""
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup().add(album)
    result = resolve_album(album_intent(spotify_album("OK Computer", artists=("Radiohead",))), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert lookup.calls["search_release_group_candidates"] == 1


def test_album_search_with_no_qualifier_to_strip_does_not_retry_on_failure() -> None:
    lookup = FakeLookup()
    result = resolve_album(album_intent(spotify_album("Totally Unknown Record", artists=("Nobody",))), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls["search_release_group_candidates"] == 1


def test_album_search_stays_unmapped_when_the_stripped_title_also_misses() -> None:
    """Two failed attempts, and the detail says so - never silently swallowed."""
    lookup = FakeLookup(
        searches={("Ghost", "Ghost (Remastered)"): None, ("Ghost", "Ghost"): None},
    )
    result = resolve_album(album_intent(spotify_album("Ghost (Remastered)", artists=("Ghost",))), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls["search_release_group_candidates"] == 2
    assert "no release group found for 'Ghost' - 'Ghost (Remastered)'" in result.detail
    assert "retried after stripping qualifiers to 'Ghost'" in result.detail
    assert "either" in result.detail


def test_track_album_search_also_benefits_from_the_qualifier_retry() -> None:
    """`_map_spotify_album` is shared, so the Singles rule's first step gets the same retry."""
    album = rg("rg-1", "After Hours")
    lookup = FakeLookup(
        searches={
            ("Test Artist", "After Hours (Deluxe)"): None,
            ("Test Artist", "After Hours"): "rg-1",
        }
    ).add(album)
    intent = track_intent("Blinding Lights", spotify_album("After Hours (Deluxe)"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == album
    assert "after stripping '(Deluxe)'" in result.detail


# --------------------------------------------------------------------------- albums: symmetric title comparison


def test_album_search_matches_when_musicbrainz_carries_the_undecorated_qualifier() -> None:
    """Issue #21 fault (a): Kyle Andrews - 'Kangaroo', MusicBrainz 'Kangaroo EP'.

    Spotify's own title has nothing to strip, so the old one-sided `strip_release_qualifiers`
    retry never fired at all. The fix compares both sides stripped, so this now resolves off the
    very first search - no retry needed.
    """
    album = rg("rg-1", "Kangaroo EP", artist_name="Kyle Andrews")
    lookup = FakeLookup(searches={("Kyle Andrews", "Kangaroo"): "rg-1"}).add(album)
    result = resolve_album(album_intent(spotify_album("Kangaroo", artists=("Kyle Andrews",))), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == album
    assert lookup.calls["search_release_group_candidates"] == 1


def test_album_search_matches_a_colon_joined_musicbrainz_title_without_stripping_either_side() -> None:
    """Issue #21 fault (a): Elf - Spotify's bracketed subtitle and MusicBrainz's colon-joined one
    already fold to the same words under `normalize_title` alone, with no stripping at all."""
    mb_title = "Elf: Music From the Major Motion Picture"
    artist = "Clyde Lawrence"  # a real artist, not "Various Artists" - that short-circuits earlier
    album = rg("rg-1", mb_title, artist_name=artist)
    lookup = FakeLookup(searches={(artist, "Elf (Music from the Major Motion Picture)"): "rg-1"}).add(album)
    result = resolve_album(
        album_intent(spotify_album("Elf (Music from the Major Motion Picture)", artists=(artist,))),
        lookup,
    )
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == album
    assert lookup.calls["search_release_group_candidates"] == 1


# --------------------------------------------------------------------------- tracks: the easy path


def test_track_on_a_studio_album_resolves_to_that_album() -> None:
    album = rg("rg-1", "OK Computer")
    lookup = FakeLookup(barcodes={"111": "rg-1"}).add(album)
    result = resolve(track_intent("Karma Police", spotify_album("OK Computer", upc="111")), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == album
    assert result.source_release_group == album
    assert lookup.calls.get("release_groups_for_isrc") is None


def test_musicbrainz_type_wins_over_spotifys_album_type() -> None:
    """Spotify files EPs as 'single'. MusicBrainz says EP, so the EP is monitored directly."""
    ep = rg("rg-ep", "Blood Bank", primary=PrimaryType.EP)
    lookup = FakeLookup(barcodes={"111": "rg-ep"}).add(ep)
    intent = track_intent("Blood Bank", spotify_album("Blood Bank", upc="111", album_type="single"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == ep


def test_track_whose_album_cannot_be_mapped_is_unmapped() -> None:
    result = resolve(track_intent("Mystery", spotify_album("Unknown Record")), FakeLookup())
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert result.release_group is None


# ------------------------------------------------- tracks: the ISRC fallback when the album misses


def _unmappable_album() -> SpotifyAlbumRef:
    """A Spotify album MusicBrainz has no release group for under that title and credit."""
    return spotify_album("Live At The Nowhere Club", artists=("Test Artist",))


def test_the_isrc_fallback_recognises_the_release_spotify_named() -> None:
    """The title matches; the credit does not, which is the whole reason the search missed.

    The ISRC proves the release group holds this exact recording, so a title match identifies
    it whatever name the two catalogues print for the credit - the John Mayer / "John Mayer
    Trio" shape from issue #1.
    """
    live = rg(
        "rg-live",
        "Live At The Nowhere Club",
        artist_mbid="mb-trio",
        artist_name="Test Artist Trio",
        secondary=[SecondaryType.LIVE],
        released="2005-11-22",
    )
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-live"]}).add(live)
    result = resolve(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == live
    assert result.source_release_group == live
    assert "ISRC1" in result.detail


def test_the_isrc_fallback_matches_the_stripped_spotify_title_too() -> None:
    """`normalize_title` keeps a trailing "- EP", so the stripped form is what matches here."""
    ep = rg("rg-ep", "Blood Bank", primary=PrimaryType.EP, artist_mbid="mb-trio", artist_name="Test Artist Trio")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-ep"]}).add(ep)
    intent = track_intent("Blood Bank", spotify_album("Blood Bank - EP"), isrc="ISRC1")
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == ep


def test_the_isrc_fallback_falls_back_to_the_tracks_own_artist() -> None:
    """No candidate carries the title Spotify used, so the track's own artist decides."""
    album = rg("rg-album", "After Hours", artist_mbid="mb-weeknd", artist_name="The Weeknd")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-album"]}).add(album)
    intent = track_intent("Blinding Lights", _unmappable_album(), isrc="ISRC1", artists=("The Weeknd",))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == album


def test_the_isrc_fallback_refuses_a_release_by_another_artist() -> None:
    """No title match and no artist match: the track stays exactly as unmapped as before."""
    other = rg("rg-other", "Someone Else's Record", artist_mbid="mb-other", artist_name="Someone Else")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-other"]}).add(other)
    intent = track_intent("Gravity", _unmappable_album(), isrc="ISRC1", artists=("Test Artist",))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert result.release_group is None


def test_the_isrc_fallback_never_picks_a_various_artists_release() -> None:
    comp = rg(
        "rg-comp",
        "Live At The Nowhere Club",
        artist_mbid=VARIOUS_ARTISTS_MBID,
        artist_name="Various Artists",
        secondary=[SecondaryType.COMPILATION],
    )
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-comp"]}).add(comp)
    result = resolve(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"


def test_the_isrc_fallback_is_not_attempted_without_an_isrc() -> None:
    lookup = FakeLookup()
    result = resolve(track_intent("Mystery", _unmappable_album()), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls.get("release_groups_for_isrc") is None
    assert "no ISRC" in result.detail


def test_the_isrc_fallback_breaks_ties_on_the_earliest_release_then_the_mbid() -> None:
    """Two release groups carry the title Spotify named; the answer must not depend on order.

    The lowest MBID is `rg-a`, so picking the earlier `rg-b` proves the date is what decides.
    """
    trio = "Test Artist Trio"
    early = rg("rg-b", "Live At The Nowhere Club", released="2005-11-22", artist_mbid="mb-trio", artist_name=trio)
    late = rg("rg-a", "Live At The Nowhere Club", released="2011-01-01", artist_mbid="mb-trio", artist_name=trio)
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-a", "rg-b"]}).add(early, late)
    result = resolve(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.release_group == early


def test_the_isrc_fallback_prefers_a_studio_album_over_a_single_by_the_same_artist() -> None:
    single = rg("rg-single", "Gravity", primary=PrimaryType.SINGLE, released="2006-01-01")
    album = rg("rg-album", "Continuum", released="2006-09-12")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-single", "rg-album"]}).add(single, album)
    result = resolve(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == album


def test_the_isrc_fallback_still_waits_for_an_album_when_only_a_single_holds_the_song() -> None:
    """The stand-in is a single, so the existing Singles rule decides - nothing new is monitored."""
    single = rg("rg-single", "Gravity", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-single"]}, catalogues={"artist-1": ["rg-single"]}).add(single)
    result = resolve(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.status == ResolutionStatus.PENDING_ALBUM
    assert result.step == "track:pending"


def test_the_isrc_fallback_respects_the_smallest_scope() -> None:
    single = rg("rg-single", "Gravity", primary=PrimaryType.SINGLE, released="2006-01-01")
    album = rg("rg-album", "Continuum", released="2006-09-12")
    lookup = FakeLookup(isrcs={"ISRC1": ["rg-single", "rg-album"]}).add(single, album)
    result = resolve_smallest(track_intent("Gravity", _unmappable_album(), isrc="ISRC1"), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:smallest:single"
    assert result.release_group == single


def test_a_failing_isrc_lookup_is_reported_not_swallowed() -> None:
    """`resolve_all` turns the backend failure into a degraded run, never a quiet UNMAPPED."""
    lookup = FakeLookup(fail={"release_groups_for_isrc"})
    intent = track_intent("Gravity", _unmappable_album(), isrc="ISRC1")
    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
    )
    assert result.degraded
    assert result.resolutions[intent.reason.key].step == "error:metadata"


def test_a_failing_isrc_lookup_never_drops_a_mapping_made_earlier() -> None:
    """A cached RESOLVED resolution is reused untouched while the backend is down."""
    album = rg("rg-album", "Continuum")
    intent = track_intent("Gravity", _unmappable_album(), isrc="ISRC1")
    cached = Resolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.RESOLVED,
        release_group=album,
        step="track:album",
        scope="album",
    )
    result = resolve_all(
        snapshot(tracks=[intent]),
        FakeLookup(fail={"release_groups_for_isrc", "search_release_group_candidates"}),
        now=NOW,
        cache={intent.reason.key: cached},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
    )
    assert not result.degraded
    assert result.resolutions[intent.reason.key] is cached


# --------------------------------------------------------------------------- tracks: the Singles rule


def _single_and_album() -> tuple:
    single = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, released="2019-11-29")
    album = rg("rg-album", "After Hours", released="2020-03-20")
    return single, album


def test_liked_single_resolves_to_the_album_through_the_isrc() -> None:
    single, album = _single_and_album()
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"ISRC1": ["rg-single", "rg-album"]}).add(single, album)
    intent = track_intent("Blinding Lights", spotify_album("Blinding Lights", upc="111"), isrc="ISRC1")
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:isrc->album"
    assert result.release_group == album
    assert result.source_release_group == single


def test_isrc_candidates_from_another_artist_are_ignored() -> None:
    """A cover or a compilation credited elsewhere must not drag in a foreign artist."""
    single, album = _single_and_album()
    foreign = rg("rg-foreign", "Covers Record", artist_mbid="artist-2", artist_name="Someone Else")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"ISRC1": ["rg-foreign", "rg-album"]},
    ).add(single, album, foreign)
    intent = track_intent("Blinding Lights", spotify_album("Blinding Lights", upc="111"), isrc="ISRC1")
    assert resolve(intent, lookup).release_group == album


def test_non_studio_isrc_candidates_are_ignored() -> None:
    single, album = _single_and_album()
    live = rg("rg-live", "Live In Paris", secondary=[SecondaryType.LIVE], released="2019-01-01")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"ISRC1": ["rg-live", "rg-album"]},
    ).add(single, album, live)
    intent = track_intent("Blinding Lights", spotify_album("Blinding Lights", upc="111"), isrc="ISRC1")
    assert resolve(intent, lookup).release_group == album


def test_radio_edit_with_its_own_isrc_falls_back_to_the_title_match() -> None:
    """Radio edits are separate recordings with separate ISRCs, so only the title connects them."""
    single = rg("rg-single", "Get Lucky", primary=PrimaryType.SINGLE, released="2013-04-19")
    album = rg("rg-album", "Random Access Memories", released="2013-05-17")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"EDIT1": ["rg-single"]},
        catalogues={"artist-1": ["rg-single", "rg-album"]},
        tracklists={"rg-album": ["Give Life Back to Music", "Get Lucky", "Touch"]},
    ).add(single, album)
    intent = track_intent("Get Lucky - Radio Edit", spotify_album("Get Lucky", upc="111"), isrc="EDIT1")
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:title->album"
    assert result.release_group == album


def test_title_fallback_runs_when_the_track_has_no_isrc_at_all() -> None:
    single = rg("rg-single", "Get Lucky", primary=PrimaryType.SINGLE)
    album = rg("rg-album", "Random Access Memories")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        catalogues={"artist-1": ["rg-album"]},
        tracklists={"rg-album": ["Get Lucky"]},
    ).add(single, album)
    intent = track_intent("Get Lucky", spotify_album("Get Lucky", upc="111"), isrc=None)
    assert resolve(intent, lookup).step == "track:title->album"


def test_title_fallback_skips_non_studio_release_groups() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-09-01")
    best_of = rg("rg-comp", "Greatest Hits", secondary=[SecondaryType.COMPILATION], released="2001-01-01")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        catalogues={"artist-1": ["rg-comp"]},
        tracklists={"rg-comp": ["Song"]},
    ).add(single, best_of)
    intent = track_intent("Song", spotify_album("Song", upc="111"))
    assert resolve(intent, lookup).status == ResolutionStatus.PENDING_ALBUM


def test_several_candidates_pick_the_earliest_then_the_lowest_mbid() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE)
    early = rg("rg-b-early", "First Album", released="2010-01-01")
    late = rg("rg-a-late", "Second Album", released="2015-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"I1": ["rg-a-late", "rg-b-early"]}).add(
        single, early, late
    )
    intent = track_intent("Song", spotify_album("Song", upc="111"), isrc="I1")
    result = resolve(intent, lookup)
    assert result.release_group == early
    assert "1 other candidate(s)" in result.detail


def test_candidates_with_no_release_date_sort_last_and_tie_on_mbid() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE)
    undated_b = rg("rg-b", "B Album", released=None)
    undated_a = rg("rg-a", "A Album", released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"I1": ["rg-b", "rg-a"]}).add(single, undated_a, undated_b)
    intent = track_intent("Song", spotify_album("Song", upc="111"), isrc="I1")
    assert resolve(intent, lookup).release_group == undated_a


def test_dated_candidate_beats_an_undated_one() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE)
    undated = rg("rg-a", "A Album", released=None)
    dated = rg("rg-z", "Z Album", released="2099-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"I1": ["rg-a", "rg-z"]}).add(single, undated, dated)
    intent = track_intent("Song", spotify_album("Song", upc="111"), isrc="I1")
    assert resolve(intent, lookup).release_group == dated


def test_featured_credits_beyond_the_first_artist_are_ignored() -> None:
    """The release group's primary credit decides, not the Spotify artist list."""
    single = rg(
        "rg-single", "Uptown Funk", primary=PrimaryType.SINGLE, artist_mbid="mb-ronson", artist_name="Mark Ronson"
    )
    album = rg("rg-album", "Uptown Special", artist_mbid="mb-ronson", artist_name="Mark Ronson")
    guest = rg("rg-guest", "24K Magic", artist_mbid="mb-mars", artist_name="Bruno Mars")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"I1": ["rg-guest", "rg-album"]},
    ).add(single, album, guest)
    intent = track_intent(
        "Uptown Funk (feat. Bruno Mars)",
        spotify_album("Uptown Funk", upc="111", artists=("Mark Ronson", "Bruno Mars")),
        isrc="I1",
        artists=("Mark Ronson", "Bruno Mars"),
    )
    result = resolve(intent, lookup)
    assert result.release_group == album
    assert result.release_group is not None
    assert result.release_group.artist_mbid == "mb-ronson"


# --------------------------------------------------------------------------- tracks: pending and fallback


def test_single_with_no_album_anywhere_goes_pending() -> None:
    single = rg("rg-single", "Skyfall", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Skyfall", spotify_album("Skyfall", upc="111"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.PENDING_ALBUM
    assert result.release_group is None
    assert result.single_release_group == single
    assert result.single_release_date is not None
    assert result.single_release_date.isoformat() == "2026-09-01"


def test_pending_single_past_the_window_monitors_the_single_itself() -> None:
    single = rg("rg-single", "Skyfall", primary=PrimaryType.SINGLE, released="2012-10-04")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Skyfall", spotify_album("Skyfall", upc="111"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:single-fallback"
    assert result.release_group == single


def test_the_fallback_window_closes_exactly_on_the_boundary_day() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111"))
    day_before = datetime(2026, 6, 29, tzinfo=UTC)  # 179 days
    on_the_day = datetime(2026, 6, 30, tzinfo=UTC)  # 180 days
    assert resolve(intent, lookup, now=day_before).status == ResolutionStatus.PENDING_ALBUM
    assert resolve(intent, lookup, now=on_the_day).status == ResolutionStatus.RESOLVED


def test_release_date_is_the_clock_not_pending_since() -> None:
    """A single released yesterday stays pending even if it has been pending for years."""
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-09-17")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111"))
    long_ago = NOW - timedelta(days=5000)
    assert resolve(intent, lookup, pending_since=long_ago).status == ResolutionStatus.PENDING_ALBUM


def test_pending_since_is_the_clock_when_the_release_date_is_unknown() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111", released=None))
    assert resolve(intent, lookup, pending_since=NOW - timedelta(days=10)).status == ResolutionStatus.PENDING_ALBUM
    fallen_back = resolve(intent, lookup, pending_since=NOW - timedelta(days=200))
    assert fallen_back.status == ResolutionStatus.RESOLVED
    assert fallen_back.step == "track:single-fallback"


def test_spotifys_release_date_backs_up_a_missing_musicbrainz_date() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111", released="2010-01-01"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:single-fallback"


def test_a_single_with_no_date_at_all_and_no_history_stays_pending() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111", released=None))
    assert resolve(intent, lookup, pending_since=None).status == ResolutionStatus.PENDING_ALBUM


def test_naive_pending_since_against_an_aware_now_does_not_explode() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111", released=None))
    naive = datetime(2000, 1, 1)  # deliberately awareness-mismatched
    assert resolve(intent, lookup, pending_since=naive).status == ResolutionStatus.PENDING_ALBUM


# --------------------------------------------------------------------------- tracks: non-studio only


def test_a_track_that_exists_only_on_a_compilation_monitors_the_compilation() -> None:
    """Monitoring the compilation is the only honest way to get a song that lives nowhere else."""
    comp = rg("rg-comp", "Hey Jude", secondary=[SecondaryType.COMPILATION], released="1970-02-26")
    studio = rg("rg-studio", "Abbey Road", released="1969-09-26")
    lookup = FakeLookup(
        barcodes={"111": "rg-comp"},
        catalogues={"artist-1": ["rg-comp", "rg-studio"]},
        tracklists={"rg-studio": ["Come Together", "Something"]},
    ).add(comp, studio)
    intent = track_intent("Hey Jude", spotify_album("Hey Jude", upc="111"))
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == comp
    assert result.source_release_group == comp


def test_a_non_studio_resolution_needs_the_full_profile() -> None:
    from likearr.models import DesiredRelease, ReleaseKey

    comp = rg("rg-comp", "Hey Jude", secondary=[SecondaryType.COMPILATION])
    release = DesiredRelease(key=ReleaseKey("artist-1", "rg-comp"), release_group=comp)
    assert release.needs_full_profile is True


# --------------------------------------------------------------------------- resolve_all


def _basic_snapshot():
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup(barcodes={"111": "rg-1"}, artists={"artist-1": "Radiohead"}).add(album)
    snap = snapshot(
        artists=[artist_intent("Radiohead", spotify_id="sp-a")],
        albums=[album_intent(spotify_album("OK Computer", upc="111", artists=("Radiohead",)))],
        tracks=[track_intent("Karma Police", spotify_album("OK Computer", upc="111"))],
    )
    return snap, lookup, album


def _resolve_all(snap, lookup, *, cache=None, pending=None, now=NOW, progress=None):
    return resolve_all(
        snap,
        lookup,
        now=now,
        cache=cache or {},
        pending_since=pending or {},
        fallback_days=FALLBACK_DAYS,
        progress=progress,
    )


def test_resolve_all_covers_every_intent() -> None:
    snap, lookup, _ = _basic_snapshot()
    result = _resolve_all(snap, lookup)
    assert len(result.artist_resolutions) == 1
    assert len(result.resolutions) == 2
    assert result.metadata_errors == 0
    assert result.degraded is False
    assert all(r.status == ResolutionStatus.RESOLVED for r in result.resolutions.values())


def test_resolve_all_reports_progress_once_per_intent_in_order() -> None:
    """One artist, one album, one track: `progress` is called three times, in that order, each
    with a correct running total out of the fixed grand total (issue #119)."""
    snap, lookup, _ = _basic_snapshot()
    calls: list[tuple[int, int]] = []
    _resolve_all(snap, lookup, progress=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_resolve_all_works_unchanged_when_progress_is_none() -> None:
    snap, lookup, _ = _basic_snapshot()
    result = _resolve_all(snap, lookup, progress=None)
    assert len(result.artist_resolutions) == 1
    assert len(result.resolutions) == 2


def test_a_resolved_cache_entry_is_reused_without_calling_the_lookup() -> None:
    snap, lookup, _ = _basic_snapshot()
    first = _resolve_all(snap, lookup)
    lookup.calls.clear()
    cache = {**first.resolutions, **first.artist_resolutions}
    second = _resolve_all(snap, lookup, cache=cache)
    assert second.resolutions == first.resolutions
    assert second.artist_resolutions == first.artist_resolutions
    assert lookup.calls == {}


def test_pending_entries_are_always_re_resolved() -> None:
    """The album may have landed, or the fallback window may have closed."""
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    snap = snapshot(tracks=[track_intent("Song", spotify_album("Song", upc="111"))])
    first = _resolve_all(snap, lookup, now=datetime(2026, 2, 1, tzinfo=UTC))
    assert next(iter(first.resolutions.values())).status == ResolutionStatus.PENDING_ALBUM
    lookup.calls.clear()
    later = _resolve_all(snap, lookup, cache=dict(first.resolutions), now=datetime(2026, 9, 1, tzinfo=UTC))
    assert lookup.calls, "a pending resolution must be re-resolved, not read from cache"
    assert next(iter(later.resolutions.values())).step == "track:single-fallback"


def test_unmapped_entries_are_always_re_resolved() -> None:
    lookup = FakeLookup()
    snap = snapshot(tracks=[track_intent("Song", spotify_album("Nowhere"))])
    first = _resolve_all(snap, lookup)
    assert next(iter(first.resolutions.values())).status == ResolutionStatus.UNMAPPED
    lookup.calls.clear()
    _resolve_all(snap, lookup, cache=dict(first.resolutions))
    assert lookup.calls, "an unmapped resolution must be retried next run"


def test_a_cache_entry_from_an_older_resolver_version_is_ignored() -> None:
    snap, lookup, _ = _basic_snapshot()
    first = _resolve_all(snap, lookup)
    stale = {
        k: Resolution(
            intent_key=v.intent_key,
            status=v.status,
            release_group=v.release_group,
            step=v.step,
            detail=v.detail,
            resolver_version=RESOLVER_VERSION - 1,
        )
        for k, v in first.resolutions.items()
    }
    lookup.calls.clear()
    _resolve_all(snap, lookup, cache=stale)
    assert lookup.calls


def test_a_cache_entry_of_the_wrong_kind_is_ignored() -> None:
    """An artist key must not be satisfied by a track resolution, or vice versa."""
    snap, lookup, _ = _basic_snapshot()
    artist_key = snap.artists[0].reason.key
    cache: dict[str, Resolution | ArtistResolution] = {
        artist_key: Resolution(intent_key=artist_key, status=ResolutionStatus.RESOLVED)
    }
    result = _resolve_all(snap, lookup, cache=cache)
    assert result.artist_resolutions[artist_key].artist_mbid == "artist-1"


class _AbsorbsOneFailure:
    """A lookup that answers everything, but reports a failure it absorbed while answering one
    barcode - the shape of `CompositeLookup` answering from Lidarr after MusicBrainz errored."""

    def __init__(self, inner: FakeLookup, upc: str) -> None:
        self.inner, self.upc, self.failures = inner, upc, 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def release_groups_by_barcode(self, upc: str):
        if upc == self.upc:
            self.failures += 1
        return self.inner.release_groups_by_barcode(upc)


def _two_albums():
    lookup = FakeLookup(barcodes={"111": "rg-1", "222": "rg-2"}).add(
        rg("rg-1", "OK Computer", artist_name="Radiohead"), rg("rg-2", "Kid A", artist_name="Radiohead")
    )
    saved = album_intent(spotify_album("OK Computer", upc="111", artists=("Radiohead",)))
    liked = track_intent("Idioteque", spotify_album("Kid A", upc="222", artists=("Radiohead",)))
    return snapshot(albums=[saved], tracks=[liked]), lookup, saved, liked


def test_only_the_intent_resolved_while_the_lookup_failed_is_provisional() -> None:
    """#53: which intent a failure belongs to is known only here, where intents are resolved one
    after another. The answer is untouched - both resolve - only the flag differs."""
    snap, inner, saved, liked = _two_albums()
    lookup = _AbsorbsOneFailure(inner, "222")

    result = resolve_all(
        snap,
        lookup,  # type: ignore[arg-type]
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        lookup_failures=lambda: lookup.failures,
    )

    assert result.resolutions[saved.reason.key].status is ResolutionStatus.RESOLVED
    assert result.resolutions[liked.reason.key].status is ResolutionStatus.RESOLVED
    assert result.provisional == {liked.reason.key}


def test_a_cache_hit_is_never_provisional_and_without_a_counter_nothing_is() -> None:
    snap, inner, _, _ = _two_albums()
    lookup = _AbsorbsOneFailure(inner, "222")
    first = resolve_all(snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS)  # type: ignore[arg-type]
    assert first.provisional == set(), "no counter given: nothing is provisional"

    lookup.failures += 5  # moves between runs, not during any intent
    second = resolve_all(
        snap,
        lookup,  # type: ignore[arg-type]
        now=NOW,
        cache=dict(first.resolutions),
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        lookup_failures=lambda: lookup.failures,
    )

    assert second.resolutions == first.resolutions
    assert second.provisional == set(), "both came from the cache, which asked nothing"


@pytest.mark.parametrize("method", ["search_artist_candidates", "release_groups_by_barcode"])
def test_one_metadata_failure_never_aborts_the_run(method: str) -> None:
    snap, lookup, _ = _basic_snapshot()
    lookup.fail = {method}
    result = _resolve_all(snap, lookup)
    assert result.metadata_errors > 0
    assert result.degraded is True
    failed = [
        r for r in [*result.resolutions.values(), *result.artist_resolutions.values()] if r.step == "error:metadata"
    ]
    assert failed
    assert all(r.status == ResolutionStatus.UNMAPPED for r in failed)
    assert all("fake failure" in r.detail for r in failed)


def test_metadata_failure_on_one_intent_leaves_the_others_resolved() -> None:
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup(
        barcodes={"111": "rg-1"}, artists={"artist-1": "Radiohead"}, fail={"search_artist_candidates"}
    ).add(album)
    snap = snapshot(
        artists=[artist_intent("Radiohead", spotify_id="sp-a")],
        albums=[album_intent(spotify_album("OK Computer", upc="111", artists=("Radiohead",)))],
    )
    result = _resolve_all(snap, lookup)
    assert result.metadata_errors == 1
    assert next(iter(result.resolutions.values())).status == ResolutionStatus.RESOLVED


def test_resolution_is_deterministic_across_repeated_runs() -> None:
    snap, lookup, _ = _basic_snapshot()
    a = _resolve_all(snap, lookup)
    b = _resolve_all(snap, lookup)
    assert a.resolutions == b.resolutions
    assert a.artist_resolutions == b.artist_resolutions


def test_playlist_tracks_resolve_exactly_like_liked_tracks() -> None:
    album = rg("rg-1", "OK Computer")
    lookup = FakeLookup(barcodes={"111": "rg-1"}).add(album)
    liked = track_intent("Karma Police", spotify_album("OK Computer", upc="111"), spotify_id="t1")
    listed = track_intent("Karma Police", spotify_album("OK Computer", upc="111"), spotify_id="t1", playlist_id="pl-1")
    a = resolve(liked, lookup)
    b = resolve(listed, lookup)
    assert a.release_group == b.release_group
    assert a.step == b.step
    assert b.intent_key == "playlist:pl-1:t1"


def test_a_liked_track_on_a_various_artists_compilation_never_browses_various_artists() -> None:
    """Regression from an early version: the title fallback paged through the VA 'catalogue'."""
    from datetime import UTC, datetime

    from likearr.core.resolver import resolve_track
    from likearr.models import (
        VARIOUS_ARTISTS_MBID,
        PrimaryType,
        Reason,
        ReasonKind,
        ReleaseGroup,
        ResolutionStatus,
        SecondaryType,
        SpotifyAlbumRef,
        TrackIntent,
    )

    comp = ReleaseGroup(
        mbid="comp-1",
        title="Hits Of The Year",
        artist_mbid=VARIOUS_ARTISTS_MBID,
        artist_name="Various Artists",
        primary_type=PrimaryType.ALBUM,
        secondary_types=frozenset({SecondaryType.COMPILATION}),
    )
    album = ReleaseGroup(
        mbid="album-1",
        title="The Real Album",
        artist_mbid="artist-1",
        artist_name="Real Artist",
        primary_type=PrimaryType.ALBUM,
    )

    class Lookup:
        browsed: list[str] = []

        def release_groups_by_barcode(self, upc: str) -> list:
            return []

        def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
            return comp

        def search_release_group_candidates(self, artist: str, title: str) -> list[ReleaseGroup]:
            return [comp]

        def release_groups_for_isrc(self, isrc: str) -> list[ReleaseGroup]:
            return [comp]

        def recordings_for_isrc(self, isrc: str) -> list[IsrcRecording]:
            return [IsrcRecording(title="Great Song", release_groups=(comp,))]

        def search_artist(self, name: str) -> tuple[str, str] | None:
            return ("artist-1", "Real Artist")

        def search_artist_candidates(self, name: str) -> list[tuple[str, str]]:
            return [("artist-1", "Real Artist")]

        def artist_release_groups(self, artist_mbid: str) -> list[ReleaseGroup]:
            self.browsed.append(artist_mbid)
            return [album]

        def release_group_track_titles(self, rg_mbid: str) -> list[str]:
            return ["Great Song"] if rg_mbid == "album-1" else []

    lookup = Lookup()
    intent = TrackIntent(
        spotify_id="t1",
        name="Great Song",
        isrc="XX0000000009",
        artist_names=("Real Artist",),
        album=SpotifyAlbumRef("sa1", "Hits Of The Year", ("Various Artists",), None, "compilation", None),
        added_at=None,
        reason=Reason(ReasonKind.LIKED, "t1"),
    )
    res = resolve_track(intent, lookup, now=datetime(2026, 9, 18, tzinfo=UTC), pending_since=None, fallback_days=180)  # type: ignore[arg-type]
    assert VARIOUS_ARTISTS_MBID not in lookup.browsed
    assert lookup.browsed == ["artist-1"]
    assert res.status is ResolutionStatus.RESOLVED and res.release_group == album
    assert res.step == "track:title->album"


@pytest.mark.parametrize(("name", "artists"), [("", ("",)), ("", ()), ("Song", ("",)), ("   ", ("Someone",))])
def test_a_track_spotify_no_longer_serves_is_unmapped_without_a_lookup(name: str, artists: tuple[str, ...]) -> None:
    """#166: a taken-down or region-locked like comes back with an empty name or artist and an
    empty "Various Artists" album. Nothing can be looked up for it, and it says so."""
    lookup = FakeLookup()
    album = spotify_album("", artists=("Various Artists",), album_type="compilation")
    intent = track_intent(name, album, isrc="ISRC1", artists=artists)
    result = resolve(intent, lookup)
    assert (result.status, result.step, result.release_group) == (ResolutionStatus.UNMAPPED, UNAVAILABLE_STEP, None)
    assert "Spotify no longer serves this track" in result.detail
    assert lookup.calls == {}


def _evangeline(*, isrc_hits: Sequence[str] = ("rg-la", "rg-va"), namesakes: bool = True) -> FakeLookup:
    """#152: a song liked from a Various Artists compilation, whose artist shares a name.

    Two MusicBrainz artists are called "Evangeline": a Seattle band the name search ranks first, and
    the L.A. singer the user listens to. Each has a studio album listing "Wild Heart". The ISRC is
    on the L.A. album and the compilation.
    """
    va = rg("rg-va", "Summer Hits", artist_mbid=VARIOUS_ARTISTS_MBID, artist_name="Various Artists")
    va = replace(va, secondary_types=frozenset({SecondaryType.COMPILATION}))
    seattle = rg("rg-seattle", "Evangeline", artist_mbid="mb-1-seattle", artist_name="Evangeline", released="1992")
    la = rg("rg-la", "Tomorrow", artist_mbid="mb-2-la", artist_name="Evangeline", released="2015")
    lookup = FakeLookup(
        searches={("Various Artists", "Summer Hits"): "rg-va"},
        isrcs={"ISRC-LA": list(isrc_hits)},
        catalogues={"mb-1-seattle": ["rg-seattle"], "mb-2-la": ["rg-la"]},
        tracklists={"rg-seattle": ["Wild Heart"], "rg-la": ["Wild Heart"]},
    ).add(va, la)
    if namesakes:
        lookup.add(seattle)
    return lookup


def _evangeline_track() -> TrackIntent:
    album = spotify_album("Summer Hits", artists=("Various Artists",), album_type="compilation")
    return track_intent("Wild Heart", album, isrc="ISRC-LA", artists=("Evangeline",))


@pytest.mark.parametrize("scope", ["album", LIKED_TRACK_SCOPE_SMALLEST])
def test_a_compilation_track_takes_the_artist_its_isrc_names_not_the_top_scored_namesake(scope: str) -> None:
    lookup = _evangeline()
    result = resolve_track(
        _evangeline_track(), lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, scope=scope
    )
    assert result.status is ResolutionStatus.RESOLVED
    assert result.release_group is not None and result.release_group.mbid == "rg-la"
    assert result.step == ("track:isrc->album" if scope == "album" else "track:smallest:album")
    assert lookup.calls.get("search_artist_candidates") is None, "the ISRC answered; no name search"


@pytest.mark.parametrize("scope", ["album", LIKED_TRACK_SCOPE_SMALLEST])
def test_a_compilation_track_whose_artist_shares_a_name_and_has_no_isrc_evidence_stays_unmapped(scope: str) -> None:
    lookup = _evangeline(isrc_hits=())
    result = resolve_track(
        _evangeline_track(), lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, scope=scope
    )
    assert (result.status, result.step, result.release_group) == (
        ResolutionStatus.UNMAPPED,
        "track:various-artists",
        None,
    )
    assert "2 MusicBrainz artists are named 'Evangeline'" in result.detail
    assert lookup.calls.get("artist_release_groups") is None, "neither namesake's catalogue is searched"


@pytest.mark.parametrize("scope", ["album", LIKED_TRACK_SCOPE_SMALLEST])
def test_a_compilation_track_whose_artist_is_the_only_one_of_that_name_still_resolves_by_title(scope: str) -> None:
    lookup = _evangeline(isrc_hits=(), namesakes=False)
    result = resolve_track(
        _evangeline_track(), lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, scope=scope
    )
    assert result.status is ResolutionStatus.RESOLVED
    assert result.release_group is not None and result.release_group.mbid == "rg-la"
    assert result.step == "track:title->album"


def test_an_isrc_naming_a_different_credit_is_no_evidence_for_the_compilation_tracks_artist() -> None:
    """Only ISRC hits credited under Spotify's own artist name count; a cover's album does not."""
    lookup = _evangeline(isrc_hits=("rg-cover", "rg-va"))
    lookup.add(rg("rg-cover", "Covers", artist_mbid="mb-3-cover", artist_name="Someone Else"))
    result = resolve(_evangeline_track(), lookup)
    assert (result.status, result.step) == (ResolutionStatus.UNMAPPED, "track:various-artists")


def _swan_of_tuonela(*candidate_credit: str) -> tuple[TrackIntent, FakeLookup]:
    """#164: Berglund and the LPO's "The Swan of Tuonela", filed by Spotify on a live album. The
    composer is every classical release group's first credit, so Sibelius's catalogue is searched,
    and a studio album credited to other performers lists the same title."""
    sibelius = partial(rg, artist_mbid="mb-sibelius", artist_name="Jean Sibelius")
    live = sibelius(
        "rg-berglund-live",
        "Sibelius: Symphonies Live",
        secondary=[SecondaryType.LIVE],
        released="2012",
        credit=("mb-sibelius", "mb-lpo", "mb-berglund"),
    )
    other = sibelius("rg-jarvi", "The Lemminkainen Suite", released="1985", credit=candidate_credit)
    lookup = FakeLookup(
        searches={("Paavo Berglund", "Sibelius: Symphonies Live"): "rg-berglund-live"},
        catalogues={"mb-sibelius": ["rg-jarvi"]},
        tracklists={"rg-jarvi": ["The Swan of Tuonela"]},
    ).add(live, other)
    album = spotify_album("Sibelius: Symphonies Live", artists=("Paavo Berglund",))
    return track_intent("The Swan of Tuonela", album, artists=("Paavo Berglund",)), lookup


def test_a_classical_track_never_takes_another_performers_studio_album_of_the_work() -> None:
    intent, lookup = _swan_of_tuonela("mb-sibelius", "mb-gothenburg", "mb-jarvi")
    result = resolve(intent, lookup)
    assert result.release_group is not None
    assert (result.step, result.release_group.mbid) == ("track:non-studio", "rg-berglund-live")
    assert "rg-jarvi" not in lookup.tracklists_read, "skipped before its tracklist is fetched"
    assert "1 studio release credited to other performers" in result.detail


def test_a_classical_track_takes_a_studio_album_by_the_same_performers() -> None:
    intent, lookup = _swan_of_tuonela("mb-sibelius", "mb-berglund", "mb-lpo")
    result = resolve(intent, lookup)
    assert result.release_group is not None
    assert (result.step, result.release_group.mbid) == ("track:title->album", "rg-jarvi")


@pytest.mark.parametrize(
    ("artist", "guest", "single", "album", "song"),
    [
        (
            "Francis and the Lights",
            "Chance the Rapper",
            "May I Have This Dance",
            "Farewell, Starlite!",
            "May I Have This Dance",
        ),
        ("Dirty Projectors", "Dawn Richard", "Cool Your Heart (remixes)", "Dirty Projectors", "Cool Your Heart"),
    ],
)
def test_a_featured_guest_on_the_named_single_does_not_hide_the_artists_album(
    artist: str, guest: str, single: str, album: str, song: str
) -> None:
    """#164: the named remix single is "X feat. Y" and the album is X's. A guest is not a performer
    in the sense of the check, so the main artists are X on both sides and the album is kept."""
    mine = partial(rg, artist_mbid="mb-x", artist_name=artist)
    remix = mine(
        "rg-single",
        single,
        primary=PrimaryType.SINGLE,
        secondary=[SecondaryType.REMIX],
        released="2017",
        credit=("mb-x",),
    )
    studio = mine("rg-album", album, released="2016", credit=("mb-x",))
    lookup = FakeLookup(
        searches={(artist, single): "rg-single"}, catalogues={"mb-x": ["rg-album"]}, tracklists={"rg-album": [song]}
    ).add(remix, studio)
    intent = track_intent(f"{song} (Remix) [feat. {guest}]", spotify_album(single, artists=(artist, guest)))
    result = resolve(intent, lookup)
    assert (result.step, result.release_group) == ("track:title->album", studio)


def test_two_main_performers_with_a_guest_still_need_the_same_two() -> None:
    """Leaving the guest out does not loosen the check for a real two-performer credit."""
    intent, lookup = _swan_of_tuonela("mb-sibelius", "mb-gothenburg", "mb-jarvi")
    named = lookup.release_groups["rg-berglund-live"]
    lookup.release_groups["rg-berglund-live"] = replace(named, main_artist_mbids=("mb-sibelius", "mb-lpo"))
    result = resolve(intent, lookup)
    assert result.step == "track:non-studio"


def test_a_single_credit_track_still_takes_the_earliest_studio_album_listing_the_title() -> None:
    """The documented "same song" rule for one-artist credits is untouched: Duke Ellington's
    compilation track maps to his earlier studio album, whoever else is on the record."""
    duke = partial(rg, artist_mbid="mb-duke", artist_name="Duke Ellington")
    comp = duke("rg-comp", "The Very Best", secondary=[SecondaryType.COMPILATION], released="2001")
    studio = duke("rg-piano", "Piano Reflections", released="1953", credit=("mb-duke", "mb-bassist"))
    lookup = FakeLookup(
        searches={("Duke Ellington", "The Very Best"): "rg-comp"},
        catalogues={"mb-duke": ["rg-piano"]},
        tracklists={"rg-piano": ["Melancholia"]},
    ).add(comp, studio)
    intent = track_intent("Melancholia", spotify_album("The Very Best", artists=("Duke Ellington",)))
    result = resolve(intent, lookup)
    assert (result.step, result.release_group) == ("track:title->album", studio)


def test_a_release_groups_full_credit_is_left_out_of_equality() -> None:
    """The state database does not store `main_artist_mbids`, so a copy read back from it must still
    equal the fresh one: a cached resolution and a new one of the same release compare equal."""
    fresh = rg("rg-1", "Album", credit=("artist-1", "artist-2"))
    stored = replace(fresh, main_artist_mbids=())
    assert fresh == stored and hash(fresh) == hash(stored)


def test_an_unknown_credit_on_either_side_skips_nothing() -> None:
    """A release group from Lidarr's metadata, or read back from state, carries no credit list."""
    intent, lookup = _swan_of_tuonela()
    lookup.release_groups["rg-jarvi"] = replace(lookup.release_groups["rg-jarvi"], main_artist_mbids=())
    result = resolve(intent, lookup)
    assert result.step == "track:title->album"


def _bach(named: ReleaseGroup) -> tuple[TrackIntent, FakeLookup]:
    """#151: the title search needs Bach's catalogue, which is too large to browse."""
    lookup = FakeLookup(
        searches={("Johann Sebastian Bach", named.title): named.mbid},
        fail={"artist_release_groups"},
        fail_error=CatalogueTooLarge("artist mb-bach has more than 3000 release groups; not browsing further"),
    ).add(named)
    album = spotify_album(named.title, artists=("Johann Sebastian Bach",))
    return track_intent("Air on the G String", album, artists=("Johann Sebastian Bach",)), lookup


BACH = partial(rg, artist_mbid="mb-bach", artist_name="Johann Sebastian Bach")


def test_a_catalogue_too_large_to_search_falls_through_to_the_release_spotify_named() -> None:
    intent, lookup = _bach(BACH("rg-comp", "Baroque Favourites", secondary=[SecondaryType.COMPILATION]))
    result = resolve(intent, lookup)
    assert result.release_group is not None
    assert (result.status, result.step, result.release_group.mbid) == (
        ResolutionStatus.RESOLVED,
        "track:non-studio",
        "rg-comp",
    )
    assert "the title search was skipped: Johann Sebastian Bach's catalogue is too large to browse" in result.detail


def test_a_single_whose_artists_catalogue_is_too_large_waits_for_its_album() -> None:
    intent, lookup = _bach(BACH("rg-single", "Air on the G String", primary=PrimaryType.SINGLE, released="2026-08-01"))
    result = resolve(intent, lookup)
    assert (result.status, result.step) == (ResolutionStatus.PENDING_ALBUM, "track:pending")


def test_a_catalogue_too_large_to_search_is_no_metadata_error() -> None:
    intent, lookup = _bach(BACH("rg-comp", "Baroque Favourites", secondary=[SecondaryType.COMPILATION]))
    result = _resolve_all(snapshot(tracks=[intent]), lookup)
    assert result.metadata_errors == 0
    assert result.degraded is False
    assert result.resolutions[intent.reason.key].step == "track:non-studio"


def test_a_non_compilation_track_names_its_release_artist_without_any_search() -> None:
    album = rg("rg-1", "Evangeline", artist_mbid="mb-1-seattle", artist_name="Evangeline")
    single = rg("rg-s", "Wild Heart", artist_mbid="mb-1-seattle", artist_name="Evangeline", primary=PrimaryType.SINGLE)
    lookup = FakeLookup(
        searches={("Evangeline", "Wild Heart"): "rg-s"},
        catalogues={"mb-1-seattle": ["rg-1", "rg-s"]},
        tracklists={"rg-1": ["Wild Heart"]},
    ).add(album, single)
    lookup.add(rg("rg-la", "Tomorrow", artist_mbid="mb-2-la", artist_name="Evangeline"))
    intent = track_intent("Wild Heart", spotify_album("Wild Heart", artists=("Evangeline",)), artists=("Evangeline",))
    result = resolve(intent, lookup)
    assert (result.step, result.release_group) == ("track:title->album", album)
    assert lookup.calls.get("search_artist_candidates") is None
    assert lookup.calls.get("search_artist") is None


def test_a_various_artists_album_typed_studio_is_never_taken_as_the_artists_album() -> None:
    """MusicBrainz sometimes types a VA compilation as a plain Album; an early version claimed one."""
    from datetime import UTC, datetime

    from likearr.core.resolver import resolve_track
    from likearr.models import (
        VARIOUS_ARTISTS_MBID,
        PrimaryType,
        Reason,
        ReasonKind,
        ReleaseGroup,
        ResolutionStatus,
        SpotifyAlbumRef,
        TrackIntent,
    )

    va_album = ReleaseGroup(
        mbid="va-1",
        title="The Key of Sea Vol. 2",
        artist_mbid=VARIOUS_ARTISTS_MBID,
        artist_name="Various Artists",
        primary_type=PrimaryType.ALBUM,
    )

    class Lookup:
        def release_groups_by_barcode(self, upc: str) -> list:
            return []

        def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
            return va_album

        def search_release_group_candidates(self, artist: str, title: str) -> list[ReleaseGroup]:
            return [va_album]

        def release_groups_for_isrc(self, isrc: str) -> list[ReleaseGroup]:
            return [va_album]

        def search_artist(self, name: str) -> tuple[str, str] | None:
            return None

        def search_artist_candidates(self, name: str) -> list[tuple[str, str]]:
            return []

        def artist_release_groups(self, artist_mbid: str) -> list[ReleaseGroup]:
            raise AssertionError("must not browse")

        def release_group_track_titles(self, rg_mbid: str) -> list[str]:
            return []

    intent = TrackIntent(
        spotify_id="t9",
        name="Some Song",
        isrc="XX0000000099",
        artist_names=("Someone",),
        album=SpotifyAlbumRef("sa9", "The Key of Sea Vol. 2", ("Various Artists",), None, "compilation", None),
        added_at=None,
        reason=Reason(ReasonKind.LIKED, "t9"),
    )
    res = resolve_track(intent, Lookup(), now=datetime(2026, 9, 18, tzinfo=UTC), pending_since=None, fallback_days=180)  # type: ignore[arg-type]
    assert res.status is ResolutionStatus.UNMAPPED
    assert res.step == "track:various-artists"


# --------------------------------------------------------------------------- tracks: liked_track_scope


def resolve_smallest(
    intent,
    lookup: FakeLookup,
    *,
    followed: frozenset[str] = frozenset(),
    now: datetime = NOW,
    pending_since: datetime | None = None,
    rules: ExclusionRules = NO_EXCLUSIONS,
) -> Resolution:
    return resolve_track(
        intent,
        lookup,
        now=now,
        pending_since=pending_since,
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
        followed_artist_mbids=followed,
        rules=rules,
    )


def _dean_martin() -> tuple[TrackIntent, FakeLookup]:
    """#163: MusicBrainz files ISRC USCA29600867 on two Dean Martin recordings, "Good Mornin' Life"
    (on albums only) and "Kiss" (on the 1952 single, and on an earlier album than the song's own).
    Spotify filed the liked song on a compilation."""
    dean = partial(rg, artist_mbid="mb-dean", artist_name="Dean Martin")
    comp = dean("rg-comp", "Dino: The Essential", secondary=[SecondaryType.COMPILATION], released="1998")
    kiss_single = dean("rg-kiss", "What Could Be More Beautiful / Kiss", primary=PrimaryType.SINGLE, released="1952")
    kiss_album = dean("rg-kiss-album", "Dean Martin Sings", released="1953")
    song_album = dean("rg-song-album", "Sleep Warm", released="1959")
    lookup = FakeLookup(
        searches={("Dean Martin", "Dino: The Essential"): "rg-comp"},
        isrcs={"USCA29600867": ["rg-song-album", "rg-comp", "rg-kiss", "rg-kiss-album"]},
        isrc_titles={
            "USCA29600867": {
                "rg-song-album": "Good Mornin' Life",
                "rg-comp": "Good Mornin' Life",
                "rg-kiss": "Kiss",
                "rg-kiss-album": "Kiss",
            }
        },
    ).add(comp, kiss_single, kiss_album, song_album)
    intent = track_intent(
        "Good Mornin' Life",
        spotify_album("Dino: The Essential", artists=("Dean Martin",), album_type="compilation"),
        isrc="USCA29600867",
        artists=("Dean Martin",),
    )
    return intent, lookup


def test_smallest_never_takes_a_single_of_a_different_song_filed_under_the_same_isrc() -> None:
    intent, lookup = _dean_martin()
    result = resolve_smallest(intent, lookup)
    assert result.release_group is not None
    assert (result.step, result.release_group.mbid) == ("track:smallest:album", "rg-song-album")


def test_isrc_to_album_never_takes_an_album_of_a_different_song_filed_under_the_same_isrc() -> None:
    intent, lookup = _dean_martin()
    result = resolve(intent, lookup)
    assert result.release_group is not None
    assert (result.step, result.release_group.mbid) == ("track:isrc->album", "rg-song-album")


def _feelin_alright(liked: str) -> tuple[TrackIntent, FakeLookup]:
    """One ISRC on a spelling variant's recordings: the single under "Feelin' Alright", the studio
    album only under "Feeling Alright". Both are the same song."""
    single = rg("rg-single", "Feelin' Alright", primary=PrimaryType.SINGLE, released="1969")
    album = rg("rg-album", "With a Little Help From My Friends", released="1969-05-01")
    live = rg("rg-live", "Mad Dogs & Englishmen", secondary=[SecondaryType.LIVE], released="1970")
    lookup = FakeLookup(
        searches={("Test Artist", "Mad Dogs & Englishmen"): "rg-live"},
        isrcs={"ISRC-FA": ["rg-single", "rg-album", "rg-live"]},
        isrc_titles={"ISRC-FA": {"rg-single": "Feelin' Alright", "rg-album": "Feeling Alright", "rg-live": liked}},
    ).add(single, album, live)
    return track_intent(liked, spotify_album("Mad Dogs & Englishmen"), isrc="ISRC-FA"), lookup


def test_a_spelling_variants_album_stays_an_isrc_candidate() -> None:
    intent, lookup = _feelin_alright("Feelin' Alright")
    result = resolve(intent, lookup)
    assert result.release_group is not None
    assert (result.step, result.release_group.mbid) == ("track:isrc->album", "rg-album")
    smallest = resolve_smallest(intent, lookup)
    assert smallest.release_group is not None
    assert (smallest.step, smallest.release_group.mbid) == ("track:smallest:single", "rg-single")


def test_when_no_recording_title_is_the_liked_songs_every_isrc_candidate_stays() -> None:
    """Nothing to tell the recordings apart by, so the set is exactly what it was before #163."""
    intent, lookup = _feelin_alright("Something Else Entirely")
    lookup.isrc_titles["ISRC-FA"]["rg-live"] = "Delta Lady"
    assert resolve_smallest(intent, lookup).step == "track:smallest:single"
    result = resolve(intent, lookup)
    assert result.release_group is not None and result.release_group.mbid == "rg-album"


def _album_named_single_also_exists() -> tuple:
    """Spotify named the album; the same song also came out as a single."""
    album = rg("rg-album", "After Hours", released="2020-03-20")
    single = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, released="2019-11-29")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"ISRC1": ["rg-album", "rg-single"]}).add(album, single)
    intent = track_intent("Blinding Lights", spotify_album("After Hours", upc="111"), isrc="ISRC1")
    return intent, lookup, album, single


def test_smallest_picks_the_single_even_though_spotify_named_the_album() -> None:
    intent, lookup, _album, single = _album_named_single_also_exists()
    result = resolve_smallest(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:smallest:single"
    assert result.release_group == single
    assert result.source_release_group is not None
    assert result.source_release_group.mbid == "rg-album"


def test_album_scope_is_unchanged_by_the_new_plumbing() -> None:
    """The same intent under the default scope still resolves to the album, as it always has."""
    intent, lookup, album, _single = _album_named_single_also_exists()
    result = resolve(intent, lookup)
    assert result.step == "track:album"
    assert result.release_group == album
    assert lookup.calls.get("release_groups_for_isrc") is None


def test_smallest_prefers_an_ep_over_an_album_when_there_is_no_single() -> None:
    album = rg("rg-album", "Big Record", released="2018-01-01")
    ep = rg("rg-ep", "Small Record", primary=PrimaryType.EP, released="2019-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-album", "rg-ep"]}).add(album, ep)
    intent = track_intent("Song", spotify_album("Big Record", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:ep"
    assert result.release_group == ep


def test_smallest_picks_the_album_when_nothing_smaller_holds_the_song() -> None:
    album = rg("rg-album", "Only Record")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-album"]}).add(album)
    intent = track_intent("Song", spotify_album("Only Record", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:album"
    assert result.release_group == album


def test_smallest_names_the_alternatives_it_considered() -> None:
    """`explain` has to be able to show why the single beat the album."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    detail = resolve_smallest(intent, lookup).detail
    assert "2 candidate(s) considered" in detail
    assert "'Blinding Lights' (rg-single" in detail
    assert "'After Hours' (rg-album" in detail


def test_a_compilation_candidate_never_wins_over_a_studio_release() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2005-01-01")
    comp = rg("rg-comp", "Greatest Hits", secondary=[SecondaryType.COMPILATION], released="1990-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-comp"}, isrcs={"I1": ["rg-comp", "rg-single"]}).add(single, comp)
    intent = track_intent("Song", spotify_album("Greatest Hits", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:single"
    assert result.release_group == single


def test_a_various_artists_candidate_never_wins_over_a_studio_release() -> None:
    from likearr.models import VARIOUS_ARTISTS_MBID

    va = rg(
        "rg-va",
        "Hits Of The Year",
        primary=PrimaryType.ALBUM,
        artist_mbid=VARIOUS_ARTISTS_MBID,
        artist_name="Various Artists",
        released="1999-01-01",
    )
    album = rg("rg-album", "Real Record", released="2020-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-va", "rg-album"]}).add(va, album)
    intent = track_intent("Song", spotify_album("Real Record", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:album"
    assert result.release_group == album


def test_a_compilation_only_track_keeps_the_album_scopes_answer() -> None:
    """With no studio release anywhere, `smallest` must not resolve less than `album` would."""
    comp = rg("rg-comp", "Hey Jude", secondary=[SecondaryType.COMPILATION], released="1970-02-26")
    lookup = FakeLookup(barcodes={"111": "rg-comp"}, isrcs={"I1": ["rg-comp"]}).add(comp)
    intent = track_intent("Hey Jude", spotify_album("Hey Jude", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == comp


def test_smallest_still_goes_pending_for_a_single_with_no_studio_release_at_all() -> None:
    """A single IS a studio release, so this is really a check that the single wins as itself."""
    single = rg("rg-single", "Skyfall", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Skyfall", spotify_album("Skyfall", upc="111"))
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:single"
    assert result.release_group == single


def test_smallest_ties_break_on_the_earliest_date_then_the_lowest_mbid() -> None:
    album = rg("rg-album", "Record")
    late = rg("rg-a-late", "Late Single", primary=PrimaryType.SINGLE, released="2015-01-01")
    early = rg("rg-b-early", "Early Single", primary=PrimaryType.SINGLE, released="2010-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-a-late", "rg-b-early", "rg-album"]}).add(
        album, late, early
    )
    intent = track_intent("Song", spotify_album("Record", upc="111"), isrc="I1")
    assert resolve_smallest(intent, lookup).release_group == early

    same_a = rg("rg-a", "A Single", primary=PrimaryType.SINGLE, released="2010-01-01")
    same_z = rg("rg-z", "Z Single", primary=PrimaryType.SINGLE, released="2010-01-01")
    tied = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-z", "rg-a"]}).add(album, same_a, same_z)
    assert resolve_smallest(intent, tied).release_group == same_a


def test_smallest_is_deterministic_across_repeated_runs() -> None:
    intent, lookup, _album, _single = _album_named_single_also_exists()
    assert resolve_smallest(intent, lookup) == resolve_smallest(intent, lookup)


# --------------------------------------------------------------------------- smallest: the dedupe rule


def test_a_followed_artists_song_resolves_to_the_album_the_follow_already_monitors() -> None:
    """One like must never add the single next to the album the follow brings anyway."""
    intent, lookup, album, _single = _album_named_single_also_exists()
    result = resolve_smallest(intent, lookup, followed=frozenset({"artist-1"}))
    assert result.step == "track:smallest:covered-by-follow"
    assert result.release_group == album
    assert "followed artist" in result.detail


def test_a_followed_artist_whose_song_is_only_on_a_single_still_gets_the_single() -> None:
    single = rg("rg-single", "Loosie", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"I1": ["rg-single"]}).add(single)
    intent = track_intent("Loosie", spotify_album("Loosie", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup, followed=frozenset({"artist-1"}))
    assert result.step == "track:smallest:single"
    assert result.release_group == single


def test_the_dedupe_rule_prefers_the_ep_over_the_album_for_a_followed_artist() -> None:
    album = rg("rg-album", "Big Record", released="2018-01-01")
    ep = rg("rg-ep", "Small Record", primary=PrimaryType.EP, released="2019-01-01")
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2017-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}, isrcs={"I1": ["rg-single", "rg-album", "rg-ep"]}).add(
        album, ep, single
    )
    intent = track_intent("Song", spotify_album("Song", upc="111"), isrc="I1")
    result = resolve_smallest(intent, lookup, followed=frozenset({"artist-1"}))
    assert result.step == "track:smallest:covered-by-follow"
    assert result.release_group == ep


def test_another_artists_follow_does_not_cover_this_song() -> None:
    intent, lookup, _album, single = _album_named_single_also_exists()
    result = resolve_smallest(intent, lookup, followed=frozenset({"artist-2"}))
    assert result.step == "track:smallest:single"
    assert result.release_group == single


# --------------------------------------------------------------------------- resolve_all and the scope


def test_resolve_all_passes_the_scope_through_to_every_track() -> None:
    album = rg("rg-album", "After Hours")
    single = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, released="2019-11-29")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-album", "rg-single"]}).add(album, single)
    snap = snapshot(tracks=[track_intent("Blinding Lights", spotify_album("After Hours", upc="111"), isrc="I1")])

    default = _resolve_all(snap, lookup)
    assert next(iter(default.resolutions.values())).release_group == album

    smallest = resolve_all(
        snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS, scope="smallest"
    )
    assert next(iter(smallest.resolutions.values())).release_group == single


def test_resolve_all_derives_the_followed_artists_the_dedupe_rule_needs() -> None:
    """Artists resolve first, so a track can know its own artist is followed."""
    album = rg("rg-album", "After Hours", artist_name="The Weeknd")
    single = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, artist_name="The Weeknd")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"I1": ["rg-single", "rg-album"]},
        artists={"artist-1": "The Weeknd"},
    ).add(album, single)
    snap = snapshot(
        artists=[artist_intent("The Weeknd", spotify_id="sp-a")],
        tracks=[
            track_intent(
                "Blinding Lights",
                spotify_album("Blinding Lights", upc="111", artists=("The Weeknd",)),
                isrc="I1",
                artists=("The Weeknd",),
            )
        ],
    )
    result = resolve_all(
        snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS, scope="smallest"
    )
    track = next(iter(result.resolutions.values()))
    assert track.step == "track:smallest:covered-by-follow"
    assert track.release_group == album


def test_a_cached_track_resolution_is_not_reused_after_the_scope_changes() -> None:
    """Flipping liked_track_scope must re-resolve every like, not only the unresolved ones."""
    from likearr.core.resolver import _reusable
    from likearr.models import PrimaryType, ReleaseGroup, Resolution, ResolutionStatus

    rg = ReleaseGroup(mbid="rg", title="Album", artist_mbid="a", artist_name="A", primary_type=PrimaryType.ALBUM)
    cached = Resolution(
        intent_key="liked:t", status=ResolutionStatus.RESOLVED, release_group=rg, step="track:album", scope="album"
    )
    assert _reusable({"liked:t": cached}, "liked:t", Resolution, scope="album") is cached
    assert _reusable({"liked:t": cached}, "liked:t", Resolution, scope="smallest") is None
    assert _reusable({"liked:t": cached}, "liked:t", Resolution) is cached  # scope-agnostic callers unchanged


# ---------------------------------------------- following an artist later (issue #9)


def _followable_world():
    """One liked song that exists both as a single and on the artist's studio album."""
    album = rg("rg-album", "After Hours", artist_name="The Weeknd", released="2020-03-20")
    single = rg(
        "rg-single", "Blinding Lights", artist_name="The Weeknd", primary=PrimaryType.SINGLE, released="2019-11-29"
    )
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"I1": ["rg-single", "rg-album"]},
        artists={"artist-1": "The Weeknd"},
    ).add(album, single)
    track = track_intent(
        "Blinding Lights",
        spotify_album("Blinding Lights", upc="111", artists=("The Weeknd",)),
        isrc="I1",
        artists=("The Weeknd",),
    )
    return lookup, album, single, track


def _smallest_all(snap, lookup, cache=None):
    return resolve_all(
        snap,
        lookup,
        now=NOW,
        cache=cache or {},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
    )


def test_following_the_artist_later_swaps_the_cached_single_for_the_album() -> None:
    """Issue #9: the resolution was permanent, so the follow's dedupe rule never got to apply."""
    lookup, album, single, track = _followable_world()

    before = _smallest_all(snapshot(tracks=[track]), lookup)
    resolved = next(iter(before.resolutions.values()))
    assert resolved.release_group == single
    assert resolved.followed is False

    followed_snapshot = snapshot(artists=[artist_intent("The Weeknd", spotify_id="sp-a")], tracks=[track])
    after = _smallest_all(followed_snapshot, lookup, cache=dict(before.resolutions))
    swapped = next(iter(after.resolutions.values()))

    assert swapped.step == "track:smallest:covered-by-follow"
    assert swapped.release_group == album
    assert swapped.followed is True


def test_unfollowing_the_artist_gives_the_single_back() -> None:
    lookup, _album, single, track = _followable_world()
    followed_snapshot = snapshot(artists=[artist_intent("The Weeknd", spotify_id="sp-a")], tracks=[track])

    before = _smallest_all(followed_snapshot, lookup)
    after = _smallest_all(snapshot(tracks=[track]), lookup, cache=dict(before.resolutions))

    assert next(iter(after.resolutions.values())).release_group == single


def test_an_unchanged_follow_state_still_reuses_the_cached_resolution() -> None:
    """The check must not cost a re-resolution on every ordinary run."""
    lookup, _album, _single, track = _followable_world()
    snap = snapshot(tracks=[track])

    before = _smallest_all(snap, lookup)
    lookup.calls.clear()
    after = _smallest_all(snap, lookup, cache=dict(before.resolutions))

    assert after.resolutions == before.resolutions
    assert lookup.calls == {}


def test_a_resolution_from_before_the_field_existed_is_back_filled_not_re_resolved() -> None:
    """The upgrade path: on a `smallest` library most cached resolutions are covered-by-follow.

    Reading their unrecorded `followed` as False would re-resolve the whole library on the first
    run. The step already says which branch of the dedupe rule ran, so where it agrees with
    today's follow state the cached answer is recorded and kept.
    """
    lookup, album, _single, track = _followable_world()
    followed_snapshot = snapshot(artists=[artist_intent("The Weeknd", spotify_id="sp-a")], tracks=[track])
    first = _smallest_all(followed_snapshot, lookup)
    made = next(iter(first.resolutions.values()))
    assert made.step == "track:smallest:covered-by-follow"

    legacy = {**first.artist_resolutions, made.intent_key: replace(made, followed=None)}
    lookup.calls.clear()
    after = _smallest_all(followed_snapshot, lookup, cache=legacy)
    kept = next(iter(after.resolutions.values()))

    assert lookup.calls == {}, "back-filled in place, not re-resolved"
    assert kept.release_group == album
    assert kept.followed is True, "and the back-fill is written back to the cache"


def test_an_unrecorded_resolution_whose_step_disagrees_is_re_resolved() -> None:
    """A `covered-by-follow` answer for an artist nobody follows any more is exactly the stale one."""
    lookup, _album, single, track = _followable_world()
    followed_snapshot = snapshot(artists=[artist_intent("The Weeknd", spotify_id="sp-a")], tracks=[track])
    made = next(iter(_smallest_all(followed_snapshot, lookup).resolutions.values()))

    legacy = {made.intent_key: replace(made, followed=None)}
    after = _smallest_all(snapshot(tracks=[track]), lookup, cache=legacy)
    fresh = next(iter(after.resolutions.values()))

    assert fresh.release_group == single
    assert fresh.followed is False


def test_follow_state_does_not_disturb_the_album_scope() -> None:
    """Under `album` the follow changes nothing, so re-resolving over it would be waste."""
    lookup, album, _single, track = _followable_world()
    snap = snapshot(tracks=[track])
    before = _resolve_all(snap, lookup)
    assert next(iter(before.resolutions.values())).release_group == album

    lookup.calls.clear()
    followed_snapshot = snapshot(artists=[artist_intent("The Weeknd", spotify_id="sp-a")], tracks=[track])
    after = _resolve_all(followed_snapshot, lookup, cache=dict(before.resolutions))

    assert next(iter(after.resolutions.values())) == next(iter(before.resolutions.values()))


# --------------------------------------------------------------------------- followed artists
#
# A name search cannot tell two artists apart who share a name, and it can silently pick the
# wrong one: a followed "Lawrence" (the New York sibling band) can resolve to a German DJ, and a
# followed "Evangeline" (an L.A. singer-songwriter) to a Seattle alt-country band. MusicBrainz's
# Spotify URL relationship is the authoritative direction and cannot make that mistake.

SP_LAWRENCE = "5rwUYLyUq8gBsVaOUcUxpE"
NY_LAWRENCE = ArtistCandidate("b6e422c0", "Lawrence", "Clyde Lawrence and Gracie Lawrence")
EUROBEAT_LAWRENCE = ArtistCandidate("cf8e5830", "Lawrence", "eurobeat artist")
GERMAN_DJ = "819a9744"


@dataclass(slots=True)
class FakeLinks:
    """An `ArtistLinks` from a dictionary, counting calls so the cached path is testable."""

    by_spotify_id: dict[str, list[ArtistCandidate]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def artists_for_spotify_artist(self, spotify_artist_id: str) -> Sequence[ArtistCandidate]:
        self.calls.append(spotify_artist_id)
        return tuple(self.by_spotify_id.get(spotify_artist_id, []))


def followed(name: str = "Lawrence", spotify_id: str = SP_LAWRENCE) -> ArtistIntent:
    return artist_intent(name, spotify_id=spotify_id)


def test_the_spotify_url_relation_decides_the_artist() -> None:
    links = FakeLinks({SP_LAWRENCE: [NY_LAWRENCE]})
    lookup = FakeLookup(artists={GERMAN_DJ: "Lawrence"})

    result = resolve_artist(followed(), lookup, links=links)

    assert result.status is ResolutionStatus.RESOLVED
    assert result.artist_mbid == NY_LAWRENCE.mbid, "the band the user follows, not the German DJ"
    assert result.step == "artist:spotify-url"
    assert lookup.calls.get("search_artist_candidates") is None, "the name search is never reached"


def test_several_links_prefer_the_artist_already_in_the_library() -> None:
    """MusicBrainz carries bad links too; the library says which artist the user demonstrably has."""
    links = FakeLinks({SP_LAWRENCE: [NY_LAWRENCE, EUROBEAT_LAWRENCE]})

    result = resolve_artist(followed(), FakeLookup(), links=links, known_artist_mbids=frozenset({NY_LAWRENCE.mbid}))

    assert result.artist_mbid == NY_LAWRENCE.mbid
    assert result.step == "artist:spotify-url:in-library"


def test_several_links_and_none_in_the_library_is_refused_not_guessed() -> None:
    links = FakeLinks({SP_LAWRENCE: [NY_LAWRENCE, EUROBEAT_LAWRENCE]})

    result = resolve_artist(followed(), FakeLookup(), links=links)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.artist_mbid is None
    assert result.step == "artist:ambiguous-link"
    assert "Clyde Lawrence and Gracie Lawrence" in result.detail
    assert "eurobeat artist" in result.detail, "both candidates are named, with their disambiguations"


def test_several_links_all_in_the_library_is_still_refused() -> None:
    links = FakeLinks({SP_LAWRENCE: [NY_LAWRENCE, EUROBEAT_LAWRENCE]})
    both = frozenset({NY_LAWRENCE.mbid, EUROBEAT_LAWRENCE.mbid})

    result = resolve_artist(followed(), FakeLookup(), links=links, known_artist_mbids=both)

    assert result.status is ResolutionStatus.UNMAPPED


def test_no_link_falls_back_to_the_name_search() -> None:
    links = FakeLinks()
    lookup = FakeLookup(artists={"mb-someone": "Lawrence"})

    result = resolve_artist(followed(), lookup, links=links)

    assert result.status is ResolutionStatus.RESOLVED
    assert result.artist_mbid == "mb-someone"
    assert result.step == "artist:search"


def test_without_a_links_lookup_at_all_the_name_search_still_works() -> None:
    """Every existing call site passes no `links` and must behave exactly as before."""
    result = resolve_artist(followed(), FakeLookup(artists={"mb-someone": "Lawrence"}))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.step == "artist:search"


def test_several_exact_name_artists_and_no_link_is_refused_not_guessed() -> None:
    """An unlinked followed artist whose name several MusicBrainz artists share is not the top-scored
    one by default: that picked a stranger and monitored their whole catalogue."""
    lookup = FakeLookup(artists={"mb-seattle": "Evangeline", "mb-nola": "Evangeline", "mb-la": "Evangeline"})

    result = resolve_artist(followed("Evangeline", spotify_id="sp-evangeline"), lookup, links=FakeLinks())

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.artist_mbid is None
    assert result.step == ARTIST_AMBIGUOUS_NAME_STEP
    for mbid in ("mb-seattle", "mb-nola", "mb-la"):
        assert mbid in result.detail, "every candidate is named, so a human can pick"


@pytest.mark.parametrize("in_lidarr", [frozenset({"mb-la"}), frozenset({"mb-la", "mb-seattle"})])
def test_several_exact_name_artists_are_refused_even_when_lidarr_holds_one(in_lidarr: frozenset[str]) -> None:
    """Unlike several links, a name is no claim about this Spotify page. The one Lidarr holds may be
    a namesake added by hand, or by an earlier guess, and preferring it would monitor its catalogue."""
    lookup = FakeLookup(artists={"mb-seattle": "Evangeline", "mb-la": "Evangeline"})

    result = resolve_artist(followed("Evangeline", spotify_id="sp-evangeline"), lookup, known_artist_mbids=in_lidarr)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.artist_mbid is None
    assert result.step == ARTIST_AMBIGUOUS_NAME_STEP


def test_a_near_miss_name_is_still_reported_not_resolved() -> None:
    lookup = FakeLookup(artist_searches={"Lawrence": ("mb-other", "Lawrence Arabia")})

    result = resolve_artist(followed(), lookup)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == "artist:search"
    assert "Lawrence Arabia" in result.detail


def test_the_link_lookup_is_asked_once_per_followed_artist() -> None:
    links = FakeLinks({SP_LAWRENCE: [NY_LAWRENCE]})
    snap = snapshot(artists=[followed()])

    resolve_all(
        snap,
        FakeLookup(),
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=180,
        links=links,
        known_artist_mbids=frozenset(),
    )

    assert links.calls == [SP_LAWRENCE], "one lookup, and the adapter caches it on disk"


# ------------------------------------------- the issue #15 opt-outs: box sets and remix EPs

NO_COMPILATIONS = ExclusionRules(allow_compilation_fallback=False)
NO_REMIXES = ExclusionRules(allow_remix_releases=False)
KEEP_NO_REMIX_ONLY = ExclusionRules(allow_remix_releases=False, keep_remix_only_tracks=False)


def _only_on_a_box_set() -> tuple:
    """A song MusicBrainz knows only from a compilation, which is what `track:non-studio` is for."""
    box = rg(
        "rg-box",
        "The Complete Decca Masters",
        secondary=[SecondaryType.COMPILATION],
        released="1994-01-01",
    )
    lookup = FakeLookup(searches={("Judy Garland", "The Complete Decca Masters"): "rg-box"}).add(box)
    intent = track_intent(
        "Over the Rainbow",
        spotify_album("The Complete Decca Masters", artists=("Judy Garland",)),
        artists=("Judy Garland",),
    )
    return intent, lookup, box


def test_a_compilation_only_track_still_monitors_the_compilation_by_default() -> None:
    """The opt-out is opt-in: an untouched config resolves exactly as it did before issue #15."""
    intent, lookup, box = _only_on_a_box_set()
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == box


def test_opting_out_of_compilations_reports_the_box_set_it_refused() -> None:
    intent, lookup, box = _only_on_a_box_set()

    result = resolve(intent, lookup, rules=NO_COMPILATIONS)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_COMPILATION_STEP
    assert is_excluded(result)
    assert result.release_group is None, "nothing is monitored"
    assert result.source_release_group == box, "but the release it refused is still reported"
    assert "The Complete Decca Masters" in result.detail
    assert "allow_compilation_fallback" in result.detail


def test_opting_out_of_compilations_leaves_a_live_album_alone() -> None:
    """Compilation-typed only. Cannonball's 'Live at "The Club"' IS the record of that song."""
    live = rg("rg-live", 'Mercy, Mercy, Mercy! Live at "The Club"', secondary=[SecondaryType.LIVE])
    lookup = FakeLookup(searches={("Cannonball Adderley", live.title): "rg-live"}).add(live)
    intent = track_intent(
        "Mercy, Mercy, Mercy",
        spotify_album(live.title, artists=("Cannonball Adderley",)),
        artists=("Cannonball Adderley",),
    )

    result = resolve(intent, lookup, rules=NO_COMPILATIONS)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == live


def test_opting_out_of_compilations_leaves_the_singles_rule_alone() -> None:
    """A single is not a compilation, so neither the pending wait nor its fallback changes."""
    single = rg("rg-single", "Skyfall", primary=PrimaryType.SINGLE, released="2012-10-04")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Skyfall", spotify_album("Skyfall", upc="111"))

    waiting = resolve(intent, lookup, rules=NO_COMPILATIONS, now=datetime(2012, 11, 1, tzinfo=UTC))
    assert waiting.status == ResolutionStatus.PENDING_ALBUM

    result = resolve(intent, lookup, rules=NO_COMPILATIONS)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:single-fallback"
    assert result.release_group == single


def _remix_ep_holds_the_original() -> tuple:
    """Issue #15's shape: an EP MusicBrainz types as studio, whose title is the only evidence.

    Real case, recorded in the golden corpus: "Grease (The Remix EP)", release group
    2f26958e-b86d-3b3c-8a15-57253046ea58, primary type EP, **no secondary types at all**.
    """
    album = rg("rg-album", "Grease", released="1978-04-14")
    remix_ep = rg("rg-remix", "Grease (The Remix EP)", primary=PrimaryType.EP, released="1998-01-01")
    assert remix_ep.is_studio, "the fixture must reproduce the untagged EP, or it proves nothing"
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-album", "rg-remix"]}).add(album, remix_ep)
    intent = track_intent("You're the One That I Want", spotify_album("Grease", upc="111"), isrc="I1")
    return intent, lookup, album, remix_ep


def test_the_smallest_scope_picks_the_untagged_remix_ep_by_default() -> None:
    """The behaviour the issue is about, pinned so the opt-out below is provably doing something."""
    intent, lookup, _album, remix_ep = _remix_ep_holds_the_original()
    result = resolve_smallest(intent, lookup)
    assert result.step == "track:smallest:ep"
    assert result.release_group == remix_ep


def test_opting_out_of_remixes_falls_through_to_the_real_album() -> None:
    """A filter, not a veto: the EP leaves the running and the album wins on its own merits."""
    intent, lookup, album, _remix_ep = _remix_ep_holds_the_original()

    result = resolve_smallest(intent, lookup, rules=NO_REMIXES)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:smallest:album"
    assert result.release_group == album


def test_the_remix_secondary_type_alone_would_have_changed_nothing() -> None:
    """`is_studio` already refuses a Remix-typed release, which is why the title rule exists."""
    typed = rg("rg-typed", "Something", primary=PrimaryType.EP, secondary=[SecondaryType.REMIX])
    album = rg("rg-album", "Something", released="2001-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-album"}, isrcs={"I1": ["rg-album", "rg-typed"]}).add(album, typed)
    intent = track_intent("Song", spotify_album("Something", upc="111"), isrc="I1")

    assert resolve_smallest(intent, lookup).release_group == album, "already excluded, opt-out or not"


def test_a_liked_remix_still_gets_its_remix() -> None:
    """Refusing every remix release to someone who liked a remix would leave them nothing."""
    intent, lookup, _album, remix_ep = _remix_ep_holds_the_original()
    liked_the_remix = replace(intent, name="You're the One That I Want - Remix")

    result = resolve_smallest(liked_the_remix, lookup, rules=NO_REMIXES)

    assert result.release_group == remix_ep


def test_a_remix_release_spotify_named_outright_is_reported_not_monitored() -> None:
    """Nothing to fall through to, so the refusal itself is the answer - once keeping a remix-only
    song (issue #89) is switched off too. With it on, the default, that same release is kept."""
    remix_ep = rg("rg-remix", "The Feeling (Remixes)", primary=PrimaryType.EP)
    lookup = FakeLookup(barcodes={"111": "rg-remix"}).add(remix_ep)
    intent = track_intent("The Feeling", spotify_album("The Feeling (Remixes)", upc="111"))

    kept = resolve(intent, lookup, rules=NO_REMIXES)
    assert kept.step == REMIX_ONLY_STEP
    assert kept.release_group == remix_ep

    result = resolve(intent, lookup, rules=KEEP_NO_REMIX_ONLY)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_REMIX_STEP
    assert result.source_release_group == remix_ep
    assert "allow_remix_releases" in result.detail


# ------------------------------------------- the deny list


def test_a_denied_release_falls_through_to_the_next_candidate() -> None:
    """Issue #15's 'Mercy, Mercy, Mercy': an untagged live album reads as studio and wins.

    MusicBrainz types Cannonball Adderley's "Live in Concert" (8832ad43-...) Album with no
    secondary types at all, so no rule can tell it from a studio record. Naming it is the only
    way to say "not that one", and the title fallback then lands where the other copies did.
    """
    untagged_live = rg("rg-live-in-concert", "Live in Concert", released="1966-10-20")
    studio = rg("rg-studio", "Somethin' Else", released="1975-03-09")
    lookup = FakeLookup(
        barcodes={"111": "rg-comp"},
        catalogues={"artist-1": ["rg-live-in-concert", "rg-studio"]},
        tracklists={"rg-live-in-concert": ["Mercy, Mercy, Mercy"], "rg-studio": ["Mercy, Mercy, Mercy"]},
    ).add(
        untagged_live,
        studio,
        rg("rg-comp", "Some Collection", secondary=[SecondaryType.COMPILATION]),
    )
    intent = track_intent("Mercy, Mercy, Mercy", spotify_album("Some Collection", upc="111"))

    before = resolve(intent, lookup)
    assert before.step == "track:title->album"
    assert before.release_group == untagged_live, "earliest date wins, and nothing marks it as live"

    after = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-studio"})))
    assert after.release_group == untagged_live

    denied = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-live-in-concert"})))
    assert denied.status == ResolutionStatus.RESOLVED
    assert denied.step == "track:title->album"
    assert denied.release_group == studio


def test_denying_every_candidate_reports_the_release_it_would_have_monitored() -> None:
    box = rg("rg-box", "The Complete Decca Masters", secondary=[SecondaryType.COMPILATION])
    lookup = FakeLookup(searches={("Judy Garland", box.title): "rg-box"}).add(box)
    intent = track_intent("Over the Rainbow", spotify_album(box.title, artists=("Judy Garland",)))

    result = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-box"})))

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_DENIED_STEP
    assert result.source_release_group == box
    assert "deny_releases" in result.detail


def test_denying_the_release_spotify_named_lets_the_isrc_find_another() -> None:
    """The deny list makes the mapping 'fail' on purpose, so the #13 fallback gets its turn."""
    box = rg("rg-box", "Greatest Hits", secondary=[SecondaryType.COMPILATION])
    album = rg("rg-album", "Studio Record", released="1999-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-box"}, isrcs={"I1": ["rg-box", "rg-album"]}).add(box, album)
    intent = track_intent("Song", spotify_album("Greatest Hits", upc="111"), isrc="I1")

    result = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-box"})))

    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == album


# ------------------------------------------- caching, which is where the cost lives


def _cached_under(rules: ExclusionRules) -> tuple:
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
        rules=rules,
    )
    return intent, lookup, dict(first.resolutions)


def _re_resolve(intent, lookup, cache, rules: ExclusionRules):
    lookup.calls.clear()
    resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache=cache,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
        rules=rules,
    )
    return lookup.calls


def test_deploying_the_opt_outs_re_resolves_nothing() -> None:
    """The default token is "", which is also what every pre-#15 row carries. No bump needed."""
    intent, lookup, cache = _cached_under(NO_EXCLUSIONS)
    assert next(iter(cache.values())).rules == ""

    stale = {key: replace(res, rules="") for key, res in cache.items()}
    assert _re_resolve(intent, lookup, stale, NO_EXCLUSIONS) == {}


def test_flipping_a_switch_re_resolves_every_track() -> None:
    intent, lookup, cache = _cached_under(NO_EXCLUSIONS)
    assert _re_resolve(intent, lookup, cache, NO_REMIXES) != {}


def test_adding_a_deny_entry_re_resolves_only_what_landed_on_it() -> None:
    """Otherwise one MBID would cost a full re-resolve of ~2,000 liked tracks."""
    intent, lookup, cache = _cached_under(NO_EXCLUSIONS)
    chosen = next(iter(cache.values())).release_group
    assert chosen is not None

    elsewhere = ExclusionRules(deny_releases=frozenset({"rg-something-else"}))
    assert _re_resolve(intent, lookup, cache, elsewhere) == {}, "this track did not land on it"

    on_it = ExclusionRules(deny_releases=frozenset({chosen.mbid}))
    assert _re_resolve(intent, lookup, cache, on_it) != {}


SINGLE_DENIED = ExclusionRules(deny_releases=frozenset({"rg-single"}))
"""Under `smallest` the fixture's song resolves to the single; denying it falls through to the album."""


def test_a_track_records_the_denied_release_it_fell_through_from() -> None:
    _intent, _lookup, cache = _cached_under(SINGLE_DENIED)
    resolution = next(iter(cache.values()))

    assert resolution.release_group is not None
    assert resolution.release_group.mbid == "rg-album"
    assert resolution.denied_skipped == frozenset({"rg-single"})


def test_a_track_that_met_no_denied_release_records_none() -> None:
    _intent, _lookup, cache = _cached_under(ExclusionRules(deny_releases=frozenset({"rg-something-else"})))

    assert next(iter(cache.values())).denied_skipped == frozenset()


def test_a_still_denied_release_keeps_the_fallback_answer_reused() -> None:
    intent, lookup, cache = _cached_under(SINGLE_DENIED)

    assert _re_resolve(intent, lookup, cache, SINGLE_DENIED) == {}
    also_denied = ExclusionRules(deny_releases=frozenset({"rg-single", "rg-something-else"}))
    assert _re_resolve(intent, lookup, cache, also_denied) == {}, "another entry added elsewhere"


def test_removing_a_deny_entry_re_resolves_what_fell_through_from_it() -> None:
    """Issue #271: `_reusable` only checked the release an answer chose, so a song kept off a denied
    release stayed on its fallback after the entry was removed."""
    intent, lookup, cache = _cached_under(SINGLE_DENIED)

    assert _re_resolve(intent, lookup, cache, NO_EXCLUSIONS) != {}

    again = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache=cache,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
        rules=NO_EXCLUSIONS,
    )
    back = next(iter(again.resolutions.values()))
    assert back.release_group is not None
    assert back.release_group.mbid == "rg-single", "back on the release it was kept off"
    assert back.denied_skipped == frozenset()


def test_removing_a_deny_entry_a_track_never_met_re_resolves_nothing() -> None:
    intent, lookup, cache = _cached_under(ExclusionRules(deny_releases=frozenset({"rg-something-else"})))

    assert _re_resolve(intent, lookup, cache, NO_EXCLUSIONS) == {}


def test_the_deny_list_is_read_only_where_the_probe_can_record_it() -> None:
    """`denied_skipped` is recorded by `_DenyProbe.__contains__`. A new read of `deny_releases`
    elsewhere in the resolver (a set operation, a loop) would bypass it and quietly bring #271 back,
    so any such read has to be looked at, and this list updated, on purpose."""
    tree = ast.parse(inspect.getsource(resolver_module))
    readers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.Attribute) and node.attr == "deny_releases"
    }

    assert readers == {"_refusal", "_reusable", "resolve_all"}


def test_a_row_without_the_field_is_reused_as_before() -> None:
    """Rows written before #271 read `denied_skipped` as empty: nothing is re-resolved on upgrade."""
    intent, lookup, cache = _cached_under(NO_EXCLUSIONS)
    stale = {key: replace(res, denied_skipped=frozenset()) for key, res in cache.items()}

    assert _re_resolve(intent, lookup, stale, SINGLE_DENIED) != {}, "adding still re-resolves what landed on it"
    assert _re_resolve(intent, lookup, stale, NO_EXCLUSIONS) == {}


def _through_state(cache: dict, tmp_path: Path) -> dict:
    """The cache as the next run reads it: written to SQLite and read back, not the objects in hand."""
    with SqliteState(tmp_path / "state.sqlite") as state:
        for resolution in cache.values():
            state.cache_resolution(resolution)
        loaded = {key: state.cached_resolution(key, RESOLVER_VERSION) for key in cache}
    # A lost row would make "re-resolves under other rules" pass for the wrong reason.
    assert all(res is not None for res in loaded.values())
    return loaded


def test_a_non_default_token_survives_the_state_and_is_reused(tmp_path: Path) -> None:
    """Issue #100: the stored row dropped its token, so under `c1r0` nothing was ever reused."""
    intent, lookup, cache = _cached_under(NO_REMIXES)
    stored = _through_state(cache, tmp_path)

    assert next(iter(stored.values())).rules == NO_REMIXES.token == "c1r0"
    assert _re_resolve(intent, lookup, stored, NO_REMIXES) == {}


def test_a_stored_token_still_re_resolves_under_other_rules(tmp_path: Path) -> None:
    intent, lookup, cache = _cached_under(NO_REMIXES)
    stored = _through_state(cache, tmp_path)

    assert _re_resolve(intent, lookup, stored, NO_EXCLUSIONS) != {}


def test_a_stored_fall_through_re_resolves_once_its_entry_is_removed(tmp_path: Path) -> None:
    """Issue #271 through the real state: the skipped release survives SQLite, not just memory."""
    intent, lookup, cache = _cached_under(SINGLE_DENIED)
    stored = _through_state(cache, tmp_path)

    assert next(iter(stored.values())).denied_skipped == frozenset({"rg-single"})
    assert _re_resolve(intent, lookup, stored, SINGLE_DENIED) == {}
    assert _re_resolve(intent, lookup, stored, NO_EXCLUSIONS) != {}


# ------------------------------------------- cached answers expire (issue #165)


def _max_age(days: float) -> Callable[[str], timedelta]:
    return lambda _key: timedelta(days=days)


def _run_at(
    when: datetime,
    intent: AlbumIntent | TrackIntent,
    lookup: FakeLookup,
    cache: dict,
    max_age: Callable[[str], timedelta] | None = None,
) -> tuple[dict, dict]:
    """One run at `when`: the resolutions it ends with, and the lookups it made."""
    lookup.calls.clear()
    world = snapshot(albums=[intent]) if isinstance(intent, AlbumIntent) else snapshot(tracks=[intent])
    result = resolve_all(
        world,
        lookup,
        now=when,
        cache=cache,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        max_age=max_age,
    )
    return dict(result.resolutions), dict(lookup.calls)


def test_a_fresh_answer_records_when_it_was_checked() -> None:
    intent, lookup, _album, _single = _album_named_single_also_exists()

    resolutions, _calls = _run_at(NOW, intent, lookup, {})

    assert next(iter(resolutions.values())).checked_at == NOW


def test_an_answer_younger_than_its_max_age_is_reused_and_keeps_its_clock() -> None:
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})

    again, calls = _run_at(NOW + timedelta(days=119), intent, lookup, first, max_age=_max_age(120))

    assert calls == {}
    assert next(iter(again.values())).checked_at == NOW, "reuse is not a check: the clock must not move"


def test_an_answer_older_than_its_max_age_is_looked_up_again() -> None:
    """Issue #165: a RESOLVED answer was reused until RESOLVER_VERSION moved, however old it was, so
    a MusicBrainz correction never reached an intent that had already resolved."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    later = NOW + timedelta(days=121)

    again, calls = _run_at(later, intent, lookup, first, max_age=_max_age(120))

    assert calls != {}
    assert next(iter(again.values())).checked_at == later, "the re-check restarts the clock"


def test_an_expired_answer_picks_up_what_musicbrainz_says_now() -> None:
    album = rg("rg-album", "After Hours", released="2020-03-20")
    corrected = rg("rg-corrected", "After Hours", released="2020-03-20")
    lookup = FakeLookup(barcodes={"111": "rg-album"}).add(album, corrected)
    intent = album_intent(spotify_album("After Hours", upc="111"))
    first, _ = _run_at(NOW, intent, lookup, {})
    lookup.barcodes["111"] = "rg-corrected"

    kept, _ = _run_at(NOW + timedelta(days=30), intent, lookup, first, max_age=_max_age(120))
    moved, _ = _run_at(NOW + timedelta(days=200), intent, lookup, first, max_age=_max_age(120))

    assert next(iter(kept.values())).release_group == album
    assert next(iter(moved.values())).release_group == corrected


def test_without_a_max_age_an_answer_never_expires() -> None:
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})

    _again, calls = _run_at(NOW + timedelta(days=3650), intent, lookup, first)

    assert calls == {}


def test_the_max_age_is_asked_per_intent_key() -> None:
    """The shell jitters it by key so a library cached in one run does not fall due in one run."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    asked: list[str] = []

    def max_age(key: str) -> timedelta:
        asked.append(key)
        return timedelta(days=120)

    _run_at(NOW + timedelta(days=1), intent, lookup, first, max_age=max_age)

    assert asked == [intent.reason.key]


def test_a_row_from_before_the_clock_is_reused_and_starts_it_now() -> None:
    """Rows written before #165 have no `checked_at`. Re-resolving them all on the first run would
    be the stampede the jitter exists to prevent, so they are reused and their clock starts."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    old = {key: replace(res, checked_at=None) for key, res in first.items()}
    deploy = NOW + timedelta(days=400)

    again, calls = _run_at(deploy, intent, lookup, old, max_age=_max_age(120))

    assert calls == {}
    assert next(iter(again.values())).checked_at == deploy


def test_a_check_time_that_cannot_be_compared_restarts_the_clock_instead_of_failing() -> None:
    """A naive `checked_at` against an aware `now` is read like a missing one, as `_days_since`
    reads the pending clock: one odd row must not raise out of `resolve_all` and stop the run."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    naive = {key: replace(res, checked_at=NOW.replace(tzinfo=None)) for key, res in first.items()}
    later = NOW + timedelta(days=400)

    again, calls = _run_at(later, intent, lookup, naive, max_age=_max_age(120))

    assert calls == {}
    assert next(iter(again.values())).checked_at == later


def _dateless_single() -> tuple:
    """A song on a single neither MusicBrainz nor Spotify dates, with no album: only the pending
    clock can ever settle it on the single."""
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=None)
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    intent = track_intent("Song", spotify_album("Song", upc="111", released=None))
    return intent, lookup


def _settled_on_the_single(intent: TrackIntent, lookup: FakeLookup) -> dict:
    """Settled by the clock; the shell then clears the clock, as `_persist_resolutions` does."""
    first = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={intent.reason.key: NOW - timedelta(days=FALLBACK_DAYS + 20)},
        fallback_days=FALLBACK_DAYS,
    )
    settled = dict(first.resolutions)
    assert next(iter(settled.values())).step == SINGLE_FALLBACK_STEP
    return settled


def test_an_expired_single_fallback_settled_by_the_clock_stays_on_the_single() -> None:
    """Its clock was cleared when it settled, so a plain re-resolve would go back to waiting and
    let go of the single: an expiry must never step an answer backwards (#165 review)."""
    intent, lookup = _dateless_single()
    settled = _settled_on_the_single(intent, lookup)
    later = NOW + timedelta(days=200)

    again, calls = _run_at(later, intent, lookup, settled, max_age=_max_age(120))

    answer = next(iter(again.values()))
    assert calls != {}, "it was looked up again: a corrected single or a new album would land"
    assert (answer.status, answer.step) == (ResolutionStatus.RESOLVED, SINGLE_FALLBACK_STEP)
    assert answer.release_group is not None and answer.release_group.mbid == "rg-single"
    assert answer.checked_at == later


def test_an_expired_single_fallback_with_a_naive_leftover_clock_does_not_fail_the_run() -> None:
    """A clock that cannot be compared with `now` is read as missing, as `_days_since` reads it."""
    intent, lookup = _dateless_single()
    settled = _settled_on_the_single(intent, lookup)
    later = NOW + timedelta(days=200)

    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=later,
        cache=settled,
        pending_since={intent.reason.key: NOW.replace(tzinfo=None)},
        fallback_days=FALLBACK_DAYS,
        max_age=_max_age(120),
    )

    answer = next(iter(result.resolutions.values()))
    assert (answer.status, answer.step) == (ResolutionStatus.RESOLVED, SINGLE_FALLBACK_STEP)


def test_an_expired_single_fallback_still_moves_to_an_album_that_appeared() -> None:
    intent, lookup = _dateless_single()
    settled = _settled_on_the_single(intent, lookup)
    album = rg("rg-album", "The Album", released="2026-01-01")
    lookup.add(album)
    lookup.tracklists["rg-album"] = ["Song"]
    lookup.catalogues["artist-1"] = ["rg-album", "rg-single"]

    again, _ = _run_at(NOW + timedelta(days=200), intent, lookup, settled, max_age=_max_age(120))

    answer = next(iter(again.values()))
    assert answer.release_group is not None and answer.release_group.mbid == "rg-album"


def test_an_expired_single_fallback_is_kept_even_when_the_single_is_now_dated_recent() -> None:
    """The backstop: a corrected release date inside the window would otherwise send it back to
    waiting. The cached answer stands, its clock restarted."""
    intent, lookup = _dateless_single()
    settled = _settled_on_the_single(intent, lookup)
    later = NOW + timedelta(days=200)
    dated = (later - timedelta(days=5)).date().isoformat()
    lookup.add(rg("rg-single", "Song", primary=PrimaryType.SINGLE, released=dated))

    again, _ = _run_at(later, intent, lookup, settled, max_age=_max_age(120))

    answer = next(iter(again.values()))
    assert (answer.status, answer.step) == (ResolutionStatus.RESOLVED, SINGLE_FALLBACK_STEP)
    assert answer.checked_at == later


def test_an_expired_answer_is_served_stale_when_musicbrainz_fails() -> None:
    """As `mb_cache` serves a stale entry when its refetch fails: a failed re-check never costs a
    mapping. The answer stays due, so the next run tries again."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    lookup.fail = {"release_groups_by_barcode"}

    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW + timedelta(days=200),
        cache=first,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        max_age=_max_age(120),
    )

    assert result.resolutions == first, "the cached answer, clock unmoved"
    assert result.metadata_errors == 1, "the failure is still reported"
    assert result.provisional == set()


def test_an_expired_answer_is_served_stale_when_the_re_check_is_only_provisional() -> None:
    """A re-check reached after a MusicBrainz failure (a Lidarr stand-in, say) is not trusted over
    an answer MusicBrainz confirmed: the cached one is kept and stays due."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    first, _ = _run_at(NOW, intent, lookup, {})
    failures = iter(range(100))

    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW + timedelta(days=200),
        cache=first,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        max_age=_max_age(120),
        lookup_failures=lambda: next(failures),
    )

    assert result.resolutions == first
    assert result.provisional == set()


def test_a_first_resolve_after_a_failure_is_still_provisional() -> None:
    """Serving stale needs something cached: an intent with no answer yet behaves as before (#53)."""
    intent, lookup, _album, _single = _album_named_single_also_exists()
    failures = iter(range(100))

    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        max_age=_max_age(120),
        lookup_failures=lambda: next(failures),
    )

    assert result.provisional == {intent.reason.key}


def test_expiry_leaves_the_other_reuse_rules_alone() -> None:
    """A young answer under changed rules still re-resolves: age is one more reason, not the only one."""
    intent, lookup, cache = _cached_under(NO_EXCLUSIONS)
    lookup.calls.clear()
    resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW + timedelta(days=1),
        cache=cache,
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope="smallest",
        rules=NO_REMIXES,
        max_age=_max_age(120),
    )
    assert lookup.calls != {}


# ------------------------------------------- same-name artists with a same-titled album (issue #32)
#
# The name search is by title and artist *name*, so two MusicBrainz artists called "Jungle" who both
# put out an album called "Jungle" tie exactly. The track's own ISRC decides between them; a date
# never does, and when nothing decides it the answer is UNMAPPED rather than a guess.

LONDON, US = "artist-london", "artist-us"
LONDON_ALBUM = rg("rg-london", "Jungle", artist_mbid=LONDON, artist_name="Jungle", released="2014-07-14")
LONDON_SINGLE = rg(
    "rg-london-single",
    "Busy Earnin'",
    artist_mbid=LONDON,
    artist_name="Jungle",
    primary=PrimaryType.SINGLE,
    released="2014-06-02",
)
US_1969 = rg("rg-1969", "Jungle", artist_mbid=US, artist_name="Jungle", released="1969-01-01")


def _jungle_lookup(isrc_hits: list[str]) -> FakeLookup:
    return FakeLookup(isrcs={"GBBKS1400112": isrc_hits}).add(LONDON_ALBUM, LONDON_SINGLE, US_1969)


def _busy_earnin(isrc: str | None = "GBBKS1400112"):
    return track_intent("Busy Earnin'", spotify_album("Jungle", artists=("Jungle",)), isrc=isrc, artists=("Jungle",))


def test_same_name_artists_are_told_apart_by_the_tracks_isrc() -> None:
    result = resolve(_busy_earnin(), _jungle_lookup(["rg-london", "rg-london-single"]))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == LONDON_ALBUM
    assert "2 different artists named 'Jungle'" in result.detail
    assert "ISRC GBBKS1400112" in result.detail


def test_same_name_artists_under_the_smallest_scope_pick_within_the_isrcs_artist() -> None:
    lookup = _jungle_lookup(["rg-london", "rg-london-single"])
    result = resolve_track(
        _busy_earnin(), lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, scope="smallest"
    )
    assert result.release_group == LONDON_SINGLE
    assert result.step == "track:smallest:single"


def test_same_name_artists_with_no_isrc_are_ambiguous_rather_than_the_earliest() -> None:
    result = resolve(_busy_earnin(isrc=None), _jungle_lookup([]))
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert result.release_group is None and result.source_release_group is None
    assert "rg-1969" in result.detail and "rg-london" in result.detail
    assert "no ISRC" in result.detail


def test_same_name_artists_whose_isrc_names_neither_are_ambiguous() -> None:
    """An ISRC that only a third artist's release carries is no evidence for either candidate."""
    stranger = rg("rg-stranger", "Covers", artist_mbid="artist-stranger", artist_name="Someone Else")
    lookup = _jungle_lookup(["rg-stranger"]).add(stranger)
    result = resolve(_busy_earnin(), lookup)
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert "none of them by any of these artists" in result.detail


def test_same_name_artists_whose_isrc_names_both_are_ambiguous() -> None:
    result = resolve(_busy_earnin(), _jungle_lookup(["rg-london", "rg-1969"]))
    assert result.step == AMBIGUOUS_SAME_NAME_STEP


def test_a_saved_album_whose_title_two_same_name_artists_share_is_ambiguous() -> None:
    result = resolve_album(album_intent(spotify_album("Jungle", artists=("Jungle",))), _jungle_lookup([]))
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP


def test_an_ambiguous_intent_is_re_resolved_every_run_and_is_never_a_metadata_failure() -> None:
    """UNMAPPED is never reused from the cache, so MusicBrainz gaining the ISRC fixes it next run."""
    intent = _busy_earnin(isrc=None)
    first = resolve_all(
        snapshot(tracks=[intent]), _jungle_lookup([]), now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS
    )
    lookup = _jungle_lookup([])
    resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache=dict(first.resolutions),
        pending_since={},
        fallback_days=FALLBACK_DAYS,
    )
    assert lookup.calls["search_release_group_candidates"] == 1
    assert first.metadata_errors == 0


# The other shape: the search returns two same-named artists, but only ONE passes the title check -
# and it is the wrong one. MusicBrainz's "Still Feeling You (Deluxe 2020)" by the right Couch
# passes the adapter's gate (it deletes any parenthetical) but not the resolver's; a stranger's
# plain "Still Feeling You" passes both. Taking the lone survivor by name is the #32 guess again.

RIGHT_COUCH, WRONG_COUCH = "artist-couch", "artist-other-couch"
COUCH_DELUXE = rg("rg-couch-deluxe", "Still Feeling You (Deluxe 2020)", artist_mbid=RIGHT_COUCH, artist_name="Couch")
STRANGER_COUCH = rg(
    "rg-other-couch", "Still Feeling You", artist_mbid=WRONG_COUCH, artist_name="Couch", released="1999-01-01"
)


def _couch_lookup(isrc_hits: list[str], *, artist_search: tuple[str, str] | None) -> FakeLookup:
    return FakeLookup(
        candidate_searches={("Couch", "Still Feeling You"): ["rg-other-couch", "rg-couch-deluxe"]},
        isrcs={"USCOUCH00001": isrc_hits},
        artist_searches={"Couch": artist_search},
    ).add(COUCH_DELUXE, STRANGER_COUCH)


def _still_feeling_you(isrc: str | None = "USCOUCH00001"):
    return track_intent(
        "Still Feeling You", spotify_album("Still Feeling You", artists=("Couch",)), isrc=isrc, artists=("Couch",)
    )


def test_a_lone_title_survivor_the_isrc_contradicts_is_not_taken() -> None:
    """The ISRC names the other Couch, and its own release is found through the ISRC fallback."""
    lookup = _couch_lookup(["rg-couch-deluxe"], artist_search=(RIGHT_COUCH, "Couch"))
    result = resolve(_still_feeling_you(), lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == COUCH_DELUXE
    assert "so that match is not taken" in result.detail


def test_a_contradicted_survivor_with_no_resolvable_isrc_release_is_ambiguous() -> None:
    """The ISRC says the survivor is wrong, but its own release cannot be reached: refuse, don't guess."""
    lookup = _couch_lookup(["rg-couch-deluxe"], artist_search=(WRONG_COUCH, "Couch"))
    result = resolve(_still_feeling_you(), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert result.release_group is None


def test_a_lone_title_survivor_the_isrc_confirms_is_taken() -> None:
    lookup = _couch_lookup(["rg-other-couch"], artist_search=None)
    result = resolve(_still_feeling_you(), lookup)
    assert result.release_group == STRANGER_COUCH
    assert "the ISRC does not contradict it" in result.detail


def test_a_lone_title_survivor_with_no_isrc_evidence_stands_on_its_title() -> None:
    """No ISRC, or one only a compilation carries, says nothing against the title match."""
    compilation = rg("rg-comp", "Hits", artist_mbid=VARIOUS_ARTISTS_MBID, artist_name="Various Artists")
    for isrc, hits in ((None, []), ("USCOUCH00001", ["rg-comp"])):
        lookup = _couch_lookup(hits, artist_search=None).add(compilation)
        assert resolve(_still_feeling_you(isrc), lookup).release_group == STRANGER_COUCH


# ------------------------------------------- a credit MusicBrainz joins by a relationship (issue #14)
#
# The shapes are the ones the #14 re-measurement found at `track:album:search`, with
# made-up MBIDs: the golden corpus holds the one case recorded from MusicBrainz itself (Try!).


def _related(intent, lookup: FakeLookup, *, rules: ExclusionRules = NO_EXCLUSIONS, scope: str = "album") -> Resolution:
    return resolve_track(
        intent,
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
        scope=scope,
        rules=rules,
        relations=lookup,
    )


def _try_world() -> tuple:
    """John Mayer / John Mayer Trio, *Try!*: 12 of the 16 intents #14 is for."""
    try_live = rg(
        "rg-try",
        "Try!",
        artist_mbid="mb-trio",
        artist_name="John Mayer Trio",
        secondary=[SecondaryType.LIVE],
        released="2005-11-22",
    )
    lookup = FakeLookup(
        relations={
            "mb-trio": [
                relation("mb-mayer", "John Mayer"),
                relation("mb-palladino", "Pino Palladino"),
                relation("mb-jordan", "Steve Jordan"),
            ]
        },
        tracklists={"rg-try": ["Who Did You Think I Was", "Good Love Is on the Way", "Gravity"]},
    ).add(try_live)
    intent = track_intent(
        "Gravity",
        spotify_album("TRY! - Live In Concert", artists=("John Mayer",)),
        isrc="ZZ0000000001",
        artists=("John Mayer",),
    )
    return intent, lookup, try_live


def test_a_title_under_a_band_the_spotify_artist_is_a_member_of_is_taken() -> None:
    """*Try!* is Album + Live, MusicBrainz knows no ISRC for it and the Trio has no studio album
    holding the song, so it is `track:non-studio` to *Try!* itself - the intended outcome on #14."""
    intent, lookup, try_live = _try_world()

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == try_live
    assert result.source_release_group == try_live
    joined = "'member of band' relationship between 'John Mayer Trio' (mb-trio) and 'John Mayer' (mb-mayer)"
    assert joined in result.detail


def test_without_the_relationship_lookup_the_try_shape_stays_unmapped() -> None:
    """`relations=None` is the resolver as it was: the credit alone still refuses it."""
    intent, lookup, _ = _try_world()
    result = resolve(intent, lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert lookup.calls.get("artist_relations", 0) == 0


@pytest.mark.parametrize(
    ("spotify_credit", "title", "mb_credit", "direction", "secondary", "step"),
    [
        # Clyde Lawrence is a member of Lawrence: the relation is on Clyde's side, forward.
        ("Lawrence", "Homesick", "Clyde Lawrence", "forward", (), "track:album"),
        (
            "David Bromberg",
            "Reckless Abandon/Bandit In a Bathing Suit",
            "David Bromberg Band",
            "backward",
            (),
            "track:album",
        ),
        (
            "Max Roach",
            "Verve Jazz Masters 44",
            "The Clifford Brown\u2013Max Roach Quintet",
            "backward",
            (SecondaryType.COMPILATION,),
            "track:non-studio",
        ),
    ],
)
def test_the_other_measured_shapes_are_taken(
    spotify_credit: str,
    title: str,
    mb_credit: str,
    direction: str,
    secondary: tuple[SecondaryType, ...],
    step: str,
) -> None:
    found = rg("rg-found", title, artist_mbid="mb-credited", artist_name=mb_credit, secondary=secondary)
    lookup = FakeLookup(
        relations={"mb-credited": [relation("mb-spotify", spotify_credit, direction=direction)]},
        tracklists={"rg-found": ["Another Song", "A Song"]},
    ).add(found)
    intent = track_intent("A Song", spotify_album(title, artists=(spotify_credit,)), artists=(spotify_credit,))

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == step
    assert result.release_group == found


def test_no_relationship_at_all_stays_unmapped_and_says_so() -> None:
    """Sister Sparrow & The Dirty Birds: MusicBrainz records no artist relationships for them, so
    their intents wait for an editor to add Sister Sparrow as a member. Then they resolve unaided."""
    weather = rg(
        "rg-weather", "The Weather Below", artist_mbid="mb-ssdb", artist_name="Sister Sparrow & The Dirty Birds"
    )
    lookup = FakeLookup(tracklists={"rg-weather": ["Sugar"]}).add(weather)
    album = spotify_album("The Weather Below", artists=("Sister Sparrow",))
    intent = track_intent("Sugar", album, artists=("Sister Sparrow",))

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert result.release_group is None
    assert "records no 'collaboration' or 'member of band' relationship" in result.detail
    assert lookup.calls["artist_relations"] == 1

    lookup.relations["mb-ssdb"] = [relation("mb-sparrow", "Sister Sparrow")]
    assert _related(intent, lookup).release_group == weather


def test_members_of_an_unrelated_band_are_no_join() -> None:
    """The Late Show Band has `member of band` relationships - to its players, none of them Stay
    Human. The type is not enough on its own: the other end must be Spotify's credit. (The real
    case fails on the title too; this one gives it a matching title to prove the join refuses it.)"""
    album = rg("rg-late", "The Late Show EP", artist_mbid="mb-lsb", artist_name="The Late Show Band")
    lookup = FakeLookup(relations={"mb-lsb": [relation("mb-cato", "Louis Cato"), relation("mb-saylor", "Joe Saylor")]})
    lookup.add(album)
    intent = track_intent(
        "Humanism", spotify_album("The Late Show EP", artists=("Stay Human",)), artists=("Stay Human",)
    )

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert lookup.calls["artist_relations"] == 1


def test_a_title_no_other_credit_carries_costs_no_relationship_lookup_and_no_new_detail() -> None:
    """Stay Human's shape: the title fails too. That is almost every UNMAPPED track, and for
    them the rule must be invisible - no request, and exactly the answer they had before."""
    other = rg("rg-other", "Live on Broadway", artist_mbid="mb-lsb", artist_name="The Late Show Band")
    lookup = FakeLookup(relations={"mb-lsb": [relation("mb-sh", "Stay Human")]}).add(other)
    intent = track_intent(
        "Humanism", spotify_album("The Late Show EP", artists=("Stay Human",)), artists=("Stay Human",)
    )

    with_rule = _related(intent, lookup)
    without = resolve(intent, lookup)

    assert with_rule == without
    assert lookup.calls.get("artist_relations", 0) == 0


@pytest.mark.parametrize("kind", ["sibling", "tribute", "supporting musician", "subgroup", "is person", ""])
def test_no_other_relationship_type_joins_two_credits(kind: str) -> None:
    """Clyde and Gracie Lawrence are siblings, and that is no reason to file her records under him."""
    assert kind not in JOINING_RELATIONSHIPS
    homesick = rg("rg-homesick", "Homesick", artist_mbid="mb-clyde", artist_name="Clyde Lawrence")
    lookup = FakeLookup(relations={"mb-clyde": [relation("mb-lawrence", "Lawrence", kind, "forward")]}).add(homesick)
    intent = track_intent("Homesick", spotify_album("Homesick", artists=("Lawrence",)), artists=("Lawrence",))
    assert _related(intent, lookup).status == ResolutionStatus.UNMAPPED


def test_collaboration_joins_two_credits() -> None:
    project = rg("rg-proj", "Joint Record", artist_mbid="mb-proj", artist_name="Two Names Project")
    lookup = FakeLookup(
        relations={"mb-proj": [relation("mb-a", "Name One", "collaboration")]}, tracklists={"rg-proj": ["Track"]}
    ).add(project)
    intent = track_intent("Track", spotify_album("Joint Record", artists=("Name One",)), artists=("Name One",))
    result = _related(intent, lookup)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == project


@pytest.mark.parametrize(
    ("spotify_credit", "related_name"),
    [
        ("John Mayer", "John Mayer Band"),  # containment one way ...
        ("John Mayer Band", "John Mayer"),  # ... and the other
        ("Lawrence", "Lawrence Welk"),
    ],
)
def test_the_related_artist_must_be_the_spotify_credit_exactly(spotify_credit: str, related_name: str) -> None:
    """The relationship makes two MBIDs one act; the name only picks out which relation is
    Spotify's credit, and it is full equality there too - containment is what #14 refused."""
    found = rg("rg-x", "Some Record", artist_mbid="mb-credited", artist_name="Some Other Credit")
    lookup = FakeLookup(relations={"mb-credited": [relation("mb-related", related_name)]}).add(found)
    intent = track_intent("Song", spotify_album("Some Record", artists=(spotify_credit,)), artists=(spotify_credit,))
    assert _related(intent, lookup).status == ResolutionStatus.UNMAPPED


def test_the_credit_comparison_is_the_name_searchs_fold() -> None:
    """A leading "the", case and punctuation fold exactly as the name search's credit gate does."""
    found = rg("rg-x", "Some Record", artist_mbid="mb-credited", artist_name="Some Other Credit")
    lookup = FakeLookup(
        relations={"mb-credited": [relation("mb-related", "The Band Name")]}, tracklists={"rg-x": ["Song"]}
    ).add(found)
    intent = track_intent("Song", spotify_album("Some Record", artists=("band name",)), artists=("band name",))
    assert _related(intent, lookup).release_group == found


def test_a_relation_back_to_the_credited_artist_itself_is_no_join() -> None:
    found = rg("rg-x", "Some Record", artist_mbid="mb-credited", artist_name="Some Other Credit")
    lookup = FakeLookup(relations={"mb-credited": [relation("mb-credited", "Name")]}).add(found)
    intent = track_intent("Song", spotify_album("Some Record", artists=("Name",)), artists=("Name",))
    assert _related(intent, lookup).status == ResolutionStatus.UNMAPPED


def test_two_joined_artists_are_doubt_and_neither_is_taken() -> None:
    trio = rg("rg-a", "Shared Title", artist_mbid="mb-trio", artist_name="Name Trio", released="2001-01-01")
    band = rg("rg-b", "Shared Title", artist_mbid="mb-band", artist_name="Name Band", released="2002-01-01")
    lookup = FakeLookup(relations={"mb-trio": [relation("mb-name", "Name")], "mb-band": [relation("mb-name", "Name")]})
    lookup.add(trio, band)
    intent = track_intent("Song", spotify_album("Shared Title", artists=("Name",)), artists=("Name",))

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert "2 of them are related to 'Name', so none is chosen" in result.detail


def test_an_unrelated_artist_sharing_the_title_does_not_stop_the_related_one() -> None:
    """Same-title collisions are dense - 26 terms in the #14 probe. They are simply not joined."""
    stranger = rg("rg-s", "Homesick", artist_mbid="mb-stranger", artist_name="Lawrence Welk", released="1960-01-01")
    homesick = rg("rg-h", "Homesick", artist_mbid="mb-clyde", artist_name="Clyde Lawrence", released="2020-01-01")
    lookup = FakeLookup(
        relations={
            "mb-clyde": [relation("mb-lawrence", "Lawrence", direction="forward")],
            "mb-stranger": [relation("mb-orch", "Lawrence Welk Orchestra", direction="forward")],
        },
        tracklists={"rg-h": ["Homesick"], "rg-s": ["Homesick"]},
    ).add(stranger, homesick)
    intent = track_intent("Homesick", spotify_album("Homesick", artists=("Lawrence",)), artists=("Lawrence",))

    result = _related(intent, lookup)

    assert result.release_group == homesick
    assert lookup.calls["artist_relations"] == 2, "one lookup per distinct credited artist"


def test_the_earliest_of_the_joined_artists_same_titled_releases_is_taken() -> None:
    """As for any liked track's name search: earliest date, then MBID."""
    later = rg("rg-late", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2008-01-01")
    first = rg("rg-first", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2005-11-22")
    lookup = FakeLookup(
        relations={"mb-trio": [relation("mb-mayer", "John Mayer")]},
        tracklists={"rg-late": ["Gravity"], "rg-first": ["Gravity"]},
    ).add(later, first)
    intent = track_intent("Gravity", spotify_album("Try!", artists=("John Mayer",)), artists=("John Mayer",))
    result = _related(intent, lookup)
    assert result.release_group == first
    assert lookup.calls["artist_relations"] == 1, "one lookup for the one artist, however many releases"


def test_a_various_artists_credit_is_never_taken() -> None:
    comp = rg("rg-va", "Hits", artist_mbid=VARIOUS_ARTISTS_MBID, artist_name="Various Artists")
    lookup = FakeLookup(relations={VARIOUS_ARTISTS_MBID: [relation("mb-a", "Some Artist", "collaboration")]}).add(comp)
    intent = track_intent("Song", spotify_album("Hits", artists=("Some Artist",)), artists=("Some Artist",))
    result = _related(intent, lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls.get("artist_relations", 0) == 0


def test_the_name_search_that_matches_the_credit_is_never_second_guessed() -> None:
    """The rule runs only where the resolver would otherwise give up."""
    own = rg("rg-own", "Try!", artist_mbid="mb-mayer", artist_name="John Mayer")
    trio = rg("rg-try", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio")
    lookup = FakeLookup(relations={"mb-trio": [relation("mb-mayer", "John Mayer")]}).add(own, trio)
    intent = track_intent("Gravity", spotify_album("Try!", artists=("John Mayer",)), artists=("John Mayer",))

    result = _related(intent, lookup)

    assert result.release_group == own
    assert lookup.calls.get("release_groups_under_other_credits", 0) == 0
    assert lookup.calls.get("artist_relations", 0) == 0


def test_the_isrc_stand_in_is_never_second_guessed() -> None:
    """An ISRC names this recording; a relationship only joins two artists. The ISRC goes first."""
    intent, lookup, try_live = _try_world()
    lookup.isrcs["ZZ0000000001"] = ["rg-try"]
    result = _related(intent, lookup)
    assert result.release_group == try_live
    assert "member of band" not in result.detail
    assert lookup.calls.get("artist_relations", 0) == 0


def test_the_stripped_title_is_tried_like_the_name_search_tries_it() -> None:
    ep = rg("rg-ep", "Kangaroo", primary=PrimaryType.EP, artist_mbid="mb-band", artist_name="Kyle Andrews Band")
    lookup = FakeLookup(
        relations={"mb-band": [relation("mb-kyle", "Kyle Andrews")]}, tracklists={"rg-ep": ["Song"]}
    ).add(ep)
    intent = track_intent("Song", spotify_album("Kangaroo - EP", artists=("Kyle Andrews",)), artists=("Kyle Andrews",))
    result = _related(intent, lookup)
    assert result.step == "track:album"
    assert result.release_group == ep
    assert result.detail.count("member of band") == 1, "track:album quotes the mapping once, not twice"


def test_an_opted_out_release_under_the_joined_credit_is_reported_as_excluded() -> None:
    """Verve Jazz Masters 44 with `allow_compilation_fallback = false`: the opt-out still decides,
    and says which release it refused, rather than the credit mismatch that preceded it."""
    comp = rg(
        "rg-verve",
        "Verve Jazz Masters 44",
        artist_mbid="mb-quintet",
        artist_name="The Clifford Brown\u2013Max Roach Quintet",
        secondary=[SecondaryType.COMPILATION],
    )
    lookup = FakeLookup(
        relations={"mb-quintet": [relation("mb-roach", "Max Roach")]}, tracklists={"rg-verve": ["Jordu"]}
    ).add(comp)
    intent = track_intent(
        "Jordu", spotify_album("Verve Jazz Masters 44", artists=("Max Roach",)), artists=("Max Roach",)
    )

    result = _related(intent, lookup, rules=NO_COMPILATIONS)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_COMPILATION_STEP
    assert result.release_group is None
    assert result.source_release_group == comp
    assert result.detail.count("member of band") == 1


def test_the_joined_credits_studio_album_holding_the_song_wins_over_the_compilation() -> None:
    """The rest of the Singles rule applies unchanged under the joined credit: a song the joined
    artist's studio album also holds resolves to that album, as it would under any credit."""
    comp = rg(
        "rg-verve",
        "Verve Jazz Masters 44",
        artist_mbid="mb-quintet",
        artist_name="The Clifford Brown\u2013Max Roach Quintet",
        secondary=[SecondaryType.COMPILATION],
        released="1995-01-01",
    )
    studio = rg(
        "rg-study",
        "Study in Brown",
        artist_mbid="mb-quintet",
        artist_name="The Clifford Brown\u2013Max Roach Quintet",
        released="1955-01-01",
    )
    lookup = FakeLookup(
        relations={"mb-quintet": [relation("mb-roach", "Max Roach")]},
        catalogues={"mb-quintet": ["rg-verve", "rg-study"]},
        tracklists={"rg-study": ["Cherokee", "Jacqui"], "rg-verve": ["Cherokee", "Jordu"]},
    ).add(comp, studio)
    album = spotify_album("Verve Jazz Masters 44", artists=("Max Roach",))
    intent = track_intent("Cherokee", album, artists=("Max Roach",))

    result = _related(intent, lookup)

    assert result.step == "track:title->album"
    assert result.release_group == studio
    assert result.source_release_group == comp
    assert "member of band" in result.detail


def test_the_smallest_scope_falls_back_to_the_joined_live_album_too() -> None:
    intent, lookup, try_live = _try_world()
    result = _related(intent, lookup, scope=LIKED_TRACK_SCOPE_SMALLEST)
    assert result.step == "track:non-studio"
    assert result.release_group == try_live
    assert "member of band" in result.detail


def test_resolve_all_passes_the_relationship_lookup_on() -> None:
    intent, lookup, try_live = _try_world()
    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        relations=lookup,
    )
    resolution = result.resolutions[intent.reason.key]
    assert resolution.release_group == try_live
    assert resolution.resolver_version == RESOLVER_VERSION == 12
    assert not result.provisional


def test_a_relationship_lookup_failure_is_a_metadata_error_for_the_plain_lookup() -> None:
    """The fake raises, as `MusicBrainzLookup` does; `CompositeLookup` is what turns it into
    "could not be read" (tested with the adapters). Either way nothing wrong is monitored."""
    intent, lookup, _ = _try_world()
    lookup.fail = {"artist_relations"}
    result = resolve_all(
        snapshot(tracks=[intent]),
        lookup,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        relations=lookup,
    )
    assert result.resolutions[intent.reason.key].step == "error:metadata"
    assert result.metadata_errors == 1


def test_a_saved_album_is_not_joined_by_a_relationship() -> None:
    """Only liked and playlist tracks were measured; a saved album resolves as it did."""
    _, lookup, _ = _try_world()
    result = resolve_album(album_intent(spotify_album("Try!", artists=("John Mayer",))), lookup)
    assert result.status == ResolutionStatus.UNMAPPED


def test_a_track_spotify_files_under_various_artists_asks_nothing() -> None:
    """Nobody is a member of "Various Artists"; the rule must not spend requests finding that out."""
    comp = rg("rg-hits", "Summer Hits", artist_mbid="mb-dj", artist_name="Some DJ")
    lookup = FakeLookup(relations={"mb-dj": [relation("mb-va", "Various Artists", "collaboration")]}).add(comp)
    album = spotify_album("Summer Hits", artists=("Various Artists",), album_type="compilation")
    intent = track_intent("Song", album, artists=("Some Singer",))

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls.get("release_groups_under_other_credits", 0) == 0
    assert lookup.calls.get("artist_relations", 0) == 0


# ------------------------------------------- #14 review: the song must be on the record


def test_a_joined_artists_record_without_the_song_is_not_taken() -> None:
    """A title alone is weak evidence under a different credit: the liked song must be on it."""
    intent, lookup, _ = _try_world()
    lookup.tracklists["rg-try"] = ["Who Did You Think I Was", "Vultures"]

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert "no tracklist there lists 'Gravity', so none is chosen" in result.detail


def test_a_related_acts_generic_title_is_not_this_record() -> None:
    """A band the artist is in has its own "Live": same title, related act, different record."""
    live = rg("rg-live", "Live", artist_mbid="mb-band", artist_name="Name Band", secondary=[SecondaryType.LIVE])
    lookup = FakeLookup(
        relations={"mb-band": [relation("mb-name", "Name")]}, tracklists={"rg-live": ["Band Song", "Other Song"]}
    ).add(live)
    intent = track_intent("Solo Song", spotify_album("Live", artists=("Name",)), artists=("Name",))
    assert _related(intent, lookup).status == ResolutionStatus.UNMAPPED


def test_a_release_with_no_tracklist_is_no_evidence() -> None:
    intent, lookup, _ = _try_world()
    del lookup.tracklists["rg-try"]
    assert _related(intent, lookup).status == ResolutionStatus.UNMAPPED


@pytest.mark.parametrize(
    ("spotify_name", "taken"),
    [
        ("Gravity", True),
        ("Gravity - Live", True),
        ("Gravity (Live)", True),
        ("Gravity [Live]", True),
        ("Gravity - Live In Concert", True),
        # Pinned, not endorsed: `normalize_title` stops at a second " - ", so a long venue suffix
        # does not fold. The title fallback has the same limit; a dry run shows it if it bites.
        ("Gravity - Live at the Sears Centre, Hoffman Estates, IL - November 2005", False),
    ],
)
def test_the_song_is_folded_as_the_title_fallback_folds_it(spotify_name: str, taken: bool) -> None:
    intent, lookup, try_live = _try_world()
    intent = replace(intent, name=spotify_name)
    assert (_related(intent, lookup).release_group == try_live) is taken


def test_the_earliest_release_holding_the_song_is_taken_not_the_earliest_by_title() -> None:
    early = rg("rg-early", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2005-01-01")
    later = rg("rg-later", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2006-01-01")
    lookup = FakeLookup(
        relations={"mb-trio": [relation("mb-mayer", "John Mayer")]},
        tracklists={"rg-early": ["Vultures"], "rg-later": ["Gravity"]},
    ).add(early, later)
    intent = track_intent("Gravity", spotify_album("Try!", artists=("John Mayer",)), artists=("John Mayer",))
    assert _related(intent, lookup).release_group == later


def test_a_refused_release_of_the_joined_artist_gives_way_to_an_allowed_one() -> None:
    """Both hold the song; the earlier is on the deny list, so the later one is taken."""
    denied = rg("rg-denied", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2005-01-01")
    allowed = rg("rg-allowed", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2006-01-01")
    lookup = FakeLookup(
        relations={"mb-trio": [relation("mb-mayer", "John Mayer")]},
        tracklists={"rg-denied": ["Gravity"], "rg-allowed": ["Gravity"]},
    ).add(denied, allowed)
    intent = track_intent("Gravity", spotify_album("Try!", artists=("John Mayer",)), artists=("John Mayer",))

    result = _related(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-denied"})))

    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == allowed


# ------------------------------------------- #14 review: doubt chooses nothing


def _two_joinable(second_relations) -> FakeLookup:
    trio = rg("rg-a", "Shared Title", artist_mbid="mb-trio", artist_name="Name Trio", released="2001-01-01")
    band = rg("rg-b", "Shared Title", artist_mbid="mb-band", artist_name="Name Band", released="2002-01-01")
    return FakeLookup(
        relations={"mb-trio": [relation("mb-name", "Name")], "mb-band": second_relations},
        tracklists={"rg-a": ["Song"], "rg-b": ["Song"]},
    ).add(trio, band)


def test_an_unreadable_relationship_lookup_is_doubt_not_no() -> None:
    """Two joinable artists, the second's relationships cannot be read: it might be the second
    joined artist, so the first is not taken as if it were the only one."""
    lookup = _two_joinable(None)
    intent = track_intent("Song", spotify_album("Shared Title", artists=("Name",)), artists=("Name",))

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert "the relationships of 'Name Band' (mb-band) could not be read, so none is chosen" in result.detail


def test_with_the_second_artist_readable_and_unrelated_the_first_is_taken() -> None:
    """The control for the test above: the same world, with the second lookup answered."""
    lookup = _two_joinable([relation("mb-other", "Someone Else")])
    intent = track_intent("Song", spotify_album("Shared Title", artists=("Name",)), artists=("Name",))
    assert _related(intent, lookup).release_group == lookup.release_groups["rg-a"]


def test_one_artist_joined_to_two_different_artists_of_spotifys_name_is_doubt() -> None:
    """There is more than one John Mayer in MusicBrainz. If the Trio were related to two of them,
    which one Spotify means is exactly what a name cannot say."""
    intent, lookup, _ = _try_world()
    lookup.relations["mb-trio"] = [relation("mb-mayer", "John Mayer"), relation("mb-mayer-2", "John Mayer")]

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert "is related to 2 artists of that name" in result.detail


def test_two_relationships_to_the_same_artist_are_one_join() -> None:
    intent, lookup, try_live = _try_world()
    lookup.relations["mb-trio"] = [
        relation("mb-mayer", "John Mayer"),
        relation("mb-mayer", "John Mayer", "collaboration"),
    ]
    assert _related(intent, lookup).release_group == try_live


def test_an_isrc_naming_another_artist_of_spotifys_name_refuses_the_join() -> None:
    """The ISRC files the recording under a *different* John Mayer: the relationship is about the
    wrong one, however well the title matches."""
    intent, lookup, _ = _try_world()
    other = rg("rg-other", "Some Other Record", artist_mbid="mb-other-mayer", artist_name="John Mayer")
    lookup.add(other)
    lookup.isrcs["ZZ0000000001"] = ["rg-other"]
    lookup.artist_searches["John Mayer"] = None  # the stand-in's second tier finds no artist

    result = _related(intent, lookup)

    assert result.status == ResolutionStatus.UNMAPPED
    assert "a different artist of that name, so none is chosen" in result.detail


def test_an_isrc_on_the_related_artists_own_releases_does_not_refuse() -> None:
    intent, lookup, try_live = _try_world()
    own = rg("rg-continuum", "Continuum", artist_mbid="mb-mayer", artist_name="John Mayer")
    lookup.add(own)
    lookup.isrcs["ZZ0000000001"] = ["rg-continuum"]
    lookup.artist_searches["John Mayer"] = None

    assert _related(intent, lookup).release_group == try_live


# ------------------------------------------- #14 review: the rule never runs where it must not


def _joinable_trio(lookup: FakeLookup) -> FakeLookup:
    """Add a joinable Trio *Try!* holding "Gravity" to a world, as bait the rule must not take."""
    trio = rg("rg-try", "Try!", artist_mbid="mb-trio", artist_name="John Mayer Trio", released="2005-11-22")
    lookup.relations["mb-trio"] = [relation("mb-mayer", "John Mayer")]
    lookup.tracklists["rg-try"] = ["Gravity"]
    return lookup.add(trio)


def test_an_opted_out_release_spotify_named_is_reported_not_replaced_by_a_joined_one() -> None:
    own = rg("rg-own", "Try!", artist_mbid="mb-mayer", artist_name="John Mayer")
    lookup = _joinable_trio(FakeLookup().add(own))
    intent = track_intent("Gravity", spotify_album("Try!", artists=("John Mayer",)), artists=("John Mayer",))

    result = _related(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-own"})))

    assert result.step == EXCLUDED_DENIED_STEP
    assert result.source_release_group == own
    assert lookup.calls.get("artist_relations", 0) == 0


def test_a_contested_name_search_stays_ambiguous_with_a_joined_credit_on_offer() -> None:
    """Two same-named John Mayers, the ISRC naming the one whose title failed: ambiguous (#32),
    and the relationship rule is not a way round it."""
    survivor = rg("rg-survivor", "Try!", artist_mbid="mb-mayer", artist_name="John Mayer")
    rival = rg("rg-rival", "Try! (Deluxe 2020)", artist_mbid="mb-mayer-2", artist_name="John Mayer")
    rivals_other = rg("rg-rival-2", "Elsewhere", artist_mbid="mb-mayer-2", artist_name="John Mayer")
    lookup = _joinable_trio(
        FakeLookup(
            candidate_searches={("John Mayer", "Try!"): ["rg-survivor", "rg-rival"]},
            isrcs={"ZZ0000000009": ["rg-rival-2"]},
            artist_searches={"John Mayer": None},
        ).add(survivor, rival, rivals_other)
    )
    intent = track_intent(
        "Gravity", spotify_album("Try!", artists=("John Mayer",)), isrc="ZZ0000000009", artists=("John Mayer",)
    )

    result = _related(intent, lookup)

    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert lookup.calls.get("artist_relations", 0) == 0


def test_an_isrc_stand_in_whose_every_release_is_refused_reports_the_refusal() -> None:
    comp = rg(
        "rg-comp", "Best Of", artist_mbid="mb-mayer", artist_name="John Mayer", secondary=[SecondaryType.COMPILATION]
    )
    lookup = _joinable_trio(FakeLookup(isrcs={"ZZ0000000009": ["rg-comp"]}).add(comp))
    intent = track_intent(
        "Gravity", spotify_album("Try!", artists=("John Mayer",)), isrc="ZZ0000000009", artists=("John Mayer",)
    )

    result = _related(intent, lookup, rules=NO_COMPILATIONS)

    assert result.step == EXCLUDED_COMPILATION_STEP
    assert result.source_release_group == comp
    assert lookup.calls.get("artist_relations", 0) == 0


# ------------------------------------------- issue #89: exact titles first, and remix-only songs


def _cruisr_all_over() -> tuple:
    """CRUISR's "All Over": the plain EP is the release Spotify named, and an
    earlier remix single by the same artist matches it too once "(Bear//Face Remix)" is read as a
    qualifier - which `normalize_title` already does, so the earliest date used to decide."""
    ep = rg(
        "f7a134c9-ep",
        "All Over",
        artist_mbid="mb-cruisr",
        artist_name="CRUISR",
        primary=PrimaryType.EP,
        released="2014-09-23",
    )
    remix = rg(
        "9857e732-remix",
        "All Over (Bear//Face Remix)",
        artist_mbid="mb-cruisr",
        artist_name="CRUISR",
        primary=PrimaryType.SINGLE,
        secondary=[SecondaryType.REMIX],
        released="2014-01-01",
    )
    other_remix = rg(
        "rg-other-remix",
        "All Over (Remixes)",
        artist_mbid="mb-cruisr",
        artist_name="CRUISR",
        primary=PrimaryType.SINGLE,
        secondary=[SecondaryType.REMIX],
        released="2014-03-01",
    )
    lookup = FakeLookup(
        candidate_searches={("CRUISR", "All Over"): ["9857e732-remix", "f7a134c9-ep"]},
        isrcs={"USVR91425001": ["9857e732-remix", "rg-other-remix"]},
    ).add(ep, remix, other_remix)
    intent = track_intent(
        "All Over", spotify_album("All Over", artists=("CRUISR",)), isrc="USVR91425001", artists=("CRUISR",)
    )
    return intent, lookup, ep


@pytest.mark.parametrize("rules", [NO_EXCLUSIONS, NO_REMIXES, KEEP_NO_REMIX_ONLY])
def test_an_exact_title_beats_an_earlier_remix_that_only_matches_after_stripping(rules: ExclusionRules) -> None:
    intent, lookup, ep = _cruisr_all_over()

    result = resolve(intent, lookup, rules=rules)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == ep


def test_the_exact_title_wins_under_the_smallest_scope_too() -> None:
    intent, lookup, ep = _cruisr_all_over()
    assert resolve_smallest(intent, lookup).release_group == ep
    assert resolve_smallest(intent, lookup, rules=NO_REMIXES).release_group == ep


def test_an_earlier_live_release_does_not_beat_the_plain_title() -> None:
    """General, not remix-specific: an earlier "X (Live)" (untyped, so only its title says so) must
    not beat a plain "X" when Spotify named the plain one."""
    live = rg("rg-live", "Hometown (Live)", released="2001-01-01")
    plain = rg("rg-plain", "Hometown", released="2005-01-01")
    lookup = FakeLookup(candidate_searches={("Test Artist", "Hometown"): ["rg-live", "rg-plain"]}).add(live, plain)

    result = resolve(track_intent("Song", spotify_album("Hometown")), lookup)

    assert result.release_group == plain


def test_a_decorated_spotify_title_still_prefers_its_own_decoration() -> None:
    """The exact tier compares against what Spotify printed: Spotify's "X (Live)" is MusicBrainz's
    "X (Live)", even when a plain "X" is earlier."""
    live = rg("rg-live", "Hometown (Live)", released="2005-01-01")
    plain = rg("rg-plain", "Hometown", released="2001-01-01")
    lookup = FakeLookup(candidate_searches={("Test Artist", "Hometown (Live)"): ["rg-plain", "rg-live"]}).add(
        live, plain
    )

    result = resolve(track_intent("Song", spotify_album("Hometown (Live)")), lookup)

    assert result.release_group == live


def test_stripping_stays_the_fallback_when_no_title_matches_exactly() -> None:
    """Spotify's "Kangaroo (Remastered)" still maps to MusicBrainz's "Kangaroo EP": no exact tier."""
    ep = rg("rg-ep", "Kangaroo EP", primary=PrimaryType.EP, released="2010-01-01")
    lookup = FakeLookup(candidate_searches={("Test Artist", "Kangaroo (Remastered)"): ["rg-ep"]}).add(ep)

    result = resolve(track_intent("Song", spotify_album("Kangaroo (Remastered)")), lookup)

    assert result.release_group == ep


def test_a_saved_album_prefers_the_exact_title_between_two_same_typed_eps() -> None:
    """The saved-album order had the same flaw, narrower: two studio EPs, one "(Remixes)", tie on
    type and studio-ness, so the earliest date took the remix EP."""
    remixes = rg("rg-remixes", "The Feeling (Remixes)", primary=PrimaryType.EP, released="2012-09-11")
    plain = rg("rg-plain", "The Feeling", primary=PrimaryType.EP, released="2012-10-01")
    lookup = FakeLookup(candidate_searches={("Test Artist", "The Feeling"): ["rg-remixes", "rg-plain"]}).add(
        remixes, plain
    )

    result = resolve_album(album_intent(spotify_album("The Feeling", released=None)), lookup)

    assert result.release_group == plain


def test_a_saved_album_still_prefers_the_ep_type_over_an_exact_titled_single() -> None:
    """Exact-first only breaks a tie the type and studio-ness left; it never outranks them."""
    single = rg("rg-single", "Kangaroo", primary=PrimaryType.SINGLE, released="2009-01-01")
    ep = rg("rg-ep", "Kangaroo EP", primary=PrimaryType.EP, released="2010-01-01")
    lookup = FakeLookup(candidate_searches={("Test Artist", "Kangaroo"): ["rg-single", "rg-ep"]}).add(single, ep)

    result = resolve_album(album_intent(spotify_album("Kangaroo", released=None)), lookup)

    assert result.release_group == ep


def _the_knocks_learn_to_fly() -> tuple:
    """The Knocks' "Learn To Fly" on Spotify's "The Feeling": the name search
    finds only a remix release, and the ISRC's only release group is the "(Remixes)" EP, which
    carries the original recording and no secondary types at all."""
    fatrat = rg(
        "efc9c725-fatrat",
        "The Feeling (TheFatRat Remix)",
        artist_mbid="mb-knocks",
        artist_name="The Knocks",
        primary=PrimaryType.OTHER,
        released="2012-01-01",
    )
    remixes = rg(
        "d83c4c9b-remixes",
        "The Feeling (Remixes)",
        artist_mbid="mb-knocks",
        artist_name="The Knocks",
        primary=PrimaryType.EP,
        released="2012-09-11",
    )
    lookup = FakeLookup(
        candidate_searches={("The Knocks", "The Feeling"): ["efc9c725-fatrat"]},
        isrcs={"USUM71205871": ["d83c4c9b-remixes"]},
    ).add(fatrat, remixes)
    intent = track_intent(
        "Learn To Fly",
        spotify_album("The Feeling", artists=("The Knocks",)),
        isrc="USUM71205871",
        artists=("The Knocks",),
    )
    return intent, lookup, remixes


def test_a_song_whose_only_release_is_a_remix_is_kept_by_default() -> None:
    intent, lookup, remixes = _the_knocks_learn_to_fly()

    result = resolve(intent, lookup, rules=NO_REMIXES)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == REMIX_ONLY_STEP
    assert not is_excluded(result)
    assert result.release_group == remixes, "the EP the ISRC proves, over the remix the name search found"
    assert "only release" in result.detail
    assert "keep_remix_only_tracks" in result.detail


def test_switching_keep_remix_only_off_reports_the_refusal_as_before() -> None:
    intent, lookup, _remixes = _the_knocks_learn_to_fly()

    result = resolve(intent, lookup, rules=KEEP_NO_REMIX_ONLY)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_REMIX_STEP


def test_keep_remix_only_changes_nothing_while_remixes_are_allowed() -> None:
    intent, lookup, remixes = _the_knocks_learn_to_fly()

    result = resolve(intent, lookup)

    assert result.step == "track:isrc->album"
    assert result.release_group == remixes


def test_a_remix_only_song_prefers_an_ep_over_a_remix_single() -> None:
    """Album/EP over Single, then the earliest date - among releases the ISRC proves hold it."""
    single = rg("rg-single", "Song (Club Remix)", primary=PrimaryType.SINGLE, released="2010-01-01")
    ep = rg("rg-ep", "Song (Remixes)", primary=PrimaryType.EP, released="2011-01-01")
    lookup = FakeLookup(searches={("Test Artist", "Song"): None}, isrcs={"I1": ["rg-single", "rg-ep"]}).add(single, ep)
    intent = track_intent("Song", spotify_album("Song"), isrc="I1")

    result = resolve(intent, lookup, rules=NO_REMIXES)

    assert result.step == REMIX_ONLY_STEP
    assert result.release_group == ep


@pytest.mark.parametrize("keep", [True, False])
def test_a_real_album_still_beats_a_remix_ep_whatever_keep_remix_only_says(keep: bool) -> None:
    """Grease (#15): the remix EP is refused and the album is there, so nothing is remix-only."""
    intent, lookup, album, _remix_ep = _remix_ep_holds_the_original()
    rules = ExclusionRules(allow_remix_releases=False, keep_remix_only_tracks=keep)

    assert resolve(intent, lookup, rules=rules).release_group == album
    assert resolve_smallest(intent, lookup, rules=rules).release_group == album


def test_keep_remix_only_never_overrides_a_compilation_refusal() -> None:
    """A remix compilation is refused twice over; only a release refused *just* for being a remix
    is kept, so the answer stays the refusal."""
    comp = rg("rg-comp", "Club Hits (Remixes)", secondary=[SecondaryType.COMPILATION])
    lookup = FakeLookup(searches={("Test Artist", "Club Hits"): None}, isrcs={"I1": ["rg-comp"]}).add(comp)
    intent = track_intent("Song", spotify_album("Club Hits"), isrc="I1")
    rules = ExclusionRules(allow_remix_releases=False, allow_compilation_fallback=False)

    result = resolve(intent, lookup, rules=rules)

    assert result.status == ResolutionStatus.UNMAPPED
    assert is_excluded(result)


def test_keep_remix_only_never_overrides_the_deny_list() -> None:
    intent, lookup, _remixes = _the_knocks_learn_to_fly()
    rules = ExclusionRules(allow_remix_releases=False, deny_releases=frozenset({"d83c4c9b-remixes"}))

    result = resolve(intent, lookup, rules=rules)

    assert result.status == ResolutionStatus.UNMAPPED
    assert is_excluded(result)


def test_the_rules_token_moves_when_keep_remix_only_flips() -> None:
    assert ExclusionRules().token == "", "the defaults still re-resolve nothing"
    assert NO_REMIXES.token == "c1r0", "keeping is the default, so an existing install's token is unchanged"
    assert KEEP_NO_REMIX_ONLY.token == "c1r0k0"
    assert ExclusionRules(keep_remix_only_tracks=False).token == "c1r1k0"


def test_a_remix_named_by_spotify_is_not_remix_only_when_only_the_va_soundtrack_shares_its_isrc() -> None:
    """Grease again, from the name-search side: Spotify named the remix EP, and the ISRC's only
    release is the Various Artists soundtrack. That soundtrack is a home that is not a remix."""
    remix_ep = rg("rg-remix", "Grease (The Remix EP)", primary=PrimaryType.EP)
    soundtrack = rg("rg-va", "Grease", artist_mbid=VARIOUS_ARTISTS_MBID, artist_name="Various Artists")
    lookup = FakeLookup(searches={("Test Artist", "Grease (The Remix EP)"): "rg-remix"}, isrcs={"I1": ["rg-va"]}).add(
        remix_ep, soundtrack
    )
    intent = track_intent("Song", spotify_album("Grease (The Remix EP)"), isrc="I1")

    result = resolve(intent, lookup, rules=NO_REMIXES)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_REMIX_STEP


@pytest.mark.parametrize("keep", [True, False])
def test_a_refused_remix_single_still_finds_the_artists_album_that_lists_the_song(keep: bool) -> None:
    """With remixes allowed the Singles rule finds the album by tracklist; the song therefore has a
    home that is not a remix, and keeping the remix single would monitor what the setting refuses.
    Until issue #96 the refusal was the answer; now the album search runs from the refused single
    too, so the song lands on the album whatever `keep_remix_only_tracks` says."""
    single = rg("rg-single", "Song (Club Remix)", primary=PrimaryType.SINGLE, released="2019-01-01")
    album = rg("rg-album", "The Album", released="2020-01-01")
    lookup = FakeLookup(
        searches={("Test Artist", "Song (Club Remix)"): "rg-single"},
        catalogues={"artist-1": ["rg-single", "rg-album"]},
        tracklists={"rg-album": ["Song"]},
    ).add(single, album)
    intent = track_intent("Song", spotify_album("Song (Club Remix)"))
    rules = ExclusionRules(allow_remix_releases=False, keep_remix_only_tracks=keep)

    assert resolve(intent, lookup).release_group == album
    result = resolve(intent, lookup, rules=rules)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:title->album"
    assert result.release_group == album
    assert result.source_release_group == single, "the release Spotify named, refused, is what it searched from"
    assert "allow_remix_releases" in result.detail, "and the detail says why that release is not monitored"


# ------------------------------------------- issue #96: the album search runs from a refused release


def _box_set_whose_artist_album_lists_the_song() -> tuple:
    """Spotify files the song on a box set, the box set's copy has its own ISRC (a remaster), and
    that ISRC is filed on nothing else - but the artist's studio album lists the title."""
    box = rg("rg-box", "The Complete Masters", secondary=[SecondaryType.COMPILATION], released="1994-01-01")
    album = rg("rg-album", "The Album", released="1970-01-01")
    lookup = FakeLookup(
        searches={("Test Artist", "The Complete Masters"): "rg-box"},
        isrcs={"I1": ["rg-box"]},
        catalogues={"artist-1": ["rg-box", "rg-album"]},
        tracklists={"rg-album": ["Song"]},
    ).add(box, album)
    intent = track_intent("Song", spotify_album("The Complete Masters"), isrc="I1")
    return intent, lookup, box, album


@pytest.mark.parametrize("smallest", [False, True])
def test_a_refused_box_set_still_finds_the_album_by_tracklist(smallest: bool) -> None:
    intent, lookup, box, album = _box_set_whose_artist_album_lists_the_song()

    result = (resolve_smallest if smallest else resolve)(intent, lookup, rules=NO_COMPILATIONS)

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:title->album"
    assert result.release_group == album
    assert result.source_release_group == box
    assert "allow_compilation_fallback" in result.detail


def test_a_denied_single_still_finds_the_album_by_tracklist() -> None:
    """The deny list is "not this one, the next one" - from the release Spotify named, too."""
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2019-01-01")
    album = rg("rg-album", "The Album", released="2020-01-01")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        catalogues={"artist-1": ["rg-single", "rg-album"]},
        tracklists={"rg-album": ["Song"]},
    ).add(single, album)
    intent = track_intent("Song", spotify_album("Song", upc="111"))

    result = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-single"})))

    assert result.step == "track:title->album"
    assert result.release_group == album
    assert "deny_releases" in result.detail


def test_a_refused_studio_album_is_never_the_answer_of_its_own_search() -> None:
    """A denied studio album maps to itself (`track:album`); that is still the refusal."""
    album = rg("rg-album", "The Album", released="2020-01-01")
    lookup = FakeLookup(barcodes={"111": "rg-album"}).add(album)
    intent = track_intent("Song", spotify_album("The Album", upc="111"))

    result = resolve(intent, lookup, rules=ExclusionRules(deny_releases=frozenset({"rg-album"})))

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_DENIED_STEP
    assert result.release_group is None


def test_an_album_search_that_finds_only_refused_releases_reports_the_refusal() -> None:
    """The album the tracklist names is a box set too, so nothing allowed holds the song."""
    intent, lookup, _box, album = _box_set_whose_artist_album_lists_the_song()
    other_box = replace(album, secondary_types=frozenset({SecondaryType.COMPILATION}))
    lookup.add(other_box)

    result = resolve(intent, lookup, rules=NO_COMPILATIONS)

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_COMPILATION_STEP


def test_a_remix_only_song_prefers_a_release_lidarr_can_hold() -> None:
    """No Lidarr metadata profile likearr sets up allows the `Remix` secondary type, so a
    Remix-typed EP would never reach the catalogue; an untyped remix single is kept instead."""
    typed_ep = rg("rg-typed", "Song (Remixes)", primary=PrimaryType.EP, secondary=[SecondaryType.REMIX])
    single = rg("rg-single", "Song (Club Remix)", primary=PrimaryType.SINGLE, released="2021-01-01")
    lookup = FakeLookup(searches={("Test Artist", "Song"): None}, isrcs={"I1": ["rg-typed", "rg-single"]}).add(
        typed_ep, single
    )
    intent = track_intent("Song", spotify_album("Song"), isrc="I1")

    result = resolve(intent, lookup, rules=NO_REMIXES)

    assert result.step == REMIX_ONLY_STEP
    assert result.release_group == single


def _eminem_encore() -> tuple:
    """Eminem's "Mockingbird", from a replay of #89: Spotify files it on the album
    "Encore", and the name search returns - through the adapter's looser gate - the Album, an
    earlier same-titled Single that does not carry the song, and an even earlier "Encore (Bonus
    CD)" EP that fails the resolver's own title check."""
    album = rg("38a1cae8-album", "Encore", released="2004-11-12", artist_mbid="mb-eminem", artist_name="Eminem")
    single = rg(
        "3aa049e2-single",
        "Encore",
        primary=PrimaryType.SINGLE,
        released="2004-11-09",
        artist_mbid="mb-eminem",
        artist_name="Eminem",
    )
    bonus = rg(
        "6d126770-bonus",
        "Encore (Bonus CD)",
        primary=PrimaryType.EP,
        released="2004-11-02",
        artist_mbid="mb-eminem",
        artist_name="Eminem",
    )
    mockingbird = rg(
        "df4b48f5-mock",
        "Mockingbird",
        primary=PrimaryType.SINGLE,
        released="2005-04-25",
        artist_mbid="mb-eminem",
        artist_name="Eminem",
    )
    lookup = FakeLookup(
        candidate_searches={("Eminem", "Encore"): ["6d126770-bonus", "3aa049e2-single", "38a1cae8-album"]},
        isrcs={"USIR10400001": ["38a1cae8-album", "df4b48f5-mock"]},
    ).add(album, single, bonus, mockingbird)
    intent = track_intent(
        "Mockingbird", spotify_album("Encore", artists=("Eminem",)), isrc="USIR10400001", artists=("Eminem",)
    )
    return intent, lookup, album, mockingbird


def test_an_equally_titled_single_without_the_song_does_not_become_the_release_spotify_named() -> None:
    """The title ties the Album and the Single, so the ISRC decides before the date does."""
    intent, lookup, album, mockingbird = _eminem_encore()

    assert resolve(intent, lookup).release_group == album
    smallest = resolve_smallest(intent, lookup)
    assert smallest.step == "track:smallest:single"
    assert smallest.release_group == mockingbird
    assert smallest.source_release_group == album


def test_the_isrc_is_not_asked_when_the_title_alone_decides() -> None:
    """Only a tie in a literal title tier pays for the ISRC search in the album scope."""
    intent, lookup, ep = _cruisr_all_over()
    lookup.isrcs.clear()

    assert resolve(replace(intent, isrc="NOPE"), lookup).release_group == ep
    assert lookup.calls.get("release_groups_for_isrc", 0) == 0


def test_a_title_track_single_holds_its_song_even_where_the_isrc_is_not_filed_on_it() -> None:
    """Spotify files "Nick Of Time" on "Nick of Time". MusicBrainz has an earlier same-titled Single
    and the Album, and files the ISRC under the Album only. The single named after the song
    carries it, so the date decides as before and the `smallest` scope keeps the single."""
    single = rg("rg-single", "Nick of Time", primary=PrimaryType.SINGLE, released="1989-01-01")
    album = rg("rg-album", "Nick of Time", released="1989-03-21")
    lookup = FakeLookup(
        candidate_searches={("Test Artist", "Nick of Time"): ["rg-single", "rg-album"]},
        isrcs={"I1": ["rg-album"]},
    ).add(single, album)
    intent = track_intent("Nick Of Time", spotify_album("Nick of Time"), isrc="I1")

    result = resolve_smallest(intent, lookup)

    assert result.source_release_group == single
    assert result.release_group == single
