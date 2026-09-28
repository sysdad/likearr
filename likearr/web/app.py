"""`likearr start`: the Starlette app. Routes, page assembly and the middleware stack.

Everything that decides anything lives in a module of its own and is tested without a server:
`auth` (who may in), `jobs` (spawning the CLI), `status` (the prose), `settings` (the config
round trip). This module wires them to URLs.

Two rules hold for every route:

- **No GET changes anything.** Every mutation - login, logout, a settings save, starting or
  cancelling a job, even fetching the playlist list - is a POST, and every POST passes the
  cross-origin check before it reaches a route.
- **The server reads; the children work.** It opens the state database per request, read-only
  in practice, and never builds a `Context`: that would construct HTTP clients and a
  `SpotifyAuth` that reads the token file, and no page needs either. Anything that talks to
  Spotify, Lidarr or MusicBrainz is a `likearr` child process, which is also what keeps every
  Spotify token refresh under the token file's lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from likearr.adapters.spotify import (
    lacks_collaborative,
    read_authorized_at,
    read_granted_scopes,
    reauth_due,
)
from likearr.adapters.state_sqlite import RunRow, SqliteState
from likearr.config import ALLOWED_HOSTS_ENV, Config, ConfigError, is_mbid, load_config
from likearr.core.cron import next_fire
from likearr.core.explain import STATUSES, deniable, deny_note, more_note
from likearr.models import PHASE_MARKER_APPLY, PROGRESS_MARKER_POST_RESOLVE, Diff, HealthRecord, RunStatus
from likearr.shell.diff_io import DiffFileError, diff_from_run_dict
from likearr.shell.last_run import LastRun, explain_from_last_run, facts_path
from likearr.web import unmatched as unmatched_view
from likearr.web.auth import (
    GENERATION_KEY,
    SESSION_KEY,
    AllowedHostMiddleware,
    ASGIApp,
    AuthGateMiddleware,
    BodyLimitMiddleware,
    CrossOriginMiddleware,
    SecureCookieMiddleware,
    SecurityHeadersMiddleware,
    password_matches,
)
from likearr.web.context import (
    _HERE,
    POLL_STOP,
    WebSettings,
    _Web,
    _web,
    oom_note,
)
from likearr.web.helpers import _first_applied, _read_plan
from likearr.web.jobs import JobMeta, JobRefused, JobState, last_json_object
from likearr.web.plans import (
    run_change_sections,
)
from likearr.web.routes import cleanup as cleanup_routes
from likearr.web.routes import plans as plans_routes
from likearr.web.routes import settings as settings_routes
from likearr.web.routes.cleanup import _prune_or_none
from likearr.web.routes.plans import _deny, _job_review, _last_plan, _names, _plan_or_none, _plan_state_now
from likearr.web.schedule import SCHEDULED_KIND, Scheduler, assert_single_worker, fire_now
from likearr.web.status import (
    CollisionCard,
    ago,
    build_status,
    coverage,
    describe_run,
    first_run_checklist,
    health_glance,
    lost_state_sentence,
    reauth_banner_note,
    reauth_view,
)

__all__ = ["WebSettings", "create_app"]

log = logging.getLogger(__name__)


LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
"""Always allowed on top of `LIKEARR_ALLOWED_HOSTS`, so the container healthcheck on 127.0.0.1 keeps
working whatever the list says. Safe to allow: a rebinding page sends its own name as the host,
never a loopback literal. No ``::1``: `AllowedHostMiddleware` compares only what precedes the first
":" of the Host header, so an IPv6 literal can never match."""


HISTORY_ROWS = 20
RUN_CHANGES_CAP = 10
"""Rows per section on Status's inline "What changed" before it says "show all" and points
at the run's own `/runs/<id>` page, which shows every row."""

MAX_QUERY = 200


EXPLAIN_LIMIT = 40
"""At most this many answers per Look up in the server; a broader query is told to narrow down."""

_STATE_TEXT = {
    JobState.RUNNING: "Running.",
    JobState.DONE: "Finished.",
    JobState.GUARDED: "Finished, and guards held some changes back.",
    JobState.STALE: "Nothing was applied: Spotify or Lidarr changed since the check. Check again.",
    JobState.BUSY: "Another likearr command held the run lock, so nothing ran. Try again when it finishes.",
    JobState.FAILED: "Failed. The log below says why.",
    JobState.CANCELLED: "Cancelled.",
    JobState.INTERRUPTED: "Interrupted: likearr stopped or restarted while this was running.",
    JobState.SKIPPED: "Skipped: nothing ran. The log below says why.",
}

_ADOPTED_TEXT = "Still running from before likearr restarted. It can't be stopped from here; it ends on its own."


# ---------------------------------------------------------------- health and login


def healthz(request: Request) -> Response:
    """For the container healthcheck: the config loads and, once the state database exists, it
    opens and its last run reads.

    A missing database is never created here (`SqliteState` would create an empty one), but it
    reads healthy - `ok (no runs yet)` - as long as its directory exists and is writable: that is
    a fresh install with no run yet, which the image's own healthcheck must pass. A missing or
    read-only directory stays unhealthy, since that is what catches a wrong `[state] db` path or a
    bad mount before any run gets the chance to write there. Once the file exists, it must open
    and its last run must read.
    """
    web = _web(request)
    try:
        config = web.config()
        if not config.state_db.exists():
            parent = config.state_db.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                log.warning("healthcheck: no state database and its directory is not writable at %s", parent)
                return PlainTextResponse("unhealthy: no state database", status_code=503)
            return PlainTextResponse("ok (no runs yet)")
        with SqliteState(config.state_db) as state:
            state.last_run()
    except Exception:
        log.warning("healthcheck failed", exc_info=True)
        return PlainTextResponse("unhealthy", status_code=503)
    return PlainTextResponse("ok")


_ICON_SVG_PATH = _HERE / "static" / "icon.svg"


def favicon_ico(request: Request) -> Response:
    """Browsers still ask `GET /favicon.ico` directly, ignoring the `<link rel="icon">` in
    `base.html`'s head. This answers it with the same mark rather than leaving it a
    404 (logged in) or, worse, a 303 to `/login` (logged out - `/favicon.ico` is also in
    `auth._OPEN_PATHS` for that reason)."""
    return Response(_ICON_SVG_PATH.read_bytes(), media_type="image/svg+xml")


def login_form(request: Request) -> Response:
    return _web(request).render(request, "login.html", {"error": ""})


