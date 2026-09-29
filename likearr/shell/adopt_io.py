"""`adopt.json`: the reviewable plan of record for `adopt`, written by a plan and read back by `--apply`.

The same contract as `diff.json` (see `shell.diff_io`): a complete description of what will be
done plus the digests of the world it was computed against, so `--apply` executes exactly this
file and refuses one the world has moved out from under. The keep list is baked into the plan -
which is the point: applying never re-derives what to keep from a file that may not be there.

`mode` is ``claim`` (the default: claim what a source wants, leave the rest) or
``unmonitor-rest``. A plan with no `mode` predates it and reads as ``unmonitor-rest``, which is
what it was. `held` and `left` are for the reader only: `--apply` does nothing with them.

The same rows, with the Lidarr digest they were planned against, are what a first check writes
into its `diff.json` as ``existing_albums`` (`ExistingAlbums`) for the web review to offer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from likearr.core.adopt import HELD_CATALOGUE, AdoptPlan, HeldRelease, LeftRelease
from likearr.fsio import write_atomic
from likearr.models import UnmonitorRelease
from likearr.shell.diff_io import DiffFileError, _key_from_dict, _key_to_dict, _owned_from_dict, _owned_to_dict

__all__ = [
    "ADOPT_PLAN_KIND",
    "MODE_CLAIM",
    "MODE_UNMONITOR_REST",
    "AdoptPlanFile",
    "ExistingAlbums",
    "adopt_plan_to_dict",
    "adoption_from_dict",
    "adoption_to_dict",
    "existing_from_dict",
    "existing_to_dict",
    "read_adopt_plan",
    "read_existing",
    "write_adopt_plan",
]

ADOPT_PLAN_KIND = "adopt-plan"


@dataclass(frozen=True, slots=True)
class AdoptPlanFile:
    """An adoption plan and the world it was computed against."""

    created_at: datetime
    source_digest: str
    lidarr_digest: str
    resolver_version: int
    adoption: AdoptPlan


MODE_CLAIM = "claim"
MODE_UNMONITOR_REST = "unmonitor-rest"


def adoption_to_dict(adoption: AdoptPlan) -> dict[str, Any]:
    """An `AdoptPlan` as JSON: its mode, a summary of counts and every row."""
    return {
        "mode": MODE_UNMONITOR_REST if adoption.unmonitor_rest else MODE_CLAIM,
        "summary": {
            "claim": len(adoption.claim),
            "keep": len(adoption.keep_as_manual),
            "unmonitor": len(adoption.unmonitor),
            "held": len(adoption.held),
            "left": len(adoption.left),
        },
        "claim": [_owned_to_dict(r) for r in adoption.claim],
        "keep_as_manual": [_owned_to_dict(r) for r in adoption.keep_as_manual],
        "unmonitor": [{"key": _key_to_dict(u.key), "title": u.title} for u in adoption.unmonitor],
        "held": [
            {"key": _key_to_dict(h.key), "title": h.title, "step": h.step, "cause": h.cause, "reason": h.reason}
            for h in adoption.held
        ],
        "left": [{"key": _key_to_dict(r.key), "title": r.title} for r in adoption.left],
    }


def adoption_from_dict(raw: Mapping[str, Any]) -> AdoptPlan:
    """Read `adoption_to_dict` back. A plan with no ``mode`` predates it and was always an
    unmonitor-the-rest plan, so it reads as one. Raises KeyError, TypeError or ValueError."""
    mode = raw.get("mode", MODE_UNMONITOR_REST)
    if mode not in (MODE_CLAIM, MODE_UNMONITOR_REST):
        raise ValueError(f"unknown adopt mode {mode!r}")
    adoption = AdoptPlan(
        claim=[_owned_from_dict(r) for r in _items(raw, "claim")],
        keep_as_manual=[_owned_from_dict(r) for r in _items(raw, "keep_as_manual")],
        unmonitor=[
            UnmonitorRelease(key=_key_from_dict(u["key"]), title=str(u.get("title") or ""), lost_reasons=frozenset())
            for u in _items(raw, "unmonitor")
        ],
        held=[
            HeldRelease(
                key=_key_from_dict(h["key"]),
                title=str(h.get("title") or ""),
                step=str(h.get("step") or ""),
                cause=str(h.get("cause") or HELD_CATALOGUE),
            )
            for h in _items(raw, "held")
        ],
        left=[LeftRelease(key=_key_from_dict(r["key"]), title=str(r.get("title") or "")) for r in _items(raw, "left")],
        unmonitor_rest=mode == MODE_UNMONITOR_REST,
    )
    if not adoption.unmonitor_rest and (adoption.unmonitor or adoption.keep_as_manual):
        raise ValueError("a claim-only plan lists releases to unmonitor or keep")
    return adoption


def adopt_plan_to_dict(plan: AdoptPlanFile) -> dict[str, Any]:
    return {
        "kind": ADOPT_PLAN_KIND,
        "created_at": plan.created_at.isoformat(),
        "source_digest": plan.source_digest,
        "lidarr_digest": plan.lidarr_digest,
        "resolver_version": plan.resolver_version,
        **adoption_to_dict(plan.adoption),
    }


def _items(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise DiffFileError(f"adopt plan field {key!r} is not a list")
    return [item for item in value if isinstance(item, Mapping)]


def adopt_plan_from_dict(raw: Mapping[str, Any]) -> AdoptPlanFile:
    if raw.get("kind") != ADOPT_PLAN_KIND:
        raise DiffFileError("this is not an adopt plan; write one with `likearr adopt --out FILE`")
    try:
        return AdoptPlanFile(
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            source_digest=str(raw["source_digest"]),
            lidarr_digest=str(raw["lidarr_digest"]),
            resolver_version=int(raw["resolver_version"]),
            adoption=adoption_from_dict(raw),
        )
    except DiffFileError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DiffFileError(f"this does not look like a likearr adopt plan: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ExistingAlbums:
    """A first check's "Albums you already monitor": the full adoption (every match claimed, the
    rest planned for unmonitor with its holds) and the Lidarr digest it was planned against. The
    reviewer's choice picks the part to apply (`core.adopt.choose`)."""

    adoption: AdoptPlan
    lidarr_digest: str
    artists: dict[str, str] = field(default_factory=dict)
    """artist MBID -> name in Lidarr, for every artist the rows name."""
    titles: dict[str, str] = field(default_factory=dict)
    """release group MBID -> title in Lidarr, for the claims (the other rows carry their own)."""


