"""`diff_from_run_dict`: decoding `runs.diff_json` - a different shape from this module's own
`diff_to_dict`/`read_diff`, produced by `adapters.state_sqlite.record_run`'s `dataclasses.asdict`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from likearr.adapters.state_sqlite import SqliteState
from likearr.models import (
    AddArtist,
    ArtistResolution,
    Diff,
    MonitorRelease,
    Profile,
    ProfileRatchet,
    Reason,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    UnmonitorRelease,
)
from likearr.shell.diff_io import DiffFileError, diff_from_run_dict


@pytest.fixture
def state(tmp_path: Path) -> SqliteState:
    return SqliteState(tmp_path / "state.sqlite")


def _diff(**overrides: object) -> Diff:
    defaults: dict[str, object] = dict(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_digest="abc",
        lidarr_digest="def",
        add_artists=[AddArtist(artist_mbid="a1", name="David Bromberg Band", profile=Profile.LEAN)],
        monitor=[
            MonitorRelease(
                key=ReleaseKey(artist_mbid="a1", rg_mbid="rg1"),
                title="Bandit In a Bathing Suit",
                reasons=frozenset({Reason(kind=ReasonKind.LIKED, source_id="t1")}),
                step="album:upc",
            )
        ],
        unmonitor=[
            UnmonitorRelease(
                key=ReleaseKey(artist_mbid="a2", rg_mbid="rg2"),
                title="Bayside EP",
                lost_reasons=frozenset({Reason(kind=ReasonKind.FOLLOWED, source_id="a2")}),
            )
        ],
        ratchets=[ProfileRatchet(artist_mbid="a3", name="Daisy Jones & the Six", to_profile=Profile.FULL, because="")],
        set_new_items_none=[],
        guards=[],
        pending=[Resolution(intent_key="p1", status=ResolutionStatus.PENDING_ALBUM)],
        unmapped=[ArtistResolution(intent_key="u1", status=ResolutionStatus.UNMAPPED)],
        projected_wanted=5,
        update_reasons=[(ReleaseKey("a4", "rg4"), frozenset({Reason(kind=ReasonKind.SAVED, source_id="al1")}))],
    )
    defaults.update(overrides)
    return Diff(**defaults)  # type: ignore[arg-type]


def _stored_diff_dict(state: SqliteState, diff: Diff) -> dict[str, object]:
    """`diff` as `record_run` actually stores it, read back through the accessor."""
    from likearr.models import HealthRecord, RunStatus

    record = HealthRecord(
        ts=1,
        version="",
        resolver_version=diff.resolver_version,
        exit_code=0,
        status=RunStatus.OK,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={},
        unmapped=0,
        pending_album=0,
        message="",
        dry_run=False,
    )
    state.record_run(record, diff)
    (row,) = state.run_history(limit=1)
    found = state.run_by_id(row.id)
    assert found is not None
    assert found[1] is not None
    return found[1]


def test_the_named_sections_decode_with_their_rows_intact(state: SqliteState) -> None:
    raw = _stored_diff_dict(state, _diff())

    decoded = diff_from_run_dict(raw)

    assert decoded.add_artists[0].name == "David Bromberg Band"
    assert decoded.monitor[0].title == "Bandit In a Bathing Suit"
    assert decoded.monitor[0].key == ReleaseKey("a1", "rg1")
    (reason,) = decoded.monitor[0].reasons
    assert reason.kind is ReasonKind.LIKED
    assert decoded.unmonitor[0].title == "Bayside EP"
    (lost,) = decoded.unmonitor[0].lost_reasons
    assert lost.kind is ReasonKind.FOLLOWED
    assert decoded.ratchets[0].name == "Daisy Jones & the Six"
    assert decoded.projected_wanted == 5


def test_update_reasons_pending_and_unmapped_decode_empty_rather_than_crash(state: SqliteState) -> None:
    """These don't round-trip through `record_run`'s encoding, so the decoder drops them instead
    of guessing wrong - it must not raise."""
    raw = _stored_diff_dict(state, _diff())

    decoded = diff_from_run_dict(raw)

    assert decoded.update_reasons == []
    assert decoded.pending == []
    assert decoded.unmapped == []


def test_a_diff_with_no_extras_decodes_too(state: SqliteState) -> None:
    raw = _stored_diff_dict(
        state, _diff(add_artists=[], monitor=[], unmonitor=[], ratchets=[], pending=[], unmapped=[], update_reasons=[])
    )

    decoded = diff_from_run_dict(raw)

    assert decoded.add_artists == []
    assert decoded.monitor == []


def test_not_a_json_object_is_refused() -> None:
    with pytest.raises(DiffFileError):
        diff_from_run_dict([])  # type: ignore[arg-type]
