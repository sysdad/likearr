"""`shell.last_run`: what the last run saw, kept for a fast `explain`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from likearr.models import AddArtist, MonitorRelease, ReleaseKey, UnmonitorRelease
from likearr.shell.last_run import (
    FACTS_VERSION,
    last_run_facts,
    read_last_run,
    record_last_run,
    write_last_run,
)
from tests.unit.fakes import lidarr_album, lidarr_view, rg
from tests.unit.test_diff import desired_state
from tests.unit.test_explain import (
    BUSY,
    JUNGLE_COLLISION,
    JUNGLE_RESOLUTION,
    JUNGLE_SNAPSHOT,
    _jungle,
    _jungle_desired,
    _lawrence,
    _lawrence_inputs,
)

RAN_AT = datetime(2026, 9, 23, 16, 20, tzinfo=UTC)


def _round_trip(tmp_path: Path, **facts: object):
    path = tmp_path / "last-run.json"
    facts.setdefault("resolutions", {})
    facts.setdefault("artist_resolutions", {})
    write_last_run(path, last_run_facts(ran_at=RAN_AT, owned_keys=(), **facts))  # type: ignore[arg-type]
    last = read_last_run(path)
    assert last is not None
    return last


def test_the_jungle_case_reads_back_to_the_same_report(tmp_path: Path) -> None:
    original = _jungle()
    view = lidarr_view()
    last = _round_trip(
        tmp_path,
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={BUSY.key: JUNGLE_RESOLUTION},
        desired=_jungle_desired(),
        view=view,
        collisions=[JUNGLE_COLLISION],
    )

    again = _jungle(
        desired=last.desired,
        view=last.view,
        snapshot=last.snapshot,
        collisions=list(last.collisions),
        resolutions=last.resolutions,
        artist_resolutions=last.artist_resolutions,
    )

    assert again == original
    assert last.ran_at == RAN_AT
    assert last.applied is False


def test_a_followed_artist_reads_back_to_the_same_report(tmp_path: Path) -> None:
    inputs = _lawrence_inputs()
    last = _round_trip(
        tmp_path, snapshot=inputs["snapshot"], desired=inputs["desired"], view=inputs["view"], collisions=[]
    )

    assert _lawrence(desired=last.desired, view=last.view, snapshot=last.snapshot) == _lawrence()


def test_after_an_apply_the_view_carries_what_it_changed(tmp_path: Path) -> None:
    kept = rg("rg-kept", "Kept", released="2020-01-01")
    dropped = rg("rg-dropped", "Dropped", released="2019-01-01")
    refused = rg("rg-refused", "Refused", released="2018-01-01")
    desired = desired_state((kept, [BUSY]), (refused, [BUSY]))
    view = lidarr_view(albums=[lidarr_album(kept, id=1), lidarr_album(dropped, id=2, monitored=True)])
    executed = SimpleNamespace(
        add_artists=[AddArtist("artist-1", "Test Artist", profile=desired.profile_needs["artist-1"])],
        monitor=[
            MonitorRelease(ReleaseKey("artist-1", "rg-kept"), "Kept", frozenset(), "test"),
            MonitorRelease(ReleaseKey("artist-1", "rg-refused"), "Refused", frozenset(), "test"),
        ],
        unmonitor=[UnmonitorRelease(ReleaseKey("artist-1", "rg-dropped"), "Dropped", frozenset())],
    )
    applied = SimpleNamespace(
        skipped_artists=[], unknown_artists=[], foreign_artists=[], unmapped_in_lidarr=["artist-1/rg-refused"]
    )

    last = _round_trip(
        tmp_path,
        snapshot=JUNGLE_SNAPSHOT,
        desired=desired_state((kept, [BUSY]), (refused, [BUSY]), (dropped, [BUSY])),
        view=view,
        collisions=[],
        executed=executed,
        applied=applied,
    )

    assert last.applied is True
    assert last.view.album(ReleaseKey("artist-1", "rg-kept")).monitored  # type: ignore[union-attr]
    assert not last.view.album(ReleaseKey("artist-1", "rg-dropped")).monitored  # type: ignore[union-attr]
    assert last.view.album(ReleaseKey("artist-1", "rg-refused")) is None
    assert last.view.artists["artist-1"].id == 0  # added: in Lidarr now, its id unknown here


def test_only_the_albums_the_desired_and_owned_releases_touch_are_kept(tmp_path: Path) -> None:
    other = rg("rg-other", "Unrelated")
    view = lidarr_view(albums=[lidarr_album(JUNGLE_RESOLUTION.release_group, id=1), lidarr_album(other, id=2)])  # type: ignore[arg-type]
    path = tmp_path / "last-run.json"
    facts = last_run_facts(
        ran_at=RAN_AT,
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={},
        artist_resolutions={},
        desired=_jungle_desired(),
        view=view,
        owned_keys=(),
        collisions=[],
    )

    assert [a["rg_mbid"] for a in facts["view"]["albums"]] == [JUNGLE_RESOLUTION.release_group.mbid]  # type: ignore[union-attr]
    write_last_run(path, facts)
    assert read_last_run(path) is not None


def test_a_missing_unreadable_or_older_file_reads_as_none(tmp_path: Path) -> None:
    assert read_last_run(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{half")
    assert read_last_run(bad) is None
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"version": FACTS_VERSION + 1}))
    assert read_last_run(old) is None
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"version": FACTS_VERSION, "view": {}}))
    assert read_last_run(broken) is None


def test_recording_never_raises(tmp_path: Path) -> None:
    def explode() -> dict[str, object]:
        raise RuntimeError("disk full")

    record_last_run(tmp_path / "last-run.json", explode)

    assert not (tmp_path / "last-run.json").exists()


def test_unmatched_and_artist_resolutions_are_kept_too(tmp_path: Path) -> None:
    from likearr.models import ArtistResolution, Resolution, ResolutionStatus

    unmapped = Resolution(intent_key="liked:x", status=ResolutionStatus.UNMAPPED, step="track:none", detail="no match")
    artist = ArtistResolution(
        intent_key="followed:y", status=ResolutionStatus.RESOLVED, artist_mbid="artist-1", artist_name="A", step="s"
    )

    last = _round_trip(
        tmp_path,
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={"liked:x": unmapped},
        artist_resolutions={"followed:y": artist},
        desired=_jungle_desired(),
        view=lidarr_view(),
        collisions=[],
    )

    assert last.resolutions == {"liked:x": unmapped}
    assert last.artist_resolutions == {"followed:y": artist}


def test_the_plans_unmonitors_guards_and_kind_are_kept(tmp_path: Path) -> None:
    from likearr.models import Guard, ReleaseKey

    guard = Guard("scheduled-cap", "over the cap", blocked_unmonitors=12, subject="")
    key = ReleaseKey("artist-1", "rg-1")
    last = _round_trip(
        tmp_path,
        snapshot=JUNGLE_SNAPSHOT,
        desired=_jungle_desired(),
        view=lidarr_view(),
        collisions=[],
        unmonitor=[key],
        guards=[guard],
        refused=True,
    )

    assert last.unmonitor == frozenset({key})
    assert last.guards == (guard,)
    assert last.kind == "refused apply" and not last.applied
    assert "refused" in last.label


def test_a_file_from_before_unmonitors_were_kept_leaves_them_unknown(tmp_path: Path) -> None:
    path = tmp_path / "last-run.json"
    facts = last_run_facts(
        ran_at=RAN_AT,
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={},
        artist_resolutions={},
        desired=_jungle_desired(),
        view=lidarr_view(),
        owned_keys=(),
        collisions=[],
    )
    for key in ("unmonitor", "guards", "kind"):
        del facts[key]
    facts["applied"] = True
    write_last_run(path, facts)

    last = read_last_run(path)

    assert last is not None
    assert last.unmonitor is None and last.guards == ()
    assert last.kind == "apply"


def test_writing_clears_temp_files_a_dead_run_left_behind(tmp_path: Path) -> None:
    import os

    old = tmp_path / ".last-run.json.abc123.tmp"
    old.write_text("half")
    os.utime(old, (1, 1))
    fresh = tmp_path / ".last-run.json.def456.tmp"
    fresh.write_text("a writer at work")

    _round_trip(tmp_path, snapshot=JUNGLE_SNAPSHOT, desired=_jungle_desired(), view=lidarr_view(), collisions=[])

    assert not old.exists()
    assert fresh.exists()


def test_a_temp_file_write_atomic_left_behind_is_swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`likearr.fsio.write_atomic` names its temp file so `remove_stale_temps` still finds it (#158)."""
    import os

    from likearr import fsio
    from likearr.shell.last_run import remove_stale_temps

    path = tmp_path / "last-run.json"
    left: list[str] = []
    monkeypatch.setattr(fsio.os, "replace", lambda src, dst: left.append(src))  # the process died here
    fsio.write_atomic(path, "{}")
    monkeypatch.undo()
    [temp] = left
    assert Path(temp).exists() and not path.exists()
    os.utime(temp, (1, 1))

    remove_stale_temps(path)

    assert not Path(temp).exists()


