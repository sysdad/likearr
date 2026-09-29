"""Report which files on disk no source asks for any more.

This only ever produces a report. Nothing here deletes, moves or unmonitors anything; the shell's
`prune-stage` moves files to a holding directory and `rm` stays a human's job.

A row is a **candidate** when the album has files, no source wants it and likearr does not own it.
A candidate is **protected** instead when deleting it would take away the only local copy of a
song the user liked, when it is a studio album or EP of a followed artist whose catalogue or own
lookup failed this run, or when a song or saved album whose lookup failed names it - see
:func:`build_prune_report` for the rules. Why, is kept as data (`Protection`), so
the review can say it in words with no id in them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from likearr.core.desire import CATALOGUE_UNREAD_STEPS
from likearr.core.failed_lookups import FailedItems, failed_artist_names, failed_items
from likearr.core.normalize import normalize_name
from likearr.models import (
    ArtistResolution,
    DesiredState,
    LidarrView,
    OwnedRelease,
    PrimaryType,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SecondaryType,
    SourceSnapshot,
    TrackIntent,
)

__all__ = [
    "PROTECTION_KINDS",
    "LookupGaps",
    "Protection",
    "PruneReport",
    "PruneRow",
    "build_prune_report",
    "follow_may_bring",
    "lookup_gaps",
    "split_track_key",
    "unread_catalogues",
]

_TRACK_REASON_PREFIXES = ("liked:", "playlist:")

PROTECTION_KINDS = ("pending_album", "album_not_downloaded", "catalogue_unread", "lookup_failed")
"""``pending_album``: the song is waiting for its album to be released, so nothing is monitored
for it. ``album_not_downloaded``: the album it matched has no files yet. ``catalogue_unread``: the
artist is followed, but their catalogue or their own lookup could not be read this run, so whether
the follow wants this album is unknown. ``lookup_failed``: a liked song, playlist song or saved
album of this title and artist failed its lookup this run, so whether it wants this album is
unknown."""

THIS_RUN_KINDS = frozenset({"catalogue_unread", "lookup_failed"})
"""Kinds that hold only while a lookup keeps failing: the next run that reads it decides."""


def split_track_key(intent_key: str) -> tuple[str, str, str] | None:
    """``(source, playlist id, track id)`` for a liked-song or playlist-track intent key
    (`Reason.key`: ``liked:<track id>``, ``playlist:<playlist id>:<track id>``); the playlist id is
    ``""`` for a liked song. ``None`` for any other key."""
    source, _, rest = intent_key.partition(":")
    if source == "liked" and rest and ":" not in rest:
        return "liked", "", rest
    playlist_id, _, track_id = rest.partition(":")
    if source == "playlist" and playlist_id and track_id and ":" not in track_id:
        return "playlist", playlist_id, track_id
    return None


@dataclass(frozen=True, slots=True)
class Protection:
    """Why a release must not be pruned, as data: the review words it, and names no id."""

    kind: str
    """One of `PROTECTION_KINDS`."""
    intent_key: str
    """`Reason.key` of the liked song or playlist track whose only copy this is; for
    ``catalogue_unread``, of the artist's follow; for ``lookup_failed``, of the song or saved
    album whose lookup failed."""
    song: str = ""
    """The track's title, from the run's snapshot; empty when the snapshot did not have it."""
    song_artists: tuple[str, ...] = ()
    album: str = ""
    """The title of the album the song matched; empty while it waits for one."""
    album_mbid: str = ""

    @property
    def source(self) -> str:
        """``liked`` or ``playlist``; ``""`` for a key that is neither."""
        parts = split_track_key(self.intent_key)
        return parts[0] if parts is not None else ""

    @property
    def playlist_id(self) -> str:
        parts = split_track_key(self.intent_key)
        return parts[1] if parts is not None else ""

    @property
    def this_run_only(self) -> bool:
        """Kept only until a run reads what failed (`THIS_RUN_KINDS`)."""
        return self.kind in THIS_RUN_KINDS

    def describe(self) -> str:
        """The terminal's line, ids and all: `prune-stage` quotes it when it refuses a trash."""
        if self.kind == "lookup_failed":
            return (
                f"a Spotify song or album of this title and artist ({self.intent_key}) could not be looked up "
                "this run, so whether it wants this album is unknown"
            )
        if self.kind == "catalogue_unread":
            return (
                f"its artist is followed ({self.intent_key}) but their catalogue could not be read this run, "
                "so whether the follow wants it is unknown"
            )
        if self.kind == "pending_album":
            return (
                f"holds a liked track ({self.intent_key}) that is still waiting for an album; "
                "this is the only copy on disk"
            )
        where = f"{self.album!r} ({self.album_mbid})" if self.album_mbid else "its resolved album"
        return (
            f"holds a liked track ({self.intent_key}) whose album {where} has no files yet; "
            "this is the only copy on disk"
        )

    def to_dict(self) -> dict[str, Any]:
        """The ``protection`` object of a protected row in `prune.json`."""
        parts = split_track_key(self.intent_key)
        return {
            "kind": self.kind,
            "intent_key": self.intent_key,
            "source": parts[0] if parts is not None else None,
            "playlist_id": (parts[1] or None) if parts is not None else None,
            "track_id": parts[2] if parts is not None else None,
            "song": self.song or None,
            "song_artists": list(self.song_artists),
            "album": self.album or None,
            "album_mbid": self.album_mbid or None,
        }


