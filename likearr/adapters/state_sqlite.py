"""SQLite-backed ownership state. A shell concern the core never sees (`Context.state` is this
class, concrete, wherever the core needs it).

One file, one connection per `SqliteState` instance. Schema is created idempotently on open
behind a `schema_version` table so future migrations have somewhere to hook in. The connection
runs in autocommit mode (`isolation_level=None`) so `transaction()` can drive `BEGIN IMMEDIATE`
/ `COMMIT` / `ROLLBACK` explicitly, including nested use via a depth counter.

Note: the MusicBrainz adapter owns an `mb_cache` table and the Spotify library adapter owns a
`spotify_search_cache` table, both in this same database file, each on its own connection. This
module never creates or touches either, and neither is covered by `SCHEMA_VERSION`.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from likearr.core.health import Fingerprint, HealthBaseline
from likearr.models import (
    RESOLVER_VERSION,
    Diff,
    Guard,
    HealthRecord,
    NameCollision,
    OwnedArtist,
    OwnedRelease,
    PrimaryType,
    Profile,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    RunStatus,
    SecondaryType,
)

SCHEMA_VERSION = 8
"""Current schema version this module knows how to read and write.

2: `health_baseline` / `health_baseline_meta`, for the run-to-run comparison in `core.health`.
Purely additive - a v1 file gains two empty tables and reads as "no baseline yet", which is the
first-run path, so an upgraded deployment re-baselines silently instead of alarming.

3: `gap_refreshes`, the per-artist backoff on the freshness refreshes. Additive in
the same way - a v2 file gains one empty table and reads as "never refreshed for a gap", so the
first run after the upgrade refreshes normally and the backoff starts from there.

4: `lidarr_negative_cache`, which Lidarr metadata search terms/lookups fail server-side.
Additive in the same way - a v3 file gains one empty table and reads as "nothing
cached yet", so the first run after the upgrade asks Lidarr about every term exactly as before.

5: `health_baseline_meta.rules`, the `ExclusionRules.token` a baseline was collected under.
The first change here that is a **column** rather than a table, so
`CREATE TABLE IF NOT EXISTS` is no longer the whole migration - see `_ensure_schema`. Still
additive, and additive in both directions: an older file gains the column at its default `''`,
which is also the token of a default configuration, so an upgraded deployment keeps comparing
against its existing baseline instead of re-baselining for nothing - and an older *image* opened
against a v5 file still works, because every read names its columns and every write omits
`rules`, which the default fills in. That matters because this is the version a rollback would
cross.

6: `scheduler_state`, one row recording the in-service scheduler's last fire time. Additive, a
new table only: an older file gains it empty, which reads as "never fired before",
and the scheduler's own first-start rule (no catch-up with no record) makes that the correct answer
rather than a false missed-fire.

7: `scheduler_state.cancelled`, a column recording whether the last fire was cancelled by a
redeploy while still planning. A v6 file gains it at its default `0`, which
reads as "not cancelled" - the same as any fire that ran to completion - so an upgrade never
invents a missed fire that never happened.

8: `first_apply`, one row recording when a hand `run --apply` first completed.
Scheduled applies are held until it exists, so a new install's first plan is always reviewed
before anything unattended applies one. A table of its own, not a column on `scheduler_state`:
that row's existence means "the scheduler has fired", which the missed-fire catch-up reads, so a
first apply made before any fire would have had to invent one. The first change with a **data**
step: a file upgraded from v7 or earlier is marked as already applied when it has ever applied
anything - any `owned_releases` row, or a health baseline (only an apply writes one) - so an
existing install keeps its schedule running with no step from the user. It runs once, keyed on
the version stamp, so an `adopt --apply` after the upgrade never counts. Additive for a rollback
too: an older image ignores the table, and never stamps the version back down.
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS owned_releases (
    artist_mbid TEXT NOT NULL,
    rg_mbid TEXT NOT NULL,
    lidarr_album_id INTEGER,
    reasons_json TEXT NOT NULL,
    step TEXT NOT NULL,
    resolver_version INTEGER NOT NULL,
    monitored_at TEXT NOT NULL,
    PRIMARY KEY (artist_mbid, rg_mbid)
);

