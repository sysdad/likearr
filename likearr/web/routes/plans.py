"""Check for changes and Apply changes: the plan pages' routes - starting a dry run, reviewing
its plan section by section, applying it, and "Not this one".

Split out of `likearr.web.app`; `create_app` mounts `ROUTES` where these routes always
stood in its list, and passes `AFTER` to `_Web` for the file count and names after a check.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from likearr.adapters.state_sqlite import LastPlan, SqliteState
from likearr.config import Config, ConfigError, is_mbid
from likearr.core.cron import next_fire
from likearr.core.explain import deniable, deny_note, release_label
from likearr.models import Diff, HealthRecord, ResolutionStatus
from likearr.shell.diff_io import diff_summary
from likearr.shell.last_run import facts_path
from likearr.web import settings as cfg
from likearr.web.context import AfterCallback, _Web, _web
from likearr.web.helpers import _form_pairs, _read_config, _read_plan
from likearr.web.jobs import JobMeta, JobRefused, JobState
from likearr.web.plans import (
    FILES_COUNTING,
    FILES_UNAVAILABLE,
    SECTIONS,
    PlanState,
    Whereabouts,
    applied_since,
    on_disk_labels,
    plan_state,
    plan_token_of_file,
    replaced_count,
    section_rows,
    select_rows,
    whereabouts,
)

log = logging.getLogger("likearr.web.app")
"""Under the app's own name, so log lines read as they did before the split."""


PLAN_HISTORY_ROWS = 200
"""Enough runs to know whether an apply has landed since any plan still worth showing (a week)."""


# ---------------------------------------------------------------- plans

_SHRINK_GUARDS = frozenset({"source-shrink", "artist-shrink"})


def _plan_jobs(web: _Web) -> list[JobMeta]:
    return [m for m in web.runner.jobs() if m.kind == "plan"]


def _records(config: Config) -> list[HealthRecord]:
    """The health records - no diffs parsed - or nothing when there is no state database yet
    (never create one). Enough to tell whether an apply has landed since a plan."""
    if not config.state_db.is_file():
        return []
    with SqliteState(config.state_db) as state:
        return state.runs(PLAN_HISTORY_ROWS)


def _last_plan(config: Config) -> LastPlan | None:
    if not config.state_db.is_file():
        return None
    with SqliteState(config.state_db) as state:
        return state.last_plan()


def _plan_page_context(web: _Web, config: Config, error: str = "") -> dict[str, Any]:
    now = web.now()
    last = _last_plan(config)
    shrinks = [g.message for g in last.guards if g.code in _SHRINK_GUARDS] if last is not None else []
    jobs = _plan_jobs(web)
    schedule = config.schedule.cron
    fire = next_fire(schedule, now, web.tz) if config.schedule.enabled else None
    records = _records(config)
    plans = []
    for meta in jobs[:10]:
        # Only the recorded settings are needed for the label, so the diff is not rebuilt: a plan
        # after a resolver bump holds thousands of rows.
        readable, fingerprint, resolver = _recorded_settings(web, meta.id)
        state = (
            plan_state(
                meta,
                fingerprint,
                config.plan_fingerprint,
                applied_since=_applied(records, meta),
                now=now,
                resolver_version=resolver,
            )
            if readable
            else PlanState(str(meta.state))
        )
        plans.append({"meta": meta, "state": state})
    return {
        "shrinks": shrinks,
        "shrink_known": last is not None,
        "shrinks_accepted": last is not None and last.accept_shrink,
        # No check ever recorded, so a check here would hit MusicBrainz cold at 1 request a
        # second for every song. Misses a user who wiped `mb_cache` or bumped the
        # resolver, which is rare; that trade is deliberate, not an oversight.
        "first_check": last is None,
        "next_fire": fire,
        "schedule": schedule,
        "plans": plans,
        "error": error,
    }


def _applied(records: Sequence[HealthRecord], meta: JobMeta) -> bool:
    return bool(meta.finished_at) and applied_since(records, meta.finished_at or "")


def _recorded_settings(web: _Web, job_id: str) -> tuple[bool, dict[str, Any] | None, int | None]:
    """(whether the plan's diff.json reads, the `config_fingerprint` it recorded, its resolver version)."""
    path = web.runner.diff_path(job_id)
    if path is None:
        return False, None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None, None
    if not isinstance(raw, dict):
        return False, None, None
    recorded = raw.get("config_fingerprint")
    resolver = raw.get("resolver_version")
    return True, recorded if isinstance(recorded, dict) else None, resolver if isinstance(resolver, int) else None


