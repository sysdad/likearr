"""Logging configuration for the CLI shell.

`setup_logging` wires a single stderr handler with ISO-8601 timestamps, quiets `httpx`/`httpcore`
so request lines (which include full URLs with query strings, e.g. `apikey=...`) never surface at
INFO, and installs a filter that redacts credential-looking values wherever they appear in a log
message.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime

# Each pattern's own match spans the whole thing to redact: the "prefix" (key/header name plus
# separator, captured in group 1, kept in the output) followed - uncaptured - by everything that
# makes up the secret value. `Authorization` additionally swallows an optional scheme word
# ("Bearer ", "Basic ", ...) so the actual token isn't left exposed one word after it: a plain
# `\S+` after the separator would only consume "Bearer" and stop at the space before the token.
_REDACT_PATTERNS = [
    re.compile(r"(?i)(X-Api-Key[\"']?\s*[:=]\s*[\"']?)[^\s\"',&]+"),
    re.compile(r"(?i)(Authorization[\"']?\s*[:=]\s*[\"']?)(?:(?:Bearer|Basic|Token|ApiKey)\s+)?[^\s\"',&]+"),
    re.compile(r"(?i)(apikey=)[^\s&'\"]+"),
    re.compile(r"(?i)(access_token[\"']?\s*[:=]\s*[\"']?)[^\s\"',&]+"),
    re.compile(r"(?i)(refresh_token[\"']?\s*[:=]\s*[\"']?)[^\s\"',&]+"),
    # Spotify's authorization ``code`` query parameter: it is a one-time secret good for a token
    # exchange, so an OAuth callback URL (``/spotify/callback``, and uvicorn's own
    # access log line for it) must never carry it in the clear. Matched only as a query parameter
    # (preceded by ``?`` or ``&``), not the many unrelated things named "code" elsewhere.
    re.compile(r"(?i)([?&]code=)[^\s&'\"]+"),
]


class RedactingFilter(logging.Filter):
    """Replaces credential-looking values in a log message with `<redacted>`.

    Substitution always replaces the *entire* match (prefix and secret alike) with
    `group(1) + "<redacted>"` - never just a captured sub-group - so nothing between "the label"
    and "the end of the match" can survive uncaught.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        original = record.getMessage()
        redacted = original
        for pattern in _REDACT_PATTERNS:
            redacted = pattern.sub(lambda m: m.group(1) + "<redacted>", redacted)
        if redacted != original:
            record.msg = redacted
            record.args = ()
        return True


class _IsoFormatter(logging.Formatter):
    """A `logging.Formatter` that renders timestamps as ISO-8601 (UTC)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="seconds")


def setup_logging(verbose: bool) -> None:
    """Configure root logging: stderr handler, ISO timestamps, INFO or DEBUG, secrets redacted."""
    level = logging.DEBUG if verbose else logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_IsoFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # httpx/httpcore log full request URLs (including query-string secrets like `apikey=...`) at
    # INFO. Keep them at WARNING regardless of `verbose` so those lines are never emitted.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