CREATE TABLE IF NOT EXISTS owned_artists (
    artist_mbid TEXT PRIMARY KEY,
    lidarr_artist_id INTEGER,
    added_by_us INTEGER NOT NULL,
    profile TEXT NOT NULL,
    ratcheted_at TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
    intent_key TEXT PRIMARY KEY,
    resolver_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    json TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending (
    intent_key TEXT PRIMARY KEY,
    since TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_counts (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followed_counts (
    artist_mbid TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_refreshes (
    artist_mbid  TEXT PRIMARY KEY,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lidarr_negative_cache (
    identity  TEXT PRIMARY KEY,
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_baseline (
    dimension TEXT NOT NULL,
    identity  TEXT NOT NULL,
    PRIMARY KEY (dimension, identity)
);

CREATE TABLE IF NOT EXISTS health_baseline_meta (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    resolver_version  INTEGER NOT NULL,
    liked_track_scope TEXT NOT NULL,
    source_set        TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    rules             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    diff_json TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    last_fire TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS first_apply (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    applied_at TEXT NOT NULL
);
"""

_MAX_RUNS = 200


@dataclasses.dataclass(frozen=True, slots=True)
class RunRow:
    """One row of run history as the web UI's Status page reads it.

    The health record alone does not say which guards fired, how many releases the run would
    leave wanted, or which artists it skipped for a name collision: those live on the diff, which
    the `runs` table stores beside it. Only those facts are pulled out of it, in SQL, so reading
    twenty rows never deserialises twenty diffs.
    """

    record: HealthRecord
    id: int = 0
    """The `runs` row id; 0 for a `RunRow` built without one (tests). What `/runs/<id>`
    reads, and what a job history entry is matched to by `run_id_in_job`."""
    guards: tuple[str, ...] = ()
    """The fired guards' messages, in the diff's order. Empty for a run with no diff."""
    guard_blocked: tuple[int, ...] = ()
    """How many unmonitors each guard in `guards` held back: 0 for an advisory one."""
    guard_codes: tuple[str, ...] = ()
    """The same guards' codes (``source-shrink``, ``artist-shrink``...), in the same order."""
    projected_wanted: int | None = None
    """`Diff.projected_wanted`, or ``None`` when the run never got as far as a diff."""
    name_collisions: tuple[NameCollision, ...] = ()
    """`Diff.name_collisions`: artists skipped because Lidarr already holds their name."""


@dataclasses.dataclass(frozen=True, slots=True)
class LastPlan:
    """What the /plan page needs from the newest plan: its guards and whether it accepted shrinks."""

    guards: tuple[Guard, ...]
    accept_shrink: bool


# ---------------------------------------------------------------- reasons (JSON round-trip)


def _reasons_to_json(reasons: Sequence[Reason] | frozenset[Reason] | set[Reason]) -> str:
    """Serialise reasons as a sorted JSON list of {kind, source_id, playlist_id}.

    Sorted by `Reason.key` so the JSON is stable regardless of set iteration order.
    """
    ordered = sorted(reasons, key=lambda r: r.key)
    return json.dumps([{"kind": r.kind.value, "source_id": r.source_id, "playlist_id": r.playlist_id} for r in ordered])


def _reasons_from_json(raw: str) -> frozenset[Reason]:
    items = json.loads(raw)
    return frozenset(
        Reason(kind=ReasonKind(item["kind"]), source_id=item["source_id"], playlist_id=item.get("playlist_id"))
        for item in items
    )


# ---------------------------------------------------------------- ReleaseGroup / Resolution JSON


def _release_group_to_json(rg: ReleaseGroup | None) -> dict[str, object] | None:
    if rg is None:
        return None
    return {
        "mbid": rg.mbid,
        "title": rg.title,
        "artist_mbid": rg.artist_mbid,
        "artist_name": rg.artist_name,
        "primary_type": rg.primary_type.value if rg.primary_type is not None else None,
        "secondary_types": sorted(t.value for t in rg.secondary_types),
        "first_release_date": rg.first_release_date.isoformat() if rg.first_release_date is not None else None,
    }


def _release_group_from_json(d: dict[str, object] | None) -> ReleaseGroup | None:
    if d is None:
        return None
    primary_type = d.get("primary_type")
    first_release_date = d.get("first_release_date")
    secondary_types_raw = d.get("secondary_types")
    secondary_types: list[object] = list(secondary_types_raw) if isinstance(secondary_types_raw, list) else []
    return ReleaseGroup(
        mbid=str(d["mbid"]),
        title=str(d["title"]),
        artist_mbid=str(d["artist_mbid"]),
        artist_name=str(d["artist_name"]),
        primary_type=PrimaryType(primary_type) if primary_type is not None else None,
        secondary_types=frozenset(SecondaryType(t) for t in secondary_types),
        first_release_date=date.fromisoformat(str(first_release_date)) if first_release_date else None,
    )


def resolution_to_json(resolution: Resolution) -> str:
    """Serialise a `Resolution`, including its nested `ReleaseGroup`s, to a JSON string.

    Dates become ISO strings and enums become their plain values. Pair with `resolution_from_json`,
    which is tolerant of extra/missing keys so additive model fields never break old rows.
    """
    payload = {
        "intent_key": resolution.intent_key,
        "status": resolution.status.value,
        "release_group": _release_group_to_json(resolution.release_group),
        "step": resolution.step,
        "detail": resolution.detail,
        "single_release_date": resolution.single_release_date.isoformat()
        if resolution.single_release_date is not None
        else None,
        "single_release_group": _release_group_to_json(resolution.single_release_group),
        "resolver_version": resolution.resolver_version,
        "source_release_group": _release_group_to_json(resolution.source_release_group),
        "scope": resolution.scope,
        "followed": resolution.followed,
        "rules": resolution.rules,
        "denied_skipped": sorted(resolution.denied_skipped),
        "checked_at": resolution.checked_at.isoformat() if resolution.checked_at is not None else None,
    }
    return json.dumps(payload, sort_keys=True)


def resolution_from_json(raw: str) -> Resolution:
    """Deserialise a `Resolution` written by `resolution_to_json`. Tolerant of extra keys."""
    d = json.loads(raw)
    single_release_date = d.get("single_release_date")
    return Resolution(
        intent_key=d["intent_key"],
        status=ResolutionStatus(d["status"]),
        release_group=_release_group_from_json(d.get("release_group")),
        step=d.get("step", ""),
        detail=d.get("detail", ""),
        single_release_date=date.fromisoformat(single_release_date) if single_release_date else None,
        single_release_group=_release_group_from_json(d.get("single_release_group")),
        resolver_version=d.get("resolver_version", RESOLVER_VERSION),
        source_release_group=_release_group_from_json(d.get("source_release_group")),
        scope=d.get("scope", "album"),
        followed=None if (followed := d.get("followed")) is None else bool(followed),
        # An older row has no token and reads as the defaults, so under a non-default
        # `[rules]` it re-resolves once and is written back with the live token.
        rules=str(d.get("rules") or ""),
        # An older row records none, so it is reused until something else moves it.
        denied_skipped=frozenset(str(m) for m in d.get("denied_skipped") or ()),
        # An older row has no check time; `_due` starts its clock on the next run.
        checked_at=datetime.fromisoformat(checked_at) if (checked_at := d.get("checked_at")) else None,
    )


# ---------------------------------------------------------------- generic dataclass/enum JSON default


def _json_default(obj: object) -> object:
    """`default=` hook for `json.dumps` covering what `dataclasses.asdict` leaves behind.

    `dataclasses.asdict` does not convert dataclass instances nested inside a `set`/`frozenset`
    (e.g. `frozenset[Reason]`); it deep-copies them as-is. This hook is invoked by the JSON
    encoder for whatever it can't serialise natively, at any depth, so it also covers those.
    """
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, frozenset | set):

        def _sort_key(item: object) -> str:
            key = getattr(item, "key", None)
            if isinstance(key, str):
                return key
            return str(item)

        return sorted(obj, key=_sort_key)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serializable")


def _health_record_to_json(record: HealthRecord) -> str:
    return json.dumps(record.to_dict(), default=_json_default, sort_keys=True)


def _health_record_from_json(raw: str) -> HealthRecord:
    d = json.loads(raw)
    return HealthRecord(
        ts=d["ts"],
        version=d.get("version", ""),
        resolver_version=d.get("resolver_version", RESOLVER_VERSION),
        exit_code=d["exit_code"],
        status=RunStatus(d["status"]),
        spotify_ok=d["spotify_ok"],
        spotify_schema_ok=d["spotify_schema_ok"],
        mb_ok=d["mb_ok"],
        lidarr_ok=d["lidarr_ok"],
        lidarr_metadata_ok=d["lidarr_metadata_ok"],
        counts=dict(d.get("counts", {})),
        unmapped=d.get("unmapped", 0),
        pending_album=d.get("pending_album", 0),
        message=d.get("message", ""),
        dry_run=d.get("dry_run", True),
        unmapped_new=d.get("unmapped_new", 0),
        unmapped_resolved=d.get("unmapped_resolved", 0),
        unmapped_ratio=d.get("unmapped_ratio", 0.0),
        regressions=d.get("regressions", 0),
        catalogue_gaps=d.get("catalogue_gaps", 0),
        catalogue_gaps_new=d.get("catalogue_gaps_new", 0),
        catalogue_gaps_recent=d.get("catalogue_gaps_recent", 0),
        catalogue_gaps_recent_new=d.get("catalogue_gaps_recent_new", 0),
        refresh_failures=d.get("refresh_failures", 0),
        absent_in_lidarr=d.get("absent_in_lidarr", 0),
        absent_in_lidarr_new=d.get("absent_in_lidarr_new", 0),
        lidarr_metadata_errors=d.get("lidarr_metadata_errors", 0),
        lidarr_metadata_errors_new=d.get("lidarr_metadata_errors_new", 0),
        mb_errors=d.get("mb_errors", 0),
        skipped_artists=d.get("skipped_artists", 0),
        skipped_artists_new=d.get("skipped_artists_new", 0),
        name_collisions=d.get("name_collisions", 0),
        name_collisions_new=d.get("name_collisions_new", 0),
        catalogue_too_large=d.get("catalogue_too_large", 0),
        catalogue_too_large_new=d.get("catalogue_too_large_new", 0),
        baseline=d.get("baseline", ""),
        baseline_advanced=d.get("baseline_advanced", False),
        new_conditions=list(d.get("new_conditions", [])),
        changes_made=d.get("changes_made"),
        changes_planned=d.get("changes_planned"),
        lidarr_changed=d.get("lidarr_changed"),
        tagged_without_state=d.get("tagged_without_state", 0),
    )


_RUN_FACTS = "json_extract(diff_json, '$.guards', '$.projected_wanted', '$.name_collisions')"
"""One json_extract with three paths parses a stored diff once and answers a JSON array."""


def _run_row(row: sqlite3.Row) -> RunRow:
    guards, projected, collisions = json.loads(row["facts"]) if row["facts"] else (None, None, None)
    guards = [g for g in guards or [] if isinstance(g, dict)]
    return RunRow(
        record=_health_record_from_json(row["record_json"]),
        id=int(row["id"]),
        guards=tuple(str(g.get("message", "")) for g in guards),
        guard_blocked=tuple(_int(g.get("blocked_unmonitors")) for g in guards),
        guard_codes=tuple(str(g.get("code", "")) for g in guards),
        projected_wanted=int(projected) if projected is not None else None,
        name_collisions=tuple(c for c in map(_name_collision, collisions or []) if c is not None),
    )


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _name_collision(d: object) -> NameCollision | None:
    """A `NameCollision` from its stored JSON, tolerating fields a newer or older version lacks.

    ``None`` for an entry that will not read (not an object, a count that is not a number): one bad
    row is skipped rather than taking the Status page down with it.
    """
    if not isinstance(d, dict):
        return None
    try:
        return _collision_fields(d)
    except (TypeError, ValueError):
        return None


def _collision_fields(d: dict[str, Any]) -> NameCollision:
    return NameCollision(
        name=str(d.get("name", "")),
        wanted_mbid=str(d.get("wanted_mbid", "")),
        existing_mbid=str(d.get("existing_mbid", "")),
        existing_lidarr_id=int(d.get("existing_lidarr_id") or 0),
        existing_name=str(d.get("existing_name", "")),
        wanted_disambiguation=str(d.get("wanted_disambiguation", "")),
        existing_disambiguation=str(d.get("existing_disambiguation", "")),
        dropped_releases=int(d.get("dropped_releases") or 0),
    )


def _diff_to_json(diff: Diff) -> str:
    return json.dumps(dataclasses.asdict(diff), default=_json_default, sort_keys=True)


_WAL_RETRY_SECONDS = 10.0
_WAL_RETRY_PAUSE = 0.05


def _is_busy(e: sqlite3.OperationalError) -> bool:
    message = str(e).lower()
    return "locked" in message or "busy" in message


class SqliteState:
    """Ownership state, backed by a single SQLite file (WAL mode).

    Every write method wraps itself in `transaction()`, so it works standalone (auto-commits) or
    nested inside a caller's own `with state.transaction():` block (joins the outer transaction).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts the connection in autocommit mode so `transaction()` can
        # drive BEGIN/COMMIT/ROLLBACK explicitly instead of fighting sqlite3's implicit ones.
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self._configure_pragmas()
        self._ensure_schema()

    def _configure_pragmas(self) -> None:
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._switch_to_wal()
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _switch_to_wal(self) -> None:
        """Set WAL mode, retrying while another process holds the file (two processes opening a
        brand-new database at once). Stops retrying after `_WAL_RETRY_SECONDS`; each attempt can
        also wait out the busy timeout."""
        deadline = time.monotonic() + _WAL_RETRY_SECONDS
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if not _is_busy(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(_WAL_RETRY_PAUSE)

    def _ensure_schema(self) -> None:
        """Create anything missing, then record the version.

        Every change up to v3 was a new table, so `CREATE TABLE IF NOT EXISTS` *was* the whole
        migration and an older file needed no data rewritten - it simply gained empty tables.
        v4 adds a column to an existing table, which that idiom cannot do, so the column is added
        explicitly when it is missing. It carries a default, so the rows already there are correct
        without being rewritten. The version row is stamped forward afterwards so `doctor` and any
        future migration can tell what they are looking at.

        v8 is the first step that writes data (`_mark_applied_if_it_ever_applied`), so it runs only
        for a file stamped older than 8, in the same transaction as the stamp: a crash between the
        two leaves the file at its old version, and the next open simply runs it again.
        """
        self._conn.executescript(_SCHEMA_SQL)
        self._add_column("health_baseline_meta", "rules", "TEXT NOT NULL DEFAULT ''")
        self._add_column("scheduler_state", "cancelled", "INTEGER NOT NULL DEFAULT 0")
        # One statement, so two processes opening a new file cannot both insert a version row.
        self._conn.execute(
            "INSERT INTO schema_version (version) SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION,),
        )
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row["version"] < SCHEMA_VERSION:
            with self.transaction():
                if row["version"] < 8:
                    self._mark_applied_if_it_ever_applied()
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    def _mark_applied_if_it_ever_applied(self) -> None:
        """The v8 data step: an install upgraded from before the first-apply gate existed has
        already had its first plan applied if it owns any release or has a health baseline - only
        an apply writes either - so its schedule keeps running. The time is the migration's own;
        the real first apply's was never recorded. A file with neither is left unset, and its
        scheduled applies wait for a hand apply like a new install's."""
        self._conn.execute(
            "INSERT OR IGNORE INTO first_apply (id, applied_at) SELECT 1, ? "
            "WHERE EXISTS (SELECT 1 FROM owned_releases) OR EXISTS (SELECT 1 FROM health_baseline_meta)",
            (datetime.now(UTC).isoformat(),),
        )

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        """Add a column to an existing table, once. SQLite has no `ADD COLUMN IF NOT EXISTS`.

        Another process opening the same file can add it between the check and the ALTER; its
        "duplicate column" error means the column is there, which is all this wants.
        """
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            return
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    def schema_version(self) -> int:
        """The version recorded in the file. `doctor` and tests read it."""
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row is not None else 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- transaction

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT / ROLLBACK. Nested use joins the outer transaction."""
        outermost = self._tx_depth == 0
        if outermost:
            self._conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if outermost:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._tx_depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    # -------------------------------------------------------------- owned releases

    def owned_releases(self) -> dict[ReleaseKey, OwnedRelease]:
        rows = self._conn.execute(
            "SELECT artist_mbid, rg_mbid, lidarr_album_id, reasons_json, step, resolver_version, monitored_at "
            "FROM owned_releases"
        ).fetchall()
        result: dict[ReleaseKey, OwnedRelease] = {}
        for row in rows:
            key = ReleaseKey(artist_mbid=row["artist_mbid"], rg_mbid=row["rg_mbid"])
            result[key] = OwnedRelease(
                key=key,
                reasons=_reasons_from_json(row["reasons_json"]),
                step=row["step"],
                resolver_version=row["resolver_version"],
                monitored_at=datetime.fromisoformat(row["monitored_at"]),
                lidarr_album_id=row["lidarr_album_id"],
            )
        return result

    def record_monitored(self, releases: Sequence[OwnedRelease]) -> None:
        with self.transaction():
            for r in releases:
                self._conn.execute(
                    """
                    INSERT INTO owned_releases
                        (artist_mbid, rg_mbid, lidarr_album_id, reasons_json, step, resolver_version, monitored_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (artist_mbid, rg_mbid) DO UPDATE SET
                        lidarr_album_id = excluded.lidarr_album_id,
                        reasons_json = excluded.reasons_json,
                        step = excluded.step,
                        resolver_version = excluded.resolver_version,
                        monitored_at = excluded.monitored_at
                    """,
                    (
                        r.key.artist_mbid,
                        r.key.rg_mbid,
                        r.lidarr_album_id,
                        _reasons_to_json(r.reasons),
                        r.step,
                        r.resolver_version,
                        r.monitored_at.isoformat(),
                    ),
                )

    def record_unmonitored(self, keys: Sequence[ReleaseKey]) -> None:
        with self.transaction():
            for k in keys:
                self._conn.execute(
                    "DELETE FROM owned_releases WHERE artist_mbid = ? AND rg_mbid = ?",
                    (k.artist_mbid, k.rg_mbid),
                )

    def update_reasons(self, key: ReleaseKey, reasons: frozenset) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE owned_releases SET reasons_json = ? WHERE artist_mbid = ? AND rg_mbid = ?",
                (_reasons_to_json(reasons), key.artist_mbid, key.rg_mbid),
            )

    # -------------------------------------------------------------- owned artists

    def owned_artists(self) -> dict[str, OwnedArtist]:
        rows = self._conn.execute(
            "SELECT artist_mbid, lidarr_artist_id, added_by_us, profile, ratcheted_at FROM owned_artists"
        ).fetchall()
        return {
            row["artist_mbid"]: OwnedArtist(
                artist_mbid=row["artist_mbid"],
                lidarr_artist_id=row["lidarr_artist_id"],
                added_by_us=bool(row["added_by_us"]),
                profile=Profile(row["profile"]),
                ratcheted_at=datetime.fromisoformat(row["ratcheted_at"]) if row["ratcheted_at"] else None,
            )
            for row in rows
        }

    def record_artist(self, artist: OwnedArtist) -> None:
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO owned_artists (artist_mbid, lidarr_artist_id, added_by_us, profile, ratcheted_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (artist_mbid) DO UPDATE SET
                    lidarr_artist_id = excluded.lidarr_artist_id,
                    added_by_us = excluded.added_by_us,
                    profile = excluded.profile,
                    ratcheted_at = excluded.ratcheted_at
                """,
                (
                    artist.artist_mbid,
                    artist.lidarr_artist_id,
                    int(artist.added_by_us),
                    artist.profile.value,
                    artist.ratcheted_at.isoformat() if artist.ratcheted_at else None,
                ),
            )

    # -------------------------------------------------------------- resolution cache

    def cached_resolution(self, intent_key: str, resolver_version: int) -> Resolution | None:
        row = self._conn.execute(
            "SELECT resolver_version, json FROM resolutions WHERE intent_key = ?", (intent_key,)
        ).fetchone()
        if row is None or row["resolver_version"] != resolver_version:
            return None
        return resolution_from_json(row["json"])

    def cache_resolution(self, resolution: Resolution) -> None:
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO resolutions (intent_key, resolver_version, status, json, resolved_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (intent_key) DO UPDATE SET
                    resolver_version = excluded.resolver_version,
                    status = excluded.status,
                    json = excluded.json,
                    resolved_at = excluded.resolved_at
                """,
                (
                    resolution.intent_key,
                    resolution.resolver_version,
                    resolution.status.value,
                    resolution_to_json(resolution),
                    datetime.now(UTC).isoformat(),
                ),
            )

    # -------------------------------------------------------------- pending

    def pending_since(self, intent_key: str) -> datetime | None:
        row = self._conn.execute("SELECT since FROM pending WHERE intent_key = ?", (intent_key,)).fetchone()
        return datetime.fromisoformat(row["since"]) if row is not None else None

    def mark_pending(self, intent_key: str, since: datetime) -> None:
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO pending (intent_key, since) VALUES (?, ?)
                ON CONFLICT (intent_key) DO UPDATE SET since = excluded.since
                """,
                (intent_key, since.isoformat()),
            )

    def clear_pending(self, intent_key: str) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM pending WHERE intent_key = ?", (intent_key,))

    # -------------------------------------------------------------- source / followed counts

    def last_source_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT key, count FROM source_counts").fetchall()
        return {row["key"]: row["count"] for row in rows}

    def record_source_counts(self, counts: dict[str, int]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction():
            self._conn.execute("DELETE FROM source_counts")
            self._conn.executemany(
                "INSERT INTO source_counts (key, count, recorded_at) VALUES (?, ?, ?)",
                [(key, count, now) for key, count in counts.items()],
            )

    def last_followed_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT artist_mbid, count FROM followed_counts").fetchall()
        return {row["artist_mbid"]: row["count"] for row in rows}

    def record_followed_counts(self, counts: dict[str, int]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction():
            self._conn.execute("DELETE FROM followed_counts")
            self._conn.executemany(
                "INSERT INTO followed_counts (artist_mbid, count, recorded_at) VALUES (?, ?, ?)",
                [(artist_mbid, count, now) for artist_mbid, count in counts.items()],
            )

    # -------------------------------------------------------------- gap refresh backoff

    def last_gap_refreshes(self) -> dict[str, datetime]:
        rows = self._conn.execute("SELECT artist_mbid, refreshed_at FROM gap_refreshes").fetchall()
        return {row["artist_mbid"]: datetime.fromisoformat(row["refreshed_at"]) for row in rows}

    def record_gap_refreshes(self, artist_mbids: Sequence[str], at: datetime) -> None:
        """Stamp each artist as refreshed *now*, whether or not the refresh succeeded.

        A refresh that failed still cost Lidarr a command, and an artist whose metadata is stuck
        is exactly the one that would otherwise be retried on every run for weeks. The backoff is
        about how often likearr asks, not about whether the answer was any good.
        """
        stamp = at.isoformat()
        with self.transaction():
            self._conn.executemany(
                """
                INSERT INTO gap_refreshes (artist_mbid, refreshed_at) VALUES (?, ?)
                ON CONFLICT (artist_mbid) DO UPDATE SET refreshed_at = excluded.refreshed_at
                """,
                [(artist_mbid, stamp) for artist_mbid in artist_mbids],
            )

    # -------------------------------------------------------------- lidarr metadata negative cache

    def lidarr_negative_cache(self) -> dict[str, datetime]:
        rows = self._conn.execute("SELECT identity, cached_at FROM lidarr_negative_cache").fetchall()
        return {row["identity"]: datetime.fromisoformat(row["cached_at"]) for row in rows}

    def record_lidarr_negative_cache(self, identities: Sequence[str], at: datetime) -> None:
        """Stamp each identity as failed *now*, whether it was already cached or not.

        Only ever called with identities that failed a genuine attempt this run, and only when
        `CompositeLookup.lidarr_metadata_any_success` held - see `shell.plan.plan`.
        """
        stamp = at.isoformat()
        with self.transaction():
            self._conn.executemany(
                """
                INSERT INTO lidarr_negative_cache (identity, cached_at) VALUES (?, ?)
                ON CONFLICT (identity) DO UPDATE SET cached_at = excluded.cached_at
                """,
                [(identity, stamp) for identity in identities],
            )

    # -------------------------------------------------------------- health baseline

    def health_baseline(self) -> HealthBaseline | None:
        """The previous run's identities, or None when no run has ever written one.

        The meta row is what says a baseline exists: without it an empty `health_baseline` table
        would be indistinguishable from "last run saw no faults", and those must not be confused -
        the first means "cannot compare, do not alarm", the second means "all clear".
        """
        meta = self._conn.execute(
            "SELECT resolver_version, liked_track_scope, source_set, rules FROM health_baseline_meta WHERE id = 1"
        ).fetchone()
        if meta is None:
            return None
        identities: dict[str, set[str]] = {}
        for row in self._conn.execute("SELECT dimension, identity FROM health_baseline"):
            identities.setdefault(row["dimension"], set()).add(row["identity"])
        return HealthBaseline(
            fingerprint=Fingerprint(
                resolver_version=meta["resolver_version"],
                liked_track_scope=meta["liked_track_scope"],
                source_set=tuple(json.loads(meta["source_set"])),
                rules=meta["rules"],
            ),
            identities={dimension: frozenset(values) for dimension, values in identities.items()},
        )

    def record_health_baseline(self, baseline: HealthBaseline) -> None:
        """Replace the baseline wholesale. Never merged: the caller decided what survives."""
        now = datetime.now(UTC).isoformat()
        with self.transaction():
            self._conn.execute("DELETE FROM health_baseline")
            self._conn.executemany(
                "INSERT INTO health_baseline (dimension, identity) VALUES (?, ?)",
                [(dimension, identity) for dimension, values in baseline.identities.items() for identity in values],
            )
            self._conn.execute(
                """
                INSERT INTO health_baseline_meta
                    (id, resolver_version, liked_track_scope, source_set, recorded_at, rules)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    resolver_version = excluded.resolver_version,
                    liked_track_scope = excluded.liked_track_scope,
                    source_set = excluded.source_set,
                    recorded_at = excluded.recorded_at,
                    rules = excluded.rules
                """,
                (
                    baseline.fingerprint.resolver_version,
                    baseline.fingerprint.liked_track_scope,
                    json.dumps(list(baseline.fingerprint.source_set)),
                    now,
                    baseline.fingerprint.rules,
                ),
            )

    # -------------------------------------------------------------- runs / health history

    def record_run(self, record: HealthRecord, diff: Diff | None) -> None:
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO runs (ts, status, exit_code, record_json, diff_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.ts,
                    record.status.value,
                    record.exit_code,
                    _health_record_to_json(record),
                    _diff_to_json(diff) if diff is not None else None,
                ),
            )
            self._conn.execute(
                "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
                (_MAX_RUNS,),
            )

    def last_run(self) -> HealthRecord | None:
        """The most recently recorded run's health record, if any."""
        row = self._conn.execute("SELECT record_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return _health_record_from_json(row["record_json"]) if row is not None else None

    def run_history(self, limit: int = 20) -> list[RunRow]:
        """The most recent `limit` runs, newest first, with their guards and projected wanted."""
        rows = self._conn.execute(
            f"SELECT id, record_json, {_RUN_FACTS} AS facts FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_run_row(row) for row in rows]

    def last_published_run(self, *, skip_idle: bool = False) -> RunRow | None:
        """The newest run the health sinks were sent, however far back: not a dry run, and not a
        reviewed diff refused because the settings changed (`stale`, local only, stored with no
        diff). What Home Assistant's retained record shows.

        ``skip_idle`` also passes over `paused` and `skipped` runs, which say nothing about the
        library: what a `notify = "problems"` webhook compares this run against, so a
        paused tick between an error and the next run neither hides the error nor fakes a recovery.
        """
        idle = "AND status NOT IN ('paused', 'skipped')" if skip_idle else ""
        row = self._conn.execute(
            f"""
            SELECT id, record_json, {_RUN_FACTS} AS facts FROM runs
            WHERE json_extract(record_json, '$.dry_run') = 0 AND NOT (status = 'stale' AND diff_json IS NULL)
            {idle}
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return _run_row(row) if row is not None else None

    def run_by_id(self, run_id: int) -> tuple[RunRow, dict[str, Any] | None] | None:
        """One run by id: its `RunRow` (as `run_history` returns one) and its diff, parsed from
        JSON but not decoded into a `Diff` - `shell.diff_io.diff_from_run_dict` does that, to keep
        this adapter free of the shell's own diff format. `None` when no run has this id.

        For the Status page's "What changed" and `/runs/<id>`: read-only.
        """
        row = self._conn.execute(
            f"SELECT id, record_json, diff_json, {_RUN_FACTS} AS facts FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        diff_raw = row["diff_json"]
        diff = json.loads(diff_raw) if diff_raw is not None else None
        return _run_row(row), diff if isinstance(diff, dict) else None

    def run_id_in_job(self, started: float, finished: float, *, grace_s: float = 30) -> int | None:
        """The one applied run a finished job published, or `None` - never the nearest guess.

        Job history keeps no recorded run id, but it needs none: the job runner runs one job
        at a time, and a run's record is published by that job's own child, so it falls inside
        the job's lifetime (`grace_s` past `finished` covers the child stamping `ts` moments
        before it exits). Dry runs, and `skipped`/`paused` records (published by a *different*
        process that found the lock held or the schedule off), are never the job's own run. Zero
        or several candidates both mean "not sure", and a wrong "What changed" list would be
        worse than no link.
        """
        rows = self._conn.execute(
            "SELECT id FROM runs WHERE json_extract(record_json, '$.dry_run') = 0 "
            "AND status NOT IN ('skipped', 'paused') AND ts BETWEEN ? AND ?",
            (started, finished + grace_s),
        ).fetchall()
        return int(rows[0]["id"]) if len(rows) == 1 else None

    def last_scheduled_fire(self) -> datetime | None:
        """When the in-service scheduler last fired, if it ever has.

        Read at startup to decide the missed-fire catch-up (`likearr.web.schedule`): `None` means
        this service has never fired before (including a v5-or-earlier file, which has no row),
        and that is deliberately not a missed fire - only a gap after a recorded fire is.
        """
        row = self._conn.execute("SELECT last_fire FROM scheduler_state WHERE id = 1").fetchone()
        return datetime.fromisoformat(row["last_fire"]) if row is not None else None

    def record_scheduled_fire(self, when: datetime) -> None:
        """Record `when` as the scheduler's last fire, and clear any earlier `cancelled` mark: a
        fresh fire has not been cancelled yet."""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO scheduler_state (id, last_fire, cancelled) VALUES (1, ?, 0) "
                "ON CONFLICT (id) DO UPDATE SET last_fire = excluded.last_fire, cancelled = 0",
                (when.astimezone(UTC).isoformat(),),
            )

    def scheduled_fire_cancelled(self) -> bool:
        """Whether the last recorded fire (`last_scheduled_fire`) was cancelled by a redeploy
        while still planning, and so still needs to run. `False` with no row
        yet, matching `last_scheduled_fire`."""
        row = self._conn.execute("SELECT cancelled FROM scheduler_state WHERE id = 1").fetchone()
        return bool(row["cancelled"]) if row is not None else False

    def mark_scheduled_fire_cancelled(self) -> None:
        """Mark the last recorded fire cancelled: the missed-fire catch-up
        (`scheduled_fire_cancelled`) then treats *that* fire's own timestamp as still due, rather
        than the schedule's next slot after it. A no-op with no row - nothing has fired yet for
        there to be anything to mark."""
        with self.transaction():
            self._conn.execute("UPDATE scheduler_state SET cancelled = 1 WHERE id = 1")

    def first_apply_at(self) -> datetime | None:
        """When a hand `run --apply` first completed, if one ever has. `None` holds every
        scheduled run (`shell.run.run_command`), and the Status and Settings pages say so."""
        row = self._conn.execute("SELECT applied_at FROM first_apply WHERE id = 1").fetchone()
        return datetime.fromisoformat(row["applied_at"]) if row is not None else None

    def record_first_apply(self, when: datetime) -> None:
        """Record `when` as the first hand apply, unless one is already recorded: the first stays
        the first. Written by `run_command` only, after a hand `run --apply` completes its apply -
        never by a scheduled run, and never by `adopt --apply`, which is not a reviewed run of the
        plan."""
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO first_apply (id, applied_at) VALUES (1, ?)",
                (when.astimezone(UTC).isoformat(),),
            )

    def artist_names(self) -> dict[str, str]:
        """Artist MBID to name, as the resolver last saw them.

        A diff names its releases' artists by MBID only; the cached resolutions carry each release
        group's artist name, which is enough to label a row without asking Lidarr. Read-only.
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT json_extract(json, '$.release_group.artist_mbid') AS mbid,
                            json_extract(json, '$.release_group.artist_name') AS name
            FROM resolutions
            WHERE json_extract(json, '$.release_group.artist_mbid') IS NOT NULL
            """
        ).fetchall()
        return {str(row["mbid"]): str(row["name"]) for row in rows if row["name"]}

    def last_plan(self) -> LastPlan | None:
        """The guards and the shrink choice of the newest run that planned against the world as it
        was: a stored diff, and not a stale refusal (which stores the old saved diff). One row, one
        json_extract - the /plan page needs nothing else from the diffs."""
        row = self._conn.execute(
            """
            SELECT json_extract(diff_json, '$.guards', '$.accept_shrink') AS facts FROM runs
            WHERE diff_json IS NOT NULL AND status != 'stale'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        guards, accept = json.loads(row["facts"]) if row["facts"] else (None, None)
        return LastPlan(
            guards=tuple(
                Guard(
                    code=str(g.get("code", "")),
                    message=str(g.get("message", "")),
                    blocked_unmonitors=int(g.get("blocked_unmonitors") or 0),
                    subject=str(g.get("subject") or ""),
                )
                for g in guards or []
                if isinstance(g, dict)
            ),
            accept_shrink=bool(accept),
        )

    def release_titles(self) -> dict[str, str]:
        """Release group MBID to title, as the resolver last saw them.
        Labels a plan's rows that carry only a key; read-only."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT json_extract(json, '$.release_group.mbid') AS mbid,
                            json_extract(json, '$.release_group.title') AS title
            FROM resolutions
            WHERE json_extract(json, '$.release_group.mbid') IS NOT NULL
            """
        ).fetchall()
        return {str(row["mbid"]): str(row["title"]) for row in rows if row["title"]}

    def release_details(self) -> dict[str, tuple[str, str, tuple[str, ...], str]]:
        """Release group MBID to (title, primary type, secondary types, first release date), for
        every release group the cached resolutions name - the one each intent resolved to and the
        one Spotify named for it. Labels a plan's rows ("Tease Me (single, 1993)"). Read-only."""
        out: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
        for path in ("$.release_group", "$.source_release_group"):
            rows = self._conn.execute(
                f"SELECT json_extract(json, '{path}') AS rg FROM resolutions "
                f"WHERE json_extract(json, '{path}') IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    rg = json.loads(row["rg"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(rg, dict) or not rg.get("mbid"):
                    continue
                out.setdefault(
                    str(rg["mbid"]),
                    (
                        str(rg.get("title") or ""),
                        str(rg.get("primary_type") or ""),
                        tuple(str(t) for t in rg.get("secondary_types") or []),
                        str(rg.get("first_release_date") or ""),
                    ),
                )
        return out

    def runs(self, limit: int = 50) -> list[HealthRecord]:
        """The most recent `limit` health records, newest first."""
        rows = self._conn.execute("SELECT record_json FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_health_record_from_json(row["record_json"]) for row in rows]