def plan_page(request: Request) -> Response:
    web = _web(request)
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "plan.html", {"config_error": str(exc)})
    return web.render(request, "plan.html", {**_plan_page_context(web, config), "config_error": ""})


async def plan_start(request: Request) -> Response:
    """Start a dry run: `likearr run --out <its job dir>/diff.json [--accept-shrink]`.

    It takes the run lock, so the lock is checked first: a scheduled run in progress is a "try
    again", never a child that fails on the lock.
    """
    web = _web(request)
    form = await request.form(max_files=0, max_fields=8)
    accept = form.get("accept_shrink") == "on"
    args = ["run", "--out", "{job_dir}/diff.json", *(["--accept-shrink"] if accept else [])]
    label = "check, letting shrinks through" if accept else "check"
    try:
        meta = web.runner.start("plan", args, label=label, needs_run_lock=True, expand_job_dir=True)
    except JobRefused as exc:
        try:
            context = {**_plan_page_context(web, web.config(), str(exc)), "config_error": ""}
        except ConfigError as config_exc:
            context = {"config_error": str(config_exc)}
        return web.render(request, "plan.html", context, 409)
    return RedirectResponse(f"/jobs/{meta.id}", status_code=303)


def _plan_or_none(web: _Web, job_id: str) -> tuple[JobMeta, Diff] | None:
    meta = web.runner.get(job_id)
    if meta is None or meta.kind != "plan":
        return None
    diff = _read_plan(web, job_id)
    return (meta, diff) if diff is not None else None


@dataclass(frozen=True, slots=True)
class _RowNames:
    """What a plan's rows are labelled with, gathered once per page."""

    artists: dict[str, str]
    playlists: dict[str, str]
    titles: dict[str, str]
    where: Whereabouts
    on_disk: dict[str, str] | None = None
    """Release group to "12 files on disk (stay where they are)", for the unmonitor rows."""
    counting: bool = False


# ---------------------------------------------------------------- after a check: files, then names


def count_files(web: _Web, plan: JobMeta) -> bool:
    """Start `likearr lidarr-files` for a finished check that unmonitors something: a child job
    that only reads Lidarr, never the server itself, and never the cron's `run`. Its answer is
    cached in its own job directory (``files.json``), tied to the plan by `plan_id`."""
    if plan.kind != "plan" or plan.state not in {JobState.DONE, JobState.GUARDED}:
        return False
    path = web.runner.diff_path(plan.id)
    diff = _read_plan(web, plan.id) if path is not None else None
    if path is None or diff is None or not diff.unmonitor:
        return False
    try:
        web.runner.start(
            "files",
            ["lidarr-files", "--plan", str(path), "--out", "{job_dir}/files.json"],
            label="Files on disk for the releases to unmonitor",
            expand_job_dir=True,
            plan_id=plan.id,
        )
    except JobRefused as exc:
        log.info("file counts for plan %s not started: %s", plan.id, exc)
        return False
    return True


def _after_plan(web: _Web, meta: JobMeta) -> None:
    if web.settings.auto_count_files and count_files(web, meta):
        return  # the names wait for the count: see `_after_files`
    _after_files(web, meta)


def _after_files(web: _Web, _meta: JobMeta) -> None:
    if web.settings.auto_fetch_names:
        web.fetch_names_if_needed()


FILES_START_WINDOW = timedelta(seconds=30)
"""How long after a check ends its file count may still be about to start."""


def _file_counts(web: _Web, plan_id: str, diff: Diff) -> tuple[dict[str, str] | None, bool]:
    """The unmonitor rows' "On disk" column from the plan's newest `lidarr-files` job, and whether
    that job is still counting. ``None``: no count was asked for (nothing to unmonitor, or a server
    that does not count), so no column."""
    for meta in web.runner.jobs():
        if meta.kind == "files" and meta.plan_id == plan_id:
            break
    else:
        # The check's own last poll can land between its end and the count's start: a moment
        # after the check, the count is still to come. Past that, nothing started it.
        plan = web.runner.get(plan_id)
        just_finished = (
            plan is not None
            and plan.finished_at
            and web.now() - datetime.fromisoformat(plan.finished_at) <= FILES_START_WINDOW
        )
        if web.settings.auto_count_files and diff.unmonitor and just_finished:
            return on_disk_labels(diff, FILES_COUNTING), True
        return None, False
    if not meta.finished:
        return on_disk_labels(diff, FILES_COUNTING), True
    path = web.runner.job_file(meta.id, "files.json") if meta.state is JobState.DONE else None
    try:
        answer = json.loads(path.read_text(encoding="utf-8")) if path is not None else None
    except (OSError, ValueError):
        answer = None
    return on_disk_labels(diff, answer if isinstance(answer, dict) else FILES_UNAVAILABLE), False


