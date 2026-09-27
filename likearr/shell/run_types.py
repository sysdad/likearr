"""What a run produces: the plan and apply results, and the errors an apply stops with.

Split out of `shell.run` (#156) so `shell.plan`, `shell.apply` and `shell.run_report` can share them
without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from likearr.core.resolver import ResolveResult
from likearr.models import DesiredState, Diff, LidarrView, SourceSnapshot

# ---------------------------------------------------------------------------- results


@dataclass(slots=True)
class PlanResult:
    """One planning pass: everything the diff was computed from, kept for the other commands."""

    diff: Diff
    snapshot: SourceSnapshot
    desired: DesiredState
    resolve_result: ResolveResult
    view: LidarrView
    spotify_schema_ok: bool
    mb_ok: bool
    lidarr_metadata_ok: bool
    lean_profile_id: int | None
    full_profile_id: int | None
    intent_keys: tuple[str, ...] = ()
    """Every intent this run read, for the health baseline's regression test."""
    lidarr_metadata_failures: tuple[str, ...] = ()
    """Which Lidarr metadata lookups failed, not just that one did."""
    catalogue_too_large: tuple[str, ...] = ()
    """Followed artists past MusicBrainz's browse ceiling."""
    mb_stale_served: int = 0
    """MusicBrainz lookups answered from an expired cache entry because the refetch failed."""
    lidarr_metadata_attempts: int = 0
    """Lidarr metadata lookups genuinely asked of Lidarr this run (issue #18); excludes a term
    skipped because it is still inside its negative-cache TTL."""
    lidarr_metadata_attempt_failures: int = 0
    """Of `lidarr_metadata_attempts`, how many failed. `core.health.lidarr_metadata_outage`'s input."""
    tagged_without_state: tuple[str, ...] = ()
    """Artists carrying likearr's Lidarr tag with no `owned_artists` row (#175): a lost or replaced
    state database. Reported in the log and the run record; it changes nothing else."""

    @property
    def degraded(self) -> bool:
        return not (self.spotify_schema_ok and self.mb_ok and self.lidarr_metadata_ok)


@dataclass(slots=True)
class ApplyResult:
    """What `apply` actually did, as opposed to what the diff proposed."""

    added: int = 0
    monitored: int = 0
    unmonitored: int = 0
    ratcheted: int = 0
    new_items_none: int = 0
    """Artists whose "Monitor New Albums" was set to None (phase c): the artist ids sent."""
    artists_monitored: int = 0
    refreshed: int = 0
    """Followed artists refreshed to chase a recent release Lidarr's catalogue did not hold."""
    refresh_failures: int = 0
    """Of those, the refreshes that failed or outlasted their wait.

    Counted, never a skip: the artist keeps its monitors for the run (see `_execute` phase d3)."""
    skipped_artists: list[str] = field(default_factory=list)
    """Artist MBIDs skipped because Lidarr's metadata server failed on them this run."""
    unknown_artists: list[str] = field(default_factory=list)
    """Artist MBIDs Lidarr refused to add because its metadata server does not know them yet
    (`LidarrArtistUnknown`). Skipped this run and tried again next run, like `skipped_artists`,
    but neither a metadata outage nor a class-B skip: it can last weeks for an artist new to
    MusicBrainz, and a run that degraded for all that time would be the permanent amber again."""
    unmapped_in_lidarr: list[str] = field(default_factory=list)
    """``artist_mbid/rg_mbid`` pairs Lidarr still has no album for; retried next run."""
    already_monitored: list[str] = field(default_factory=list)
    """Releases that were already monitored when likearr looked, so ownership was NOT claimed."""
    lidarr_metadata_ok: bool = True
    lidarr_written: bool = False
    """Set before any call to Lidarr that is not a known read (`_WriteWatch`): tags and profiles
    created, profiles set, new-item monitoring changed, artists added or re-monitored. What makes
    "changed nothing" true or false, beyond the counted changes (#54)."""


def changes_made(applied: ApplyResult) -> int:
    """What an apply changed in Lidarr, counted the way `planned_changes` counts what it meant to:
    artists added, releases monitored and unmonitored, profiles ratcheted, and artists whose
    "Monitor New Albums" was set to None. Re-monitoring an artist follows from an add or a ratchet,
    so it is not counted twice."""
    return applied.added + applied.monitored + applied.unmonitored + applied.ratcheted + applied.new_items_none


def planned_changes(diff: Diff, *, allow_unmonitors: bool = True) -> int:
    """The changes a diff asks of Lidarr, counted the way `changes_made` counts what an apply did
    (a guarded apply leaves its unmonitors out)."""
    unmonitors = len(diff.unmonitor) if allow_unmonitors else 0
    return len(diff.add_artists) + len(diff.monitor) + unmonitors + len(diff.ratchets) + len(diff.set_new_items_none)


_LIDARR_READS = frozenset(
    {
        "version",
        "check_version",
        "load_view",
        "load_albums",
        "root_folders",
        "track_files",
        "import_lists",
        "command_queue",
        "lookup_release_group",
        "search_release_group",
        "search_release_group_candidates",
        "artist_track_file_records",
        # Get-or-create, and almost always only a get: `_execute` counts the create itself, when the
        # plan's view shows the tag or profile missing.
        "ensure_tag",
        "ensure_metadata_profile",
    }
)
"""The Lidarr calls that only read. Anything else an apply calls counts as a write."""


class _WriteWatch:
    """`ctx.lidarr` for an apply: marks `result.lidarr_written` before any call that is not a known
    read, so a new write method is counted by default rather than missed."""

    def __init__(self, inner: object, result: ApplyResult) -> None:
        self._inner = inner
        self._result = result

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name.startswith("_") or name in _LIDARR_READS or not callable(attr):
            return attr
        result = self._result

        def write(*args: Any, **kwargs: Any) -> Any:
            result.lidarr_written = True
            return attr(*args, **kwargs)

        return write


class ApplyStopped(Exception):
    """An apply failed after it began changing Lidarr (#54). Carries what it had done by then, so
    the record can say "stopped part-way: N of M changes made" rather than a bare failure."""

    def __init__(self, applied: ApplyResult, planned: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.applied = applied
        self.planned = planned
        self.cause = cause


class ConfigStaleError(Exception):
    """A reviewed diff was planned under a different `[rules]`/`[guards]` than today's.

    Raised by `apply` before it plans anything, so a refusal costs a file read rather than a full
    plan held under the run lock. `run_command` turns it into exit 3, status `stale`.
    """
