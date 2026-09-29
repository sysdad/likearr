"""Clean up: the prune review's routes - building a report, deciding each artist, exporting the
decisions, the read-only previews and the finish checklist.

Split out of `likearr.web.app`; `create_app` mounts `ROUTES` where these routes always
stood in its list, and passes `AFTER` to `_Web` for the preview chain.

Every route answers only while `[prune] enabled` is on (see `_when_on`). Off, ``GET /prune``
says what Clean up is and how to turn it on, and every other route answers that page with a 404,
so nothing starts and nothing is exported. The ledger and old prune jobs are left as they are.
"""

from __future__ import annotations

import functools
import itertools
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from likearr.adapters.spotify import read_granted_scopes
from likearr.config import ConfigError
from likearr.fsio import write_atomic
from likearr.prune_ledger import (
    LedgerBusy,
    LedgerUnreadable,
    ledger_lock,
    ledger_path,
    read_ledger,
    record,
    write_ledger,
)
from likearr.shell.last_run import facts_path
from likearr.shell.promote_save import REQUIRED_SCOPES
from likearr.web import cleanup, prune
from likearr.web.context import AfterCallback, _Web, _web
from likearr.web.jobs import JobMeta, JobRefused, JobState

log = logging.getLogger("likearr.web.app")
"""Under the app's own name, so log lines read as they did before the split."""


# ---------------------------------------------------------------- prune review


def _prune_jobs(web: _Web) -> list[JobMeta]:
    return [m for m in web.runner.jobs() if m.kind == "prune"]


_PRUNE_LOCK = threading.Lock()
"""One decision at a time: each one reads the draft, changes one artist and writes it back. The
ledger is written under it too, so two exports cannot interleave their read-modify-write."""


def prune_page(request: Request) -> Response:
    web = _web(request)
    reports = [m for m in _prune_jobs(web) if m.state is JobState.DONE and web.runner.job_file(m.id, "prune.json")]
    return web.render(request, "prune.html", {"jobs": _prune_jobs(web)[:10], "latest": reports[0] if reports else None})


async def prune_start(request: Request) -> Response:
    """Build the report: `likearr prune-report --out <its job dir>/prune.json`, as a job. It reads
    Spotify, MusicBrainz and Lidarr like a check does, and writes nothing to any of them."""
    web = _web(request)
    try:
        meta = web.runner.start(
            "prune", ["prune-report", "--out", "{job_dir}/prune.json"], label="prune report", expand_job_dir=True
        )
    except JobRefused as exc:
        return web.render(
            request, "prune.html", {"jobs": _prune_jobs(web)[:10], "latest": None, "error": str(exc)}, 409
        )
    return RedirectResponse(f"/jobs/{meta.id}", status_code=303)


def _prune_or_none(web: _Web, job_id: str) -> tuple[JobMeta, prune.PruneView, Path] | None:
    meta = web.runner.get(job_id)
    if meta is None or meta.kind != "prune":
        return None
    path = web.runner.job_file(job_id, "prune.json")
    view = prune.read_report(path) if path is not None else None
    return (meta, view, path.parent) if view is not None and path is not None else None


def _prune_words(web: _Web, artists: Sequence[prune.PruneArtist]) -> dict[str, Mapping[str, str]]:
    """What a protected album's "always kept" line names, beyond the report: playlists by
    their cached name, and - for a report that did not record it - each song's title from the last
    run's snapshot. Nothing is fetched; what is not known is worded without it. Each is read only
    when a row needs it: the names file only for a playlist's song, the last run only for a song
    with no title."""
    protections = [r.protection for a in artists for r in a.releases if r.protection is not None]
    keys = {p.intent_key for p in protections if not p.song}
    names = web.playlist_names().names if any(p.source == "playlist" for p in protections) else {}
    songs: dict[str, str] = {}
    if keys:
        try:
            last = web.last_run(facts_path(web.config()))
        except ConfigError:
            last = None
        if last is not None:
            songs = {t.reason.key: t.name for t in last.snapshot.tracks if t.reason.key in keys and t.name}
    return {"playlist_names": names, "songs": songs}


def _prune_rows_context(
    web: _Web, job_id: str, view: prune.PruneView, draft: prune.Draft, params: Mapping[str, str]
) -> dict[str, Any]:
    query = params.get("q", "")[:200]
    # No filter asked for opens on what needs a decision; anything unknown is everything.
    show = params.get("show", "needs")
    show = show if show in prune.SHOW_FILTERS else "all"
    raw_page = params.get("page", "1")
    page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) <= 6 else 1
    artists, matched, pages, page = prune.select_artists(view, draft, query=query, show=show, page=page)
    return {
        "job_id": job_id,
        "rows": prune.rows_for_page(artists, draft, **_prune_words(web, artists)),
        "matched": matched,
        "pages": pages,
        "page": page,
        "query": query,
        "show": show,
        "total": len(view.artists),
    }


def _prune_summary(view: prune.PruneView, draft: prune.Draft) -> dict[str, Any]:
    s = prune.summary(view, draft)
    return {
        **s,
        "candidate_size": prune.human_bytes(s["candidate_bytes"]),
        "trash_size": prune.human_bytes(s["trash_bytes"]),
    }


def _prune_draft_locked(web: _Web, job_id: str, view: prune.PruneView, job_dir: Path) -> tuple[prune.Draft, str]:
    """The job's draft with anything new in the ledger filled in, and why the ledger will not
    read (empty when it did, or when there is none yet). Call with `_PRUNE_LOCK` held.

    Done on every read, not once: `prefill` applies only entries this draft has not seen and never
    this review's own, so another review's export made after the page was first opened still
    reaches it, and a missing or broken ledger costs nothing but the pre-fill until it is fixed.
    The draft is written back only when something changed. If the pre-fill changed a choice and the
    files were already exported, they are removed and the page says export again, exactly as after
    any other change - no command may read decisions the page no longer shows.
    """
    draft = prune.read_draft(job_dir / "prune-draft.json")
    ledger = read_ledger(ledger_path(web.config_path))
    filled = prune.prefill(draft, view, ledger, own_source=prune.ledger_source(job_id))
    if filled is draft:
        return draft, ledger.problem
    try:
        if (filled.artists, filled.releases) != (draft.artists, draft.releases):
            removed = [name for name in EXPORTED if (job_dir / name).is_file()]
            for name in removed:
                (job_dir / name).unlink()
            if removed:
                filled.export_stale = True
        prune.write_draft(job_dir / "prune-draft.json", filled)
    except FileNotFoundError:  # the job store pruned it from under this request
        pass
    return filled, ledger.problem


def _prune_draft(web: _Web, job_id: str, view: prune.PruneView, job_dir: Path) -> tuple[prune.Draft, str]:
    with _PRUNE_LOCK:
        return _prune_draft_locked(web, job_id, view, job_dir)


def prune_review(request: Request) -> Response:
    web = _web(request)
    found = _prune_or_none(web, request.path_params["job_id"])
    if found is None:
        return web.render(request, "missing.html", {}, status_code=404)
    meta, view, job_dir = found
    draft, ledger_problem = _prune_draft(web, meta.id, view, job_dir)
    return web.render(
        request,
        "prune_review.html",
        {
            "meta": meta,
            "view": view,
            "notes": draft.notes,
            "ledger_problem": ledger_problem,
            "rows_ctx": _prune_rows_context(web, meta.id, view, draft, request.query_params),
            "output": web.runner.shown_output(meta.id),
            "tail": web.runner.log_tail(meta.id),
            **_prune_export_context(view, job_dir, draft),
            "finish": None
            if draft.export_stale
            else _prune_finish_context(web, meta, job_dir, _prune_summary(view, draft)),
        },
    )


