"""Editing `config.toml` from a browser: an allowlist, a comment-preserving round trip, and no surprises.

The live `config.toml` is the source of truth (not any copy kept elsewhere, which is
documentation only), so a browser save has to be as careful as a hand edit:

0. **The file must not have moved under the form.** The page records the file's sha256 when it
   renders; a save whose hash no longer matches writes nothing. The form posts every allowlisted
   value, so without this it would silently revert a hand edit made while the page was open.
1. **`tomlkit`, not `tomllib` plus a writer**, so the heavily commented file keeps its comments.
2. **Only allowlisted keys are set, and only the ones whose value changed.** A key the file leaves
   to its default stays implicit, so a later change to the default still reaches it.
3. **`parse_config` is the validator** - the same code a run loads the file with - so a value the
   browser can save is a value a run can load, and its messages (a mistyped MBID in
   ``deny_releases``, say) are the ones a human already reads. An error writes nothing.
4. **A backup first**, ``config.toml.bak-YYYYMMDD-HHMMSS``, keeping the newest `BACKUP_KEEP`.
   Hand-made backups carry a tag (``.bak-20250101-before-upgrade``), never match the pattern, and
   are never pruned.
5. **An atomic replace**, so a crash mid-write cannot leave the next cron fire a truncated file.

**The allowlist governs reading as well as writing.** The page renders these keys and nothing
else: no path, no URL, no MusicBrainz contact, no ``[health]`` block, and never an environment
value. Secrets never live in the TOML at all (see `likearr.config`), but the page does not rely
on that. The one exception is `LIBRARY_KEYS`: `[lidarr] root_folder` and `quality_profile`
are picked from Lidarr's own lists, not typed, so they have their own small form and
`plan_library`, and go through the same `write_config` as everything else.

Three kinds of change get a second confirm (`SaveCheck.confirm`): one that re-resolves liked and
playlist tracks on the next run; one that loosens a guard, because a loosened guard applies
unattended at the next cron fire; and one that switches a Spotify source on or adds a playlist,
because the next cron fire monitors all of it with no cap. Tightening a guard, switching a source
off, and anything else, saves at once.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomlkit
import tomlkit.items

from likearr.config import Config, ConfigError, parse_config, validate_cron_and_timezone
from likearr.core.cron import CronError, CronExpr, next_fire_from, parse_cron
from likearr.fsio import write_atomic
from likearr.models import LIKED_TRACK_SCOPES

__all__ = [
    "BACKUP_KEEP",
    "FIELDS",
    "FIELD_BY_NAME",
    "LIBRARY_KEYS",
    "PAUSED_REASON_LIMIT",
    "SCHEDULE_PREVIEW_COUNT",
    "Change",
    "ChangeView",
    "Field",
    "SaveCheck",
    "SaveConflict",
    "SchedulePreview",
    "current_values",
    "describe_changes",
    "file_hash",
    "parse_form",
    "plan_library",
    "plan_pause",
    "plan_resume",
    "plan_save",
    "plan_schedule",
    "preview_schedule",
    "write_config",
]

SCHEDULE_PREVIEW_COUNT = 5
"""How many upcoming fires the Settings page's schedule preview shows."""

PAUSED_REASON_LIMIT = 200
"""Matches `config._schedule`'s own limit on `[schedule] paused_reason`."""

BACKUP_KEEP = 30

_NUMBER_LIMIT = 1_000_000_000
"""No setting means anything past a billion. Checked before anything formats the number: a
400-digit integer overflows `float` in the confirm text and would otherwise be a 500."""

_BACKUP_NAME = re.compile(r"^(?P<stem>.+)\.bak-\d{8}-\d{6}(?:-\d+)?$")

Value = str | int | float | bool | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Field:
    section: str
    key: str
    kind: str
    """``str``, ``choice``, ``int``, ``float``, ``bool`` or ``list``."""
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()
    choice_labels: Mapping[str, str] = field(default_factory=dict)
    """``kind == "choice"`` only: a sentence per choice, shown as the ``<option>`` text. The stored
    value (``choices``) is unchanged - this only replaces what the dropdown shows for it."""

    @property
    def name(self) -> str:
        """The form field name: ``section.key``."""
        return f"{self.section}.{self.key}"


