"""The web UI's job runner: long work runs as a child `likearr` process, never in the server.

Why a child process and not a thread:

- **The CLI stays the single implementation by construction.** A job is exactly the command a
  human would type, so there is no second call path into `plan` or `apply` that could drift.
- **Isolation.** A plan holds the whole library in memory and talks to three flaky services. A
  hang or an out-of-memory kill takes the job with it, not the admin page.
- **Cancellation is real.** SIGTERM to a child works; killing a thread mid-HTTP-call does not.
- **Every Spotify token refresh stays in the CLI**, under the token file's own lock.

Each job is a directory, not a database row, so a job survives a restart and can be read from
the terminal when something goes wrong::

    <root>/2026-09-22T14-03-11Z-a1b2c3/
        meta.json   kind, argv, label, started_at, finished_at, exit_code, state, drain
        log.txt     the child's stderr, tailed live by the job page
        out.txt     the child's stdout: the answer, and a run's health JSON line

The rules the design asks for, all enforced here rather than by the pages that call it:

- job ids are checked against their exact generated shape before any path is built from one;
- arguments are a list and no shell is involved; free text goes after ``--``;
- one job at a time, refused before anything is spawned;
- a job that runs `likearr run` checks the run lock without waiting, and releases it at once,
  before spawning - so a cron fire in progress is a friendly "try again", not a child that
  publishes a lock error to Home Assistant;
- a `scheduled` job (`submit_scheduled`) queues behind whatever holds the one job slot for up to
  an hour rather than being refused outright, then gives up and is recorded `skipped` - the
  in-service scheduler's fire, and "Run now", both go through it;
- a job still marked running when the server starts is marked ``interrupted`` - unless its process
  is provably still alive and still that job (same PID, same kernel start time, same argv; Linux
  ``/proc``), when it is re-adopted: watched until it ends, and no other job starts meanwhile;
- on shutdown, new jobs are refused, a job that must not be cut short (an apply) is waited for,
  and anything else is stopped;
- the newest `KEEP_JOBS` job directories are kept, pruned each time a job starts - after it has
  started, and never a plan that may still be applied (`keep`).

Standard library only, so this module is importable - and testable - on its own, without the rest
of the web stack.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from likearr.adapters.http import redact, redact_literals
from likearr.adapters.lock import LockHeld, run_lock
from likearr.adapters.spotify import TOKEN_REQUEST_WORST_CASE_S
from likearr.config import UI_PASSWORD_ENV
from likearr.fsio import write_atomic
from likearr.models import EXIT_BUSY, PHASE_MARKER_APPLY

__all__ = [
    "JOB_PHASE_APPLY",
    "KEEP_JOBS",
    "PASSWORD_ENV",
    "QUEUE_WAIT_S",
    "SCHEDULED_KIND",
    "STOP_WAIT_S",
    "JobMeta",
    "JobRefused",
    "JobRunner",
    "JobState",
    "last_json_object",
    "new_job_id",
    "state_for_exit",
    "still_running",
    "valid_job_id",
]

log = logging.getLogger(__name__)

KEEP_JOBS = 20

PASSWORD_ENV = UI_PASSWORD_ENV
"""Stripped from every child's environment: no child has any use for the UI's password."""

SCHEDULED_KIND = "scheduled"
"""The job kind a scheduler fire - or "Run now" - is submitted as (`likearr.web.schedule`). The
only kind whose shutdown behaviour depends on its `phase` rather than the static `drain` it
started with: see `JobRunner._must_drain`."""

JOB_PHASE_APPLY = "apply"
"""`JobMeta.phase`'s value once a scheduled job has printed `PHASE_MARKER_APPLY`: it may have
started writing to Lidarr and must be drained, never cancelled, on shutdown. The default `""`
means "still planning, or not a scheduled job" - cancelling it is safe."""

_CANCELLED_DURING_PLANNING = "cancelled for shutdown during planning"
"""Written to a cancelled scheduled job's `log.txt`, whether it was stopped by a graceful
shutdown (`JobRunner.shutdown`) or found dead on a hard restart (`JobRunner.recover`): either way
nothing reached Lidarr, so the missed-fire catch-up re-runs it once the service is back."""

_JOB_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{6}$")

_HOOK_FIELDS: frozenset[str] = frozenset({"plan_token"})
"""The meta fields a finish hook may set: a plan's token. The playlists hook sets none; it only
writes its cache."""

_TAIL_BYTES = 64 * 1024

QUEUE_WAIT_S = 60.0 * 60.0
"""How long a `scheduled` submission queues behind a running job before it gives up and is
recorded `skipped`: 60 minutes."""

STOP_WAIT_S = TOKEN_REQUEST_WORST_CASE_S + 55.0
"""How long a stopped child gets between SIGTERM and SIGKILL: two minutes.

