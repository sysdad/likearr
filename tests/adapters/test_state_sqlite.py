from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.state_sqlite import (
    SCHEMA_VERSION,
    SqliteState,
    resolution_from_json,
    resolution_to_json,
)
from likearr.core.health import Fingerprint, HealthBaseline
from likearr.models import (
    AddArtist,
    Diff,
    Guard,
    HealthRecord,
    MonitorRelease,
    NameCollision,
    OwnedArtist,
    OwnedRelease,
    PrimaryType,
    Profile,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    RunStatus,
    SecondaryType,
    UnmonitorRelease,
)


@pytest.fixture
def state(tmp_path: Path) -> SqliteState:
    return SqliteState(tmp_path / "state.sqlite")


def _reason(kind: ReasonKind = ReasonKind.FOLLOWED, source_id: str = "spotify-artist-1") -> Reason:
    return Reason(kind=kind, source_id=source_id)


def _release_group(**overrides: object) -> ReleaseGroup:
    defaults: dict[str, object] = dict(
        mbid="rg-mbid-1",
        title="Some Album",
        artist_mbid="artist-mbid-1",
        artist_name="Some Artist",
        primary_type=PrimaryType.ALBUM,
        secondary_types=frozenset({SecondaryType.LIVE, SecondaryType.COMPILATION}),
        first_release_date=date(2020, 6, 15),
    )
    defaults.update(overrides)
    return ReleaseGroup(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- owned_releases


def test_owned_releases_round_trip(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    reasons = frozenset({_reason(), _reason(ReasonKind.SAVED, "spotify-album-1")})
    owned = OwnedRelease(
        key=key,
        reasons=reasons,
        step="album:upc",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
        lidarr_album_id=42,
    )

    state.record_monitored([owned])

    result = state.owned_releases()
    assert result == {key: owned}


def test_record_monitored_upserts(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    first = OwnedRelease(
        key=key,
        reasons=frozenset({_reason()}),
        step="album:upc",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
        lidarr_album_id=1,
    )
    second = OwnedRelease(
        key=key,
        reasons=frozenset({_reason(ReasonKind.MANUAL, "manual")}),
        step="album:manual",
        resolver_version=2,
        monitored_at=datetime(2026, 2, 1, tzinfo=UTC),
        lidarr_album_id=2,
    )

    state.record_monitored([first])
    state.record_monitored([second])

    result = state.owned_releases()
    assert len(result) == 1
    assert result[key] == second


def test_record_unmonitored_deletes(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    owned = OwnedRelease(
        key=key,
        reasons=frozenset({_reason()}),
        step="album:upc",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    state.record_monitored([owned])
    assert key in state.owned_releases()

    state.record_unmonitored([key])

    assert state.owned_releases() == {}


def test_update_reasons_replaces(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    owned = OwnedRelease(
        key=key,
        reasons=frozenset({_reason()}),
        step="album:upc",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    state.record_monitored([owned])

    new_reasons = frozenset({_reason(ReasonKind.PLAYLIST, "track-1"), _reason(ReasonKind.SAVED, "album-2")})
    state.update_reasons(key, new_reasons)

    assert state.owned_releases()[key].reasons == new_reasons


def test_reasons_ordering_is_stable(state: SqliteState) -> None:
    """The same reason set serialises identically regardless of insertion/iteration order."""
    r1 = _reason(ReasonKind.FOLLOWED, "z-last")
    r2 = _reason(ReasonKind.SAVED, "a-first")
    r3 = _reason(ReasonKind.LIKED, "m-middle")
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")

    owned_a = OwnedRelease(
        key=key,
        reasons=frozenset({r1, r2, r3}),
        step="s",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    owned_b = OwnedRelease(
        key=key,
        reasons=frozenset({r3, r1, r2}),
        step="s",
        resolver_version=1,
        monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    state.record_monitored([owned_a])
    conn = sqlite3.connect(state._path)
    row_a = conn.execute("SELECT reasons_json FROM owned_releases WHERE rg_mbid = 'rg1'").fetchone()
    conn.close()

    state.record_monitored([owned_b])
    conn = sqlite3.connect(state._path)
    row_b = conn.execute("SELECT reasons_json FROM owned_releases WHERE rg_mbid = 'rg1'").fetchone()
    conn.close()

    assert row_a[0] == row_b[0]
    parsed = json.loads(row_a[0])
    # Sorted by Reason.key ("kind:source_id"), not by source_id alone: followed < liked < saved.
    assert [item["source_id"] for item in parsed] == ["z-last", "m-middle", "a-first"]


# ---------------------------------------------------------------- owned_artists


def test_owned_artists_round_trip(state: SqliteState) -> None:
    artist = OwnedArtist(
        artist_mbid="a1",
        lidarr_artist_id=7,
        added_by_us=True,
        profile=Profile.LEAN,
        ratcheted_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    state.record_artist(artist)

    assert state.owned_artists() == {"a1": artist}


def test_owned_artists_round_trip_no_ratchet(state: SqliteState) -> None:
    artist = OwnedArtist(artist_mbid="a2", lidarr_artist_id=None, added_by_us=False, profile=Profile.FULL)

    state.record_artist(artist)

    assert state.owned_artists() == {"a2": artist}


def test_record_artist_upserts(state: SqliteState) -> None:
    artist = OwnedArtist(artist_mbid="a1", lidarr_artist_id=1, added_by_us=True, profile=Profile.LEAN)
    state.record_artist(artist)

    updated = OwnedArtist(
        artist_mbid="a1",
        lidarr_artist_id=1,
        added_by_us=True,
        profile=Profile.FULL,
        ratcheted_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    state.record_artist(updated)

    assert state.owned_artists() == {"a1": updated}


# ---------------------------------------------------------------- resolutions


def test_resolution_json_round_trip_with_release_group() -> None:
    rg = _release_group(
        secondary_types=frozenset({SecondaryType.LIVE, SecondaryType.REMIX}),
        first_release_date=date(2019, 12, 25),
    )
    single_rg = _release_group(mbid="single-mbid", primary_type=PrimaryType.SINGLE, secondary_types=frozenset())
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.PENDING_ALBUM,
        release_group=rg,
        step="track:isrc->album",
        detail="matched via isrc",
        single_release_date=date(2026, 1, 1),
        single_release_group=single_rg,
        resolver_version=3,
    )

    raw = resolution_to_json(resolution)
    restored = resolution_from_json(raw)

    assert restored == resolution


def test_resolution_json_round_trip_with_source_release_group() -> None:
    """`Resolution.source_release_group` is an additive model field; make sure it round-trips."""
    source_rg = _release_group(mbid="source-mbid", primary_type=PrimaryType.SINGLE, secondary_types=frozenset())
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.RESOLVED,
        release_group=_release_group(),
        source_release_group=source_rg,
    )

    raw = resolution_to_json(resolution)
    restored = resolution_from_json(raw)

    assert restored == resolution
    assert restored.source_release_group == source_rg


def test_resolution_json_round_trip_minimal() -> None:
    resolution = Resolution(intent_key="followed:artist-1", status=ResolutionStatus.UNMAPPED)

    raw = resolution_to_json(resolution)
    restored = resolution_from_json(raw)

    assert restored == resolution


def test_resolution_json_round_trip_keeps_the_rules_token() -> None:
    """Without the token a `c1r0` row read back as `""` and was never reused."""
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.RESOLVED,
        release_group=_release_group(),
        rules="c1r0",
    )

    restored = resolution_from_json(resolution_to_json(resolution))

    assert restored == resolution
    assert restored.rules == "c1r0"


def test_resolution_json_round_trips_every_model_field() -> None:
    """The codec lists fields by hand, so a new `Resolution` field is dropped silently, on write or
    on read, and the in-memory fakes never notice. Every field is set off its
    default here, so a field added to the model fails this test until the codec carries it."""
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.PENDING_ALBUM,
        release_group=_release_group(),
        step="track:smallest:single",
        detail="matched via isrc",
        single_release_date=date(2026, 1, 1),
        single_release_group=_release_group(mbid="single-mbid"),
        resolver_version=3,
        source_release_group=_release_group(mbid="source-mbid"),
        scope="smallest",
        followed=True,
        rules="c1r0",
        denied_skipped=frozenset({"denied-b", "denied-a"}),
        checked_at=datetime(2026, 9, 18, 12, 30, tzinfo=UTC),
    )
    defaults = Resolution(intent_key="", status=ResolutionStatus.RESOLVED)

    left_at_default = [
        f.name for f in dataclasses.fields(Resolution) if getattr(resolution, f.name) == getattr(defaults, f.name)
    ]
    assert left_at_default == []
    assert resolution_from_json(resolution_to_json(resolution)) == resolution


def test_resolution_from_json_reads_a_row_without_a_token_as_the_defaults() -> None:
    """Older rows carry no key; `""` makes them re-resolve once under a live token."""
    d = json.loads(resolution_to_json(Resolution(intent_key="k", status=ResolutionStatus.RESOLVED, rules="c1r0")))
    del d["rules"]

    assert resolution_from_json(json.dumps(d)).rules == ""


def test_resolution_json_keeps_the_denied_releases_skipped_as_a_sorted_list() -> None:
    """What the answer fell through from is what lets an un-deny re-resolve it."""
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.RESOLVED,
        release_group=_release_group(),
        denied_skipped=frozenset({"rg-b", "rg-a"}),
    )
    raw = resolution_to_json(resolution)

    assert json.loads(raw)["denied_skipped"] == ["rg-a", "rg-b"]
    assert raw == resolution_to_json(resolution_from_json(raw)), "stable, so a rewrite is byte-identical"
    assert resolution_from_json(raw).denied_skipped == frozenset({"rg-a", "rg-b"})


def test_resolution_from_json_reads_a_row_without_denied_releases_as_empty() -> None:
    d = json.loads(resolution_to_json(Resolution(intent_key="k", status=ResolutionStatus.RESOLVED)))
    del d["denied_skipped"]

    assert resolution_from_json(json.dumps(d)).denied_skipped == frozenset()
    d["denied_skipped"] = None
    assert resolution_from_json(json.dumps(d)).denied_skipped == frozenset()


def test_resolution_json_keeps_when_the_answer_was_checked() -> None:
    """The age a cached answer expires by. It is on the answer, not the row, because
    the row's `resolved_at` column is rewritten on every run, reused answers included."""
    checked = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)
    resolution = Resolution(intent_key="k", status=ResolutionStatus.RESOLVED, checked_at=checked)

    assert json.loads(resolution_to_json(resolution))["checked_at"] == "2026-09-18T12:30:00+00:00"
    assert resolution_from_json(resolution_to_json(resolution)).checked_at == checked


def test_resolution_from_json_reads_a_row_without_a_check_time_as_unknown() -> None:
    d = json.loads(resolution_to_json(Resolution(intent_key="k", status=ResolutionStatus.RESOLVED)))
    del d["checked_at"]

    assert resolution_from_json(json.dumps(d)).checked_at is None


def test_resolution_from_json_tolerates_extra_keys() -> None:
    resolution = Resolution(intent_key="k", status=ResolutionStatus.RESOLVED)
    raw = resolution_to_json(resolution)
    d = json.loads(raw)
    d["some_future_field"] = "unused"
    d["release_group"] = None

    restored = resolution_from_json(json.dumps(d))

    assert restored.intent_key == "k"
    assert restored.status == ResolutionStatus.RESOLVED


def test_cached_resolution_round_trip(state: SqliteState) -> None:
    resolution = Resolution(
        intent_key="liked:track-1",
        status=ResolutionStatus.RESOLVED,
        release_group=_release_group(),
        step="album:upc",
        resolver_version=1,
    )

    state.cache_resolution(resolution)

    assert state.cached_resolution("liked:track-1", 1) == resolution


def test_cached_resolution_misses_on_version_bump(state: SqliteState) -> None:
    resolution = Resolution(intent_key="liked:track-1", status=ResolutionStatus.UNMAPPED, resolver_version=1)
    state.cache_resolution(resolution)

    assert state.cached_resolution("liked:track-1", 2) is None


def test_cached_resolution_missing_key_returns_none(state: SqliteState) -> None:
    assert state.cached_resolution("nope", 1) is None


def test_cache_resolution_upserts(state: SqliteState) -> None:
    first = Resolution(intent_key="k", status=ResolutionStatus.UNMAPPED, resolver_version=1)
    state.cache_resolution(first)

    second = Resolution(
        intent_key="k", status=ResolutionStatus.RESOLVED, release_group=_release_group(), resolver_version=1
    )
    state.cache_resolution(second)

    assert state.cached_resolution("k", 1) == second


# ---------------------------------------------------------------- pending


def test_pending_round_trip(state: SqliteState) -> None:
    since = datetime(2026, 5, 1, tzinfo=UTC)
    assert state.pending_since("liked:track-1") is None

    state.mark_pending("liked:track-1", since)
    assert state.pending_since("liked:track-1") == since

    state.clear_pending("liked:track-1")
    assert state.pending_since("liked:track-1") is None


def test_mark_pending_upserts(state: SqliteState) -> None:
    state.mark_pending("k", datetime(2026, 1, 1, tzinfo=UTC))
    state.mark_pending("k", datetime(2026, 2, 1, tzinfo=UTC))

    assert state.pending_since("k") == datetime(2026, 2, 1, tzinfo=UTC)


# ---------------------------------------------------------------- source / followed counts


def test_source_counts_round_trip(state: SqliteState) -> None:
    assert state.last_source_counts() == {}

    counts = {"followed_artists": 10, "saved_albums": 5, "playlist:abc": 3}
    state.record_source_counts(counts)

    assert state.last_source_counts() == counts


def test_source_counts_are_replaced_not_merged(state: SqliteState) -> None:
    state.record_source_counts({"followed_artists": 10, "saved_albums": 5})
    state.record_source_counts({"followed_artists": 12})

    assert state.last_source_counts() == {"followed_artists": 12}


def test_followed_counts_round_trip(state: SqliteState) -> None:
    counts = {"artist-1": 4, "artist-2": 0}
    state.record_followed_counts(counts)

    assert state.last_followed_counts() == counts


# ---------------------------------------------------------------- runs / health history


def _health_record(**overrides: object) -> HealthRecord:
    defaults: dict[str, object] = dict(
        ts=1234567890,
        version="0.1.0",
        resolver_version=1,
        exit_code=0,
        status=RunStatus.OK,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={"followed_artists": 3},
        unmapped=0,
        pending_album=0,
        message="",
        dry_run=True,
    )
    defaults.update(overrides)
    return HealthRecord(**defaults)  # type: ignore[arg-type]


def _diff() -> Diff:
    reason = _reason()
    return Diff(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_digest="abc",
        lidarr_digest="def",
        add_artists=[AddArtist(artist_mbid="a1", name="Artist", profile=Profile.LEAN)],
        monitor=[
            MonitorRelease(
                key=ReleaseKey(artist_mbid="a1", rg_mbid="rg1"),
                title="Some Album",
                reasons=frozenset({reason}),
                step="album:upc",
            )
        ],
        unmonitor=[
            UnmonitorRelease(
                key=ReleaseKey(artist_mbid="a1", rg_mbid="rg2"),
                title="Other Album",
                lost_reasons=frozenset({reason}),
            )
        ],
        ratchets=[],
        set_new_items_none=["a1"],
        guards=[Guard(code="shrink", message="source shrank", blocked_unmonitors=2)],
        pending=[Resolution(intent_key="p1", status=ResolutionStatus.PENDING_ALBUM)],
        unmapped=[],
        projected_wanted=10,
    )


def test_record_run_and_last_run(state: SqliteState) -> None:
    assert state.last_run() is None

    record = _health_record()
    state.record_run(record, _diff())

    assert state.last_run() == record


def test_record_run_without_diff(state: SqliteState) -> None:
    record = _health_record(status=RunStatus.ERROR, exit_code=1)
    state.record_run(record, None)

    assert state.last_run() == record


def test_runs_returns_newest_first(state: SqliteState) -> None:
    for i in range(3):
        state.record_run(_health_record(ts=i), None)

    result = state.runs(limit=10)

    assert [r.ts for r in result] == [2, 1, 0]


def test_run_history_carries_each_runs_guards_and_projected_wanted(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1), _diff())
    state.record_run(_health_record(ts=2, status=RunStatus.ERROR, exit_code=1), None)

    newest, oldest = state.run_history(limit=10)

    assert newest.record.ts == 2
    assert newest.guards == ()
    assert newest.projected_wanted is None
    assert oldest.record.ts == 1
    assert oldest.guards == ("source shrank",)
    assert oldest.projected_wanted == 10


def test_run_history_carries_the_name_collisions(state: SqliteState) -> None:
    diff = _diff()
    diff.name_collisions.append(
        NameCollision(
            name="Jungle",
            wanted_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095",
            existing_mbid="6bbb3983-ce8a-4971-96e0-7cae73268fc4",
            existing_lidarr_id=9003,
            existing_name="Jungle",
            wanted_disambiguation="US psychedelic rock",
            existing_disambiguation="London modern soul collective",
            dropped_releases=3,
        )
    )
    state.record_run(_health_record(ts=1), diff)
    state.record_run(_health_record(ts=2), None)

    newest, oldest = state.run_history(limit=10)

    assert newest.name_collisions == ()
    (collision,) = oldest.name_collisions
    assert collision.existing_lidarr_id == 9003
    assert collision.wanted_disambiguation == "US psychedelic rock"
    assert collision.dropped_releases == 3


def test_a_malformed_stored_collision_is_skipped_not_a_crash(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1), _diff())
    state._conn.execute(
        """UPDATE runs SET diff_json = json_set(diff_json, '$.name_collisions', json(?))""",
        ('[{"name": "Bad", "existing_lidarr_id": "not a number"}, {"name": "Good", "dropped_releases": 2}]',),
    )

    (row,) = state.run_history(limit=1)

    assert [c.name for c in row.name_collisions] == ["Good"]


def test_run_history_carries_the_guard_codes(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1), _diff())

    (row,) = state.run_history(limit=1)

    assert row.guard_codes == ("shrink",)


def test_artist_names_come_from_the_cached_resolutions(state: SqliteState) -> None:
    rg = ReleaseGroup(
        mbid="rg-9", title="An Album", artist_mbid="artist-9", artist_name="The Band", primary_type=PrimaryType.ALBUM
    )
    state.cache_resolution(Resolution(intent_key="liked:t9", status=ResolutionStatus.RESOLVED, release_group=rg))
    state.cache_resolution(Resolution(intent_key="liked:t10", status=ResolutionStatus.UNMAPPED))

    assert state.artist_names() == {"artist-9": "The Band"}


def test_run_history_honours_the_limit(state: SqliteState) -> None:
    for i in range(5):
        state.record_run(_health_record(ts=i), None)

    assert [row.record.ts for row in state.run_history(limit=2)] == [4, 3]


def test_record_run_keeps_only_last_200(state: SqliteState) -> None:
    for i in range(210):
        state.record_run(_health_record(ts=i), None)

    all_runs = state.runs(limit=1000)

    assert len(all_runs) == 200
    assert all_runs[0].ts == 209
    assert all_runs[-1].ts == 10


# ---------------------------------------------------------------- transactions


def test_transaction_commits(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    with state.transaction():
        state.record_artist(OwnedArtist(artist_mbid="a1", lidarr_artist_id=1, added_by_us=True, profile=Profile.LEAN))
        state.record_monitored(
            [
                OwnedRelease(
                    key=key,
                    reasons=frozenset({_reason()}),
                    step="s",
                    resolver_version=1,
                    monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
        )

    assert "a1" in state.owned_artists()
    assert key in state.owned_releases()


def test_transaction_rolls_back_on_exception(state: SqliteState) -> None:
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")

    class Boom(Exception):
        pass

    with pytest.raises(Boom), state.transaction():
        state.record_monitored(
            [
                OwnedRelease(
                    key=key,
                    reasons=frozenset({_reason()}),
                    step="s",
                    resolver_version=1,
                    monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
        )
        raise Boom("boom")

    assert state.owned_releases() == {}


def test_nested_transaction_reuses_outer(state: SqliteState) -> None:
    key1 = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    key2 = ReleaseKey(artist_mbid="a1", rg_mbid="rg2")

    def owned(key: ReleaseKey) -> OwnedRelease:
        return OwnedRelease(
            key=key,
            reasons=frozenset({_reason()}),
            step="s",
            resolver_version=1,
            monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    with state.transaction():
        state.record_monitored([owned(key1)])
        with state.transaction():
            state.record_monitored([owned(key2)])

    result = state.owned_releases()
    assert set(result) == {key1, key2}


def test_nested_transaction_rolls_back_fully_on_outer_exception(state: SqliteState) -> None:
    key1 = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")
    key2 = ReleaseKey(artist_mbid="a1", rg_mbid="rg2")

    def owned(key: ReleaseKey) -> OwnedRelease:
        return OwnedRelease(
            key=key,
            reasons=frozenset({_reason()}),
            step="s",
            resolver_version=1,
            monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    class Boom(Exception):
        pass

    with pytest.raises(Boom), state.transaction():
        state.record_monitored([owned(key1)])
        with state.transaction():
            state.record_monitored([owned(key2)])
        raise Boom("boom")

    assert state.owned_releases() == {}


# ---------------------------------------------------------------- idempotent re-open


def test_reopen_is_idempotent_and_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    key = ReleaseKey(artist_mbid="a1", rg_mbid="rg1")

    first = SqliteState(db_path)
    first.record_monitored(
        [
            OwnedRelease(
                key=key,
                reasons=frozenset({_reason()}),
                step="s",
                resolver_version=1,
                monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    first.close()

    second = SqliteState(db_path)
    try:
        assert key in second.owned_releases()

        conn = sqlite3.connect(db_path)
        version_rows = conn.execute("SELECT version FROM schema_version").fetchall()
        conn.close()
        assert version_rows == [(SCHEMA_VERSION,)], "one row, stamped forward, never appended to"
    finally:
        second.close()


# ---------------------------------------------------------------- health baseline


def _fingerprint(**overrides: object) -> Fingerprint:
    defaults: dict[str, object] = dict(resolver_version=4, liked_track_scope="album", source_set=("liked_tracks",))
    defaults.update(overrides)
    return Fingerprint(**defaults)  # type: ignore[arg-type]


def test_no_baseline_yet_reads_as_none(state: SqliteState) -> None:
    assert state.health_baseline() is None


def test_a_baseline_round_trips_including_its_empty_dimensions(state: SqliteState) -> None:
    """An empty dimension is a statement - "no collisions last run" - not an absence."""
    baseline = HealthBaseline(
        fingerprint=_fingerprint(),
        identities={
            "unmapped": frozenset({"i-1|rg-1", "i-2|rg-2"}),
            "name_collisions": frozenset(),
            "intents": frozenset({"i-1", "i-2", "i-3"}),
        },
    )
    state.record_health_baseline(baseline)

    loaded = state.health_baseline()

    assert loaded is not None
    assert loaded.fingerprint == baseline.fingerprint
    assert loaded.of("unmapped") == {"i-1|rg-1", "i-2|rg-2"}
    assert loaded.of("name_collisions") == frozenset()
    assert loaded.of("intents") == {"i-1", "i-2", "i-3"}


def test_rewriting_a_baseline_drops_the_identities_it_no_longer_holds(state: SqliteState) -> None:
    state.record_health_baseline(
        HealthBaseline(fingerprint=_fingerprint(), identities={"unmapped": frozenset({"gone", "kept"})})
    )
    state.record_health_baseline(
        HealthBaseline(fingerprint=_fingerprint(), identities={"unmapped": frozenset({"kept"})})
    )

    loaded = state.health_baseline()

    assert loaded is not None
    assert loaded.of("unmapped") == {"kept"}


def test_a_changed_fingerprint_is_stored_not_merged(state: SqliteState) -> None:
    state.record_health_baseline(HealthBaseline(fingerprint=_fingerprint(), identities={}))
    state.record_health_baseline(HealthBaseline(fingerprint=_fingerprint(resolver_version=5), identities={}))

    loaded = state.health_baseline()

    assert loaded is not None
    assert loaded.fingerprint.resolver_version == 5


# ---------------------------------------------------------------- schema upgrade


_V1_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE owned_releases (
    artist_mbid TEXT NOT NULL, rg_mbid TEXT NOT NULL, lidarr_album_id INTEGER,
    reasons_json TEXT NOT NULL, step TEXT NOT NULL, resolver_version INTEGER NOT NULL,
    monitored_at TEXT NOT NULL, PRIMARY KEY (artist_mbid, rg_mbid)
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, status TEXT NOT NULL,
    exit_code INTEGER NOT NULL, record_json TEXT NOT NULL, diff_json TEXT
);
"""

_V1_RECORD = {
    "ts": 1,
    "version": "0.1.0",
    "resolver_version": 3,
    "exit_code": 0,
    "status": "degraded",
    "spotify_ok": True,
    "spotify_schema_ok": True,
    "mb_ok": True,
    "lidarr_ok": True,
    "lidarr_metadata_ok": False,
    "counts": {"followed_artists": 2},
    "unmapped": 240,
    "pending_album": 0,
    "message": "",
    "dry_run": False,
}


def test_a_v1_database_upgrades_in_place_and_keeps_its_rows(tmp_path: Path) -> None:
    """A long-lived install's state DB can be a v1 file with many runs and every owned release in it."""
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute(
        "INSERT INTO owned_releases VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "a1",
            "rg1",
            7,
            json.dumps([{"kind": "followed", "source_id": "sp1", "playlist_id": None}]),
            "s",
            3,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO runs (ts, status, exit_code, record_json, diff_json) VALUES (?, ?, ?, ?, ?)",
        (1, "degraded", 0, json.dumps(_V1_RECORD), None),
    )
    conn.commit()
    conn.close()

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION
        assert set(state.owned_releases()) == {ReleaseKey(artist_mbid="a1", rg_mbid="rg1")}
        assert state.health_baseline() is None, "an upgraded db has no baseline, so it takes the first-run path"
        state.record_health_baseline(HealthBaseline(fingerprint=_fingerprint(), identities={}))
        assert state.health_baseline() is not None
        assert state.last_gap_refreshes() == {}, "and no artist is inside the gap-refresh backoff yet"


_V3_BASELINE_META = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE health_baseline (
    dimension TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY (dimension, identity)
);
CREATE TABLE health_baseline_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1), resolver_version INTEGER NOT NULL,
    liked_track_scope TEXT NOT NULL, source_set TEXT NOT NULL, recorded_at TEXT NOT NULL
);
"""


_V4_NEGATIVE_CACHE = """
CREATE TABLE lidarr_negative_cache (
    identity TEXT PRIMARY KEY, cached_at TEXT NOT NULL
);
"""


@pytest.mark.parametrize("version", [3, 4])
def test_an_older_baseline_survives_the_rules_column_and_still_compares(tmp_path: Path, version: int) -> None:
    """Schema 5 adds a COLUMN, which `CREATE TABLE IF NOT EXISTS` cannot do.

    Both directions matter. A v3 file has to gain `lidarr_negative_cache` (a table) AND
    the `rules` column; a v4 file has to gain only the column. The migration is version-gated on
    neither - `_add_column` asks `PRAGMA table_info` - so the two land in either order and a file
    that already has the column is untouched.

    The existing baseline has to keep comparing in both cases: it was collected under a
    configuration with no opt-outs, and the default `ExclusionRules.token` is `""` too, so
    deploying the code must not cost the user a re-baseline.
    """
    path = tmp_path / f"v{version}.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V3_BASELINE_META)
    if version >= 4:
        conn.executescript(_V4_NEGATIVE_CACHE)
        conn.execute(
            "INSERT INTO lidarr_negative_cache VALUES ('album-search:Leopold Stokowski|', '2026-09-20T00:00:00+00:00')"
        )
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute("INSERT INTO health_baseline (dimension, identity) VALUES ('unmapped', 'i-1|rg-1')")
    conn.execute(
        "INSERT INTO health_baseline_meta VALUES (1, 4, 'smallest', ?, '2026-09-20T00:00:00+00:00')",
        (json.dumps(["liked_tracks"]),),
    )
    conn.commit()
    conn.close()

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION
        loaded = state.health_baseline()
        assert loaded is not None
        assert loaded.of("unmapped") == {"i-1|rg-1"}, "the identities are not rewritten"
        assert loaded.fingerprint.rules == "", "which is what a default configuration produces"
        assert loaded.fingerprint.mismatch(_fingerprint(liked_track_scope="smallest")) == ""
        cached = state.lidarr_negative_cache()
        assert list(cached) == (["album-search:Leopold Stokowski|"] if version >= 4 else [])


def test_the_rules_column_is_added_once_and_never_rewrites_a_v5_file(tmp_path: Path) -> None:
    """`_add_column` is PRAGMA-gated, not version-gated, so reopening must be a no-op."""
    path = tmp_path / "v5.sqlite"
    with SqliteState(path) as state:
        state.record_health_baseline(HealthBaseline(fingerprint=_fingerprint(rules="c0r1"), identities={}))
    with SqliteState(path) as state:
        loaded = state.health_baseline()
        assert loaded is not None
        assert loaded.fingerprint.rules == "c0r1"


def test_an_opt_out_token_round_trips_and_moves_the_fingerprint(tmp_path: Path) -> None:
    with SqliteState(tmp_path / "rules.sqlite") as state:
        state.record_health_baseline(HealthBaseline(fingerprint=_fingerprint(rules="c0r1"), identities={}))

        loaded = state.health_baseline()

        assert loaded is not None
        assert loaded.fingerprint.rules == "c0r1"
        assert loaded.fingerprint.mismatch(_fingerprint()) == "rules-changed"


def test_the_gap_refresh_backoff_round_trips(tmp_path: Path) -> None:
    """Schema 3. An upgraded file starts empty, so the first run after it refreshes normally."""
    when = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    with SqliteState(tmp_path / "gap.sqlite") as state:
        assert state.last_gap_refreshes() == {}
        state.record_gap_refreshes(["a1", "a2"], when)
        assert state.last_gap_refreshes() == {"a1": when, "a2": when}

        later = when + timedelta(days=1)
        state.record_gap_refreshes(["a1"], later)
        assert state.last_gap_refreshes() == {"a1": later, "a2": when}, "stamped per artist, not wholesale"


def test_the_lidarr_negative_cache_round_trips(tmp_path: Path) -> None:
    """Schema 4. An upgraded file starts empty, so the first run after it asks Lidarr normally."""
    when = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with SqliteState(tmp_path / "negcache.sqlite") as state:
        assert state.lidarr_negative_cache() == {}
        state.record_lidarr_negative_cache(["album-search:a|b", "album-lookup:rg-1"], when)
        assert state.lidarr_negative_cache() == {"album-search:a|b": when, "album-lookup:rg-1": when}

        later = when + timedelta(days=1)
        state.record_lidarr_negative_cache(["album-search:a|b"], later)
        assert state.lidarr_negative_cache() == {
            "album-search:a|b": later,
            "album-lookup:rg-1": when,
        }, "stamped per identity, not wholesale"


def test_the_scheduler_last_fire_round_trips(tmp_path: Path) -> None:
    """Schema 6. No record yet means the scheduler has never fired."""
    when = datetime(2026, 9, 24, 18, 20, tzinfo=UTC)
    with SqliteState(tmp_path / "sched.sqlite") as state:
        assert state.last_scheduled_fire() is None
        state.record_scheduled_fire(when)
        assert state.last_scheduled_fire() == when

        later = when + timedelta(hours=6)
        state.record_scheduled_fire(later)
        assert state.last_scheduled_fire() == later, "one row, overwritten, never appended to"


def test_a_fire_marked_cancelled_round_trips_and_a_fresh_fire_clears_it(tmp_path: Path) -> None:
    """Schema 7. A redeploy that cancelled a fire while it was still planning
    marks it so the missed-fire catch-up knows to re-run that exact slot."""
    when = datetime(2026, 9, 24, 18, 20, tzinfo=UTC)
    with SqliteState(tmp_path / "sched.sqlite") as state:
        assert not state.scheduled_fire_cancelled()
        state.mark_scheduled_fire_cancelled()
        assert not state.scheduled_fire_cancelled(), "nothing has fired yet; there is no row to mark"

        state.record_scheduled_fire(when)
        assert not state.scheduled_fire_cancelled()
        state.mark_scheduled_fire_cancelled()
        assert state.scheduled_fire_cancelled()

        state.record_scheduled_fire(when + timedelta(hours=6))
        assert not state.scheduled_fire_cancelled(), "a fresh fire is not cancelled until marked so"


def test_a_v3_database_upgrades_and_starts_with_an_empty_negative_cache(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.execute("CREATE TABLE gap_refreshes (artist_mbid TEXT PRIMARY KEY, refreshed_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    conn.commit()
    conn.close()

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION
        assert state.lidarr_negative_cache() == {}
        assert state.last_scheduled_fire() is None, "schema 6: an upgraded file has never fired"
        assert not state.scheduled_fire_cancelled(), "schema 7: an upgraded file has never been cancelled"


def test_a_v6_scheduler_state_row_gains_the_cancelled_column_at_zero(tmp_path: Path) -> None:
    """Schema 7: `scheduler_state.cancelled` is a new column, not a new table,
    so an existing row must gain it rather than the table being recreated empty."""
    path = tmp_path / "v6.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.execute("CREATE TABLE scheduler_state (id INTEGER PRIMARY KEY CHECK (id = 1), last_fire TEXT NOT NULL)")
    conn.execute("INSERT INTO scheduler_state (id, last_fire) VALUES (1, ?)", ("2026-09-24T18:20:00+00:00",))
    conn.execute("INSERT INTO schema_version (version) VALUES (6)")
    conn.commit()
    conn.close()

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION
        assert state.last_scheduled_fire() == datetime(2026, 9, 24, 18, 20, tzinfo=UTC)
        assert not state.scheduled_fire_cancelled()


# ---------------------------------------------------------------- first open by two processes


def test_the_schema_creates_scheduler_state_with_its_cancelled_column() -> None:
    """A new file never needs the ALTER, so two first opens cannot both run it."""
    from likearr.adapters.state_sqlite import _SCHEMA_SQL

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SCHEMA_SQL)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduler_state)")}
    finally:
        conn.close()
    assert "cancelled" in columns


def test_a_new_file_opened_twice_has_one_version_row(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    with SqliteState(path), SqliteState(path) as state:
        assert state._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


class _ConnProxy:
    """Forwards to a real connection, except for the statements a test scripts."""

    def __init__(self, conn: sqlite3.Connection, execute) -> None:
        self._conn = conn
        self._execute = execute

    def execute(self, sql: str, *args: Any):
        return self._execute(self._conn, sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_a_column_another_process_added_first_counts_as_migrated(tmp_path: Path) -> None:
    """The other process's ALTER lands between the column check and this one's ALTER."""
    with SqliteState(tmp_path / "s.sqlite") as state:
        real = state._conn

        def execute(conn: sqlite3.Connection, sql: str, *args: Any):
            if sql.startswith("PRAGMA table_info"):
                return iter(())  # the check ran before the other process added the column
            return conn.execute(sql, *args)

        state._conn = _ConnProxy(real, execute)  # type: ignore[assignment]
        try:
            state._add_column("scheduler_state", "cancelled", "INTEGER NOT NULL DEFAULT 0")
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                state._add_column("no_such_table", "x", "INTEGER")
        finally:
            state._conn = real


def _flaky_wal(failures: list[str]):
    def execute(conn: sqlite3.Connection, sql: str, *args: Any):
        if sql == "PRAGMA journal_mode=WAL" and failures:
            raise sqlite3.OperationalError(failures.pop(0))
        return conn.execute(sql, *args)

    return execute


def test_the_wal_switch_retries_while_the_database_is_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("likearr.adapters.state_sqlite._WAL_RETRY_PAUSE", 0.0)
    with SqliteState(tmp_path / "s.sqlite") as state:
        real = state._conn
        failures = ["database is locked", "database is locked", "database is busy"]
        state._conn = _ConnProxy(real, _flaky_wal(failures))  # type: ignore[assignment]
        try:
            state._switch_to_wal()
        finally:
            state._conn = real
        assert failures == []
        assert real.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_wal_switch_gives_up_after_its_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("likearr.adapters.state_sqlite._WAL_RETRY_PAUSE", 0.0)
    monkeypatch.setattr("likearr.adapters.state_sqlite._WAL_RETRY_SECONDS", 0.0)
    with SqliteState(tmp_path / "s.sqlite") as state:
        real = state._conn
        state._conn = _ConnProxy(real, _flaky_wal(["database is locked"] * 1000))  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                state._switch_to_wal()
        finally:
            state._conn = real


def test_the_wal_switch_does_not_retry_other_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("likearr.adapters.state_sqlite._WAL_RETRY_PAUSE", 0.0)
    with SqliteState(tmp_path / "s.sqlite") as state:
        real = state._conn
        failures = ["disk I/O error", "database is locked"]
        state._conn = _ConnProxy(real, _flaky_wal(failures))  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
                state._switch_to_wal()
        finally:
            state._conn = real
        assert failures == ["database is locked"], "raised on the first, non-busy error"


# ---------------------------------------------------------------- first reviewed apply

_V7_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE owned_releases (
    artist_mbid TEXT NOT NULL, rg_mbid TEXT NOT NULL, lidarr_album_id INTEGER,
    reasons_json TEXT NOT NULL, step TEXT NOT NULL, resolver_version INTEGER NOT NULL,
    monitored_at TEXT NOT NULL, PRIMARY KEY (artist_mbid, rg_mbid)
);
CREATE TABLE owned_artists (
    artist_mbid TEXT PRIMARY KEY, lidarr_artist_id INTEGER, added_by_us INTEGER NOT NULL,
    profile TEXT NOT NULL, ratcheted_at TEXT
);
CREATE TABLE resolutions (
    intent_key TEXT PRIMARY KEY, resolver_version INTEGER NOT NULL, status TEXT NOT NULL,
    json TEXT NOT NULL, resolved_at TEXT NOT NULL
);
CREATE TABLE pending (intent_key TEXT PRIMARY KEY, since TEXT NOT NULL);
CREATE TABLE source_counts (key TEXT PRIMARY KEY, count INTEGER NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE followed_counts (artist_mbid TEXT PRIMARY KEY, count INTEGER NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE gap_refreshes (artist_mbid TEXT PRIMARY KEY, refreshed_at TEXT NOT NULL);
CREATE TABLE lidarr_negative_cache (identity TEXT PRIMARY KEY, cached_at TEXT NOT NULL);
CREATE TABLE health_baseline (
    dimension TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY (dimension, identity)
);
CREATE TABLE health_baseline_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1), resolver_version INTEGER NOT NULL,
    liked_track_scope TEXT NOT NULL, source_set TEXT NOT NULL, recorded_at TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT ''
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, status TEXT NOT NULL,
    exit_code INTEGER NOT NULL, record_json TEXT NOT NULL, diff_json TEXT
);
CREATE TABLE scheduler_state (
    id INTEGER PRIMARY KEY CHECK (id = 1), last_fire TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0
);
INSERT INTO schema_version (version) VALUES (7);
"""


def _v7_file(path: Path, *, owned: bool = False, baseline: bool = False) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_V7_SCHEMA)
    if owned:
        conn.execute(
            "INSERT INTO owned_releases VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a1", "rg1", 7, json.dumps([]), "s", 3, "2026-01-01T00:00:00+00:00"),
        )
    if baseline:
        conn.execute(
            "INSERT INTO health_baseline_meta (id, resolver_version, liked_track_scope, source_set, recorded_at) "
            "VALUES (1, 4, 'album', 'liked_tracks', '2026-09-20T00:00:00+00:00')"
        )
    conn.commit()
    conn.close()
    return path


def test_no_first_apply_yet_reads_as_none_and_the_first_one_is_kept(tmp_path: Path) -> None:
    """Schema 8: the first hand-applied run is recorded once, and a later apply never moves it."""
    first = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    with SqliteState(tmp_path / "fresh.sqlite") as state:
        assert state.first_apply_at() is None
        state.record_first_apply(first)
        assert state.first_apply_at() == first
        state.record_first_apply(first + timedelta(days=1))
        assert state.first_apply_at() == first, "the first apply, not the latest"


def test_a_v7_file_that_owns_releases_migrates_as_already_applied(tmp_path: Path) -> None:
    """An existing install (one that has owned releases) must keep its schedule running with no
    step from the user: the v7 to v8 migration marks it as already applied."""
    path = _v7_file(tmp_path / "v7.sqlite", owned=True)

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION == 8
        assert state.first_apply_at() is not None


def test_a_v7_file_with_only_a_health_baseline_migrates_as_already_applied(tmp_path: Path) -> None:
    """Only an apply writes the health baseline, so an install whose applies claimed nothing (every
    release was already monitored) has still applied before."""
    path = _v7_file(tmp_path / "v7.sqlite", baseline=True)

    with SqliteState(path) as state:
        assert state.first_apply_at() is not None


def test_an_empty_v7_file_migrates_with_first_apply_unset(tmp_path: Path) -> None:
    path = _v7_file(tmp_path / "v7.sqlite")

    with SqliteState(path) as state:
        assert state.schema_version() == SCHEMA_VERSION
        assert state.first_apply_at() is None


def test_the_migration_runs_once_so_a_later_adopt_never_counts_as_an_apply(tmp_path: Path) -> None:
    """A v8 file is never re-migrated: `adopt --apply` fills `owned_releases` after the upgrade, and
    reopening the file must not read that as a reviewed apply."""
    path = _v7_file(tmp_path / "v7.sqlite")
    with SqliteState(path) as state:
        state.record_monitored(
            [
                OwnedRelease(
                    key=ReleaseKey(artist_mbid="a1", rg_mbid="rg1"),
                    reasons=frozenset({_reason()}),
                    step="adopt",
                    resolver_version=1,
                    monitored_at=datetime(2026, 9, 25, tzinfo=UTC),
                )
            ]
        )

    with SqliteState(path) as state:
        assert state.first_apply_at() is None


def test_a_fresh_file_is_not_marked_applied(tmp_path: Path) -> None:
    with SqliteState(tmp_path / "fresh.sqlite") as state:
        assert state.first_apply_at() is None


def test_a_run_recorded_before_this_change_still_deserialises(tmp_path: Path) -> None:
    """200 rows of history predate every new field; they must not need a migration to read."""
    path = tmp_path / "v1-runs.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute(
        "INSERT INTO runs (ts, status, exit_code, record_json, diff_json) VALUES (?, ?, ?, ?, ?)",
        (1, "degraded", 0, json.dumps(_V1_RECORD), None),
    )
    conn.commit()
    conn.close()

    with SqliteState(path) as state:
        last = state.last_run()

    assert last is not None
    assert last.status is RunStatus.DEGRADED
    assert last.unmapped == 240
    assert last.unmapped_new == 0, "a field it never carried defaults rather than raising"
    assert last.baseline == ""
    assert last.new_conditions == []
    assert last.tagged_without_state == 0


def test_the_last_published_run_skips_dry_runs_and_settings_refusals_however_many(state: SqliteState) -> None:
    from likearr.models import Guard

    applied = _diff()
    applied.guards[:] = [Guard(code="source-shrink", message="shrank", blocked_unmonitors=4)]
    state.record_run(_health_record(ts=1, dry_run=False, status=RunStatus.GUARDED, exit_code=2), applied)
    for ts in range(2, 30):
        state.record_run(_health_record(ts=ts, dry_run=True), _diff())
    state.record_run(_health_record(ts=30, dry_run=False, status=RunStatus.STALE, exit_code=3), None)

    row = state.last_published_run()

    assert row is not None
    assert row.record.ts == 1
    assert row.guards == ("shrank",) and row.guard_blocked == (4,)


def test_the_last_published_run_can_skip_paused_and_skipped_runs(state: SqliteState) -> None:
    """The webhook compares against the last run that said something about the library.
    A paused or skipped tick in between says nothing, so it must not hide an error before it."""
    state.record_run(_health_record(ts=1, dry_run=False, status=RunStatus.ERROR, exit_code=1, message="down"), None)
    state.record_run(_health_record(ts=2, dry_run=False, status=RunStatus.PAUSED), None)
    state.record_run(_health_record(ts=3, dry_run=False, status=RunStatus.SKIPPED), None)
    state.record_run(_health_record(ts=4, dry_run=True), _diff())
    state.record_run(_health_record(ts=5, dry_run=False, status=RunStatus.STALE, exit_code=3), None)

    everything = state.last_published_run()
    row = state.last_published_run(skip_idle=True)

    assert everything is not None and everything.record.status is RunStatus.SKIPPED, "the default is unchanged"
    assert row is not None
    assert row.record.ts == 1
    assert row.record.message == "down"


def test_there_is_no_published_run_before_an_apply(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1, dry_run=True), _diff())

    assert state.last_published_run() is None


def test_the_last_plan_guards_and_shrink_choice_come_from_the_newest_planned_run(state: SqliteState) -> None:
    from likearr.models import Guard

    older = _diff()
    older.guards[:] = [Guard(code="source-shrink", message="shrank", blocked_unmonitors=3)]
    newer = _diff()
    newer.guards[:] = []
    newer.accept_shrink = True
    state.record_run(_health_record(ts=1), older)
    state.record_run(_health_record(ts=2), newer)
    state.record_run(_health_record(ts=3, status=RunStatus.ERROR, exit_code=1), None)

    plan = state.last_plan()

    assert plan is not None
    assert plan.guards == ()
    assert plan.accept_shrink is True


def test_there_is_no_last_plan_before_one_is_recorded(state: SqliteState) -> None:
    assert state.last_plan() is None


def test_release_titles_come_from_the_cached_resolutions(state: SqliteState) -> None:
    from likearr.models import Resolution, ResolutionStatus

    rg = ReleaseGroup(mbid="rg-9", title="Kid A", artist_mbid="a-9", artist_name="Radiohead", primary_type=None)
    state.cache_resolution(Resolution("liked:t9", ResolutionStatus.RESOLVED, release_group=rg))

    assert state.release_titles() == {"rg-9": "Kid A"}


def test_an_apply_s_changes_made_and_planned_are_kept_and_an_older_record_reads_as_unknown(
    state: SqliteState,
) -> None:
    """The counts that tell a part-way apply from one that changed nothing survive the store."""
    state.record_run(_health_record(dry_run=False, changes_made=12, changes_planned=40, lidarr_changed=True), None)
    state.record_run(_health_record(), None)
    state._conn.execute(  # a record written before the fields existed
        "UPDATE runs SET record_json = json_remove(record_json, '$.changes_made', '$.changes_planned', "
        "'$.lidarr_changed') "
        "WHERE id = (SELECT MAX(id) FROM runs)"
    )

    newest, partial = state.run_history(2)

    assert (partial.record.changes_made, partial.record.changes_planned, partial.record.lidarr_changed) == (
        12,
        40,
        True,
    )
    assert (newest.record.changes_made, newest.record.changes_planned, newest.record.lidarr_changed) == (
        None,
        None,
        None,
    )


def test_the_lost_state_count_is_kept_and_an_older_record_reads_as_zero(state: SqliteState) -> None:
    """Tagged artists with no `owned_artists` row. Additive, so an older record reads as 0."""
    state.record_run(_health_record(tagged_without_state=3), None)
    state.record_run(_health_record(), None)
    state._conn.execute(  # a record written before the field existed
        "UPDATE runs SET record_json = json_remove(record_json, '$.tagged_without_state') "
        "WHERE id = (SELECT MAX(id) FROM runs)"
    )

    newest, counted = state.run_history(2)

    assert counted.record.tagged_without_state == 3
    assert newest.record.tagged_without_state == 0


# ---------------------------------------------------------------- run_by_id / run_id_in_job


def test_run_history_carries_each_row_s_id(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1), None)
    state.record_run(_health_record(ts=2), None)

    newest, oldest = state.run_history(limit=10)

    assert newest.id > oldest.id > 0


def test_run_by_id_returns_the_record_and_the_stored_diff(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1), None)  # a lower id to make sure the right row comes back
    state.record_run(_health_record(ts=2, dry_run=False), _diff())

    (newest, _oldest) = state.run_history(limit=10)
    found = state.run_by_id(newest.id)

    assert found is not None
    row, diff_raw = found
    assert row.record.ts == 2
    assert isinstance(diff_raw, dict)
    assert diff_raw["add_artists"][0]["artist_mbid"] == "a1"


def test_run_by_id_is_none_for_a_missing_id(state: SqliteState) -> None:
    assert state.run_by_id(999) is None


def test_run_by_id_s_diff_is_none_when_the_run_stored_none(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1, status=RunStatus.STALE, exit_code=3), None)

    (row,) = state.run_history(limit=1)
    found = state.run_by_id(row.id)

    assert found is not None
    assert found[1] is None


def test_run_id_in_job_links_the_one_applied_run_published_while_the_job_ran(state: SqliteState) -> None:
    state.record_run(_health_record(ts=900, dry_run=False), None)  # before the job started
    state.record_run(_health_record(ts=1050, dry_run=False), None)
    (_, inside) = state.run_history(limit=10)[::-1]

    assert state.run_id_in_job(1000, 1060) == inside.id
    # The child publishes moments before it exits; a few seconds of slack past `finished_at`.
    assert state.run_id_in_job(1000, 1040, grace_s=30) == inside.id


def test_run_id_in_job_never_guesses(state: SqliteState) -> None:
    """No run, a run only before the job started, or two candidates: no link, never the nearest."""
    assert state.run_id_in_job(1000, 1100) is None
    state.record_run(_health_record(ts=990, dry_run=False), None)
    assert state.run_id_in_job(1000, 1100) is None
    state.record_run(_health_record(ts=1010, dry_run=False), None)
    state.record_run(_health_record(ts=1020, dry_run=False), None)
    assert state.run_id_in_job(1000, 1100) is None


def test_run_id_in_job_ignores_dry_skipped_and_paused_records(state: SqliteState) -> None:
    state.record_run(_health_record(ts=1010, dry_run=True), None)
    state.record_run(_health_record(ts=1020, dry_run=False, status=RunStatus.SKIPPED), None)
    state.record_run(_health_record(ts=1030, dry_run=False, status=RunStatus.PAUSED), None)
    assert state.run_id_in_job(1000, 1100) is None
    state.record_run(_health_record(ts=1040, dry_run=False), None)
    (applied, *_) = state.run_history(limit=10)
    assert state.run_id_in_job(1000, 1100) == applied.id
