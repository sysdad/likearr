"""`web.doctor.parse_doctor_json`: turning `doctor --json` back into checks for the Doctor view."""

from __future__ import annotations

import json

from likearr.web.doctor import parse_doctor_json


def _line(**overrides: object) -> str:
    payload: dict[str, object] = {
        "checks": [
            {"level": "PASS", "name": "lidarr", "detail": "reachable, version 3.1.0"},
            {"level": "FAIL", "name": "root folder", "detail": "/music is not configured"},
        ],
        "summary": {"total": 2, "failed": 1, "warnings": 0, "skipped": 0},
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parses_checks_and_summary() -> None:
    view = parse_doctor_json(_line())

    assert view is not None
    assert view.total == 2
    assert view.failed == 1
    assert view.ok is False
    assert [c.level for c in view.checks] == ["PASS", "FAIL"]
    assert view.checks[1].name == "root folder"


def test_ok_is_true_with_no_failures() -> None:
    view = parse_doctor_json(_line(summary={"total": 1, "failed": 0, "warnings": 0, "skipped": 0}))

    assert view is not None
    assert view.ok is True


def test_stray_log_lines_before_the_json_are_ignored() -> None:
    """`doctor --json` prints exactly one line, but a real job's stdout can carry a blank line or
    two around it (buffering); only the last non-empty line is trusted."""
    view = parse_doctor_json("\n" + _line() + "\n")

    assert view is not None
    assert view.total == 2


def test_garbage_output_is_not_a_view() -> None:
    assert parse_doctor_json("FAIL  lidarr: connection refused\n") is None


def test_empty_output_is_not_a_view() -> None:
    assert parse_doctor_json("") is None


def test_a_missing_summary_field_is_not_a_view() -> None:
    payload = json.loads(_line())
    del payload["summary"]["failed"]
    assert parse_doctor_json(json.dumps(payload)) is None


def test_problems_come_failures_first_then_warnings_and_passes_apart() -> None:
    """The Settings Doctor section leads with what needs attention (#85): every FAIL, then every
    WARN (and any level it does not know), each in the order doctor ran them; passes and skips
    are kept apart to be collapsed."""
    checks = [
        {"level": "PASS", "name": "config", "detail": "loaded"},
        {"level": "WARN", "name": "tag", "detail": "missing"},
        {"level": "FAIL", "name": "lidarr", "detail": "refused"},
        {"level": "SKIP", "name": "spotify liked", "detail": "not requested"},
        {"level": "ODD", "name": "future", "detail": "new level"},
        {"level": "FAIL", "name": "musicbrainz", "detail": "down"},
    ]
    view = parse_doctor_json(_line(checks=checks, summary={"total": 6, "failed": 2, "warnings": 1, "skipped": 1}))

    assert view is not None
    assert [c.name for c in view.problems] == ["lidarr", "musicbrainz", "tag", "future"]
    assert [c.name for c in view.passed] == ["config"]
    assert [c.name for c in view.skipped_checks] == ["spotify liked"]
