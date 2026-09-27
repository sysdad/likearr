"""The in-service scheduler: a fake clock and an injected wait only - no real sleeps, no real
server loop (a real wait can hang a test session for hours)."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.state_sqlite import SqliteState
from likearr.web.jobs import JobRunner
from likearr.web.schedule import (
    MISSED_FIRE_DELAY_S,
    SCHEDULED_KIND,
    Scheduler,
    assert_single_worker,
    fire_now,
    scheduled_argv,
)

CONFIG = """\
[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "spotify-token.json"

[state]
db = "state.sqlite"

[schedule]
cron = "20 */6 * * *"
timezone = "UTC"
"""

FAKE_CLI = """
import sys
print("ran: " + " ".join(sys.argv[1:]))
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def fake_cli(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return [sys.executable, str(script)]


@pytest.fixture
def runner(tmp_path: Path, fake_cli: list[str]) -> Iterator[JobRunner]:
    runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "likearr.lock")
    yield runner
    # `fire_now`/`submit_scheduled` start a queue thread and a watcher thread in the background;
    # a test that does not itself wait for them can otherwise leave one running past the test,
    # logging into a stream a later test's `capsys` has already closed (issue #137).
    runner.shutdown(timeout=5)


class FakeClock:
    """A controllable `now()`. `advance` is what an injected `wait` calls instead of sleeping."""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _wait_that_parks_after_n_fires(runner: JobRunner, clock: FakeClock, fires: int = 1):
    """`Scheduler.wait`: advances the fake clock instead of sleeping, so reaching a fire far in
    "the future" costs no wall-clock time - but the moment `submit_scheduled` has been called
    `fires` times, it stops advancing and blocks for real on `event` (`stop()`'s own event), so the
    scheduler thread parks quietly instead of racing on to the next fire while the test inspects
    state or calls `stop()`. Without this a fully "instant" wait lets the thread outrun the test
    and fire far more times than intended before any assertion runs.
    """
    seen = threading.Event()
    if fires <= 0:
        seen.set()  # park on the very first wait call; the loop never gets to fire
    original = runner.submit_scheduled

    def counting_submit(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        counting_submit.calls += 1  # type: ignore[attr-defined]
        if counting_submit.calls >= fires:  # type: ignore[attr-defined]
            seen.set()

    counting_submit.calls = 0  # type: ignore[attr-defined]
    runner.submit_scheduled = counting_submit  # type: ignore[method-assign]

    def wait(event: threading.Event, timeout: float) -> bool:
        if seen.is_set():
            return event.wait(None)  # parked: only `stop()` (which sets `event`) wakes it
        stopped = event.wait(0)
        if not stopped:
            clock.advance(timeout)
            stopped = event.is_set()
        return stopped

    return wait, seen


def _poll_for_job(runner: JobRunner, kind: str = SCHEDULED_KIND, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(m.kind == kind for m in runner.jobs()):
            return
        time.sleep(0.01)
    raise AssertionError(f"no {kind!r} job appeared within {timeout}s")


# ---------------------------------------------------------------- assert_single_worker


def test_a_single_worker_with_no_reload_is_fine() -> None:
    assert_single_worker(1, reload=False)


@pytest.mark.parametrize(("workers", "reload"), [(2, False), (1, True), (3, True)])
def test_more_than_one_worker_or_reload_is_refused(workers: int, reload: bool) -> None:
    with pytest.raises(RuntimeError):
        assert_single_worker(workers, reload=reload)


# ---------------------------------------------------------------- scheduled_argv


def test_the_argv_is_fixed_and_never_carries_an_accept_flag() -> None:
    argv = scheduled_argv()

    assert argv == ["run", "--scheduled", "--apply"]
    assert "--accept-shrink" not in argv
    assert "--accept-health" not in argv


# ---------------------------------------------------------------- fire_now


def test_fire_now_records_the_fire_and_submits_the_fixed_job(config_path: Path, runner: JobRunner) -> None:
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))

    fire_now(runner, config_path, now=clock)

    _poll_for_job(runner)
    with SqliteState(config_path.parent / "state.sqlite") as state:
        assert state.last_scheduled_fire() == clock.value
    job = next(m for m in runner.jobs() if m.kind == SCHEDULED_KIND)
    assert job.argv[-3:] == ["run", "--scheduled", "--apply"]


# ---------------------------------------------------------------- the loop: fires on schedule


def test_the_scheduler_fires_at_the_next_slot(config_path: Path, runner: JobRunner) -> None:
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))  # next "20 */6 * * *" fire: 12:20
    wait, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait)

    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler never fired"
    finally:
        scheduler.stop(timeout=5.0)
    _poll_for_job(runner)
    assert clock.value == datetime(2026, 9, 24, 12, 20, tzinfo=UTC)


