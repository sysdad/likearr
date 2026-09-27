"""Issue #123: `isrc-search` and `rg-tracks` cache rows keep only the fields likearr reads.

Three things are pinned here:

- **Equivalence.** Every parser of those two row kinds - the adapter's and the offline replay's -
  gives the same answer from a full MusicBrainz body and from the trimmed one. No answer may move
  without a `RESOLVER_VERSION` bump, and this change carries none.
- **The guard.** Every field a parser reads is a field the projector keeps. A parser that starts
  reading a new field fails `test_every_field_a_parser_reads_is_kept_*` until the projector keeps
  it too, whatever the fixture values happen to be.
- **Trim on write**, and that a row stored whole (as a cache written before the trim holds) is
  still read as it is: every parser gives the same answer from it.

Every body here is fake and hand-written in MusicBrainz's shape; nothing calls MusicBrainz.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from likearr.adapters.musicbrainz import (
    _ISRC_SEARCH_FIELDS,
    _RG_TRACKS_FIELDS,
    _SCHEMA,
    MusicBrainzLookup,
    _project,
)
from likearr.config import MusicBrainzConfig

from .conftest import MB_URL, FakeClock

ISRC = "XX0000000001"
OTHER_ISRC = "XX9999999999"
RG_MBID = "00000000-0000-4000-8000-000000000001"

# The fields issue #123 names, spelled out independently of the code: a projector that drifts from
# this list fails `test_the_trimmed_kinds_and_their_fields_are_the_ones_the_issue_names`.
EXPECTED_ISRC_SEARCH = {
    "recordings": {
        "title": None,
        "isrcs": None,
        "artist-credit": {"name": None},
        "releases": {"release-group": {"id": None}},
    }
}
EXPECTED_RG_TRACKS = {"releases": {"status": None, "media": {"tracks": {"title": None}}}}


def _rg_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _credit(name: str, n: int) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "joinphrase": "",
            "artist": {
                "id": f"00000000-0000-4000-a000-{n:012d}",
                "name": name,
                "sort-name": name,
                "disambiguation": "fake",
                "aliases": [{"name": f"{name} alias", "locale": None, "type": "Artist name"}],
            },
        }
    ]


def _release(n: int, *, compilation: bool, status: str = "Official") -> dict[str, Any]:
    """One release as a recording search returns it: most of the bulk of an `isrc-search` row."""
    return {
        "id": f"00000000-0000-4000-b000-{n:012d}",
        "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
        "count": 1,
        "title": f"Fake Hits Volume {n}" if compilation else "Fake Album",
        "status": status,
        "artist-credit": [{"name": "Various Artists", "artist": {"id": "va", "name": "Various Artists"}}]
        if compilation
        else _credit("Fake Band", 1),
        "release-group": {
            "id": _rg_id(100 + n if compilation else 1),
            "type-id": "f529b476-6e62-324f-b0aa-1f3e33d313fc",
            "primary-type-id": "f529b476-6e62-324f-b0aa-1f3e33d313fc",
            "title": f"Fake Hits Volume {n}" if compilation else "Fake Album",
            "primary-type": "Album",
            "secondary-types": ["Compilation"] if compilation else [],
            "secondary-type-ids": ["dd2a21e1-0c00-3729-a7a0-de60b84eb5d1"] if compilation else [],
        },
        "date": f"20{n % 25:02d}-01-01",
        "country": "XW",
        "release-events": [{"date": "2014", "area": {"id": "x", "name": "[Worldwide]", "iso-3166-1-codes": ["XW"]}}],
        "track-count": 20,
        "media": [
            {
                "position": 1,
                "format": "CD",
                "track": [{"id": f"t{n}", "number": "3", "title": "Fake Song", "length": 215000}],
                "track-count": 20,
                "track-offset": 2,
            }
        ],
    }


def _rich_isrc_body() -> dict[str, Any]:
    """The richest `isrc-search` body here, and every odd shape the parsers must treat alike.

    A hit song whose recording sits on dozens of releases, compilations included (the 800 KB rows
    in the issue), a fuzzy hit for another ISRC, a recording with no `isrcs` (never filtered), one
    with an empty list (never filtered either), and entries a parser must skip.
    """
    return {
        "created": "2026-09-18T12:00:00.000Z",
        "count": 4,
        "offset": 0,
        "recordings": [
            {
                "id": "00000000-0000-4000-c000-000000000001",
                "score": 100,
                "title": "Fake Song",
                "length": 215000,
                "video": None,
                "artist-credit": _credit("Fake Band", 1) + _credit("Guest", 2),
                "first-release-date": "2014-07-14",
                "isrcs": [ISRC],
                "tags": [{"count": 1, "name": "rock"}],
                "releases": [
                    _release(n, compilation=n % 3 != 0, status="Official" if n % 7 else "Promotion") for n in range(60)
                ]
                + [
                    {"id": "no-group", "title": "No Group"},
                    {"id": "group-without-id", "release-group": {"title": "Nameless"}},
                    {"id": "odd-id", "release-group": {"id": 12345}},
                    "not a release",
                ],
            },
            {
                "id": "00000000-0000-4000-c000-000000000002",
                "score": 88,
                "title": "Fake Song (Live)",
                "isrcs": [OTHER_ISRC],
                "artist-credit": _credit("Fake Band", 1),
                "releases": [_release(500, compilation=False)],
            },
            {
                "id": "00000000-0000-4000-c000-000000000003",
                "score": 70,
                "title": "Fake Song",
                "artist-credit": _credit("Fake Band", 1),
                "releases": [_release(600, compilation=True)],
            },
            {
                "id": "00000000-0000-4000-c000-000000000004",
                "score": 60,
                "title": "Fake Song (Demo)",
                "isrcs": [],
                "releases": None,
            },
            "not a recording",
        ],
    }


def _track(n: int, title: str) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-d000-{n:012d}",
        "number": str(n),
        "position": n,
        "title": title,
        "length": 200000 + n,
        "recording": {"id": f"rec-{n}", "title": title, "length": 200000 + n, "video": False},
    }


def _rich_rg_tracks_body() -> dict[str, Any]:
    """The richest `rg-tracks` body: five releases, a Promotion one first, an Official one with two
    media, a data track with no title, and entries a parser must skip."""

    def release(n: int, status: object, media: object) -> dict[str, Any]:
        return {
            "id": f"00000000-0000-4000-e000-{n:012d}",
            "title": "Fake Album",
            "status": status,
            "status-id": "x",
            "quality": "normal",
            "barcode": f"000000000000{n}",
            "date": "2014-07-14",
            "country": "GB",
            "packaging": "Jewel Case",
            "text-representation": {"language": "eng", "script": "Latn"},
            "cover-art-archive": {"artwork": True, "count": 3, "front": True, "back": True},
            "media": media,
        }

    return {
        "release-count": 5,
        "release-offset": 0,
        "releases": [
            release(1, "Promotion", [{"position": 1, "format": "CD", "tracks": [_track(1, "Promo Only")]}]),
            release(
                2,
                "Official",
                [
                    {
                        "position": 1,
                        "format": "CD",
                        "track-count": 3,
                        "tracks": [_track(1, "One"), _track(2, "Two"), _track(3, "")],
                    },
                    {"position": 2, "format": "CD", "tracks": [_track(4, "Three"), "not a track"]},
                    {"position": 3, "format": "DVD"},
                    "not a medium",
                ],
            ),
            release(3, None, [{"tracks": [_track(1, "Unknown Status")]}]),
            release(4, "Bootleg", None),
            "not a release",
        ],
    }


ISRC_BODIES: dict[str, dict[str, Any]] = {
    "rich": _rich_isrc_body(),
    # tests/adapters/test_musicbrainz.py::test_release_groups_for_isrc_dedupes
    "dedupe": {
        "recordings": [
            {
                "isrcs": [ISRC],
                "artist-credit": _credit("Fake Band", 1),
                "releases": [
                    {"release-group": {"id": _rg_id(1)}},
                    {"release-group": {"id": _rg_id(1)}},
                    {"release-group": {"id": _rg_id(2)}},
                ],
            },
            {"isrcs": [OTHER_ISRC], "releases": [{"release-group": {"id": "ignored"}}]},
        ]
    },
    # tests/adapters/test_same_name_artists.py::_serve_jungle
    "same-name": {
        "recordings": [
            {"isrcs": [ISRC], "releases": [{"release-group": {"id": _rg_id(7)}}, {"release-group": {"id": _rg_id(8)}}]}
        ]
    },
    "only-fuzzy-hits": {
        "count": 1,
        "recordings": [{"isrcs": [OTHER_ISRC], "releases": [_release(9, compilation=False)]}],
    },
    "empty": {"created": "2026-09-18T12:00:00.000Z", "count": 0, "offset": 0, "recordings": []},
    "no-result-key": {"count": 0},
}

RG_TRACKS_BODIES: dict[str, dict[str, Any]] = {
    "rich": _rich_rg_tracks_body(),
    # tests/adapters/test_musicbrainz.py::test_release_group_track_titles_prefers_official
    "prefers-official": {
        "releases": [
            {"status": "Promotion", "media": [{"tracks": [{"title": "Promo Only"}]}]},
            {
                "status": "Official",
                "media": [{"tracks": [{"title": "One"}, {"title": "Two"}]}, {"tracks": [{"title": "Three"}]}],
            },
        ]
    },
    "no-official": {
        "releases": [
            {"status": "Bootleg", "title": "x", "media": [{"format": "CD", "tracks": [{"title": "", "length": 1}]}]},
            {"title": "y", "media": [{"format": "CD", "tracks": [_track(1, "Only")]}]},
        ]
    },
    "numeric-status": {"releases": [{"status": 5, "media": [{"tracks": [{"title": 7}]}]}]},
    "empty": {"release-count": 0, "release-offset": 0, "releases": []},
    "no-result-key": {"release-count": 0},
}


# ---------------------------------------------------------------------------- helpers


class _Refuse(httpx.BaseTransport):
    """Any request is a test failure waiting to happen: refuse it, and count it."""

    def __init__(self) -> None:
        self.attempts = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        raise httpx.ConnectError("no network in this test", request=request)


def _seed(db: Path, rows: Mapping[str, tuple[str, int, int]]) -> None:
    """Write rows as an older likearr would have: the adapter's own schema, bodies verbatim."""
    with sqlite3.connect(db) as conn:
        conn.execute(_SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO mb_cache (key, body, fetched_at, negative) VALUES (?, ?, ?, ?)",
            [(k, body, fetched_at, negative) for k, (body, fetched_at, negative) in rows.items()],
        )
    conn.close()


