"""In-memory fakes for the shell's tests.

`FakeLidarr` is the interesting one. It is a dictionary-backed `LidarrShell` that behaves like
the real thing in the four ways that matter to `apply`:

- an artist's albums do not exist until `refresh_artist` has run, which is why the shell re-reads
  them after adding an artist rather than trusting the planning view;
- `refresh_artist` monitors the albums it finds as the artist's stored "Monitor New Albums" says
  (`all`, `new` or `none`), which is how a widened profile could monitor every new release type;
- `refresh_artist` can fail for a named artist, which is how a Lidarr metadata outage looks;
- `set_albums_monitored` can be made to raise on the Nth monitor or unmonitor batch, which is how
  a crash halfway through an apply looks. It can raise before flipping anything, after flipping part of the
  batch, or after flipping all of it, because a real Lidarr can answer with an error either way.

Everything else is a plain dict, and every call is recorded in `calls` so a test can assert that
a guarded run made *zero* Lidarr writes rather than merely fewer than usual.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.lidarr import expected_metadata_profile_types
from likearr.adapters.spotify_library import OwnedPlaylist, PlaylistEntry
from likearr.adapters.state_sqlite import SqliteState
from likearr.config import (
    Config,
    GuardsConfig,
    HealthConfig,
    LidarrConfig,
    MusicBrainzConfig,
    RulesConfig,
    SpotifyConfig,
)
from likearr.core.match import spotify_id_from_url
from likearr.models import (
    HealthRecord,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    Profile,
    ReleaseGroup,
    SourceSnapshot,
    SpotifyAlbumRef,
    SpotifyArtistRef,
)
from likearr.ports import (
    LidarrArtistExists,
    LidarrArtistUnknown,
    LidarrError,
    LidarrMetadataError,
    SearchBudgetExceeded,
    SourceError,
)
from likearr.shell.context import Context
from tests.unit.fakes import FakeLookup, snapshot

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

LEAN_ID = 10
FULL_ID = 20
QUALITY_ID = 1
TAG_ID = 7
ALBUMS_ONLY_TAG_ID = 8


# --------------------------------------------------------------------------- source


@dataclass(slots=True)
class FakeSource:
    """A `SourcePort` that returns a canned snapshot, or raises like a real outage."""

    snapshot: SourceSnapshot = field(default_factory=lambda: snapshot())
    error: SourceError | None = None
    reads: int = 0

    def read(self) -> SourceSnapshot:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


# --------------------------------------------------------------------------- spotify library


@dataclass(slots=True)
class FakeLibrary:
    """A `SpotifyLibraryPort` backed by dictionaries, for `promote-save`.

    `followed` and `saved` are the *live* Spotify library: the two listing methods return them
    and the writes add to them, so a test can apply twice and assert the second pass wrote
    nothing. There are no `/contains` methods here because there are none on the port - they are
    403 on a Development Mode account.
    """

    scopes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"user-follow-read", "user-library-read", "user-follow-modify", "user-library-modify"}
        )
    )
    artist_hits: dict[str, list[SpotifyArtistRef]] = field(default_factory=dict)
    """artist name -> what `search?type=artist` returns."""
    upc_hits: dict[str, list[SpotifyAlbumRef]] = field(default_factory=dict)
    album_hits: dict[tuple[str, str], list[SpotifyAlbumRef]] = field(default_factory=dict)
    """(artist name, album title) -> what `search?type=album` returns."""
    followed: set[str] = field(default_factory=set)
    saved: set[str] = field(default_factory=set)
    playlists: list[OwnedPlaylist] = field(default_factory=list)
    """What `owned_playlists` answers, already filtered to the user's own and sorted."""
    unowned_playlists: list[PlaylistEntry] = field(default_factory=list)
    """Non-owned rows `all_playlists` adds alongside `playlists` (followed, collaborative,
    Spotify's own algorithmic and editorial playlists) - each must already have `owned=False`."""
    playlists_error: SourceError | None = None
    """Raised by `owned_playlists` and `all_playlists` instead, the way an expired token or the
    quota looks."""
    searches: int = 0
    budget: int | None = None
    """Raise `SearchBudgetExceeded` once this many searches have been made."""
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def _search(self, label: str, payload: Any) -> None:
        if self.budget is not None and self.searches >= self.budget:
            raise SearchBudgetExceeded("fake: search budget spent")
        self.searches += 1
        self.calls.append((label, payload))

    def granted_scopes(self) -> frozenset[str]:
        return self.scopes

    def search_artists(self, name: str) -> Sequence[SpotifyArtistRef]:
        self._search("search_artists", name)
        return tuple(self.artist_hits.get(name, []))

    def search_albums_by_upc(self, upc: str) -> Sequence[SpotifyAlbumRef]:
        self._search("search_albums_by_upc", upc)
        return tuple(self.upc_hits.get(upc, []))

    def search_albums(self, artist: str, title: str) -> Sequence[SpotifyAlbumRef]:
        self._search("search_albums", (artist, title))
        return tuple(self.album_hits.get((artist, title), []))

    def followed_artist_ids(self) -> frozenset[str]:
        self.calls.append(("followed_artist_ids", None))
        return frozenset(self.followed)

    def saved_album_ids(self) -> frozenset[str]:
        self.calls.append(("saved_album_ids", None))
        return frozenset(self.saved)

    def owned_playlists(self) -> list[OwnedPlaylist]:
        self.calls.append(("owned_playlists", None))
        if self.playlists_error is not None:
            raise self.playlists_error
        return list(self.playlists)

    def all_playlists(self) -> list[PlaylistEntry]:
        self.calls.append(("all_playlists", None))
        if self.playlists_error is not None:
            raise self.playlists_error
        owned = [PlaylistEntry(id=p.id, name=p.name, track_count=p.track_count, owned=True) for p in self.playlists]
        return sorted(owned + list(self.unowned_playlists), key=lambda p: (p.name.casefold(), p.id))

    def follow_artists(self, artist_ids: Sequence[str]) -> None:
        self.calls.append(("follow_artists", list(artist_ids)))
        self.followed.update(artist_ids)

    def save_albums(self, album_ids: Sequence[str]) -> None:
        self.calls.append(("save_albums", list(album_ids)))
        self.saved.update(album_ids)

    def writes(self) -> list[tuple[str, Any]]:
        """Only the calls that change something on Spotify."""
        return [(name, payload) for name, payload in self.calls if name in {"follow_artists", "save_albums"}]


