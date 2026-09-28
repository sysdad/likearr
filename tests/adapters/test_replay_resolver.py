"""Tests for `scripts/replay_resolver.py`'s Lidarr fallback check.

The snapshot is synthetic: a fresh state database with invented resolutions and `mb_cache` rows,
written the way likearr writes them. Nothing here reads a real snapshot.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from likearr.adapters.musicbrainz import _SCHEMA
from likearr.adapters.state_sqlite import SqliteState
from likearr.models import PrimaryType, ReleaseGroup, Resolution, ResolutionStatus

WANTED, STRANGER, TITLE = "青い鳥", "赤い月", "夜明けの歌"


def _load_replay() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "replay_resolver.py"
    spec = importlib.util.spec_from_file_location("replay_resolver_lidarr_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rg(mbid: str, title: str, artist: str, artist_mbid: str = "artist-fake") -> ReleaseGroup:
    return ReleaseGroup(
        mbid=mbid, title=title, artist_mbid=artist_mbid, artist_name=artist, primary_type=PrimaryType.ALBUM
    )


def _saved(key: str, artist: str, title: str, rg: ReleaseGroup) -> Resolution:
    return Resolution(
        intent_key=key,
        status=ResolutionStatus.RESOLVED,
        release_group=rg,
        step="album:search",
        detail=f"{artist!r} - {title!r} matched release group {rg.title!r} ({rg.mbid}) by name",
        source_release_group=rg,
    )


def _snapshot(path: Path) -> Path:
    stranger = _rg("rg-stranger", TITLE, STRANGER, "artist-stranger")
    from_mb = _rg("rg-from-mb", "Fake Album", "Fake Band")
    uncached = _rg("rg-uncached", "Other Fake Album", "Fake Band")
    stripped = _rg("rg-stripped", "Fake Album Two", "Fake Band")
    chosen = _rg("rg-chosen", "Fake Record", "Fake Band")
    resolutions = [
        _saved("saved:sp-stranger", WANTED, TITLE, stranger),
        _saved("saved:sp-from-mb", "Fake Band", "Fake Album", from_mb),
        _saved("saved:sp-uncached", "Fake Band", "Other Fake Album", uncached),
        Resolution(
            intent_key="liked:sp-track",
            status=ResolutionStatus.RESOLVED,
            release_group=stripped,
            step="track:album",
            detail=(
                "'Fake Song' is on 'Fake Album Two' (rg-stripped), a studio Album; 'Fake Band' - "
                "'Fake Album Two (Deluxe)' matched release group 'Fake Album Two' (rg-stripped) by name "
                "after stripping '(Deluxe)'"
            ),
            source_release_group=stripped,
        ),
        Resolution(
            intent_key="liked:sp-same-name",
            status=ResolutionStatus.RESOLVED,
            release_group=chosen,
            step="track:album",
            detail=(
                "'Other Fake Song' is on 'Fake Record' (rg-chosen), a studio Album; 2 different artists "
                "named 'Fake Band' each have a release titled 'Fake Record': 'Fake Record' (rg-chosen, "
                "date unknown) by Fake Band (artist-fake); 'Fake Record' (rg-namesake, date unknown) by "
                "Fake Band (artist-namesake); ISRC XX0000000001 chose rg-chosen"
            ),
            source_release_group=chosen,
        ),
        Resolution(
            intent_key="saved:sp-barcode",
            status=ResolutionStatus.RESOLVED,
            release_group=from_mb,
            step="album:upc",
            detail="barcode 000000000001 is release group 'Fake Album' (rg-from-mb)",
        ),
    ]
    with SqliteState(path) as state:
        for r in resolutions:
            state.cache_resolution(r)
    with sqlite3.connect(path) as conn:
        conn.execute(_SCHEMA)
        conn.executemany(
            "INSERT INTO mb_cache (key, body, fetched_at, negative) VALUES (?, ?, 0, ?)",
            [
                # MusicBrainz found nothing for the non-Latin search, so Lidarr answered it.
                (f"rg-search:{WANTED}|{TITLE}", json.dumps({"release-groups": []}), 1),
                (
                    "rg-search:fake band|fake album",
                    json.dumps({"release-groups": [{"id": "rg-from-mb", "title": "Fake Album"}]}),
                    0,
                ),
            ],
        )
    conn.close()
    return path


def test_the_check_refuses_the_non_latin_stranger_and_keeps_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replay = _load_replay()
    out = tmp_path / "new.json"

    assert replay.main([str(_snapshot(tmp_path / "snapshot.sqlite")), "--out", str(out)]) == 0

    check = json.loads(out.read_text())["lidarr_check"]
    rows = check["rows"]
    assert set(rows) == {
        "saved:sp-stranger",
        "saved:sp-from-mb",
        "saved:sp-uncached",
        "liked:sp-track",
        "liked:sp-same-name",
    }, "a barcode answer never went through the name search"
    assert (rows["saved:sp-stranger"]["source"], rows["saved:sp-stranger"]["accepted"]) == ("lidarr-fallback", False)
    assert rows["saved:sp-stranger"]["ascii_fold_empty"] is True
    assert (rows["saved:sp-from-mb"]["source"], rows["saved:sp-from-mb"]["accepted"]) == ("musicbrainz", True)
    assert (rows["saved:sp-uncached"]["source"], rows["saved:sp-uncached"]["accepted"]) == ("unknown", True)
    assert rows["liked:sp-track"]["spotify_title"] == "Fake Album Two", "the stripped title is what was searched"
    assert rows["liked:sp-track"]["accepted"] is True
    assert rows["liked:sp-same-name"]["stored_mbid"] == "rg-chosen"
    assert rows["liked:sp-same-name"]["accepted"] is True
    assert not any(r["ascii_fold_empty"] for k, r in rows.items() if k != "saved:sp-stranger")
    assert check["other_requests"] == 0 and check["unparsed"] == 0

    printed = capsys.readouterr().out
    assert "lidarr fallback check: 5 name-search answer(s)" in printed
    assert "refused by this code: lidarr-fallback 1, unknown 0, musicbrainz 0" in printed
    for name in (WANTED, STRANGER, TITLE, "Fake Band", "Fake Album", "stand-in-no-key"):
        assert name not in printed, "only counts go to standard output"


def _result(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "resolver_version": 0,
        "stored": len(rows),
        "replayed": 0,
        "skipped": {},
        "network_attempts": 0,
        "results": {},
        "lidarr_check": {"rows": rows, "unparsed": 0, "other_requests": 0},
    }


def _row(*, accepted: bool, source: str = "lidarr-fallback", empty: bool = True) -> dict[str, Any]:
    return {
        "source": source,
        "step": "album:search",
        "ascii_fold_empty": empty,
        "accepted": accepted,
        "spotify_artist": WANTED if empty else "Fake Band",
        "spotify_title": TITLE if empty else "Fake Album (Part Two)",
        "stored_artist": STRANGER if empty else "Fake Band",
        "stored_title": TITLE if empty else "Fake Album",
        "stored_mbid": "rg-x",
    }


def test_compare_counts_expected_and_unexplained_moves_and_writes_rows_only_to_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replay = _load_replay()
    old, new, detail = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "moves.json"
    old.write_text(
        json.dumps(
            _result(
                {
                    "saved:a": _row(accepted=True),
                    "saved:b": _row(accepted=True, empty=False),
                    "saved:c": _row(accepted=True, source="musicbrainz", empty=False),
                }
            )
        )
    )
    new.write_text(
        json.dumps(
            _result(
                {
                    "saved:a": _row(accepted=False),
                    "saved:b": _row(accepted=False, empty=False),
                    "saved:c": _row(accepted=True, source="musicbrainz", empty=False),
                }
            )
        )
    )

    assert replay.main(["--compare", str(new), str(old), "--lidarr-detail", str(detail)]) == 0

    printed = capsys.readouterr().out
    assert "lidarr fallback check, old -> new: 2 answer(s) moved" in printed
    assert "1  expected: lidarr-fallback, old accepts -> new refuses" in printed
    assert "1  needs an explanation: lidarr-fallback, old accepts -> new refuses" in printed
    for name in (WANTED, STRANGER, TITLE, "Fake Band", "Part Two"):
        assert name not in printed
    moved = json.loads(detail.read_text())["old"]
    assert [(r["key"], r["expected"]) for r in moved] == [("saved:a", True), ("saved:b", False)]
