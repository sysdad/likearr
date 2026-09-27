"""Plans in the web UI: when a saved dry run may still be applied, and the diff in plain language.

A plan is a `likearr run` dry-run job whose `diff.json` lives in its job directory. The browser
never carries the plan; it names a job. What this module decides:

- **Lifecycle.** A finished plan is *reviewable* until the world it was planned against moves on.
  It is *superseded* when an apply has landed since (any apply - a cron run's included), or when
  `[rules]`/`[guards]` no longer match the configuration it recorded (the same comparison `apply`
  makes since #27, so a settings save supersedes it). It *expires* after `EXPIRE_AFTER`
  unreviewed. Both are computed when read, from facts that already exist, rather than stored: no
  hook to forget, and a hand edit of `config.toml` supersedes exactly like a browser save. The
  digest check in `apply` stays the real backstop; this just says so before the last step.
- **Sections.** Each part of the diff as rows whose columns read as sentences - why a release is
  wanted, how the resolver found it, which reasons it lost - never field names.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from likearr.core.diff import config_changes
from likearr.core.explain import deniable, deny_note, describe_step
from likearr.models import RESOLVER_VERSION, Diff, HealthRecord, Profile, Reason, ReasonKind, Resolution, RunStatus
from likearr.playlist_names import playlist_url
from likearr.web.jobs import JobMeta, JobState

__all__ = [
    "APPLIED",
    "EXPIRE_AFTER",
    "PAGE_SIZE",
    "SECTIONS",
    "PlanState",
    "Section",
    "applied_since",
    "describe_reasons",
    "describe_step",
    "plan_state",
    "plan_token",
    "plan_token_of_file",
    "run_change_sections",
    "section_rows",
    "select_rows",
]

EXPIRE_AFTER = timedelta(days=7)
PAGE_SIZE = 50

APPLIED = frozenset({RunStatus.OK, RunStatus.GUARDED, RunStatus.DEGRADED})
"""Statuses of an apply that ran. A stale, skipped or failed one changed nothing."""

_REVIEWABLE_JOBS = frozenset({JobState.DONE, JobState.GUARDED})
"""A dry run that planned: exit 0, or exit 2 with guards holding some unmonitors back."""


@dataclass(frozen=True, slots=True)
class PlanState:
    name: str
    """``reviewable``, ``superseded``, ``expired``, or the job's own state when it never planned."""
    why: str = ""

    @property
    def reviewable(self) -> bool:
        return self.name == "reviewable"


_CHANGE_COUNTS = ("monitored", "unmonitored", "added", "new_items_none")
"""What an apply's health record says it changed, artists set to "Monitor New Albums: None"
included (#172): an apply that did only that moved Lidarr from under the plan too. Ratchets and
re-monitored artists are not in the record; an apply that did only those (rare: they come with
monitors) leaves the plan reviewable, and `apply`'s digest check still refuses it if Lidarr moved."""


