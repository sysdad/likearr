"""`likearr`: the command line.

Argument parsing and nothing else. Every subcommand builds a :class:`Context` and hands off to
`shell.run` or `shell.commands`, and `main` turns whatever comes back into an exit code.

Expected failures - a missing config file, a Lidarr that is not there - print one line and exit 1.
Another run holding the lock prints one line and exits 4. A traceback is reserved for a bug, and
even then it only reaches the terminal at ``-v``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from likearr import __version__
from likearr.adapters.health import build_sinks
from likearr.adapters.lock import LockHeld
from likearr.config import UI_PASSWORD_ENV, UI_PASSWORD_MIN_LENGTH, ConfigError, load_config, write_initial_config
from likearr.logging_setup import setup_logging
from likearr.models import EXIT_BUSY, EXIT_ERROR
from likearr.playlist_names import names_path
from likearr.ports import LidarrError, SourceError
from likearr.shell import commands, prune_commands, setup_commands
from likearr.shell.context import Context, build_context
from likearr.shell.diff_io import DiffFileError
from likearr.shell.output import emit
from likearr.shell.promote_save import DEFAULT_PLAN_PATH, PromoteSaveError, promote_save_command
from likearr.shell.run import DEFAULT_DIFF_PATH, run_command, scheduled_run_without_state
from likearr.shell.run_types import ExistingChoice

__all__ = ["build_parser", "main"]

log = logging.getLogger(__name__)

CLEANUP_COMMANDS = frozenset({"prune-report", "prune-stage", "prune-checks", "promote-save"})
"""Clean up's commands. They run whatever `[prune] enabled` says - typing one is already
opting in - but say once that the web UI's Clean up is off."""
CLEANUP_OFF_WARNING = (
    "WARN  Clean up is off in config ([prune] enabled = false): this command runs anyway, "
    "but the web UI does not offer Clean up"
)

DEFAULT_CONFIG = "config.toml"
CONFIG_ENV = "LIKEARR_CONFIG"


class _SubParser(argparse.ArgumentParser):
    """A subcommand parser that inherits the shared `-c` / `-v` options."""

    common: argparse.ArgumentParser | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        parents = list(kwargs.pop("parents", []) or [])  # type: ignore[arg-type]
        if _SubParser.common is not None:
            parents.append(_SubParser.common)
        super().__init__(*args, parents=parents, **kwargs)  # type: ignore[arg-type]


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface, in one place."""
    parser = argparse.ArgumentParser(
        prog="likearr",
        description="Mirror your Spotify follows, saved albums and Liked Songs into Lidarr - and nothing else.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get(CONFIG_ENV, DEFAULT_CONFIG),
        help=f"path to config.toml (default: ${CONFIG_ENV} or ./{DEFAULT_CONFIG})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    parser.add_argument("--version", action="version", version=f"likearr {__version__}")
    # `-c` / `-v` are also accepted AFTER the subcommand (`likearr run -c config.toml`), which is how
    # the Docker CMD and the docs write it. SUPPRESS keeps the top-level default when they are absent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_SubParser)
    _SubParser.common = common

    run = subparsers.add_parser("run", help="plan a run, and optionally apply it")
    run.add_argument("--out", type=Path, default=DEFAULT_DIFF_PATH, help="where to write the diff (default: diff.json)")
    run.add_argument(
        "--apply",
        nargs="?",
        const="",
        default=None,
        metavar="DIFF",
        help=(
            "apply a reviewed diff file (default: the --out path); "
            "with --scheduled and no file, plan and apply in one go"
        ),
    )
    run.add_argument("--scheduled", action="store_true", help="unattended run: caps unmonitors, applies without review")
    run.add_argument(
        "--accept-shrink",
        action="store_true",
        help="on a hand-run plan: accept the source/artist shrinks it sees instead of refusing their unmonitors",
    )
    run.add_argument(
        "--accept-health",
        action="store_true",
        help=(
            "on a hand-run apply: accept the new name collisions, skipped artists and oversized "
            "catalogues this run reports, so later runs stop calling them new"
        ),
    )
    run.add_argument("--force", action="store_true", help="apply a stale diff anyway (rarely what you want)")
    run.add_argument(
        "--claim-existing",
        action="store_true",
        help="on a first apply: let likearr manage the albums you already monitor that match what you like",
    )
    run.add_argument(
        "--unmonitor-rest",
        action="store_true",
        help="on a first apply: unmonitor the albums you already monitor that match nothing you like",
    )
    run.add_argument(
        "--keep",
        type=Path,
        default=None,
        metavar="FILE",
        help="with --unmonitor-rest: release group MBIDs to leave monitored, one per line",
    )

    auth = subparsers.add_parser("auth", help="authorize likearr against your Spotify account")
    auth.add_argument("--manual", action="store_true", help="print the URL and paste the redirect back by hand")
    auth.add_argument(
        "--promote-save",
        action="store_true",
        help=(
            "also ask for the write access `promote-save` needs (follow artists, save albums); "
            "without it a first sign-in is read-only"
        ),
    )

    doctor = subparsers.add_parser("doctor", help="check config, Lidarr, MusicBrainz and Spotify; change nothing")
    doctor.add_argument("--no-spotify", action="store_true", help="skip every Spotify check")
    doctor.add_argument("--json", action="store_true", help="one line of JSON (the web UI's Doctor view)")

    setup = subparsers.add_parser("setup-profiles", help="create the Lean/Full profiles, the tag and root defaults")
    setup.add_argument("--apply", action="store_true", help="make the changes (default: show them)")
    setup.add_argument("--json", action="store_true", help="one line of JSON (the web UI's Lidarr setup panel)")

    adopt = subparsers.add_parser("adopt", help="take responsibility for monitoring that predates likearr")
    adopt.add_argument(
        "--unmonitor-rest",
        action="store_true",
        help="also unmonitor every monitored album no source wants and the keep-list doesn't list",
    )
    adopt.add_argument(
        "--keep", type=Path, default=None, metavar="FILE", help="with --unmonitor-rest: keep-list, one mbid per line"
    )
    adopt.add_argument(
        "--out", type=Path, default=Path("adopt.json"), help="where to write the plan (default: adopt.json)"
    )
    adopt.add_argument(
        "--apply",
        nargs="?",
        const="",
        default=None,
        metavar="PLAN",
        help="execute a reviewed adopt plan (default: the --out path); without it, only plan",
    )

    playlists = subparsers.add_parser(
        "playlists", help="list your Spotify playlists and which a run can read; change nothing"
    )
    playlists.add_argument(
        "--json", action="store_true", help="one line of JSON for scripts (the web UI's playlist picker)"
    )

    lidarr_files = subparsers.add_parser(
        "lidarr-files", help="track files Lidarr holds for the releases a plan unmonitors; change nothing"
    )
    lidarr_files.add_argument("--plan", type=Path, required=True, help="the plan (a `run --out` file) to read")
    lidarr_files.add_argument("--out", type=Path, default=None, help="also write the answer to this JSON file")
    lidarr_files.add_argument("--json", action="store_true", help="one line of JSON (the web UI's plan review)")

    explain = subparsers.add_parser("explain", help="why a release is, or is not, monitored")
    explain.add_argument("--json", action="store_true", help="one line of JSON: the summary and the detail")
    explain.add_argument(
        "--from-last-run",
        action="store_true",
        help="answer at once from what the last run recorded, asking Spotify and Lidarr nothing",
    )
    explain.add_argument("query", help="an artist name, a release title, a song or an MBID")

    prune_report = subparsers.add_parser("prune-report", help="albums with files that no source asks for")
    prune_report.add_argument("--out", type=Path, default=Path("prune.json"), help="where to write the report")

    prune_stage = subparsers.add_parser("prune-stage", help="move prune candidates out of the library (never deletes)")
    prune_stage.add_argument("--manifest", type=Path, required=True, help="the prune-report JSON to act on")
    prune_stage.add_argument("--holding", type=Path, required=True, help="where to move files (outside the library)")
    prune_stage.add_argument("--apply", action="store_true", help="actually move them (default: show the moves)")
    prune_stage.add_argument(
        "--no-mount-check",
        action="store_true",
        help="preview without checking the library mount (the web UI's preview; refused with --apply)",
    )
    prune_stage.add_argument(
        "--out", type=Path, default=None, metavar="FILE", help="also write the preview's totals and Lidarr plan as JSON"
    )
    selection = prune_stage.add_mutually_exclusive_group(required=True)
    selection.add_argument("--artists", default="", help="comma-separated artist names or MBIDs to stage")
    selection.add_argument("--all-candidates", action="store_true", help="stage every candidate in the report")
    selection.add_argument(
        "--decisions",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "a prune-review decisions file (JSON: trash, trash_artists, promote, save, save_releases, notes - "
            "see docs/cli.md); prune-stage acts on 'trash' and 'trash_artists' only"
        ),
    )

    checks = subparsers.add_parser(
        "prune-checks",
        help="read-only: Lidarr import lists with automatic add, and its command queue, before prune-stage --apply",
    )
    checks.add_argument("--out", type=Path, default=None, metavar="FILE", help="also write the answer as JSON")

    promote_save = subparsers.add_parser(
        "promote-save",
        help=(
            "follow the 'promote' artists, and save the 'save' artists' kept albums and the "
            "'save_releases' albums, on Spotify"
        ),
    )
    promote_save.add_argument(
        "--decisions",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "a prune-review decisions file (JSON: promote, save, save_releases - see docs/cli.md); required "
            "to plan, and defaults to the path the plan recorded when applying"
        ),
    )
    promote_save.add_argument(
        "--reviewed",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "the review snapshot the decisions were made against (review-data.json). REQUIRED to "
            "plan any save: only albums a human actually reviewed and kept may be saved. Defaults "
            "on --apply to the path the plan recorded"
        ),
    )
    promote_save.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_PLAN_PATH,
        help=f"where to write the plan (default: {DEFAULT_PLAN_PATH})",
    )
    promote_save.add_argument(
        "--apply",
        nargs="?",
        const="",
        default=None,
        metavar="PLAN",
        help="write the reviewed plan to Spotify (default: the --out path)",
    )
    promote_save.add_argument("--force", action="store_true", help="apply a stale plan anyway (rarely what you want)")

    start = subparsers.add_parser(
        "start",
        help="the likearr service: web UI, scheduler and job runner (needs LIKEARR_UI_PASSWORD)",
    )
    start.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1; 0.0.0.0 in a container)"
    )
    start.add_argument("--port", type=int, default=8770, help="port to listen on (default: 8770)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, dispatch, and turn every expected failure into a one-line message and exit 1."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except ConfigError as exc:
        emit(f"config error: {exc}")
        return EXIT_ERROR
    except LockHeld as exc:
        emit(str(exc))
        return EXIT_BUSY
    except prune_commands.PruneStagePartialError as exc:
        emit(f"stopped: {exc}")
        return EXIT_ERROR
    except (prune_commands.PruneStageError, PromoteSaveError) as exc:
        emit(f"refused: {exc}")
        return EXIT_ERROR
    except DiffFileError as exc:
        emit(str(exc))
        return EXIT_ERROR
    except (SourceError, LidarrError) as exc:
        emit(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        emit("interrupted")
        return EXIT_ERROR
    except Exception as exc:
        emit(f"unexpected error: {type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            emit(traceback.format_exc())
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "start":
        return _start(args)
    now = datetime.now(UTC)
    # A prune-stage preview reads the report and Lidarr only; only --apply re-plans against Spotify.
    needs_spotify = args.command not in {"lidarr-files", "prune-checks"} and (
        args.command not in {"doctor", "setup-profiles"} or not getattr(args, "no_spotify", False)
    )
    if args.command == "prune-stage" and not args.apply:
        needs_spotify = False
    if args.command == "run" and args.scheduled and (held := _held_without_state(args)) is not None:
        return held

    with build_context(args.config, need_spotify=needs_spotify, verbose=args.verbose) as ctx:
        if args.command in CLEANUP_COMMANDS and not ctx.config.prune.enabled:
            emit(CLEANUP_OFF_WARNING)
        if args.command == "run":
            return _run(ctx, args, now=now)
        if args.command == "auth":
            return commands.auth_command(ctx, manual=args.manual, promote_save=args.promote_save)
        if args.command == "doctor":
            return setup_commands.doctor_command(ctx, no_spotify=args.no_spotify, as_json=args.json)
        if args.command == "setup-profiles":
            return setup_commands.setup_profiles_command(ctx, do_apply=args.apply, as_json=args.json)
        if args.command == "adopt":
            apply_path = None if args.apply is None else Path(args.apply or args.out)
            return commands.adopt_command(
                ctx,
                keep_file=args.keep,
                apply_path=apply_path,
                out=args.out,
                unmonitor_rest=args.unmonitor_rest,
                now=now,
            )
        if args.command == "explain":
            return commands.explain_command(
                ctx,
                args.query,
                now=now,
                as_json=args.json,
                names_file=names_path(Path(args.config)),
                from_last_run=args.from_last_run,
            )
        if args.command == "playlists":
            return commands.playlists_command(ctx, as_json=args.json)
        if args.command == "lidarr-files":
            return commands.lidarr_files_command(ctx, plan_file=args.plan, out=args.out, as_json=args.json)
        if args.command == "prune-report":
            return prune_commands.prune_report_command(ctx, out=args.out, now=now)
        if args.command == "promote-save":
            do_apply = args.apply is not None
            return promote_save_command(
                ctx,
                decisions=args.decisions,
                reviewed=args.reviewed,
                out=args.out,
                apply_path=Path(args.apply) if do_apply and args.apply else None,
                do_apply=do_apply,
                force=args.force,
                now=now,
            )
        if args.command == "prune-checks":
            return prune_commands.prune_checks_command(ctx, out=args.out, now=now)
        if args.command == "prune-stage":
            return prune_commands.prune_stage_command(
                ctx,
                manifest=args.manifest,
                holding=args.holding,
                do_apply=args.apply,
                artists=[a for a in args.artists.split(",") if a.strip()],
                all_candidates=args.all_candidates,
                decisions=args.decisions,
                now=now,
                check_mount=not args.no_mount_check,
                out=args.out,
            )
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


def _start(args: argparse.Namespace) -> int:
    """`likearr start`. Never builds a `Context`: the server reads, and its child jobs do the work.

    Fails closed: without ``LIKEARR_UI_PASSWORD``, or with one shorter than
    ``UI_PASSWORD_MIN_LENGTH``, there is no server, because a UI that can unmonitor hundreds of
    albums must not start open or behind a guessable password. The web dependencies are imported
    here and nowhere else, so every other command starts without paying for uvicorn's import.
    """
    from likearr.web.server import serve

    # Taken out of the environment as it is read: nothing the server spawns can inherit it then.
    password = os.environ.pop(UI_PASSWORD_ENV, "")
    if not password.strip():
        emit(f"refusing to start: {UI_PASSWORD_ENV} is not set, and the web UI never runs without a password")
        return EXIT_ERROR
    # Counted as given, the same value `serve` compares logins against. Only the length is named.
    if len(password) < UI_PASSWORD_MIN_LENGTH:
        emit(
            f"refusing to start: {UI_PASSWORD_ENV} is {len(password)} character{'' if len(password) == 1 else 's'}; "
            f"use at least {UI_PASSWORD_MIN_LENGTH} (openssl rand -base64 24 makes one)"
        )
        return EXIT_ERROR
    config_path = Path(args.config).resolve()
    # A first start with only Compose's environment and an empty volume: no hand-written file.
    if write_initial_config(config_path):
        emit(f"wrote a new config file at {config_path} from the example; finish setting up in the browser")
    return serve(config_path, host=args.host, port=args.port, password=password, verbose=args.verbose)


def _held_without_state(args: argparse.Namespace) -> int | None:
    """`run --scheduled` on a new install, decided before `build_context` opens (and so creates)
    the state database: with no database there has been no reviewed apply, so the fire publishes
    `paused` and stops (`run.scheduled_run_without_state`). ``None`` to carry on as usual - a
    database exists, or the config does not load, which `build_context` then reports exactly as it
    always has."""
    try:
        config = load_config(args.config)
    except ConfigError:
        return None
    setup_logging(args.verbose)
    return scheduled_run_without_state(config, build_sinks(config.health), dry_run=args.apply is None)


def _run(ctx: Context, args: argparse.Namespace, *, now: datetime) -> int:
    """`likearr run`, whose three modes are all one code path with different arguments.

    - no ``--apply``: plan, write the diff, change nothing.
    - ``--apply [FILE]``: execute exactly that reviewed diff (``--apply`` alone means ``--out``).
    - ``--scheduled --apply``: plan and apply in one go, with no file to review and no file to
      go stale. Only this combination is allowed to skip the review step.
    """
    do_apply = args.apply is not None
    apply_path: Path | None = None
    if do_apply:
        if args.apply:
            apply_path = Path(args.apply)
        elif not args.scheduled:
            apply_path = args.out
    return run_command(
        ctx,
        now=now,
        out=args.out,
        apply_path=apply_path,
        do_apply=do_apply,
        scheduled=args.scheduled,
        force=args.force,
        accept_shrink=args.accept_shrink,
        accept_health=args.accept_health,
        existing=_existing_choice(args),
    )


def _existing_choice(args: argparse.Namespace) -> ExistingChoice | None:
    """`run`'s ``--claim-existing``, ``--unmonitor-rest`` and ``--keep``, or ``None`` when none is given."""
    if not (args.claim_existing or args.unmonitor_rest or args.keep is not None):
        return None
    keep = frozenset(commands.read_keep_file(args.keep))
    return ExistingChoice(claim=args.claim_existing, unmonitor_rest=args.unmonitor_rest, keep=keep)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
