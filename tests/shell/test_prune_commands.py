"""`shell.prune_commands`: prune-report, prune-checks and prune-stage."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.lock import LockHeld, run_lock
from likearr.models import EXIT_ERROR, EXIT_OK, PrimaryType, ReasonKind, SecondaryType
from likearr.ports import LidarrError
from likearr.shell import prune_commands
from tests.shell.commands_shared import STRANGER
from tests.shell.conftest import NOW, CapturingSink, FakeLidarr, FakeSource, make_config, make_context
from tests.unit.fakes import (
    FakeLookup,
    artist_intent,
    lidarr_album,
    lidarr_artist,
    owned,
    reason,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)

# --------------------------------------------------------------------------- prune


def _prune_world(tmp_path: Path) -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """Nothing is wanted, and Lidarr has two albums with files by an artist nobody follows."""
    lookup = FakeLookup().add(STRANGER)
    source = FakeSource(snapshot())
    other = rg("rg-8", "Another Record", artist_mbid="artist-9", artist_name="A Stranger")
    lookup.add(other)
    lidarr = FakeLidarr()
    lidarr.seed(
        lidarr_artist("artist-9", id=9, name="A Stranger", path="/music/A Stranger"),
        lidarr_album(STRANGER, id=901, artist_id=9, monitored=True, files=2, size=200),
        lidarr_album(other, id=902, artist_id=9, monitored=True, files=1, size=100),
    )
    lidarr.track_file_rows = {
        901: [
            {"path": "/music/A Stranger/Something Else/01 - One.flac", "size": 120},
            {"path": "/music/A Stranger/Something Else/02 - Two.flac", "size": 80},
        ],
        902: [{"path": "/music/A Stranger/Another Record/CD1/01 - Solo.flac", "size": 100}],
    }
    return source, lookup, lidarr


def test_prune_report_lists_candidates(tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    out = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = prune_commands.prune_report_command(ctx, out=out, now=NOW)

    payload = json.loads(out.read_text())
    assert code == EXIT_OK
    assert payload["summary"]["candidates"] == 2
    assert payload["summary"]["total_bytes"] == 300
    assert "A Stranger" in capsys.readouterr().out
    assert {row["artist_followed"] for row in payload["candidates"]} == {False}  # nobody follows them
    assert {row["protection"] for row in payload["candidates"]} == {None}


def test_prune_report_writes_prune_json_0600(tmp_path: Path, sink: CapturingSink) -> None:
    """`prune.json` is a reviewable plan, like `diff.json`, so it keeps the same
    0600 mode rather than whatever the umask gives a plain write."""
    source, lookup, lidarr = _prune_world(tmp_path)
    out = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=out, now=NOW)

    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_prune_report_writes_why_a_row_is_protected_as_data(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot's songs reach the report, and a protected row carries `protection` beside
    the terminal's `protected_reason` line."""
    from likearr.core import prune as core_prune

    source, lookup, lidarr = _prune_world(tmp_path)
    think = track_intent("Think", spotify_album("Aretha Now"), spotify_id="t1", playlist_id="pl-1")
    source.snapshot = snapshot(tracks=[think])
    seen: dict[str, object] = {}

    def build(*args: Any, **kwargs: Any) -> core_prune.PruneReport:
        seen["tracks"] = tuple(kwargs["tracks"])
        report = core_prune.build_prune_report(*args, **kwargs)
        # Hold one candidate back as a playlist song's only copy, as the protection rule would.
        row = report.candidates.pop()
        protection = core_prune.Protection(
            "album_not_downloaded", "playlist:pl-1:t1", song="Think", album="Respect", album_mbid="rg-7"
        )
        report.protected.append(replace(row, protected_reason=protection.describe(), protection=protection))
        return report

    monkeypatch.setattr(prune_commands, "build_prune_report", build)
    out = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert prune_commands.prune_report_command(ctx, out=out, now=NOW) == EXIT_OK

    assert seen["tracks"] == (think,)
    [row] = json.loads(out.read_text())["protected"]
    assert row["protected_reason"].startswith("holds a liked track (playlist:pl-1:t1) whose album 'Respect'")
    assert row["protection"] == {
        "kind": "album_not_downloaded",
        "intent_key": "playlist:pl-1:t1",
        "source": "playlist",
        "playlist_id": "pl-1",
        "track_id": "t1",
        "song": "Think",
        "song_artists": [],
        "album": "Respect",
        "album_mbid": "rg-7",
    }


def test_prune_stage_refuses_a_holding_dir_inside_the_library(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    manifest.write_text(json.dumps({"candidates": [], "protected": []}))
    with (
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
        pytest.raises(prune_commands.PruneStageError, match="inside the Lidarr root folder"),
    ):
        prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=Path("/music/holding"), all_candidates=True, now=NOW
        )


def test_prune_stage_dry_run_moves_nothing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        code = prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
        )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "would /music/A Stranger/Something Else/01 - One.flac" in out
    assert "3 files" in out
    assert not (tmp_path / "holding").exists()
    assert "delete_artist" not in lidarr.names()


def test_prune_stage_requires_a_selection(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="--artists"):
            prune_commands.prune_stage_command(
                ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", now=NOW
            )