@dataclass(frozen=True, slots=True)
class PruneRow:
    """One album with files on disk, as the prune report sees it."""

    artist_mbid: str
    artist_name: str
    lidarr_artist_id: int | None
    rg_mbid: str
    title: str
    primary_type: PrimaryType | None
    secondary_types: frozenset[SecondaryType]
    release_date: date | None
    track_file_count: int
    size_on_disk: int
    lidarr_album_id: int | None
    path: str = ""
    """Lidarr's folder for the artist, when the view knows it. Empty otherwise."""
    protected_reason: str | None = None
    """Why this row was held back from the candidate list, or None when it is a candidate: the
    terminal's line, `Protection.describe`."""
    artist_followed: bool | None = False
    """The artist is followed on Spotify, as the same read `run` makes knows it (a follow that
    resolved to this MusicBrainz artist); ``None`` when follows were not read at all
    (`[spotify] followed_artists = false`), so nobody can say. The review uses it to say why
    nothing asks for an album - a followed artist brings studio albums and EPs only - and to hide
    "follow"."""
    follow_unmatched: bool = False
    """A Spotify follow with this artist's name could not be matched to a MusicBrainz artist, so
    "not followed" may be wrong: the review says so instead of claiming it."""
    protection: Protection | None = None
    """`protected_reason` as data, for the review's words; None for a candidate."""


@dataclass(slots=True)
class PruneReport:
    """Everything the prune command needs to print, and nothing it needs to decide."""

    created_at: datetime
    candidates: list[PruneRow] = field(default_factory=list)
    protected: list[PruneRow] = field(default_factory=list)
    by_artist: dict[str, int] = field(default_factory=dict)
    """artist name -> number of candidate rows."""
    bytes_by_artist: dict[str, int] = field(default_factory=dict)
    """artist name -> candidate bytes on disk."""
    total_bytes: int = 0
    """Bytes the candidate rows occupy. Protected rows are excluded."""

    @property
    def total_candidates(self) -> int:
        return len(self.candidates)


def _is_track_reason(intent_key: str) -> bool:
    """True for a liked-song or playlist-track intent key (see `Reason.key`)."""
    return intent_key.startswith(_TRACK_REASON_PREFIXES)


def build_prune_report(
    desired: DesiredState,
    view: LidarrView,
    owned: Mapping[ReleaseKey, OwnedRelease],
    resolutions: Iterable[Resolution],
    *,
    now: datetime,
    followed_read: bool = True,
    unmatched_follows: frozenset[str] = frozenset(),
    tracks: Iterable[TrackIntent] = (),
    snapshot: SourceSnapshot | None = None,
    lookup_failed: AbstractSet[str] = frozenset(),
) -> PruneReport:
    """List the albums with files that no source asks for, protecting liked songs' only copy.

    A candidate needs all three of: files on disk, no reason in the desired state, and no
    ownership record. The last one matters - an owned release with no desired reason is
    already on the diff's unmonitor list, and unmonitoring it is a decision about *monitoring*,
    not about deleting the files that are already there.

    **The protection rule.** For a liked or playlist track, the release Spotify named is often
    not the release likearr monitors: the user liked a song on a single, and likearr monitored
    the album that song also appears on. If that album has not been downloaded yet, the single is
    the only copy of the song on disk, and deleting it loses the song. So a release group is
    protected when it is the release Spotify named for a liked/playlist track
    (`Resolution.source_release_group`, or `single_release_group` for a track still waiting for
    its album) *and* the release that track resolved to has no files in the view. A PENDING_ALBUM
    track resolved to nothing at all, so its single is always protected.

    **Unread catalogues.** A followed artist whose catalogue could not be read this run (in
    `desired.unmapped` at a step in `CATALOGUE_UNREAD_STEPS`) contributed none of their releases,
    so "nothing asks for it" is unknown rather than true for the studio albums and EPs the follow
    would bring: those rows are protected (``catalogue_unread``). Their other releases - a live
    album, a compilation - are not what a follow brings, and stay candidates.

    **Failed lookups.** Rows are protected the same way, matched by name as `adopt` does (see
    `likearr.core.failed_lookups`), when a followed artist of the row's artist name failed their
    own lookup (``catalogue_unread``, studio albums and EPs only), or when a saved album, liked
    song or playlist song in `snapshot` whose lookup failed names the row's title and artist
    (``lookup_failed``, whatever its type). `lookup_failed` holds the intent keys MusicBrainz
    failed during (`ResolveResult.provisional`).

    Each row also says whether its artist is followed (`followed_read` False: follows were not
    read, so ``None``), and whether a Spotify follow of that name went unmatched
    (`unmatched_follows`: casefolded names). A protected row names the song it protects by its
    title and artists, looked up in `tracks` (the run's snapshot) by intent key.
    """
    report = PruneReport(created_at=now)
    songs = {track.reason.key: track for track in tracks}
    gaps = lookup_gaps(desired, snapshot, lookup_failed)

    protectors: dict[str, list[tuple[Resolution, bool]]] = {}
    for resolution in resolutions:
        if not _is_track_reason(resolution.intent_key):
            continue
        source = resolution.source_release_group or resolution.single_release_group
        if source is None:
            continue
        target = resolution.release_group
        if target is not None and target.mbid == source.mbid:
            # The release Spotify named is the release being monitored; ordinary rules apply.
            continue
        target_has_files = False
        if target is not None:
            album = view.album(ReleaseKey(artist_mbid=target.artist_mbid, rg_mbid=target.mbid))
            target_has_files = album is not None and album.has_files
        protectors.setdefault(source.mbid, []).append((resolution, target_has_files))

    for artist_mbid in sorted(view.albums):
        artist = view.artists.get(artist_mbid)
        for rg_mbid in sorted(view.albums[artist_mbid]):
            album = view.albums[artist_mbid][rg_mbid]
            if not album.has_files:
                continue
            key = ReleaseKey(artist_mbid=artist_mbid, rg_mbid=rg_mbid)
            release = desired.releases.get(key)
            wanted = release is not None and bool(release.reasons)
            is_owned = key in owned
            if wanted or is_owned:
                continue

            artist_name = artist.name if artist is not None else desired.artists.get(artist_mbid, artist_mbid)
            protection = _protection(protectors.get(rg_mbid, []), songs) or gaps.protection(
                artist_mbid, artist_name, album.title, album.primary_type, album.secondary_types
            )
            row = PruneRow(
                artist_mbid=artist_mbid,
                artist_name=artist_name,
                lidarr_artist_id=artist.id if artist is not None else None,
                rg_mbid=rg_mbid,
                title=album.title,
                primary_type=album.primary_type,
                secondary_types=album.secondary_types,
                release_date=album.release_date,
                track_file_count=album.track_file_count,
                size_on_disk=album.size_on_disk,
                lidarr_album_id=album.id,
                path=artist.path if artist is not None else "",
                protected_reason=protection.describe() if protection is not None else None,
                artist_followed=(artist_mbid in desired.followed_artists) if followed_read else None,
                follow_unmatched=followed_read
                and artist_mbid not in desired.followed_artists
                and bool(artist is not None and artist.name.casefold() in unmatched_follows),
                protection=protection,
            )
            if protection is not None:
                report.protected.append(row)
                continue
            report.candidates.append(row)
            report.by_artist[row.artist_name] = report.by_artist.get(row.artist_name, 0) + 1
            report.bytes_by_artist[row.artist_name] = report.bytes_by_artist.get(row.artist_name, 0) + row.size_on_disk
            report.total_bytes += row.size_on_disk

    return report