@dataclass(slots=True)
class FakeLinks:
    """A `ReleaseLinkLookup`: MusicBrainz's Spotify relationships and barcodes, from dictionaries.

    `artist_links` and `album_links` hold raw relationship URLs rather than ids, so a test can
    stage a malformed or wrong-entity URL and watch it fall through to the search tiers exactly
    as the real adapter would.
    """

    barcodes: dict[str, list[str]] = field(default_factory=dict)
    artist_links: dict[str, list[str]] = field(default_factory=dict)
    album_links: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _one(self, urls: Sequence[str], kind: str) -> str | None:
        """Mirrors the real adapter: exactly one valid, right-kind id, or nothing."""
        found: list[str] = []
        for url in urls:
            spotify_id = spotify_id_from_url(url, kind)
            if spotify_id and spotify_id not in found:
                found.append(spotify_id)
        return found[0] if len(found) == 1 else None

    def spotify_artist_id(self, artist_mbid: str) -> str | None:
        self.calls.append(("spotify_artist_id", artist_mbid))
        return self._one(self.artist_links.get(artist_mbid, []), "artist")

    def spotify_album_id(self, rg_mbid: str) -> str | None:
        self.calls.append(("spotify_album_id", rg_mbid))
        return self._one(self.album_links.get(rg_mbid, []), "album")

    def release_group_barcodes(self, rg_mbid: str) -> Sequence[str]:
        self.calls.append(("release_group_barcodes", rg_mbid))
        return tuple(self.barcodes.get(rg_mbid, []))


# --------------------------------------------------------------------------- lidarr


def _monitors_new_album(monitor_new_items: str, released: date | None, newest: date) -> bool:
    """Whether a refresh monitors an album it finds, as Lidarr's `ShouldMonitorNewAlbum` decides it:
    "all" monitors every one, "new" one released on or after the newest album the artist already
    had (an undated one counts as the oldest), and "none" none."""
    if monitor_new_items == "all":
        return True
    if monitor_new_items == "new":
        return (released or date.min) >= newest
    return False