def test_prune_stage_apply_moves_files_and_removes_the_artist(tmp_path: Path, sink: CapturingSink) -> None:
    """Whole-artist stage: files move, a manifest is written, and Lidarr forgets the artist."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    rows = {
        901: [library / "A Stranger" / "Something Else" / "01 - One.flac"],
        902: [library / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac"],
    }
    for paths in rows.values():
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("audio")
    lidarr.track_file_rows = {
        album_id: [{"path": str(p), "size": 5} for p in paths] for album_id, paths in rows.items()
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
        )

    day = holding / NOW.date().isoformat()
    assert code == EXIT_OK
    assert (day / "A Stranger" / "Something Else" / "01 - One.flac").exists()
    assert (day / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac").exists(), "disc folders survive"
    assert not rows[901][0].exists()
    moves = json.loads((day / "manifest.json").read_text())["moves"]
    assert len(moves) == 2
    assert ("delete_artist", (9, False)) in lidarr.calls
    assert "rescan_artist" not in lidarr.names()


def test_prune_stage_apply_takes_the_run_lock(tmp_path: Path, sink: CapturingSink) -> None:
    """A held lock stops --apply before anything re-plans, moves or touches Lidarr."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    rows = {
        901: [library / "A Stranger" / "Something Else" / "01 - One.flac"],
        902: [library / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac"],
    }
    for paths in rows.values():
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("audio")
    lidarr.track_file_rows = {
        album_id: [{"path": str(p), "size": 5} for p in paths] for album_id, paths in rows.items()
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with run_lock(ctx.lock_path), pytest.raises(LockHeld):
            prune_commands.prune_stage_command(
                ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
            )

    assert not holding.exists()
    for paths in rows.values():
        for path in paths:
            assert path.exists()
    assert "delete_artist" not in lidarr.names()
    assert "rescan_artist" not in lidarr.names()

    # The lock was released when the held `run_lock` block above exited - a following one succeeds.
    with (
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx,
        run_lock(ctx.lock_path),
    ):
        pass


def test_prune_stage_dry_run_still_works_while_the_lock_is_held(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preview is deliberately left unlocked: the web UI's prune-preview job must keep working
    while a run holds the lock."""
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        with run_lock(ctx.lock_path):
            code = prune_commands.prune_stage_command(
                ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
            )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "would /music/A Stranger/Something Else/01 - One.flac" in out
    assert not (tmp_path / "holding").exists()


def test_prune_stage_partial_rescans_instead_of_deleting(tmp_path: Path, sink: CapturingSink) -> None:
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    kept = library / "A Stranger" / "Something Else" / "01 - One.flac"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("audio")
    lidarr.track_file_rows = {901: [{"path": str(kept), "size": 5}], 902: []}
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        raw = json.loads(manifest.read_text())
        # Pretend one of the two candidates is protected, so the artist is only partly staged.
        moved = raw["candidates"].pop(0)
        moved["protected_reason"] = "holds a liked track"
        raw["protected"].append(moved)
        manifest.write_text(json.dumps(raw))
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=tmp_path / "holding", do_apply=True, all_candidates=True, now=NOW
        )

    assert code == EXIT_OK
    assert "delete_artist" not in lidarr.names()
    assert ("rescan_artist", 9) in lidarr.calls


def test_prune_stage_never_removes_an_artist_likearr_owns_a_release_of(tmp_path: Path, sink: CapturingSink) -> None:
    """Ownership boundary: adopted releases live only in owned_releases, and that must be enough."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    for album_id, name in ((901, "Something Else"), (902, "Another Record")):
        path = library / "A Stranger" / name / "01.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("audio")
        lidarr.track_file_rows[album_id] = [{"path": str(path), "size": 5}]
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        # likearr owns a third, unrelated release of this artist (adopted: no owned_artists row).
        third = rg("rg-9x", "Kept Record", artist_mbid="artist-9", artist_name="A Stranger")
        _key, record = owned(third, album_id=903)
        ctx.state.record_monitored([record])
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=tmp_path / "holding", do_apply=True, all_candidates=True, now=NOW
        )
    assert code == EXIT_OK
    assert "delete_artist" not in lidarr.names()
    assert ("rescan_artist", 9) in lidarr.calls


def test_prune_stage_never_removes_an_artist_with_other_files_on_disk(tmp_path: Path, sink: CapturingSink) -> None:
    """If Lidarr still holds files the report did not list, removing the artist would orphan them."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    for album_id, name in ((901, "Something Else"), (902, "Another Record")):
        path = library / "A Stranger" / name / "01.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("audio")
        lidarr.track_file_rows[album_id] = [{"path": str(path), "size": 5}]
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))
    lidarr.extra_files[9] = 1
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=tmp_path / "holding", do_apply=True, all_candidates=True, now=NOW
        )
    assert code == EXIT_OK
    assert "delete_artist" not in lidarr.names()
    assert ("rescan_artist", 9) in lidarr.calls


def test_prune_stage_rescans_when_lidarr_cannot_count_the_artists_files(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fully-selected, unowned, unprotected artist whose file count Lidarr cannot answer must
    still fall back to a rescan, never a remove (mutation M7 flips this to "remove")."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    rows = {
        901: [library / "A Stranger" / "Something Else" / "01 - One.flac"],
        902: [library / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac"],
    }
    for paths in rows.values():
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("audio")
    lidarr.track_file_rows = {
        album_id: [{"path": str(p), "size": 5} for p in paths] for album_id, paths in rows.items()
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))
    lidarr.fail_file_count = {9}

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
        )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "could not read Lidarr's file count" in out
    assert ("rescan_artist", 9) in lidarr.calls
    assert "delete_artist" not in lidarr.names()


def test_prune_stage_rescans_when_track_files_fails_for_one_album(tmp_path: Path, sink: CapturingSink) -> None:
    """A `track_files` failure for one album stages nothing for that album, but the artist's other
    album still stages; the artist is rescanned, not removed. This is because `listed` then
    undercounts what Lidarr still holds (`on_disk != staged`), not because of the fallback reason:
    removing the `except LidarrError` in `_moves_for` would make this fail too."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    kept = library / "A Stranger" / "Something Else" / "01 - One.flac"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("audio")
    unmoved = library / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac"
    unmoved.parent.mkdir(parents=True, exist_ok=True)
    unmoved.write_text("audio")
    lidarr.track_file_rows = {
        901: [{"path": str(kept), "size": 5}],
        902: [{"path": str(unmoved), "size": 5}],
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))
    lidarr.fail_track_files = {902}

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        code = prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
        )

    day = holding / NOW.date().isoformat()
    assert code == EXIT_OK
    assert (day / "A Stranger" / "Something Else" / "01 - One.flac").exists()
    assert not kept.exists()
    assert unmoved.exists(), "the album whose files could not be listed stages nothing"
    assert ("rescan_artist", 9) in lidarr.calls
    assert "delete_artist" not in lidarr.names()


