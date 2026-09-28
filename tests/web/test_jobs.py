"""The job runner, against a fake CLI that runs as a real child process."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.lock import run_lock
from likearr.models import PHASE_MARKER_APPLY
from likearr.web.jobs import (
    JOB_PHASE_APPLY,
    KEEP_JOBS,
    SCHEDULED_KIND,
    JobMeta,
    JobRefused,
    JobRunner,
    JobState,
    new_job_id,
    state_for_exit,
    valid_job_id,
)

FAKE_CLI = """
import os, sys, time
args = sys.argv[1:]
cmd = args[0]
if cmd == "ok":
    print("the answer")
    print("a log line", file=sys.stderr)
elif cmd == "exit":
    sys.exit(int(args[1]))
elif cmd == "sleep":
    print("started", file=sys.stderr, flush=True)
    time.sleep(float(args[1]))
elif cmd == "lock":
    print("another run already holds the lock: /data/likearr.lock", file=sys.stderr)
    sys.exit(4)
elif cmd == "says-lock-fails":
    print("error: the log mentions who holds the lock, but the run failed", file=sys.stderr)
    sys.exit(1)
elif cmd == "echo-args":
    print(repr(args[1:]))
elif cmd == "env":
    print(os.environ.get(args[1], "<unset>"))
elif cmd == "secret-log":
    print("refresh failed: access_token=abcdefghijklmnop0123456789", file=sys.stderr)
elif cmd == "names":
    print("Basic Channel - Bearer of Bad News - Authorization: Denied")
elif cmd == "leak-key":
    print("oops " + os.environ["LIKEARR_LIDARR_API_KEY"])
elif cmd == "leak-key-log":
    print("Traceback: headers were " + os.environ["LIKEARR_LIDARR_API_KEY"], file=sys.stderr)
elif cmd == "long-log":
    sys.stderr.write("x" * 70000 + " access_token=abcdefghijklmnop0123456789\\n")
    sys.stderr.write("the last line\\n")
elif cmd == "hold-lock":
    from likearr.adapters.lock import run_lock
    print("holding", file=sys.stderr, flush=True)
    with run_lock(args[1]):
        time.sleep(float(args[2]))
elif cmd == "plan-then-sleep":
    # Still planning: never prints the apply-phase marker.
    print("planning", file=sys.stderr, flush=True)
    time.sleep(float(args[1]))
elif cmd == "apply-then-sleep":
    from likearr.models import PHASE_MARKER_APPLY
    print(PHASE_MARKER_APPLY, file=sys.stderr, flush=True)
    time.sleep(float(args[1]))
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return [sys.executable, str(script)]


@pytest.fixture
def runner(tmp_path: Path, fake_cli: list[str]) -> Iterator[JobRunner]:
    runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "likearr.lock")
    yield runner
    # Most tests already join every job they start (`_finish`, `cancel` + `_finish`, or their own
    # `shutdown`); this is the safety net for the rest, and for whatever the next test adds.
    runner.shutdown(timeout=5)


def _finish(runner: JobRunner, job_id: str) -> None:
    assert runner.wait(job_id, timeout=20)


# ---------------------------------------------------------------- ids


def test_a_new_job_id_has_the_validated_shape() -> None:
    job_id = new_job_id(datetime(2026, 9, 22, 14, 3, 11, tzinfo=UTC))

    assert job_id.startswith("2026-09-22T14-03-11Z-")
    assert valid_job_id(job_id)


@pytest.mark.parametrize(
    "candidate",
    [
        "../etc",
        "2026-09-22T14-03-11Z-a1b2c",
        "2026-09-22T14-03-11Z-a1b2c3/..",
        "2026-09-22T14-03-11Z-A1B2C3",
        "2026-09-22T14-03-11Z-a1b2c3\n",
        "",
    ],
)
def test_anything_else_is_not_a_job_id(candidate: str) -> None:
    assert not valid_job_id(candidate)


def test_an_invalid_id_never_reaches_the_filesystem(runner: JobRunner) -> None:
    assert runner.get("../../etc/passwd") is None
    assert runner.log_tail("../../etc/passwd") == ""
    assert runner.output("../../etc/passwd") == ""


# ---------------------------------------------------------------- exit codes


@pytest.mark.parametrize(
    ("exit_code", "cancelled", "state"),
    [
        (0, False, JobState.DONE),
        (2, False, JobState.GUARDED),
        (3, False, JobState.STALE),
        (4, False, JobState.BUSY),
        (1, False, JobState.FAILED),
        (-15, True, JobState.CANCELLED),
        (0, True, JobState.DONE),  # finished just as Cancel was clicked: it still finished
        (-9, False, JobState.FAILED),
    ],
)
def test_exit_codes_map_to_states(exit_code: int, cancelled: bool, state: JobState) -> None:
    assert state_for_exit(exit_code, cancelled=cancelled) is state


# ---------------------------------------------------------------- running


def test_a_job_runs_as_a_child_and_its_outputs_land_in_its_directory(runner: JobRunner, tmp_path: Path) -> None:
    meta = runner.start("explain", ["ok"], label="Radiohead")
    _finish(runner, meta.id)

    job_dir = tmp_path / "ui" / "jobs" / meta.id
    assert (job_dir / "out.txt").read_text() == "the answer\n"
    assert (job_dir / "log.txt").read_text() == "a log line\n"
    saved = json.loads((job_dir / "meta.json").read_text())
    assert saved["state"] == "done"
    assert saved["exit_code"] == 0
    assert saved["kind"] == "explain"
    assert saved["label"] == "Radiohead"
    assert saved["finished_at"]
    assert runner.current() is None