def unread_catalogues(desired: DesiredState, lookup_failed: AbstractSet[str] = frozenset()) -> dict[str, str]:
    """Followed artists whose catalogue could not be read this run: artist mbid -> follow's intent
    key. With `lookup_failed`, also an unmapped follow MusicBrainz failed during, as `adopt` holds."""
    return {
        u.artist_mbid: u.intent_key
        for u in desired.unmapped
        if isinstance(u, ArtistResolution)
        and u.artist_mbid
        and (u.step in CATALOGUE_UNREAD_STEPS or u.intent_key in lookup_failed)
    }


@dataclass(frozen=True, slots=True)
class LookupGaps:
    """What this run could not look up, for deciding whether an album is kept this time."""

    unread: dict[str, str] = field(default_factory=dict)
    """`unread_catalogues`: artist mbid -> follow's intent key."""
    failed_artists: dict[str, str] = field(default_factory=dict)
    """`failed_artist_names`: normalised name -> follow's intent key."""
    failed: FailedItems = field(default_factory=FailedItems)

    def protection(
        self,
        artist_mbid: str,
        artist_name: str,
        title: str,
        primary_type: PrimaryType | None,
        secondary_types: frozenset[SecondaryType],
    ) -> Protection | None:
        """Why this album is kept this time, or None."""
        if follow_may_bring(primary_type, secondary_types):
            if artist_mbid in self.unread:
                return Protection(kind="catalogue_unread", intent_key=self.unread[artist_mbid])
            follow = self.failed_artists.get(normalize_name(artist_name)) if artist_name else None
            if follow is not None:
                return Protection(kind="catalogue_unread", intent_key=follow)
        item = self.failed.naming(artist_name, title)
        if item is None:
            return None
        is_track = split_track_key(item.intent_key) is not None
        return Protection(
            kind="lookup_failed",
            intent_key=item.intent_key,
            song=item.name if is_track else "",
            song_artists=item.artists if is_track else (),
            album=title,
        )


def lookup_gaps(
    desired: DesiredState, snapshot: SourceSnapshot | None, lookup_failed: AbstractSet[str] = frozenset()
) -> LookupGaps:
    """The catalogues, followed artists and songs or saved albums this run could not look up."""
    return LookupGaps(
        unread=unread_catalogues(desired, lookup_failed),
        failed_artists=failed_artist_names(desired, lookup_failed),
        failed=failed_items(desired, snapshot, lookup_failed),
    )


def follow_may_bring(primary_type: PrimaryType | None, secondary_types: frozenset[SecondaryType]) -> bool:
    """A studio album or EP, or one whose type Lidarr does not know: what a follow may bring."""
    return not secondary_types and primary_type in (PrimaryType.ALBUM, PrimaryType.EP, None)


def _protection(claims: list[tuple[Resolution, bool]], songs: Mapping[str, TrackIntent]) -> Protection | None:
    """The first reason this release group must not be pruned, or None."""
    for resolution, target_has_files in sorted(claims, key=lambda c: c[0].intent_key):
        if resolution.status == ResolutionStatus.PENDING_ALBUM:
            kind = "pending_album"
        elif not target_has_files:
            kind = "album_not_downloaded"
        else:
            continue
        track = songs.get(resolution.intent_key)
        target = resolution.release_group if kind == "album_not_downloaded" else None
        return Protection(
            kind=kind,
            intent_key=resolution.intent_key,
            song=track.name if track is not None else "",
            song_artists=track.artist_names if track is not None else (),
            album=target.title if target is not None else "",
            album_mbid=target.mbid if target is not None else "",
        )
    return None
