"""`shell.commands`: auth, playlists, adopt, lidarr-files and explain."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from likearr.adapters.lock import LockHeld, run_lock
from likearr.adapters.spotify import SpotifyAccount, SpotifyAuth, TokenSet
from likearr.adapters.spotify_library import OwnedPlaylist, PlaylistEntry
from likearr.models import EXIT_ERROR, EXIT_OK, EXIT_STALE, ReasonKind, ReleaseGroup, ReleaseKey
from likearr.ports import CatalogueTooLarge, MetadataError, SourceError
from likearr.shell import commands
from likearr.shell.context import Context
from likearr.shell.diff_io import DiffFileError
from tests.shell.commands_shared import (
    ALBUM,
    EP,
    QUOTA_BODY,
    STRANGER,
    FakeSpotify,
    followed_world,
    token_file_data,
)
from tests.shell.conftest import NOW, CapturingSink, FakeLibrary, FakeLidarr, FakeSource, make_config, make_context
from tests.unit.fakes import FakeLookup, artist_intent, lidarr_album, lidarr_artist, owned, reason, rg, snapshot

# --------------------------------------------------------------------------- adopt


def _adopt_world(
    tmp_path: Path, *, sink: CapturingSink, first_applied: bool = True
) -> tuple[Any, FakeLidarr, FakeSource, FakeLookup]:
    """artist-1 is followed (rg-1 wanted), artist-9 is a stranger with one monitored album."""
    source, lookup, _ = followed_world()
    lookup.add(STRANGER)
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=True),
    )
    lidarr.seed(
        lidarr_artist("artist-9", id=9, name="A Stranger"),
        lidarr_album(STRANGER, id=901, artist_id=9, monitored=True),
    )
    ctx = make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, first_applied=first_applied)
    return ctx, lidarr, source, lookup


def test_adopt_plans_only_and_writes_a_plan_file(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, _ = followed_world()
    lookup.add(STRANGER)
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
    )
    lidarr.seed(
        lidarr_artist("artist-9", id=9, name="A Stranger"),
        lidarr_album(STRANGER, id=901, artist_id=9, monitored=True),
    )
    keep = tmp_path / "keep.txt"
    keep.write_text("# a note\nrg-9\n")

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = commands.adopt_command(ctx, keep_file=keep, out=tmp_path / "adopt.json", now=NOW)
        assert ctx.state.owned_releases() == {}

    payload = json.loads((tmp_path / "adopt.json").read_text())
    assert code == EXIT_OK
    assert payload["summary"] == {"claim": 2, "keep": 1, "unmonitor": 0}
    assert payload["source_digest"] and payload["lidarr_digest"]
    assert lidarr.writes() == []
    out = capsys.readouterr().out
    assert "claim" in out
    assert "--apply" in out


def test_adopt_plan_says_the_next_run_sets_monitor_new_albums_to_none_on_the_artists_it_will_own(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """#172: adopt only records ownership, and the next run then sets "Monitor New Albums" to None
    on every artist holding a claimed or kept release. The plan says so, by name."""
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    lidarr.artists["artist-1"] = replace(lidarr.artists["artist-1"], monitor_new_items="all")
    lidarr.artists["artist-9"] = replace(lidarr.artists["artist-9"], monitor_new_items="new")
    keep = tmp_path / "keep.txt"
    keep.write_text("rg-9\n")
    with ctx:
        code = commands.adopt_command(ctx, keep_file=keep, out=tmp_path / "adopt.json", now=NOW)

    lines = capsys.readouterr().out.splitlines()
    at = lines.index("1 to claim, 1 to keep, 0 to unmonitor")
    assert code == EXIT_OK
    assert lines[at + 1] == (
        'the next run sets "Monitor New Albums" to None on 2 artist(s) whose releases likearr will then own: '
        "A Stranger, Test Artist"
    )
    assert lidarr.writes() == [], "only said, not done"


def test_adopt_plan_leaves_out_artists_already_on_none_and_names_twenty_at_most(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr()
    keep = tmp_path / "keep.txt"
    for i in range(23):
        mbid, group = f"artist-k{i:02d}", rg(f"rg-k{i:02d}", f"Kept {i}", artist_mbid=f"artist-k{i:02d}")
        setting = "none" if i == 0 else "all"
        lidarr.seed(
            lidarr_artist(mbid, id=100 + i, name=f"Kept {i:02d}", monitor_new_items=setting),
            lidarr_album(group, id=1000 + i, artist_id=100 + i, monitored=True),
        )
    keep.write_text("".join(f"artist:artist-k{i:02d}\n" for i in range(23)))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        commands.adopt_command(ctx, keep_file=keep, out=tmp_path / "adopt.json", now=NOW)

    line = next(x for x in capsys.readouterr().out.splitlines() if "Monitor New Albums" in x)
    shown = ", ".join(f"Kept {i:02d}" for i in range(1, 21))
    assert line == (
        'the next run sets "Monitor New Albums" to None on 22 artist(s) whose releases likearr will then own: '
        f"{shown}, and 2 more"
    )


def test_adopt_plan_says_nothing_about_monitor_new_albums_when_every_artist_is_on_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    with ctx:
        commands.adopt_command(ctx, out=tmp_path / "adopt.json", now=NOW)

    assert "Monitor New Albums" not in capsys.readouterr().out


def test_adopt_apply_alone_is_not_the_first_reviewed_apply(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #111: `adopt --apply` claims what Lidarr already monitors; it is not a reviewed run of
    the plan, so scheduled applies stay held after it."""
    ctx, _lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink, first_applied=False)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        assert commands.adopt_command(ctx, apply_path=plan_path, now=NOW) == EXIT_OK
        owned = ctx.state.owned_releases()
        first = ctx.state.first_apply_at()

    assert owned, "adopt did claim releases"
    assert first is None


