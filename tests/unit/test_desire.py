"""Tests for building the desired state out of resolutions."""

from __future__ import annotations

from likearr.core.desire import CATALOGUE_TOO_LARGE_STEP, build_desired
from likearr.core.resolver import ResolveResult, resolve_all
from likearr.models import (
    ArtistResolution,
    PrimaryType,
    Profile,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SecondaryType,
)
from likearr.ports import CatalogueTooLarge
from tests.unit.fakes import (
    NOW,
    FakeLookup,
    album_intent,
    artist_intent,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)

FALLBACK_DAYS = 180


def _desire(snap, lookup: FakeLookup, *, albums_only: set[str] | None = None, now=NOW):
    result = resolve_all(snap, lookup, now=now, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS)
    return build_desired(snap, result, lookup, albums_only_artists=albums_only or set()), result


# --------------------------------------------------------------------------- followed artists


def _catalogue_lookup() -> FakeLookup:
    album = rg("rg-album", "First Record", artist_name="Radiohead")
    ep = rg("rg-ep", "Small Record", primary=PrimaryType.EP, artist_name="Radiohead")
    single = rg("rg-single", "A Song", primary=PrimaryType.SINGLE, artist_name="Radiohead")
    live = rg("rg-live", "Live Record", secondary=[SecondaryType.LIVE], artist_name="Radiohead")
    return FakeLookup(
        artists={"artist-1": "Radiohead"},
        catalogues={"artist-1": ["rg-album", "rg-ep", "rg-single", "rg-live"]},
    ).add(album, ep, single, live)


def test_a_followed_artist_wants_studio_albums_and_eps_only() -> None:
    lookup = _catalogue_lookup()
    snap = snapshot(artists=[artist_intent("Radiohead")])
    desired, _ = _desire(snap, lookup)
    assert {k.rg_mbid for k in desired.releases} == {"rg-album", "rg-ep"}
    assert desired.followed_artists == {"artist-1"}
    assert desired.followed_counts == {"artist-1": 2}
    assert desired.profile_needs["artist-1"] is Profile.LEAN


def test_the_albums_only_tag_drops_eps() -> None:
    lookup = _catalogue_lookup()
    snap = snapshot(artists=[artist_intent("Radiohead")])
    desired, _ = _desire(snap, lookup, albums_only={"artist-1"})
    assert {k.rg_mbid for k in desired.releases} == {"rg-album"}
    assert desired.followed_counts == {"artist-1": 1}
    assert desired.catalogue_counts == {"artist-1": 2}, "the tag is the user's filter, not a smaller catalogue"
    step = next(iter(desired.releases.values())).steps
    assert next(iter(step.values())) == "followed:catalogue:albums-only"


def test_a_denied_release_leaves_a_followed_catalogue() -> None:
    """#153, option B: "Not this one" on a followed artist's album removes it from the catalogue."""
    lookup = _catalogue_lookup()
    snap = snapshot(artists=[artist_intent("Radiohead")])
    result = resolve_all(snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS)
    desired = build_desired(snap, result, lookup, albums_only_artists=set(), deny_releases=frozenset({"rg-album"}))
    assert {k.rg_mbid for k in desired.releases} == {"rg-ep"}
    assert desired.followed_counts == {"artist-1": 1}
    assert desired.catalogue_counts == {"artist-1": 2}, "a deny is the user's filter, not a smaller catalogue"


def test_a_saved_album_still_overrides_a_denied_release() -> None:
    """Saving the album on Spotify is the most explicit wish there is; the deny list never refuses it,
    even when the same release is denied out of the artist's followed catalogue."""
    lookup = _catalogue_lookup()
    lookup.barcodes["111"] = "rg-album"
    saved = album_intent(spotify_album("First Record", upc="111", artists=("Radiohead",)))
    snap = snapshot(artists=[artist_intent("Radiohead")], albums=[saved])
    result = resolve_all(snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS)
    desired = build_desired(snap, result, lookup, albums_only_artists=set(), deny_releases=frozenset({"rg-album"}))
    album = desired.releases[ReleaseKey("artist-1", "rg-album")]
    assert {r.kind for r in album.reasons} == {ReasonKind.SAVED}
    assert desired.followed_counts == {"artist-1": 1}


