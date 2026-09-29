"""Write a demo data directory for the README screenshots: no Spotify, no Lidarr, no network.

    uv run python scripts/demo_state.py /tmp/likearr-demo
    LIKEARR_UI_PASSWORD='a demo password, 16+ chars' LIKEARR_SPOTIFY_CLIENT_ID=demo \
        LIKEARR_LIDARR_URL=http://127.0.0.1:9 uv run likearr start -c /tmp/likearr-demo/config.toml --port 8770

`LIKEARR_SPOTIFY_CLIENT_ID` is only there so Settings doesn't say Spotify is not configured; it
is never sent anywhere unless someone clicks Connect Spotify.

It writes a complete data directory - `config.toml`, the state database, the last run's facts,
a Spotify token file with no secrets in it, the playlist-name cache, and two jobs in the web UI's
job store - that makes the service look like a healthy library a few days in: Status "All good."
with coverage and a run history, a check waiting on "Review changes" with artists to add, releases
to monitor and two to unmonitor, answers on Look up, and a few entries on "Not added".

`--first-review` writes a new install instead: a library monitored by hand in Lidarr, Spotify
connected, and one check waiting on "Review changes" with its "Albums you already monitor"
section (albums that match, albums that don't, and one held after a failed lookup).

**How.** Nothing here is hand-written into the database. The real shell runs (`run_command`: the
first hand check and apply, a day and a half of scheduled applies, and the pending check) against
the test suite's in-memory fakes: `FakeSource` for Spotify, `FakeLidarr` for Lidarr and
`FakeLookup` for MusicBrainz. So this script imports from `tests/` (`tests.shell.conftest` and
`tests.unit.fakes`), which is fine for a dev script and means it needs the dev extra
(`uv sync --extra dev`). The release groups are real: their MusicBrainz ids, titles, types and
dates come from the recorded public corpus in `tests/fixtures/mb/corpus.json`. The Spotify ids are
made up. Nothing comes from anyone's library.

**No network.** The suite's own network guard (`tests/_network_guard.py`) is installed before
anything runs, so a lookup that slipped past the fakes fails instead of reaching anyone.
`LIKEARR_LIDARR_URL` is `http://127.0.0.1:9`, where nothing answers. The token file carries no access or
refresh token, so a job started from the browser (Check for changes, Run and apply now, a scheduled
fire, the live Look up) fails at once without calling anything: take screenshots, don't click those.

**Times are real.** The runs are stamped relative to the moment the script runs, and the schedule
is written so its last fire was an hour before and its next is five hours after: Status reads
"All good." only while its last apply is under 13 hours old, and a check is reviewable for a
week. A server still up at the next fire, or started after it, records that fire's failed run and
Status turns amber. Regenerate the directory right before taking screenshots.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `tests` is importable from the checkout root, not from scripts/
    sys.path.insert(0, str(ROOT))

from likearr.adapters.health import StdoutSink  # noqa: E402
from likearr.config import load_config  # noqa: E402
from likearr.models import (  # noqa: E402
    AlbumIntent,
    ArtistIntent,
    PrimaryType,
    Reason,
    ReasonKind,
    ReleaseGroup,
    SourceSnapshot,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.playlist_names import names_path, write_names  # noqa: E402
from likearr.ports import MetadataError  # noqa: E402
from likearr.shell import run_report  # noqa: E402
from likearr.shell.diff_io import read_diff  # noqa: E402
from likearr.shell.output import emit_lines  # noqa: E402
from likearr.shell.run import run_command  # noqa: E402
from likearr.web.jobs import JobMeta, JobState, new_job_id  # noqa: E402
from likearr.web.plans import plan_token_of_file  # noqa: E402
from tests._network_guard import install as install_network_guard  # noqa: E402
from tests.shell.conftest import CapturingSink, FakeLidarr, FakeSource, make_context  # noqa: E402
from tests.unit.fakes import FakeLookup, lidarr_album, lidarr_artist, load_corpus  # noqa: E402

__all__ = ["DemoInfo", "build_demo", "build_first_review", "main"]

# --------------------------------------------------------------------------- the library

ARTISTS = {
    "Radiohead": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
    "Daft Punk": "056e4f3e-d505-4dad-8ec1-d04f521cbb56",
    "Bon Iver": "437a0e49-c6ae-42f6-a6c1-84f25ed366bc",
    "The Weeknd": "c8b03190-306c-4120-bb0b-6f2ebfc06ea9",
    "Adele": "cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493",
    "The Beatles": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d",
    "John Mayer": "144ef525-85e9-40c3-8335-02c32d0861f3",
    "Mark Ronson": "c3c82bdc-d9e7-4836-9746-c24ead47ca19",
}
"""The demo's artists, by their real MusicBrainz ids (from the recorded corpus)."""

