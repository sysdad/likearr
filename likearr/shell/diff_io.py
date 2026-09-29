"""`diff.json`: the reviewable plan of record, written by `plan` and read back by `apply`.

The file is a complete, lossless description of a `Diff` plus the two digests that say which
world it was computed against, so `apply` can refuse a plan the world has moved out from under -
and the `[rules]` and `[guards]` it was planned under (`config_fingerprint`), so it can refuse a
plan the user's own configuration has moved out from under too.
It is also meant to be read by a human before they type `--apply`, which is why it carries a
`summary` block of counts and why every enum is written out in full rather than as an index.

Round-tripping matters more than compactness here: `apply` reconstructs the `Diff` from this
file and executes exactly it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from likearr.adapters.state_sqlite import resolution_from_json, resolution_to_json
from likearr.core.diff import is_catalogue_gap, is_recent_catalogue_gap
from likearr.fsio import write_atomic
from likearr.models import (
    RESOLVER_VERSION,
    AddArtist,
    ArtistResolution,
    Diff,
    Guard,
    MonitorRelease,
    NameCollision,
    OwnedRelease,
    Profile,
    ProfileRatchet,
    Reason,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    UnmonitorRelease,
)

__all__ = ["DiffFileError", "diff_from_run_dict", "diff_summary", "diff_to_dict", "read_diff", "write_diff"]


class DiffFileError(Exception):
    """A diff file is missing, unreadable or not a diff likearr wrote."""


# ---------------------------------------------------------------------------- small codecs


def _reason_to_dict(reason: Reason) -> dict[str, Any]:
    return {"kind": reason.kind.value, "source_id": reason.source_id, "playlist_id": reason.playlist_id}


def _reason_from_dict(raw: Mapping[str, Any]) -> Reason:
    return Reason(
        kind=ReasonKind(raw["kind"]),
        source_id=str(raw["source_id"]),
        playlist_id=raw.get("playlist_id"),
    )


def _reasons_to_list(reasons: frozenset[Reason]) -> list[dict[str, Any]]:
    """Sorted by `Reason.key`, so the file is byte-stable for the same plan."""
    return [_reason_to_dict(r) for r in sorted(reasons, key=lambda r: r.key)]


def _reasons_from_list(raw: object) -> frozenset[Reason]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(_reason_from_dict(item) for item in raw if isinstance(item, Mapping))


def _key_to_dict(key: ReleaseKey) -> dict[str, str]:
    return {"artist_mbid": key.artist_mbid, "rg_mbid": key.rg_mbid}


def _key_from_dict(raw: Mapping[str, Any]) -> ReleaseKey:
    return ReleaseKey(artist_mbid=str(raw["artist_mbid"]), rg_mbid=str(raw["rg_mbid"]))


def _resolution_to_dict(resolution: Resolution) -> dict[str, Any]:
    """Reuse the state adapter's public codec, so both files agree on the shape."""
    return json.loads(resolution_to_json(resolution))


def _artist_resolution_to_dict(resolution: ArtistResolution) -> dict[str, Any]:
    return {
        "_kind": "artist",
        "intent_key": resolution.intent_key,
        "status": resolution.status.value,
        "artist_mbid": resolution.artist_mbid,
        "artist_name": resolution.artist_name,
        "step": resolution.step,
        "detail": resolution.detail,
        "resolver_version": resolution.resolver_version,
    }


def _artist_resolution_from_dict(raw: Mapping[str, Any]) -> ArtistResolution:
    return ArtistResolution(
        intent_key=str(raw.get("intent_key") or ""),
        status=ResolutionStatus(raw["status"]),
        artist_mbid=raw.get("artist_mbid"),
        artist_name=str(raw.get("artist_name") or ""),
        step=str(raw.get("step") or ""),
        detail=str(raw.get("detail") or ""),
        resolver_version=int(raw.get("resolver_version") or RESOLVER_VERSION),
    )


def _collision_to_dict(c: NameCollision) -> dict[str, Any]:
    return {
        "name": c.name,
        "wanted_mbid": c.wanted_mbid,
        "wanted_disambiguation": c.wanted_disambiguation,
        "existing_mbid": c.existing_mbid,
        "existing_lidarr_id": c.existing_lidarr_id,
        "existing_name": c.existing_name,
        "existing_disambiguation": c.existing_disambiguation,
        "dropped_releases": c.dropped_releases,
    }


def _collision_from_dict(c: Mapping[str, Any]) -> NameCollision:
    return NameCollision(
        name=str(c.get("name") or ""),
        wanted_mbid=str(c.get("wanted_mbid") or ""),
        existing_mbid=str(c.get("existing_mbid") or ""),
        existing_lidarr_id=int(c.get("existing_lidarr_id") or 0),
        existing_name=str(c.get("existing_name") or ""),
        wanted_disambiguation=str(c.get("wanted_disambiguation") or ""),
        existing_disambiguation=str(c.get("existing_disambiguation") or ""),
        dropped_releases=int(c.get("dropped_releases") or 0),
    )


def _unresolved_to_dict(item: Resolution | ArtistResolution) -> dict[str, Any]:
    if isinstance(item, ArtistResolution):
        return _artist_resolution_to_dict(item)
    return _resolution_to_dict(item)


def _unresolved_from_dict(raw: Mapping[str, Any]) -> Resolution | ArtistResolution:
    if raw.get("_kind") == "artist":
        return _artist_resolution_from_dict(raw)
    return resolution_from_json(json.dumps(raw))


# ---------------------------------------------------------------------------- the document


def diff_summary(diff: Diff) -> dict[str, int]:
    """The counts a human reads first, and the ones the health record reports."""
    return {
        "add_artists": len(diff.add_artists),
        "monitor": len(diff.monitor),
        "unmonitor": len(diff.unmonitor),
        "ratchets": len(diff.ratchets),
        "set_new_items_none": len(diff.set_new_items_none),
        "monitor_artists": len(diff.monitor_artists),
        "refresh_artists": len(diff.refresh_artists),
        "update_reasons": len(diff.update_reasons),
        "guards": len(diff.guards),
        "name_collisions": len(diff.name_collisions),
        "releases_dropped_to_collisions": sum(c.dropped_releases for c in diff.name_collisions),
        "blocked_unmonitors": sum(g.blocked_unmonitors for g in diff.guards),
        "pending": len(diff.pending),
        "unmapped": sum(1 for u in diff.unmapped if not is_catalogue_gap(u)),
        "catalogue_gaps": sum(1 for u in diff.unmapped if is_catalogue_gap(u) and not is_recent_catalogue_gap(u)),
        "catalogue_gaps_recent": sum(1 for u in diff.unmapped if is_recent_catalogue_gap(u)),
        "projected_wanted": diff.projected_wanted,
        "claim": len(diff.claim),
    }


def diff_to_dict(diff: Diff) -> dict[str, Any]:
    """Everything in `Diff`, plus the digests, the timestamp and a `summary` of counts."""
    return {
        "created_at": diff.created_at.isoformat(),
        "source_digest": diff.source_digest,
        "lidarr_digest": diff.lidarr_digest,
        "resolver_version": diff.resolver_version,
        "config_fingerprint": diff.config_fingerprint,
        "summary": diff_summary(diff),
        "add_artists": [
            {"artist_mbid": a.artist_mbid, "name": a.name, "profile": a.profile.value} for a in diff.add_artists
        ],
        "monitor": [
            {
                "key": _key_to_dict(m.key),
                "title": m.title,
                "reasons": _reasons_to_list(m.reasons),
                "step": m.step,
            }
            for m in diff.monitor
        ],
        "unmonitor": [
            {
                "key": _key_to_dict(u.key),
                "title": u.title,
                "lost_reasons": _reasons_to_list(u.lost_reasons),
            }
            for u in diff.unmonitor
        ],
        "ratchets": [
            {
                "artist_mbid": r.artist_mbid,
                "name": r.name,
                "to_profile": r.to_profile.value,
                "because": r.because,
            }
            for r in diff.ratchets
        ],
        "set_new_items_none": list(diff.set_new_items_none),
        "monitor_artists": list(diff.monitor_artists),
        "refresh_artists": list(diff.refresh_artists),
        "accept_shrink": diff.accept_shrink,
        "update_reasons": [
            {"key": _key_to_dict(key), "reasons": _reasons_to_list(reasons)} for key, reasons in diff.update_reasons
        ],
        "guards": [
            {
                "code": g.code,
                "message": g.message,
                "blocked_unmonitors": g.blocked_unmonitors,
                "subject": g.subject,
            }
            for g in diff.guards
        ],
        "name_collisions": [_collision_to_dict(c) for c in diff.name_collisions],
        "pending": [_resolution_to_dict(p) for p in diff.pending],
        "unmapped": [_unresolved_to_dict(u) for u in diff.unmapped],
        "projected_wanted": diff.projected_wanted,
        "claim": [_owned_to_dict(c) for c in diff.claim],
    }


