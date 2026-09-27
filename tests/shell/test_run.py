"""`shell.run`: plan, apply, and everything that is supposed to go wrong safely."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import h11
import httpx
import pytest
import respx

from likearr.adapters.health import MqttSink, WebhookSink
from likearr.adapters.http import build_client
from likearr.adapters.lidarr import LidarrClient
from likearr.config import MqttSinkConfig, ScheduleConfig, WebhookSinkConfig
from likearr.logging_setup import setup_logging
from likearr.models import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_GUARDED,
    EXIT_OK,
    EXIT_STALE,
    RESOLVER_VERSION,
    HealthRecord,
    LidarrView,
    OwnedArtist,
    PrimaryType,
    Profile,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    RunStatus,
    SecondaryType,
)
from likearr.ports import CatalogueTooLarge, LidarrError, MetadataError, SourceError
from likearr.shell.apply import _refresh_timeout_s
from likearr.shell.context import Context
from likearr.shell.diff_io import diff_summary, read_diff, write_diff
from likearr.shell.plan import resolution_max_age
from likearr.shell.run import apply, plan, print_plan, run_command, tagged_without_state
from tests.shell.conftest import (
    ALBUMS_ONLY_TAG_ID,
    FULL_ID,
    LEAN_ID,
    NOW,
    TAG_ID,
    CapturingSink,
    FakeLidarr,
    FakeSource,
    make_config,
    make_context,
)
from tests.unit.fakes import (
    FakeLookup,
    album_intent,
    artist_intent,
    lidarr_album,
    lidarr_artist,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)

ALBUM = rg("rg-1", "First Album")
EP = rg("rg-2", "An EP", primary=PrimaryType.EP)
LIVE = rg("rg-3", "Live At Somewhere", secondary=[SecondaryType.LIVE])


def followed_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """One followed artist whose catalogue is an album and an EP, and an empty Lidarr."""
    lookup = FakeLookup().add(ALBUM, EP)
    lookup.catalogues["artist-1"] = ["rg-1", "rg-2"]
    source = FakeSource(snapshot(artists=[artist_intent("Test Artist", spotify_id="sp-a1")]))
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    return source, lookup, lidarr


NEW_ALBUM = rg("rg-new", "Just Out", released="2026-09-11")
"""A week before `NOW`: the album a followed artist has just released."""


def new_release_world(*, lidarr_gets_it: bool = True) -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """Issue #8: MusicBrainz has the artist's new album; Lidarr's catalogue does not yet.

    `lidarr_gets_it` decides whether a RefreshArtist actually brings the album in, which is the
    difference between "Lidarr was simply behind" and "Lidarr's metadata is stuck on this artist".
    """
    lookup = FakeLookup().add(ALBUM, NEW_ALBUM)
    lookup.catalogues["artist-1"] = ["rg-1", "rg-new"]
    source = FakeSource(snapshot(artists=[artist_intent("Test Artist", spotify_id="sp-a1")]))
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, NEW_ALBUM] if lidarr_gets_it else [ALBUM]})
    lidarr.seed(lidarr_artist("artist-1"), lidarr_album(ALBUM, monitored=True))
    return source, lookup, lidarr


def context_for(tmp_path: Path, sink: CapturingSink, **kwargs: object):
    source, lookup, lidarr = followed_world()
    return make_context(
        tmp_path,
        source=kwargs.get("source", source),  # type: ignore[arg-type]
        lookup=kwargs.get("lookup", lookup),  # type: ignore[arg-type]
        lidarr=kwargs.get("lidarr", lidarr),  # type: ignore[arg-type]
        sink=sink,
    )


# --------------------------------------------------------------------------- plan


def test_plan_wants_the_whole_followed_catalogue(tmp_path: Path, sink: CapturingSink) -> None:
    with context_for(tmp_path, sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)

    assert [a.artist_mbid for a in result.diff.add_artists] == ["artist-1"]
    assert sorted(m.key.rg_mbid for m in result.diff.monitor) == ["rg-1", "rg-2"]
    assert result.diff.unmonitor == []
    assert result.diff.guards == []


def test_plan_writes_the_diff_and_publishes_a_dry_run_record(tmp_path: Path, sink: CapturingSink) -> None:
    out = tmp_path / "diff.json"
    with context_for(tmp_path, sink) as ctx:
        code = run_command(ctx, now=NOW, out=out, do_apply=False)

    assert code == EXIT_OK
    assert sink.last.dry_run is True
    assert sink.last.status is RunStatus.OK
    assert sink.last.counts["monitored"] == 2
    assert sink.last.counts["added"] == 1
    reloaded = read_diff(out)
    assert sorted(m.key.rg_mbid for m in reloaded.monitor) == ["rg-1", "rg-2"]
    assert reloaded.source_digest


def test_a_dry_run_never_moves_the_shrink_baseline(tmp_path: Path, sink: CapturingSink) -> None:
    """Recording source counts on a dry run would disarm the next run's shrink guard."""
    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        assert ctx.state.last_source_counts() == {}
        assert ctx.state.last_followed_counts() == {}


def test_a_dry_run_caches_resolutions(tmp_path: Path, sink: CapturingSink) -> None:
    with context_for(tmp_path, sink) as ctx:
        plan(ctx, now=NOW, scheduled=False)
        cached = ctx.state.cached_resolution("saved:sp-album", 1)
    assert cached is None  # nothing saved in this world; the followed artist is not cached


def test_plan_does_not_write_state_when_persist_is_false(tmp_path: Path, sink: CapturingSink) -> None:
    saved = spotify_album("First Album", spotify_id="sp-alb", upc="111")
    lookup = FakeLookup().add(ALBUM, EP)
    lookup.barcodes["111"] = "rg-1"
    source = FakeSource(snapshot(albums=[album_intent(saved)]))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx:
        plan(ctx, now=NOW, scheduled=False, persist=False)
        assert ctx.state.cached_resolution("saved:sp-alb", RESOLVER_VERSION) is None
        plan(ctx, now=NOW, scheduled=False, persist=True)
        assert ctx.state.cached_resolution("saved:sp-alb", RESOLVER_VERSION) is not None


def _saved_album_world(tmp_path: Path, sink: CapturingSink, lookup: FakeLookup) -> Context:
    saved = spotify_album("First Album", spotify_id="sp-alb", upc="111")
    source = FakeSource(snapshot(albums=[album_intent(saved)]))
    return make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink)


def test_a_cached_answer_expires_and_picks_up_a_musicbrainz_correction(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #165, end to end: a young cached answer is reused with no lookup; one past its jittered
    max age (at least 4/3 of `positive_cache_days`, so 120 days by default) is looked up again."""
    corrected = rg("rg-fixed", "First Album")
    lookup = FakeLookup().add(ALBUM, corrected)
    lookup.barcodes["111"] = "rg-1"
    with _saved_album_world(tmp_path, sink, lookup) as ctx:
        plan(ctx, now=NOW, scheduled=False)
        lookup.barcodes["111"] = "rg-fixed"  # MusicBrainz corrects the barcode

        lookup.calls.clear()
        plan(ctx, now=NOW + timedelta(days=100), scheduled=False)
        young = ctx.state.cached_resolution("saved:sp-alb", RESOLVER_VERSION)
        assert lookup.calls.get("release_groups_by_barcode") is None, "young: reused, not asked"

        plan(ctx, now=NOW + timedelta(days=160), scheduled=False)
        old = ctx.state.cached_resolution("saved:sp-alb", RESOLVER_VERSION)

    assert young is not None and young.release_group is not None
    assert young.release_group.mbid == "rg-1"
    assert old is not None and old.release_group is not None
    assert old.release_group.mbid == "rg-fixed"
    assert old.checked_at == NOW + timedelta(days=160)


def test_an_expired_single_fallback_is_never_unmonitored(tmp_path: Path, sink: CapturingSink) -> None:
    """#165 review, end to end: a liked song settled on its undated single by the pending clock is
    applied, its clock cleared; when the answer expires the re-check keeps the single, so the
    second apply unmonitors nothing and nothing goes back to waiting."""
    from tests.unit.fakes import track_intent

    single = rg("rg-single", "A Song", primary=PrimaryType.SINGLE, released=None)
    spotify_single = spotify_album("A Song", spotify_id="sp-single", upc="333", album_type="single", released=None)
    lookup = FakeLookup().add(single)
    lookup.barcodes["333"] = "rg-single"
    source = FakeSource(snapshot(tracks=[track_intent("A Song", spotify_single, spotify_id="sp-t1")]))
    later = NOW + timedelta(days=200)

    lidarr = FakeLidarr(catalogue={"artist-1": [single]})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.mark_pending("liked:sp-t1", NOW - timedelta(days=200))
        first_path = tmp_path / "first.json"
        run_command(ctx, now=NOW, out=first_path)
        assert run_command(ctx, now=NOW, out=first_path, apply_path=first_path, do_apply=True) == EXIT_OK
        owned_first = set(ctx.state.owned_releases())
        assert ctx.state.pending_since("liked:sp-t1") is None, "settling cleared the clock"

        lookup.calls.clear()
        second_path = tmp_path / "second.json"
        run_command(ctx, now=later, out=second_path)
        assert lookup.calls != {}, "the answer was due and was looked up again"
        assert run_command(ctx, now=later, out=second_path, apply_path=second_path, do_apply=True) == EXIT_OK
        cached = ctx.state.cached_resolution("liked:sp-t1", RESOLVER_VERSION)
        owned_second = set(ctx.state.owned_releases())

    first, second = read_diff(tmp_path / "first.json"), read_diff(tmp_path / "second.json")
    assert [m.key.rg_mbid for m in first.monitor] == ["rg-single"]
    assert owned_first, "the single was applied and is owned"
    assert second.unmonitor == []
    assert second.pending == []
    assert owned_second == owned_first
    assert cached is not None and cached.step == "track:single-fallback"
    assert cached.checked_at == later


def test_the_resolution_max_age_is_jittered_by_key_within_its_band() -> None:
    max_age = resolution_max_age(90)
    ages = {key: max_age(key) for key in (f"liked:track-{n}" for n in range(200))}

    assert all(timedelta(days=120) <= age <= timedelta(days=150) for age in ages.values())
    assert max_age("liked:track-1") == resolution_max_age(90)("liked:track-1"), "stable across runs"
    assert len({age.days for age in ages.values()}) > 20, "spread over weeks, not one night"


def test_a_zero_positive_cache_age_checks_every_answer_every_run() -> None:
    """`positive_cache_days = 0` asks MusicBrainz every time; a cached answer follows it."""
    assert resolution_max_age(0)("liked:track-1") == timedelta(0)


def test_source_error_stops_before_any_lidarr_call(tmp_path: Path, sink: CapturingSink) -> None:
    _source, lookup, lidarr = followed_world()
    source = FakeSource(error=SourceError("spotify: QUOTA_EXCEEDED"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True)

    assert code == EXIT_ERROR
    assert lidarr.calls == []
    assert sink.last.spotify_ok is False
    assert sink.last.status is RunStatus.ERROR
    assert "QUOTA_EXCEEDED" in sink.last.message


RAW_NEWLINE_KEY_BODY = "fake-lidarr-key-0123456789"
LEAK_HOOK = "https://hook.test/likearr"


def _h11_refuses_the_headers(request: httpx.Request) -> httpx.Response:
    """What the real transport does with a header value ending in CR or LF: h11 refuses it before
    anything is sent, and httpx re-raises its message - which quotes the value - unchanged."""
    try:
        h11.Request(method=request.method, target=request.url.raw_path, headers=list(request.headers.raw))
    except h11.LocalProtocolError as exc:
        raise httpx.LocalProtocolError(str(exc), request=request) from exc
    raise AssertionError("h11 accepted the header, so this test would prove nothing")


@respx.mock
def test_a_lidarr_key_with_a_raw_newline_reaches_no_error_log_webhook_or_mqtt_payload(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], preserve_root_logging: None
) -> None:
    """Issue #7. Config strips the key when it reads it; this builds the client with the raw key to
    prove the redaction behind that stripping holds on its own, all the way to every sink.

    Logging is set up as `likearr run -v` sets it up, after `capsys`, so every log line lands on
    the captured stderr through likearr's own handler.
    """
    setup_logging(verbose=True)
    logging.getLogger("likearr.test").debug("capture check")
    source, lookup, _lidarr = followed_world()
    respx.route(host="lidarr.test").mock(side_effect=_h11_refuses_the_headers)
    webhook = respx.post(LEAK_HOOK).mock(return_value=httpx.Response(200))
    mqtt_client = MagicMock()
    http = build_client()
    lidarr = LidarrClient(
        make_config(tmp_path).lidarr, http, api_key=RAW_NEWLINE_KEY_BODY + "\n", sleep=lambda _s: None
    )
    sinks = [
        sink,
        WebhookSink(WebhookSinkConfig(url=LEAK_HOOK)),
        MqttSink(MqttSinkConfig(host="mqtt.test", topic="likearr/health")),
    ]

    with (
        patch("likearr.adapters.health.mqtt.Client", return_value=mqtt_client),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=sinks) as ctx,  # type: ignore[arg-type]
    ):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
    http.close()

    assert code == EXIT_ERROR
    assert "LocalProtocolError" in sink.last.message, "the transport-error path was not the one taken"
    assert webhook.called
    mqtt_client.publish.assert_called_once()
    _topic, mqtt_payload = mqtt_client.publish.call_args[0]
    captured = capsys.readouterr()
    assert "capture check" in captured.err, "stderr was not captured, so this test would prove nothing"
    assert "LocalProtocolError" in captured.err, "the error was never logged"
    seen = {
        "error": sink.last.message,
        "log (stderr)": captured.err,
        "stdout": captured.out,
        "webhook": webhook.calls.last.request.content.decode(),
        "mqtt": str(mqtt_payload),
    }
    assert not {where for where, text in seen.items() if RAW_NEWLINE_KEY_BODY in text}


def test_a_dry_run_records_what_it_saw_for_explain(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell.last_run import read_last_run

    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    last = read_last_run(tmp_path / "last-run.json")
    assert last is not None
    assert last.applied is False
    assert last.ran_at == NOW
    assert {k.rg_mbid for k in last.desired.releases} == {"rg-1", "rg-2"}
    assert [a.name for a in last.snapshot.artists] == ["Test Artist"]


def test_an_apply_records_the_view_after_its_changes(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell.last_run import read_last_run

    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    last = read_last_run(tmp_path / "last-run.json")
    assert last is not None
    assert last.applied is True
    assert "artist-1" in last.view.artists
    assert last.view.album(ReleaseKey("artist-1", "rg-1")).monitored  # type: ignore[union-attr]


def test_failing_to_record_for_explain_changes_nothing_about_the_run(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likearr.shell.last_run as last_run

    def refuse(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(last_run, "write_last_run", refuse)
    with context_for(tmp_path, sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.OK
    assert not (tmp_path / "last-run.json").exists()


def test_a_read_only_plan_records_nothing_for_explain(tmp_path: Path, sink: CapturingSink) -> None:
    with context_for(tmp_path, sink) as ctx:
        plan(ctx, now=NOW, scheduled=False, persist=False)

    assert not (tmp_path / "last-run.json").exists()


# --------------------------------------------------------------------------- apply


def test_apply_adds_refreshes_and_monitors(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()
        owned_artists = ctx.state.owned_artists()
        counts = ctx.state.last_source_counts()

    assert code == EXIT_OK
    order = [name for name in lidarr.names() if name in {"add_artist", "refresh_artist", "set_albums_monitored"}]
    assert order == ["add_artist", "refresh_artist", "set_albums_monitored"]
    assert sorted(k.rg_mbid for k in owned) == ["rg-1", "rg-2"]
    assert all(record.lidarr_album_id is not None for record in owned.values())
    assert owned_artists["artist-1"].added_by_us is True
    assert counts["followed_artists"] == 1
    assert sink.last.dry_run is False
    assert sink.last.counts["monitored"] == 2


def test_apply_is_idempotent(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        second = plan(ctx, now=NOW, scheduled=False)

    assert second.diff.is_empty
    assert second.diff.monitor == []
    assert second.diff.unmonitor == []


def test_a_stale_diff_is_refused_and_changes_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        # The user follows someone else before typing --apply.
        source.snapshot = snapshot(
            artists=[
                artist_intent("Test Artist", spotify_id="sp-a1"),
                artist_intent("Other", spotify_id="sp-a2"),
            ]
        )
        before = len(lidarr.calls)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_STALE
    assert sink.last.status is RunStatus.STALE
    assert "add_artist" not in [name for name, _ in lidarr.calls[before:]]
    from likearr.shell.last_run import read_last_run

    last = read_last_run(tmp_path / "last-run.json")
    assert last is not None and last.kind == "refused apply"  # never labelled a dry run


def test_force_applies_a_stale_diff(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        source.snapshot = snapshot(
            artists=[
                artist_intent("Test Artist", spotify_id="sp-a1"),
                artist_intent("Other", spotify_id="sp-a2"),
            ]
        )
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True, force=True)

    assert code == EXIT_OK
    assert "add_artist" in lidarr.names()


DENIED_RG = "0b9c1c6e-5b1a-4d1e-9f2a-3c4d5e6f7a8b"


def _deny(ctx: Context, *rg_mbids: str) -> None:
    """What the web UI's "Not this one" does between plan and apply: edit `[rules] deny_releases`."""
    ctx.config = replace(ctx.config, rules=replace(ctx.config.rules, deny_releases=rg_mbids))


def test_the_diff_records_the_config_it_was_planned_under(tmp_path: Path, sink: CapturingSink) -> None:
    out = tmp_path / "diff.json"
    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        expected = ctx.config.plan_fingerprint

    assert json.loads(out.read_text())["config_fingerprint"] == expected
    assert read_diff(out).config_fingerprint == expected


def test_a_diff_planned_before_a_release_was_denied_is_refused(tmp_path: Path, sink: CapturingSink) -> None:
    """Spotify and Lidarr have not moved, so only the config can say the reviewed plan is wrong now."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        _deny(ctx, DENIED_RG)
        before = len(lidarr.calls)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_STALE
    assert sink.last.status is RunStatus.STALE
    assert "[rules] deny_releases" in sink.last.message
    writes = {"add_artist", "refresh_artist", "set_albums_monitored"}
    assert not [name for name, _ in lidarr.calls[before:] if name in writes]
    assert owned == {}


def test_a_denied_release_of_a_followed_artist_is_not_monitored(tmp_path: Path, sink: CapturingSink) -> None:
    """#153, option B: `deny_releases` reaches the followed catalogue, so "Not this one" on a
    followed artist's EP keeps it out of Lidarr while the rest of the catalogue is monitored."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        _deny(ctx, EP.mbid)
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert {key.rg_mbid for key in owned} == {ALBUM.mbid}


@pytest.mark.parametrize("narrow", ["deny", "albums-only"])
def test_narrowing_a_followed_catalogue_by_hand_does_not_hold_scheduled_runs(
    tmp_path: Path, sink: CapturingSink, narrow: str
) -> None:
    """Denying one release of a followed artist, or tagging them albums-only, is the user's own
    filter. The artist-shrink guard is for a catalogue that shrank on MusicBrainz, so it must not
    hold every scheduled run until someone accepts the shrink by hand."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True) == EXIT_OK
        assert {k.rg_mbid for k in ctx.state.owned_releases()} == {ALBUM.mbid, EP.mbid}

        if narrow == "deny":
            _deny(ctx, EP.mbid)
        else:
            lidarr.artists["artist-1"] = replace(lidarr.artists["artist-1"], tags=frozenset({ALBUMS_ONLY_TAG_ID}))
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK, sink.last.message
    assert {k.rg_mbid for k in owned} == {ALBUM.mbid}
    assert lidarr.album("artist-1", EP.mbid).monitored is False  # type: ignore[union-attr]


def test_a_guard_change_after_planning_also_refuses_the_diff(tmp_path: Path, sink: CapturingSink) -> None:
    """A tightened guard would have refused unmonitors the saved plan still carries."""
    out = tmp_path / "diff.json"
    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        ctx.config = replace(ctx.config, guards=replace(ctx.config.guards, source_shrink_pct=1.0))
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_STALE
    assert "[guards] source_shrink_pct" in sink.last.message


def test_a_diff_written_before_the_config_was_recorded_is_refused(tmp_path: Path, sink: CapturingSink) -> None:
    """It cannot vouch for the config it was planned under, and a re-plan costs one warm run."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        raw = json.loads(out.read_text())
        del raw["config_fingerprint"]
        out.write_text(json.dumps(raw))
        before = len(lidarr.calls)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_STALE
    assert "did not record" in sink.last.message
    assert "add_artist" not in [name for name, _ in lidarr.calls[before:]]


def test_force_applies_a_diff_whose_config_moved(tmp_path: Path, sink: CapturingSink) -> None:
    """`--force` means what it already meant: the human has looked and wants this plan anyway."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        _deny(ctx, DENIED_RG)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True, force=True)

    assert code == EXIT_OK
    assert "add_artist" in lidarr.names()


def test_an_unchanged_config_still_applies(tmp_path: Path, sink: CapturingSink) -> None:
    """The ordinary reviewed apply: same config, same world, nothing refused."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_OK
    assert "set_albums_monitored" in lidarr.names()


def test_a_metadata_outage_skips_the_artist_and_never_unmonitors_from_it(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.fail_refresh = {"artist-1"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert sink.last.lidarr_metadata_ok is False, "still published: the call really did fail"
    assert sink.last.skipped_artists == 1
    assert sink.last.status is RunStatus.OK, "a first run has nothing to compare against and must not alarm"
    assert sink.last.baseline == "first-run"
    assert owned == {}, "nothing may be claimed for an artist whose catalogue never arrived"
    assert not any(name == "set_albums_monitored" for name in lidarr.names())


def test_a_ratchet_refresh_failure_also_skips_that_artists_own_unmonitor(tmp_path: Path, sink: CapturingSink) -> None:
    """The outage test above uses a brand-new artist with nothing to unmonitor. Here the artist
    already exists, is due a profile ratchet, and owns a release that should now come off - and the
    RefreshArtist that follows the ratchet fails. The artist must be skipped exactly as an add's
    failed refresh would skip it, so the stale ownership row survives instead of being unmonitored
    against a catalogue Lidarr never finished reading (issue #132, kills M1 and M2)."""
    saved = spotify_album("Live At Somewhere", spotify_id="sp-live", upc="222")
    lookup = FakeLookup().add(ALBUM, LIVE)
    lookup.catalogues["artist-1"] = ["rg-1"]  # the EP has left the followed catalogue this run
    lookup.barcodes["222"] = "rg-3"
    source = FakeSource(
        snapshot(
            artists=[artist_intent("Test Artist", spotify_id="sp-a1")],
            albums=[album_intent(saved)],
        )
    )
    artist = lidarr_artist("artist-1", id=1, name="Test Artist", metadata_profile_id=LEAN_ID)
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, LIVE]})
    lidarr.seed(
        artist,
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
        lidarr_album(LIVE, id=103, monitored=False),
    )
    lidarr.fail_refresh = {"artist-1"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored(
            [
                _owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.FOLLOWED, "sp-a1"), album_id=101),
                _owned_release(ReleaseKey("artist-1", "rg-2"), Reason(ReasonKind.FOLLOWED, "sp-a1"), album_id=102),
            ]
        )
        exit_code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)
        owned = ctx.state.owned_releases()

    assert exit_code == EXIT_OK, "a ratchet's failed refresh is a skip, not a guard"
    assert ReleaseKey("artist-1", "rg-2") in {u.key for u in diff.unmonitor}, "the diff still wants it gone"
    assert applied.skipped_artists == ["artist-1"]
    assert applied.lidarr_metadata_ok is False
    assert applied.unmonitored == 0
    assert not any(name == "set_albums_monitored" for name in lidarr.names())
    assert lidarr.album("artist-1", "rg-2").monitored is True  # type: ignore[union-attr]
    assert ReleaseKey("artist-1", "rg-1") in owned
    assert ReleaseKey("artist-1", "rg-2") in owned, "the artist's ownership rows are untouched by the skip"