def test_arguments_reach_the_child_as_a_list_with_no_shell(runner: JobRunner) -> None:
    meta = runner.start("explain", ["echo-args", "--", "-v; rm -rf / $(whoami)"])
    _finish(runner, meta.id)

    assert runner.output(meta.id).strip() == repr(["--", "-v; rm -rf / $(whoami)"])


def test_the_child_never_inherits_the_ui_password(runner: JobRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "passed-through")

    first = runner.start("explain", ["env", "LIKEARR_UI_PASSWORD"])
    _finish(runner, first.id)
    second = runner.start("explain", ["env", "LIKEARR_LIDARR_API_KEY"])
    _finish(runner, second.id)

    assert runner.output(first.id).strip() == "<unset>"
    assert runner.output(second.id).strip() == "passed-through"


def test_only_one_job_runs_at_a_time(runner: JobRunner) -> None:
    first = runner.start("explain", ["sleep", "5"])
    try:
        with pytest.raises(JobRefused, match="already running"):
            runner.start("explain", ["ok"])
        assert runner.current() is not None
        assert runner.current().id == first.id  # type: ignore[union-attr]
    finally:
        runner.cancel(first.id)
        _finish(runner, first.id)


def test_a_cancelled_job_is_recorded_as_cancelled(runner: JobRunner) -> None:
    meta = runner.start("explain", ["sleep", "30"])
    assert runner.cancel(meta.id)
    _finish(runner, meta.id)

    assert runner.get(meta.id).state is JobState.CANCELLED  # type: ignore[union-attr]


def test_a_child_that_reports_the_run_lock_held_is_busy_not_failed(runner: JobRunner) -> None:
    meta = runner.start("run", ["lock"])
    _finish(runner, meta.id)

    assert runner.get(meta.id).state is JobState.BUSY  # type: ignore[union-attr]


def test_busy_comes_from_the_exit_code_never_from_words_in_the_log(runner: JobRunner) -> None:
    """The phrase "holds the lock" in a failed child's log no longer reads as busy."""
    meta = runner.start("run", ["says-lock-fails"])
    _finish(runner, meta.id)

    assert runner.get(meta.id).state is JobState.FAILED  # type: ignore[union-attr]


def test_the_log_tail_is_redacted(runner: JobRunner) -> None:
    meta = runner.start("explain", ["secret-log"])
    _finish(runner, meta.id)

    tail = runner.log_tail(meta.id)
    assert "abcdefghijklmnop0123456789" not in tail
    assert "access_token" in tail


# ---------------------------------------------------------------- the run lock


def test_a_run_job_is_not_spawned_while_the_run_lock_is_held(runner: JobRunner, tmp_path: Path) -> None:
    with run_lock(tmp_path / "likearr.lock"), pytest.raises(JobRefused, match="another likearr command holds the run lock"):
        runner.start("run", ["ok"], needs_run_lock=True)

    assert not (tmp_path / "ui" / "jobs").exists() or not any((tmp_path / "ui" / "jobs").iterdir())


def test_the_lock_check_releases_the_lock_before_the_child_starts(runner: JobRunner, tmp_path: Path) -> None:
    meta = runner.start("run", ["ok"], needs_run_lock=True)
    _finish(runner, meta.id)

    with run_lock(tmp_path / "likearr.lock"):  # would raise LockHeld if the server kept it
        pass
    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]


# ---------------------------------------------------------------- submit_scheduled


def test_submit_scheduled_starts_at_once_when_the_slot_is_free(runner: JobRunner) -> None:
    runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")

    for _ in range(200):
        if runner.current() is not None:
            break
        time.sleep(0.01)
    meta = runner.current()
    assert meta is not None and meta.kind == "scheduled"
    _finish(runner, meta.id)
    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]


def test_submit_scheduled_queues_behind_a_running_job_then_starts(runner: JobRunner) -> None:
    first = runner.start("explain", ["sleep", "0.3"])
    runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")

    _finish(runner, first.id)  # the sleep job
    for _ in range(300):
        jobs = [m for m in runner.jobs() if m.kind == "scheduled"]
        if jobs and jobs[0].finished:
            break
        time.sleep(0.02)
    scheduled = [m for m in runner.jobs() if m.kind == "scheduled"]
    assert scheduled, "the queued job never showed up"
    assert scheduled[0].state is JobState.DONE


def test_submit_scheduled_gives_up_and_is_recorded_skipped_after_the_queue_wait(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    short_runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "likearr.lock", queue_wait_s=0.2)
    first = short_runner.start("explain", ["sleep", "5"])
    try:
        short_runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")

        skipped = None
        for _ in range(300):
            skipped = next((m for m in short_runner.jobs() if m.kind == "scheduled"), None)
            if skipped is not None and skipped.finished:
                break
            time.sleep(0.02)
        assert skipped is not None
        assert skipped.state is JobState.SKIPPED
        assert skipped.exit_code is None
        assert "waited" in (tmp_path / "ui" / "jobs" / skipped.id / "log.txt").read_text()
    finally:
        short_runner.cancel(first.id)
        _finish(short_runner, first.id)


