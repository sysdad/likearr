"""`docs/spotify.md`: the redirect URI and scopes it documents must never drift from
what the code actually sends Spotify. Each fact here is read from the source of truth - `SpotifyConfig`'s
default and `models.SPOTIFY_READ_SCOPES` / `SPOTIFY_WRITE_SCOPES` - rather than hardcoded a second
time, so a future change to either fails this test instead of silently going stale in the doc.
"""

from __future__ import annotations

from pathlib import Path

from likearr.config import SpotifyConfig
from likearr.models import SPOTIFY_READ_SCOPES, SPOTIFY_WRITE_SCOPES

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"
SPOTIFY_DOC = DOCS_ROOT / "spotify.md"


def _text() -> str:
    return SPOTIFY_DOC.read_text()


def test_the_default_redirect_uri_is_documented_and_matches_the_config_default() -> None:
    default_redirect_uri = SpotifyConfig.__dataclass_fields__["redirect_uri"].default
    assert default_redirect_uri == "http://127.0.0.1:8765/callback"  # sanity: what this test guards
    assert default_redirect_uri in _text()


def test_the_loopback_ip_is_named_and_localhost_is_called_out_as_rejected() -> None:
    text = _text()
    assert "127.0.0.1" in text
    assert "never `localhost`" in text or "not `localhost`" in text
    assert "Spotify rejects" in text


def test_the_read_and_write_scopes_are_documented_and_match_the_code() -> None:
    text = _text()
    assert " ".join(SPOTIFY_READ_SCOPES) in text
    assert " ".join(SPOTIFY_WRITE_SCOPES) in text


def test_pkce_and_no_client_secret_are_documented() -> None:
    text = _text()
    assert "PKCE" in text
    assert "LIKEARR_SPOTIFY_CLIENT_ID" in text
    assert "Client Secret" in text


def test_the_development_mode_five_user_allowlist_is_documented() -> None:
    text = _text()
    assert "5 Spotify accounts" in text or "5-user allowlist" in text


def test_readme_links_to_the_spotify_app_guide() -> None:
    readme = (DOCS_ROOT.parent / "README.md").read_text()
    assert "docs/spotify.md" in readme


def test_the_docs_say_read_only_by_default_and_write_on_opt_in() -> None:
    """Sign-in asks for the read scopes only; `--promote-save` adds the write ones. The
    README, DEPLOY.md, CLI.md and this guide must all say so, and name the opt-in command."""
    for path in (DOCS_ROOT.parent / "README.md", DOCS_ROOT / "DEPLOY.md", DOCS_ROOT / "CLI.md", SPOTIFY_DOC):
        text = path.read_text()
        assert "read-only by default" in text, path.name
        assert "--promote-save" in text, path.name
