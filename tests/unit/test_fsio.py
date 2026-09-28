"""`likearr.fsio.write_atomic`: a reader sees the old file or the new one, and the new one survives a power cut."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from likearr import fsio
from likearr.fsio import write_atomic


@pytest.fixture
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _temps(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.tmp"))


def _mode(path: Path | str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_it_writes_the_text_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"

    write_atomic(path, '{"a": 1}\n')

    assert path.read_bytes() == b'{"a": 1}\n'
    assert _temps(tmp_path) == []


def test_it_replaces_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text("old\n", encoding="utf-8")

    write_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert _temps(tmp_path) == []


def test_text_is_written_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "names.json"

    write_atomic(path, "Sigur Rós, Ágætis byrjun\n")

    assert path.read_bytes() == "Sigur Rós, Ágætis byrjun\n".encode()


def test_a_failed_rename_leaves_the_old_file_and_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("old\n", encoding="utf-8")

    def refuse(src: Any, dst: Any) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(fsio.os, "replace", refuse)
    with pytest.raises(OSError, match="I/O error"):
        write_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "old\n"
    assert _temps(tmp_path) == []


def test_a_write_that_fails_part_way_leaves_the_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("old\n", encoding="utf-8")

    def disk_full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fsio.os, "fsync", disk_full)
    with pytest.raises(OSError, match="No space left"):
        write_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "old\n"
    assert _temps(tmp_path) == []


def test_an_interrupt_mid_write_also_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token.json"

    def interrupted(src: Any, dst: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fsio.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_atomic(path, "secret", mode=0o600)

    assert not path.exists()
    assert _temps(tmp_path) == []


def test_the_data_is_fsynced_before_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "manifest.json"
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        events.append("fsync dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync file")
        real_fsync(fd)

    def replace(src: Any, dst: Any) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(fsio.os, "fsync", fsync)
    monkeypatch.setattr(fsio.os, "replace", replace)
    write_atomic(path, "moves\n")

    assert events == ["fsync file", "replace", "fsync dir"]


def test_a_directory_that_refuses_fsync_still_gets_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "manifest.json"
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(fsio.os, "fsync", fsync)
    write_atomic(path, "moves\n")

    assert path.read_text(encoding="utf-8") == "moves\n"


def test_mode_0600_gives_a_0600_file_and_is_set_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_022: None
) -> None:
    path = tmp_path / "token.json"
    seen: list[int] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        seen.append(_mode(src))
        real_replace(src, dst)

    monkeypatch.setattr(fsio.os, "replace", replace)
    write_atomic(path, "secret", mode=0o600)

    assert seen == [0o600]
    assert _mode(path) == 0o600


def test_the_temp_file_never_has_a_wider_mode_than_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_022: None
) -> None:
    """The token and config writes must not be readable by others, not even while the bytes land."""
    path = tmp_path / "token.json"
    created: list[int] = []
    at_fsync: list[int] = []
    real_open, real_fsync = os.open, os.fsync

    def open_(file: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            created.append(mode)
        return real_open(file, flags, mode, *args, **kwargs)

    def fsync(fd: int) -> None:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            at_fsync.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(fsio.os, "open", open_)
    monkeypatch.setattr(fsio.os, "fsync", fsync)
    write_atomic(path, "secret", mode=0o600)

    assert created == [0o600]
    assert at_fsync == [0o600]


def test_an_explicit_mode_is_exact_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        write_atomic(tmp_path / "config.toml", "x = 1\n", mode=0o640)
    finally:
        os.umask(old)

    assert _mode(tmp_path / "config.toml") == 0o640


def test_no_mode_gives_what_a_plain_write_would(tmp_path: Path, umask_022: None) -> None:
    """The plan files were written with `Path.write_text`: the umask decides, as it did then."""
    write_atomic(tmp_path / "diff.json", "{}\n")
    (tmp_path / "plain.json").write_text("{}\n", encoding="utf-8")

    assert _mode(tmp_path / "diff.json") == _mode(tmp_path / "plain.json") == 0o644


def test_the_temp_file_sits_beside_the_target_named_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "last-run.json"
    names: list[Path] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        names.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(fsio.os, "replace", replace)
    write_atomic(path, "{}")

    [temp] = names
    assert temp.parent == tmp_path
    assert temp.name.startswith(".last-run.json.") and temp.name.endswith(".tmp")
    assert temp.match(".last-run.json.*.tmp")


def test_two_writes_never_share_a_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "out.json"
    names: list[str] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        names.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(fsio.os, "replace", replace)
    write_atomic(path, "1")
    write_atomic(path, "2")

    assert len(set(names)) == 2


def test_it_does_not_create_a_missing_directory(tmp_path: Path) -> None:
    """Callers that want the directory make it: a job folder pruned mid-job must not come back."""
    with pytest.raises(FileNotFoundError):
        write_atomic(tmp_path / "gone" / "meta.json", "{}")

    assert not (tmp_path / "gone").exists()


def test_the_call_sites_keep_the_modes_they_had(tmp_path: Path, umask_022: None) -> None:
    """Plan and diff files are 0600; the manifest still keeps the umask's mode
    (it is a record of moves, not a plan); state files were `mkstemp` (0600) already."""
    from datetime import UTC, datetime

    from likearr.playlist_names import write_names
    from likearr.prune_ledger import Ledger, write_ledger
    from likearr.shell import prune_commands
    from likearr.shell.diff_io import write_diff
    from likearr.shell.last_run import write_last_run
    from tests.adapters.test_state_sqlite import _diff

    now = datetime(2026, 9, 25, tzinfo=UTC)
    write_diff(_diff(), tmp_path / "diff.json")
    prune_commands._write_manifest(tmp_path, [], now=now)
    write_names(tmp_path / "playlist-names.json", {"pl-1": "Road trip"}, fetched_at=now)
    write_ledger(tmp_path / "ledger.json", Ledger())
    write_last_run(tmp_path / "last-run.json", {})

    assert _mode(tmp_path / "diff.json") == 0o600
    assert _mode(tmp_path / "manifest.json") == 0o644
    assert {_mode(tmp_path / n) for n in ("playlist-names.json", "ledger.json", "last-run.json")} == {0o600}
    assert _temps(tmp_path) == []


def test_adopt_and_promote_save_plan_files_are_0600(tmp_path: Path, umask_022: None) -> None:
    """`write_adopt_plan` and `write_plan` (promote-save) kept the umask's mode,
    like `write_diff` did above. `prune_report_command`'s `prune.json` is covered in
    `tests/shell/test_prune_commands.py`, which already has the fakes a real report needs."""
    from datetime import UTC, datetime

    from likearr.core.adopt import AdoptPlan
    from likearr.models import PromoteSavePlan
    from likearr.shell.adopt_io import AdoptPlanFile, write_adopt_plan
    from likearr.shell.promote_save import write_plan

    now = datetime(2026, 9, 25, tzinfo=UTC)
    write_adopt_plan(
        AdoptPlanFile(
            created_at=now,
            source_digest="d",
            lidarr_digest="d",
            resolver_version=1,
            adoption=AdoptPlan(),
        ),
        tmp_path / "adopt.json",
    )
    write_plan(
        PromoteSavePlan(
            created_at=now,
            decisions_path="decisions.json",
            decisions_digest="d",
            lidarr_digest="d",
            follow=[],
            save=[],
            already_followed=[],
            already_saved=[],
            unmatched=[],
        ),
        tmp_path / "promote-save.json",
    )

    assert _mode(tmp_path / "adopt.json") == 0o600
    assert _mode(tmp_path / "promote-save.json") == 0o600
