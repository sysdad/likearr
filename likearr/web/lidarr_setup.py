"""Parse `likearr setup-profiles --json` for the web UI's Lidarr setup panel (issue #80).

The planning itself lives in `shell.setup_commands._build_setup_profiles_plan`, reused unchanged
by both the CLI's dry run and this ``--json`` line; this module only turns that line back into a
small view a template can render, and names the fixed argv `--apply` runs as a job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from likearr.web.jobs import last_json_object

__all__ = ["APPLY_ARGV", "LidarrSetupView", "ProfileView", "parse_setup_profiles_json"]

APPLY_ARGV = ["setup-profiles", "--apply"]
"""The exact, fixed argv the "Apply" confirm spawns through `JobRunner` - never built from
anything a request carries, so there is nothing a form could tamper into a different command."""


@dataclass(frozen=True, slots=True)
class ProfileView:
    name: str
    kind: str
    status: str
    """"ok", "missing" or "differs"."""
    applies: bool = False
    """Will `--apply` write anything here? A metadata profile's "differs" is always `False` -
    `ensure_metadata_profile` reuses an existing profile by name as-is and never edits it - so
    `status` and `applies` must both be read; `status` alone cannot say what applying will do."""
    id: int | None = None
    diff: dict[str, list[str]] | None = None


@dataclass(frozen=True, slots=True)
class LidarrSetupView:
    profiles: tuple[ProfileView, ...]
    tag: dict[str, Any]
    root_folder: dict[str, Any]
    todo: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def needs_apply(self) -> bool:
        return bool(self.todo)

    @property
    def kept_as_is(self) -> tuple[ProfileView, ...]:
        """Metadata profiles that differ from what likearr would create but that `--apply` will
        not touch - unlike a differing root folder (`root_folder["applies"]` is `True` there:
        `set_root_folder_defaults` really does overwrite its monitor defaults), a differing profile
        is always kept exactly as it is. The page shows these even when `needs_apply` is `False`."""
        return tuple(p for p in self.profiles if p.status == "differs" and not p.applies)


def parse_setup_profiles_json(output: str) -> LidarrSetupView | None:
    """`output` is a finished ``setup-profiles --json`` job's stdout. `None` when it is not
    readable as that - the page then falls back to the job's own log."""
    payload = last_json_object(output, lambda d: True, strict=True)
    if payload is None:
        return None
    if isinstance(payload.get("error"), str):
        return LidarrSetupView(profiles=(), tag={}, root_folder={}, error=payload["error"])
    raw_profiles = payload.get("profiles")
    tag = payload.get("tag")
    root_folder = payload.get("root_folder")
    todo = payload.get("todo")
    if not isinstance(raw_profiles, list) or not isinstance(tag, dict) or not isinstance(root_folder, dict):
        return None
    if not isinstance(todo, list) or not all(isinstance(t, str) for t in todo):
        return None
    profiles: list[ProfileView] = []
    for entry in raw_profiles:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("status"), str)
        ):
            return None
        profiles.append(
            ProfileView(
                name=entry["name"],
                kind=str(entry.get("kind") or ""),
                status=entry["status"],
                applies=bool(entry.get("applies", False)),
                id=entry.get("id") if isinstance(entry.get("id"), int) else None,
                diff=entry.get("diff") if isinstance(entry.get("diff"), dict) else None,
            )
        )
    return LidarrSetupView(profiles=tuple(profiles), tag=tag, root_folder=root_folder, todo=tuple(todo))