RELEASES = {
    # Radiohead: followed, so their whole studio catalogue is wanted.
    "Pablo Honey": "cd76f76b-ff15-3784-a71d-4da3078a6851",
    "The Bends": "b8048f24-c026-3398-b23a-b5e50716cbc7",
    "OK Computer": "b1392450-e666-3926-a536-22c65f834433",
    "Kid A": "e75c0549-ad55-39e3-8025-c72c5d4a3c5d",
    "Amnesiac": "bca9280e-28b4-327f-8fe0-fd918579e486",
    "Hail to the Thief": "5c14fd50-a2f1-3672-9537-b0dad91bea2f",
    "In Rainbows": "6e335887-60ba-38f0-95af-fae7774336bf",
    "The King of Limbs": "899b6d09-807e-4c18-a6d4-3642e00d6a3d",
    "A Moon Shaped Pool": "bbce0087-d386-4246-a51d-dbcdfdbe8fb2",
    # Daft Punk: followed.
    "Homework": "00054665-89fa-33d5-a8f0-1728ea8c32c3",
    "Discovery": "48117b90-a16e-34ca-a514-19c702df1158",
    "Human After All": "f9e8042a-674e-3f01-80ec-7f0ab1c537df",
    "Random Access Memories": "aa997ea0-2936-40bd-884d-3af8a0e064dc",  # gitleaks:allow (a MusicBrainz id)
    # Bon Iver: followed in the pending check.
    "For Emma, Forever Ago": "187935b5-a0a4-3e6f-9684-48b67a5190a1",
    "Blood Bank": "8ff93c7e-99e3-3c02-8a21-f927dbda9530",
    "Bon Iver, Bon Iver": "2cb36662-3560-4b90-a0a5-7924ac039490",
    "22, a Million": "f0e8f425-a941-48df-b5d7-2ceeaaf77c71",
    "i,i": "e53d381a-3b37-4154-9a6e-7d343b3e182e",
    # The rest come in through a saved album, a liked song or a playlist.
    "Starboy": "ceaa5c39-91c7-4c8a-886c-85b11fa8a1f6",
    "After Hours": "78570bea-2a26-467c-a3db-c52723ceb394",
    "19": "9796da06-2d59-3176-8598-2105f31ee54a",
    "21": "e4174758-d333-4a8e-a31f-dd0edd51518e",
    "25": "5537624c-3d2f-4f5c-8099-df916082c85c",
    "Skyfall": "4307ecf9-d0f2-4b95-b7ad-2f8cba84a5e9",
    "Revolver": "72d15666-99a7-321e-b1f3-a3f8c09dff9f",
    "Abbey Road": "9162580e-5df4-32de-80cc-f45a8d8a9b1d",
    "Hey Jude": "0e986744-f2d8-4066-b6d2-51487aee38df",
    "Heavier Things": "b6d8aecb-39c7-3287-9b3c-f57fe35a610c",
    "Continuum": "21e886f7-db66-3103-beb4-da9323adddc7",
    "Version": "8d9fb420-1b0d-316e-9367-9717d364b6db",
    "Uptown Special": "13045772-371a-470a-95da-44b7e3488224",
    # A Various Artists soundtrack: likearr leaves those alone, so it is a "Not added" row.
    "Guardians of the Galaxy: Awesome Mix, Vol. 1": "950092d6-45f6-4269-87da-99a9ff2fcc52",
}
"""Well-known releases, by their real MusicBrainz release-group ids (from the recorded corpus)."""