FIELDS: tuple[Field, ...] = (
    Field("spotify", "followed_artists", "bool", "Followed artists", "Mirror the artists you follow."),
    Field("spotify", "saved_albums", "bool", "Saved albums", "Mirror the albums in your library."),
    Field("spotify", "liked_tracks", "bool", "Liked songs", "Mirror the albums your liked songs live on."),
    Field(
        "spotify", "playlists", "list", "Playlists", "Playlists you own or collaborate on whose songs count as liked."
    ),
    Field(
        "rules",
        "liked_track_scope",
        "choice",
        "Liked song resolves to",
        "Which release a liked or playlist song resolves to.",
        choices=tuple(sorted(LIKED_TRACK_SCOPES)),
        choice_labels={
            "album": "The studio album or EP the song is on",
            "smallest": "The smallest release that has the song",
        },
    ),
    Field("rules", "singles_fallback_days", "int", "Singles fallback (days)", "Monitor a lone single after this long."),
    Field(
        "rules",
        "recent_release_days",
        "int",
        "Recent release window (days)",
        "When a followed artist has a release this new that Lidarr doesn't list yet, likearr asks Lidarr to "
        "refresh the artist. Older missing releases are only reported.",
    ),
    Field("rules", "albums_only_tag", "str", "Albums-only tag", "Lidarr tag for a followed artist's albums only."),
    Field(
        "rules",
        "allow_compilation_fallback",
        "bool",
        "Allow compilations",
        "Monitor a compilation when it is a song's only home.",
    ),
    Field(
        "rules",
        "allow_remix_releases",
        "bool",
        "Allow remix releases",
        "Choose a remix release for a song, even when another release has it.",
    ),
    Field(
        "rules",
        "keep_remix_only_tracks",
        "bool",
        "Keep remix-only songs",
        "With remix releases off, still monitor a remix when it is a song's only release.",
    ),
    Field(
        "rules",
        "deny_releases",
        "list",
        "Refused releases",
        "Albums liked and playlist songs never resolve to, and followed artists' catalogues leave out, one "
        "MusicBrainz id per line. A saved album still wins. Not this one adds to this list.",
    ),
    Field(
        "guards",
        "max_unmonitors_scheduled",
        "int",
        "Max unmonitors per scheduled run",
        "A scheduled run applies no unmonitors at all when there would be more than this many; run likearr by "
        "hand to review and apply them. Hand runs have no cap.",
    ),
    Field(
        "guards",
        "source_shrink_pct",
        "float",
        "Source shrink guard (%)",
        "If a Spotify source's item count drops by more than this percent since the last run, its unmonitors "
        "are held back until it recovers or you let the shrink through while reviewing a plan.",
    ),
    Field(
        "guards",
        "artist_shrink_pct",
        "float",
        "Artist shrink guard (%)",
        "Same idea per followed artist: unmonitors are held back if their album and EP count drops by more "
        "than this percent since the last run.",
    ),
    Field(
        "guards",
        "unmapped_ratio_amber",
        "float",
        "Unmapped share before Status needs attention (0-1)",
        "Status needs attention when more than this share of songs and albums newly fails to map, compared "
        "with the last run. It blocks nothing.",
    ),
    Field(
        "guards",
        "projected_wanted_max",
        "int",
        "Projected wanted warning",
        "Warn when a run would leave more than this many releases monitored with no files yet, Lidarr's "
        "wanted list as projected. It blocks nothing.",
    ),
)

FIELD_BY_NAME = {f.name: f for f in FIELDS}

_RE_RESOLVE = frozenset(
    {
        ("rules", "liked_track_scope"),
        ("rules", "allow_compilation_fallback"),
        ("rules", "allow_remix_releases"),
        ("rules", "keep_remix_only_tracks"),
    }
)
"""The rules the health fingerprint carries (`liked_track_scope` and `ExclusionRules.token`):
changing one re-resolves every liked and playlist song and re-baselines the health comparison.
`deny_releases` is deliberately outside the token and re-resolves far less; see `_confirmations`."""

_SOURCES = frozenset({("spotify", "followed_artists"), ("spotify", "saved_albums"), ("spotify", "liked_tracks")})

_SOURCE_WARNING = (
    "the next scheduled run will monitor everything this resolves to, with no cap - check for changes first "
    "(Review changes)"
)
"""Why a new source asks first: monitors are not capped the way unmonitors are
(`max_unmonitors_scheduled`), and `projected_wanted_max` only warns, so a source switched on or a
playlist added is acted on in full by the next unattended run."""