def diff_from_dict(raw: Mapping[str, Any]) -> Diff:
    """Rebuild the `Diff` `apply` will execute. Raises `DiffFileError` on anything unexpected."""
    try:
        return Diff(
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            source_digest=str(raw["source_digest"]),
            lidarr_digest=str(raw["lidarr_digest"]),
            add_artists=[
                AddArtist(
                    artist_mbid=str(a["artist_mbid"]),
                    name=str(a.get("name") or ""),
                    profile=Profile(a["profile"]),
                )
                for a in _items(raw, "add_artists")
            ],
            monitor=[
                MonitorRelease(
                    key=_key_from_dict(m["key"]),
                    title=str(m.get("title") or ""),
                    reasons=_reasons_from_list(m.get("reasons")),
                    step=str(m.get("step") or ""),
                )
                for m in _items(raw, "monitor")
            ],
            unmonitor=[
                UnmonitorRelease(
                    key=_key_from_dict(u["key"]),
                    title=str(u.get("title") or ""),
                    lost_reasons=_reasons_from_list(u.get("lost_reasons")),
                )
                for u in _items(raw, "unmonitor")
            ],
            ratchets=[_ratchet_from_dict(r) for r in _items(raw, "ratchets")],
            set_new_items_none=[str(m) for m in raw.get("set_new_items_none") or []],
            monitor_artists=[str(m) for m in raw.get("monitor_artists") or []],
            refresh_artists=[str(m) for m in raw.get("refresh_artists") or []],
            accept_shrink=bool(raw.get("accept_shrink") or False),
            config_fingerprint=_config_fingerprint_from(raw.get("config_fingerprint")),
            guards=[
                Guard(
                    code=str(g.get("code") or ""),
                    message=str(g.get("message") or ""),
                    blocked_unmonitors=int(g.get("blocked_unmonitors") or 0),
                    subject=str(g.get("subject") or ""),
                )
                for g in _items(raw, "guards")
            ],
            pending=[resolution_from_json(json.dumps(p)) for p in _items(raw, "pending")],
            unmapped=[_unresolved_from_dict(u) for u in _items(raw, "unmapped")],
            projected_wanted=int(raw.get("projected_wanted") or 0),
            resolver_version=int(raw.get("resolver_version") or RESOLVER_VERSION),
            update_reasons=[
                (_key_from_dict(u["key"]), _reasons_from_list(u.get("reasons"))) for u in _items(raw, "update_reasons")
            ],
            name_collisions=[_collision_from_dict(c) for c in _items(raw, "name_collisions")],
            claim=[_owned_from_dict(c) for c in _items(raw, "claim")],
        )
    except DiffFileError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DiffFileError(f"this does not look like a likearr diff file: {exc}") from exc


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


def _items(raw: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise DiffFileError(f"diff file field {key!r} is not a list")
    return [item for item in value if isinstance(item, Mapping)]


def _config_fingerprint_from(raw: object) -> dict[str, dict[str, object]] | None:
    """`config_fingerprint` as written, or ``None`` for a diff that predates it (or wrote null)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or not all(isinstance(v, Mapping) for v in raw.values()):
        raise DiffFileError("diff file field 'config_fingerprint' is not an object of sections")
    return {str(section): dict(values) for section, values in raw.items()}


def _ratchet_from_dict(raw: Mapping[str, Any]) -> ProfileRatchet:
    """Build a `ProfileRatchet` from its JSON form."""
    return ProfileRatchet(
        artist_mbid=str(raw["artist_mbid"]),
        name=str(raw.get("name") or ""),
        to_profile=Profile(raw["to_profile"]),
        because=str(raw.get("because") or ""),
    )


# ---------------------------------------------------------------------------- files


def write_diff(diff: Diff, path: Path, *, existing_albums: Mapping[str, Any] | None = None) -> None:
    """Write `diff.json` (pretty-printed, stable ordering) creating parent directories. 0600:
    a plan names every artist and release it will touch. `existing_albums` is a first check's
    "Albums you already monitor" (`shell.adopt_io.existing_to_dict`), written alongside."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = diff_to_dict(diff)
    if existing_albums is not None:
        raw["existing_albums"] = dict(existing_albums)
    write_atomic(path, json.dumps(raw, indent=2) + "\n", mode=0o600)


def diff_from_run_dict(raw: Mapping[str, Any]) -> Diff:
    """Decode a `runs.diff_json` value into a `Diff`, for the Status page's "What changed" and
    `/runs/<id>`.

    `record_run` (`adapters.state_sqlite`) stores a run's diff with `dataclasses.asdict`, not this
    module's own `diff_to_dict` - a different shape for the same data, close enough to reuse
    `diff_from_dict` on with two fields patched out first:

    - `update_reasons` is stored as `[key, reasons]` pairs there (a tuple, asdict'd positionally),
      not the `{"key": ..., "reasons": ...}` objects `diff_from_dict` expects, so it would always
      decode empty anyway; patching it to `[]` makes that explicit rather than silent. The issue
      this exists for allows the Status page to omit `update_reasons`.
    - `pending` and `unmapped` are dropped before decoding: nothing that reads a run's diff for
      display needs them, and their polymorphic shape (`Resolution` vs `ArtistResolution`) does
      not carry the `_kind` discriminator `diff_to_dict`'s own writer adds, so decoding them here
      would silently misread an `ArtistResolution` as a `Resolution`.

    Raises:
        DiffFileError: `raw` is not a JSON object shaped like a diff.
    """
    if not isinstance(raw, Mapping):
        raise DiffFileError("stored run diff is not a JSON object")
    return diff_from_dict({**raw, "update_reasons": [], "pending": [], "unmapped": [], "claim": []})


def read_diff(path: Path) -> Diff:
    """Read a diff file back.

    Raises:
        DiffFileError: the file is missing, is not JSON, or is not shaped like a diff.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiffFileError(f"no diff file at {path} - run `likearr run` first") from exc
    except OSError as exc:
        raise DiffFileError(f"cannot read the diff file at {path}: {exc}") from exc
    except ValueError as exc:
        raise DiffFileError(f"the diff file at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DiffFileError(f"the diff file at {path} is not a JSON object")
    return diff_from_dict(raw)