def test_a_featured_appearance_is_not_part_of_a_followed_catalogue() -> None:
    """Otherwise following one artist quietly adds their collaborators to Lidarr."""
    own = rg("rg-own", "Own Record", artist_name="Radiohead")
    guest = rg("rg-guest", "Someone Else's Record", artist_mbid="artist-2", artist_name="Someone Else")
    lookup = FakeLookup(
        artists={"artist-1": "Radiohead"},
        catalogues={"artist-1": ["rg-own", "rg-guest"]},
    ).add(own, guest)
    desired, _ = _desire(snapshot(artists=[artist_intent("Radiohead")]), lookup)
    assert {k.rg_mbid for k in desired.releases} == {"rg-own"}
    assert "artist-2" not in desired.artists


def test_an_unresolvable_followed_artist_lands_in_unmapped() -> None:
    desired, _ = _desire(snapshot(artists=[artist_intent("Nobody At All")]), FakeLookup())
    assert not desired.releases
    assert len(desired.unmapped) == 1
    assert desired.unmapped[0].step == "artist:search"


def test_a_catalogue_lookup_failure_reports_the_artist_instead_of_dropping_it() -> None:
    """A silently dropped artist looks exactly like an unfollowed one, and costs a catalogue."""
    lookup = _catalogue_lookup()
    lookup.fail = {"artist_release_groups"}
    desired, _ = _desire(snapshot(artists=[artist_intent("Radiohead")]), lookup)
    assert not desired.releases
    assert "artist-1" in desired.artists, "the artist must still be known so nothing is unmonitored"
    reported = [u for u in desired.unmapped if u.step == "error:metadata"]
    assert len(reported) == 1
    assert isinstance(reported[0], ArtistResolution)
    assert reported[0].artist_mbid == "artist-1"


def test_a_catalogue_too_large_to_browse_is_reported_under_its_own_step() -> None:
    """Distinguished from an outage, because it is permanent and it recurs on every run.

    `CatalogueTooLarge` subclasses `MetadataError`, so it would otherwise land in the generic
    `error:metadata` bucket and be indistinguishable from MusicBrainz being down. It gets its own
    step so the health layer can route it by step alone, exactly as it already does for catalogue
    gaps, keeping that layer pure.
    """
    lookup = _catalogue_lookup()
    lookup.fail = {"artist_release_groups"}
    lookup.fail_error = CatalogueTooLarge("artist-1 has more than 3000 release groups")
    desired, _ = _desire(snapshot(artists=[artist_intent("Radiohead")]), lookup)

    assert not desired.releases
    assert "artist-1" in desired.artists, "the artist must still be known so nothing is unmonitored"
    reported = [u for u in desired.unmapped if u.step == CATALOGUE_TOO_LARGE_STEP]
    assert len(reported) == 1
    assert isinstance(reported[0], ArtistResolution)
    assert reported[0].artist_mbid == "artist-1"
    assert not [u for u in desired.unmapped if u.step == "error:metadata"]


# --------------------------------------------------------------------------- albums and tracks


def test_saved_albums_and_liked_tracks_merge_their_reasons_on_one_release() -> None:
    album = rg("rg-1", "OK Computer", artist_name="Radiohead")
    lookup = FakeLookup(barcodes={"111": "rg-1"}).add(album)
    snap = snapshot(
        albums=[album_intent(spotify_album("OK Computer", spotify_id="sp-al", upc="111", artists=("Radiohead",)))],
        tracks=[
            track_intent("Karma Police", spotify_album("OK Computer", upc="111"), spotify_id="t1"),
            track_intent("No Surprises", spotify_album("OK Computer", upc="111"), spotify_id="t2"),
        ],
    )
    desired, _ = _desire(snap, lookup)
    assert len(desired.releases) == 1
    release = desired.releases[ReleaseKey("artist-1", "rg-1")]
    assert {r.kind for r in release.reasons} == {ReasonKind.SAVED, ReasonKind.LIKED}
    assert len(release.reasons) == 3
    assert len(release.steps) == 3