def prune_rows(request: Request) -> Response:
    web = _web(request)
    found = _prune_or_none(web, request.path_params["job_id"])
    if found is None:
        return PlainTextResponse("no such prune report", status_code=404)
    meta, view, job_dir = found
    draft, _problem = _prune_draft(web, meta.id, view, job_dir)
    return web.render(request, "_prune_rows.html", _prune_rows_context(web, meta.id, view, draft, request.query_params))


EXPORTED = ("decisions.json", "review-data.json")
_EXPIRED = "This report expired - build a new one."


def _prune_expired() -> Response:
    return PlainTextResponse(_EXPIRED, status_code=409)


def _prune_row_response(
    web: _Web,
    request: Request,
    job_id: str,
    view: prune.PruneView,
    job_dir: Path,
    draft: prune.Draft,
    artist: prune.PruneArtist,
    *,
    conflict: str = "",
    status_code: int = 200,
) -> Response:
    """The artist's row as it now stands, plus - out of band - the summary and the export status."""
    return web.render(
        request,
        "_prune_artist.html",
        {
            "job_id": job_id,
            "row": prune.rows_for_page([artist], draft, **_prune_words(web, [artist]))[0],
            "conflict": conflict,
            "oob": True,
            **_prune_export_context(view, job_dir, draft),
        },
        status_code,
    )


def _prune_export_context(view: prune.PruneView, job_dir: Path, draft: prune.Draft) -> dict[str, Any]:
    return {
        "summary": _prune_summary(view, draft),
        "exported": all((job_dir / name).is_file() for name in EXPORTED),
        "export_stale": draft.export_stale,
        "report_path": str(job_dir / "prune.json"),
        "job_dir": str(job_dir),
    }


async def prune_decide(request: Request) -> Response:
    """Record ONE change - an artist's decision, or one album's override - against the revision the
    row was showing. A stale tab gets 409 and the row as it now is, never a silent overwrite. A
    change after an export removes the exported files, so the commands never read stale ones."""
    web = _web(request)
    form = await request.form(max_files=0, max_fields=8)
    found = _prune_or_none(web, request.path_params["job_id"])
    if found is None:
        return _prune_expired()
    meta, view, job_dir = found
    artist_mbid = str(form.get("artist") or "")
    raw_rev = str(form.get("rev") or "")
    rev = int(raw_rev) if raw_rev.isascii() and raw_rev.isdigit() and len(raw_rev) <= 9 else -1
    decision = form.get("decision")
    release = form.get("release")
    change: dict[str, Any] = (
        {"decision": str(decision)}
        if decision is not None and release is None
        else {"release": (str(release), str(form.get("value") or ""))}
        if release is not None and decision is None
        else {}
    )
    artist = view.artist(artist_mbid)
    with _PRUNE_LOCK:
        draft, _problem = _prune_draft_locked(web, meta.id, view, job_dir)
        try:
            changed = prune.decide(draft, view, artist_mbid, rev=rev, **change)
        except prune.StaleChange:
            assert artist is not None
            note = "Changed in another tab - this row now shows what was saved. Make your change again."
            return _prune_row_response(
                web, request, meta.id, view, job_dir, draft, artist, conflict=note, status_code=409
            )
        except prune.DecisionError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        try:
            removed = [name for name in EXPORTED if (job_dir / name).is_file()]
            for name in removed:
                (job_dir / name).unlink()
            if removed:
                changed.export_stale = True
            prune.write_draft(job_dir / "prune-draft.json", changed)
        except FileNotFoundError:  # the job store pruned it from under this request
            return _prune_expired()
    assert artist is not None  # decide() refused anything else
    return _prune_row_response(web, request, meta.id, view, job_dir, changed, artist)


