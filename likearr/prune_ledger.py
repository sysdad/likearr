"""The Clean up ledger (#55): what earlier reviews decided, per album and per artist.

A prune report is rebuilt from scratch every time, and on its own it remembers nothing: a report
can list over a thousand albums, most of them kept in an earlier review.
This file is that memory. It lives at ``<config dir>/ui/prune-ledger.json`` beside the job store:

- **releases**, keyed by release-group MBID: ``keep`` (kept, nothing asked of Spotify), ``save``
  (kept and saved on Spotify, on its own or with its artist) or ``trash``, each with the day it
  was decided and where the decision came from;
- **artists**, keyed by artist MBID: only the artist-level Spotify intents, ``promote`` and
  ``save``. An artist-level keep or trash is not recorded, because it is not a fact about the
  artist's future albums: a new album of an artist kept last time still needs a decision.

Every export from the web UI records into it (`record`). A report's draft is pre-filled from it -
see `likearr.web.prune.prefill`, which holds the two rules that matter most: a past trash never
pre-fills as a trash, and a past follow or save never pre-fills as one either. The ledger says
what was decided; only a click in the current review asks Spotify for anything.

It is never read by `prune-stage` or `promote-save`, so nothing in it can move a file or reach
Spotify on its own.

**A ledger that will not read is never written over** (`LedgerUnreadable`): rewriting it with only
the new entries would lose every earlier decision without a word. The web export refuses and says
so. It also takes `ledger_lock`, a file lock beside the ledger, so two writers never interleave.

Neutral ground, like `likearr.playlist_names`: it imports nothing from the web UI or the CLI.
"""

from __future__ import annotations

import fcntl
import json
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from likearr.config import is_mbid
from likearr.fsio import write_atomic

__all__ = [
    "ARTIST_DECISIONS",
    "LEDGER_VERSION",
    "RELEASE_DECISIONS",
    "Entry",
    "Ledger",
    "LedgerBusy",
    "LedgerUnreadable",
    "ledger_lock",
    "ledger_path",
    "parse_entries",
    "parse_entry",
    "read_ledger",
    "record",
    "write_ledger",
]

LEDGER_VERSION = 1
RELEASE_DECISIONS = ("keep", "save", "trash")
ARTIST_DECISIONS = ("promote", "save")
MAX_SOURCE = 200
"""Longest "where it came from" kept: it is a label, and a hand-edited file must not bloat a page."""

LOCK_TRIES = 50
LOCK_PAUSE_S = 0.1
"""How long `ledger_lock` waits for another writer: at most 50 x 0.1 s. A writer holds it for one
read and one write of a small file, so a wait this long means something is stuck."""

_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
"""A day, in ASCII digits only: `\\d` would take any Unicode digit."""


@dataclass(frozen=True, slots=True)
class Entry:
    decision: str
    on: str
    """The day it was decided, ``YYYY-MM-DD``."""
    source: str
    """Where it came from, e.g. "review of 2026-01-15 (decisions.json)" or "Clean up <job id>"."""

    def to_dict(self) -> dict[str, str]:
        return {"decision": self.decision, "on": self.on, "from": self.source}


@dataclass(slots=True)
class Ledger:
    releases: dict[str, Entry] = field(default_factory=dict)
    artists: dict[str, Entry] = field(default_factory=dict)
    imports: list[str] = field(default_factory=list)
    """Carried forward untouched and read by nothing: an older likearr's one-time import command
    recorded digests here. A ledger with a non-empty list keeps it; one without never gets one."""
    problem: str = ""
    """Why the file on disk will not read; empty when it read, or when there is none yet. A
    ledger with a problem is read as empty and must never be written over (`LedgerUnreadable`)."""


class LedgerUnreadable(Exception):
    """The ledger on disk will not read, so it is left exactly as it is rather than replaced."""


class LedgerBusy(Exception):
    """Another writer held the ledger lock for longer than `ledger_lock` waits."""


