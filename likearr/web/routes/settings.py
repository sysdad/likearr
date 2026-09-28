"""Settings, Spotify connect, Lidarr setup and Doctor: the Settings page and its sections' routes.

Split out of `likearr.web.app` (#154); `create_app` mounts `ROUTES` where these routes always
stood in its list.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from likearr.adapters.spotify import (
    AccountRefused,
    SpotifyAuth,
    TokenSet,
    asks_for_write_scopes,
    lacks_collaborative,
    read_account,
    read_authorized_at,
    read_granted_scopes,
    reauth_due,
)
from likearr.adapters.state_sqlite import SqliteState
from likearr.config import Config, ConfigError
from likearr.models import SPOTIFY_WRITE_SCOPES, RunStatus
from likearr.playlist_names import (
    COLLABORATIVE_REAUTH_REASON,
    NOT_OWNED_CONFIGURED_NOTE,
    NOT_OWNED_REASON,
    NOT_OWNED_WORKAROUND,
    NamesCache,
    playlist_url,
)
from likearr.ports import SourceError
from likearr.web import doctor as doctor_view
from likearr.web import lidarr_setup, spotify_connect
from likearr.web import settings as cfg
from likearr.web.auth import content_security_policy
from likearr.web.context import POLL_STOP, AfterCallback, _Web, _web
from likearr.web.helpers import _first_applied, _form_pairs, _parse_playlists, _playlist_jobs, _read_config, _readable
from likearr.web.jobs import JobMeta, JobRefused, JobState
from likearr.web.status import ReauthView, reauth_view

log = logging.getLogger("likearr.web.app")
"""Under the app's own name, so log lines read as they did before the split (#154)."""


PLAYLISTS_FRESH = timedelta(minutes=10)
"""How long a `likearr playlists --json` answer is reused instead of asking Spotify again when the
button is pressed. It governs nothing else: the names it brought are kept in the names file
(`likearr.playlist_names`) for good."""


_AFTER_SAVE = {"/plan": "Saved. Check for changes again to see what those songs resolve to now."}
"""Where a settings save may send the browser afterwards, and what it then says. An allowlist,
so a posted `next` can never become an open redirect."""


# ---------------------------------------------------------------- settings


def _picker(
    selected: Sequence[str],
    fetched: Mapping[str, Any] | None,
    note: str = "",
    *,
    names: NamesCache,
    poll: str = "",
    log_job: str = "",
    offer: bool = False,
) -> dict[str, Any]:
    """What the playlist picker shows. `selected` is what the form says, not what the file says.

    - Not asked yet (`offer`): the selection as checkboxes, and a button that asks Spotify.
      Opening the page never starts a job by itself.
    - Waiting (`poll` follows a running fetch): the selection rides along as hidden inputs and is
      listed, not offered, so nothing the user clicks can be lost when the next swap arrives. A
      save made meanwhile keeps it.
    - Answered: every playlist `GET /me/playlists` lists, readable or not, checked if selected,
      plus any selected id Spotify did not list at all. A readable entry - owned, or one you
      collaborate on once the token has ``playlist-read-collaborative`` (#103, item 3) - is a
      normal checkbox. An unreadable one not already configured (followed, someone else's, or
      one of Spotify's own algorithmic or editorial playlists) is greyed out, disabled and
      carries the reason and the workaround (issue #103, item 1); a collaborative one the token
      cannot read yet says to re-authorize instead - a disabled checkbox never posts its value,
      so it can never be *added* by clicking it. An unreadable one that *is*
      already configured is greyed out too, but stays checked and rides a hidden input, so an
      unrelated save never drops it (likearr does not change what you set without saying so); its
      own note says it is kept but unreadable, and a separate, always-enabled "Remove from
      settings" checkbox is the one way to take it out on purpose.
    - Unanswered (the fetch failed or could not start): the selection, with why.

    Whatever the state, a playlist is named from the answer, else from `names` (the names file),
    else shown as its id, linked to it on Spotify.
    """
    base = {
        "note": note,
        "poll": poll,
        "waiting": bool(poll),
        "log": log_job,
        "offer": offer,
        "names_as_of": names.fetched_at if names.names else None,
        "no_names": not names.names,
        "not_owned_reason": NOT_OWNED_REASON,
        "not_owned_workaround": NOT_OWNED_WORKAROUND,
        "not_owned_configured_note": NOT_OWNED_CONFIGURED_NOTE,
        "collaborative_reauth_reason": COLLABORATIVE_REAUTH_REASON,
    }

    def row(
        pid: str,
        name: str,
        *,
        tracks: object = None,
        checked: bool = True,
        missing: bool = False,
        readable: bool = True,
        needs_reauth: bool = False,
    ) -> dict[str, Any]:
        name = name or names.names.get(pid, "")
        url = None if name else playlist_url(pid)
        return {
            "id": pid,
            "name": name,
            "tracks": tracks,
            "checked": checked,
            "missing": missing,
            "url": url,
            "readable": readable,
            "needs_reauth": needs_reauth,
        }

    if fetched is None:
        return {**base, "rows": [row(pid, "") for pid in selected], "loaded": False}
    entries = [p for p in fetched["playlists"] if isinstance(p, dict) and p.get("id")]
    known_ids = {str(p["id"]) for p in entries}
    rows = [
        row(
            str(p["id"]),
            str(p.get("name") or ""),
            tracks=p.get("track_count"),
            checked=str(p["id"]) in selected,
            # Owned, or collaborative once the token may read it (#103, item 3).
            readable=_readable(p),
            needs_reauth=p.get("needs_reauth") is True,
        )
        for p in entries
    ]
    rows += [row(pid, "", missing=True) for pid in selected if pid not in known_ids]
    return {**base, "rows": rows, "loaded": True}


_ASKING = "Asking Spotify for your playlists; they can be changed once the list is here."
_UNANSWERED = "Spotify did not list your playlists, so these are the selected ones, named as last known."


def _picker_for(
    web: _Web, selected: Sequence[str], jobs: Sequence[JobMeta], names: NamesCache
) -> dict[str, Any] | None:
    """The picker from the newest playlists job, or ``None`` when a new fetch is due.

    A running fetch is followed rather than duplicated, and an answer - failed as well as
    successful - is reused for `PLAYLISTS_FRESH`: without the failed case, every visit while
    Spotify is refusing would start another doomed child and push an older job out of the store.
    """
    playlist_jobs = _playlist_jobs(jobs)
    if not playlist_jobs:
        return None
    meta = playlist_jobs[0]
    if not meta.finished:
        return _picker(selected, None, _ASKING, names=names, poll=meta.id)
    if not meta.finished_at or web.now() - datetime.fromisoformat(meta.finished_at) > PLAYLISTS_FRESH:
        return None
    fetched = _parse_playlists(web.runner.output(meta.id)) if meta.state is JobState.DONE else None
    if fetched is None:
        return _picker(selected, None, _UNANSWERED, names=names, log_job=meta.id)
    return _picker(selected, fetched, names=names)