def existing_to_dict(existing: ExistingAlbums) -> dict[str, Any]:
    return {
        "lidarr_digest": existing.lidarr_digest,
        "artists": dict(sorted(existing.artists.items())),
        "titles": dict(sorted(existing.titles.items())),
        **adoption_to_dict(existing.adoption),
    }


def existing_from_dict(raw: object) -> ExistingAlbums | None:
    """The ``existing_albums`` of a `diff.json`, or ``None`` when it has none. Raises
    `DiffFileError` when it is there but does not read."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise DiffFileError("diff file field 'existing_albums' is not an object")
    try:
        artists, titles = raw.get("artists") or {}, raw.get("titles") or {}
        if not isinstance(artists, Mapping) or not isinstance(titles, Mapping):
            raise ValueError("'artists' or 'titles' is not an object")
        return ExistingAlbums(
            adoption=adoption_from_dict(raw),
            lidarr_digest=str(raw["lidarr_digest"]),
            artists={str(k): str(v) for k, v in artists.items()},
            titles={str(k): str(v) for k, v in titles.items()},
        )
    except DiffFileError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DiffFileError(f"diff file field 'existing_albums' does not read: {exc}") from exc


def read_existing(path: Path) -> ExistingAlbums | None:
    """The ``existing_albums`` of the `diff.json` at `path`. Raises `DiffFileError` when the file
    will not read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DiffFileError(f"cannot read the diff file at {path}: {exc}") from exc
    except ValueError as exc:
        raise DiffFileError(f"the diff file at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DiffFileError(f"the diff file at {path} is not a JSON object")
    return existing_from_dict(raw.get("existing_albums"))


def write_adopt_plan(plan: AdoptPlanFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(adopt_plan_to_dict(plan), indent=2) + "\n", mode=0o600)


def read_adopt_plan(path: Path) -> AdoptPlanFile:
    """Read a plan back. Raises `DiffFileError` when it is missing, not JSON, or not an adopt plan."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiffFileError(f"no adopt plan at {path} - run `likearr adopt` first") from exc
    except OSError as exc:
        raise DiffFileError(f"cannot read the adopt plan at {path}: {exc}") from exc
    except ValueError as exc:
        raise DiffFileError(f"the adopt plan at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DiffFileError(f"the adopt plan at {path} is not a JSON object")
    return adopt_plan_from_dict(raw)
