"""What the last run saw, kept so `explain` can answer at once, without asking anyone again.

A live `explain` re-reads all of Spotify and Lidarr: minutes, for one question. Almost every
question is about what the last run did, and the last run already knew the answer. So at the end of
every `likearr run` that planned, the inputs `core.explain` reads are written here, beside the state
database: the Spotify intents with their names and years (the state keeps only ids), what each
resolved to - unmatched ones too, which the resolution cache does not keep - the desired state, the
slice of Lidarr's view the desired and owned releases touch, and the name collisions. Ownership is
already in the state database and is read from it, as it stands.

For an apply, the view is the one the run planned from with the applied changes laid on top - what
was monitored, unmonitored and added, less what Lidarr refused - so "as of the last run" means after
it, not before. File counts are the ones the run read: downloads land later.

Writing it is best-effort and changes nothing else about a run: a failure is logged as a warning,
never raised, and never touches the exit code, the health record or the state database. A missing,
unreadable or older-format file reads as ``None``, and `explain` says to run once or ask live.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from likearr.adapters.musicbrainz import CachedDisambiguations
from likearr.adapters.state_sqlite import _release_group_from_json, _release_group_to_json
from likearr.config import Config
from likearr.core.explain import Explanation, explain_report
from likearr.fsio import write_atomic
from likearr.models import (
    AlbumIntent,
    ArtistIntent,
    ArtistResolution,
    DesiredRelease,
    DesiredState,
    Diff,
    Guard,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    NameCollision,
    OwnedRelease,
    PrimaryType,
    Profile,
    ReleaseKey,
    Resolution,
    SecondaryType,
    SourceSnapshot,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.shell.diff_io import (
    _collision_from_dict,
    _collision_to_dict,
    _key_from_dict,
    _key_to_dict,
    _reason_from_dict,
    _reason_to_dict,
    _reasons_from_list,
    _reasons_to_list,
    _unresolved_from_dict,
    _unresolved_to_dict,
)

__all__ = [
    "FACTS_VERSION",
    "STALE_TEMP_AGE_S",
    "LastRun",
    "explain_from_last_run",
    "facts_path",
    "last_run_facts",
    "read_last_run",
    "remove_stale_temps",
    "snapshot_from_dict",
    "snapshot_to_dict",
    "write_last_run",
]

log = logging.getLogger(__name__)

FACTS_VERSION = 1
"""Bumped when the shape changes incompatibly; an older file then reads as ``None``."""


@dataclass(frozen=True, slots=True)
class LastRun:
    """`core.explain`'s inputs as the last run left them, less what the state database holds."""

    ran_at: datetime
    kind: str
    """``apply`` (the view carries its changes), ``dry run``, or ``refused apply``: an apply that
    found its reviewed plan stale and changed nothing, recorded with the fresh plan it made."""
    snapshot: SourceSnapshot
    resolutions: dict[str, Resolution]
    artist_resolutions: dict[str, ArtistResolution]
    desired: DesiredState
    view: LidarrView
    collisions: tuple[NameCollision, ...]
    unmonitor: frozenset[ReleaseKey] | None = None
    """The plan's unmonitors after its guards; ``None`` from a file written before they were kept."""
    monitor: frozenset[ReleaseKey] | None = None
    """The plan's monitors; ``None`` from a file written before they were kept."""
    guards: tuple[Guard, ...] = ()

    @property
    def applied(self) -> bool:
        return self.kind == "apply"

    @property
    def label(self) -> str:
        """ "an apply", "a dry run", "an apply that was refused (nothing changed)"."""
        return {"apply": "an apply", "dry run": "a dry run"}.get(
            self.kind, "an apply that was refused as stale (nothing changed)"
        )


def facts_path(config: Config) -> Path:
    """``last-run.json`` beside the state database."""
    return config.state_db.parent / "last-run.json"


