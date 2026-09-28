"""likearr: mirror your Spotify follows, saved albums and Liked Songs into Lidarr.

The version has exactly one source: the static ``version`` in ``pyproject.toml``. It is read
here through ``importlib.metadata`` off the installed distribution, never hand-copied, so the
two can't drift apart again.
"""

from __future__ import annotations

import os
from importlib import metadata

__all__ = ["__version__", "build_info", "commit"]


def _resolve_version() -> str:
    """`pyproject.toml`'s `[project] version`, read off the installed distribution.

    Falls back to `0+unknown` when likearr has no installed distribution to read - a bare
    source checkout run without `uv sync`/`pip install` - rather than raising, so the fallback
    itself is never the reason a run fails.
    """
    try:
        return metadata.version("likearr")
    except metadata.PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()


def commit() -> str | None:
    """The short commit SHA baked into a Docker image via the `LIKEARR_COMMIT` env var.

    `None` when it was never set - a local `uv run likearr` outside the image, or an image built
    without the `VCS_REF` build arg - which callers treat as "unknown" in their own wording rather
    than being handed the literal string here.
    """
    return os.environ.get("LIKEARR_COMMIT") or None


def build_info() -> str:
    """`0.5.0` locally, `0.5.0 (abc1234)` in an image built with `--build-arg VCS_REF=abc1234`."""
    sha = commit()
    return f"{__version__} ({sha})" if sha else __version__
