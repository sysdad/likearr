"""`adopt.json`: the reviewable plan of record for `adopt`, written by a plan and read back by `--apply`.

The same contract as `diff.json` (see `shell.diff_io`): a complete description of what will be
done plus the digests of the world it was computed against, so `--apply` executes exactly this
file and refuses one the world has moved out from under. The keep list is baked into the plan -
which is the point: applying never re-derives what to keep from a file that may not be there.

`held` (#6) lists what the plan leaves alone because its artist's catalogue was not read. It is
for the reader only: `--apply` does nothing with it, and a plan written before it existed reads
as holding nothing. The `summary` keeps its original three counts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from likearr.core.adopt import AdoptPlan, HeldRelease
from likearr.fsio import write_atomic
from likearr.models import OwnedRelease, UnmonitorRelease
from likearr.shell.diff_io import DiffFileError, _key_from_dict, _key_to_dict, _reasons_from_list, _reasons_to_list

__all__ = ["ADOPT_PLAN_KIND", "AdoptPlanFile", "adopt_plan_to_dict", "read_adopt_plan", "write_adopt_plan"]

ADOPT_PLAN_KIND = "adopt-plan"


@dataclass(frozen=True, slots=True)
class AdoptPlanFile:
    """An adoption plan and the world it was computed against."""

    created_at: datetime
    source_digest: str
    lidarr_digest: str
    resolver_version: int
    adoption: AdoptPlan


def _owned_to_dict(record: OwnedRelease) -> dict[str, Any]:
    return {
        "key": _key_to_dict(record.key),
        "reasons": _reasons_to_list(record.reasons),
        "step": record.step,
        "resolver_version": record.resolver_version,
        "monitored_at": record.monitored_at.isoformat(),
        "lidarr_album_id": record.lidarr_album_id,
    }


def _owned_from_dict(raw: Mapping[str, Any]) -> OwnedRelease:
    album_id = raw.get("lidarr_album_id")
    return OwnedRelease(
        key=_key_from_dict(raw["key"]),
        reasons=_reasons_from_list(raw.get("reasons")),
        step=str(raw.get("step") or ""),
        resolver_version=int(raw["resolver_version"]),
        monitored_at=datetime.fromisoformat(str(raw["monitored_at"])),
        lidarr_album_id=int(album_id) if album_id is not None else None,
    )


def adopt_plan_to_dict(plan: AdoptPlanFile) -> dict[str, Any]:
    adoption = plan.adoption
    return {
        "kind": ADOPT_PLAN_KIND,
        "created_at": plan.created_at.isoformat(),
        "source_digest": plan.source_digest,
        "lidarr_digest": plan.lidarr_digest,
        "resolver_version": plan.resolver_version,
        "summary": {
            "claim": len(adoption.claim),
            "keep": len(adoption.keep_as_manual),
            "unmonitor": len(adoption.unmonitor),
        },
        "claim": [_owned_to_dict(r) for r in adoption.claim],
        "keep_as_manual": [_owned_to_dict(r) for r in adoption.keep_as_manual],
        "unmonitor": [{"key": _key_to_dict(u.key), "title": u.title} for u in adoption.unmonitor],
        "held": [
            {"key": _key_to_dict(h.key), "title": h.title, "step": h.step, "reason": h.reason} for h in adoption.held
        ],
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
            adoption=AdoptPlan(
                claim=[_owned_from_dict(r) for r in _items(raw, "claim")],
                keep_as_manual=[_owned_from_dict(r) for r in _items(raw, "keep_as_manual")],
                unmonitor=[
                    UnmonitorRelease(
                        key=_key_from_dict(u["key"]), title=str(u.get("title") or ""), lost_reasons=frozenset()
                    )
                    for u in _items(raw, "unmonitor")
                ],
                held=[
                    HeldRelease(
                        key=_key_from_dict(h["key"]), title=str(h.get("title") or ""), step=str(h.get("step") or "")
                    )
                    for h in _items(raw, "held")
                ],
            ),
        )
    except DiffFileError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DiffFileError(f"this does not look like a likearr adopt plan: {exc}") from exc


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