def test_prune_stage_skips_a_candidate_with_no_lidarr_album_id(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """A candidate row whose `lidarr_album_id` is missing (a stale or hand-edited manifest) is
    skipped with a warning rather than staging its files."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    kept = library / "A Stranger" / "Something Else" / "01 - One.flac"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("audio")
    unmoved = library / "A Stranger" / "Another Record" / "CD1" / "01 - Solo.flac"
    unmoved.parent.mkdir(parents=True, exist_ok=True)
    unmoved.write_text("audio")
    lidarr.track_file_rows = {
        901: [{"path": str(kept), "size": 5}],
        902: [{"path": str(unmoved), "size": 5}],
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        raw = json.loads(manifest.read_text())
        another = next(c for c in raw["candidates"] if c["title"] == "Another Record")
        another["lidarr_album_id"] = None
        manifest.write_text(json.dumps(raw))
        with caplog.at_level(logging.WARNING, logger="likearr"):
            code = prune_commands.prune_stage_command(
                ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
            )

    day = holding / NOW.date().isoformat()
    assert code == EXIT_OK
    assert (day / "A Stranger" / "Something Else" / "01 - One.flac").exists()
    assert unmoved.exists(), "the album with no Lidarr id stages nothing"
    assert any("no Lidarr album id" in r.getMessage() for r in caplog.records)
    assert ("rescan_artist", 9) in lidarr.calls
    assert "delete_artist" not in lidarr.names()


def _library_config(tmp_path: Path) -> Any:
    """A config whose root folder exists, so the mount check passes and the test reaches
    what it is about."""
    (tmp_path / "library").mkdir(exist_ok=True)
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(tmp_path / "library")))
    return config


def _stage_all(ctx: Any, manifest: Path, tmp_path: Path) -> int:
    return prune_commands.prune_stage_command(
        ctx, manifest=manifest, holding=tmp_path / "holding", do_apply=True, all_candidates=True, now=NOW
    )


def test_prune_stage_apply_refuses_a_manifest_whose_album_gained_a_spotify_reason(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The report is a snapshot: an artist followed since must not have their files moved."""
    source, lookup, lidarr = _prune_world(tmp_path)
    lookup.catalogues["artist-9"] = ["rg-9", "rg-8"]
    manifest = tmp_path / "prune.json"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=_library_config(tmp_path)
    ) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        source.snapshot = snapshot(artists=[artist_intent("A Stranger", spotify_id="sp-a9")])
        with pytest.raises(prune_commands.PruneStageError, match="Something Else") as exc:
            _stage_all(ctx, manifest, tmp_path)

    assert "Another Record" in str(exc.value)
    assert "Spotify" in str(exc.value)
    assert not (tmp_path / "holding").exists(), "nothing was moved"


def test_prune_stage_apply_refuses_an_album_of_an_artist_followed_since_whose_catalogue_is_unread(
    tmp_path: Path, sink: CapturingSink
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=_library_config(tmp_path)
    ) as ctx:
        assert prune_commands.prune_report_command(ctx, out=manifest, now=NOW) == EXIT_OK
        assert json.loads(manifest.read_text())["candidates"], "the albums were candidates before the follow"
        source.snapshot = snapshot(artists=[artist_intent("A Stranger", spotify_id="sp-a9")])
        lookup.fail.add("artist_release_groups")
        with pytest.raises(prune_commands.PruneStageError, match="catalogue could not be read"):
            _stage_all(ctx, manifest, tmp_path)

    assert not (tmp_path / "holding").exists(), "nothing was moved"