def test_adopt_apply_executes_the_reviewed_plan(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        code = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert ReleaseKey("artist-1", "rg-1") in owned, "claimed with its real reasons"
    assert ReleaseKey("artist-9", "rg-9") not in owned, "an unmonitored release was never ours"
    assert lidarr.album("artist-9", "rg-9").monitored is False  # type: ignore[union-attr]
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]


def test_adopt_apply_keeps_what_the_plan_kept(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("artist:artist-9\n")
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, keep_file=keep, out=plan_path, now=NOW)
        commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        record = ctx.state.owned_releases()[ReleaseKey("artist-9", "rg-9")]

    assert record.is_manual
    assert {r.kind for r in record.reasons} == {ReasonKind.MANUAL}
    assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]


def test_adopt_keeps_a_keep_listed_release_a_source_also_wants_through_a_later_unfollow(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the keep list and wanted by the follow: the plan says `claim+keep`, the row carries both
    reasons, and unfollowing afterwards leaves the release monitored."""
    from likearr.shell.run import run_command

    ctx, lidarr, source, _lookup = _adopt_world(tmp_path, sink=sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("rg-1\n")
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, keep_file=keep, out=plan_path, now=NOW)
        assert "claim+keep" in capsys.readouterr().out
        commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        record = ctx.state.owned_releases()[ReleaseKey("artist-1", "rg-1")]
        assert record.is_manual
        assert ReasonKind.FOLLOWED in {r.kind for r in record.reasons}

        source.snapshot = snapshot(artists=[], counts={"followed_artists": 19})
        ctx.state.record_source_counts({"followed_artists": 20})
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True)

    assert code == EXIT_OK
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]


def test_adopt_apply_is_exactly_the_plan_and_nothing_more(tmp_path: Path, sink: CapturingSink) -> None:
    """A re-run without the keep file is the accident this exists to prevent: the plan carries the keep."""
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("artist:artist-9\n")
    plan_path = tmp_path / "adopt.json"
    late = rg("rg-7", "Monitored By Hand Later", artist_mbid="artist-9", artist_name="A Stranger")
    with ctx:
        commands.adopt_command(ctx, keep_file=keep, out=plan_path, now=NOW)
        lidarr.albums["artist-9"]["rg-7"] = lidarr_album(late, id=902, artist_id=9, monitored=True)
        code = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)

    assert code == EXIT_OK
    assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]
    assert lidarr.album("artist-9", "rg-7").monitored is True, "not in the reviewed plan, so not touched"  # type: ignore[union-attr]


def test_adopt_apply_refuses_a_plan_when_lidarr_moved(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        lidarr.albums["artist-9"]["rg-9"] = replace(lidarr.albums["artist-9"]["rg-9"], monitored=False)
        writes_before = list(lidarr.writes())
        code = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        assert ctx.state.owned_releases() == {}

    assert code == EXIT_STALE
    assert lidarr.writes() == writes_before
    assert "moved" in capsys.readouterr().out


def test_adopt_apply_refuses_a_plan_when_the_sources_moved(tmp_path: Path, sink: CapturingSink) -> None:
    """Following the stranger after the plan was reviewed must not let the plan unmonitor them."""
    ctx, lidarr, source, _lookup = _adopt_world(tmp_path, sink=sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        source.snapshot = snapshot(
            artists=[
                artist_intent("Test Artist", spotify_id="sp-a1"),
                artist_intent("A Stranger", spotify_id="sp-a9"),
            ]
        )
        code = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)

    assert code == EXIT_STALE
    assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]


def test_adopt_apply_refuses_a_plan_whose_album_became_owned(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        _key, record = owned(ALBUM, reason(ReasonKind.MANUAL, "hand"), album_id=101)
        ctx.state.record_monitored([record])
        code = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)

    assert code == EXIT_STALE
    assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]


def test_adopt_apply_needs_a_plan_file_that_exists(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    with ctx, pytest.raises(DiffFileError, match="adopt"):
        commands.adopt_command(ctx, apply_path=tmp_path / "missing.json", now=NOW)


def test_adopt_apply_refuses_a_file_that_is_not_an_adopt_plan(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    path = tmp_path / "diff.json"
    path.write_text(json.dumps({"created_at": NOW.isoformat(), "add_artists": []}))
    with ctx, pytest.raises(DiffFileError):
        commands.adopt_command(ctx, apply_path=path, now=NOW)


def test_adopt_apply_takes_the_run_lock(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        with run_lock(ctx.lock_path), pytest.raises(LockHeld):
            commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]


def test_adopt_planning_takes_the_run_lock_too(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    with ctx, run_lock(ctx.lock_path), pytest.raises(LockHeld):
        commands.adopt_command(ctx, out=tmp_path / "adopt.json", now=NOW)


def test_adopt_refuses_a_keep_file_at_apply(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, lidarr, _source, _lookup = _adopt_world(tmp_path, sink=sink)
    keep = tmp_path / "keep.txt"
    keep.write_text("rg-9\n")
    plan_path = tmp_path / "adopt.json"
    with ctx:
        commands.adopt_command(ctx, out=plan_path, now=NOW)
        code = commands.adopt_command(ctx, keep_file=keep, apply_path=plan_path, now=NOW)

    assert code == EXIT_ERROR
    assert "--keep" in capsys.readouterr().out
    assert lidarr.album("artist-9", "rg-9").monitored is True  # type: ignore[union-attr]


def test_read_keep_file_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "keep.txt"
    path.write_text("# header\n\n  rg-1  \nartist:artist-2  # trailing\n")
    assert commands.read_keep_file(path) == {"rg-1", "artist:artist-2"}
    assert commands.read_keep_file(None) == set()


class _UnreadCatalogue(FakeLookup):
    """A lookup whose catalogue browse for one artist raises `error` (issue #6)."""

    def __init__(self, artist_mbid: str, error: Exception) -> None:
        super().__init__()
        self._unread_artist = artist_mbid
        self._unread_error = error

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        if artist_mbid == self._unread_artist:
            self._count("artist_release_groups")
            raise self._unread_error
        return super().artist_release_groups(artist_mbid)


HAND_MONITORED = [rg(f"rg-h{i}", f"By Hand {i}", artist_mbid="artist-2", artist_name="Prolific") for i in (1, 2, 3)]


def _unread_world(tmp_path: Path, *, sink: CapturingSink, error: Exception) -> tuple[Any, FakeLidarr]:
    """Two followed artists: "Test Artist" (artist-1), whose catalogue reads, and "Prolific"
    (artist-2), whose catalogue browse raises `error`, with three albums monitored by hand."""
    lookup = _UnreadCatalogue("artist-2", error).add(ALBUM, EP, *HAND_MONITORED)
    lookup.catalogues["artist-1"] = ["rg-1", "rg-2"]
    source = FakeSource(
        snapshot(
            artists=[
                artist_intent("Test Artist", spotify_id="sp-a1"),
                artist_intent("Prolific", spotify_id="sp-a2"),
            ]
        )
    )
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist"), lidarr_album(ALBUM, id=101, monitored=True))
    lidarr.seed(
        lidarr_artist("artist-2", id=2, name="Prolific"),
        *(lidarr_album(g, id=201 + i, artist_id=2, monitored=True) for i, g in enumerate(HAND_MONITORED)),
    )
    return make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink), lidarr


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CatalogueTooLarge("more than 3000 release groups"), "too large to browse"),
        (MetadataError("musicbrainz answered 503"), "could not be read"),
    ],
)
def test_adopt_holds_back_the_albums_of_an_artist_whose_catalogue_was_not_read(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], error: Exception, reason: str
) -> None:
    """Issue #6: none of the artist's releases reached the desired set, so every hand-monitored
    album of theirs looked unwanted and was planned for unmonitor. They are held instead, in the
    printed plan and in the plan file, and `--apply` leaves them monitored."""
    ctx, lidarr = _unread_world(tmp_path, sink=sink, error=error)
    plan_path = tmp_path / "adopt.json"
    with ctx:
        code = commands.adopt_command(ctx, out=plan_path, now=NOW)
        out = capsys.readouterr().out
        payload = json.loads(plan_path.read_text())
        applied = commands.adopt_command(ctx, apply_path=plan_path, now=NOW)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert payload["unmonitor"] == []
    assert payload["summary"] == {"claim": 1, "keep": 0, "unmonitor": 0}, "the summary keeps its shape"
    assert [(h["key"]["rg_mbid"], h["title"]) for h in payload["held"]] == [
        ("rg-h1", "By Hand 1"),
        ("rg-h2", "By Hand 2"),
        ("rg-h3", "By Hand 3"),
    ]
    assert all(reason in h["reason"] for h in payload["held"])
    assert "3 held back" in out
    held_line = next(line for line in out.splitlines() if line.strip().startswith("Prolific (3 albums):"))
    assert reason in held_line
    assert sum(line.startswith("held ") for line in out.splitlines()) == 3, "each held album is a plan row"

    assert applied == EXIT_OK
    assert all(lidarr.album("artist-2", g.mbid).monitored for g in HAND_MONITORED)  # type: ignore[union-attr]
    assert not any(k.artist_mbid == "artist-2" for k in owned), "held means not claimed either"
    assert ReleaseKey("artist-1", "rg-1") in owned, "the control artist, whose catalogue reads, is claimed"