FOLLOWED_CATALOGUES = {
    "Radiohead": [
        "Pablo Honey",
        "The Bends",
        "OK Computer",
        "Kid A",
        "Amnesiac",
        "Hail to the Thief",
        "In Rainbows",
        "The King of Limbs",
        "A Moon Shaped Pool",
    ],
    "Daft Punk": ["Homework", "Discovery", "Human After All", "Random Access Memories"],
    "Bon Iver": ["For Emma, Forever Ago", "Blood Bank", "Bon Iver, Bon Iver", "22, a Million", "i,i"],
}

ISRCS = {"Skyfall": ("GBBKS1200164", "Skyfall")}
"""song -> (its real ISRC, the release group MusicBrainz files it under). Skyfall is only ever a
single, out long enough ago that likearr stops waiting for an album and monitors the single."""

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
AWESOME_MIX = "Guardians of the Galaxy: Awesome Mix, Vol. 1"
AWESOME_MIX_BARCODE = "050087316471"
"""The real barcode of the soundtrack's representative release, as Spotify would give its UPC."""

PLAYLIST_ID = "4dEm0R0adTr1pPl4yl1stX"
PLAYLIST_NAME = "Road Trip"

WAITING = frozenset({"Human After All", "Heavier Things", "19"})
"""Monitored, but Lidarr has not downloaded them yet: the "Waiting for a download" count."""

PASSWORD_HINT = "a demo password, 16+ chars"


def _spotify_id(kind: str, name: str) -> str:
    """A made-up, stable, Spotify-shaped id: 22 base62 characters."""
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    n = int.from_bytes(hashlib.sha256(f"{kind}:{name}".encode()).digest(), "big")
    out = []
    for _ in range(22):
        n, r = divmod(n, 62)
        out.append(alphabet[r])
    return "".join(out)


@dataclass(frozen=True, slots=True)
class _Album:
    title: str
    artist: str
    kind: str = "album"
    """Spotify's album_type: album, single or compilation."""
    upc: str | None = None


_ALBUMS = {
    a.title: a
    for a in (
        _Album("OK Computer", "Radiohead"),
        _Album("In Rainbows", "Radiohead"),
        _Album("Random Access Memories", "Daft Punk"),
        _Album("Discovery", "Daft Punk"),
        _Album("22, a Million", "Bon Iver"),
        _Album("Starboy", "The Weeknd"),
        _Album("After Hours", "The Weeknd"),
        _Album("19", "Adele"),
        _Album("21", "Adele"),
        _Album("25", "Adele"),
        _Album("Skyfall", "Adele", kind="single"),
        _Album("Revolver", "The Beatles"),
        _Album("Abbey Road", "The Beatles"),
        _Album("Help!", "The Beatles"),  # not in the demo's MusicBrainz: "Couldn't be matched"
        _Album("Hey Jude", "The Beatles", kind="compilation"),
        _Album("Heavier Things", "John Mayer"),
        _Album("Continuum", "John Mayer"),
        _Album("Version", "Mark Ronson"),
        _Album("Uptown Special", "Mark Ronson"),
        _Album(AWESOME_MIX, "Various Artists", kind="compilation", upc=AWESOME_MIX_BARCODE),
    )
}


# --------------------------------------------------------------------------- what Spotify says


@dataclass(slots=True)
class _Likes:
    """One moment of the Spotify library: who is followed, which albums saved, which songs liked."""

    followed: list[str]
    saved: list[str]
    liked: list[tuple[str, str]]
    """(song, album title)."""
    playlist: list[tuple[str, str]]


def _album_ref(title: str, released: str | None) -> SpotifyAlbumRef:
    album = _ALBUMS[title]
    return SpotifyAlbumRef(
        spotify_id=_spotify_id("album", title),
        name=title,
        artist_names=(album.artist,),
        upc=album.upc,
        album_type=album.kind,
        release_date=datetime.fromisoformat(released).date() if released else None,
    )


