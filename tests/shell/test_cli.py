"""`shell.cli`: argument parsing, dispatch and exit codes.

The CLI is tested against a stubbed `build_context`, because what it is responsible for is
turning arguments into the right call with the right flags and turning an expected failure into
one line and exit 1 - not what the run then does.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from likearr.config import ConfigError, PruneConfig
from likearr.models import EXIT_BUSY, EXIT_ERROR, EXIT_GUARDED, EXIT_OK, EXIT_STALE
from likearr.ports import LidarrError
from likearr.shell import cli
from tests.shell.conftest import CapturingSink, FakeLibrary, make_config, make_context


@pytest.fixture
def stub_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`build_context` returns a fake-backed context instead of touching the network."""
    created: list[Any] = []

    def build(config_path: Any, **_kwargs: Any):
        ctx = make_context(tmp_path, sink=CapturingSink())
        created.append(ctx)
        return ctx

    monkeypatch.setattr(cli, "build_context", build)
    return created


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what the CLI asked `run_command` to do, without doing it."""
    recorded: dict[str, Any] = {}

    def fake_run_command(ctx: Any, **kwargs: Any) -> int:
        recorded.update(kwargs)
        return int(recorded.pop("_result", EXIT_OK))

    monkeypatch.setattr(cli, "run_command", fake_run_command)
    return recorded


def test_version_and_help_exit_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "likearr" in capsys.readouterr().out


def test_a_missing_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_serve_is_not_a_command_any_more(capsys: pytest.CaptureFixture[str]) -> None:
    """`serve` was renamed to `start` outright (issue #68 phase 4): no alias, no deprecation."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'serve'" in capsys.readouterr().err


def test_the_command_list_is_exactly_these() -> None:
    """The one-time ledger import is gone (#162): nothing on the list is for one install only."""
    listed = (
        "{run,auth,doctor,setup-profiles,adopt,playlists,lidarr-files,explain,prune-report,"
        "prune-stage,prune-checks,promote-save,start}"
    )
    assert listed in cli.build_parser().format_help()


def test_start_is_wired_to_the_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from likearr.models import EXIT_OK

    seen: dict[str, Any] = {}

    def fake_serve(config_path: Any, **kwargs: Any) -> int:
        seen["config_path"] = config_path
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setenv("LIKEARR_UI_PASSWORD", "cli-test-password-long-enough")
    monkeypatch.setattr("likearr.web.server.serve", fake_serve)
    config_path = tmp_path / "config.toml"
    config_path.write_text("")

    assert cli.main(["start", "-c", str(config_path), "--host", "0.0.0.0", "--port", "9999"]) == EXIT_OK
    assert seen["host"] == "0.0.0.0"
    assert seen["port"] == 9999


def test_run_defaults_to_a_dry_run(stub_context: list[Any], calls: dict[str, Any]) -> None:
    assert cli.main(["-c", "config.toml", "run"]) == EXIT_OK
    assert calls["do_apply"] is False
    assert calls["apply_path"] is None
    assert calls["out"] == Path("diff.json")
    assert calls["scheduled"] is False


def test_run_apply_without_a_file_uses_the_out_path(stub_context: list[Any], calls: dict[str, Any]) -> None:
    cli.main(["run", "--out", "plan.json", "--apply"])
    assert calls["do_apply"] is True
    assert calls["apply_path"] == Path("plan.json")


def test_run_apply_with_a_file_uses_that_file(stub_context: list[Any], calls: dict[str, Any]) -> None:
    cli.main(["run", "--apply", "reviewed.json"])
    assert calls["apply_path"] == Path("reviewed.json")


def test_scheduled_apply_has_no_diff_file(stub_context: list[Any], calls: dict[str, Any]) -> None:
    """`run --scheduled --apply` plans and applies in one go, so there is nothing to go stale."""
    cli.main(["run", "--scheduled", "--apply"])
    assert calls["do_apply"] is True
    assert calls["scheduled"] is True
    assert calls["apply_path"] is None


def _example_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    example = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    config.write_text(example.read_text())
    return config


def test_a_scheduled_run_on_a_new_install_publishes_paused_and_creates_no_state_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #111: the example config (every `[schedule]` key commented out, so the schedule is on)
    and no state database yet. The fire publishes `paused` and exits 0 without ever building a
    `Context`, so no database is created and nothing is contacted."""
    config = _example_config(tmp_path)
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "test-key")

    def explode(*_args: Any, **_kwargs: Any):
        raise AssertionError("a held scheduled run must not build a Context")

    monkeypatch.setattr(cli, "build_context", explode)

    assert cli.main(["-c", str(config), "run", "--scheduled", "--apply"]) == EXIT_OK
    assert not (tmp_path / "state.sqlite").exists()
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["status"] == "paused"
    assert record["exit_code"] == EXIT_OK
    assert record["message"] == (
        "waiting for your first reviewed apply: connect Spotify, then review and apply your first plan"
    )


def test_a_scheduled_run_with_a_state_database_goes_on_to_run_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_context: list[Any], calls: dict[str, Any]
) -> None:
    from likearr.adapters.state_sqlite import SqliteState

    config = _example_config(tmp_path)
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "test-key")
    SqliteState(tmp_path / "state.sqlite").close()

    assert cli.main(["-c", str(config), "run", "--scheduled", "--apply"]) == EXIT_OK
    assert calls["scheduled"] is True, "run_command decides the gate from the database"


def test_a_hand_run_never_checks_for_the_state_database_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_context: list[Any], calls: dict[str, Any]
) -> None:
    config = _example_config(tmp_path)
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "test-key")

    assert cli.main(["-c", str(config), "run", "--apply"]) == EXIT_OK
    assert calls["do_apply"] is True
    assert calls["scheduled"] is False


def test_accept_shrink_is_passed_through(stub_context: list[Any], calls: dict[str, Any]) -> None:
    cli.main(["run", "--accept-shrink"])
    assert calls["accept_shrink"] is True
    cli.main(["run"])
    assert calls["accept_shrink"] is False


def test_force_is_passed_through(stub_context: list[Any], calls: dict[str, Any]) -> None:
    cli.main(["run", "--apply", "d.json", "--force"])
    assert calls["force"] is True


@pytest.mark.parametrize("code", [EXIT_OK, EXIT_ERROR, EXIT_GUARDED, EXIT_STALE])
def test_the_run_exit_code_is_returned_unchanged(
    stub_context: list[Any], monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(cli, "run_command", lambda ctx, **kwargs: code)
    assert cli.main(["run"]) == code


def test_config_env_var_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.CONFIG_ENV, "/etc/likearr/config.toml")
    args = cli.build_parser().parse_args(["run"])
    assert args.config == "/etc/likearr/config.toml"


def test_a_config_error_is_one_line_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(*_args: Any, **_kwargs: Any):
        raise ConfigError("config file not found: /nope/config.toml")

    monkeypatch.setattr(cli, "build_context", explode)
    assert cli.main(["run"]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert out.splitlines() == ["config error: config file not found: /nope/config.toml"]
    assert "Traceback" not in out


def test_doctor_on_a_wrongly_typed_guard_names_the_key_not_an_unexpected_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#110: the real `build_context`, so the real load. It fails before anything is contacted."""
    config = tmp_path / "config.toml"
    example = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    config.write_text(
        example.read_text().replace("max_unmonitors_scheduled = 100", 'max_unmonitors_scheduled = "lots"', 1)
    )

    assert cli.main(["-c", str(config), "doctor"]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "config error: [guards] max_unmonitors_scheduled must be a whole number >= 0, got 'lots'"
    ]


def test_no_root_handler_survives_the_previous_capsys_bound_cli_test() -> None:
    """Regression for issue #39. The test above runs the real `build_context`, which calls
    `setup_logging` and binds a root `StreamHandler` to `sys.stderr` as `capsys` has replaced it
    for that test - a stream `capsys` closes as soon as that test ends.

    `preserve_root_logging` (`tests/conftest.py`) is autouse, so by the time this test starts the
    root logger has been restored to whatever it held before that test ran: no handler here should
    be bound to a stream that is already closed.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None:
            assert not stream.closed, f"{handler!r} is bound to a closed stream"


def test_a_lidarr_error_is_one_line_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stub_context: list[Any]
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise LidarrError("lidarr GET /system/status: connection refused")

    monkeypatch.setattr(cli, "run_command", explode)
    assert cli.main(["run"]) == EXIT_ERROR
    assert "connection refused" in capsys.readouterr().out


def test_an_unexpected_error_prints_no_traceback_without_v(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stub_context: list[Any]
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(cli, "run_command", explode)
    assert cli.main(["run"]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "unexpected error: ZeroDivisionError: a bug" in out
    assert "Traceback" not in out

    monkeypatch.setattr(cli, "run_command", explode)
    assert cli.main(["-v", "run"]) == EXIT_ERROR
    assert "Traceback" in capsys.readouterr().out


def test_doctor_no_spotify_skips_building_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def build(config_path: Any, **kwargs: Any):
        seen.update(kwargs)
        return make_context(tmp_path, sink=CapturingSink())

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(cli.setup_commands, "doctor_command", lambda ctx, **kwargs: EXIT_OK)
    assert cli.main(["doctor", "--no-spotify"]) == EXIT_OK
    assert seen["need_spotify"] is False


def test_doctor_json_flag_reaches_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def build(config_path: Any, **kwargs: Any):
        return make_context(tmp_path, sink=CapturingSink())

    def doctor_command(ctx: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(cli.setup_commands, "doctor_command", doctor_command)
    assert cli.main(["doctor", "--no-spotify", "--json"]) == EXIT_OK
    assert seen["as_json"] is True


def test_setup_profiles_json_flag_reaches_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def build(config_path: Any, **kwargs: Any):
        return make_context(tmp_path, sink=CapturingSink())

    def setup_profiles_command(ctx: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(cli.setup_commands, "setup_profiles_command", setup_profiles_command)
    assert cli.main(["setup-profiles", "--json"]) == EXIT_OK
    assert seen == {"do_apply": False, "as_json": True}


def test_lidarr_files_never_builds_spotify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def build(config_path: Any, **kwargs: Any):
        seen.update(kwargs)
        return make_context(tmp_path, sink=CapturingSink())

    def files(ctx: Any, **kwargs: Any) -> int:
        seen["files"] = kwargs
        return EXIT_OK

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(cli.commands, "lidarr_files_command", files)
    assert cli.main(["lidarr-files", "--plan", "diff.json", "--out", "files.json", "--json"]) == EXIT_OK
    assert seen["need_spotify"] is False
    assert seen["files"] == {"plan_file": Path("diff.json"), "out": Path("files.json"), "as_json": True}


@pytest.mark.parametrize(
    ("argv", "spotify"),
    [
        (["prune-stage", "--manifest", "p.json", "--holding", "/h", "--decisions", "d.json"], False),
        (["prune-stage", "--manifest", "p.json", "--holding", "/h", "--decisions", "d.json", "--apply"], True),
        (["prune-checks", "--out", "c.json"], False),
    ],
)
def test_a_prune_preview_and_the_checks_never_build_spotify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], spotify: bool
) -> None:
    """Only --apply re-plans against Spotify; the previews the web UI runs read Lidarr alone (#58)."""
    seen: dict[str, Any] = {}

    def build(config_path: Any, **kwargs: Any):
        seen.update(kwargs)
        return make_context(tmp_path, sink=CapturingSink())

    def stage(ctx: Any, **kwargs: Any) -> int:
        seen["stage"] = kwargs
        return EXIT_OK

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", stage)
    monkeypatch.setattr(cli.prune_commands, "prune_checks_command", lambda ctx, **kw: EXIT_OK)
    assert cli.main(argv) == EXIT_OK
    assert seen["need_spotify"] is spotify


CLEANUP_ARGV = [
    ["prune-report", "--out", "p.json"],
    ["prune-stage", "--manifest", "p.json", "--holding", "/h", "--decisions", "d.json"],
    ["prune-checks", "--out", "c.json"],
    ["promote-save", "--decisions", "d.json", "--reviewed", "r.json"],
]


def _cleanup_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool) -> list[str]:
    """Wire every Clean up command to a stub and the config to `[prune] enabled`; return the
    commands that ran."""
    ran: list[str] = []
    config = make_config(tmp_path, prune=PruneConfig(enabled=enabled))
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, config=config))
    for module, name in [
        (cli.prune_commands, "prune_report_command"),
        (cli.prune_commands, "prune_stage_command"),
        (cli.prune_commands, "prune_checks_command"),
        (cli, "promote_save_command"),
        (cli.commands, "playlists_command"),
    ]:
        monkeypatch.setattr(module, name, lambda ctx, _name=name, **kw: ran.append(_name) or EXIT_OK)
    return ran


