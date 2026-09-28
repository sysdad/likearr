"""Wiring: turn a config file into the objects every command needs.

This is the only module that constructs adapters. Everything downstream (`shell.run`,
`shell.commands`, `shell.setup_commands`, `shell.prune_commands`) takes a :class:`Context` and
never reaches for the network, the filesystem or the environment itself, which is what makes the
shell testable with in-memory fakes.

Secrets come from the environment only (see :mod:`likearr.config`); nothing here reads a
credential out of the TOML file, and nothing here prints one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from likearr.adapters.health import build_sinks
from likearr.adapters.http import build_client
from likearr.adapters.lidarr import LIDARR_TIMEOUT, LidarrClient
from likearr.adapters.lookup import CompositeLookup
from likearr.adapters.musicbrainz import MusicBrainzLookup, build_user_agent
from likearr.adapters.spotify import SpotifyAuth, SpotifySource
from likearr.adapters.spotify_library import OwnedPlaylist, PlaylistEntry, SpotifyLibrary
from likearr.adapters.state_sqlite import SqliteState
from likearr.config import LIDARR_URL_ENV, Config, ConfigError, load_config
from likearr.logging_setup import setup_logging
from likearr.ports import (
    ArtistDetails,
    ArtistLinks,
    CreditRelations,
    HealthSink,
    LidarrPort,
    MetadataLookup,
    SourcePort,
    SpotifyLibraryPort,
)

__all__ = ["Context", "LidarrShell", "SpotifyLibraryShell", "build_context"]

log = logging.getLogger(__name__)


class LidarrShell(LidarrPort, Protocol):
    """:class:`likearr.ports.LidarrPort` plus the calls only the shell's commands make.

    `LidarrPort` is the contract the pure core was written against and stays minimal. `doctor`,
    `setup-profiles` and `prune-stage` need a handful of extra endpoints, and declaring them here
    keeps them type-checked without widening the port the core depends on.
    """

    def check_version(self) -> str: ...

    def root_folders(self) -> list[dict[str, Any]]: ...

    def metadata_profile_details(self) -> list[dict[str, Any]]: ...

    def add_root_folder(self, path: str) -> dict[str, Any]: ...

    def set_root_folder_defaults(self, path: str, monitor: str = ..., new_items: str = ...) -> None: ...

    def track_files(self, album_id: int) -> list[dict[str, Any]]: ...

    def artist_track_file_records(self, artist_id: int) -> int:
        """Live count of the track-file records Lidarr holds for the artist, across every album."""
        ...

    def delete_artist(self, artist_id: int, *, delete_files: bool = False) -> None:
        """Remove the artist row only. `delete_files=True` is always refused: likearr never deletes files."""
        ...

    def rescan_artist(self, artist_id: int) -> None:
        """Queue a rescan of the artist's own folder (never a root-wide scan); do not wait for it."""
        ...

    def import_lists(self) -> list[dict[str, Any]]: ...

    def command_queue(self) -> list[dict[str, Any]]:
        """Commands still queued or running."""
        ...


class SpotifyLibraryShell(SpotifyLibraryPort, Protocol):
    """:class:`likearr.ports.SpotifyLibraryPort` plus the listing only the shell's commands use.

    Same reasoning as `LidarrShell`: `likearr playlists` (and, through it, the web UI's playlist
    picker) needs the user's own playlists, which no part of the core ever asks for, so the port
    `promote-save` was written against stays as it is.
    """

    def owned_playlists(self) -> list[OwnedPlaylist]:
        """Every playlist a run can read (owned, or collaborative with the scope), sorted by name.
        Read-only."""
        ...

    def all_playlists(self) -> list[PlaylistEntry]:
        """Every playlist `GET /me/playlists` lists, owned and not, sorted by name. Read-only."""
        ...