async def login(request: Request) -> Response:
    web = _web(request)
    address = request.client.host if request.client else "unknown"
    # Only a urlencoded form, parsed with tight limits: this route is reachable without a session,
    # so it must not spool a multipart upload or read a thousand fields for a stranger.
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return PlainTextResponse("send the login form", status_code=415)
    form = await request.form(max_files=0, max_fields=4, max_part_size=4096)
    # The pause is checked after the body has arrived, and nothing below awaits: the check, the
    # comparison and the recording of a failure are one step, so requests already in flight
    # cannot all pass the check before the first of them is counted.
    wait = web.limiter.blocked_for(address)
    if wait > 0:
        error = f"Too many attempts. Try again in {int(wait) + 1} seconds."
        return web.render(request, "login.html", {"error": error}, status_code=429)
    given = form.get("password")
    if isinstance(given, str) and password_matches(given, web.settings.password):
        web.limiter.succeeded(address)
        request.session.clear()
        request.session[SESSION_KEY] = True
        request.session[GENERATION_KEY] = web.generation
        return RedirectResponse("/", status_code=303)
    web.limiter.failed(address)
    log.warning("failed login from %s", address)
    return web.render(request, "login.html", {"error": "That is not the password."}, status_code=401)


async def logout(request: Request) -> Response:
    """End the session, and every copy of its cookie: a signed cookie cannot be revoked, so the
    generation it names is. One user, one password: logging out logs out every browser."""
    _web(request).end_sessions()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- status


_RESOLVER_CHANGE_NOTE = (
    "likearr's matching changed since the run before this one, so some of these changes may come from "
    "that, not from Spotify."
)
"""Above the list, when the record's baseline says the resolver version changed: a plain
answer to "did my Spotify change, or did likearr?", distinct from the terse `_BASELINE_NOTES` used
elsewhere on the page."""


def _resolver_note(record: HealthRecord) -> str:
    return _RESOLVER_CHANGE_NOTE if record.baseline == "resolver-version-changed" else ""


def _part_way_note(record: HealthRecord, diff: Diff) -> str:
    """Above the list, when the list below is not what actually happened: a stale
    refusal made no changes at all, a guarded run held back every unmonitor by design, or (the
    original case) an apply stopped part-way with no way to say which of its planned changes
    actually reached Lidarr, only how many. In every case the list is labelled as the plan the
    run was attempting rather than what it did.

    `RunStatus.GUARDED` - not `diff.guarded` - decides the guard branch: `diff.guards` reflects
    the plan, and a plan that was guarded can still crash and land as `RunStatus.ERROR`, whose own
    branch below already covers "not everything in it reached Lidarr" for that combination.
    """
    if record.dry_run:
        return ""
    if record.status is RunStatus.STALE:
        return (
            "This run made no changes: Spotify or Lidarr changed since its check, so it was refused. The "
            "list below is what it would have applied."
        )
    if record.status is RunStatus.GUARDED:
        blocked = sum(g.blocked_unmonitors for g in diff.guards)
        return (
            f'A guard held back {blocked} unmonitor{"" if blocked == 1 else "s"} this run: the "Releases to '
            'unmonitor" list below is the plan it was attempting, not what changed in Lidarr.'
        )
    if record.status is not RunStatus.ERROR:
        return ""
    if record.changes_made and record.changes_planned is not None and record.changes_made >= record.changes_planned:
        # Every planned change reached Lidarr; only confirming the last batch failed.
        return (
            f"This run made all {record.changes_planned} of its planned changes, but confirming them with "
            "Lidarr failed. The list below is what it applied."
        )
    if record.changes_made:
        planned = f" of {record.changes_planned}" if record.changes_planned is not None else ""
        return (
            f"This run stopped part-way: {record.changes_made}{planned} changes made. The list below is the "
            "plan it was attempting; not all of it reached Lidarr."
        )
    if record.lidarr_changed:
        return (
            "This run stopped part-way before making any of its planned changes, though Lidarr may have "
            "changed some other way. The list below is the plan it was attempting."
        )
    return ""


def _run_changes_from_found(
    web: _Web, config: Config, found: tuple[RunRow, dict[str, Any] | None], *, cap: int | None
) -> dict[str, Any] | None:
    """What `_run_changes.html` renders for one already-fetched run row: its named sections
    (`run_change_sections`, reusing the plan review page's own row rendering), and the two notes
    the issue asks for.

    ``None`` when the run stored no diff (a run that never planned - a config-stale refusal, or a
    failure before the check ran): there is nothing to show by name. A refusal because the *plan*
    went stale (the world moved since it was made) still stores its unexecuted diff, same as a
    guarded run's blocked unmonitors - `_part_way_note` is what tells those apart from a run that
    actually changed Lidarr.
    """
    row, diff_raw = found
    if diff_raw is None:
        return None
    try:
        diff = diff_from_run_dict(diff_raw)
    except DiffFileError:
        return None
    names = _names(web, config, diff)
    sections = run_change_sections(
        diff,
        artist_names=names.artists,
        playlist_names=names.playlists,
        release_titles=names.titles,
        where=names.where,
        cap=cap,
    )
    return {
        "run_id": row.id,
        "record": row.record,
        "when": datetime.fromtimestamp(row.record.ts, tz=UTC),
        "sections": sections,
        "any_rows": bool(sections),
        "resolver_note": _resolver_note(row.record),
        "part_way_note": _part_way_note(row.record, diff),
    }


def _run_changes(web: _Web, config: Config, run_id: int, *, cap: int | None) -> dict[str, Any] | None:
    """`_run_changes_from_found`, opening its own state database read and looking `run_id` up
    first: `None` when there is no run with that id, in addition to the "no diff" case above.
    """
    if not config.state_db.is_file():
        return None
    with SqliteState(config.state_db) as state:
        found = state.run_by_id(run_id)
    if found is None:
        return None
    return _run_changes_from_found(web, config, found, cap=cap)


_RUN_LINKED_KINDS = frozenset({"apply", SCHEDULED_KIND})
"""Job kinds that change Lidarr and so may have a run to link to."""


def _job_run_id(state: SqliteState, meta: JobMeta) -> int | None:
    """The run id job history links a finished apply-like job to: the one applied run published
    during that job's lifetime (see `SqliteState.run_id_in_job`); `None` for any other job, or
    when that isn't exactly one run."""
    if meta.kind not in _RUN_LINKED_KINDS or not meta.finished_at or not meta.started_at:
        return None
    started = datetime.fromisoformat(meta.started_at).timestamp()
    finished = datetime.fromisoformat(meta.finished_at).timestamp()
    return state.run_id_in_job(started, finished)


