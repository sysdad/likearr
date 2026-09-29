"""Compare the desired state with Lidarr and with what likearr owns, and say what to do.

Pure and side-effect free: `build_diff` reads data and returns a `Diff`. Nothing here touches
Lidarr, the state database or the clock (`now` is a parameter).

Two ideas carry most of the safety:

**Ownership.** likearr unmonitors only releases it monitored itself, recorded in `owned`, and
never anything marked `manual`. A release that is monitored in Lidarr but not owned is invisible
to `unmonitor` - `adopt` is the only path that ever takes responsibility for it.

**A name may exist once.** Lidarr matches an incoming download to an artist by *name*, so two
artists sharing one name make every import of theirs ambiguous and Lidarr refuses to guess. An
add that would create such a pair is refused here instead, as a `name-collision` guard.

**A reason is lost only when it leaves the source.** A liked track whose MusicBrainz lookup
failed this run has not been unliked. Comparing owned reasons against desired reasons alone
would read every transient mapping failure as a deletion, so `live_reason_keys` (the reason keys
actually present in this run's snapshot) vetoes that: a reason still in the source is never lost,
however badly it resolved.

**...or the user opts it out.** The one exception, and the reason it is safe. An intent that
resolved to a `track:excluded:*` step did not fail to resolve: it resolved to "the
user's configuration refuses this release". That answer is produced only by a deterministic rule
written down in `[rules]`, never by a lookup, a timeout or an outage, so the argument above does
not apply to it - and without the exception the opt-out would be inert, because the song is still
liked, so its reason would stay live and the box set would stay monitored for ever. The track is
still reported as unmapped either way; what changes is only whether the release it used to hold
is released.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

from likearr.config import GuardsConfig
from likearr.core.normalize import normalize_name
from likearr.core.resolver import is_excluded
from likearr.models import (
    RESOLVER_VERSION,
    AddArtist,
    ArtistResolution,
    Diff,
    Guard,
    LidarrArtist,
    LidarrView,
    MonitorRelease,
    NameCollision,
    OwnedArtist,
    OwnedRelease,
    Profile,
    ProfileRatchet,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SourceKind,
    UnmonitorRelease,
)
from likearr.models import DesiredState as _DesiredState

__all__ = [
    "CATALOGUE_GAP_STEP",
    "NAME_COLLISION_GUARD",
    "RECENT_GAP_STEP",
    "artists_by_normalized_name",
    "best_reason_key",
    "build_diff",
    "config_changes",
    "is_catalogue_gap",
    "is_recent_catalogue_gap",
    "is_stale",
    "manual_reasons",
]

_NEW_ITEMS_NONE = "none"


CATALOGUE_GAP_STEP = "lidarr:not-in-catalogue"
"""Step for a followed-catalogue release group Lidarr does not track. Informational, not unmapped."""

RECENT_GAP_STEP = "lidarr:not-in-catalogue-yet"
"""Step for a catalogue gap that is a *recent or future* release: Lidarr's metadata has not caught up.

