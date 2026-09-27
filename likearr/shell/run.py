"""The run loop: plan, apply, and the health record that always gets published.

`plan` is a read: sources, resolver, desired state, Lidarr view, diff. It writes nothing to
Lidarr and only the harmless parts of state (the resolution cache and the pending clock).

`apply` is the only code in likearr with side effects on Lidarr. It executes exactly the diff it
was given, in a fixed order, committing its state writes batch by batch so a crash halfway
through leaves a consistent picture that the next run finishes rather than a lie it has to
recover from.

This module is the command that ties them together under the run lock. The parts live in their
own modules (#156): `plan` in `shell.plan`, `apply` in `shell.apply`, their results in
`shell.run_types`, and the health record and the printed summaries in `shell.run_report`. The
names other modules import are re-exported here.

Three invariants hold on every path through this module:

- **A source error means nothing happens.** Not a partial apply, not a "monitors only" apply:
  a `SourceError` publishes a health record with ``spotify_ok=false`` and exits 1, before a
  single Lidarr write.
- **Ownership is only claimed for what likearr itself flipped.** An album already monitored when
  likearr looked stays unowned; claiming it would hand likearr the right to unmonitor something a
  human chose.
- **Every exit publishes a health record.** Including an unexpected exception, whose class and
  redacted message become the record's `message`.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from likearr.adapters.health import publish_all
from likearr.adapters.http import redact
from likearr.adapters.lock import LockHeld, run_lock
from likearr.config import Config
from likearr.core.desire import CATALOGUE_TOO_LARGE_STEP
from likearr.core.diff import best_reason_key, is_catalogue_gap, is_recent_catalogue_gap
from likearr.core.health import (
    HealthDelta,
    HealthVerdict,
    Observation,
    classify,
    compare,
    lidarr_metadata_outage,
    next_baseline,
    source_set,
)
from likearr.models import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_GUARDED,
    EXIT_OK,
    EXIT_STALE,
    RESOLVER_VERSION,
    ArtistResolution,
    Diff,
    Fingerprint,
    HealthRecord,
    Resolution,
    RunStatus,
)
from likearr.ports import HealthSink, LidarrError, QuotaExceeded, SourceError
from likearr.shell import last_run
from likearr.shell.apply import apply
from likearr.shell.context import Context
from likearr.shell.diff_io import DiffFileError, write_diff
from likearr.shell.plan import plan, tagged_without_state
from likearr.shell.run_report import (
    CONDITION_TEXT,
    NAMES_SHOWN,
    _artist_labels,
    _publish,
    _record,
    _record_from_plan,
    print_applied,
    print_plan,
)
from likearr.shell.run_types import (
    ApplyResult,
    ApplyStopped,
    ConfigStaleError,
    PlanResult,
    changes_made,
    planned_changes,
)

__all__ = [
    "CONDITION_TEXT",
    "FIRST_APPLY_MESSAGE",
    "NAMES_SHOWN",
    "ApplyResult",
    "PlanResult",
    "apply",
    "plan",
    "run_command",
    "scheduled_run_without_state",
    "tagged_without_state",
]

log = logging.getLogger(__name__)

DEFAULT_DIFF_PATH = Path("diff.json")

# ---------------------------------------------------------------------------- the command


def run_command(
    ctx: Context,
    *,
    now: datetime | None = None,
    out: Path = DEFAULT_DIFF_PATH,
    apply_path: Path | None = None,
    do_apply: bool = False,
    scheduled: bool = False,
    force: bool = False,
    accept_shrink: bool = False,
    accept_health: bool = False,
) -> int:
    """`likearr run`: plan (and optionally apply), under the run lock, always publishing health.

    Args:
        do_apply: execute rather than just plan. With `apply_path` it executes that reviewed
            file; without one (only allowed together with `scheduled`) it plans and applies in
            one go.
        scheduled: unattended run. Caps unmonitors, and is the only mode allowed to apply
            without a reviewed diff file. Finding the run lock held is a ``skipped`` status with
            exit 0 for it, and an error for a run a human started.
        accept_shrink: accept the source and artist shrinks this plan sees, so their guards are not
            applied and the diff records it. Only on a hand-run plan: refused with ``scheduled``
            and with an apply, where the reviewed diff already carries the decision.
        accept_health: fold this run's discrete faults - a name collision, a skipped artist, an
            oversized catalogue - into the health baseline, so they stop being reported as new.
            The mirror image of ``accept_shrink``: it needs an apply (only an apply writes the
            baseline) and is refused with ``scheduled``, because acceptance is a human act and a
            cron line carrying it would silence the signal for good.

    Returns:
        A `likearr.models` ``EXIT_*`` code. Never raises for an expected failure.
    """
    now = now or datetime.now(UTC)
    dry_run = not do_apply
    # An apply that failed anywhere but inside `_execute` (ApplyStopped) changed nothing.
    nothing_changed: int | None = None if dry_run else 0
    unchanged: bool | None = None if dry_run else False

    if accept_shrink and (scheduled or do_apply):
        # A usage error, refused before anything starts, so nothing is published. A guard that an
        # unattended run could switch off would be no guard, and an apply has no plan to accept.
        why = "--scheduled" if scheduled else "--apply"
        log.error("--accept-shrink is for a hand-run plan and cannot be combined with %s", why)
        return EXIT_ERROR

    if accept_health and (scheduled or not do_apply):
        # Refused for the same reason, from the other direction. Only an apply writes the
        # baseline, so on a plan the flag would silently do nothing; and in a cron line it would
        # fold every new fault in on the spot, leaving the health signal permanently mute - which
        # is the defect this whole mechanism exists to remove.
        why = "--scheduled" if scheduled else "a dry run"
        log.error("--accept-health is for a hand-run apply and cannot be combined with %s", why)
        return EXIT_ERROR

    if scheduled and (held := _held(ctx.config, first_applied=lambda: ctx.state.first_apply_at() is not None)):
        # Checked before the lock is even requested, not inside it: a paused scheduled run does no
        # work at all, so there is nothing to hold the lock for, and it must not sit behind a hand
        # run that is already applying. A hand run (`run`, `run --apply`, no `--scheduled`) never
        # reaches this check: pause stops the cron line, not the user. `ts` still has to
        # stay fresh for the HA dead-man, so this still publishes, exactly like the skipped-lock
        # path below. The first-apply gate (#111) is the same check for the same reasons, and it
        # comes before `plan`, so a fire before Spotify is connected is `paused`, not `error`.
        log.info("scheduled run skipped: %s", held)
        _publish(ctx, _held_record(held, dry_run=dry_run), diff=None)
        return EXIT_OK

    if setup_needed := ctx.config.lidarr.library_needed:
        # Issue #3: a first start has no root folder or quality profile until one is picked, and
        # a plan without them could not add an artist anywhere. Refused, not skipped, so a
        # scheduled run past the first-apply gate still turns the health sensor.
        log.error("refusing to run: %s", setup_needed)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=setup_needed,
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_ERROR

    try:
        with run_lock(ctx.lock_path):
            return _run_locked(
                ctx,
                now=now,
                out=out,
                apply_path=apply_path,
                do_apply=do_apply,
                scheduled=scheduled,
                force=force,
                accept_shrink=accept_shrink,
                accept_health=accept_health,
            )
    except LockHeld as exc:
        if scheduled:
            # Two cron fires overlapping (a long apply, a manual run in progress) is the run in
            # progress doing its job, not a failure, and it must not light the health sensor.
            log.warning("%s; skipping this scheduled run", exc)
            _publish(
                ctx,
                _record(
                    status=RunStatus.SKIPPED,
                    exit_code=EXIT_OK,
                    message="another run holds the lock",
                    dry_run=dry_run,
                    changes_made=nothing_changed,
                    lidarr_changed=unchanged,
                ),
                diff=None,
            )
            return EXIT_OK
        log.error("%s", exc)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_BUSY,
                message="another run holds the lock",
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_BUSY
    except ApplyStopped as stop:
        made = changes_made(stop.applied)
        cause = stop.cause
        why = redact(str(cause)) if isinstance(cause, LidarrError) else f"{type(cause).__name__}: {redact(str(cause))}"
        if not isinstance(cause, LidarrError):
            log.debug("%s", "".join(traceback.format_exception(cause)))
        if made and made >= stop.planned:
            # Every planned change reached Lidarr (a batch it applied but then answered with an
            # error, #174) - "stopped part-way" would be wrong, nothing was left undone.
            message = (
                f"the apply finished: all {stop.planned} planned changes were made, but confirming it failed: {why}"
            )
        elif made:
            message = f"the apply stopped part-way: {made} of {stop.planned} changes made: {why}"
        elif stop.applied.lidarr_written:
            message = (
                "the apply stopped part-way: Lidarr settings may have changed, but none of the "
                f"{stop.planned} planned changes was made: {why}"
            )
        else:
            message = f"the apply failed before changing anything: {why}"
        log.error("%s", message)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=message,
                lidarr_ok=not isinstance(cause, LidarrError),
                counts={
                    "monitored": stop.applied.monitored,
                    "unmonitored": stop.applied.unmonitored,
                    "added": stop.applied.added,
                    "new_items_none": stop.applied.new_items_none,
                },
                dry_run=False,
                changes_made=made,
                changes_planned=stop.planned,
                lidarr_changed=stop.applied.lidarr_written or made > 0,
            ),
            diff=None,
        )
        return EXIT_ERROR
    except QuotaExceeded as exc:
        if scheduled:
            # A spent quota (detected since #61) is not this run's fault and
            # will not clear before the next regular slot, so it exits clean rather than error -
            # and, critically, the scheduler must not treat it as a missed fire: retrying sooner
            # only spends more of a quota that is already gone. A hand run keeps today's behaviour
            # (below): a person asked for this, and an error is the honest answer.
            log.warning("scheduled run skipped: %s", exc)
            _publish(
                ctx,
                _record(
                    status=RunStatus.SKIPPED,
                    exit_code=EXIT_OK,
                    message="Spotify quota exceeded",
                    spotify_ok=False,
                    dry_run=dry_run,
                    changes_made=nothing_changed,
                    lidarr_changed=unchanged,
                ),
                diff=None,
            )
            return EXIT_OK
        log.error("source read failed: %s", exc)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=redact(str(exc)),
                spotify_ok=False,
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_ERROR
    except SourceError as exc:
        log.error("source read failed: %s", exc)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=redact(str(exc)),
                spotify_ok=False,
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_ERROR
    except (LidarrError, DiffFileError) as exc:
        log.error("%s", exc)
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=redact(str(exc)),
                lidarr_ok=not isinstance(exc, LidarrError),
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_ERROR
    except Exception as exc:
        log.error("unexpected failure: %s: %s", type(exc).__name__, redact(str(exc)))
        log.debug("%s", traceback.format_exc())
        _publish(
            ctx,
            _record(
                status=RunStatus.ERROR,
                exit_code=EXIT_ERROR,
                message=f"{type(exc).__name__}: {redact(str(exc))}",
                dry_run=dry_run,
                changes_made=nothing_changed,
                lidarr_changed=unchanged,
            ),
            diff=None,
        )
        return EXIT_ERROR


FIRST_APPLY_MESSAGE = "waiting for your first reviewed apply: connect Spotify, then review and apply your first plan"
"""The `paused` message of a scheduled run held until a hand `run --apply` has completed (#111).
On a new install the schedule is on from the start, and without this the first unattended fire
after Connect Spotify would apply the whole first plan - every artist add and every monitor -
with nobody having seen it."""


def _held(config: Config, *, first_applied: Callable[[], bool]) -> str | None:
    """Why a scheduled run must do nothing at all, or ``None`` when it may run.

    The config pause comes first: it is a choice someone made, so its reason is the one worth
    saying, and it needs no state read. `first_applied` is only asked when the schedule is on.
    """
    if not config.schedule.enabled:
        return f"scheduled runs are paused: {config.schedule.paused_reason or 'no reason given'}"
    if not first_applied():
        return FIRST_APPLY_MESSAGE
    return None


def _held_record(message: str, *, dry_run: bool) -> HealthRecord:
    """The `paused` record of a scheduled run that did nothing: no plan, no Lidarr call."""
    return _record(
        status=RunStatus.PAUSED,
        exit_code=EXIT_OK,
        message=message,
        dry_run=dry_run,
        changes_made=None if dry_run else 0,
        lidarr_changed=None if dry_run else False,
    )


def scheduled_run_without_state(config: Config, sinks: Sequence[HealthSink], *, dry_run: bool) -> int | None:
    """`run --scheduled` on an install with no state database yet: publish `paused` and stop.

    Called by the CLI before it builds a `Context`, because building one opens - and so creates -
    the state database, and a fire on a brand-new install must not (the healthcheck reads a missing
    file as "no runs yet", `web.app.healthz`). With no database there has been no apply, so this is
    always held: by the config pause when there is one, else by the first-apply gate. It publishes
    to the sinks only - there is no `runs` table to record it in - so `ts` still stays fresh for the
    HA dead-man. ``None`` when the database exists: `run_command` then decides, as usual.
    """
    if config.state_db.exists():
        return None
    held = _held(config, first_applied=lambda: False) or FIRST_APPLY_MESSAGE
    log.info("scheduled run skipped: %s", held)
    try:
        publish_all(list(sinks), _held_record(held, dry_run=dry_run))
    except Exception:  # pragma: no cover - sinks are best-effort by contract
        log.warning("publishing the health record failed", exc_info=True)
    return EXIT_OK


def _record_first_apply(ctx: Context, now: datetime) -> None:
    """Record the first hand apply (#111), which lets scheduled applies start. Bookkeeping after an
    apply that already succeeded, so a failure here is logged, never the run's result."""
    try:
        ctx.state.record_first_apply(now)
    except Exception:  # pragma: no cover
        log.warning("recording the first apply in the state database failed", exc_info=True)


def _run_locked(
    ctx: Context,
    *,
    now: datetime,
    out: Path,
    apply_path: Path | None,
    do_apply: bool,
    scheduled: bool,
    force: bool,
    accept_shrink: bool,
    accept_health: bool,
) -> int:
    if not do_apply:
        result = plan(ctx, now=now, scheduled=scheduled, accept_shrink=accept_shrink)
        write_diff(result.diff, out)
        _, delta, verdict = _assess(ctx, result, None)
        print_plan(result, out, delta)
        exit_code = _exit_code_of(verdict.status)
        _publish(
            ctx,
            _record_from_plan(result, verdict=verdict, delta=delta, exit_code=exit_code, applied=None, dry_run=True),
            diff=result.diff,
        )
        _record_for_explain(ctx, result, now=now)
        return exit_code

    try:
        exit_code, applied, fresh, diff = apply(ctx, apply_path, now=now, scheduled=scheduled, force=force)
    except ConfigStaleError as exc:
        log.error("%s", exc)
        # A usage outcome, not a fault: nothing was planned or changed, and the remedy is a
        # re-plan the user is about to make. So stdout and the runs table only - publishing it to
        # a retained sink would light Home Assistant's amber (`stale` is a member) and reset its
        # dead-man's switch for a run that did nothing, just as a hand dry-run would (issue #19).
        _publish(
            ctx,
            _record(status=RunStatus.STALE, exit_code=EXIT_STALE, message=str(exc), dry_run=False),
            diff=None,
            local_only=True,
        )
        return EXIT_STALE
    if exit_code == EXIT_STALE:
        # Nothing was applied, so nothing is observed and the baseline is not touched.
        stale_delta = compare(_observe(fresh, None), ctx.state.health_baseline(), _fingerprint(ctx, fresh))
        _publish(
            ctx,
            _record_from_plan(
                fresh,
                verdict=HealthVerdict(RunStatus.STALE, ()),
                delta=stale_delta,
                exit_code=EXIT_STALE,
                applied=None,
                dry_run=False,
                message=f"the diff at {apply_path} no longer matches Spotify and Lidarr; re-plan before applying",
            ),
            diff=diff,
        )
        _record_for_explain(ctx, fresh, now=now, refused=True)  # nothing applied: what the fresh plan saw
        return EXIT_STALE

    print_applied(applied, diff)
    if not scheduled:
        # A hand `run --apply` of a reviewed diff (the browser's Apply, or a terminal) that got
        # through its apply step: from here on, scheduled applies may go ahead (#111).
        _record_first_apply(ctx, now)
    fresh = replace(fresh, lidarr_metadata_ok=applied.lidarr_metadata_ok)
    observation, delta, verdict = _assess(ctx, fresh, applied, diff)
    exit_code = _exit_code_of(verdict.status)
    # Before publishing, so the record reports the write that happened rather than the one intended.
    advanced = _advance_baseline(ctx, observation, fresh, accept=accept_health)
    _publish(
        ctx,
        _record_from_plan(
            fresh,
            verdict=verdict,
            delta=delta,
            exit_code=exit_code,
            applied=applied,
            dry_run=False,
            message=_applied_message(applied, diff),
            baseline_advanced=advanced,
            planned=planned_changes(diff, allow_unmonitors=not diff.guarded),
            executed=diff,
        ),
        diff=diff,
    )
    _record_for_explain(ctx, fresh, now=now, executed=diff, applied=applied)
    return exit_code


def _record_for_explain(
    ctx: Context,
    result: PlanResult,
    *,
    now: datetime,
    executed: Diff | None = None,
    applied: ApplyResult | None = None,
    refused: bool = False,
) -> None:
    """Keep what this run saw for `explain --from-last-run` (see `shell.last_run`). Last, and
    best-effort: it never raises, and nothing about the run depends on it."""
    last_run.record_last_run(
        last_run.facts_path(ctx.config),
        lambda: last_run.last_run_facts(
            ran_at=now,
            snapshot=result.snapshot,
            resolutions=result.resolve_result.resolutions,
            artist_resolutions=result.resolve_result.artist_resolutions,
            desired=result.desired,
            view=result.view,
            owned_keys=ctx.state.owned_releases(),
            collisions=(executed or result.diff).name_collisions,
            unmonitor=[u.key for u in (executed or result.diff).unmonitor],
            monitor=[m.key for m in (executed or result.diff).monitor],
            guards=(executed or result.diff).guards,
            executed=executed,
            applied=applied,
            refused=refused,
        ),
    )


def _exit_code_of(status: RunStatus) -> int:
    """Exit codes are unchanged by the health work: only `guarded` is ever non-zero here.

    `degraded` has always been exit 0 and stays exit 0, so nothing downstream of the exit code
    moves when a run that used to report `degraded` starts reporting `ok`.

    A name collision is **degraded**, not guarded, and that has not changed either. Exit 2 means
    "unmonitors were refused", which is the operator's cue to check nothing was lost; a collision
    refuses an *add* and loses nothing, so reporting it as guarded would blunt what exit 2 tells
    them. `projected-wanted` is advisory in the same way: reported in the guards and the message,
    but it moves neither the status nor the exit code, because treating it as guarded would refuse
    every unmonitor on every run once the wanted list passed the limit.
    """
    return EXIT_GUARDED if status is RunStatus.GUARDED else EXIT_OK


def _fingerprint(ctx: Context, result: PlanResult) -> Fingerprint:
    """What must hold for this run's identities to be comparable with the last run's.

    The source component is the snapshot's own count keys, which already spell out the enabled
    sources and every playlist by id - so enabling a source, or adding or removing a playlist,
    moves the fingerprint without any config plumbing.

    The rules component is `ExclusionRules.token`, which is `""` for a default configuration, so
    it moves only when the user actually turns an opt-out on - and then it moves for the same
    reason `liked_track_scope` does. Turning one on unmaps mapped tracks deliberately, easily past
    the 5% jump threshold on a large library, and a run that has been asked a different question has
    not got worse.
    """
    return Fingerprint(
        resolver_version=RESOLVER_VERSION,
        liked_track_scope=ctx.config.rules.liked_track_scope,
        source_set=source_set(result.snapshot.counts),
        rules=ctx.config.rules.exclusions.token,
    )


def _release_identity(item: Resolution | ArtistResolution) -> str:
    rg = getattr(item, "release_group", None)
    return f"{item.intent_key}|{rg.mbid}" if rg is not None else item.intent_key


def _observe(result: PlanResult, applied: ApplyResult | None, executed: Diff | None = None) -> Observation:
    """Everything this run saw that a chronic condition is measured by, keyed by identity.

    Each identity maps to the intent that wants it, so `core.health` can tell a regression (an
    intent that was fine last run and is not now) from the base rate (a brand-new intent that
    never mapped). A failed Lidarr lookup belongs to a search term rather than to an intent, so
    it carries none and always counts.

    `observed` matters as much as the identities: a plan cannot see skipped artists or what Lidarr
    had no album for, and "absent because we could not look" must never read as "absent because it
    was fixed".
    """
    diff = result.diff
    unmapped: dict[str, str] = {}
    gaps: dict[str, str] = {}
    recent_gaps: dict[str, str] = {}
    too_large: dict[str, str] = dict.fromkeys(result.catalogue_too_large, "")
    for item in diff.unmapped:
        if item.step == CATALOGUE_TOO_LARGE_STEP:
            artist_mbid = getattr(item, "artist_mbid", None)
            too_large[artist_mbid or item.intent_key] = ""
        elif is_recent_catalogue_gap(item):
            recent_gaps[_release_identity(item)] = item.intent_key
        elif is_catalogue_gap(item):
            gaps[_release_identity(item)] = item.intent_key
        else:
            unmapped[_release_identity(item)] = item.intent_key

    identities: dict[str, Mapping[str, str]] = {
        "unmapped": unmapped,
        "catalogue_gaps": gaps,
        "catalogue_gaps_recent": recent_gaps,
        "lidarr_metadata": dict.fromkeys(result.lidarr_metadata_failures, ""),
        "name_collisions": {f"{c.wanted_mbid}|{c.existing_mbid}": "" for c in diff.name_collisions},
        "catalogue_too_large": too_large,
    }
    observed = set(identities)

    if applied is not None:
        # `unmapped_in_lidarr` is `artist/rg` only, so the intent comes back off the monitor item
        # that produced it, using the *same* representative-reason rule `core.diff` stamped on the
        # unmapped entries - two rules here would silently stop identities lining up across runs.
        monitored = [*diff.monitor, *(executed.monitor if executed is not None else ())]
        intent_of = {f"{m.key.artist_mbid}/{m.key.rg_mbid}": best_reason_key(m.reasons) for m in monitored}
        identities["absent_in_lidarr"] = {k: intent_of.get(k, "") for k in applied.unmapped_in_lidarr}
        identities["skipped_artists"] = dict.fromkeys(applied.skipped_artists, "")
        observed |= {"absent_in_lidarr", "skipped_artists"}

    return Observation(
        identities=identities,
        intents=frozenset(result.intent_keys),
        observed=frozenset(observed),
    )


def _assess(
    ctx: Context, result: PlanResult, applied: ApplyResult | None, executed: Diff | None = None
) -> tuple[Observation, HealthDelta, HealthVerdict]:
    """Observe, compare against the baseline, and decide the status. Writes nothing.

    Two diffs, deliberately, on a reviewed apply. The conditions are observed from `result`, the
    fresh plan, because that is what is true of the library *now*. Whether the run was guarded
    comes from `executed`, the reviewed diff that was actually carried out, because that is what
    really blocked unmonitors - a diff planned with `--accept-shrink` carries no shrink guard, and
    reading the fresh plan's guards instead would exit 2 on the very apply that accepted them.
    """
    observation = _observe(result, applied, executed)
    fingerprint = _fingerprint(ctx, result)
    delta = compare(observation, ctx.state.health_baseline(), fingerprint)
    verdict = classify(
        delta,
        spotify_schema_ok=result.spotify_schema_ok,
        mb_ok=result.mb_ok,
        lidarr_outage=lidarr_metadata_outage(result.lidarr_metadata_attempts, result.lidarr_metadata_attempt_failures),
        guarded=(executed if executed is not None else result.diff).guarded,
        ratio=ctx.config.guards.unmapped_ratio_amber,
    )
    return observation, delta, verdict


def _advance_baseline(ctx: Context, observation: Observation, result: PlanResult, *, accept: bool) -> bool:
    """Record what this run saw, so the next run has something to compare against.

    Only an apply calls this. A dry run that moved the line would mean the apply the operator
    actually cares about compares against itself and reports nothing - the same reasoning that
    keeps the shrink baselines out of dry runs.

    Returns whether the write happened, because the health record says so and must not claim a
    baseline it does not have: a run that reported `baseline_advanced` after a failed write would
    leave the next run comparing against stale identities with nothing in the payload to explain
    the deltas.
    """
    try:
        ctx.state.record_health_baseline(
            next_baseline(observation, ctx.state.health_baseline(), _fingerprint(ctx, result), accept=accept)
        )
    except Exception:  # pragma: no cover - never let bookkeeping fail a run that already succeeded
        log.warning("recording the health baseline failed", exc_info=True)
        return False
    return True


MESSAGE_NAMES = 5
"""How many artists the health record's message names under one count. The record goes to Home
Assistant and the Status page, so it names a few and says how many more; the log names them all."""


def _applied_message(applied: ApplyResult, diff: Diff) -> str:
    parts: list[str] = []
    if applied.skipped_artists:
        parts.append(
            f"{len(applied.skipped_artists)} artist(s) skipped: Lidarr metadata unavailable "
            f"({_in_brief(_artist_labels(applied.skipped_artists, diff))})"
        )
    if applied.unknown_artists:
        parts.append(
            f"{len(applied.unknown_artists)} artist(s) not added: Lidarr's metadata does not know them yet, "
            f"tried again next run ({_in_brief(_artist_labels(applied.unknown_artists, diff))})"
        )
    if applied.unmapped_in_lidarr:
        parts.append(f"{len(applied.unmapped_in_lidarr)} release(s) not yet in Lidarr's catalogue")
    if applied.already_monitored:
        parts.append(f"{len(applied.already_monitored)} release(s) already monitored, not claimed")
    return "; ".join(parts)


def _in_brief(labels: Sequence[str]) -> str:
    shown = ", ".join(labels[:MESSAGE_NAMES])
    return f"{shown} and {len(labels) - MESSAGE_NAMES} more" if len(labels) > MESSAGE_NAMES else shown