async def prune_export(request: Request) -> Response:
    """Write the decisions file and the review snapshot beside the report, for the terminal steps,
    and record what was decided in the ledger, so the next report starts from it. It writes
    those files and nothing else: no move, no Spotify call."""
    web = _web(request)
    form = await request.form(max_files=0, max_fields=4)
    found = _prune_or_none(web, request.path_params["job_id"])
    if found is None:
        return _prune_expired()
    meta, view, job_dir = found
    with _PRUNE_LOCK:
        draft, _problem = _prune_draft_locked(web, meta.id, view, job_dir)
        draft.notes = str(form.get("notes") or "")[: prune.MAX_NOTES]
        draft.export_stale = False
        try:
            prune.write_draft(job_dir / "prune-draft.json", draft)
            _write_json(job_dir / "decisions.json", prune.decisions_file(view, draft))
            _write_json(job_dir / "review-data.json", prune.review_data(view, draft))
        except FileNotFoundError:
            return _prune_expired()
    flash = "Exported decisions.json and review-data.json beside the report."
    day = web.now().astimezone(web.tz).date().isoformat()
    problem = await anyio.to_thread.run_sync(
        _record_in_ledger, ledger_path(web.config_path), view, job_dir, draft, day, prune.ledger_source(meta.id)
    )
    if problem:
        flash += f" These decisions were not remembered for the next review: {problem}"
    started = await anyio.to_thread.run_sync(preview_prune, web, meta.id) if web.settings.auto_preview_prune else ""
    if started:
        flash += f" The previews didn't start ({started}); use Preview again below."
    request.session["flash"] = flash
    return RedirectResponse(f"/prune/{meta.id}#finish", status_code=303)


def _record_in_ledger(
    path: Path, view: prune.PruneView, job_dir: Path, exported: prune.Draft, day: str, source: str
) -> str:
    """Record one export in the ledger; why it could not be, or ``""``. Runs in a worker thread:
    `ledger_lock` may wait (bounded) for another writer, and that wait must not stall the server.
    Outside `_PRUNE_LOCK` for the same reason - the file lock alone orders two writers, threads and
    processes alike.

    Two exports of one report can reach here out of order. So under the file lock the draft on disk
    is read again, and the write is skipped when it has moved on from `exported`: the later export
    records the newer answer, and the ledger never ends on the older one. (A change with no export
    after it is not recorded at all - the ledger holds exported decisions only.)"""
    try:
        with ledger_lock(path):
            now = prune.read_draft(job_dir / "prune-draft.json")
            if (now.artists, now.releases, now.carried, now.revs) != (
                exported.artists,
                exported.releases,
                exported.carried,
                exported.revs,
            ):
                return "the review changed while it was exporting; export again to remember it"
            releases, artists = prune.ledger_changes(view, exported)
            write_ledger(path, record(read_ledger(path), releases, artists, on=day, source=source))
    except (LedgerUnreadable, LedgerBusy, OSError) as exc:
        log.warning("could not record the review in %s: %s", path, exc)
        return str(exc)
    return ""


# ---------------------------------------------------------------- the previews


_PREVIEW_LOCK = threading.RLock()
"""Held while a preview's binding is read, a job started, and the binding written: an after-callback
waits for it, so a preview that finishes at once still finds its own id recorded."""

_PREVIEW_STEPS = {"prune-preview": "stage", "spotify-preview": "spotify", "prune-checks": "checks"}


def _prune_dir(web: _Web, prune_id: str) -> Path | None:
    path = web.runner.job_file(prune_id, "prune.json")
    return path.parent if path is not None else None


def preview_prune(web: _Web, prune_id: str) -> str:
    """Start the read-only previews of a report's current export, bound to it by the sha256 of its
    ``decisions.json``: the move first, then (see `_after_preview`) Spotify when the export asks
    for a follow or a save, then the Lidarr checks. An export that trashes nothing skips the move
    and the checks. Returns why it could not start, or ""."""
    return start_preview(web, prune_id, None, fresh=True)


