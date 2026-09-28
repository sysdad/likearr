"""The web app's shared state: `WebSettings`, `_Web` (settings, the job runner, templates) and
`_web`, which every route module reaches it through.

Split out of `likearr.web.app` so the route modules in `likearr.web.routes` can share it
without importing `app`, which imports them. It imports `helpers`; `helpers` never imports it
at run time.
"""

from __future__ import annotations

import functools
import logging
import secrets
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.templating import Jinja2Templates

from likearr import __version__, commit
from likearr.adapters.spotify import can_read_collaborative, read_granted_scopes
from likearr.adapters.state_sqlite import SqliteState
from likearr.config import Config, ConfigError, load_config
from likearr.playlist_names import NamesCache, names_path, read_names, write_names
from likearr.shell.last_run import LastRun, read_last_run
from likearr.web import prune, spotify_connect
from likearr.web.auth import LoginLimiter, content_security_policy
from likearr.web.helpers import _names_of, _needs_reauth_ids_of, _not_owned_ids_of, _parse_playlists, _playlist_jobs
from likearr.web.jobs import JOB_PHASE_APPLY, JobMeta, JobRefused, JobRunner, JobState
from likearr.web.plans import EXPIRE_AFTER, plan_token_of_file
from likearr.web.schedule import SCHEDULED_KIND
from likearr.web.status import ago, change_summary, short_message

log = logging.getLogger("likearr.web.app")
"""Under the app's own name, so log lines read as they did before the split."""


_HERE = Path(__file__).parent

POLL_STOP = 286
"""htmx's "stop polling" status: a finished job's fragment returns it and the page stops asking."""

PRUNE_KEEP = timedelta(days=30)
"""How long a prune report, and the review decided beside it, outlives the job store's newest 20 -
counted from its last use (the latest decision or export), not from when it was built."""


STOP_GRACE_PERIOD_S = 30 * 60
"""The compose ``stop_grace_period`` (deploy/compose.example.yaml): 30 minutes, because
an apply that adds many artists waits up to `[lidarr] refresh_timeout_s` (300 s) for each one's
RefreshArtist and can run past ten. Keep the two in step."""

SHUTDOWN_MARGIN_S = 2 * 60
"""Left of the grace period for uvicorn's own shutdown once the apply has finished."""


@dataclass(slots=True)
class WebSettings:
    config_path: Path
    password: str
    cli: Sequence[str] | None = None
    """How a job starts likearr. Default: this interpreter, ``-m likearr.shell.cli -c <config>``."""
    session_secret: str | None = None
    """Signing key for the session cookie. Default: a fresh random key, so a restart logs out."""
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    limiter: LoginLimiter | None = None
    auto_fetch_names: bool = False
    """Fetch playlist names by themselves when they are needed (see `_Web.fetch_names_if_needed`):
    at start, after a check, after a settings save. `likearr start` turns it on; tests turn it on
    only where they test it, so a job they start next never finds the slot taken."""
    auto_count_files: bool = False
    """Count the files on disk behind a finished check's unmonitors, in a read-only child job
    (`likearr lidarr-files`, see `routes.plans.count_files`). `likearr start` turns it on; tests only
    where they test it, for the same reason as `auto_fetch_names`."""
    auto_preview_prune: bool = False
    """Start Clean up's read-only previews when a review is exported (`routes.cleanup.preview_prune`).
    `likearr start` turns it on; tests only where they test it, for the same reason."""
    auto_preview_setup: bool = False
    """Preview the Lidarr setup at start while a root folder or quality profile is not chosen yet
    (`routes.settings.preview_setup_if_needed`). `likearr start` turns it on; tests only where
    they test it, for the same reason as `auto_fetch_names`."""
    scheduler: bool = False
    """Start the in-service scheduler (`likearr.web.schedule.Scheduler`) in the lifespan. `likearr
    start` turns it on; tests only where they test it, so a background thread computing `next_fire`
    is never running behind a test that never asked for it."""
    workers: int = 1
    reload: bool = False
    """Passed to `assert_single_worker` when `scheduler` is on: `likearr start` never sets either
    away from its default today, and these exist only so that changes here without updating the
    scheduler for it (see `likearr.web.schedule`'s docstring) fail loudly."""
    shutdown_timeout_s: float = STOP_GRACE_PERIOD_S - SHUTDOWN_MARGIN_S
    """How long shutdown waits for a job that must finish (an apply): the compose
    ``stop_grace_period`` less a margin for the server's own shutdown."""


