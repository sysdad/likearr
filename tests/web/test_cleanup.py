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
    trashes,
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


def test_whether_an_export_trashes_decides_the_preview_chain(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"trash": [], "trash_artists": [], "promote": ["a"]}))

    assert not trashes(decisions)
    assert Binding("d", asks_spotify=True, trashes=False).chain() == ["spotify"]
    assert Binding("d", trashes=False).chain() == []
    assert Binding("d", asks_spotify=True).chain() == ["stage", "spotify", "checks"]
    decisions.write_text(json.dumps({"trash": ["x"]}))
    assert trashes(decisions)
    decisions.write_text("{")
    assert trashes(decisions), "an unreadable file goes to the move preview, which says what is wrong"


def test_a_binding_written_before_it_said_whether_it_trashes_still_runs_the_move(tmp_path: Path) -> None:
    (tmp_path / "previews.json").write_text('{"decisions_sha256": "abc", "stage": "s"}')

    binding = read_binding(tmp_path)

    assert binding is not None and binding.trashes and binding.chain() == ["stage", "checks"]


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


def test_the_spotify_preview_names_a_save_matched_under_another_title(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from likearr.models import PromoteSavePlan, ReleaseKey, SaveAlbum
    from likearr.shell.promote_save import write_plan

    def save(title: str, spotify_title: str) -> SaveAlbum:
        return SaveAlbum(
            key=ReleaseKey("a", title),
            artist_name="Radiohead",
            title=title,
            spotify_id="sp",
            step="album:name",
            spotify_title=spotify_title,
            spotify_artists=("Radiohead",),
        )

    plan = PromoteSavePlan(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        decisions_path="",
        decisions_digest="d",
        lidarr_digest="l",
        reviewed_digest="r",
        follow=[],
        save=[save("Kid A", "Kid A (Remastered)"), save("In Rainbows", "In Rainbows")],
        already_followed=[],
        already_saved=[],
        unmatched=[],
    )
    path = tmp_path / "promote-save.json"
    write_plan(plan, path)

    preview = read_spotify(path)

    assert preview is not None
    assert preview.save == ["Radiohead - Kid A (on Spotify: Radiohead - Kid A (Remastered))", "Radiohead - In Rainbows"]


def test_names_from_a_preview_are_cut_to_a_page_s_worth(tmp_path: Path) -> None:
    path = tmp_path / "stage.json"
    path.write_text(json.dumps({"files": 1, "remove": [{"name": "x" * 5000, "why": "y"}]}))

    stage = read_stage(path)

    assert stage is not None and len(stage.remove[0][0]) == 300