def _run_job_link(state: SqliteState, jobs: Sequence[JobMeta], run_id: int) -> JobMeta | None:
    """The kept apply or scheduled job whose window contains `run_id`: the reverse
    of `_job_run_id`, so the run page can link to a job's log when the job is still kept. At most
    one job's window ever contains a given run - the job runner runs one job at a time - so the
    first match is returned."""
    for meta in jobs:
        if _job_run_id(state, meta) == run_id:
            return meta
    return None


def status(request: Request) -> Response:
    web = _web(request)
    now = web.now()
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "status.html", {"config_error": str(exc)})
    state_missing = not config.state_db.is_file()
    jobs = web.runner.jobs()
    rows = []
    published = None
    last_fire_at = None
    first_applied = False
    job_run_ids: dict[str, int] = {}
    if not state_missing:  # never create it: see healthz
        with SqliteState(config.state_db) as state:
            rows = state.run_history(HISTORY_ROWS)
            published = state.last_published_run()
            last_fire_at = state.last_scheduled_fire()
            first_applied = state.first_apply_at() is not None
            job_run_ids = {m.id: rid for m in jobs[:5] if (rid := _job_run_id(state, m)) is not None}
    names = web.playlist_names().names
    view = build_status(rows, now=now, tz=web.tz, playlist_names=names, lidarr_url=config.ui.lidarr_url)
    authorized_at = read_authorized_at(config.spotify.token_file)
    reauth = reauth_view(authorized_at, reauth_due(authorized_at) if authorized_at else None, now=now)
    granted_scopes = read_granted_scopes(config.spotify.token_file)
    # Same test `spotify_status` uses for Settings' "has_token": true once a token file with
    # either field is on disk, so a token missing only `authorized_at` still counts as connected.
    has_token = authorized_at is not None or granted_scopes is not None
    reauth_note = reauth_banner_note(reauth, has_token=has_token)
    checklist = first_run_checklist(has_token=has_token, published=published is not None)
    schedule = config.schedule.cron
    last = web.last_run(facts_path(config))
    # Paused stops the false "next run" the Status page used to show while cron was commented out
    # by hand: with the schedule off, there is no next fire to report -
    # the scheduler still fires on schedule underneath, but every fire while
    # paused is a child that does nothing and says so, so there is nothing useful to count down to.
    fire = next_fire(schedule, now, web.tz) if config.schedule.enabled else None
    # The time comes from SqliteState, never from the job store: the job store only keeps the
    # newest KEEP_JOBS (20), so an old fire's job directory can be pruned while its time is still
    # the most recent one recorded. The job (when it still exists) adds the result and a link.
    last_fire_job = next((m for m in jobs if m.kind == SCHEDULED_KIND), None)
    last_fire_reason = ""
    if last_fire_job is not None and last_fire_job.state is JobState.SKIPPED:
        last_fire_reason = web.runner.log_tail(last_fire_job.id, lines=1).strip()
    run_changes = (
        _run_changes(web, config, view.last_applied.run_id, cap=RUN_CHANGES_CAP)
        if view.last_applied is not None and view.last_applied.run_id
        else None
    )
    return web.render(
        request,
        "status.html",
        {
            "view": view,
            "run_changes": run_changes,
            "job_run_ids": job_run_ids,
            "collision_actions": _collision_actions(web, config, view.collisions, last) if view.collisions else {},
            "glance": health_glance(published, now=now, tz=web.tz, collisions_shown=bool(view.collisions)),
            "checklist": checklist,
            "coverage": coverage(last) if last is not None else None,
            "reauth": reauth,
            "reauth_note": reauth_note,
            "has_token": has_token,
            "granted_scopes": sorted(granted_scopes) if granted_scopes else [],
            # A token from before playlist-read-collaborative keeps working; only a
            # playlist you collaborate on needs the re-auth, so this is a note, never a problem.
            "needs_collaborative": lacks_collaborative(granted_scopes),
            "schedule": schedule,
            "next_fire": fire,
            "next_in": ago(now, fire) if fire is not None else "",
            "schedule_paused": not config.schedule.enabled,
            "first_applied": first_applied,
            "paused_at_local": config.schedule.paused_at.astimezone(web.tz) if config.schedule.paused_at else None,
            "paused_reason": config.schedule.paused_reason,
            "last_fire_at": last_fire_at.astimezone(web.tz) if last_fire_at else None,
            "last_fire_job": last_fire_job,
            "last_fire_failed": last_fire_job is not None and last_fire_job.state is JobState.FAILED,
            "last_fire_reason": last_fire_reason,
            "recent_jobs": jobs[:5],
            "ui_errors": config.ui.errors,
            "setup_needed": config.lidarr.setup_needed,
            "state_missing": state_missing,
            "lost_state": lost_state_sentence(view.tagged_without_state, config.lidarr.tag),
            "config_error": "",
        },
    )


async def run_now(request: Request) -> Response:
    """Status page's Run now: the same `scheduled` job, the same lock, the same queue - the *arr
    "Run now" pattern. `fire_now` itself returns at once - the job shows up in
    job history, whether it starts straight away, waits behind another job, or - past the queue
    wait - is recorded skipped - but it does its own config read and sqlite write synchronously,
    so it runs off the event loop, the same as `job_cancel` does for stopping a child.

    While `[schedule] enabled` is false, the button is not on the page, but a stale
    tab or a direct POST can still reach here: load the config fresh and refuse before firing, so
    a press never writes a skipped fire into "Last scheduled fire". A config that fails to load is
    not this route's problem to report - fall through to the old behaviour, the same as a fire from
    the scheduler loop would."""
    web = _web(request)
    try:
        config = web.config()
    except ConfigError:
        config = None
    if config is not None and not config.schedule.enabled:
        request.session["flash"] = "Scheduled runs are paused - resume them in Settings."
        return RedirectResponse("/", status_code=303)
    # The same refusal for the first-apply gate: the button is disabled until then, and the
    # child it would start publishes `paused` anyway, but that would still record a fire.
    if config is not None and not await anyio.to_thread.run_sync(lambda: _first_applied(config)):
        request.session["flash"] = (
            "Scheduled runs start after your first reviewed apply - review changes and apply them first."
        )
        return RedirectResponse("/", status_code=303)
    await anyio.to_thread.run_sync(
        lambda: fire_now(web.runner, web.config_path, now=web.now, label="Scheduled run (Run now)")
    )
    request.session["flash"] = "Scheduled run started - or queued behind whatever is running now."
    return RedirectResponse("/", status_code=303)


