"""Ports: the interfaces between the pure core and the outside world.

The core imports only this module and `models`. Adapters implement these Protocols.
Tests implement them with in-memory fakes. Nothing in here performs I/O itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from likearr.models import (
    ArtistCandidate,
    ArtistRelation,
    BarcodeMatch,
    HealthRecord,
    IsrcRecording,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    Profile,
    ReleaseGroup,
    SourceSnapshot,
    SpotifyAlbumRef,
    SpotifyArtistRef,
)


class SourceError(Exception):
    """Any failure reading a source. The run aborts with zero unmonitors."""


class SchemaError(SourceError):
    """A structural field the tool depends on is missing from a source response."""


class ScopeError(SourceError):
    """The stored token lacks a scope the command needs.

    Never recovered from automatically: widening scopes means a fresh consent screen, so the user
    re-runs `likearr auth --manual` themselves and the command says exactly that.
    """


class QuotaExceeded(SourceError):
    """Spotify answered 429 ``QUOTA_EXCEEDED``: the developer account's quota is spent.

    Never retried, because another call spends more of what is already gone. ``retry_after`` is
    Spotify's ``Retry-After`` in seconds, or None when it sent none. ``token_request`` is True when
    the token endpoint answered it, which a 401's refresh can reach from any Web API call.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, token_request: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.token_request = token_request


class SearchBudgetExceeded(SourceError):
    """The per-run Spotify `search` budget ran out. Matched work is cached; re-run to continue."""


class MetadataError(Exception):
    """MusicBrainz (or another metadata backend) failed for this lookup."""


class CatalogueTooLarge(MetadataError):
    """An artist's catalogue is longer than MusicBrainz is browsed for: huge, not unreachable."""


class LidarrError(Exception):
    """Lidarr returned an error or an unexpected shape."""


class LidarrMetadataError(LidarrError):
    """Lidarr's own metadata server failed (artist refresh / lookup); skip the artist this run."""


class LidarrArtistUnknown(LidarrMetadataError):
    """Lidarr's metadata server does not know this artist yet, so Lidarr refused to add it.

    Skipped for the run like any metadata failure and tried again next run, but not an outage:
    it can last weeks for an artist new to MusicBrainz, so it does not degrade the run."""


class LidarrArtistExists(LidarrError):
    """Lidarr refused an add because the artist is already there.

    Carries the artist Lidarr holds, so the caller can decide whose it is: one carrying likearr's
    tag is likearr's own add from a run that stopped before recording it; any other was added by
    someone else, and must not become likearr's."""

    def __init__(self, message: str, artist: LidarrArtist) -> None:
        super().__init__(message)
        self.artist = artist


class SourcePort(Protocol):
    """Reads intents from a source (Spotify in v1). Must be all-or-nothing."""

    def read(self) -> SourceSnapshot:
        """Read every configured source. Raise SourceError on ANY failure - never return a partial snapshot."""
        ...


class MetadataLookup(Protocol):
    """Catalogue queries the resolver needs. Implementations cache aggressively and rate-limit.

    Every method returns data or raises MetadataError. A "not found" is an empty result, not an error.
    """

    def release_groups_by_barcode(self, upc: str) -> Sequence[BarcodeMatch]:
        """Every distinct release group holding a release with this barcode, compared as a GTIN.

        Leading zeros carry no meaning in a barcode: Spotify pads its UPC to 13 or 14 digits where
        MusicBrainz usually stores the 12-digit UPC-A, so both sides are compared with them dropped.
        Empty is "not found". Ordered with release groups holding an Official release
        first; which of several is meant is the resolver's call, never the adapter's.
        """
        ...

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        """Every release group containing a recording with this ISRC."""
        ...

    def recordings_for_isrc(self, isrc: str) -> Sequence[IsrcRecording]:
        """The same answer as `release_groups_for_isrc`, per recording and with its title.

        One ISRC can be filed on two different songs; this says which release groups came from
        which recording, from the same cached search and at no extra request.
        """
        ...

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        """Name-based lookup, used when barcodes are missing. Must be conservative (return None on doubt).

        Two different artists who share the name and the title are doubt: ``None``.
        """
        ...

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """Every release group the name search matched exactly, for the resolver to choose from.

        Empty is "not found". Several can be one artist's releases sharing a title (an album and
        its lead single), which the resolver chooses between knowing what kind of Spotify intent
        asked; or different MusicBrainz artists sharing both the name and the title,
        between which only evidence about the track itself (its ISRC) may choose - never a date.
        Ordered by earliest first-release date, then MBID, only for determinism.
        """
        ...

    def search_artist(self, name: str) -> tuple[str, str] | None:
        """Name-based artist lookup → (mbid, name), conservative."""
        ...

    def search_artist_candidates(self, name: str) -> Sequence[tuple[str, str]]:
        """Every artist whose name is exactly `name`, as (mbid, name), best-scored first.

        `search_artist` returns the top one of these and never says there were others; a caller
        that must not guess between namesakes checks for exactly one. Empty is "not found".
        """
        ...

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        """All release groups credited to the artist (any type)."""
        ...

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        """Track titles of one representative release in the group (for the title fallback)."""
        ...