def _rows(db: Path) -> dict[str, tuple[str, int, int]]:
    conn = sqlite3.connect(db)
    try:
        return {k: (b, f, n) for k, b, f, n in conn.execute("SELECT key, body, fetched_at, negative FROM mb_cache")}
    finally:
        conn.close()


def _open(
    db: Path, mb_config: MusicBrainzConfig, clock: FakeClock, *, refuse: _Refuse | None = None, **kwargs: Any
) -> MusicBrainzLookup:
    return MusicBrainzLookup(
        mb_config,
        httpx.Client(transport=refuse or _Refuse()),
        cache_path=db,
        now=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,
    )


def _rg_ids_in(body: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    recordings = body.get("recordings")
    for rec in recordings if isinstance(recordings, list) else []:
        releases = rec.get("releases") if isinstance(rec, dict) else None
        for rel in releases if isinstance(releases, list) else []:
            rg = rel.get("release-group") if isinstance(rel, dict) else None
            if isinstance(rg, dict) and isinstance(rg.get("id"), str):
                out.add(rg["id"])
    return out


def _negative(body: Mapping[str, Any], result_key: str) -> int:
    return 0 if body.get(result_key) else 1


def _isrc_answer(db: Path, body: Mapping[str, Any], mb_config: MusicBrainzConfig, clock: FakeClock) -> tuple[str, ...]:
    """`release_groups_for_isrc` answered from a cache holding exactly `body`, no request allowed."""
    now = int(clock.time())
    rows = {f"isrc-search:{ISRC}": (json.dumps(body), now, _negative(body, "recordings"))}
    for rg in _rg_ids_in(body):
        rows[f"rg:{rg}"] = (json.dumps({"id": rg, "title": f"Group {rg}", "primary-type": "Album"}), now, 0)
    _seed(db, rows)
    refuse = _Refuse()
    lookup = _open(db, mb_config, clock, refuse=refuse)
    try:
        answer = tuple(g.mbid for g in lookup.release_groups_for_isrc(ISRC))
    finally:
        lookup.close()
    assert refuse.attempts == 0
    return answer


def _tracks_answer(
    db: Path, body: Mapping[str, Any], mb_config: MusicBrainzConfig, clock: FakeClock
) -> tuple[str, ...]:
    now = int(clock.time())
    _seed(
        db,
        {f"rg-tracks:{RG_MBID}": (json.dumps(body), now, _negative(body, "releases"))},
    )
    refuse = _Refuse()
    lookup = _open(db, mb_config, clock, refuse=refuse)
    try:
        answer = tuple(lookup.release_group_track_titles(RG_MBID))
    finally:
        lookup.close()
    assert refuse.attempts == 0
    return answer


def _paths(value: object, prefix: str = "") -> Iterator[str]:
    """Every key path in a JSON value, list elements sharing their list's path."""
    if isinstance(value, list):
        for item in value:
            yield from _paths(item, prefix)
    elif isinstance(value, dict):
        for key, sub in value.items():
            yield f"{prefix}{key}"
            yield from _paths(sub, f"{prefix}{key}.")


def _spec_paths(spec: Mapping[str, Any], prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, sub in spec.items():
        out.add(f"{prefix}{key}")
        if sub is not None:
            out |= _spec_paths(sub, f"{prefix}{key}.")
    return out


class _Reads(dict[str, Any]):
    """A dict that records every key path a parser reads from it. Iterating it reads every key,
    recorded as ``*`` - which no projector keeps, so a parser that walks a whole object fails."""

    def __init__(self, data: Mapping[str, Any], prefix: str, seen: set[str]) -> None:
        super().__init__(data)
        self._prefix = prefix
        self._seen = seen

    def _read(self, key: object) -> None:
        self._seen.add(f"{self._prefix}{key}")

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self._read(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        self._read(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._read(key)
        return super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        self._read("*")
        return super().__iter__()

    def keys(self):  # type: ignore[override]
        self._read("*")
        return super().keys()

    def items(self):  # type: ignore[override]
        self._read("*")
        return super().items()

    def values(self):  # type: ignore[override]
        self._read("*")
        return super().values()


def _wrap(value: object, seen: set[str], prefix: str = "") -> Any:
    if isinstance(value, list):
        return [_wrap(item, seen, prefix) for item in value]
    if isinstance(value, dict):
        return _Reads({k: _wrap(v, seen, f"{prefix}{k}.") for k, v in value.items()}, prefix, seen)
    return value


def _load_replay() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "replay_resolver.py"
    spec = importlib.util.spec_from_file_location("replay_resolver_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------- the projectors


def test_the_trimmed_kinds_and_their_fields_are_the_ones_the_issue_names() -> None:
    assert _ISRC_SEARCH_FIELDS == EXPECTED_ISRC_SEARCH
    assert _RG_TRACKS_FIELDS == EXPECTED_RG_TRACKS


@pytest.mark.parametrize(
    ("body", "spec"),
    [pytest.param(b, EXPECTED_ISRC_SEARCH, id=f"isrc-search:{n}") for n, b in ISRC_BODIES.items()]
    + [pytest.param(b, EXPECTED_RG_TRACKS, id=f"rg-tracks:{n}") for n, b in RG_TRACKS_BODIES.items()],
)
def test_a_trimmed_body_keeps_exactly_the_kept_paths_it_had(body: dict[str, Any], spec: dict[str, Any]) -> None:
    trimmed = _project(body, spec)
    kept = _spec_paths(spec)
    assert set(_paths(trimmed)) == set(_paths(body)) & kept
    assert _project(trimmed, spec) == trimmed, "trimming twice changes nothing"
    assert json.dumps(_project(trimmed, spec)) == json.dumps(trimmed), "byte for byte"


def test_the_richest_isrc_body_shrinks_a_lot() -> None:
    body = _rich_isrc_body()
    assert len(json.dumps(_project(body, EXPECTED_ISRC_SEARCH))) * 5 < len(json.dumps(body))


# ---------------------------------------------------------------------------- equivalence


@pytest.mark.parametrize("name", list(ISRC_BODIES))
def test_isrc_search_answers_the_same_from_a_full_and_a_trimmed_body(
    name: str, mb_config: MusicBrainzConfig, tmp_path: Path, clock: FakeClock
) -> None:
    body = ISRC_BODIES[name]
    full = _isrc_answer(tmp_path / "full.db", body, mb_config, clock)
    trimmed = _isrc_answer(tmp_path / "trimmed.db", _project(body, EXPECTED_ISRC_SEARCH), mb_config, clock)
    assert full == trimmed
    if name == "rich":
        assert len(full) > 10, "the rich body must actually exercise the parser"


@pytest.mark.parametrize("name", list(RG_TRACKS_BODIES))
def test_rg_tracks_answers_the_same_from_a_full_and_a_trimmed_body(
    name: str, mb_config: MusicBrainzConfig, tmp_path: Path, clock: FakeClock
) -> None:
    body = RG_TRACKS_BODIES[name]
    full = _tracks_answer(tmp_path / "full.db", body, mb_config, clock)
    trimmed = _tracks_answer(tmp_path / "trimmed.db", _project(body, EXPECTED_RG_TRACKS), mb_config, clock)
    assert full == trimmed
    if name == "rich":
        assert full == ("One", "Two", "Three")


@respx.mock
def test_a_cold_fetch_and_the_cached_trimmed_body_give_the_same_answers(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """The issue's acceptance test: the first call parses the network body, later ones the trimmed
    row, and both answer alike."""
    isrc_route = respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json=_rich_isrc_body()))
    tracks_route = respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=_rich_rg_tracks_body()))
    respx.get(url__regex=rf"{MB_URL}/release-group/.+").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"id": request.url.path.rsplit("/", 1)[1], "title": "Group", "primary-type": "Album"}
        )
    )
    db = tmp_path / "state.db"
    lookup = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    cold_isrc = lookup.release_groups_for_isrc(ISRC)
    cold_tracks = lookup.release_group_track_titles(RG_MBID)
    lookup.close()

    again = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    assert again.release_groups_for_isrc(ISRC) == cold_isrc
    assert again.release_group_track_titles(RG_MBID) == cold_tracks
    again.close()
    assert isrc_route.call_count == 1 and tracks_route.call_count == 1
    assert len(cold_isrc) > 10 and cold_tracks == ("One", "Two", "Three")