def test_adopt_warns_loudly_at_the_top_when_musicbrainz_failed(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degraded resolve (`mb_ok` false) can leave out more than a catalogue; the warning leads."""
    ctx, _lidarr = _unread_world(tmp_path, sink=sink, error=MetadataError("musicbrainz answered 503"))
    with ctx:
        commands.adopt_command(ctx, out=tmp_path / "adopt.json", now=NOW)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("WARNING"), lines[:3]
    assert "MusicBrainz" in lines[0]
    assert any("re-run `likearr adopt` later" in line for line in lines[:4])


def test_adopt_does_not_warn_when_the_resolve_was_healthy(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """A catalogue too large to browse is permanent, not an outage: its albums are held, quietly."""
    ctx, _lidarr = _unread_world(tmp_path, sink=sink, error=CatalogueTooLarge("more than 3000 release groups"))
    with ctx:
        commands.adopt_command(ctx, out=tmp_path / "adopt.json", now=NOW)

    assert "WARNING" not in capsys.readouterr().out


def test_an_adopt_plan_file_without_held_releases_still_reads(tmp_path: Path) -> None:
    """A plan written before #6 has no `held` field; it reads as holding nothing."""
    from likearr.shell.adopt_io import read_adopt_plan

    path = tmp_path / "adopt.json"
    path.write_text(
        json.dumps(
            {
                "kind": "adopt-plan",
                "created_at": NOW.isoformat(),
                "source_digest": "s",
                "lidarr_digest": "l",
                "resolver_version": 1,
                "summary": {"claim": 0, "keep": 0, "unmonitor": 0},
                "claim": [],
                "keep_as_manual": [],
                "unmonitor": [],
            }
        )
    )
    assert read_adopt_plan(path).adoption.held == []


# --------------------------------------------------------------------------- explain


def test_explain_writes_no_state(tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = commands.explain_command(ctx, "First Album", now=NOW)
        assert ctx.state.cached_resolution("followed:sp-a1", 1) is None

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "First Album" in out
    assert lidarr.writes() == []


def test_explain_json_is_one_line_with_the_summary_and_the_detail(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = commands.explain_command(ctx, "First Album", now=NOW, as_json=True)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert code == EXIT_OK
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert report["query"] == "First Album"
    assert report["summary"] and "text" in report["summary"][0]
    assert "links" in report["summary"][0]
    assert "First Album" in report["details"]


def test_explain_names_playlists_from_the_names_file(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.playlist_names import read_names, write_names

    names_file = tmp_path / "ui" / "playlist-names.json"
    write_names(names_file, {"pl-1": "Road trip"}, fetched_at=NOW)

    assert read_names(names_file).names == {"pl-1": "Road trip"}
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert commands.explain_command(ctx, "First Album", now=NOW, names_file=names_file) == EXIT_OK


def test_explain_from_the_last_run_asks_nobody(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.shell.run import run_command

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        capsys.readouterr()
        reads, calls = source.reads, len(lidarr.calls)

        code = commands.explain_command(ctx, "Test Artist", from_last_run=True)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert source.reads == reads and len(lidarr.calls) == calls
    assert out.startswith("As of the last run, 2026-")
    assert "an apply" in out
    assert "You follow Test Artist on Spotify" in out


def test_explain_from_the_last_run_as_json_says_when(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.shell.run import run_command

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        capsys.readouterr()
        assert commands.explain_command(ctx, "First Album", from_last_run=True, as_json=True) == EXIT_OK

    report = json.loads(capsys.readouterr().out)
    assert report["as_of"] == NOW.isoformat()
    assert report["applied"] is False
    assert report["summary"]


def test_explain_from_the_last_run_with_no_run_recorded_says_what_to_do(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    with make_context(tmp_path, sink=sink) as ctx:
        code = commands.explain_command(ctx, "Test Artist", from_last_run=True)

    assert code == EXIT_ERROR
    assert "no run has been recorded" in capsys.readouterr().out


# --------------------------------------------------------------------------- auth: the six-month clock


def test_auth_without_spotify_configured_is_refused(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    with make_context(tmp_path, sink=sink) as ctx:
        ctx.spotify_error = "LIKEARR_SPOTIFY_CLIENT_ID is not set"
        code = commands.auth_command(ctx)

    assert code == EXIT_ERROR
    assert "spotify is not configured" in capsys.readouterr().out


class _ScriptedAuth(SpotifyAuth):
    """A `SpotifyAuth` whose browser round trip and token exchange are scripted."""

    def build_authorize_url(
        self, *, redirect_uri: str | None = None, include_write: bool = False
    ) -> tuple[str, str, str]:
        return "https://accounts.spotify.test/authorize?fake", "fake-verifier", "fake-state"

    def run_local_callback_server(
        self, redirect_uri: str, timeout_s: float = 300.0, *, expected_state: str
    ) -> tuple[str, str]:
        assert expected_state == "fake-state"
        return "fake-code", "fake-state"

    def exchange_code(self, code: str, verifier: str, *, redirect_uri: str | None = None) -> TokenSet:
        return TokenSet(
            access_token="fake-access-new",
            refresh_token="fake-refresh-new",
            expires_at=NOW.timestamp() + 3600,
            scope="user-follow-read",
            authorized_at=datetime(2026, 9, 18, tzinfo=UTC).timestamp(),
        )


def test_auth_prints_when_reauth_will_be_due(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "_spotify_me", lambda ctx: SpotifyAccount("fake-user", "Test User"))
    with make_context(tmp_path, sink=sink) as ctx:
        ctx.auth = _ScriptedAuth(ctx.config.spotify, httpx.Client())
        code = commands.auth_command(ctx)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "re-auth due: 2027-03-18" in out


class _RealUrlAuth(_ScriptedAuth):
    """The real authorize URL (so its `scope` is what Spotify would be asked for), with the state
    pinned so the scripted callback matches it."""

    def build_authorize_url(
        self, *, redirect_uri: str | None = None, include_write: bool = False
    ) -> tuple[str, str, str]:
        url, verifier, _state = SpotifyAuth.build_authorize_url(
            self, redirect_uri=redirect_uri, include_write=include_write
        )
        return url, verifier, "fake-state"


def _asked_scopes(out: str) -> list[str]:
    """The `scope` of the one authorize URL `likearr auth` printed."""
    import urllib.parse

    url = next(line.strip() for line in out.splitlines() if "accounts.spotify.com/authorize" in line)
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0].split()


def _auth_printing_the_url(
    tmp_path: Path,
    sink: CapturingSink,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_scope: str | None,
    **kwargs: Any,
) -> tuple[int, str]:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    monkeypatch.setattr(commands, "_spotify_me", lambda ctx: SpotifyAccount("fake-user", "Test User"))
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: "http://127.0.0.1:8765/callback?code=fake-code&state=fake-state"
    )
    with make_context(tmp_path, sink=sink) as ctx:
        if token_scope is not None:
            ctx.config.spotify.token_file.write_text(json.dumps(token_file_data(scope=token_scope)))
        ctx.auth = _RealUrlAuth(ctx.config.spotify, httpx.Client())
        code = commands.auth_command(ctx, **kwargs)
    return code, capsys.readouterr().out


READ = ["user-follow-read", "user-library-read", "playlist-read-private", "playlist-read-collaborative"]
WRITE = ["user-follow-modify", "user-library-modify"]


class _NoStateAuth(_ScriptedAuth):
    def run_local_callback_server(
        self, redirect_uri: str, timeout_s: float = 300.0, *, expected_state: str
    ) -> tuple[str, str]:
        return "fake-code", ""

    def exchange_code(self, code: str, verifier: str, *, redirect_uri: str | None = None) -> TokenSet:
        raise AssertionError("a callback with no state must never be exchanged")


@pytest.mark.parametrize("manual", [False, True])
def test_auth_refuses_a_callback_with_no_state(
    tmp_path: Path,
    sink: CapturingSink,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    manual: bool,
) -> None:
    """#171: a code with no state used to pass; the web flow already refused it."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "http://127.0.0.1:8765/callback?code=fake-code")
    with make_context(tmp_path, sink=sink) as ctx:
        ctx.auth = _NoStateAuth(ctx.config.spotify, httpx.Client())
        code = commands.auth_command(ctx, manual=manual)

    assert code == EXIT_ERROR
    assert "the 'state' parameter did not match; the callback did not come from this run" in capsys.readouterr().out
    assert not ctx.config.spotify.token_file.exists()


def test_auth_manual_asks_a_new_user_for_read_scopes_only(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#161: no token file yet - a new user - so the consent screen asks to read, nothing more,
    and says how to add promote-save's write access."""
    code, out = _auth_printing_the_url(tmp_path, sink, capsys, monkeypatch, token_scope=None, manual=True)

    assert code == EXIT_OK
    assert _asked_scopes(out) == READ
    assert "read-only" in out
    assert "--promote-save" in out


def test_auth_manual_promote_save_asks_for_the_write_scopes_too(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _auth_printing_the_url(
        tmp_path, sink, capsys, monkeypatch, token_scope=None, manual=True, promote_save=True
    )

    assert code == EXIT_OK
    assert _asked_scopes(out) == [*READ, *WRITE]


def test_auth_keeps_the_write_scopes_a_token_file_already_has(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#161 decision (a): a plain re-authorization never quietly drops write access you approved."""
    code, out = _auth_printing_the_url(
        tmp_path, sink, capsys, monkeypatch, token_scope=" ".join([*READ, *WRITE]), manual=True
    )

    assert code == EXIT_OK
    assert _asked_scopes(out) == [*READ, *WRITE]
    assert "already has" in out


def test_auth_re_authorizing_a_read_only_token_stays_read_only(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _auth_printing_the_url(tmp_path, sink, capsys, monkeypatch, token_scope=" ".join(READ))

    assert code == EXIT_OK
    assert _asked_scopes(out) == READ


def auth_against(ctx: Context, spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run `likearr auth` with the browser scripted and `GET /me` served by `spotify`."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    ctx.config.spotify.token_file.write_text(json.dumps(token_file_data()))
    transport = httpx.MockTransport(spotify.handler)
    ctx.auth = _ScriptedAuth(ctx.config.spotify, httpx.Client(transport=transport), sleep=lambda _s: None)
    monkeypatch.setattr(commands, "build_client", lambda: httpx.Client(transport=transport))
    return commands.auth_command(ctx)


def test_auth_names_the_quota_when_get_me_meets_it_and_asks_once(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /me` used to retry a quota answer four times and then say only "could not confirm"."""
    spotify = FakeSpotify({"/v1/me": httpx.Response(429, json=QUOTA_BODY)})
    with make_context(tmp_path, sink=sink) as ctx:
        code = auth_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_OK, "the token was written and works; only the account check was refused"
    assert "PASS  token written" in out
    [line] = [x for x in out.splitlines() if x.startswith("WARN  spotify quota:")]
    assert "GET /me" in line and "QUOTA_EXCEEDED" in line
    assert "Spotify sent no Retry-After" in line
    assert "zero unmonitors" in line
    assert line.endswith("The token was still written")
    assert spotify.api_calls() == ["/v1/me"], "a quota answer is asked for once"


def test_auth_names_the_token_refresh_when_a_401_s_refresh_meets_the_quota(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = FakeSpotify(
        {
            "/v1/me": httpx.Response(401, json={"error": {"status": 401}}),
            "/api/token": httpx.Response(429, json=QUOTA_BODY),
        }
    )
    with make_context(tmp_path, sink=sink) as ctx:
        code = auth_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "WARN  spotify quota: Spotify answered the token refresh request with 429 QUOTA_EXCEEDED" in out
    assert spotify.api_calls() == ["/v1/me"]


def test_auth_gives_spotify_s_retry_after_for_the_quota(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = FakeSpotify({"/v1/me": httpx.Response(429, headers={"Retry-After": "7200"}, json=QUOTA_BODY)})
    with make_context(tmp_path, sink=sink) as ctx:
        auth_against(ctx, spotify, monkeypatch)

    assert "Retry-After: 7200 s (about 2 h)" in capsys.readouterr().out
    assert spotify.api_calls() == ["/v1/me"]


def test_auth_says_why_get_me_failed(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other failure is still a WARN, but no longer a silent one."""
    spotify = FakeSpotify({"/v1/me": httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})})
    with make_context(tmp_path, sink=sink) as ctx:
        code = auth_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "WARN  could not confirm the account with GET /me (" in out
    assert "HTTP 403" in out
    assert "fake-access" not in out, "the reason never carries a token"


def test_auth_warns_rather_than_crashing_when_the_token_lock_cannot_be_taken(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is already written by then; a lock-file error must not turn that into exit 1."""
    spotify = FakeSpotify()
    with make_context(tmp_path, sink=sink) as ctx:
        ctx.config.spotify.token_file.write_text(json.dumps(token_file_data()))
        transport = httpx.MockTransport(spotify.handler)
        ctx.auth = _ScriptedAuth(ctx.config.spotify, httpx.Client(transport=transport), sleep=lambda _s: None)
        monkeypatch.setattr(commands, "build_client", lambda: httpx.Client(transport=transport))

        def no_lock() -> str:
            raise PermissionError("token lock file is read-only")

        monkeypatch.setattr(ctx.auth, "access_token", no_lock)
        code = commands.auth_command(ctx)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "WARN  could not confirm the account with GET /me (token lock file is read-only)" in out
    assert spotify.api_calls() == []


def test_auth_names_the_account_get_me_returns(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    spotify = FakeSpotify({"/v1/me": httpx.Response(200, json={"display_name": "Test User", "id": "fake-user"})})
    with make_context(tmp_path, sink=sink) as ctx:
        code = auth_against(ctx, spotify, monkeypatch)

    assert code == EXIT_OK
    assert "account: Test User (fake-user)" in capsys.readouterr().out


def test_auth_records_the_account_in_the_token_file(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings reads "Connected as" from the token file."""
    spotify = FakeSpotify({"/v1/me": httpx.Response(200, json={"display_name": "Test User", "id": "fake-other"})})
    with make_context(tmp_path, sink=sink) as ctx:
        auth_against(ctx, spotify, monkeypatch)
        on_disk = json.loads(ctx.config.spotify.token_file.read_text())

    assert (on_disk["user_id"], on_disk["display_name"]) == ("fake-other", "Test User")


# --------------------------------------------------------------------------- playlists


MINE = OwnedPlaylist(id="pl-mine-0000000000000a", name="Mine", track_count=12)
ALSO_MINE = OwnedPlaylist(id="pl-mine-0000000000000b", name="Road Trip", track_count=0)
NOT_MINE = PlaylistEntry(id="pl-other-000000000000c", name="Discover Weekly", track_count=30, owned=False)
SHARED = PlaylistEntry(
    id="pl-shared-00000000000d",
    name="Band Van",
    track_count=8,
    owned=False,
    collaborative=True,
    collaborative_scope=True,
)
SHARED_BEFORE_REAUTH = PlaylistEntry(
    id="pl-shared-00000000000e", name="Old Share", track_count=5, owned=False, collaborative=True
)


def playlists_context(tmp_path: Path, sink: CapturingSink, *, configured: tuple[str, ...], library: FakeLibrary):
    config = make_config(tmp_path)
    config = replace(config, spotify=replace(config.spotify, playlists=configured))
    return make_context(tmp_path, sink=sink, config=config, library=library)


def test_playlists_json_is_one_line_in_the_documented_shape(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    library = FakeLibrary(playlists=[MINE, ALSO_MINE])
    with playlists_context(tmp_path, sink, configured=(MINE.id, "pl-gone-000000000000000"), library=library) as ctx:
        code = commands.playlists_command(ctx, as_json=True)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert out.count("\n") == 1, "exactly one line: the web server parses it"
    assert json.loads(out) == {
        "playlists": [
            {"id": MINE.id, "name": "Mine", "track_count": 12, "owned": True, "readable": True, "needs_reauth": False},
            {
                "id": ALSO_MINE.id,
                "name": "Road Trip",
                "track_count": 0,
                "owned": True,
                "readable": True,
                "needs_reauth": False,
            },
        ],
        "configured": [MINE.id, "pl-gone-000000000000000"],
        "missing": ["pl-gone-000000000000000"],
    }
    assert library.writes() == [], "listing playlists writes nothing"


def test_playlists_json_includes_unowned_entries_marked_not_owned(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """A followed, collaborative or Spotify-owned playlist is listed too - just not selectable,
    because Development Mode returns no items for it (issue #103, item 1)."""
    library = FakeLibrary(playlists=[MINE], unowned_playlists=[NOT_MINE])
    with playlists_context(tmp_path, sink, configured=(MINE.id,), library=library) as ctx:
        code = commands.playlists_command(ctx, as_json=True)

    out = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert {
        "id": NOT_MINE.id,
        "name": "Discover Weekly",
        "track_count": 30,
        "owned": False,
        "readable": False,
        "needs_reauth": False,
    } in out["playlists"]
    assert out["missing"] == [], "a playlist Spotify lists, even unowned, is not 'missing'"


def test_playlists_json_with_nothing_configured(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    with playlists_context(tmp_path, sink, configured=(), library=FakeLibrary()) as ctx:
        assert commands.playlists_command(ctx, as_json=True) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"playlists": [], "configured": [], "missing": []}


def test_playlists_table_marks_the_configured_and_warns_about_the_missing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    library = FakeLibrary(playlists=[MINE, ALSO_MINE])
    with playlists_context(tmp_path, sink, configured=(MINE.id, "pl-gone-000000000000000"), library=library) as ctx:
        code = commands.playlists_command(ctx, as_json=False)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    lines = out.splitlines()
    mine = next(line for line in lines if MINE.id in line)
    road_trip = next(line for line in lines if ALSO_MINE.id in line)
    assert mine.startswith("*") and "Mine" in mine and "12" in mine
    assert not road_trip.startswith("*") and "Road Trip" in road_trip
    assert any(line.startswith("WARN") and "pl-gone-000000000000000" in line for line in lines)


def test_playlists_table_lists_unowned_entries_separately_with_the_reason(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    library = FakeLibrary(playlists=[MINE], unowned_playlists=[NOT_MINE])
    with playlists_context(tmp_path, sink, configured=(NOT_MINE.id,), library=library) as ctx:
        code = commands.playlists_command(ctx, as_json=False)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Discover Weekly" in out
    assert "Spotify doesn't share this playlist's songs with a personal app" in out
    assert "like the songs you want, or copy them into a playlist you own" in out
    assert any(
        line.startswith("WARN") and NOT_MINE.id in line and "not owned by you" in line for line in out.splitlines()
    )


def test_playlists_json_marks_a_collaborative_playlist_readable_or_needing_a_reauth(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """#103 item 3: someone else's collaborative playlist is readable once the token has
    playlist-read-collaborative, and marked needs_reauth before that - the web picker offers the
    first and says "re-authorize" for the second."""
    library = FakeLibrary(playlists=[MINE], unowned_playlists=[SHARED, SHARED_BEFORE_REAUTH])
    with playlists_context(tmp_path, sink, configured=(), library=library) as ctx:
        assert commands.playlists_command(ctx, as_json=True) == EXIT_OK

    by_id = {p["id"]: p for p in json.loads(capsys.readouterr().out)["playlists"]}
    assert (by_id[SHARED.id]["owned"], by_id[SHARED.id]["readable"], by_id[SHARED.id]["needs_reauth"]) == (
        False,
        True,
        False,
    )
    assert by_id[SHARED_BEFORE_REAUTH.id]["readable"] is False
    assert by_id[SHARED_BEFORE_REAUTH.id]["needs_reauth"] is True


def test_playlists_table_lists_a_collaborative_playlist_as_readable_and_an_old_one_as_needing_a_reauth(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    library = FakeLibrary(playlists=[MINE], unowned_playlists=[SHARED, SHARED_BEFORE_REAUTH, NOT_MINE])
    with playlists_context(tmp_path, sink, configured=(SHARED_BEFORE_REAUTH.id,), library=library) as ctx:
        assert commands.playlists_command(ctx, as_json=False) == EXIT_OK

    lines = capsys.readouterr().out.splitlines()
    reauth_header = next(i for i, line in enumerate(lines) if line.startswith("Not readable yet ("))
    other_header = next(i for i, line in enumerate(lines) if line.startswith("Not readable ("))
    shared = next(i for i, line in enumerate(lines) if SHARED.id in line)
    old = next(i for i, line in enumerate(lines) if SHARED_BEFORE_REAUTH.id in line)
    assert shared < reauth_header < old < other_header, "readable first, then needs-a-reauth, then the rest"
    assert "(collaborative)" in lines[shared]
    assert "re-authorize Spotify" in lines[reauth_header]
    assert "2 readable, 2 not readable" in "\n".join(lines)
    warn = next(line for line in lines if line.startswith("WARN") and SHARED_BEFORE_REAUTH.id in line)
    assert "re-authorize Spotify" in warn and "not owned by you" not in warn


@pytest.mark.parametrize(
    ("scopes", "hinted"), [(frozenset(), True), (frozenset({"playlist-read-collaborative"}), False)]
)
def test_a_missing_playlist_suggests_a_reauth_only_when_the_token_lacks_the_collaborative_scope(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], scopes: frozenset[str], hinted: bool
) -> None:
    """#103 item 3: a token without playlist-read-collaborative may not be shown someone else's
    collaborative playlist at all, so "deleted, or the wrong id" alone would mislead."""
    library = FakeLibrary(scopes=scopes, playlists=[MINE])
    with playlists_context(tmp_path, sink, configured=("pl-gone-000000000000000",), library=library) as ctx:
        assert commands.playlists_command(ctx, as_json=False) == EXIT_OK

    warn = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("WARN"))
    assert "deleted, or the wrong id" in warn
    assert ("re-authorize Spotify" in warn) is hinted


@pytest.mark.parametrize("as_json", [True, False])
def test_playlists_source_error_is_one_line_and_exit_1(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    library = FakeLibrary(playlists_error=SourceError("spotify playlists: still unauthorized - run `likearr auth`"))
    with playlists_context(tmp_path, sink, configured=(), library=library) as ctx:
        code = commands.playlists_command(ctx, as_json=as_json)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert out.count("\n") == 1, out
    assert "still unauthorized" in out


def test_playlists_without_spotify_is_one_line_and_exit_1(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    with make_context(tmp_path, sink=sink, library=None) as ctx:
        ctx.spotify_error = "LIKEARR_SPOTIFY_CLIENT_ID is not set"
        code = commands.playlists_command(ctx, as_json=True)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert out.count("\n") == 1, out
    assert "spotify is not configured" in out


# --------------------------------------------------------------------------- lidarr-files


def _unmonitor_plan(tmp_path: Path) -> Path:
    from likearr.models import UnmonitorRelease
    from likearr.shell.diff_io import write_diff
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.unmonitor[:] = [
        UnmonitorRelease(ReleaseKey("artist-1", rg), rg, frozenset()) for rg in ("rg-1", "rg-2", "rg-gone")
    ]
    path = tmp_path / "diff.json"
    write_diff(diff, path)
    return path


def test_lidarr_files_counts_what_stays_on_disk_and_writes_nothing_to_lidarr(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    lidarr = FakeLidarr().seed(
        lidarr_artist("artist-1", id=7),
        lidarr_album(ALBUM, id=1, artist_id=7, monitored=True, files=12, size=500),
        lidarr_album(EP, id=2, artist_id=7, monitored=True),
    )
    out = tmp_path / "files.json"
    with make_context(tmp_path, sink=sink, lidarr=lidarr) as ctx:
        code = commands.lidarr_files_command(ctx, plan_file=_unmonitor_plan(tmp_path), out=out, as_json=True)

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert printed.count("\n") == 1, "exactly one line: the web server may parse it"
    expected = {
        "albums": {"rg-1": {"track_files": 12, "size_on_disk": 500}, "rg-2": {"track_files": 0, "size_on_disk": 0}},
        "missing": ["rg-gone"],
    }
    assert json.loads(printed) == expected
    assert json.loads(out.read_text()) == expected
    assert lidarr.writes() == [] and lidarr.names() == ["load_view"]


def test_lidarr_files_says_fail_and_writes_no_answer_when_lidarr_fails(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.ports import LidarrError

    class DownLidarr(FakeLidarr):
        def load_view(self, artist_mbids: Any = None) -> Any:
            raise LidarrError("lidarr GET /artist: connection refused")

    lidarr = DownLidarr()
    out = tmp_path / "files.json"
    with make_context(tmp_path, sink=sink, lidarr=lidarr) as ctx:
        code = commands.lidarr_files_command(ctx, plan_file=_unmonitor_plan(tmp_path), out=out, as_json=True)

    printed = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert printed.startswith("FAIL") and printed.count("\n") == 1
    assert not out.exists()


def test_lidarr_files_refuses_a_plan_that_does_not_read(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bad.json").write_text("{")
    with make_context(tmp_path, sink=sink) as ctx:
        assert commands.lidarr_files_command(ctx, plan_file=tmp_path / "bad.json") == EXIT_ERROR
    assert capsys.readouterr().out.startswith("FAIL")
