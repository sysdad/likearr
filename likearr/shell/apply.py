"""The apply phase of a run: the only code in likearr with side effects on Lidarr.

`apply` executes exactly the diff it was given, in a fixed order, committing its state writes
batch by batch. Every Lidarr write a run makes is in this module. Split out of `shell.run`;
see that module for the invariants every run keeps.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from likearr.adapters.http import redact
from likearr.adapters.lidarr import BATCH_SIZE
from likearr.core.diff import config_changes, is_stale, lidarr_digest, manual_reasons
from likearr.models import (
    EXIT_GUARDED,
    EXIT_OK,
    EXIT_STALE,
    PHASE_MARKER_APPLY,
    RESOLVER_VERSION,
    Diff,
    LidarrArtist,
    LidarrView,
    MonitorRelease,
    OwnedArtist,
    OwnedRelease,
    Profile,
    ReleaseKey,
    UnmonitorRelease,
)
from likearr.ports import (
    CatalogueTooLarge,
    LidarrArtistExists,
    LidarrArtistUnknown,
    LidarrError,
    LidarrMetadataError,
    MetadataError,
)
from likearr.shell.context import Context
from likearr.shell.diff_io import DiffFileError, read_diff
from likearr.shell.plan import _format_refresh_duration, plan
from likearr.shell.run_report import _widening_on_new_items, _widening_warning
from likearr.shell.run_types import (
    ApplyResult,
    ApplyStopped,
    ConfigStaleError,
    PlanResult,
    _WriteWatch,
    planned_changes,
)

# The run's logger, not this module's own: the log lines, the job page's progress parsing
# (`web.app._phase`) and anything filtering on the logger name have always read `likearr.shell.run`.
log = logging.getLogger("likearr.shell.run")

# ---------------------------------------------------------------------------- apply


def apply(
    ctx: Context,
    diff_path: Path | None,
    *,
    now: datetime,
    scheduled: bool,
    force: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, ApplyResult, PlanResult, Diff]:
    """Execute a diff against Lidarr.

    Args:
        diff_path: the reviewed `diff.json` to execute, or ``None`` for a scheduled
            plan-and-apply in one go (`likearr run --scheduled`), which has no file to go stale.
        force: apply a stale diff anyway. Only ever set from ``--force``; the digests exist
            precisely so this is a deliberate act.
        monotonic: the clock the re-plan's resolve-progress line and the add-loop's per-artist
            refresh timing are measured against. Injected so a test never has to
            sleep for real; defaults to the wall clock.

    Returns:
        ``(exit_code, what was applied, the fresh plan, the diff that was executed)``.

    Raises:
        DiffFileError: the diff file is missing or malformed.
        ConfigStaleError: the diff's `[rules]`/`[guards]` differ from today's (and not ``force``).
        SourceError / LidarrError: propagated to `run_command`, which publishes the health record.
    """
    # Everything that can refuse a saved diff on the file alone is checked before planning: a plan
    # is minutes on a warm cache and hours after a resolver bump, held under the run lock, and a
    # refusal decided by a file read must not make the user (or a cron fire) wait for one.
    saved: Diff | None = None
    if diff_path is not None:
        saved = read_diff(diff_path)
        if saved.accept_shrink and scheduled:
            raise DiffFileError(
                f"the diff at {diff_path} was planned with --accept-shrink, which a scheduled run never honours; "
                "apply it by hand"
            )
        if saved.resolver_version != RESOLVER_VERSION:
            raise DiffFileError(
                f"the diff at {diff_path} was made by resolver version {saved.resolver_version}, "
                f"but this likearr is version {RESOLVER_VERSION}; re-plan before applying"
            )
        config_reason = _config_stale_reason(saved, ctx, diff_path)
        if config_reason:
            if not force:
                raise ConfigStaleError(config_reason)
            log.warning("--force: applying %s anyway, although %s", diff_path, config_reason)

    fresh = plan(ctx, now=now, scheduled=scheduled, monotonic=monotonic)

    if saved is None:
        diff = fresh.diff
    else:
        diff = saved
        current_lidarr = lidarr_digest(
            fresh.view,
            diff.monitor,
            diff.unmonitor,
            diff.ratchets,
            diff.set_new_items_none,
            diff.monitor_artists,
            diff.refresh_artists,
            [a.artist_mbid for a in diff.add_artists],
        )
        if is_stale(diff, fresh.snapshot.digest(), current_lidarr):
            if not force:
                log.error("the world moved since %s was planned; nothing was changed", diff_path)
                return EXIT_STALE, ApplyResult(), fresh, diff
            log.warning("--force: applying a stale diff from %s anyway", diff_path)

    guarded = diff.guarded
    result = ApplyResult(lidarr_metadata_ok=fresh.lidarr_metadata_ok)
    watched = replace(ctx, lidarr=cast("Any", _WriteWatch(ctx.lidarr, result)))
    # The single point planning ends and the first Lidarr write begins:
    # everything above this line only reads. `web.jobs.JobRunner` looks for this exact line in a
    # scheduled job's stderr to decide, on a redeploy's SIGTERM, whether the child is still safe to
    # cancel or must be drained. Plain stderr, not `log`, so the line is never reformatted, timed
    # or dropped by a logger's own level filtering.
    sys.stderr.write(PHASE_MARKER_APPLY + "\n")
    sys.stderr.flush()
    try:
        _execute(watched, diff, fresh, now=now, allow_unmonitors=not guarded, result=result, monotonic=monotonic)
    except Exception as exc:
        # Each phase commits as it goes: `result` holds exactly what reached Lidarr before this.
        raise ApplyStopped(result, planned_changes(diff, allow_unmonitors=not guarded), exc) from exc

    source_baseline, followed_baseline = _next_baselines(ctx, diff, fresh)
    with ctx.state.transaction():
        ctx.state.record_source_counts(source_baseline)
        ctx.state.record_followed_counts(followed_baseline)

    exit_code = EXIT_GUARDED if guarded else EXIT_OK
    return exit_code, result, fresh, diff


def _config_stale_reason(diff: Diff, ctx: Context, diff_path: Path) -> str:
    """Why `diff`'s configuration disqualifies it, in words, or `""` when it was planned under today's.

    Checked before planning and so before the digests, and a refusal names the setting that
    moved: Spotify and Lidarr can both be exactly as they were while the user has just denied a
    release the saved plan monitors. A diff that recorded no configuration is refused too - it
    cannot vouch for one, and the remedy is a re-plan: a few minutes on a warm cache, longer after
    a resolver bump.
    """
    changes = config_changes(diff.config_fingerprint, ctx.config.plan_fingerprint)
    if changes is None:
        return (
            f"the diff at {diff_path} was planned by a likearr that did not record its configuration, "
            "so there is no telling whether [rules] or [guards] changed since; re-plan before applying"
        )
    if changes:
        return (
            f"the configuration changed since the diff at {diff_path} was planned ({', '.join(changes)}); "
            "re-plan before applying"
        )
    return ""


def _next_baselines(ctx: Context, diff: Diff, fresh: PlanResult) -> tuple[dict[str, int], dict[str, int]]:
    """The shrink baselines to record after an apply: this run's counts, except where a guard held.

    A source (or followed artist) whose shrink guard refused unmonitors keeps its *previous* count.
    Recording the shrunken one would make the shrink the new normal, and the next run - six hours
    later - would carry out exactly the unmonitors the guard refused: "refused until you look" would
    mean "refused once". A guard holds until the count recovers or a human has looked. That covers a
    source dropped from config too, which would otherwise vanish from the baseline the moment it
    stopped being read. A `schema` guard holds every source: the counts it would record came from
    a response the parser could not fully read.
    """
    sources = dict(fresh.snapshot.counts)
    followed = dict(fresh.desired.catalogue_counts)
    last_sources = ctx.state.last_source_counts()
    last_followed = ctx.state.last_followed_counts()
    for guard in diff.guards:
        if guard.code == "source-shrink" and guard.subject in last_sources:
            sources[guard.subject] = last_sources[guard.subject]
        elif guard.code == "artist-shrink" and guard.subject in last_followed:
            followed[guard.subject] = last_followed[guard.subject]
        elif guard.code == "schema":
            sources = dict(last_sources)
    return sources, followed


def _refresh_timeout_s(ctx: Context, artist_mbid: str, view: LidarrView) -> float:
    """How long to wait for this artist's RefreshArtist: the floor plus an allowance per release group.

    The size is the larger of what MusicBrainz lists for the artist (memoised, and already read
    when planning a followed artist's catalogue) and what Lidarr already holds. A catalogue too
    large for MusicBrainz to be browsed is exactly the huge case this scaling exists for, so it
    gets the ceiling; any other failure to size, or an unknown size, gets the floor. A wait that
    still runs out skips the artist for this run only: Lidarr carries on refreshing, so the
    catalogue is there for the next one.

    Sizing is not a health signal. `CompositeLookup` flags MusicBrainz unhealthy on any failure, so
    the flag is put back: a lookup that only sizes a timeout must not turn the run's `mb_ok` false.
    """
    lidarr = ctx.config.lidarr
    albums = len(view.albums.get(artist_mbid, {}))
    mb_ok = ctx.composite.mb_ok if ctx.composite is not None else True
    try:
        albums = max(albums, len(ctx.lookup.artist_release_groups(artist_mbid)))
    except CatalogueTooLarge:
        return max(lidarr.refresh_timeout_max_s, lidarr.refresh_timeout_s)
    except MetadataError as exc:
        log.debug("cannot size the catalogue of %s for the refresh wait: %s", artist_mbid, exc)
    finally:
        if ctx.composite is not None:
            ctx.composite.mb_ok = mb_ok
    return lidarr.refresh_timeout_for(albums)


def _execute(
    ctx: Context,
    diff: Diff,
    fresh: PlanResult,
    *,
    now: datetime,
    allow_unmonitors: bool,
    result: ApplyResult | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ApplyResult:
    """The eight phases, in order, each committing its state before the next begins. `result` is
    filled in as they go, so a caller holding it knows what was done if a phase raises."""
    result = result if result is not None else ApplyResult(lidarr_metadata_ok=fresh.lidarr_metadata_ok)
    config = ctx.config
    view = fresh.view

    # (a) -------------------------------------------------------------- ids that must exist
    # The quality profile first: checking it only reads, so a missing one stops the apply before
    # anything is created.
    quality_id = view.quality_profiles.get(config.lidarr.quality_profile)
    if quality_id is None and diff.add_artists:
        raise LidarrError(
            f"lidarr has no quality profile named {config.lidarr.quality_profile!r} "
            f"(it has: {', '.join(sorted(view.quality_profiles)) or 'none'})"
        )
    # `ensure_*` is get-or-create: a write only when the plan's view lacked the name.
    if not _named(config.lidarr.tag, view.tags):
        result.lidarr_written = True
    tag_id = ctx.lidarr.ensure_tag(config.lidarr.tag)
    if not _named(config.lidarr.lean_profile, view.metadata_profiles):
        result.lidarr_written = True
    lean_id = ctx.lidarr.ensure_metadata_profile(Profile.LEAN, config.lidarr.lean_profile)
    if not _named(config.lidarr.full_profile, view.metadata_profiles):
        result.lidarr_written = True
    full_id = ctx.lidarr.ensure_metadata_profile(Profile.FULL, config.lidarr.full_profile)

    owned_artists = ctx.state.owned_artists()
    skipped: set[str] = set()  # every artist the later phases leave alone, the unknown ones too
    unknown: set[str] = set()  # of those, the ones Lidarr's metadata does not know yet
    foreign: set[str] = set()  # and the ones someone else added before likearr could
    added_artists: dict[str, LidarrArtist] = {}

    # (b) -------------------------------------------------------------- add artists
    add_total = len(diff.add_artists)
    for add_index, add in enumerate(diff.add_artists, start=1):
        profile_id = full_id if add.profile is Profile.FULL else lean_id
        try:
            artist = ctx.lidarr.add_artist(
                add.artist_mbid,
                add.name,
                root_folder=config.lidarr.root_folder,
                quality_profile_id=quality_id or 0,
                metadata_profile_id=profile_id,
                tag_ids=[tag_id],
            )
        except LidarrArtistUnknown as exc:
            # Not an outage, so `lidarr_metadata_ok` is left alone. Nothing was added, so
            # there is nothing likearr owns to unmonitor; the next plan asks for the add again.
            log.warning("skipping %s (%s) this run: %s", add.name, add.artist_mbid, exc)
            skipped.add(add.artist_mbid)
            unknown.add(add.artist_mbid)
            continue
        except LidarrMetadataError as exc:
            log.warning("skipping %s (%s): lidarr metadata failed while adding: %s", add.name, add.artist_mbid, exc)
            skipped.add(add.artist_mbid)
            result.lidarr_metadata_ok = False
            continue
        except LidarrArtistExists as exc:
            if tag_id not in exc.artist.tags:
                # Added by hand or by an import list since the plan was made. Recording
                # it would make it likearr's for good, and every later run would then force its
                # "Monitor New Albums" to None. Left to whoever added it: no row, no refresh, and
                # none of this plan's monitors for it; the next plan sees it as theirs.
                log.info(
                    "not adding %s (%s): it is already in Lidarr without the %r tag, so someone else "
                    "added it; leaving it and its albums alone this run",
                    add.name,
                    add.artist_mbid,
                    config.lidarr.tag,
                )
                skipped.add(add.artist_mbid)
                foreign.add(add.artist_mbid)
                continue
            # Carrying likearr's tag, it is likearr's own add from a run that stopped between the
            # add and the record below: finish the job.
            artist = exc.artist
        with ctx.state.transaction():
            ctx.state.record_artist(
                OwnedArtist(
                    artist_mbid=add.artist_mbid,
                    lidarr_artist_id=artist.id,
                    added_by_us=True,
                    profile=add.profile,
                    ratcheted_at=now if add.profile is Profile.FULL else None,
                )
            )
        added_artists[add.artist_mbid] = artist
        result.added += 1
        # A RefreshArtist can take minutes: logged whether it succeeds or fails, so
        # the job page's progress line moves either way - the add loop is often the slowest part
        # of an apply on a big library, and a failure here is exactly when "how far along is it"
        # matters most.
        refresh_started = monotonic()
        try:
            ctx.lidarr.refresh_artist(artist, timeout_s=_refresh_timeout_s(ctx, artist.mbid, view))
        except LidarrMetadataError as exc:
            # The artist exists in Lidarr and is recorded as likearr's, but its catalogue is not
            # there yet. Monitoring anything for it now would monitor nothing at all, and
            # unmonitoring from it would read an empty catalogue as "not wanted any more".
            log.warning("skipping %s (%s) this run: RefreshArtist failed: %s", add.name, add.artist_mbid, exc)
            skipped.add(add.artist_mbid)
            result.lidarr_metadata_ok = False
        finally:
            refresh_took = _format_refresh_duration(monotonic() - refresh_started)
            log.info(
                "progress: adding artists %d/%d (%s): last refresh took %s",
                add_index,
                add_total,
                add.name,
                refresh_took,
            )

    result.skipped_artists = sorted(skipped - unknown - foreign)
    result.unknown_artists = sorted(unknown)
    result.foreign_artists = sorted(foreign)

    # (c) -------------------------------------------------------------- monitorNewItems: none
    # Before (d): a ratchet's refresh shows more release types, and Lidarr monitors every one it
    # finds under an artist left on "all". Set here first, so none of them is.
    new_items_ids = [
        artist.id
        for mbid in diff.set_new_items_none
        if (artist := _artist_of(mbid, view, added_artists)) is not None and mbid not in skipped
    ]
    if new_items_ids:
        ctx.lidarr.set_artists_new_items_none(new_items_ids)
        result.new_items_none = len(new_items_ids)

    # (d) -------------------------------------------------------------- profile ratchets
    widened_from = {r.artist_mbid for r in _widening_on_new_items(diff)}
    for ratchet in diff.ratchets:
        artist = _artist_of(ratchet.artist_mbid, view, added_artists)
        if artist is None or ratchet.artist_mbid in skipped:
            continue
        ctx.lidarr.set_artist_profile(artist, full_id)
        if ratchet.artist_mbid in widened_from:
            log.warning("%s", _widening_warning(ratchet))
        try:
            ctx.lidarr.refresh_artist(artist, timeout_s=_refresh_timeout_s(ctx, artist.mbid, view))
        except LidarrMetadataError as exc:
            log.warning("ratcheted %s but RefreshArtist failed: %s", ratchet.name, exc)
            skipped.add(ratchet.artist_mbid)
            result.lidarr_metadata_ok = False
            result.skipped_artists = sorted(skipped - unknown - foreign)
            continue
        previous = owned_artists.get(ratchet.artist_mbid)
        with ctx.state.transaction():
            ctx.state.record_artist(
                OwnedArtist(
                    artist_mbid=ratchet.artist_mbid,
                    lidarr_artist_id=artist.id,
                    added_by_us=previous.added_by_us if previous is not None else False,
                    profile=Profile.FULL,
                    ratcheted_at=now,
                )
            )
        result.ratcheted += 1

    # (d2) ------------------------------------------------------------ re-monitor artists
    # After the refresh, because Lidarr can apply `addOptions.monitor: none` to the artist itself
    # during add/refresh, and the POST response cannot be trusted to say so. A just-added artist is
    # therefore always re-monitored rather than checked; an existing one is in the diff.
    #
    # `result.artists_monitored` counts only the plan's own `monitor_artists`: a
    # just-added artist's re-monitor is part of the add, not a second event, so it is covered by
    # `set_artists_monitored` below but left out of this count - matching what the plan summary
    # already counts as "unmonitored artists to re-monitor" and what `changes_made` counts as part
    # of the add.
    plan_monitor_ids = [
        artist.id
        for mbid in diff.monitor_artists
        if (artist := _artist_of(mbid, view, added_artists)) is not None and mbid not in skipped
    ]
    result.artists_monitored = len(plan_monitor_ids)
    monitor_ids = plan_monitor_ids + [
        a.id for mbid, a in added_artists.items() if mbid not in skipped and a.id not in plan_monitor_ids
    ]
    if monitor_ids:
        ctx.lidarr.set_artists_monitored(monitor_ids)

    # (d3) ------------------------------------------------------------ chase recent releases
    # A followed artist whose new album Lidarr's catalogue does not hold yet. `refresh_artist`
    # sends `isNewArtist: true`, so the follow-up rescan stays inside the artist's own folder
    # rather than walking every root (see docs/dev/DESIGN.md, upstream quirks). The release is NOT
    # monitored this run even if it appears: the diff is the plan of record, and it listed this
    # release as a gap rather than as a monitor. The next run picks it up.
    #
    # **A failure here is not a skipped artist**, unlike the add and ratchet refreshes. Those
    # refresh an artist whose album rows cannot be trusted until they finish - a brand-new artist
    # has none at all - so carrying on would monitor nothing and unmonitor everything. This one is
    # opportunistic: the artist was already in Lidarr with a catalogue this run has read, and the
    # refresh only asks for one more release. Dropping their monitors over it would cost something
    # real, and registering a class-B `skipped_artists` identity would degrade every run until a
    # human accepted it - for a timeout on a request likearr did not have to make.
    attempted: list[str] = []
    for mbid in diff.refresh_artists:
        artist = _artist_of(mbid, view, added_artists)
        if artist is None or mbid in skipped:
            continue
        attempted.append(mbid)
        try:
            ctx.lidarr.refresh_artist(artist, timeout_s=_refresh_timeout_s(ctx, artist.mbid, view))
        except LidarrMetadataError as exc:
            log.warning("refreshing %s for a recent release failed, carrying on: %s", artist.name, exc)
            result.refresh_failures += 1
            result.lidarr_metadata_ok = False
            continue
        result.refreshed += 1
    if attempted:
        # Stamped whether or not it worked: the backoff rations how often likearr asks, and an
        # artist whose metadata is stuck is exactly the one that must not be asked every run.
        with ctx.state.transaction():
            ctx.state.record_gap_refreshes(attempted, now)

    # (e) -------------------------------------------------------------- monitor
    _monitor(ctx, diff.monitor, view, added_artists, skipped, result, now=now)

    # (f) -------------------------------------------------------------- reason-set updates
    if diff.update_reasons:
        with ctx.state.transaction():
            for key, reasons in diff.update_reasons:
                ctx.state.update_reasons(key, reasons)

    # (g) -------------------------------------------------------------- unmonitor
    if not allow_unmonitors:
        if diff.unmonitor:
            log.warning("guarded run: %d unmonitors were NOT applied", len(diff.unmonitor))
    else:
        _unmonitor(ctx, diff.unmonitor, view, skipped, result)

    return result


def _named(name: str, known: Mapping[str, int]) -> bool:
    """Whether Lidarr has `name` among `known` (tags, profiles), compared as its `ensure_*` does."""
    want = name.strip().lower()
    return any(k.strip().lower() == want for k in known)


def _artist_of(mbid: str, view: LidarrView, added: Mapping[str, LidarrArtist]) -> LidarrArtist | None:
    return added.get(mbid) or view.artists.get(mbid)


def _monitor(
    ctx: Context,
    monitor: Sequence[MonitorRelease],
    view: LidarrView,
    added: Mapping[str, LidarrArtist],
    skipped: set[str],
    result: ApplyResult,
    *,
    now: datetime,
) -> None:
    """Resolve each release to a Lidarr album id, flip the unmonitored ones, record only those.

    Albums only appear after the artist has been refreshed, so the album list is re-read here
    rather than taken from the planning view. A release group Lidarr still does not have is
    reported and retried next run: it is a metadata lag, not a reason to fail the run.

    Each batch's ownership rows are written *before* the PUT, because Lidarr can apply a batch and
    still answer with an error. A row nobody writes is an album monitored for good; a row
    on an album that stayed unmonitored is harmless. See `_settle_failed_batch` for the undo.
    """
    by_artist: dict[str, list[MonitorRelease]] = {}
    for item in monitor:
        if item.key.artist_mbid in skipped:
            continue
        by_artist.setdefault(item.key.artist_mbid, []).append(item)

    album_ids: list[int] = []
    records: list[OwnedRelease] = []
    artists: dict[str, LidarrArtist] = {}
    for artist_mbid in sorted(by_artist):
        artist = _artist_of(artist_mbid, view, added)
        if artist is None:
            for item in by_artist[artist_mbid]:
                result.unmapped_in_lidarr.append(f"{artist_mbid}/{item.key.rg_mbid}")
            continue
        artists[artist_mbid] = artist
        albums = ctx.lidarr.load_albums(artist)
        for item in by_artist[artist_mbid]:
            album = albums.get(item.key.rg_mbid)
            if album is None:
                result.unmapped_in_lidarr.append(f"{artist_mbid}/{item.key.rg_mbid}")
                continue
            if album.monitored:
                # Someone else monitored it. likearr did not flip it, so likearr does not own
                # it, so likearr will never unmonitor it.
                result.already_monitored.append(f"{artist_mbid}/{item.key.rg_mbid}")
                log.info("%r is already monitored in Lidarr; not claiming ownership", item.title)
                continue
            album_ids.append(album.id)
            records.append(
                OwnedRelease(
                    key=item.key,
                    reasons=item.reasons,
                    step=item.step,
                    resolver_version=RESOLVER_VERSION,
                    monitored_at=now,
                    lidarr_album_id=album.id,
                )
            )

    owned_before = ctx.state.owned_releases() if records else {}
    # A release kept by hand and since unmonitored in Lidarr comes back through here once a source
    # wants it again. Its `manual` reason goes into the row written ahead of the PUT. The reason-set
    # update in (f) carries it too, but (f) never runs when a batch fails after Lidarr applied it.
    # The plan's own monitor items stay source-only, so the reason key they stand for is unchanged.
    records = [
        replace(r, reasons=r.reasons | kept)
        if r.key in owned_before and (kept := manual_reasons(owned_before[r.key].reasons))
        else r
        for r in records
    ]
    for start in range(0, len(album_ids), BATCH_SIZE):
        batch_ids = album_ids[start : start + BATCH_SIZE]
        batch_records = records[start : start + BATCH_SIZE]
        with ctx.state.transaction():
            ctx.state.record_monitored(batch_records)
        try:
            ctx.lidarr.set_albums_monitored(batch_ids, True)
        except Exception:
            result.monitored += _settle_failed_batch(ctx, batch_records, artists, owned_before)
            raise
        result.monitored += len(batch_ids)


def _settle_failed_batch(
    ctx: Context,
    batch: Sequence[OwnedRelease],
    artists: Mapping[str, LidarrArtist],
    owned_before: Mapping[ReleaseKey, OwnedRelease],
) -> int:
    """After a monitor PUT raised, undo the rows written ahead for albums Lidarr did not flip.

    Lidarr can apply a batch and still answer with an error: HTTP 500 when one album id in it no
    longer exists (it flips the others anyway), or a reply lost after the change landed. So read
    the batch's albums back and undo only the rows whose album is still unmonitored or gone. An
    undone row goes back to what was owned before this batch, or is removed if nothing was.

    If the read-back fails too, every row is kept: each album was unmonitored when `_monitor` read
    it, so one that is monitored now is almost certainly likearr's write. The read-back stops at
    the first artist it cannot read, so a Lidarr that is down costs one more failed read, not one
    per artist.

    Returns how many albums of the batch Lidarr is confirmed to have monitored.
    """
    now_monitored: set[ReleaseKey] = set()
    for artist_mbid in sorted({r.key.artist_mbid for r in batch}):
        try:
            albums = ctx.lidarr.load_albums(artists[artist_mbid])
        except Exception as exc:
            log.warning(
                "could not read Lidarr back after a failed monitor batch (%s); keeping all %d "
                "ownership row(s) of that batch so none of its albums is left monitored and unowned",
                redact(str(exc)),
                len(batch),
            )
            return 0
        for record in batch:
            if record.key.artist_mbid != artist_mbid:
                continue
            album = albums.get(record.key.rg_mbid)
            if album is not None and album.monitored:
                now_monitored.add(record.key)

    not_applied = [r for r in batch if r.key not in now_monitored]
    restore = [owned_before[r.key] for r in not_applied if r.key in owned_before]
    remove = [r.key for r in not_applied if r.key not in owned_before]
    with ctx.state.transaction():
        ctx.state.record_unmonitored(remove)
        ctx.state.record_monitored(restore)
    log.warning(
        "a monitor batch failed; Lidarr shows %d of its %d album(s) monitored, and likearr claimed only those",
        len(now_monitored),
        len(batch),
    )
    return len(now_monitored)


def _unmonitor(
    ctx: Context,
    unmonitor: Sequence[UnmonitorRelease],
    view: LidarrView,
    skipped: set[str],
    result: ApplyResult,
) -> None:
    """Unmonitor in batches, dropping ownership only for what Lidarr confirmed as flipped."""
    owned = ctx.state.owned_releases()
    pairs: list[tuple[int, ReleaseKey]] = []
    for item in unmonitor:
        key = item.key
        if key.artist_mbid in skipped:
            continue
        album = view.album(key)
        album_id = album.id if album is not None else (owned[key].lidarr_album_id if key in owned else None)
        if album_id is None:
            log.warning("no Lidarr album id for %s/%s; leaving it alone", key.artist_mbid, key.rg_mbid)
            continue
        pairs.append((album_id, key))

    for start in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[start : start + BATCH_SIZE]
        ctx.lidarr.set_albums_monitored([album_id for album_id, _ in batch], False)
        with ctx.state.transaction():
            ctx.state.record_unmonitored([key for _, key in batch])
        result.unmonitored += len(batch)