def test_the_replay_index_is_the_same_from_full_and_trimmed_bodies(tmp_path: Path) -> None:
    """`scripts/replay_resolver.py` reads `isrc-search` rows itself, with its own field list
    (the recording title and first credit's name, for its `by_title` index)."""
    replay = _load_replay()

    def index(db: Path, project: bool) -> object:
        rows = {
            f"isrc-search:{ISRC}": _rich_isrc_body(),
            f"isrc-search:{OTHER_ISRC}": ISRC_BODIES["only-fuzzy-hits"],
            "isrc-search:XX0000000002": ISRC_BODIES["dedupe"],
        }
        _seed(
            db,
            {
                k: (json.dumps(_project(b, EXPECTED_ISRC_SEARCH) if project else b), 0, _negative(b, "recordings"))
                for k, b in rows.items()
            },
        )
        conn = sqlite3.connect(db)
        try:
            by_rg, by_title = replay._isrc_index(conn)
        finally:
            conn.close()
        return dict(by_rg), dict(by_title)

    full = index(tmp_path / "full.db", project=False)
    assert full == index(tmp_path / "trimmed.db", project=True)
    by_rg, by_title = full  # type: ignore[misc]
    assert by_title["Fake Song"] and by_rg


# ---------------------------------------------------------------------------- the guard


