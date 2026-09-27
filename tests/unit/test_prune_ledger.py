"""The Clean up ledger (#55): what earlier reviews decided."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from likearr.prune_ledger import (
    Entry,
    Ledger,
    LedgerBusy,
    LedgerUnreadable,
    ledger_lock,
    ledger_path,
    read_ledger,
    record,
    write_ledger,
)

A1 = "11111111-1111-1111-1111-111111111111"
A2 = "22222222-2222-2222-2222-222222222222"
A3 = "33333333-3333-3333-3333-333333333333"
RG = [f"{i:08d}-aaaa-bbbb-cccc-dddddddddddd" for i in range(8)]


def test_the_ledger_lives_beside_the_job_store(tmp_path: Path) -> None:
    assert ledger_path(tmp_path / "config.toml") == tmp_path / "ui" / "prune-ledger.json"


def test_a_missing_ledger_is_empty_and_a_broken_one_says_why(tmp_path: Path) -> None:
    assert read_ledger(tmp_path / "none.json") == Ledger()  # nothing decided yet: no problem
    for name, body in [
        ("bad.json", "{"),
        ("list.json", "[]"),
        ("future.json", json.dumps({"version": 2, "releases": {}})),
        ("shape.json", json.dumps({"releases": []})),
    ]:
        (tmp_path / name).write_text(body)
        ledger = read_ledger(tmp_path / name)
        assert ledger.problem, name
        assert (ledger.releases, ledger.artists) == ({}, {})


def test_a_ledger_that_will_not_read_is_never_written_over(tmp_path: Path) -> None:
    path = tmp_path / "prune-ledger.json"
    path.write_text('{"releases": {"half a file')
    ledger = read_ledger(path)

    with pytest.raises(LedgerUnreadable, match="will not read"):
        write_ledger(path, record(ledger, {RG[0]: "keep"}, {}, on="2026-10-01", source="Clean up j1"))
    assert path.read_text() == '{"releases": {"half a file'


def test_the_ledger_lock_waits_a_bounded_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import likearr.prune_ledger as pl

    monkeypatch.setattr(pl, "LOCK_TRIES", 3)
    monkeypatch.setattr(pl, "LOCK_PAUSE_S", 0.01)
    path = tmp_path / "prune-ledger.json"

    with ledger_lock(path), pytest.raises(LedgerBusy), ledger_lock(path):
        pass  # a second holder - another process, as far as flock is concerned - gives up
    with ledger_lock(path):
        pass  # and the lock is free again afterwards


def test_only_well_formed_entries_survive_a_read(tmp_path: Path) -> None:
    good = {"decision": "keep", "on": "2026-01-15", "from": "review"}
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "releases": {
                    RG[0]: good,
                    "../../etc/passwd": good,  # not an MBID
                    RG[1]: {**good, "decision": "burn"},
                    RG[2]: {**good, "on": "<script>"},
                    RG[5]: {**good, "on": "٢٠٢٦-٠٩-١٩"},  # Arabic-Indic digits: a \d match, not a day
                    RG[3]: "keep",
                    RG[4]: {**good, "from": "x" * 5000},
                },
                "artists": {A1: {**good, "decision": "promote"}, A2: {**good, "decision": "trash"}},
                "imports": ["abc", 5],
            }
        )
    )

    ledger = read_ledger(path)

    assert set(ledger.releases) == {RG[0], RG[4]}
    assert len(ledger.releases[RG[4]].source) == 200
    assert set(ledger.artists) == {A1}  # an artist records only promote / save
    assert ledger.imports == ["abc"]


def test_the_ledger_round_trips(tmp_path: Path) -> None:
    ledger = Ledger(
        releases={RG[0]: Entry("keep", "2026-01-15", "review")},
        artists={A1: Entry("save", "2026-01-15", "review")},
        imports=["d1"],
    )
    path = tmp_path / "ui" / "prune-ledger.json"  # the directory is made

    write_ledger(path, ledger)

    assert read_ledger(path) == ledger


def test_recording_the_same_decision_again_keeps_its_first_date() -> None:
    ledger = Ledger(releases={RG[0]: Entry("keep", "2026-01-15", "review"), RG[1]: Entry("keep", "2026-01-15", "r")})

    after = record(ledger, {RG[0]: "keep", RG[1]: "trash", RG[2]: "save"}, {}, on="2026-10-01", source="Clean up j1")

    assert after.releases[RG[0]] == Entry("keep", "2026-01-15", "review")
    assert after.releases[RG[1]] == Entry("trash", "2026-10-01", "Clean up j1")
    assert after.releases[RG[2]] == Entry("save", "2026-10-01", "Clean up j1")


def test_an_artist_decided_otherwise_is_forgotten() -> None:
    ledger = Ledger(artists={A1: Entry("promote", "2026-01-15", "review"), A2: Entry("save", "2026-01-15", "r")})

    after = record(ledger, {}, {A1: "", A3: "save"}, on="2026-10-01", source="Clean up j1")

    assert set(after.artists) == {A2, A3}


def test_a_ledger_with_an_imports_key_still_loads_and_keeps_it_on_the_next_export(tmp_path: Path) -> None:
    """An older likearr's one-time import left an ``imports`` list in the file (the live ledger has
    one). It still reads, its decisions carry forward, and the next export leaves the list alone."""
    path = tmp_path / "ui" / "prune-ledger.json"
    path.parent.mkdir()
    kept = {"decision": "keep", "on": "2026-01-15", "from": "review of 2026-01-15 (decisions.json)"}
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "releases": {RG[0]: kept, RG[1]: {**kept, "decision": "trash"}},
                "artists": {A1: {**kept, "decision": "promote"}},
                "imports": ["0" * 64],
            }
        )
    )

    ledger = read_ledger(path)
    assert not ledger.problem
    write_ledger(path, record(ledger, {RG[2]: "keep"}, {}, on="2026-10-01", source="Clean up j1"))

    after = read_ledger(path)
    assert after.releases[RG[0]] == Entry("keep", "2026-01-15", "review of 2026-01-15 (decisions.json)")
    assert after.releases[RG[1]].decision == "trash"
    assert after.releases[RG[2]] == Entry("keep", "2026-10-01", "Clean up j1")
    assert set(after.artists) == {A1}
    assert json.loads(path.read_text())["imports"] == ["0" * 64]


def test_a_new_ledger_has_no_imports_key(tmp_path: Path) -> None:
    path = tmp_path / "prune-ledger.json"

    write_ledger(path, record(Ledger(), {RG[0]: "keep"}, {}, on="2026-10-01", source="Clean up j1"))

    assert set(json.loads(path.read_text())) == {"version", "releases", "artists"}