def test_the_plans_monitors_are_kept_too(tmp_path: Path) -> None:
    from likearr.models import ReleaseKey

    key = ReleaseKey("artist-1", "rg-9")
    last = _round_trip(
        tmp_path, snapshot=JUNGLE_SNAPSHOT, desired=_jungle_desired(), view=lidarr_view(), collisions=[], monitor=[key]
    )

    assert last.monitor == frozenset({key})


def test_a_snapshots_schema_ok_and_warnings_round_trip(tmp_path: Path) -> None:
    """Issue #68 phase 3: `spotify_snapshot` reuses this shape and needs it lossless."""
    from dataclasses import replace

    dirty = replace(JUNGLE_SNAPSHOT, schema_ok=False, schema_warnings=("liked tracks: missing 'isrc'",))
    last = _round_trip(tmp_path, snapshot=dirty, desired=_jungle_desired(), view=lidarr_view(), collisions=[])

    assert last.snapshot.schema_ok is False
    assert last.snapshot.schema_warnings == ("liked tracks: missing 'isrc'",)


def test_a_file_from_before_schema_ok_was_recorded_defaults_true(tmp_path: Path) -> None:
    from likearr.shell.last_run import snapshot_to_dict

    path = tmp_path / "last-run.json"
    facts = last_run_facts(
        ran_at=RAN_AT,
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={},
        artist_resolutions={},
        desired=_jungle_desired(),
        view=lidarr_view(),
        owned_keys=(),
        collisions=[],
    )
    raw = dict(facts)
    stripped = dict(snapshot_to_dict(JUNGLE_SNAPSHOT))
    del stripped["schema_ok"]
    del stripped["schema_warnings"]
    raw["snapshot"] = stripped
    write_last_run(path, raw)

    last = read_last_run(path)

    assert last is not None
    assert last.snapshot.schema_ok is True
    assert last.snapshot.schema_warnings == ()
