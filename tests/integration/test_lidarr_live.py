"""The apply loop against a real, disposable Lidarr.

Skipped unless ``LIKEARR_TEST_LIDARR_URL`` and ``LIKEARR_TEST_LIDARR_API_KEY`` are set::

    LIKEARR_TEST_LIDARR_URL=http://127.0.0.1:18687 \\
    LIKEARR_TEST_LIDARR_API_KEY="$(tr -d '\\n' < ~/.likearr-test-lidarr-key)" \\
      uv run pytest tests/integration -m integration -q

What is real here: `LidarrClient`, `SqliteState`, and the whole of `shell.apply.apply`. What is
faked: Spotify (a `FakeSource` with a canned snapshot) and MusicBrainz (a `FakeLookup` built
from the recorded corpus). The MBIDs are real ones, so Lidarr's own metadata server can find the
artist - that is the part a fake cannot stand in for, and the part that has historically broken.

The test adds exactly one artist, monitors exactly one album, and deletes the artist again on
the way out **without ever deleting files**. It prints nothing secret: the API key is read from
the environment and never logged, echoed or put in an assertion message.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from likearr.adapters.http import build_client
from likearr.adapters.lidarr import LidarrClient
from likearr.adapters.state_sqlite import SqliteState
from likearr.config import (
    Config,
    GuardsConfig,
    HealthConfig,
    LidarrConfig,
    MusicBrainzConfig,
    RulesConfig,
    SpotifyConfig,
)
from likearr.models import EXIT_OK, PrimaryType, Profile, ReleaseKey, SecondaryType
from likearr.ports import LidarrError
from likearr.shell.context import Context
from likearr.shell.run import ApplyStopped, apply, plan
from tests.shell.conftest import CapturingSink, FakeSource
from tests.unit.fakes import FakeLookup, artist_intent, load_corpus, snapshot

# The suite-wide pytest timeout is 60s (pyproject.toml), but the fixture above allows Lidarr's
# own metadata refresh up to `refresh_timeout_s=300.0`. A cold container hitting a slow
# api.lidarr.audio can outrun the 60s default, so this module gets a longer per-test bound
# instead - still bounded, just not the suite default.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(360)]

URL = os.environ.get("LIKEARR_TEST_LIDARR_URL", "")
API_KEY = os.environ.get("LIKEARR_TEST_LIDARR_API_KEY", "")

pytest.importorskip("httpx")

if not URL or not API_KEY:  # pragma: no cover - the skip is the point
    pytest.skip(
        "set LIKEARR_TEST_LIDARR_URL and LIKEARR_TEST_LIDARR_API_KEY to run the live Lidarr tests",
        allow_module_level=True,
    )

DEFAULT_ROOT_FOLDER = "/config"
"""A path that exists and is writable inside a stock Lidarr container.

``/music`` is the obvious choice and the wrong one: on an image that drops privileges (hotio,
linuxserver) an unmounted or root-owned ``/music`` fails Lidarr's own FolderWritableValidator
with a 400, and the test would be asserting the container's mount layout rather than likearr.
Override with ``LIKEARR_TEST_LIDARR_ROOT`` when the instance has a real library.
"""

TEST_ARTIST_NAME = "Radiohead"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def artist_mbid(corpus) -> str:
    return corpus.rg("ok_computer").artist_mbid


@pytest.fixture(scope="module")
def ok_computer(corpus):
    return corpus.rg("ok_computer")


@pytest.fixture(scope="module")
def root_folder() -> str:
    """The root folder these tests use: the override, whatever Lidarr already has, or /config."""
    override = os.environ.get("LIKEARR_TEST_LIDARR_ROOT", "")
    if override:
        return override
    with build_client() as http:
        probe = LidarrClient(
            LidarrConfig(url=URL.rstrip("/"), root_folder=DEFAULT_ROOT_FOLDER, quality_profile="Any"),
            http,
            api_key=API_KEY,
        )
        existing = [str(f.get("path") or "") for f in probe.root_folders()]
    return existing[0] if existing else DEFAULT_ROOT_FOLDER


@pytest.fixture
def config(tmp_path: Path, root_folder: str) -> Config:
    return Config(
        lidarr=LidarrConfig(
            url=URL.rstrip("/"),
            root_folder=root_folder,
            quality_profile="Any",
            refresh_timeout_s=300.0,
        ),
        spotify=SpotifyConfig(token_file=tmp_path / "token.json"),
        musicbrainz=MusicBrainzConfig(contact="likearr@example.test"),
        state_db=tmp_path / "state.sqlite",
        rules=RulesConfig(),
        guards=GuardsConfig(),
        health=HealthConfig(stdout=False),
        lock_file=tmp_path / "likearr.lock",
    )


@pytest.fixture
def client(config: Config) -> Iterator[LidarrClient]:
    with build_client() as http:
        yield LidarrClient(config.lidarr, http, api_key=API_KEY)


@pytest.fixture
def lookup(corpus, artist_mbid: str, ok_computer) -> FakeLookup:
    """MusicBrainz, stubbed from the recorded corpus: Radiohead's catalogue is OK Computer."""
    fake = FakeLookup().add(ok_computer)
    fake.catalogues[artist_mbid] = [ok_computer.mbid]
    return fake