def start_preview(web: _Web, prune_id: str, step: str | None, *, fresh: bool = False) -> str:
    """Start one preview step for the current export, and record its job in the binding.

    `fresh` begins a new chain, at `step` or, when `step` is None, at the chain's first step for
    the export (nothing starts when the chain is empty): the binding is replaced only once its
    first job has started, so a refused start (another job is running - perhaps this chain's own)
    leaves the previews on the page as they were. Otherwise the step joins the binding's chain,
    and nothing starts when the export changed since: a preview would be shown for an export it
    never saw. Every step is read-only - no ``--apply``, no ``--force``."""
    job_dir = _prune_dir(web, prune_id)
    if job_dir is None:
        return "this report expired"
    try:
        config = web.config()
    except ConfigError as exc:
        return f"config.toml does not load: {exc}"
    decisions, reviewed = job_dir / "decisions.json", job_dir / "review-data.json"
    kinds = {
        "stage": (
            "prune-preview",
            [
                "prune-stage",
                "--manifest",
                str(job_dir / "prune.json"),
                "--holding",
                config.holding_dir,
                "--decisions",
                str(decisions),
                "--no-mount-check",
                "--out",
                "{job_dir}/stage.json",
            ],
            "Preview the clean up",
        ),
        "spotify": (
            "spotify-preview",
            [
                "promote-save",
                "--decisions",
                str(decisions),
                "--reviewed",
                str(reviewed),
                "--out",
                "{job_dir}/promote-save.json",
            ],
            "Preview Spotify changes",
        ),
        "checks": ("prune-checks", ["prune-checks", "--out", "{job_dir}/checks.json"], "Check Lidarr"),
    }
    with _PREVIEW_LOCK:
        digest = cleanup.decisions_digest(decisions)
        if digest is None:
            return "export the decisions first"
        binding = (
            cleanup.Binding(digest, asks_spotify=cleanup.asks_spotify(decisions), trashes=cleanup.trashes(decisions))
            if fresh
            else cleanup.read_binding(job_dir)
        )
        if binding is None:
            return "preview this export first (Preview again)"
        if binding.decisions_sha256 != digest:
            return "the export changed since; preview it again"
        if step is None:
            chain = binding.chain()
            if not chain:
                return ""
            step = chain[0]
        kind, args, label = kinds[step]
        try:
            meta = web.runner.start(kind, args, label=label, expand_job_dir=True, plan_id=prune_id)
        except JobRefused as exc:
            return str(exc)
        setattr(binding, step, meta.id)
        cleanup.write_binding(job_dir, binding)
    return ""


def _after_preview(web: _Web, meta: JobMeta) -> None:
    """A preview job finished: start the next step its export still lacks, in chain order - the
    move, Spotify (when asked for), the Lidarr checks. Only for a job the binding names: a job
    from an earlier export, or a chain replaced since, never writes this export's binding. The
    chain carries on after any of its jobs, so "Check again" clicked between two steps (which
    takes the slot the next step wanted) delays that step rather than ending the chain."""
    step = _PREVIEW_STEPS[meta.kind]
    with _PREVIEW_LOCK:
        job_dir = _prune_dir(web, meta.plan_id)
        binding = cleanup.read_binding(job_dir) if job_dir is not None else None
        if binding is None or getattr(binding, step) != meta.id:
            return
        then = next((s for s in binding.chain() if not getattr(binding, s)), None)
    if then is not None and web.cleanup_enabled():  # turned off mid-chain: start nothing more
        problem = start_preview(web, meta.plan_id, then)
        if problem:
            log.info("clean up preview %s not started: %s", then, problem)


_FINISH_FILES = {"stage": "stage.json", "spotify": "promote-save.json", "checks": "checks.json"}
_CHAIN_GAP = timedelta(seconds=10)