def test_prune_report_refuses_a_short_spotify_read(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    source.snapshot = snapshot(schema_ok=False)
    out = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = prune_commands.prune_report_command(ctx, out=out, now=NOW)

    assert code == EXIT_ERROR
    assert not out.exists()
    assert "Spotify read was incomplete" in capsys.readouterr().out


def test_prune_stage_apply_refuses_when_the_spotify_read_is_short_now(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=_library_config(tmp_path)
    ) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        source.snapshot = snapshot(schema_ok=False)
        with pytest.raises(prune_commands.PruneStageError, match="Spotify read was incomplete"):
            _stage_all(ctx, manifest, tmp_path)

    assert not (tmp_path / "holding").exists(), "nothing was moved"


def test_prune_report_keeps_the_studio_albums_of_a_followed_artist_whose_catalogue_was_not_read(
    tmp_path: Path, sink: CapturingSink
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    source.snapshot = snapshot(artists=[artist_intent("A Stranger", spotify_id="sp-a9")])
    lookup.fail.add("artist_release_groups")
    out = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert prune_commands.prune_report_command(ctx, out=out, now=NOW) == EXIT_OK

    payload = json.loads(out.read_text())
    assert payload["candidates"] == []
    assert {row["protection"]["kind"] for row in payload["protected"]} == {"catalogue_unread"}
    assert {row["rg_mbid"] for row in payload["protected"]} == {"rg-8", "rg-9"}


def test_prune_stage_apply_refuses_a_manifest_whose_album_became_owned(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=_library_config(tmp_path)
    ) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        _key, record = owned(STRANGER, reason(ReasonKind.FOLLOWED, "sp-a9"), album_id=901)
        ctx.state.record_monitored([record])
        with pytest.raises(prune_commands.PruneStageError, match="Something Else") as exc:
            _stage_all(ctx, manifest, tmp_path)

    assert "owned" in str(exc.value)
    assert "Another Record" not in str(exc.value), "only the albums that changed are named"
    assert not (tmp_path / "holding").exists()


def test_prune_stage_dry_run_does_not_need_a_fresh_world(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        _key, record = owned(STRANGER, reason(ReasonKind.FOLLOWED, "sp-a9"), album_id=901)
        ctx.state.record_monitored([record])
        code = prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
        )
    assert code == EXIT_OK


def test_prune_stage_dry_run_prints_the_lidarr_plan(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
        )
    out = capsys.readouterr().out
    assert "Lidarr afterwards: remove 1 artists" in out
    assert "remove 'A Stranger': every file of theirs is staged" in out
    assert "delete_artist" not in lidarr.names(), "a dry run changes nothing in Lidarr"


# --------------------------------------------------------------------------- prune-stage --decisions

BYSTANDER = rg("rg-7", "Solo Record", artist_mbid="artist-7", artist_name="Bystander")


def _prune_world_two_artists(tmp_path: Path) -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """`_prune_world` plus a second, unrelated artist with one candidate of its own."""
    source, lookup, lidarr = _prune_world(tmp_path)
    lookup.add(BYSTANDER)
    lidarr.seed(
        lidarr_artist("artist-7", id=7, name="Bystander", path="/music/Bystander"),
        lidarr_album(BYSTANDER, id=701, artist_id=7, monitored=True, files=1, size=50),
    )
    lidarr.track_file_rows[701] = [{"path": "/music/Bystander/Solo Record/01 - Alone.flac", "size": 50}]
    return source, lookup, lidarr


def test_prune_stage_decisions_selects_trash_and_trash_artists(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """`trash` picks one release group by mbid; `trash_artists` picks every row for an artist."""
    source, lookup, lidarr = _prune_world_two_artists(tmp_path)
    manifest = tmp_path / "prune.json"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"version": 1, "trash": ["rg-8"], "trash_artists": ["artist-7"]}))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        code = prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", decisions=decisions, now=NOW
        )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "/music/A Stranger/Another Record/CD1/01 - Solo.flac" in out  # rg-8, via 'trash'
    assert "/music/Bystander/Solo Record/01 - Alone.flac" in out  # rg-7, via 'trash_artists'
    assert "/music/A Stranger/Something Else/01 - One.flac" not in out  # rg-9, not selected
    assert "2 files" in out


def test_prune_stage_decisions_warns_on_unknown_ids(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"version": 1, "trash": ["rg-8", "rg-nope"], "trash_artists": ["artist-nope"]}))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        code = prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", decisions=decisions, now=NOW
        )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "warn  'rg-nope' in 'trash' is not a candidate release group in this report; skipped" in out
    assert "warn  'artist-nope' in 'trash_artists' has no candidate rows in this report; skipped" in out
    assert "1 files" in out  # only rg-8 was a real candidate


def test_prune_stage_decisions_refuses_a_protected_release_group(tmp_path: Path, sink: CapturingSink) -> None:
    """Naming a protected row in `trash` is refused, not silently skipped like an unknown id."""
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"version": 1, "trash": ["rg-9"]}))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        raw = json.loads(manifest.read_text())
        moved = next(r for r in raw["candidates"] if r["rg_mbid"] == "rg-9")
        raw["candidates"].remove(moved)
        moved["protected_reason"] = "holds a liked track; this is the only copy on disk"
        raw["protected"].append(moved)
        manifest.write_text(json.dumps(raw))

        with pytest.raises(prune_commands.PruneStageError, match="protected"):
            prune_commands.prune_stage_command(
                ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", decisions=decisions, now=NOW
            )


def test_prune_stage_decisions_ignores_promote_and_save(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "version": 1,
                "trash": ["rg-8"],
                "trash_artists": [],
                "promote": ["artist-1"],
                "save": ["rg-2"],
                "notes": "keep an eye on this one",
            }
        )
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        capsys.readouterr()
        code = prune_commands.prune_stage_command(
            ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", decisions=decisions, now=NOW
        )

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "info  1 'promote', 1 'save' and 0 'save_releases' decision(s) are ignored by prune-stage" in out
    assert "later Spotify step" in out


def test_prune_stage_decisions_file_must_be_valid_json(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    decisions = tmp_path / "decisions.json"
    decisions.write_text("not json")
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="cannot read the prune decisions"):
            prune_commands.prune_stage_command(
                ctx, check_mount=False, manifest=manifest, holding=tmp_path / "holding", decisions=decisions, now=NOW
            )


def test_prune_stage_rejects_more_than_one_selection_method(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="choose only one"):
            prune_commands.prune_stage_command(
                ctx,
                check_mount=False,
                manifest=manifest,
                holding=tmp_path / "holding",
                artists=["A Stranger"],
                all_candidates=True,
                now=NOW,
            )