def _snapshot(likes: _Likes, *, at: datetime, groups: dict[str, ReleaseGroup]) -> SourceSnapshot:
    def released(title: str) -> str | None:
        rg = groups.get(RELEASES.get(title, ""))
        return rg.first_release_date.isoformat() if rg is not None and rg.first_release_date else None

    def track(song: str, album: str, reason: Reason, added: datetime) -> TrackIntent:
        isrc = ISRCS.get(song, (None, ""))[0]
        return TrackIntent(
            spotify_id=reason.source_id,
            name=song,
            isrc=isrc,
            artist_names=(_ALBUMS[album].artist,),
            album=_album_ref(album, released(album)),
            added_at=added,
            reason=reason,
        )

    artists = tuple(
        ArtistIntent(
            spotify_id=_spotify_id("artist", name),
            name=name,
            reason=Reason(ReasonKind.FOLLOWED, _spotify_id("artist", name)),
        )
        for name in likes.followed
    )
    albums = tuple(
        AlbumIntent(
            album=_album_ref(title, released(title)),
            reason=Reason(ReasonKind.SAVED, _spotify_id("album", title)),
        )
        for title in likes.saved
    )
    liked = [
        track(song, album, Reason(ReasonKind.LIKED, _spotify_id("track", song)), at - timedelta(days=3 + i))
        for i, (song, album) in enumerate(likes.liked)
    ]
    listed = [
        track(
            song,
            album,
            Reason(ReasonKind.PLAYLIST, _spotify_id("track", song), playlist_id=PLAYLIST_ID),
            at - timedelta(days=10 + i),
        )
        for i, (song, album) in enumerate(likes.playlist)
    ]
    return SourceSnapshot(
        fetched_at=at,
        artists=artists,
        albums=albums,
        tracks=(*liked, *listed),
        counts={
            "followed_artists": len(artists),
            "saved_albums": len(albums),
            "liked_tracks": len(liked),
            f"playlist:{PLAYLIST_ID}": len(listed),
        },
    )


FIRST = _Likes(
    followed=["Radiohead", "Daft Punk"],
    saved=["21", "Abbey Road", "Continuum", "Version", AWESOME_MIX],
    liked=[
        ("Karma Police", "OK Computer"),
        ("Get Lucky", "Random Access Memories"),
        ("Rolling in the Deep", "21"),
        ("Hello", "25"),
        ("Here Comes the Sun", "Abbey Road"),
        ("Gravity", "Continuum"),
        ("Yesterday", "Help!"),
        ("Hey Jude", "Hey Jude"),
        ("Skyfall", "Skyfall"),
    ],
    playlist=[
        ("Uptown Funk", "Uptown Special"),
        ("One More Time", "Discovery"),
        ("Something", "Abbey Road"),
    ],
)
"""What Spotify held at the first check."""


def _later(likes: _Likes, **changes: Any) -> _Likes:
    return replace(likes, **changes)


# A new liked song a day in, and another at the last scheduled fire: small applies in the history,
# and a "What changed" under Status's last applied run.
DAY_ONE = _later(FIRST, liked=[("Daughters", "Heavier Things"), *FIRST.liked])
DAY_TWO = _later(DAY_ONE, liked=[("Chasing Pavements", "19"), *DAY_ONE.liked])
# What the pending check sees: Bon Iver followed, a Weeknd song liked and another added to the
# playlist, "Revolver" saved in place of "Version", and "Hello" unliked - so the check adds two
# artists, monitors their releases and Revolver, and unmonitors Version and 25. The counts stay
# level (one saved album out, one in; one song unliked, one liked), so no shrink guard holds the
# unmonitors back.
PENDING = _later(
    DAY_TWO,
    followed=[*DAY_TWO.followed, "Bon Iver"],
    saved=["21", "Abbey Road", "Continuum", "Revolver", AWESOME_MIX],
    liked=[("Blinding Lights", "After Hours"), *[s for s in DAY_TWO.liked if s[0] != "Hello"]],
    playlist=[*DAY_TWO.playlist, ("Starboy", "Starboy")],
)


# --------------------------------------------------------------------------- MusicBrainz and Lidarr


def _release_groups() -> dict[str, ReleaseGroup]:
    corpus = load_corpus()
    groups = {mbid: corpus.groups[mbid] for mbid in RELEASES.values()}
    for rg in groups.values():  # a guard against a typo above: every id is the artist's own
        assert ARTISTS.get(rg.artist_name, VARIOUS_ARTISTS) == rg.artist_mbid, rg
    return groups


