"""The playlist-name cache: id to name, kept by the web UI and read by the CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from likearr.playlist_names import names_path, playlist_url, read_names, write_names

WHEN = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)


def test_the_cache_lives_beside_the_config(tmp_path: Path) -> None:
    assert names_path(tmp_path / "config.toml") == tmp_path / "ui" / "playlist-names.json"


def test_a_missing_or_unreadable_cache_is_empty(tmp_path: Path) -> None:
    assert read_names(tmp_path / "nope.json").names == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    cache = read_names(bad)
    assert cache.names == {}
    assert cache.fetched_at is None


def test_writing_merges_and_the_newest_name_wins(tmp_path: Path) -> None:
    path = tmp_path / "ui" / "playlist-names.json"
    write_names(path, {"pl-1": "Road trip", "pl-2": "Gym"}, fetched_at=WHEN)
    write_names(path, {"pl-1": "Road Trip 2026"}, fetched_at=WHEN.replace(hour=19))

    cache = read_names(path)

    assert cache.names == {"pl-1": "Road Trip 2026", "pl-2": "Gym"}  # a playlist gone from the account keeps its name
    assert cache.fetched_at == WHEN.replace(hour=19)
    assert not [p for p in path.parent.iterdir() if p.name != path.name]  # atomic: no temp file left


def test_not_owned_is_replaced_whole_not_merged(tmp_path: Path) -> None:
    """Unlike `names`, `not_owned` reflects only the newest fetch: a playlist absent from a later
    listing (unfollowed, deleted) must stop being flagged, and only the latest listing knows that
    (issue #103, item 1)."""
    path = tmp_path / "ui" / "playlist-names.json"
    write_names(path, {"pl-1": "Road trip", "pl-2": "Discover Weekly"}, fetched_at=WHEN, not_owned=["pl-2"])
    assert read_names(path).not_owned == frozenset({"pl-2"})

    write_names(path, {"pl-1": "Road trip"}, fetched_at=WHEN.replace(hour=19), not_owned=())

    cache = read_names(path)
    assert cache.not_owned == frozenset()
    assert cache.names == {"pl-1": "Road trip", "pl-2": "Discover Weekly"}  # names still merge


def test_needs_reauth_round_trips_and_is_replaced_whole(tmp_path: Path) -> None:
    """#103 item 3: the collaborative playlists a re-authorization would make readable, kept beside
    `not_owned` so a save can say "re-authorize" rather than "copy it"."""
    path = tmp_path / "ui" / "playlist-names.json"
    write_names(path, {"pl-c": "Band Van"}, fetched_at=WHEN, not_owned=["pl-c"], needs_reauth=["pl-c"])
    assert read_names(path).needs_reauth == frozenset({"pl-c"})

    write_names(path, {"pl-c": "Band Van"}, fetched_at=WHEN.replace(hour=19), not_owned=())
    assert read_names(path).needs_reauth == frozenset()


def test_not_owned_missing_from_the_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "ui" / "playlist-names.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"fetched_at": "2026-09-23T18:00:00+00:00", "names": {"pl-1": "Road trip"}}')

    assert read_names(path).not_owned == frozenset()
    assert read_names(path).needs_reauth == frozenset()


def test_an_unnamed_playlist_links_to_spotify() -> None:
    assert playlist_url("37i9dQZF1DXcBWIGoYBM5M") == "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize("playlist_id", ["", "../x", "a b", "javascript:alert(1)", "x" * 65])
def test_an_id_that_is_not_a_spotify_id_gets_no_link(playlist_id: str) -> None:
    assert playlist_url(playlist_id) is None