def test_a_newly_skipped_artist_degrades_once_there_is_a_baseline(tmp_path: Path, sink: CapturingSink) -> None:
    """A clean run, then the user follows someone whose refresh fails. That is the alarm worth having.

    A skip is tied to an artist *add*: there is no refresh on a later run, so a skip does not
    naturally recur. What matters is that it is reported on the run it happens, against a baseline
    that says it did not happen before.
    """
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert sink.last.status is RunStatus.OK

        second = rg("rg-9", "Theirs", artist_mbid="artist-2", artist_name="Other")
        lookup.add(second)
        lookup.catalogues["artist-2"] = ["rg-9"]
        lidarr.catalogue["artist-2"] = [second]
        lidarr.fail_refresh = {"artist-2"}
        source.snapshot = snapshot(
            artists=[
                artist_intent("Test Artist", spotify_id="sp-a1"),
                artist_intent("Other", spotify_id="sp-a2"),
            ]
        )
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK, "degraded has always been exit 0 and still is"
    assert sink.last.status is RunStatus.DEGRADED
    assert sink.last.baseline == "compared"
    assert sink.last.skipped_artists_new == 1
    assert "new-skipped-artist" in sink.last.new_conditions
    assert "1 artist(s) newly skipped" in sink.last.message


# ------------------------------------------------- an artist Lidarr refuses to add (issue #173)


def two_artist_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """`followed_world` plus a second followed artist, "Other" (`artist-2`), to be refused."""
    source, lookup, lidarr = followed_world()
    theirs = rg("rg-9", "Theirs", artist_mbid="artist-2", artist_name="Other")
    lookup.add(theirs)
    lookup.catalogues["artist-2"] = ["rg-9"]
    lidarr.catalogue["artist-2"] = [theirs]
    source.snapshot = snapshot(
        artists=[
            artist_intent("Test Artist", spotify_id="sp-a1"),
            artist_intent("Other", spotify_id="sp-a2"),
        ]
    )
    return source, lookup, lidarr