A `likearr` child holds SIGTERM back for as long as a Spotify token request can take
(`TOKEN_REQUEST_WORST_CASE_S`), so that a rotated refresh token is always saved. A SIGKILL inside
that window would strand an already-used refresh token and fail every later run until
`likearr auth --manual`, so the wait is derived from it rather than chosen beside it."""


class JobState(enum.StrEnum):
    RUNNING = "running"
    DONE = "done"
    """Exit 0."""
    GUARDED = "guarded"
    """Exit 2: a run whose guards held something back."""
    STALE = "stale"
    """Exit 3: the world moved since the plan. A message, not an error."""
    BUSY = "busy"
    """Exit 1 because another run held the run lock. Retry, nothing is wrong."""
    FAILED = "failed"
    CANCELLED = "cancelled"
    """Stopped from the UI."""
    INTERRUPTED = "interrupted"
    """The server stopped (or restarted) while the job ran."""
    SKIPPED = "skipped"
    """A `scheduled` submission that never ran: the queue wait ran out, or the run lock was
    already held (`JobRunner.submit_scheduled`). No child was spawned; there is no exit code."""


class JobRefused(Exception):
    """The job was not started. The message is written for the person who asked."""


@dataclass(frozen=True, slots=True)
class JobMeta:
    id: str
    kind: str
    argv: list[str]
    label: str
    started_at: str
    """ISO-8601, UTC."""
    finished_at: str | None
    exit_code: int | None
    state: JobState
    drain: bool
    """Waited for on shutdown rather than stopped. True for an apply, which leaves Lidarr half
    changed if it is cut short."""
    plan_token: str = ""
    """A plan job's integrity token, recorded when it finishes (see `likearr.web.plans.plan_token`)."""
    plan_id: str = ""
    """An apply job's plan: the job whose `diff.json` it applies."""
    pid: int = 0
    """The child's process id, recorded as it starts."""
    pid_start: str = ""
    """The kernel's start time of `pid` (``/proc/<pid>/stat``), so a reused PID is never taken
    for the job; empty where there is no ``/proc``."""
    adopted: bool = False
    """Still running from before a server restart: watched, not spawned, by this server."""
    phase: str = ""
    """`SCHEDULED_KIND` only: `""` while still planning, `JOB_PHASE_APPLY`
    once it has printed `PHASE_MARKER_APPLY` and may have started writing to Lidarr. Determined on
    demand - at a shutdown decision or at startup recovery - by looking for that line in the job's
    own `log.txt`, and recorded here only once a job that was still running stops being so, for
    `after` callbacks and any later reader to see without re-reading the log."""

    @property
    def finished(self) -> bool:
        return self.state is not JobState.RUNNING

    def to_json(self) -> str:
        data = asdict(self)
        data["state"] = str(self.state)
        return json.dumps(data, indent=2)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> JobMeta:
        return cls(
            id=str(data["id"]),
            kind=str(data.get("kind", "")),
            argv=[str(a) for a in data.get("argv", [])],
            label=str(data.get("label", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=data.get("finished_at"),
            exit_code=data.get("exit_code"),
            state=JobState(data.get("state", JobState.FAILED)),
            drain=bool(data.get("drain", False)),
            plan_token=str(data.get("plan_token", "")),
            plan_id=str(data.get("plan_id", "")),
            pid=int(data.get("pid") or 0),
            pid_start=str(data.get("pid_start", "")),
            adopted=bool(data.get("adopted", False)),
            phase=str(data.get("phase", "")),
        )


def new_job_id(now: datetime) -> str:
    """``2026-09-22T14-03-11Z-a1b2c3``: sortable by time, unguessable enough to be unique."""
    return f"{now.astimezone(UTC):%Y-%m-%dT%H-%M-%SZ}-{secrets.token_hex(3)}"


def valid_job_id(candidate: str) -> bool:
    """True only for the exact generated shape, so a URL can never walk out of the job root.

    `re.fullmatch` rather than `match` with ``$``: ``$`` also matches before a trailing newline.
    """
    return _JOB_ID.fullmatch(candidate) is not None


def state_for_exit(exit_code: int, *, cancelled: bool) -> JobState:
    """The CLI's documented exit codes (0 ok, 1 error, 2 guarded, 3 stale, 4 busy) as job states.

    Exit 0 is done even when Cancel was clicked: a child that finished just as the click landed
    did finish, and a playlists answer it wrote is a good one.
    """
    if exit_code == 0:
        return JobState.DONE
    if cancelled:
        return JobState.CANCELLED
    if exit_code == 2:
        return JobState.GUARDED
    if exit_code == 3:
        return JobState.STALE
    if exit_code == EXIT_BUSY:
        return JobState.BUSY
    return JobState.FAILED


@dataclass(slots=True)
class _Running:
    meta: JobMeta
    proc: subprocess.Popen[bytes]
    watcher: threading.Thread
    cancelled: bool = False
    interrupted: bool = False
    interrupt_reason: str = ""
    """Set by `shutdown` when it interrupts a scheduled job still in the plan phase; appended to
    the job's `log.txt` by `_watch` once the child has actually exited."""


class JobRunner:
    """Spawns, tracks and records jobs under `root`. Thread-safe; one instance per server."""

    def __init__(
        self,
        root: Path,
        cli: Sequence[str],
        *,
        lock_path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        finish_hooks: Mapping[str, Callable[[Path], Mapping[str, str]]] | None = None,
        keep: Callable[[JobMeta], bool] | None = None,
        after: Mapping[str, Callable[[JobMeta], None]] | None = None,
        alive: Callable[[JobMeta], bool] | None = None,
        adopted_poll_s: float = 2.0,
        queue_wait_s: float = QUEUE_WAIT_S,
    ) -> None:
        """
        Args:
            root: the job directory root (``/data/ui/jobs`` in the container).
            cli: the command every job starts with, config flag included - for example
                ``[sys.executable, "-m", "likearr.shell.cli", "-c", "/data/config.toml"]``.
            lock_path: the run lock `likearr run` takes, checked before spawning a run job.
            finish_hooks: by job kind, called with the job's directory when a job of that kind
                finishes done or guarded, before its record says so - a playlists job's names are
                cached by the time its page shows it finished. What a hook returns is recorded on
                the job's meta, for the fields in `_HOOK_FIELDS`. A hook that raises is logged, and
                the job is recorded anyway.
            after: by job kind, called with the finished job's record once its slot is free - so it
                may start the next job (a check chains a playlist-names refresh). Any state; one
                that raises is logged and changes nothing.
            keep: a job past the newest `KEEP_JOBS` that must not be pruned yet - a plan that may
                still be applied. The running job and the plan a job is starting on are never
                pruned either way.
            alive: whether a job recorded as running is still that job's live process
                (`still_running`); tests replace it.
            adopted_poll_s: how often a re-adopted job's process is looked at.
            queue_wait_s: how long `submit_scheduled` queues behind a running job before giving up
                (`QUEUE_WAIT_S`); tests shrink it rather than waiting a real hour.
        """
        self._root = root
        self._cli = list(cli)
        self._lock_path = lock_path
        self._now = now
        self._finish_hooks = dict(finish_hooks or {})
        self._keep = keep or (lambda _meta: False)
        self._after = dict(after or {})
        self._alive = alive or still_running
        self._adopted_poll_s = adopted_poll_s
        self._queue_wait_s = queue_wait_s
        self._mutex = threading.Lock()
        self._running: _Running | None = None
        self._adopted: dict[str, JobMeta] = {}
        self._stop_watching = threading.Event()
        self._draining = False
        self._slot_free = threading.Event()
        """Set exactly when neither a running nor an adopted job holds the one job slot - so
        `submit_scheduled`'s queue wakes the moment it may retry, rather than polling."""
        self._slot_free.set()

    # ---------------------------------------------------------------- lifecycle

    def recover(self) -> list[str]:
        """Settle every job still recorded as running. Call once, at startup.

        In the container nothing can still be running: the server is PID 1, and when it exits the
        kernel takes every process in the container with it. Such a job is marked interrupted; for
        an apply that means Lidarr was partly changed, the remedy is a fresh plan, and the Status
        page says so.

        Run bare (a terminal, systemd without ``KillMode=control-group``), a child can outlive
        the server: an apply runs in its own session (`start_new_session`), so the Ctrl-C that
        stops `start` never reaches it, and a server killed outright leaves any child behind. So
        a job whose process is provably still it (`still_running`) is re-adopted instead: marked
        `adopted`, watched until it ends, then recorded from what it printed (`_outcome`). Until
        then no other job starts, exactly as if this server had spawned it.

        Returns the ids marked interrupted.
        """
        interrupted: list[str] = []
        for meta in self.jobs():
            if meta.state is not JobState.RUNNING:
                continue
            if self._alive(meta):
                adopted = replace(meta, adopted=True)
                self._write_meta(adopted)
                with self._mutex:
                    self._adopted[meta.id] = adopted
                    self._slot_free.clear()
                threading.Thread(
                    target=self._watch_adopted, args=(adopted,), name=f"adopted-{meta.id}", daemon=True
                ).start()
                log.warning("job %s (%s) is still running from before a restart; watching it", meta.id, meta.kind)
                continue
            phase, cancelled = self._scheduled_phase(meta)
            finished = replace(meta, state=JobState.INTERRUPTED, finished_at=self._stamp(), phase=phase)
            self._write_meta(finished)
            if cancelled:
                self._append_log(meta.id, _CANCELLED_DURING_PLANNING)
            if meta.kind == SCHEDULED_KIND:
                # Not through `_watch`: a hard crash (the whole container gone) never ran it, so
                # this is the only chance to mark a cancelled-during-planning fire for the
                # missed-fire catch-up to see (`_after_scheduled` in `likearr.web.context`).
                callback = self._after.get(SCHEDULED_KIND)
                if callback is not None:
                    try:
                        callback(finished)
                    except Exception:
                        log.exception("job %s: its %s after-callback failed", meta.id, meta.kind)
            interrupted.append(meta.id)
        if interrupted:
            log.warning("marked %d job(s) interrupted by a server restart: %s", len(interrupted), interrupted)
        return interrupted

    def start(
        self,
        kind: str,
        args: Sequence[str],
        *,
        label: str = "",
        needs_run_lock: bool = False,
        drain: bool = False,
        expand_job_dir: bool = False,
        plan_id: str = "",
    ) -> JobMeta:
        """Spawn ``cli + args`` as a new job and return its metadata straight away.

        Raises:
            JobRefused: shutting down, another job is running, or (with `needs_run_lock`) a
                scheduled run holds the run lock. Nothing was spawned.
        """
        with self._mutex:
            if self._draining:
                raise JobRefused("likearr is shutting down; try again once it is back")
            if self._running is not None:
                raise JobRefused(f"another job is already running ({self._running.meta.kind}); wait for it to finish")
            if self._adopted:
                other = next(iter(self._adopted.values()))
                raise JobRefused(
                    f"a job from before likearr restarted is still running ({other.kind}); wait for it to finish"
                )
            if needs_run_lock:
                self._check_run_lock()

            job_id = new_job_id(self._now())
            while (self._root / job_id).exists():  # pragma: no cover - 1 in 16M within one second
                job_id = new_job_id(self._now())
            job_dir = self._root / job_id
            job_dir.mkdir(parents=True, mode=0o700)
            os.chmod(job_dir, 0o700)  # mkdir's mode is masked by the umask

            # With `expand_job_dir`, "{job_dir}" in an argument names this job's own directory (a
            # plan's `--out`), which exists only from here on. Opt-in, so free text - an explain
            # query that happens to contain "{job_dir}" - always reaches the child as typed.
            argv = [*self._cli, *(a.replace("{job_dir}", str(job_dir)) if expand_job_dir else a for a in args)]
            meta = JobMeta(
                id=job_id,
                kind=kind,
                argv=argv,
                label=label,
                started_at=self._stamp(),
                finished_at=None,
                exit_code=None,
                state=JobState.RUNNING,
                drain=drain,
                plan_id=plan_id,
            )
            self._write_meta(meta)
            out = (job_dir / "out.txt").open("wb")
            err = (job_dir / "log.txt").open("wb")
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    cwd=job_dir,
                    env=_child_env(),
                    close_fds=True,
                    # A job that must drain (an apply) gets its own session: a Ctrl-C at a terminal
                    # running `start` by hand reaches the server, never the child writing to Lidarr.
                    start_new_session=drain,
                )
            except (OSError, ValueError) as exc:  # ValueError: an argument Popen refuses, e.g. a NUL byte
                out.close()
                err.close()
                failed = replace(meta, state=JobState.FAILED, exit_code=-1, finished_at=self._stamp())
                self._write_meta(failed)
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                raise JobRefused(f"could not start likearr: {reason}") from exc
            # Who the child is, for a server that restarts while it runs (`recover`).
            meta = replace(meta, pid=proc.pid, pid_start=_process_start(proc.pid))
            self._write_meta(meta)
            watcher = threading.Thread(target=self._watch, args=(job_id, out, err), name=f"job-{job_id}", daemon=True)
            self._running = _Running(meta=meta, proc=proc, watcher=watcher)
            self._slot_free.clear()
            watcher.start()
            log.info("job %s started: %s %s", job_id, kind, label)
            # Pruned only now the child runs: whatever it reads (a plan's diff.json) exists by then,
            # and `plan_id` is protected besides.
            self._prune(protect={job_id, plan_id} - {""})
            return meta

    def cancel(self, job_id: str) -> bool:
        """Ask the running job to stop, if it is `job_id`, and return at once.

        SIGTERM now; SIGKILL only after `STOP_WAIT_S`, from a background thread, so the request
        that asked never waits. Until the child goes, `stopping` says so: it may be finishing a
        Spotify token save, which it will not abandon. False when there is nothing of that id.
        """
        with self._mutex:
            running = self._running
            if running is None or running.meta.id != job_id:
                return False
            if running.meta.drain:
                # An apply is never stopped halfway: that would leave Lidarr partly changed.
                log.warning("job %s: cancel refused; it must finish", job_id)
                return False
            running.cancelled = True
        log.info("job %s: stop requested; SIGKILL only after %.0fs", job_id, STOP_WAIT_S)
        threading.Thread(target=_stop, args=(running.proc,), name=f"stop-{job_id}", daemon=True).start()
        return True

    def stopping(self, job_id: str) -> bool:
        """True while `job_id` has been asked to stop and has not finished yet."""
        with self._mutex:
            running = self._running
            return running is not None and running.meta.id == job_id and (running.cancelled or running.interrupted)

    def shutdown(self, timeout: float) -> None:
        """Refuse new jobs, then wait for a job that must drain, or stop one that need not.

        Called from the server's shutdown hook (SIGTERM). `timeout` bounds the wait for a
        draining job; the container's own stop grace period is the real bound, and it is set
        longer than an apply takes.

        A `SCHEDULED_KIND` job does not use the `drain` it was started with
        for this decision - see `_must_drain`: still planning, it is cancelled and re-fired after
        restart, because planning has written nothing to Lidarr; once it has printed the
        apply-phase marker it is drained exactly like a UI apply, because Lidarr may already be
        half-changed. Every other kind keeps its static `drain`.
        """
        self._stop_watching.set()  # a re-adopted job stays recorded as running, for the next start
        with self._mutex:
            self._draining = True
            running = self._running
            must_drain = running is not None and self._must_drain(running)
            if running is not None and not must_drain:
                running.interrupted = True
                if running.meta.kind == SCHEDULED_KIND:
                    running.interrupt_reason = _CANCELLED_DURING_PLANNING
        if running is None:
            return
        if must_drain:
            log.warning("waiting up to %.0fs for job %s (%s) to finish", timeout, running.meta.id, running.meta.kind)
            running.watcher.join(timeout)
            return
        _stop(running.proc)
        running.watcher.join(STOP_WAIT_S + 5)

    def _must_drain(self, running: _Running) -> bool:
        """Whether `shutdown` must wait for `running` rather than stop it.

        Must be called with `self._mutex` held. A scheduled job's own phase decides this (its
        `log.txt` is read fresh, not just the last value `recover`/an earlier check saw, so the
        decision is as current as it can be); `running.meta` is updated with what was found, so the
        record this job finishes with already carries it. Every other kind keeps the `drain` flag
        it was started with.
        """
        if running.meta.kind == SCHEDULED_KIND:
            phase, _ = self._scheduled_phase(running.meta)
            running.meta = replace(running.meta, phase=phase)
            return phase == JOB_PHASE_APPLY
        return running.meta.drain

    def _scheduled_phase(self, meta: JobMeta) -> tuple[str, bool]:
        """`meta`'s apply-phase, and whether that makes it "cancelled during planning". For
        anything but a `SCHEDULED_KIND` job, `meta.phase` is returned unchanged and
        it is never "cancelled" in this sense - only a scheduled job's shutdown depends on it."""
        if meta.kind != SCHEDULED_KIND:
            return meta.phase, False
        if meta.phase == JOB_PHASE_APPLY or self._printed_apply_marker(meta.id):
            return JOB_PHASE_APPLY, False
        return "", True

    def _printed_apply_marker(self, job_id: str) -> bool:
        """Whether `PHASE_MARKER_APPLY` has appeared anywhere in `job_id`'s `log.txt`.

        The *whole* file, not `_read_text`'s tail: a long-running apply can write well past
        `_TAIL_BYTES` of stderr after the marker, and a shutdown that only looked at the tail
        would read that as still planning and SIGTERM a job that may already be writing to Lidarr
        - the exact failure this phase check exists to prevent.
        """
        if not valid_job_id(job_id):
            return False
        try:
            return PHASE_MARKER_APPLY.encode() in (self._root / job_id / "log.txt").read_bytes()
        except OSError:
            # An unreadable log proves nothing, so it fails toward the safe answer: a job that
            # may already be writing to Lidarr is drained, never cancelled mid-apply.
            log.warning("job %s: log.txt unreadable; treating it as applying", job_id, exc_info=True)
            return True

    def _append_log(self, job_id: str, text: str) -> None:
        """Add a line to a finished job's `log.txt`, where the job page and `log_tail` already
        look. Best-effort: a job whose log cannot be appended to still has the right `state`."""
        try:
            with (self._root / job_id / "log.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"\nlikearr: {text}\n")
        except OSError:
            log.warning("could not append to job %s's log", job_id, exc_info=True)

    def wait(self, job_id: str, timeout: float) -> bool:
        """Block until `job_id` is no longer running. For tests and shutdown; pages poll instead."""
        with self._mutex:
            running = self._running
        if running is not None and running.meta.id == job_id:
            running.watcher.join(timeout)
            return not running.watcher.is_alive()
        return True

    def submit_scheduled(self, kind: str, args: Sequence[str], *, label: str = "") -> None:
        """Start `kind` now, or queue behind whatever is running, from a background thread.

        For the scheduler's own fire and for "Run now" (`likearr.web.schedule`): neither may block
        its caller for up to an hour, so this returns at once and the outcome shows up as a job.
        The run lock is still checked before spawning (`needs_run_lock=True`): a UI plan or apply
        already holding the slot is queued for up to `queue_wait_s`, same as before this existed
        (`start`'s own refusal). The run lock itself being held by something outside this server -
        a host cron line, say - is not queued: that is the collision the lock has
        always existed to catch, and it is recorded `skipped` at once, exactly as a scheduled run
        that lost the lock race has always reported itself (`RunStatus.SKIPPED` in `shell.run`).
        """
        threading.Thread(
            target=self._run_queued, args=(kind, list(args), label), name=f"queue-{kind}", daemon=True
        ).start()

    def _run_queued(self, kind: str, args: list[str], label: str) -> None:
        if self._draining:
            self._record_skipped(kind, args, label, "likearr is shutting down; the scheduled run was not started")
            return
        # The lock is probed directly only when this runner's own slot is free: a UI plan or
        # apply job holds the *real* run lock for as long as it runs, so probing while one is
        # running would see it held and wrongly call that "external" (the host cron line, say),
        # skipping at once instead of queueing behind this runner's own job. A tiny window remains between
        # this check and the probe below - `start`'s own check on every retry is the backstop.
        if self._slot_is_free():
            try:
                self._check_run_lock()
            except JobRefused as exc:
                log.info("job kind %s: %s; recording it skipped", kind, exc)
                self._record_skipped(kind, args, label, str(exc))
                return
        deadline = self._now() + timedelta(seconds=self._queue_wait_s)
        while True:
            try:
                # `drain=True` still isolates the child from a terminal Ctrl-C (`start_new_session`)
                # and still means Cancel refuses it - but for `SCHEDULED_KIND`, `shutdown`'s own
                # wait-or-stop decision no longer trusts this flag: `_must_drain` decides from the
                # job's actual apply phase instead, because a scheduled run
                # spends most of its time still planning, which is safe to cancel and re-fire.
                self.start(kind, args, label=label, needs_run_lock=True, drain=True)
                return
            except JobRefused as exc:
                if self._draining:
                    self._record_skipped(
                        kind, args, label, "likearr is shutting down; the scheduled run was not started"
                    )
                    return
                remaining = (deadline - self._now()).total_seconds()
                if remaining <= 0:
                    minutes = int(self._queue_wait_s // 60)
                    log.warning("job kind %s: queued %d minutes behind %s; giving up", kind, minutes, exc)
                    self._record_skipped(kind, args, label, f"waited {minutes} minutes for the slot to free; {exc}")
                    return
                self._event_wait(remaining)

    def _slot_is_free(self) -> bool:
        with self._mutex:
            return self._running is None and not self._adopted

    def _event_wait(self, timeout: float) -> None:
        self._slot_free.wait(timeout)
        self._slot_free.clear()  # re-checked by the next loop iteration's `start`; never a stale "free"

    def _record_skipped(self, kind: str, args: Sequence[str], label: str, reason: str) -> None:
        """Record a job that never ran a child: the queue gave up, or the run lock was held.

        A directory like any other job's, so it shows in job history and on Status - just with no
        `pid` and no exit code. `reason` goes to `log.txt`, where the job page already looks."""
        with self._mutex:
            now = self._now()
            job_id = new_job_id(now)
            while (self._root / job_id).exists():  # pragma: no cover - 1 in 16M within one second
                job_id = new_job_id(now)
            job_dir = self._root / job_id
            job_dir.mkdir(parents=True, mode=0o700)
            os.chmod(job_dir, 0o700)
            # The reason goes down before `meta.json`: the meta is what makes the job visible to
            # a reader, so a reader that sees it must also find its log.
            (job_dir / "out.txt").write_bytes(b"")
            (job_dir / "log.txt").write_text(reason, encoding="utf-8")
            stamp = self._stamp()
            meta = JobMeta(
                id=job_id,
                kind=kind,
                argv=[*self._cli, *args],
                label=label,
                started_at=stamp,
                finished_at=stamp,
                exit_code=None,
                state=JobState.SKIPPED,
                drain=False,
            )
            self._write_meta(meta)
            self._prune(protect={job_id})
        log.warning("job %s (%s) skipped: %s", job_id, kind, reason)

    # ---------------------------------------------------------------- reading

    def current(self) -> JobMeta | None:
        with self._mutex:
            if self._running is not None:
                return self._running.meta
            return next(iter(self._adopted.values()), None)

    def get(self, job_id: str) -> JobMeta | None:
        if not valid_job_id(job_id):
            return None
        return self._read_meta(self._root / job_id)

    def jobs(self) -> list[JobMeta]:
        """Every job on disk, newest first."""
        if not self._root.is_dir():
            return []
        out: list[JobMeta] = []
        for job_dir in sorted(self._root.iterdir(), key=lambda p: p.name, reverse=True):
            if valid_job_id(job_dir.name):
                meta = self._read_meta(job_dir)
                if meta is not None:
                    out.append(meta)
        return out

    def diff_path(self, job_id: str) -> Path | None:
        """A plan job's `diff.json`, if the id is valid and the file exists."""
        return self.job_file(job_id, "diff.json")

    def job_file(self, job_id: str, name: str, *, must_exist: bool = True) -> Path | None:
        """A file in a job's own directory, if the id is valid (and, by default, the file exists).
        `name` is always one of the server's own fixed names, never request data."""
        if not valid_job_id(job_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.json", name):
            return None
        path = self._root / job_id / name
        if not (self._root / job_id).is_dir():
            return None
        return path if path.is_file() or not must_exist else None

    def log_tail(self, job_id: str, lines: int = 80) -> str:
        """The last `lines` lines of the child's stderr, with anything credential-shaped redacted.

        likearr's logging already redacts, and never logs a token or a key. This is the second
        layer, because this text is rendered in a browser: the patterns, plus the exact values of
        the environment's secrets, which catch one a traceback prints without a label.
        """
        text = self._read_text(job_id, "log.txt", tail=True)
        return redact("\n".join(text.splitlines()[-lines:]), literals=_env_secrets())

    def output(self, job_id: str) -> str:
        """The child's stdout, verbatim: it is parsed (`playlists --json`) as well as shown."""
        return self._read_text(job_id, "out.txt", tail=False)

    def shown_output(self, job_id: str) -> str:
        """The child's stdout for a browser, with the values of this process's own secrets removed.

        Only exact values, never patterns: stdout is the answer - artist names, titles, playlist
        names - and the pattern redactor used on the log would turn "Basic Channel" into "Basic
        REDACTED". likearr redacts its own error messages before printing them; this is the
        backstop for a secret it did not know to look for.
        """
        return redact_literals(self.output(job_id), _env_secrets())

    # ---------------------------------------------------------------- internals

    def _watch(self, job_id: str, out: IO[bytes], err: IO[bytes]) -> None:
        running = self._running
        assert running is not None and running.meta.id == job_id
        exit_code = running.proc.wait()
        out.close()
        err.close()
        # Re-read after the child is gone, not just at shutdown's decision: the child can print
        # the marker in the instant between that decision and the signal, and a finished
        # scheduled run should record the phase it really reached either way.
        phase, _ = self._scheduled_phase(running.meta)
        if running.interrupt_reason:
            reason = running.interrupt_reason
            if running.meta.kind == SCHEDULED_KIND and phase == JOB_PHASE_APPLY:
                reason = "stopped for shutdown just after it began applying; re-plan to reconcile Lidarr"
            self._append_log(job_id, reason)
        finished: JobMeta | None = None
        with self._mutex:
            try:
                if running.interrupted:
                    state = JobState.INTERRUPTED
                else:
                    state = state_for_exit(exit_code, cancelled=running.cancelled)
                finished = replace(
                    running.meta, state=state, exit_code=exit_code, finished_at=self._stamp(), phase=phase
                )
                finished = self._run_finish_hook(finished)
                self._write_meta(finished)
                log.info("job %s finished: %s (exit %d)", job_id, state, exit_code)
            except Exception:
                # The job directory vanished or the disk is full. Record nothing, but never leave
                # the slot taken: that would refuse every later job until a restart.
                log.exception("job %s finished (exit %d) but its record could not be written", job_id, exit_code)
            finally:
                self._running = None
                self._slot_free.set()
        # Outside the mutex, the slot now free: an after-callback may start the next job.
        callback = self._after.get(running.meta.kind)
        if callback is not None and finished is not None:
            try:
                callback(finished)
            except Exception:
                log.exception("job %s: its %s after-callback failed", job_id, running.meta.kind)

    def _watch_adopted(self, meta: JobMeta) -> None:
        """Wait for a re-adopted job's process to end, then record it. Its exit code cannot
        be read here (it is not this process's child), so the outcome comes from what it printed."""
        while self._alive(meta):
            if self._stop_watching.wait(self._adopted_poll_s):
                return  # shutting down: it stays recorded as running, and the next start looks again
        exit_code = _printed_exit_code(self._read_text(meta.id, "out.txt", tail=True))
        state = state_for_exit(exit_code, cancelled=False) if exit_code is not None else JobState.INTERRUPTED
        phase, cancelled = self._scheduled_phase(meta) if state is JobState.INTERRUPTED else (meta.phase, False)
        finished = replace(meta, state=state, exit_code=exit_code, finished_at=self._stamp(), phase=phase)
        try:
            if cancelled:
                self._append_log(meta.id, _CANCELLED_DURING_PLANNING)
            finished = self._run_finish_hook(finished)
            self._write_meta(finished)
            log.info("job %s, from before the restart, finished: %s", meta.id, state)
        except Exception:
            log.exception("job %s, from before the restart, ended but its record could not be written", meta.id)
        finally:
            with self._mutex:
                self._adopted.pop(meta.id, None)
                if not self._adopted and self._running is None:
                    self._slot_free.set()
        # Outside the mutex, as `_watch` does: an after-callback (e.g. `_after_scheduled` marking
        # a cancelled-during-planning fire for the missed-fire catch-up) may itself need the slot.
        callback = self._after.get(meta.kind)
        if callback is not None:
            try:
                callback(finished)
            except Exception:
                log.exception("job %s: its %s after-callback failed", meta.id, meta.kind)

    def _run_finish_hook(self, meta: JobMeta) -> JobMeta:
        hook = self._finish_hooks.get(meta.kind)
        if hook is None or meta.state not in {JobState.DONE, JobState.GUARDED}:
            return meta
        try:
            extra = dict(hook(self._root / meta.id))
        except Exception:
            log.exception("job %s: its %s finish hook failed; recording the job without it", meta.id, meta.kind)
            return meta
        return replace(meta, **{k: v for k, v in extra.items() if k in _HOOK_FIELDS})

    def _check_run_lock(self) -> None:
        """Take the run lock without waiting and let go at once; refuse if it is taken.

        A child that finds the lock taken publishes a run-level error, which an apply sends to
        retained MQTT. Checking here instead means a scheduled run in progress costs a retry and
        nothing else. The window between this release and the child's own take is milliseconds;
        a cron fire that lands in it is reported by the child as the collision it really was.
        """
        try:
            with run_lock(self._lock_path):
                pass
        except LockHeld as exc:
            raise JobRefused("a scheduled run is in progress; try again in a few minutes") from exc

    def _prune(self, *, protect: set[str]) -> None:
        """Keep the newest `KEEP_JOBS` job directories, the ones in `protect`, and older ones the
        `keep` predicate says may still be needed. Only directories named as job ids are touched.
        Never raises: pruning is housekeeping."""
        try:
            names = sorted((p.name for p in self._root.iterdir() if p.is_dir() and valid_job_id(p.name)), reverse=True)
            for name in names[KEEP_JOBS:]:
                if name in protect:
                    continue
                meta = self._read_meta(self._root / name)
                if meta is not None and (meta.state is JobState.RUNNING or self._keep(meta)):
                    continue
                shutil.rmtree(self._root / name, ignore_errors=True)
        except OSError:
            log.warning("pruning the job store failed", exc_info=True)

    def _read_meta(self, job_dir: Path) -> JobMeta | None:
        try:
            return JobMeta.from_mapping(json.loads((job_dir / "meta.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_meta(self, meta: JobMeta) -> None:
        job_dir = self._root / meta.id
        write_atomic(job_dir / "meta.json", meta.to_json(), mode=0o600)

    def _read_text(self, job_id: str, name: str, *, tail: bool) -> str:
        if not valid_job_id(job_id):
            return ""
        path = self._root / job_id / name
        try:
            with path.open("rb") as fh:
                cut = False
                if tail:
                    size = fh.seek(0, os.SEEK_END)
                    cut = size > _TAIL_BYTES
                    fh.seek(max(0, size - _TAIL_BYTES))
                data = fh.read()
                if cut:
                    # The cut lands mid-line, and could separate a label from the secret after
                    # it; the partial line is dropped rather than shown half-redacted.
                    data = data[data.find(b"\n") + 1 :] if b"\n" in data else b""
                return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _stamp(self) -> str:
        return self._now().astimezone(UTC).isoformat(timespec="seconds")


def still_running(meta: JobMeta) -> bool:
    """Whether `meta`'s process is alive and is still that job: the same PID, started at the same
    moment, running the same argv. Linux ``/proc`` only; elsewhere (or for a job recorded before
    PIDs were) it cannot be proved, and the answer is False - the job is marked interrupted."""
    if meta.pid <= 0 or not meta.pid_start:
        return False
    if _process_start(meta.pid) != meta.pid_start:
        return False  # gone, a zombie's reaped slot, or the PID reused by another process
    try:
        cmdline = Path(f"/proc/{meta.pid}/cmdline").read_bytes()
    except OSError:
        return False
    return cmdline.rstrip(b"\0").split(b"\0") == [a.encode() for a in meta.argv]


def _process_start(pid: int) -> str:
    """Field 22 of ``/proc/<pid>/stat``, the process's start time in clock ticks since boot;
    empty where there is no ``/proc`` or no such process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""
    # The command name (field 2) is in parentheses and may hold spaces or ")": split after the last.
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else ""


def last_json_object(
    output: str, accept: Callable[[dict[str, Any]], bool], *, strict: bool = False
) -> dict[str, Any] | None:
    """The JSON object a likearr child printed on stdout, as the web layer's one wire-format
    reader for "the JSON line a child printed" (six call sites used to each decode this by hand).

    `strict=False` (the default) scans `output` backward and returns the first line that parses
    to a JSON object and satisfies `accept` - a log warning on stderr never reaches stdout, but a
    later diagnostic line on stdout itself should not hide the real record before it.

    `strict=True` reads only the literal last line: doctor's and lidarr_setup's `--json` commands
    print exactly one line and nothing after it, on purpose, so a stray extra line there is a
    parse failure, not something to scan past.

    `None` for empty output, a line that is not valid JSON (at the position strict/non-strict
    looks), JSON that is not an object, or an object `accept` rejects.
    """
    stripped = output.strip()
    if not stripped:
        return None
    lines = stripped.splitlines()
    candidates = (lines[-1],) if strict else reversed(lines)
    for line in candidates:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and accept(data):
            return data
    return None


def _printed_exit_code(output: str) -> int | None:
    """The ``exit_code`` of the last JSON record a `likearr run` printed (its health record), if any."""
    data = last_json_object(output, lambda d: isinstance(d.get("exit_code"), int))
    return int(data["exit_code"]) if data is not None else None


def _env_secrets() -> tuple[str, ...]:
    """The values of the secret-bearing variables in this environment (keys, secrets, passwords)."""
    return tuple(
        value
        for name, value in os.environ.items()
        if name.startswith("LIKEARR_") and any(word in name for word in ("KEY", "SECRET", "PASSWORD", "TOKEN"))
    )


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop(PASSWORD_ENV, None)
    return env


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM, then SIGKILL if the child has not gone within `STOP_WAIT_S`."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(STOP_WAIT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - likearr exits on SIGTERM
        proc.kill()
