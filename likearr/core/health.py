"""What makes a run `ok`, and what makes it `degraded`.

Pure and side-effect free: no I/O, no clock, no config objects. The shell observes, this module
decides, and every rule below is therefore testable on its own.

**The rule.** A run is `ok` when nothing is *newly* wrong. It is `degraded` when something changed
for the worse, or an external dependency actually failed this run. Chronic conditions are reported
as counts.

That distinction is the whole point. Without it, a real library reports `degraded` on every run:
a few Lidarr search terms fail permanently, a few hundred intents never map and Lidarr's
catalogue will never hold some promos. A signal that is always on is a signal that is off, and an
operator will quite reasonably configure their dashboard to ignore it - so a MusicBrainz ban, a
metadata outage, a newly skipped artist and a new name collision all look like a normal day too.

**How "newly" is decided.** Every chronic condition has a stable *identity*. The previous run's
identities are persisted, and this run compares sets. Counts are never compared: 200 unmapped
intents that quietly became a *different* 200 is exactly the failure a count cannot see.

Three classes, because three kinds of thing need three different rules:

- **Class A, mapping shortfalls** (`unmapped`, `catalogue_gaps`, `absent_in_lidarr`,
  `lidarr_metadata`). One consequence: a release likearr wanted is not monitored. Large, churny,
  chronic by nature. Alarms only on a *jump*, and only for regressions (below).
- **Class B, discrete faults** (`skipped_artists`, `name_collisions`, `catalogue_too_large`).
  Rare, individually actionable, each costs a named artist's whole catalogue. Any new one alarms.
- **Class C, this-run dependency failures** (the Spotify schema canary, MusicBrainz, and a Lidarr
  metadata outage). No chronic set exists for any of them, so all three always alarm - including
  on a first run.

Plus `REPORTED`, which is not a class: dimensions that are compared and counted so the operator
can read a trend, and that decide nothing. `catalogue_gaps_recent` is the only one - see its
docstring for why a followed artist's brand-new album must never degrade a run.

**Regressions.** A class-A identity counts toward the jump only when its intent existed on the
previous run. "Something that was there last run and was fine is now not fine" is an alarm;
"a brand-new intent failed to map" is the 11-20% base rate and is never an alarm however many
arrive at once - otherwise liking 300 songs in a sitting would report a fault. Identities with no
intent of their own (a failed Lidarr lookup is attached to a search term, not to an intent) always
count: their measured failure rate on brand-new terms is low enough that a bulk add cannot flood
them, and they are the only thing that shows a metadata-proxy outage. That exemption is named in
`INTENTLESS` rather than inferred from an empty intent, so a caller that simply failed to work out
the intent cannot accidentally inherit it.

Note what is deliberately *not* a status input any more: the bare `lidarr_metadata_ok` boolean. It
is still published and still false on the chronic days; it just no longer decides the status on
its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from likearr.models import Fingerprint, HealthBaseline, RunStatus

# `Fingerprint` and `HealthBaseline` live in `likearr.models`: they are persisted by the state
# adapter (a shell concern the core never sees), so they belong with the other contract types
# rather than in the core, which `ports` must not depend on. Re-exported here because this is
# where they are used.
__all__ = [
    "CLASS_A",
    "CLASS_B",
    "DIMENSIONS",
    "INTENTLESS",
    "LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS",
    "LIDARR_METADATA_OUTAGE_RATIO",
    "PROBLEM_STATUSES",
    "REPORTED",
    "Fingerprint",
    "HealthBaseline",
    "HealthDelta",
    "HealthVerdict",
    "Notification",
    "Observation",
    "classify",
    "compare",
    "lidarr_metadata_outage",
    "next_baseline",
    "notification",
    "should_notify",
]


CLASS_A = ("unmapped", "catalogue_gaps", "absent_in_lidarr", "lidarr_metadata")
"""Mapping shortfalls. Summed into one threshold, because they have one consequence."""

CLASS_B = ("skipped_artists", "catalogue_too_large", "name_collisions")
"""Discrete faults. Any new identity degrades, and it keeps degrading until it clears."""

REPORTED = ("catalogue_gaps_recent",)
"""Counted and compared, but in neither class: never a regression, never a degrade reason.