def _prune_finish_context(web: _Web, meta: JobMeta, job_dir: Path, summary: Mapping[str, Any]) -> dict[str, Any] | None:
    """The "Finish the clean up" checklist for the current export, or ``None`` before one.

    Reads only: the preview jobs' own files, and the token file for the scope names (never a token).
    A preview is shown only for the export it previewed (`cleanup.Binding`)."""
    digest = cleanup.decisions_digest(job_dir / "decisions.json")
    if digest is None:
        return None
    try:
        config = web.config()
    except ConfigError as exc:
        return {"config_error": str(exc)}
    binding = cleanup.read_binding(job_dir)
    bound = binding is not None and binding.decisions_sha256 == digest
    steps: dict[str, dict[str, Any]] = {}
    for step, name in _FINISH_FILES.items():
        job_id = getattr(binding, step) if bound and binding is not None else ""
        step_meta = web.runner.get(job_id) if job_id else None
        state = "missing" if step_meta is None else "running" if not step_meta.finished else step_meta.state.value
        path = web.runner.job_file(job_id, name) if step_meta is not None and step_meta.finished else None
        steps[step] = {"meta": step_meta, "state": state, "path": path}
    # The chain starts each step as the one before it finishes: a step not started yet, just after
    # its predecessor finished, is on its way - keep refreshing, briefly, rather than say "not
    # previewed". Bounded, so a start that was refused never keeps a page polling.
    chain = binding.chain() if binding is not None else []
    for before, step in itertools.pairwise(chain):
        prior = steps[before]["meta"]
        if (
            steps[step]["state"] == "missing"
            and prior is not None
            and prior.finished_at
            and web.now() - datetime.fromisoformat(prior.finished_at) <= _CHAIN_GAP
        ):
            steps[step]["state"] = "running"
    stage = cleanup.read_stage(steps["stage"]["path"])
    if stage is not None and stage.decisions_sha256 != digest:
        stage = None
    spotify = cleanup.read_spotify(steps["spotify"]["path"])
    granted = read_granted_scopes(config.spotify.token_file)
    return {
        "config_error": "",
        "job_id": meta.id,
        "steps": steps,
        "stage": stage,
        "spotify": spotify,
        "checks": cleanup.read_checks(steps["checks"]["path"]),
        "asks_spotify": cleanup.asks_spotify(job_dir / "decisions.json"),
        "trashes": cleanup.trashes(job_dir / "decisions.json"),
        "missing_scopes": sorted(REQUIRED_SCOPES - granted) if granted is not None else None,
        # In direct-callback mode the missing-write-scope hint can also start the flow itself.
        "callback_mode": bool(config.ui.public_url),
        "running": any(s["state"] == "running" for s in steps.values()),
        "holding": config.holding_dir,
        "cli_command": config.ui.cli_command,
        "prune_errors": config.prune.errors,
        "lidarr_url": config.ui.lidarr_url,
        "summary": summary,
        "commands": cleanup.commands(
            prefix=config.ui.cli_command,
            config_path=web.config_path.absolute(),
            job_dir=job_dir,
            holding=config.holding_dir,
            preview_plan=steps["spotify"]["path"] if spotify is not None else None,
        ),
    }


def _prune_exported(web: _Web, job_id: str) -> tuple[JobMeta, prune.PruneView, Path] | None:
    """The report, if its decisions are exported and current: what a preview may be started for."""
    found = _prune_or_none(web, job_id)
    if found is None:
        return None
    _meta, _view, job_dir = found
    draft = prune.read_draft(job_dir / "prune-draft.json")
    if draft.export_stale or not all((job_dir / name).is_file() for name in EXPORTED):
        return None
    return found


async def prune_preview(request: Request) -> Response:
    """Preview the current export again: the move, Spotify, the Lidarr checks. Read-only jobs."""
    web = _web(request)
    found = _prune_exported(web, request.path_params["job_id"])
    if found is None:
        return _prune_expired()
    problem = await anyio.to_thread.run_sync(preview_prune, web, found[0].id)
    if problem:
        request.session["flash"] = f"The previews didn't start: {problem}."
    return RedirectResponse(f"/prune/{found[0].id}#finish", status_code=303)


async def prune_checks(request: Request) -> Response:
    """Check Lidarr again (import lists, the command queue), for the current export."""
    web = _web(request)
    found = _prune_exported(web, request.path_params["job_id"])
    if found is None:
        return _prune_expired()
    problem = await anyio.to_thread.run_sync(start_preview, web, found[0].id, "checks")
    if problem:
        request.session["flash"] = f"Lidarr wasn't checked: {problem}."
    return RedirectResponse(f"/prune/{found[0].id}#finish", status_code=303)