def test_every_field_the_isrc_parser_reads_is_kept(
    mb_config: MusicBrainzConfig, tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails the moment `release_groups_for_isrc` reads a field the projector drops."""
    seen: set[str] = set()
    lookup = _open(tmp_path / "state.db", mb_config, clock)

    def fetch(path: str, params: Mapping[str, Any], *, cache_key: str, result_key: str, **_: Any) -> Mapping[str, Any]:
        if cache_key.startswith("isrc-search:"):
            return _wrap(_rich_isrc_body(), seen)
        return {"id": cache_key.split(":", 1)[1], "title": "Group"}

    monkeypatch.setattr(lookup, "_fetch", fetch)
    assert lookup.release_groups_for_isrc(ISRC)
    lookup.close()
    assert seen, "the tracking wrapper saw nothing: the guard would pass vacuously"
    assert seen <= _spec_paths(EXPECTED_ISRC_SEARCH), sorted(seen - _spec_paths(EXPECTED_ISRC_SEARCH))


def test_every_field_the_track_titles_parser_reads_is_kept(
    mb_config: MusicBrainzConfig, tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: set[str] = set()
    lookup = _open(tmp_path / "state.db", mb_config, clock)
    monkeypatch.setattr(lookup, "_fetch", lambda *_a, **_k: _wrap(_rich_rg_tracks_body(), seen))
    assert lookup.release_group_track_titles(RG_MBID) == ("One", "Two", "Three")
    lookup.close()
    assert seen
    assert seen <= _spec_paths(EXPECTED_RG_TRACKS), sorted(seen - _spec_paths(EXPECTED_RG_TRACKS))


def test_every_field_the_replay_reads_from_isrc_rows_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replay = _load_replay()
    seen: set[str] = set()
    db = tmp_path / "state.db"
    _seed(db, {f"isrc-search:{ISRC}": (json.dumps(_rich_isrc_body()), 0, 0)})
    monkeypatch.setattr(replay, "json", SimpleNamespace(loads=lambda text: _wrap(json.loads(text), seen)))
    conn = sqlite3.connect(db)
    try:
        replay._isrc_index(conn)
    finally:
        conn.close()
    assert seen
    assert seen <= _spec_paths(EXPECTED_ISRC_SEARCH), sorted(seen - _spec_paths(EXPECTED_ISRC_SEARCH))


# ---------------------------------------------------------------------------- trim on write


@respx.mock
def test_a_new_isrc_search_row_stores_only_the_kept_fields(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json=_rich_isrc_body()))
    respx.get(url__regex=rf"{MB_URL}/release-group/.+").mock(return_value=httpx.Response(200, json={"id": "x"}))
    db = tmp_path / "state.db"
    lookup = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    lookup.release_groups_for_isrc(ISRC)
    lookup.close()
    body, _, negative = _rows(db)[f"isrc-search:{ISRC}"]
    stored = json.loads(body)
    assert set(_paths(stored)) <= _spec_paths(EXPECTED_ISRC_SEARCH)
    assert stored == _project(_rich_isrc_body(), EXPECTED_ISRC_SEARCH)
    assert negative == 0


@respx.mock
def test_a_new_rg_tracks_row_stores_only_the_kept_fields(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=_rich_rg_tracks_body()))
    db = tmp_path / "state.db"
    lookup = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    lookup.release_group_track_titles(RG_MBID)
    lookup.close()
    body, _, negative = _rows(db)[f"rg-tracks:{RG_MBID}"]
    assert set(_paths(json.loads(body))) <= _spec_paths(EXPECTED_RG_TRACKS)
    assert negative == 0


@respx.mock
@pytest.mark.parametrize(
    ("path", "call", "key", "empty"),
    [
        ("recording", lambda lk: lk.release_groups_for_isrc(ISRC), f"isrc-search:{ISRC}", "recordings"),
        ("release", lambda lk: lk.release_group_track_titles(RG_MBID), f"rg-tracks:{RG_MBID}", "releases"),
    ],
)
def test_an_empty_result_is_still_a_negative_row_that_keeps_its_result_key(
    path: str,
    call: Any,
    key: str,
    empty: str,
    mb_config: MusicBrainzConfig,
    client: httpx.Client,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    route = respx.get(f"{MB_URL}/{path}").mock(
        return_value=httpx.Response(200, json={"created": "2026", "count": 0, "offset": 0, empty: []})
    )
    db = tmp_path / "state.db"
    lookup = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    assert call(lookup) == ()
    assert call(lookup) == (), "answered from the negative row"
    lookup.close()
    assert route.call_count == 1
    body, _, negative = _rows(db)[key]
    assert json.loads(body) == {empty: []}
    assert negative == 1


@respx.mock
def test_the_first_call_returns_the_full_body_and_later_calls_the_trimmed_one(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    respx.get(f"{MB_URL}/recording").mock(return_value=httpx.Response(200, json=_rich_isrc_body()))
    lookup = MusicBrainzLookup(mb_config, client, cache_path=tmp_path / "state.db", now=clock.time, sleep=clock.sleep)

    def fetch() -> Mapping[str, Any]:
        return lookup._fetch(
            "recording",
            {"query": f"isrc:{ISRC}"},
            cache_key=f"isrc-search:{ISRC}",
            result_key="recordings",
            keep_fields=_ISRC_SEARCH_FIELDS,
        )

    assert fetch() == _rich_isrc_body()
    assert fetch() == _project(_rich_isrc_body(), EXPECTED_ISRC_SEARCH)
    lookup.close()


@respx.mock
def test_other_row_kinds_are_stored_whole(
    mb_config: MusicBrainzConfig, client: httpx.Client, tmp_path: Path, clock: FakeClock
) -> None:
    """`rg-releases` shares `/release` with `rg-tracks` but reads links and barcodes: not trimmed."""
    body = {"releases": [{**_release(1, compilation=False), "barcode": "0000000000001", "relations": []}]}
    respx.get(f"{MB_URL}/release").mock(return_value=httpx.Response(200, json=body))
    db = tmp_path / "state.db"
    lookup = MusicBrainzLookup(mb_config, client, cache_path=db, now=clock.time, sleep=clock.sleep)
    lookup.release_group_barcodes(RG_MBID)
    lookup.close()
    assert json.loads(_rows(db)[f"rg-releases:{RG_MBID}"][0]) == body


# ---------------------------------------------------------------------------- a cache from an older likearr


def test_a_cache_written_before_the_trim_opens_as_it_is_and_answers_from_its_rows(
    mb_config: MusicBrainzConfig, tmp_path: Path, clock: FakeClock
) -> None:
    """A state database from an older likearr can hold `isrc-search:` and `rg-tracks:` rows stored
    whole, and the row an old one-time compaction left behind (``meta:trimmed:1``). Opening it
    rewrites nothing, sends no request, and answers from the whole bodies exactly as from trimmed
    ones; each row is trimmed only when it is next fetched."""
    db = tmp_path / "state.db"
    now = int(clock.time())
    old = {
        f"isrc-search:{ISRC}": (json.dumps(_rich_isrc_body()), now, 0),
        f"rg-tracks:{RG_MBID}": (json.dumps(_rich_rg_tracks_body()), now, 0),
        "meta:trimmed:1": ('{"kinds": ["isrc-search", "rg-tracks"], "rows": 2, "bytes": 1}', now, 0),
    } | {
        f"rg:{rg}": (json.dumps({"id": rg, "title": f"Group {rg}", "primary-type": "Album"}), now, 0)
        for rg in _rg_ids_in(_rich_isrc_body())
    }
    _seed(db, old)
    refuse = _Refuse()

    lookup = _open(db, mb_config, clock, refuse=refuse)
    try:
        assert _rows(db) == old, "opening rewrites nothing"
        from_whole = (
            tuple(g.mbid for g in lookup.release_groups_for_isrc(ISRC)),
            tuple(lookup.release_group_track_titles(RG_MBID)),
        )
    finally:
        lookup.close()

    assert refuse.attempts == 0
    assert from_whole == (
        _isrc_answer(tmp_path / "isrc.db", _project(_rich_isrc_body(), EXPECTED_ISRC_SEARCH), mb_config, clock),
        _tracks_answer(tmp_path / "tracks.db", _project(_rich_rg_tracks_body(), EXPECTED_RG_TRACKS), mb_config, clock),
    )
    assert from_whole[0] and from_whole[1], "a real answer, not two empty ones"