def explain_from_last_run(
    query: str,
    *,
    config: Config,
    owned: Mapping[ReleaseKey, OwnedRelease],
    playlist_names: Mapping[str, str],
    limit: int | None = None,
    read: Callable[[Path], LastRun | None] | None = None,
) -> tuple[Explanation, LastRun] | None:
    """`core.explain` over the last run's facts: no Spotify, no Lidarr, no MusicBrainz request.

    Ownership is today's (`owned`, from the state database); disambiguations come from the
    MusicBrainz cache alone. ``None`` when no run has been recorded that this version can read.
    Safe in the web server: it reads two local files and writes nothing. `read` replaces
    `read_last_run`, for the web server's cached copy.
    """
    last = (read or read_last_run)(facts_path(config))
    if last is None:
        return None
    with CachedDisambiguations(config.state_db) as disambiguation:
        report = explain_report(
            query,
            desired=last.desired,
            owned=owned,
            view=last.view,
            resolutions=last.resolutions,
            artist_resolutions=last.artist_resolutions,
            snapshot=last.snapshot,
            collisions=last.collisions,
            disambiguation=disambiguation,
            playlist_names=playlist_names,
            limit=limit,
            unmonitor=last.unmonitor,
            guards=last.guards,
        )
    return report, last


# ---------------------------------------------------------------- building


class _Applied(Protocol):
    """What an apply did, as `last_run_facts` needs it: `ApplyResult` without importing `shell.run`."""

    skipped_artists: list[str]
    unknown_artists: list[str]
    foreign_artists: list[str]
    unmapped_in_lidarr: list[str]


def last_run_facts(
    *,
    ran_at: datetime,
    snapshot: SourceSnapshot,
    resolutions: Mapping[str, Resolution],
    artist_resolutions: Mapping[str, ArtistResolution],
    desired: DesiredState,
    view: LidarrView,
    owned_keys: Iterable[ReleaseKey],
    collisions: Iterable[NameCollision],
    unmonitor: Iterable[ReleaseKey] = (),
    monitor: Iterable[ReleaseKey] = (),
    guards: Iterable[Guard] = (),
    executed: Diff | None = None,
    applied: _Applied | None = None,
    refused: bool = False,
) -> dict[str, Any]:
    """The document to write. With `executed` and `applied`, the view carries the apply's changes;
    `refused` marks an apply that found its plan stale. `unmonitor` and `guards` are the plan's,
    so Explain says a release is unmonitored only when the plan does it."""
    if executed is not None and applied is not None:
        view = _after_apply(view, executed, applied)
    keys = set(desired.releases) | set(owned_keys)
    artist_mbids = set(desired.artists) | {k.artist_mbid for k in keys}
    albums = [album for key in sorted(keys, key=lambda k: (k.artist_mbid, k.rg_mbid)) if (album := view.album(key))]
    return {
        "version": FACTS_VERSION,
        "ran_at": ran_at.isoformat(),
        "kind": "apply" if executed is not None else "refused apply" if refused else "dry run",
        "snapshot": snapshot_to_dict(snapshot),
        "resolutions": [_unresolved_to_dict(r) for _, r in sorted(resolutions.items())],
        "artist_resolutions": [_unresolved_to_dict(r) for _, r in sorted(artist_resolutions.items())],
        "desired": _desired_to_dict(desired),
        "view": {
            "artists": [_artist_to_dict(view.artists[m]) for m in sorted(artist_mbids) if m in view.artists],
            "albums": [_album_to_dict(a) for a in albums],
        },
        "collisions": [_collision_to_dict(c) for c in collisions],
        "unmonitor": [_key_to_dict(k) for k in sorted(unmonitor, key=lambda k: (k.artist_mbid, k.rg_mbid))],
        "monitor": [_key_to_dict(k) for k in sorted(monitor, key=lambda k: (k.artist_mbid, k.rg_mbid))],
        "guards": [
            {"code": g.code, "message": g.message, "blocked_unmonitors": g.blocked_unmonitors, "subject": g.subject}
            for g in guards
        ],
    }