_MAX_RUN_ID_DIGITS = 9
"""More than enough for the `runs` table's `AUTOINCREMENT` id; bounds a hand-edited URL's digit run
before `int()` sees it, the same guard `plan_section`'s `page` param uses."""


def run_page(request: Request) -> Response:
    """`/runs/<id>`: every run's own page, not only ones with a diff - a failed or
    config-stale run stores none. The run record itself (status, message, guards) always shows;
    "What changed" only when there is a diff to show it from, and a link to the run's job log
    when a kept apply or scheduled job's window still contains it."""
    web = _web(request)
    raw = request.path_params["run_id"]
    if not (raw.isascii() and raw.isdigit() and len(raw) <= _MAX_RUN_ID_DIGITS and int(raw) > 0):
        return web.render(request, "missing.html", {"what": "run"}, status_code=404)
    run_id = int(raw)
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "run.html", {"config_error": str(exc)}, 409)
    if not config.state_db.is_file():
        return web.render(request, "missing.html", {"what": "run"}, status_code=404)
    with SqliteState(config.state_db) as state:
        found = state.run_by_id(run_id)
        if found is None:
            return web.render(request, "missing.html", {"what": "run"}, status_code=404)
        row, _diff_raw = found
        run = describe_run(row, now=web.now(), tz=web.tz)
        run_changes = _run_changes_from_found(web, config, found, cap=None)
        job = _run_job_link(state, web.runner.jobs(), run_id)
    return web.render(
        request,
        "run.html",
        {"run": run, "run_changes": run_changes, "job": job, "config_error": ""},
    )


def _collision_actions(
    web: _Web, config: Config, cards: Sequence[CollisionCard], last: LastRun | None
) -> dict[str, dict[str, Any]]:
    """What each collision card can do, by the wanted artist's MBID:

    - ``accept``: the apply confirm of the newest check that is still reviewable and reports this
      collision, with `--accept-health` ticked. Empty when there is none: "Check again" makes one.
    - ``releases``: what the last run wanted of the skipped artist, ``(release group, title)``,
      each with "Not this one" - the Look up deny flow, which checks the last run wanted it. Only a
      release "Not this one" can stop (`deniable`): one a saved album wants is left out.
    """
    newest: tuple[JobMeta, Diff] | None = None
    for meta in web.runner.jobs():
        found = _plan_or_none(web, meta.id) if meta.kind == "plan" else None
        if found is not None:
            if _plan_state_now(web, config, found[0], found[1]).reviewable:
                newest = found
            break  # only the newest check can be applied: an older one is superseded by it
    in_plan = {c.wanted_mbid for c in newest[1].name_collisions} if newest is not None else set()
    out: dict[str, dict[str, Any]] = {}
    for card in cards:
        wanted = card.wanted_mbid
        if not wanted:
            continue
        releases = sorted(
            (
                (key.rg_mbid, release.release_group.title)
                for key, release in (last.desired.releases.items() if last is not None else ())
                if key.artist_mbid == wanted and is_mbid(key.rg_mbid) and deniable(release.reasons)
            ),
            key=lambda pair: pair[1].casefold(),
        )
        accept = f"/plan/{newest[0].id}/apply?accept_health=1" if newest is not None and wanted in in_plan else ""
        out[wanted] = {"accept": accept, "releases": releases}
    return out


def _unmatched_rows(request: Request) -> tuple[_Web, dict[str, Any], list[unmatched_view.Row]]:
    """The page's context so far, and every row of the last run. It reads the last-run file and
    the playlist-name cache, nothing else: no Spotify, MusicBrainz or Lidarr call."""
    web = _web(request)
    try:
        config = web.config()
    except ConfigError as exc:
        return web, {"config_error": str(exc), "last": None}, []
    last = web.last_run(facts_path(config))
    context: dict[str, Any] = {"config_error": "", "last": last, "lidarr_url": config.ui.lidarr_url}
    if last is None:
        return web, context, []
    rows = unmatched_view.build_rows(
        last, playlist_names=web.playlist_names().names, recent_release_days=config.rules.recent_release_days
    )
    return web, context, rows


def _unmatched_sections(rows: list[unmatched_view.Row], params: Mapping[str, str]) -> dict[str, Any]:
    filters = unmatched_view.filters_from(params, rows)
    cards = unmatched_view.cards(rows)
    return {
        "filters": filters,
        "sections": unmatched_view.sections(rows, filters),
        "any_rows": bool(rows),
        # Each section heading says its card's numbers, in its card's words.
        "cards": {c.group.code: c for c in cards},
        "card_list": cards,
    }


def unmatched(request: Request) -> Response:
    """Everything the last run could not match to a release: cards per group, then each group's
    reasons, one row per release, filtered and paged. Status links here."""
    web, context, rows = _unmatched_rows(request)
    if context["last"] is not None:
        context.update(
            _unmatched_sections(rows, request.query_params),
            reason_options=unmatched_view.reason_options(rows),
            source_options=unmatched_view.source_options(rows),
            sorts=unmatched_view.SORTS,
            coverage=coverage(context["last"]),
        )
    return web.render(request, "unmatched.html", context)


def unmatched_rows(request: Request) -> Response:
    """Every section again, for a change of the filter bar."""
    web, context, rows = _unmatched_rows(request)
    if context["last"] is None:
        return PlainTextResponse("no run recorded yet", status_code=404)
    context.update(_unmatched_sections(rows, request.query_params))
    return web.render(request, "_unmatched_sections.html", context)


def unmatched_part(request: Request) -> Response:
    """One reason's rows, for its pager."""
    web, context, rows = _unmatched_rows(request)
    params = request.query_params
    raw_page = params.get("page", "1")
    page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) <= 6 else 1
    found = (
        unmatched_view.part(
            rows, unmatched_view.filters_from(params, rows), params.get("group", ""), params.get("reason", ""), page
        )
        if context["last"] is not None
        else None
    )
    if found is None:
        return PlainTextResponse("no such part", status_code=404)
    context["part"] = found
    return web.render(request, "_unmatched_part.html", context)


# ---------------------------------------------------------------- jobs