def test_submit_scheduled_is_skipped_at_once_when_the_run_lock_is_held_externally(
    runner: JobRunner, tmp_path: Path
) -> None:
    with run_lock(tmp_path / "likearr.lock"):
        runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")
        skipped = None
        for _ in range(200):
            skipped = next((m for m in runner.jobs() if m.kind == "scheduled"), None)
            if skipped is not None:
                break
            time.sleep(0.01)
    assert skipped is not None
    assert skipped.state is JobState.SKIPPED
    assert "another likearr command holds the run lock" in (tmp_path / "ui" / "jobs" / skipped.id / "log.txt").read_text()


def test_a_ui_start_is_refused_while_a_scheduled_job_runs(runner: JobRunner) -> None:
    runner.submit_scheduled("scheduled", ["sleep", "0.3"], label="Scheduled run")
    for _ in range(200):
        if runner.current() is not None:
            break
        time.sleep(0.01)
    current = runner.current()
    assert current is not None and current.kind == "scheduled"

    with pytest.raises(JobRefused, match="already running"):
        runner.start("plan", ["ok"])

    _finish(runner, current.id)


def test_a_scheduled_fire_queues_rather_than_skips_when_our_own_job_holds_the_run_lock(
    runner: JobRunner, tmp_path: Path
) -> None:
    """The run lock is genuinely held (a real `flock`, not a fake), but by a job *this* JobRunner
    started (a UI "run" job) - not by something external (the host cron line). It must queue
    behind that job, not be misread as an external collision and skipped at once: probing while
    the slot is busy would see the lock held either way, so the probe only ever runs when the
    slot is free.
    """
    holder = runner.start("run", ["hold-lock", str(tmp_path / "likearr.lock"), "0.3"])

    runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")

    scheduled = None
    for _ in range(400):
        scheduled = next((m for m in runner.jobs() if m.kind == "scheduled"), None)
        if scheduled is not None and scheduled.finished:
            break
        time.sleep(0.02)
    assert scheduled is not None, "no scheduled job record appeared"
    assert scheduled.state is JobState.DONE, f"queued and started rather than skipped at once: {scheduled.state}"
    _finish(runner, holder.id)


def test_a_queued_scheduled_submission_is_recorded_skipped_on_shutdown(runner: JobRunner, tmp_path: Path) -> None:
    """A `scheduled` submission still waiting in the queue when the server shuts down must not
    vanish with no record: `shutdown` (SIGTERM) refuses every new job, and before this fix
    `_run_queued` returned silently on that refusal - the missed-fire catch-up and Status would
    never know the fire happened at all."""
    holder = runner.start("explain", ["sleep", "5"])
    runner.submit_scheduled("scheduled", ["ok"], label="Scheduled run")
    for _ in range(200):
        if runner.current() is not None and runner.current().kind == "explain":  # type: ignore[union-attr]
            break
        time.sleep(0.01)

    runner.shutdown(timeout=0.1)  # refuses new jobs at once; does not wait for a non-draining job

    skipped = None
    for _ in range(200):
        skipped = next((m for m in runner.jobs() if m.kind == "scheduled"), None)
        if skipped is not None:
            break
        time.sleep(0.01)
    assert skipped is not None, "the queued submission was dropped with no record"
    assert skipped.state is JobState.SKIPPED
    assert "shutting down" in (tmp_path / "ui" / "jobs" / skipped.id / "log.txt").read_text()
    runner.cancel(holder.id)
    _finish(runner, holder.id)


# ---------------------------------------------------------------- restart, shutdown, pruning


def _plant(root: Path, job_id: str, state: str, kind: str = "explain") -> Path:
    job_dir = root / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": kind,
        "argv": [],
        "label": "",
        "started_at": "2026-09-22T14:03:11+00:00",
        "finished_at": None,
        "exit_code": None,
        "state": state,
        "drain": False,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    return job_dir


def test_a_job_still_running_at_startup_is_marked_interrupted(tmp_path: Path, fake_cli: list[str]) -> None:
    root = tmp_path / "ui" / "jobs"
    _plant(root, "2026-09-22T14-03-11Z-a1b2c3", "running")
    _plant(root, "2026-09-22T14-04-11Z-a1b2c4", "done")

    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock")
    interrupted = runner.recover()

    assert interrupted == ["2026-09-22T14-03-11Z-a1b2c3"]
    assert runner.get("2026-09-22T14-03-11Z-a1b2c3").state is JobState.INTERRUPTED  # type: ignore[union-attr]
    assert runner.get("2026-09-22T14-04-11Z-a1b2c4").state is JobState.DONE  # type: ignore[union-attr]


def test_starting_a_job_prunes_to_the_newest_twenty(tmp_path: Path, runner: JobRunner) -> None:
    root = tmp_path / "ui" / "jobs"
    for i in range(KEEP_JOBS + 5):
        _plant(root, f"2026-09-01T00-00-{i:02d}Z-aaaaaa", "done")
    stranger = root / "not-a-job"
    stranger.mkdir()

    meta = runner.start("explain", ["ok"])
    _finish(runner, meta.id)

    kept = sorted(p.name for p in root.iterdir() if valid_job_id(p.name))
    assert len(kept) == KEEP_JOBS
    assert meta.id in kept
    assert "2026-09-01T00-00-00Z-aaaaaa" not in kept
    assert stranger.exists()


def test_shutdown_refuses_new_jobs_and_interrupts_a_job_that_need_not_drain(runner: JobRunner) -> None:
    meta = runner.start("explain", ["sleep", "30"])

    runner.shutdown(timeout=10)

    assert runner.get(meta.id).state is JobState.INTERRUPTED  # type: ignore[union-attr]
    with pytest.raises(JobRefused, match="shutting down"):
        runner.start("explain", ["ok"])