def _settings_context(
    web: _Web,
    text: bytes,
    config: Config,
    *,
    display: Mapping[tuple[str, str], Any] | None = None,
    errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The settings form, from one read of the file: its hash and its values must match."""
    values: dict[tuple[str, str], Any] = dict(display) if display is not None else cfg.current_values(config)
    selected = tuple(values[("spotify", "playlists")])
    # Nothing is fetched until asked: opening a page must not start a job. A recent answer, or a
    # fetch already running, is shown as it is.
    names = web.playlist_names()
    jobs = web.runner.jobs()
    picker = _picker_for(web, selected, jobs, names) or _picker(selected, None, names=names, offer=True)
    return {
        "fields": cfg.FIELDS,
        "values": {f.name: values[(f.section, f.key)] for f in cfg.FIELDS},
        "file_hash": cfg.file_hash(text),
        "errors": dict(errors or {}),
        "picker": picker,
        "config_error": "",
        "schedule": config.schedule,
        "cleanup": config.prune,
        "first_applied": _first_applied(config),
        "paused_at_local": config.schedule.paused_at.astimezone(web.tz) if config.schedule.paused_at else None,
        "schedule_preview": cfg.preview_schedule(config.schedule.cron, config.schedule.timezone, now=web.now()),
        "spotify": spotify_status(config, now=web.now()),
        "setup": _setup_panel(web, _latest(jobs, *SETUP_KINDS)),
        "doctor": _doctor_panel(web, _latest(jobs, "doctor")),
    }


def settings_page(request: Request) -> Response:
    web = _web(request)
    try:
        context = _settings_context(web, *_read_config(web))
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc))
    return web.render(request, "settings.html", context)


async def settings_save(request: Request) -> Response:
    web = _web(request)
    posted = await _posted(request)
    now = web.now()
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)

    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        changed = {
            "": "config.toml changed since you opened this page - edited by hand, or saved from another tab. "
            "Nothing was saved; these are the values in the file now."
        }
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)

    values, errors = cfg.parse_form(posted)
    posted_playlists = values.get(("spotify", "playlists"))
    if isinstance(posted_playlists, tuple):
        # "Remove from settings" is the one way to drop a configured-but-not-owned playlist on
        # purpose (issue #103): its own checkbox is disabled (see `_picker`), so nothing removes
        # it just by being posted back unchanged.
        remove_ids = {v.strip() for v in posted.get("spotify.playlists.remove", []) if v.strip()}
        if remove_ids:
            posted_playlists = tuple(pid for pid in posted_playlists if pid not in remove_ids)
            values[("spotify", "playlists")] = posted_playlists
        # A playlist already in `[spotify].playlists` rides a hidden input every save, owned or
        # not - likearr never drops what you set without saying so. Only a newly added id gets
        # refused for being not owned; one already configured, posted back unchanged, saves fine.
        already_configured = set(config.spotify.playlists)
        cached = web.playlist_names()
        refused = [pid for pid in posted_playlists if pid in cached.not_owned and pid not in already_configured]
        reauth = [pid for pid in refused if pid in cached.needs_reauth]
        others = [pid for pid in refused if pid not in cached.needs_reauth]
        messages = []
        if others:
            messages.append(f"not saved - {', '.join(others)}: {NOT_OWNED_REASON}. Instead, {NOT_OWNED_WORKAROUND}.")
        if reauth:
            messages.append(f"not saved - {', '.join(reauth)}: {COLLABORATIVE_REAUTH_REASON}.")
        if messages:
            errors["spotify.playlists"] = " ".join(messages)
    if errors:
        display = {**cfg.current_values(config), **values}
        for name in errors:
            f = cfg.FIELD_BY_NAME[name]
            # A field `parse_form` could not parse at all has no entry in `values` (it `continue`s
            # past the assignment), so the raw text is what re-shows the invalid input. One that
            # parsed fine and failed a later check - the playlist ownership refusal below - keeps
            # its parsed value; `posted.get(name, [""])[0]` would flatten the picker's tuple to one
            # id (its first character, once iterated) and break the selection entirely.
            if (f.section, f.key) not in values:
                display[(f.section, f.key)] = posted.get(name, [""])[0]
        context = _settings_context(web, text, config, display=display, errors=errors)
        return web.render(request, "settings.html", context, 400)

    check = cfg.plan_save(
        text.decode("utf-8"), values, base_dir=web.config_path.parent, playlist_names=web.playlist_names().names
    )
    if check.errors:
        context = _settings_context(web, text, config, display=values, errors=check.errors)
        return web.render(request, "settings.html", context, 400)
    if not check.changes:
        request.session["flash"] = "Nothing changed, so nothing was saved."
        return RedirectResponse("/settings", status_code=303)
    # The confirm is bound to exactly what it showed: its page carries a digest of the file this
    # save would write, and only that digest lets the save through. "confirmed=yes" alone, or a
    # value changed on the way back from the confirm page, gets the confirm page again.
    planned = cfg.file_hash(check.new_text.encode("utf-8"))
    if check.confirm and posted.get("confirm_digest", [""])[0] != planned:
        return web.render(
            request,
            "settings_confirm.html",
            {
                "reasons": check.confirm,
                "changes": cfg.describe_changes(check.changes, web.playlist_names().names),
                "pairs": _form_pairs(values),
                "file_hash": cfg.file_hash(text),
                "confirm_digest": planned,
            },
        )
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=now)
    except cfg.SaveConflict:
        text, config = _read_config(web)
        changed = {"": "config.toml changed while saving. Nothing was saved; these are the values now."}
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)
    changed = ", ".join(f"{c.section}.{c.key}" for c in check.changes)
    log.info("settings saved from the web UI: %s (backup %s)", changed, backup.name)
    # A playlist just added has no name yet: fetch it now, from this POST, never from the GET after.
    if web.settings.auto_fetch_names:
        await anyio.to_thread.run_sync(web.fetch_names_if_needed)
    next_url = posted.get("next", [""])[0]
    if next_url in _AFTER_SAVE:
        request.session["flash"] = f"{_AFTER_SAVE[next_url]} The previous file is {backup.name}."
        return RedirectResponse(next_url, status_code=303)
    request.session["flash"] = f"Saved {changed}. The previous file is {backup.name}."
    return RedirectResponse("/settings", status_code=303)


async def _posted(request: Request) -> dict[str, list[str]]:
    form = await request.form(max_files=0)
    return {name: [v for v in form.getlist(name) if isinstance(v, str)] for name in form}


async def settings_pause(request: Request) -> Response:
    """Pause scheduled runs: saves at once, no confirm - see `cfg.plan_pause`."""
    web = _web(request)
    posted = await _posted(request)
    now = web.now()
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)

    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        changed = {
            "": "config.toml changed since you opened this page - edited by hand, or saved from another tab. "
            "Nothing was saved; these are the values in the file now."
        }
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)

    reason = posted.get("reason", [""])[0]
    check = cfg.plan_pause(text.decode("utf-8"), reason, base_dir=web.config_path.parent, now=now)
    if check.errors:
        context = _settings_context(web, text, config, errors=check.errors)
        return web.render(request, "settings.html", context, 400)
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=now)
    except cfg.SaveConflict:
        text, config = _read_config(web)
        changed = {"": "config.toml changed while saving. Nothing was saved; these are the values now."}
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)
    log.info("scheduled runs paused from the web UI (%s; backup %s)", reason or "no reason given", backup.name)
    request.session["flash"] = f"Scheduled runs paused. The previous file is {backup.name}."
    return RedirectResponse("/settings", status_code=303)


async def settings_resume(request: Request) -> Response:
    """Resume scheduled runs: always the second confirm - see `cfg.plan_resume`."""
    web = _web(request)
    posted = await _posted(request)
    now = web.now()
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)

    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        changed = {
            "": "config.toml changed since you opened this page - edited by hand, or saved from another tab. "
            "Nothing was saved; these are the values in the file now."
        }
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)

    check = cfg.plan_resume(text.decode("utf-8"), base_dir=web.config_path.parent)
    if check.errors:
        context = _settings_context(web, text, config, errors=check.errors)
        return web.render(request, "settings.html", context, 400)
    planned = cfg.file_hash(check.new_text.encode("utf-8"))
    if posted.get("confirm_digest", [""])[0] != planned:
        return web.render(
            request,
            "settings_confirm.html",
            {
                "reasons": check.confirm,
                "changes": cfg.describe_changes(check.changes, {}),
                "pairs": [],
                "file_hash": cfg.file_hash(text),
                "confirm_digest": planned,
                "confirm_action": "/settings/resume",
            },
        )
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=now)
    except cfg.SaveConflict:
        text, config = _read_config(web)
        changed = {"": "config.toml changed while saving. Nothing was saved; these are the values now."}
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)
    log.info("scheduled runs resumed from the web UI (backup %s)", backup.name)
    request.session["flash"] = f"Scheduled runs resumed. The previous file is {backup.name}."
    return RedirectResponse("/settings", status_code=303)


async def settings_cleanup(request: Request) -> Response:
    """Turn Clean up on or off (#148): `[prune] enabled`, through the same backed-up write as every
    other save, with no confirm - see `cfg.plan_cleanup`."""
    web = _web(request)
    posted = await _posted(request)
    now = web.now()
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)

    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        changed = {
            "": "config.toml changed since you opened this page - edited by hand, or saved from another tab. "
            "Nothing was saved; these are the values in the file now."
        }
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)

    enabled = posted.get("enabled", [""])[0] == "1"
    check = cfg.plan_cleanup(text.decode("utf-8"), enabled, base_dir=web.config_path.parent)
    if check.errors:
        context = _settings_context(web, text, config, errors=check.errors)
        return web.render(request, "settings.html", context, 400)
    if not check.changes:
        request.session["flash"] = "Nothing changed, so nothing was saved."
        return RedirectResponse("/settings", status_code=303)  # the flash is at the top
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=now)
    except cfg.SaveConflict:
        text, config = _read_config(web)
        changed = {"": "config.toml changed while saving. Nothing was saved; these are the values now."}
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)
    word = "on" if enabled else "off"
    log.info("Clean up turned %s from the web UI (backup %s)", word, backup.name)
    request.session["flash"] = f"Clean up is {word}. The previous file is {backup.name}."
    return RedirectResponse("/settings", status_code=303)  # the flash is at the top


def settings_schedule_preview(request: Request) -> Response:
    """GET /settings/schedule/preview: the live "Next fires" fragment, from the cron and timezone
    fields as typed - not the saved ones. Read-only: writes nothing, needs no `file_hash`, and
    never touches config.toml (issue #86). `hx-include` on the schedule form's own fields is what
    carries both values here on every keystroke."""
    web = _web(request)
    cron = request.query_params.get("schedule.cron", "")
    timezone = request.query_params.get("schedule.timezone", "")
    preview = cfg.preview_schedule(cron, timezone, now=web.now())
    return web.render(request, "_schedule_preview.html", {"schedule_preview": preview})


async def settings_schedule(request: Request) -> Response:
    """Edit `[schedule] cron` / `[schedule] timezone`: saves at once unless the new schedule fires
    more often than the old one, which gets the second confirm - see `cfg.plan_schedule`."""
    web = _web(request)
    posted = await _posted(request)
    now = web.now()
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)

    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        changed = {
            "": "config.toml changed since you opened this page - edited by hand, or saved from another tab. "
            "Nothing was saved; these are the values in the file now."
        }
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)

    cron = posted.get("schedule.cron", [""])[0]
    timezone = posted.get("schedule.timezone", [""])[0]
    # Off the event loop: `plan_schedule` walks the schedule's next fires over a 14-day window to
    # compare fire rates (`_fires_per_day`) - bounded and cheap since `MIN_SCHEDULE_INTERVAL_MINUTES`
    # caps it at a few hundred steps, but real file and TOML parsing work all the same.
    check = await anyio.to_thread.run_sync(
        lambda: cfg.plan_schedule(text.decode("utf-8"), cron, timezone, base_dir=web.config_path.parent, now=now)
    )
    if check.errors:
        context = _settings_context(web, text, config, errors=check.errors)
        return web.render(request, "settings.html", context, 400)
    if not check.changes:
        request.session["flash"] = "Nothing changed, so nothing was saved."
        return RedirectResponse("/settings", status_code=303)
    planned = cfg.file_hash(check.new_text.encode("utf-8"))
    if check.confirm and posted.get("confirm_digest", [""])[0] != planned:
        return web.render(
            request,
            "settings_confirm.html",
            {
                "reasons": check.confirm,
                "changes": cfg.describe_changes(check.changes, {}),
                "pairs": [("schedule.cron", cron), ("schedule.timezone", timezone)],
                "file_hash": cfg.file_hash(text),
                "confirm_digest": planned,
                "confirm_action": "/settings/schedule",
            },
        )
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=now)
    except cfg.SaveConflict:
        text, config = _read_config(web)
        changed = {"": "config.toml changed while saving. Nothing was saved; these are the values now."}
        return web.render(request, "settings.html", _settings_context(web, text, config, errors=changed), 409)
    changed = ", ".join(f"{c.section}.{c.key}" for c in check.changes)
    log.info("schedule saved from the web UI: %s (backup %s)", changed, backup.name)
    request.session["flash"] = f"Saved {changed}. The previous file is {backup.name}."
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- Spotify connect (#79)


SPOTIFY_DOCS = "https://github.com/sysdad/likearr/blob/main/docs/spotify.md"
"""Where the Spotify box sends a connect that fails."""


def _refused_on_last_run(config: Config, authorized_at: datetime | None) -> bool:
    """Whether the newest run failed because Spotify refused the stored refresh token
    (``invalid_grant``: revoked, or past its six months) after it was last authorized."""
    if not config.state_db.is_file():  # never create it: see healthz
        return False
    try:
        with SqliteState(config.state_db) as state:
            last = state.last_run()
    except Exception:  # a broken state database is Status's to report, not this line's
        return False
    if last is None or last.status is not RunStatus.ERROR or "invalid_grant" not in last.message:
        return False
    return authorized_at is None or last.ts > authorized_at.timestamp()


def _reauth_reason(reauth: ReauthView, *, has_token: bool, revoked: bool, needs_collaborative: bool) -> str:
    """Which one reason to re-authorize the Spotify box gives (#40), most urgent first; the
    template turns the key into its sentence. "switch-only" is the answer when nothing is wrong."""
    if not has_token:
        return "never"
    if revoked:
        return "revoked"
    if reauth.due is None:
        return "unknown-date"
    if reauth.days_left is not None and reauth.days_left < 0:
        return "overdue"
    if reauth.tone == "warn":
        return "due-soon"
    if needs_collaborative:
        return "collaborative"
    return "switch-only"


def spotify_status(config: Config, *, now: datetime) -> dict[str, Any]:
    """What the Settings page's Spotify panel shows: whether it can be connected at all, and -
    from the token file, without ever taking its lock - who it is connected as, the same
    authorization date, re-auth due date and scopes Status already reads, and the reason to
    re-authorize, if any."""
    try:
        configured = bool(config.spotify.client_id)
        client_id_error = ""
    except ConfigError as exc:
        configured = False
        client_id_error = str(exc)
    authorized_at = read_authorized_at(config.spotify.token_file)
    reauth = reauth_view(authorized_at, reauth_due(authorized_at) if authorized_at else None, now=now)
    granted = read_granted_scopes(config.spotify.token_file)
    has_token = authorized_at is not None or granted is not None
    # A token granted before likearr asked for playlist-read-collaborative (#103, item 3):
    # everything it did before still works; only playlists you collaborate on need a re-auth.
    needs_collaborative = lacks_collaborative(granted)
    revoked = has_token and _refused_on_last_run(config, authorized_at)
    return {
        "configured": configured,
        "client_id_error": client_id_error,
        "reauth": reauth,
        "account": read_account(config.spotify.token_file),
        "granted_scopes": sorted(granted) if granted else [],
        "has_token": has_token,
        # #161: a plain Re-authorize keeps write access the token already has; offer the opt-in only without it.
        "keeps_write": asks_for_write_scopes(config.spotify.token_file),
        "reason": _reauth_reason(reauth, has_token=has_token, revoked=revoked, needs_collaborative=needs_collaborative),
        "callback_mode": bool(config.ui.public_url),
        "public_url": config.ui.public_url,
        "redirect_uri": config.spotify.redirect_uri,
        "docs": SPOTIFY_DOCS,
    }


async def spotify_connect_start(request: Request) -> Response:
    """POST /settings/spotify/connect: begin the PKCE flow (paste-back, or direct-callback when
    `[ui] public_url` is https) and show its next step. See `web.spotify_connect`'s docstring for
    why the exchange this eventually leads to runs in-process rather than as a child job.

    Read scopes only by default (#161). A posted ``promote_save=1`` - the web UI's
    ``likearr auth --promote-save`` - adds the write scopes, as does a token that already has
    them (`asks_for_write_scopes`). The choice is recorded server-side with the attempt.

    promote-save follows Clean up's switch (#148): while `[prune] enabled` is off the box is not
    shown, and a posted ``promote_save=1`` is ignored. A token that already has write access keeps
    it on a re-auth either way - dropping it quietly would be its own surprise."""
    web = _web(request)
    promote_save = (await _posted(request)).get("promote_save", [""])[0] == "1"
    try:
        text, config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)
    promote_save = promote_save and config.prune.enabled
    try:
        _ = config.spotify.client_id  # raises ConfigError when unset; the value itself is unused here
    except ConfigError as exc:
        context = _settings_context(web, text, config)
        context["spotify_error"] = f"cannot connect Spotify: {exc}"
        return web.render(request, "settings.html", context, 400)

    callback_mode = bool(config.ui.public_url)
    redirect_uri = f"{config.ui.public_url}/spotify/callback" if callback_mode else None
    include_write = asks_for_write_scopes(config.spotify.token_file, promote_save=promote_save)
    try:
        url, verifier, state = await anyio.to_thread.run_sync(
            lambda: spotify_connect.build_authorize(
                config.spotify, redirect_uri=redirect_uri, include_write=include_write
            )
        )
    except SourceError as exc:
        context = _settings_context(web, text, config)
        context["spotify_error"] = f"could not start Spotify authorization: {exc}"
        return web.render(request, "settings.html", context, 400)

    web.spotify_pending.start(
        state=state,
        verifier=verifier,
        redirect_uri=redirect_uri or config.spotify.redirect_uri,
        mode="callback" if callback_mode else "paste",
        include_write=include_write,
    )
    log.info(
        "spotify authorization started from the web UI (%s mode, %s)",
        "callback" if callback_mode else "paste-back",
        "read and write scopes" if include_write else "read scopes only",
    )
    # Chromium and WebKit browsers check `form-action` on each redirect of a form submission,
    # against the page that submitted it, and drop a hop it does not allow without a word. So a 303
    # from here to Spotify works only when that page allowed Spotify and the `public_url` origin
    # Spotify may send the same navigation straight back to (#11): direct-callback mode, reached at
    # `public_url` itself. `web.render` widens those pages' `form-action` in exactly that case
    # (`CONNECT_FORM_PAGES`), and this answer carries the same policy. Anywhere else - paste-back
    # mode, or the UI opened at another address - the answer is a same-origin page with a plain
    # link to Spotify: a link click is not a form submission, so it works in every browser.
    one_click = spotify_connect.one_click_form_action(request.headers.get("host", ""), config.ui.public_url)
    if callback_mode and one_click:
        response = RedirectResponse(url, status_code=303)
        response.headers["content-security-policy"] = content_security_policy(form_action=one_click)
        return response
    context = _settings_context(web, text, config)
    context["spotify_continue_url" if callback_mode else "spotify_authorize_url"] = url
    return web.render(request, "settings.html", context)


async def _finish_spotify_auth(
    web: _Web, config: Config, *, returned_state: str, code: str
) -> str | spotify_connect.PendingSwitch:
    """Common to the paste-back finish and the direct-callback route: the single-use, server-side
    `state` `spotify_connect_start` minted - minted only behind the login gate, so only a session
    that was logged in when the flow started can ever hold a valid one - is the whole of the
    authorization here; there is no session binding to check on top of it (see
    `web.spotify_connect`'s docstring for why).

    Saves the new token when it belongs to the recorded Spotify account, or none is recorded yet
    (#40). Another account's token is held instead, and returned for the caller to ask the user
    about (`spotify_switch` saves it). Otherwise returns a message to show; never raises, and never
    includes the code, the verifier or a token in what it returns."""
    pending = web.spotify_pending.consume(returned_state) if returned_state else None
    if pending is None:
        return (
            "That Spotify authorization attempt has expired or was already used. Click Connect Spotify to start again."
        )
    if not code:
        return "Spotify did not send back an authorization code. Click Connect Spotify to start again."
    try:
        tokens = await anyio.to_thread.run_sync(
            spotify_connect.exchange, config.spotify, code, pending.verifier, pending.redirect_uri
        )
    except AccountRefused as exc:
        log.info("spotify authorization refused for this account: %s", exc)
        return (
            "Spotify won't let likearr use that account: an app in development mode only serves the accounts "
            "on its User Management list. Add the account there, then connect again. The current connection "
            "is unchanged."
        )
    except SourceError as exc:
        log.info("spotify authorization failed: %s", exc)
        if "GET /me" in str(exc):
            return (
                f"likearr could not check which Spotify account that is ({exc}), so nothing was saved. "
                "The current connection is unchanged; try again."
            )
        return (
            f"Spotify authorization failed: {exc}. Check that {pending.redirect_uri} is a redirect URI "
            f"in your Spotify app ({SPOTIFY_DOCS})."
        )
    new = tokens.account
    previous = read_account(config.spotify.token_file)
    if new is not None and previous is not None and previous.id != new.id:
        log.info("spotify authorization is for another account; waiting for the user to confirm the switch")
        return web.spotify_switches.hold(tokens, previous=previous, new=new, include_write=pending.include_write)
    await anyio.to_thread.run_sync(spotify_connect.save, config.spotify, tokens)
    return await _connected(web, config, tokens, include_write=pending.include_write)


async def _connected(web: _Web, config: Config, tokens: TokenSet, *, include_write: bool) -> str:
    """After a save: fetch playlist names if needed, and say what was granted."""
    log.info("spotify connected from the web UI: scopes %s", tokens.scope or "(none reported)")
    # A token now exists, so `names_needed` no longer holds this back (#118): with an empty names
    # cache, fetch them now rather than leaving Status without playlist names until the next check.
    # It also re-lists when the new token may read collaborative playlists the last listing greyed
    # out for want of the scope (#103, item 3), so the picker and a save stop refusing them.
    if web.settings.auto_fetch_names:
        await anyio.to_thread.run_sync(web.fetch_names_if_needed)
    granted = tokens.scope or "(none reported)"
    who = f" as {tokens.account.label}" if tokens.account else ""
    connected = f"Spotify connected{who}. Granted scopes: {granted}."
    # What this attempt asked for comes from the server-side pending entry, never the callback (#161).
    missing_write = sorted(set(SPOTIFY_WRITE_SCOPES) - set(tokens.scope.split()))
    if not missing_write:
        return connected
    if include_write:
        return (
            f"{connected} Spotify did not grant the write access promote-save needs (missing "
            f"{', '.join(missing_write)}), so promote-save will refuse until you re-authorize and approve it."
        )
    if not config.prune.enabled:  # promote-save is part of Clean up (#148): nothing to offer
        return connected
    return f"{connected} Read access only. To use promote-save, re-authorize with its write-access box ticked."


def _finished(web: _Web, request: Request, result: str | spotify_connect.PendingSwitch) -> Response:
    """A switch to confirm is its own page; a message is the flash on Settings."""
    if isinstance(result, spotify_connect.PendingSwitch):
        return web.render(request, "spotify_callback.html", {"switch": result})
    request.session["flash"] = result
    return RedirectResponse("/settings", status_code=303)


async def spotify_switch(request: Request) -> Response:
    """POST /settings/spotify/switch: the confirm for a token of another Spotify account (#40).
    Behind the login gate, unlike the callback that held it. ``confirmed=yes`` saves it; anything
    else drops it."""
    web = _web(request)
    posted = await _posted(request)
    switch = web.spotify_switches.consume(posted.get("switch", [""])[0])
    if switch is None:
        request.session["flash"] = (
            "That account switch has expired or was already used. Re-authorize Spotify to start again."
        )
        return RedirectResponse("/settings#spotify", status_code=303)
    if posted.get("confirmed", [""])[0] != "yes":
        request.session["flash"] = f"Still connected as {switch.previous.label}. Nothing was saved."
        return RedirectResponse("/settings#spotify", status_code=303)
    try:
        config = web.config()
    except ConfigError as exc:
        request.session["flash"] = f"config.toml does not load, so nothing was saved: {exc}"
        return RedirectResponse("/settings#spotify", status_code=303)
    current = read_account(config.spotify.token_file)
    if current is None or current.id != switch.previous.id:
        request.session["flash"] = (
            "The Spotify connection changed since you were asked, so nothing was saved. "
            "Re-authorize Spotify to try again."
        )
        return RedirectResponse("/settings#spotify", status_code=303)
    await anyio.to_thread.run_sync(spotify_connect.save, config.spotify, switch.tokens)
    log.info("spotify switched to another account from the web UI")
    request.session["flash"] = await _connected(web, config, switch.tokens, include_write=switch.include_write)
    return RedirectResponse("/settings#spotify", status_code=303)


async def spotify_connect_finish(request: Request) -> Response:
    """POST /settings/spotify/finish: the paste-back mode's redirect-URL field. Behind the login
    gate like every other Settings action; the `state` it carries still has to match a pending
    attempt (see `_finish_spotify_auth`)."""
    web = _web(request)
    posted = await _posted(request)
    try:
        config = web.config()
    except ConfigError as exc:
        request.session["flash"] = f"config.toml does not load: {exc}"
        return RedirectResponse("/settings", status_code=303)
    pasted = posted.get("redirect_url", [""])[0]
    try:
        code, returned_state = SpotifyAuth.parse_redirect_url(pasted)
    except SourceError as exc:
        request.session["flash"] = str(exc)
        return RedirectResponse("/settings", status_code=303)
    return _finished(web, request, await _finish_spotify_auth(web, config, returned_state=returned_state, code=code))


_OAUTH_ERROR_CODE = re.compile(r"[a-z_]{1,40}")
"""The shape of an OAuth 2.0 authorization error code; anything else on `/spotify/callback` is not echoed."""


async def spotify_callback(request: Request) -> Response:
    """GET /spotify/callback: the direct-callback mode's redirect target, and the one route
    exempted from the login gate (`auth._OPEN_PATHS`) - Spotify reaches it by a cross-site top-level
    GET redirect, which the `SameSite=Strict` session cookie (kept strict for every other route) is
    never sent on. Authorization instead comes entirely from the single-use, ten-minute,
    server-side `state` `spotify_connect_start` minted while the session *was* logged in - see
    `web.spotify_connect`'s docstring and `_finish_spotify_auth`.

    This handler never reads or writes `request.session`: with no cookie attached, Starlette hands
    it a fresh, empty session, and *writing* to it here would make the response set a brand-new,
    unauthenticated session cookie - silently logging the real session out the next time the
    browser sent both. The result is rendered directly instead (`spotify_callback.html`), with a
    plain link back to Settings for the user to click - a same-site navigation, which does carry
    the Strict cookie.
    """
    web = _web(request)
    try:
        config = web.config()
    except ConfigError as exc:
        # No login here, and in direct-callback mode the route faces the internet: the detail
        # (a quoted bad value, the config path) goes to the log only, as `/healthz` does (#171).
        log.warning("spotify callback: config.toml does not load: %s", exc)
        message = "likearr's configuration has a problem; log in to see it"
        return web.render(request, "spotify_callback.html", {"message": message}, 503)
    if not config.ui.public_url:
        return PlainTextResponse("direct-callback mode is not configured", status_code=404)
    params = request.query_params
    if "error" in params:
        web.spotify_pending.consume(params.get("state", ""))
        # This route needs no login, so the query string is anyone's text: only a standard OAuth
        # error code (RFC 6749 4.1.2.1, e.g. `access_denied`) is ever shown, never free text.
        error = params["error"]
        shown = error if _OAUTH_ERROR_CODE.fullmatch(error) else "an unrecognised error"
        return web.render(request, "spotify_callback.html", {"message": f"Spotify refused authorization: {shown}"})
    result = await _finish_spotify_auth(
        web, config, returned_state=params.get("state", ""), code=params.get("code", "")
    )
    if isinstance(result, spotify_connect.PendingSwitch):
        return web.render(request, "spotify_callback.html", {"switch": result})
    return web.render(request, "spotify_callback.html", {"message": result})


# ---------------------------------------------------------------- Lidarr setup + Doctor, in Settings (#80, #85)
#
# Both live in their own Settings section, each one htmx fragment (`_doctor.html`,
# `_lidarr_setup.html`) that polls every 2 seconds only while its job runs, and answers the poll
# that sees it finish with `POLL_STOP`. Without htmx, every route here lands back on the section.

SETUP_KINDS = ("lidarr-setup-preview", "lidarr-setup-apply")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _latest(jobs: Sequence[JobMeta], *kinds: str) -> JobMeta | None:
    return next((m for m in jobs if m.kind in kinds), None)


def _doctor_panel(web: _Web, meta: JobMeta | None, note: str = "") -> dict[str, Any]:
    """The Doctor section: the newest doctor job, if any, and its checks once it has finished.
    Reads only the job store - never config.toml or the state database - so it renders exactly
    when those are broken or not there yet."""
    view = doctor_view.parse_doctor_json(web.runner.output(meta.id)) if meta is not None and meta.finished else None
    return {"job": meta, "view": view, "note": note}


def _broken_settings(web: _Web, exc: Exception) -> dict[str, Any]:
    """Settings when config.toml does not load: no form, but Doctor still renders (#85)."""
    return {"config_error": str(exc), "doctor": _doctor_panel(web, _latest(web.runner.jobs(), "doctor"))}


def _setup_panel(web: _Web, meta: JobMeta | None, **extra: Any) -> dict[str, Any]:
    """The Lidarr setup section for its newest job: a preview (and its table once finished) or
    an apply (and its output once finished). `extra` is `confirm`, `note` or `recheck`.

    `library` is what the "Lidarr library" picker (#3) needs: the file's hash and the two
    `[lidarr]` keys a first start leaves unset, or ``None`` when config.toml does not load."""
    view = None
    output = ""
    if meta is not None and meta.finished:
        if meta.kind == "lidarr-setup-preview":
            view = lidarr_setup.parse_setup_profiles_json(web.runner.output(meta.id))
        else:
            # Shown in the browser, so with this process's secrets removed, as the job page does.
            output = web.runner.shown_output(meta.id).strip()
    try:
        text, config = _read_config(web)
    except (OSError, ConfigError, tomllib.TOMLDecodeError):
        library = None
    else:
        values = {key: getattr(config.lidarr, key) for key in cfg.LIBRARY_KEYS}
        library = {"file_hash": cfg.file_hash(text), **values, "unset": not all(values.values())}
    return {"job": meta, "view": view, "output": output, "library": library, **extra}


def _setup_answer(request: Request, web: _Web, panel: dict[str, Any]) -> Response:
    """htmx gets the section's fragment. A plain form post gets the confirm on the Settings page
    itself, or goes back to the section with any note as the flash."""
    if _is_htmx(request):
        return web.render(request, "_lidarr_setup.html", {"setup": panel})
    if panel.get("confirm"):
        try:
            context = _settings_context(web, *_read_config(web))
        except (ConfigError, tomllib.TOMLDecodeError) as exc:
            return web.render(request, "settings.html", _broken_settings(web, exc), 409)
        return web.render(request, "settings.html", {**context, "setup": panel})
    if panel.get("note"):
        request.session["flash"] = panel["note"]
    return RedirectResponse("/settings#lidarr-setup", status_code=303)


async def lidarr_setup_preview_start(request: Request) -> Response:
    """POST /settings/lidarr-setup/preview: `setup-profiles --json`, read-only. A preview already
    running is followed rather than refused - two tabs that both saw an apply finish both ask
    for the re-check."""
    web = _web(request)
    running = next((m for m in web.runner.jobs() if m.kind == "lidarr-setup-preview" and not m.finished), None)
    if running is not None:
        return _setup_answer(request, web, _setup_panel(web, running))
    try:
        meta = web.runner.start("lidarr-setup-preview", ["setup-profiles", "--json"], label="Preview Lidarr setup")
    except JobRefused as exc:
        latest = _latest(web.runner.jobs(), *SETUP_KINDS)
        return _setup_answer(request, web, _setup_panel(web, latest, note=f"could not preview Lidarr setup: {exc}"))
    return _setup_answer(request, web, _setup_panel(web, meta))


def lidarr_setup_poll(request: Request) -> Response:
    """GET /settings/lidarr-setup/{job_id}: the section's poll, for a preview or an apply. A
    plain visit (an old link to the #80 page) goes to the section, which shows the newest job.

    The poll that sees an apply finish cleanly answers with a read-only preview queued on
    `load` - the re-check. Only that poll: opening Settings later never starts one."""
    if not _is_htmx(request):
        return RedirectResponse("/settings#lidarr-setup", status_code=303)
    web = _web(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None or meta.kind not in SETUP_KINDS:
        return PlainTextResponse("no such job", status_code=404, headers={"HX-Redirect": "/settings"})
    recheck = meta.kind == "lidarr-setup-apply" and meta.state is JobState.DONE
    panel = _setup_panel(web, meta, recheck=recheck)
    return web.render(request, "_lidarr_setup.html", {"setup": panel}, POLL_STOP if meta.finished else 200)


async def lidarr_setup_apply(request: Request) -> Response:
    """POST /settings/lidarr-setup/{job_id}/apply: the second confirm, then `setup-profiles --apply`
    as a child job (issue #80's Requirements) - `lidarr_setup.APPLY_ARGV`, the same fixed argv
    every time, never built from anything the request carries. Nothing is started without
    ``confirmed=yes``, which only the inline "Yes, apply" button sends."""
    web = _web(request)
    posted = await _posted(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None or meta.kind != "lidarr-setup-preview":
        return PlainTextResponse("no such preview", status_code=404, headers={"HX-Redirect": "/settings"})
    panel = _setup_panel(web, meta)
    view = panel["view"]
    if view is None or view.error or not view.needs_apply:
        note = "Nothing to apply." if view is not None and not view.error else "Preview it again first."
        return _setup_answer(request, web, {**panel, "note": note})
    if posted.get("confirmed", [""])[0] != "yes":
        return _setup_answer(request, web, {**panel, "confirm": True})
    try:
        started = web.runner.start("lidarr-setup-apply", lidarr_setup.APPLY_ARGV, label="Apply Lidarr setup")
    except JobRefused as exc:
        return _setup_answer(request, web, {**panel, "note": f"could not apply the Lidarr setup: {exc}"})
    log.info("lidarr setup apply started from the web UI (job %s)", started.id)
    return _setup_answer(request, web, _setup_panel(web, started))


def _library_choices(web: _Web) -> lidarr_setup.LidarrSetupView | None:
    """The newest finished preview's view, whose lists are the only values a pick may take."""
    meta = next((m for m in web.runner.jobs() if m.kind == "lidarr-setup-preview" and m.finished), None)
    return lidarr_setup.parse_setup_profiles_json(web.runner.output(meta.id)) if meta is not None else None


async def lidarr_library(request: Request) -> Response:
    """POST /settings/lidarr-library: save the root folder and quality profile picked from Lidarr's
    own lists (#3), through the same backed-up write as every other save. A value must be one the
    newest preview listed, so the form can only ever write a name Lidarr has."""
    web = _web(request)
    posted = await _posted(request)
    try:
        text, _config = _read_config(web)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        return web.render(request, "settings.html", _broken_settings(web, exc), 409)
    if posted.get("file_hash", [""])[0] != cfg.file_hash(text):
        request.session["flash"] = "config.toml changed since you opened this page, so nothing was saved. Pick again."
        return RedirectResponse("/settings#lidarr-setup", status_code=303)
    view = _library_choices(web)
    allowed = {
        "root_folder": view.root_folders if view is not None else (),
        "quality_profile": view.quality_profiles if view is not None else (),
    }
    chosen = {key: posted.get(key, [""])[0] for key in cfg.LIBRARY_KEYS}
    if any(value and value not in allowed[key] for key, value in chosen.items()):
        request.session["flash"] = "That is not one of Lidarr's choices any more. Preview Lidarr setup again."
        return RedirectResponse("/settings#lidarr-setup", status_code=303)
    check = cfg.plan_library(text.decode("utf-8"), chosen, base_dir=web.config_path.parent)
    if check.errors or not check.changes:
        request.session["flash"] = "; ".join(check.errors.values()) or "Nothing changed, so nothing was saved."
        return RedirectResponse("/settings#lidarr-setup", status_code=303)
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=web.now())
    except cfg.SaveConflict:
        request.session["flash"] = "config.toml changed while saving, so nothing was saved. Pick again."
        return RedirectResponse("/settings#lidarr-setup", status_code=303)
    saved = ", ".join(f"{c.key.replace('_', ' ')} {c.new}" for c in check.changes)
    log.info("lidarr library set from the web UI: %s (backup %s)", saved, backup.name)
    request.session["flash"] = f"Saved: {saved}. The previous file is {backup.name}."
    return RedirectResponse("/settings#lidarr-setup", status_code=303)


def _after_setup_preview(web: _Web, meta: JobMeta) -> None:
    """A preview's after-callback (#3): with no root folder chosen and exactly one in Lidarr, use
    it - there is nothing to choose between. Written like any Settings save, backup included. The
    quality profile is always picked by hand: Lidarr ships several."""
    view = lidarr_setup.parse_setup_profiles_json(web.runner.output(meta.id))
    if view is None or view.error or len(view.root_folders) != 1:
        return
    try:
        text, config = _read_config(web)
    except (OSError, ConfigError, tomllib.TOMLDecodeError):
        return
    if config.lidarr.root_folder:
        return
    check = cfg.plan_library(
        text.decode("utf-8"), {"root_folder": view.root_folders[0]}, base_dir=web.config_path.parent
    )
    if check.errors or not check.changes:
        return
    try:
        backup = cfg.write_config(web.config_path, check.new_text, expected_hash=cfg.file_hash(text), now=web.now())
    except (cfg.SaveConflict, OSError) as exc:
        log.warning("could not save Lidarr's only root folder as [lidarr] root_folder: %s", exc)
        return
    log.info("[lidarr] root_folder set to Lidarr's only root folder %s (backup %s)", view.root_folders[0], backup.name)


def preview_setup_if_needed(web: _Web) -> None:
    """At start (#3): with the Lidarr URL set but a root folder or quality profile not chosen, ask
    Lidarr for its lists now, so Settings has them and a single root folder is taken by itself."""
    try:
        lidarr = web.config().lidarr
    except ConfigError:
        return
    if not lidarr.url or (lidarr.root_folder and lidarr.quality_profile):
        return
    try:
        web.runner.start("lidarr-setup-preview", ["setup-profiles", "--json"], label="Preview Lidarr setup")
    except JobRefused as exc:
        log.info("Lidarr setup preview not started at start: %s", exc)


async def doctor_start(request: Request) -> Response:
    """POST /doctor: `doctor --json`, read-only. Only this button runs it - never opening
    Settings - since doctor calls Spotify and its quota has run out before."""
    web = _web(request)
    note = ""
    try:
        meta: JobMeta | None = web.runner.start("doctor", ["doctor", "--json"], label="Doctor checks")
    except JobRefused as exc:
        note = f"could not run doctor: {exc}"
        meta = _latest(web.runner.jobs(), "doctor")
    if not _is_htmx(request):
        if note:
            request.session["flash"] = note
        return RedirectResponse("/settings#doctor", status_code=303)
    return web.render(request, "_doctor.html", {"doctor": _doctor_panel(web, meta, note)})


def doctor_redirect(request: Request) -> Response:
    """GET /doctor: Doctor is a Settings section now (#85); old links land on it."""
    return RedirectResponse("/settings#doctor", status_code=303)


def doctor_poll(request: Request) -> Response:
    """GET /settings/doctor/{job_id}: the Doctor section's poll."""
    if not _is_htmx(request):
        return RedirectResponse("/settings#doctor", status_code=303)
    web = _web(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None or meta.kind != "doctor":
        return PlainTextResponse("no such job", status_code=404, headers={"HX-Redirect": "/settings"})
    status_code = POLL_STOP if meta.finished else 200
    return web.render(request, "_doctor.html", {"doctor": _doctor_panel(web, meta)}, status_code)


def _selection(web: _Web, params: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The playlists the form holds right now, carried by the picker itself; the file's otherwise."""
    if "picker" in params:
        return tuple(dict.fromkeys(v.strip() for v in params.get("spotify.playlists", []) if v.strip()))
    return web.config().spotify.playlists


async def playlists_refresh(request: Request) -> Response:
    """Follow or reuse the latest playlists fetch, or start one. Returns the picker fragment."""
    web = _web(request)
    form = await request.form(max_files=0)
    selected = _selection(web, {k: [v for v in form.getlist(k) if isinstance(v, str)] for k in form})
    names = web.playlist_names()
    picker = _picker_for(web, selected, web.runner.jobs(), names)
    if picker is None:
        try:
            meta = web.runner.start("playlists", ["playlists", "--json"], label="Spotify playlists")
        except JobRefused as exc:
            picker = _picker(selected, None, f"Could not ask Spotify for your playlists ({exc}).", names=names)
        else:
            picker = _picker(selected, None, _ASKING, names=names, poll=meta.id)
    return web.render(request, "_picker.html", {"picker": picker})


def playlists_poll(request: Request) -> Response:
    web = _web(request)
    meta = web.runner.get(request.path_params["job_id"])
    if meta is None or meta.kind != "playlists":
        return PlainTextResponse("no such job", status_code=404, headers={"HX-Redirect": "/settings"})
    selected = _selection(web, {k: request.query_params.getlist(k) for k in request.query_params})
    names = web.playlist_names()
    if not meta.finished:
        picker = _picker(selected, None, _ASKING, names=names, poll=meta.id)
        return web.render(request, "_picker.html", {"picker": picker})
    fetched = _parse_playlists(web.runner.output(meta.id)) if meta.state is JobState.DONE else None
    if fetched is not None:
        picker = _picker(selected, fetched, names=names)
    else:
        picker = _picker(selected, None, _UNANSWERED, names=names, log_job=meta.id)
    return web.render(request, "_picker.html", {"picker": picker}, POLL_STOP)


ROUTES: list[Route] = [
    Route("/settings", settings_page, methods=["GET"]),
    Route("/settings", settings_save, methods=["POST"]),
    Route("/settings/pause", settings_pause, methods=["POST"]),
    Route("/settings/resume", settings_resume, methods=["POST"]),
    Route("/settings/cleanup", settings_cleanup, methods=["POST"]),
    Route("/settings/schedule", settings_schedule, methods=["POST"]),
    Route("/settings/schedule/preview", settings_schedule_preview, methods=["GET"]),
    Route("/settings/playlists", playlists_refresh, methods=["POST"]),
    Route("/settings/playlists/{job_id}", playlists_poll, methods=["GET"]),
    Route("/settings/spotify/connect", spotify_connect_start, methods=["POST"]),
    Route("/settings/spotify/finish", spotify_connect_finish, methods=["POST"]),
    Route("/settings/spotify/switch", spotify_switch, methods=["POST"]),
    Route("/spotify/callback", spotify_callback, methods=["GET"]),
    Route("/settings/lidarr-setup/preview", lidarr_setup_preview_start, methods=["POST"]),
    Route("/settings/lidarr-library", lidarr_library, methods=["POST"]),
    Route("/settings/lidarr-setup/{job_id}", lidarr_setup_poll, methods=["GET"]),
    Route("/settings/lidarr-setup/{job_id}/apply", lidarr_setup_apply, methods=["POST"]),
    Route("/doctor", doctor_redirect, methods=["GET"]),
    Route("/doctor", doctor_start, methods=["POST"]),
    Route("/settings/doctor/{job_id}", doctor_poll, methods=["GET"]),
]
"""In `create_app`'s order: Starlette matches the first route that fits."""

AFTER: dict[str, AfterCallback] = {"lidarr-setup-preview": _after_setup_preview}
"""This module's after-callbacks by job kind, which `create_app` passes to `_Web`."""