@pytest.mark.parametrize("argv", CLEANUP_ARGV)
def test_a_cleanup_command_warns_once_when_clean_up_is_off_and_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """#148: typing a Clean up command is already opting in, so it runs - with one line saying
    the web UI's Clean up is off."""
    ran = _cleanup_cli(monkeypatch, tmp_path, enabled=False)

    assert cli.main(argv) == EXIT_OK

    assert len(ran) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "WARN  Clean up is off in config ([prune] enabled = false): this command runs "
        "anyway, but the web UI does not offer Clean up"
    ]


@pytest.mark.parametrize("argv", CLEANUP_ARGV)
def test_a_cleanup_command_says_nothing_extra_when_clean_up_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    ran = _cleanup_cli(monkeypatch, tmp_path, enabled=True)

    assert cli.main(argv) == EXIT_OK

    assert len(ran) == 1
    assert "Clean up is off" not in capsys.readouterr().out


def test_other_commands_never_mention_clean_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = _cleanup_cli(monkeypatch, tmp_path, enabled=False)

    assert cli.main(["playlists"]) == EXIT_OK

    assert ran == ["playlists_command"]
    assert "Clean up" not in capsys.readouterr().out


def test_prune_stage_passes_the_preview_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))
    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", lambda ctx, **kw: seen.update(kw) or EXIT_OK)

    argv = [
        "prune-stage",
        "--manifest",
        "p",
        "--holding",
        "/h",
        "--decisions",
        "d",
        "--no-mount-check",
        "--out",
        "s.json",
    ]
    cli.main(argv)

    assert (seen["check_mount"], seen["out"]) == (False, Path("s.json"))