A followed artist's brand-new album is a catalogue gap until Lidarr's own scheduled refresh picks
it up. The release date is what tells the two apart - an undated or long-past release group is a
promo, a bootleg or a non-Official pressing Lidarr deliberately never tracks, and refreshing for
it would be pointless work on every run for ever.
"""

_GAP_STEPS = (CATALOGUE_GAP_STEP, RECENT_GAP_STEP)


def is_catalogue_gap(item: Resolution | ArtistResolution) -> bool:
    """True for followed-catalogue entries Lidarr does not hold; excluded from the unmapped count.

    Covers both kinds, recent and chronic: neither is a resolution failure, so neither belongs in
    `unmapped`. Use `is_recent_catalogue_gap` to tell them apart.
    """
    return item.step in _GAP_STEPS


def is_recent_catalogue_gap(item: Resolution | ArtistResolution) -> bool:
    """True for a gap whose release is recent or still to come - the one a refresh can fix."""
    return item.step == RECENT_GAP_STEP


def _hours_since(now: datetime, then: datetime | None) -> float | None:
    """Hours between two instants, or `None` for "never" - including an unusable stored value.

    A naive timestamp against an aware `now` raises rather than comparing wrongly; treating that
    as "never refreshed" is the safe answer, because the worst it costs is one extra refresh.
    """
    if then is None:
        return None
    try:
        return (now - then).total_seconds() / 3600.0
    except TypeError:
        return None


def _is_recent(release_group: ReleaseGroup, now: datetime, recent_release_days: int) -> bool:
    """Is this release new enough (or still unreleased) that Lidarr may simply not have it yet?

    An unknown first-release date is **not** recent. MusicBrainz leaves promos and bootlegs undated
    far more often than it leaves new albums undated, and guessing "new" would queue a refresh for
    the same artist on every run for ever - the exact always-on signal this codebase keeps removing.
    """
    released = release_group.first_release_date
    if released is None:
        return False
    return (now.date() - released).days <= recent_release_days


def _source_key(reason: Reason) -> str | None:
    """The `SourceSnapshot.counts` key a reason came from, for the source shrink guard."""
    if reason.kind is ReasonKind.FOLLOWED:
        return str(SourceKind.FOLLOWED_ARTISTS)
    if reason.kind is ReasonKind.SAVED:
        return str(SourceKind.SAVED_ALBUMS)
    if reason.kind is ReasonKind.LIKED:
        return str(SourceKind.LIKED_TRACKS)
    if reason.kind is ReasonKind.PLAYLIST and reason.playlist_id:
        return f"playlist:{reason.playlist_id}"
    return None


def _shrink_pct(last: int, current: int) -> float:
    """How far a count fell, as a percentage of the previous count."""
    if last <= 0:
        return 0.0
    return max(0.0, (last - current) / last * 100.0)


NAME_COLLISION_GUARD = "name-collision"
"""An artist likearr would add shares a name with one Lidarr already has, under a different MBID."""


def artists_by_normalized_name(view: LidarrView) -> dict[str, tuple[LidarrArtist, ...]]:
    """Lidarr's artists grouped by normalised name, for the collision guard and `doctor`.

    `normalize_name` is the same fold the resolver compares names with: case, accents,
    punctuation and a leading "the". Lidarr's own import matcher is looser still, so anything
    that collides here would certainly collide there.
    """
    out: dict[str, list[LidarrArtist]] = {}
    for artist in view.artists.values():
        out.setdefault(normalize_name(artist.name), []).append(artist)
    return {name: tuple(sorted(group, key=lambda a: a.id)) for name, group in out.items()}


def _name_collision(
    name: str,
    wanted_mbid: str,
    existing: Sequence[LidarrArtist],
    dropped_releases: int,
) -> NameCollision:
    """Record an artist skipped because Lidarr already holds their name.

    **This is usually not a duplicate.** The typical collision is two distinct artists sharing one
    name: "Lawrence" is a New York band *and* a German DJ, "Evangeline" an L.A. singer-songwriter
    *and* a Seattle alt-country band. likearr is usually right to want the second one.

    The constraint is Lidarr's. Its importer matches an incoming download to an artist **by
    name**; two artists sharing one make that ambiguous, so it declines to guess, leaves
    `artistId` and `albumId` null and reports "found multiple artists". Those queue items then sit
    unimportable forever, and Cleanuparr cannot clear them either - with no album id there is
    nothing to re-search after a blocklist, so it correctly abstains. Nothing is misbehaving; two
    same-named artists simply cannot coexist there.

    Fires **whatever the existing artist's file count is**. The ambiguity is in the name and
    Lidarr's matcher never looks at files.
    """
    first = existing[0] if existing else None
    return NameCollision(
        name=name,
        wanted_mbid=wanted_mbid,
        existing_mbid=first.mbid if first else "",
        existing_lidarr_id=first.id if first else 0,
        existing_name=first.name if first else "",
        dropped_releases=dropped_releases,
    )


def _name_collision_guard(collision: NameCollision) -> Guard:
    """The prose form of a `NameCollision`, for the guards list and the health record."""
    other = (
        f"Lidarr artist {collision.existing_lidarr_id} ({collision.existing_mbid})"
        if collision.in_lidarr
        else f"{collision.existing_mbid}, also wanted this run and skipped too"
    )
    return Guard(
        code=NAME_COLLISION_GUARD,
        message=(
            f"skipped {collision.name!r} ({collision.wanted_mbid}), a different artist that shares "
            f"a name with {other}. Lidarr matches incoming downloads by name and cannot "
            f"hold two artists under one, so adding this one would strand every download of "
            f"either with no artist or album id. {collision.dropped_releases} release(s) went "
            "unmonitored as a result; see the run summary for which artist is which. Lidarr takes "
            "an artist's name from MusicBrainz, so the options are to keep one, or add both and "
            "import the other's downloads by hand (Lidarr's Manual Import may work). If the match "
            f"looks wrong, `likearr explain {_shell_word(collision.name)}` shows why."
        ),
        blocked_unmonitors=0,
    )


def _shell_word(text: str) -> str:
    """`text` as one word a shell will accept pasted, readably: bare when it can be, and in double
    quotes rather than `shlex.quote`'s ``'"'"'`` when the only awkward character is an apostrophe."""
    quoted = shlex.quote(text)
    if quoted == text or "'" not in text or any(c in text for c in '"$`\\!'):
        return quoted
    return f'"{text}"'