_GUARD_EFFECT = {
    "max_unmonitors_scheduled": "unmonitor up to {new} releases in one unattended run",
    "source_shrink_pct": "go ahead with unmonitors after a Spotify source shrinks by up to {new}%",
    "artist_shrink_pct": "unmonitor a followed artist's releases after their catalogue shrinks by up to {new}%",
    "unmapped_ratio_amber": "leave up to {new:.0%} of songs and albums unmapped before Status shows needs attention",
    "projected_wanted_max": "leave up to {new} releases wanted with no files before warning",
}
"""Every guard is looser when its number goes up. What the next scheduled run may then do."""


class SaveConflict(Exception):
    """`config.toml` changed since the form was rendered. Nothing was written."""


@dataclass(frozen=True, slots=True)
class Change:
    section: str
    key: str
    old: Value
    new: Value


_SCHEDULE_LABELS = {"cron": "Schedule (cron)", "timezone": "Schedule timezone", "enabled": "Scheduled runs"}
"""Readable labels for the `[schedule]` keys a `Change` can name - these have no `Field` entry
(the schedule fieldset isn't part of the allowlisted form `FIELDS` walks)."""


@dataclass(frozen=True, slots=True)
class ChangeView:
    """One readable row for the confirm page (`settings_confirm.html`): the field's label and a
    plain-English summary lead, with the raw `section.key` and the old/new values (IDs included)
    folded into a collapsed `<details>` underneath - see `describe_changes`."""

    label: str
    summary: str
    section: str
    key: str
    old_raw: str
    new_raw: str


def _label(change: Change) -> str:
    f = FIELD_BY_NAME.get(f"{change.section}.{change.key}")
    if f:
        return f.label
    if change.section == "schedule":
        return _SCHEDULE_LABELS.get(change.key, f"{change.section}.{change.key}")
    return f"{change.section}.{change.key}"


def _raw(value: Value) -> str:
    if isinstance(value, tuple):
        return ", ".join(value) if value else "(none)"
    return str(value)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def describe_changes(changes: Sequence[Change], playlist_names: Mapping[str, str]) -> list[ChangeView]:
    """One `ChangeView` per change, leading with what it means rather than what it is:

    - a bool reads "on" or "off";
    - `spotify.playlists` reads "added: <names> / removed: <names>", not the whole before/after
      list, naming each playlist from `playlist_names` where it is known, else by its id;
    - `rules.deny_releases` reads a count ("1 release added to the refused list, 2 removed"), not
      the MBIDs - there is no cheap name for a release group here, and an MBID is not readable;
    - anything else reads "<old> -> <new>", which for a string or a number is already readable.

    The raw section/key and old/new values (`old_raw`/`new_raw`, IDs and MBIDs included) are
    always kept too, for the confirm page's collapsed ``<details>``.
    """
    rows: list[ChangeView] = []
    for c in changes:
        if (c.section, c.key) == ("spotify", "playlists") and isinstance(c.old, tuple) and isinstance(c.new, tuple):
            added, removed = _added_removed(c.old, c.new)
            parts = []
            if added:
                parts.append("added: " + ", ".join(playlist_names.get(pid, pid) for pid in added))
            if removed:
                parts.append("removed: " + ", ".join(playlist_names.get(pid, pid) for pid in removed))
            summary = " / ".join(parts) if parts else "no change"
        elif (c.section, c.key) == ("rules", "deny_releases") and isinstance(c.old, tuple) and isinstance(c.new, tuple):
            added, removed = _added_removed(c.old, c.new)
            parts = []
            if added:
                parts.append(f"{_plural(len(added), 'release')} added to the refused list")
            if removed:
                parts.append(f"{len(removed)} removed")
            summary = ", ".join(parts) if parts else "no change"
        elif isinstance(c.old, bool) and isinstance(c.new, bool):
            summary = f"{'on' if c.old else 'off'} -> {'on' if c.new else 'off'}"
        else:
            summary = f"{_raw(c.old)} -> {_raw(c.new)}"
        rows.append(ChangeView(_label(c), summary, c.section, c.key, _raw(c.old), _raw(c.new)))
    return rows