def test_an_artist_lidarr_does_not_know_is_skipped_and_the_rest_applies(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lidarr's 400 "An artist with this ID was not found" skips that one artist, not the apply.

    It is not an outage and can last weeks, so the run stays green: the artist
    is named in the record and the summary, and the next run tries it again.
    """
    from likearr.shell.last_run import read_last_run

    source, lookup, lidarr = two_artist_world()
    lidarr.unknown_add = {"artist-2"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()
        first = sink.last
        summary = capsys.readouterr().out
        again = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sorted(k.rg_mbid for k in owned) == ["rg-1", "rg-2"], "the other artist's monitors applied"
    assert "artist-2" not in lidarr.artists
    assert first.status is RunStatus.OK
    assert first.lidarr_metadata_ok is True, "not an outage"
    assert first.skipped_artists == 0, "not a class-B skip, which would degrade until accepted"
    assert "Other (artist-2)" in first.message
    assert "does not know" in first.message
    assert "Other (artist-2)" in summary

    assert again == EXIT_OK
    assert [p for n, p in lidarr.calls if n == "add_artist"].count("artist-2") == 2, "tried again next run"
    assert sink.last.baseline == "compared"
    assert sink.last.status is RunStatus.OK, "still green on the run after, with a baseline to compare"
    assert sink.last.new_conditions == []
    assert "Other (artist-2)" in sink.last.message

    last = read_last_run(tmp_path / "last-run.json")
    assert last is not None and last.applied
    assert "artist-2" not in last.view.artists, "Explain must not say the refused artist was added"


def test_a_metadata_outage_on_add_skips_that_artist_and_the_rest_applies(tmp_path: Path, sink: CapturingSink) -> None:
    """A 5xx on POST /artist (SkyHook down) skips the artist and does count as degraded metadata."""
    source, lookup, lidarr = two_artist_world()
    lidarr.fail_add = {"artist-2"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert sorted(k.rg_mbid for k in owned) == ["rg-1", "rg-2"]
    assert sink.last.lidarr_metadata_ok is False
    assert sink.last.skipped_artists == 1
    assert "Other (artist-2)" in sink.last.message


def test_a_metadata_outage_on_add_is_a_skipped_artist_not_a_catalogue_gap(tmp_path: Path, sink: CapturingSink) -> None:
    """Nothing was ever added for this artist, so its releases must not be reported as a Lidarr
    catalogue gap either: that would retry them as `unmapped_in_lidarr` forever alongside the skip,
    double-counting one failure as two different faults (issue #132, kills M3)."""
    source, lookup, lidarr = two_artist_world()
    lidarr.fail_add = {"artist-2"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        exit_code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)

    assert exit_code == EXIT_OK
    assert applied.skipped_artists == ["artist-2"]
    assert applied.unmapped_in_lidarr == []
    assert applied.lidarr_metadata_ok is False


def test_a_refused_add_does_not_hold_back_an_existing_artists_monitor(tmp_path: Path, sink: CapturingSink) -> None:
    """Rig B (#173): an artist already in Lidarr with a wanted album unmonitored, and a followed
    artist Lidarr refuses. The album is monitored on the same run."""
    source, lookup, lidarr = two_artist_world()
    lidarr.seed(lidarr_artist("artist-1"), lidarr_album(ALBUM, id=101), lidarr_album(EP, id=102))
    lidarr.unknown_add = {"artist-2"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert lidarr.albums["artist-1"]["rg-1"].monitored
    assert lidarr.albums["artist-1"]["rg-2"].monitored


def test_a_bad_request_on_add_still_stops_the_apply(tmp_path: Path, sink: CapturingSink) -> None:
    """Only the refusals #173 names are skipped. A 400 about what likearr sent (a root folder, a
    profile) is likearr's mistake, and skipping it would hide it on every run."""
    source, lookup, lidarr = two_artist_world()
    lidarr.reject_add = {"artist-2"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_ERROR
    assert sink.last.status is RunStatus.ERROR
    assert "Root folder" in sink.last.message


# ------------------------------------------------- an artist someone else adds first (issue #4)


def test_an_artist_someone_else_added_first_is_left_to_them(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """Lidarr's "already exists" on an artist without likearr's tag: added by hand or by an import
    list while the run was resolving. Recording it as likearr's would force its "Monitor New
    Albums" to None on every run after, so it gets no row, no refresh and no re-monitor."""
    source, lookup, lidarr = two_artist_world()
    lidarr.added_elsewhere = {"artist-2": False}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        with caplog.at_level(logging.INFO, logger="likearr"):
            exit_code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)
        owned_artists = ctx.state.owned_artists()
        theirs = lidarr.artists["artist-2"]
        before = len(lidarr.calls)
        second = plan(ctx, now=NOW, scheduled=True)
        again = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert exit_code == EXIT_OK
    assert "artist-2" not in owned_artists, "someone else's artist is not likearr's"
    assert "artist-1" in owned_artists, "the rest of the apply went ahead"
    assert applied.foreign_artists == ["artist-2"]
    assert applied.skipped_artists == [], "not a metadata failure, so not a class-B skip"
    assert ("refresh_artist", "artist-2") not in lidarr.calls
    assert all(theirs.id not in ids for name, ids in lidarr.calls if name == "set_artists_monitored")
    assert "someone else added it" in caplog.text

    assert "artist-2" not in second.diff.set_new_items_none, "their own setting is theirs to keep"
    assert again == EXIT_OK
    assert all(theirs.id not in ids for name, ids in lidarr.calls[before:] if name == "set_artists_new_items_none")
    assert lidarr.artists["artist-2"].monitor_new_items == "all"


def test_an_artist_carrying_likearrs_tag_is_likearrs_own_add_resumed(tmp_path: Path, sink: CapturingSink) -> None:
    """The same "already exists" on an artist that carries likearr's tag is likearr's own add from
    a run that stopped before recording it: recorded, refreshed and re-monitored, as before #4."""
    source, lookup, lidarr = two_artist_world()
    lidarr.added_elsewhere = {"artist-2": True}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        exit_code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)
        owned_artists = ctx.state.owned_artists()

    assert exit_code == EXIT_OK
    assert owned_artists["artist-2"].added_by_us is True
    assert owned_artists["artist-2"].lidarr_artist_id == lidarr.artists["artist-2"].id
    assert applied.foreign_artists == []
    assert applied.added == 2
    assert ("refresh_artist", "artist-2") in lidarr.calls
    monitored = [ids for name, ids in lidarr.calls if name == "set_artists_monitored"]
    assert any(lidarr.artists["artist-2"].id in ids for ids in monitored)


def test_a_reviewed_plan_goes_stale_when_an_artist_it_adds_appears(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #4: the artist appeared in Lidarr between the review and the apply."""
    source, lookup, lidarr = two_artist_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        lidarr.seed(lidarr_artist("artist-2", id=77, monitor_new_items="all"))
        before = len(lidarr.calls)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_STALE
    assert "add_artist" not in [name for name, _ in lidarr.calls[before:]]


# ------------------------------------------------- a followed artist's new album (issue #8)


def test_a_new_release_lidarr_lacks_is_planned_as_a_refresh_not_buried_in_the_gaps(
    tmp_path: Path, sink: CapturingSink
) -> None:
    source, lookup, lidarr = new_release_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)
        write_diff(result.diff, out)

    assert result.diff.refresh_artists == ["artist-1"]
    assert result.diff.monitor == [], "the release is not in Lidarr, so there is nothing to monitor yet"
    assert diff_summary(result.diff)["catalogue_gaps_recent"] == 1
    assert diff_summary(result.diff)["catalogue_gaps"] == 0
    assert read_diff(out).refresh_artists == ["artist-1"], "a reviewed apply carries the refresh"


def test_applying_refreshes_the_artist_so_the_next_run_monitors_the_new_album(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The whole point of #8: without this the album waits on Lidarr's own schedule, or for ever."""
    source, lookup, lidarr = new_release_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        first = sink.last
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert "refresh_artist" in lidarr.names()
    assert first.catalogue_gaps_recent == 1
    assert first.catalogue_gaps == 0, "a new release is not filed with the chronic promos"
    assert sorted(k.rg_mbid for k in owned) == ["rg-new"], "monitored on the run after the refresh"
    assert sink.last.catalogue_gaps_recent == 0


def test_a_new_release_never_degrades_a_run_however_long_lidarr_takes(tmp_path: Path, sink: CapturingSink) -> None:
    """The follow is the intent and it existed last run, so in class A this would be a regression."""
    source, lookup, lidarr = new_release_world(lidarr_gets_it=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK

    assert sink.last.status is RunStatus.OK
    assert sink.last.baseline == "compared"
    assert sink.last.regressions == 0
    assert sink.last.new_conditions == []
    assert sink.last.catalogue_gaps_recent == 1
    assert sink.last.catalogue_gaps_recent_new == 0, "it survived an apply: the count is the signal"


def test_a_failed_freshness_refresh_is_counted_but_never_skips_the_artist(tmp_path: Path, sink: CapturingSink) -> None:
    """Unlike an add or a ratchet, this refresh is opportunistic.

    The artist was already in Lidarr with a catalogue this run read, so a timeout costs only the
    one release it was chasing. Dropping their monitors would cost something real, and a class-B
    `skipped_artists` identity degrades every run until a human accepts it - for a request likearr
    did not have to make.
    """
    source, lookup, lidarr = new_release_world()
    # A second release under the same artist that this run would monitor.
    other = rg("rg-other", "Older Record", released="2015-01-01")
    lookup.add(other)
    lookup.catalogues["artist-1"] = ["rg-1", "rg-new", "rg-other"]
    lidarr.albums["artist-1"]["rg-other"] = lidarr_album(other, id=303, monitored=False)
    lidarr.fail_refresh = {"artist-1"}

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert sink.last.skipped_artists == 0, "an opportunistic refresh never skips an artist"
    assert "new-skipped-artist" not in sink.last.new_conditions
    assert sink.last.refresh_failures == 1
    assert sink.last.status is RunStatus.OK
    assert "rg-other" in [k.rg_mbid for k in owned], "the artist's other monitors still applied"


def test_a_refreshed_artist_is_left_alone_until_the_backoff_expires(tmp_path: Path, sink: CapturingSink) -> None:
    """Without this, a promo Lidarr will never carry is chased on every run for 60 days."""
    source, lookup, lidarr = new_release_world(lidarr_gets_it=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert lidarr.names().count("refresh_artist") == 1
        assert ctx.state.last_gap_refreshes() == {"artist-1": NOW}

        soon = NOW + timedelta(hours=6)
        assert run_command(ctx, now=soon, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert lidarr.names().count("refresh_artist") == 1, "inside the backoff"
        assert sink.last.catalogue_gaps_recent == 1, "still reported, just not asked again"

        later = NOW + timedelta(hours=25)
        assert run_command(ctx, now=later, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert lidarr.names().count("refresh_artist") == 2, "the backoff expired"


def test_a_failed_freshness_refresh_still_starts_the_backoff(tmp_path: Path, sink: CapturingSink) -> None:
    """An artist whose metadata is stuck is exactly the one that must not be asked every run."""
    source, lookup, lidarr = new_release_world(lidarr_gets_it=False)
    lidarr.fail_refresh = {"artist-1"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        assert ctx.state.last_gap_refreshes() == {"artist-1": NOW}
        run_command(ctx, now=NOW + timedelta(hours=6), out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.names().count("refresh_artist") == 1


def test_the_refresh_cap_is_configurable(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = new_release_world()
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", replace(config.lidarr, max_refreshes_per_run=0))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)

    assert result.diff.refresh_artists == []
    assert diff_summary(result.diff)["catalogue_gaps_recent"] == 1, "still reported, just not chased"


def test_the_recency_window_is_configurable(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = new_release_world()
    config = make_config(tmp_path)
    object.__setattr__(config, "rules", replace(config.rules, recent_release_days=1))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)

    assert result.diff.refresh_artists == []
    assert diff_summary(result.diff)["catalogue_gaps"] == 1, "outside the window it is an ordinary gap"


# ------------------------------------ following an artist swaps the single out (issue #9)


def test_following_an_artist_swaps_the_liked_single_for_the_album_without_an_alarm(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The swap moves a release, so its health identity moves with it. That is not a regression.

    `core.diff.best_reason_key` stamps the intent on every identity and the intent - the like - is
    unchanged, so a swap that lands on a release Lidarr actually holds produces no shortfall
    identity at all. This is the test the issue asked for: the run that swaps must stay `ok`.
    """
    single = rg("rg-single", "Blinding Lights", primary=PrimaryType.SINGLE, released="2019-11-29")
    album = rg("rg-album", "After Hours", released="2020-03-20")
    lookup = FakeLookup(
        barcodes={"111": "rg-single"},
        isrcs={"I1": ["rg-single", "rg-album"]},
        artists={"artist-1": "Test Artist"},
    ).add(single, album)
    lookup.catalogues["artist-1"] = ["rg-album", "rg-single"]
    liked = track_intent("Blinding Lights", spotify_album("Blinding Lights", upc="111"), isrc="I1")
    source = FakeSource(snapshot(tracks=[liked]))
    lidarr = FakeLidarr(catalogue={"artist-1": [single, album]})
    lidarr.seed(
        lidarr_artist("artist-1"),
        lidarr_album(single, id=101),
        lidarr_album(album, id=102),
    )
    config = make_config(tmp_path)
    object.__setattr__(config, "rules", replace(config.rules, liked_track_scope="smallest"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        assert sorted(k.rg_mbid for k in ctx.state.owned_releases()) == ["rg-single"]

        source.snapshot = snapshot(artists=[artist_intent("Test Artist", spotify_id="sp-a1")], tracks=[liked])
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_OK
        owned = ctx.state.owned_releases()

    assert sorted(k.rg_mbid for k in owned) == ["rg-album"], "the follow already brings the album"
    assert sink.last.baseline == "compared", "otherwise this proves nothing about the comparison"
    assert sink.last.status is RunStatus.OK
    assert sink.last.regressions == 0
    assert sink.last.new_conditions == []


def test_the_refresh_wait_scales_with_the_catalogue_size(tmp_path: Path, sink: CapturingSink) -> None:
    """Jean Sibelius, Bing Crosby, Springsteen and Johnny Cash blew a flat 300 s wait."""
    source, lookup, lidarr = followed_world()  # a two-release-group catalogue
    config = make_config(tmp_path)
    object.__setattr__(
        config,
        "lidarr",
        replace(config.lidarr, refresh_timeout_s=100.0, refresh_per_album_s=10.0, refresh_timeout_max_s=10_000.0),
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.refresh_timeouts["artist-1"] == 100.0 + 2 * 10.0


def test_an_unreadable_catalogue_gets_the_floor_wait(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    lookup.catalogues["artist-1"] = []  # nothing to size the wait by
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", replace(config.lidarr, refresh_timeout_s=100.0, refresh_per_album_s=10.0))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.refresh_timeouts["artist-1"] == 100.0


def test_a_ratchet_refresh_is_sized_by_the_albums_lidarr_already_has(tmp_path: Path, sink: CapturingSink) -> None:
    saved = spotify_album("Live At Somewhere", spotify_id="sp-live", upc="222")
    lookup = FakeLookup().add(LIVE)
    lookup.barcodes["222"] = "rg-3"
    source = FakeSource(snapshot(albums=[album_intent(saved)]))
    lidarr = FakeLidarr(catalogue={"artist-1": [LIVE]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist", metadata_profile_id=LEAN_ID),
        lidarr_album(LIVE, id=103, monitored=False),
        *[lidarr_album(rg(f"rg-x{i}", f"Other {i}", artist_mbid="artist-1"), id=200 + i) for i in range(5)],
    )
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", replace(config.lidarr, refresh_timeout_s=100.0, refresh_per_album_s=10.0))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.refresh_timeouts["artist-1"] == 100.0 + 6 * 10.0


class _FlaggingLookup(FakeLookup):
    """A lookup that fails the way `CompositeLookup` does: flag MusicBrainz unhealthy, then raise."""

    def __init__(self, error: Exception, composite: SimpleNamespace) -> None:
        super().__init__()
        self._error = error
        self._composite = composite

    def artist_release_groups(self, artist_mbid: str):  # type: ignore[override]
        self._composite.mb_ok = False
        raise self._error


def _sizing_world(tmp_path: Path, error: Exception):
    composite = SimpleNamespace(
        mb_ok=True, lidarr_metadata_ok=True, provisional_release_groups=frozenset(), mb_failure_count=0
    )
    config = make_config(tmp_path)
    object.__setattr__(
        config,
        "lidarr",
        replace(config.lidarr, refresh_timeout_s=100.0, refresh_per_album_s=10.0, refresh_timeout_max_s=5000.0),
    )
    ctx = make_context(tmp_path, lookup=_FlaggingLookup(error, composite), config=config)
    ctx.composite = composite  # type: ignore[assignment]
    return ctx, composite


def test_a_catalogue_too_large_to_browse_gets_the_ceiling_wait(tmp_path: Path) -> None:
    """The artists this feature exists for are the ones MusicBrainz refuses to page through."""
    ctx, _composite = _sizing_world(tmp_path, CatalogueTooLarge("more than 3000"))
    with ctx:
        assert _refresh_timeout_s(ctx, "artist-1", ctx.lidarr.load_view(None)) == 5000.0


def test_sizing_a_refresh_wait_never_flags_musicbrainz_unhealthy(tmp_path: Path) -> None:
    ctx, composite = _sizing_world(tmp_path, CatalogueTooLarge("more than 3000"))
    with ctx:
        _refresh_timeout_s(ctx, "artist-1", ctx.lidarr.load_view(None))
    assert composite.mb_ok is True


def test_an_outage_while_sizing_gets_the_floor_and_leaves_mb_ok_alone(tmp_path: Path) -> None:
    ctx, composite = _sizing_world(tmp_path, MetadataError("musicbrainz is down"))
    with ctx:
        assert _refresh_timeout_s(ctx, "artist-1", ctx.lidarr.load_view(None)) == 100.0, "an outage is not 'huge'"
    assert composite.mb_ok is True


# --------------------------------------------------------------------------- lidarr metadata cache/outage (issue #18)


def _composite_world(tmp_path: Path, sink: CapturingSink, **overrides: object) -> Context:
    """A `Context` whose `ctx.composite` is a bare fake, so `plan()`'s reads off it are directly
    controllable - the same pattern `_sizing_world` uses, extended with the issue #18 fields."""
    source, lookup, lidarr = followed_world()
    defaults: dict[str, object] = dict(
        mb_ok=True,
        lidarr_metadata_ok=True,
        lidarr_metadata_failures=(),
        catalogue_too_large=(),
        mb_stale_served=0,
        lidarr_metadata_attempts=0,
        lidarr_metadata_attempt_failures=0,
        lidarr_metadata_new_failures=(),
        lidarr_metadata_any_success=False,
        provisional_release_groups=frozenset(),
        mb_failure_count=0,
        mb_cache_hits=0,
        mb_live_calls=0,
    )
    defaults.update(overrides)
    ctx = make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink)
    ctx.composite = SimpleNamespace(**defaults)  # type: ignore[assignment]
    return ctx


def test_a_negative_cache_entry_is_written_when_another_lookup_succeeded(tmp_path: Path, sink: CapturingSink) -> None:
    with _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_new_failures=("album-search:Leopold Stokowski|Rhapsody",),
        lidarr_metadata_any_success=True,
    ) as ctx:
        plan(ctx, now=NOW, scheduled=False)

    assert ctx.state.lidarr_negative_cache() == {"album-search:Leopold Stokowski|Rhapsody": NOW}


def test_no_negative_cache_entry_when_nothing_else_succeeded(tmp_path: Path, sink: CapturingSink) -> None:
    """An api.lidarr.audio outage must not poison the cache for a week."""
    with _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_new_failures=("album-search:Leopold Stokowski|Rhapsody",),
        lidarr_metadata_any_success=False,
        provisional_release_groups=frozenset(),
    ) as ctx:
        plan(ctx, now=NOW, scheduled=False)

    assert ctx.state.lidarr_negative_cache() == {}


def test_a_cache_hit_is_not_written_again(tmp_path: Path, sink: CapturingSink) -> None:
    """Nothing was asked of Lidarr for a cache-skipped term, so there is nothing new to cache."""
    with _composite_world(tmp_path, sink, lidarr_metadata_new_failures=(), lidarr_metadata_any_success=True) as ctx:
        plan(ctx, now=NOW, scheduled=False)

    assert ctx.state.lidarr_negative_cache() == {}


def test_negative_caching_is_skipped_on_a_read_only_plan(tmp_path: Path, sink: CapturingSink) -> None:
    with _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_new_failures=("album-search:Leopold Stokowski|Rhapsody",),
        lidarr_metadata_any_success=True,
    ) as ctx:
        plan(ctx, now=NOW, scheduled=False, persist=False)

    assert ctx.state.lidarr_negative_cache() == {}


def test_a_lidarr_metadata_outage_degrades_the_run(tmp_path: Path, sink: CapturingSink) -> None:
    """The scenario rule 9 could not see (issue #18): most of this run's Lidarr lookups failed."""
    with _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_attempts=20,
        lidarr_metadata_attempt_failures=15,
        lidarr_metadata_any_success=True,
    ) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.DEGRADED
    assert "lidarr-metadata-outage" in sink.last.new_conditions


def test_a_few_chronic_failures_never_read_as_an_outage(tmp_path: Path, sink: CapturingSink) -> None:
    """The everyday shape: 2 of ~80 attempted lookups fail, every run, for ever."""
    with _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_attempts=80,
        lidarr_metadata_attempt_failures=2,
        lidarr_metadata_any_success=True,
    ) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert sink.last.status is RunStatus.OK
    assert "lidarr-metadata-outage" not in sink.last.new_conditions


def test_an_artist_whose_refresh_timed_out_heals_on_the_next_run(tmp_path: Path, sink: CapturingSink) -> None:
    """Lidarr keeps refreshing after likearr stops waiting, so the catalogue is there next time."""
    source, lookup, lidarr = followed_world()
    lidarr.fail_refresh = {"artist-1"}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        assert ctx.state.owned_releases() == {}

        lidarr.fail_refresh = set()
        lidarr.refresh_artist(lidarr.artists["artist-1"])  # Lidarr finishing in the background
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert {k.rg_mbid for k in owned} == {"rg-1", "rg-2"}
    assert sink.last.status is RunStatus.OK


def test_a_crash_mid_apply_leaves_only_committed_batches_and_the_next_run_finishes(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch one commits, batch two explodes, and a re-run picks up exactly what is left."""
    monkeypatch.setattr("likearr.shell.apply.BATCH_SIZE", 1)
    source, lookup, lidarr = followed_world()
    lidarr.fail_monitor_batch = 2
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_crash = ctx.state.owned_releases()

        assert code == EXIT_ERROR
        assert len(owned_after_crash) == 1
        assert sink.last.status is RunStatus.ERROR
        # #54: what reached Lidarr before the failure is recorded, so Status can say how far it got.
        crash = sink.last
        assert crash.changes_made is not None and crash.changes_planned is not None
        assert 0 < crash.changes_made < crash.changes_planned
        assert crash.counts["monitored"] == 1
        assert crash.message.startswith(f"the apply stopped part-way: {crash.changes_made} of {crash.changes_planned}")

        lidarr.fail_monitor_batch = None
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_retry = ctx.state.owned_releases()

    assert second == EXIT_OK
    assert sorted(k.rg_mbid for k in owned_after_retry) == ["rg-1", "rg-2"]


def test_a_monitor_batch_lidarr_applied_but_answered_with_an_error_is_owned_and_later_unmonitored(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#174: the reply to batch two is lost after the change landed. likearr still owns it."""
    monkeypatch.setattr("likearr.shell.apply.BATCH_SIZE", 1)
    source, lookup, lidarr = followed_world()
    lidarr.fail_monitor_batch = 2
    lidarr.fail_monitor_batch_applies = 1
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_crash = ctx.state.owned_releases()

        assert code == EXIT_ERROR
        assert sink.last.status is RunStatus.ERROR
        assert sorted(k.rg_mbid for k in owned_after_crash) == ["rg-1", "rg-2"]
        assert lidarr.album("artist-1", "rg-2").monitored is True  # type: ignore[union-attr]
        assert sink.last.counts["monitored"] == 2, "both albums were confirmed monitored"
        # #174 follow-up: everything planned actually landed, so this must not read as partial.
        assert sink.last.changes_made == sink.last.changes_planned == 3, "add + both monitors"
        assert sink.last.message == (
            "the apply finished: all 3 planned changes were made, but confirming it failed: "
            "fake: lidarr fell over on monitor batch 2"
        )
        assert "stopped part-way" not in sink.last.message

        # The artist is unfollowed, one of 20 (so no shrink guard holds it back): the next apply
        # lets go of both albums, the one whose reply was lost included.
        lidarr.fail_monitor_batch = None
        source.snapshot = snapshot(artists=[], counts={"followed_artists": 19})
        ctx.state.record_source_counts({"followed_artists": 20})
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_unfollow = ctx.state.owned_releases()

    assert second == EXIT_OK
    assert owned_after_unfollow == {}
    assert lidarr.album("artist-1", "rg-1").monitored is False  # type: ignore[union-attr]
    assert lidarr.album("artist-1", "rg-2").monitored is False  # type: ignore[union-attr]


def test_a_monitor_batch_lidarr_applied_in_part_owns_only_the_applied_albums(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#174: Lidarr's 500 on a stale id still flips the rest. Own exactly what it flipped."""
    source, lookup, lidarr = followed_world()
    lidarr.fail_monitor_batch = 1
    lidarr.fail_monitor_batch_applies = 1
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_crash = ctx.state.owned_releases()

        assert code == EXIT_ERROR
        assert [k.rg_mbid for k in owned_after_crash] == ["rg-1"]
        assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]
        assert lidarr.album("artist-1", "rg-2").monitored is False  # type: ignore[union-attr]
        assert sink.last.counts["monitored"] == 1, "only what Lidarr confirmed counts as made"

        lidarr.fail_monitor_batch = None
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_retry = ctx.state.owned_releases()

    assert second == EXIT_OK
    assert sorted(k.rg_mbid for k in owned_after_retry) == ["rg-1", "rg-2"]


def test_a_failed_monitor_batch_keeps_its_rows_when_lidarr_cannot_be_read_back(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#174: with no way to tell what landed, keep the rows. An owned row on an unmonitored album is harmless."""
    monkeypatch.setattr("likearr.shell.apply.BATCH_SIZE", 1)
    source, lookup, lidarr = followed_world()
    lidarr.fail_monitor_batch = 2
    lidarr.down_after_monitor_failure = True
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_after_crash = ctx.state.owned_releases()

    assert code == EXIT_ERROR
    assert sorted(k.rg_mbid for k in owned_after_crash) == ["rg-1", "rg-2"]
    assert lidarr.names().count("load_albums") >= 2, "the failed batch was read back once"
    assert sink.last.counts["monitored"] == 1, "a batch that could not be read back is not counted as made"


def test_a_failed_monitor_batch_puts_back_an_ownership_row_that_was_already_there(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#174: undoing the row written ahead restores what was owned before, rather than deleting it."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=False),
        lidarr_album(EP, id=102, monitored=True),
    )
    lidarr.fail_monitor_batch = 1
    before = _owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.MANUAL, "by-hand"), album_id=101)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored([before])
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_ERROR
    assert owned == {before.key: before}


@pytest.mark.parametrize("monitored_before", [True, False], ids=["reason-update", "re-monitor"])
def test_a_release_kept_by_hand_stays_kept_after_a_source_wants_it_and_lets_go(
    tmp_path: Path, sink: CapturingSink, monitored_before: bool
) -> None:
    """`manual` is sticky. A release kept at adoption, then wanted by a follow, then unfollowed,
    is never unmonitored: the follow's reason joins the manual one rather than replacing it.

    Both write paths: the album still monitored (the reason-set update) and the album unmonitored
    by hand since (the monitor upsert)."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=monitored_before),
        lidarr_album(EP, id=102, monitored=False),
    )
    kept = _owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.MANUAL, "adopt"), album_id=101)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored([kept])
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        wanted = ctx.state.owned_releases()[kept.key]
        assert wanted.is_manual, "the follow's reason joined the manual one"
        assert ReasonKind.FOLLOWED in {r.kind for r in wanted.reasons}

        source.snapshot = snapshot(artists=[], counts={"followed_artists": 19})
        ctx.state.record_source_counts({"followed_artists": 20})
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]
    assert owned[kept.key].is_manual
    assert lidarr.album("artist-1", "rg-2").monitored is False, "the follow's own EP is let go"  # type: ignore[union-attr]


def test_a_release_kept_by_hand_keeps_manual_when_its_monitor_batch_lands_but_errors(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The row is written ahead of the PUT, and a batch that errors skips the reason-set updates, so
    the row written ahead is the one that must carry `manual`."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=False),
        lidarr_album(EP, id=102, monitored=True),
    )
    lidarr.fail_monitor_batch = 1
    lidarr.fail_monitor_batch_applies = 1
    kept = _owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.MANUAL, "adopt"), album_id=101)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored([kept])
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_ERROR
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]
    assert owned[kept.key].is_manual, "the row written ahead of the failed batch kept `manual`"


def test_an_already_monitored_album_is_never_claimed(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, _ = followed_world()
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        artist,
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=False),
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert sorted(k.rg_mbid for k in owned) == ["rg-2"], "only the release likearr itself flipped"


def test_hand_monitoring_between_plan_and_apply_is_not_claimed_with_force(tmp_path: Path, sink: CapturingSink) -> None:
    """Plan wants the EP monitored; a human beats it to it in Lidarr before the reviewed diff is
    force-applied. Without `--force` the Lidarr digest mismatch alone would refuse the diff as
    stale, so this exercises the live re-check inside `_monitor` itself (issue #132, kills M4)."""
    source, lookup, _ = followed_world()
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(artist, lidarr_album(ALBUM, id=101, monitored=True), lidarr_album(EP, id=102, monitored=False))
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        planned = read_diff(out)
        assert [m.key.rg_mbid for m in planned.monitor] == ["rg-2"]

        lidarr.albums["artist-1"]["rg-2"] = replace(lidarr.albums["artist-1"]["rg-2"], monitored=True)

        exit_code, applied, _fresh, _diff = apply(ctx, out, now=NOW, scheduled=False, force=True)
        owned = ctx.state.owned_releases()

    assert exit_code == EXIT_OK
    assert applied.already_monitored == ["artist-1/rg-2"]
    assert ReleaseKey("artist-1", "rg-2") not in owned, "likearr did not flip it, so likearr does not own it"


def test_scheduled_guard_applies_monitors_but_zero_unmonitors(tmp_path: Path, sink: CapturingSink) -> None:
    """A source that halved since the last run keeps its monitors and loses its unmonitors."""
    source, lookup, _ = followed_world()
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP, LIVE]})
    lidarr.seed(
        artist,
        lidarr_album(ALBUM, id=101, monitored=False),
        lidarr_album(EP, id=102, monitored=False),
        lidarr_album(LIVE, id=103, monitored=True),
    )
    config = make_config(tmp_path)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        # likearr owns the live album for a liked track that is no longer in the source, and the
        # liked-tracks source looks like it collapsed from 40 items to 0.
        ctx.state.record_monitored(
            [
                _owned_release(
                    ReleaseKey(artist_mbid="artist-1", rg_mbid="rg-3"),
                    Reason(ReasonKind.LIKED, "sp-track-gone"),
                    album_id=103,
                )
            ]
        )
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_GUARDED
    assert sink.last.status is RunStatus.GUARDED
    assert ReleaseKey("artist-1", "rg-3") in owned, "a guarded unmonitor must not drop ownership"
    assert lidarr.album("artist-1", "rg-3") is not None
    assert lidarr.album("artist-1", "rg-3").monitored is True  # type: ignore[union-attr]
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]


def test_a_guarded_run_holds_back_an_unmonitor_no_guard_targeted(tmp_path: Path, sink: CapturingSink) -> None:
    """The guard test above dooms every unmonitor in the diff, which is the easy case. Here a
    source-shrink guard on `liked_tracks` only dooms the live album's unmonitor; a second owned
    release, monitored under a `saved` reason no guard ever looks at, is due for unmonitor too and
    survives `block()` untouched. `Diff.guarded` must still hold it back: a guard holds back every
    unmonitor, not just the ones it named (issue #132, kills M5)."""
    source, lookup, _ = followed_world()
    saved_gone = rg("rg-4", "The Saved One")
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP, LIVE, saved_gone]})
    lidarr.seed(
        artist,
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
        lidarr_album(LIVE, id=103, monitored=True),
        lidarr_album(saved_gone, id=104, monitored=True),
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored(
            [
                _owned_release(ReleaseKey("artist-1", "rg-3"), Reason(ReasonKind.LIKED, "sp-track-gone"), album_id=103),
                _owned_release(ReleaseKey("artist-1", "rg-4"), Reason(ReasonKind.SAVED, "sp-album-gone"), album_id=104),
            ]
        )
        # Only liked_tracks shrinks; saved_albums is never recorded as a baseline, so nothing guards
        # the saved release's own unmonitor - it survives block() and reaches apply on its own.
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        exit_code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)
        owned = ctx.state.owned_releases()

    assert exit_code == EXIT_GUARDED
    assert diff.guarded
    unmonitor_keys = {u.key for u in diff.unmonitor}
    assert ReleaseKey("artist-1", "rg-3") not in unmonitor_keys, "block() removed the doomed one"
    assert ReleaseKey("artist-1", "rg-4") in unmonitor_keys, "the diff still carries the undoomed one"
    assert applied.unmonitored == 0
    assert ReleaseKey("artist-1", "rg-3") in owned
    assert ReleaseKey("artist-1", "rg-4") in owned, "a guarded run must not apply an unmonitor no guard named"
    assert lidarr.album("artist-1", "rg-4").monitored is True  # type: ignore[union-attr]
    assert not any(name == "set_albums_monitored" and payload[1] is False for name, payload in lidarr.calls)


