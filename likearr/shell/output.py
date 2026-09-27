"""User-facing output.

Everything a command prints for a human goes through :func:`emit`, and nothing goes through
`print`. Two reasons: stdout also carries the health record's JSON line, so it is worth having
one place that owns it; and a single funnel makes it trivial for a test to capture what a
command said.

Logging (stderr) is for the operator's trail; this is for the answer they asked for.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

__all__ = ["emit", "emit_lines"]


def emit(line: str = "") -> None:
    """Write one line to stdout."""
    sys.stdout.write(line + "\n")


def emit_lines(lines: Iterable[str]) -> None:
    """Write several lines to stdout, flushing once."""
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    sys.stdout.flush()