def test_pending_resolutions_go_to_pending_and_monitor_nothing() -> None:
    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(barcodes={"111": "rg-single"}).add(single)
    snap = snapshot(tracks=[track_intent("Song", spotify_album("Song", upc="111"))])
    desired, _ = _desire(snap, lookup)
    assert not desired.releases
    assert len(desired.pending) == 1
    assert desired.pending[0].status == ResolutionStatus.PENDING_ALBUM


def test_unmapped_resolutions_go_to_unmapped_and_monitor_nothing() -> None:
    snap = snapshot(tracks=[track_intent("Song", spotify_album("Nowhere"))])
    desired, _ = _desire(snap, FakeLookup())
    assert not desired.releases
    assert len(desired.unmapped) == 1


def test_a_non_studio_release_forces_the_full_profile() -> None:
    comp = rg("rg-comp", "Hey Jude", secondary=[SecondaryType.COMPILATION], released="1970-02-26")
    lookup = FakeLookup(barcodes={"111": "rg-comp"}).add(comp)
    snap = snapshot(tracks=[track_intent("Hey Jude", spotify_album("Hey Jude", upc="111"))])
    desired, _ = _desire(snap, lookup)
    assert desired.profile_needs["artist-1"] is Profile.FULL


def test_a_studio_only_artist_stays_lean() -> None:
    album = rg("rg-1", "OK Computer")
    lookup = FakeLookup(barcodes={"111": "rg-1"}).add(album)
    snap = snapshot(tracks=[track_intent("Karma Police", spotify_album("OK Computer", upc="111"))])
    desired, _ = _desire(snap, lookup)
    assert desired.profile_needs["artist-1"] is Profile.LEAN


def test_every_artist_with_a_desired_release_is_listed() -> None:
    one = rg("rg-1", "Record One", artist_mbid="a1", artist_name="One")
    two = rg("rg-2", "Record Two", artist_mbid="a2", artist_name="Two")
    lookup = FakeLookup(barcodes={"111": "rg-1", "222": "rg-2"}).add(one, two)
    snap = snapshot(
        albums=[
            album_intent(spotify_album("Record One", spotify_id="s1", upc="111", artists=("One",))),
            album_intent(spotify_album("Record Two", spotify_id="s2", upc="222", artists=("Two",))),
        ]
    )
    desired, _ = _desire(snap, lookup)
    assert desired.artists == {"a1": "One", "a2": "Two"}


def test_a_cached_resolution_with_no_live_intent_contributes_nothing() -> None:
    """A stale cache entry must not resurrect a reason that has left the source."""
    album = rg("rg-1", "OK Computer")
    lookup = FakeLookup().add(album)
    empty = snapshot()
    orphan = Resolution(
        intent_key="liked:gone",
        status=ResolutionStatus.RESOLVED,
        release_group=album,
        step="track:album",
    )
    desired = build_desired(
        empty,
        ResolveResult(resolutions={"liked:gone": orphan}),
        lookup,
        albums_only_artists=set(),
    )
    assert not desired.releases


def test_build_desired_is_deterministic() -> None:
    lookup = _catalogue_lookup()
    snap = snapshot(artists=[artist_intent("Radiohead")])
    a, _ = _desire(snap, lookup)
    b, _ = _desire(snap, lookup)
    assert a.releases.keys() == b.releases.keys()
    assert a.followed_counts == b.followed_counts
    assert a.profile_needs == b.profile_needs
