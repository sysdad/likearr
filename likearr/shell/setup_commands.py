"""`likearr doctor` and `likearr setup-profiles`: is this install set up, and set it up.

`doctor` never changes anything at all. `setup-profiles` prints what it would create in Lidarr
and creates it only when the user typed `--apply`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from likearr import __version__, build_info, commit
from likearr.adapters.http import build_client
from likearr.adapters.lidarr import metadata_profile_diff
from likearr.adapters.spotify import authorized_request, describe_wait
from likearr.adapters.state_sqlite import SCHEMA_VERSION
from likearr.config import MUSICBRAINZ_CONTACT_ENV, PLACEHOLDER_CONTACT
from likearr.core.diff import artists_by_normalized_name
from likearr.models import EXIT_ERROR, EXIT_OK, LidarrView, Profile
from likearr.ports import LidarrError, MetadataError, QuotaExceeded, SourceError
from likearr.shell.context import Context
from likearr.shell.output import emit
from likearr.shell.run import tagged_without_state

__all__ = [
    "doctor_command",
    "setup_profiles_command",
]

_MB_CANARY_RG = "b1392450-e666-3926-a536-22c65f834433"
"""Radiohead - OK Computer: a release group that has existed since MusicBrainz did."""


# ---------------------------------------------------------------------------- doctor


@dataclass(slots=True)
class Check:
    """One line of `doctor` output."""

    level: str
    """PASS, WARN, FAIL or SKIP (not checked, because an earlier answer made asking pointless)."""
    name: str
    detail: str

    def line(self) -> str:
        return f"{self.level:<4}  {self.name}: {self.detail}"

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "name": self.name, "detail": self.detail}


def doctor_command(ctx: Context, *, no_spotify: bool = False, as_json: bool = False) -> int:
    """`likearr doctor`: check everything a run depends on, and write nothing anywhere.

    Every check reports PASS, WARN or FAIL and the command keeps going after a failure, because
    the point is one complete picture rather than the first thing that broke. Exit 1 if anything
    FAILed; a WARN (no token yet, profiles not created) is not a failure.

    `as_json` (``--json``) prints one line of JSON instead - ``{"checks": [...], "summary": {...}}``
    - for the web UI's read-only Doctor view, which runs this as a child job and
    renders the same checks a terminal would see.
    """
    checks: list[Check] = []
    checks.append(Check("PASS", "version", f"likearr {__version__}, commit {commit() or 'unknown'}"))
    checks.append(Check("PASS", "config", f"loaded from {ctx.config_path}"))
    if ctx.config.musicbrainz.contact.strip().lower() == PLACEHOLDER_CONTACT:
        # Loads so a first `doctor` still runs, but it identifies nobody.
        checks.append(
            Check(
                "WARN",
                "musicbrainz contact",
                f"{MUSICBRAINZ_CONTACT_ENV} is the old example's {PLACEHOLDER_CONTACT}; set it to your email "
                "or project URL, which MusicBrainz asks for so it can reach you, or unset it to use likearr's own",
            )
        )
    checks.extend(_check_state(ctx))
    checks.extend(_check_lidarr(ctx))
    checks.append(_check_musicbrainz(ctx))
    if no_spotify:
        checks.append(Check("WARN", "spotify", "skipped (--no-spotify)"))
    else:
        checks.extend(_check_spotify(ctx))

    failures = sum(1 for c in checks if c.level == "FAIL")
    warnings = sum(1 for c in checks if c.level == "WARN")
    skipped = sum(1 for c in checks if c.level == "SKIP")
    if as_json:
        emit(
            json.dumps(
                {
                    "version": build_info(),
                    "checks": [c.to_dict() for c in checks],
                    "summary": {
                        "total": len(checks),
                        "failed": failures,
                        "warnings": warnings,
                        "skipped": skipped,
                    },
                }
            )
        )
    else:
        for check in checks:
            emit(check.line())
        emit("")
        skip_note = f", {skipped} skipped" if skipped else ""
        emit(f"{len(checks)} checks, {failures} failed, {warnings} warnings{skip_note}")
    return EXIT_ERROR if failures else EXIT_OK


def _check_state(ctx: Context) -> list[Check]:
    out = [Check("PASS", "state db", f"{ctx.config.state_db} (schema {SCHEMA_VERSION})")]
    last = ctx.state.last_run()
    if last is None:
        out.append(Check("WARN", "last run", "no run recorded yet"))
    else:
        when = datetime.fromtimestamp(last.ts, tz=UTC).isoformat(timespec="seconds")
        out.append(
            Check(
                "PASS",
                "last run",
                f"{when} status={last.status} exit={last.exit_code} "
                f"monitored={last.counts.get('monitored', 0)} unmonitored={last.counts.get('unmonitored', 0)}",
            )
        )
    return out


def _check_lidarr(ctx: Context) -> list[Check]:
    out: list[Check] = []
    try:
        version = ctx.lidarr.check_version()
    except LidarrError as exc:
        return [Check("FAIL", "lidarr", str(exc))]
    out.append(Check("PASS", "lidarr", f"reachable, version {version}"))

    try:
        view = ctx.lidarr.load_view(None)
    except LidarrError as exc:
        return [*out, Check("FAIL", "lidarr read", str(exc))]

    try:
        folders = [str(f.get("path") or "") for f in ctx.lidarr.root_folders()]
    except LidarrError as exc:
        folders = []
        out.append(Check("FAIL", "root folder", str(exc)))
    wanted = ctx.config.lidarr.root_folder
    if not wanted:
        out.append(Check("FAIL", "root folder", _not_chosen("root_folder", folders)))
    elif folders:
        if any(p.rstrip("/") == wanted.rstrip("/") for p in folders):
            out.append(Check("PASS", "root folder", f"{wanted} exists"))
        else:
            out.append(
                Check("FAIL", "root folder", f"{wanted} is not configured in Lidarr (has: {', '.join(folders)})")
            )

    quality = ctx.config.lidarr.quality_profile
    if not quality:
        out.append(Check("FAIL", "quality profile", _not_chosen("quality_profile", sorted(view.quality_profiles))))
    elif quality in view.quality_profiles:
        out.append(Check("PASS", "quality profile", f"{quality!r} -> id {view.quality_profiles[quality]}"))
    else:
        out.append(
            Check(
                "FAIL",
                "quality profile",
                f"{quality!r} does not exist (has: {', '.join(sorted(view.quality_profiles)) or 'none'})",
            )
        )

    for label, name in (
        ("lean profile", ctx.config.lidarr.lean_profile),
        ("full profile", ctx.config.lidarr.full_profile),
    ):
        if name in view.metadata_profiles:
            out.append(Check("PASS", label, f"{name!r} -> id {view.metadata_profiles[name]}"))
        else:
            out.append(Check("WARN", label, f"{name!r} missing (run `likearr setup-profiles --apply`)"))

    tag = ctx.config.lidarr.tag
    if tag in view.tags:
        out.append(Check("PASS", "tag", f"{tag!r} -> id {view.tags[tag]}"))
    else:
        out.append(Check("WARN", "tag", f"{tag!r} does not exist yet; it is created on the first apply"))
    out.append(_check_state_matches_lidarr(ctx, view))
    out.extend(_check_duplicate_artists(view))
    out.extend(_check_unmonitored_artists(ctx, view))
    return out


def _not_chosen(key: str, options: Sequence[str]) -> str:
    """Doctor's line for a `[lidarr]` key a first start leaves unset, with Lidarr's choices."""
    return (
        f"not set: no run plans or applies until it is. Pick one in Settings, under Lidarr setup, or set "
        f"[lidarr] {key} in config.toml (Lidarr has: {', '.join(options) or 'none'})"
    )


LOST_STATE_SHOWN = 10
"""Tagged artists with no state named individually before `doctor` summarises the rest."""


def _check_state_matches_lidarr(ctx: Context, view: LidarrView) -> Check:
    """Lidarr artists carrying likearr's tag that the state database has no record of.

    FAIL when `owned_artists` is empty: likearr tagged artists in this Lidarr and the database
    remembers none of them, which is a lost or replaced database, and from then on nothing likearr
    monitored before is ever unmonitored. WARN when other rows exist, because someone can also add
    the tag by hand. Report only: it reads the artist list `doctor` already has and one table.
    """
    tag = ctx.config.lidarr.tag
    owned_artists = ctx.state.owned_artists()
    lost = sorted(tagged_without_state(view, tag, owned_artists))
    if not lost:
        return Check("PASS", "state matches lidarr", f"every artist tagged {tag!r} has a record in the state database")
    labels = [f"{a.name} ({mbid})" if (a := view.artists.get(mbid)) and a.name else mbid for mbid in lost]
    names = ", ".join(labels[:LOST_STATE_SHOWN])
    if len(labels) > LOST_STATE_SHOWN:
        names += f" and {len(labels) - LOST_STATE_SHOWN} more"
    if not owned_artists:
        return Check(
            "FAIL",
            "state matches lidarr",
            f"{len(lost)} artist(s) in Lidarr carry the {tag!r} tag, but the state database records no artist "
            "at all: it was lost or replaced. Restore it from backup (docs/troubleshooting.md, Restoring from backup); "
            f"until you do, nothing likearr monitored before is ever unmonitored. {names}",
        )
    return Check(
        "WARN",
        "state matches lidarr",
        f"{len(lost)} artist(s) in Lidarr carry the {tag!r} tag with no record in the state database: {names}. "
        "If you lost or replaced the database, restore it from backup; if you added the tag by hand, "
        "remove it from them",
    )


DUPLICATE_ARTISTS_SHOWN = 10
"""Duplicated names named individually before `doctor` summarises the rest."""


def _check_duplicate_artists(view: LidarrView) -> list[Check]:
    """Two Lidarr artists sharing a name, which makes every import of theirs unmatchable.

    Lidarr matches an incoming download to an artist by *name*. When two share one it declines to
    guess - `artistId` and `albumId` come back null with "found multiple artists" - and the queue
    item can never import. Cleanuparr cannot rescue it either: with no album id there is nothing
    to re-search after a blocklist, so it abstains too. Left alone, that strands downloads on
    every protocol, silently, until someone notices.

    FAIL rather than WARN: doctor's WARNs are "not set up yet" (no token, no profiles, no tag),
    and this is a live defect breaking imports right now. It costs nothing - the artist list was
    already read for the checks above - which is the whole point, because this would have caught
    it on the day it happened.
    """
    groups = sorted(
        ((name, artists) for name, artists in artists_by_normalized_name(view).items() if len(artists) > 1),
        key=lambda pair: pair[0],
    )
    if not groups:
        return [Check("PASS", "duplicate artists", f"{len(view.artists)} artists, every name unique")]

    out: list[Check] = []
    for _name, artists in groups[:DUPLICATE_ARTISTS_SHOWN]:
        where = ", ".join(f"id {a.id} ({a.mbid})" for a in artists)
        out.append(
            Check(
                "FAIL",
                "duplicate artists",
                f"{artists[0].name!r} exists {len(artists)} times - {where}. Lidarr cannot match an "
                "import by name while both exist; remove the one you do not want (the row, never "
                "the files)",
            )
        )
    if len(groups) > DUPLICATE_ARTISTS_SHOWN:
        out.append(
            Check("FAIL", "duplicate artists", f"and {len(groups) - DUPLICATE_ARTISTS_SHOWN} more duplicated name(s)")
        )
    return out


UNMONITORED_ARTISTS_SHOWN = 10
"""Unmonitored artists named individually before `doctor` summarises the rest."""


def _check_unmonitored_artists(ctx: Context, view: LidarrView) -> list[Check]:
    """An artist Lidarr holds unmonitored while likearr has a release monitored under them.

    Lidarr never searches, and never lists as wanted, an album whose artist is unmonitored, so
    those releases are invisible to RSS sync, the backlog search and soularr however their own flag
    reads. A library can hold many artists in this state before anyone notices, because
    nothing errors. `run` re-monitors them every time; this is the check that says it is needed.

    FAIL, for the reason a duplicate name is: a live defect, not a "not set up yet". It reads only
    what `doctor` already has - the artist list and the ownership table - so it costs nothing.
    """
    holding = {key.artist_mbid for key in ctx.state.owned_releases()}
    broken = sorted(
        (a for mbid in holding if (a := view.artists.get(mbid)) is not None and not a.monitored),
        key=lambda a: a.name.casefold(),
    )
    if not broken:
        return [Check("PASS", "unmonitored artists", "every artist holding a monitored release is monitored")]

    names = ", ".join(f"{a.name!r} (id {a.id})" for a in broken[:UNMONITORED_ARTISTS_SHOWN])
    more = f" and {len(broken) - UNMONITORED_ARTISTS_SHOWN} more" if len(broken) > UNMONITORED_ARTISTS_SHOWN else ""
    return [
        Check(
            "FAIL",
            "unmonitored artists",
            f"{len(broken)} artist(s) are unmonitored in Lidarr while holding releases likearr monitored, so "
            f"those are never searched: {names}{more}. `likearr run --apply` re-monitors them",
        )
    ]


def _check_musicbrainz(ctx: Context) -> Check:
    """One cheap, rate-limited GET of a release group that has always existed.

    The lookup is the composite one a real run uses, so a MusicBrainz outage can be answered by
    Lidarr's own metadata proxy instead. That is a PASS for the run and a WARN for the operator,
    and the two are reported differently rather than blurred into "it worked".
    """
    try:
        found = ctx.lookup.search_release_group("Radiohead", "OK Computer")
    except MetadataError as exc:
        return Check("FAIL", "musicbrainz", str(exc))
    if ctx.composite is not None and not ctx.composite.mb_ok:
        return Check("WARN", "musicbrainz", "MusicBrainz failed; Lidarr's metadata proxy answered the canary instead")
    if found is None:
        return Check("WARN", "musicbrainz", "reachable but the canary lookup found nothing")
    if found.mbid != _MB_CANARY_RG:
        return Check("WARN", "musicbrainz", f"canary resolved to {found.mbid}, expected {_MB_CANARY_RG}")
    return Check("PASS", "musicbrainz", f"canary {found.artist_name} - {found.title} resolved")


def _check_spotify(ctx: Context) -> list[Check]:
    """Token file, refresh, and one page from each enabled source. Never paginates.

    A ``QUOTA_EXCEEDED`` answer stops the Spotify checks where they are: every page after it would
    spend more of a quota that is already gone, so the rest are reported as SKIP, not requested.
    """
    if ctx.auth is None or ctx.source is None:
        return [Check("WARN", "spotify", ctx.spotify_error or "not configured")]
    token_file = ctx.config.spotify.token_file
    if not token_file.exists():
        return [
            Check(
                "WARN", "spotify", f"no token file at {token_file}; Connect Spotify in Settings (or run `likearr auth`)"
            )
        ]

    canaries = _spotify_canaries(ctx)
    out: list[Check] = []
    try:
        tokens = ctx.auth.refresh()
    except QuotaExceeded as exc:
        return [_quota_check("token refresh", exc, skipping=bool(canaries)), *_skipped(n for n, *_ in canaries)]
    except SourceError as exc:
        return [Check("FAIL", "spotify token", str(exc))]
    expires = datetime.fromtimestamp(tokens.expires_at, tz=UTC).isoformat(timespec="seconds")
    out.append(Check("PASS", "spotify token", f"refresh works, scopes {tokens.scope or '(none)'}, expires {expires}"))

    with build_client() as client:
        for index, (name, url, params, block) in enumerate(canaries):
            try:
                page = authorized_request(client, ctx.auth, "GET", url, params=params, context=name)
            except QuotaExceeded as exc:
                later = [n for n, *_ in canaries[index + 1 :]]
                out.append(_quota_check(name, exc, skipping=bool(later)))
                out.extend(_skipped(later))
                break
            except SourceError as exc:
                out.append(Check("FAIL", f"spotify {name}", str(exc)))
                continue
            body = page.get(block) if block else page
            if not isinstance(body, dict):
                out.append(Check("FAIL", f"spotify {name}", f"response has no {block!r} object"))
                continue
            items = body.get("items")
            if not isinstance(items, list):
                out.append(Check("FAIL", f"spotify {name}", "'items' is missing or not a list"))
                continue
            total = body.get("total")
            shown = f"{total} total" if isinstance(total, int) else f"{len(items)} on the first page"
            out.append(Check("PASS", f"spotify {name}", f"{shown}"))
    return out


def _quota_check(where: str, exc: QuotaExceeded, *, skipping: bool) -> Check:
    """The one FAIL line for an exhausted quota: what it means for a run, and when it may clear.

    `where` is the request that was made; a 401 on it refreshes the token first, so the quota
    answer may have come from the token endpoint instead, and then the line says so.
    """
    if exc.token_request:
        where = "token refresh"
    wait = (
        "Spotify sent no Retry-After" if exc.retry_after is None else f"Retry-After: {describe_wait(exc.retry_after)}"
    )
    rest = " The remaining Spotify checks are skipped so doctor spends no more of it" if skipping else ""
    return Check(
        "FAIL",
        "spotify quota",
        f"Spotify answered the {where} request with 429 QUOTA_EXCEEDED: the developer account's quota is spent "
        f"({wait}). Until it recovers every `likearr run` aborts with zero unmonitors.{rest}",
    )


def _skipped(names: Iterable[str]) -> list[Check]:
    return [Check("SKIP", f"spotify {name}", "not requested: the quota is exhausted") for name in names]


def _spotify_canaries(ctx: Context) -> list[tuple[str, str, dict[str, Any], str]]:
    """The one page per enabled source that `doctor` reads. Deliberately never paginates."""
    from likearr.adapters.spotify import API_BASE

    out: list[tuple[str, str, dict[str, Any], str]] = []
    cfg = ctx.config.spotify
    if cfg.followed_artists:
        out.append(("followed artists", f"{API_BASE}/me/following", {"type": "artist", "limit": 1}, "artists"))
    if cfg.saved_albums:
        out.append(("saved albums", f"{API_BASE}/me/albums", {"limit": 1}, ""))
    if cfg.liked_tracks:
        out.append(("liked tracks", f"{API_BASE}/me/tracks", {"limit": 1}, ""))
    for playlist_id in cfg.playlists:
        out.append((f"playlist {playlist_id}", f"{API_BASE}/playlists/{playlist_id}/items", {"limit": 1}, ""))
    return out


# ---------------------------------------------------------------------------- setup-profiles


@dataclass(slots=True)
class SetupProfilesPlan:
    """What `setup-profiles` sees and would do, built once and shared by the text, `--json` and
    web UI renderings, so there is exactly one place that decides what is missing,
    what already matches, and what exists but differs.
    """

    profiles: list[dict[str, Any]]
    """Each: name, kind ('lean'/'full'), status ('ok'/'missing'/'differs'), id (when it exists),
    `applies` (bool: will `--apply` write anything here), and - only when status is 'differs' -
    `diff` (`adapters.lidarr.metadata_profile_diff`).

    `status` and `applies` are independent on purpose: a metadata profile's 'differs' always has
    `applies = False` (`ensure_metadata_profile` reuses an existing profile by name as-is and never
    edits it), while a root folder's 'differs' has `applies = True` (`set_root_folder_defaults`
    really does overwrite its monitor defaults). Reading `status` alone would suggest they behave
    the same way; they do not, and `applies` is the field that says which.
    """
    tag: dict[str, Any]
    """name, status ('ok'/'missing'), id (when it exists), applies."""
    root_folder: dict[str, Any]
    """path, status ('ok'/'missing'/'differs'), applies, and the current defaults when it exists."""
    todo: list[str]
    """What `--apply` would create or change - the same lines the text output has always shown.
    Never includes a 'differs' profile (see `profiles`'s docstring): applying would not change it."""
    root_folders: list[str] = field(default_factory=list)
    """Every root folder Lidarr has, for Settings to pick `[lidarr] root_folder` from."""
    quality_profiles: list[str] = field(default_factory=list)
    """Every quality profile Lidarr has, for Settings to pick `[lidarr] quality_profile` from."""

    @property
    def needs_apply(self) -> bool:
        return bool(self.todo)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": self.profiles,
            "tag": self.tag,
            "root_folder": self.root_folder,
            "todo": self.todo,
            "needs_apply": self.needs_apply,
            "root_folders": self.root_folders,
            "quality_profiles": self.quality_profiles,
        }