def _lookup(groups: dict[str, ReleaseGroup]) -> FakeLookup:
    lookup = FakeLookup().add(*groups.values())
    lookup.barcodes[AWESOME_MIX_BARCODE] = RELEASES[AWESOME_MIX]
    for artist, titles in FOLLOWED_CATALOGUES.items():
        lookup.catalogues[ARTISTS[artist]] = [RELEASES[t] for t in titles]
    for isrc, title in ISRCS.values():
        lookup.isrcs[isrc] = [RELEASES[title]]
    return lookup


def _lidarr(groups: dict[str, ReleaseGroup]) -> FakeLidarr:
    """An empty Lidarr, set up the way `likearr setup-profiles` leaves it, whose metadata knows every
    demo artist's releases (what a refresh brings in once an artist is added)."""
    catalogue: dict[str, list[ReleaseGroup]] = {}
    for rg in sorted(groups.values(), key=lambda g: (g.first_release_date is None, g.first_release_date, g.title)):
        if rg.artist_mbid != VARIOUS_ARTISTS:
            catalogue.setdefault(rg.artist_mbid, []).append(rg)
    return FakeLidarr(catalogue=catalogue)


def _download(lidarr: FakeLidarr) -> None:
    """Lidarr fetched what is monitored, apart from `WAITING`: files on disk for the coverage counts."""
    waiting = {RELEASES[t] for t in WAITING}
    for by_rg in lidarr.albums.values():
        for rg_mbid, album in list(by_rg.items()):
            if album.monitored and rg_mbid not in waiting and not album.track_file_count:
                files = 1 if album.primary_type is PrimaryType.SINGLE else 9 + int(rg_mbid[:2], 16) % 8
                by_rg[rg_mbid] = replace(album, track_file_count=files, size_on_disk=files * 41_000_000)


# --------------------------------------------------------------------------- files


def _schedule(now: datetime) -> tuple[str, list[datetime]]:
    """A cron line firing every six hours whose last fire was an hour before `now`, and those fires,
    newest first: the history lines up with the schedule, and the next fire is five hours off."""
    last = (now - timedelta(hours=1)).replace(second=0, microsecond=0)
    hours = ",".join(str(h) for h in sorted((last.hour + 6 * k) % 24 for k in range(4)))
    return f"{last.minute} {hours} * * *", [last - timedelta(hours=6 * k) for k in range(7)]


def _config_text(cron: str) -> str:
    return f"""\
# likearr demo configuration, written by scripts/demo_state.py for the README screenshots.
# Nothing here is real: no Lidarr answers at this address, and the Spotify token file holds no token.

[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "spotify-token.json"
playlists = ["{PLAYLIST_ID}"]

[state]
db = "state.sqlite"
lock_file = "likearr.lock"

[rules]
# Keeps "Hey Jude" (a 1988 compilation) out, so "Not added" shows a setting at work.
allow_compilation_fallback = false

[schedule]
cron = "{cron}"
timezone = "UTC"
"""