def test_shutdown_waits_for_a_job_that_must_drain(runner: JobRunner) -> None:
    meta = runner.start("run", ["sleep", "1"], drain=True)

    started = time.monotonic()
    runner.shutdown(timeout=20)

    assert time.monotonic() - started >= 0.5
    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]


# ------------------------------------------------------- scheduled shutdown by phase


def _wait_for_phase(runner: JobRunner, meta: JobMeta, timeout: float = 2.0) -> None:
    """Give the child time to write its first stderr line before shutdown races it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runner.log_tail(meta.id).strip():
            return
        time.sleep(0.01)
    raise AssertionError(f"job {meta.id} never printed anything")


def test_a_scheduled_job_still_planning_is_cancelled_on_shutdown(runner: JobRunner) -> None:
    meta = runner.start(SCHEDULED_KIND, ["plan-then-sleep", "30"], needs_run_lock=True, drain=True)
    _wait_for_phase(runner, meta)

    runner.shutdown(timeout=10)

    finished = runner.get(meta.id)
    assert finished is not None
    assert finished.state is JobState.INTERRUPTED
    assert finished.phase != JOB_PHASE_APPLY
    assert "cancelled for shutdown during planning" in runner.log_tail(meta.id)


def test_a_scheduled_job_in_the_apply_phase_is_drained_on_shutdown(runner: JobRunner) -> None:
    meta = runner.start(SCHEDULED_KIND, ["apply-then-sleep", "0.5"], needs_run_lock=True, drain=True)
    _wait_for_phase(runner, meta)

    started = time.monotonic()
    runner.shutdown(timeout=10)

    assert time.monotonic() - started >= 0.3
    finished = runner.get(meta.id)
    assert finished is not None
    assert finished.state is JobState.DONE
    assert finished.phase == JOB_PHASE_APPLY


def test_a_scheduled_job_that_printed_nothing_yet_is_cancelled_not_drained(runner: JobRunner) -> None:
    """The child that hasn't printed anything (no `plan-then-sleep`-style first line) is still
    safely treated as planning: the default phase is never "apply" unless the marker was seen."""
    meta = runner.start(SCHEDULED_KIND, ["sleep", "30"], needs_run_lock=True, drain=True)
    time.sleep(0.1)

    started = time.monotonic()
    runner.shutdown(timeout=10)

    assert time.monotonic() - started < 2.0
    finished = runner.get(meta.id)
    assert finished is not None
    assert finished.state is JobState.INTERRUPTED
    assert finished.phase != JOB_PHASE_APPLY


def test_the_apply_marker_is_found_even_past_the_tail_window(runner: JobRunner, tmp_path: Path) -> None:
    """A long apply can write well past `_TAIL_BYTES` of stderr after the marker; the phase check
    must not be fooled into reading that as still planning."""
    meta = runner.start(SCHEDULED_KIND, ["ok"], needs_run_lock=True, drain=True)
    _finish(runner, meta.id)
    log_path = tmp_path / "ui" / "jobs" / meta.id / "log.txt"
    log_path.write_bytes(PHASE_MARKER_APPLY.encode() + b"\n" + b"x" * 70_000)
    current = runner.get(meta.id)
    assert current is not None

    phase, cancelled = runner._scheduled_phase(current)

    assert phase == JOB_PHASE_APPLY
    assert not cancelled


def test_an_unreadable_log_is_read_as_applying_so_the_job_is_drained(runner: JobRunner, tmp_path: Path) -> None:
    """Unreadable proves nothing, so the phase check fails safe: drain, never cancel a job that
    may already be writing to Lidarr."""
    meta = runner.start(SCHEDULED_KIND, ["ok"], needs_run_lock=True, drain=True)
    _finish(runner, meta.id)
    log_path = tmp_path / "ui" / "jobs" / meta.id / "log.txt"
    log_path.unlink()
    log_path.mkdir()  # read_bytes() on a directory raises IsADirectoryError, an OSError
    current = runner.get(meta.id)
    assert current is not None

    phase, cancelled = runner._scheduled_phase(current)

    assert phase == JOB_PHASE_APPLY
    assert not cancelled


def test_a_ui_apply_job_still_always_drains_regardless_of_phase(runner: JobRunner) -> None:
    """Only `SCHEDULED_KIND` uses the phase; a UI apply job's static `drain=True` is unaffected."""
    meta = runner.start("apply", ["sleep", "1"], drain=True)

    started = time.monotonic()
    runner.shutdown(timeout=20)

    assert time.monotonic() - started >= 0.5
    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]