@dataclass(slots=True)
class Context:
    """Everything a command needs, already wired together.

    `source` and `auth` are ``None`` when Spotify was not requested (``doctor --no-spotify``) or
    when its configuration is unusable; commands that need them say so rather than crashing.
    """

    config: Config
    config_path: Path
    state: SqliteState
    lidarr: LidarrShell
    lookup: MetadataLookup
    sinks: list[HealthSink]
    source: SourcePort | None = None
    auth: SpotifyAuth | None = None
    library: SpotifyLibraryShell | None = None
    """Spotify's library side: `promote-save`'s writes and `playlists`' listing. ``None`` whenever
    `source` is, and for the same reasons; every other command runs without it."""
    composite: CompositeLookup | None = None
    """The real :class:`CompositeLookup` when one was built, for its ``mb_ok`` flags (and, for
    `promote-save`, its :class:`~likearr.ports.ReleaseLinkLookup` side)."""
    artist_links: ArtistLinks | None = None
    """Where a followed artist's MusicBrainz identity comes from - the Spotify URL relationship.
    Normally the composite lookup; a separate field so a test can supply just this."""
    artist_details: ArtistDetails | None = None
    """Where a name collision's MusicBrainz disambiguations come from. Normally the composite
    lookup; a separate field so the dependency is explicit and a test can supply just this."""
    artist_relations: CreditRelations | None = None
    """Where a liked track's credit is joined to MusicBrainz's by a relationship.
    Normally the composite lookup; ``None`` runs the resolver without that rule."""
    spotify_error: str | None = None
    """Why Spotify is unavailable, when it is. `doctor` reports it; `run` refuses without it."""
    _closeables: list[Any] = field(default_factory=list)

    @property
    def lock_path(self) -> Path:
        """The run lock file: the configured one, or the state DB with a ``.lock`` suffix."""
        return self.config.lock_path

    def close(self) -> None:
        """Close every client and connection, never raising."""
        for item in reversed(self._closeables):
            try:
                item.close()
            except Exception:  # pragma: no cover - close() failures must not mask a real error
                log.debug("failed to close %s", type(item).__name__, exc_info=True)
        self._closeables.clear()

    def __enter__(self) -> Context:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_context(
    config_path: Path | str,
    *,
    need_spotify: bool = True,
    verbose: bool = False,
    sinks: Sequence[HealthSink] | None = None,
) -> Context:
    """Load the config and construct every adapter.

    Args:
        need_spotify: when False, Spotify is not constructed at all and `Context.source` is
            ``None``. When True, a Spotify configuration problem (a missing client id, say) is
            still not fatal here - it is recorded in `Context.spotify_error` so `doctor` can
            report it as a WARN instead of dying before it has checked anything else.
        verbose: DEBUG logging.
        sinks: override the configured health sinks (tests).

    Raises:
        ConfigError: the config file is missing or invalid, or something likearr cannot run
            without (the Lidarr API key, the state database) is unusable.
    """
    setup_logging(verbose)
    resolved_config_path = Path(config_path).resolve()
    config = load_config(config_path)
    closeables: list[Any] = []

    try:
        state = SqliteState(config.state_db)
    except Exception as exc:
        raise ConfigError(f"cannot open the state database at {config.state_db}: {exc}") from exc
    closeables.append(state)

    if not config.lidarr.url:
        # Checked before anything is opened, like the API key: likearr has nowhere to send it.
        _close_all(closeables)
        raise ConfigError(f"{LIDARR_URL_ENV} is not set")

    try:
        # The API key travels in X-Api-Key, which httpx does not strip on a cross-origin
        # redirect, so no request (redirect hops included) may leave Lidarr's own origin.
        lidarr_client = build_client(timeout=LIDARR_TIMEOUT, pinned_origin=config.lidarr.url)
        closeables.append(lidarr_client)
        lidarr = LidarrClient(config.lidarr, lidarr_client)

        mb_client = build_client(user_agent=build_user_agent(config.musicbrainz.contact))
        closeables.append(mb_client)
        musicbrainz = MusicBrainzLookup(
            config.musicbrainz,
            mb_client,
            cache_path=config.state_db,
            max_age_days=config.musicbrainz.positive_cache_days,
        )
        closeables.append(musicbrainz)
        # A snapshot, not a live read: nothing else writes to this table while the run is in
        # progress, and `plan` decides at the *end* of the run whether to write anything back
        # (only when at least one other Lidarr metadata lookup succeeded this run).
        composite = CompositeLookup(
            musicbrainz,
            lidarr,
            negative_cache=state.lidarr_negative_cache(),
            negative_cache_days=config.musicbrainz.negative_cache_days,
        )
    except ConfigError:
        _close_all(closeables)
        raise
    except Exception as exc:
        _close_all(closeables)
        raise ConfigError(f"cannot reach the configured services: {exc}") from exc

    auth: SpotifyAuth | None = None
    source: SourcePort | None = None
    library: SpotifyLibrary | None = None
    spotify_error: str | None = None
    if need_spotify:
        try:
            spotify_client = build_client()
            closeables.append(spotify_client)
            auth = SpotifyAuth(config.spotify, spotify_client)
            source = SpotifySource(config.spotify, auth, spotify_client)
            # Its own search cache lives in the state DB, on its own connection, exactly like the
            # MusicBrainz cache - so a re-plan after a quota error repeats no search calls.
            library = SpotifyLibrary(auth, spotify_client, cache_path=config.state_db)
            closeables.append(library)
        except ConfigError as exc:
            spotify_error = str(exc)
            auth, source, library = None, None, None

    return Context(
        config=config,
        config_path=resolved_config_path,
        state=state,
        lidarr=lidarr,
        lookup=composite,
        sinks=list(sinks) if sinks is not None else build_sinks(config.health),
        source=source,
        auth=auth,
        library=library,
        composite=composite,
        artist_links=composite,
        artist_details=composite,
        artist_relations=composite,
        spotify_error=spotify_error,
        _closeables=closeables,
    )


def _close_all(closeables: list[Any]) -> None:
    for item in reversed(closeables):
        try:
            item.close()
        except Exception:  # pragma: no cover
            log.debug("failed to close %s", type(item).__name__, exc_info=True)
    closeables.clear()