def ledger_path(config_path: Path) -> Path:
    """`<config dir>/ui/prune-ledger.json`, beside the web UI's job store."""
    return config_path.parent / "ui" / "prune-ledger.json"


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Hold the ledger's file lock (``<ledger>.lock``) for one read-modify-write.

    A file lock, not a thread lock, so it also holds across processes. The wait is bounded - a
    counted, non-blocking retry - so a stuck writer is an error, not a hang.

    Raises:
        LedgerBusy: the lock stayed taken for the whole wait.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("w") as fh:
        for attempt in range(LOCK_TRIES):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == LOCK_TRIES - 1:
                    raise LedgerBusy(f"another likearr process is writing {path}; try again") from None
                time.sleep(LOCK_PAUSE_S)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def parse_entry(raw: object, allowed: tuple[str, ...]) -> Entry | None:
    """One entry, or ``None`` for anything that is not exactly one: a hand-edited or damaged file
    loses the bad entry, never the page."""
    if not isinstance(raw, Mapping):
        return None
    decision, on, source = raw.get("decision"), raw.get("on"), raw.get("from")
    if decision not in allowed or not isinstance(on, str) or _DAY.fullmatch(on) is None:
        return None
    return Entry(decision=str(decision), on=on, source=str(source or "")[:MAX_SOURCE])


def parse_entries(raw: object, allowed: tuple[str, ...]) -> dict[str, Entry]:
    """A map of MBID -> entry; anything else in it is dropped. Shared with the draft's copy."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Entry] = {}
    for key, value in raw.items():
        entry = parse_entry(value, allowed)
        if isinstance(key, str) and is_mbid(key) and entry is not None:
            out[key] = entry
    return out


def read_ledger(path: Path) -> Ledger:
    """The ledger. Never raises: a missing file is an empty ledger (nothing decided yet), and one
    that will not read is an empty ledger whose `problem` says why - so the page still works, and
    no writer replaces the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Ledger()
    except OSError as exc:
        return Ledger(problem=f"cannot read {path}: {exc}")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return Ledger(problem=f"{path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        return Ledger(problem=f"{path} is not a ledger (not a JSON object)")
    if raw.get("version", LEDGER_VERSION) != LEDGER_VERSION:
        return Ledger(problem=f"{path} is ledger version {raw.get('version')!r}; this likearr reads version 1")
    for part in ("releases", "artists"):
        if not isinstance(raw.get(part, {}), dict):
            return Ledger(problem=f"{path} has a {part!r} that is not a JSON object")
    imports = raw.get("imports")
    return Ledger(
        releases=parse_entries(raw.get("releases"), RELEASE_DECISIONS),
        artists=parse_entries(raw.get("artists"), ARTIST_DECISIONS),
        imports=[str(d) for d in imports if isinstance(d, str)] if isinstance(imports, list) else [],
    )


def write_ledger(path: Path, ledger: Ledger) -> None:
    """Write it atomically: a reader sees the old ledger or the new one, never half of one.

    Raises:
        LedgerUnreadable: `ledger` was read from a file that would not read. Writing it would
            replace every earlier decision with only the new ones.
    """
    if ledger.problem:
        raise LedgerUnreadable(
            f"the Clean up ledger was left as it is, because it will not read: {ledger.problem}. "
            "Fix or move that file, then export again."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {
        "version": LEDGER_VERSION,
        "releases": {k: e.to_dict() for k, e in sorted(ledger.releases.items())},
        "artists": {k: e.to_dict() for k, e in sorted(ledger.artists.items())},
    }
    if ledger.imports:
        body["imports"] = ledger.imports
    write_atomic(path, json.dumps(body, indent=1) + "\n", mode=0o600)


def _merge(current: Mapping[str, Entry], changes: Mapping[str, str], *, on: str, source: str) -> dict[str, Entry]:
    """Apply `changes` (id -> decision, ``""`` to forget). The same decision again keeps its first
    date and source, so an album kept in January still reads "You kept this on 15 Jan 2026." after
    every later export that kept it again - and is not mistaken for a decision of that later review."""
    out = dict(current)
    for key, decision in changes.items():
        if not decision:
            out.pop(key, None)
        elif key not in out or out[key].decision != decision:
            out[key] = Entry(decision=decision, on=on, source=source[:MAX_SOURCE])
    return out


def record(ledger: Ledger, releases: Mapping[str, str], artists: Mapping[str, str], *, on: str, source: str) -> Ledger:
    """The ledger after one export. `artists` maps an artist to ``promote`` / ``save``, or to
    ``""`` when the export decided them otherwise (keep, trash): an earlier follow or save must
    not be remembered as one the reviewer has since changed."""
    return Ledger(
        releases=_merge(ledger.releases, releases, on=on, source=source),
        artists=_merge(ledger.artists, artists, on=on, source=source),
        imports=list(ledger.imports),
        problem=ledger.problem,
    )