class ReleaseLinkLookup(Protocol):
    """What MusicBrainz knows that points *outside* MusicBrainz: Spotify links, and barcodes.

    Separate from `MetadataLookup` on purpose. The resolver's questions all run MusicBrainz-inward
    (barcode -> release group, ISRC -> release groups); these run outward, and only `promote-save`
    asks them. Adding them to `MetadataLookup` would have made every existing implementation,
    including the tests' fakes, incomplete.

    The two ``spotify_*`` methods are `promote-save`'s first and best mapping tier: an editor
    curated the link, so there is nothing to match and nothing to guess. Each returns ``None``
    rather than a guess whenever MusicBrainz names no link - or names several different ones.
    """

    def spotify_artist_id(self, artist_mbid: str) -> str | None:
        """The one Spotify artist id MusicBrainz links this artist to, or ``None``."""
        ...

    def spotify_album_id(self, rg_mbid: str) -> str | None:
        """The one Spotify album id MusicBrainz links this release group to, or ``None``."""
        ...

    def release_group_barcodes(self, rg_mbid: str) -> Sequence[str]:
        """Distinct barcodes of the group's releases, best pressing first. Empty when unknown."""
        ...


class ArtistLinks(Protocol):
    """Which MusicBrainz artist a Spotify artist *is*, from MusicBrainz's own URL relationship.

    The authoritative direction, and the reason it exists: resolving a followed artist by **name
    search** cannot tell two artists apart who share a name, and it can silently pick the wrong
    one. An editor linking a Spotify page to an artist cannot make that mistake.
    """

    def artists_for_spotify_artist(self, spotify_artist_id: str) -> Sequence[ArtistCandidate]:
        """Every MusicBrainz artist linked to that Spotify artist page. Empty when none is."""
        ...