def build_diff(
    desired: _DesiredState,
    view: LidarrView,
    owned: Mapping[ReleaseKey, OwnedRelease],
    owned_artists: Mapping[str, OwnedArtist],
    *,
    last_source_counts: Mapping[str, int],
    last_followed_counts: Mapping[str, int],
    source_counts: Mapping[str, int],
    live_reason_keys: set[str],
    guards: GuardsConfig,
    scheduled: bool,
    schema_ok: bool,
    accept_shrink: bool = False,
    now: datetime,
    source_digest: str,
    lean_profile_id: int | None,
    full_profile_id: int | None,
    recent_release_days: int = 60,
    max_refreshes: int = 10,
    last_gap_refreshes: Mapping[str, datetime] | None = None,
    gap_refresh_interval_hours: float = 24.0,
    manage_monitored: bool = False,
) -> Diff:
    """Build the plan of record for one run.

    Arguments beyond the obvious:

    - `source_counts` / `last_source_counts`: this run's and the previous run's per-source item
      counts, keyed as `SourceSnapshot.counts` is. The source shrink guard compares them.
    - `last_followed_counts`: the previous run's studio Album/EP count per followed artist, before
      the user's own filters. This run's counts come from `desired.catalogue_counts`.
    - `live_reason_keys`: every reason key present in this run's snapshot. A reason in this set is
      never treated as lost (see the module docstring), except where its intent was opted out.
    - `scheduled`: True for an unattended run, which caps unmonitors.
    - `schema_ok`: False when a source response was missing fields likearr depends on, or read
      fewer items than it reported. Every unmonitor is then blocked, because the shrink may be
      the parser's fault, or a short read, rather than the user's.
    - `accept_shrink`: a human reviewed a source or artist shrink and accepts it, so those two
      guards are not applied. The `schema` guard and the scheduled cap still are.
    - `lean_profile_id` / `full_profile_id`: Lidarr metadata profile IDs, used for the one-way
      Full ratchet. Ratchets are skipped when either is unknown.
    - `recent_release_days`: how new a followed artist's release has to be for a catalogue gap to
      read as "Lidarr has not caught up" rather than "Lidarr will never hold this".
    - `max_refreshes`: the per-run cap on `refresh_artists`.
    - `last_gap_refreshes` / `gap_refresh_interval_hours`: when each artist was last refreshed for
      a gap, and how long to leave them alone afterwards.

    The returned `Diff` is complete: applying exactly its `add_artists`, `refresh_artists`,
    `monitor`, `unmonitor`, `ratchets`, `set_new_items_none`, `monitor_artists` and
    `update_reasons` converges Lidarr onto `desired`, as far as the guards allow.
    """
    add_artists: list[AddArtist] = []
    monitor: list[MonitorRelease] = []
    claim: list[OwnedRelease] = []
    unmonitor: list[UnmonitorRelease] = []
    disown: list[ReleaseKey] = []
    ratchets: list[ProfileRatchet] = []
    set_new_items_none: list[str] = []
    guard_list: list[Guard] = []
    unmapped: list[Resolution | ArtistResolution] = list(desired.unmapped)
    recent_gaps: list[tuple[str, ReleaseGroup]] = []
    """(artist mbid, release group) for every gap a refresh could plausibly close."""
    last_gap_refreshes = last_gap_refreshes or {}

    # ------------------------------------------------------------------ artists to add
    by_name = artists_by_normalized_name(view)
    # New artists grouped by name too: two same-named artists that are both new this run would
    # otherwise pass the Lidarr-side check one after the other, and the second add creates the pair.
    # Neither is preferred. The one a follow brought in may be a namesake a name search settled on,
    # so "keep the followed one" could keep the stranger; both are skipped and reported instead.
    # An artist whose MusicBrainz id is already in Lidarr has nothing to add and nothing to collide.
    new_artists = {
        mbid: normalize_name(name) for mbid, name in sorted(desired.artists.items()) if mbid not in view.artists
    }
    new_by_name: dict[str, list[str]] = {}
    for mbid, folded in new_artists.items():
        new_by_name.setdefault(folded, []).append(mbid)
    collided: set[str] = set()
    name_collisions: list[NameCollision] = []
    for mbid, folded in new_artists.items():
        name = desired.artists[mbid]
        existing = by_name.get(folded, ())
        namesakes = [other for other in new_by_name[folded] if other != mbid]
        if existing or namesakes:
            collided.add(mbid)
            dropped = sum(1 for key in desired.releases if key.artist_mbid == mbid)
            if existing:
                collision = _name_collision(name, mbid, existing, dropped)
            else:
                collision = NameCollision(
                    name=name,
                    wanted_mbid=mbid,
                    existing_mbid=namesakes[0],
                    existing_name=desired.artists[namesakes[0]],
                    dropped_releases=dropped,
                )
            name_collisions.append(collision)
            guard_list.append(_name_collision_guard(collision))
            continue
        add_artists.append(
            AddArtist(
                artist_mbid=mbid,
                name=name,
                profile=desired.profile_needs.get(mbid, Profile.LEAN),
            )
        )

    # ------------------------------------------------------------------ releases to monitor
    for key in sorted(desired.releases, key=lambda k: (k.artist_mbid, k.rg_mbid)):
        if key.artist_mbid in collided:
            # The artist was not added, so Lidarr has no album under this release group and never
            # will. Listing it would be a plan item that can only ever fail, repeated every run.
            continue
        release = desired.releases[key]
        album = view.album(key)
        if album is not None:
            if not album.monitored:
                monitor.append(
                    MonitorRelease(
                        key=key,
                        title=release.release_group.title,
                        reasons=frozenset(release.reasons),
                        step=_best_step(release.steps),
                    )
                )
            elif manage_monitored and key not in owned and release.reasons:
                claim.append(
                    OwnedRelease(
                        key=key,
                        reasons=frozenset(release.reasons),
                        step=_best_step(release.steps),
                        resolver_version=RESOLVER_VERSION,
                        monitored_at=now,
                        lidarr_album_id=album.id,
                    )
                )
            continue
        artist_loaded = key.artist_mbid in view.albums
        if key.artist_mbid in view.artists and artist_loaded:
            # The artist is in Lidarr and their albums were read, but this release group is not
            # among them. Lidarr's metadata has not caught up (or never will). Report it so the
            # next run retries rather than silently wanting something Lidarr cannot see.
            artist_name = desired.artists.get(key.artist_mbid, key.artist_mbid)
            if all(r.kind == ReasonKind.FOLLOWED for r in release.reasons):
                # Only a followed artist's catalogue wants it. MusicBrainz lists promos, bootlegs and
                # samplers that Lidarr deliberately never tracks (its profiles allow Official releases
                # only), so this is expected, recurs every run, and must not count as unmapped.
                #
                # Unless the release is *new*. Then the likeliest explanation is not "Lidarr will
                # never hold this" but "Lidarr's own scheduled refresh has not reached this artist
                # yet", which is exactly what likearr can do something about: queue a refresh, and
                # say so on its own line rather than burying it in the chronic count.
                if _is_recent(release.release_group, now, recent_release_days):
                    recent_gaps.append((key.artist_mbid, release.release_group))
                    released = release.release_group.first_release_date
                    step, detail = (
                        RECENT_GAP_STEP,
                        f"{artist_name!r}: MusicBrainz release group {release.release_group.mbid} "
                        f"({release.release_group.title!r}, first released "
                        f"{released.isoformat() if released else 'unknown'}) is not in Lidarr's "
                        "catalogue for this artist yet; likearr refreshes the artist so the next "
                        "run can monitor it",
                    )
                else:
                    step, detail = (
                        CATALOGUE_GAP_STEP,
                        f"{artist_name!r}: MusicBrainz release group {release.release_group.mbid} "
                        f"({release.release_group.title!r}) is not in Lidarr's catalogue for this artist "
                        "(usually promo/bootleg/non-Official); informational",
                    )
            else:
                step, detail = (
                    "lidarr:missing-release-group",
                    f"Lidarr has {artist_name!r} but no album with release group {release.release_group.mbid} "
                    f"({release.release_group.title!r}); refresh the artist in Lidarr, then re-run",
                )
            unmapped.append(
                Resolution(
                    intent_key=best_reason_key(release.reasons),
                    status=ResolutionStatus.UNMAPPED,
                    release_group=release.release_group,
                    step=step,
                    detail=detail,
                )
            )
            continue
        # The artist is not in Lidarr yet (or their albums were not loaded). Listing the release
        # keeps the diff an honest statement of intent; apply does add -> refresh -> monitor.
        monitor.append(
            MonitorRelease(
                key=key,
                title=release.release_group.title,
                reasons=frozenset(release.reasons),
                step=_best_step(release.steps),
            )
        )

    monitor_keys = {m.key for m in monitor}

    # ------------------------------------------------------------------ releases to unmonitor
    # A reason that resolved to a DIFFERENT release group this run has moved (a resolver fix, a
    # MusicBrainz merge). It is not a transient failure, so it no longer holds its old release.
    # Only releases no longer desired reach that test, so "resolved at any desired release" is
    # "resolved somewhere else".
    resolved_at = {reason.key for other in desired.releases.values() for reason in other.reasons}
    # An opted-out intent is the one kind of unresolved intent that still lets go of its release.
    # See the module docstring: it is a decision, not a failure, and no outage can produce it.
    live_reason_keys = live_reason_keys - {u.intent_key for u in unmapped if is_excluded(u)}
    for key in sorted(owned, key=lambda k: (k.artist_mbid, k.rg_mbid)):
        record = owned[key]
        if record.is_manual:
            continue
        # A release still desired is never unmonitored, even when none of its old reasons survive
        # (unfollowed, but now saved): `update_reasons` re-tags it instead.
        if key in desired.releases:
            continue
        if any(r.key in live_reason_keys and r.key not in resolved_at for r in record.reasons):
            # A reason this release had is still in the source and resolved nowhere this run; it
            # merely failed to resolve. Nothing was unliked, so nothing is unmonitored.
            continue
        album = view.album(key)
        if album is None:
            if view.lacks_album(key):
                disown.append(key)
            continue
        if not album.monitored:
            # Already unmonitored: nothing to write, but a row left here would unmonitor the
            # album again after someone monitors it by hand.
            disown.append(key)
            continue
        unmonitor.append(UnmonitorRelease(key=key, title=album.title, lost_reasons=frozenset(record.reasons)))

    # ------------------------------------------------------------------ reason-set updates
    # A `manual` reason is sticky: no source ever carries it, so without the union the first
    # source to want a release kept by hand would overwrite it, and losing that source later
    # would unmonitor the release the user asked likearr never to drop.
    update_reasons: list[tuple[ReleaseKey, frozenset[Reason]]] = []
    for key in sorted(desired.releases, key=lambda k: (k.artist_mbid, k.rg_mbid)):
        record = owned.get(key)
        if record is None:
            continue
        wanted = frozenset(desired.releases[key].reasons) | manual_reasons(record.reasons)
        if record.reasons != wanted:
            update_reasons.append((key, wanted))

    # ------------------------------------------------------------------ profile ratchets
    if lean_profile_id is not None and full_profile_id is not None:
        for mbid in sorted(desired.profile_needs):
            if desired.profile_needs[mbid] is not Profile.FULL:
                continue
            artist = view.artists.get(mbid)
            if artist is None or artist.metadata_profile_id != lean_profile_id:
                continue
            because = next(
                (
                    r.release_group.title
                    for k, r in sorted(desired.releases.items(), key=lambda kv: kv[0].rg_mbid)
                    if k.artist_mbid == mbid and r.needs_full_profile
                ),
                "a non-studio release",
            )
            ratchets.append(
                ProfileRatchet(
                    artist_mbid=mbid,
                    name=artist.name,
                    to_profile=Profile.FULL,
                    because=f"{because!r} is not a studio album or EP and needs the Full metadata profile",
                )
            )

    # ------------------------------------------------------------------ unmonitored artists
    # An unmonitored artist's albums are never searched or listed as wanted, whatever their own
    # flag says. Only artists that hold something likearr wants: one a human unmonitored on purpose and
    # that likearr wants nothing from is not likearr's to touch. A collided artist is not in Lidarr.
    holding = {k.artist_mbid for k in desired.releases if k.artist_mbid not in collided}
    monitor_artists = sorted(
        mbid for mbid in holding if (artist := view.artists.get(mbid)) is not None and not artist.monitored
    )

    # ------------------------------------------------------------------ monitorNewItems
    # "Monitor New Albums: None" only on an artist holding a release likearr owns. A
    # hand-managed artist whose wanted release is already monitored keeps its own setting: what
    # Lidarr auto-monitors there is never owned, so likearr never unmonitors it. The five sets:
    # - `owned_artists`: artists likearr added or ratcheted.
    # - the artists of `owned`: releases likearr owns, adopted and keep-as-manual ones included.
    # - the artists of `monitor`: releases likearr claims this run. Set in the same run as the
    #   claim, so the plan after the apply is empty rather than holding this write.
    # - the artists of `claim`: already-monitored releases likearr owns from this run on
    #   (`manage_monitored`), for the same reason.
    # - the artists of `ratchets`: apply (d) refreshes an artist right after widening its profile,
    #   and left on "all", Lidarr would monitor every release type the new profile shows. Apply (c)
    #   runs before (d) for exactly this reason.
    # - `monitor_artists`: artists likearr turns back on. Once monitored, Lidarr would auto-monitor
    #   their future albums on "all", an expansion likearr caused. Apply (c) runs before (d2).
    relevant = (
        set(owned_artists)
        | {k.artist_mbid for k in owned}
        | {m.key.artist_mbid for m in monitor}
        | {c.key.artist_mbid for c in claim}
        | {r.artist_mbid for r in ratchets}
        | set(monitor_artists)
    )
    for mbid in sorted(relevant):
        artist = view.artists.get(mbid)
        if artist is not None and artist.monitor_new_items != _NEW_ITEMS_NONE:
            set_new_items_none.append(mbid)

    # ------------------------------------------------------------------ artists to refresh
    # A followed artist with a recent or future release Lidarr has no album for. Only artists
    # Lidarr already holds: one being added this run is refreshed by the add itself, and one that
    # collided is not in Lidarr at all.
    #
    # Two things ration this. The **backoff** drops an artist refreshed within
    # `gap_refresh_interval_hours`: MusicBrainz dates plenty of promos and non-Official releases
    # Lidarr will never carry, and without it such an artist is refreshed on every run for the
    # whole recency window - hundreds of commands to be told the same thing. The **cap** then keeps
    # a busy release week from queueing more than one run should wait on. Order is "longest since
    # we last asked" first, so nobody starves behind a permanently stuck gap, with the freshest
    # release breaking ties. An artist the cap or the backoff drops is still reported as a recent
    # gap; only the asking is rationed.
    freshest: dict[str, date] = {}
    for artist_mbid, release_group in recent_gaps:
        if artist_mbid not in view.artists or artist_mbid in collided:
            continue
        released = release_group.first_release_date or date.min
        if released > freshest.get(artist_mbid, date.min):
            freshest[artist_mbid] = released
    due = {
        mbid: _hours_since(now, last_gap_refreshes.get(mbid))
        for mbid in freshest
        if (hours := _hours_since(now, last_gap_refreshes.get(mbid))) is None or hours >= gap_refresh_interval_hours
    }
    refresh_artists = sorted(
        due,
        key=lambda mbid: (due[mbid] is not None, -(due[mbid] or 0.0), -freshest[mbid].toordinal(), mbid),
    )[: max(max_refreshes, 0)]

    # ------------------------------------------------------------------ guards
    def block(code: str, message: str, doomed: set[ReleaseKey], subject: str = "") -> None:
        """Drop the affected unmonitors and record why."""
        hit = [u for u in unmonitor if u.key in doomed]
        if not hit:
            return
        for u in hit:
            unmonitor.remove(u)
        guard_list.append(Guard(code=code, message=message, blocked_unmonitors=len(hit), subject=subject))

    if not schema_ok:
        block(
            "schema",
            "a source response was missing fields likearr depends on, or read fewer items than it reported; "
            "every unmonitor is refused this run",
            {u.key for u in unmonitor},
        )

    if not accept_shrink:
        for source, last in sorted(last_source_counts.items()):
            if last <= 0:
                continue
            drop = _shrink_pct(last, source_counts.get(source, 0))
            if drop <= guards.source_shrink_pct:
                continue
            doomed = {u.key for u in unmonitor if any(_source_key(r) == source for r in u.lost_reasons)}
            block(
                "source-shrink",
                (
                    f"source {source!r} fell {drop:.1f}% ({last} -> {source_counts.get(source, 0)}), "
                    f"over the {guards.source_shrink_pct:.1f}% limit; if that is right, "
                    "plan with `likearr run --accept-shrink` and apply the reviewed diff"
                ),
                doomed,
                source,
            )

        for mbid, last in sorted(last_followed_counts.items()):
            if last <= 0 or mbid not in desired.followed_artists:
                # Absent from the followed source is an unfollow, not a catalogue that shrank to
                # nothing: the reason left with the artist, and a mass unfollow is `source-shrink`'s
                # to catch. An artist still followed whose catalogue could not be read stays here.
                continue
            # The catalogue before the user's own filters: a denied release or the albums-only tag
            # is a choice, not a catalogue that shrank on MusicBrainz, and must not hold the run.
            now_count = desired.catalogue_counts.get(mbid, 0)
            drop = _shrink_pct(last, now_count)
            if drop <= guards.artist_shrink_pct:
                continue
            block(
                "artist-shrink",
                (
                    f"followed artist {desired.artists.get(mbid, mbid)!r} ({mbid}) fell {drop:.1f}% "
                    f"({last} -> {now_count} studio releases), "
                    f"over the {guards.artist_shrink_pct:.1f}% limit; if that is right, "
                    "plan with `likearr run --accept-shrink` and apply the reviewed diff"
                ),
                {u.key for u in unmonitor if u.key.artist_mbid == mbid},
                mbid,
            )

    if scheduled and len(unmonitor) > guards.max_unmonitors_scheduled:
        block(
            "scheduled-cap",
            (
                f"{len(unmonitor)} unmonitors on a scheduled run, over the cap of "
                f"{guards.max_unmonitors_scheduled}; run likearr by hand to review them"
            ),
            {u.key for u in unmonitor},
        )

    if any(g.blocked_unmonitors for g in guard_list) or not schema_ok:
        # A guarded run doubts what the sources say is no longer wanted, so it lets go of nothing.
        disown = []
    elif scheduled and len(disown) > guards.max_unmonitors_scheduled:
        # As many as the unmonitor cap on an unattended run points at a bad read; a run by hand
        # shows the count for review.
        disown = []

    # ------------------------------------------------------------------ projected wanted
    projected_wanted = 0
    for key in desired.releases:
        album = view.album(key)
        will_be_monitored = key in monitor_keys or (album is not None and album.monitored)
        if not will_be_monitored:
            continue
        if album is None or not album.has_files:
            projected_wanted += 1

    if projected_wanted > guards.projected_wanted_max:
        # Informational only: it blocks nothing, so `Diff.guarded` stays a statement about
        # refused unmonitors. The shell decides whether a wanted list this size is acceptable.
        guard_list.append(
            Guard(
                code="projected-wanted",
                message=(
                    f"{projected_wanted} desired releases would be monitored with no files on disk, "
                    f"over the advisory limit of {guards.projected_wanted_max}"
                ),
                blocked_unmonitors=0,
            )
        )

    return Diff(
        created_at=now,
        source_digest=source_digest,
        lidarr_digest=lidarr_digest(
            view,
            monitor,
            unmonitor,
            ratchets,
            set_new_items_none,
            monitor_artists,
            refresh_artists,
            [a.artist_mbid for a in add_artists],
            disown,
        ),
        add_artists=add_artists,
        monitor=monitor,
        unmonitor=unmonitor,
        ratchets=ratchets,
        set_new_items_none=set_new_items_none,
        guards=guard_list,
        pending=list(desired.pending),
        unmapped=unmapped,
        projected_wanted=projected_wanted,
        update_reasons=update_reasons,
        name_collisions=name_collisions,
        monitor_artists=monitor_artists,
        refresh_artists=refresh_artists,
        accept_shrink=accept_shrink,
        claim=claim,
        disown=disown,
    )