# --------------------------------------------------------------------------- the mount pre-check


def test_prune_stage_preview_refuses_a_root_folder_that_is_not_mounted(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="root folder /music is not visible here") as exc:
            prune_commands.prune_stage_command(
                ctx, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
            )
    assert "install.md" in str(exc.value)


def test_prune_stage_preview_refuses_the_old_example_holding_path(tmp_path: Path, sink: CapturingSink) -> None:
    """compose.example.yaml once suggested --holding /music/_likearr-holding with the library at /music."""
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="inside the Lidarr root folder /music"):
            prune_commands.prune_stage_command(
                ctx,
                check_mount=False,
                manifest=manifest,
                holding=Path("/music/_likearr-holding"),
                all_candidates=True,
                now=NOW,
            )


def test_prune_stage_preview_refuses_files_lidarr_lists_that_are_not_visible(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The root folder exists here, but the library is not mounted in it: Lidarr's paths lead nowhere."""
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=_library_config(tmp_path)
    ) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="3 of 3 files Lidarr lists are not here"):
            prune_commands.prune_stage_command(
                ctx, manifest=manifest, holding=tmp_path / "holding", all_candidates=True, now=NOW
            )


def test_prune_stage_preview_refuses_a_holding_folder_on_another_filesystem(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    config = _library_config(tmp_path)
    library, holding = tmp_path / "library", tmp_path / "elsewhere" / "holding"
    holding.parent.mkdir()
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(self, *args, **kwargs)
        if self == holding.parent:
            return os.stat_result((*result[:2], result.st_dev + 1, *result[3:]))
        return result

    monkeypatch.setattr(Path, "stat", stat)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="not on the same filesystem"):
            prune_commands.prune_stage_command(ctx, manifest=manifest, holding=holding, all_candidates=True, now=NOW)
    assert library.is_dir()


def test_prune_stage_never_skips_the_mount_check_on_apply(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _prune_world(tmp_path)
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="--apply always checks the mount"):
            prune_commands.prune_stage_command(
                ctx,
                check_mount=False,
                manifest=manifest,
                holding=tmp_path / "h",
                do_apply=True,
                all_candidates=True,
                now=NOW,
            )


