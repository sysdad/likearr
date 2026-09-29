"""Albums already monitored before likearr: `adopt`'s two modes, a first check's "Albums you
already monitor", `run --apply`'s choice for them, and `[rules] manage_monitored`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from likearr.config import RulesConfig
from likearr.models import EXIT_ERROR, EXIT_OK, EXIT_STALE, ReleaseKey
from likearr.shell import commands
from likearr.shell.adopt_io import read_adopt_plan, read_existing
from likearr.shell.diff_io import read_diff
from likearr.shell.run import run_command
from likearr.shell.run_types import ExistingChoice
from tests.shell.commands_shared import ALBUM, EP, STRANGER, followed_world
from tests.shell.conftest import NOW, CapturingSink, FakeLidarr, make_config, make_context
from tests.unit.fakes import lidarr_album, lidarr_artist, rg

OTHER = rg("rg-8", "Another Thing", artist_mbid="artist-9", artist_name="A Stranger")
WANTED = ReleaseKey("artist-1", "rg-1")


def _world(
    tmp_path: Path, sink: CapturingSink, *, first_applied: bool = False, **config: Any
) -> tuple[Any, FakeLidarr]:
    """artist-1 is followed and rg-1 is monitored by hand; artist-9 is a stranger with two albums
    monitored by hand. Nothing is owned, and by default nothing has been applied yet."""
    source, lookup, _ = followed_world()
    lookup.add(STRANGER, OTHER)
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist"), lidarr_album(ALBUM, id=101, monitored=True))
    lidarr.seed(
        lidarr_artist("artist-9", id=9, name="A Stranger"),
        lidarr_album(STRANGER, id=901, artist_id=9, monitored=True),
        lidarr_album(OTHER, id=902, artist_id=9, monitored=True),
    )
    ctx = make_context(
        tmp_path,
        source=source,
        lookup=lookup,
        lidarr=lidarr,
        sink=sink,
        first_applied=first_applied,
        config=make_config(tmp_path, **config) if config else None,
    )
    return ctx, lidarr


def _monitored(lidarr: FakeLidarr, artist: str, rg_mbid: str) -> bool:
    album = lidarr.album(artist, rg_mbid)
    assert album is not None
    return album.monitored


# --------------------------------------------------------------------------- adopt


def test_adopt_by_default_only_claims_and_leaves_the_rest_as_it_is(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        assert commands.adopt_command(ctx, out=plan_path, now=NOW) == EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(plan_path.read_text())
        assert commands.adopt_command(ctx, apply_path=plan_path, now=NOW) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert payload["mode"] == "claim"
    assert payload["summary"] == {"claim": 1, "keep": 0, "unmonitor": 0, "held": 0, "left": 2}
    assert "1 to claim, 2 left as they are" in out
    assert "leave" in out
    assert set(owned) == {WANTED}
    assert lidarr.writes() == [], "claiming never calls Lidarr"
    assert _monitored(lidarr, "artist-9", "rg-9") and _monitored(lidarr, "artist-9", "rg-8")


def test_adopt_unmonitor_rest_records_its_mode_and_unmonitors_what_nothing_wants(
    tmp_path: Path, sink: CapturingSink
) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("rg-8\n")
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, unmonitor_rest=True, keep_file=keep, out=plan_path, now=NOW)
        assert read_adopt_plan(plan_path).adoption.unmonitor_rest is True
        assert commands.adopt_command(ctx, apply_path=plan_path, now=NOW) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert json.loads(plan_path.read_text())["mode"] == "unmonitor-rest"
    assert not _monitored(lidarr, "artist-9", "rg-9")
    assert _monitored(lidarr, "artist-9", "rg-8"), "kept"
    assert set(owned) == {WANTED, ReleaseKey("artist-9", "rg-8")}


def test_adopt_keep_without_unmonitor_rest_is_refused(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("rg-8\n")
    with ctx:
        code = commands.adopt_command(ctx, keep_file=keep, out=tmp_path / "adopt.json", now=NOW)

    assert code == EXIT_ERROR
    assert "--keep only applies with --unmonitor-rest" in capsys.readouterr().out
    assert not (tmp_path / "adopt.json").exists()
    assert lidarr.writes() == []


def test_adopt_apply_refuses_unmonitor_rest_the_plan_already_carries(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        code = commands.adopt_command(ctx, apply_path=plan_path, unmonitor_rest=True, now=NOW)
    assert code == EXIT_ERROR
    assert lidarr.writes() == []


def test_an_adopt_plan_from_before_the_mode_reads_as_unmonitor_rest(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _world(tmp_path, sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, unmonitor_rest=True, out=plan_path, now=NOW)
    raw = json.loads(plan_path.read_text())
    del raw["mode"], raw["left"]
    plan_path.write_text(json.dumps(raw))

    adoption = read_adopt_plan(plan_path).adoption
    assert adoption.unmonitor_rest is True
    assert {u.key.rg_mbid for u in adoption.unmonitor} == {"rg-8", "rg-9"}


def test_a_claim_only_plan_that_lists_unmonitors_does_not_read(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _world(tmp_path, sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, unmonitor_rest=True, out=plan_path, now=NOW)
    raw = json.loads(plan_path.read_text())
    raw["mode"] = "claim"
    plan_path.write_text(json.dumps(raw))

    with pytest.raises(commands.DiffFileError):
        read_adopt_plan(plan_path)


# --------------------------------------------------------------------------- a first check


def test_a_first_check_lists_the_albums_already_monitored(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    diff_path = tmp_path / "diff.json"
    with ctx:
        assert run_command(ctx, now=NOW, out=diff_path) == EXIT_OK

    existing = read_existing(diff_path)
    assert existing is not None
    assert [r.key for r in existing.adoption.claim] == [WANTED]
    assert {u.key.rg_mbid for u in existing.adoption.unmonitor} == {"rg-8", "rg-9"}
    assert existing.artists["artist-9"] == "A Stranger"
    assert existing.titles["rg-1"] == "First Album"
    assert lidarr.writes() == []


def test_a_check_after_the_first_apply_lists_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _world(tmp_path, sink, first_applied=True)
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
    assert read_existing(diff_path) is None


def _plan_then_apply(ctx: Any, tmp_path: Path, choice: ExistingChoice | None) -> int:
    diff_path = tmp_path / "diff.json"
    run_command(ctx, now=NOW, out=diff_path)
    return run_command(ctx, now=NOW, apply_path=diff_path, do_apply=True, existing=choice)


def test_applying_with_claim_manages_the_matches_and_leaves_the_rest(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    with ctx:
        assert _plan_then_apply(ctx, tmp_path, ExistingChoice(claim=True)) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert set(owned) == {WANTED}
    assert owned[WANTED].lidarr_album_id == 101
    assert _monitored(lidarr, "artist-9", "rg-9") and _monitored(lidarr, "artist-9", "rg-8")
    assert sink.records[-1].counts["claimed"] == 1


def test_applying_without_claim_leaves_every_album_already_monitored_alone(tmp_path: Path, sink: CapturingSink) -> None:
    for choice in (None, ExistingChoice(claim=False)):
        base = tmp_path / ("none" if choice is None else "off")
        base.mkdir()
        ctx, lidarr = _world(base, CapturingSink())
        with ctx:
            assert _plan_then_apply(ctx, base, choice) == EXIT_OK
            assert WANTED not in ctx.state.owned_releases()
        assert _monitored(lidarr, "artist-9", "rg-9") and _monitored(lidarr, "artist-9", "rg-8")


def test_unmonitoring_the_rest_spares_the_kept(tmp_path: Path, sink: CapturingSink) -> None:
    """rg-8 is kept by the reviewer, so only rg-9 is unmonitored."""
    ctx, lidarr = _world(tmp_path, sink)
    with ctx:
        assert _plan_then_apply(ctx, tmp_path, ExistingChoice(unmonitor_rest=True, keep=frozenset({"rg-8"}))) == 0
        owned = ctx.state.owned_releases()
    assert not _monitored(lidarr, "artist-9", "rg-9")
    assert _monitored(lidarr, "artist-9", "rg-8")
    assert owned == {}, "an unmonitored album is never recorded as owned, and claim was not chosen"


def test_an_existing_choice_is_refused_when_those_albums_moved(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
        lidarr.set_albums_monitored([901], False)  # unmonitored by hand after the check
        code = run_command(
            ctx, now=NOW, apply_path=diff_path, do_apply=True, existing=ExistingChoice(claim=True, unmonitor_rest=True)
        )
        owned = ctx.state.owned_releases()
    assert code == EXIT_STALE
    assert owned == {}
    assert _monitored(lidarr, "artist-9", "rg-8")


@pytest.mark.parametrize(
    ("how", "choice"),
    [
        ({"scheduled": True, "do_apply": True}, ExistingChoice(claim=True)),
        ({"do_apply": False}, ExistingChoice(claim=True)),
        ({"do_apply": True, "apply": True}, ExistingChoice(keep=frozenset({"rg-8"}))),
    ],
    ids=["scheduled", "dry-run", "keep-without-unmonitor-rest"],
)
def test_an_existing_choice_is_refused_outside_a_reviewed_apply(
    tmp_path: Path, sink: CapturingSink, how: dict[str, Any], choice: ExistingChoice
) -> None:
    ctx, lidarr = _world(tmp_path, sink)
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
        apply_path = diff_path if how.pop("apply", False) else None
        code = run_command(ctx, now=NOW, out=tmp_path / "other.json", apply_path=apply_path, existing=choice, **how)
    assert code == EXIT_ERROR
    assert lidarr.writes() == []


def test_an_existing_choice_on_a_diff_without_them_is_refused(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _world(tmp_path, sink, first_applied=True)
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
        code = run_command(ctx, now=NOW, apply_path=diff_path, do_apply=True, existing=ExistingChoice(claim=True))
    assert code == EXIT_ERROR
    assert WANTED not in ctx.state.owned_releases()


# --------------------------------------------------------------------------- [rules] manage_monitored


def test_manage_monitored_claims_a_wanted_album_already_monitored_on_every_run(
    tmp_path: Path, sink: CapturingSink
) -> None:
    ctx, lidarr = _world(tmp_path, sink, first_applied=True, rules=RulesConfig(manage_monitored=True))
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
        assert [c.key for c in read_diff(diff_path).claim] == [WANTED]
        assert run_command(ctx, now=NOW, apply_path=diff_path, do_apply=True) == EXIT_OK
        owned = ctx.state.owned_releases()
    assert set(owned) == {WANTED}
    assert _monitored(lidarr, "artist-9", "rg-9"), "what nothing wants is still left alone"
    assert sink.records[-1].counts["claimed"] == 1


def test_without_manage_monitored_a_run_claims_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _world(tmp_path, sink, first_applied=True)
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
        assert read_diff(diff_path).claim == []
        run_command(ctx, now=NOW, apply_path=diff_path, do_apply=True)
        assert ctx.state.owned_releases() == {}


def test_a_first_check_leaves_what_manage_monitored_already_claims_out_of_its_list(
    tmp_path: Path, sink: CapturingSink
) -> None:
    ctx, _ = _world(tmp_path, sink, rules=RulesConfig(manage_monitored=True))
    diff_path = tmp_path / "diff.json"
    with ctx:
        run_command(ctx, now=NOW, out=diff_path)
    existing = read_existing(diff_path)
    assert existing is not None
    assert existing.adoption.claim == []
    assert [c.key for c in read_diff(diff_path).claim] == [WANTED]


def test_a_failed_unmonitor_batch_counts_what_lidarr_did_unmonitor(tmp_path: Path, sink: CapturingSink) -> None:
    """Lidarr unmonitors the batch and still answers with an error: the stopped apply says so."""
    ctx, lidarr = _world(tmp_path, sink)
    lidarr.fail_unmonitor_batch = 1
    lidarr.fail_unmonitor_batch_applies = 2
    with ctx:
        code = _plan_then_apply(ctx, tmp_path, ExistingChoice(unmonitor_rest=True))
    assert code == EXIT_ERROR
    assert not _monitored(lidarr, "artist-9", "rg-9") and not _monitored(lidarr, "artist-9", "rg-8")
    assert sink.records[-1].counts["unmonitored"] == 2
    assert "all 2 planned changes were made" in sink.records[-1].message