def test_unmonitor_drops_ownership_when_the_reason_really_left(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, _ = followed_world()
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP, LIVE]})
    lidarr.seed(
        artist,
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
        lidarr_album(LIVE, id=103, monitored=True),
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored(
            [
                _owned_release(
                    ReleaseKey(artist_mbid="artist-1", rg_mbid="rg-3"),
                    Reason(ReasonKind.LIKED, "sp-track-gone"),
                    album_id=103,
                )
            ]
        )
        ctx.state.record_source_counts({"followed_artists": 1})
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert ReleaseKey("artist-1", "rg-3") not in owned
    assert lidarr.album("artist-1", "rg-3").monitored is False  # type: ignore[union-attr]


# --------------------------------------------------------------------------- guards keep their baseline (#2)


def _liked_world(tmp_path: Path, sink: CapturingSink):
    """A followed artist plus one owned live album whose liked track has left the source."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP, LIVE]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
        lidarr_album(LIVE, id=103, monitored=True),
    )
    ctx = make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink)
    ctx.state.record_monitored(
        [_owned_release(ReleaseKey("artist-1", "rg-3"), Reason(ReasonKind.LIKED, "sp-track-gone"), album_id=103)]
    )
    return ctx, lidarr


def test_a_guarded_source_keeps_its_baseline_so_the_next_run_is_refused_too(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The guard used to be refused once, then the shrunken count became the baseline."""
    ctx, lidarr = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        first = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        baseline = ctx.state.last_source_counts()
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert first == EXIT_GUARDED
    assert baseline["liked_tracks"] == 40, "a guarded source's baseline is not advanced"
    assert second == EXIT_GUARDED, "six hours later it is still refused"
    assert ReleaseKey("artist-1", "rg-3") in owned
    assert lidarr.album("artist-1", "rg-3").monitored is True  # type: ignore[union-attr]
    assert ("set_albums_monitored", ([103], False)) not in lidarr.calls


def test_a_guarded_run_still_advances_the_sources_that_were_not_guarded(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 3, "liked_tracks": 40})
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        baseline = ctx.state.last_source_counts()

    assert baseline["followed_artists"] == 1
    assert baseline["liked_tracks"] == 40


def test_a_source_removed_from_config_keeps_its_baseline_while_it_is_refused(
    tmp_path: Path, sink: CapturingSink
) -> None:
    ctx, _ = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_monitored(
            [
                _owned_release(
                    ReleaseKey("artist-1", "rg-3"), Reason(ReasonKind.PLAYLIST, "sp-t", "pl-gone"), album_id=103
                )
            ]
        )
        ctx.state.record_source_counts({"followed_artists": 1, "playlist:pl-gone": 50})
        first = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        baseline = ctx.state.last_source_counts()

    assert (first, second) == (EXIT_GUARDED, EXIT_GUARDED)
    assert baseline["playlist:pl-gone"] == 50


def test_a_guarded_followed_artist_keeps_its_baseline_so_the_next_run_is_refused_too(
    tmp_path: Path, sink: CapturingSink
) -> None:
    ctx, lidarr = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_followed_counts({"artist-1": 10})  # the catalogue now lists 2: -80%
        first = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        baseline = ctx.state.last_followed_counts()
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert first == EXIT_GUARDED
    assert baseline == {"artist-1": 10}
    assert second == EXIT_GUARDED
    assert ReleaseKey("artist-1", "rg-3") in owned
    assert lidarr.album("artist-1", "rg-3").monitored is True  # type: ignore[union-attr]


def test_a_schema_guarded_run_moves_no_source_baseline(tmp_path: Path, sink: CapturingSink) -> None:
    """A truncated read is exactly what the schema guard suspects; it must not become the baseline."""
    from dataclasses import replace

    ctx, _ = _liked_world(tmp_path, sink)
    with ctx:
        assert isinstance(ctx.source, FakeSource)
        ctx.source.snapshot = replace(ctx.source.snapshot, schema_ok=False)
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        first = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        baseline = ctx.state.last_source_counts()

    assert first == EXIT_GUARDED
    assert baseline == {"followed_artists": 1, "liked_tracks": 40}


def _unfollow_world(tmp_path: Path, sink: CapturingSink):
    """Two owned albums under a followed reason; the artist is then unfollowed among 20 followed."""
    source, lookup, _ = followed_world()
    source.snapshot = snapshot(artists=[], counts={"followed_artists": 19})
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist"),
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
    )
    ctx = make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink)
    followed = Reason(ReasonKind.FOLLOWED, "sp-a1")
    ctx.state.record_monitored(
        [
            _owned_release(ReleaseKey("artist-1", "rg-1"), followed, album_id=101),
            _owned_release(ReleaseKey("artist-1", "rg-2"), followed, album_id=102),
        ]
    )
    ctx.state.record_source_counts({"followed_artists": 20})
    ctx.state.record_followed_counts({"artist-1": 2})
    return ctx, lidarr


def test_an_ordinary_unfollow_goes_through_on_the_first_run(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _unfollow_world(tmp_path, sink)
    with ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.OK
    assert sink.last.counts["unmonitored"] == 2
    assert owned == {}
    assert lidarr.album("artist-1", "rg-1").monitored is False  # type: ignore[union-attr]


# --------------------------------------------------------------------------- --accept-shrink


def test_accept_shrink_on_a_hand_run_plan_carries_through_a_reviewed_apply(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _liked_world(tmp_path, sink)
    out = tmp_path / "diff.json"
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        assert run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True) == EXIT_GUARDED

        assert run_command(ctx, now=NOW, out=out, do_apply=False, accept_shrink=True) == EXIT_OK
        planned = read_diff(out)
        assert planned.accept_shrink is True and planned.guards == []
        assert [u.key.rg_mbid for u in planned.unmonitor] == ["rg-3"]
        assert lidarr.album("artist-1", "rg-3").monitored is True  # type: ignore[union-attr]

        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)
        baseline = ctx.state.last_source_counts()
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert lidarr.album("artist-1", "rg-3").monitored is False  # type: ignore[union-attr]
    assert ReleaseKey("artist-1", "rg-3") not in owned
    assert baseline["liked_tracks"] == 0, "accepted: the new count is the baseline"


def test_accept_shrink_apply_does_not_repeat_the_shrink_advice(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #182: the apply that carried out the accepted shrink must not tell the operator to go
    run `--accept-shrink` - they just did, and the diff they applied carries no shrink guard."""
    ctx, _lidarr = _liked_world(tmp_path, sink)
    out = tmp_path / "diff.json"
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        run_command(ctx, now=NOW, out=out, do_apply=False, accept_shrink=True)

        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_OK
    assert "--accept-shrink" not in sink.last.message


def test_a_guarded_apply_that_did_not_accept_the_shrink_still_carries_the_advice(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """A guarded apply (no --accept-shrink) still needs to tell the operator what to do about it."""
    ctx, _lidarr = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_GUARDED
    assert "--accept-shrink" in sink.last.message


def test_accept_shrink_is_refused_with_scheduled(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _liked_world(tmp_path, sink)
    with ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False, scheduled=True, accept_shrink=True)
        again = run_command(ctx, now=NOW, out=tmp_path / "d.json", do_apply=True, scheduled=True, accept_shrink=True)

    assert (code, again) == (EXIT_ERROR, EXIT_ERROR)
    assert lidarr.writes() == []
    assert not (tmp_path / "diff.json").exists()


def test_accept_shrink_is_refused_on_an_apply(tmp_path: Path, sink: CapturingSink) -> None:
    """It belongs on the plan a human reviews; an apply only carries what the diff recorded."""
    ctx, lidarr = _liked_world(tmp_path, sink)
    with ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, accept_shrink=True)

    assert code == EXIT_ERROR
    assert lidarr.writes() == []


def test_a_scheduled_run_will_not_apply_a_diff_that_carries_accept_shrink(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, lidarr = _liked_world(tmp_path, sink)
    out = tmp_path / "diff.json"
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        run_command(ctx, now=NOW, out=out, do_apply=False, accept_shrink=True)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True, scheduled=True)

    assert code == EXIT_ERROR
    assert lidarr.album("artist-1", "rg-3").monitored is True  # type: ignore[union-attr]


# --------------------------------------------------------------------------- projected-wanted is advisory (#3)


def _over_the_wanted_limit(tmp_path: Path, sink: CapturingSink):
    """The world of `_liked_world`, with a wanted-list limit of 1 that two monitored albums exceed."""
    from dataclasses import replace

    ctx, lidarr = _liked_world(tmp_path, sink)
    object.__setattr__(ctx.config, "guards", replace(ctx.config.guards, projected_wanted_max=1))
    return ctx, lidarr


def test_projected_wanted_over_the_limit_does_not_block_unmonitors(tmp_path: Path, sink: CapturingSink) -> None:
    """An un-like must still reach Lidarr when the wanted list is long."""
    ctx, lidarr = _over_the_wanted_limit(tmp_path, sink)
    with ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.OK
    assert sink.last.counts["unmonitored"] == 1
    assert lidarr.album("artist-1", "rg-3").monitored is False  # type: ignore[union-attr]
    assert ReleaseKey("artist-1", "rg-3") not in owned
    assert "advisory limit" in sink.last.message, "advisory: still reported, just not enforced"


def test_projected_wanted_over_the_limit_is_still_reported_on_a_dry_run(tmp_path: Path, sink: CapturingSink) -> None:
    ctx, _ = _over_the_wanted_limit(tmp_path, sink)
    with ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        diff = read_diff(tmp_path / "diff.json")

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.OK
    assert [g.code for g in diff.guards] == ["projected-wanted"]
    assert len(diff.unmonitor) == 1


# --------------------------------------------------------------------------- unmonitored artists (#11)


def _unmonitored_artist_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """The followed artist already exists in Lidarr, unmonitored, holding both albums."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist", monitored=False),
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
    )
    return source, lookup, lidarr


def test_a_dry_run_reports_the_unmonitored_artist_and_writes_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _unmonitored_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)

    assert result.diff.monitor_artists == ["artist-1"]
    assert lidarr.writes() == []