@dataclass(slots=True)
class SaveCheck:
    new_text: str
    changes: list[Change] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    """Form field name (or ``""`` for the whole file) to the message. Non-empty: write nothing."""
    confirm: list[str] = field(default_factory=list)
    """One sentence per reason this save needs a second confirm."""


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_values(config: Config) -> dict[tuple[str, str], Value]:
    """The effective value of every allowlisted key, defaults included. Nothing else is read."""
    sections: dict[str, Any] = {"rules": config.rules, "guards": config.guards, "spotify": config.spotify}
    return {(f.section, f.key): getattr(sections[f.section], f.key) for f in FIELDS}


def parse_form(form: Mapping[str, Sequence[str]]) -> tuple[dict[tuple[str, str], Value], dict[str, str]]:
    """Turn posted form data into typed values for the allowlisted keys, and per-field errors.

    Every other posted name is ignored, so a crafted form cannot reach a key the page does not
    render. Range checks are left to `parse_config`, which is the one place they live.
    A number must still be finite and within `_NUMBER_LIMIT` here, before it is written: ``float()``
    takes "nan" and "inf", and a NaN guard compares false with everything, so it would slip past the
    loosening confirm. `parse_config` refuses both too, so neither can reach the file either way.
    """
    values: dict[tuple[str, str], Value] = {}
    errors: dict[str, str] = {}
    for f in FIELDS:
        raw = list(form.get(f.name, []))
        first = raw[0].strip() if raw else ""
        try:
            if f.kind == "bool":
                values[(f.section, f.key)] = bool(raw) and first not in {"", "off", "false"}
            elif f.kind in ("int", "float"):
                number = int(first) if f.kind == "int" else float(first)
                if abs(number) > _NUMBER_LIMIT or not math.isfinite(number):  # abs first: a huge int overflows isfinite
                    errors[f.name] = "is out of range"
                    continue
                values[(f.section, f.key)] = number
            elif f.kind == "list" and f.key == "playlists":
                values[(f.section, f.key)] = tuple(dict.fromkeys(v.strip() for v in raw if v.strip()))
            elif f.kind == "list":
                # One per line in the textarea; any whitespace or comma separates, so the value also
                # survives the confirm page's hidden field. An MBID contains neither.
                # MBIDs are written lowercase, as `parse_config` reads them.
                values[(f.section, f.key)] = tuple(v.lower() for v in re.split(r"[\s,]+", " ".join(raw)) if v)
            else:
                values[(f.section, f.key)] = first
        except ValueError:
            errors[f.name] = "must be a whole number" if f.kind == "int" else "must be a finite number"
    return values, errors


def _key(field: tuple[str, str], item: str) -> str:
    """How two list entries are compared: MBIDs case-insensitively, as `parse_config` reads them."""
    return item.strip().lower() if field == ("rules", "deny_releases") else item.strip()


def _merged(field: tuple[str, str], old: Sequence[str], new: Sequence[str]) -> tuple[str, ...]:
    """`new` as a set, in the file's own order: kept entries where they were, additions at the end.

    Order carries no meaning in either list, and the picker lists playlists by name rather than
    in file order, so comparing sequences would turn an untouched form into a rewrite.
    """
    wanted = {_key(field, v) for v in new}
    kept = [v for v in old if _key(field, v) in wanted]
    have = {_key(field, v) for v in kept}
    return tuple(kept) + tuple(v for v in new if _key(field, v) not in have)


def _changed(field: tuple[str, str], old: Value, new: Value) -> bool:
    if isinstance(old, tuple) and isinstance(new, tuple):
        return {_key(field, v) for v in old} != {_key(field, v) for v in new}
    return new != old


def _set_value(table: Any, field: tuple[str, str], new: Value) -> None:
    """Set one key, editing an existing array in place so the comments inside it survive.

    Assigning a fresh list would replace the whole array, and every per-entry annotation with it.
    """
    key = field[1]
    current = table.get(key)
    if isinstance(new, tuple) and isinstance(current, tomlkit.items.Array):
        wanted = {_key(field, v) for v in new}
        for index in reversed(range(len(current))):
            if _key(field, str(current[index])) not in wanted:
                del current[index]
        present = {_key(field, str(v)) for v in current}
        for value in new:
            if _key(field, value) not in present:
                current.append(value)
        return
    table[key] = list(new) if isinstance(new, tuple) else new


