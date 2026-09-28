"""The in-service scheduler: decides *when* to fire, never *what* runs.

A background thread, started from the app `lifespan` and stopped with it. Each iteration:

1. Load `config.toml` fresh and compute the next fire with `core.cron.next_fire`, every chunk of
   the wait (`_RECHECK_S`), not just once - so a Settings edit to `[schedule] cron`/`timezone` is
   honoured within one chunk, without a restart, rather than only once the *old* fire time (which
   the edit may have moved much later, or sooner) arrives.
2. Wait for it, waking at once on shutdown.
3. Submit the one command the schedule is ever allowed to run: `run --scheduled --apply -c
   <config path>`, fixed in code (`scheduled_argv`). The UI can change *when* this fires; it can
   never change what it runs.

The loop never dies except on `stop()`: any unexpected exception in an iteration - a locked
sqlite file, a transient filesystem error, anything not already handled - is logged and the
iteration retries after one `_RECHECK_S`, rather than silently ending the schedule for good.

**Paused stays one code path.** `[schedule] enabled = false` is not checked here. The scheduler
fires on schedule regardless, and the child (`run_command` in `likearr/shell/run.py`) is the one
that reads the pause and publishes `RunStatus.PAUSED` without doing any work - the same path phase
1 already built and ships. The alternative - this module skipping the fire and publishing PAUSED
itself - would need a second place that knows how to publish a health record, and a second answer
to "did we fire", for a state (paused) that is already cheap: a paused child exits at once, before
touching Spotify or Lidarr. One path, chosen for boringness over saving one `Popen`.

**The queue, not this module, decides whether a fire actually runs.** `JobRunner.submit_scheduled`
(`likearr/web/jobs.py`) is what queues behind a job already holding the slot, waits up to an hour,
and records `skipped` if it gives up; this module only ever calls it, at the right time. "Run now"
(`fire_now`, called from the Status page) goes through the exact same function, so a manual kick is
subject to the same lock and the same queue as a real fire - the *arr "Run now" pattern.

**Missed fires.** The scheduler's last fire time is persisted (`SqliteState.record_scheduled_fire`
/ `last_scheduled_fire`). At startup, if the schedule's next fire *after* that recorded time is
already due, the service catches up once, `MISSED_FIRE_DELAY_S` after startup - never once per
missed fire. A service that has never fired before (no record - including
a fresh state database) does not catch up: there is nothing to have missed.

**Single worker.** Two uvicorn workers - or `--reload`'s own subprocess - would each start this
thread and fire every job twice. `likearr start` never exposes `--workers` or `--reload`, so
`assert_single_worker` is a tripwire against ever adding either without updating this module.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from likearr.adapters.state_sqlite import SqliteState
from likearr.config import ConfigError, load_config
from likearr.core.cron import CronError, next_fire
from likearr.web.jobs import SCHEDULED_KIND, JobRunner

__all__ = [
    "MISSED_FIRE_DELAY_S",
    "SCHEDULED_KIND",
    "Scheduler",
    "assert_single_worker",
    "fire_now",
    "scheduled_argv",
]

log = logging.getLogger(__name__)

MISSED_FIRE_DELAY_S = 5 * 60.0
"""A missed fire (one or more, while the service was down) is caught up
once, 5 minutes after startup - never once per missed fire."""

_RECHECK_S = 300.0
"""How often a wait for the next fire re-reads `config.toml`, so a Settings edit to the schedule
is noticed within 5 minutes rather than only at the old, possibly much later, fire time."""


_PASSED_FIRE_GRACE_S = 60.0
"""Slack beyond one recheck chunk before an overdue slot counts as passed, not late."""


def scheduled_argv() -> list[str]:
    """The one command a fire may ever run, fixed in code: `run --scheduled --apply`.

    The config path is not repeated here - `JobRunner`'s own `cli` prefix already carries `-c
    <config_path>` ahead of every job's argv, the same as every other job kind (`plan`, `check`,
    ...) - so this is the whole of what `submit_scheduled` adds.

    `--accept-shrink` / `--accept-health` are never here and never will be - `run_command` already
    refuses them with `--scheduled` (`run_command` in `shell/run.py`); this is the second lock on the same
    door, so the argv itself is never the thing anyone would need to change to loosen it.
    """
    return ["run", "--scheduled", "--apply"]


def assert_single_worker(workers: int, *, reload: bool) -> None:
    """Refuse to start the scheduler under more than one uvicorn worker, or `--reload`.

    Each of those is its own OS process re-importing the app, so each would start its own copy of
    this thread and every scheduled run would fire once per process. `likearr start` never exposes
    `--workers` or `--reload` today, so this should never trip; it exists so adding either later
    fails loudly here instead of quietly doubling every scheduled run. A real exception, not a bare
    `assert`: `python -O` strips those, which would silently remove the only thing standing between
    a two-worker `start` and every scheduled run firing twice.
    """
    if workers != 1 or reload:
        raise RuntimeError(
            f"the scheduler must run with exactly one worker and no --reload, not workers={workers} reload={reload}"
        )


def fire_now(
    runner: JobRunner, config_path: Path, *, now: Callable[[], datetime], label: str = "Scheduled run"
) -> None:
    """Record a fire and submit it - shared by the scheduler loop and the Status page's Run now
    button, so a manual kick updates the missed-fire bookkeeping and goes through the same lock
    and the same queue (`JobRunner.submit_scheduled`) as a real one. Returns at once."""
    ts = now()
    try:
        config = load_config(config_path)
        with SqliteState(config.state_db) as state:
            state.record_scheduled_fire(ts)
    except (ConfigError, OSError):
        log.exception("schedule: could not record the fire time; a future missed-fire check may be wrong")
    runner.submit_scheduled(SCHEDULED_KIND, scheduled_argv(), label=label)
    log.info("schedule: fired at %s (%s)", ts.isoformat(), label)


@dataclass(slots=True)
class Scheduler:
    """Runs `fire_now` on the configured schedule, in a background thread. One per server."""

    config_path: Path
    runner: JobRunner
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    wait: Callable[[threading.Event, float], bool] = field(default=lambda event, timeout: event.wait(timeout))
    """`event.wait(timeout)`, or a fake for tests: returns True the moment `stop()` is called,
    False on a plain timeout. Never a real sleep - tests inject a clock and this together."""
    startup_delay_s: float = MISSED_FIRE_DELAY_S
    recheck_s: float = _RECHECK_S
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def start(self) -> None:
        assert self._thread is None, "Scheduler.start() called twice"
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ---------------------------------------------------------------- the loop

    def _loop(self) -> None:
        """The thread body. Never dies except on `stop()`: an unexpected error in any one
        iteration (a locked sqlite file, say - `sqlite3.Error` is not an `OSError`, and nothing
        here can enumerate every way a filesystem or a config can misbehave) is logged and the
        loop retries after `recheck_s`, rather than silently ending the schedule for good."""
        if self._run_catchup():
            return
        reference = self.now()
        while not self._stop.is_set():
            try:
                if self._wait_for_next_fire(reference):
                    return
                fire_now(self.runner, self.config_path, now=self.now)
                # Only advance `reference` on success: a `fire_now` that raises must retry this
                # same due fire next iteration, not silently skip ahead to the schedule's next
                # slot (which could be hours away) because `reference` had already moved past it.
                reference = self.now()
            except Exception:
                log.exception("schedule: unexpected error; retrying in %.0fs", self.recheck_s)
                if self._wait(self.recheck_s):
                    return

    def _run_catchup(self) -> bool:
        """True if stopped before the missed-fire catch-up (if any is due) finished firing."""
        try:
            catchup_at = self._catchup_time()
        except Exception:
            log.exception("schedule: unexpected error computing the missed-fire catch-up; skipping it")
            return False
        if catchup_at is None:
            return False
        if self._wait_until(catchup_at):
            return True
        try:
            fire_now(self.runner, self.config_path, now=self.now, label="Scheduled run (missed while likearr was down)")
        except Exception:
            log.exception("schedule: the missed-fire catch-up failed unexpectedly")
        return False

    def _wait_for_next_fire(self, reference: datetime) -> bool:
        """True if stopped before the schedule's next fire after `reference` arrives.

        Re-reads `config.toml` and recomputes the fire from `reference` every chunk (at most
        `recheck_s` apart), not just once at the top: a Settings edit that moves the fire sooner
        must be honoured within one chunk, not only once the *old* fire time arrives - a fire
        computed once and then only waited for would miss exactly that edit.
        """
        after = reference
        while True:
            try:
                config = load_config(self.config_path)
                tz = ZoneInfo(config.schedule.timezone)
                fire = next_fire(config.schedule.cron, after, tz)
            except (ConfigError, CronError, ZoneInfoNotFoundError, ValueError, OSError) as exc:
                log.error("schedule: %s; retrying in %.0fs", exc, self.recheck_s)
                if self._wait(self.recheck_s):
                    return True
                continue
            if fire is None:
                log.error("schedule: cron %r never fires; retrying in %.0fs", config.schedule.cron, self.recheck_s)
                if self._wait(self.recheck_s):
                    return True
                continue
            remaining = (fire - self.now()).total_seconds()
            if remaining < -(self.recheck_s + _PASSED_FIRE_GRACE_S):
                # Further behind than this loop's own chunking or a retry after an error can put
                # a real fire: the slot came from a Settings edit made after it had passed (the
                # fire is computed from the *last* fire, not from the edit). Firing it would make
                # saving a schedule an unattended apply, so skip to the next slot after now.
                log.info("schedule: the slot at %s had already passed when the schedule changed; skipping it", fire)
                after = self.now()
                continue
            if remaining <= 0:
                return False
            if self._wait(min(remaining, self.recheck_s)):
                return True
            # Woken by a plain chunk timeout (not stopped): loop around and re-read config,
            # rather than assuming `fire` - computed from the config as it was one chunk ago -
            # is still current.

    def _wait_until(self, target: datetime) -> bool:
        """True if stopped before `target`, waiting in chunks of at most `recheck_s`. For the
        catch-up target only: a fixed point in time, not derived from the schedule, so there is
        nothing to re-read config for."""
        while True:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return False
            if self._wait(min(remaining, self.recheck_s)):
                return True

    def _wait(self, seconds: float) -> bool:
        return self.wait(self._stop, seconds)

    def _catchup_time(self) -> datetime | None:
        """When to fire the missed-fire catch-up, or `None` if none is due.

        `None` covers: the config does not load, there is no state database yet (nothing has ever
        run), this service has never recorded a fire (a fresh database, or one older than fire
        tracking), or the schedule's next fire after the last recorded one is still in the future -
        none of those is a missed fire.

        A fire recorded `cancelled` (`SqliteState.scheduled_fire_cancelled`) -
        a redeploy's SIGTERM caught its child still planning - is due at its *own* recorded time,
        not the schedule's next slot after it: that exact fire never ran, so it is what is missed,
        not whatever comes next.
        """
        try:
            config = load_config(self.config_path)
        except ConfigError:
            return None
        if not config.state_db.is_file():
            return None
        with SqliteState(config.state_db) as state:
            last = state.last_scheduled_fire()
            cancelled = state.scheduled_fire_cancelled()
        if last is None:
            return None
        if cancelled:
            due: datetime | None = last
        else:
            try:
                tz = ZoneInfo(config.schedule.timezone)
                due = next_fire(config.schedule.cron, last, tz)
            except (CronError, ZoneInfoNotFoundError, ValueError, OSError):
                return None
        if due is None or due > self.now():
            return None
        catchup_at = self.now() + timedelta(seconds=self.startup_delay_s)
        log.warning(
            "schedule: at least one fire was missed while likearr was down (due %s); catching up once, in %.0fs",
            due.isoformat(),
            self.startup_delay_s,
        )
        return catchup_at