A followed artist's brand-new album is a catalogue gap until Lidarr's metadata catches up. Its
intent - the follow - existed last run, so inside class A every ordinary new release would count
toward the jump, and a busy release week would report a fault for the system working exactly as
designed. It is not nothing, either: a gap that is still here after a refresh and an apply means
Lidarr's metadata proxy is not doing its job for that artist. So it is reported rather than
classified, and the operator reads the trend (`total - new`) instead of a status flip. A gap that
is never picked up ages out of the window on its own and becomes an ordinary class-A
`catalogue_gaps` identity, where the existing jump rule sees it."""

INTENTS = "intents"
"""Not a condition: the previous run's intent keys, kept so regressions can be told apart."""

INTENTLESS = ("lidarr_metadata",)
"""Class-A dimensions whose identities genuinely have no intent, and so always count as regressions.

A failed lookup belongs to a search term, not to an intent, and an outage is the only thing that
floods them. Everywhere else an empty intent means the caller could not work out which intent was
responsible, which is a defect in the caller rather than a fault in the library - so it is treated
conservatively and does *not* count. Alarming on it would turn a join bug into exactly the kind of
false signal this module exists to remove."""

DIMENSIONS = (*CLASS_A, *CLASS_B, *REPORTED)

_CONDITION_OF = {
    "skipped_artists": "new-skipped-artist",
    "catalogue_too_large": "new-catalogue-too-large",
    "name_collisions": "new-name-collision",
}

COMPARED = "compared"
FIRST_RUN = "first-run"


@dataclass(frozen=True, slots=True)
class Observation:
    """What one run saw, by identity.

    `identities` maps a dimension to `{identity: intent key}`, where the intent key is `""` for a
    condition that has no intent of its own. `observed` names the dimensions this run could see at
    all: a plan cannot see skipped artists, and "absent because unobserved" must never be read as
    "absent because fixed".
    """

    identities: Mapping[str, Mapping[str, str]]
    intents: frozenset[str] = frozenset()
    observed: frozenset[str] = frozenset()

    def of(self, dimension: str) -> Mapping[str, str]:
        return self.identities.get(dimension, {})

    def saw(self, dimension: str) -> bool:
        return dimension in self.observed


@dataclass(frozen=True, slots=True)
class HealthDelta:
    """This run against the baseline. Counts for the record, `regressions` for the threshold."""

    state: str
    """`compared`, or why a comparison was impossible: `first-run`, `<thing>-changed`."""
    totals: Mapping[str, int]
    new: Mapping[str, int]
    resolved: Mapping[str, int]
    regressions: int
    """Class-A identities that are new *and* whose intent existed last run. Rule 10's only input."""
    intents_total: int

    @property
    def comparable(self) -> bool:
        return self.state == COMPARED


@dataclass(frozen=True, slots=True)
class HealthVerdict:
    status: RunStatus
    conditions: tuple[str, ...]
    """Short codes naming what is newly wrong, in a stable order. Empty on an `ok` run."""


def compare(current: Observation, baseline: HealthBaseline | None, fingerprint: Fingerprint) -> HealthDelta:
    """Diff this run's identities against the baseline's.

    When there is no baseline, or the fingerprint moved, the counts are still reported but nothing
    is called new: the comparison would be meaningless and the run must not alarm for it.
    """
    state = FIRST_RUN if baseline is None else (baseline.fingerprint.mismatch(fingerprint) or COMPARED)
    comparable = state == COMPARED

    totals: dict[str, int] = {}
    new: dict[str, int] = {}
    resolved: dict[str, int] = {}
    regressions = 0
    last_intents = baseline.of(INTENTS) if baseline is not None else frozenset()

    for dimension in DIMENSIONS:
        was = baseline.of(dimension) if baseline is not None else frozenset()
        if not current.saw(dimension):
            # Unobserved: stand in with what the baseline holds, and claim no movement either way.
            totals[dimension] = len(was)
            new[dimension] = 0
            resolved[dimension] = 0
            continue
        now = current.of(dimension)
        totals[dimension] = len(now)
        appeared = {identity: intent for identity, intent in now.items() if identity not in was}
        new[dimension] = len(appeared) if comparable else 0
        resolved[dimension] = len(was - set(now)) if comparable else 0
        if comparable and dimension in CLASS_A:
            intentless = dimension in INTENTLESS
            regressions += sum(1 for intent in appeared.values() if intentless or intent in last_intents)

    return HealthDelta(
        state=state,
        totals=totals,
        new=new,
        resolved=resolved,
        regressions=regressions,
        intents_total=len(current.intents),
    )


LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS = 10
"""The fewest Lidarr metadata lookups a run must have actually attempted before a failure ratio
means anything.

A normal run attempts on the order of a hundred: well under class A's jump
threshold (`guards.unmapped_ratio_amber` x intents, which runs to the hundreds), so a total
`api.lidarr.audio` outage could not trip rule 9 - the exact gap this constant closes. A handful of
attempts proves nothing either way, so this floor keeps a quiet run (or one where every failing
term is already negative-cached) from reading as an outage on one unlucky lookup.
"""

LIDARR_METADATA_OUTAGE_RATIO = 0.5
"""The share of attempted Lidarr metadata lookups that must fail for this run to call it an
outage.

Deliberately not the class-A jump ratio (`guards.unmapped_ratio_amber`, tuned for a *regression*
against last run's intents): this is a same-run failure rate with no baseline to compare against,
so it needs its own, much coarser bar. Half is comfortably above the couple-of-terms-in-a-hundred
chronic rate a healthy run carries and comfortably below the ~100% a real
`api.lidarr.audio` outage produces.
"""


def lidarr_metadata_outage(attempts: int, failures: int) -> bool:
    """True when this run's own Lidarr metadata lookups look like an outage, not chronic noise.

    Class C: this-run only, no identity, no baseline - a real `api.lidarr.audio` outage must alarm
    on a first run same as any other. `attempts` counts only lookups genuinely asked of Lidarr this
    run; a term skipped because it is still inside its negative-cache TTL is not an
    attempt and must not count toward either number, or a library with many chronically-failing
    terms would look like an outage on a day when nothing changed.
    """
    return attempts >= LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS and failures > LIDARR_METADATA_OUTAGE_RATIO * attempts


def classify(
    delta: HealthDelta,
    *,
    spotify_schema_ok: bool,
    mb_ok: bool,
    lidarr_outage: bool = False,
    guarded: bool,
    ratio: float,
) -> HealthVerdict:
    """The status rules, in order. The first thing that matches decides.

    `guarded` outranks every degrade reason: exit 2 means unmonitors were refused, which is the
    operator's cue to check nothing was lost, and blunting it would cost more than the extra
    detail is worth. The reasons are still collected and reported either way.

    `lidarr_outage` is the caller's own answer from `lidarr_metadata_outage` (this module): a
    plain bool parameter, like `spotify_schema_ok` and `mb_ok`, so this stays a pure decision over
    already-computed facts rather than reaching for the raw attempt/failure counts itself.
    """
    conditions: list[str] = []
    if not spotify_schema_ok:
        conditions.append("spotify-schema")
    if not mb_ok:
        conditions.append("mb-outage")
    if lidarr_outage:
        conditions.append("lidarr-metadata-outage")
    if delta.comparable:
        conditions.extend(_CONDITION_OF[d] for d in CLASS_B if delta.new.get(d, 0))
        if delta.regressions > ratio * delta.intents_total:
            conditions.append("mapping-shortfall-jump")

    if guarded:
        return HealthVerdict(RunStatus.GUARDED, tuple(conditions))
    if conditions:
        return HealthVerdict(RunStatus.DEGRADED, tuple(conditions))
    return HealthVerdict(RunStatus.OK, ())


