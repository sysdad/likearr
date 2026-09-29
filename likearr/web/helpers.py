"""Helpers more than one part of the web app uses: the Status and job pages in `app`, the
Settings and Plans routes in `routes`, and `_Web` in `context`.

Split out of `likearr.web.app`. Imports `context` only for type checking, never at run
time, so `context` can import this module.
"""

from __future__ import annotations

import errno
import logging
import os
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from likearr.adapters.state_sqlite import SqliteState
from likearr.config import Config, parse_config
from likearr.models import Diff
from likearr.shell.diff_io import DiffFileError, read_diff
from likearr.web import settings as cfg
from likearr.web.jobs import JobMeta, last_json_object

if TYPE_CHECKING:
    from likearr.web.context import _Web


log = logging.getLogger("likearr.web.app")
"""Under the app's own name, so log lines read as they did before the split."""


def _first_applied(config: Config) -> bool:
    """Whether a hand apply has completed, so scheduled applies may go ahead. A missing
    state database is a new install that has not, and is never created here: see healthz."""
    if not config.state_db.is_file():
        return False
    with SqliteState(config.state_db) as state:
        return state.first_apply_at() is not None


def _playlist_jobs(jobs: Sequence[JobMeta]) -> list[JobMeta]:
    return [m for m in jobs if m.kind == "playlists"]


def _names_of(fetched: Mapping[str, Any]) -> dict[str, str]:
    """id to name, from a `playlists --json` answer."""
    return {
        str(p["id"]): str(p["name"])
        for p in fetched["playlists"]
        if isinstance(p, dict) and p.get("id") and p.get("name")
    }


def _readable(entry: Mapping[str, Any]) -> bool:
    """A `playlists --json` entry's ``readable``, else True, as the picker has
    always read a bare entry."""
    value = entry.get("readable")
    return value if isinstance(value, bool) else True


def _not_owned_ids_of(fetched: Mapping[str, Any]) -> frozenset[str]:
    """Ids a `playlists --json` answer lists but marks not readable - followed, someone else's
    (a collaborative one only until the token may read it), or one of Spotify's own algorithmic
    or editorial playlists. What the picker greys out and a settings save refuses."""
    return frozenset(
        str(p["id"]) for p in fetched["playlists"] if isinstance(p, dict) and p.get("id") and not _readable(p)
    )


def _needs_reauth_ids_of(fetched: Mapping[str, Any]) -> frozenset[str]:
    """Ids a `playlists --json` answer marks ``needs_reauth``: collaborative playlists the stored
    token cannot read until Spotify is re-authorized."""
    return frozenset(
        str(p["id"])
        for p in fetched["playlists"]
        if isinstance(p, dict) and p.get("id") and p.get("needs_reauth") is True and not _readable(p)
    )


def _parse_playlists(output: str) -> dict[str, Any] | None:
    """The one JSON line `likearr playlists --json` prints, read from its raw, unredacted stdout."""
    return last_json_object(output, lambda d: isinstance(d.get("playlists"), list))


def _read_config(web: _Web) -> tuple[bytes, Config]:
    """The file's bytes and the config parsed from exactly those bytes."""
    text = web.config_path.read_bytes()
    return text, parse_config(tomllib.loads(text.decode("utf-8")), base_dir=web.config_path.parent)


def _form_pairs(values: Mapping[tuple[str, str], Any]) -> list[tuple[str, str]]:
    """The allowlisted values as hidden (name, value) pairs, to carry through the confirm page."""
    pairs: list[tuple[str, str]] = []
    for f in cfg.FIELDS:
        value = values[(f.section, f.key)]
        if f.kind == "bool":
            if value:
                pairs.append((f.name, "on"))
        elif f.kind == "list" and f.key == "playlists":
            pairs += [(f.name, str(v)) for v in value]
        elif f.kind == "list":
            pairs.append((f.name, " ".join(value)))
        else:
            pairs.append((f.name, str(value)))
    return pairs


def _read_plan(web: _Web, job_id: str) -> Diff | None:
    path = web.runner.diff_path(job_id)
    if path is None:
        return None
    try:
        return read_diff(path)
    except DiffFileError:
        log.warning("plan %s: diff.json does not read", job_id, exc_info=True)
        return None


_UNWRITABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY})


def unwritable_message(exc: OSError) -> str | None:
    """What to tell someone when likearr could not write a file, naming the directory and the fix;
    ``None`` for any other `OSError`. A single-file bind mount of a file likearr replaces (the
    rename answers EBUSY) is named as that."""
    if exc.errno not in _UNWRITABLE_ERRNOS:
        return None
    target = exc.filename2 or exc.filename
    path = Path(os.fsdecode(target)) if isinstance(target, str | bytes) and target else None
    if exc.errno == errno.EBUSY:
        name = path.name if path is not None else "a file"
        return (
            f"likearr can't replace {name}: it is mounted into the container as a single file. "
            "Mount the directory that holds it instead, then restart likearr."
        )
    where = str(path.parent) if path is not None else "its data directory"
    if exc.errno == errno.EROFS:
        return f"likearr can't save to {where}: it is read-only. Mount it read-write, then restart likearr."
    return f"likearr can't write to {where}. Give the user likearr runs as write access to it, then try again."