def _names(web: _Web, config: Config, diff: Diff, plan_id: str = "") -> _RowNames:
    """Artist names (the cached resolutions, then the diff's own), playlist names (the names file),
    release titles and "(type, year)" labels (the last run's facts, then the cached resolutions),
    and where each intent points now - so an unmonitor can say what happened to its reasons."""
    artists: dict[str, str] = {}
    titles: dict[str, str] = {}
    details: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
    if config.state_db.is_file():
        with SqliteState(config.state_db) as state:
            artists = state.artist_names()
            titles = state.release_titles()
            details = state.release_details()
    for mbid, name in [
        *((a.artist_mbid, a.name) for a in diff.add_artists),
        *((r.artist_mbid, r.name) for r in diff.ratchets),
    ]:
        if name:
            artists.setdefault(mbid, name)  # the diff's own names only fill gaps
    labels = {rg: release_label(*d) for rg, d in details.items() if d[0]}
    last = web.last_run(facts_path(config))
    desired_reasons: dict[str, str] = {}
    live: frozenset[str] | None = None
    unmatched: frozenset[str] = frozenset()
    if last is not None:
        for release in last.desired.releases.values():
            rg = release.release_group
            labels[rg.mbid] = release_label(
                rg.title,
                str(rg.primary_type or ""),
                [str(t) for t in sorted(rg.secondary_types)],
                rg.first_release_date.isoformat() if rg.first_release_date else "",
            )
            for reason in release.reasons:
                desired_reasons.setdefault(reason.key, rg.mbid)
        for by_rg in last.view.albums.values():
            for album in by_rg.values():
                labels.setdefault(
                    album.rg_mbid,
                    release_label(
                        album.title,
                        str(album.primary_type or ""),
                        [str(t) for t in sorted(album.secondary_types)],
                        album.release_date.isoformat() if album.release_date else "",
                    ),
                )
        snap = last.snapshot
        live = frozenset(i.reason.key for i in (*snap.tracks, *snap.albums, *snap.artists))
        unmatched = frozenset(
            k
            for k, r in [*last.resolutions.items(), *last.artist_resolutions.items()]
            if r.status is not ResolutionStatus.RESOLVED
        )
    where = whereabouts(diff, desired_reasons=desired_reasons, live=live, unmatched=unmatched, labels=labels)
    on_disk, counting = _file_counts(web, plan_id, diff) if plan_id else (None, False)
    return _RowNames(
        artists=artists,
        playlists=web.playlist_names().names,
        titles=titles,
        where=where,
        on_disk=on_disk,
        counting=counting,
    )


def _section_context(diff: Diff, name: str, names: _RowNames, job_id: str, query: str, page: int) -> dict[str, Any]:
    rows = section_rows(
        diff,
        name,
        artist_names=names.artists,
        playlist_names=names.playlists,
        release_titles=names.titles,
        where=names.where,
        on_disk=names.on_disk,
    )
    shown, matched, pages = select_rows(rows, query=query, page=page)
    return {
        "counting": names.counting and name == "unmonitor",
        "job_id": job_id,
        "section": SECTIONS[name],
        "rows": shown,
        "columns": [c for c in rows[0] if not c.startswith("_")] if rows else [],
        "total": len(rows),
        "matched": matched,
        "page": min(max(page, 1), pages),
        "pages": pages,
        "query": query,
    }


def plan_review(request: Request) -> Response:
    web = _web(request)
    job_id = request.path_params["job_id"]
    found = _plan_or_none(web, job_id)
    if found is None:
        return web.render(request, "missing.html", {}, status_code=404)
    meta, diff = found
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "plan_review.html", {"config_error": str(exc)}, 409)
    state = plan_state(
        meta,
        diff.config_fingerprint,
        config.plan_fingerprint,
        applied_since=_applied(_records(config), meta),
        now=web.now(),
        resolver_version=diff.resolver_version,
    )
    return web.render(
        request, "plan_review.html", {"review": _review_context(web, config, meta, diff, state), "config_error": ""}
    )


_CHANGE_KEYS = (
    "add_artists",
    "monitor",
    "unmonitor",
    "ratchets",
    "monitor_artists",
    "set_new_items_none",
    "refresh_artists",
)
"""The `diff_summary` counts that are changes to Lidarr."""


