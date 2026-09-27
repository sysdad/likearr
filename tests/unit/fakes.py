"""In-memory fakes and builders for the pure core's tests.

`FakeLookup` implements `likearr.ports.MetadataLookup` out of plain dictionaries, counts its
calls (so cache behaviour is testable) and can be told to raise `MetadataError` from any method.
The builders below exist so a test can say what it is about in two lines instead of twenty.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from likearr.core.normalize import credits_match, normalize_name, normalize_title, strip_release_qualifiers
from likearr.models import (
    AlbumIntent,
    ArtistIntent,
    ArtistRelation,
    BarcodeMatch,
    IsrcRecording,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    OwnedRelease,
    PrimaryType,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    SecondaryType,
    SourceSnapshot,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.ports import MetadataError

CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "mb" / "corpus.json"

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
"""A fixed clock for every test. The core never reads the real one."""


# --------------------------------------------------------------------------- the lookup fake


@dataclass(slots=True)
class FakeLookup:
    """A `MetadataLookup` backed by dictionaries.

    `release_groups` is the universe, keyed by MBID. `barcodes`, `isrcs`, `catalogues` and
    `tracklists` index into it. `searches` and `artist_searches` override the derived name
    indexes, which is how a test stages a near-miss that must be refused.
    """

    release_groups: dict[str, ReleaseGroup] = field(default_factory=dict)
    barcodes: dict[str, str | list[str]] = field(default_factory=dict)
    """Barcode -> the release group(s) holding it, in the order MusicBrainz lists them. Compared as
    a GTIN, leading zeros dropped, as the real adapter compares them (issue #150)."""
    unofficial: set[str] = field(default_factory=set)
    """Release group MBIDs whose barcode hits are not Official releases."""
    isrcs: dict[str, list[str]] = field(default_factory=dict)
    isrc_titles: dict[str, dict[str, str]] = field(default_factory=dict)
    """ISRC -> {release group MBID: the title of the recording it came from} (issue #163). A release
    group left out comes from an untitled recording, which the resolver never drops."""
    catalogues: dict[str, list[str]] = field(default_factory=dict)
    tracklists: dict[str, list[str]] = field(default_factory=dict)
    artists: dict[str, str] = field(default_factory=dict)
    searches: dict[tuple[str, str], str | None] = field(default_factory=dict)
    candidate_searches: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    """Stage a name search that returns several candidates as given, e.g. one whose title only the
    adapter's looser gate accepts. Checked before `searches`."""
    artist_searches: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    relations: dict[str, list[ArtistRelation] | None] = field(default_factory=dict)
    """Artist MBID -> the artist-artist relationships MusicBrainz records for it (issue #14).
    ``None`` stages "could not be read", the composite lookup's answer to a failure."""
    other_credit_searches: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    """Stage `release_groups_under_other_credits` as given, overriding the derived index."""
    fail: set[str] = field(default_factory=set)
    """Method names that raise `MetadataError` instead of answering."""
    fail_error: Exception | None = None
    """What `fail` raises, when a test needs a specific subclass. Defaults to `MetadataError`."""
    calls: dict[str, int] = field(default_factory=dict)
    tracklists_read: list[str] = field(default_factory=list)
    """Every release group whose tracklist was asked for, in order (issue #164)."""
    cache_hits: int = 0
    """Unused by this fake (it has no cache of its own); mirrors `MusicBrainzLookup.cache_hits` so
    a test can wrap it in `CompositeLookup` and read `mb_cache_hits` off that (issue #119)."""
    live_calls: int = 0
    """Every fake call counts as live, since this fake never actually caches anything - mirrors
    `MusicBrainzLookup.live_calls` for the same reason as `cache_hits` above."""

    # -- helpers ------------------------------------------------------------
    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.live_calls += 1
        if name in self.fail:
            raise self.fail_error or MetadataError(f"fake failure in {name}")

    def add(self, *groups: ReleaseGroup) -> FakeLookup:
        for rg in groups:
            self.release_groups[rg.mbid] = rg
            self.artists.setdefault(rg.artist_mbid, rg.artist_name)
        return self

    # -- the port -----------------------------------------------------------
    def release_groups_by_barcode(self, upc: str) -> Sequence[BarcodeMatch]:
        self._count("release_groups_by_barcode")
        want = upc.lstrip("0")
        mbids: list[str] = []
        for code, found in self.barcodes.items():
            if want and code.lstrip("0") == want:
                mbids.extend([found] if isinstance(found, str) else found)
        groups = [self.release_groups[m] for m in dict.fromkeys(mbids) if m in self.release_groups]
        matches = [BarcodeMatch(release_group=g, official=g.mbid not in self.unofficial) for g in groups]
        return sorted(matches, key=lambda m: not m.official)

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        self._count("release_groups_for_isrc")
        return [self.release_groups[m] for m in self.isrcs.get(isrc, []) if m in self.release_groups]

    def recordings_for_isrc(self, isrc: str) -> Sequence[IsrcRecording]:
        """`isrcs` grouped into one recording per title in `isrc_titles`, first-seen order."""
        self._count("recordings_for_isrc")
        titles = self.isrc_titles.get(isrc, {})
        by_title: dict[str, list[ReleaseGroup]] = {}
        for mbid in self.isrcs.get(isrc, []):
            if mbid in self.release_groups:
                by_title.setdefault(titles.get(mbid, ""), []).append(self.release_groups[mbid])
        return [IsrcRecording(title=t, release_groups=tuple(gs)) for t, gs in by_title.items()]

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        found = self.search_release_group_candidates(artist, title)
        return found[0] if len(found) == 1 else None

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """Every match, as the real adapter answers, ordered by earliest date then MBID."""
        self._count("search_release_group_candidates")
        override = (artist, title)
        if override in self.candidate_searches:
            return tuple(self.release_groups[m] for m in self.candidate_searches[override])
        if override in self.searches:
            mbid = self.searches[override]
            found = self.release_groups.get(mbid) if mbid else None
            return (found,) if found else ()
        want = (normalize_name(artist), normalize_title(title))
        hits = [
            rg
            for rg in self.release_groups.values()
            if (normalize_name(rg.artist_name), normalize_title(rg.title)) == want
        ]
        return tuple(
            sorted(hits, key=lambda g: (g.first_release_date is None, g.first_release_date or date.min, g.mbid))
        )

    def release_groups_under_other_credits(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """Title matches under a credit that is *not* `artist`, as the real adapter answers: the
        exact title tier when it has any, otherwise the one after stripping qualifiers from both."""
        self._count("release_groups_under_other_credits")
        if (artist, title) in self.other_credit_searches:
            return tuple(self.release_groups[m] for m in self.other_credit_searches[(artist, title)])
        others = [
            rg for rg in self.release_groups.values() if rg.artist_mbid and not credits_match(rg.artist_name, artist)
        ]
        tier = [rg for rg in others if normalize_title(rg.title) == normalize_title(title)] or [
            rg
            for rg in others
            if normalize_title(strip_release_qualifiers(rg.title)) == normalize_title(strip_release_qualifiers(title))
        ]
        return tuple(
            sorted(tier, key=lambda g: (g.first_release_date is None, g.first_release_date or date.min, g.mbid))
        )

    def artist_relations(self, artist_mbid: str) -> Sequence[ArtistRelation] | None:
        self._count("artist_relations")
        known = self.relations.get(artist_mbid, [])
        return None if known is None else tuple(known)

    def search_artist(self, name: str) -> tuple[str, str] | None:
        self._count("search_artist")
        if name in self.artist_searches:
            return self.artist_searches[name]
        want = normalize_name(name)
        hits = [(mbid, n) for mbid, n in self.artists.items() if normalize_name(n) == want]
        return min(hits) if hits else None

    def search_artist_candidates(self, name: str) -> Sequence[tuple[str, str]]:
        """Every artist of exactly this name (issue #152). A staged `artist_searches` answer is the
        only candidate, so a test that stages one artist gets that one here too."""
        self._count("search_artist_candidates")
        if name in self.artist_searches:
            staged = self.artist_searches[name]
            return (staged,) if staged else ()
        want = normalize_name(name)
        return tuple(sorted((mbid, n) for mbid, n in self.artists.items() if normalize_name(n) == want))

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        self._count("artist_release_groups")
        return [self.release_groups[m] for m in self.catalogues.get(artist_mbid, []) if m in self.release_groups]

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        self._count("release_group_track_titles")
        self.tracklists_read.append(rg_mbid)
        return list(self.tracklists.get(rg_mbid, []))


# --------------------------------------------------------------------------- the golden corpus


def _parse_date(raw: str | None) -> date | None:
    """MusicBrainz dates can be 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Partial dates round down."""
    if not raw:
        return None
    parts = raw.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


@dataclass(slots=True)
class Corpus:
    """The recorded MusicBrainz fixture, ready to feed a `FakeLookup`."""

    raw: dict
    groups: dict[str, ReleaseGroup]

    def rg(self, case: str) -> ReleaseGroup:
        """The release group a named case points at, e.g. ``after_hours``."""
        return self.groups[self.raw["cases"][case]]

    def isrc(self, case: str, track: str) -> str:
        """The recorded ISRC of one track on one case's release."""
        return self.raw["track_isrcs"][f"{case}:{track}"]

    def barcode(self, case: str) -> str:
        """The recorded barcode of the case's representative release."""
        mbid = self.raw["cases"][case]
        for code, target in sorted(self.raw["barcodes"].items()):
            if target == mbid:
                return code
        raise KeyError(f"no barcode recorded for case {case!r}")

    def lookup(self, **overrides: object) -> FakeLookup:
        """A `FakeLookup` over the whole corpus."""
        fake = FakeLookup(
            release_groups=dict(self.groups),
            barcodes=dict(self.raw["barcodes"]),
            isrcs={k: list(v) for k, v in self.raw["isrc_release_groups"].items()},
            catalogues={k: list(v) for k, v in self.raw["artist_release_groups"].items()},
            tracklists={k: list(v) for k, v in self.raw["tracklists"].items()},
            artists=dict(self.raw["artists"]),
            relations={
                mbid: [
                    ArtistRelation(
                        relationship=r["type"],
                        direction=r["direction"],
                        artist_mbid=r["artist"]["id"],
                        artist_name=r["artist"]["name"],
                    )
                    for r in rels
                ]
                for mbid, rels in self.raw.get("artist_relations", {}).items()
            },
        )
        for key, value in overrides.items():
            setattr(fake, key, value)
        return fake


def load_corpus(path: Path = CORPUS_PATH) -> Corpus:
    """Load ``tests/fixtures/mb/corpus.json`` into `ReleaseGroup` objects."""
    raw = json.loads(path.read_text())
    groups: dict[str, ReleaseGroup] = {}
    for mbid, rg in raw["release_groups"].items():
        primary = PrimaryType(rg["primary_type"]) if rg.get("primary_type") else None
        groups[mbid] = ReleaseGroup(
            mbid=mbid,
            title=rg["title"],
            artist_mbid=rg["artist_mbid"],
            artist_name=rg["artist_name"],
            primary_type=primary,
            secondary_types=frozenset(SecondaryType(s) for s in rg.get("secondary_types", [])),
            first_release_date=_parse_date(rg.get("first_release_date")),
        )
    return Corpus(raw=raw, groups=groups)


# --------------------------------------------------------------------------- model builders


def relation(mbid: str, name: str, relationship: str = "member of band", direction: str = "backward") -> ArtistRelation:
    """One artist-artist relationship, `member of band` seen from the band by default."""
    return ArtistRelation(relationship=relationship, direction=direction, artist_mbid=mbid, artist_name=name)


def rg(
    mbid: str,
    title: str,
    *,
    artist_mbid: str = "artist-1",
    artist_name: str = "Test Artist",
    primary: PrimaryType | None = PrimaryType.ALBUM,
    secondary: Iterable[SecondaryType] = (),
    released: str | None = "2020-01-01",
    credit: Sequence[str] | None = None,
) -> ReleaseGroup:
    """A `ReleaseGroup`, studio Album by default. `credit` is the main credited artists' MBIDs
    (#164); by default the one artist, `artist_mbid`."""
    return ReleaseGroup(
        mbid=mbid,
        title=title,
        artist_mbid=artist_mbid,
        artist_name=artist_name,
        primary_type=primary,
        secondary_types=frozenset(secondary),
        first_release_date=_parse_date(released),
        main_artist_mbids=tuple(credit) if credit is not None else (artist_mbid,),
    )


def reason(kind: ReasonKind, source_id: str, playlist_id: str | None = None) -> Reason:
    return Reason(kind=kind, source_id=source_id, playlist_id=playlist_id)


def spotify_album(
    name: str,
    *,
    spotify_id: str = "sp-album",
    artists: Sequence[str] = ("Test Artist",),
    upc: str | None = None,
    album_type: str = "album",
    released: str | None = "2020-01-01",
) -> SpotifyAlbumRef:
    return SpotifyAlbumRef(
        spotify_id=spotify_id,
        name=name,
        artist_names=tuple(artists),
        upc=upc,
        album_type=album_type,
        release_date=_parse_date(released),
    )


def artist_intent(name: str, *, spotify_id: str = "sp-artist") -> ArtistIntent:
    return ArtistIntent(
        spotify_id=spotify_id,
        name=name,
        reason=reason(ReasonKind.FOLLOWED, spotify_id),
    )


def album_intent(album: SpotifyAlbumRef) -> AlbumIntent:
    return AlbumIntent(album=album, reason=reason(ReasonKind.SAVED, album.spotify_id))


def track_intent(
    name: str,
    album: SpotifyAlbumRef,
    *,
    spotify_id: str = "sp-track",
    isrc: str | None = None,
    artists: Sequence[str] = ("Test Artist",),
    playlist_id: str | None = None,
) -> TrackIntent:
    kind = ReasonKind.PLAYLIST if playlist_id else ReasonKind.LIKED
    return TrackIntent(
        spotify_id=spotify_id,
        name=name,
        isrc=isrc,
        artist_names=tuple(artists),
        album=album,
        added_at=NOW,
        reason=reason(kind, spotify_id, playlist_id),
    )


def snapshot(
    *,
    artists: Sequence[ArtistIntent] = (),
    albums: Sequence[AlbumIntent] = (),
    tracks: Sequence[TrackIntent] = (),
    counts: dict[str, int] | None = None,
    schema_ok: bool = True,
    fetched_at: datetime = NOW,
) -> SourceSnapshot:
    if counts is None:
        counts = {
            "followed_artists": len(artists),
            "saved_albums": len(albums),
            "liked_tracks": len([t for t in tracks if t.reason.kind == ReasonKind.LIKED]),
        }
    return SourceSnapshot(
        fetched_at=fetched_at,
        artists=tuple(artists),
        albums=tuple(albums),
        tracks=tuple(tracks),
        counts=counts,
        schema_ok=schema_ok,
    )


def live_reason_keys(snap: SourceSnapshot) -> set[str]:
    """Every reason key present in a snapshot, as `build_diff` wants it."""
    keys = {i.reason.key for i in snap.artists}
    keys |= {i.reason.key for i in snap.albums}
    keys |= {i.reason.key for i in snap.tracks}
    return keys


def lidarr_artist(
    mbid: str,
    *,
    id: int = 1,
    name: str = "Test Artist",
    monitored: bool = True,
    monitor_new_items: str = "none",
    metadata_profile_id: int = 10,
    quality_profile_id: int = 1,
    tags: Iterable[int] = (),
    path: str = "",
) -> LidarrArtist:
    return LidarrArtist(
        id=id,
        mbid=mbid,
        name=name,
        monitored=monitored,
        monitor_new_items=monitor_new_items,
        metadata_profile_id=metadata_profile_id,
        quality_profile_id=quality_profile_id,
        tags=frozenset(tags),
        path=path,
    )


def lidarr_album(
    release: ReleaseGroup,
    *,
    id: int = 100,
    artist_id: int = 1,
    monitored: bool = False,
    files: int = 0,
    size: int = 0,
) -> LidarrAlbum:
    return LidarrAlbum(
        id=id,
        rg_mbid=release.mbid,
        artist_id=artist_id,
        artist_mbid=release.artist_mbid,
        title=release.title,
        monitored=monitored,
        primary_type=release.primary_type,
        secondary_types=release.secondary_types,
        release_date=release.first_release_date,
        track_file_count=files,
        size_on_disk=size,
    )


LEAN_PROFILE_ID = 10
FULL_PROFILE_ID = 20


def lidarr_view(
    *,
    artists: Iterable[LidarrArtist] = (),
    albums: Iterable[LidarrAlbum] = (),
) -> LidarrView:
    """A `LidarrView`; albums are filed under their own artist MBID automatically."""
    by_artist: dict[str, LidarrArtist] = {a.mbid: a for a in artists}
    by_album: dict[str, dict[str, LidarrAlbum]] = {a.mbid: {} for a in by_artist.values()}
    for album in albums:
        by_album.setdefault(album.artist_mbid, {})[album.rg_mbid] = album
    return LidarrView(
        artists=by_artist,
        albums=by_album,
        metadata_profiles={"Lean": LEAN_PROFILE_ID, "Full": FULL_PROFILE_ID},
        quality_profiles={"Any": 1},
        tags={"likearr": 1, "albums-only": 2},
        version="2.6.0.4344",
    )


def owned(
    release: ReleaseGroup,
    *reasons: Reason,
    step: str = "test",
    album_id: int | None = 100,
    at: datetime = NOW,
    resolver_version: int = 1,
) -> tuple[ReleaseKey, OwnedRelease]:
    """An ownership record, ready to drop into an `owned` mapping."""
    key = ReleaseKey(artist_mbid=release.artist_mbid, rg_mbid=release.mbid)
    return key, OwnedRelease(
        key=key,
        reasons=frozenset(reasons),
        step=step,
        resolver_version=resolver_version,
        monitored_at=at,
        lidarr_album_id=album_id,
    )