def next_baseline(
    current: Observation,
    baseline: HealthBaseline | None,
    fingerprint: Fingerprint,
    *,
    accept: bool,
) -> HealthBaseline:
    """What to persist after an apply.

    Class A and the reported-only dimensions are overwritten with what this run saw. A jump is a
    *rate* signal and is inherently a one-run event; making it sticky would rebuild the permanent
    `degraded` this module exists to remove. A reported-only dimension is overwritten for the
    plainer reason that its whole purpose is to say what is still here *now*.

    Class B is sticky: identities that went away are dropped, but new ones are **not** folded in,
    so a new fault keeps degrading every run until it clears or a human accepts it. Amber on this
    deployment carries no notification, so a fault that self-clears after one run is one nobody
    ever sees. A fault that heals on its own still clears on its own, because it is simply absent
    from the next run's observation.

    `accept` is `likearr run --accept-health`: fold everything in, class B included. A dimension
    this run could not observe always keeps whatever the baseline already held.
    """
    identities: dict[str, frozenset[str]] = {}
    for dimension in DIMENSIONS:
        was = baseline.of(dimension) if baseline is not None else frozenset()
        if not current.saw(dimension):
            identities[dimension] = was
            continue
        now = frozenset(current.of(dimension))
        if accept or dimension in CLASS_A or dimension in REPORTED:
            identities[dimension] = now
        else:
            identities[dimension] = was & now
    identities[INTENTS] = current.intents
    return HealthBaseline(fingerprint=fingerprint, identities=identities)


# ---------------------------------------------------------------------------- notifications

PROBLEM_STATUSES = frozenset({RunStatus.ERROR, RunStatus.GUARDED, RunStatus.DEGRADED, RunStatus.STALE})
"""The statuses a `notify = "problems"` webhook tells a person about. `paused` and `skipped` are
the schedule doing what it was told, and are never sent; `ok` is sent only as a recovery."""

_IDLE_STATUSES = frozenset({RunStatus.PAUSED, RunStatus.SKIPPED})


def should_notify(status: RunStatus, message: str, previous: tuple[RunStatus, str] | None) -> bool:
    """Whether this run is news to someone waiting on a notification.

    `previous` is the status and message of the last run the webhook could have been sent, skipping
    `paused` and `skipped` ticks, which say nothing about the library (`None` when there is none).

    A problem notifies when it differs from the previous run's status or message, so a guard that
    holds for days, or an outage that fails every run, is one post rather than four a day. `ok`
    notifies only as the recovery from a problem. `paused` and `skipped` never do.
    """
    if status in _IDLE_STATUSES:
        return False
    if status in PROBLEM_STATUSES:
        return previous != (status, message)
    return previous is not None and previous[0] in PROBLEM_STATUSES


@dataclass(frozen=True, slots=True)
class Notification:
    """The readable half of a webhook body. The field names are Apprise API's (`/notify/<key>`),
    which ntfy's templating can read as well."""

    title: str
    body: str
    type: str
    """`failure`, `warning`, `success` or `info`: the four types Apprise API accepts."""


_TITLE_AND_TYPE: dict[RunStatus, tuple[str, str]] = {
    RunStatus.ERROR: ("likearr: run failed", "failure"),
    RunStatus.STALE: ("likearr: plan is stale", "failure"),
    RunStatus.GUARDED: ("likearr: guard held back changes", "warning"),
    RunStatus.DEGRADED: ("likearr: new problems", "warning"),
    RunStatus.OK: ("likearr: run ok", "success"),
    RunStatus.PAUSED: ("likearr: scheduled runs are paused", "info"),
    RunStatus.SKIPPED: ("likearr: scheduled run skipped", "info"),
}

_RECOVERED_TITLE = "likearr: back to ok"


def notification(status: RunStatus, message: str, *, recovered: bool) -> Notification:
    """A title, body and type for a run. `recovered` marks an `ok` that clears a problem.

    The body is `message` exactly as the health record carries it - every place that builds one
    already passes upstream text through `adapters.http.redact` - or a fixed line when it is
    empty, as it is on a clean run. Nothing else is added: no Lidarr URL, no host, since a
    notification often goes to a public ntfy topic. The titles are fixed strings.
    """
    title, kind = _TITLE_AND_TYPE[status]
    if recovered and status is RunStatus.OK:
        title = _RECOVERED_TITLE
    body = message
    if not body.strip():
        body = (
            "The run finished with nothing newly wrong."
            if status is RunStatus.OK
            else f"The run ended with status {status}. The likearr Status page has the details."
        )
    return Notification(title=title, body=body, type=kind)


def source_set(counts: Iterable[str]) -> tuple[str, ...]:
    """The fingerprint's source component: `SourceSnapshot.counts` keys, sorted.

    Those keys already spell out which sources are enabled and which playlists are read, by id, so
    enabling a source and adding or removing a playlist both fall out of data the snapshot carries.
    """
    return tuple(sorted(counts))
