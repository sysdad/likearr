"""Logging configuration for the CLI shell.

`setup_logging` wires a single stderr handler with ISO-8601 timestamps, quiets `httpx`/`httpcore`
so request lines (which include full URLs with query strings, e.g. `apikey=...`) never surface at
INFO, and installs a filter that redacts credential-looking values wherever they appear in a log
message or its traceback.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from likearr.adapters.http import redact

_FORMATTER = logging.Formatter()
"""Renders a record's traceback when no handler has yet, so it can be redacted before one does."""


def _unformattable(record: logging.LogRecord) -> str:
    """A malformed call's message - arguments that don't fit the format, or an object whose str()
    raises - as the format alone, so the line is logged rather than raising. The arguments' values
    are left out: nothing labels them, so no pattern could tell a secret among them."""
    try:
        return f"{record.msg} (log arguments did not fit the format)"
    except Exception:
        return "(a log message that could not be rendered)"


class RedactingFilter(logging.Filter):
    """Replaces credential-looking values in a log message and its traceback with `REDACTED`,
    using the same patterns as every adapter's error text (`likearr.adapters.http.redact`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            original: str | None = record.getMessage()
        except Exception:
            original = None
        message = redact(_unformattable(record) if original is None else original)
        if message != original:
            record.msg = message
            record.args = ()
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = _FORMATTER.formatException(record.exc_info)
            except Exception:
                record.exc_text = "(a traceback that could not be rendered)"
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
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
