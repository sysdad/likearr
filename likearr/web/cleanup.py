"""Finishing a clean up: the checklist after Export, for any standard likearr install.

After "Export the decisions", Clean up walks the reviewer through carrying the review out. The
page previews what it can, read-only, as child jobs - never in the server process:

- **the move**: `prune-stage --no-mount-check --out` (no ``--apply``): the totals and the Lidarr
  plan, from Lidarr's data. The web UI has no library mount, so the mount is left to the terminal
  preview, which checks it;
- **Lidarr**: `prune-checks` - import lists with automatic add, and the command queue;
- **Spotify**: the `promote-save` plan, which makes Spotify reads only.

The steps that change something - `prune-stage --apply`, `promote-save --apply` - stay commands
the reviewer runs in a terminal, shown exactly: `[ui] cli_command` in front, ``-c`` the config the
server was started with, the holding folder from `[prune] holding_dir`, the files by their paths in
the job store. No command here ever carries ``--force``, and none deletes anything: emptying the
holding folder is the reviewer's, by hand.

A preview is bound to the export it previewed (`Binding`, ``previews.json`` beside the report): its
sha256 of ``decisions.json``. Exporting again, or any change that removes the exported files,
leaves the old previews unshown.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from likearr.fsio import write_atomic
from likearr.shell.promote_save import PromoteSaveError, read_plan, save_differs, spotify_label

__all__ = [
    "BINDING_FILE",
    "Binding",
    "Checks",
    "Commands",
    "SpotifyPreview",
    "StagePreview",
    "asks_spotify",
    "commands",
    "decisions_digest",
    "read_binding",
    "read_checks",
    "read_spotify",
    "read_stage",
    "trashes",
    "write_binding",
]

BINDING_FILE = "previews.json"
_NAME = 300
"""Longest name or reason shown from a preview's file: they are data, and a page must stay a page."""


@dataclass(slots=True)
class Binding:
    """Which preview jobs belong to which export."""

    decisions_sha256: str
    stage: str = ""
    spotify: str = ""
    checks: str = ""
    asks_spotify: bool = False
    """The export asks for a follow or a save, so the chain includes the Spotify preview."""
    trashes: bool = True
    """The export trashes an album, so the chain includes the move and the Lidarr checks."""

    def chain(self) -> list[str]:
        """The preview steps this export needs, in the order they run."""
        return [
            *(["stage"] if self.trashes else []),
            *(["spotify"] if self.asks_spotify else []),
            *(["checks"] if self.trashes else []),
        ]