def _after_apply(view: LidarrView, executed: Diff, applied: _Applied) -> LidarrView:
    # An artist Lidarr refused to add is skipped the same way: it is not in Lidarr at all.
    # One someone else added first is in Lidarr, but none of the plan's changes to it were made.
    skipped = set(applied.skipped_artists) | set(applied.unknown_artists) | set(applied.foreign_artists)
    refused = set(applied.unmapped_in_lidarr)
    artists = dict(view.artists)
    albums = {mbid: dict(by_rg) for mbid, by_rg in view.albums.items()}
    for add in executed.add_artists:
        if add.artist_mbid not in skipped and add.artist_mbid not in artists:
            artists[add.artist_mbid] = LidarrArtist(
                id=0,
                mbid=add.artist_mbid,
                name=add.name,
                monitored=True,
                monitor_new_items="none",
                metadata_profile_id=0,
                quality_profile_id=0,
                tags=frozenset(),
            )

    def set_monitored(key: ReleaseKey, title: str, monitored: bool) -> None:
        album = albums.get(key.artist_mbid, {}).get(key.rg_mbid)
        if album is None:
            album = LidarrAlbum(
                id=0,
                rg_mbid=key.rg_mbid,
                artist_id=0,
                artist_mbid=key.artist_mbid,
                title=title,
                monitored=monitored,
                primary_type=None,
                secondary_types=frozenset(),
                release_date=None,
            )
        albums.setdefault(key.artist_mbid, {})[key.rg_mbid] = replace(album, monitored=monitored)

    for item in executed.monitor:
        if item.key.artist_mbid not in skipped and f"{item.key.artist_mbid}/{item.key.rg_mbid}" not in refused:
            set_monitored(item.key, item.title, True)
    for item in executed.unmonitor:
        if item.key.artist_mbid not in skipped:
            set_monitored(item.key, item.title, False)
    return replace(view, artists=artists, albums=albums)


# ---------------------------------------------------------------- reading and writing