AfterCallback = Callable[["_Web", JobMeta], None]
"""A feature module's after-callback (`likearr.web.routes.*.AFTER`): the app's `_Web`, then the
finished job."""


class _Web:
    """The app's shared state: settings, the job runner, templates."""

    def __init__(self, settings: WebSettings, config: Config, *, after: Mapping[str, AfterCallback]) -> None:
        """`after`: the feature modules' after-callbacks by job kind (`create_app` passes each
        route module's `AFTER`), called with this `_Web` and the finished job once its slot is
        free - see `JobRunner`'s own `after`."""
        self.settings = settings
        self.config_path = settings.config_path
        cli = list(settings.cli or [sys.executable, "-m", "likearr.shell.cli", "-c", str(settings.config_path)])
        self.names_file = names_path(settings.config_path)
        self._last_run_lock = threading.Lock()
        self._last_run_cache: tuple[tuple[str, int, int, int], LastRun | None] | None = None
        self.runner = JobRunner(
            settings.config_path.parent / "ui" / "jobs",
            cli,
            lock_path=config.lock_path,
            now=settings.now,
            keep=self._keep_job,
            # The feature modules' callbacks, each bound to this `_Web`; the scheduled run's is the
            # app's own and always wins.
            after={
                **{kind: functools.partial(callback, self) for kind, callback in after.items()},
                SCHEDULED_KIND: self._after_scheduled,
            },
            finish_hooks={
                "playlists": self._cache_playlist_names,
                # A plan's token is recorded as it finishes, from the file it just wrote (see plan_token).
                "plan": lambda job_dir: {"plan_token": plan_token_of_file(job_dir.name, job_dir / "diff.json")},
            },
        )
        # Not `or`: a limiter with no failures recorded is falsy (it has a length).
        self.limiter = settings.limiter if settings.limiter is not None else LoginLimiter()
        self.spotify_pending = spotify_connect.PendingSpotifyAuthStore(now=lambda: settings.now().timestamp())
        self.spotify_switches = spotify_connect.PendingSwitchStore(now=lambda: settings.now().timestamp())
        """Server-side PKCE state for "Connect Spotify": see `spotify_connect`."""
        self.templates = Jinja2Templates(directory=str(_HERE / "templates"))
        self.templates.env.filters["when"] = self._when
        self.templates.env.filters["fire_when"] = self._fire_when
        self.templates.env.filters["human_bytes"] = prune.human_bytes
        self.templates.env.filters["oom_note"] = oom_note
        self.templates.env.filters["short_message"] = short_message
        self.templates.env.filters["change_summary"] = change_summary
        self.templates.env.filters["job_title"] = lambda kind: _JOB_TITLES.get(kind, str(kind).capitalize())
        # The footer (base.html): version never changes without a restart, and neither does the
        # commit baked in at image build time, so both are resolved once here rather than per
        # request.
        self.templates.env.globals["version"] = __version__
        self.templates.env.globals["commit"] = commit()
        self._nav_running_template = self.templates.get_template("_nav_running.html")
        """Resolved once: every htmx fragment response re-renders it (see `render`), so a per-request
        template lookup would otherwise run on every poll."""
        self.generation = secrets.token_hex(8)
        """The current session generation (see `auth.GENERATION_KEY`). In memory, so a restart
        also ends every session - as the fresh signing key already does."""

    def _cache_playlist_names(self, job_dir: Path) -> Mapping[str, str]:
        """A playlists job's finish hook: merge its answer into the names file. Runs only for a job
        that succeeded, so a failed refresh leaves the names as they were."""
        fetched = _parse_playlists((job_dir / "out.txt").read_text(encoding="utf-8", errors="replace"))
        if fetched is not None:
            write_names(
                self.names_file,
                _names_of(fetched),
                fetched_at=self.now(),
                not_owned=_not_owned_ids_of(fetched),
                needs_reauth=_needs_reauth_ids_of(fetched),
            )
        return {}

    def last_run(self, path: Path) -> LastRun | None:
        """`read_last_run`, parsed once per version of the file and kept as one copy: it is several
        MB, rewritten once a run, and read by every Status load and every Explain. The file is
        replaced, never edited, so a new one has a new inode, mtime or size and is read afresh."""
        try:
            info = path.stat()
        except OSError:
            return None
        stamp = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
        with self._last_run_lock:
            if self._last_run_cache is not None and self._last_run_cache[0] == stamp:
                return self._last_run_cache[1]
            # Parsed under the lock: two requests arriving together must not hold two copies.
            last = read_last_run(path)
            self._last_run_cache = (stamp, last)
            return last

    def _keep_job(self, meta: JobMeta) -> bool:
        """Past the newest jobs, a plan that could still be applied is kept: every reviewable plan
        is younger than `EXPIRE_AFTER`, so none of them is pruned from under a later apply. A
        prune report under review is kept for `PRUNE_KEEP`: its decisions live beside it."""
        keep_for = {
            "plan": EXPIRE_AFTER,
            "files": EXPIRE_AFTER,
            "prune": PRUNE_KEEP,
            # A preview's Spotify plan is what the checklist's `promote-save --apply` applies.
            "prune-preview": PRUNE_KEEP,
            "spotify-preview": PRUNE_KEEP,
            "prune-checks": PRUNE_KEEP,
        }.get(meta.kind)
        if keep_for is None or not meta.finished_at:
            return False
        last_use = datetime.fromisoformat(meta.finished_at)
        if meta.kind == "prune":
            # Counted from the last decision or export, not from when the report was built: a
            # review that takes weeks is never pruned from under the person doing it.
            for name in ("prune-draft.json", "decisions.json"):
                path = self.runner.job_file(meta.id, name)
                if path is not None:
                    try:
                        last_use = max(last_use, datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
                    except OSError:
                        continue
        return self.now() - last_use <= keep_for

    def names_needed(self) -> bool:
        """The names file is missing, or lacks a playlist the config names: "load on the first sync,
        then persist". A refresh of names already known is the Settings button's, never automatic.

        False with no Spotify token file: fetching playlist names needs one, and without it
        the job would only fail with "no token file ... - run `likearr auth` first". Checked here,
        once, so startup, a finished check and a saved settings form never start that doomed job.

        Also true when the names file still marks playlists as needing a re-authorization and the
        token now has ``playlist-read-collaborative``."""
        try:
            config = self.config()
        except ConfigError:
            return False
        if not config.spotify.token_file.exists():
            return False
        cache = read_names(self.names_file)
        if not cache.names:
            return True
        if any(pid not in cache.names for pid in config.spotify.playlists):
            return True
        # The last listing greyed out collaborative playlists for a token without
        # playlist-read-collaborative. Once a re-authorization (web or `likearr auth`) grants it,
        # that answer is stale: re-list, or the picker and a save keep refusing them.
        return bool(cache.needs_reauth) and can_read_collaborative(read_granted_scopes(config.spotify.token_file))

    def _after_scheduled(self, meta: JobMeta) -> None:
        """After a scheduled job settles: a redeploy that cancelled it while it was still planning
        marks its fire cancelled, so the missed-fire catch-up re-fires it after
        restart. Any other outcome - done, failed, guarded, stale, busy, or interrupted after it
        had already printed the apply-phase marker - leaves the fire recorded as serviced."""
        if meta.state is not JobState.INTERRUPTED or meta.phase == JOB_PHASE_APPLY:
            return
        try:
            config = load_config(self.config_path)
            with SqliteState(config.state_db) as state:
                state.mark_scheduled_fire_cancelled()
        except (ConfigError, OSError, sqlite3.Error):
            log.exception(
                "job %s: could not mark its fire cancelled; the missed-fire catch-up may not re-fire it", meta.id
            )

    def fetch_names_if_needed(self, _meta: JobMeta | None = None) -> None:
        """Start a `playlists --json` job when names are needed and nothing else runs. Called after
        a check finishes, after a settings save, and once at start - never from a GET, and never on
        the cron's `run`, which stays free of any extra Spotify call."""
        if not self.names_needed():
            return
        try:
            self.runner.start("playlists", ["playlists", "--json"], label="Spotify playlist names")
        except JobRefused as exc:
            log.info("playlist names are needed but not fetched now: %s", exc)

    def playlist_names(self) -> NamesCache:
        """The names file; before it exists, the newest successful playlists job's answer, so the
        names a job fetched before the file was introduced are not lost to an upgrade."""
        cache = read_names(self.names_file)
        if cache.names:
            return cache
        for meta in _playlist_jobs(self.runner.jobs()):
            if meta.state is JobState.DONE and meta.finished_at:
                names = _names_of(_parse_playlists(self.runner.output(meta.id)) or {"playlists": []})
                return NamesCache(names, datetime.fromisoformat(meta.finished_at)) if names else cache
        return cache

    def end_sessions(self) -> None:
        """Log out every session: the cookies stay signed, but name a generation that has gone."""
        self.generation = secrets.token_hex(8)

    def config(self) -> Config:
        """Read fresh on every request: a settings save, or a hand edit, shows on the next page."""
        return load_config(self.config_path)

    def cleanup_enabled(self) -> bool:
        """`[prune] enabled`: whether Clean up and promote-save's Spotify write access are
        offered. Read fresh, so the Settings switch shows on the next page. `False` when config.toml
        does not load: off is the default, and the page's own `config_error` handling says why."""
        try:
            return self.config().prune.enabled
        except ConfigError:
            return False

    @property
    def tz(self) -> ZoneInfo:
        """`[schedule] timezone`, read fresh every time, not cached from startup: Settings can
        change it live, and a zone cached at startup would show Status's next
        fire and every rendered time in the old zone until the next restart."""
        try:
            return ZoneInfo(self.config().schedule.timezone)
        except ConfigError:
            return ZoneInfo("UTC")

    def now(self) -> datetime:
        return self.settings.now()

    def _when(self, value: datetime | str | None) -> str:
        if value is None or value == "":
            return ""
        when = datetime.fromisoformat(value) if isinstance(value, str) else value
        local = when.astimezone(self.tz)
        return f"{local:%a %d %b %H:%M} {local.tzname()} ({ago(self.now(), when)})"

    def _fire_when(self, value: datetime) -> str:
        """Like `_when`, but shown in `value`'s own timezone rather than `self.tz`: a schedule
        preview's fires (`cfg.preview_schedule`) may be for a timezone typed into the form and not
        yet saved, which `self.tz` (read from config.toml) would otherwise silently override."""
        return f"{value:%a %d %b %H:%M} {value.tzname()} ({ago(self.now(), value)})"

    def render(self, request: Request, name: str, context: Mapping[str, Any], status_code: int = 200) -> Response:
        """Render a page, or a fragment (a template named ``_*``) for htmx to swap in.

        Only a full page takes the one-shot flash message: a fragment never shows it, so another
        tab's 2-second poll would otherwise swallow a "Saved" meant for the page being loaded.

        A fragment answering an htmx request also carries an out-of-band copy of the nav's running
        pill (``_nav_running.html``, the same OOB-partial convention as ``_prune_summary.html`` /
        ``_prune_export_status.html``), rendered fresh from ``runner.current()`` here in the one
        shared path every poll goes through - the job fragment, Doctor, Lidarr setup, playlists and
        Clean up - so a run finishing on any of them clears the pill everywhere, not just on the
        page that started it.

        ``has_mqtt`` goes into every render, page or fragment: it gates the Home Assistant wording
        wherever it appears, including fragments included from a page that never asked
        for it directly (Jinja includes inherit the caller's context). ``cleanup_enabled``
        goes in the same way and gates the nav's Clean up link. Both are `False` when config.toml
        does not load - the page's own `config_error` handling already says why.

        The pages that can hold a form posting to ``/settings/spotify/connect`` (`CONNECT_FORM_PAGES`)
        widen `form-action` when one-click Connect applies (see
        `spotify_connect.one_click_form_action`); every other response keeps the default policy.
        """
        try:
            config: Config | None = self.config()
        except ConfigError:
            config = None
        base: dict[str, Any] = {
            "has_mqtt": config is not None and config.health.mqtt is not None,
            "cleanup_enabled": config is not None and config.prune.enabled,
        }
        if not name.startswith("_"):
            base["job"] = self.runner.current()
            base["flash"] = request.session.pop("flash", None)
        if not (name.startswith("_") and request.headers.get("HX-Request") == "true"):
            response: Response = self.templates.TemplateResponse(
                request, name, {**base, **context}, status_code=status_code
            )
        else:
            # An htmx fragment: render it and its out-of-band nav copy together, the same way
            # `TemplateResponse` itself renders (`request` defaulted into the context), into one
            # response - never patch a rendered response's body and re-derive Content-Length by hand.
            merged: dict[str, Any] = {**base, **context}
            merged.setdefault("request", request)
            html = self.templates.get_template(name).render(merged)
            html += self._nav_running_template.render(job=self.runner.current(), oob=True)
            response = HTMLResponse(html, status_code=status_code)
        if name in CONNECT_FORM_PAGES and config is not None:
            one_click = spotify_connect.one_click_form_action(request.headers.get("host", ""), config.ui.public_url)
            if one_click:
                response.headers["content-security-policy"] = content_security_policy(form_action=one_click)
        return response


CONNECT_FORM_PAGES = frozenset({"settings.html", "prune_review.html", "_prune_finish.html"})
"""The templates that can hold a form posting to ``/settings/spotify/connect``: Settings'
Connect Spotify, and Clean up's "Authorize write access on Spotify" in `_prune_finish.html`,
included by `prune_review.html`. It is the page holding the form whose `form-action` the browser
checks. The fragment is listed for a direct load, where it is the page; under htmx its header is
never applied to the page it is swapped into, which carries its own."""


def _web(request: Request) -> _Web:
    return request.app.state.web


KILLED_EXIT = -9
"""A child the kernel killed (SIGKILL) that nobody asked to stop: in the container, the memory limit."""


def oom_note(meta: JobMeta) -> str:
    """What a job the system killed says instead of "failed (exit -9)"; ``""`` for any other job.

    A cancel that escalated to SIGKILL is recorded as cancelled and a shutdown's as interrupted,
    so a failed -9 is one nobody asked for - in the container, the memory limit (the OOM killer).
    An apply is the one job that may have changed something before it was stopped, and so is a
    scheduled run (`SCHEDULED_KIND`) that had already printed `PHASE_MARKER_APPLY` when it was
    killed - `JobRunner` stores that as `meta.phase == JOB_PHASE_APPLY` on the finished job, SIGKILL
    included, so no log scan is needed here.
    """
    if meta.state is not JobState.FAILED or meta.exit_code != KILLED_EXIT:
        return ""
    if meta.kind == "apply" or (meta.kind == SCHEDULED_KIND and meta.phase == JOB_PHASE_APPLY):
        return (
            "This ran out of memory (the container's limit) and was stopped partway, so Lidarr may be "
            "partly changed. Check for changes again."
        )
    return "This ran out of memory (the container's limit) and was stopped. Nothing was changed."


_JOB_TITLES = {
    "prune": "Find unneeded albums",
    "plan": "Check for changes",
    "apply": "Apply changes",
    "explain": "Look up",
    "playlists": "Spotify playlists",
    "files": "Count files on disk",
    "prune-preview": "Preview the clean up",
    "spotify-preview": "Preview Spotify changes",
    "prune-checks": "Check Lidarr",
    "doctor": "Doctor checks",
    "lidarr-setup-preview": "Preview Lidarr setup",
    "lidarr-setup-apply": "Apply Lidarr setup",
    SCHEDULED_KIND: "Scheduled run",
}
"""What each kind of job is called on a page: the words the navigation uses, never the CLI's."""