def plan_save(
    text: str,
    values: Mapping[tuple[str, str], Value],
    *,
    base_dir: Path,
    playlist_names: Mapping[str, str] | None = None,
) -> SaveCheck:
    """Apply `values` to `text` and validate the result, writing nothing.

    Returns the new file text, what changed, any errors, and the reasons for a second confirm,
    which name an added playlist by `playlist_names` where it is known.
    """
    old = current_values(parse_config(tomllib.loads(text), base_dir=base_dir))
    changes: list[Change] = []
    for setting, new in values.items():
        if setting not in old or not _changed(setting, old[setting], new):
            continue
        before = old[setting]
        after = _merged(setting, before, new) if isinstance(before, tuple) and isinstance(new, tuple) else new
        changes.append(Change(setting[0], setting[1], before, after))
    if not changes:
        return SaveCheck(new_text=text)

    doc = tomlkit.parse(text)
    for change in changes:
        if change.section not in doc:
            doc.add(change.section, tomlkit.table())
        _set_value(doc[change.section], (change.section, change.key), change.new)
    new_text = tomlkit.dumps(doc)

    check = SaveCheck(new_text=new_text, changes=changes)
    try:
        parse_config(tomllib.loads(new_text), base_dir=base_dir)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors[_field_of(str(exc))] = str(exc)
        return check
    check.confirm = _confirmations(changes, playlist_names or {})
    return check


def _field_of(message: str) -> str:
    """The form field a `ConfigError` names (``[rules] deny_releases ...``), or ``""``."""
    match = re.match(r"\[(\w+)\] (\w+)", message)
    if match and f"{match[1]}.{match[2]}" in FIELD_BY_NAME:
        return f"{match[1]}.{match[2]}"
    return ""