class CreditRelations(Protocol):
    """What MusicBrainz knows that joins two differently-credited artists into one act.

    Separate from `MetadataLookup` for the reason `ReleaseLinkLookup` is: every existing lookup,
    the tests' fakes included, stays complete, and a caller that passes nothing gets the resolver
    exactly as it was. The resolver asks only after every other way of mapping a liked track's
    album has failed, and the identity rule - which relationship types count, and between whom -
    lives in `core.resolver`, not here.
    """

    def release_groups_under_other_credits(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """The release groups the name search for (`artist`, `title`) already found whose title
        matches but whose artist credit is *not* `artist` - the ones `search_release_group_candidates`
        refused on the credit alone. Read from what that search left behind, never a new request.
        Empty when there are none (or that search was never answered); ordered by date, then MBID."""
        ...

    def artist_relations(self, artist_mbid: str) -> Sequence[ArtistRelation] | None:
        """Every artist-artist relationship MusicBrainz records for this artist. Empty when none.

        ``None`` means they could not be read (the composite lookup's answer to a MusicBrainz
        failure), which is not the same as "none": the resolver then chooses nothing at all, since
        the unreadable artist might have been a second joined one."""
        ...


class ArtistDetails(Protocol):
    """The one MusicBrainz fact a name collision needs to be readable rather than cryptic."""

    def artist_disambiguation(self, artist_mbid: str) -> str:
        """MusicBrainz's one-line disambiguation, or ``""`` when it has none (or cannot say).

        "Germany DJ & producer" versus "Clyde Lawrence and Gracie Lawrence" is what tells a human
        that a `name-collision` is two different artists rather than a duplicate. Never raises:
        an absent disambiguation is normal, and a lookup failure must not cost the report.
        """
        ...


class SpotifyLibraryPort(Protocol):
    """Spotify's library from the *writing* side: search, the two listings, and the two writes.

    Deliberately not part of `SourcePort`. A source is read-only and all-or-nothing; this port is
    the only place in likearr that changes anything on Spotify, and every method is idempotent or
    a plain query so a resumed run never double-writes.

    Batch limits are the caller's to respect only in spirit: implementations chunk and page
    internally to the documented per-request maxima, so callers may pass any number of ids and
    ask the two listing methods as often as they like.
    """

    def granted_scopes(self) -> frozenset[str]:
        """Scopes the stored token actually carries. Never performs an authorization flow."""
        ...

    def search_artists(self, name: str) -> Sequence[SpotifyArtistRef]:
        """`GET /search?type=artist`. Candidates only - the caller decides what is confident."""
        ...

    def search_albums_by_upc(self, upc: str) -> Sequence[SpotifyAlbumRef]:
        """`GET /search?type=album` with the `upc:` filter."""
        ...

    def search_albums(self, artist: str, title: str) -> Sequence[SpotifyAlbumRef]:
        """`GET /search?type=album` with the `album:` and `artist:` filters."""
        ...

    def followed_artist_ids(self) -> frozenset[str]:
        """Every artist the user follows, by paging `GET /me/following?type=artist`.

        Not `GET /me/following/contains`, which is the obvious call for this and answers **403**
        on a Development Mode account (see the adapter). Implementations read the list once and
        answer every membership test from it.
        """
        ...

    def saved_album_ids(self) -> frozenset[str]:
        """Every album in the user's library, by paging `GET /me/albums`. Same 403 story."""
        ...

    def follow_artists(self, artist_ids: Sequence[str]) -> None:
        """Follow artists: `PUT /me/library?uris=spotify:artist:<id>`.

        **Not** `PUT /me/following?type=artist`, which the public reference documents and which
        answers **403** for a Development Mode app (measured). Idempotent either way: following an
        already-followed artist is a no-op.
        """
        ...

    def save_albums(self, album_ids: Sequence[str]) -> None:
        """Save albums: `PUT /me/library?uris=spotify:album:<id>`.

        **Not** `PUT /me/albums`, documented but **403** here - the per-type library writes are
        deprecated and blocked for a development-mode app. One unified endpoint serves both this
        and `follow_artists`, told apart by the URI prefix. Idempotent.
        """
        ...


class LidarrPort(Protocol):
    def version(self) -> str: ...

    def load_view(self, artist_mbids: Iterable[str] | None = None) -> LidarrView:
        """Read artists, profiles, tags; load albums only for the given artists (never `GET /album` unfiltered).
        None loads albums for no artists."""
        ...

    def load_albums(self, artist: LidarrArtist) -> dict[str, LidarrAlbum]:
        """rg_mbid → album for one artist."""
        ...

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        """Lidarr's own metadata lookup for a release group (album/lookup). Raise LidarrMetadataError on outage."""
        ...

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None: ...

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """Lidarr's name search, one hit per artist whose name and title both match.

        One artist's answer is exactly `search_release_group`'s. Several are same-named artists,
        which the resolver decides between exactly as it does for MusicBrainz's candidates."""
        ...

    def add_artist(
        self,
        artist_mbid: str,
        name: str,
        *,
        root_folder: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        tag_ids: Sequence[int],
    ) -> LidarrArtist:
        """Add with monitored=true, monitorNewItems=none, addOptions.monitor=none, no search.

        Raise LidarrMetadataError when Lidarr's metadata server fails the add, its subclass
        LidarrArtistUnknown when that server does not know the artist yet, and LidarrArtistExists,
        carrying the artist Lidarr holds, when the artist is already there."""
        ...

    def refresh_artist(self, artist: LidarrArtist, *, timeout_s: float = 300) -> None:
        """Queue RefreshArtist and POLL until it completes. Raise LidarrMetadataError if it fails."""
        ...

    def set_albums_monitored(self, album_ids: Sequence[int], monitored: bool) -> None: ...

    def set_artist_profile(self, artist: LidarrArtist, metadata_profile_id: int) -> None: ...

    def set_artists_new_items_none(self, artist_ids: Sequence[int]) -> None: ...

    def set_artists_monitored(self, artist_ids: Sequence[int]) -> None: ...

    def ensure_tag(self, label: str) -> int: ...

    def ensure_metadata_profile(self, profile: Profile, name: str) -> int:
        """Create the Lean/Full profile if missing; return its id. Idempotent."""
        ...


class HealthSink(Protocol):
    local: bool
    """True for a sink that never leaves the machine (stdout). A dry run publishes to local sinks
    only, never to a retained/remote one (MQTT, webhook): a hand-run dry-run applies nothing, and
    publishing it everywhere would overwrite the last real run's retained record and reset a
    dead-man's-switch built on it. See `adapters.health.publish_all`."""

    def publish(self, record: HealthRecord) -> None:
        """Best effort; must never raise into the caller (log instead)."""
        ...