@dataclass(slots=True)
class FakeLidarr:
    """A dictionary-backed `LidarrShell`."""

    artists: dict[str, LidarrArtist] = field(default_factory=dict)
    albums: dict[str, dict[str, LidarrAlbum]] = field(default_factory=dict)
    catalogue: dict[str, list[ReleaseGroup]] = field(default_factory=dict)
    """artist mbid -> the release groups that appear once the artist has been refreshed."""
    metadata_profiles: dict[str, int] = field(default_factory=lambda: {"Lean": LEAN_ID, "Full": FULL_ID})
    quality_profiles: dict[str, int] = field(default_factory=lambda: {"Standard": QUALITY_ID})
    tags: dict[str, int] = field(default_factory=lambda: {"likearr": TAG_ID, "albums-only": ALBUMS_ONLY_TAG_ID})
    root_folder_paths: list[str] = field(default_factory=lambda: ["/music"])
    root_folder_defaults: tuple[str, str] = ("none", "none")
    """(defaultMonitorOption, defaultNewItemMonitorOption) `root_folders` reports for every path -
    a test overrides this to simulate a root folder whose defaults were changed by hand."""
    metadata_profile_details_override: dict[str, dict[str, Any]] = field(default_factory=dict)
    """name -> raw `primaryAlbumTypes`/`secondaryAlbumTypes` for `metadata_profile_details`, for a
    test that wants an existing profile to differ from what likearr would create."""
    track_file_rows: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    version_string: str = "3.1.0.4875"

    fail_refresh: set[str] = field(default_factory=set)
    """Artist MBIDs whose RefreshArtist raises `LidarrMetadataError`."""
    fail_add: set[str] = field(default_factory=set)
    """Artist MBIDs whose add Lidarr answers with a 5xx (its metadata server is down). The real
    adapter raises `LidarrMetadataError` for that."""
    unknown_add: set[str] = field(default_factory=set)
    """Artist MBIDs Lidarr's metadata server does not know yet: Lidarr's 400 "An artist with this
    ID was not found", which the real adapter raises as `LidarrArtistUnknown`."""
    reject_add: set[str] = field(default_factory=set)
    """Artist MBIDs whose add Lidarr refuses for any other reason, such as a bad root folder: a
    plain `LidarrError`, which stops the apply."""
    added_elsewhere: dict[str, bool] = field(default_factory=dict)
    """Artist MBIDs someone adds to Lidarr just before likearr's own add, each with
    whether it carries the tags likearr's add sends (True: likearr's own add, from a run that
    stopped before recording it). The add then meets Lidarr's 400 "already exists"."""
    unmonitor_added_artists: bool = False
    """Lidarr's real behaviour: POST /artist answers monitored=true, then `addOptions.monitor: none`
    leaves the stored artist unmonitored."""
    fail_monitor_batch: int | None = None
    """1-based index of the `set_albums_monitored(.., True)` batch that raises."""
    fail_monitor_batch_applies: int = 0
    """How many albums, from the front of the failing batch, Lidarr flips before it answers with
    the error. 0 is a clean failure. Part of the batch is Lidarr 3.1's HTTP 500 on a batch holding
    one album id that no longer exists: it still flips the ids that do. The whole batch is a reply
    lost after the change had landed."""
    down_after_monitor_failure: bool = False
    """After the failing monitor batch, every `load_albums` raises too, like a Lidarr that crashed."""
    fail_unmonitor_batch: int | None = None
    """1-based index of the `set_albums_monitored(.., False)` batch that raises."""
    fail_unmonitor_batch_applies: int = 0
    """How many albums, from the front of the failing unmonitor batch, Lidarr flips before it
    answers with the error, as `fail_monitor_batch_applies`."""
    down_after_unmonitor_failure: bool = False
    """After the failing unmonitor batch, every `load_albums` raises too."""

    refresh_timeouts: dict[str, float] = field(default_factory=dict)
    """artist mbid -> the `timeout_s` the last RefreshArtist for them was given."""

    calls: list[tuple[str, Any]] = field(default_factory=list)
    monitor_batches: int = 0
    unmonitor_batches: int = 0
    _down: bool = False
    _next_artist_id: int = 1000
    _next_album_id: int = 5000

    # -- helpers ------------------------------------------------------------
    def record(self, name: str, payload: Any = None) -> None:
        self.calls.append((name, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def writes(self) -> list[str]:
        """Only the calls that change something in Lidarr."""
        mutating = {
            "add_artist",
            "set_albums_monitored",
            "set_artist_profile",
            "set_artists_new_items_none",
            "set_artists_monitored",
            "ensure_tag",
            "ensure_metadata_profile",
            "add_root_folder",
            "set_root_folder_defaults",
            "delete_artist",
            "rescan_artist",
            "refresh_artist",
        }
        return [name for name in self.names() if name in mutating]

    def seed(self, artist: LidarrArtist, *albums: LidarrAlbum) -> FakeLidarr:
        self.artists[artist.mbid] = artist
        self.albums.setdefault(artist.mbid, {})
        for album in albums:
            self.albums[artist.mbid][album.rg_mbid] = album
        return self

    def album(self, artist_mbid: str, rg_mbid: str) -> LidarrAlbum | None:
        return self.albums.get(artist_mbid, {}).get(rg_mbid)

    def _album_by_id(self, album_id: int) -> tuple[str, str] | None:
        for artist_mbid, albums in self.albums.items():
            for rg_mbid, album in albums.items():
                if album.id == album_id:
                    return artist_mbid, rg_mbid
        return None

    # -- LidarrPort ---------------------------------------------------------
    def version(self) -> str:
        return self.version_string

    def check_version(self) -> str:
        self.record("check_version")
        return self.version_string

    def load_view(self, artist_mbids: Iterable[str] | None = None) -> LidarrView:
        wanted = list(dict.fromkeys(artist_mbids or ()))
        self.record("load_view", wanted)
        return LidarrView(
            artists=dict(self.artists),
            albums={mbid: dict(self.albums.get(mbid, {})) for mbid in wanted if mbid in self.artists},
            metadata_profiles=dict(self.metadata_profiles),
            quality_profiles=dict(self.quality_profiles),
            tags=dict(self.tags),
            version=self.version_string,
        )

    def load_albums(self, artist: LidarrArtist) -> dict[str, LidarrAlbum]:
        self.record("load_albums", artist.mbid)
        if self._down:
            raise LidarrError("fake: lidarr is down")
        return dict(self.albums.get(artist.mbid, {}))

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        return None

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        return None

    def search_release_group_candidates(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        return ()

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
        self.record("add_artist", artist_mbid)
        if artist_mbid in self.fail_add:
            raise LidarrMetadataError(f"fake: lidarr metadata failed adding {artist_mbid} (HTTP 503)")
        if artist_mbid in self.unknown_add:
            raise LidarrArtistUnknown(f"fake: Lidarr's metadata does not know this artist yet ({name}, {artist_mbid})")
        if artist_mbid in self.reject_add:
            raise LidarrError(f"fake: lidarr POST /artist: HTTP 400 - Root folder does not exist ({artist_mbid})")
        if artist_mbid in self.added_elsewhere and artist_mbid not in self.artists:
            self._next_artist_id += 1
            self.seed(
                LidarrArtist(
                    id=self._next_artist_id,
                    mbid=artist_mbid,
                    name=name,
                    monitored=True,
                    monitor_new_items="all",
                    metadata_profile_id=metadata_profile_id,
                    quality_profile_id=quality_profile_id,
                    tags=frozenset(tag_ids) if self.added_elsewhere[artist_mbid] else frozenset(),
                )
            )
        if artist_mbid in self.artists:
            raise LidarrArtistExists(
                f"fake: lidarr POST /artist: HTTP 400 - This artist has already been added ({artist_mbid})",
                self.artists[artist_mbid],
            )
        self._next_artist_id += 1
        artist = LidarrArtist(
            id=self._next_artist_id,
            mbid=artist_mbid,
            name=name,
            monitored=True,
            monitor_new_items="none",
            metadata_profile_id=metadata_profile_id,
            quality_profile_id=quality_profile_id,
            tags=frozenset(tag_ids),
            path=f"{root_folder.rstrip('/')}/{name}",
        )
        self.artists[artist_mbid] = replace(artist, monitored=False) if self.unmonitor_added_artists else artist
        self.albums.setdefault(artist_mbid, {})
        return artist

    def refresh_artist(self, artist: LidarrArtist, *, timeout_s: float = 300) -> None:
        self.record("refresh_artist", artist.mbid)
        self.refresh_timeouts[artist.mbid] = timeout_s
        if artist.mbid in self.fail_refresh:
            raise LidarrMetadataError(f"fake: RefreshArtist failed for {artist.mbid}")
        # Lidarr reads "Monitor New Albums" from the artist as stored when the refresh runs, not from
        # whatever the caller holds: an apply's view predates its own `set_artists_new_items_none`.
        stored = self.artists.get(artist.mbid, artist)
        existing = self.albums.setdefault(artist.mbid, {})
        newest = max((a.release_date or date.min for a in existing.values()), default=date.min)
        for rg in self.catalogue.get(artist.mbid, []):
            if rg.mbid in existing:
                continue
            self._next_album_id += 1
            existing[rg.mbid] = LidarrAlbum(
                id=self._next_album_id,
                rg_mbid=rg.mbid,
                artist_id=artist.id,
                artist_mbid=artist.mbid,
                title=rg.title,
                monitored=_monitors_new_album(stored.monitor_new_items, rg.first_release_date, newest),
                primary_type=rg.primary_type,
                secondary_types=rg.secondary_types,
                release_date=rg.first_release_date,
            )

    def set_albums_monitored(self, album_ids: Sequence[int], monitored: bool) -> None:
        self.record("set_albums_monitored", (list(album_ids), monitored))
        if monitored:
            self.monitor_batches += 1
            if self.fail_monitor_batch is not None and self.monitor_batches == self.fail_monitor_batch:
                self._flip(album_ids[: self.fail_monitor_batch_applies], monitored)
                self._down = self._down or self.down_after_monitor_failure
                raise LidarrError(f"fake: lidarr fell over on monitor batch {self.monitor_batches}")
        else:
            self.unmonitor_batches += 1
            if self.fail_unmonitor_batch is not None and self.unmonitor_batches == self.fail_unmonitor_batch:
                self._flip(album_ids[: self.fail_unmonitor_batch_applies], monitored)
                self._down = self._down or self.down_after_unmonitor_failure
                raise LidarrError(f"fake: lidarr fell over on unmonitor batch {self.unmonitor_batches}")
        self._flip(album_ids, monitored)

    def _flip(self, album_ids: Sequence[int], monitored: bool) -> None:
        for album_id in album_ids:
            found = self._album_by_id(album_id)
            if found is None:
                continue
            artist_mbid, rg_mbid = found
            album = self.albums[artist_mbid][rg_mbid]
            self.albums[artist_mbid][rg_mbid] = LidarrAlbum(
                id=album.id,
                rg_mbid=album.rg_mbid,
                artist_id=album.artist_id,
                artist_mbid=album.artist_mbid,
                title=album.title,
                monitored=monitored,
                primary_type=album.primary_type,
                secondary_types=album.secondary_types,
                release_date=album.release_date,
                track_file_count=album.track_file_count,
                size_on_disk=album.size_on_disk,
            )

    def set_artist_profile(self, artist: LidarrArtist, metadata_profile_id: int) -> None:
        self.record("set_artist_profile", (artist.mbid, metadata_profile_id))
        current = self.artists[artist.mbid]
        self.artists[artist.mbid] = LidarrArtist(
            id=current.id,
            mbid=current.mbid,
            name=current.name,
            monitored=current.monitored,
            monitor_new_items=current.monitor_new_items,
            metadata_profile_id=metadata_profile_id,
            quality_profile_id=current.quality_profile_id,
            tags=current.tags,
            path=current.path,
        )

    def set_artists_new_items_none(self, artist_ids: Sequence[int]) -> None:
        self.record("set_artists_new_items_none", list(artist_ids))
        for mbid, artist in list(self.artists.items()):
            if artist.id in artist_ids:
                self.artists[mbid] = LidarrArtist(
                    id=artist.id,
                    mbid=artist.mbid,
                    name=artist.name,
                    monitored=artist.monitored,
                    monitor_new_items="none",
                    metadata_profile_id=artist.metadata_profile_id,
                    quality_profile_id=artist.quality_profile_id,
                    tags=artist.tags,
                    path=artist.path,
                )

    def set_artists_monitored(self, artist_ids: Sequence[int]) -> None:
        self.record("set_artists_monitored", list(artist_ids))
        for mbid, artist in list(self.artists.items()):
            if artist.id in artist_ids:
                self.artists[mbid] = replace(artist, monitored=True)

    def ensure_tag(self, label: str) -> int:
        self.record("ensure_tag", label)
        return self.tags.setdefault(label, max(self.tags.values(), default=0) + 1)

    def ensure_metadata_profile(self, profile: Profile, name: str) -> int:
        self.record("ensure_metadata_profile", name)
        return self.metadata_profiles.setdefault(name, max(self.metadata_profiles.values(), default=0) + 1)

    def metadata_profile_details(self) -> list[dict[str, Any]]:
        self.record("metadata_profile_details")
        out: list[dict[str, Any]] = []
        for name, pid in self.metadata_profiles.items():
            override = self.metadata_profile_details_override.get(name)
            if override is not None:
                out.append({"id": pid, "name": name, **override})
                continue
            profile = Profile.FULL if name == "Full" else Profile.LEAN
            primary, secondary = expected_metadata_profile_types(profile)
            out.append(
                {
                    "id": pid,
                    "name": name,
                    "primaryAlbumTypes": [{"albumType": {"name": t}, "allowed": True} for t in sorted(primary)],
                    "secondaryAlbumTypes": [{"albumType": {"name": t}, "allowed": True} for t in sorted(secondary)],
                }
            )
        return out

    # -- LidarrShell extras -------------------------------------------------
    def root_folders(self) -> list[dict[str, Any]]:
        self.record("root_folders")
        monitor, new_items = self.root_folder_defaults
        return [
            {"id": i + 1, "path": p, "defaultMonitorOption": monitor, "defaultNewItemMonitorOption": new_items}
            for i, p in enumerate(self.root_folder_paths)
        ]

    def add_root_folder(self, path: str) -> dict[str, Any]:
        self.record("add_root_folder", path)
        if path not in self.root_folder_paths:
            self.root_folder_paths.append(path)
        return {"id": len(self.root_folder_paths), "path": path}

    def set_root_folder_defaults(self, path: str, monitor: str = "none", new_items: str = "none") -> None:
        self.record("set_root_folder_defaults", (path, monitor, new_items))

    def track_files(self, album_id: int) -> list[dict[str, Any]]:
        self.record("track_files", album_id)
        if album_id in self.fail_track_files:
            raise LidarrError(f"fake: lidarr GET /trackfile?albumId={album_id} failed")
        return list(self.track_file_rows.get(album_id, []))

    def delete_artist(self, artist_id: int, *, delete_files: bool = False) -> None:
        if self.fail_delete:
            raise LidarrError("fake: lidarr is down")
        self.record("delete_artist", (artist_id, delete_files))
        if delete_files:
            raise LidarrError("fake: refusing delete_files=True")

    def rescan_artist(self, artist_id: int) -> None:
        self.record("rescan_artist", artist_id)

    import_list_rows: list[dict[str, Any]] = field(default_factory=list)
    queued_commands: list[dict[str, Any]] = field(default_factory=list)

    def import_lists(self) -> list[dict[str, Any]]:
        self.record("import_lists")
        return list(self.import_list_rows)

    def command_queue(self) -> list[dict[str, Any]]:
        self.record("command_queue")
        return list(self.queued_commands)

    extra_records: dict[int, int] = field(default_factory=dict)
    """Track-file records Lidarr holds beyond the listed albums, that its statistic does not count."""
    fail_delete: bool = False
    fail_file_count: set[int] = field(default_factory=set)
    """Artist ids whose `artist_track_file_records` raises `LidarrError` (Lidarr can't count files)."""
    fail_track_files: set[int] = field(default_factory=set)
    """Album ids whose `track_files` raises `LidarrError` (Lidarr can't list an album's files)."""
    extra_files: dict[int, int] = field(default_factory=dict)
    """Files Lidarr holds for an artist beyond the seeded albums (simulates something left on disk)."""

    def artist_track_file_count(self, artist_id: int) -> int:
        """Lidarr's ``statistics.trackFileCount``: from the album fields, apart from the records, as
        Lidarr keeps them apart. likearr must never decide a removal on it."""
        self.record("artist_track_file_count", artist_id)
        mbid = next((a.mbid for a in self.artists.values() if a.id == artist_id), None)
        albums = self.albums.get(mbid, {}) if mbid is not None else {}
        return sum(a.track_file_count for a in albums.values()) + self.extra_files.get(artist_id, 0)

    def artist_track_file_records(self, artist_id: int) -> int:
        """``len(GET /trackfile?artistId=)``: the records listed per album, plus any `extra_files`."""
        self.record("artist_track_file_records", artist_id)
        if artist_id in self.fail_file_count:
            raise LidarrError(f"fake: lidarr GET /trackfile?artistId={artist_id} failed")
        mbid = next((a.mbid for a in self.artists.values() if a.id == artist_id), None)
        albums = self.albums.get(mbid, {}) if mbid is not None else {}
        listed = sum(
            len(self.track_file_rows[a.id]) if a.id in self.track_file_rows else a.track_file_count
            for a in albums.values()
        )
        return listed + self.extra_files.get(artist_id, 0) + self.extra_records.get(artist_id, 0)


# --------------------------------------------------------------------------- health sink


@dataclass(slots=True)
class CapturingSink:
    """A `HealthSink` that keeps every record instead of publishing it.

    `local` defaults to `True` so every existing test that registers a single `CapturingSink`
    (standing in for the always-on stdout sink) keeps seeing dry-run records unchanged; a test
    passes `local=False` to stand in for a retained sink like MQTT instead.
    """

    records: list[HealthRecord] = field(default_factory=list)
    local: bool = True

    def publish(self, record: HealthRecord) -> None:
        self.records.append(record)

    @property
    def last(self) -> HealthRecord:
        assert self.records, "no health record was published"
        return self.records[-1]


# --------------------------------------------------------------------------- config / context


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    """A `Config` pointing entirely inside `tmp_path`."""
    config = Config(
        lidarr=LidarrConfig(
            url="http://lidarr.test:8686",
            root_folder="/music",
            quality_profile="Standard",
            refresh_timeout_s=1.0,
        ),
        spotify=SpotifyConfig(token_file=tmp_path / "spotify-token.json"),
        musicbrainz=MusicBrainzConfig(contact="likearr@example.test"),
        state_db=tmp_path / "state.sqlite",
        rules=RulesConfig(),
        guards=GuardsConfig(),
        health=HealthConfig(stdout=False),
        lock_file=tmp_path / "likearr.lock",
    )
    for key, value in overrides.items():
        object.__setattr__(config, key, value)
    return config


def make_context(
    tmp_path: Path,
    *,
    source: FakeSource | None = None,
    lidarr: FakeLidarr | None = None,
    lookup: FakeLookup | None = None,
    sink: CapturingSink | None = None,
    sinks: list[Any] | None = None,
    config: Config | None = None,
    library: FakeLibrary | None = None,
    artist_details: Any = None,
    artist_links: Any = None,
    artist_relations: Any = None,
    composite: Any = None,
    first_applied: bool = True,
) -> Context:
    """A `Context` wired to fakes and a real SQLite state file under `tmp_path`.

    `sinks` overrides `sink` when more than one is needed (a local stdout-like sink plus one or
    more retained ones), and defaults to the single `sink` every other test uses.

    `first_applied` records a first hand apply in the state file, so a scheduled run
    applies as it did before the first-apply gate existed. Every test of what a scheduled run
    *does* wants that; a test of the gate itself passes ``first_applied=False``.
    """
    config = config or make_config(tmp_path)
    state = SqliteState(config.state_db)
    if first_applied:
        state.record_first_apply(NOW)
    return Context(
        config=config,
        state=state,
        lidarr=lidarr or FakeLidarr(),
        lookup=lookup or FakeLookup(),
        sinks=sinks if sinks is not None else [sink or CapturingSink()],
        source=source or FakeSource(),
        auth=None,
        library=library,
        composite=composite,
        artist_details=artist_details,
        artist_links=artist_links,
        artist_relations=artist_relations,
    )


@pytest.fixture
def sink() -> CapturingSink:
    return CapturingSink()


@pytest.fixture
def lidarr() -> FakeLidarr:
    return FakeLidarr()