def test_a_config_change_is_picked_up_within_one_recheck_interval(config_path: Path, runner: JobRunner) -> None:
    # The original schedule fires only at 18:20 - far away. The rewritten one fires at 12:05, five
    # minutes after "now" - *before* the old target. A fix that only recomputes once (at the top of
    # a wait, then sleeps in chunks toward that fixed target) would still fire at 18:20, since it
    # never re-derives the fire from the rewritten config; a correct one fires at 12:05.
    config_path.write_text(CONFIG.replace('cron = "20 */6 * * *"', 'cron = "20 18 * * *"'))
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    park, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    rewritten = threading.Event()

    def wait(event: threading.Event, timeout: float) -> bool:
        if not rewritten.is_set():
            # `CONFIG.replace(...)`, not the current file's text: the pristine template always
            # contains "20 */6 * * *", so this write always lands the new cron regardless of
            # what the file was rewritten to just above.
            config_path.write_text(CONFIG.replace('cron = "20 */6 * * *"', 'cron = "5 12 * * *"'))
            rewritten.set()
        return park(event, timeout)

    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait, recheck_s=60.0)
    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler never fired"
    finally:
        scheduler.stop(timeout=5.0)
    assert clock.value == datetime(2026, 9, 24, 12, 5, tzinfo=UTC)


def test_an_edit_whose_next_fire_has_already_passed_does_not_fire_on_save(config_path: Path, runner: JobRunner) -> None:
    # The fire is recomputed from the *last* fire (`reference`), so an edit can yield a slot that
    # is already behind "now": started at 10:00 waiting for 18:20, edited at 12:00 to "daily at
    # 11:00". next_fire(10:00) is 11:00 today - an hour ago. Firing it would turn saving a
    # schedule (even a less frequent one, which needs no confirm) into an unattended apply. The
    # passed slot is skipped; the next real one is 11:00 tomorrow.
    config_path.write_text(CONFIG.replace('cron = "20 */6 * * *"', 'cron = "20 18 * * *"'))
    clock = FakeClock(datetime(2026, 9, 24, 10, 0, tzinfo=UTC))
    park, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    edited = threading.Event()

    def wait(event: threading.Event, timeout: float) -> bool:
        if not edited.is_set():
            config_path.write_text(CONFIG.replace('cron = "20 */6 * * *"', 'cron = "0 11 * * *"'))
            clock.value = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
            edited.set()
            return event.is_set()
        return park(event, timeout)

    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait, recheck_s=60.0)
    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler never fired"
    finally:
        scheduler.stop(timeout=5.0)
    assert clock.value == datetime(2026, 9, 25, 11, 0, tzinfo=UTC)


def test_paused_still_fires_the_fixed_job_the_child_reads_the_pause(config_path: Path, runner: JobRunner) -> None:
    # Recommendation (issue #68 phase 2): one code path. The scheduler never checks `enabled`
    # itself; `run --scheduled --apply` does, and publishes PAUSED without doing any work.
    text = config_path.read_text().replace("[schedule]\n", "[schedule]\nenabled = false\n")
    config_path.write_text(text)
    clock = FakeClock(datetime(2026, 9, 24, 18, 20, 1, tzinfo=UTC))
    wait, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait)

    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler never fired"
    finally:
        scheduler.stop(timeout=5.0)
    _poll_for_job(runner)
    job = next(m for m in runner.jobs() if m.kind == SCHEDULED_KIND)
    assert job.argv[-3:] == ["run", "--scheduled", "--apply"]


def test_stop_returns_promptly_without_waiting_for_a_far_off_fire(config_path: Path, runner: JobRunner) -> None:
    clock = FakeClock(datetime(2026, 9, 24, 0, 0, tzinfo=UTC))  # fire not due for 20h
    wait, _fired = _wait_that_parks_after_n_fires(runner, clock, fires=0)  # never fires before stop()
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait)

    scheduler.start()
    time.sleep(0.05)  # let the thread reach its first (parked) wait
    started = time.monotonic()
    scheduler.stop(timeout=5.0)

    assert time.monotonic() - started < 2.0
    assert not any(m.kind == SCHEDULED_KIND for m in runner.jobs())


