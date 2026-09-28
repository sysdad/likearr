"""The plan phase of a run: sources, resolver, desired state, Lidarr view, diff.

`plan` is a read. It writes nothing to Lidarr and only the harmless parts of state (the
resolution cache and the pending clock). Split out of `shell.run`; see that module for
the invariants every run keeps.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from likearr.adapters.musicbrainz import jittered_max_age
from likearr.core.desire import build_desired
from likearr.core.diff import build_diff
from likearr.core.health import lidarr_metadata_outage
from likearr.core.resolver import ResolveResult, resolve_all
from likearr.models import (
    PROGRESS_MARKER_POST_RESOLVE,
    RESOLVER_VERSION,
    ArtistResolution,
    Diff,
    LidarrView,
    Resolution,
    ResolutionStatus,
    SourceSnapshot,
)
from likearr.ports import ArtistDetails, MetadataError, SourceError
from likearr.shell import spotify_snapshot
from likearr.shell.context import Context
from likearr.shell.run_types import PlanResult

# The run's logger, not this module's own: the log lines, the job page's progress parsing
# (`web.app._phase`) and anything filtering on the logger name have always read `likearr.shell.run`.
log = logging.getLogger("likearr.shell.run")

# ---------------------------------------------------------------------------- plan

RESOLUTION_AGE_FACTOR = 4 / 3
"""A cached track or saved-album answer is looked up again at this multiple of `[musicbrainz]
positive_cache_days` (120 days at the default 90), jittered per intent key by up to another 25%.