def lidarr_digest(
    view: LidarrView,
    monitor: list[MonitorRelease],
    unmonitor: list[UnmonitorRelease],
    ratchets: list[ProfileRatchet],
    set_new_items_none: list[str],
    monitor_artists: Sequence[str] = (),
    refresh_artists: Sequence[str] = (),
    add_artists: Sequence[str] = (),
    disown: Sequence[ReleaseKey] = (),
) -> str:
    """Hash the Lidarr state the diff depends on, so a stale diff can be refused.

    Covers every album the diff would touch (id and monitored flag) and every artist it would
    touch (id, metadata profile, monitorNewItems, the monitored flag of those it re-monitors, the
    id of those it refreshes, and whether those it adds are in Lidarr yet). Anything the diff does
    not touch is excluded, so unrelated Lidarr activity between planning and applying does not
    invalidate the plan.

    An artist to add is absent when the plan is made, so it adds nothing to the hash then: a plan
    hashes as it did before its adds were covered, and goes stale only once one appears.
    """
    h = hashlib.sha256()
    album_parts: list[str] = []
    touched = {m.key for m in monitor} | {u.key for u in unmonitor} | set(disown)
    for key in sorted(touched, key=lambda k: (k.artist_mbid, k.rg_mbid)):
        album = view.album(key)
        if album is not None:
            album_parts.append(f"{album.id}:{int(album.monitored)}")
    for part in sorted(album_parts):
        h.update(b"a" + part.encode())

    artist_parts: list[str] = []
    for mbid in sorted({r.artist_mbid for r in ratchets} | set(set_new_items_none)):
        artist = view.artists.get(mbid)
        if artist is not None:
            artist_parts.append(f"{artist.id}:{artist.metadata_profile_id}:{artist.monitor_new_items}")
    for part in sorted(artist_parts):
        h.update(b"r" + part.encode())

    monitored_parts = [f"{a.id}:{int(a.monitored)}" for mbid in monitor_artists if (a := view.artists.get(mbid))]
    for part in sorted(monitored_parts):
        h.update(b"m" + part.encode())

    refresh_parts = [str(a.id) for mbid in refresh_artists if (a := view.artists.get(mbid))]
    for part in sorted(refresh_parts):
        h.update(b"f" + part.encode())

    added_parts = [f"{mbid}:{a.id}" for mbid in add_artists if (a := view.artists.get(mbid))]
    for part in sorted(added_parts):
        h.update(b"n" + part.encode())
    return h.hexdigest()