def test_a_job_recovered_still_planning_after_a_hard_restart_is_interrupted_with_the_reason(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    root = tmp_path / "ui" / "jobs"
    job_dir = _plant(root, "2026-09-22T14-03-11Z-a1b2c3", "running", kind=SCHEDULED_KIND)
    (job_dir / "log.txt").write_text("planning\n")

    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock")
    interrupted = runner.recover()

    assert interrupted == ["2026-09-22T14-03-11Z-a1b2c3"]
    meta = runner.get("2026-09-22T14-03-11Z-a1b2c3")
    assert meta is not None
    assert meta.state is JobState.INTERRUPTED
    assert meta.phase != JOB_PHASE_APPLY
    assert "cancelled for shutdown during planning" in (job_dir / "log.txt").read_text()


def test_a_job_recovered_already_in_the_apply_phase_is_not_marked_cancelled(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    root = tmp_path / "ui" / "jobs"
    job_dir = _plant(root, "2026-09-22T14-03-11Z-a1b2c3", "running", kind=SCHEDULED_KIND)
    (job_dir / "log.txt").write_text(f"planning\n{PHASE_MARKER_APPLY}\n")

    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock")
    runner.recover()

    meta = runner.get("2026-09-22T14-03-11Z-a1b2c3")
    assert meta is not None
    assert meta.phase == JOB_PHASE_APPLY
    assert "cancelled for shutdown during planning" not in (job_dir / "log.txt").read_text()


def test_jobs_are_listed_newest_first(runner: JobRunner) -> None:
    first = runner.start("explain", ["ok"])
    _finish(runner, first.id)
    time.sleep(1.1)  # ids carry whole seconds
    second = runner.start("explain", ["ok"])
    _finish(runner, second.id)

    assert [m.id for m in runner.jobs()] == [second.id, first.id]


def test_a_job_directory_is_private_to_the_service_user(runner: JobRunner, tmp_path: Path) -> None:
    meta = runner.start("explain", ["ok"])
    _finish(runner, meta.id)

    mode = os.stat(tmp_path / "ui" / "jobs" / meta.id).st_mode & 0o777
    assert mode == 0o700


# ---------------------------------------------------------------- review fixes


def test_output_is_the_childs_stdout_verbatim(runner: JobRunner) -> None:
    # It is parsed (the playlists JSON) as well as shown, so it must not be rewritten.
    meta = runner.start("explain", ["names"])
    _finish(runner, meta.id)

    assert runner.output(meta.id) == "Basic Channel - Bearer of Bad News - Authorization: Denied\n"
    assert "Basic Channel - Bearer of Bad News" in runner.shown_output(meta.id)


def test_shown_output_still_hides_a_secret_from_the_environment(
    runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "lidarr-key-0123456789abcdef")
    meta = runner.start("explain", ["leak-key"])
    _finish(runner, meta.id)

    assert "lidarr-key-0123456789abcdef" not in runner.shown_output(meta.id)


def test_an_argument_popen_refuses_is_a_refusal_not_a_stuck_job(runner: JobRunner) -> None:
    with pytest.raises(JobRefused):
        runner.start("explain", ["echo-args", "Radio\x00head"])

    (meta,) = runner.jobs()
    assert meta.state is JobState.FAILED
    assert runner.current() is None
    second = runner.start("explain", ["ok"])  # nothing is wedged
    _finish(runner, second.id)


def test_a_job_whose_directory_vanishes_does_not_wedge_the_runner(runner: JobRunner, tmp_path: Path) -> None:
    import shutil

    meta = runner.start("explain", ["sleep", "0.5"])
    shutil.rmtree(tmp_path / "ui" / "jobs" / meta.id)
    _finish(runner, meta.id)

    assert runner.current() is None
    second = runner.start("explain", ["ok"])
    _finish(runner, second.id)
    assert runner.get(second.id).state is JobState.DONE  # type: ignore[union-attr]


def test_a_stopped_job_gets_longer_than_a_token_save_can_take_before_sigkill() -> None:
    # Held-back SIGTERM + SIGKILL escalation must never meet inside Spotify's token save window.
    from likearr.adapters.spotify import TOKEN_REQUEST_WORST_CASE_S
    from likearr.web import jobs

    assert jobs.STOP_WAIT_S > TOKEN_REQUEST_WORST_CASE_S


def test_cancel_returns_at_once_and_the_job_shows_as_stopping(runner: JobRunner, tmp_path: Path) -> None:
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1), sys.exit(143)))\n"
        "print('ready', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n"
    )
    stubborn = JobRunner(tmp_path / "ui" / "jobs2", [sys.executable, str(script)], lock_path=tmp_path / "l.lock")
    meta = stubborn.start("explain", [])
    deadline = time.monotonic() + 10
    while "ready" not in stubborn.log_tail(meta.id):
        assert time.monotonic() < deadline
        time.sleep(0.05)

    started = time.monotonic()
    assert stubborn.cancel(meta.id)
    assert time.monotonic() - started < 0.5
    assert stubborn.stopping(meta.id)
    _finish(stubborn, meta.id)
    assert stubborn.get(meta.id).state is JobState.CANCELLED  # type: ignore[union-attr]
    assert not stubborn.stopping(meta.id)


def test_the_log_tail_also_hides_a_secret_from_the_environment(
    runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A traceback can print a value no pattern recognises; the exact value is still removed.
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "lidarr-key-0123456789abcdef")
    meta = runner.start("explain", ["leak-key-log"])
    _finish(runner, meta.id)

    tail = runner.log_tail(meta.id)
    assert "Traceback" in tail
    assert "lidarr-key-0123456789abcdef" not in tail


def test_the_log_tail_drops_the_line_the_byte_cut_split(runner: JobRunner) -> None:
    # Cutting the file at 64 KB can split a label from its value; the partial first line is dropped
    # rather than shown with a secret the patterns can no longer tie to its label.
    meta = runner.start("explain", ["long-log"])
    _finish(runner, meta.id)

    tail = runner.log_tail(meta.id)
    assert "the last line" in tail
    assert "abcdefghijklmnop0123456789" not in tail
    assert "xxxx" not in tail