def _build_setup_profiles_plan(ctx: Context) -> SetupProfilesPlan:
    config = ctx.config
    view = ctx.lidarr.load_view(None)
    details = {str(d.get("name") or ""): d for d in ctx.lidarr.metadata_profile_details()}

    todo: list[str] = []
    profiles: list[dict[str, Any]] = []
    for profile, name in ((Profile.LEAN, config.lidarr.lean_profile), (Profile.FULL, config.lidarr.full_profile)):
        entry: dict[str, Any] = {"name": name, "kind": profile.value}
        if name not in view.metadata_profiles:
            entry["status"] = "missing"
            entry["applies"] = True
            todo.append(f"create metadata profile {name!r} ({profile.value})")
        else:
            entry["id"] = view.metadata_profiles[name]
            raw = details.get(name)
            diff = metadata_profile_diff(raw, profile) if raw is not None else None
            if diff is None:
                entry["status"] = "ok"
                entry["applies"] = False
            else:
                entry["status"] = "differs"
                entry["applies"] = False  # ensure_metadata_profile keeps an existing profile as-is
                entry["diff"] = diff
        profiles.append(entry)

    tag: dict[str, Any] = {"name": config.lidarr.tag}
    if config.lidarr.tag in view.tags:
        tag["status"] = "ok"
        tag["applies"] = False
        tag["id"] = view.tags[config.lidarr.tag]
    else:
        tag["status"] = "missing"
        tag["applies"] = True
        todo.append(f"create tag {config.lidarr.tag!r}")

    root = config.lidarr.root_folder
    folders = ctx.lidarr.root_folders()
    folder = next((f for f in folders if str(f.get("path") or "").rstrip("/") == root.rstrip("/")), None)
    root_folder: dict[str, Any] = {"path": root}
    if not root:
        # Not chosen yet: nothing to create or change until Settings picks one.
        root_folder["status"] = "unset"
        root_folder["applies"] = False
    elif folder is None:
        root_folder["status"] = "missing"
        root_folder["applies"] = True
        todo.append(f"create root folder {root!r} (monitor none / new items none)")
    elif str(folder.get("defaultMonitorOption") or "") != "none" or str(
        folder.get("defaultNewItemMonitorOption") or ""
    ) not in ("none", ""):
        root_folder["status"] = "differs"
        root_folder["applies"] = True  # set_root_folder_defaults DOES overwrite these, unlike a profile
        root_folder["current"] = {
            "defaultMonitorOption": folder.get("defaultMonitorOption"),
            "defaultNewItemMonitorOption": folder.get("defaultNewItemMonitorOption"),
        }
        todo.append(f"set root folder {root!r} defaults to monitor none / new items none")
    else:
        root_folder["status"] = "ok"
        root_folder["applies"] = False

    return SetupProfilesPlan(
        profiles=profiles,
        tag=tag,
        root_folder=root_folder,
        todo=todo,
        root_folders=[str(f.get("path") or "") for f in folders if f.get("path")],
        quality_profiles=sorted(view.quality_profiles),
    )