def test_apply_remonitors_an_artist_that_already_existed_unmonitored(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _unmonitored_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        again = plan(ctx, now=NOW, scheduled=False)

    assert code == EXIT_OK
    assert ("set_artists_monitored", [1]) in lidarr.calls
    assert lidarr.artists["artist-1"].monitored is True
    assert again.diff.monitor_artists == [], "converged: the next run has nothing to fix"


def test_apply_remonitors_a_new_artist_without_trusting_the_post_response(tmp_path: Path, sink: CapturingSink) -> None:
    """Lidarr answers monitored=true, then applies `addOptions.monitor: none` to the artist."""
    source, lookup, lidarr = followed_world()
    lidarr.unmonitor_added_artists = True
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.artists["artist-1"].monitored is True
    order = [n for n in lidarr.names() if n in {"add_artist", "refresh_artist", "set_artists_monitored"}]
    assert order == ["add_artist", "refresh_artist", "set_artists_monitored"], "after the refresh settles"


def _unmonitored_and_new_artist_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """`_unmonitored_artist_world` plus a second, brand-new followed artist ("Other", artist-2) that
    has to be added rather than re-monitored."""
    source, lookup, lidarr = _unmonitored_artist_world()
    theirs = rg("rg-9", "Theirs", artist_mbid="artist-2", artist_name="Other")
    lookup.add(theirs)
    lookup.catalogues["artist-2"] = ["rg-9"]
    lidarr.catalogue["artist-2"] = [theirs]
    source.snapshot = snapshot(
        artists=[
            artist_intent("Test Artist", spotify_id="sp-a1"),
            artist_intent("Other", spotify_id="sp-a2"),
        ]
    )
    return source, lookup, lidarr


def test_apply_does_not_count_a_newly_added_artist_as_re_monitored(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #182: a just-added artist is always re-monitored (Lidarr can silently unmonitor it on
    add), but that is part of the add, not a second event - `artists_monitored` must not count it,
    even though `set_artists_monitored` still covers it."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        _code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)

    assert diff.monitor_artists == []
    assert applied.artists_monitored == 0
    assert ("set_artists_monitored", [lidarr.artists["artist-1"].id]) in lidarr.calls


def test_the_apply_summary_says_zero_re_monitored_for_a_newly_added_artist(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert "       0 artists re-monitored" in capsys.readouterr().out.splitlines()


def test_apply_counts_only_the_re_monitor_the_plan_named_not_the_new_add(tmp_path: Path, sink: CapturingSink) -> None:
    """One existing unmonitored artist to re-monitor, plus one newly added artist: only the
    existing one counts, and `set_artists_monitored` still covers both ids."""
    source, lookup, lidarr = _unmonitored_and_new_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        _code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)

    assert diff.monitor_artists == ["artist-1"]
    assert applied.artists_monitored == 1
    expected_ids = {lidarr.artists["artist-1"].id, lidarr.artists["artist-2"].id}
    [sent] = [payload for name, payload in lidarr.calls if name == "set_artists_monitored"]
    assert set(sent) == expected_ids


def test_a_guarded_run_still_remonitors_the_artist(tmp_path: Path, sink: CapturingSink) -> None:
    """Re-monitoring never loses anything, so a guard on unmonitors has no reason to hold it back."""
    source, lookup, lidarr = _unmonitored_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_source_counts({"followed_artists": 100})  # 100 -> 1: shrink guard territory
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert lidarr.artists["artist-1"].monitored is True


def test_the_lock_is_reported_not_crashed_into(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.adapters.lock import run_lock

    with context_for(tmp_path, sink) as ctx, run_lock(ctx.lock_path):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert code == EXIT_BUSY  # #54: its own code, not the generic error
    assert sink.last.status is RunStatus.ERROR
    assert sink.last.exit_code == EXIT_BUSY
    assert sink.last.message == "another run holds the lock"


def test_a_scheduled_run_that_finds_the_lock_held_is_skipped_not_an_error(tmp_path: Path, sink: CapturingSink) -> None:
    """Two crons overlapping is not a failure: the run in progress is doing the work."""
    from likearr.adapters.lock import run_lock

    with context_for(tmp_path, sink) as ctx, run_lock(ctx.lock_path):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        last = ctx.state.last_run()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.SKIPPED
    assert sink.last.exit_code == EXIT_OK
    assert sink.last.message == "another run holds the lock"
    assert last is not None and last.status is RunStatus.SKIPPED
    assert ctx.lidarr.writes() == []  # type: ignore[attr-defined]


def test_a_paused_scheduled_run_does_nothing_and_publishes_paused(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #68 phase 1: `[schedule] enabled = false` stops a scheduled run cold, with no Spotify,
    MusicBrainz or Lidarr call at all - it never even reaches the source read."""
    source, lookup, lidarr = followed_world()
    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False, paused_reason="maintenance window"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        last = ctx.state.last_run()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.exit_code == EXIT_OK
    assert sink.last.message == "scheduled runs are paused: maintenance window"
    assert last is not None and last.status is RunStatus.PAUSED
    assert source.reads == 0
    assert lookup.calls == {}
    assert lidarr.calls == []


def test_a_paused_scheduled_run_with_no_reason_still_publishes(tmp_path: Path, sink: CapturingSink) -> None:
    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False))
    with make_context(tmp_path, sink=sink, config=config) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.message == "scheduled runs are paused: no reason given"


def test_a_hand_run_ignores_the_pause(tmp_path: Path, sink: CapturingSink) -> None:
    """Pause stops the cron line, never a plan or apply a human started, `--scheduled`
    or not."""
    source, lookup, lidarr = followed_world()
    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False, paused_reason="maintenance window"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=False)

    assert code == EXIT_OK
    assert sink.last.status is not RunStatus.PAUSED
    assert source.reads == 1


def test_a_paused_scheduled_run_takes_no_lock(tmp_path: Path, sink: CapturingSink) -> None:
    """A hand run applying at the same moment must not be made to wait behind a paused fire that
    would do nothing anyway."""
    from likearr.adapters.lock import run_lock

    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False, paused_reason="busy elsewhere"))
    with make_context(tmp_path, sink=sink, config=config) as ctx, run_lock(ctx.lock_path):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED


# ---------------------------------------------------------------- the first reviewed apply (#111)

FIRST_APPLY = "waiting for your first reviewed apply: connect Spotify, then review and apply your first plan"


def test_a_scheduled_run_before_the_first_reviewed_apply_does_nothing_and_publishes_paused(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """Issue #111: a fresh config (no `[schedule]` block, so the schedule is on) holds scheduled
    applies until a hand apply has happened: no Spotify, MusicBrainz or Lidarr call at all."""
    source, lookup, lidarr = followed_world()
    config = make_config(tmp_path)
    assert config.schedule.enabled, "the code default: on"
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config, first_applied=False
    ) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        last = ctx.state.last_run()
        first = ctx.state.first_apply_at()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.exit_code == EXIT_OK
    assert sink.last.message == FIRST_APPLY
    assert last is not None and last.status is RunStatus.PAUSED, "still recorded, like the paused path"
    assert first is None, "a scheduled run never records the first apply"
    assert source.reads == 0
    assert lookup.calls == {}
    assert lidarr.calls == []
    assert not (tmp_path / "diff.json").exists()


def test_a_scheduled_run_with_no_spotify_token_before_the_first_apply_is_paused_not_error(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The gate comes before the Spotify check, so a fire before Connect Spotify stops recording
    `error` on Status, in the health record and on MQTT."""
    _source, lookup, lidarr = followed_world()
    with make_context(tmp_path, lookup=lookup, lidarr=lidarr, sink=sink, first_applied=False) as ctx:
        ctx.source = None
        ctx.spotify_error = "spotify is not configured; Connect Spotify in Settings (or run `likearr auth`)"
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.message == FIRST_APPLY
    assert lidarr.calls == []


def test_the_first_apply_gate_takes_no_lock(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.adapters.lock import run_lock

    with make_context(tmp_path, sink=sink, first_applied=False) as ctx, run_lock(ctx.lock_path):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.message == FIRST_APPLY


def test_a_config_pause_is_reported_ahead_of_the_first_apply_gate(tmp_path: Path, sink: CapturingSink) -> None:
    """Both hold the run; the pause is what someone chose on purpose, so its reason is the one said."""
    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False, paused_reason="on holiday"))
    with make_context(tmp_path, sink=sink, config=config, first_applied=False) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert sink.last.message == "scheduled runs are paused: on holiday"


def test_a_hand_apply_records_the_first_apply_and_the_next_scheduled_run_applies(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """A hand `run --apply` never reads the gate, and once it has applied, scheduled runs go ahead."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, first_applied=False) as ctx:
        assert run_command(ctx, now=NOW, out=out) == EXIT_OK
        assert ctx.state.first_apply_at() is None, "a dry run is not an apply"

        assert run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True) == EXIT_OK
        assert sink.last.status is not RunStatus.PAUSED
        assert ctx.state.first_apply_at() == NOW

        reads = source.reads
        later = NOW + timedelta(hours=6)
        code = run_command(ctx, now=later, out=out, do_apply=True, scheduled=True)
        first = ctx.state.first_apply_at()

    assert code == EXIT_OK
    assert sink.last.status is not RunStatus.PAUSED
    assert sink.last.dry_run is False
    assert source.reads == reads + 1, "the scheduled run planned and applied as it always has"
    assert first == NOW, "a scheduled apply never moves the first apply"


def test_a_stale_hand_apply_is_not_the_first_apply(tmp_path: Path, sink: CapturingSink) -> None:
    """Only an apply that completes its apply step counts: a reviewed diff refused as stale
    applied nothing."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, first_applied=False) as ctx:
        run_command(ctx, now=NOW, out=out)
        lidarr.seed(lidarr_artist("artist-1"), lidarr_album(ALBUM, monitored=True))  # the world moved
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)
        first = ctx.state.first_apply_at()

    assert code == EXIT_STALE
    assert first is None


def test_with_no_state_database_a_scheduled_run_publishes_paused_and_creates_nothing(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """Issue #111: a brand-new install has no state DB, and a scheduled fire must not create one
    (the healthcheck reads a missing file as "no runs yet"). It is decided before a `Context` -
    which opens the database - is ever built, and it publishes to the sinks only."""
    from likearr.shell.run import scheduled_run_without_state

    config = make_config(tmp_path)

    code = scheduled_run_without_state(config, [sink], dry_run=False)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.PAUSED
    assert sink.last.exit_code == EXIT_OK
    assert sink.last.message == FIRST_APPLY
    assert not config.state_db.exists()
    assert list(tmp_path.iterdir()) == [], "no database, no lock file, nothing"


def test_with_no_state_database_a_config_pause_still_says_paused_and_creates_nothing(
    tmp_path: Path, sink: CapturingSink
) -> None:
    from likearr.shell.run import scheduled_run_without_state

    config = make_config(tmp_path, schedule=ScheduleConfig(enabled=False, paused_reason="not yet"))

    assert scheduled_run_without_state(config, [sink], dry_run=False) == EXIT_OK
    assert sink.last.message == "scheduled runs are paused: not yet"
    assert not config.state_db.exists()


def test_with_a_state_database_the_run_goes_on_to_run_command(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.adapters.state_sqlite import SqliteState
    from likearr.shell.run import scheduled_run_without_state

    config = make_config(tmp_path)
    SqliteState(config.state_db).close()

    assert scheduled_run_without_state(config, [sink], dry_run=False) is None
    assert sink.records == []


def test_a_dry_run_leaves_retained_sinks_untouched(tmp_path: Path) -> None:
    """Issue #19: a hand-run dry-run must not overwrite the retained MQTT record or reset the
    Home Assistant dead-man's-switch built on it. It still prints, and it still gets recorded."""
    source, lookup, lidarr = followed_world()
    local = CapturingSink(local=True)
    remote = CapturingSink(local=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[local, remote]) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        last = ctx.state.last_run()

    assert code == EXIT_OK
    assert local.records and local.last.dry_run is True
    assert remote.records == []
    assert last is not None and last.dry_run is True


def test_an_apply_publishes_to_every_sink(tmp_path: Path) -> None:
    source, lookup, lidarr = followed_world()
    local = CapturingSink(local=True)
    remote = CapturingSink(local=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[local, remote]) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert local.records and local.last.dry_run is False
    assert remote.records and remote.last.dry_run is False


def test_a_scheduled_skip_still_publishes_everywhere(tmp_path: Path) -> None:
    """A scheduled run always carries `--apply`, so its `skipped` record is not a dry run."""
    from likearr.adapters.lock import run_lock

    source, lookup, lidarr = followed_world()
    local = CapturingSink(local=True)
    remote = CapturingSink(local=False)
    with (
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[local, remote]) as ctx,
        run_lock(ctx.lock_path),
    ):
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert local.records and local.last.status is RunStatus.SKIPPED
    assert remote.records and remote.last.status is RunStatus.SKIPPED


# --------------------------------------------------------------------------- a problems-only webhook (#112)

HOOK = "https://hooks.example.invalid/likearr"


def _published(status: RunStatus, message: str = "", *, ts: int, dry_run: bool = False) -> HealthRecord:
    return HealthRecord(
        ts=ts,
        version="0.1.0",
        resolver_version=1,
        exit_code=0 if status is RunStatus.OK else 1,
        status=status,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={},
        unmapped=0,
        pending_album=0,
        message=message,
        dry_run=dry_run,
    )


def test_publish_compares_against_the_previous_published_run_skipping_paused_and_skipped(tmp_path: Path) -> None:
    """`_publish` reads the previous published status before recording this one, and a paused or
    skipped tick in between neither notifies nor hides the error it follows."""
    import httpx
    import respx

    from likearr.adapters.health import WebhookSink
    from likearr.config import WebhookSinkConfig
    from likearr.shell.run import _publish

    sink = WebhookSink(WebhookSinkConfig(url=HOOK, notify="problems"))
    sequence = [
        (_published(RunStatus.OK, ts=1), False),  # nothing before it, and clean: silent
        (_published(RunStatus.ERROR, "lidarr down", ts=2), True),  # a new problem
        (_published(RunStatus.ERROR, "lidarr down", ts=3), False),  # the same one again
        (_published(RunStatus.PAUSED, "paused", ts=4), False),  # never
        (_published(RunStatus.SKIPPED, "lock held", ts=5), False),  # never
        (_published(RunStatus.ERROR, "lidarr down", ts=6), False),  # still the same, past the idle ticks
        (_published(RunStatus.OK, ts=7, dry_run=True), False),  # a dry run reaches no webhook
        (_published(RunStatus.OK, ts=8), True),  # the recovery, once
        (_published(RunStatus.OK, ts=9), False),  # and quiet after it
    ]
    with make_context(tmp_path, sinks=[sink]) as ctx, respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        for record, expected in sequence:
            before = route.call_count
            _publish(ctx, record, diff=None)
            assert (route.call_count > before) is expected, record
        titles = [json.loads(call.request.content)["title"] for call in route.calls]

    assert titles == ["likearr: run failed", "likearr: back to ok"]


def test_a_problems_webhook_stays_quiet_through_a_clean_apply_and_a_pause(tmp_path: Path) -> None:
    import httpx
    import respx

    from likearr.adapters.health import WebhookSink
    from likearr.config import WebhookSinkConfig

    source, lookup, lidarr = followed_world()
    sink = WebhookSink(WebhookSinkConfig(url=HOOK, notify="problems"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[sink]) as ctx, respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        first = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        ctx.config = replace(ctx.config, schedule=ScheduleConfig(enabled=False, paused_reason="away"))
        second = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert (first, second) == (EXIT_OK, EXIT_OK)
    assert not route.called


# --------------------------------------------------------------------------- the apply-phase marker (issue #68 phase 3)


def test_the_apply_phase_marker_is_printed_once_before_the_apply(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.models import PHASE_MARKER_APPLY

    with context_for(tmp_path, sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert err.count(PHASE_MARKER_APPLY) == 1


def test_the_apply_phase_marker_is_never_printed_on_a_dry_run(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.models import PHASE_MARKER_APPLY

    with context_for(tmp_path, sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert code == EXIT_OK
    assert PHASE_MARKER_APPLY not in capsys.readouterr().err


# --------------------------------------------------------------------------- Spotify snapshot reuse


def test_a_scheduled_run_saves_its_spotify_read_for_a_later_reuse(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written right after the read - inspected here before the run's own end-of-plan cleanup
    would otherwise remove it, by disabling that cleanup for this one test."""
    from likearr.shell import spotify_snapshot

    monkeypatch.setattr(spotify_snapshot, "delete_snapshot", lambda config: None)
    with context_for(tmp_path, sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=True)

    saved = spotify_snapshot.read_snapshot(ctx.config, now=NOW)
    assert saved is not None
    assert saved.artists == result.snapshot.artists


def test_a_scheduled_run_reuses_a_fresh_snapshot_with_zero_spotify_calls(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell import spotify_snapshot

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        spotify_snapshot.write_snapshot(ctx.config, replace(source.snapshot, fetched_at=NOW))

        result = plan(ctx, now=NOW + timedelta(minutes=29), scheduled=True)

    assert source.reads == 0
    assert result.snapshot.artists == source.snapshot.artists


def test_a_snapshot_31_minutes_old_is_ignored(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell import spotify_snapshot

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        spotify_snapshot.write_snapshot(ctx.config, replace(source.snapshot, fetched_at=NOW))

        plan(ctx, now=NOW + timedelta(minutes=31), scheduled=True)

    assert source.reads == 1


def test_a_snapshot_is_ignored_after_the_sources_config_changes(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.config import SpotifyConfig
    from likearr.shell import spotify_snapshot

    source, lookup, lidarr = followed_world()
    written_config = make_config(
        tmp_path, spotify=SpotifyConfig(token_file=tmp_path / "spotify-token.json", playlists=())
    )
    spotify_snapshot.write_snapshot(written_config, replace(source.snapshot, fetched_at=NOW))
    changed_config = make_config(
        tmp_path, spotify=SpotifyConfig(token_file=tmp_path / "spotify-token.json", playlists=("pl-new",))
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=changed_config) as ctx:
        plan(ctx, now=NOW + timedelta(minutes=1), scheduled=True)

    assert source.reads == 1


def test_a_hand_run_never_reuses_a_saved_snapshot(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell import spotify_snapshot

    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        spotify_snapshot.write_snapshot(ctx.config, replace(source.snapshot, fetched_at=NOW))

        plan(ctx, now=NOW + timedelta(minutes=1), scheduled=False)

    assert source.reads == 1


def test_the_saved_snapshot_is_deleted_once_a_plan_completes(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.shell import spotify_snapshot

    with context_for(tmp_path, sink) as ctx:
        plan(ctx, now=NOW, scheduled=True)

        assert spotify_snapshot.read_snapshot(ctx.config, now=NOW) is None


# --------------------------------------------------------------------------- the quota guard


def test_a_scheduled_run_with_quota_exceeded_is_skipped_not_an_error(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.ports import QuotaExceeded

    source = FakeSource(error=QuotaExceeded("spotify: 429 QUOTA_EXCEEDED"))
    with make_context(tmp_path, source=source, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        last = ctx.state.last_run()

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.SKIPPED
    assert sink.last.exit_code == EXIT_OK
    assert sink.last.message == "Spotify quota exceeded"
    assert last is not None and last.status is RunStatus.SKIPPED


def test_a_hand_run_with_quota_exceeded_still_errors(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.ports import QuotaExceeded

    source = FakeSource(error=QuotaExceeded("spotify: 429 QUOTA_EXCEEDED"))
    with make_context(tmp_path, source=source, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False, scheduled=False)

    assert code == EXIT_ERROR
    assert sink.last.status is RunStatus.ERROR


def test_a_config_refusal_is_decided_before_planning_and_stays_local(tmp_path: Path) -> None:
    """Refusing a diff whose config moved costs a file read, not a plan held under the lock, and it
    is the user's own doing - so it must not light Home Assistant's amber or reset its dead-man's
    switch. It still prints, and it still lands in the runs table with its reason."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    local = CapturingSink(local=True)
    remote = CapturingSink(local=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[local, remote]) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        _deny(ctx, DENIED_RG)
        before = len(lidarr.calls)
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)
        last = ctx.state.last_run()

    assert code == EXIT_STALE
    assert lidarr.calls[before:] == []  # no fresh plan: Lidarr was not even read
    assert local.last.status is RunStatus.STALE
    assert "[rules] deny_releases" in local.last.message
    assert remote.records == []
    assert last is not None and last.status is RunStatus.STALE and last.dry_run is False
    assert "[rules] deny_releases" in last.message


def test_a_world_moved_refusal_still_publishes_everywhere(tmp_path: Path) -> None:
    """Only the config refusal is kept local: Spotify or Lidarr moving under a reviewed plan is
    news Home Assistant has always carried, and still does."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    local = CapturingSink(local=True)
    remote = CapturingSink(local=False)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sinks=[local, remote]) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        raw = json.loads(out.read_text())
        raw["source_digest"] = "moved"
        out.write_text(json.dumps(raw))
        code = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True)

    assert code == EXIT_STALE
    assert remote.records and remote.last.status is RunStatus.STALE
    assert "no longer matches Spotify and Lidarr" in remote.last.message


def test_an_unexpected_exception_still_publishes_health(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = followed_world()

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(FakeLidarr, "load_view", explode)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert code == EXIT_ERROR
    assert sink.last.status is RunStatus.ERROR
    assert sink.last.message.startswith("ZeroDivisionError:")


def test_a_ratchet_moves_the_artist_to_full(tmp_path: Path, sink: CapturingSink) -> None:
    """A saved live album needs the Full profile, and the ratchet is recorded as one-way."""
    saved = spotify_album("Live At Somewhere", spotify_id="sp-live", upc="222")
    lookup = FakeLookup().add(LIVE)
    lookup.barcodes["222"] = "rg-3"
    source = FakeSource(snapshot(albums=[album_intent(saved)]))
    artist = lidarr_artist("artist-1", id=1, name="Test Artist", metadata_profile_id=LEAN_ID)
    lidarr = FakeLidarr(catalogue={"artist-1": [LIVE]})
    lidarr.seed(artist, lidarr_album(LIVE, id=103, monitored=False))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned_artists = ctx.state.owned_artists()

    assert code == EXIT_OK
    assert lidarr.artists["artist-1"].metadata_profile_id == FULL_ID
    assert owned_artists["artist-1"].profile is Profile.FULL
    assert owned_artists["artist-1"].added_by_us is False, "a ratchet must not claim we added them"


def test_a_release_group_lidarr_does_not_have_is_reported_not_fatal(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM]})  # the EP never appears
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        exit_code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)

    assert exit_code == EXIT_OK
    assert applied.monitored == 1
    assert applied.unmapped_in_lidarr == ["artist-1/rg-2"]


def test_a_wider_reason_set_is_written_through_apply(tmp_path: Path, sink: CapturingSink) -> None:
    """The album is already owned for being followed; it becomes saved too this run. Apply must
    write the wider reason set to state itself, not just leave `diff.update_reasons` sitting there
    unapplied (issue #69, issue #132, kills M11)."""
    saved = spotify_album("First Album", spotify_id="sp-alb1", upc="111")
    lookup = FakeLookup().add(ALBUM)
    lookup.catalogues["artist-1"] = ["rg-1"]
    lookup.barcodes["111"] = "rg-1"
    source = FakeSource(
        snapshot(
            artists=[artist_intent("Test Artist", spotify_id="sp-a1")],
            albums=[album_intent(saved)],
        )
    )
    artist = lidarr_artist("artist-1", id=1, name="Test Artist")
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM]})
    lidarr.seed(artist, lidarr_album(ALBUM, id=101, monitored=True))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored(
            [_owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.FOLLOWED, "sp-a1"), album_id=101)]
        )
        exit_code, _applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)
        owned = ctx.state.owned_releases()

    assert exit_code == EXIT_OK
    assert [key for key, _ in diff.update_reasons] == [ReleaseKey("artist-1", "rg-1")]
    assert owned[ReleaseKey("artist-1", "rg-1")].reasons == frozenset(
        {Reason(ReasonKind.FOLLOWED, "sp-a1"), Reason(ReasonKind.SAVED, "sp-alb1")}
    )


def test_pending_intents_move_the_pending_clock(tmp_path: Path, sink: CapturingSink) -> None:
    """A liked single with no album yet is marked pending once, and cleared when it lands."""
    single = rg("rg-single", "A Song", primary=PrimaryType.SINGLE, released="2026-09-01")
    album = rg("rg-album", "The Album")
    spotify_single = spotify_album(
        "A Song", spotify_id="sp-single", upc="333", album_type="single", released="2026-09-01"
    )
    from tests.unit.fakes import track_intent

    lookup = FakeLookup().add(single)
    lookup.barcodes["333"] = "rg-single"
    source = FakeSource(snapshot(tracks=[track_intent("A Song", spotify_single, spotify_id="sp-t1")]))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)
        assert len(result.diff.pending) == 1
        assert ctx.state.pending_since("liked:sp-t1") == NOW

        # The album lands; the pending row goes away.
        lookup.add(album)
        lookup.tracklists["rg-album"] = ["A Song"]
        lookup.catalogues["artist-1"] = ["rg-album", "rg-single"]
        later = NOW + timedelta(days=30)
        second = plan(ctx, now=later, scheduled=False)
        assert second.diff.pending == []
        assert ctx.state.pending_since("liked:sp-t1") is None


def _owned_release(key: ReleaseKey, reason: Reason, *, album_id: int):
    from likearr.models import OwnedRelease

    return OwnedRelease(
        key=key,
        reasons=frozenset({reason}),
        step="test",
        resolver_version=1,
        monitored_at=NOW,
        lidarr_album_id=album_id,
    )


def test_diff_file_round_trips(tmp_path: Path, sink: CapturingSink) -> None:
    with context_for(tmp_path, sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)
    path = tmp_path / "round.json"
    write_diff(result.diff, path)
    reloaded = read_diff(path)

    assert reloaded.source_digest == result.diff.source_digest
    assert reloaded.lidarr_digest == result.diff.lidarr_digest
    assert [a.artist_mbid for a in reloaded.add_artists] == [a.artist_mbid for a in result.diff.add_artists]
    assert {m.key for m in reloaded.monitor} == {m.key for m in result.diff.monitor}
    assert {r for m in reloaded.monitor for r in m.reasons} == {r for m in result.diff.monitor for r in m.reasons}
    assert reloaded.created_at == result.diff.created_at


def test_a_diff_from_another_resolver_version_is_refused(tmp_path: Path, sink: CapturingSink) -> None:
    import json

    with context_for(tmp_path, sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)
        path = tmp_path / "old.json"
        write_diff(result.diff, path)
        raw = json.loads(path.read_text())
        raw["resolver_version"] = 99
        path.write_text(json.dumps(raw))
        code = run_command(ctx, now=NOW, out=path, apply_path=path, do_apply=True)

    assert code == EXIT_ERROR


def test_now_defaults_to_the_wall_clock(tmp_path: Path, sink: CapturingSink) -> None:
    with context_for(tmp_path, sink) as ctx:
        run_command(ctx, out=tmp_path / "diff.json", do_apply=False)
    assert abs(sink.last.ts - int(datetime.now(UTC).timestamp())) < 60


def test_a_name_collision_makes_the_run_degraded_not_guarded(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 means "unmonitors were refused"; a refused *add* loses nothing, so it stays exit 0."""
    album = rg("rg-new", "Anything", artist_mbid="mbid-new", artist_name="Lawrence")
    lookup = FakeLookup().add(album)
    lookup.barcodes["UPC-NEW"] = "rg-new"
    source = FakeSource(snapshot(albums=[album_intent(spotify_album("Anything", spotify_id="sp-al", upc="UPC-NEW"))]))
    lidarr = FakeLidarr()
    lidarr.seed(lidarr_artist("mbid-existing", id=9001, name="Lawrence"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    out = capsys.readouterr().out
    assert code == EXIT_OK, "a refused *add* loses nothing, so it is never exit 2"
    assert sink.last.status is RunStatus.OK, "a first run has no baseline, so nothing is new yet"
    assert sink.last.name_collisions == 1, "reported as a count from the very first run"
    assert "1 artist(s) NOT added - Lidarr already has the name" in out, "and named in full, always"
    assert "Lawrence" in sink.last.message
    assert lidarr.writes() == [], "a dry run writes nothing either way"


def test_a_name_collision_degrades_once_there_is_a_baseline_to_call_it_new_against(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """Still degraded rather than guarded: exit 2 means unmonitors were refused, and none were."""
    source, lookup, lidarr = collision_world(tmp_path)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK
    assert sink.last.status is RunStatus.DEGRADED
    assert sink.last.new_conditions == ["new-name-collision"]


def test_an_accepted_name_collision_stops_being_news(tmp_path: Path, sink: CapturingSink) -> None:
    """A user may have collisions they have decided to live with; without this they would pin amber for ever."""
    source, lookup, lidarr = collision_world(tmp_path)
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True)
        assert run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True) == EXIT_OK
        assert sink.last.status is RunStatus.DEGRADED

        run_command(ctx, now=NOW, out=out, do_apply=False)
        accepted = run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True, accept_health=True, force=True)
        after = run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True)

    assert (accepted, after) == (EXIT_OK, EXIT_OK)
    assert sink.last.status is RunStatus.OK
    assert sink.last.name_collisions == 1, "still counted, just no longer news"


def test_accept_health_is_refused_on_a_scheduled_run_and_on_a_dry_run(tmp_path: Path, sink: CapturingSink) -> None:
    """In a cron line it would silence class B for good, which is the defect this mechanism fixes."""
    source, lookup, lidarr = collision_world(tmp_path)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        scheduled = run_command(
            ctx, now=NOW, out=tmp_path / "d.json", do_apply=True, scheduled=True, accept_health=True
        )
        dry = run_command(ctx, now=NOW, out=tmp_path / "d.json", accept_health=True)

    assert (scheduled, dry) == (EXIT_ERROR, EXIT_ERROR)
    assert sink.records == [], "a usage error is refused before anything runs, so nothing is published"
    assert lidarr.writes() == []


def collision_world(tmp_path: Path) -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """One wanted artist whose name Lidarr already holds, with two releases at stake."""
    first = rg("rg-1", "One", artist_mbid="mbid-wanted", artist_name="Lawrence")
    second = rg("rg-2", "Two", artist_mbid="mbid-wanted", artist_name="Lawrence")
    lookup = FakeLookup().add(first, second)
    lookup.barcodes["UPC-1"] = "rg-1"
    lookup.barcodes["UPC-2"] = "rg-2"
    source = FakeSource(
        snapshot(
            albums=[
                album_intent(spotify_album("One", spotify_id="sp-1", upc="UPC-1")),
                album_intent(spotify_album("Two", spotify_id="sp-2", upc="UPC-2")),
            ]
        )
    )
    lidarr = FakeLidarr()
    lidarr.seed(lidarr_artist("mbid-existing", id=9001, name="Lawrence"))
    del tmp_path
    return source, lookup, lidarr


@dataclass(slots=True)
class FakeDetails:
    """An `ArtistDetails` from a dictionary, so a test can stage a missing disambiguation."""

    by_mbid: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def artist_disambiguation(self, artist_mbid: str) -> str:
        self.calls.append(artist_mbid)
        return self.by_mbid.get(artist_mbid, "")


def test_the_summary_names_both_artists_and_what_the_skip_cost(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = collision_world(tmp_path)
    details = FakeDetails(
        {
            "mbid-wanted": "Germany DJ & producer",
            "mbid-existing": "Clyde Lawrence and Gracie Lawrence",
        }
    )
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, artist_details=details) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    out = capsys.readouterr().out
    assert "1 artist(s) NOT added - Lidarr already has the name (2 release(s) unmonitored" in out
    assert "'Lawrence': 2 release(s) skipped" in out
    assert "wanted:   mbid-wanted - Germany DJ & producer" in out
    assert "in Lidarr: id 9001 mbid-existing - Clyde Lawrence and Gracie Lawrence" in out


def test_the_collision_advice_is_what_lidarr_can_actually_do(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #32. Lidarr copies an artist's name from MusicBrainz on every refresh
    (`Artist.ApplyChanges` never copies Name) and throws `MultipleArtistsFoundException` on import
    when two share one, so "add them under a distinct name" was advice nobody could follow."""
    source, lookup, lidarr = collision_world(tmp_path)
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, artist_details=FakeDetails()
    ) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    out = " ".join(capsys.readouterr().out.split())
    assert "distinct name" not in out
    assert "Lidarr takes an artist's name from MusicBrainz, so it cannot hold one under a different name" in out
    assert (
        "Keep the one Lidarr has, or add both and import the other's downloads by hand with Lidarr's "
        "Manual Import, which may work" in out
    )
    assert "If the match looks wrong, `likearr explain <name>` shows why likearr wanted it" in out

    message = sink.last.message
    assert "distinct name" not in message
    assert "keep one, or add both and import the other's downloads by hand" in message
    assert "`likearr explain Lawrence` shows why" in message


def test_a_same_name_ambiguity_is_one_unmapped_intent_and_no_collision(tmp_path: Path, sink: CapturingSink) -> None:
    """Issue #32: the wrong Jungle was chosen, and the collision guard then refused it,
    so the run went degraded for a guess. Refusing the guess instead is a plain unmapped intent."""
    london = rg("rg-london", "Jungle", artist_mbid="mbid-london", artist_name="Jungle", released="2014-07-14")
    us = rg("rg-1969", "Jungle", artist_mbid="mbid-us", artist_name="Jungle", released="1969-01-01")
    lookup = FakeLookup().add(london, us)
    liked = track_intent("Busy Earnin'", spotify_album("Jungle", artists=("Jungle",)), artists=("Jungle",))
    source = FakeSource(snapshot(tracks=[liked]))
    lidarr = FakeLidarr()
    lidarr.seed(lidarr_artist("mbid-london", id=9003, name="Jungle"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    assert code == EXIT_OK
    assert sink.last.unmapped == 1
    assert sink.last.name_collisions == 0, "nothing was guessed, so there is nothing to collide"
    assert lidarr.writes() == []


def test_a_missing_disambiguation_degrades_gracefully(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = collision_world(tmp_path)
    with make_context(
        tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, artist_details=FakeDetails()
    ) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    out = capsys.readouterr().out
    assert "'Lawrence': 2 release(s) skipped" in out
    assert "wanted:   mbid-wanted\n" in out, "no disambiguation, and no dangling separator"
    assert " - " not in out.split("wanted:   mbid-wanted")[1].split("\n")[0]


def test_the_diff_file_carries_the_collision(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = collision_world(tmp_path)
    out = tmp_path / "diff.json"
    details = FakeDetails({"mbid-wanted": "Germany DJ & producer"})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, artist_details=details) as ctx:
        run_command(ctx, now=NOW, out=out)

    raw = json.loads(out.read_text())
    assert raw["summary"]["name_collisions"] == 1
    assert raw["summary"]["releases_dropped_to_collisions"] == 2
    row = raw["name_collisions"][0]
    assert row["name"] == "Lawrence"
    assert row["wanted_mbid"] == "mbid-wanted"
    assert row["wanted_disambiguation"] == "Germany DJ & producer"
    assert row["existing_lidarr_id"] == 9001
    assert row["dropped_releases"] == 2

    restored = read_diff(out).name_collisions[0]
    assert (restored.name, restored.dropped_releases) == ("Lawrence", 2)
    assert restored.wanted_disambiguation == "Germany DJ & producer"


# --------------------------------------------------------------------------- the health baseline


_PRE_CHANGE_KEYS = {
    "ts",
    "version",
    "resolver_version",
    "exit_code",
    "status",
    "spotify_ok",
    "spotify_schema_ok",
    "mb_ok",
    "lidarr_ok",
    "lidarr_metadata_ok",
    "counts",
    "unmapped",
    "pending_album",
    "message",
    "dry_run",
}


def test_the_published_record_still_carries_every_key_it_used_to(tmp_path: Path, sink: CapturingSink) -> None:
    """Home Assistant reads this payload. Nothing may be renamed or dropped, only added."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    published = sink.last.to_dict()

    assert set(published) >= _PRE_CHANGE_KEYS
    assert set(sink.last.counts) >= {"followed_artists", "desired", "monitored", "unmonitored", "added"}
    assert "intents" in sink.last.counts, "the denominator of unmapped_ratio, so the payload is auditable"


def test_a_dry_run_never_advances_the_baseline(tmp_path: Path, sink: CapturingSink) -> None:
    """Otherwise the apply the user actually reads would compare against their own dry run and see nothing.

    The same reasoning that keeps the shrink baselines out of dry runs.
    """
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")
        assert ctx.state.health_baseline() is None
        assert sink.last.baseline_advanced is False

        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        assert ctx.state.health_baseline() is not None
        assert sink.last.baseline_advanced is True


def test_a_run_that_never_applied_leaves_the_baseline_alone(tmp_path: Path, sink: CapturingSink) -> None:
    """A stale diff, a source outage and a skipped scheduled run all observed nothing."""
    source, lookup, lidarr = followed_world()
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=False)
        source.snapshot = snapshot(
            artists=[artist_intent("Test Artist", spotify_id="sp-a1"), artist_intent("Other", spotify_id="sp-a2")]
        )
        assert run_command(ctx, now=NOW, out=out, apply_path=out, do_apply=True) == EXIT_STALE
        assert ctx.state.health_baseline() is None

        source.error = SourceError("spotify is down")
        assert run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True) == EXIT_ERROR
        assert ctx.state.health_baseline() is None


def test_a_guarded_apply_still_advances_the_mapping_baseline(tmp_path: Path, sink: CapturingSink) -> None:
    """The apply ran: adds and monitors went through and only the unmonitors were refused."""
    ctx, _lidarr = _liked_world(tmp_path, sink)
    with ctx:
        ctx.state.record_source_counts({"followed_artists": 1, "liked_tracks": 40})
        assert run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True) == EXIT_GUARDED

        baseline = ctx.state.health_baseline()

    assert baseline is not None, "a guarded run observed the library perfectly well"
    assert sink.last.status is RunStatus.GUARDED
    assert sink.last.baseline_advanced is True


def test_the_chronic_library_reports_ok_from_the_second_run_on(tmp_path: Path, sink: CapturingSink) -> None:
    """A real library, in miniature: a standing set of conditions that never clears.

    Run one absorbs it and says so; run two compares and finds nothing new. Before this change
    every one of these runs published `degraded` for ever, which is why HA was told to ignore it.
    """
    source, lookup, lidarr = collision_world(tmp_path)
    lookup.fail = {"release_group_track_titles"}
    out = tmp_path / "diff.json"
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True)
        first = sink.last
        run_command(ctx, now=NOW, out=out, do_apply=True, scheduled=True)
        second = sink.last

    assert (first.baseline, first.status) == ("first-run", RunStatus.OK)
    assert second.baseline == "compared"
    assert second.name_collisions == 1, "still counted every run"
    assert second.unmapped == first.unmapped, "and the chronic shortfall is still published"


def test_a_musicbrainz_outage_degrades_however_quiet_the_rest_of_the_run_is(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """Rule 6 carries more weight than it looks: cached resolutions mean an outage barely moves
    the unmapped set, so the jump rule would never catch this on its own."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        assert sink.last.status is RunStatus.OK

        lookup.fail = {"artist_release_groups"}
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert sink.last.mb_ok is False
    assert sink.last.mb_errors > 0
    assert sink.last.status is RunStatus.DEGRADED
    assert "mb-outage" in sink.last.new_conditions


def test_a_composers_catalogue_too_large_for_a_liked_tracks_title_search_is_no_outage(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#151: the title search for a liked Bach track cannot browse Bach's catalogue. That is a
    permanent property of the artist, not MusicBrainz failing, so the run is not degraded and there
    is no `mb-outage`; the track lands on the compilation Spotify named."""
    comp = rg(
        "rg-comp",
        "Baroque Favourites",
        artist_mbid="mb-bach",
        artist_name="Bach",
        secondary=[SecondaryType.COMPILATION],
    )
    lookup = FakeLookup(
        searches={("Bach", "Baroque Favourites"): "rg-comp"},
        fail={"artist_release_groups"},
        fail_error=CatalogueTooLarge("artist mb-bach has more than 3000 release groups; not browsing further"),
    ).add(comp)
    liked = track_intent("Air", spotify_album("Baroque Favourites", artists=("Bach",)), artists=("Bach",))
    source = FakeSource(snapshot(tracks=[liked]))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False, scheduled=True)

    assert sink.last.mb_ok is True
    assert sink.last.status is RunStatus.OK
    assert "mb-outage" not in sink.last.new_conditions


def test_a_dry_run_does_not_claim_to_have_established_the_baseline(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """It writes none, so saying it did would have the operator re-running a plan for ever."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")

    out = capsys.readouterr().out

    assert "the next apply will establish it" in out
    assert "this run establishes the baseline" not in out


def test_the_record_does_not_claim_a_baseline_write_that_failed(
    tmp_path: Path, sink: CapturingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bookkeeping is best-effort and must never fail a good run - but it must not lie either."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        monkeypatch.setattr(
            ctx.state,
            "record_health_baseline",
            lambda _baseline: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
        )
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_OK, "a failed write never fails the run"
    assert sink.last.baseline_advanced is False


# ---------------------------------------------- issue #42: an answer an outage produced is not kept


class _LidarrFinds(FakeLidarr):
    """A Lidarr whose metadata name search finds these release groups, whatever it is asked."""

    def __init__(self, *found: ReleaseGroup) -> None:
        super().__init__()
        self.found = found

    def search_release_group_candidates(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        return self.found


def _saved_via_fallback(tmp_path: Path, sink: CapturingSink, *, mb_down: bool):
    from likearr.adapters.lookup import CompositeLookup

    album = rg("rg-fallback", "Fallback Album", artist_mbid="artist-f", artist_name="Fallback Band")
    lookup = FakeLookup()
    if mb_down:
        lookup.fail = {"search_release_group_candidates"}
    intent = album_intent(spotify_album("Fallback Album", spotify_id="sp-fb", artists=("Fallback Band",)))
    lidarr = _LidarrFinds(album)
    with make_context(tmp_path, source=FakeSource(snapshot(albums=[intent])), lidarr=lidarr, sink=sink) as ctx:
        composite = CompositeLookup(lookup, lidarr)
        ctx.lookup, ctx.composite = composite, composite
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")
        cached = ctx.state.cached_resolution(intent.reason.key, RESOLVER_VERSION)
    return read_diff(tmp_path / "diff.json"), cached


def test_an_answer_found_only_because_musicbrainz_failed_is_used_but_not_cached(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """It is this run's best answer, so the plan uses it; but it came from Lidarr's name search
    while MusicBrainz was down, so it is asked again - of MusicBrainz - on the next run."""
    diff, cached = _saved_via_fallback(tmp_path, sink, mb_down=True)

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-fallback"]
    assert cached is None


def test_the_same_answer_after_a_plain_musicbrainz_miss_is_cached_as_before(
    tmp_path: Path, sink: CapturingSink
) -> None:
    diff, cached = _saved_via_fallback(tmp_path, sink, mb_down=False)

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-fallback"]
    assert cached is not None and cached.release_group is not None
    assert cached.release_group.mbid == "rg-fallback"


# ---------------------------------------------- issue #53: any answer reached after an MB error


_LONDON = rg("rg-london", "Jungle", artist_mbid="jungle-london", artist_name="Jungle", released="2014-07-14")
_US_1969 = rg("rg-us", "Jungle", artist_mbid="jungle-us", artist_name="Jungle", released="1969-01-01")
_BUSY_ISRC = "GBBKS1400112"


def _jungle_world(tmp_path: Path, sink: CapturingSink, *, mb_down: bool, lidarr: FakeLidarr, pending_from=None):
    """Busy Earnin' liked on Jungle's "Jungle", plus a saved "Jungle" by Jungle, with MusicBrainz's
    name search down or merely empty; the ISRC is still answered, as from MusicBrainz's cache."""
    from likearr.adapters.lookup import CompositeLookup

    lookup = FakeLookup(isrcs={_BUSY_ISRC: ["rg-london"]}).add(_LONDON, _US_1969)
    lookup.candidate_searches[("Jungle", "Jungle")] = []
    if mb_down:
        lookup.fail = {"search_release_group_candidates"}
    liked = track_intent(
        "Busy Earnin'", spotify_album("Jungle", artists=("Jungle",)), isrc=_BUSY_ISRC, artists=("Jungle",)
    )
    saved = album_intent(spotify_album("Jungle", spotify_id="sp-jungle", artists=("Jungle",)))
    source = FakeSource(snapshot(albums=[saved], tracks=[liked]))
    with make_context(tmp_path, source=source, lidarr=lidarr, sink=sink) as ctx:
        if pending_from is not None:
            ctx.state.mark_pending(liked.reason.key, pending_from)
        composite = CompositeLookup(lookup, lidarr)
        ctx.lookup, ctx.composite = composite, composite
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")
        cached = {i.reason.key: ctx.state.cached_resolution(i.reason.key, RESOLVER_VERSION) for i in (liked, saved)}
        pending = ctx.state.pending_since(liked.reason.key)
    return read_diff(tmp_path / "diff.json"), cached, liked.reason.key, saved.reason.key, pending


def test_an_isrc_stand_in_answer_after_a_musicbrainz_error_is_used_but_not_cached(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The gap in #42's fix. The name search errors, Lidarr finds nothing, and the ISRC names the
    release: no Lidarr release group is involved, but MusicBrainz still never answered the search."""
    diff, cached, liked, _, _ = _jungle_world(tmp_path, sink, mb_down=True, lidarr=FakeLidarr())

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-london"]
    assert cached[liked] is None


def test_the_same_isrc_stand_in_answer_after_a_plain_miss_is_cached(tmp_path: Path, sink: CapturingSink) -> None:
    diff, cached, liked, _, _ = _jungle_world(tmp_path, sink, mb_down=False, lidarr=FakeLidarr())

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-london"]
    answer = cached[liked]
    assert answer is not None and answer.release_group is not None
    assert answer.release_group.mbid == "rg-london"


def test_two_same_named_artists_behind_the_fallback_through_a_whole_run(tmp_path: Path, sink: CapturingSink) -> None:
    """#53's shell-level two-artist case. The liked track's ISRC picks the London band and that is
    monitored, uncached; the saved album has no ISRC, so it is ambiguous and monitors nothing."""
    from likearr.core.resolver import AMBIGUOUS_SAME_NAME_STEP

    diff, cached, liked, saved, _ = _jungle_world(tmp_path, sink, mb_down=True, lidarr=_LidarrFinds(_US_1969, _LONDON))

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-london"], "never the 1969 band's album"
    assert [(u.intent_key, u.step) for u in diff.unmapped] == [(saved, AMBIGUOUS_SAME_NAME_STEP)]
    assert cached == {liked: None, saved: None}


def test_an_answer_reached_after_a_musicbrainz_error_leaves_the_pending_clock_alone(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """A provisional answer never clears the clock: a track that was pending keeps it, so an outage
    cannot restart the singles-fallback wait by resolving it once, provisionally."""
    since = NOW - timedelta(days=30)

    _, _, _, _, pending = _jungle_world(tmp_path, sink, mb_down=True, lidarr=FakeLidarr(), pending_from=since)

    assert pending == since


def test_a_waiting_track_reached_after_a_musicbrainz_error_still_starts_its_pending_clock(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """If MusicBrainz's search failed the same way every run, a clock that only a non-provisional
    answer could start would never start, and the singles fallback would never fire."""
    from likearr.adapters.lookup import CompositeLookup

    single = rg("rg-single", "Song", primary=PrimaryType.SINGLE, released="2026-09-01")
    lookup = FakeLookup(isrcs={"USAAA2600001": ["rg-single"]}).add(single)
    lookup.fail = {"search_release_group_candidates"}
    liked = track_intent("Song", spotify_album("Song"), isrc="USAAA2600001")
    lidarr = FakeLidarr()
    with make_context(tmp_path, source=FakeSource(snapshot(tracks=[liked])), lidarr=lidarr, sink=sink) as ctx:
        composite = CompositeLookup(lookup, lidarr)
        ctx.lookup, ctx.composite = composite, composite
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")
        pending = ctx.state.pending_since(liked.reason.key)
        cached = ctx.state.cached_resolution(liked.reason.key, RESOLVER_VERSION)

    assert composite.mb_failure_count > 0, "the search did fail"
    assert pending == NOW
    assert cached is None


def _outage_shaped(tmp_path: Path, sink: CapturingSink, *, attempts: int, failures: int) -> Context:
    return _composite_world(
        tmp_path,
        sink,
        lidarr_metadata_new_failures=("album-search:Leopold Stokowski|Rhapsody",),
        lidarr_metadata_any_success=True,
        lidarr_metadata_attempts=attempts,
        lidarr_metadata_attempt_failures=failures,
    )


def test_no_negative_cache_entry_when_the_run_looks_like_a_lidarr_outage(tmp_path: Path, sink: CapturingSink) -> None:
    """#53: one success is not enough when most lookups failed - a partial api.lidarr.audio outage
    during a MusicBrainz outage would otherwise cache every term it touched for a week."""
    from likearr.core.health import LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS

    attempts = LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS
    with _outage_shaped(tmp_path, sink, attempts=attempts, failures=attempts - 1) as ctx:
        plan(ctx, now=NOW, scheduled=False)

    assert ctx.state.lidarr_negative_cache() == {}


def test_a_negative_cache_entry_is_still_written_below_the_outage_line(tmp_path: Path, sink: CapturingSink) -> None:
    from likearr.core.health import LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS

    attempts = LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS
    with _outage_shaped(tmp_path, sink, attempts=attempts, failures=attempts // 2) as ctx:
        plan(ctx, now=NOW, scheduled=False)

    assert ctx.state.lidarr_negative_cache() == {"album-search:Leopold Stokowski|Rhapsody": NOW}


# ---------------------------------------------- issue #14: the relationship join, wired end to end


_TRY = rg(
    "rg-try",
    "Try!",
    artist_mbid="mb-trio",
    artist_name="John Mayer Trio",
    secondary=[SecondaryType.LIVE],
    released="2005-11-22",
)


def _try_run(tmp_path: Path, sink: CapturingSink, *, wired: bool):
    """Gravity liked on "TRY! - Live In Concert", which MusicBrainz holds under John Mayer Trio."""
    from tests.unit.fakes import relation

    lookup = FakeLookup(
        relations={"mb-trio": [relation("mb-mayer", "John Mayer")]}, tracklists={"rg-try": ["Gravity"]}
    ).add(_TRY)
    liked = track_intent(
        "Gravity", spotify_album("TRY! - Live In Concert", artists=("John Mayer",)), artists=("John Mayer",)
    )
    source = FakeSource(snapshot(tracks=[liked]))
    relations = lookup if wired else None
    with make_context(tmp_path, source=source, lookup=lookup, sink=sink, artist_relations=relations) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json")
        cached = ctx.state.cached_resolution(liked.reason.key, RESOLVER_VERSION)
    return read_diff(tmp_path / "diff.json"), cached


def test_the_run_takes_try_through_the_contexts_relationship_lookup(tmp_path: Path, sink: CapturingSink) -> None:
    """A new artist on the Full profile, *Try!* monitored, and - nothing having failed - cached."""
    diff, cached = _try_run(tmp_path, sink, wired=True)

    assert [m.key.rg_mbid for m in diff.monitor] == ["rg-try"]
    assert [(a.artist_mbid, a.name, a.profile) for a in diff.add_artists] == [
        ("mb-trio", "John Mayer Trio", Profile.FULL)
    ]
    assert cached is not None and cached.release_group is not None
    assert cached.release_group.mbid == "rg-try"
    assert cached.resolver_version == RESOLVER_VERSION == 11


def test_a_context_without_the_relationship_lookup_runs_without_the_rule(tmp_path: Path, sink: CapturingSink) -> None:
    diff, cached = _try_run(tmp_path, sink, wired=False)
    assert diff.monitor == []
    assert diff.add_artists == []
    assert cached is None


def test_build_context_wires_the_composite_lookup_as_the_relationship_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production gets the rule only through this wiring, so it is pinned here."""
    from likearr.shell import context as context_module

    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "fake-api-key")
    monkeypatch.setattr(context_module, "setup_logging", lambda _verbose: None)
    config = tmp_path / "config.toml"
    config.write_text(
        '[lidarr]\nurl = "http://lidarr.test:8686"\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[musicbrainz]\ncontact = "likearr@example.test"\n'
        '[state]\ndb = "state.sqlite"\n'
    )

    with context_module.build_context(config, need_spotify=False) as ctx:
        assert ctx.composite is not None
        assert ctx.artist_relations is ctx.composite


def test_an_apply_that_fails_before_changing_anything_records_nothing_changed(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#54: a source failure during an apply's own plan changed nothing, and the record says so."""
    source, lookup, lidarr = followed_world()
    source.error = SourceError("spotify: 503")
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_ERROR
    assert sink.last.changes_made == 0 and lidarr.writes() == []


def test_a_clean_apply_records_what_it_changed_and_a_dry_run_records_nothing(
    tmp_path: Path, sink: CapturingSink
) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        dry = sink.last
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        applied = sink.last

    assert (dry.changes_made, dry.changes_planned) == (None, None)
    assert applied.changes_made == applied.changes_planned and applied.changes_made


def _ratchet_world(refresh: Exception) -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """The ratchet test's world, with a RefreshArtist that fails after the profile was set."""
    saved = spotify_album("Live At Somewhere", spotify_id="sp-live", upc="222")
    lookup = FakeLookup().add(LIVE)
    lookup.barcodes["222"] = "rg-3"
    source = FakeSource(snapshot(albums=[album_intent(saved)]))

    class RefreshFails(FakeLidarr):
        def refresh_artist(self, artist: Any, *, timeout_s: float = 300) -> None:
            raise refresh

    lidarr = RefreshFails(catalogue={"artist-1": [LIVE]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist", metadata_profile_id=LEAN_ID),
        lidarr_album(LIVE, id=103, monitored=False),
    )
    return source, lookup, lidarr


def test_a_profile_set_before_a_failed_refresh_is_never_called_nothing_changed(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#67 review: the ratchet sets the Full profile, then the refresh POST answers 502. No counted
    change landed, but Lidarr was written to, so the record must not say "changed nothing"."""
    source, lookup, lidarr = _ratchet_world(LidarrError("lidarr POST /command: 502"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert code == EXIT_ERROR
    assert lidarr.artists["artist-1"].metadata_profile_id == FULL_ID  # the write that happened
    record = sink.last
    assert (record.changes_made, record.lidarr_changed) == (0, True)
    assert record.message.startswith("the apply stopped part-way: Lidarr settings may have changed, but none of the")
    assert "changed nothing" not in record.message and record.message.endswith("lidarr POST /command: 502")


def test_an_unexpected_error_mid_apply_keeps_its_type_and_logs_the_traceback(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    source, lookup, lidarr = _ratchet_world(KeyError("rg-3"))
    with (
        caplog.at_level(logging.DEBUG, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert sink.last.message.endswith("KeyError: 'rg-3'")
    assert any("Traceback" in r.getMessage() and "KeyError" in r.getMessage() for r in caplog.records)


def test_a_clean_apply_that_makes_fewer_changes_than_planned_is_not_partial(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """changes_planned is an upper bound: a release Lidarr's catalogue lacks is asked for, never made."""
    source, lookup, _ = followed_world()
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM]})  # the EP never appears
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    record = sink.last
    assert code == EXIT_OK and record.status is RunStatus.OK
    assert record.changes_made is not None and record.changes_planned is not None
    assert record.changes_made < record.changes_planned


class _ProfilesUnreadable(FakeLidarr):
    """Lidarr drops after the tag: reading the metadata profiles fails with connection refused."""

    def ensure_metadata_profile(self, profile: Any, name: str) -> int:
        raise LidarrError("lidarr GET /metadataprofile: connection refused")


def test_a_phase_a_failure_with_the_tag_already_present_changed_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    """#67 re-review: the tag exists, so ensuring it only read; then Lidarr drops. Nothing was
    written, and the record says so rather than "settings may have changed"."""
    source, lookup, _ = followed_world()
    lidarr = _ProfilesUnreadable()
    tags_before = dict(lidarr.tags)
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    record = sink.last
    assert code == EXIT_ERROR and lidarr.tags == tags_before
    assert (record.changes_made, record.lidarr_changed) == (0, False)
    assert record.message.startswith("the apply failed before changing anything: ")


def test_a_missing_quality_profile_stops_the_apply_before_anything_is_created(
    tmp_path: Path, sink: CapturingSink
) -> None:
    from dataclasses import replace as dc_replace

    source, lookup, lidarr = followed_world()
    del lidarr.tags["likearr"]  # would be created, were the quality profile checked last
    config = make_config(tmp_path)
    object.__setattr__(config, "lidarr", dc_replace(config.lidarr, quality_profile="No Such Profile"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert "likearr" not in lidarr.tags
    assert (sink.last.changes_made, sink.last.lidarr_changed) == (0, False)


def test_a_failure_after_the_tag_was_created_reads_as_settings_changed(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, _ = followed_world()
    lidarr = _ProfilesUnreadable()
    del lidarr.tags["likearr"]  # the apply creates it, then Lidarr drops
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    record = sink.last
    assert code == EXIT_ERROR and "likearr" in lidarr.tags
    assert (record.changes_made, record.lidarr_changed) == (0, True)
    assert "Lidarr settings may have changed" in record.message


# --------------------------------------------------------------------------- "Monitor New Albums" (#172)


def _hand_artist_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """#172's H2: an artist added by hand in Lidarr ("Monitor New Albums: All", no likearr tag), and
    one album of theirs the user saved on Spotify, already monitored in Lidarr."""
    saved = spotify_album("First Album", spotify_id="sp-alb", upc="111")
    lookup = FakeLookup().add(ALBUM, EP)
    lookup.barcodes["111"] = "rg-1"
    source = FakeSource(snapshot(albums=[album_intent(saved)]))
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, EP]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist", monitor_new_items="all"),
        lidarr_album(ALBUM, id=101, monitored=True),
        lidarr_album(EP, id=102, monitored=True),
    )
    return source, lookup, lidarr


def test_a_hand_added_artist_keeps_monitor_new_albums_through_plan_and_apply(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """likearr owns nothing under this artist, so it writes nothing there, and the plan never shows
    a write for it."""
    source, lookup, lidarr = _hand_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "plan.json", do_apply=False)
        planned = capsys.readouterr().out
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert "set_artists_new_items_none" not in lidarr.names()
    assert lidarr.artists["artist-1"].monitor_new_items == "all"
    assert owned == {}
    assert read_diff(tmp_path / "plan.json").set_new_items_none == []
    new_albums = [line.split() for line in planned.splitlines() if "Monitor New Albums" in line]
    assert all(words[0] == "0" for words in new_albums), new_albums


def _claiming_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """`_hand_artist_world`, with the saved album not monitored yet: likearr claims it, so the artist
    comes to hold a release likearr owns."""
    source, lookup, lidarr = _hand_artist_world()
    lidarr.albums["artist-1"]["rg-1"] = replace(lidarr.albums["artist-1"]["rg-1"], monitored=False)
    return source, lookup, lidarr


def test_the_run_that_claims_a_hand_added_artists_album_sets_none_so_the_next_plan_is_empty(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """The accepted caveat of #172: a saved album that was not already monitored is claimed, so the
    artist now holds a release likearr owns. The write lands with the claim, not a run later."""
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()
        second = plan(ctx, now=NOW, scheduled=False)

    assert code == EXIT_OK
    assert [k.rg_mbid for k in owned] == ["rg-1"]
    assert lidarr.artists["artist-1"].monitor_new_items == "none"
    assert second.diff.is_empty


COMP = rg("rg-c", "Greatest Hits", secondary=[SecondaryType.COMPILATION])
SOUNDTRACK = rg("rg-s", "Film Score", secondary=[SecondaryType.SOUNDTRACK])


def _widening_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """A hand-added artist on Lean with "Monitor New Albums: All" and one studio album monitored by
    hand, and a liked song whose only home is a compilation, which needs Full. Lean hides the
    compilation; the refresh after widening shows it and two more non-studio release groups."""
    liked = track_intent("Deep Cut", spotify_album("Greatest Hits", spotify_id="sp-hits", upc="444"))
    lookup = FakeLookup().add(ALBUM, COMP)
    lookup.barcodes["444"] = "rg-c"
    source = FakeSource(snapshot(tracks=[liked]))
    lidarr = FakeLidarr(catalogue={"artist-1": [ALBUM, COMP, LIVE, SOUNDTRACK]})
    lidarr.seed(
        lidarr_artist("artist-1", id=1, name="Test Artist", monitor_new_items="all", metadata_profile_id=LEAN_ID),
        lidarr_album(ALBUM, id=101, monitored=True),
    )
    return source, lookup, lidarr


def test_widening_a_hand_added_artist_monitors_none_of_the_release_types_it_shows(
    tmp_path: Path, sink: CapturingSink
) -> None:
    """#172: widening to Full must not auto-monitor every album it shows. "Monitor New
    Albums" goes to None before the widening refresh, so what was monitored stays monitored and
    nothing it reveals is; the next run claims the one release the widening was for."""
    source, lookup, lidarr = _widening_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        names = lidarr.names()
        revealed = {rg_mbid: lidarr.albums["artist-1"][rg_mbid].monitored for rg_mbid in ("rg-c", "rg-3", "rg-s")}
        second = plan(ctx, now=NOW, scheduled=False)
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        owned = ctx.state.owned_releases()

    assert code == EXIT_OK
    assert names.index("set_artists_new_items_none") < names.index("set_artist_profile")
    assert lidarr.artists["artist-1"].monitor_new_items == "none"
    assert lidarr.artists["artist-1"].metadata_profile_id == FULL_ID
    assert revealed == {"rg-c": False, "rg-3": False, "rg-s": False}
    assert [m.key.rg_mbid for m in second.diff.monitor] == ["rg-c"]
    assert _changes_besides_monitor(second.diff) == {}
    assert [k.rg_mbid for k in owned] == ["rg-c"]
    assert lidarr.album("artist-1", "rg-1").monitored is True  # type: ignore[union-attr]
    assert lidarr.album("artist-1", "rg-3").monitored is False  # type: ignore[union-attr]
    assert lidarr.album("artist-1", "rg-s").monitored is False  # type: ignore[union-attr]


def _changes_besides_monitor(diff: Any) -> dict[str, int]:
    """Every change count in a diff's summary except `monitor`, leaving out the zeros."""
    changes = ("add_artists", "unmonitor", "ratchets", "set_new_items_none", "monitor_artists", "refresh_artists")
    return {k: v for k, v in diff_summary(diff).items() if k in changes and v}


NEW_ALBUMS_LINE = '       1 artists to set "Monitor New Albums" to None (their new albums are not auto-monitored)'


def test_the_plan_counts_and_names_the_artists_whose_monitor_new_albums_goes_to_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    lines = capsys.readouterr().out.splitlines()
    at = lines.index(NEW_ALBUMS_LINE)
    assert lines[at - 1].endswith("profile ratchets to Full")
    assert lines[at + 1] == "           Test Artist"
    assert lines[at + 2].endswith("unmonitored artists to re-monitor")


def test_the_plan_names_twenty_of_those_artists_at_most_and_counts_the_rest(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        result = plan(ctx, now=NOW, scheduled=False)
    known = [f"artist-x{i:02d}" for i in range(22)]
    for i, mbid in enumerate(known):
        result.view.artists[mbid] = lidarr_artist(mbid, id=100 + i, name=f"Artist {i:02d}")
    result.diff.set_new_items_none[:] = ["a-no-name", *known]  # not in the view: named by its MBID

    print_plan(result, tmp_path / "diff.json")

    lines = capsys.readouterr().out.splitlines()
    at = lines.index('      23 artists to set "Monitor New Albums" to None (their new albums are not auto-monitored)')
    assert lines[at + 1 : at + 22] == [
        "           a-no-name",
        *(f"           Artist {i:02d}" for i in range(19)),
        "           and 3 more (see the diff file)",
    ]
    assert lines[at + 22].endswith("unmonitored artists to re-monitor")


def test_the_apply_result_counts_the_artist_ids_sent(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        _code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)

    assert [payload for name, payload in lidarr.calls if name == "set_artists_new_items_none"] == [[1]]
    assert applied.new_items_none == 1


def test_the_apply_summary_counts_the_artists_set_to_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    lines = capsys.readouterr().out.splitlines()
    at = lines.index('       1 artists set "Monitor New Albums" to None')
    assert lines[at - 1].endswith("artists ratcheted to Full")


def test_the_health_record_counts_the_write_on_a_dry_run_and_on_an_apply(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _claiming_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        dry = sink.last
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)
        applied = sink.last

    assert dry.counts["new_items_none"] == 1, "what the diff proposes"
    assert applied.counts["new_items_none"] == 1, "what the apply did"
    assert (applied.changes_made, applied.changes_planned) == (2, 2), "the monitor and this write"


def test_an_apply_whose_only_change_is_monitor_new_albums_says_it_changed_lidarr(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """An artist holding a release likearr owns, set back to "All" by hand: the one write there is."""
    source, lookup, lidarr = _hand_artist_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored(
            [_owned_release(ReleaseKey("artist-1", "rg-1"), Reason(ReasonKind.SAVED, "sp-alb"), album_id=101)]
        )
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    record = sink.last
    assert code == EXIT_OK
    assert lidarr.artists["artist-1"].monitor_new_items == "none"
    assert record.counts["new_items_none"] == 1
    assert (record.changes_made, record.changes_planned, record.lidarr_changed) == (1, 1, True)
    assert '       1 artists set "Monitor New Albums" to None' in capsys.readouterr().out.splitlines()


def test_an_apply_that_stops_after_the_write_counts_it_as_a_change_made(tmp_path: Path, sink: CapturingSink) -> None:
    """#54's part-way record: phase (c) set "Monitor New Albums" to None, then the widening refresh in
    phase (d) failed. One planned change reached Lidarr, and the record says so."""
    source, lookup, lidarr = _ratchet_world(LidarrError("lidarr POST /command: 502"))
    lidarr.artists["artist-1"] = replace(lidarr.artists["artist-1"], monitor_new_items="all")
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    record = sink.last
    assert code == EXIT_ERROR
    assert lidarr.artists["artist-1"].monitor_new_items == "none"
    assert record.counts["new_items_none"] == 1
    assert (record.changes_made, record.changes_planned) == (1, 3), "a monitor, a ratchet and this write"
    assert record.message.startswith("the apply stopped part-way: 1 of 3 changes made: ")


WIDEN_WARNING = (
    '  WARNING: widening Test Artist to Full shows more release types. "Monitor New Albums" is set to '
    "None first, so none of them is monitored automatically; albums already monitored stay monitored."
)


def test_the_plan_warns_about_widening_an_artist_whose_monitor_new_albums_was_not_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = _widening_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    lines = capsys.readouterr().out.splitlines()
    assert lines.count(WIDEN_WARNING) == 1
    last_count = max(i for i, line in enumerate(lines) if "projected wanted" in line)
    assert lines.index(WIDEN_WARNING) > last_count, "after the count lines"


def test_the_apply_summary_and_the_log_warn_about_the_widening_too(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    source, lookup, lidarr = _widening_world()
    with (
        caplog.at_level(logging.WARNING, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    lines = capsys.readouterr().out.splitlines()
    assert lines.count(WIDEN_WARNING) == 1
    assert lines.index(WIDEN_WARNING) > lines.index("likearr applied:")
    logged = [r.getMessage() for r in caplog.records if r.name == "likearr.shell.run" and r.levelno == logging.WARNING]
    assert logged.count(WIDEN_WARNING.removeprefix("  WARNING: ")) == 1


def test_no_widening_warning_for_an_artist_already_on_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing to warn about: the widening refresh finds "Monitor New Albums" already None."""
    source, lookup, lidarr = _widening_world()
    lidarr.artists["artist-1"] = replace(lidarr.artists["artist-1"], monitor_new_items="none")
    with (
        caplog.at_level(logging.WARNING, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        run_command(ctx, now=NOW, out=tmp_path / "plan.json", do_apply=False)
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True, scheduled=True)

    assert "set_artist_profile" in lidarr.names(), "the ratchet itself still happens"
    assert "WARNING: widening" not in capsys.readouterr().out
    assert not any(r.getMessage().startswith("widening") for r in caplog.records)


# ---------------------------------------------------------------------------- issue #119: progress


class _SteppingClock:
    """A fake monotonic clock that advances by a fixed step on every read. Never a real sleep:
    a test that wants the 60 s progress throttle to pass on every call uses a large step; one that
    wants it to never pass in the run's lifetime keeps the clock frozen (step=0)."""

    def __init__(self, *, step: float, start: float = 0.0) -> None:
        self._value = start
        self._step = step

    def __call__(self) -> float:
        self._value += self._step
        return self._value


def _many_saved_albums(n: int) -> tuple[FakeSource, FakeLookup]:
    """`n` saved albums, each its own artist and release group, so each one costs the fake lookup
    exactly one call - enough of them pushes a cold plan's live-call count past the ETA's ~50-call
    threshold (the "Want" in issue #119 shows an ETA only once there have been that many)."""
    groups = [rg(f"rg-a{i}", f"Album {i}", artist_mbid=f"artist-a{i}", artist_name=f"Artist {i}") for i in range(n)]
    lookup = FakeLookup().add(*groups)
    albums = [
        album_intent(spotify_album(f"Album {i}", spotify_id=f"sp-album-{i}", artists=(f"Artist {i}",)))
        for i in range(n)
    ]
    source = FakeSource(snapshot(albums=albums))
    return source, lookup


def test_a_cold_plan_logs_progress_with_lookup_counts_and_an_eta_past_fifty_live_calls(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    from likearr.adapters.lookup import CompositeLookup

    source, lookup = _many_saved_albums(55)
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx,
    ):
        composite = CompositeLookup(lookup, ctx.lidarr)
        ctx.lookup, ctx.composite = composite, composite
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=61.0))

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress: resolving")]
    assert lines, "expected at least one progress line"
    # the last intent always logs a final line, with no ETA, even though 55 live calls is well past
    # the threshold that would otherwise show one (issue #267's "Want" #1)
    assert lines[-1] == "progress: resolving 55/55 songs and artists, 55 MusicBrainz lookups (55 live)"

    # nothing is shown before the run has actually made ~50 live calls; every line at or past that
    # point carries an ETA, except the always-ETA-free final line checked above
    crossed = False
    for line in lines[:-1]:
        match = re.search(r"\((\d+) live\)", line)
        assert match is not None, line
        live = int(match[1])
        if live >= 50:
            crossed = True
            assert "left" in line, line
        else:
            assert "left" not in line, line
    assert crossed, "expected the fake run to actually reach the ~50-live-call threshold"


def test_a_warm_plan_all_cache_hits_logs_progress_without_an_eta(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """The same world, replanned against a state db that already holds every resolution: the
    resolver never calls the lookup, so a fresh lookup's `live_calls` stays at 0 and the line has
    no ETA - "a warm run is almost all cache hits and should show no ETA rather than a misleading
    one" (issue #119)."""
    from likearr.adapters.lookup import CompositeLookup

    source, lookup = _many_saved_albums(5)
    config = make_config(tmp_path)
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink, config=config) as ctx,
    ):
        composite = CompositeLookup(lookup, ctx.lidarr)
        ctx.lookup, ctx.composite = composite, composite
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=61.0))  # warms the cache

    caplog.clear()
    warm_lookup = FakeLookup().add(*lookup.release_groups.values())
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(
            tmp_path, source=source, lookup=warm_lookup, lidarr=FakeLidarr(), sink=CapturingSink(), config=config
        ) as ctx,
    ):
        composite = CompositeLookup(warm_lookup, ctx.lidarr)
        ctx.lookup, ctx.composite = composite, composite
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=61.0))

    assert warm_lookup.live_calls == 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress: resolving")]
    assert lines, "expected at least one progress line"
    assert "MusicBrainz lookups (0 live)" in lines[-1]
    assert not any("left" in line for line in lines)


def test_plan_logs_progress_no_more_than_once_per_60s_of_fake_time(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    source, lookup = _many_saved_albums(5)
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx,
    ):
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=0.0))

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress: resolving")]
    # a clock that never advances past the 60s throttle still logs exactly twice: the first call
    # (nothing to throttle against yet) and the always-unthrottled final line (issue #267's "Want" #1)
    assert len(lines) == 2, lines
    assert lines[0].startswith("progress: resolving 1/5")
    assert lines[-1] == "progress: resolving 5/5 songs and artists, 0 MusicBrainz lookups (0 live)"


def test_a_plan_logs_a_post_resolve_marker_once_resolving_ends(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #267's "Want" #2: nothing after resolving used to log progress at all, so the job page
    kept showing the last resolve line (ETA included) for as long as reading Lidarr and building
    the diff took. `plan` now logs a marker the moment resolving is over, before either of those."""
    source, lookup = _many_saved_albums(5)
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx,
    ):
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=61.0))

    messages = [r.getMessage() for r in caplog.records if r.name == "likearr.shell.run"]
    assert "progress: reading Lidarr and building the plan" in messages
    # logged after the last resolve line and before the diff is announced
    marker_idx = messages.index("progress: reading Lidarr and building the plan")
    last_resolve_idx = max(i for i, m in enumerate(messages) if m.startswith("progress: resolving"))
    assert last_resolve_idx < marker_idx


def test_plan_shows_under_a_few_minutes_instead_of_a_precise_eta_under_two_minutes(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #267's "Want" #3: the tail of a resolve run is the noisiest part of the estimate (a
    cache-warm stretch near the end skews the live-calls-per-intent rate), so once the estimated
    remaining time drops under two minutes the line reads "under a few minutes left" instead of
    naming a specific, falsely precise duration."""
    from likearr.adapters.lookup import CompositeLookup

    source, lookup = _many_saved_albums(160)
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=FakeLidarr(), sink=sink) as ctx,
    ):
        composite = CompositeLookup(lookup, ctx.lidarr)
        ctx.lookup, ctx.composite = composite, composite
        plan(ctx, now=NOW, scheduled=False, monotonic=_SteppingClock(step=61.0))

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress: resolving")]
    # at done=100/160 with min_interval_s=1.0 and one live call per intent, the naive ETA is the
    # 60 remaining seconds - well under the two-minute floor
    line = next(line for line in lines if line.startswith("progress: resolving 100/160"))
    assert line == (
        "progress: resolving 100/160 songs and artists, 100 MusicBrainz lookups (100 live), under a few minutes left"
    )


def _three_followed_artists() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """Three followed artists, each with one album already in their MusicBrainz catalogue, so an
    apply adds all three (issue #119's add-loop progress test)."""
    groups = [rg(f"rg-b{i}", f"Album {i}", artist_mbid=f"artist-b{i}", artist_name=f"Band {i}") for i in range(3)]
    lookup = FakeLookup().add(*groups)
    for i, group in enumerate(groups):
        lookup.catalogues[f"artist-b{i}"] = [group.mbid]
    source = FakeSource(snapshot(artists=[artist_intent(f"Band {i}", spotify_id=f"sp-b{i}") for i in range(3)]))
    lidarr = FakeLidarr(catalogue={f"artist-b{i}": [g] for i, g in enumerate(groups)})
    return source, lookup, lidarr


def test_an_apply_that_adds_three_artists_logs_progress_per_artist_with_its_refresh_time(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    source, lookup, lidarr = _three_followed_artists()
    with (
        caplog.at_level(logging.INFO, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        _code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True, monotonic=_SteppingClock(step=112.0))

    assert applied.added == 3
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("progress: adding artists")]
    assert lines == [
        "progress: adding artists 1/3 (Band 0): last refresh took 1m52s",
        "progress: adding artists 2/3 (Band 1): last refresh took 1m52s",
        "progress: adding artists 3/3 (Band 2): last refresh took 1m52s",
    ]


# --------------------------------------------------------------------------- lost state (#175)

LOST_STATE_RECORD_MESSAGE = (
    "1 artist(s) with likearr's Lidarr tag have no record in the state database: "
    "if it was lost or replaced, restore it from backup"
)


def _tagged_world() -> tuple[FakeSource, FakeLookup, FakeLidarr]:
    """`followed_world`, plus an artist in Lidarr carrying the likearr tag: what an earlier state
    database added, before it was lost or replaced."""
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-9", id=9, name="Wet Leg", tags=[TAG_ID]))
    return source, lookup, lidarr


def _view(artists: dict[str, Any], tags: dict[str, int]) -> LidarrView:
    return LidarrView(artists=artists, albums={}, metadata_profiles={}, quality_profiles={}, tags=tags)


def test_tagged_without_state_is_the_tagged_artists_with_no_owned_artists_row() -> None:
    view = _view(
        {
            "a-tagged": lidarr_artist("a-tagged", id=1, tags=[TAG_ID]),
            "a-owned": lidarr_artist("a-owned", id=2, tags=[TAG_ID, ALBUMS_ONLY_TAG_ID]),
            "a-plain": lidarr_artist("a-plain", id=3, tags=[ALBUMS_ONLY_TAG_ID]),
        },
        {"likearr": TAG_ID, "albums-only": ALBUMS_ONLY_TAG_ID},
    )
    owned_artists = {"a-owned": OwnedArtist("a-owned", 2, added_by_us=True, profile=Profile.LEAN)}

    assert tagged_without_state(view, "likearr", owned_artists) == {"a-tagged"}
    assert tagged_without_state(view, "likearr", {}) == {"a-tagged", "a-owned"}


def test_tagged_without_state_is_empty_when_the_tag_does_not_exist() -> None:
    view = _view({"a-tagged": lidarr_artist("a-tagged", id=1, tags=[TAG_ID])}, {})

    assert tagged_without_state(view, "likearr", {}) == set()


def test_plan_warns_about_tagged_artists_the_state_database_has_no_record_of(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    """A lost or replaced state database: Lidarr still holds likearr's tag on what it added."""
    source, lookup, lidarr = _tagged_world()
    with (
        caplog.at_level(logging.WARNING, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        result = plan(ctx, now=NOW, scheduled=False)

    assert result.tagged_without_state == ("artist-9",)
    logged = [r.getMessage() for r in caplog.records if r.name == "likearr.shell.run" and r.levelno == logging.WARNING]
    assert logged == [
        "1 artist(s) in Lidarr carry the 'likearr' tag but the state database has no record of them "
        "(Wet Leg (artist-9)): if you lost or replaced the database, restore it from backup; until you do, "
        "nothing likearr monitored before is ever unmonitored"
    ]
    assert lidarr.writes() == [], "report only: nothing is claimed, tagged or changed"


def test_the_run_record_carries_the_lost_state_count_and_says_so(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _tagged_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)
        stored = ctx.state.last_run()

    assert sink.last.tagged_without_state == 1
    assert sink.last.message == LOST_STATE_RECORD_MESSAGE
    assert stored is not None and stored.tagged_without_state == 1
    assert code == EXIT_OK and sink.last.status is RunStatus.OK, "report only: the verdict does not move"
    assert sink.last.new_conditions == []


def test_an_apply_record_carries_the_lost_state_count_and_claims_nothing(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = _tagged_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=True)
        owned_artists = ctx.state.owned_artists()
        owned_releases = ctx.state.owned_releases()

    assert code == EXIT_OK and sink.last.status is RunStatus.OK
    assert sink.last.tagged_without_state == 1
    assert LOST_STATE_RECORD_MESSAGE in sink.last.message
    assert "artist-9" not in owned_artists, "never adopted"
    assert all(key.artist_mbid != "artist-9" for key in owned_releases), "never claimed"


def test_no_lost_state_warning_when_every_tagged_artist_has_a_row(
    tmp_path: Path, sink: CapturingSink, caplog: pytest.LogCaptureFixture
) -> None:
    source, lookup, lidarr = _tagged_world()
    with (
        caplog.at_level(logging.WARNING, logger="likearr"),
        make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx,
    ):
        ctx.state.record_artist(OwnedArtist("artist-9", 9, added_by_us=True, profile=Profile.LEAN))
        result = plan(ctx, now=NOW, scheduled=False)
        run_command(ctx, now=NOW, out=tmp_path / "diff.json", do_apply=False)

    assert result.tagged_without_state == ()
    assert not any("no record of them" in r.getMessage() for r in caplog.records)
    assert sink.last.tagged_without_state == 0
    assert sink.last.message == ""
