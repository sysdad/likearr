"""Spotify playlist names by id, kept in a small file beside the config.

Playlists are configured by id, and an id means nothing to a person. The names come from
`likearr playlists --json`, which the web UI runs when asked; each successful answer is merged
into this file, so a name, once known, is shown everywhere a playlist appears - Settings, Status,
Explain - without asking Spotify again. It never expires for display: a renamed playlist shows its
old name until the next refresh, which beats an id. A playlist that left the account keeps its last
known name.

Neutral ground: the web UI writes it, and the CLI's `explain` reads it, so it imports nothing from
either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from likearr.fsio import write_atomic

__all__ = [
    "COLLABORATIVE_REAUTH_REASON",
    "NOT_OWNED_CONFIGURED_NOTE",
    "NOT_OWNED_REASON",
    "NOT_OWNED_WORKAROUND",
    "NamesCache",
    "names_path",
    "playlist_url",
    "read_names",
    "write_names",
]

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}")

NOT_OWNED_REASON = "Spotify doesn't share this playlist's songs with a personal app"
"""Why a playlist the picker lists cannot be a source, in one fixed wording.
The one place this sentence is written - the picker, `likearr playlists`, the README and
`docs/dev/DESIGN.md` all say this, not their own paraphrase."""

NOT_OWNED_WORKAROUND = "like the songs you want, or copy them into a playlist you own"
"""What to do instead, reused everywhere `NOT_OWNED_REASON` is."""

COLLABORATIVE_REAUTH_REASON = (
    "you collaborate on this playlist, but Spotify was connected before likearr asked to read "
    "collaborative playlists: re-authorize Spotify (Settings, or `likearr auth`), then refresh this list"
)
"""Why a collaborative playlist someone else owns is not offered yet: the
stored token predates ``playlist-read-collaborative``. A re-authorization fixes it; a copy is not
needed, so this replaces `NOT_OWNED_REASON` and its workaround for such a playlist."""

NOT_OWNED_CONFIGURED_NOTE = (
    "In your settings, but Spotify won't share its songs with a personal app: nothing is read from it"
)
"""The picker's note for a playlist that is `[spotify].playlists` already, but not owned - kept
through an unrelated save (likearr never changes what you set without saying so) rather than
silently dropped, unlike one you have never added (see `NOT_OWNED_REASON`)."""


@dataclass(frozen=True, slots=True)
class NamesCache:
    names: dict[str, str] = field(default_factory=dict)
    fetched_at: datetime | None = None
    """When the newest answer merged in was fetched; ``None`` when nothing has been yet."""
    not_owned: frozenset[str] = frozenset()
    """Ids the newest successful fetch listed but marked not readable (not owned, and not a
    collaborative one the token may read) - replaced whole by every fetch, unlike `names`: it is
    what a save refuses, and only the latest listing is trusted to say a
    playlist is *currently* unreadable. The file key stays ``not_owned``."""
    needs_reauth: frozenset[str] = frozenset()
    """The subset of `not_owned` a re-authorization would make readable: collaborative playlists
    listed by a token without ``playlist-read-collaborative``. Replaced whole too."""


def names_path(config_path: Path) -> Path:
    """`<config dir>/ui/playlist-names.json`, beside the web UI's job store."""
    return config_path.parent / "ui" / "playlist-names.json"


def read_names(path: Path) -> NamesCache:
    """The cache, or an empty one when it is missing or will not read. Never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NamesCache()
    if not isinstance(raw, dict) or not isinstance(raw.get("names"), dict):
        return NamesCache()
    names = {str(k): str(v) for k, v in raw["names"].items() if isinstance(v, str) and v}
    try:
        fetched_at = datetime.fromisoformat(str(raw.get("fetched_at")))
    except ValueError:
        fetched_at = None
    not_owned = raw.get("not_owned")
    not_owned_ids = frozenset(str(v) for v in not_owned) if isinstance(not_owned, list) else frozenset()
    reauth = raw.get("needs_reauth")
    reauth_ids = frozenset(str(v) for v in reauth) if isinstance(reauth, list) else frozenset()
    return NamesCache(names=names, fetched_at=fetched_at, not_owned=not_owned_ids, needs_reauth=reauth_ids)


def write_names(
    path: Path,
    names: Mapping[str, str],
    *,
    fetched_at: datetime,
    not_owned: Iterable[str] = (),
    needs_reauth: Iterable[str] = (),
) -> None:
    """Merge `names` into the cache (the newest name wins) and write it atomically.

    `not_owned` and `needs_reauth` are not merged like `names` - each is replaced whole with each
    call, because it is only ever passed the newest fetch's own ids: a playlist absent from a
    later listing (unfollowed, deleted) should stop being flagged, and only the latest listing
    knows that.
    """
    merged = {**read_names(path).names, **{k: v for k, v in names.items() if v}}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(
        path,
        json.dumps(
            {
                "fetched_at": fetched_at.isoformat(),
                "names": merged,
                "not_owned": sorted(set(not_owned)),
                "needs_reauth": sorted(set(needs_reauth)),
            },
            indent=2,
            sort_keys=True,
        ),
        mode=0o600,
    )


def playlist_url(playlist_id: str) -> str | None:
    """The playlist on open.spotify.com, shown where no name is known; ``None`` for a malformed id."""
    return f"https://open.spotify.com/playlist/{playlist_id}" if _SPOTIFY_ID.fullmatch(playlist_id) else None
