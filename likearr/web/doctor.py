"""Parse `likearr doctor --json` for the web UI's read-only Doctor view (issue #80).

`doctor` itself changes nothing and is run as a child job through `JobRunner`, same as every
other Lidarr- or Spotify-reading command the UI starts; this module only turns its `--json` line
back into the `shell.setup_commands.Check` shape so a template can list pass/warn/fail without
caring about the wire format.
"""

from __future__ import annotations

from dataclasses import dataclass

from likearr.web.jobs import last_json_object

__all__ = ["DoctorCheck", "DoctorView", "parse_doctor_json"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    level: str
    """PASS, WARN, FAIL or SKIP."""
    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorView:
    checks: tuple[DoctorCheck, ...]
    total: int
    failed: int
    warnings: int
    skipped: int

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def problems(self) -> tuple[DoctorCheck, ...]:
        """What needs attention, listed first (#85): every FAIL, then every WARN - and any level
        this module does not know, which is safer shown than hidden - each in doctor's order."""
        fails = tuple(c for c in self.checks if c.level == "FAIL")
        return fails + tuple(c for c in self.checks if c.level not in ("FAIL", "PASS", "SKIP"))

    @property
    def passed(self) -> tuple[DoctorCheck, ...]:
        return tuple(c for c in self.checks if c.level == "PASS")

    @property
    def skipped_checks(self) -> tuple[DoctorCheck, ...]:
        return tuple(c for c in self.checks if c.level == "SKIP")


def parse_doctor_json(output: str) -> DoctorView | None:
    """`output` is a finished doctor job's stdout: the one JSON line `doctor_command` prints for
    `--json`. `None` for anything that is not that (a crash before the checks ran, a stray extra
    line, a version mismatch) - the page falls back to "the log below says why" like any other
    unreadable job answer, rather than guessing at a shape it cannot trust."""
    payload = last_json_object(output, lambda d: True, strict=True)
    if payload is None:
        return None
    raw_checks = payload.get("checks")
    summary = payload.get("summary")
    if not isinstance(raw_checks, list) or not isinstance(summary, dict):
        return None
    checks: list[DoctorCheck] = []
    for entry in raw_checks:
        if not isinstance(entry, dict):
            return None
        level, name, detail = entry.get("level"), entry.get("name"), entry.get("detail")
        if not isinstance(level, str) or not isinstance(name, str) or not isinstance(detail, str):
            return None
        checks.append(DoctorCheck(level=level, name=name, detail=detail))
    try:
        return DoctorView(
            checks=tuple(checks),
            total=int(summary["total"]),
            failed=int(summary["failed"]),
            warnings=int(summary["warnings"]),
            skipped=int(summary["skipped"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