def test_a_finish_hook_runs_for_a_job_that_finished_well(tmp_path: Path, fake_cli: list[str]) -> None:
    seen: list[Path] = []

    def hook(job_dir: Path) -> dict[str, str]:
        seen.append(job_dir)
        return {}

    runner = JobRunner(
        tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "l.lock", finish_hooks={"playlists": hook}
    )
    good = runner.start("playlists", ["ok"])
    _finish(runner, good.id)
    bad = runner.start("playlists", ["exit", "1"])
    _finish(runner, bad.id)
    other = runner.start("explain", ["ok"])
    _finish(runner, other.id)

    assert seen == [tmp_path / "ui" / "jobs" / good.id]


def test_a_failing_finish_hook_still_records_the_job(tmp_path: Path, fake_cli: list[str]) -> None:
    def hook(job_dir: Path) -> dict[str, str]:
        raise ValueError("unreadable answer")

    runner = JobRunner(
        tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "l.lock", finish_hooks={"playlists": hook}
    )
    meta = runner.start("playlists", ["ok"])
    _finish(runner, meta.id)

    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]
    assert runner.current() is None


def test_an_argument_can_name_the_jobs_own_directory(runner: JobRunner, tmp_path: Path) -> None:
    # A plan writes its diff into its own job directory, which does not exist until the job does.
    meta = runner.start("plan", ["echo-args", "--out", "{job_dir}/diff.json"], expand_job_dir=True)
    _finish(runner, meta.id)

    job_dir = tmp_path / "ui" / "jobs" / meta.id
    assert runner.output(meta.id).strip() == repr(["--out", f"{job_dir}/diff.json"])
    assert f"{job_dir}/diff.json" in runner.get(meta.id).argv  # type: ignore[union-attr]


def test_free_text_that_happens_to_name_the_placeholder_is_left_alone(runner: JobRunner) -> None:
    meta = runner.start("explain", ["echo-args", "--", "{job_dir}"])
    _finish(runner, meta.id)

    assert runner.output(meta.id).strip() == repr(["--", "{job_dir}"])


def test_a_finish_hook_adds_to_a_job_that_finished_well(tmp_path: Path, fake_cli: list[str]) -> None:
    seen: list[Path] = []

    def hook(job_dir: Path) -> dict[str, str]:
        seen.append(job_dir)
        return {"plan_token": "t0k3n"}

    runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "l.lock", finish_hooks={"plan": hook})
    good = runner.start("plan", ["ok"])
    _finish(runner, good.id)
    bad = runner.start("plan", ["exit", "1"])
    _finish(runner, bad.id)
    other = runner.start("explain", ["ok"])
    _finish(runner, other.id)

    assert runner.get(good.id).plan_token == "t0k3n"  # type: ignore[union-attr]
    assert runner.get(bad.id).plan_token == ""  # type: ignore[union-attr]
    assert runner.get(other.id).plan_token == ""  # type: ignore[union-attr]
    assert seen == [tmp_path / "ui" / "jobs" / good.id]


def test_a_failing_plan_finish_hook_still_records_the_plan(tmp_path: Path, fake_cli: list[str]) -> None:
    def hook(job_dir: Path) -> dict[str, str]:
        raise ValueError("unreadable diff")

    runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "l.lock", finish_hooks={"plan": hook})
    meta = runner.start("plan", ["ok"])
    _finish(runner, meta.id)

    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]
    assert runner.current() is None


def test_a_job_can_name_the_plan_it_applies(runner: JobRunner) -> None:
    meta = runner.start("apply", ["ok"], plan_id="2026-09-23T17-00-00Z-a1b2c3")
    _finish(runner, meta.id)

    assert runner.get(meta.id).plan_id == "2026-09-23T17-00-00Z-a1b2c3"  # type: ignore[union-attr]


# ---------------------------------------------------------------- pruning, cancel, session


def test_pruning_never_removes_a_plan_that_may_still_be_applied_nor_the_one_being_applied(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    root = tmp_path / "ui" / "jobs"
    kept_plan = "2026-09-01T00-00-00Z-aaaaa0"
    applied_plan = "2026-09-01T00-00-01Z-aaaaa1"
    old_plan = "2026-09-01T00-00-02Z-aaaaa2"
    _plant(root, kept_plan, "done", kind="plan")
    _plant(root, applied_plan, "done", kind="plan")
    _plant(root, old_plan, "done", kind="plan")
    for i in range(KEEP_JOBS + 5):
        _plant(root, f"2026-09-02T00-00-{i:02d}Z-bbbbbb", "done")
    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "l.lock", keep=lambda m: m.id == kept_plan)

    meta = runner.start("apply", ["ok"], drain=True, plan_id=applied_plan)
    _finish(runner, meta.id)

    kept = {p.name for p in root.iterdir()}
    assert kept_plan in kept  # the predicate says it may still be applied
    assert applied_plan in kept  # the plan this job applies
    assert old_plan not in kept
    assert meta.id in kept
    assert len([n for n in kept if n.startswith("2026-09-02")]) == KEEP_JOBS - 1


def test_a_draining_job_cannot_be_cancelled(runner: JobRunner) -> None:
    meta = runner.start("apply", ["sleep", "1"], drain=True)

    assert runner.cancel(meta.id) is False
    assert not runner.stopping(meta.id)
    _finish(runner, meta.id)
    assert runner.get(meta.id).state is JobState.DONE  # type: ignore[union-attr]