def test_prune_stage_preview_writes_its_totals_and_lidarr_plan(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _prune_world_two_artists(tmp_path)
    manifest, decisions, out = tmp_path / "prune.json", tmp_path / "decisions.json", tmp_path / "stage.json"
    decisions.write_text(json.dumps({"version": 1, "trash": ["rg-9", "rg-8", "rg-7"], "trash_artists": []}))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        code = prune_commands.prune_stage_command(
            ctx,
            check_mount=False,
            manifest=manifest,
            holding=tmp_path / "holding",
            decisions=decisions,
            out=out,
            now=NOW,
        )

    summary = json.loads(out.read_text())
    assert code == EXIT_OK
    assert "mount not checked: the web UI has no library mount" in capsys.readouterr().out
    assert (summary["files"], summary["albums"], summary["mount_checked"]) == (4, 3, False)
    assert summary["decisions_sha256"] == hashlib.sha256(decisions.read_bytes()).hexdigest()
    assert {r["name"] for r in summary["remove"]} == {"A Stranger", "Bystander"}
    assert summary["holding"] == str(tmp_path / "holding")
    assert not (tmp_path / "holding").exists()
    assert "delete_artist" not in lidarr.names()


# --------------------------------------------------------------------------- prune-checks


def test_prune_checks_names_auto_add_lists_and_a_busy_queue(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    lidarr = FakeLidarr()
    lidarr.import_list_rows = [
        {"id": 3, "name": "Last.fm top", "enableAutomaticAdd": True},
        {"id": 4, "name": "Spotify playlists", "enableAutomaticAdd": False},
    ]
    lidarr.queued_commands = [{"name": "RescanFolders", "status": "started"}]
    out = tmp_path / "checks.json"
    with make_context(tmp_path, lidarr=lidarr, sink=sink) as ctx:
        code = prune_commands.prune_checks_command(ctx, out=out, now=NOW)

    answer = json.loads(out.read_text())
    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert answer["import_lists"] == [
        {"id": 3, "name": "Last.fm top", "auto_add": True},
        {"id": 4, "name": "Spotify playlists", "auto_add": False},
    ]
    assert answer["queue"] == [{"name": "RescanFolders", "status": "started"}]
    assert "automatic add: Last.fm top" in printed and "Lidarr is busy" in printed
    assert lidarr.writes() == []


def test_prune_checks_records_what_lidarr_cannot_answer(tmp_path: Path, sink: CapturingSink) -> None:
    class Down(FakeLidarr):
        def import_lists(self) -> list[dict[str, Any]]:
            raise LidarrError("lidarr GET /importlist: 500")

    out = tmp_path / "checks.json"
    with make_context(tmp_path, lidarr=Down(), sink=sink) as ctx:
        prune_commands.prune_checks_command(ctx, out=out, now=NOW)

    answer = json.loads(out.read_text())
    assert answer["import_lists"] is None and "500" in answer["errors"]["import_lists"]
    assert answer["queue"] == []


# --------------------------------------------------------------------------- the move itself


def _staged_world(tmp_path: Path, *, sizes: tuple[int, int] = (5, 5)) -> tuple[Any, FakeLidarr, Path, Path, Any]:
    """Two albums of one artist, each one file on disk, with a config whose root folder is real."""
    library = tmp_path / "library"
    source, lookup, lidarr = _prune_world(tmp_path)
    rows = {
        901: [library / "A Stranger" / "Something Else" / "01 - One.flac"],
        902: [library / "A Stranger" / "Another Record" / "01 - Solo.flac"],
    }
    for paths in rows.values():
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("audio")
    lidarr.track_file_rows = {
        album_id: [{"path": str(p), "size": size} for p in paths]
        for (album_id, paths), size in zip(rows.items(), sizes, strict=True)
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    return (source, lookup), lidarr, library, tmp_path / "holding", config


def _stage(tmp_path: Path, world: Any, *, do_apply: bool = True) -> int:
    (source, lookup), lidarr, _library, holding, config = world
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        return prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=holding, do_apply=do_apply, all_candidates=True, now=NOW
        )


def test_a_move_across_mounts_stops_at_the_first_file_and_never_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two bind mounts of one filesystem share a device number, so the check passes, but rename(2)
    answers EXDEV; shutil.move would then copy hundreds of GB and delete the originals."""
    world = _staged_world(tmp_path)
    lidarr, library, holding = world[1], world[2], world[3]

    def cross_device(src: Any, dst: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(prune_commands.os, "rename", cross_device)
    with pytest.raises(prune_commands.PruneStageError, match=r"stopped after 0 of 2 files: .* different mounts"):
        _stage(tmp_path, world)

    assert len(list(library.rglob("*.flac"))) == 2, "nothing half-moved"
    assert not list(holding.rglob("*.flac")) and not list(holding.rglob(prune_commands.JOURNAL))
    assert "delete_artist" not in lidarr.names() and "rescan_artist" not in lidarr.names()


def test_a_stage_that_stops_part_way_leaves_an_exact_record_and_removes_no_artist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _staged_world(tmp_path)
    lidarr, holding = world[1], world[3]
    real_rename = os.rename
    calls: list[Any] = []

    def second_fails(src: Any, dst: Any) -> None:
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(prune_commands.os, "rename", second_fails)
    with pytest.raises(prune_commands.PruneStageError, match="stopped after 1 of 2 files"):
        _stage(tmp_path, world)

    day = holding / NOW.date().isoformat()
    journal = [json.loads(line) for line in (day / prune_commands.JOURNAL).read_text().splitlines()]
    manifest = json.loads((day / "manifest.json").read_text())["moves"]
    assert [m["source"] for m in journal] == [calls[0]] == [m["source"] for m in manifest]
    assert Path(journal[0]["dest"]).exists() and not Path(calls[0]).exists()
    assert "delete_artist" not in lidarr.names(), "an artist is removed only when all of theirs moved"
    assert ("rescan_artist", 9) in lidarr.calls


def test_a_second_stage_the_same_day_adds_to_the_manifest(tmp_path: Path) -> None:
    world = _staged_world(tmp_path)
    holding = world[3]
    day = holding / NOW.date().isoformat()
    day.mkdir(parents=True)
    earlier = {
        "source": "/earlier/one.flac",
        "dest": str(day / "x"),
        "size": 1,
        "moved_at": "2026-09-24T01:00:00+00:00",
    }
    (day / prune_commands.JOURNAL).write_text(json.dumps(earlier) + "\n")

    assert _stage(tmp_path, world) == EXIT_OK

    moves = json.loads((day / "manifest.json").read_text())["moves"]
    assert moves[0] == earlier and len(moves) == 3


def test_a_file_already_in_the_holding_folder_is_never_replaced(tmp_path: Path) -> None:
    world = _staged_world(tmp_path)
    holding = world[3]
    taken = holding / NOW.date().isoformat() / "A Stranger" / "Another Record" / "01 - Solo.flac"
    taken.parent.mkdir(parents=True)
    taken.write_text("keep me")

    with pytest.raises(prune_commands.PruneStageError, match="a file is already there"):
        _stage(tmp_path, world)

    assert taken.read_text() == "keep me"


def test_a_file_outside_the_artist_folder_flattens_and_a_duplicate_bare_name_stops_the_stage(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """`_relative_name` flattens a file Lidarr lists outside the artist's own folder to its bare
    name. A second such file with the same bare name must stop the stage instead of silently
    overwriting the first - the same no-overwrite rule
    `test_a_file_already_in_the_holding_folder_is_never_replaced` pins, but tripped by two files
    staged in the same run rather than one already sitting in holding."""
    library = tmp_path / "library"
    library.mkdir()
    source, lookup, lidarr = _prune_world(tmp_path)
    outside = tmp_path / "outside"
    first = outside / "one" / "01 - One.flac"
    second = outside / "two" / "01 - One.flac"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("audio")
    lidarr.track_file_rows = {
        901: [{"path": str(first), "size": 5}, {"path": str(second), "size": 5}],
        902: [],
    }
    lidarr.artists["artist-9"] = lidarr_artist("artist-9", id=9, name="A Stranger", path=str(library / "A Stranger"))

    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", _with_root(config.lidarr, str(library)))
    manifest = tmp_path / "prune.json"
    holding = tmp_path / "holding"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        with pytest.raises(prune_commands.PruneStageError, match="a file is already there"):
            prune_commands.prune_stage_command(
                ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
            )

    landed = holding / NOW.date().isoformat() / "A Stranger" / "Something Else" / "01 - One.flac"
    assert landed.exists() and landed.read_text() == "audio"
    assert first.exists() != second.exists(), "exactly one of the two flattened files moved"


def _report_then_stage(tmp_path: Path, world: Any, *, report: bool = True) -> int:
    """Stage from one report across several runs, as the checklist's command does: the report is
    built once, the stage re-run against it."""
    (source, lookup), lidarr, _library, holding, config = world
    manifest = tmp_path / "prune.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, config=config) as ctx:
        if report:
            prune_commands.prune_report_command(ctx, out=manifest, now=NOW)
        return prune_commands.prune_stage_command(
            ctx, manifest=manifest, holding=holding, do_apply=True, all_candidates=True, now=NOW
        )


def _fail_second_rename(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> list[Any]:
    real_rename = os.rename
    calls: list[Any] = []

    def rename(src: Any, dst: Any) -> None:
        calls.append(src)
        if len(calls) == 2:
            raise error
        real_rename(src, dst)

    monkeypatch.setattr(prune_commands.os, "rename", rename)
    return calls


def _is_file(fd: int, path: Path) -> bool:
    """Whether `fd` is open on `path`: an `os.fsync` fake aims at the journal alone, not at every
    file `likearr.fsio.write_atomic` fsyncs on the way (`prune.json`, `manifest.json`)."""
    return path.exists() and os.path.samestat(os.fstat(fd), path.stat())


@pytest.mark.parametrize("rescanned", [True, False])
def test_a_stage_resumed_after_a_stop_removes_the_artist_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rescanned: bool
) -> None:
    """A permission error stops the stage part-way through an artist; the stop rescans them, so
    Lidarr drops the moved file. The same command again moves the rest - and removes the artist,
    whether or not Lidarr's rescan has finished by then."""
    world = _staged_world(tmp_path)
    lidarr = world[1]
    calls = _fail_second_rename(monkeypatch, PermissionError(errno.EACCES, "Permission denied"))
    with pytest.raises(prune_commands.PruneStageError, match="stopped after 1 of 2 files"):
        _report_then_stage(tmp_path, world)
    assert "delete_artist" not in lidarr.names() and ("rescan_artist", 9) in lidarr.calls
    if rescanned:  # Lidarr's rescan drops the file that left
        for album_id, rows in lidarr.track_file_rows.items():
            lidarr.track_file_rows[album_id] = [r for r in rows if r["path"] != calls[0]]
    monkeypatch.undo()

    assert _report_then_stage(tmp_path, world, report=False) == EXIT_OK

    assert ("delete_artist", (9, False)) in lidarr.calls
    assert not list(world[2].rglob("*.flac"))
    moves = json.loads((world[3] / NOW.date().isoformat() / "manifest.json").read_text())["moves"]
    assert len(moves) == 2


@pytest.mark.parametrize(("album_files", "extra_records", "removed"), [(9, 0, True), (1, 1, False)])
def test_the_removal_follows_lidarr_s_track_file_records_never_its_statistic(
    tmp_path: Path, album_files: int, extra_records: int, removed: bool
) -> None:
    """trackFileCount and the /trackfile records can disagree (a cue-sheet file, a leftover record).
    Removal compares records with records: a statistic of 18 against 2 listed still removes; a
    statistic that matches, with one record more than listed, only rescans."""
    world = _staged_world(tmp_path)
    lidarr = world[1]
    lidarr.albums["artist-9"] = {
        rg_mbid: replace(album, track_file_count=album_files) for rg_mbid, album in lidarr.albums["artist-9"].items()
    }
    lidarr.extra_records[9] = extra_records

    assert _report_then_stage(tmp_path, world) == EXIT_OK

    assert (("delete_artist", (9, False)) in lidarr.calls) is removed
    assert (("rescan_artist", 9) in lidarr.calls) is not removed
    assert "artist_track_file_count" not in lidarr.names(), "the statistic is never read"


def test_a_re_run_with_nothing_left_to_move_finishes_what_lidarr_missed(tmp_path: Path) -> None:
    """Every file moved, but Lidarr was down, so the artist stayed. The same command again moves
    nothing - an earlier stage moved it all - and removes the artist."""
    world = _staged_world(tmp_path)
    lidarr = world[1]
    lidarr.fail_delete = True
    assert _report_then_stage(tmp_path, world) == EXIT_OK
    assert "delete_artist" not in lidarr.names()
    lidarr.fail_delete = False

    assert _report_then_stage(tmp_path, world, report=False) == EXIT_OK

    assert ("delete_artist", (9, False)) in lidarr.calls


def test_after_a_stop_only_an_artist_whose_every_file_moved_is_removed() -> None:
    plan = [("A", 1, "remove", "all staged"), ("B", 2, "remove", "all staged"), ("C", 3, "rescan", "some")]
    moves = [
        prune_commands.Move(f"/l/{n}", Path(f"/h/{n}"), 1, artist_name=n, artist_id=i)
        for n, i in (("A", 1), ("B", 2), ("B", 2), ("C", 3))
    ]

    after = prune_commands._plan_after(plan, moves, moves[:2])  # stopped on B's second file, before C's

    assert after == [("A", 1, "remove", "all staged"), ("B", 2, "rescan", "stopped part-way")]


def test_a_move_the_journal_cannot_record_stops_the_stage_with_an_accurate_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _staged_world(tmp_path)
    lidarr, holding = world[1], world[3]
    journal = holding / NOW.date().isoformat() / prune_commands.JOURNAL
    real_fsync = os.fsync

    def disk_full(fd: int) -> None:
        if _is_file(fd, journal):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(prune_commands.os, "fsync", disk_full)
    with pytest.raises(prune_commands.PruneStageError, match=r"stopped after 1 of 2 files: .* could not record it"):
        _report_then_stage(tmp_path, world)

    moves = json.loads((holding / NOW.date().isoformat() / "manifest.json").read_text())["moves"]
    assert len(moves) == 1 and Path(moves[0]["dest"]).exists()
    assert ("rescan_artist", 9) in lidarr.calls and "delete_artist" not in lidarr.names()


def test_ctrl_c_mid_stage_still_leaves_the_record_and_the_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _staged_world(tmp_path)
    lidarr, holding = world[1], world[3]
    _fail_second_rename(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _report_then_stage(tmp_path, world)

    day = holding / NOW.date().isoformat()
    assert len(json.loads((day / "manifest.json").read_text())["moves"]) == 1
    assert len((day / prune_commands.JOURNAL).read_text().splitlines()) == 1
    assert ("rescan_artist", 9) in lidarr.calls and "delete_artist" not in lidarr.names()


def test_every_move_is_flushed_to_disk_before_the_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _staged_world(tmp_path)
    journal = world[3] / NOW.date().isoformat() / prune_commands.JOURNAL
    real_fsync = os.fsync
    seen: list[int] = []

    def fsync(fd: int) -> None:
        if _is_file(fd, journal):
            seen.append(len(journal.read_text().splitlines()))  # the line is written before its fsync
        real_fsync(fd)

    monkeypatch.setattr(prune_commands.os, "fsync", fsync)

    assert _report_then_stage(tmp_path, world) == EXIT_OK
    assert seen == [1, 2]


def test_the_preview_refuses_two_mounts_mountinfo_tells_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _staged_world(tmp_path)
    library, holding = world[2], world[3]
    holding.mkdir()
    real = os.path.realpath
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        f"40 1 0:50 /media/music {real(library)} rw - nfs host:/m rw\n"
        f"41 1 0:50 /media/_likearr-holding {real(holding)} rw - nfs host:/m rw\n"
    )
    monkeypatch.setattr(prune_commands, "MOUNTINFO", mountinfo)

    with pytest.raises(prune_commands.PruneStageError, match="not on the same mount"):
        _stage(tmp_path, world, do_apply=False)

    mountinfo.write_text(f"1 0 8:1 / / rw - ext4 /dev/sda1 rw\n40 1 0:50 /media {real(tmp_path)} rw - nfs h rw\n")
    assert _stage(tmp_path, world, do_apply=False) == EXIT_OK


def test_a_mount_point_with_a_space_is_read_from_mountinfo() -> None:
    info = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n7 1 0:9 / /mnt/my\\040media rw - nfs h rw\n"

    assert prune_commands._mount_of(Path("/mnt/my media/music"), info) == "7"
    assert prune_commands._mount_of(Path("/srv"), info) == "1"


def test_every_file_is_checked_not_just_the_first(tmp_path: Path) -> None:
    world = _staged_world(tmp_path)
    library = world[2]
    (library / "A Stranger" / "Something Else" / "01 - One.flac").unlink()  # the second listed

    with pytest.raises(prune_commands.PruneStageError, match="1 of 2 files Lidarr lists are not here"):
        _stage(tmp_path, world, do_apply=False)


def test_a_file_that_is_not_the_size_lidarr_lists_is_refused(tmp_path: Path) -> None:
    world = _staged_world(tmp_path, sizes=(5, 9))

    with pytest.raises(prune_commands.PruneStageError, match="1 of 2 files are not the size Lidarr lists"):
        _stage(tmp_path, world, do_apply=False)


@pytest.mark.parametrize("how", ["symlink", "dotdot"])
def test_a_holding_folder_that_leads_into_the_root_folder_is_refused(tmp_path: Path, how: str) -> None:
    world = _staged_world(tmp_path)
    library = world[2]
    if how == "symlink":
        (tmp_path / "looks-outside").symlink_to(library)
        holding = tmp_path / "looks-outside" / "_likearr-holding"
    else:
        holding = tmp_path / "elsewhere" / ".." / "library" / "_likearr-holding"
    (source, lookup), lidarr, _library, _holding, config = world

    with (
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, config=config) as ctx,
        pytest.raises(prune_commands.PruneStageError, match="inside the Lidarr root folder"),
    ):
        prune_commands.prune_report_command(ctx, out=tmp_path / "prune.json", now=NOW)
        prune_commands.prune_stage_command(
            ctx, manifest=tmp_path / "prune.json", holding=holding, all_candidates=True, now=NOW
        )


def _with_root(lidarr_config, root: str):
    from dataclasses import replace

    return replace(lidarr_config, root_folder=root)


def _owned_artist(mbid: str):
    from likearr.models import OwnedArtist, Profile

    return OwnedArtist(artist_mbid=mbid, lidarr_artist_id=None, added_by_us=False, profile=Profile.LEAN)


def test_human_bytes_reads_like_a_size() -> None:
    assert prune_commands._human_bytes(512) == "512 B"
    assert prune_commands._human_bytes(2048) == "2.0 KiB"
    assert prune_commands._human_bytes(5 * 1024**3) == "5.0 GiB"


def test_safe_directory_names() -> None:
    assert prune_commands._safe("AC/DC") == "AC_DC"
    assert prune_commands._safe("  ") == "unknown"


def test_secondary_types_round_trip_through_the_prune_report() -> None:
    row = prune_commands._row_from_dict(
        {
            "artist_mbid": "artist-1",
            "rg_mbid": "rg-1",
            "title": "x",
            "primary_type": "Album",
            "secondary_types": ["Live"],
        }
    )
    assert row.primary_type is PrimaryType.ALBUM
    assert row.secondary_types == frozenset({SecondaryType.LIVE})
