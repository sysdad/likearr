"""`likearr prune-report`, `prune-checks` and `prune-stage`: the code that moves library files and
removes Lidarr artist rows.

They read, they print what they would do, and they change something only when the user typed
`--apply`. `prune-stage` never deletes anything at all - it moves files to a holding directory
and leaves `rm` to a human who can see what is in it.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from likearr.adapters.lock import run_lock
from likearr.core.prune import PruneReport, PruneRow, build_prune_report
from likearr.fsio import write_atomic
from likearr.models import EXIT_ERROR, EXIT_OK, PrimaryType, ReleaseKey, ResolutionStatus, SecondaryType
from likearr.ports import LidarrError, LidarrMetadataError, SourceError
from likearr.shell.commands import _view_of_everything
from likearr.shell.context import Context
from likearr.shell.output import emit
from likearr.shell.run import plan

__all__ = [
    "prune_report_command",
    "prune_stage_command",
]

# The name the prune log lines carried before the split, so their output is unchanged.
log = logging.getLogger("likearr.shell.commands")

PRUNE_TOP_N = 20


# ---------------------------------------------------------------------------- prune


def prune_report_command(ctx: Context, *, out: Path = Path("prune.json"), now: datetime | None = None) -> int:
    """`likearr prune-report`: albums with files that no source asks for. Writes nothing to Lidarr."""
    now = now or datetime.now(UTC)
    try:
        result = plan(ctx, now=now, scheduled=False, persist=False)
    except (SourceError, LidarrError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    report = build_prune_report(
        result.desired,
        _view_of_everything(ctx),
        ctx.state.owned_releases(),
        list(result.resolve_result.resolutions.values()),
        now=now,
        followed_read=ctx.config.spotify.followed_artists,
        # A follow that never reached a MusicBrainz artist: the review must not say "not followed".
        unmatched_follows=frozenset(
            r.artist_name.casefold()
            for r in result.resolve_result.artist_resolutions.values()
            if r.artist_name and (r.status is not ResolutionStatus.RESOLVED or not r.artist_mbid)
        ),
        # The songs a protected row names by title, not by id.
        tracks=result.snapshot.tracks,
    )
    write_atomic(out, json.dumps(_prune_to_dict(report), indent=2) + "\n", mode=0o600)

    emit(f"likearr prune report ({out}):")
    emit(f"  {report.total_candidates:>6} candidates, {_human_bytes(report.total_bytes)}")
    emit(f"  {len(report.protected):>6} protected (only local copy of a liked track)")
    top = sorted(report.bytes_by_artist.items(), key=lambda kv: (-kv[1], kv[0]))[:PRUNE_TOP_N]
    if top:
        emit("")
        emit(f"  top {len(top)} artists by candidate bytes:")
        for artist, size in top:
            emit(f"    {_human_bytes(size):>10}  {report.by_artist.get(artist, 0):>4} albums  {artist}")
    return EXIT_OK


def _prune_to_dict(report: PruneReport) -> dict[str, Any]:
    return {
        "created_at": report.created_at.isoformat(),
        "summary": {
            "candidates": report.total_candidates,
            "protected": len(report.protected),
            "total_bytes": report.total_bytes,
            "by_artist": dict(sorted(report.by_artist.items())),
            "bytes_by_artist": dict(sorted(report.bytes_by_artist.items())),
        },
        "candidates": [_row_to_dict(r) for r in report.candidates],
        "protected": [_row_to_dict(r) for r in report.protected],
    }


def _row_to_dict(row: PruneRow) -> dict[str, Any]:
    return {
        "artist_mbid": row.artist_mbid,
        "artist_name": row.artist_name,
        "lidarr_artist_id": row.lidarr_artist_id,
        "rg_mbid": row.rg_mbid,
        "title": row.title,
        "primary_type": row.primary_type.value if row.primary_type is not None else None,
        "secondary_types": sorted(t.value for t in row.secondary_types),
        "release_date": row.release_date.isoformat() if row.release_date is not None else None,
        "track_file_count": row.track_file_count,
        "size_on_disk": row.size_on_disk,
        "lidarr_album_id": row.lidarr_album_id,
        "path": row.path,
        "protected_reason": row.protected_reason,
        "protection": row.protection.to_dict() if row.protection is not None else None,
        "artist_followed": row.artist_followed,
        "follow_unmatched": row.follow_unmatched,
    }


def _row_from_dict(raw: dict[str, Any]) -> PruneRow:
    return PruneRow(
        artist_mbid=str(raw["artist_mbid"]),
        artist_name=str(raw.get("artist_name") or ""),
        lidarr_artist_id=raw.get("lidarr_artist_id"),
        rg_mbid=str(raw["rg_mbid"]),
        title=str(raw.get("title") or ""),
        primary_type=PrimaryType(raw["primary_type"]) if raw.get("primary_type") else None,
        secondary_types=frozenset(SecondaryType(t) for t in raw.get("secondary_types") or []),
        release_date=None,
        track_file_count=int(raw.get("track_file_count") or 0),
        size_on_disk=int(raw.get("size_on_disk") or 0),
        lidarr_album_id=raw.get("lidarr_album_id"),
        path=str(raw.get("path") or ""),
        protected_reason=raw.get("protected_reason"),
        artist_followed=raw["artist_followed"] if isinstance(raw.get("artist_followed"), bool) else None,
        follow_unmatched=raw.get("follow_unmatched") is True,
    )


PRUNE_CHECKS_VERSION = 1


def prune_checks_command(ctx: Context, *, out: Path | None = None, now: datetime | None = None) -> int:
    """`likearr prune-checks`: what to check in Lidarr before `prune-stage --apply`. Read-only.

    - **Import lists with automatic add**: they would add back an artist the stage removes.
    - **The command queue**: a rescan or refresh still running while files move out can import
      them again, so the move waits for an idle queue.

    Writes JSON to `out` (the web UI's Clean up checklist reads it) and prints the same in words.
    A check Lidarr cannot answer is recorded with the error, never guessed; the other still runs.
    """
    now = now or datetime.now(UTC)
    answer: dict[str, Any] = {"version": PRUNE_CHECKS_VERSION, "checked_at": now.isoformat(), "errors": {}}
    try:
        lists = ctx.lidarr.import_lists()
        answer["import_lists"] = [
            {
                "id": int(raw["id"]) if isinstance(raw.get("id"), int) else 0,
                "name": str(raw.get("name") or ""),
                "auto_add": raw.get("enableAutomaticAdd") is True,
            }
            for raw in lists
        ]
    except LidarrError as exc:
        answer["import_lists"] = None
        answer["errors"]["import_lists"] = str(exc)
    try:
        answer["queue"] = [
            {"name": str(raw.get("name") or raw.get("commandName") or ""), "status": str(raw.get("status") or "")}
            for raw in ctx.lidarr.command_queue()
        ]
    except LidarrError as exc:
        answer["queue"] = None
        answer["errors"]["queue"] = str(exc)

    auto = [row["name"] for row in answer["import_lists"] or [] if row["auto_add"]]
    if answer["import_lists"] is None:
        emit(f"FAIL  import lists: {answer['errors']['import_lists']}")
    elif auto:
        emit(f"WARN  import lists with automatic add: {', '.join(auto)} - they would add back what the stage removes")
    else:
        emit("ok    no import list adds artists automatically")
    if answer["queue"] is None:
        emit(f"FAIL  command queue: {answer['errors']['queue']}")
    elif answer["queue"]:
        names = ", ".join(sorted({row["name"] for row in answer["queue"]}))
        emit(f"WARN  Lidarr is busy: {len(answer['queue'])} command(s) queued or running ({names})")
    else:
        emit("ok    Lidarr's command queue is idle")
    if out is not None:
        write_atomic(out, json.dumps(answer, indent=1) + "\n")
    return EXIT_OK


class PruneStageError(Exception):
    """`prune-stage` refused to run. The message says why, and nothing was moved."""


@dataclass(slots=True)
class Move:
    """One file `prune-stage` would move, or moved."""

    source: str
    dest: Path
    size: int
    artist_name: str = ""
    artist_id: int | None = None
    """Lidarr's id for the artist, so a move that stops part-way can still have them rescanned."""
    artist_mbid: str = ""
    rg_mbid: str = ""


def prune_stage_command(
    ctx: Context,
    *,
    manifest: Path,
    holding: Path,
    do_apply: bool = False,
    artists: Sequence[str] = (),
    all_candidates: bool = False,
    decisions: Path | None = None,
    now: datetime | None = None,
    check_mount: bool = True,
    out: Path | None = None,
) -> int:
    """`likearr prune-stage`: move a candidate's files out of the library, reversibly.

    Nothing is deleted, ever. Files move into ``<holding>/<date>/<artist>/<album>/`` keeping
    their name inside the album folder, and a `manifest.json` in the holding directory records
    every source, destination, size and timestamp, so the move can be undone by hand.

    Selection is exactly one of `artists`, `all_candidates` or `decisions` - a prune-review
    decisions file (see :func:`_select_from_decisions` and ``docs/dev/DESIGN.md``, "Prune decisions
    file").

    Lidarr is told afterwards: an artist whose every candidate was staged is removed from Lidarr
    with ``deleteFiles=false`` (the files are already elsewhere); a partial stage triggers a
    RescanArtist so Lidarr notices what left.

    The mount is checked first, preview and apply alike: the root folder must be visible
    at Lidarr's path and the holding folder on its filesystem, so a wrong mount is caught by the
    preview rather than by the first file of a real move. `check_mount=False` is for the web UI's
    preview, which has no library mount; it is refused with `do_apply`. `out` writes the preview's
    totals and Lidarr plan as JSON (`_stage_summary`).

    `do_apply` holds the run lock for the rest of the command: the freshness check, the ownership
    read that decides remove or rescan, the moves and the calls to Lidarr all happen inside it, so
    nothing else can act on a world this run is about to change out from under it. The preview
    never takes the lock and keeps working while a run is going.

    Raises:
        PruneStageError: the holding directory is inside the Lidarr root folder, the mount is
            wrong, the selection is empty or ambiguous, or a decisions file names a protected
            release group. Nothing has been moved when this is raised.
        LockHeld: another run holds the lock (`do_apply` only).
    """
    now = now or datetime.now(UTC)
    root_folder = ctx.config.lidarr.root_folder
    if not root_folder:
        raise PruneStageError("[lidarr] root_folder is not set, so there is no library to move files out of")
    _check_holding(holding, root_folder)
    if do_apply and not check_mount:
        raise PruneStageError("--no-mount-check is for a preview only; --apply always checks the mount")
    if check_mount:
        _check_root_visible(root_folder)

    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PruneStageError(f"cannot read the prune report at {manifest}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PruneStageError(f"{manifest} is not a prune report")

    candidates = [_row_from_dict(r) for r in raw.get("candidates") or [] if isinstance(r, dict)]
    protected_rows = [_row_from_dict(r) for r in raw.get("protected") or [] if isinstance(r, dict)]
    protected_artists = {r.artist_mbid for r in protected_rows}
    if not candidates:
        emit("no candidates in the report; nothing to stage")
        return EXIT_OK

    chosen = sum(1 for x in (bool(artists), all_candidates, decisions is not None) if x)
    if chosen > 1:
        raise PruneStageError("choose only one of --artists, --all-candidates or --decisions")
    if all_candidates:
        selected = candidates
    elif decisions is not None:
        selected = _select_from_decisions(decisions, candidates, protected_rows)
    elif artists:
        wanted = {a.strip().lower() for a in artists if a.strip()}
        selected = [r for r in candidates if r.artist_name.lower() in wanted or r.artist_mbid.lower() in wanted]
        if not selected:
            raise PruneStageError("none of the named artists have candidates in this report")
    else:
        raise PruneStageError("choose what to stage: --artists <name,name>, --all-candidates or --decisions FILE")

    # `--apply` takes the run lock from here on: this is where the world gets read (the
    # freshness check, and `_lidarr_plan`'s ownership read below) and then acted on, so it is the
    # window a concurrent run could invalidate. The preview never takes it - the web UI's
    # `prune-preview` job must keep working while a run is going.
    with run_lock(ctx.lock_path) if do_apply else contextlib.nullcontext():
        if do_apply:
            _check_manifest_fresh(ctx, selected, now=now)

        day = now.date().isoformat()
        moves: list[Move] = []
        for row in selected:
            moves.extend(_moves_for(ctx, row, holding / day))
        # Every file Lidarr lists now for the albums being staged: after this stage none is left,
        # moved now or by an earlier stage (see `_lidarr_plan`).
        listed: dict[str, int] = {}
        for move in moves:
            listed[move.artist_mbid] = listed.get(move.artist_mbid, 0) + 1
        moves, earlier = _skip_moved_before(moves, holding)
        if earlier:
            emit(f"{earlier} file(s) an earlier stage already moved (see {JOURNAL} in {holding}); left as they are")
        if check_mount:
            _check_mount(holding, root_folder, moves)

        total = sum(m.size for m in moves)
        for move in moves:
            emit(f"{'move ' if do_apply else 'would'} {move.source}")
            emit(f"       -> {move.dest}")
        emit("")
        emit(f"{len(moves)} files, {_human_bytes(total)}, from {len(selected)} albums")

        plan = _lidarr_plan(ctx, selected, candidates, protected_artists, listed)
        removals = [p for p in plan if p[2] == "remove"]
        rescans = [p for p in plan if p[2] == "rescan"]
        emit("")
        emit(f"Lidarr afterwards: remove {len(removals)} artists (row only, never files), rescan {len(rescans)}")
        for name, _artist_id, action, why in plan:
            emit(f"  {'remove' if action == 'remove' else 'rescan'} {name!r}: {why}")

        if not check_mount:
            emit("")
            emit("mount not checked: the web UI has no library mount - the terminal preview checks it")
        if out is not None:
            summary = _stage_summary(
                moves, selected, plan, holding=holding, decisions=decisions, mount_checked=check_mount, now=now
            )
            write_atomic(out, json.dumps(summary, indent=1) + "\n")

        if not do_apply:
            emit("re-run with --apply to move them (nothing is ever deleted)")
            return EXIT_OK
        if not moves:
            # An earlier stage moved everything, and may not have reached Lidarr (it was down): finish.
            emit("nothing to move")
            _tell_lidarr(ctx, plan)
            return EXIT_OK

        moved: list[Move] = []
        problem = ""
        try:
            problem = _move_all(moves, holding / day, root_folder, moved, now=now)
        finally:
            # Also on Ctrl-C: whatever moved is recorded (`_move_all`) and Lidarr is told what it
            # can safely be told - a remove only for an artist whose every file is out.
            _tell_lidarr(ctx, _plan_after(plan, moves, moved))
        if problem:
            raise PruneStageError(
                f"stopped after {len(moved)} of {len(moves)} files: {problem}. Every file that moved is recorded "
                f"in {holding / day / 'manifest.json'}; run the same command again to move the rest"
            )
        emit(f"ok    moved {len(moved)} files; manifest at {holding / day / 'manifest.json'}")
        return EXIT_OK


def _plan_after(
    plan: Sequence[tuple[str, int, str, str]], moves: Sequence[Move], moved: Sequence[Move]
) -> list[tuple[str, int, str, str]]:
    """The Lidarr plan for what actually moved. A remove stands only for an artist all of whose
    files moved - this stage's, and any an earlier stage moved; a stage that stopped before an
    artist's last file rescans them instead, and one that never reached them leaves them alone.
    Re-running the command then finishes the job, removal included (`_lidarr_plan` counts what
    Lidarr lists, not what the report said)."""
    total: dict[int | None, int] = {}
    done: dict[int | None, int] = {}
    for move in moves:
        total[move.artist_id] = total.get(move.artist_id, 0) + 1
    for move in moved:
        done[move.artist_id] = done.get(move.artist_id, 0) + 1
    out: list[tuple[str, int, str, str]] = []
    for name, artist_id, action, why in plan:
        whole, some = done.get(artist_id, 0) == total.get(artist_id, 0), done.get(artist_id, 0) > 0
        if action == "remove" and whole:
            out.append((name, artist_id, action, why))
        elif some or (action == "rescan" and whole):
            out.append((name, artist_id, "rescan", why if whole else "stopped part-way"))
    return out


JOURNAL = "moves.jsonl"
"""One line per file, written and flushed to disk as each move happens: a stage that stops
part-way leaves an exact record of what moved. ``manifest.json`` gathers the same when it ends."""


def _move_all(moves: Sequence[Move], day_dir: Path, root_folder: str, moved: list[Move], *, now: datetime) -> str:
    """Rename each file into the holding folder, appending it to `moved` and to the day's journal
    as it goes; ``""``, or why it stopped.

    `os.rename`, never a copy: across two mounts - even two bind mounts of one filesystem, which
    share a device number - `rename(2)` fails with EXDEV, and `shutil.move` would then copy and
    delete hundreds of GB. Here that stops the stage at the first such file, with nothing half-moved.
    A file already at its destination is never replaced, which a rename would do silently. A move
    the journal cannot record (a full disk) stops the stage too. Whatever happens - a stop, an
    exception, Ctrl-C - ``manifest.json`` is written from what really moved."""
    earlier = _journal_entries(day_dir / JOURNAL)
    records: list[dict[str, Any]] = []
    journal = None
    try:
        for move in moves:
            try:
                move.dest.parent.mkdir(parents=True, exist_ok=True)
                if move.dest.exists() or move.dest.is_symlink():
                    raise FileExistsError(errno.EEXIST, "a file is already there", str(move.dest))
                os.rename(move.source, move.dest)
            except OSError as exc:
                return _move_problem(exc, move, root_folder)
            moved.append(move)
            record = {
                "source": move.source,
                "dest": str(move.dest),
                "size": move.size,
                "artist_mbid": move.artist_mbid,
                "rg_mbid": move.rg_mbid,
                "moved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            records.append(record)
            try:
                if journal is None:
                    journal = (day_dir / JOURNAL).open("a", encoding="utf-8")
                journal.write(json.dumps(record) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            except OSError as exc:
                return f"{move.source} moved, but {day_dir / JOURNAL} could not record it ({exc.strerror or exc})"
        return ""
    finally:
        if journal is not None:
            try:
                journal.close()
            except OSError:
                log.warning("could not close %s", day_dir / JOURNAL)
        if records:
            _write_manifest(day_dir, [*earlier, *records], now=now)


def _journal_entries(path: Path) -> list[dict[str, Any]]:
    """The moves a journal records; a line that will not read (cut short by a crash) is skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("source"), str) and isinstance(entry.get("dest"), str):
            out.append(entry)
    return out


def _skip_moved_before(moves: Sequence[Move], holding: Path) -> tuple[list[Move], int]:
    """Leave out the files an earlier stage already moved: gone from the library, recorded in a
    journal under the holding folder, and there at their destination. Lidarr lists them until its
    rescan finishes, so a re-run in the meantime would otherwise read as a library not mounted."""
    done: dict[str, str] = {}
    for journal in holding.glob(f"*/{JOURNAL}"):
        done.update((e["source"], e["dest"]) for e in _journal_entries(journal))
    kept = [
        m for m in moves if not (m.source in done and not os.path.lexists(m.source) and Path(done[m.source]).exists())
    ]
    return kept, len(moves) - len(kept)


def _move_problem(exc: OSError, move: Move, root_folder: str) -> str:
    if exc.errno == errno.EXDEV:
        return (
            f"{move.source} and the holding folder are on different mounts, so it can only be copied, and "
            "likearr never copies instead of moving. Mount the library's parent directory once, so the root "
            f"folder {root_folder} and the holding folder sit side by side on one mount (docs/DEPLOY.md, "
            "'Mounting the library for prune-stage')"
        )
    return f"{move.source}: {exc.strerror or exc}"


def _write_manifest(day_dir: Path, moves: Sequence[Mapping[str, Any]], *, now: datetime) -> None:
    """``manifest.json``: every move of the day, this stage's and any earlier one's. Written with
    `write_atomic`, so a power cut leaves the last whole manifest, never an empty one; a failure to
    write it is logged, never raised over the stop it reports."""
    try:
        write_atomic(
            day_dir / "manifest.json",
            json.dumps({"created_at": now.isoformat(), "moves": list(moves)}, indent=2) + "\n",
        )
    except OSError as exc:
        log.warning("could not write %s: %s", day_dir / "manifest.json", exc)


def _check_manifest_fresh(ctx: Context, selected: Sequence[PruneRow], *, now: datetime) -> None:
    """Refuse to stage an album that a source or likearr has claimed since the report was written.

    A prune report is a snapshot of "nothing asks for these files". A manifest can sit for days
    while the user follows the artist or a run takes ownership of the album, and moving files out
    from under either would undo a decision made after the review. Only what changed is named.
    """
    fresh = plan(ctx, now=now, scheduled=False, persist=False)
    owned = ctx.state.owned_releases()
    changed: list[str] = []
    for row in selected:
        key = ReleaseKey(artist_mbid=row.artist_mbid, rg_mbid=row.rg_mbid)
        release = fresh.desired.releases.get(key)
        why = None
        if key in owned:
            why = "now owned by likearr"
        elif release is not None and release.reasons:
            why = "now wanted by Spotify (" + ", ".join(sorted(r.key for r in release.reasons)) + ")"
        if why:
            changed.append(f"  {row.artist_name} - {row.title}: {why}")
    if changed:
        raise PruneStageError(
            f"{len(changed)} album(s) in the report have been claimed since it was written; "
            "nothing was moved. Re-run `likearr prune-report` and stage from the new report:\n" + "\n".join(changed)
        )


STAGE_SUMMARY_VERSION = 1


def _stage_summary(
    moves: Sequence[Move],
    selected: Sequence[PruneRow],
    plan: Sequence[tuple[str, int, str, str]],
    *,
    holding: Path,
    decisions: Path | None,
    mount_checked: bool,
    now: datetime,
) -> dict[str, Any]:
    """What a `prune-stage` preview found, for the web UI: the totals, the Lidarr plan, and
    the sha256 of the decisions file it read, so the page shows it only beside that export."""
    digest = ""
    if decisions is not None:
        try:
            digest = hashlib.sha256(decisions.read_bytes()).hexdigest()
        except OSError:
            digest = ""
    return {
        "version": STAGE_SUMMARY_VERSION,
        "created_at": now.isoformat(),
        "decisions_sha256": digest,
        "holding": str(holding),
        "files": len(moves),
        "bytes": sum(m.size for m in moves),
        "albums": len(selected),
        "remove": [{"name": name, "why": why} for name, _id, action, why in plan if action == "remove"],
        "rescan": [{"name": name, "why": why} for name, _id, action, why in plan if action == "rescan"],
        "mount_checked": mount_checked,
    }


_HOW_TO_MOUNT = "docs/DEPLOY.md, 'Mounting the library for prune-stage'"
MOUNTINFO = Path("/proc/self/mountinfo")
_SHOWN = 3


def _check_root_visible(root_folder: str) -> None:
    root = Path(root_folder)
    if not root.is_dir():
        raise PruneStageError(
            f"the Lidarr root folder {root} is not visible here, so no file could be moved. Mount the library "
            f"at the path Lidarr uses ({root}), with the holding folder beside it ({_HOW_TO_MOUNT}); nothing was moved"
        )


def _check_mount(holding: Path, root_folder: str, moves: Sequence[Move], *, mountinfo: str | None = None) -> None:
    """Refuse a mount that would fail part-way through a real move, before a move is listed:

    - the root folder is visible at Lidarr's own path;
    - the holding folder is on the root folder's filesystem, and - where ``/proc/self/mountinfo``
      says - on the same mount: two bind mounts of one filesystem share a device number, yet a
      rename between them fails;
    - every file Lidarr lists is here, at the size Lidarr lists: a partial mount, or another copy
      of the library, is caught now rather than at the first missing file of ``--apply``.

    Checked on the nearest existing parent of the holding folder, which is where it would be made."""
    _check_root_visible(root_folder)
    root = Path(root_folder)
    anchor = holding if holding.is_absolute() else Path.cwd() / holding
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    apart = (
        f"the holding folder {holding} is not on the same {{what}} as the root folder {root}, so a move would "
        "have to copy every file instead of renaming it. Mount the library's parent directory once, so the root "
        f"folder and the holding folder sit side by side ({_HOW_TO_MOUNT}); nothing was moved"
    )
    if anchor.stat().st_dev != root.stat().st_dev:
        raise PruneStageError(apart.format(what="filesystem"))
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO.read_text(encoding="utf-8")
        except OSError:
            mountinfo = ""
    if mountinfo and _mount_of(anchor, mountinfo) != _mount_of(root, mountinfo):
        raise PruneStageError(apart.format(what="mount"))

    missing: list[str] = []
    differs: list[str] = []
    for move in moves:
        try:
            size = os.stat(move.source).st_size
        except OSError:
            missing.append(move.source)
            continue
        if move.size and size != move.size:
            differs.append(f"{move.source} ({size} bytes here, {move.size} in Lidarr)")
    if missing:
        raise PruneStageError(
            f"{len(missing)} of {len(moves)} files Lidarr lists are not here (e.g. {'; '.join(missing[:_SHOWN])}): "
            f"the library is not mounted, or not all of it, at the path Lidarr uses - or they were moved by hand, "
            f"or by a stage that left no record here. Mount it so {root} here is Lidarr's {root} ({_HOW_TO_MOUNT}), "
            "or wait for Lidarr's rescan to finish and build a new report; nothing was moved"
        )
    if differs:
        raise PruneStageError(
            f"{len(differs)} of {len(moves)} files are not the size Lidarr lists (e.g. {'; '.join(differs[:_SHOWN])}): "
            "this may be another copy of the library, or Lidarr's record is out of date. Check the mount, or rescan "
            "the artists in Lidarr and build a new report; nothing was moved"
        )


def _mount_of(path: Path, mountinfo: str) -> str | None:
    """The id of the mount `path` is on (``/proc/self/mountinfo``: the longest mount point that holds
    its real path), or ``None``."""
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        holds = point == "/" or real == point or real.startswith(point.rstrip("/") + "/")
        if holds and (best is None or len(point) > best[0]):
            best = (len(point), fields[0])
    return best[1] if best is not None else None


def _check_holding(holding: Path, root_folder: str) -> None:
    """Refuse a holding directory inside the library: that would move files onto themselves. Both
    paths are compared as written and as resolved (`os.path.realpath`), so neither ``..`` nor a
    symlink into the root folder slips past."""
    root = Path(root_folder)
    real_root = Path(os.path.realpath(root))
    for candidate in {holding, holding.expanduser()}:
        absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
        real = Path(os.path.realpath(absolute))
        if any(p == r or p.is_relative_to(r) for p in (absolute, real) for r in (root, real_root)):
            raise PruneStageError(
                f"the holding directory {absolute} is inside the Lidarr root folder {root}; "
                "stage somewhere outside the library"
            )


def _read_decisions(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PruneStageError(f"cannot read the prune decisions at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PruneStageError(f"{path} is not a prune decisions file")
    return raw


def _string_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _select_from_decisions(path: Path, candidates: Sequence[PruneRow], protected: Sequence[PruneRow]) -> list[PruneRow]:
    """Turn a prune-review decisions file into the candidate rows it selects.

    See ``docs/dev/DESIGN.md``, "Prune decisions file", for the format. Only ``trash`` (release-group
    mbids) and ``trash_artists`` (every candidate row for that artist) are consumed here;
    ``promote``/``save`` are the review page's way of saying what a later Spotify step should do
    with a release, and prune-stage - which only ever moves files - reports them and does nothing
    with them.

    An id that names neither a candidate nor a protected row is unknown: it is warned about and
    skipped, never silently dropped. An id in ``trash`` that names a PROTECTED row is refused
    outright (`PruneStageError`) - that release group is the only local copy of a liked track, and
    "unknown, skip it" would be the wrong answer for a row the report explicitly flagged.
    """
    raw = _read_decisions(path)
    if raw.get("version") != 1:
        emit(f"warn  {path} does not declare decisions file version 1; treating it as version 1 anyway")

    by_rg = {row.rg_mbid: row for row in candidates}
    protected_by_rg = {row.rg_mbid: row for row in protected}
    by_artist: dict[str, list[PruneRow]] = {}
    for row in candidates:
        by_artist.setdefault(row.artist_mbid, []).append(row)

    wanted_rg: set[str] = set()

    for mbid in _string_list(raw.get("trash")):
        if mbid in by_rg:
            wanted_rg.add(mbid)
            continue
        protected_row = protected_by_rg.get(mbid)
        if protected_row is not None:
            raise PruneStageError(
                f"the decisions file asks to trash {protected_row.title!r} ({mbid}), which is protected: "
                f"{protected_row.protected_reason}"
            )
        emit(f"warn  {mbid!r} in 'trash' is not a candidate release group in this report; skipped")

    for mbid in _string_list(raw.get("trash_artists")):
        rows = by_artist.get(mbid)
        if not rows:
            emit(f"warn  {mbid!r} in 'trash_artists' has no candidate rows in this report; skipped")
            continue
        wanted_rg.update(row.rg_mbid for row in rows)

    promote, save = _string_list(raw.get("promote")), _string_list(raw.get("save"))
    save_releases = _string_list(raw.get("save_releases"))
    if promote or save or save_releases:
        emit(
            f"info  {len(promote)} 'promote', {len(save)} 'save' and {len(save_releases)} 'save_releases' "
            "decision(s) are ignored by prune-stage; they are for a later Spotify step"
        )

    if not wanted_rg:
        raise PruneStageError(f"{path} selects nothing to stage; check its 'trash' and 'trash_artists'")
    return [row for row in candidates if row.rg_mbid in wanted_rg]


def _moves_for(ctx: Context, row: PruneRow, day_dir: Path) -> list[Move]:
    """Every file of one album, with the destination that preserves its name inside the album."""
    if row.lidarr_album_id is None:
        log.warning("no Lidarr album id for %r; skipping", row.title)
        return []
    try:
        files = ctx.lidarr.track_files(row.lidarr_album_id)
    except LidarrError as exc:
        log.warning("cannot list files for %r: %s", row.title, exc)
        return []

    out: list[Move] = []
    target = day_dir / _safe(row.artist_name or row.artist_mbid) / _safe(row.title or row.rg_mbid)
    for entry in files:
        source = str(entry.get("path") or "")
        if not source:
            continue
        out.append(
            Move(
                source=source,
                dest=target / _relative_name(source, row.path),
                size=int(entry.get("size") or 0),
                artist_name=row.artist_name,
                artist_id=row.lidarr_artist_id,
                artist_mbid=row.artist_mbid,
                rg_mbid=row.rg_mbid,
            )
        )
    return out


def _relative_name(source: str, artist_path: str) -> Path:
    """The file's name inside its album folder, so multi-disc layouts survive the move."""
    path = Path(source)
    if artist_path:
        try:
            relative = path.relative_to(Path(artist_path))
        except ValueError:
            return Path(path.name)
        parts = relative.parts[1:]  # drop the album folder; it is recreated from the title
        return Path(*parts) if parts else Path(path.name)
    return Path(path.name)


def _lidarr_plan(
    ctx: Context,
    selected: Sequence[PruneRow],
    candidates: Sequence[PruneRow],
    protected_artists: set[str],
    listed: Mapping[str, int],
) -> list[tuple[str, int, str, str]]:
    """What to tell Lidarr after staging: ``(artist name, lidarr id, "remove" | "rescan", why)``.

    An artist is removed from Lidarr (its row only, never files) only when staging leaves it with
    nothing: every candidate of theirs was selected, no row of theirs is protected, likearr owns
    no release of theirs (the ownership boundary), and Lidarr's live count of track-file records
    for the artist (``/trackfile?artistId=``, the unit `listed` is counted in, never the artist's
    ``trackFileCount`` statistic) equals `listed` - the files Lidarr lists now for the albums being
    staged, every one of which this stage moves or an earlier stage already moved. So nothing
    else of theirs is on disk, and
    the answer is the same however old the report is: a stage re-run after one that stopped
    part-way (before or after Lidarr's rescan) still removes the artist it finishes. Anything less
    is a rescan of the artist's folder, so Lidarr notices what left.
    """
    selected_by_artist: dict[str, list[PruneRow]] = {}
    for row in selected:
        selected_by_artist.setdefault(row.artist_mbid, []).append(row)
    all_by_artist: dict[str, list[PruneRow]] = {}
    for row in candidates:
        all_by_artist.setdefault(row.artist_mbid, []).append(row)
    # Every artist likearr owns a release of: the adopted ones live only in owned_releases, so
    # owned_artists (artists likearr itself added) is not enough on its own.
    owned_artists = {key.artist_mbid for key in ctx.state.owned_releases()} | set(ctx.state.owned_artists())

    plan: list[tuple[str, int, str, str]] = []
    for artist_mbid, rows in sorted(selected_by_artist.items()):
        artist_id = next((r.lidarr_artist_id for r in rows if r.lidarr_artist_id is not None), None)
        if artist_id is None:
            continue
        name = rows[0].artist_name
        if artist_mbid in protected_artists:
            plan.append((name, artist_id, "rescan", "a protected release stays"))
        elif artist_mbid in owned_artists:
            plan.append((name, artist_id, "rescan", "likearr owns a release of theirs"))
        elif len(rows) != len(all_by_artist.get(artist_mbid, [])):
            plan.append((name, artist_id, "rescan", "only some candidates selected"))
        else:
            staged = listed.get(artist_mbid, 0)
            try:
                on_disk = ctx.lidarr.artist_track_file_records(artist_id)
            except (LidarrError, LidarrMetadataError) as exc:
                plan.append((name, artist_id, "rescan", f"could not read Lidarr's file count: {exc}"))
                continue
            if on_disk != staged:
                plan.append((name, artist_id, "rescan", f"Lidarr holds {on_disk} files, staging {staged}"))
            else:
                plan.append((name, artist_id, "remove", "every file of theirs is staged"))
    return plan


def _tell_lidarr(ctx: Context, plan: Sequence[tuple[str, int, str, str]]) -> None:
    """Carry out `_lidarr_plan`: remove fully staged artists (never their files); rescan the rest."""
    for name, artist_id, action, _why in plan:
        try:
            if action == "remove":
                ctx.lidarr.delete_artist(artist_id, delete_files=False)
                emit(f"ok    removed {name!r} from Lidarr (files were moved, none deleted)")
            else:
                ctx.lidarr.rescan_artist(artist_id)
                emit(f"ok    queued a rescan of {name!r}")
        except (LidarrError, LidarrMetadataError) as exc:
            log.warning("could not update Lidarr for %s: %s", name, exc)


_UNSAFE = '/\\:*?"<>|'


def _safe(name: str) -> str:
    """A directory name that is safe on every filesystem likearr might be staging onto."""
    cleaned = "".join("_" if c in _UNSAFE else c for c in name).strip(" .")
    return cleaned or "unknown"


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover
