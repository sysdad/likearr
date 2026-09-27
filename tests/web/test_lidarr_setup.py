"""`web.lidarr_setup.parse_setup_profiles_json`: turning `setup-profiles --json` into a view."""

from __future__ import annotations

import json

from likearr.web.lidarr_setup import parse_setup_profiles_json


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "profiles": [
            {"name": "Lean", "kind": "lean", "status": "ok", "applies": False, "id": 1},
            {"name": "Full", "kind": "full", "status": "missing", "applies": True},
        ],
        "tag": {"name": "likearr", "status": "ok", "applies": False, "id": 3},
        "root_folder": {"path": "/music", "status": "ok", "applies": False},
        "todo": ["create metadata profile 'Full' (full)"],
        "needs_apply": True,
    }
    payload.update(overrides)
    return payload


def test_parses_a_plan_that_needs_work() -> None:
    view = parse_setup_profiles_json(json.dumps(_payload()))

    assert view is not None
    assert view.needs_apply is True
    assert view.error == ""
    assert [p.status for p in view.profiles] == ["ok", "missing"]
    assert view.todo == ("create metadata profile 'Full' (full)",)


def test_nothing_to_apply_when_everything_matches() -> None:
    payload = _payload(
        profiles=[
            {"name": "Lean", "kind": "lean", "status": "ok", "id": 1, "applies": False},
            {"name": "Full", "kind": "full", "status": "ok", "id": 2, "applies": False},
        ],
        todo=[],
        needs_apply=False,
    )
    view = parse_setup_profiles_json(json.dumps(payload))

    assert view is not None
    assert view.needs_apply is False
    assert view.kept_as_is == ()


def test_a_differing_profile_is_surfaced_with_its_diff_and_is_kept_as_is() -> None:
    """A metadata profile's `status: "differs"` always carries `applies: False` -
    `ensure_metadata_profile` never edits an existing profile - so it shows up in `kept_as_is`
    even though nothing needs applying."""
    diff = {
        "expected_primary": ["Album", "EP"],
        "actual_primary": ["Album"],
        "expected_secondary": ["Studio"],
        "actual_secondary": ["Studio", "Live"],
    }
    payload = _payload(
        profiles=[{"name": "Lean", "kind": "lean", "status": "differs", "applies": False, "id": 1, "diff": diff}],
        todo=[],
        needs_apply=False,
    )
    view = parse_setup_profiles_json(json.dumps(payload))

    assert view is not None
    assert view.needs_apply is False, "ensure_metadata_profile never edits an existing profile"
    assert [p.name for p in view.kept_as_is] == ["Lean"]
    assert view.profiles[0].applies is False
    assert view.profiles[0].diff == diff


def test_a_differing_root_folder_applies_rather_than_being_kept_as_is() -> None:
    """Unlike a metadata profile, `set_root_folder_defaults` really does overwrite an existing
    root folder's monitor defaults - its `status: "differs"` carries `applies: True`, it is in
    `todo`, and it never appears in `kept_as_is`."""
    payload = _payload(
        root_folder={"path": "/music", "status": "differs", "applies": True, "current": {}},
        todo=["set root folder '/music' defaults to monitor none / new items none"],
        needs_apply=True,
    )
    view = parse_setup_profiles_json(json.dumps(payload))

    assert view is not None
    assert view.needs_apply is True
    assert view.root_folder["applies"] is True
    assert view.kept_as_is == (), "a differing root folder is applied, never 'kept as is'"


def test_an_error_payload_is_reported_without_a_profile_table() -> None:
    view = parse_setup_profiles_json(json.dumps({"error": "lidarr: connection refused"}))

    assert view is not None
    assert view.error == "lidarr: connection refused"
    assert view.profiles == ()


def test_garbage_output_is_not_a_view() -> None:
    assert parse_setup_profiles_json("FAIL  lidarr: connection refused\n") is None


def test_empty_output_is_not_a_view() -> None:
    assert parse_setup_profiles_json("") is None