def test_promote_save_plans_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))
    monkeypatch.setattr(cli, "promote_save_command", lambda ctx, **kwargs: seen.update(kwargs) or EXIT_OK)

    assert cli.main(["promote-save", "--decisions", "d.json"]) == EXIT_OK
    assert seen["decisions"] == Path("d.json")
    assert seen["do_apply"] is False
    assert seen["apply_path"] is None
    assert seen["out"] == Path("promote-save.json")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["promote-save", "--apply", "plan.json"], Path("plan.json")),
        (["promote-save", "--apply"], None),  # bare --apply falls back to --out
    ],
)
def test_promote_save_apply_takes_an_optional_plan_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: Path | None
) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))
    monkeypatch.setattr(cli, "promote_save_command", lambda ctx, **kwargs: seen.update(kwargs) or EXIT_OK)

    assert cli.main(argv) == EXIT_OK
    assert seen["do_apply"] is True
    assert seen["apply_path"] == expected


def test_promote_save_without_decisions_is_refused_in_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink(), library=FakeLibrary())
    )
    assert cli.main(["promote-save"]) == EXIT_ERROR
    assert "refused: promote-save needs --decisions" in capsys.readouterr().out


def test_prune_stage_parses_its_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def fake(ctx: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", fake)
    cli.main(["prune-stage", "--manifest", "p.json", "--holding", "/tmp/h", "--artists", "A, B ,"])
    assert seen["artists"] == ["A", " B "]
    assert seen["all_candidates"] is False
    assert seen["decisions"] is None
    assert seen["do_apply"] is False


def test_prune_stage_parses_a_decisions_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def fake(ctx: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", fake)
    cli.main(["prune-stage", "--manifest", "p.json", "--holding", "/tmp/h", "--decisions", "d.json"])
    assert seen["decisions"] == Path("d.json")
    assert seen["all_candidates"] is False
    assert seen["artists"] == []


@pytest.mark.parametrize(
    "extra",
    [
        [],  # none of --artists / --all-candidates / --decisions
        ["--artists", "A", "--all-candidates"],
        ["--artists", "A", "--decisions", "d.json"],
        ["--all-candidates", "--decisions", "d.json"],
    ],
)
def test_prune_stage_selection_is_mutually_exclusive_at_the_argparse_level(extra: list[str]) -> None:
    """Zero or more than one of --artists / --all-candidates / --decisions is a usage error."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["prune-stage", "--manifest", "p.json", "--holding", "/tmp/h", *extra])
    assert excinfo.value.code == 2


def test_prune_stage_refusal_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def explode(ctx: Any, **_kwargs: Any) -> int:
        raise cli.prune_commands.PruneStageError("the holding directory is inside the Lidarr root folder")

    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", explode)
    assert cli.main(["prune-stage", "--manifest", "p.json", "--holding", "/music/h", "--all-candidates"]) == EXIT_ERROR
    assert "refused: the holding directory" in capsys.readouterr().out


def test_prune_stage_apply_lock_held_is_one_line_and_exit_busy(
    stub_context: list[Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.adapters.lock import LockHeld

    def held(ctx: Any, **kwargs: Any) -> int:
        raise LockHeld("another run already holds the lock: /x")

    monkeypatch.setattr(cli.prune_commands, "prune_stage_command", held)
    assert (
        cli.main(["prune-stage", "--manifest", "p.json", "--holding", "/h", "--all-candidates", "--apply"]) == EXIT_BUSY
    )
    assert "holds the lock" in capsys.readouterr().out


def test_explain_passes_the_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def fake(ctx: Any, query: str, **kwargs: Any) -> int:
        seen["query"] = query
        return EXIT_OK

    monkeypatch.setattr(cli.commands, "explain_command", fake)
    assert cli.main(["explain", "OK Computer"]) == EXIT_OK
    assert seen["query"] == "OK Computer"


def test_explain_json_and_the_names_file_beside_the_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def fake(ctx: Any, query: str, **kwargs: Any) -> int:
        seen.update(kwargs, query=query)
        return EXIT_OK

    monkeypatch.setattr(cli.commands, "explain_command", fake)
    assert cli.main(["-c", str(tmp_path / "config.toml"), "explain", "--json", "--", "-v"]) == EXIT_OK
    assert seen["query"] == "-v"
    assert seen["as_json"] is True
    assert seen["names_file"] == tmp_path / "ui" / "playlist-names.json"
    assert seen["from_last_run"] is False


def test_explain_from_the_last_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))

    def fake(ctx: Any, query: str, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli.commands, "explain_command", fake)
    assert cli.main(["explain", "--from-last-run", "Lawrence"]) == EXIT_OK
    assert seen["from_last_run"] is True


def test_config_option_accepted_after_subcommand(tmp_path: Path) -> None:
    """`likearr doctor -c X` must mean the same as `likearr -c X doctor` (Docker CMD relies on it)."""
    from likearr.shell.cli import build_parser

    cfg = str(tmp_path / "c.toml")
    before = build_parser().parse_args(["-c", cfg, "doctor"])
    after = build_parser().parse_args(["doctor", "-c", cfg])
    assert before.config == after.config == cfg
    assert build_parser().parse_args(["doctor"]).config != cfg
    assert build_parser().parse_args(["doctor", "-v"]).verbose is True


@pytest.fixture
def adopt_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    recorded: dict[str, Any] = {}

    def fake_adopt(ctx: Any, **kwargs: Any) -> int:
        recorded.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli.commands, "adopt_command", fake_adopt)
    return recorded


def test_adopt_plans_by_default(stub_context: list[Any], adopt_calls: dict[str, Any]) -> None:
    assert cli.main(["adopt", "--keep", "keep.txt", "--out", "plan.json"]) == EXIT_OK
    assert adopt_calls["apply_path"] is None
    assert adopt_calls["keep_file"] == Path("keep.txt")
    assert adopt_calls["out"] == Path("plan.json")


def test_adopt_apply_takes_the_plan_file(stub_context: list[Any], adopt_calls: dict[str, Any]) -> None:
    assert cli.main(["adopt", "--apply", "reviewed.json"]) == EXIT_OK
    assert adopt_calls["apply_path"] == Path("reviewed.json")


def test_adopt_apply_alone_uses_the_out_path(stub_context: list[Any], adopt_calls: dict[str, Any]) -> None:
    assert cli.main(["adopt", "--out", "plan.json", "--apply"]) == EXIT_OK
    assert adopt_calls["apply_path"] == Path("plan.json")


def test_adopt_lock_held_is_one_line_and_exit_busy(
    stub_context: list[Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.adapters.lock import LockHeld

    def held(ctx: Any, **kwargs: Any) -> int:
        raise LockHeld("another run already holds the lock: /x")

    monkeypatch.setattr(cli.commands, "adopt_command", held)
    assert cli.main(["adopt"]) == EXIT_BUSY
    assert "holds the lock" in capsys.readouterr().out


def test_auth_passes_its_options_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink()))
    monkeypatch.setattr(cli.commands, "auth_command", lambda ctx, **kwargs: seen.update(kwargs) or EXIT_OK)

    assert cli.main(["auth"]) == EXIT_OK
    assert seen == {"manual": False, "promote_save": False}

    seen.clear()
    assert cli.main(["auth", "--manual"]) == EXIT_OK
    assert seen["manual"] is True
    assert seen["promote_save"] is False

    seen.clear()
    assert cli.main(["auth", "--manual", "--promote-save"]) == EXIT_OK
    assert seen["manual"] is True
    assert seen["promote_save"] is True

    seen.clear()
    assert cli.main(["auth", "--promote-save"]) == EXIT_OK
    assert seen["manual"] is False
    assert seen["promote_save"] is True


def test_auth_has_no_back_fill_option(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """#162: the one-time date back-fill is gone; re-authorizing records the date."""
    monkeypatch.setenv("COLUMNS", "200")  # argparse wraps usage to the terminal width
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["auth", "--help"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.splitlines()[0] == "usage: likearr auth [-h] [--manual] [--promote-save]"


@pytest.mark.parametrize(("argv", "as_json"), [(["playlists"], False), (["playlists", "--json"], True)])
def test_playlists_parses_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], as_json: bool) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        cli, "build_context", lambda *a, **k: make_context(tmp_path, sink=CapturingSink(), library=FakeLibrary())
    )
    monkeypatch.setattr(cli.commands, "playlists_command", lambda ctx, **kwargs: seen.update(kwargs) or EXIT_OK)

    assert cli.main(argv) == EXIT_OK
    assert seen == {"as_json": as_json}


def test_the_cli_never_imports_the_web_ui() -> None:
    # Every cron run imports the CLI; the web package (and its optional dependencies) must stay out.
    import subprocess
    import sys

    probe = "import sys, likearr.shell.cli; print(sorted(m for m in sys.modules if m.startswith('likearr.web')))"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60)

    assert out.stdout.strip() == "[]"
