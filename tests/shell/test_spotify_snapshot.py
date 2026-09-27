"""`shell.spotify_snapshot`: a cancelled scheduled run's Spotify read, saved for reuse
(issue #68 phase 3). No real sleeps; only a fake clock."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from likearr.shell.spotify_snapshot import (
    MAX_AGE_S,
    delete_snapshot,
    read_snapshot,
    snapshot_path,
    write_snapshot,
)
from tests.shell.conftest import make_config
from tests.unit.fakes import artist_intent, snapshot

READ_AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _snap(**overrides: object):
    return snapshot(artists=[artist_intent("Test Artist", spotify_id="sp-a1")], fetched_at=READ_AT, **overrides)  # type: ignore[arg-type]


def test_write_then_read_round_trips_within_the_age_window(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_snapshot(config, _snap())

    read = read_snapshot(config, now=READ_AT + timedelta(minutes=29))

    assert read is not None
    assert read.artists[0].spotify_id == "sp-a1"
    assert read.fetched_at == READ_AT


def test_a_snapshot_at_exactly_the_age_limit_is_still_used(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_snapshot(config, _snap())

    assert read_snapshot(config, now=READ_AT + timedelta(seconds=MAX_AGE_S)) is not None


def test_a_snapshot_past_the_age_limit_is_ignored(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_snapshot(config, _snap())

    assert read_snapshot(config, now=READ_AT + timedelta(minutes=31)) is None


def test_a_snapshot_from_before_a_sources_config_change_is_ignored(tmp_path: Path) -> None:
    from likearr.config import SpotifyConfig

    config = make_config(tmp_path, spotify=SpotifyConfig(token_file=tmp_path / "spotify-token.json", playlists=()))
    write_snapshot(config, _snap())

    changed = make_config(
        tmp_path, spotify=SpotifyConfig(token_file=tmp_path / "spotify-token.json", playlists=("pl-new",))
    )

    assert read_snapshot(changed, now=READ_AT + timedelta(minutes=1)) is None


def test_no_saved_snapshot_reads_as_none(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert read_snapshot(config, now=READ_AT) is None


def test_the_saved_file_is_mode_0600(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_snapshot(config, _snap())

    mode = stat.S_IMODE(snapshot_path(config).stat().st_mode)
    assert mode == 0o600


def test_delete_removes_the_file_and_is_a_no_op_when_there_is_none(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_snapshot(config, _snap())
    assert snapshot_path(config).is_file()

    delete_snapshot(config)
    assert not snapshot_path(config).is_file()

    delete_snapshot(config)  # never raises with nothing to remove