def decisions_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_binding(job_dir: Path) -> Binding | None:
    try:
        raw = json.loads((job_dir / BINDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("decisions_sha256"), str):
        return None
    ids = {k: raw[k] if isinstance(raw.get(k), str) else "" for k in ("stage", "spotify", "checks")}
    return Binding(
        raw["decisions_sha256"],
        asks_spotify=raw.get("asks_spotify") is True,
        trashes=raw.get("trashes") is not False,
        **ids,
    )


def write_binding(job_dir: Path, binding: Binding) -> None:
    write_atomic(job_dir / BINDING_FILE, json.dumps(asdict(binding), indent=1), mode=0o600)


def _lists_any(decisions: Path, keys: tuple[str, ...], *, unreadable: bool) -> bool:
    """Whether an exported decisions file has a non-empty list under any of `keys`; `unreadable`
    when the file does not read as a JSON object."""
    try:
        raw = json.loads(decisions.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unreadable
    if not isinstance(raw, dict):
        return unreadable
    return any(isinstance(raw.get(k), list) and raw[k] for k in keys)


def asks_spotify(decisions: Path) -> bool:
    """Whether an exported decisions file asks `promote-save` for anything."""
    return _lists_any(decisions, ("promote", "save", "save_releases"), unreadable=False)


def trashes(decisions: Path) -> bool:
    """Whether an exported decisions file asks `prune-stage` to move anything. An unreadable file
    counts as yes, so the move preview runs and says what is wrong."""
    return _lists_any(decisions, ("trash", "trash_artists"), unreadable=True)


def _text(value: object) -> str:
    return str(value or "")[:_NAME]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---------------------------------------------------------------- the three previews


@dataclass(slots=True)
class StagePreview:
    files: int
    bytes: int
    albums: int
    remove: list[tuple[str, str]]
    """(artist, why): removed from Lidarr, the row only, never files."""
    rescan: list[tuple[str, str]]
    decisions_sha256: str


def read_stage(path: Path | None) -> StagePreview | None:
    """A `prune-stage --out` summary, or ``None`` when it will not read."""
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    def pairs(key: str) -> list[tuple[str, str]]:
        items = raw.get(key)
        return (
            [(_text(i.get("name")), _text(i.get("why"))) for i in items if isinstance(i, dict)]
            if isinstance(items, list)
            else []
        )

    return StagePreview(
        files=_count(raw.get("files")),
        bytes=_count(raw.get("bytes")),
        albums=_count(raw.get("albums")),
        remove=pairs("remove"),
        rescan=pairs("rescan"),
        decisions_sha256=_text(raw.get("decisions_sha256")),
    )


@dataclass(slots=True)
class Checks:
    auto_add: list[str] | None
    """Names of the import lists with automatic add; ``None`` when Lidarr did not answer."""
    queue: list[tuple[str, str]] | None
    """(command, status) still queued or running; ``None`` when Lidarr did not answer."""
    errors: list[str] = field(default_factory=list)
    checked_at: str = ""


def read_checks(path: Path | None) -> Checks | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    lists, queue, errors = raw.get("import_lists"), raw.get("queue"), raw.get("errors")
    return Checks(
        auto_add=[_text(r.get("name")) for r in lists if isinstance(r, dict) and r.get("auto_add") is True]
        if isinstance(lists, list)
        else None,
        queue=[(_text(r.get("name")), _text(r.get("status"))) for r in queue if isinstance(r, dict)]
        if isinstance(queue, list)
        else None,
        errors=[_text(v) for v in errors.values()] if isinstance(errors, dict) else [],
        checked_at=_text(raw.get("checked_at")),
    )


@dataclass(slots=True)
class SpotifyPreview:
    follow: list[str]
    save: list[str]
    already: int
    """Matched, and already followed or saved: nothing to write."""
    unmatched: list[tuple[str, str]]
    excluded_unreviewed: list[tuple[str, str]]
    budget_exhausted: bool


def read_spotify(path: Path | None) -> SpotifyPreview | None:
    """The `promote-save` plan the preview wrote, in the reviewer's terms."""
    if path is None:
        return None
    try:
        plan = read_plan(path)
    except (PromoteSaveError, OSError, ValueError, KeyError, TypeError):
        return None
    return SpotifyPreview(
        follow=[_text(f.name) for f in plan.follow],
        save=[
            _text(f"{s.artist_name} - {s.title}" + (f" (on Spotify: {spotify_label(s)})" if save_differs(s) else ""))
            for s in plan.save
        ],
        already=len(plan.already_followed) + len(plan.already_saved),
        unmatched=[(_text(u.name), _text(u.reason)) for u in plan.unmatched],
        excluded_unreviewed=[(_text(u.name), _text(u.reason)) for u in plan.excluded_unreviewed],
        budget_exhausted=plan.budget_exhausted,
    )


# ---------------------------------------------------------------- the commands


@dataclass(frozen=True, slots=True)
class Commands:
    """Exactly what to paste, in order. Previews first; only the ``--apply`` lines change anything."""

    stage_preview: str
    stage_apply: str
    spotify_apply: str
    """Applies the plan the page's own Spotify preview made; empty when there is none."""
    spotify_plan: str
    """Makes a fresh plan beside the report: when the page has none, or ``--apply`` says it is stale."""
    spotify_apply_fresh: str
    """Applies that fresh plan."""
    auth: str
    """Re-authorizes Spotify with the write scopes `--promote-save` step 6 needs."""


def _line(prefix: str, parts: Sequence[str]) -> str:
    return " ".join([prefix, *(shlex.quote(p) for p in parts)])


def commands(
    *,
    prefix: str,
    config_path: Path,
    job_dir: Path,
    holding: str,
    preview_plan: Path | None,
) -> Commands:
    """The terminal steps for one export, by the paths the server sees - the same inside the
    documented install's one-shot container, which mounts the same ``/data``."""
    config = ["-c", str(config_path)]
    report, decisions, reviewed = job_dir / "prune.json", job_dir / "decisions.json", job_dir / "review-data.json"
    fresh = job_dir / "promote-save.json"
    stage = ["prune-stage", *config, "--manifest", str(report), "--holding", holding, "--decisions", str(decisions)]
    plan = ["promote-save", *config, "--decisions", str(decisions), "--reviewed", str(reviewed), "--out", str(fresh)]
    return Commands(
        stage_preview=_line(prefix, stage),
        stage_apply=_line(prefix, [*stage, "--apply"]),
        spotify_apply=_line(prefix, ["promote-save", *config, "--apply", str(preview_plan)]) if preview_plan else "",
        spotify_plan=_line(prefix, plan),
        spotify_apply_fresh=_line(prefix, ["promote-save", *config, "--apply", str(fresh)]),
        auth=_line(prefix, ["auth", *config, "--manual", "--promote-save"]),
    )
