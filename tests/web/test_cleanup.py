"""Finishing a clean up: the previews' files as the page reads them, and the commands."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from likearr.web.cleanup import (
    Binding,
    asks_spotify,
    commands,
    decisions_digest,
    read_binding,
    read_checks,
    read_spotify,
    read_stage,
    write_binding,
)


def test_the_commands_are_exact_quoted_and_never_force_or_delete(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs" / "2026-09-23T18-00-00Z-abc123"
    c = commands(
        prefix="docker compose run --rm likearr",
        config_path=Path("/data/config.toml"),
        job_dir=job_dir,
        holding="/data/media/_likearr hold",
        preview_plan=None,
    )
    lines = [c.stage_preview, c.stage_apply, c.spotify_plan, c.spotify_apply_fresh, c.auth]

    assert c.stage_preview == (
        "docker compose run --rm likearr prune-stage -c /data/config.toml "
        f"--manifest {job_dir}/prune.json --holding '/data/media/_likearr hold' --decisions {job_dir}/decisions.json"
    )
    assert c.stage_apply == c.stage_preview + " --apply"
    assert c.spotify_apply == ""  # no preview plan: make one, then apply it
    assert c.spotify_apply_fresh.endswith(f"--apply {job_dir}/promote-save.json")
    assert c.auth == "docker compose run --rm likearr auth -c /data/config.toml --manual --promote-save"
    assert shlex.split(c.stage_preview)[:4] == ["docker", "compose", "run", "--rm"]
    for line in lines:
        assert "--force" not in line and " rm " not in line.replace("run --rm likearr", "")


def test_the_page_s_own_spotify_plan_is_what_gets_applied(tmp_path: Path) -> None:
    plan = tmp_path / "jobs" / "spotify" / "promote-save.json"
    c = commands(prefix="likearr", config_path=Path("c.toml"), job_dir=tmp_path, holding="/h", preview_plan=plan)

    assert c.spotify_apply == f"likearr promote-save -c c.toml --apply {plan}"


def test_a_binding_round_trips_and_a_broken_one_reads_as_none(tmp_path: Path) -> None:
    write_binding(tmp_path, Binding("abc", stage="s", spotify="", checks="c", asks_spotify=True))

    assert read_binding(tmp_path) == Binding("abc", stage="s", spotify="", checks="c", asks_spotify=True)
    (tmp_path / "previews.json").write_text('{"decisions_sha256": 7}')
    assert read_binding(tmp_path) is None


def test_the_decisions_digest_and_whether_they_ask_spotify(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"trash": ["x"], "promote": [], "save": [], "save_releases": ["r"]}))

    assert decisions_digest(decisions) is not None
    assert asks_spotify(decisions)
    decisions.write_text(json.dumps({"trash": ["x"], "promote": [], "save": []}))
    assert not asks_spotify(decisions)
    assert decisions_digest(tmp_path / "missing.json") is None


@pytest.mark.parametrize("content", ["", "[]", "{", '{"files": "7", "remove": "x"}'])
def test_a_preview_file_that_does_not_read_is_never_a_crash(tmp_path: Path, content: str) -> None:
    path = tmp_path / "file.json"
    path.write_text(content)

    stage = read_stage(path)
    assert stage is None or (stage.files, stage.remove) == (0, [])
    checks = read_checks(path)
    assert checks is None or checks.auto_add is None
    assert read_spotify(path) is None
    assert read_stage(None) is None and read_checks(None) is None and read_spotify(None) is None


def test_names_from_a_preview_are_cut_to_a_page_s_worth(tmp_path: Path) -> None:
    path = tmp_path / "stage.json"
    path.write_text(json.dumps({"files": 1, "remove": [{"name": "x" * 5000, "why": "y"}]}))

    stage = read_stage(path)

    assert stage is not None and len(stage.remove[0][0]) == 300