def plan_token(job_id: str, raw: Mapping[str, Any], content_sha256: str = "") -> str:
    """The integrity token of a plan: what its `diff.json` binds it to.

    ``sha256(job_id | sha256 of the file's bytes | source_digest | lidarr_digest |
    resolver_version | accept_shrink | config_fingerprint)``, recorded when the plan job finishes
    and checked again, against the file on disk, when it is applied. The file's own digest covers
    its contents - a monitor cut, a guard deleted, an artist added after review is a different
    plan; the named fields say what it was planned against. An integrity check, not the safety
    one: `apply` still re-plans and compares its digests and settings with the world as it is.
    """
    parts = [
        job_id,
        content_sha256,
        str(raw.get("source_digest", "")),
        str(raw.get("lidarr_digest", "")),
        str(raw.get("resolver_version", "")),
        json.dumps(bool(raw.get("accept_shrink", False))),
        json.dumps(raw.get("config_fingerprint"), sort_keys=True),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def plan_token_of_file(job_id: str, path: Path) -> str:
    """`plan_token` of the `diff.json` at `path`. Raises OSError or ValueError if it will not read."""
    content = path.read_bytes()
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object")
    return plan_token(job_id, raw, hashlib.sha256(content).hexdigest())


def applied_since(records: Sequence[HealthRecord], finished_at: str) -> bool:
    """Whether an apply that changed something (a cron run's included) was recorded after
    `finished_at`. A cron apply with nothing to do leaves Lidarr as the plan saw it, so it must not
    kill the plan - every six hours it would otherwise. An apply that failed part-way changed
    Lidarr too (#54), so it counts."""
    after = datetime.fromisoformat(finished_at).timestamp()
    return any(
        not r.dry_run
        and (r.status in APPLIED or bool(r.changes_made))
        and r.ts > after
        and any(r.counts.get(k, 0) > 0 for k in _CHANGE_COUNTS)
        for r in records
    )


def plan_state(
    meta: JobMeta,
    recorded: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    applied_since: bool,
    now: datetime,
    resolver_version: int | None = None,
) -> PlanState:
    """Where a plan job stands now.

    Args:
        recorded: the `config_fingerprint` its diff recorded.
        current: `Config.plan_fingerprint` now.
        applied_since: an apply has been recorded since the plan finished.
        resolver_version: the one the plan was made with; ``None`` when not known. An older one is
            superseded, as `apply` refuses it.
    """
    if meta.state not in _REVIEWABLE_JOBS or not meta.finished_at:
        return PlanState(str(meta.state))
    if applied_since:
        return PlanState("superseded", "Changes were applied since this plan was made, so Lidarr has moved on from it.")
    if resolver_version is not None and resolver_version != RESOLVER_VERSION:
        return PlanState("superseded", "This check was made by an older likearr; check again.")
    changed = config_changes(recorded, current)
    if changed is None:  # `apply` refuses such a diff (#27), so it is no plan to offer
        return PlanState("superseded", "This plan does not record the settings it was made under.")
    if changed:
        return PlanState("superseded", f"The settings changed since this plan was made: {', '.join(changed)}.")
    if now - datetime.fromisoformat(meta.finished_at) > EXPIRE_AFTER:
        return PlanState("expired", "This plan is more than a week old.")
    return PlanState("reviewable")


# ---------------------------------------------------------------- plain language

_PROFILES = {Profile.LEAN: "Lean: studio albums and EPs", Profile.FULL: "Full: every release type"}


NAME_LOADING = "a playlist (loading names…)"
"""A playlist whose name the names file does not hold yet. Never its raw id: a check fetches the
names right after it finishes (`likearr.web.context`), so this reads as what it is - on its way."""


def _unnamed_playlist_link(reasons: frozenset[Reason] | set[Reason], playlist_names: Mapping[str, str]) -> str | None:
    """The one playlist in `reasons` with no known name, linked on Spotify; ``None`` if not exactly one."""
    unnamed = {r.playlist_id for r in reasons if r.kind is ReasonKind.PLAYLIST and r.playlist_id not in playlist_names}
    if len(unnamed) != 1:
        return None
    (pid,) = unnamed
    return playlist_url(pid or "")


def describe_reasons(reasons: frozenset[Reason] | set[Reason], playlist_names: Mapping[str, str]) -> str:
    """Why a release is wanted: "you follow the artist; 2 liked songs; a song in Road trip"."""
    kinds = {kind: [r for r in reasons if r.kind is kind] for kind in ReasonKind}
    parts: list[str] = []
    if kinds[ReasonKind.FOLLOWED]:
        parts.append("you follow the artist")
    if kinds[ReasonKind.SAVED]:
        parts.append("you saved the album")
    liked = len(kinds[ReasonKind.LIKED])
    if liked:
        parts.append("a liked song" if liked == 1 else f"{liked} liked songs")
    playlists: dict[str, int] = {}
    for r in kinds[ReasonKind.PLAYLIST]:
        name = playlist_names.get(r.playlist_id or "") or NAME_LOADING
        playlists[name] = playlists.get(name, 0) + 1
    for name, count in sorted(playlists.items()):
        parts.append(f"a song in {name}" if count == 1 else f"{count} songs in {name}")
    if kinds[ReasonKind.PENDING_ALBUM]:
        parts.append("a liked single waiting for its album")
    if kinds[ReasonKind.MANUAL]:
        parts.append("kept by hand when likearr adopted the library")
    return "; ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# ---------------------------------------------------------------- where a lost reason went


FILES_COUNTING = "counting files..."
FILES_UNAVAILABLE = "file count unavailable"


def on_disk_labels(diff: Diff, answer: Mapping[str, Any] | str) -> dict[str, str]:
    """Each unmonitor's "On disk" cell from a `likearr lidarr-files` answer, or one word for all
    (`FILES_COUNTING`, `FILES_UNAVAILABLE`). Unmonitoring never touches files, and says so."""
    if isinstance(answer, str):
        return {u.key.rg_mbid: answer for u in diff.unmonitor}
    albums = answer.get("albums")
    albums = albums if isinstance(albums, Mapping) else {}
    missing = answer.get("missing")
    missing = set(missing) if isinstance(missing, list) else set()
    out: dict[str, str] = {}
    for u in diff.unmonitor:
        rg = u.key.rg_mbid
        album = albums.get(rg)
        count = album.get("track_files") if isinstance(album, Mapping) else None
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            out[rg] = f"{count} file{'' if count == 1 else 's'} on disk (stay where they are)"
        elif isinstance(count, int) and not isinstance(count, bool):
            out[rg] = "no files"
        elif rg in missing:
            out[rg] = "not in Lidarr any more"
        else:
            out[rg] = FILES_UNAVAILABLE
    return out


@dataclass(frozen=True, slots=True)
class Whereabouts:
    """Where each intent (a reason key) points now, as far as the server can tell - so a release
    that loses a reason says what happened to it, not just which reason it lost."""

    in_plan: dict[str, str] = field(default_factory=dict)
    """reason key -> release group of a monitor row in this plan: the intent moved there."""
    wanted: dict[str, str] = field(default_factory=dict)
    """reason key -> release group already wanted or monitored, outside this plan's monitors."""
    unmatched: frozenset[str] = frozenset()
    live: frozenset[str] | None = None
    """reason keys still on Spotify; ``None`` when the server does not know."""
    labels: dict[str, str] = field(default_factory=dict)
    """release group -> "Title (type, year)"."""

    def label(self, rg_mbid: str, title: str) -> str:
        return self.labels.get(rg_mbid, title)


def whereabouts(
    diff: Diff,
    *,
    desired_reasons: Mapping[str, str] | None = None,
    live: frozenset[str] | None = None,
    unmatched: frozenset[str] = frozenset(),
    labels: Mapping[str, str] | None = None,
) -> Whereabouts:
    """Put together what the plan itself says (its monitors, its reason updates, its unmatched)
    with what the last run recorded (`desired_reasons`: reason key -> the release group it wants;
    `live`: every reason key on Spotify). Followed-artist reasons are left out of the moves: one
    follow backs a whole catalogue, so it never "moved" to one release."""
    in_plan = {r.key: m.key.rg_mbid for m in diff.monitor for r in m.reasons if r.kind is not ReasonKind.FOLLOWED}
    wanted = {
        r.key: key.rg_mbid for key, reasons in diff.update_reasons for r in reasons if r.kind is not ReasonKind.FOLLOWED
    }
    for reason_key, rg in (desired_reasons or {}).items():
        if not reason_key.startswith(f"{ReasonKind.FOLLOWED}:"):
            wanted.setdefault(reason_key, rg)
    gone = {r.intent_key for r in (*diff.unmapped, *diff.pending)} | set(unmatched)
    return Whereabouts(in_plan=in_plan, wanted=wanted, unmatched=frozenset(gone), live=live, labels=dict(labels or {}))


def _subject(reason: Reason, playlist_names: Mapping[str, str]) -> str:
    if reason.kind is ReasonKind.SAVED:
        return "your saved album"
    if reason.kind is ReasonKind.LIKED:
        return "your liked song"
    if reason.kind is ReasonKind.PLAYLIST:
        name = playlist_names.get(reason.playlist_id or "")
        return f'the song in "{name}"' if name else f"the song in {NAME_LOADING}"
    return "it"


def _gone(reason: Reason, playlist_names: Mapping[str, str]) -> str:
    if reason.kind is ReasonKind.SAVED:
        return "you no longer have this album saved"
    if reason.kind is ReasonKind.LIKED:
        return "you no longer have this song liked"
    if reason.kind is ReasonKind.PLAYLIST:
        name = playlist_names.get(reason.playlist_id or "")
        return f'the song is no longer in "{name}"' if name else f"the song is no longer in {NAME_LOADING}"
    if reason.kind is ReasonKind.FOLLOWED:
        return "you no longer follow the artist"
    return "nothing on Spotify asks for it any more"


def why_no_longer_needed(
    lost: frozenset[Reason], where: Whereabouts, playlist_names: Mapping[str, str], *, titles: Mapping[str, str]
) -> str:
    """Why a release is no longer needed, one clause per reason it lost, in plain text: the label
    ("Tease Me (album, 1992)") names where it moved. `titles`: this plan's monitor rows."""
    clauses: list[str] = []
    for reason in sorted(lost, key=lambda r: r.key):
        if reason.kind is ReasonKind.FOLLOWED:
            live = where.live is None or reason.key in where.live
            clauses.append(
                "it no longer counts among the studio albums and EPs of an artist you follow, or you refused it"
                if live
                else "you no longer follow the artist"
            )
        elif reason.key in where.in_plan:
            rg = where.in_plan[reason.key]
            clauses.append(
                f"{_subject(reason, playlist_names)} now matches {where.label(rg, titles.get(rg, rg))} instead"
            )
        elif reason.key in where.wanted:
            rg = where.wanted[reason.key]
            clauses.append(
                f"{_subject(reason, playlist_names)} now matches {where.label(rg, rg)} instead, which is already wanted"
            )
        elif reason.key in where.unmatched:
            clauses.append(f"likearr can no longer match {_subject(reason, playlist_names)} (see Missing)")
        elif where.live is not None and reason.key not in where.live:
            clauses.append(_gone(reason, playlist_names))
        else:
            clauses.append(f"{_subject(reason, playlist_names)} no longer points here")
    text = "; ".join(dict.fromkeys(clauses))
    return text[:1].upper() + text[1:]


def replaced_count(diff: Diff, where: Whereabouts) -> int:
    """Unmonitors whose every lost reason now points at another release (in this plan, or already
    wanted): the same thing you like, matched better - not something you stopped liking."""
    count = 0
    for u in diff.unmonitor:
        keys = [r.key for r in u.lost_reasons if r.kind is not ReasonKind.FOLLOWED]
        if keys and all(k in where.in_plan or k in where.wanted for k in keys):
            count += 1
    return count


# ---------------------------------------------------------------- sections


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    title: str
    lead: str
    """One sentence saying what the rows are."""


SECTIONS: dict[str, Section] = {
    s.name: s
    for s in (
        Section("add_artists", "Artists to add", "Added to Lidarr, with the metadata profile each will get."),
        Section("monitor", "Releases to monitor", "Newly wanted, and why."),
        Section("unmonitor", "Releases to unmonitor", "No longer backed by anything on Spotify."),
        Section("ratchets", "Profiles to widen", "Artists moved to a profile that shows more release types."),
        Section(
            "monitor_artists", "Artists to re-monitor", "Held unmonitored in Lidarr though a wanted release is theirs."
        ),
        Section(
            "set_new_items_none",
            "Artists to stop auto-monitoring",
            'Lidarr\'s "Monitor New Albums" set to None, so their new albums are monitored only when you like '
            "them. Only artists likearr holds a release of.",
        ),
        Section("refresh_artists", "Artists to refresh", "A recent release Lidarr has not caught up with yet."),
        Section("update_reasons", "Why-it's-wanted updates", "Still monitored, for a different set of reasons."),
        Section("guards", "Guards", "What the safety checks held back, and why."),
        Section("name_collisions", "Name collisions", "Wanted artists skipped because Lidarr holds the name."),
        Section("pending", "Waiting", "Liked singles waiting for their album."),
        Section("unmapped", "Not found", "Songs, albums and artists that could not be matched."),
    )
}


def _artist(mbid: str, names: Mapping[str, str]) -> str:
    return names.get(mbid, mbid)


def _unresolved(item: Any) -> dict[str, str]:
    what = item.intent_key
    why = getattr(item, "detail", "") or str(getattr(item, "status", ""))
    return {"What": what, "Why": why}


def section_rows(
    diff: Diff,
    name: str,
    *,
    artist_names: Mapping[str, str],
    playlist_names: Mapping[str, str],
    release_titles: Mapping[str, str] | None = None,
    where: Whereabouts | None = None,
    on_disk: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """One section of `diff` as rows of plain-language columns, in the diff's own order.

    `on_disk` (`on_disk_labels`) adds the unmonitor rows' "On disk" column; ``None``, no column.

    A key starting with ``_`` is not a column. ``_href:<Column>`` makes that column's cell a link;
    ``_release`` carries what a monitor row's "Not this one" needs, and is set only where the
    button would work (`deniable`, issue #153); ``_deny_note`` says where the choice lives instead.
    """
    if name == "add_artists":
        return [{"Artist": a.name or a.artist_mbid, "Profile": _PROFILES[a.profile]} for a in diff.add_artists]
    where = where or Whereabouts()
    if name == "monitor":
        rows = []
        for m in diff.monitor:
            row = {
                "Release": where.label(m.key.rg_mbid, m.title),
                "Artist": _artist(m.key.artist_mbid, artist_names),
                "Wanted because": describe_reasons(m.reasons, playlist_names),
                "Found by": describe_step(m.step),
                "_id": f"monitor-{m.key.rg_mbid}",
            }
            if deniable(m.reasons):
                row["_release"] = m.key.rg_mbid
            else:
                row["_deny_note"] = deny_note(m.reasons)
            link = _unnamed_playlist_link(m.reasons, playlist_names)
            if link:
                row["_href:Wanted because"] = link
            rows.append(row)
        return rows
    if name == "unmonitor":
        titles = {m.key.rg_mbid: m.title for m in diff.monitor}
        rows = []
        for u in diff.unmonitor:
            row = {
                "Release": where.label(u.key.rg_mbid, u.title),
                "Artist": _artist(u.key.artist_mbid, artist_names),
                "Why it's no longer needed": why_no_longer_needed(u.lost_reasons, where, playlist_names, titles=titles),
            }
            if on_disk is not None:
                row["On disk"] = on_disk.get(u.key.rg_mbid, FILES_UNAVAILABLE)
            rows.append(row)
        return rows
    if name == "ratchets":
        new_items = set(diff.set_new_items_none)
        return [
            {
                "Artist": r.name or r.artist_mbid,
                "To": _PROFILES[r.to_profile],
                "Why": _with_widen_note(r.because) if r.artist_mbid in new_items else r.because,
            }
            for r in diff.ratchets
        ]
    if name == "monitor_artists":
        return [{"Artist": _artist(mbid, artist_names)} for mbid in diff.monitor_artists]
    if name == "set_new_items_none":
        return [{"Artist": _artist(mbid, artist_names)} for mbid in diff.set_new_items_none]
    if name == "refresh_artists":
        return [{"Artist": _artist(mbid, artist_names)} for mbid in diff.refresh_artists]
    if name == "update_reasons":
        return [
            {
                **_release_cell(key.rg_mbid, release_titles or {}),
                "Artist": _artist(key.artist_mbid, artist_names),
                "Now wanted because": describe_reasons(reasons, playlist_names),
                **(
                    {"_href:Now wanted because": link}
                    if (link := _unnamed_playlist_link(reasons, playlist_names))
                    else {}
                ),
            }
            for key, reasons in diff.update_reasons
        ]
    if name == "guards":
        # A name collision's guard holds nothing back and its own section says it in full.
        return [
            {"Guard": g.message, "Held back": _count(g.blocked_unmonitors, "unmonitor")}
            for g in diff.guards
            if not (g.code == "name-collision" and g.blocked_unmonitors == 0)
        ]
    if name == "name_collisions":
        return [
            {
                "Name": c.name,
                "Wanted": c.wanted_disambiguation or c.wanted_mbid,
                "In Lidarr": (
                    f"{c.existing_disambiguation or c.existing_mbid} (Lidarr id {c.existing_lidarr_id})"
                    if c.in_lidarr
                    else f"Not yet: {c.existing_disambiguation or c.existing_mbid}, also wanted and skipped"
                ),
                "Cost": _count(c.dropped_releases, "release"),
            }
            for c in diff.name_collisions
        ]
    if name == "pending":
        return [_pending(p) for p in diff.pending]
    if name == "unmapped":
        return [_unresolved(u) for u in diff.unmapped]
    raise KeyError(name)


_WIDEN_NOTE = (
    '"Monitor New Albums" is set to None first, so the release types this shows are not monitored; '
    "albums already monitored stay monitored."
)
"""What a widening does to an artist whose "Monitor New Albums" the same plan sets to None (#172)."""


def _with_widen_note(because: str) -> str:
    return f"{because.rstrip('.')}. {_WIDEN_NOTE}" if because else _WIDEN_NOTE


def _release_cell(rg_mbid: str, titles: Mapping[str, str]) -> dict[str, str]:
    """A release by its title from the resolution cache, else its MBID linked to MusicBrainz."""
    title = titles.get(rg_mbid)
    if title:
        return {"Release": title}
    return {"Release": rg_mbid, "_href:Release": f"https://musicbrainz.org/release-group/{rg_mbid}"}


def _pending(p: Resolution) -> dict[str, str]:
    since = p.single_release_date.isoformat() if p.single_release_date else "unknown"
    title = p.single_release_group.title if p.single_release_group else p.intent_key
    return {"Single": title, "Released": since}


RUN_CHANGE_SECTIONS = (
    "add_artists",
    "monitor",
    "unmonitor",
    "ratchets",
    "monitor_artists",
    "set_new_items_none",
    "refresh_artists",
)
"""What an applied run's "What changed" shows (#76): every section that can hold a named change,
in `SECTIONS`' own order. `update_reasons` is left out - state only, no Lidarr change - and so are
`guards`, `name_collisions`, `pending` and `unmapped`, which the run's own headline and the
collision cards already say what they need to."""


def run_change_sections(
    diff: Diff,
    *,
    artist_names: Mapping[str, str],
    playlist_names: Mapping[str, str],
    release_titles: Mapping[str, str] | None = None,
    where: Whereabouts | None = None,
    cap: int | None = None,
) -> list[dict[str, Any]]:
    """`RUN_CHANGE_SECTIONS`, rendered through `section_rows` - the same rows and the same wording
    the plan review page uses for them - with empty sections left out and (`cap`) long ones capped.

    Returns one dict per non-empty section: ``section`` (its `Section`), ``rows`` (at most `cap` of
    them, or all of them when `cap` is falsy), ``total`` (how many there really are), ``columns``,
    and ``capped`` (whether `rows` is fewer than `total`) - what the Status page's inline "What
    changed" and `/runs/<id>` (#76) both render, the second with no `cap`.
    """
    out: list[dict[str, Any]] = []
    for name in RUN_CHANGE_SECTIONS:
        rows = section_rows(
            diff,
            name,
            artist_names=artist_names,
            playlist_names=playlist_names,
            release_titles=release_titles,
            where=where,
        )
        if not rows:
            continue
        shown = rows[:cap] if cap else rows
        out.append(
            {
                "section": SECTIONS[name],
                "rows": shown,
                "columns": [c for c in rows[0] if not c.startswith("_")],
                "total": len(rows),
                "capped": len(shown) < len(rows),
            }
        )
    return out


def select_rows(rows: Sequence[dict[str, str]], *, query: str, page: int) -> tuple[list[dict[str, str]], int, int]:
    """The rows matching `query` (any column, case-insensitive), then one page of them.

    Returns:
        (the page's rows, how many matched, how many pages). An out-of-range page is clamped.
    """
    needle = query.strip().casefold()
    matched = [
        r for r in rows if not needle or any(needle in v.casefold() for k, v in r.items() if not k.startswith("_"))
    ]
    pages = max(1, math.ceil(len(matched) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    start = (page - 1) * PAGE_SIZE
    return matched[start : start + PAGE_SIZE], len(matched), pages
