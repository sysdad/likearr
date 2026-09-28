"""`docs/spotify.md`: the redirect URI and scopes it tells a reader to use must match what the code
sends Spotify. Each value is read from the code, so a change there fails this test instead of
leaving the doc wrong.
"""

from __future__ import annotations

from pathlib import Path

from likearr.config import SpotifyConfig
from likearr.models import SPOTIFY_READ_SCOPES, SPOTIFY_WRITE_SCOPES

SPOTIFY_DOC = Path(__file__).resolve().parents[2] / "docs" / "spotify.md"


def test_the_default_redirect_uri_matches_the_config_default() -> None:
    default_redirect_uri = SpotifyConfig.__dataclass_fields__["redirect_uri"].default
    assert default_redirect_uri in SPOTIFY_DOC.read_text()


def test_the_read_and_write_scopes_match_the_code() -> None:
    text = SPOTIFY_DOC.read_text()
    assert " ".join(SPOTIFY_READ_SCOPES) in text
    assert " ".join(SPOTIFY_WRITE_SCOPES) in text