def _review_context(web: _Web, config: Config, meta: JobMeta, diff: Diff, state: PlanState) -> dict[str, Any]:
    """A plan's review, as `_plan_body.html` renders it: on the review page, and on the finished
    check's own job page, so there is no click-through between the two."""
    names = _names(web, config, diff, meta.id)
    summary = diff_summary(diff)
    return {
        "meta": meta,
        "state": state,
        "summary": summary,
        "changes": sum(summary[k] for k in _CHANGE_KEYS),
        "replaced": replaced_count(diff, names.where),
        "accept_shrink": diff.accept_shrink,
        "sections": [_section_context(diff, name, names, meta.id, "", 1) for name in SECTIONS],
        "diff_path": str(web.runner.diff_path(meta.id)),
    }


def _job_review(web: _Web, meta: JobMeta) -> dict[str, Any] | None:
    """The review of a finished check, for its job page; ``None`` for anything else, or when the
    plan or the config will not read (the page then links to the review, which says why)."""
    if meta.kind != "plan" or meta.state not in {JobState.DONE, JobState.GUARDED}:
        return None
    diff = _read_plan(web, meta.id)
    if diff is None:
        return None
    try:
        config = web.config()
    except ConfigError:
        return None
    return _review_context(web, config, meta, diff, _plan_state_now(web, config, meta, diff))


def _plan_state_now(web: _Web, config: Config, meta: JobMeta, diff: Diff) -> PlanState:
    return plan_state(
        meta,
        diff.config_fingerprint,
        config.plan_fingerprint,
        applied_since=_applied(_records(config), meta),
        now=web.now(),
        resolver_version=diff.resolver_version,
    )


def _apply_context(web: _Web, meta: JobMeta, diff: Diff, state: PlanState, error: str = "") -> dict[str, Any]:
    path = web.runner.diff_path(meta.id)
    return {
        "meta": meta,
        "state": state,
        "summary": diff_summary(diff),
        "changes": sum(diff_summary(diff)[k] for k in _CHANGE_KEYS),
        "guards": [g.message for g in diff.guards if g.blocked_unmonitors > 0],
        "command": f"likearr run --apply {path}",
        "accept_shrink": diff.accept_shrink,
        "error": error,
        "config_error": "",
    }


def plan_apply_page(request: Request) -> Response:
    """The confirm step: what applying this plan will do, with `--accept-health` and never `--force`."""
    web = _web(request)
    found = _plan_or_none(web, request.path_params["job_id"])
    if found is None:
        return web.render(request, "missing.html", {}, status_code=404)
    meta, diff = found
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "plan_apply.html", {"config_error": str(exc)}, 409)
    state = _plan_state_now(web, config, meta, diff)
    context = _apply_context(web, meta, diff, state)
    # From a name collision's "Accept as known": the box ticked and the reason spelled out.
    # Still a submit away: nothing is applied, and nothing accepted, without the button.
    context["accept_preset"] = request.query_params.get("accept_health") == "1" and bool(diff.name_collisions)
    return web.render(request, "plan_apply.html", context)


async def plan_apply(request: Request) -> Response:
    """Apply a reviewed plan: `likearr run --apply <its diff.json> [--accept-health]`.

    The browser carries only the job id and its token. The token is compared with the one recorded
    when the plan finished and with the file on disk now, so a replaced or edited diff is refused
    here. The real safety check is still `apply`'s own: it re-plans, compares digests and
    settings, and exits stale (3) if the world moved.
    """
    web = _web(request)
    form = await request.form(max_files=0, max_fields=8)
    job_id = request.path_params["job_id"]
    found = _plan_or_none(web, job_id)
    if found is None:
        return web.render(request, "missing.html", {}, status_code=404)
    meta, diff = found
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "plan_apply.html", {"config_error": str(exc)}, 409)
    state = _plan_state_now(web, config, meta, diff)
    if not state.reviewable:
        return web.render(request, "plan_apply.html", _apply_context(web, meta, diff, state), 409)
    path = web.runner.diff_path(job_id)
    given = form.get("plan_token")
    try:
        on_disk = plan_token_of_file(job_id, path) if path is not None else ""
    except (OSError, ValueError):
        on_disk = ""
    if not (isinstance(given, str) and meta.plan_token and given == meta.plan_token == on_disk):
        error = "These changes were edited on disk since you reviewed them, so nothing was applied. Check again."
        return web.render(request, "plan_apply.html", _apply_context(web, meta, diff, state, error), 409)
    args = ["run", "--apply", str(path), *(["--accept-health"] if form.get("accept_health") == "on" else [])]
    try:
        apply_job = web.runner.start(
            "apply",
            args,
            label=f"apply of the {meta.label or 'check'}",
            needs_run_lock=True,
            drain=True,
            plan_id=job_id,
        )
    except JobRefused as exc:
        return web.render(request, "plan_apply.html", _apply_context(web, meta, diff, state, str(exc)), 409)
    log.info("apply of plan %s started as job %s", job_id, apply_job.id)
    return RedirectResponse(f"/jobs/{apply_job.id}", status_code=303)