def prune_finish(request: Request) -> Response:
    """The checklist alone, for its own refresh while a preview runs."""
    web = _web(request)
    found = _prune_or_none(web, request.path_params["job_id"])
    if found is None:
        return PlainTextResponse("no such prune report", status_code=404)
    meta, view, job_dir = found
    draft = prune.read_draft(job_dir / "prune-draft.json")
    finish = None if draft.export_stale else _prune_finish_context(web, meta, job_dir, _prune_summary(view, draft))
    return web.render(request, "_prune_finish.html", {"finish": finish})


def prune_download(request: Request) -> Response:
    """The exported files as downloads: generated from the current decisions each time."""
    web = _web(request)
    found = _prune_or_none(web, request.path_params["job_id"])
    name = request.path_params["name"]
    if found is None or name not in {"decisions.json", "review-data.json"}:
        return PlainTextResponse("no such file", status_code=404)
    meta, view, job_dir = found
    draft, _problem = _prune_draft(web, meta.id, view, job_dir)
    body = prune.decisions_file(view, draft) if name == "decisions.json" else prune.review_data(view, draft)
    return Response(
        json.dumps(body, indent=2) + "\n",
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{name}"'},
    )


def _write_json(path: Path, body: Mapping[str, Any]) -> None:
    write_atomic(path, json.dumps(body, indent=2) + "\n", mode=0o600)


def _cleanup_off(request: Request, status: int = 404) -> Response:
    """What every Clean up route answers while `[prune] enabled` is off: what it does and
    how to turn it on. ``GET /prune`` - the nav's old link, a bookmark - is a page (its route passes
    200, which covers the HEAD Starlette adds too); anything else is not there (404), so a stale
    tab's decide or export changes nothing."""
    return _web(request).render(request, "prune_off.html", {}, status)


def _when_on(endpoint: Callable[[Request], Response], *, off_status: int = 404) -> Callable[[Request], Response]:
    """`endpoint` while Clean up is on, `_cleanup_off` with `off_status` otherwise. Stays a plain
    function, so Starlette still runs it in a worker thread."""

    @functools.wraps(endpoint)
    def gated(request: Request) -> Response:
        return endpoint(request) if _web(request).cleanup_enabled() else _cleanup_off(request, off_status)

    return gated


def _when_on_async(endpoint: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    """`_when_on` for an ``async`` endpoint."""

    @functools.wraps(endpoint)
    async def gated(request: Request) -> Response:
        return await endpoint(request) if _web(request).cleanup_enabled() else _cleanup_off(request)

    return gated


ROUTES: list[Route] = [
    Route("/prune", _when_on(prune_page, off_status=200), methods=["GET"]),
    Route("/prune", _when_on_async(prune_start), methods=["POST"]),
    Route("/prune/{job_id}", _when_on(prune_review), methods=["GET"]),
    Route("/prune/{job_id}/rows", _when_on(prune_rows), methods=["GET"]),
    Route("/prune/{job_id}/decide", _when_on_async(prune_decide), methods=["POST"]),
    Route("/prune/{job_id}/export", _when_on_async(prune_export), methods=["POST"]),
    Route("/prune/{job_id}/download/{name}", _when_on(prune_download), methods=["GET"]),
    Route("/prune/{job_id}/preview", _when_on_async(prune_preview), methods=["POST"]),
    Route("/prune/{job_id}/checks", _when_on_async(prune_checks), methods=["POST"]),
    Route("/prune/{job_id}/finish", _when_on(prune_finish), methods=["GET"]),
]
"""In `create_app`'s order: Starlette matches the first route that fits."""

# The previews, one after another: the move, Spotify, the Lidarr checks. Keyed by `_PREVIEW_STEPS`,
# which `_after_preview` looks each kind up in, so the two never drift apart.
AFTER: dict[str, AfterCallback] = dict.fromkeys(_PREVIEW_STEPS, _after_preview)
"""This module's after-callbacks by job kind, which `create_app` passes to `_Web`."""
