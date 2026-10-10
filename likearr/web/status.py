"""The Status page's view model: "is this thing working", answered without a field name.

Pure functions over the `runs` history (`SqliteState.run_history`), so the prose is tested
without a server. Three rules from the design shape it:

- **The last applied run is kept apart from the last run of any kind.** Every run is recorded,
  dry runs included, and a UI dry run five minutes ago must not look like it changed anything.
  A skipped, stale or failed apply changed nothing either, so none of them is "last applied" -
  unless it failed part-way, having changed some of Lidarr: that one is, and says how far it got
  ("stopped part-way: 12 of 40 changes made").
- **What moved, not what stands.** The chronic counts (hundreds of unmapped songs on a
  large library, every run) are what the health change detection exists to look past,
  so the page spells out the ``*_new`` counts and the new conditions, never the totals.
- **Projected wanted is labelled as projected**: it is the diff's estimate of releases left
  monitored with no files, not Lidarr's live wanted list.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from likearr.adapters.spotify import NOTHING_CHANGED_RETRY
from likearr.adapters.state_sqlite import CoveragePoint, RunRow
from likearr.config import is_mbid
from likearr.core.cron import CronError, longest_gap
from likearr.core.explain import resolution_outcome
from likearr.models import APPLIED_STATUSES, EXIT_BUSY, HealthRecord, NameCollision, RunStatus, SourceKind
from likearr.playlist_names import playlist_url
from likearr.shell.last_run import LastRun, release_counts
from likearr.shell.run import CONDITION_TEXT

__all__ = [
    "ChecklistStep",
    "CollisionCard",
    "Coverage",
    "HealthGlance",
    "ReauthView",
    "RunSummary",
    "StatusView",
    "ago",
    "build_status",
    "collision_cards",
    "condition_sentence",
    "coverage",
    "describe_run",
    "first_run_checklist",
    "health_glance",
    "lost_state_sentence",
    "reauth_banner_note",
    "reauth_view",
    "source_counts",
    "stale_after",
]

_REAUTH_WARN_DAYS = 30

_NEWLY = (
    ("unmapped_new", "newly unmapped songs or albums"),
    ("regressions", "releases that mapped last run no longer do"),
    ("skipped_artists_new", "new artist(s) skipped for a Lidarr metadata failure"),
    ("name_collisions_new", "new name collision(s)"),
    ("catalogue_too_large_new", "new followed artist(s) too large for MusicBrainz to browse"),
    ("absent_in_lidarr_new", "wanted release(s) newly missing from Lidarr's catalogue"),
    ("catalogue_gaps_recent_new", "new release(s) from followed artists Lidarr has not caught up with"),
    ("catalogue_gaps_new", "new catalogue gap(s) Lidarr will probably never track"),
    ("lidarr_metadata_errors_new", "new Lidarr metadata lookup failure(s)"),
)
"""The change-detection counts, in the order they matter. A plural noun is written so that "1"
still reads, and the singular overrides below cover the ones a person would notice."""

_SINGULAR = {"name_collisions_new": "new name collision"}

_BASELINE_NOTES = {
    "first-run": "This was the first run, so there was nothing to compare with.",
    "resolver-version-changed": "Nothing to compare with this time: likearr's matching logic changed.",
    "scope-changed": "Nothing to compare with this time: liked_track_scope changed.",
    "sources-changed": "Nothing to compare with this time: the Spotify sources changed.",
    "rules-changed": "Nothing to compare with this time: the rules changed since the last apply.",
}

_SOURCE_LABELS = {
    str(SourceKind.FOLLOWED_ARTISTS): "Followed artists",
    str(SourceKind.SAVED_ALBUMS): "Saved albums",
    str(SourceKind.LIKED_TRACKS): "Liked songs",
}


@dataclass(frozen=True, slots=True)
class RunSummary:
    record: HealthRecord
    when: datetime
    ago: str
    applied: bool
    headline: str
    tone: str
    """``ok``, ``warn``, ``bad`` or ``quiet`` - the page's colour, and nothing else."""
    run_id: int = 0
    """The `runs` row id (`RunRow.id`); 0 when built without one. What `/runs/<id>` links to
    for this run's full "What changed"."""
    conditions: list[str] = field(default_factory=list)
    newly: list[str] = field(default_factory=list)
    guards: tuple[str, ...] = ()
    baseline_note: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class ReauthView:
    authorized_at: datetime | None
    due: datetime | None
    days_left: int | None
    tone: str