async def plan_deny(request: Request) -> Response:
    """ "Not this one": add a release group to `[rules] deny_releases`, through the settings confirm.

    The change goes through the same validated write as any settings save, and its second confirm
    (a refused release re-resolves the songs that landed on it). It never re-plans by itself:
    after the save the browser goes to /plan, and the old plan reads as superseded.
    """
    web = _web(request)
    form = await request.form(max_files=0, max_fields=4)
    release = str(form.get("release") or "").strip().lower()
    found = _plan_or_none(web, request.path_params["job_id"])
    if found is None:
        return web.render(request, "missing.html", {}, status_code=404)
    # Only a release this plan monitors: the button is on those rows and nowhere else.
    monitored = {m.key.rg_mbid: m for m in found[1].monitor}
    if not is_mbid(release) or release not in monitored:
        return PlainTextResponse("not a release this plan monitors", status_code=400)
    reasons = monitored[release].reasons
    if not deniable(reasons):
        # The button is not drawn on such a row; a stale page or a hand-made post still
        # must not write a refusal that changes nothing.
        request.session["flash"] = f"Not this one can't stop {release}: {deny_note(reasons)}"
        return RedirectResponse(f"/plan/{found[0].id}", status_code=303)
    return _deny(request, web, release)


def _deny(request: Request, web: _Web, release: str) -> Response:
    """Add `release` to `[rules] deny_releases` through the settings confirm; back to /plan after."""
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return PlainTextResponse(f"config.toml does not load: {exc}", status_code=409)
    values = cfg.current_values(config)
    current = values[("rules", "deny_releases")]
    denied = tuple(current) if isinstance(current, tuple | list) else ()
    if release in denied:
        request.session["flash"] = f"{release} is already refused."
        return RedirectResponse("/plan", status_code=303)
    values[("rules", "deny_releases")] = (*denied, release)
    check = cfg.plan_save(text.decode("utf-8"), values, base_dir=web.config_path.parent)
    if check.errors:
        return PlainTextResponse("; ".join(check.errors.values()), status_code=400)
    return web.render(
        request,
        "settings_confirm.html",
        {
            "reasons": check.confirm,
            "changes": cfg.describe_changes(check.changes, {}),
            "pairs": _form_pairs(values),
            "file_hash": cfg.file_hash(text),
            "confirm_digest": cfg.file_hash(check.new_text.encode("utf-8")),
            "next_url": "/plan",
        },
    )


def plan_section(request: Request) -> Response:
    web = _web(request)
    job_id, name = request.path_params["job_id"], request.path_params["name"]
    found = _plan_or_none(web, job_id)
    if found is None or name not in SECTIONS:
        return PlainTextResponse("no such plan section", status_code=404)
    _meta, diff = found
    try:
        config = web.config()
    except ConfigError as exc:
        return PlainTextResponse(f"config.toml does not load: {exc}", status_code=409)
    query = request.query_params.get("q", "")[:200]
    raw_page = request.query_params.get("page", "1")
    # isdigit() alone accepts "²", which int() refuses: a 500 for a hand-edited URL.
    page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) <= 6 else 1
    context = _section_context(diff, name, _names(web, config, diff, job_id), job_id, query, page)
    return web.render(request, "_section.html", context)


ROUTES: list[Route] = [
    Route("/plan", plan_page, methods=["GET"]),
    Route("/plan", plan_start, methods=["POST"]),
    Route("/plan/{job_id}", plan_review, methods=["GET"]),
    Route("/plan/{job_id}/section/{name}", plan_section, methods=["GET"]),
    Route("/plan/{job_id}/apply", plan_apply_page, methods=["GET"]),
    Route("/plan/{job_id}/apply", plan_apply, methods=["POST"]),
    Route("/plan/{job_id}/deny", plan_deny, methods=["POST"]),
]
"""In `create_app`'s order: Starlette matches the first route that fits."""

AFTER: dict[str, AfterCallback] = {
    # After a check: count the files behind its unmonitors, then (once that is done) fetch any
    # playlist names still missing. One job at a time, so each waits for the last.
    "plan": _after_plan,
    "files": _after_files,
}
"""This module's after-callbacks by job kind, which `create_app` passes to `_Web`."""