def _elapsed(meta: JobMeta, now: datetime) -> str:
    start = datetime.fromisoformat(meta.started_at)
    end = datetime.fromisoformat(meta.finished_at) if meta.finished_at else now
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


def _phase(log_text: str) -> str:
    """Coarse progress from the log markers the CLI already writes."""
    if PHASE_MARKER_APPLY in log_text:
        return "Applying changes to Lidarr"
    if PROGRESS_MARKER_POST_RESOLVE in log_text:
        return "Reading Lidarr and building the plan"
    if "sources read:" in log_text:
        return "Spotify read; resolving against MusicBrainz and reading Lidarr"
    return "Reading Spotify"


def _progress_line(log_text: str) -> str:
    """The newest ``progress:`` line the child logged, timestamp and logger name stripped off
    - shown under the phase while a run is in progress. The fixed ``progress:``
    prefix (`shell.run`'s resolve and add-loop progress lines both use it) is what lets this be a
    simple scan rather than a format-specific parse."""
    for line in reversed(log_text.splitlines()):
        idx = line.find("progress:")
        if idx != -1:
            return line[idx:].strip()
    return ""


def _explain_report(output: str) -> dict[str, Any] | None:
    """The report `likearr explain --json` prints, as cards; ``None`` when the output holds none,
    which the job page then shows as plain output like any other job's."""
    data = last_json_object(output, lambda d: isinstance(d.get("summary"), list))
    return _lookup_view(data) if data is not None else None