def write_last_run(path: Path, facts: Mapping[str, Any]) -> None:
    """Write `facts` atomically: a reader sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_stale_temps(path)
    write_atomic(path, json.dumps(facts, separators=(",", ":")), mode=0o600)


STALE_TEMP_AGE_S = 3600
"""A temp file this old was left by a run that died mid-write: no live writer takes this long."""


def remove_stale_temps(path: Path) -> None:
    """Clear `.{path.name}.*.tmp` files older than `STALE_TEMP_AGE_S` beside `path`: what a writer
    that died mid-write (`write_last_run`, `shell.spotify_snapshot.write_snapshot`) left behind."""
    now = time.time()
    for temp in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            if now - temp.stat().st_mtime > STALE_TEMP_AGE_S:
                temp.unlink()
        except OSError:
            continue


def record_last_run(path: Path, build: Callable[[], Mapping[str, Any]]) -> None:
    """Build and write the facts, never raising: a failure costs `explain` its fast answer, no more."""
    try:
        write_last_run(path, build())
    except Exception:
        log.warning("could not record this run for explain at %s", path, exc_info=True)


def read_last_run(path: Path) -> LastRun | None:
    """The last run's facts, or ``None`` when there are none this version can read. Never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != FACTS_VERSION:
            return None
        view = raw["view"]
        resolutions = [_unresolved_from_dict(r) for r in [*raw["resolutions"], *raw["artist_resolutions"]]]
        albums: dict[str, dict[str, LidarrAlbum]] = {}
        for album in (_album_from_dict(a) for a in view["albums"]):
            albums.setdefault(album.artist_mbid, {})[album.rg_mbid] = album
        return LastRun(
            ran_at=datetime.fromisoformat(raw["ran_at"]),
            kind=str(raw.get("kind") or ("apply" if raw.get("applied") else "dry run")),
            snapshot=snapshot_from_dict(raw["snapshot"]),
            resolutions={r.intent_key: r for r in resolutions if isinstance(r, Resolution)},
            artist_resolutions={r.intent_key: r for r in resolutions if isinstance(r, ArtistResolution)},
            desired=_desired_from_dict(raw["desired"]),
            view=LidarrView(
                artists={a.mbid: a for a in (_artist_from_dict(d) for d in view["artists"])},
                albums=albums,
                metadata_profiles={},
                quality_profiles={},
                tags={},
            ),
            collisions=tuple(_collision_from_dict(c) for c in raw["collisions"]),
            unmonitor=frozenset(_key_from_dict(k) for k in raw["unmonitor"]) if "unmonitor" in raw else None,
            monitor=frozenset(_key_from_dict(k) for k in raw["monitor"]) if "monitor" in raw else None,
            guards=tuple(
                Guard(
                    code=str(g["code"]),
                    message=str(g["message"]),
                    blocked_unmonitors=int(g.get("blocked_unmonitors") or 0),
                    subject=str(g.get("subject") or ""),
                )
                for g in raw.get("guards", [])
            ),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------- the Spotify side


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_date(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _album_ref_to_dict(album: SpotifyAlbumRef) -> dict[str, Any]:
    return {
        "spotify_id": album.spotify_id,
        "name": album.name,
        "artist_names": list(album.artist_names),
        "upc": album.upc,
        "album_type": album.album_type,
        "release_date": _date(album.release_date),
    }


def _album_ref_from_dict(raw: Mapping[str, Any]) -> SpotifyAlbumRef:
    return SpotifyAlbumRef(
        spotify_id=str(raw["spotify_id"]),
        name=str(raw["name"]),
        artist_names=tuple(str(n) for n in raw["artist_names"]),
        upc=raw.get("upc"),
        album_type=str(raw.get("album_type") or ""),
        release_date=_parse_date(raw.get("release_date")),
    )


def snapshot_to_dict(snapshot: SourceSnapshot) -> dict[str, Any]:
    """A `SourceSnapshot` as JSON, losslessly: every field round-trips through `snapshot_from_dict`
    (see `tests/shell/test_last_run.py`). Shared with `shell.spotify_snapshot`,
    which saves a scheduled run's read for a redeploy to reuse."""
    return {
        "fetched_at": snapshot.fetched_at.isoformat(),
        "counts": dict(snapshot.counts),
        "schema_ok": snapshot.schema_ok,
        "schema_warnings": list(snapshot.schema_warnings),
        "artists": [
            {"spotify_id": a.spotify_id, "name": a.name, "reason": _reason_to_dict(a.reason)} for a in snapshot.artists
        ],
        "albums": [
            {"album": _album_ref_to_dict(a.album), "reason": _reason_to_dict(a.reason)} for a in snapshot.albums
        ],
        "tracks": [
            {
                "spotify_id": t.spotify_id,
                "name": t.name,
                "isrc": t.isrc,
                "artist_names": list(t.artist_names),
                "album": _album_ref_to_dict(t.album),
                "added_at": t.added_at.isoformat() if t.added_at is not None else None,
                "reason": _reason_to_dict(t.reason),
            }
            for t in snapshot.tracks
        ],
    }


def snapshot_from_dict(raw: Mapping[str, Any]) -> SourceSnapshot:
    """The inverse of `snapshot_to_dict`. `schema_ok`/`schema_warnings` default true/empty for a
    file written before they were included, which is also the harmless answer for `last-run.json`."""
    return SourceSnapshot(
        fetched_at=datetime.fromisoformat(raw["fetched_at"]),
        artists=tuple(
            ArtistIntent(spotify_id=str(a["spotify_id"]), name=str(a["name"]), reason=_reason_from_dict(a["reason"]))
            for a in raw["artists"]
        ),
        albums=tuple(
            AlbumIntent(album=_album_ref_from_dict(a["album"]), reason=_reason_from_dict(a["reason"]))
            for a in raw["albums"]
        ),
        tracks=tuple(
            TrackIntent(
                spotify_id=str(t["spotify_id"]),
                name=str(t["name"]),
                isrc=t.get("isrc"),
                artist_names=tuple(str(n) for n in t["artist_names"]),
                album=_album_ref_from_dict(t["album"]),
                added_at=datetime.fromisoformat(t["added_at"]) if t.get("added_at") else None,
                reason=_reason_from_dict(t["reason"]),
            )
            for t in raw["tracks"]
        ),
        counts={str(k): int(v) for k, v in raw["counts"].items()},
        schema_ok=bool(raw.get("schema_ok", True)),
        schema_warnings=tuple(str(w) for w in raw.get("schema_warnings", ())),
    )


# ---------------------------------------------------------------- the desired state


def _desired_to_dict(desired: DesiredState) -> dict[str, Any]:
    return {
        "releases": [
            {
                "key": _key_to_dict(r.key),
                "release_group": _release_group_to_json(r.release_group),
                "reasons": _reasons_to_list(frozenset(r.reasons)),
                "steps": dict(r.steps),
            }
            for r in sorted(desired.releases.values(), key=lambda r: (r.key.artist_mbid, r.key.rg_mbid))
        ],
        "artists": dict(desired.artists),
        "followed_artists": sorted(desired.followed_artists),
        "profile_needs": {m: str(p) for m, p in desired.profile_needs.items()},
        "followed_counts": dict(desired.followed_counts),
    }


def _desired_from_dict(raw: Mapping[str, Any]) -> DesiredState:
    releases: dict[ReleaseKey, DesiredRelease] = {}
    for item in raw["releases"]:
        key = _key_from_dict(item["key"])
        release_group = _release_group_from_json(item["release_group"])
        if release_group is None:
            raise ValueError("a desired release with no release group")
        releases[key] = DesiredRelease(
            key=key,
            release_group=release_group,
            reasons=set(_reasons_from_list(item["reasons"])),
            steps={str(k): str(v) for k, v in item["steps"].items()},
        )
    return DesiredState(
        releases=releases,
        artists={str(k): str(v) for k, v in raw["artists"].items()},
        followed_artists={str(m) for m in raw["followed_artists"]},
        pending=[],
        unmapped=[],
        profile_needs={str(m): Profile(p) for m, p in raw["profile_needs"].items()},
        followed_counts={str(m): int(c) for m, c in raw["followed_counts"].items()},
    )


# ---------------------------------------------------------------- the Lidarr side


def _artist_to_dict(artist: LidarrArtist) -> dict[str, Any]:
    return {
        "id": artist.id,
        "mbid": artist.mbid,
        "name": artist.name,
        "monitored": artist.monitored,
        "monitor_new_items": artist.monitor_new_items,
        "metadata_profile_id": artist.metadata_profile_id,
        "quality_profile_id": artist.quality_profile_id,
    }


def _artist_from_dict(raw: Mapping[str, Any]) -> LidarrArtist:
    return LidarrArtist(
        id=int(raw["id"]),
        mbid=str(raw["mbid"]),
        name=str(raw["name"]),
        monitored=bool(raw["monitored"]),
        monitor_new_items=str(raw["monitor_new_items"]),
        metadata_profile_id=int(raw["metadata_profile_id"]),
        quality_profile_id=int(raw["quality_profile_id"]),
        tags=frozenset(),
    )


def _album_to_dict(album: LidarrAlbum) -> dict[str, Any]:
    return {
        "id": album.id,
        "rg_mbid": album.rg_mbid,
        "artist_id": album.artist_id,
        "artist_mbid": album.artist_mbid,
        "title": album.title,
        "monitored": album.monitored,
        "primary_type": album.primary_type.value if album.primary_type is not None else None,
        "secondary_types": sorted(t.value for t in album.secondary_types),
        "release_date": _date(album.release_date),
        "track_file_count": album.track_file_count,
    }


def _album_from_dict(raw: Mapping[str, Any]) -> LidarrAlbum:
    primary = raw.get("primary_type")
    return LidarrAlbum(
        id=int(raw["id"]),
        rg_mbid=str(raw["rg_mbid"]),
        artist_id=int(raw["artist_id"]),
        artist_mbid=str(raw["artist_mbid"]),
        title=str(raw["title"]),
        monitored=bool(raw["monitored"]),
        primary_type=PrimaryType(primary) if primary else None,
        secondary_types=frozenset(SecondaryType(t) for t in raw.get("secondary_types") or []),
        release_date=_parse_date(raw.get("release_date")),
        track_file_count=int(raw.get("track_file_count") or 0),
    )