Longer than the cache it is built from on purpose: an `mb_cache` entry lives
up to 1.25 times `positive_cache_days`, so an answer re-resolved sooner could read the same stale
entries back and learn nothing. A correction therefore takes up to about five months to land, which
suits a slow problem: MusicBrainz edits to things a user actually liked are rare.
"""


def resolution_max_age(positive_cache_days: float) -> Callable[[str], timedelta]:
    """How old a cached answer may get before `resolve_all` looks it up again, per intent key.

    Jittered by key exactly as `mb_cache` entries are (`jittered_max_age`), so a library cached in
    one run - at deploy, all of it - falls due over weeks rather than on one night. Zero follows
    `positive_cache_days = 0`, which asks MusicBrainz every time: every answer is checked every run.
    """
    days = positive_cache_days * RESOLUTION_AGE_FACTOR

    def max_age(key: str) -> timedelta:
        return timedelta(days=jittered_max_age(days, key))

    return max_age


def plan(
    ctx: Context,
    *,
    now: datetime,
    scheduled: bool,
    persist: bool = True,
    accept_shrink: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> PlanResult:
    """Read everything and compute the diff. Writes nothing to Lidarr.

    Args:
        now: the clock, injected so the whole shell is deterministic under test.
        scheduled: True for an unattended run, which caps unmonitors in the core's guards.
        accept_shrink: a human accepts the source and artist shrinks; those guards are skipped and
            the diff records it. Never for a scheduled run (`run_command` refuses that).
        persist: when False (``explain``, ``prune-report``), not even the resolution cache is
            written, so a read-only command stays read-only.
        monotonic: the clock the resolve-progress line's 60 s throttle is measured against.
            Injected so a test never has to sleep for real; defaults to the wall
            clock.

    Raises:
        SourceError: the sources could not be read in full. Nothing else has happened yet.
        LidarrError: Lidarr could not be read.
    """
    if ctx.source is None:
        raise SourceError(
            ctx.spotify_error or "spotify is not configured; Connect Spotify in Settings (or run `likearr auth`)"
        )

    reused = spotify_snapshot.read_snapshot(ctx.config, now=now) if scheduled else None
    if reused is not None:
        snapshot = reused
        log.info(
            "reusing the Spotify read from %s: a redeploy cancelled the run that made it, no Spotify "
            "call is spent replanning",
            snapshot.fetched_at.isoformat(),
        )
    else:
        snapshot = ctx.source.read()
        if scheduled:
            spotify_snapshot.write_snapshot(ctx.config, snapshot)
    log.info(
        "sources read: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(snapshot.counts.items())) or "nothing configured",
    )
    for warning in snapshot.schema_warnings:
        log.warning("source schema warning: %s", warning)

    owned = ctx.state.owned_releases()
    owned_artists = ctx.state.owned_artists()

    intent_keys = _intent_keys(snapshot)
    cache: dict[str, Resolution | ArtistResolution] = {}
    pending_since: dict[str, datetime] = {}
    for key in intent_keys:
        cached = ctx.state.cached_resolution(key, RESOLVER_VERSION)
        if cached is not None:
            cache[key] = cached
        since = ctx.state.pending_since(key)
        if since is not None:
            pending_since[key] = since

    # Lidarr is read before resolution now, not after. Two things need it: the `albums-only` tag,
    # and - since a followed artist resolves through MusicBrainz's Spotify URL relationship -
    # which artists the library already holds, which is how a Spotify page linked to several
    # MusicBrainz artists is decided. This read carries no albums (`load_view(None)`), which is
    # what keeps it cheap; the album-bearing view comes after the desired state names its artists.
    tag_view = ctx.lidarr.load_view(None)
    albums_only = _albums_only_artists(tag_view, ctx.config.rules.albums_only_tag)
    lost = tuple(sorted(tagged_without_state(tag_view, ctx.config.lidarr.tag, owned_artists)))
    if lost:
        _warn_tagged_without_state(tag_view, ctx.config.lidarr.tag, lost)

    total_intents = len(snapshot.artists) + len(snapshot.albums) + len(snapshot.tracks)
    resolve_result = resolve_all(
        snapshot,
        ctx.lookup,
        now=now,
        cache=cache,
        pending_since=pending_since,
        fallback_days=ctx.config.rules.singles_fallback_days,
        scope=ctx.config.rules.liked_track_scope,
        links=ctx.artist_links,
        known_artist_mbids=frozenset(tag_view.artists),
        rules=ctx.config.rules.exclusions,
        lookup_failures=_mb_failure_count(ctx),
        relations=ctx.artist_relations,
        progress=_resolve_progress_logger(ctx, total_intents, monotonic=monotonic) if total_intents else None,
        max_age=resolution_max_age(ctx.config.musicbrainz.positive_cache_days),
    )
    log.info(PROGRESS_MARKER_POST_RESOLVE)

    desired = build_desired(
        snapshot,
        resolve_result,
        ctx.lookup,
        albums_only_artists=albums_only,
        deny_releases=ctx.config.rules.exclusions.deny_releases,
    )

    wanted_artists = sorted(set(desired.artists) | set(owned_artists) | {k.artist_mbid for k in owned})
    view = ctx.lidarr.load_view(wanted_artists)

    lean_id = view.metadata_profiles.get(ctx.config.lidarr.lean_profile)
    full_id = view.metadata_profiles.get(ctx.config.lidarr.full_profile)
    if lean_id is None or full_id is None:
        log.warning(
            "metadata profiles %r/%r are missing from Lidarr; profile ratchets are skipped this run "
            "(run `likearr setup-profiles --apply`)",
            ctx.config.lidarr.lean_profile,
            ctx.config.lidarr.full_profile,
        )

    diff = build_diff(
        desired,
        view,
        owned,
        owned_artists,
        last_source_counts=ctx.state.last_source_counts(),
        last_followed_counts=ctx.state.last_followed_counts(),
        source_counts=snapshot.counts,
        live_reason_keys=_live_reason_keys(snapshot),
        guards=ctx.config.guards,
        scheduled=scheduled,
        schema_ok=snapshot.schema_ok,
        accept_shrink=accept_shrink,
        now=now,
        source_digest=snapshot.digest(),
        lean_profile_id=lean_id,
        full_profile_id=full_id,
        recent_release_days=ctx.config.rules.recent_release_days,
        max_refreshes=ctx.config.lidarr.max_refreshes_per_run,
        last_gap_refreshes=ctx.state.last_gap_refreshes(),
        gap_refresh_interval_hours=ctx.config.lidarr.recent_gap_refresh_hours,
    )
    diff = replace(diff, config_fingerprint=ctx.config.plan_fingerprint)

    _describe_collisions(diff, ctx.artist_details)

    if persist:
        _persist_resolutions(ctx, resolve_result, intent_keys, pending_since, now=now)
        _persist_lidarr_negative_cache(ctx, now=now)

    # The plan phase is complete: Spotify and Lidarr were both just read fresh, so any saved
    # snapshot from an earlier cancelled attempt can only be staler than what this run just saw.
    spotify_snapshot.delete_snapshot(ctx.config)

    return PlanResult(
        diff=diff,
        snapshot=snapshot,
        desired=desired,
        resolve_result=resolve_result,
        view=view,
        spotify_schema_ok=snapshot.schema_ok,
        mb_ok=_mb_ok(ctx, resolve_result, diff),
        lidarr_metadata_ok=ctx.composite.lidarr_metadata_ok if ctx.composite is not None else True,
        lean_profile_id=lean_id,
        full_profile_id=full_id,
        intent_keys=tuple(intent_keys),
        lidarr_metadata_failures=ctx.composite.lidarr_metadata_failures if ctx.composite is not None else (),
        catalogue_too_large=ctx.composite.catalogue_too_large if ctx.composite is not None else (),
        mb_stale_served=ctx.composite.mb_stale_served if ctx.composite is not None else 0,
        lidarr_metadata_attempts=ctx.composite.lidarr_metadata_attempts if ctx.composite is not None else 0,
        lidarr_metadata_attempt_failures=(
            ctx.composite.lidarr_metadata_attempt_failures if ctx.composite is not None else 0
        ),
        tagged_without_state=lost,
    )


def _persist_lidarr_negative_cache(ctx: Context, *, now: datetime) -> None:
    """Write this run's genuinely-failed Lidarr metadata identities to the negative cache.

    Gated on `lidarr_metadata_any_success`, decided once here at the end of the run rather than
    per call: writing on a run where every Lidarr metadata call failed would let a plain
    `api.lidarr.audio` outage poison every term it touched for `negative_cache_days`. Gated as
    well on the run not looking like an outage (`core.health.lidarr_metadata_outage`, rule 7): one
    success is not enough when most lookups failed, and a MusicBrainz outage sends every
    name search to Lidarr, so a partial Lidarr outage in the same run would reach many terms.
    """
    composite = ctx.composite
    if composite is None or not composite.lidarr_metadata_any_success:
        return
    if lidarr_metadata_outage(composite.lidarr_metadata_attempts, composite.lidarr_metadata_attempt_failures):
        log.warning(
            "Lidarr metadata lookups mostly failed this run (%d of %d); no search term is negative-cached",
            composite.lidarr_metadata_attempt_failures,
            composite.lidarr_metadata_attempts,
        )
        return
    new_failures = composite.lidarr_metadata_new_failures
    if new_failures:
        ctx.state.record_lidarr_negative_cache(new_failures, now)


def _describe_collisions(diff: Diff, details: ArtistDetails | None) -> None:
    """Fill in each collision's MusicBrainz disambiguations, in place.

    Kept out of `core.diff`, which is pure: this is I/O. It is two cached, rate-limited artist
    lookups per collision - and collisions are rare, a handful even on a large library - so the cost
    is a rounding error. A lookup that fails or has nothing to say leaves the field empty and the
    collision is still reported, just with less to go on.
    """
    if details is None:
        return
    for index, collision in enumerate(diff.name_collisions):
        try:
            wanted = details.artist_disambiguation(collision.wanted_mbid)
            existing = details.artist_disambiguation(collision.existing_mbid) if collision.existing_mbid else ""
        except MetadataError:  # pragma: no cover - CompositeLookup already swallows these
            continue
        diff.name_collisions[index] = replace(collision, wanted_disambiguation=wanted, existing_disambiguation=existing)


def _intent_keys(snapshot: SourceSnapshot) -> list[str]:
    keys = [i.reason.key for i in snapshot.artists]
    keys += [i.reason.key for i in snapshot.albums]
    keys += [i.reason.key for i in snapshot.tracks]
    return keys


def _live_reason_keys(snapshot: SourceSnapshot) -> set[str]:
    return set(_intent_keys(snapshot))


def _albums_only_artists(view: LidarrView, tag_label: str) -> set[str]:
    """Artist MBIDs carrying the `albums-only` tag in Lidarr. Empty when the tag does not exist."""
    tag_id = view.tags.get(tag_label)
    if tag_id is None:
        return set()
    return {mbid for mbid, artist in view.artists.items() if tag_id in artist.tags}


def tagged_without_state(view: LidarrView, tag_label: str, owned_artists: Container[str]) -> set[str]:
    """Artist MBIDs carrying likearr's own tag in Lidarr with no `owned_artists` row.

    Every artist likearr adds gets both, so a tagged artist with no row means the state database
    was lost or replaced (or someone added the tag by hand). Unlike "owns nothing", this does not
    go quiet once a fresh database claims its first release. Report only: nothing here claims,
    adopts or changes ownership. Empty when the tag does not exist.
    """
    tag_id = view.tags.get(tag_label)
    if tag_id is None:
        return set()
    return {mbid for mbid, artist in view.artists.items() if tag_id in artist.tags and mbid not in owned_artists}


def _warn_tagged_without_state(view: LidarrView, tag_label: str, mbids: Iterable[str]) -> None:
    labels = [f"{a.name} ({mbid})" if (a := view.artists.get(mbid)) and a.name else mbid for mbid in mbids]
    log.warning(
        "%d artist(s) in Lidarr carry the %r tag but the state database has no record of them (%s): if you "
        "lost or replaced the database, restore it from backup; until you do, nothing likearr monitored "
        "before is ever unmonitored",
        len(labels),
        tag_label,
        ", ".join(labels),
    )


def _lost_state_message(count: int) -> str:
    """The run record's half of the lost-state warning; Status words it for a person from the count."""
    if not count:
        return ""
    return (
        f"{count} artist(s) with likearr's Lidarr tag have no record in the state database: "
        "if it was lost or replaced, restore it from backup"
    )