_LIDARR_PATH = re.compile(r"/(artist|album)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

LOOKUP_STATUS = {
    "downloaded": ("Downloaded", "ok"),
    "waiting": ("Waiting for download", ""),
    "monitored": ("Monitored", "ok"),
    "not-monitored": ("Not monitored yet", ""),
    "not-in-lidarr": ("Not in Lidarr yet", ""),
    "skipped": ("Skipped (name collision)", "warn"),
    "unmatched": ("Couldn't match", "warn"),
    "ambiguous": ("Couldn't tell which artist", "warn"),
    "failed": ("Lookup failed", "warn"),
    "pending": ("Waiting for its album", ""),
    "excluded": ("Left out by your settings", ""),
    "no-longer-wanted": ("No longer wanted", ""),
    "kept-by-hand": ("Kept by hand", ""),
}
"""A card's status pill: label and tone, by `core.explain.STATUSES`."""
assert set(LOOKUP_STATUS) == set(STATUSES)


def _lookup_view(data: Mapping[str, Any]) -> dict[str, Any]:
    """A Look up report as the page renders it, from the last run or a live check's output alike.

    Everything that lands in an href is checked here: links are https only, a Lidarr path is an
    artist or album MBID path, and "Not this one" gets only an MBID. A card with no headline leads
    with its text.
    """
    cards: list[dict[str, Any]] = []
    left_out = data.get("left_out")
    left_out = left_out if isinstance(left_out, int) and left_out > 0 else 0
    for item in data.get("summary", []):
        if not isinstance(item, dict):
            continue
        if len(cards) == EXPLAIN_LIMIT:
            left_out += 1
            continue
        status = str(item.get("status", ""))
        facts = item.get("facts")
        lidarr_path = str(item.get("lidarr_path", ""))
        release = str(item.get("release", "")).lower()
        label, tone = LOOKUP_STATUS.get(status, ("", ""))
        cards.append(
            {
                "headline": str(item.get("headline") or item.get("text", "")),
                "wrong_match": bool(item.get("wrong_match")),
                "status": label,
                "tone": tone,
                "facts": [
                    (str(f[0]), str(f[1]))
                    for f in (facts if isinstance(facts, list) else [])
                    if isinstance(f, list | tuple) and len(f) == 2
                ],
                "links": [
                    {"label": str(link.get("label", "")), "url": str(link.get("url", ""))}
                    for link in item.get("links", [])
                    if isinstance(link, dict) and str(link.get("url", "")).startswith("https://")
                ],
                "detail": str(item.get("detail", "")),
                "lidarr_path": lidarr_path if _LIDARR_PATH.fullmatch(lidarr_path) else "",
                "release": release if is_mbid(release) else "",
            }
        )
    return {
        "summary": cards,
        "details": str(data.get("details", "")),
        "left_out": left_out,
        "more_note": more_note(left_out),
    }


def _plan_accepted_shrinks(web: _Web, plan_id: str) -> bool:
    """Whether the plan an apply job applied was made with ``--accept-shrink``."""
    diff = _read_plan(web, plan_id) if plan_id and web.runner.get(plan_id) is not None else None
    return diff is not None and diff.accept_shrink


def _run_message(output: str) -> str:
    """The `message` of the health record a `likearr run` printed as its last JSON line."""
    data = last_json_object(output, lambda d: "status" in d)
    return str(data.get("message") or "") if data is not None else ""


_STALE_FALLBACK = "Spotify, Lidarr or the settings changed since this plan was made."
"""A stale apply's message when its health record was not printed (`[health] stdout = false`)."""


_RUN_RECORD_KEYS = {"ts", "resolver_version", "exit_code", "status"}
"""A subset of `HealthRecord.to_dict()`'s keys (`likearr/models.py`), stable regardless of field
order - `_split_run_record` matches on these being present, never on the JSON's literal text, so
reordering or adding a field to `HealthRecord` can't silently break the split."""


def _split_run_record(output: str) -> tuple[str, str]:
    """Split the health record's raw JSON line off a finished job's stdout (`adapters.health`'s
    stdout sink writes it last).

    It is data for `likearr status`/Home Assistant, not something to read under the "likearr
    applied:" summary, so it moves into the Technical log instead. Only the last non-blank line,
    when it parses as JSON and carries the run record's keys, is taken - so a job with no health
    record (or one that never printed it) is unaffected, and trailing blank lines after the record
    don't hide it.
    """
    lines = output.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return output, ""
    try:
        data = json.loads(lines[idx])
    except ValueError:
        return output, ""
    if isinstance(data, dict) and data.keys() >= _RUN_RECORD_KEYS:
        return "\n".join(lines[:idx]), lines[idx]
    return output, ""


_RUN_MESSAGE_KINDS = frozenset({"plan", "apply", SCHEDULED_KIND})
"""The kinds whose health record `message` (when there is one) is the failure reason - the same
field `stale_message` reads. Anything else falls to `_ERROR_LOG_LINE`/`_FAIL_OUTPUT_LINE`."""

_ERROR_LOG_LINE = re.compile(r"^\S+\s+ERROR\s+\S+:\s*(.+)$")
"""A `logging_setup.setup_logging` line - `<iso timestamp> ERROR <logger name>: <message>` -
captured with its prefix stripped."""

_FAIL_OUTPUT_LINE = re.compile(r"^FAIL\s+(.+)$")
"""A command's own `emit(f"FAIL  {exc}")` line (`shell/commands.py`), prefix stripped."""

_CONFIG_ERROR_LINE = re.compile(r"^(config error:.+)$")
"""`shell/cli.py`'s own top-level `except ConfigError` line - printed to stdout before any
command-specific code runs, for every subcommand, with no run record and no ERROR/FAIL line to
match instead. Kept whole (unlike the other two patterns) since `_failure_remedy` matches on this
same "config error:" prefix to choose its remedy."""


def _last_match(text: str, pattern: re.Pattern[str]) -> str:
    """The stripped capture of the last line in `text` that `pattern` matches whole, or ``""``."""
    for line in reversed(text.splitlines()):
        found = pattern.match(line)
        if found:
            return found[1].strip()
    return ""


def _failure_reason(meta: JobMeta, run_record: str, raw_output: str, tail: str, output: str) -> str:
    """A failed or skipped job's headline reason, for the job page above the Technical log:
    empty when nothing said why, so the fallback text ("...says why.") stays true.

    A plan, apply or scheduled run's own health record `message` first (`stale_message` reads the
    same field); otherwise - or when that came up empty, run record or not - the last line the
    child logged at `ERROR`, the last `FAIL` line it printed, or the `config error:` line the CLI
    itself prints when `config.toml` won't load, whichever a cause is found in first.
    """
    if meta.state not in (JobState.FAILED, JobState.SKIPPED):
        return ""
    reason = _run_message(run_record or raw_output) if meta.kind in _RUN_MESSAGE_KINDS else ""
    return (
        reason
        or _last_match(tail, _ERROR_LOG_LINE)
        or _last_match(output, _FAIL_OUTPUT_LINE)
        or _last_match(output, _CONFIG_ERROR_LINE)
    )


def _run_record_flag(run_record: str, key: str) -> bool | None:
    """A boolean flag from a finished job's run record line (already isolated by
    `_split_run_record`), or ``None`` when there is no record or the flag isn't a bool there."""
    if not run_record:
        return None
    try:
        data = json.loads(run_record)
    except ValueError:
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


_NO_TOKEN_FILE = "no token file"
"""What the Spotify adapter's message names (`shell/run.py`) when there is no `spotify-token.json`
yet - the day-one failure. Caught by text as well as `spotify_ok`,
since the "other kinds" branch of `_failure_reason` never has a run record to read a flag from."""


def _failure_remedy(reason: str, *, spotify_ok: bool | None, lidarr_ok: bool | None) -> tuple[str, str, str]:
    """A failed or skipped job's fix, in UI terms: (lead-in text, link label, link target), or
    `("", "", "")` when no known cause matches - the Technical log is still the only answer.

    A pure function so every mapping is a one-line unit test. The
    run record's flags decide it where there is one; the message text otherwise, since `likearr
    auth` (what the CLI itself names) has no page of its own - the browser path to the same fix is
    Settings' Connect Spotify button, or Doctor.
    """
    if spotify_ok is False or _NO_TOKEN_FILE in reason:
        return "Connect Spotify in", "Settings", "/settings"
    if lidarr_ok is False:
        return "Check the Lidarr address and key, then run the", "Doctor", "/settings#doctor"
    if reason.startswith("config error:"):  # `shell/cli.py`'s own top-level `except ConfigError`
        return "Fix the settings in", "Settings", "/settings"
    return "", "", ""


_STATE_TEXT_REASON_SHOWN = {
    JobState.FAILED: "Failed.",
    JobState.SKIPPED: "Skipped: nothing ran.",
}
"""`_STATE_TEXT`'s line for a job whose `failure_reason` is already shown: shortened so the
page never says "the log below says why" above a reason it is already showing."""


def _state_text(meta: JobMeta, failure_reason: str) -> str:
    if failure_reason:
        return _STATE_TEXT_REASON_SHOWN.get(meta.state, _STATE_TEXT[meta.state])
    return _STATE_TEXT[meta.state]


def _job_context(web: _Web, meta: JobMeta) -> dict[str, Any]:
    tail = web.runner.log_tail(meta.id)
    raw_output = web.runner.shown_output(meta.id) if meta.finished else ""
    output, run_record = _split_run_record(raw_output)
    report = _explain_report(output) if meta.kind == "explain" and meta.state is JobState.DONE else None
    stale = meta.state is JobState.STALE
    failure_reason = _failure_reason(meta, run_record, raw_output, tail, output)
    remedy_text, remedy_label, remedy_url = _failure_remedy(
        failure_reason,
        spotify_ok=_run_record_flag(run_record, "spotify_ok"),
        lidarr_ok=_run_record_flag(run_record, "lidarr_ok"),
    )
    return {
        # A stale apply is a message with a way forward, not an error: the run says what moved,
        # and "Check again" keeps the plan's own shrink choice. `run_record` is
        # the same JSON line this would otherwise search `raw_output` for again; fall back to the
        # full output only when `_split_run_record` found no such line to hand it directly.
        "stale_message": (_run_message(run_record or raw_output) or _STALE_FALLBACK) if stale else "",
        "replan_accept_shrink": stale and _plan_accepted_shrinks(web, meta.plan_id),
        "meta": meta,
        "state_text": oom_note(meta)
        or (_ADOPTED_TEXT if meta.adopted and not meta.finished else _state_text(meta, failure_reason)),
        "failure_reason": failure_reason,
        "remedy_text": remedy_text,
        "remedy_label": remedy_label,
        "remedy_url": remedy_url,
        "elapsed": _elapsed(meta, web.now()),
        "phase": _phase(tail) if meta.state is JobState.RUNNING else "",
        "progress": _progress_line(tail) if meta.state is JobState.RUNNING else "",
        "tail": tail,
        "output": output,
        "run_record": run_record,
        "report": report,
        "lidarr_url": _lidarr_url(web) if report else "",
        "review": _job_review(web, meta) if meta.finished else None,
        "stopping": web.runner.stopping(meta.id),
        "run_id": _job_run_id_for(web, meta) if meta.finished else None,
        "first_check": meta.kind == "plan" and not meta.finished and _first_check_pending(web),
    }


def _lidarr_url(web: _Web) -> str:
    try:
        return web.config().ui.lidarr_url
    except ConfigError:
        return ""


def _first_check_pending(web: _Web) -> bool:
    """Whether no check has ever been recorded - the empty MusicBrainz cache this job is paying
    for right now. Mirrors `_plan_page_context`'s `first_check`."""
    try:
        config = web.config()
    except ConfigError:
        return False
    return _last_plan(config) is None


def _job_run_id_for(web: _Web, meta: JobMeta) -> int | None:
    """`_job_run_id`, opening its own state database read: this job's own page, unlike Status,
    does not already have one open."""
    if meta.kind not in _RUN_LINKED_KINDS or not meta.finished_at:
        return None
    try:
        config = web.config()
    except ConfigError:
        return None
    if not config.state_db.is_file():
        return None
    with SqliteState(config.state_db) as state:
        return _job_run_id(state, meta)


def _prune_ready(web: _Web, meta: JobMeta) -> bool:
    """A "Find unneeded albums" job that finished with a readable report: its page is the review
    itself. One without (failed, or a report that won't parse) keeps its job page and log, and so
    does every one while Clean up is off: its review would answer "Clean up is off"."""
    return (
        meta.kind == "prune"
        and meta.state is JobState.DONE
        and web.cleanup_enabled()
        and _prune_or_none(web, meta.id) is not None
    )


def jobs_page(request: Request) -> Response:
    """`/jobs`: every kept job, newest first - `JobRunner.jobs()` already lists
    them that way; only Status's own "Recent jobs" list ever capped it to 5. Each one links to
    its own page, and to its run's "What changed" where `_job_run_id` finds one."""
    web = _web(request)
    jobs = web.runner.jobs()
    job_run_ids: dict[str, int] = {}
    try:
        config = web.config()
    except ConfigError:
        config = None
    if config is not None and config.state_db.is_file():
        with SqliteState(config.state_db) as state:
            job_run_ids = {m.id: rid for m in jobs if (rid := _job_run_id(state, m)) is not None}
    return web.render(request, "jobs.html", {"jobs": jobs, "job_run_ids": job_run_ids})


def job_page(request: Request) -> Response:
    web = _web(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None:
        return web.render(request, "missing.html", {}, status_code=404)
    if _prune_ready(web, meta):
        # No "decide what to keep" click-through: a finished search opens on its review.
        return RedirectResponse(f"/prune/{meta.id}", status_code=303)
    return web.render(request, "job.html", _job_context(web, meta))


def job_fragment(request: Request) -> Response:
    web = _web(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None:
        return PlainTextResponse("no such job", status_code=404, headers={"HX-Redirect": "/"})
    if _prune_ready(web, meta):
        # The poll that sees it finish takes the browser straight to the review.
        return Response(status_code=POLL_STOP, headers={"HX-Redirect": f"/prune/{meta.id}"})
    status_code = POLL_STOP if meta.finished else 200
    return web.render(request, "_job.html", _job_context(web, meta), status_code=status_code)


async def job_cancel(request: Request) -> Response:
    web = _web(request)
    job_id = request.path_params["job_id"]
    meta = web.runner.get(job_id)
    if meta is None:
        return web.render(request, "missing.html", {}, status_code=404)
    if meta.drain:
        # An apply is never stopped halfway (`JobRunner.cancel` refuses it too): Lidarr would be
        # left partly changed.
        context = {**_job_context(web, meta), "cancel_refused": True}
        return web.render(request, "job.html", context, 409)
    # Off the event loop: stopping a child touches a process and a thread, and must never hold up
    # every other request while it does.
    await anyio.to_thread.run_sync(web.runner.cancel, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# ---------------------------------------------------------------- explain


def _query_problem(query: str) -> str:
    if not query:
        return "Type an artist, a release title, a song or an MBID."
    if len(query) > MAX_QUERY:
        return f"Keep it under {MAX_QUERY} characters."
    if any(not ch.isprintable() for ch in query):
        return "That has characters in it no name or title has."
    return ""


def explain_form(request: Request) -> Response:
    """The form, and - given a query - the answer from the last run, worked out right here.

    No job: `shell.last_run.explain_from_last_run` reads the facts file and the state database
    and asks nobody, so it answers in well under a second. The live check is a POST (a job).
    """
    web = _web(request)
    recent = [m for m in web.runner.jobs() if m.kind == "explain"][:10]
    raw = request.query_params.get("query")
    if raw is None:
        return web.render(request, "explain.html", {"query": "", "error": "", "recent": recent})
    query = " ".join(raw.split())
    context: dict[str, Any] = {"query": query, "error": _query_problem(query), "recent": recent}
    if context["error"]:
        return web.render(request, "explain.html", context, 400)
    try:
        config = web.config()
    except ConfigError as exc:
        return web.render(request, "explain.html", {**context, "error": str(exc)}, 409)
    owned = {}
    if config.state_db.is_file():  # never create it: see healthz
        with SqliteState(config.state_db) as state:
            owned = state.owned_releases()
    names = web.playlist_names().names
    answer = explain_from_last_run(
        query, config=config, owned=owned, playlist_names=names, limit=EXPLAIN_LIMIT, read=web.last_run
    )
    context.update(answered=True, lidarr_url=config.ui.lidarr_url, deny=True)
    if answer is not None:
        report, last = answer
        context.update(report=_lookup_view(report.as_dict()), as_of=last.ran_at, run_label=last.label)
    return web.render(request, "explain.html", context)


async def explain_deny(request: Request) -> Response:
    """ "Not this one" on a Look up card: refuse a release the last run wanted, the same way the
    plan review's button does (`_deny`)."""
    web = _web(request)
    form = await request.form(max_files=0, max_fields=4)
    release = str(form.get("release") or "").strip().lower()
    raw = form.get("query")
    query = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not is_mbid(release):
        return PlainTextResponse("not a release", status_code=400)
    try:
        config = web.config()
    except ConfigError as exc:
        return PlainTextResponse(f"config.toml does not load: {exc}", status_code=409)
    # Off the event loop: the first read after a run parses a file of several MB, under a lock.
    last = await anyio.to_thread.run_sync(web.last_run, facts_path(config))
    # Only a release the last run wanted: the button is on those cards and nowhere else. A newer
    # run may have let go of it since the page was drawn: then there is nothing to refuse.
    wanted = [r for k, r in (last.desired.releases.items() if last is not None else ()) if k.rg_mbid == release]
    back = f"/explain?{urlencode({'query': query})}" if query and not _query_problem(query) else "/explain"
    if not wanted:
        request.session["flash"] = "That release isn't wanted any more by the latest run - nothing to do."
        return RedirectResponse(back, status_code=303)
    reasons = [reason for r in wanted for reason in r.reasons]
    if not deniable(reasons):
        # A saved album or a hand-kept release: refusing it would change nothing.
        request.session["flash"] = f"Not this one can't stop that release: {deny_note(reasons)}"
        return RedirectResponse(back, status_code=303)
    return _deny(request, web, release)


async def explain_start(request: Request) -> Response:
    """The live check: `likearr explain --json` as a job, reading Spotify and Lidarr afresh."""
    web = _web(request)
    form = await request.form(max_files=0, max_fields=8)
    raw = form.get("query")
    query = " ".join(raw.split()) if isinstance(raw, str) else ""
    recent = [m for m in web.runner.jobs() if m.kind == "explain"][:10]
    error = _query_problem(query)
    if error:
        return web.render(request, "explain.html", {"query": query, "error": error, "recent": recent}, 400)
    try:
        # The query goes after `--`, so one starting with "-" is never read as a flag.
        meta = web.runner.start("explain", ["explain", "--json", "--", query], label=query)
    except JobRefused as exc:
        return web.render(request, "explain.html", {"query": query, "error": str(exc), "recent": recent}, 409)
    return RedirectResponse(f"/jobs/{meta.id}", status_code=303)


def _not_found(request: Request, exc: Exception) -> Response:
    """`exception_handlers[404]`: Starlette's own answer to a path no route matches at all
    is a bare `text/plain` "Not Found" - no nav, no viewport tag. `AuthGateMiddleware` sits closer
    to the browser than the router and has already sent a logged-out visitor to `/login` for any
    path outside `_OPEN_PATHS`, so a request that reaches here is always a logged-in one; a
    fragment endpoint that returns its own 404 (a missing job, a missing run) never raises, so this
    only ever answers a URL with no route at all. The same `missing.html` those handlers already
    use, styled and with the nav."""
    web = _web(request)
    return web.render(request, "missing.html", {"what": "page"}, status_code=404)


# ---------------------------------------------------------------- the app


def create_app(settings: WebSettings) -> ASGIApp:
    """Build the app. Reads the config once for what cannot change without a restart: the hosts
    it answers to, the lock and the job root. Pages re-read it on every request.

    Raises:
        ConfigError: the config does not load, or its `[ui]` block has a problem. A run ignores
            `[ui]`; the server is the one place that refuses to start on it.
    """
    config = load_config(settings.config_path)
    if config.ui.errors:
        raise ConfigError("; ".join(config.ui.errors))
    web = _Web(settings, config, after={**plans_routes.AFTER, **cleanup_routes.AFTER, **settings_routes.AFTER})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        web.runner.recover()
        if settings.auto_fetch_names:
            await anyio.to_thread.run_sync(web.fetch_names_if_needed)
        if settings.auto_preview_setup:
            await anyio.to_thread.run_sync(settings_routes.preview_setup_if_needed, web)
        scheduler: Scheduler | None = None
        if settings.scheduler:
            assert_single_worker(settings.workers, reload=settings.reload)
            scheduler = Scheduler(config_path=settings.config_path, runner=web.runner, now=settings.now)
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.stop(timeout=5.0)
        # SIGTERM: refuse new jobs; wait for one that must not be cut short, stop anything else.
        await anyio.to_thread.run_sync(web.runner.shutdown, settings.shutdown_timeout_s)

    hosts = list(dict.fromkeys([*config.ui.allowed_hosts, *LOOPBACK_HOSTS]))
    # Unset: loopback plus any IPv4 address, and no host name - see AllowedHostMiddleware.
    any_ipv4 = not config.ui.allowed_hosts
    log.info(
        "likearr answers to host(s) %s%s (%s)",
        ", ".join(hosts),
        " and any IPv4 address" if any_ipv4 else "",
        ALLOWED_HOSTS_ENV,
    )
    middleware = [
        Middleware(AllowedHostMiddleware, allowed_hosts=hosts, any_ipv4=any_ipv4),
        Middleware(CrossOriginMiddleware),
        Middleware(SecureCookieMiddleware),
        Middleware(
            SessionMiddleware,
            secret_key=settings.session_secret or secrets.token_urlsafe(32),
            session_cookie="likearr_session",
            max_age=7 * 24 * 3600,
            # Kept "strict" for every route, including Spotify's direct-callback mode:
            # `/spotify/callback` never depends on this cookie at all (it is exempted from the
            # login gate and authorizes itself with a single-use server-side `state` instead -
            # see `web.spotify_connect` and `spotify_callback`), so there is no reason to loosen
            # a cookie that gates every other page in the app.
            same_site="strict",
            https_only=False,
            path="/",
        ),
        Middleware(AuthGateMiddleware, generation=lambda: web.generation),
    ]
    routes = [
        Route("/healthz", healthz),
        Route("/favicon.ico", favicon_ico, methods=["GET"]),
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", status),
        Route("/run-now", run_now, methods=["POST"]),
        Route("/runs/{run_id}", run_page, methods=["GET"]),
        Route("/unmatched", unmatched, methods=["GET"]),
        Route("/unmatched/rows", unmatched_rows, methods=["GET"]),
        Route("/unmatched/part", unmatched_part, methods=["GET"]),
        Route("/explain", explain_form, methods=["GET"]),
        Route("/explain", explain_start, methods=["POST"]),
        Route("/explain/deny", explain_deny, methods=["POST"]),
        *settings_routes.ROUTES,
        *cleanup_routes.ROUTES,
        *plans_routes.ROUTES,
        Route("/jobs", jobs_page, methods=["GET"]),
        Route("/jobs/{job_id}", job_page, methods=["GET"]),
        Route("/jobs/{job_id}/fragment", job_fragment, methods=["GET"]),
        Route("/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static"),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan, exception_handlers={404: _not_found})
    app.state.web = web
    # Outside Starlette itself: its ServerErrorMiddleware always wraps user middleware, so headers
    # added inside it would be missing from exactly the responses nobody planned, the 500s.
    return SecurityHeadersMiddleware(BodyLimitMiddleware(app))