def test_a_draining_job_runs_in_its_own_session(runner: JobRunner) -> None:
    meta = runner.start("apply", ["sleep", "1"], drain=True)
    running = runner._running
    assert running is not None
    assert os.getsid(running.proc.pid) != os.getsid(0)  # a terminal's Ctrl-C cannot reach it
    _finish(runner, meta.id)

    other = runner.start("explain", ["sleep", "0.5"])
    running = runner._running
    assert running is not None
    assert os.getsid(running.proc.pid) == os.getsid(0)
    _finish(runner, other.id)


@pytest.mark.parametrize("name", ["../meta.json", "..", ".hidden.json", "a/b.json", "log.txt", ""])
def test_a_job_file_is_only_ever_a_plain_json_name(runner: JobRunner, name: str) -> None:
    meta = runner.start("explain", ["ok"])
    _finish(runner, meta.id)

    assert runner.job_file(meta.id, name, must_exist=False) is None
    assert runner.job_file(meta.id, "prune.json", must_exist=False) is not None


def test_an_after_callback_runs_once_the_slot_is_free_and_may_start_the_next_job(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    started: list[str] = []
    runner: JobRunner

    def chain(meta: JobMeta) -> None:
        assert runner.current() is None
        started.append(runner.start("playlists", ["ok"]).id)

    runner = JobRunner(tmp_path / "ui" / "jobs", fake_cli, lock_path=tmp_path / "l.lock", after={"plan": chain})
    plan = runner.start("plan", ["ok"])
    _finish(runner, plan.id)
    deadline = time.monotonic() + 10
    while not started and time.monotonic() < deadline:
        time.sleep(0.02)

    assert len(started) == 1
    _finish(runner, started[0])
    assert runner.get(started[0]).kind == "playlists"  # type: ignore[union-attr]


# ---------------------------------------------------------------- a job that outlives the server


def _plant_running(root: Path, job_id: str, *, pid: int, pid_start: str, argv: list[str], kind: str = "apply") -> Path:
    job_dir = _plant(root, job_id, "running", kind=kind)
    meta = json.loads((job_dir / "meta.json").read_text())
    meta.update(pid=pid, pid_start=pid_start, argv=argv, drain=kind == "apply")
    (job_dir / "meta.json").write_text(json.dumps(meta))
    return job_dir


class _Process:
    """A stand-in for another process's liveness, ended by the test."""

    def __init__(self) -> None:
        self.gone = False
        self.asked: list[str] = []

    def alive(self, meta: JobMeta) -> bool:
        self.asked.append(meta.id)
        return not self.gone


def _wait_until(predicate: Any, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_job_still_alive_at_startup_is_adopted_and_recorded_when_it_ends(tmp_path: Path, fake_cli: list[str]) -> None:
    root = tmp_path / "ui" / "jobs"
    job_id = "2026-09-22T14-03-11Z-a1b2c3"
    job_dir = _plant_running(root, job_id, pid=4242, pid_start="123", argv=["likearr", "run", "--apply"])
    (job_dir / "out.txt").write_text('likearr applied: 1 monitored\n{"status": "guarded", "exit_code": 2}\n')
    process = _Process()
    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock", alive=process.alive, adopted_poll_s=0.02)

    assert runner.recover() == []  # not interrupted: still at work
    adopted = runner.get(job_id)
    assert adopted is not None and adopted.state is JobState.RUNNING and adopted.adopted
    assert runner.current() == adopted
    with pytest.raises(JobRefused, match="from before likearr restarted"):
        runner.start("explain", ["ok"])

    process.gone = True
    _wait_until(lambda: runner.current() is None)

    ended = runner.get(job_id)
    assert ended is not None and ended.state is JobState.GUARDED and ended.exit_code == 2  # from its own record
    _finish(runner, runner.start("explain", ["ok"]).id)  # the slot is free again


def test_an_adopted_job_that_printed_no_record_ends_as_interrupted(tmp_path: Path, fake_cli: list[str]) -> None:
    root = tmp_path / "ui" / "jobs"
    job_id = "2026-09-22T14-03-11Z-a1b2c3"
    _plant_running(root, job_id, pid=4242, pid_start="123", argv=["likearr", "explain"], kind="explain")
    process = _Process()
    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock", alive=process.alive, adopted_poll_s=0.02)
    runner.recover()

    process.gone = True
    _wait_until(lambda: runner.current() is None)

    assert runner.get(job_id).state is JobState.INTERRUPTED  # type: ignore[union-attr]


def test_an_adopted_scheduled_job_still_planning_that_ends_unreadably_runs_the_after_callback(
    tmp_path: Path, fake_cli: list[str]
) -> None:
    """A `scheduled` job re-adopted (run bare, or systemd without `KillMode=control-group`) that
    dies without printing a record must still be able to mark its fire cancelled for the
    missed-fire catch-up, exactly as a normal restart's `recover` does - `_watch_adopted` did not
    used to call any `after` callback at all."""
    root = tmp_path / "ui" / "jobs"
    job_id = "2026-09-22T14-03-11Z-a1b2c3"
    job_dir = _plant_running(root, job_id, pid=4242, pid_start="123", argv=["likearr"], kind=SCHEDULED_KIND)
    (job_dir / "log.txt").write_text("planning\n")
    seen: list[JobMeta] = []
    process = _Process()
    runner = JobRunner(
        root,
        fake_cli,
        lock_path=tmp_path / "likearr.lock",
        alive=process.alive,
        adopted_poll_s=0.02,
        after={SCHEDULED_KIND: seen.append},
    )
    runner.recover()

    process.gone = True
    _wait_until(lambda: runner.current() is None)

    assert len(seen) == 1
    assert seen[0].state is JobState.INTERRUPTED
    assert seen[0].phase != JOB_PHASE_APPLY
    assert "cancelled for shutdown during planning" in (job_dir / "log.txt").read_text()


def test_shutdown_leaves_an_adopted_job_recorded_as_running(tmp_path: Path, fake_cli: list[str]) -> None:
    root = tmp_path / "ui" / "jobs"
    job_id = "2026-09-22T14-03-11Z-a1b2c3"
    _plant_running(root, job_id, pid=4242, pid_start="123", argv=["likearr"])
    process = _Process()
    runner = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock", alive=process.alive, adopted_poll_s=0.02)
    runner.recover()

    runner.shutdown(timeout=1)
    time.sleep(0.1)

    assert runner.get(job_id).state is JobState.RUNNING  # type: ignore[union-attr]  # the next start looks again


def test_a_job_recorded_before_pids_is_marked_interrupted(tmp_path: Path, fake_cli: list[str]) -> None:
    from likearr.web.jobs import still_running

    root = tmp_path / "ui" / "jobs"
    _plant(root, "2026-09-22T14-03-11Z-a1b2c3", "running")
    meta = JobRunner(root, fake_cli, lock_path=tmp_path / "likearr.lock").get("2026-09-22T14-03-11Z-a1b2c3")

    assert meta is not None and not still_running(meta)


def test_a_job_records_its_process_as_it_starts(runner: JobRunner) -> None:
    meta = runner.start("explain", ["sleep", "5"])
    try:
        recorded = runner.get(meta.id)
        assert recorded is not None and recorded.pid > 0
        if Path("/proc").is_dir():
            assert recorded.pid_start
    finally:
        runner.cancel(meta.id)
        _finish(runner, meta.id)


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="the liveness check reads Linux /proc")
def test_still_running_proves_the_process_is_the_same_job(tmp_path: Path, fake_cli: list[str]) -> None:
    import subprocess

    from likearr.web.jobs import _process_start, still_running

    argv = [*fake_cli, "sleep", "30"]
    proc = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_until(lambda: _process_start(proc.pid) != "")
        meta = JobMeta(
            id="2026-09-22T14-03-11Z-a1b2c3",
            kind="apply",
            argv=argv,
            label="",
            started_at="",
            finished_at=None,
            exit_code=None,
            state=JobState.RUNNING,
            drain=True,
            pid=proc.pid,
            pid_start=_process_start(proc.pid),
        )
        _wait_until(lambda: still_running(meta))  # the interpreter may still be exec-ing

        assert not still_running(replace(meta, pid_start="1"))  # a reused PID
        assert not still_running(replace(meta, argv=[*argv[:-1], "31"]))  # another command
    finally:
        proc.kill()
        proc.wait(10)
    assert not still_running(meta)


# --------------------------------------------------------------------------- last_json_object


def test_last_json_object_scans_backward_for_the_first_json_line() -> None:
    from likearr.web.jobs import last_json_object

    output = '{"a": 1}\n{"a": 2}\n{"a": 3}\n'
    assert last_json_object(output, lambda d: True) == {"a": 3}


def test_last_json_object_applies_the_predicate_and_keeps_scanning_past_a_failing_line() -> None:
    from likearr.web.jobs import last_json_object

    output = '{"kind": "wanted"}\n{"kind": "not this one"}\n'
    assert last_json_object(output, lambda d: d.get("kind") == "wanted") == {"kind": "wanted"}


def test_last_json_object_skips_a_line_that_parses_but_is_not_a_dict() -> None:
    from likearr.web.jobs import last_json_object

    output = '{"a": 1}\n[1, 2, 3]\n'
    assert last_json_object(output, lambda d: True) == {"a": 1}


def test_last_json_object_skips_garbage_lines_that_do_not_parse() -> None:
    from likearr.web.jobs import last_json_object

    output = '{"a": 1}\nnot json at all\n'
    assert last_json_object(output, lambda d: True) == {"a": 1}


def test_last_json_object_returns_none_for_empty_output() -> None:
    from likearr.web.jobs import last_json_object

    assert last_json_object("", lambda d: True) is None
    assert last_json_object("   \n  \n", lambda d: True) is None


def test_last_json_object_returns_none_when_nothing_parses_or_passes() -> None:
    from likearr.web.jobs import last_json_object

    assert last_json_object("not json\nalso not json\n", lambda d: True) is None
    assert last_json_object('{"a": 1}\n', lambda d: False) is None


def test_last_json_object_strict_reads_only_the_literal_last_line() -> None:
    from likearr.web.jobs import last_json_object

    # A stray extra line after the real JSON line: strict must not scan past it (doctor's and
    # lidarr_setup's deliberate behaviour - a stray trailing line is a parse failure).
    output = '{"a": 1}\nstray trailing line\n'
    assert last_json_object(output, lambda d: True, strict=True) is None


def test_last_json_object_strict_still_applies_the_predicate_to_the_last_line() -> None:
    from likearr.web.jobs import last_json_object

    output = '{"a": 1}\n{"a": 2}\n'
    assert last_json_object(output, lambda d: d.get("a") == 2, strict=True) == {"a": 2}
    assert last_json_object(output, lambda d: d.get("a") == 1, strict=True) is None


def test_last_json_object_strict_returns_none_for_empty_output() -> None:
    from likearr.web.jobs import last_json_object

    assert last_json_object("", lambda d: True, strict=True) is None
