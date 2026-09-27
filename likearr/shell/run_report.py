"""What a run reports: the health record it always publishes, and the summary a human reads.

These only format and publish. Split out of `shell.run` (#156).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from likearr import __version__
from likearr.adapters.health import publish_all
from likearr.core.diff import NAME_COLLISION_GUARD
from likearr.core.health import HealthDelta, HealthVerdict, should_notify
from likearr.core.resolver import ARTIST_AMBIGUOUS_NAME_STEP, ARTIST_AMBIGUOUS_STEP
from likearr.models import RESOLVER_VERSION, Diff, HealthRecord, LidarrView, ProfileRatchet, RunStatus
from likearr.shell.context import Context
from likearr.shell.diff_io import diff_summary
from likearr.shell.output import emit
from likearr.shell.plan import _lost_state_message, _mb_errors
from likearr.shell.run_types import ApplyResult, PlanResult, changes_made

# The run's logger, not this module's own: the log lines, the job page's progress parsing
# (`web.app._phase`) and anything filtering on the logger name have always read `likearr.shell.run`.
log = logging.getLogger("likearr.shell.run")

# ---------------------------------------------------------------------------- health records


def _record(
    *,
    status: RunStatus,
    exit_code: int,
    message: str = "",
    spotify_ok: bool = True,
    spotify_schema_ok: bool = True,
    mb_ok: bool = True,
    lidarr_ok: bool = True,
    lidarr_metadata_ok: bool = True,
    counts: Mapping[str, int] | None = None,
    unmapped: int = 0,
    pending_album: int = 0,
    dry_run: bool = True,
    **health: object,
) -> HealthRecord:
    """A health record with everything defaulted, for the paths that never reached a plan.

    `health` carries the change-detection fields straight through to `HealthRecord`; a run that
    failed before planning leaves them all at their defaults, which is the honest answer - it
    observed nothing, so it can say nothing about what moved.
    """
    return HealthRecord(
        ts=int(time.time()),
        version=__version__,
        resolver_version=RESOLVER_VERSION,
        exit_code=exit_code,
        status=status,
        spotify_ok=spotify_ok,
        spotify_schema_ok=spotify_schema_ok,
        mb_ok=mb_ok,
        lidarr_ok=lidarr_ok,
        lidarr_metadata_ok=lidarr_metadata_ok,
        counts=dict(counts or {}),
        unmapped=unmapped,
        pending_album=pending_album,
        message=message,
        dry_run=dry_run,
        **health,  # type: ignore[arg-type]
    )


def _record_from_plan(
    result: PlanResult,
    *,
    verdict: HealthVerdict,
    delta: HealthDelta,
    exit_code: int,
    applied: ApplyResult | None,
    dry_run: bool,
    message: str = "",
    baseline_advanced: bool = False,
    planned: int | None = None,
    executed: Diff | None = None,
) -> HealthRecord:
    """The health record for a run that got as far as a plan.

    On a dry run the `monitored` / `unmonitored` / `added` / `new_items_none` counts are what the
    diff *proposes*; on an apply they are what actually happened, which is not the same number
    whenever an artist was skipped or a release group was still missing from Lidarr.

    The chronic counts come from `delta.totals` rather than from the diff, because on a dry run
    the apply-only dimensions are unobservable and the baseline's own numbers stand in for them -
    reporting zero there would read as "fixed" when it means "could not look".

    `executed` is the reviewed diff an apply actually carried out, when it differs from `result`'s
    own (fresh, just-rebuilt) plan - the same distinction `_assess` and `_record_for_explain`
    already make. The fallback guard message must come from it too (issue #182): a plan made with
    `--accept-shrink` carries no shrink guard, but the fresh plan rebuilt inside `apply()` still
    shows one, so reading `result.diff` here would tell the operator to accept a shrink they just
    accepted.
    """
    counts = dict(result.snapshot.counts)
    counts["desired"] = len(result.desired.releases)
    counts["intents"] = len(result.intent_keys)
    if applied is None:
        counts["monitored"] = len(result.diff.monitor)
        counts["unmonitored"] = len(result.diff.unmonitor)
        counts["added"] = len(result.diff.add_artists)
        counts["new_items_none"] = len(result.diff.set_new_items_none)
    else:
        counts["monitored"] = applied.monitored
        counts["unmonitored"] = applied.unmonitored
        counts["added"] = applied.added
        counts["new_items_none"] = applied.new_items_none
    unmapped = delta.totals.get("unmapped", 0)
    lost = len(result.tagged_without_state)
    base = "; ".join(p for p in (message or _guard_message(executed or result.diff), _lost_state_message(lost)) if p)
    return _record(
        status=verdict.status,
        exit_code=exit_code,
        message=_message_of(base, verdict, delta),
        spotify_ok=True,
        spotify_schema_ok=result.spotify_schema_ok,
        mb_ok=result.mb_ok,
        lidarr_ok=True,
        lidarr_metadata_ok=applied.lidarr_metadata_ok if applied is not None else result.lidarr_metadata_ok,
        counts=counts,
        unmapped=unmapped,
        pending_album=len(result.diff.pending),
        dry_run=dry_run,
        unmapped_new=delta.new.get("unmapped", 0),
        unmapped_resolved=delta.resolved.get("unmapped", 0),
        unmapped_ratio=round(unmapped / len(result.intent_keys), 3) if result.intent_keys else 0.0,
        regressions=delta.regressions,
        catalogue_gaps=delta.totals.get("catalogue_gaps", 0),
        catalogue_gaps_new=delta.new.get("catalogue_gaps", 0),
        catalogue_gaps_recent=delta.totals.get("catalogue_gaps_recent", 0),
        catalogue_gaps_recent_new=delta.new.get("catalogue_gaps_recent", 0),
        refresh_failures=applied.refresh_failures if applied is not None else 0,
        absent_in_lidarr=delta.totals.get("absent_in_lidarr", 0),
        absent_in_lidarr_new=delta.new.get("absent_in_lidarr", 0),
        lidarr_metadata_errors=delta.totals.get("lidarr_metadata", 0),
        lidarr_metadata_errors_new=delta.new.get("lidarr_metadata", 0),
        mb_errors=_mb_errors(result.resolve_result, result.diff) + result.mb_stale_served,
        skipped_artists=delta.totals.get("skipped_artists", 0),
        skipped_artists_new=delta.new.get("skipped_artists", 0),
        name_collisions=delta.totals.get("name_collisions", 0),
        name_collisions_new=delta.new.get("name_collisions", 0),
        catalogue_too_large=delta.totals.get("catalogue_too_large", 0),
        catalogue_too_large_new=delta.new.get("catalogue_too_large", 0),
        baseline=delta.state,
        baseline_advanced=baseline_advanced,
        new_conditions=list(verdict.conditions),
        changes_made=changes_made(applied) if applied is not None else None,
        changes_planned=planned if applied is not None else None,
        lidarr_changed=(applied.lidarr_written or changes_made(applied) > 0) if applied is not None else None,
        tagged_without_state=lost,
    )


CONDITION_TEXT = {
    "spotify-schema": "a Spotify response was missing fields likearr depends on, or read fewer items than it reported",
    "mb-outage": "MusicBrainz lookups failed this run",
    "lidarr-metadata-outage": "most attempted Lidarr metadata lookups failed this run",
    "new-skipped-artist": "artist(s) newly skipped for a Lidarr metadata failure",
    "new-catalogue-too-large": "followed artist(s) newly too large for MusicBrainz to browse",
    "new-name-collision": "name collision(s) not seen last run",
    "mapping-shortfall-jump": "releases that mapped last run no longer do",
}


def _message_of(base: str, verdict: HealthVerdict, delta: HealthDelta) -> str:
    """Whatever the run already had to say, then what is newly wrong and by how much."""
    parts = [base] if base else []
    counts = {
        "new-skipped-artist": delta.new.get("skipped_artists", 0),
        "new-catalogue-too-large": delta.new.get("catalogue_too_large", 0),
        "new-name-collision": delta.new.get("name_collisions", 0),
        "mapping-shortfall-jump": delta.regressions,
    }
    for condition in verdict.conditions:
        count = counts.get(condition)
        text = CONDITION_TEXT[condition]
        parts.append(f"{count} {text}" if count else text)
    return "; ".join(parts)


def _guard_message(diff: Diff) -> str:
    return "; ".join(g.message for g in diff.guards)


def _publish(ctx: Context, record: HealthRecord, *, diff: Diff | None, local_only: bool = False) -> None:
    """Publish to every sink and record the run. Neither failure may mask the run's own result.

    ``local_only`` keeps a non-dry record off retained/remote sinks too; see `publish_all`.
    """
    try:
        publish_all(ctx.sinks, record, local_only=local_only, notify=_is_news(ctx, record))
    except Exception:  # pragma: no cover - sinks are best-effort by contract
        log.warning("publishing the health record failed", exc_info=True)
    try:
        ctx.state.record_run(record, diff)
    except Exception:  # pragma: no cover
        log.warning("recording the run in the state database failed", exc_info=True)


def _is_news(ctx: Context, record: HealthRecord) -> bool:
    """`should_notify` for this record, against the last published run before it (#112).

    Read before `record_run` stores this one, so "previous" is really the one before. `paused` and
    `skipped` runs are passed over: they say nothing about the library. A failed read counts as
    "no previous run", so a problem still notifies and a clean run stays quiet - the webhook is
    best-effort and must never change the run's result.
    """
    try:
        row = ctx.state.last_published_run(skip_idle=True)
    except Exception:
        log.warning("reading the previous run for the webhook failed", exc_info=True)
        row = None
    previous = (row.record.status, row.record.message) if row is not None else None
    return should_notify(record.status, record.message, previous)


# ---------------------------------------------------------------------------- human output


def print_plan(result: PlanResult, out: Path, delta: HealthDelta | None = None, *, dry_run: bool = True) -> None:
    """The summary a human reads before deciding whether to apply."""
    diff = result.diff
    summary = diff_summary(diff)
    emit(f"likearr plan ({out}):")
    emit(f"  {summary['add_artists']:>6} artists to add")
    emit(f"  {summary['monitor']:>6} releases to monitor")
    emit(f"  {summary['unmonitor']:>6} releases to unmonitor")
    emit(f"  {summary['ratchets']:>6} profile ratchets to Full")
    emit(
        f'  {summary["set_new_items_none"]:>6} artists to set "Monitor New Albums" to None '
        "(their new albums are not auto-monitored)"
    )
    _emit_names(sorted((_artist_name(mbid, result.view) for mbid in diff.set_new_items_none), key=str.casefold))
    emit(f"  {summary['monitor_artists']:>6} unmonitored artists to re-monitor")
    emit(f"  {summary['refresh_artists']:>6} artists to refresh (a recent release Lidarr hasn't got yet)")
    emit(f"  {summary['update_reasons']:>6} reason-set updates (state only)")
    emit(f"  {summary['pending']:>6} pending (liked single with no album yet)")
    emit(f"  {summary['unmapped']:>6} unmapped")
    if summary.get("catalogue_gaps_recent"):
        emit(
            f"  {summary['catalogue_gaps_recent']:>6} recent/future releases not in Lidarr's catalogue yet (refreshing)"
        )
    if summary.get("catalogue_gaps"):
        emit(f"  {summary['catalogue_gaps']:>6} followed-catalogue releases Lidarr doesn't track (informational)")
    emit(f"  {summary['projected_wanted']:>6} projected wanted (monitored, no files)")
    for ratchet in _widening_on_new_items(diff):
        emit(f"  WARNING: {_widening_warning(ratchet)}")
    if diff.accept_shrink:
        emit("  --accept-shrink: the source and artist shrink guards were NOT applied to this plan")
    for guard in diff.guards:
        if guard.code == NAME_COLLISION_GUARD:
            continue  # printed in full, with both artists named, by `print_name_collisions`
        blocked = f" ({guard.blocked_unmonitors} unmonitors refused)" if guard.blocked_unmonitors else ""
        emit(f"  GUARD [{guard.code}]{blocked}: {guard.message}")
    print_name_collisions(diff)
    print_ambiguous_artists(diff)
    if not result.spotify_schema_ok:
        emit(
            "  WARNING: a Spotify response was missing fields likearr depends on, or read fewer items than it reported"
        )
    if not result.mb_ok:
        emit("  WARNING: MusicBrainz lookups failed this run; some intents are unresolved")
    print_health_delta(delta, dry_run=dry_run)
    if diff.is_empty:
        emit("  nothing to do")


NAMES_SHOWN = 20
"""How many artists a plan names under one count before it says how many more there are (the `run`
plan here, and the `adopt` plan)."""


def _emit_names(names: Sequence[str], *, rest: str = "see the diff file") -> None:
    """`names` indented under the count line above them: the first `NAMES_SHOWN`, then how many more."""
    for name in names[:NAMES_SHOWN]:
        emit(f"           {name}")
    if len(names) > NAMES_SHOWN:
        emit(f"           and {len(names) - NAMES_SHOWN} more ({rest})")


def _artist_name(mbid: str, view: LidarrView) -> str:
    artist = view.artists.get(mbid)
    return artist.name if artist is not None and artist.name else mbid


def _widening_on_new_items(diff: Diff) -> list[ProfileRatchet]:
    """The ratchets whose artist the diff also sets "Monitor New Albums" to None on: it was not None
    when planned, so a widening without phase (c) first would monitor every release type it shows.
    Each is warned about in the plan, the apply summary and the log."""
    new_items = set(diff.set_new_items_none)
    return [r for r in diff.ratchets if r.artist_mbid in new_items]


def _widening_warning(ratchet: ProfileRatchet) -> str:
    return (
        f"widening {ratchet.name or ratchet.artist_mbid} to Full shows more release types. "
        '"Monitor New Albums" is set to None first, so none of them is monitored automatically; '
        "albums already monitored stay monitored."
    )


def print_health_delta(delta: HealthDelta | None, *, dry_run: bool) -> None:
    """What moved since the last apply, which is the only part worth reading twice.

    The counts above are the standing state and are often much the same from one run to the next.
    These are the lines that say whether anything is actually different today.

    `dry_run` only changes the wording, but it has to: a plan never writes a baseline, so telling
    the operator it just established one would have them re-running a plan that can never stop
    saying "first run".
    """
    if delta is None:
        return
    emit("")
    if not delta.comparable:
        reason = "no previous run to compare with" if delta.state == "first-run" else delta.state
        established = "the next apply will establish it" if dry_run else "this run establishes the baseline"
        emit(f"  health: {reason}; {established}, so nothing is reported as new")
        return
    moved = [(name, count) for name, count in sorted(delta.new.items()) if count]
    cleared = sum(delta.resolved.values())
    if not moved:
        emit(f"  health: nothing new since the last apply ({cleared} condition(s) cleared)")
        return
    emit("  health: new since the last apply -")
    for name, count in moved:
        emit(f"    {count:>6} {name.replace('_', ' ')}")
    if delta.regressions:
        emit(f"    {delta.regressions:>6} of those are regressions (the intent existed last run)")


def print_name_collisions(diff: Diff) -> None:
    """Artists skipped because Lidarr already holds their name, and what each skip cost.

    Its own section rather than a guard line, because a collision is almost never a duplicate -
    it is a *different* artist the user wanted, and the summary has to make that obvious enough
    to act on. Each line names both artists (with MusicBrainz's disambiguation where there is
    one) and the number of releases that went unmonitored, so the cost of each skip is visible
    at a glance.

    The advice is only what Lidarr can actually do (issue #32). It used to suggest adding the
    artist "under a distinct name", which Lidarr cannot hold: `Artist.ApplyChanges` never copies
    the name, so a refresh puts MusicBrainz's back, and an import with two same-named artists
    throws `MultipleArtistsFoundException`.
    """
    if not diff.name_collisions:
        return
    total = sum(c.dropped_releases for c in diff.name_collisions)
    emit("")
    emit(
        f"  {len(diff.name_collisions)} artist(s) NOT added - Lidarr already has the name"
        f"{'' if all(c.in_lidarr for c in diff.name_collisions) else ', or another new artist wants it'} "
        f"({total} release(s) unmonitored as a result):"
    )
    for collision in sorted(diff.name_collisions, key=lambda c: c.name.casefold()):
        emit(f"    {collision.name!r}: {collision.dropped_releases} release(s) skipped")
        emit(f"      wanted:   {collision.wanted_mbid}{_disambiguation(collision.wanted_disambiguation)}")
        if collision.in_lidarr:
            emit(
                f"      in Lidarr: id {collision.existing_lidarr_id} {collision.existing_mbid}"
                f"{_disambiguation(collision.existing_disambiguation)}"
            )
        else:
            emit(
                f"      also wanted: {collision.existing_mbid}"
                f"{_disambiguation(collision.existing_disambiguation)} (new this run, skipped too)"
            )
    emit("    Lidarr matches incoming downloads by name, so two artists cannot share one: it would")
    emit("    leave every download of either unimportable. Lidarr takes an artist's name from")
    emit("    MusicBrainz, so it cannot hold one under a different name either. Keep the one Lidarr")
    emit("    has, or add both and import the other's downloads by hand with Lidarr's Manual Import,")
    emit("    which may work. If the match looks wrong, `likearr explain <name>` shows why likearr")
    emit("    wanted it.")
    if not all(c.in_lidarr for c in diff.name_collisions):
        emit("    Where both artists are new, likearr adds neither: add the right one in Lidarr by hand,")
        emit("    and the next run keeps it and skips the other.")


def print_ambiguous_artists(diff: Diff) -> None:
    """Followed artists MusicBrainz links to several artists (none of them in the library), or,
    with no link, whose name several MusicBrainz artists share.

    The same treatment as a name collision, and for the same reason: likearr declining to choose
    is only useful if the human can see what it declined between. `detail` already names every
    candidate with its MusicBrainz disambiguation.
    """
    for step, why in (
        (ARTIST_AMBIGUOUS_STEP, "the Spotify page links to several artists"),
        (ARTIST_AMBIGUOUS_NAME_STEP, "no link, and several MusicBrainz artists share the name"),
    ):
        ambiguous = [u for u in diff.unmapped if u.step == step]
        if not ambiguous:
            continue
        emit("")
        emit(f"  {len(ambiguous)} followed artist(s) NOT resolved - {why}:")
        for item in ambiguous:
            emit(f"    {item.detail}")


def _disambiguation(text: str) -> str:
    return f" - {text}" if text else ""


def _artist_labels(mbids: Sequence[str], diff: Diff) -> list[str]:
    """ "Name (mbid)" for each artist an apply skipped. Every skip comes from an add or a ratchet,
    and both carry the artist's name."""
    names = {a.artist_mbid: a.name for a in diff.add_artists} | {r.artist_mbid: r.name for r in diff.ratchets}
    return [f"{names[mbid]} ({mbid})" if names.get(mbid) else mbid for mbid in mbids]


def print_applied(applied: ApplyResult, diff: Diff) -> None:
    """What actually changed."""
    emit("likearr applied:")
    emit(f"  {applied.added:>6} artists added")
    emit(f"  {applied.monitored:>6} releases monitored")
    emit(f"  {applied.unmonitored:>6} releases unmonitored")
    emit(f"  {applied.ratcheted:>6} artists ratcheted to Full")
    emit(f'  {applied.new_items_none:>6} artists set "Monitor New Albums" to None')
    emit(f"  {applied.artists_monitored:>6} artists re-monitored")
    emit(f"  {applied.refreshed:>6} artists refreshed for a recent release (monitored next run)")
    if applied.refresh_failures:
        emit(f"  {applied.refresh_failures:>6} of those refreshes failed (monitors kept; retried after the backoff)")
    if applied.skipped_artists:
        emit(f"  {len(applied.skipped_artists):>6} artists skipped (Lidarr metadata unavailable)")
        _emit_names(_artist_labels(applied.skipped_artists, diff), rest="the log names them all")
    if applied.unknown_artists:
        emit(
            f"  {len(applied.unknown_artists):>6} artists not added: Lidarr's metadata does not know them yet "
            "(tried again next run)"
        )
        _emit_names(_artist_labels(applied.unknown_artists, diff), rest="the log names them all")
    if applied.unmapped_in_lidarr:
        emit(f"  {len(applied.unmapped_in_lidarr):>6} releases not in Lidarr's catalogue yet (retried next run)")
    if applied.already_monitored:
        emit(f"  {len(applied.already_monitored):>6} releases already monitored (ownership not claimed)")
    # From the diff: an apply that reaches this summary set every ratchet's profile, since a failed
    # profile write stops the apply.
    for ratchet in _widening_on_new_items(diff):
        emit(f"  WARNING: {_widening_warning(ratchet)}")
    for guard in diff.guards:
        if guard.code == NAME_COLLISION_GUARD:
            continue  # printed in full, with both artists named, by `print_name_collisions`
        emit(f"  GUARD [{guard.code}]: {guard.message}")
    print_name_collisions(diff)