def _emit_setup_profiles_plan(plan: SetupProfilesPlan) -> None:
    for entry in plan.profiles:
        if entry["status"] == "ok":
            emit(f"ok    metadata profile {entry['name']!r} exists (id {entry['id']})")
        elif entry["status"] == "differs":
            diff = entry["diff"]
            emit(
                f"warn  metadata profile {entry['name']!r} exists (id {entry['id']}) but its allowed album types "
                f"differ from what likearr would create - primary: has {diff['actual_primary']}, likearr wants "
                f"{diff['expected_primary']}; secondary: has {diff['actual_secondary']}, likearr wants "
                f"{diff['expected_secondary']}. setup-profiles never edits an existing profile, so this one is "
                "kept as it is; rename or fix it by hand in Lidarr if that is not what you want"
            )
    if plan.tag["status"] == "ok":
        emit(f"ok    tag {plan.tag['name']!r} exists (id {plan.tag['id']})")
    if plan.root_folder["status"] == "ok":
        emit(f"ok    root folder {plan.root_folder['path']!r} already defaults to monitor none")
    elif plan.root_folder["status"] == "unset":
        emit(f"warn  root folder: {_not_chosen('root_folder', plan.root_folders)}")


def setup_profiles_command(ctx: Context, *, do_apply: bool = False, as_json: bool = False) -> int:
    """`likearr setup-profiles`: create the Lean/Full profiles, the tag, and safe root defaults.

    Dry-run by default: it prints what is missing and changes nothing. `--apply` creates it.
    Everything it does is idempotent, so applying twice is a no-op. An existing profile of the
    same name is always reused as-is, even if its allowed album types differ from what likearr
    would create - `--apply` never edits one - so the preview surfaces that difference instead of
    silently hiding it.

    `as_json` (``--json``) prints the plan as one line of JSON instead of the dry-run text, for
    the web UI's Lidarr setup panel, which previews with this and applies by spawning
    `setup-profiles --apply` as a job.
    """
    try:
        plan = _build_setup_profiles_plan(ctx)
    except LidarrError as exc:
        if as_json:
            emit(json.dumps({"error": str(exc)}))
        else:
            emit(f"FAIL  lidarr: {exc}")
        return EXIT_ERROR

    if as_json:
        emit(json.dumps(plan.to_dict()))
        if not do_apply:
            return EXIT_OK
    else:
        _emit_setup_profiles_plan(plan)
        if not plan.todo:
            emit("nothing to do")
            return EXIT_OK
        for item in plan.todo:
            emit(f"{'apply' if do_apply else 'would'} {item}")
        if not do_apply:
            emit("")
            emit("re-run with --apply to make these changes")
            return EXIT_OK

    if not plan.todo:
        return EXIT_OK

    config = ctx.config
    try:
        for profile, name in ((Profile.LEAN, config.lidarr.lean_profile), (Profile.FULL, config.lidarr.full_profile)):
            profile_id = ctx.lidarr.ensure_metadata_profile(profile, name)
            emit(f"ok    metadata profile {name!r} -> id {profile_id}")
        tag_id = ctx.lidarr.ensure_tag(config.lidarr.tag)
        emit(f"ok    tag {config.lidarr.tag!r} -> id {tag_id}")
        root = config.lidarr.root_folder
        folder = next(
            (f for f in ctx.lidarr.root_folders() if str(f.get("path") or "").rstrip("/") == root.rstrip("/")), None
        )
        # No root folder chosen yet: `plan.root_folder` said so, and nothing is done to one.
        if root and folder is None:
            ctx.lidarr.add_root_folder(root)
            emit(f"ok    root folder {root!r} created")
        elif root:
            ctx.lidarr.set_root_folder_defaults(root, "none", "none")
            emit(f"ok    root folder {root!r} defaults set to none/none")
    except LidarrError as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR
    return EXIT_OK