def is_stale(diff: Diff, current_source_digest: str, current_lidarr_digest: str) -> bool:
    """True when the world moved under a saved diff and applying it would be a guess."""
    return diff.source_digest != current_source_digest or diff.lidarr_digest != current_lidarr_digest


def config_changes(
    recorded: Mapping[str, Mapping[str, object]] | None, current: Mapping[str, Mapping[str, object]]
) -> list[str] | None:
    """The settings that differ between a saved diff's configuration and today's, as `[section] key`.

    `[]` when nothing moved. ``None`` when the diff recorded no configuration at all, which is not
    the same answer: a diff written before `Diff.config_fingerprint` existed is no evidence that
    nothing changed, and the caller decides what that is worth. A setting only one side knows about
    counts as changed, so a diff planned before a setting existed cannot vouch for it either.
    """
    if recorded is None:
        return None
    changed: list[str] = []
    for section in sorted(set(recorded) | set(current)):
        before, after = recorded.get(section) or {}, current.get(section) or {}
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after or before[key] != after[key]:
                changed.append(f"[{section}] {key}")
    return changed


def _best_step(steps: dict[str, str]) -> str:
    """One representative resolver step for a release wanted for several reasons."""
    return steps[sorted(steps)[0]] if steps else ""


def manual_reasons(reasons: Iterable[Reason]) -> frozenset[Reason]:
    """The `manual` reasons among `reasons`: the ones a later write must carry over, never drop."""
    return frozenset(r for r in reasons if r.kind is ReasonKind.MANUAL)


def best_reason_key(reasons: Iterable[Reason]) -> str:
    """One representative intent key for a release wanted for several reasons.

    Public because the shell has to stamp the *same* key on the releases it observes for the
    health baseline. Two copies of this rule would drift, and every affected release would start
    reading as a regression the run after they diverged.
    """
    return sorted(r.key for r in reasons)[0] if reasons else ""