METADATA_ERROR_STEP = "error:metadata"
"""`core.desire`'s step for a followed artist whose catalogue could not be listed."""


def _mb_errors(resolve_result: ResolveResult, diff: Diff) -> int:
    """Intents abandoned this run because a MusicBrainz lookup failed.

    Two sources, because two layers give up for the same reason: the resolver counts its own, and
    `core.desire` reports a followed artist whose catalogue it could not list. The second is not a
    resolver error and would otherwise go uncounted, although it costs a whole catalogue.

    A catalogue that is merely too large to browse is deliberately not here: it has its own step,
    it is permanent rather than an outage, and counting it would make `mb_ok` false on every run
    for that artist.
    """
    return resolve_result.metadata_errors + sum(1 for u in diff.unmapped if u.step == METADATA_ERROR_STEP)


def _mb_ok(ctx: Context, resolve_result: ResolveResult, diff: Diff) -> bool:
    """MusicBrainz is 'ok' when no layer saw a backend failure that actually cost something.

    A lookup that failed and was answered from an expired cache entry is deliberately **not** here,
    although it is counted in `HealthRecord.mb_errors`. Nothing was lost - the adapter's standing
    rule is that a failed lookup never drops a mapping - and since positive entries gained a max
    age a MusicBrainz wobble reaches many more lookups than it used to. Degrading on it
    would light the signal for a run in which likearr did exactly the right thing, which is the
    failure mode `core.health` exists to remove. A failure with no cached answer still raises, and
    still turns this false.
    """
    adapter_ok = ctx.composite.mb_ok if ctx.composite is not None else True
    return adapter_ok and _mb_errors(resolve_result, diff) == 0