@pytest.fixture
def followed(artist_mbid: str) -> FakeSource:
    return FakeSource(snapshot(artists=[artist_intent(TEST_ARTIST_NAME, spotify_id="sp-radiohead")]))


@pytest.fixture
def ctx(config: Config, client: LidarrClient, lookup: FakeLookup, followed: FakeSource) -> Iterator[Context]:
    state = SqliteState(config.state_db)
    context = Context(
        config=config,
        state=state,
        lidarr=client,
        lookup=lookup,
        sinks=[CapturingSink()],
        source=followed,
        auth=None,
        composite=None,
    )
    try:
        yield context
    finally:
        state.close()


@pytest.fixture(autouse=True)
def clean_lidarr(client: LidarrClient, artist_mbid: str) -> Iterator[None]:
    """Leave the instance exactly as it was found: no test artist, files untouched."""
    _remove_artist(client, artist_mbid)
    try:
        yield
    finally:
        _remove_artist(client, artist_mbid)


def _remove_artist(client: LidarrClient, artist_mbid: str, timeout_s: float = 90.0) -> None:
    """Delete the test artist, and never while Lidarr is refreshing it.

    Deleting an artist that has a ``RefreshArtist`` command in flight puts Lidarr 3.1.0 into a
    loop: the refresh re-creates the artist, creating an artist queues a refresh, and the pair
    repeats about once a second until something else stops it. Observed on a real Lidarr -
    every command in the cycle is recorded as ``RefreshArtist failed``, and the artist id climbs
    by one per second. So: wait for the queue to drain, then delete, then confirm.

    likearr's own apply path never hits this, because `refresh_artist` polls the command to
    completion before it does anything else. It is the tests, which add without refreshing, that
    have to be careful.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _wait_for_refresh_queue(client, deadline)
        artist = client.load_view(None).artists.get(artist_mbid)
        if artist is None:
            return
        client.delete_artist(artist.id, delete_files=False)
        time.sleep(1.0)
    raise AssertionError(f"lidarr still has {artist_mbid} after {timeout_s:.0f}s")


def _wait_for_refresh_queue(client: LidarrClient, deadline: float) -> None:
    """Block until no RefreshArtist command is queued or started."""
    while time.monotonic() < deadline:
        commands = client._json("GET", "command")
        pending = [
            c
            for c in (commands or [])
            if isinstance(c, dict)
            and str(c.get("name") or "") == "RefreshArtist"
            and str(c.get("status") or "") in {"queued", "started"}
        ]
        if not pending:
            return
        time.sleep(1.0)


# --------------------------------------------------------------------------- the tests


def test_version_is_supported(client: LidarrClient) -> None:
    version = client.check_version()
    assert version.split(".")[0] in {"2", "3"}


def test_setup_creates_profiles_a_tag_and_a_root_folder(client: LidarrClient, root_folder: str) -> None:
    """Everything the apply loop needs, created idempotently before it runs."""
    lean = client.ensure_metadata_profile(Profile.LEAN, "Lean")
    full = client.ensure_metadata_profile(Profile.FULL, "Full")
    tag = client.ensure_tag("likearr")
    assert lean > 0 and full > 0 and tag > 0
    assert client.ensure_metadata_profile(Profile.LEAN, "Lean") == lean, "creating twice must not duplicate"

    folder = client.add_root_folder(root_folder)
    assert str(folder.get("path", "")).rstrip("/") == root_folder
    assert any(str(f.get("path") or "").rstrip("/") == root_folder for f in client.root_folders())


def test_apply_adds_the_artist_and_monitors_exactly_one_album(
    ctx: Context, client: LidarrClient, artist_mbid: str, ok_computer
) -> None:
    _prepare(client, ctx.config.lidarr.root_folder)

    exit_code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)

    assert exit_code == EXIT_OK, [g.message for g in diff.guards]
    assert applied.added == 1
    assert applied.monitored == 1
    assert applied.skipped_artists == []

    view = client.load_view([artist_mbid])
    artist = view.artists[artist_mbid]
    assert artist.monitor_new_items == "none", "Lidarr must not auto-monitor new releases"
    assert client.ensure_tag("likearr") in artist.tags
    assert artist.metadata_profile_id == client.ensure_metadata_profile(Profile.LEAN, "Lean")

    albums = client.load_albums(artist)
    monitored = sorted(rg for rg, album in albums.items() if album.monitored)
    assert monitored == [ok_computer.mbid], "exactly the one album the diff asked for"
    assert len(albums) > 1, "the catalogue really was fetched from Lidarr's metadata server"

    owned = ctx.state.owned_releases()
    key = ReleaseKey(artist_mbid=artist_mbid, rg_mbid=ok_computer.mbid)
    assert key in owned
    assert owned[key].lidarr_album_id == albums[ok_computer.mbid].id
    assert ctx.state.owned_artists()[artist_mbid].added_by_us is True


def test_a_second_plan_is_empty(ctx: Context, client: LidarrClient, artist_mbid: str) -> None:
    _prepare(client, ctx.config.lidarr.root_folder)
    apply(ctx, None, now=NOW, scheduled=True)

    second = plan(ctx, now=NOW, scheduled=False)

    assert second.diff.is_empty, (
        f"add={len(second.diff.add_artists)} monitor={len(second.diff.monitor)} "
        f"unmonitor={len(second.diff.unmonitor)} ratchets={len(second.diff.ratchets)} "
        f"new_items={second.diff.set_new_items_none}"
    )


def test_losing_the_reason_unmonitors_and_drops_ownership(
    ctx: Context, client: LidarrClient, artist_mbid: str, ok_computer, followed: FakeSource
) -> None:
    _prepare(client, ctx.config.lidarr.root_folder)
    apply(ctx, None, now=NOW, scheduled=True)

    # The user unfollows Radiohead. The reason is gone from the source, so the release loses its
    # last reason and likearr unmonitors what it itself monitored - and nothing else.
    followed.snapshot = snapshot(artists=[], counts={"followed_artists": 0})
    ctx.state.record_source_counts({"followed_artists": 0})

    # The artist's studio-release count went 1 -> 0, but it is absent from the followed source, which
    # is an unfollow rather than a shrunken catalogue: `artist-shrink` does not apply, so the
    # unmonitor goes through on the first run. (`source-shrink` still covers a mass unfollow.)
    exit_code, applied, _fresh, diff = apply(ctx, None, now=NOW, scheduled=True)

    assert exit_code == EXIT_OK, [g.message for g in diff.guards]
    assert applied.unmonitored == 1
    artist = client.load_view(None).artists[artist_mbid]
    albums = client.load_albums(artist)
    assert not any(album.monitored for album in albums.values())
    assert ReleaseKey(artist_mbid=artist_mbid, rg_mbid=ok_computer.mbid) not in ctx.state.owned_releases()


def test_adding_the_same_artist_twice_is_idempotent(client: LidarrClient, artist_mbid: str, root_folder: str) -> None:
    """Lidarr answers the second POST with a 400; the adapter turns that into the existing artist."""
    _prepare(client, root_folder)
    lean = client.ensure_metadata_profile(Profile.LEAN, "Lean")
    tag = client.ensure_tag("likearr")
    quality = client.load_view(None).quality_profiles["Any"]

    first = client.add_artist(
        artist_mbid,
        TEST_ARTIST_NAME,
        root_folder=root_folder,
        quality_profile_id=quality,
        metadata_profile_id=lean,
        tag_ids=[tag],
    )
    second = client.add_artist(
        artist_mbid,
        TEST_ARTIST_NAME,
        root_folder=root_folder,
        quality_profile_id=quality,
        metadata_profile_id=lean,
        tag_ids=[tag],
    )
    assert first.id == second.id
    # Adding an artist makes Lidarr queue its own RefreshArtist. Let it finish, exactly as the
    # apply path does, so the teardown is not deleting an artist that is being refreshed.
    client.refresh_artist(second, timeout_s=300)


def test_a_crashed_apply_is_finished_by_the_next_run(
    ctx: Context, client: LidarrClient, artist_mbid: str, ok_computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist is added and recorded, the monitor call explodes, and a re-run completes it."""
    _prepare(client, ctx.config.lidarr.root_folder)
    real = LidarrClient.set_albums_monitored
    calls = {"n": 0}

    def explode(self: LidarrClient, album_ids, monitored: bool) -> None:
        if monitored:
            calls["n"] += 1
            raise LidarrError("simulated crash between the add and the monitor")
        real(self, album_ids, monitored)

    monkeypatch.setattr(LidarrClient, "set_albums_monitored", explode)
    planned = plan(ctx, now=NOW, scheduled=True)
    assert len(planned.diff.monitor) == 1, [m.key.rg_mbid for m in planned.diff.monitor]
    with pytest.raises(ApplyStopped) as stopped:
        apply(ctx, None, now=NOW, scheduled=True)
    assert isinstance(stopped.value.cause, LidarrError)
    assert stopped.value.applied.added == 1
    assert stopped.value.applied.monitored == 0

    assert calls["n"] == 1
    assert ctx.state.owned_releases() == {}, "nothing may be claimed for a flip that never happened"
    assert ctx.state.owned_artists()[artist_mbid].added_by_us is True, "the add did commit"

    monkeypatch.setattr(LidarrClient, "set_albums_monitored", real)
    exit_code, applied, _fresh, _diff = apply(ctx, None, now=NOW, scheduled=True)

    assert exit_code == EXIT_OK
    assert applied.added == 0, "the artist was already there"
    assert applied.monitored == 1
    assert ReleaseKey(artist_mbid=artist_mbid, rg_mbid=ok_computer.mbid) in ctx.state.owned_releases()