def test_a_transient_sqlite_error_does_not_kill_the_scheduler_thread(
    config_path: Path, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sqlite3.OperationalError` is not an `OSError`; before this fix it escaped every `except`
    in the loop and ended the thread for good - the next fire, and every one after it, silently
    never happened."""
    import sqlite3

    import likearr.web.schedule as schedule_module

    real_sqlite_state = schedule_module.SqliteState
    calls = 0

    class FlakyState:
        def __init__(self, path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            self._inner = real_sqlite_state(path)

        def __enter__(self) -> Any:
            return self._inner.__enter__()

        def __exit__(self, *exc_info: object) -> None:
            self._inner.__exit__(*exc_info)

    monkeypatch.setattr(schedule_module, "SqliteState", FlakyState)
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    wait, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    # recheck_s stays a normal-sized value: with the fake clock this costs no real time either
    # way, and a tiny one would force thousands of chunks to cover the ~20-minute wait to the
    # first fire (each one loading config.toml), which is slow for no reason in a real interpreter.
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock, wait=wait, recheck_s=60.0)

    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler thread died instead of retrying after the sqlite error"
    finally:
        scheduler.stop(timeout=5.0)
    assert calls >= 2, "the fake never got a chance to succeed"


# ---------------------------------------------------------------- missed-fire catch-up


def test_no_catchup_on_a_first_ever_start(config_path: Path, runner: JobRunner) -> None:
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock)

    assert scheduler._catchup_time() is None


def test_a_missed_fire_catches_up_once_five_minutes_after_startup(config_path: Path, runner: JobRunner) -> None:
    # Last fire recorded at 00:20; the schedule's next fire after that (06:20) is already in the
    # past relative to "now" (12:00), so at least one fire was missed.
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.record_scheduled_fire(datetime(2026, 9, 24, 0, 20, tzinfo=UTC))
    clock = FakeClock(datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    wait, fired = _wait_that_parks_after_n_fires(runner, clock, fires=1)
    scheduler = Scheduler(
        config_path=config_path, runner=runner, now=clock, wait=wait, startup_delay_s=MISSED_FIRE_DELAY_S
    )

    scheduler.start()
    try:
        assert fired.wait(2.0), "the scheduler never caught up"
    finally:
        scheduler.stop(timeout=5.0)
    assert clock.value == datetime(2026, 9, 24, 12, 5, tzinfo=UTC)


def test_no_catchup_when_nothing_was_missed(config_path: Path, runner: JobRunner) -> None:
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.record_scheduled_fire(datetime(2026, 9, 24, 17, 0, tzinfo=UTC))  # next due: 18:20, still future
    clock = FakeClock(datetime(2026, 9, 24, 17, 30, tzinfo=UTC))
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock)

    assert scheduler._catchup_time() is None


# ------------------------------------------------------- a cancelled fire is due at its own time (phase 3)


def test_a_cancelled_fire_is_due_at_its_own_recorded_time_not_the_next_slot(
    config_path: Path, runner: JobRunner
) -> None:
    # A redeploy's SIGTERM caught the 12:20 fire's child still planning: that exact slot never
    # ran, so it - not the next one at 18:20 - is what the catch-up owes.
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.record_scheduled_fire(datetime(2026, 9, 24, 12, 20, tzinfo=UTC))
        state.mark_scheduled_fire_cancelled()
    clock = FakeClock(datetime(2026, 9, 24, 12, 25, tzinfo=UTC))
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock)

    assert scheduler._catchup_time() == clock.value + timedelta(seconds=MISSED_FIRE_DELAY_S)


def test_a_cancelled_fire_still_in_the_future_relative_to_now_is_not_yet_due(
    config_path: Path, runner: JobRunner
) -> None:
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.record_scheduled_fire(datetime(2026, 9, 24, 12, 20, tzinfo=UTC))
        state.mark_scheduled_fire_cancelled()
    clock = FakeClock(datetime(2026, 9, 24, 10, 0, tzinfo=UTC))  # "now" is before the cancelled fire
    scheduler = Scheduler(config_path=config_path, runner=runner, now=clock)

    assert scheduler._catchup_time() is None


def test_a_fresh_fire_clears_an_earlier_cancelled_mark(config_path: Path) -> None:
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.record_scheduled_fire(datetime(2026, 9, 24, 12, 20, tzinfo=UTC))
        state.mark_scheduled_fire_cancelled()
        assert state.scheduled_fire_cancelled()

        state.record_scheduled_fire(datetime(2026, 9, 24, 18, 20, tzinfo=UTC))

        assert not state.scheduled_fire_cancelled()


def test_mark_scheduled_fire_cancelled_is_a_no_op_with_no_row(config_path: Path) -> None:
    with SqliteState(config_path.parent / "state.sqlite") as state:
        state.mark_scheduled_fire_cancelled()

        assert state.last_scheduled_fire() is None
        assert not state.scheduled_fire_cancelled()
