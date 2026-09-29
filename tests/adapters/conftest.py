"""Shared fixtures for the adapter tests.

Every id, key and token in these tests is obviously fake: MBIDs are ``0000...``-style, Spotify
ids spell out what they are, and the Lidarr API key is the literal string ``fake-api-key``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from likearr.adapters.http import build_client
from likearr.config import LidarrConfig, MusicBrainzConfig, SpotifyConfig
from tests.clock import FakeClock

FAKE_API_KEY = "fake-api-key-0000"
LIDARR_URL = "http://lidarr.test:8686"
MB_URL = "https://musicbrainz.test/ws/2"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with build_client() as c:
        yield c


@pytest.fixture
def spotify_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SpotifyConfig:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    monkeypatch.delenv("LIKEARR_SPOTIFY_CLIENT_SECRET", raising=False)
    return SpotifyConfig(token_file=tmp_path / "spotify-token.json")


@pytest.fixture
def lidarr_config() -> LidarrConfig:
    return LidarrConfig(url=LIDARR_URL, root_folder="/music", quality_profile="Standard")


@pytest.fixture
def mb_config() -> MusicBrainzConfig:
    return MusicBrainzConfig(contact="likearr@example.test", base_url=MB_URL, min_interval_s=1.0)