def _mb_failure_count(ctx: Context) -> Callable[[], int] | None:
    """`resolve_all`'s `lookup_failures`: the composite's running count of MusicBrainz failures."""
    composite = ctx.composite
    if composite is None:
        return None
    return lambda: composite.mb_failure_count


PROGRESS_LOG_INTERVAL_S = 60.0
"""How often `plan`'s resolve-progress line is allowed to repeat: often
enough that a long first run does not look stuck, rarely enough that it never becomes one line per
item."""

_PROGRESS_ETA_MIN_LIVE_CALLS = 50
"""Below this many live MusicBrainz calls, an ETA is not shown at all: too few data points to be
worth more than a guess, and a warm run (almost all cache hits) never reaches it, which is exactly
the point - "should show no ETA rather than a misleading one"."""

_PROGRESS_ETA_MIN_SECONDS = 120.0
"""Below this, an ETA is shown as "under a few minutes" rather than a specific duration: the tail
of a resolve run is the noisiest part of the estimate (a cache-warm
stretch near the end skews the live-calls-per-intent rate), so a countdown that close reads as more
precise than it actually is."""


def _resolve_progress_logger(ctx: Context, total: int, *, monotonic: Callable[[], float]) -> Callable[[int, int], None]:
    """A `resolve_all` `progress` callback that logs a throttled ``progress:`` line.

    Read by `web.app._phase` to show the newest one on the job page while a run is in progress
    (the fixed ``progress:`` prefix is what makes that a simple scan). Logged at most once every
    `PROGRESS_LOG_INTERVAL_S` of `monotonic` time - always the first call, so a short plan still
    gets one line - and always attributed to `likearr.shell.run`, never to the pure resolver that
    only calls this function and never logs itself.

    The last intent's call (`done == total`) always logs, bypassing the throttle, and always with
    no ETA: otherwise the job page could be left showing a resolve line -
    ETA included - from several hundred intents back, for as long as the read-Lidarr-and-build-the-
    diff work that follows resolving takes.
    """
    last_logged: float | None = None

    def log_progress(done: int, _total: int) -> None:
        nonlocal last_logged
        final = done >= total
        now = monotonic()
        if not final and last_logged is not None and now - last_logged < PROGRESS_LOG_INTERVAL_S:
            return
        last_logged = now
        log.info(_progress_line(ctx, done, total, final=final))

    return log_progress