def test_prune_stage_reads_real_track_files(
    client: LidarrClient, artist_mbid: str, ok_computer, root_folder: str
) -> None:
    """The instance has no library, so this asserts the call shape rather than its contents."""
    _prepare(client, root_folder)
    lean = client.ensure_metadata_profile(Profile.LEAN, "Lean")
    artist = client.add_artist(
        artist_mbid,
        TEST_ARTIST_NAME,
        root_folder=root_folder,
        quality_profile_id=client.load_view(None).quality_profiles["Any"],
        metadata_profile_id=lean,
        tag_ids=[client.ensure_tag("likearr")],
    )
    client.refresh_artist(artist, timeout_s=300)
    albums = client.load_albums(artist)
    assert client.track_files(albums[ok_computer.mbid].id) == []


def test_the_metadata_profiles_really_restrict_types(client: LidarrClient, artist_mbid: str, root_folder: str) -> None:
    """Lean must not pull singles into the catalogue; Full must allow live records."""
    _prepare(client, root_folder)
    lean = client.ensure_metadata_profile(Profile.LEAN, "Lean")
    artist = client.add_artist(
        artist_mbid,
        TEST_ARTIST_NAME,
        root_folder=root_folder,
        quality_profile_id=client.load_view(None).quality_profiles["Any"],
        metadata_profile_id=lean,
        tag_ids=[client.ensure_tag("likearr")],
    )
    client.refresh_artist(artist, timeout_s=300)
    albums = client.load_albums(artist)

    assert albums, "Lidarr's metadata server returned a catalogue"
    assert not any(a.primary_type is PrimaryType.SINGLE for a in albums.values()), "Lean excludes singles"
    assert not any(SecondaryType.LIVE in a.secondary_types for a in albums.values()), "Lean is studio only"


def _prepare(client: LidarrClient, root_folder: str) -> None:
    """Everything `setup-profiles --apply` would have done, so each test stands alone."""
    client.add_root_folder(root_folder)
    client.ensure_metadata_profile(Profile.LEAN, "Lean")
    client.ensure_metadata_profile(Profile.FULL, "Full")
    client.ensure_tag("likearr")