def _write_token(path: Path, authorized: datetime) -> None:
    """What the Status and Settings pages read from a token file - when it was authorized, and its
    scopes - and nothing a child job could spend."""
    body = {
        "authorized_at": int(authorized.timestamp()),
        "scope": "user-follow-read user-library-read playlist-read-private",
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


@dataclass(slots=True)
class _Clock:
    """Stands in for `time` inside `likearr.shell.run_report`, whose health records stamp `time.time()`:
    each demo run is recorded at its own moment instead of all at once."""

    at: float = 0.0

    def time(self) -> float:
        return self.at

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _SimulatedTimeFormatter(logging.Formatter):
    def __init__(self, clock: _Clock) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.clock = clock

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(self.clock.at, UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class _Captured:
    out: str = ""
    log: str = ""


@contextlib.contextmanager
def _capture(clock: _Clock) -> Iterator[_Captured]:
    """A run's stdout (its summary and health line) and its `likearr` log, as a job would keep them."""
    captured = _Captured()
    out, err = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(err)
    handler.setFormatter(_SimulatedTimeFormatter(clock))
    logger = logging.getLogger("likearr")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        captured.out, captured.log = out.getvalue(), err.getvalue()


def _write_job(
    jobs: Path,
    *,
    kind: str,
    label: str,
    args: Sequence[str],
    started: datetime,
    finished: datetime,
    captured: _Captured,
    config_path: Path,
    job_id: str | None = None,
) -> JobMeta:
    """A finished job directory, as `JobRunner` leaves one: `meta.json`, `out.txt`, `log.txt`."""
    job_id = job_id or new_job_id(started)
    job_dir = jobs / job_id
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = JobMeta(
        id=job_id,
        kind=kind,
        argv=["likearr", "-c", str(config_path), *(a.replace("{job_dir}", str(job_dir)) for a in args)],
        label=label,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        exit_code=0,
        state=JobState.DONE,
        drain=kind != "plan",
    )
    diff = job_dir / "diff.json"
    if diff.is_file():
        meta = replace(meta, plan_token=plan_token_of_file(job_id, diff))
    (job_dir / "out.txt").write_text(captured.out, encoding="utf-8")
    (job_dir / "log.txt").write_text(captured.log, encoding="utf-8")
    (job_dir / "meta.json").write_text(meta.to_json(), encoding="utf-8")
    return meta


# --------------------------------------------------------------------------- the whole thing


@dataclass(frozen=True, slots=True)
class DemoInfo:
    """What the demo holds, for the screenshots and for its test."""

    out_dir: Path
    plan_id: str
    added_artists: tuple[str, ...]
    monitored: tuple[str, ...]
    unmonitored: tuple[str, ...]
    lookups: dict[str, tuple[str, str]] = field(default_factory=dict)
    """kind -> (a Look up query, something its answer shows)."""
    not_added: tuple[tuple[str, str], ...] = ()
    """("Not added" group heading, a title listed under it)."""


def build_demo(out_dir: Path, *, now: datetime | None = None) -> DemoInfo:
    """Write the demo data directory into `out_dir` (created if missing; must hold no state yet)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = out_dir / "config.toml"
    if (out_dir / "state.sqlite").exists() or config_path.exists():
        raise SystemExit(f"{out_dir} already holds a likearr data directory; give an empty one")

    cron, fires = _schedule(now)
    config_path.write_text(_config_text(cron), encoding="utf-8")
    config = load_config(config_path)
    _write_token(out_dir / "spotify-token.json", now - timedelta(days=24))
    write_names(names_path(config_path), {PLAYLIST_ID: PLAYLIST_NAME}, fetched_at=fires[-1])

    groups = _release_groups()
    source = FakeSource()
    lidarr = _lidarr(groups)
    sinks: list[Any] = [CapturingSink(), StdoutSink()]
    clock = _Clock()
    jobs = out_dir / "ui" / "jobs"
    ctx = make_context(
        out_dir, config=config, source=source, lidarr=lidarr, lookup=_lookup(groups), sinks=sinks, first_applied=False
    )

    def one_run(at: datetime, likes: _Likes, out: Path, **how: Any) -> _Captured:
        clock.at = at.timestamp()
        source.snapshot = _snapshot(likes, at=at, groups=groups)
        with _capture(clock) as captured:
            code = run_command(ctx, now=at, out=out, **how)
        if code != 0:
            raise SystemExit(f"demo run at {at:%Y-%m-%d %H:%M} exited {code}:\n{captured.log}{captured.out}")
        return captured

    try:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(run_report, "time", clock):
            first_diff = Path(scratch) / "first.json"
            # The first check and its reviewed apply, three hours before the first scheduled fire.
            start = fires[-1] - timedelta(hours=3)
            one_run(start, FIRST, first_diff, do_apply=False)
            one_run(start + timedelta(minutes=4), FIRST, first_diff, apply_path=first_diff, do_apply=True)
            _download(lidarr)
            # A day and a half of scheduled applies, a new liked song in two of them.
            last_fire: tuple[datetime, _Captured] | None = None
            for fire in reversed(fires):
                likes = DAY_TWO if fire == fires[0] else DAY_ONE if fire >= fires[4] else FIRST
                captured = one_run(
                    fire + timedelta(seconds=40), likes, Path(scratch) / "scheduled.json", do_apply=True, scheduled=True
                )
                _download(lidarr)
                last_fire = (fire, captured)
            # The check waiting on Review changes, twelve minutes ago.
            checked = now - timedelta(minutes=12)
            plan_id = new_job_id(checked - timedelta(seconds=35))
            plan_diff = jobs / plan_id / "diff.json"
            captured = one_run(checked, PENDING, plan_diff, do_apply=False)
        assert last_fire is not None
        fire, fired = last_fire
        _write_job(
            jobs,
            kind="scheduled",
            label="Scheduled run",
            args=["run", "--scheduled", "--apply"],
            started=fire,
            finished=fire + timedelta(seconds=70),
            captured=fired,
            config_path=config_path,
        )
        ctx.state.record_scheduled_fire(fire)
        _write_job(
            jobs,
            kind="plan",
            label="check",
            args=["run", "--out", "{job_dir}/diff.json"],
            started=checked - timedelta(seconds=35),
            finished=checked + timedelta(seconds=2),
            captured=captured,
            config_path=config_path,
            job_id=plan_id,
        )
    finally:
        ctx.close()
        ctx.state.close()

    diff = read_diff(plan_diff)
    titles = {mbid: title for title, mbid in RELEASES.items()}
    return DemoInfo(
        out_dir=out_dir,
        plan_id=plan_id,
        added_artists=tuple(sorted(a.name for a in diff.add_artists)),
        monitored=tuple(sorted(titles[m.key.rg_mbid] for m in diff.monitor)),
        unmonitored=tuple(sorted(titles[u.key.rg_mbid] for u in diff.unmonitor)),
        lookups={"artist": ("Radiohead", "OK Computer"), "album": ("Abbey Road", "The Beatles")},
        not_added=(
            ("Couldn't be matched", "Yesterday"),
            ("Couldn't be matched", AWESOME_MIX),
            ("Left out by your settings", "Hey Jude"),
        ),
    )


# --------------------------------------------------------------------------- a first review

HAND_MONITORED = {
    "The Beatles": ["Abbey Road", "Revolver", "Help!"],
    "Adele": ["19", "21"],
    "John Mayer": ["Continuum", "Heavier Things"],
    "Mark Ronson": ["Version", "Uptown Special"],
}
"""A library monitored by hand before likearr: some of it matches what Spotify likes, some doesn't."""

HELP = "Help!"
HELP_MBID = "8ad6f7b3-0d2a-4d6d-9d3e-6a1c7b3e0a11"
"""A made-up id for an album only the demo Lidarr holds. The liked song on it fails its
MusicBrainz lookup, so unmonitoring the rest would hold it back."""


class _FailingLookup(FakeLookup):
    """The demo MusicBrainz, failing on one album: a lookup that failed this run."""

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        if title == HELP:
            raise MetadataError(f"MusicBrainz did not answer for {artist!r} / {title!r}")
        return super().search_release_group_candidates(artist, title)


def _hand_monitored(lidarr: FakeLidarr, groups: dict[str, ReleaseGroup]) -> None:
    """Put `HAND_MONITORED` into the demo Lidarr: artists added by hand, their albums monitored."""
    album_id = 7000
    for artist_id, (name, titles) in enumerate(sorted(HAND_MONITORED.items()), start=700):
        mbid = ARTISTS[name]
        lidarr.artists[mbid] = lidarr_artist(mbid, id=artist_id, name=name, monitor_new_items="all")
        for title in titles:
            album_id += 1
            if title == HELP:
                rg = replace(groups[RELEASES["Revolver"]], mbid=HELP_MBID, title=HELP)
            else:
                rg = groups[RELEASES[title]]
            lidarr.albums.setdefault(mbid, {})[rg.mbid] = lidarr_album(
                rg, id=album_id, artist_id=artist_id, monitored=True, files=10
            )


def build_first_review(out_dir: Path, *, now: datetime | None = None) -> DemoInfo:
    """A new install over a library monitored by hand: Spotify connected, one check waiting on
    Review changes, nothing applied yet. The check's "Albums you already monitor" has albums that
    match what Spotify likes, albums that don't, and one held back after a failed lookup."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = out_dir / "config.toml"
    if (out_dir / "state.sqlite").exists() or config_path.exists():
        raise SystemExit(f"{out_dir} already holds a likearr data directory; give an empty one")

    cron, _ = _schedule(now)
    config_path.write_text(_config_text(cron), encoding="utf-8")
    config = load_config(config_path)
    _write_token(out_dir / "spotify-token.json", now - timedelta(hours=1))
    write_names(names_path(config_path), {PLAYLIST_ID: PLAYLIST_NAME}, fetched_at=now - timedelta(hours=1))

    groups = _release_groups()
    lookup = _FailingLookup().add(*groups.values())
    lookup.barcodes[AWESOME_MIX_BARCODE] = RELEASES[AWESOME_MIX]
    for artist, titles in FOLLOWED_CATALOGUES.items():
        lookup.catalogues[ARTISTS[artist]] = [RELEASES[t] for t in titles]
    for isrc, title in ISRCS.values():
        lookup.isrcs[isrc] = [RELEASES[title]]
    lidarr = _lidarr(groups)
    _hand_monitored(lidarr, groups)
    source = FakeSource()
    clock = _Clock()
    ctx = make_context(
        out_dir,
        config=config,
        source=source,
        lidarr=lidarr,
        lookup=lookup,
        sinks=[CapturingSink(), StdoutSink()],
        first_applied=False,
    )
    checked = now - timedelta(minutes=12)
    plan_id = new_job_id(checked - timedelta(seconds=35))
    plan_diff = out_dir / "ui" / "jobs" / plan_id / "diff.json"
    try:
        with mock.patch.object(run_report, "time", clock):
            clock.at = checked.timestamp()
            source.snapshot = _snapshot(FIRST, at=checked, groups=groups)
            with _capture(clock) as captured:
                code = run_command(ctx, now=checked, out=plan_diff)
            if code != 0:
                raise SystemExit(f"demo check exited {code}:\n{captured.log}{captured.out}")
        _write_job(
            out_dir / "ui" / "jobs",
            kind="plan",
            label="check",
            args=["run", "--out", "{job_dir}/diff.json"],
            started=checked - timedelta(seconds=35),
            finished=checked + timedelta(seconds=2),
            captured=captured,
            config_path=config_path,
            job_id=plan_id,
        )
    finally:
        ctx.close()
        ctx.state.close()
    diff = read_diff(plan_diff)
    titles = {mbid: title for title, mbid in RELEASES.items()}
    return DemoInfo(
        out_dir=out_dir,
        plan_id=plan_id,
        added_artists=tuple(sorted(a.name for a in diff.add_artists)),
        monitored=tuple(sorted(titles.get(m.key.rg_mbid, m.title) for m in diff.monitor)),
        unmonitored=(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a demo data directory for the README screenshots.")
    parser.add_argument("out_dir", type=Path, help="an empty directory to write the demo data directory into")
    parser.add_argument(
        "--first-review",
        action="store_true",
        help="a new install over a library monitored by hand, its first check waiting on Review changes",
    )
    args = parser.parse_args(argv)
    install_network_guard()
    info = build_first_review(args.out_dir) if args.first_review else build_demo(args.out_dir)
    config = info.out_dir.resolve() / "config.toml"
    emit_lines(
        [
            f"demo data directory written to {info.out_dir}",
            f"  the pending check adds {', '.join(info.added_artists)}, monitors {len(info.monitored)} releases "
            f"and unmonitors {len(info.unmonitored)}",
            "pages worth a screenshot, at http://127.0.0.1:8770 once started:",
            "  /  (Status)",
            f"  /plan/{info.plan_id}  (Review changes)",
            *(f"  /explain?query={quote_plus(query)}  (Look up)" for query, _ in info.lookups.values()),
            "  /unmatched  (Not added)",
            "start it:",
            f"  LIKEARR_UI_PASSWORD='{PASSWORD_HINT}' LIKEARR_SPOTIFY_CLIENT_ID=demo "
            "LIKEARR_LIDARR_URL=http://127.0.0.1:9 "
            f"uv run likearr start -c {config} --port 8770",
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