@dataclass(frozen=True, slots=True)
class CollisionCard:
    """One name collision, as the Status page shows it: who is who, what it cost, where to look.

    Every link is built from a validated MBID and nothing else, so a malformed id in a stored diff
    can never become a link. The Lidarr routes are the SPA's own, read from its source
    (frontend/src/App/AppRoutes.js at v3.1.0.4875): ``/artist/:foreignArtistId`` - the MusicBrainz
    id, not Lidarr's numeric one - and ``/add/search``, which reads ``?term=`` and searches as the
    page opens. ``lidarr:<mbid>`` is an id lookup there (SkyHookProxy.SearchForNewArtist), so the
    add page opens on exactly the wanted artist.
    """

    name: str
    wanted_label: str
    existing_label: str
    existing_lidarr_id: int
    dropped_releases: int
    wanted_musicbrainz: str
    existing_musicbrainz: str
    existing_in_lidarr: str
    add_in_lidarr: str
    wanted_mbid: str = ""
    """The wanted artist's MBID, validated; empty when it was not one. What the card's actions key on."""
    other_in_lidarr: bool = True
    """False when the other artist is not in Lidarr either: both were new in the same run, and both
    were skipped."""


@dataclass(frozen=True, slots=True)
class StatusView:
    last_applied: RunSummary | None
    last_any: RunSummary | None
    history: list[RunSummary]
    sources: list[tuple[str, int, str | None]]
    projected_wanted: int | None
    collisions: list[CollisionCard] = field(default_factory=list)
    """From the newest run that planned: a collision fixed since is not shown."""
    last_planned: HealthRecord | None = None
    """The newest run that read the sources, for the counts At a glance falls back on."""
    wanted_missing_url: str = ""
    """Lidarr's Wanted > Missing page, for the "waiting for a download" note. Empty without a
    `lidarr_url` (same rule as `collision_cards`): a bare path would open on the likearr UI itself."""
    tagged_without_state: int = 0
    """From the newest run that planned: artists carrying likearr's Lidarr tag that the state
    database has no record of. A run that failed before it read Lidarr does not hide it, and a
    record from before the field existed reads as 0."""


def lost_state_sentence(count: int, tag: str) -> str:
    """Status's line for the lost-state warning, or ``""`` when there is nothing to say."""
    if count <= 0:
        return ""
    artists = "1 artist" if count == 1 else f"{count} artists"
    return (
        f"Lidarr has {artists} tagged {tag} that likearr's state database doesn't know. If you lost the "
        "database, restore it from backup. Until then, likearr never unmonitors anything it monitored before."
    )


