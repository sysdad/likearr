"""The apply loop against a real, empty, disposable Lidarr.

Skipped unless ``LIKEARR_TEST_LIDARR_URL``, ``LIKEARR_TEST_LIDARR_API_KEY`` and
``LIKEARR_TEST_LIDARR_ROOT`` are all set::

    LIKEARR_TEST_LIDARR_URL=http://127.0.0.1:18687 \\
    LIKEARR_TEST_LIDARR_API_KEY="$(tr -d '\\n' < ~/.likearr-test-lidarr-key)" \\
    LIKEARR_TEST_LIDARR_ROOT=/config \\
      uv run pytest tests/integration -m integration -q

The instance must be empty (no artists) when the session starts, and must be disposable: these
tests add artists and delete them again, and the root folder is never one the instance already
has a real library in - it comes only from ``LIKEARR_TEST_LIDARR_ROOT``. A populated instance
fails the session immediately with a clear message instead of running.

What is real here: `LidarrClient`, `SqliteState`, and the whole of `shell.apply.apply`. What is
faked: Spotify (a `FakeSource` with a canned snapshot) and MusicBrainz (a `FakeLookup` built
from the recorded corpus). The MBIDs are real ones, so Lidarr's own metadata server can find the
artist - that is the part a fake cannot stand in for, and the part that has historically broken.

Teardown deletes only the artist ids these tests themselves added (tracked as each add happens),
never by re-deriving them from the fixture's MBID, and never any artist already on the instance.
Deletion never removes files (``delete_files=False``). It prints nothing secret: the API key is
read from the environment and never logged, echoed or put in an assertion message.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
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
from likearr.models import EXIT_OK, LidarrArtist, PrimaryType, Profile, ReleaseKey, SecondaryType
from likearr.ports import LidarrArtistExists, LidarrError
from likearr.shell.context import Context
from likearr.shell.run import ApplyStopped, apply, plan
from tests.shell.conftest import CapturingSink, FakeSource
from tests.unit.fakes import NOW, FakeLookup, artist_intent, load_corpus, snapshot

# The suite-wide pytest timeout is 60s (pyproject.toml), but the fixture above allows Lidarr's
# own metadata refresh up to `refresh_timeout_s=300.0`. A cold container hitting a slow
# api.lidarr.audio can outrun the 60s default, so this module gets a longer per-test bound
# instead - still bounded, just not the suite default.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(360)]

URL = os.environ.get("LIKEARR_TEST_LIDARR_URL", "")
API_KEY = os.environ.get("LIKEARR_TEST_LIDARR_API_KEY", "")
ROOT_FOLDER = os.environ.get("LIKEARR_TEST_LIDARR_ROOT", "")

pytest.importorskip("httpx")

if not URL or not API_KEY:  # pragma: no cover - the skip is the point
    pytest.skip(
        "set LIKEARR_TEST_LIDARR_URL and LIKEARR_TEST_LIDARR_API_KEY to run the live Lidarr tests",
        allow_module_level=True,
    )

if not ROOT_FOLDER:  # pragma: no cover - the skip is the point
    pytest.skip(
        "set LIKEARR_TEST_LIDARR_ROOT to a writable path in the disposable Lidarr container to "
        "run the live Lidarr tests; it is never inferred from the instance's own root folders",
        allow_module_level=True,
    )

TEST_ARTIST_NAME = "Radiohead"


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
    """The root folder these tests use: always ``LIKEARR_TEST_LIDARR_ROOT``, never inferred from
    whatever root folder the instance already has - that would be a real library on a non-empty
    instance, and these tests only ever run against a disposable one."""
    return ROOT_FOLDER


@pytest.fixture(scope="module", autouse=True)
def _lidarr_must_start_empty() -> None:
    """Fail the session before any test runs if the instance already has artists.

    These tests add and delete artists; running them against an instance that already has a
    library would delete something the tests never added. A disposable Lidarr - the only kind
    these tests are meant for - starts with none.
    """
    with build_client() as http:
        probe = LidarrClient(
            LidarrConfig(url=URL.rstrip("/"), root_folder=ROOT_FOLDER, quality_profile="Any"),
            http,
            api_key=API_KEY,
        )
        artists = probe.load_view(None).artists
    if artists:
        pytest.fail(
            f"LIKEARR_TEST_LIDARR_URL ({URL}) already has {len(artists)} artist(s); the live "
            "Lidarr tests only run against an empty, disposable Lidarr instance and refuse to "
            "start against one that already has a library.",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def _added_artist_ids() -> list[int]:
    """Ids of artists these tests added to Lidarr, recorded by `_track_added_artists` as each
    `add_artist` call returns. Teardown deletes exactly these ids and nothing else."""
    return []


@pytest.fixture(autouse=True)
def _track_added_artists(monkeypatch: pytest.MonkeyPatch, _added_artist_ids: list[int]) -> None:
    """Record the id of every artist `LidarrClient.add_artist` adds, on every `LidarrClient`
    instance a test builds - including `apply()`'s own, which never returns the id directly."""
    real_add_artist = LidarrClient.add_artist

    def recording_add_artist(self: LidarrClient, *args: object, **kwargs: object) -> LidarrArtist:
        artist = real_add_artist(self, *args, **kwargs)  # type: ignore[arg-type]
        _added_artist_ids.append(artist.id)
        return artist

    monkeypatch.setattr(LidarrClient, "add_artist", recording_add_artist)


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
        config_path=config.state_db.parent / "config.toml",
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
def _delete_added_artists(client: LidarrClient, _added_artist_ids: list[int]) -> Iterator[None]:
    """Delete exactly the artist ids this test added (`_track_added_artists`), and nothing that
    was already on the instance. Files are never removed (``delete_files=False``)."""
    try:
        yield
    finally:
        ids = list(dict.fromkeys(_added_artist_ids))  # de-duplicated, order kept
        _added_artist_ids.clear()
        _remove_artists(client, ids)


def _remove_artists(client: LidarrClient, artist_ids: Sequence[int], timeout_s: float = 90.0) -> None:
    """Delete each given artist id, and never while Lidarr is refreshing it.

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
    for artist_id in artist_ids:
        while time.monotonic() < deadline:
            _wait_for_refresh_queue(client, deadline)
            existing = {a.id for a in client.load_view(None).artists.values()}
            if artist_id not in existing:
                break
            client.delete_artist(artist_id, delete_files=False)
            time.sleep(1.0)
        else:
            raise AssertionError(f"lidarr still has artist {artist_id} after {timeout_s:.0f}s")


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


def test_adding_the_same_artist_twice_reports_the_existing_one(
    client: LidarrClient, artist_mbid: str, root_folder: str
) -> None:
    """Lidarr answers the second POST with a 400; the adapter raises `LidarrArtistExists` carrying
    the artist Lidarr holds, tag included, so apply can tell its own add from someone else's."""
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
    with pytest.raises(LidarrArtistExists) as raised:
        client.add_artist(
            artist_mbid,
            TEST_ARTIST_NAME,
            root_folder=root_folder,
            quality_profile_id=quality,
            metadata_profile_id=lean,
            tag_ids=[tag],
        )
    assert raised.value.artist.id == first.id
    assert tag in raised.value.artist.tags
    # Adding an artist makes Lidarr queue its own RefreshArtist. Let it finish, exactly as the
    # apply path does, so the teardown is not deleting an artist that is being refreshed.
    client.refresh_artist(first, timeout_s=300)


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