def _progress_line(ctx: Context, done: int, total: int, *, final: bool = False) -> str:
    """One ``progress:`` line: done/total, the MusicBrainz lookup split, and an ETA once there
    have been enough live calls to base one on - except on the final line
    (`final=True`), which never carries one: `done == total` means nothing is left to estimate, and
    the ETA formula's own floor (`_format_eta`'s ``max(1, ...)``) would otherwise print a misleading
    "about 1m left" on a resolve that has, in fact, just finished."""
    cache_hits = ctx.composite.mb_cache_hits if ctx.composite is not None else 0
    live_calls = ctx.composite.mb_live_calls if ctx.composite is not None else 0
    line = (
        f"progress: resolving {done}/{total} songs and artists, "
        f"{cache_hits + live_calls:,} MusicBrainz lookups ({live_calls:,} live)"
    )
    if not final and live_calls >= _PROGRESS_ETA_MIN_LIVE_CALLS and done > 0:
        min_interval_s = ctx.config.musicbrainz.min_interval_s
        remaining = total - done
        eta_seconds = remaining * (live_calls / done) * min_interval_s
        eta = (
            "under a few minutes left"
            if eta_seconds < _PROGRESS_ETA_MIN_SECONDS
            else f"about {_format_eta(eta_seconds)} left"
        )
        line += f", {eta}"
    return line


def _format_eta(seconds: float) -> str:
    """A coarse ``1h40m`` (or ``40m``) duration: an ETA is a rough steer, not a countdown, so it is
    never shown down to the second."""
    total_minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes}m" if hours else f"{minutes}m"


def _format_refresh_duration(seconds: float) -> str:
    """A ``1m52s`` (or ``45s``) duration for one artist's refresh wait: finer than `_format_eta`
    because a single refresh is the thing the add-loop's progress line is actually reporting, not
    estimating."""
    total_seconds = max(0, round(seconds))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}m{secs}s" if minutes else f"{secs}s"


def _rests_on(resolution: Resolution, provisional: frozenset[str]) -> bool:
    """Whether the release Spotify named, or the answer, came from a provisional Lidarr lookup."""
    return any(
        rg is not None and rg.mbid in provisional for rg in (resolution.release_group, resolution.source_release_group)
    )


def _persist_resolutions(
    ctx: Context,
    resolve_result: ResolveResult,
    intent_keys: Iterable[str],
    pending_since: Mapping[str, datetime],
    *,
    now: datetime,
) -> None:
    """Cache what resolved and move the pending clock. Never touches the shrink baselines.

    The shrink baselines (`record_source_counts` / `record_followed_counts`) are deliberately
    excluded: a dry-run that recorded them would quietly move the line the *next* run's shrink
    guard is measured against, so a `run` followed by `run --apply` would have disarmed its own
    safety net.
    """
    known = set(intent_keys)
    # An answer reached after MusicBrainz failed is acted on this run - `run --apply` monitors it,
    # owns it, and unmonitors what it replaced - but cached nowhere: a cached RESOLVED answer is
    # reused until RESOLVER_VERSION moves or it expires, and this one was never fully
    # checked against MusicBrainz. Uncached, the next run asks again. That covers every path, the
    # ISRC stand-in included. An answer resting on a release group Lidarr's name search found
    # after a MusicBrainz error is never cached either, whichever intent it reached.
    #
    # The pending clock moves one way only for such an intent. It is started for a track that is
    # now waiting and has no clock yet, or a search that fails the same way every run would keep
    # the singles fallback from ever firing; it is never cleared, so an outage cannot restart a
    # waiting track's clock by resolving it once, provisionally.
    provisional_rgs = ctx.composite.provisional_release_groups if ctx.composite is not None else frozenset()
    uncached, clock_kept = 0, 0
    with ctx.state.transaction():
        for key, resolution in resolve_result.resolutions.items():
            provisional = key in resolve_result.provisional
            if resolution.status is ResolutionStatus.RESOLVED:
                if provisional:
                    uncached += 1
                elif not _rests_on(resolution, provisional_rgs):
                    ctx.state.cache_resolution(resolution)
            if resolution.status is ResolutionStatus.PENDING_ALBUM:
                if key not in pending_since:
                    ctx.state.mark_pending(key, now)
            elif key in pending_since:
                if provisional:
                    clock_kept += 1
                else:
                    ctx.state.clear_pending(key)
        for key in pending_since:
            if key not in known:
                ctx.state.clear_pending(key)
    if uncached or clock_kept:
        log.info(
            "after a MusicBrainz failure: %d resolved answer(s) acted on this run but not cached, "
            "%d waiting track(s) kept their pending clock instead of clearing it",
            uncached,
            clock_kept,
        )