def ago(now: datetime, then: datetime) -> str:
    """``"3 h ago"``, ``"in 2 h"``: rough on purpose; the exact time sits next to it."""
    seconds = (now - then).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        text = f"{int(seconds // 60)} min"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)} h"
    else:
        days = int(seconds // 86400)
        text = f"{days} day" if days == 1 else f"{days} days"
    return f"in {text}" if future else f"{text} ago"


_MESSAGE_TRUNCATE_CHARS = 120
"""History's Message column holds whatever the run wrote - up to a raw JSON body with a
Spotify URL twice over, hundreds of characters on one row. The full text still reaches the
page, in the `title` attribute next to this - only what's shown inline is capped."""


def short_message(message: str) -> str:
    """The first sentence if there is one within `_MESSAGE_TRUNCATE_CHARS`, else that many
    characters with an ellipsis. `message` unchanged (or empty) either way if it's already short."""
    if len(message) <= _MESSAGE_TRUNCATE_CHARS:
        return message
    period = message.find(". ", 0, _MESSAGE_TRUNCATE_CHARS)
    if period != -1:
        return message[: period + 1]
    return message[:_MESSAGE_TRUNCATE_CHARS].rstrip() + "…"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


_NEW_ITEMS_NONE = '"Monitor New Albums: None"'


def _headline(record: HealthRecord) -> str:
    counts = record.counts
    monitored, unmonitored, added = counts.get("monitored", 0), counts.get("unmonitored", 0), counts.get("added", 0)
    none_set = counts.get("new_items_none", 0)  # Said only when there are some
    if record.status is RunStatus.SKIPPED:
        return "Skipped: another run was already in progress."
    if record.status is RunStatus.PAUSED:
        return f"Paused: {record.message.removeprefix('scheduled runs are paused: ')}"
    if record.status is RunStatus.ERROR:
        if record.exit_code == EXIT_BUSY:
            return "Didn't run: another run held the lock."
        if not record.dry_run and record.changes_made:
            if record.changes_planned is not None and record.changes_made >= record.changes_planned:
                # Everything planned reached Lidarr (a batch it applied but then answered with an
                # error) - not "part-way", the confirmation was what failed.
                return (
                    f"Finished: all {record.changes_planned} planned changes made, but confirming it "
                    f"failed. {_why_stopped(record)}"
                )
            planned = f" of {record.changes_planned}" if record.changes_planned is not None else ""
            return f"Stopped part-way: {record.changes_made}{planned} changes made. {_why_stopped(record)}"
        if not record.dry_run and record.lidarr_changed:
            return f"Stopped part-way: Lidarr settings may have changed, no planned change made. {_why_stopped(record)}"
        if (
            not record.dry_run
            and record.changes_made == 0
            and record.lidarr_changed is False
            and not (record.message or "").endswith(NOTHING_CHANGED_RETRY)
        ):
            return f"Failed, and changed nothing: {_why_stopped(record)}"
        return f"Failed: {record.message or 'see the log'}"
    if record.status is RunStatus.STALE:
        return f"Nothing applied: {record.message or 'the plan was stale'}"
    if record.dry_run:
        new_items = f", set {_count(none_set, 'artist')} to {_NEW_ITEMS_NONE}" if none_set else ""
        return (
            f"Dry run: would monitor {_count(monitored, 'release')}, unmonitor {unmonitored}, "
            f"add {_count(added, 'artist')}{new_items}."
        )
    new_items = f", {_count(none_set, 'artist')} set to {_NEW_ITEMS_NONE}" if none_set else ""
    return (
        f"Applied: {_count(monitored, 'release')} monitored, {unmonitored} unmonitored, "
        f"{_count(added, 'artist')} added{new_items}."
    )


def last_change(view: StatusView) -> RunSummary | None:
    """The newest kept apply that changed something: counts above zero, or an apply that stopped
    part-way or failed after touching Lidarr (kept for its headline). Most scheduled applies change
    nothing, so the newest apply is often not it."""
    for run in view.history:
        if not run.applied:
            continue
        counts = run.record.counts
        changed = sum(counts.get(k, 0) for k in ("monitored", "unmonitored", "added", "new_items_none", "claimed"))
        if changed or run.record.status not in APPLIED_STATUSES:
            return run
    return None


def change_summary(run: RunSummary) -> str:
    """The "Last change to Lidarr" card's line: what an apply did, or its headline when it did
    not finish cleanly."""
    record = run.record
    if record.status not in APPLIED_STATUSES:
        return run.headline
    counts = record.counts
    monitored, unmonitored, added = counts.get("monitored", 0), counts.get("unmonitored", 0), counts.get("added", 0)
    none_set = counts.get("new_items_none", 0)
    new_items = f", set {_count(none_set, 'artist')} to {_NEW_ITEMS_NONE}" if none_set else ""
    claimed = counts.get("claimed", 0)
    managed = f" {_count(claimed, 'album')} you already monitored now managed." if claimed else ""
    return (
        f"Monitored {_count(monitored, 'release')}, unmonitored {unmonitored}, "
        f"added {_count(added, 'artist')}{new_items}.{managed}"
    )


@dataclass(frozen=True, slots=True)
class Pending:
    """The "Pending changes" card: a check newer than the last apply that found something to do."""

    run: RunSummary
    found: str
    """"8 to monitor, 2 to unmonitor, 2 artists to add"."""
    next_run: str
    """What the next automatic run does with them, in one line."""
    held: bool
    """Some of it will be held back: said in the warning style."""


def pending_changes(view: StatusView, *, schedule_on: bool, first_applied: bool, unmonitor_cap: int) -> Pending | None:
    """The newest run when it is a check that found changes, else ``None``. The note reads only
    what the check recorded: its blocking guards, and its unmonitor count against the cap a
    scheduled run applies (`max_unmonitors_scheduled`)."""
    run = view.last_any
    if run is None or not run.record.dry_run or run.record.status not in APPLIED_STATUSES:
        return None
    counts = run.record.counts
    monitored, unmonitored, added = counts.get("monitored", 0), counts.get("unmonitored", 0), counts.get("added", 0)
    none_set = counts.get("new_items_none", 0)
    parts = [f"{n} {what}" for n, what in ((monitored, "to monitor"), (unmonitored, "to unmonitor")) if n]
    if added:
        parts.append(f"{_count(added, 'artist')} to add")
    if none_set:
        parts.append(f"{_count(none_set, 'artist')} to set to {_NEW_ITEMS_NONE}")
    if claimed := counts.get("claimed", 0):
        parts.append(f"{_count(claimed, 'album')} to start managing")
    if not parts:
        return None
    held = False
    if not schedule_on:
        note = "Automatic runs are paused: these apply only when you review them."
    elif not first_applied:
        note = "These apply only when you review them."
    elif run.guards:
        note, held = "The next automatic run applies these, but a guard holds some unmonitors back.", True
    elif unmonitored > unmonitor_cap:
        note, held = (
            f"The next automatic run holds back all {unmonitored} unmonitors: over the cap of {unmonitor_cap}.",
            True,
        )
    else:
        note = "The next automatic run applies these."
    return Pending(run=run, found=", ".join(parts), next_run=note, held=held)


def _why_stopped(record: HealthRecord) -> str:
    """The cause, without the lead-in the record's message already spells out in the headline."""
    message = record.message or "see the log"
    for lead in (
        "the apply stopped part-way: ",
        "the apply finished: ",
        "the apply failed before changing anything: ",
    ):
        if message.startswith(lead):
            message = message[len(lead) :]
            if ": " in message and not lead.startswith("the apply failed"):
                # "N of M changes made: <cause>" or "all N planned changes were made, but
                # confirming it failed: <cause>" - the headline already says the counted part.
                message = message.split(": ", 1)[1]
    return message


def _tone(record: HealthRecord, guards: Sequence[str]) -> str:
    if record.status is RunStatus.ERROR and record.exit_code == EXIT_BUSY:
        return "quiet"  # nothing ran: not a failure
    if record.status is RunStatus.ERROR:
        return "bad"
    if record.status in {RunStatus.SKIPPED, RunStatus.STALE, RunStatus.PAUSED}:
        return "quiet"
    if record.status in {RunStatus.DEGRADED, RunStatus.GUARDED} or guards or record.new_conditions:
        return "warn"
    return "ok"


def _newly(record: HealthRecord) -> list[str]:
    out: list[str] = []
    for name, noun in _NEWLY:
        n = int(getattr(record, name, 0) or 0)
        if n:
            out.append(f"{n} {_SINGULAR[name]}" if n == 1 and name in _SINGULAR else f"{n} {noun}")
    return out


def _blocking(row: RunRow) -> tuple[str, ...]:
    """The guards that held unmonitors back. An advisory one (projected-wanted, a name collision's
    note) holds nothing back and leaves the run green, as Home Assistant sees it."""
    blocked = row.guard_blocked or tuple(1 for _ in row.guards)  # a row read without the counts
    return tuple(g for g, n in zip(row.guards, blocked, strict=False) if n > 0)


def describe_run(row: RunRow, *, now: datetime, tz: ZoneInfo) -> RunSummary:
    record = row.record
    when = datetime.fromtimestamp(record.ts, tz=tz)
    guards = _blocking(row)
    return RunSummary(
        record=record,
        when=when,
        ago=ago(now, when),
        applied=not record.dry_run
        and (record.status in APPLIED_STATUSES or bool(record.changes_made) or bool(record.lidarr_changed)),
        run_id=row.id,
        headline=_headline(record),
        tone=_tone(record, guards),
        conditions=[CONDITION_TEXT.get(c, c) for c in record.new_conditions],
        newly=_newly(record),
        guards=guards,
        baseline_note=_BASELINE_NOTES.get(record.baseline, ""),
        message=record.message if record.status not in {RunStatus.ERROR, RunStatus.STALE} else "",
    )


def source_counts(counts: Mapping[str, int], playlist_names: Mapping[str, str]) -> list[tuple[str, int, str | None]]:
    """The run's per-source counts as (label, count, link), with playlists by name where one is
    known. A playlist with no known name is its id, linked to it on Spotify."""
    out: list[tuple[str, int, str | None]] = [
        (label, counts[key], None) for key, label in _SOURCE_LABELS.items() if key in counts
    ]
    for key in sorted(k for k in counts if k.startswith("playlist:")):
        playlist_id = key.removeprefix("playlist:")
        name = playlist_names.get(playlist_id)
        if name:
            out.append((f"Playlist: {name}", counts[key], None))
        else:
            out.append((f"Playlist {playlist_id}", counts[key], playlist_url(playlist_id)))
    return out


def reauth_view(authorized_at: datetime | None, due: datetime | None, *, now: datetime) -> ReauthView:
    """When Spotify's refresh token dies. Unknown is a warning: the first sign would be every run failing."""
    if due is None:
        return ReauthView(authorized_at, None, None, "warn")
    if due < now:
        # Negative, and never 0: "0 days left" would read as "today" for a token already dead.
        return ReauthView(authorized_at, due, -max(1, (now - due) // timedelta(days=1)), "bad")
    days_left = (due - now) // timedelta(days=1)
    tone = "warn" if days_left <= _REAUTH_WARN_DAYS else "ok"
    return ReauthView(authorized_at, due, days_left, tone)


def reauth_banner_note(reauth: ReauthView, *, has_token: bool) -> str:
    """Which sentence, if any, the Status banner adds about Spotify re-authorization.

    The banner is the single place on the page that says whether something needs the user, so
    both its branches - the green "All good" and the amber "Needs attention" - call this to decide
    whether a re-auth sentence belongs there, and which one. The rule: a
    due or expired token never becomes a *problem* - `health_glance` and its `problems` are
    unchanged by this, so Home Assistant's amber stays in step with `HealthGlance.healthy`, and an
    expired token only turns the banner amber once a run actually fails.

    Empty when `reauth.tone` is "ok": not due within `_REAUTH_WARN_DAYS`, so the banner's "nothing
    needs you" keeps meaning that. Otherwise one of four keys, for the template to turn into the
    full sentence (the date and the Settings link are the template's job, not this pure function's
    - it has no timezone to format one with):

    - "no-token": no token file at all.
    - "unknown-date": a token file with no `authorized_at`: re-authorizing records it.
    - "due-soon": due within the warn window.
    - "overdue": due date has passed.
    """
    if reauth.tone == "ok":
        return ""
    if reauth.due is None:
        return "no-token" if not has_token else "unknown-date"
    return "overdue" if reauth.days_left is not None and reauth.days_left < 0 else "due-soon"


_MUSICBRAINZ = "https://musicbrainz.org"


def _label(name: str, disambiguation: str) -> str:
    return f"{name} ({disambiguation})" if disambiguation else name


def collision_cards(collisions: Sequence[NameCollision], *, lidarr_url: str) -> list[CollisionCard]:
    """The Status page's card for each collision, with links to MusicBrainz and to Lidarr.

    No Lidarr links without a `lidarr_url`: a bare path would open on the likearr UI itself.
    """
    cards: list[CollisionCard] = []
    for c in collisions:
        wanted = c.wanted_mbid if is_mbid(c.wanted_mbid) else ""
        existing = c.existing_mbid if is_mbid(c.existing_mbid) else ""
        term = urllib.parse.quote(f"lidarr:{wanted}", safe="")
        cards.append(
            CollisionCard(
                name=c.name,
                wanted_label=_label(c.name, c.wanted_disambiguation),
                existing_label=_label(c.existing_name or c.name, c.existing_disambiguation),
                existing_lidarr_id=c.existing_lidarr_id,
                dropped_releases=c.dropped_releases,
                wanted_musicbrainz=f"{_MUSICBRAINZ}/artist/{wanted}" if wanted else "",
                existing_musicbrainz=f"{_MUSICBRAINZ}/artist/{existing}" if existing else "",
                existing_in_lidarr=(
                    f"{lidarr_url}/artist/{existing}" if existing and lidarr_url and c.in_lidarr else ""
                ),
                add_in_lidarr=f"{lidarr_url}/add/search?term={term}" if wanted and lidarr_url else "",
                wanted_mbid=wanted,
                other_in_lidarr=c.in_lidarr,
            )
        )
    return cards


def _has_source_counts(counts: Mapping[str, int]) -> bool:
    """Whether a run read any source: a plan counts each one, a failed or skipped run none."""
    return any(key in _SOURCE_LABELS or key.startswith("playlist:") for key in counts)


def build_status(
    rows: Sequence[RunRow],
    *,
    now: datetime,
    tz: ZoneInfo,
    playlist_names: Mapping[str, str] | None = None,
    lidarr_url: str = "",
) -> StatusView:
    summaries = [describe_run(row, now=now, tz=tz) for row in rows]
    last_applied = next((s for s in summaries if s.applied), None)
    last_planned = next((row.record for row in rows if _has_source_counts(row.record.counts)), None)
    with_counts = last_planned.counts if last_planned is not None else {}
    # The newest run that planned against the current world: a stale refusal stores the old saved
    # diff beside its record, so its projection and collisions describe an earlier plan.
    planned = next(
        (row for row in rows if row.projected_wanted is not None and row.record.status is not RunStatus.STALE),
        None,
    )
    projected = planned.projected_wanted if planned is not None else None
    return StatusView(
        last_applied=last_applied,
        last_any=summaries[0] if summaries else None,
        history=summaries,
        sources=source_counts(with_counts, playlist_names or {}),
        projected_wanted=projected,
        collisions=collision_cards(planned.name_collisions, lidarr_url=lidarr_url) if planned is not None else [],
        last_planned=last_planned,
        wanted_missing_url=f"{lidarr_url}/wanted/missing" if lidarr_url else "",
        tagged_without_state=last_planned.tagged_without_state if last_planned is not None else 0,
    )


# ---------------------------------------------------------------- at a glance

STALE_AFTER = timedelta(hours=13)
"""The shortest time without a run that needs attention, whatever the schedule: the example
``binary_sensor.likearr_stale``'s 13 hours (docs/install.md, "MQTT"), which suits the default
six-hourly schedule. A sparser schedule waits longer (`stale_after`)."""

STALE_SLACK = timedelta(hours=2)
"""Added to the schedule's longest gap: a fire can queue for up to an hour behind another job
(`jobs.QUEUE_WAIT_S`), and the run itself takes a while."""


def stale_after(cron: str, tz: ZoneInfo, now: datetime) -> timedelta:
    """How long without a run needs attention under `cron`: its longest gap between fires plus
    `STALE_SLACK`, never under `STALE_AFTER`. A line that can't be read, or fires fewer than
    twice in the window `longest_gap` looks at, keeps `STALE_AFTER`."""
    try:
        gap = longest_gap(cron, tz, now)
    except CronError:
        return STALE_AFTER
    return max(STALE_AFTER, gap + STALE_SLACK) if gap is not None else STALE_AFTER


_HA_AMBER = frozenset({RunStatus.ERROR, RunStatus.STALE, RunStatus.GUARDED, RunStatus.DEGRADED})
"""The statuses the Home Assistant problem flag in docs/install.md lights on."""


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {many}"


RUN_PAGE = "run"
"""A problem's link target meaning "the run's own page" (`/runs/<id>`), resolved by `health_glance`."""


def condition_sentence(
    condition: str, record: HealthRecord, collisions: Sequence[NameCollision] = ()
) -> tuple[str, str]:
    """A new condition as one plain sentence for the Status banner, and where it is explained.

    Built from the record (and the run's own collisions), so it names what happened and how much;
    the CLI's terse `CONDITION_TEXT` is the fallback for a condition this does not know yet.
    """
    if condition == "new-name-collision":
        names = [c.name for c in collisions]
        count = max(record.name_collisions_new, 1)
        if count == 1 and len(names) == 1:
            who = names[0]
        elif names and len(names) <= 3:
            who = f"{_n(count, 'artist', 'artists')} ({', '.join(names)})"
        else:
            who = _n(count, "artist", "artists")
        return (
            f"likearr skipped {who}: a different artist with the same name is already in Lidarr - see below.",
            "#collisions",
        )
    if condition == "mapping-shortfall-jump":
        text = f"{_n(record.regressions, 'release that', 'releases that')} matched at the last run no longer match"
        if record.unmapped_new:
            text += f", and {_n(record.unmapped_new, 'song or album', 'songs or albums')} newly couldn't be matched"
        return text + ".", "/unmatched"
    if condition == "mb-outage":
        return (
            "MusicBrainz lookups failed this run, so some songs weren't matched; likearr tries again next run.",
            "/unmatched",
        )
    if condition == "lidarr-metadata-outage":
        failed = record.lidarr_metadata_errors_new or record.lidarr_metadata_errors
        count = f" ({failed} failed)" if failed else ""
        return (
            f"Lidarr's metadata server failed most lookups this run{count}; the artists affected are tried again "
            "next run.",
            RUN_PAGE,
        )
    if condition == "new-skipped-artist":
        n = max(record.skipped_artists_new, 1)
        return (
            f"{_n(n, 'artist was', 'artists were')} skipped because Lidarr couldn't look "
            f"{'it' if n == 1 else 'them'} up; likearr tries again next run.",
            RUN_PAGE,
        )
    if condition == "new-catalogue-too-large":
        n = max(record.catalogue_too_large_new, 1)
        return (
            f"{_n(n, 'followed artist has', 'followed artists have')} more releases than MusicBrainz lets likearr "
            "read, so only part of their catalogue is wanted.",
            RUN_PAGE,
        )
    if condition == "spotify-schema":
        return (
            "Spotify's answer was incomplete, so this run held back every unmonitor. If it keeps happening, "
            "Spotify has changed something.",
            RUN_PAGE,
        )
    return CONDITION_TEXT.get(condition, condition), RUN_PAGE


@dataclass(frozen=True, slots=True)
class ChecklistStep:
    label: str
    url: str
    done: bool | None
    """Whether this step is finished, from local state only. ``None`` for a step that can only be
    confirmed with a live call (Lidarr setup): Status never makes one, so that step always shows
    as a link with no tick either way, never as done."""


def first_run_checklist(*, has_token: bool, published: bool) -> list[ChecklistStep] | None:
    """The three-step setup checklist Status shows in place of "No run has finished yet." on a
    fresh install: connect Spotify, set up Lidarr, check for changes - the same steps the
    README's quick start walks through.

    Shown whenever a step is not done yet - no token file, or no run has ever published to the
    sinks. Once both hold, this returns ``None`` and the ordinary "at a glance" banner takes over.
    Every state it reads is local (the token file's presence, `published`): no Spotify or Lidarr
    call, so it is safe to build on every Status GET.
    """
    if has_token and published:
        return None
    return [
        ChecklistStep("Connect Spotify", "/settings#spotify", has_token),
        ChecklistStep("Set up Lidarr", "/settings#lidarr-setup", None),
        ChecklistStep("Check for changes", "/plan", published),
    ]


@dataclass(frozen=True, slots=True)
class HealthGlance:
    """Health in words, as Home Assistant sees it: the newest run it was sent."""

    healthy: bool
    run: RunSummary | None
    """``None`` when no run has reached Home Assistant yet, which it shows as a problem."""
    problems: list[tuple[str, str]] = field(default_factory=list)
    """(what is wrong, where on the page it is explained - an anchor, or "")."""
    notes: list[str] = field(default_factory=list)
    """Advisory guards: said, but not a problem - they held nothing back and Home Assistant stays green."""


def health_glance(
    published: RunRow | None,
    *,
    now: datetime,
    tz: ZoneInfo,
    collisions_shown: bool = True,
    stale: timedelta = STALE_AFTER,
) -> HealthGlance:
    """What Home Assistant's problem flag and stale sensor say, in words, from `published`: the
    newest run the sinks were sent (`SqliteState.last_published_run`), however far back. A new
    name collision links to the collision cards only when the page shows them (`collisions_shown`).
    No run for longer than `stale` (`stale_after` of the schedule) is a problem."""
    if published is None:
        # Fails closed, mirroring Home Assistant's dead-man's switch: no record at all is a problem.
        return HealthGlance(False, None, [("No run has finished yet.", "")])
    record = published.record
    run = describe_run(published, now=now, tz=tz)
    problems: list[tuple[str, str]] = []
    if record.status is RunStatus.ERROR:
        reason = (record.message or "see its log").removesuffix(".")
        problems.append((f"The last run failed: {reason}.", "#history"))
    elif record.status is RunStatus.STALE:
        problems.append(
            ("The last apply was refused: Spotify or Lidarr changed since its check. Check again.", "#history")
        )
    for condition in record.new_conditions:
        text, anchor = condition_sentence(condition, record, published.name_collisions)
        if condition == "new-name-collision" and not collisions_shown:
            text, anchor = text.removesuffix(" - see below."), RUN_PAGE
        problems.append((text, anchor))
    blocked = dict(zip(published.guards, published.guard_blocked, strict=False))
    problems.extend((guard, RUN_PAGE) for guard in published.guards if blocked.get(guard, 0) > 0)
    # An advisory name-collision guard is the collision card's to explain, not a second note.
    codes = dict(zip(published.guards, published.guard_codes, strict=False))
    notes = [g for g in published.guards if blocked.get(g, 0) <= 0 and codes.get(g) != "name-collision"]
    if record.status is RunStatus.PAUSED:
        # Not a problem: paused is on purpose, and `ts` is still fresh, which is the whole point -
        # it keeps the HA dead-man from also lighting up while scheduled runs are stopped.
        notes.append(f"scheduled runs are paused ({run.headline.removeprefix('Paused: ')})")
    if record.status in _HA_AMBER and not problems:
        problems.append((record.message or f"The last run's status is {record.status}.", RUN_PAGE))
    if now - run.when > stale:
        problems.append(
            (f"No run for {ago(now, run.when).removesuffix(' ago')}: check the scheduler (Settings → Schedule).", "")
        )
    run_page = f"/runs/{run.run_id}" if run.run_id else "#history"
    problems = [(text, run_page if anchor == RUN_PAGE else anchor) for text, anchor in problems]
    return HealthGlance(healthy=not problems, run=run, problems=problems, notes=notes)


@dataclass(frozen=True, slots=True)
class Coverage:
    """How what you like, follow and put in playlists stands in Lidarr, as of the last run.

    Every intent is counted once, by its reason key (a song twice in one playlist is one), and
    lands in exactly one of the outcome counts, so they add up to `intents`.
    """

    as_of: datetime
    label: str
    """"an apply", "a dry run", or a refused apply (`LastRun.label`)."""
    dry: bool
    """The facts are a plan's, not an apply's: what is not monitored may be about to be."""
    intents: int
    """Songs, albums and followed artists read from Spotify."""
    matched_releases: int
    """Songs and albums matched to a release."""
    matched_artists: int
    """Followed artists matched to a MusicBrainz artist."""
    unmatched: int
    """Couldn't be matched: the true misses. Nothing is monitored for them."""
    ambiguous: int
    """Two different artists share the name and the title, and likearr would not guess which.
    Counted apart from `unmatched` so each line equals the Not added page's card for it."""
    excluded: int
    """Left out by your settings (a remix, a compilation, a denied release)."""
    lookup_failed: int
    """The lookup failed this run; retried at the next."""
    pending: int
    """Liked singles waiting for their album."""
    releases: int
    """Releases those matches want."""
    monitored: int
    downloaded: int
    """Monitored, with files on disk (as the run read them)."""
    waiting: int
    """Monitored, no files yet: waiting for a download."""
    not_monitored: int
    """Wanted but not monitored."""
    would_monitor: int = 0
    """Of `not_monitored`, how many the plan would monitor (a dry run's facts only)."""


def coverage(last: LastRun) -> Coverage:
    keys = {i.reason.key for i in (*last.snapshot.tracks, *last.snapshot.albums, *last.snapshot.artists)}
    outcomes = {
        "matched": 0,
        "matched-artist": 0,
        "pending": 0,
        "excluded": 0,
        "failed": 0,
        "unmatched": 0,
        "ambiguous": 0,
    }
    for key in keys:
        resolution = last.resolutions.get(key) or last.artist_resolutions.get(key)
        outcome = resolution_outcome(resolution) if resolution is not None else "unmatched"
        if outcome == "matched" and key in last.artist_resolutions:
            outcome = "matched-artist"
        outcomes[outcome] += 1
    releases, monitored, downloaded = release_counts(last.desired, last.view)
    dry = not last.applied
    would = 0
    if dry:
        unmonitored = (k for k in last.desired.releases if not ((a := last.view.album(k)) is not None and a.monitored))
        would = sum(1 for k in unmonitored if k in (last.monitor or ()))
    return Coverage(
        as_of=last.ran_at,
        label=last.label,
        dry=dry,
        intents=len(keys),
        matched_releases=outcomes["matched"],
        matched_artists=outcomes["matched-artist"],
        unmatched=outcomes["unmatched"],
        ambiguous=outcomes["ambiguous"],
        excluded=outcomes["excluded"],
        lookup_failed=outcomes["failed"],
        pending=outcomes["pending"],
        releases=releases,
        monitored=monitored,
        downloaded=downloaded,
        waiting=monitored - downloaded,
        not_monitored=releases - monitored,
        would_monitor=would,
    )


# ---------------------------------------------------------------- coverage over time


@dataclass(frozen=True, slots=True)
class CoverageTrend:
    """The coverage chart: percent downloaded per day as SVG points in a 100 x 100 box (x by date,
    so a gap in runs shows as one; y on a fixed 0-100% scale, 0 at the bottom)."""

    line: str
    """Polyline points."""
    area: str
    """Polygon points: the line closed down to the 0% baseline."""
    first: CoveragePoint
    last: CoveragePoint
    first_pct: int
    last_pct: int


def _pct(point: CoveragePoint) -> float:
    return 100 * point.downloaded / point.releases if point.releases else 0.0


def coverage_trend(points: Sequence[CoveragePoint]) -> CoverageTrend | None:
    """The chart for `points` (oldest first), or ``None`` until there are two days to compare."""
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    span = max((last.day - first.day).days, 1)
    coords = [(round(100 * (p.day - first.day).days / span, 2), round(100 - _pct(p), 2)) for p in points]
    line = " ".join(f"{x},{y}" for x, y in coords)
    return CoverageTrend(
        line=line,
        area=f"{coords[0][0]},100 {line} {coords[-1][0]},100",
        first=first,
        last=last,
        first_pct=round(_pct(first)),
        last_pct=round(_pct(last)),
    )