def _added_removed(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """The entries of a list setting that `new` adds to `old`, and the ones it drops, in order."""
    return [v for v in new if v not in old], [v for v in old if v not in new]


def _deny_moves(change: Change) -> str:
    """Which songs a `deny_releases` change re-resolves, per direction.

    An added entry moves the songs whose answer is that release; a removed one moves the songs the
    resolver kept off it (`Resolution.denied_skipped`), which can go back to it now. `plan_save`
    makes a `Change` only when the entries differ, so at least one side is always non-empty.
    """
    old = change.old if isinstance(change.old, tuple) else ()
    new = change.new if isinstance(change.new, tuple) else ()
    added, removed = _added_removed(old, new)
    parts = []
    if added:
        parts.append(f"the songs that landed on the {'release' if len(added) == 1 else 'releases'} you added")
    if removed:
        parts.append(f"the songs that were kept off the {'release' if len(removed) == 1 else 'releases'} you removed")
    return " and ".join(parts)


def _confirmations(changes: Sequence[Change], playlist_names: Mapping[str, str]) -> list[str]:
    out: list[str] = []
    re_resolving = [c.key for c in changes if (c.section, c.key) in _RE_RESOLVE]
    if re_resolving:
        out.append(
            f"Changing {', '.join(re_resolving)} re-resolves every liked and playlist song on the next run and "
            "re-baselines the health comparison, so check for changes (Review changes) and review them before "
            "the next scheduled run applies them."
        )
    switched_on = [c.key for c in changes if (c.section, c.key) in _SOURCES and c.new is True and c.old is False]
    if switched_on:
        out.append(f"Switching on {', '.join(switched_on)}: {_SOURCE_WARNING}.")
    for c in changes:
        if (c.section, c.key) == ("spotify", "playlists") and isinstance(c.new, tuple) and isinstance(c.old, tuple):
            added = [pid for pid in c.new if pid not in c.old]
            if added:
                named = ", ".join(f'"{playlist_names[pid]}" ({pid})' if pid in playlist_names else pid for pid in added)
                out.append(f"Adding playlist{'s' if len(added) > 1 else ''} {named}: {_SOURCE_WARNING}.")
    for c in changes:
        if (c.section, c.key) == ("rules", "deny_releases"):
            out.append(
                f"Changing deny_releases re-resolves {_deny_moves(c)} on the next run, so check for changes "
                "(Review changes) and review what they resolve to instead before the next scheduled run "
                "applies them."
            )
    for c in changes:
        if (
            c.section == "guards"
            and isinstance(c.new, int | float)
            and isinstance(c.old, int | float)
            and c.new > c.old
        ):
            effect = _GUARD_EFFECT[c.key].format(new=c.new)
            out.append(f"Loosening {c.key} from {c.old:g} to {c.new:g} lets the next scheduled run {effect}.")
    return out


def plan_pause(text: str, reason: str, *, base_dir: Path, now: datetime) -> SaveCheck:
    """`[schedule] enabled = false`, with `paused_reason` and `paused_at`. Saves at once - no
    confirm - because pausing only ever removes an unattended apply, never adds one.
    """
    was_enabled = parse_config(tomllib.loads(text), base_dir=base_dir).schedule.enabled
    reason = " ".join(reason.split())[:PAUSED_REASON_LIMIT]
    doc = tomlkit.parse(text)
    if "schedule" not in doc:
        doc.add("schedule", tomlkit.table())
    doc["schedule"]["enabled"] = False
    doc["schedule"]["paused_reason"] = reason
    doc["schedule"]["paused_at"] = now.astimezone(UTC)
    new_text = tomlkit.dumps(doc)

    check = SaveCheck(new_text=new_text, changes=[Change("schedule", "enabled", was_enabled, False)])
    try:
        parse_config(tomllib.loads(new_text), base_dir=base_dir)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors["schedule.paused_reason"] = str(exc)
    return check


LIBRARY_KEYS = ("root_folder", "quality_profile")
"""The `[lidarr]` keys a first start leaves unset and Settings picks from Lidarr's lists."""


def plan_library(text: str, chosen: Mapping[str, str], *, base_dir: Path) -> SaveCheck:
    """Set `[lidarr] root_folder` and `quality_profile` from `chosen`, each only when given and
    different. No confirm: nothing is added to Lidarr until a plan is reviewed and applied."""
    before = parse_config(tomllib.loads(text), base_dir=base_dir).lidarr
    doc = tomlkit.parse(text)
    if "lidarr" not in doc:
        doc.add("lidarr", tomlkit.table())
    changes: list[Change] = []
    for key in LIBRARY_KEYS:
        new = chosen.get(key, "")
        old = getattr(before, key)
        if new and new != old:
            doc["lidarr"][key] = new  # type: ignore[index]
            changes.append(Change("lidarr", key, old, new))
    new_text = tomlkit.dumps(doc)
    check = SaveCheck(new_text=new_text, changes=changes)
    try:
        parse_config(tomllib.loads(new_text), base_dir=base_dir)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors["lidarr"] = str(exc)
    return check


def plan_resume(text: str, *, base_dir: Path) -> SaveCheck:
    """`[schedule] enabled = true`. Always needs the second confirm (`SaveCheck.confirm`): it turns
    unattended applies back on, the same class of change as loosening a guard.
    """
    was_enabled = parse_config(tomllib.loads(text), base_dir=base_dir).schedule.enabled
    doc = tomlkit.parse(text)
    if "schedule" not in doc:
        doc.add("schedule", tomlkit.table())
    doc["schedule"]["enabled"] = True
    new_text = tomlkit.dumps(doc)

    check = SaveCheck(new_text=new_text, changes=[Change("schedule", "enabled", was_enabled, True)])
    try:
        parse_config(tomllib.loads(new_text), base_dir=base_dir)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors["schedule.enabled"] = str(exc)
        return check
    check.confirm = [
        "Resuming turns unattended applies back on: the next scheduled run applies whatever it plans, with no review."
    ]
    return check


def plan_cleanup(text: str, enabled: bool, *, base_dir: Path) -> SaveCheck:
    """`[prune] enabled`. Saves at once, no confirm, either way: turning Clean up on only
    shows a review page and lets Settings offer promote-save's Spotify write access; nothing moves
    and nothing reaches Spotify until a command is run by hand. The ledger and old prune jobs are
    never touched."""
    was = parse_config(tomllib.loads(text), base_dir=base_dir).prune.enabled
    doc = tomlkit.parse(text)
    if "prune" not in doc:
        doc.add("prune", tomlkit.table())
    section = doc["prune"]
    if not isinstance(section, dict):  # `prune = "x"` or `[[prune]]`: loads (never fatal), but no key to set
        return SaveCheck(new_text=text, errors={"prune.enabled": "[prune] must be a table; fix it in config.toml"})
    # A hand-written `enabled = "yes"` parses as off: writing a boolean over it is a change too.
    written = section.get("enabled", False)
    section["enabled"] = enabled
    new_text = tomlkit.dumps(doc)

    changed = was != enabled or not isinstance(written, bool)
    check = SaveCheck(new_text=new_text, changes=[Change("prune", "enabled", was, enabled)] if changed else [])
    try:
        parse_config(tomllib.loads(new_text), base_dir=base_dir)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors["prune.enabled"] = str(exc)
    return check


@dataclass(frozen=True, slots=True)
class SchedulePreview:
    """The Settings page's live preview of a cron line: the next few fires, or why it does not
    parse. Never raises - a page rendering a preview while the admin is mid-edit must not 500."""

    fires: tuple[datetime, ...] = ()
    error: str = ""
    summary: str = ""
    """A plain-English description ("Every day at 06:00"), only for the handful of shapes cheap
    and unambiguous to describe (`_describe`). Empty otherwise - the fires alone are fine."""


def preview_schedule(
    cron: str, timezone: str, *, now: datetime, count: int = SCHEDULE_PREVIEW_COUNT
) -> SchedulePreview:
    """The next `count` fires of `cron` in `timezone`, after `now`.

    Validated with `config.validate_cron_and_timezone` - the same function `_schedule` raises
    from at load - so a live preview and a rejected save can never give different reasons for the
    same bad line.
    """
    try:
        validate_cron_and_timezone(cron, timezone)
    except ConfigError as exc:
        return SchedulePreview(error=str(exc))
    expr = parse_cron(cron)
    tz = ZoneInfo(timezone)
    fires: list[datetime] = []
    cursor = now
    for _ in range(count):
        fire = next_fire_from(expr, cursor, tz)
        if fire is None:
            break
        fires.append(fire)
        cursor = fire
    return SchedulePreview(fires=tuple(fires), summary=_describe(expr))


_WEEKDAYS_MON_FRI = (1, 2, 3, 4, 5)
_ALL_WEEKDAYS = tuple(range(7))
_ALL_MONTHS = tuple(range(1, 13))


def _describe(expr: CronExpr) -> str:
    """A plain-English description of the handful of cron shapes it is cheap and unambiguous to
    describe: daily at a fixed time, every N hours on the hour at a fixed minute, or weekdays at a
    fixed time. Anything else is left undescribed - a wrong guess is worse than none.
    """
    if len(expr.minutes) != 1 or expr.days_restricted or expr.months != _ALL_MONTHS:
        return ""
    minute = expr.minutes[0]
    if len(expr.hours) == 1:
        hour = expr.hours[0]
        if expr.weekdays == _WEEKDAYS_MON_FRI:
            return f"Weekdays at {hour:02d}:{minute:02d}"
        if expr.weekdays == _ALL_WEEKDAYS:
            return f"Every day at {hour:02d}:{minute:02d}"
        return ""
    if expr.weekdays == _ALL_WEEKDAYS and len(expr.hours) >= 2 and expr.hours[0] == 0:
        step = expr.hours[1] - expr.hours[0]
        if step > 0 and expr.hours == tuple(range(0, 24, step)):
            return f"Every {'hour' if step == 1 else f'{step} hours'} at :{minute:02d}"
    return ""


def _fires_per_day(cron: str, timezone: str, *, now: datetime, window_days: int = 14) -> float:
    """How often `cron` fires, averaged over the next `window_days` from `now`. Only ever compared
    against another call of itself (`plan_schedule`'s confirm rule), never shown to anyone.

    `cron` is parsed once, not once per loop step: `parse_config` already refuses anything below
    `MIN_SCHEDULE_INTERVAL_MINUTES` (60), so this is at most `window_days * 24` iterations, cheap
    enough to call straight from the (synchronous) settings route - see `plan_schedule`.
    """
    try:
        expr = parse_cron(cron)
        tz = ZoneInfo(timezone)
    except (CronError, ZoneInfoNotFoundError, ValueError, OSError):
        return 0.0
    end_utc = (now + timedelta(days=window_days)).astimezone(UTC)
    count = 0
    cursor = now
    for _ in range(window_days * 24 + 1):  # a fire an hour is the tightest the config allows
        fire = next_fire_from(expr, cursor, tz)
        if fire is None or fire.astimezone(UTC) >= end_utc:
            break
        count += 1
        cursor = fire
    return count / window_days


def plan_schedule(text: str, cron: str, timezone: str, *, base_dir: Path, now: datetime) -> SaveCheck:
    """`[schedule] cron` and `[schedule] timezone`, validated like any other saved setting.

    A schedule that fires more often than the one it replaces gets the second confirm
    (`SaveCheck.confirm`), the same class of change as loosening a guard: the next fire applies
    unattended, and a tighter schedule means more of them with no extra review. Anything else -
    a looser schedule, or only the timezone changing - saves at once.
    """
    old = parse_config(tomllib.loads(text), base_dir=base_dir).schedule
    cron = cron.strip()
    timezone = timezone.strip()
    changes: list[Change] = []
    if cron != old.cron:
        changes.append(Change("schedule", "cron", old.cron, cron))
    if timezone != old.timezone:
        changes.append(Change("schedule", "timezone", old.timezone, timezone))
    if not changes:
        return SaveCheck(new_text=text)

    doc = tomlkit.parse(text)
    if "schedule" not in doc:
        doc.add("schedule", tomlkit.table())
    doc["schedule"]["cron"] = cron
    doc["schedule"]["timezone"] = timezone
    new_text = tomlkit.dumps(doc)

    check = SaveCheck(new_text=new_text, changes=changes)
    try:
        new_schedule = parse_config(tomllib.loads(new_text), base_dir=base_dir).schedule
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        check.errors["schedule.cron"] = str(exc)
        return check
    if _fires_per_day(new_schedule.cron, new_schedule.timezone, now=now) > _fires_per_day(
        old.cron, old.timezone, now=now
    ):
        check.confirm = [
            "This schedule fires more often than the current one, so the next scheduled run applies whatever it "
            "plans, unattended, sooner and more often than before."
        ]
    return check


def write_config(path: Path, new_text: str, *, expected_hash: str, now: datetime) -> Path:
    """Back up `path`, prune old UI backups, and atomically replace it with `new_text`.

    Raises:
        SaveConflict: the file's hash is not `expected_hash`. Nothing was written.

    Returns:
        The backup's path.
    """
    current = path.read_bytes()
    if file_hash(current) != expected_hash:
        raise SaveConflict(f"{path.name} changed since this page was opened")

    # No world bits on the file or its backups: config.toml can hold a capability URL
    # ([health.webhook] url). Group bits stay, so a host user in the container's group can still
    # edit it by hand. A 0600 file stays 0600.
    old = os.stat(path)
    mode = old.st_mode & 0o770
    backup = _write_backup(path, current, mode=mode, now=now, times_ns=(old.st_atime_ns, old.st_mtime_ns))
    _prune_backups(path)

    write_atomic(path, new_text, mode=mode)
    return backup


def _write_backup(path: Path, data: bytes, *, mode: int, now: datetime, times_ns: tuple[int, int]) -> Path:
    """Write `data` to the first free `<name>.bak-<stamp>[-n]` beside `path`, with exactly `mode`
    from before its first byte, and the old file's times (as `shutil.copy2` kept them).

    The name is taken with ``O_EXCL | O_NOFOLLOW``: a file or symlink already there, dangling or
    not, is skipped rather than written through.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for n in range(1, 1000):
        backup = path.with_name(f"{path.name}.bak-{stamp}" + (f"-{n}" if n > 1 else ""))
        try:
            fd = os.open(backup, flags, mode)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), mode)  # the umask may have narrowed the open's mode
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                if os.utime in os.supports_fd:  # the file just written, never a path swapped in since
                    os.utime(fh.fileno(), ns=times_ns)
                else:  # pragma: no cover - every platform likearr ships on supports it
                    os.utime(backup, ns=times_ns, follow_symlinks=False)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup
    raise FileExistsError(f"no free backup name for {path.name} at {stamp}")


def _prune_backups(path: Path) -> None:
    ours = sorted(
        (p for p in path.parent.iterdir() if (m := _BACKUP_NAME.match(p.name)) and m["stem"] == path.name),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in ours[BACKUP_KEEP:]:
        old.unlink(missing_ok=True)
