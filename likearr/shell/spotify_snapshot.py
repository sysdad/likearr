"""A cancelled scheduled run's Spotify read, saved so a redeploy costs zero Spotify calls.

The MusicBrainz work a plan does - the slow part, up to
an hour on a cold cache - is already saved lookup by lookup (`adapters/musicbrainz.py`), so a
re-fired run answers from cache with no network. The one thing not preserved was the Spotify read
itself: a few dozen requests, done once, up front, by `SpotifySource.read`. So a *scheduled* run
writes its `SourceSnapshot` here the moment the read finishes, and a later scheduled run reuses it
- skipping `ctx.source.read()` entirely - while it is under `MAX_AGE_S` old and the sources config
that shaped it (the followed/saved/liked switches, the playlist allowlist) has not changed.

Never written or read by a hand run (`run`, `run --apply`, a UI plan): only a *scheduled* run can be
cancelled mid-plan by a redeploy, so only a scheduled run has anything to gain from this, and a hand
run answering from a stale Spotify read would be the one place likearr silently trusts old data.

Deleted after any run - scheduled or not - completes the plan phase (`shell.plan.plan`'s own last
step): a completed plan means Spotify and Lidarr were both read fresh moments ago, so a snapshot
saved before it can only ever be older and is no longer worth keeping around.

Best-effort throughout: a failure to save or remove the file costs the next scheduled attempt its
head start, never a run's outcome, its exit code or its health record.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from likearr.config import Config
from likearr.fsio import write_atomic
from likearr.models import SourceSnapshot
from likearr.shell.last_run import remove_stale_temps, snapshot_from_dict, snapshot_to_dict

__all__ = ["MAX_AGE_S", "delete_snapshot", "read_snapshot", "snapshot_path", "write_snapshot"]

log = logging.getLogger(__name__)

_VERSION = 1

MAX_AGE_S = 30 * 60.0
"""A saved Spotify read is reused only under 30 minutes old."""


def snapshot_path(config: Config) -> Path:
    """`<data>/ui/spotify-snapshot.json`, beside the web UI's job store and `last-run.json`."""
    return config.state_db.parent / "ui" / "spotify-snapshot.json"


def sources_digest(config: Config) -> str:
    """A stable hash of the switches a Spotify read depends on: the saved snapshot is reused only
    while this still matches, so a sources config change (a playlist added, a switch flipped)
    never serves a read that no longer reflects what is configured."""
    payload = {
        "followed_artists": config.spotify.followed_artists,
        "saved_albums": config.spotify.saved_albums,
        "liked_tracks": config.spotify.liked_tracks,
        "playlists": sorted(config.spotify.playlists),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def write_snapshot(config: Config, snapshot: SourceSnapshot) -> None:
    """Save `snapshot` for a later scheduled run to reuse: 0600, atomic. Called only for a
    scheduled run's own fresh read, right after it finishes."""
    path = snapshot_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        remove_stale_temps(path)
        doc = {
            "version": _VERSION,
            "read_at": snapshot.fetched_at.isoformat(),
            "sources_digest": sources_digest(config),
            "snapshot": snapshot_to_dict(snapshot),
        }
        write_atomic(path, json.dumps(doc, separators=(",", ":")), mode=0o600)
    except OSError:
        log.warning("could not save this Spotify read at %s for a redeploy to reuse", path, exc_info=True)


def read_snapshot(config: Config, *, now: datetime) -> SourceSnapshot | None:
    """The saved snapshot, if it is under `MAX_AGE_S` old and its sources digest still matches
    `config`; else `None`. Never raises, and never touches the file."""
    path = snapshot_path(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return None
    try:
        read_at = datetime.fromisoformat(str(raw["read_at"]))
    except (KeyError, ValueError):
        return None
    age = (now - read_at).total_seconds()
    if age < 0 or age > MAX_AGE_S:
        return None
    if raw.get("sources_digest") != sources_digest(config):
        return None
    try:
        return snapshot_from_dict(raw["snapshot"])
    except (KeyError, ValueError, TypeError):
        return None


def delete_snapshot(config: Config) -> None:
    """Remove the saved snapshot, if there is one. Called after any run completes the plan phase."""
    try:
        snapshot_path(config).unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove the saved Spotify read at %s", snapshot_path(config), exc_info=True)
