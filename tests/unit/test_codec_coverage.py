"""Every persisted model survives its codec, field by field.

The codecs that write likearr's files and state rows list their fields by hand, across the package.
Nothing failed when a new model field was left out of one: `Resolution.rules` read back as its
default for weeks, and it reached the diff file the same way. This test builds each
persisted dataclass from its own type hints with **every field set away from its default**, at
every depth, round-trips it through each codec that writes it, and compares field by field.

A new field therefore fails here, naming the codec and the field, until it is either carried by
the codec or added to that codec's `dropped` list on purpose, with a one-line reason. A field that
reads back as its default counts as lost, because the builder never uses a default.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import tempfile
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from likearr.adapters.spotify import TokenSet
from likearr.adapters.state_sqlite import (
    SqliteState,
    _diff_to_json,
    _health_record_from_json,
    _health_record_to_json,
    _release_group_from_json,
    _release_group_to_json,
    resolution_from_json,
    resolution_to_json,
)
from likearr.core.prune import PruneRow
from likearr.models import (
    ArtistResolution,
    DesiredState,
    Diff,
    Guard,
    HealthBaseline,
    HealthRecord,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    ReleaseGroup,
    Resolution,
    SourceSnapshot,
)
from likearr.prune_ledger import Entry, parse_entry
from likearr.shell.adopt_io import AdoptPlanFile, adopt_plan_from_dict, adopt_plan_to_dict
from likearr.shell.diff_io import diff_from_dict, diff_from_run_dict, diff_to_dict
from likearr.shell.last_run import (
    _album_from_dict,
    _album_to_dict,
    _artist_from_dict,
    _artist_to_dict,
    _desired_from_dict,
    _desired_to_dict,
    last_run_facts,
    read_last_run,
    snapshot_from_dict,
    snapshot_to_dict,
    write_last_run,
)
from likearr.shell.promote_save import PROMOTE_SAVE_PLAN_VERSION, PromoteSavePlan, plan_from_dict, plan_to_dict
from likearr.shell.prune_commands import _row_from_dict, _row_to_dict
from likearr.web.cleanup import Binding, read_binding, write_binding
from likearr.web.jobs import JobMeta

# ---------------------------------------------------------------- the instance builder


class _Builder:
    """Builds a dataclass instance with every field off its default, from its type hints.

    Strings carry their own field path, so a value that lands in the wrong field is caught too.
    `fixed` pins a field path to a value where the model or the codec demands one (a version the
    reader checks, a date format), and those paths are the only ones allowed to sit at a default.
    """

    def __init__(self, fixed: Mapping[str, object]) -> None:
        self.fixed = fixed
        self.counter = 0

    def build(self, cls: type, path: str = "") -> Any:
        hints = typing.get_type_hints(cls)
        values = {}
        for f in dataclasses.fields(cls):
            if not f.init:
                continue
            here = f"{path}.{f.name}" if path else f.name
            if here in self.fixed:
                values[f.name] = self.fixed[here]
            else:
                values[f.name] = self.value(hints[f.name], here, _default(f))
        return cls(**values)

    def value(self, tp: Any, path: str, default: object = dataclasses.MISSING) -> Any:
        self.counter += 1
        origin, args = typing.get_origin(tp), typing.get_args(tp)
        if origin in (typing.Union, types.UnionType):
            arms = [a for a in args if a is not type(None)]
            return self.value(arms[0], path, dataclasses.MISSING)
        if dataclasses.is_dataclass(tp) and isinstance(tp, type):
            return self.build(tp, path)
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            return next(m for m in reversed(list(tp)) if m != default)
        if tp is bool:
            return not default if isinstance(default, bool) else True
        if tp is int:
            return (default if isinstance(default, int) else 0) + 100 + self.counter
        if tp is float:
            return (default if isinstance(default, float) else 0.0) + 0.5 + self.counter
        if tp is str:
            return f"{path}#{self.counter}"
        if tp is datetime:
            return datetime(2021, 2, 3, 4, 5, self.counter % 60, tzinfo=UTC)
        if tp is date:
            return date(2021, 2, 1 + self.counter % 28)
        if tp in (object, Any):
            return f"{path}#{self.counter}"
        if origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return (self.value(args[0], path),)
            return tuple(self.value(a, path) for a in args)
        if origin is list:
            return self._items(args[0], path)
        if origin in (set, frozenset):
            return origin(self._items(args[0], path))
        if origin in (dict, Mapping):
            return {self.value(args[0], path): self.value(args[1], path)}
        raise TypeError(f"the builder has no value for {tp!r} at {path}; teach it one")

    def _items(self, tp: Any, path: str) -> list[Any]:
        """One of each arm for a union element type (`Resolution | ArtistResolution`), so a
        polymorphic list is covered for both."""
        if typing.get_origin(tp) in (typing.Union, types.UnionType):
            return [self.value(a, path) for a in typing.get_args(tp) if a is not type(None)]
        return [self.value(tp, path)]


def _default(f: dataclasses.Field[Any]) -> object:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return dataclasses.MISSING


def _losses(expected: object, actual: object, path: str = "") -> set[str]:
    """Field paths whose value did not survive. Reads every dataclass field, `compare=False` ones
    included, and recurses through lists, tuples and dicts; element positions are not part of the
    path, so one entry per field is reported however many items carry it. A value that comes back
    as another type counts as lost too: a list for a tuple, or a set for a frozenset, compares
    equal but leaves a frozen model unhashable."""
    if type(expected) is not type(actual):
        return {path or "<root>"}
    if dataclasses.is_dataclass(expected) and not isinstance(expected, type):
        out: set[str] = set()
        for f in dataclasses.fields(expected):
            here = f"{path}.{f.name}" if path else f.name
            out |= _losses(getattr(expected, f.name), getattr(actual, f.name), here)
        return out
    if isinstance(expected, list | tuple) and isinstance(actual, list | tuple):
        if len(expected) != len(actual):
            return {path}
        return set().union(*(_losses(e, a, path) for e, a in zip(expected, actual, strict=True)))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            return {path}
        return set().union(*(_losses(v, actual[k], path) for k, v in expected.items()))
    return set() if expected == actual else {path}


def _at_default(obj: object, path: str = "") -> set[str]:
    """Field paths the builder left at their default, which would hide a loss."""
    out: set[str] = set()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            here = f"{path}.{f.name}" if path else f.name
            value = getattr(obj, f.name)
            default = _default(f)
            if default is not dataclasses.MISSING and value == default:
                out.add(here)
            out |= _at_default(value, here)
    elif isinstance(obj, list | tuple | set | frozenset):
        for item in obj:
            out |= _at_default(item, path)
    elif isinstance(obj, dict):
        for item in obj.values():
            out |= _at_default(item, path)
    return out


def _via_json(value: Any) -> Any:
    """What a file or a JSON column hands back: the codec's output written and read as text."""
    return json.loads(json.dumps(value))


# ---------------------------------------------------------------- the codecs


_RG_UNSTORED = "`main_artist_mbids` is resolver-only (compare=False): nothing reads it after a run"


@dataclass(frozen=True)
class Codec:
    name: str
    model: type
    round_trip: Callable[[Any], Any]
    dropped: Mapping[str, str] = field(default_factory=dict)
    """Field path -> why this codec leaves it out on purpose."""
    fixed: Mapping[str, object] = field(default_factory=dict)
    """Field path -> the value the model or the reader requires there."""
    prepare: Callable[[Any], Any] = lambda obj: obj
    """Restores an invariant the builder cannot know, such as a dict keyed by its values' own key."""


def _desired_consistent(desired: DesiredState) -> DesiredState:
    # `releases` is keyed by each release's own key, which is how `_desired_from_dict` rebuilds it.
    desired.releases = {r.key: r for r in desired.releases.values()}
    return desired


def _through_dir(write: Callable[[Path], object], read: Callable[[Path], Any]) -> Any:
    """For a codec that only writes to and reads from disk: write into a scratch directory, read back."""
    with tempfile.TemporaryDirectory() as tmp:
        write(Path(tmp))
        return read(Path(tmp))


def _baseline_via_state(baseline: HealthBaseline) -> HealthBaseline | None:
    def write(d: Path) -> None:
        with SqliteState(d / "state.sqlite") as state:
            state.record_health_baseline(baseline)

    def read(d: Path) -> HealthBaseline | None:
        with SqliteState(d / "state.sqlite") as state:
            return state.health_baseline()

    return _through_dir(write, read)


def _guard_via_last_run(guard: Guard) -> Guard | None:
    """`last-run.json` writes and reads its guards inline, apart from the other codecs above."""
    builder = _Builder({})
    facts = last_run_facts(
        ran_at=datetime(2021, 2, 3, tzinfo=UTC),
        snapshot=builder.build(SourceSnapshot),
        resolutions={},
        artist_resolutions={},
        desired=_desired_consistent(builder.build(DesiredState)),
        view=LidarrView(artists={}, albums={}, metadata_profiles={}, quality_profiles={}, tags={}),
        owned_keys=(),
        collisions=(),
        guards=(guard,),
    )
    name = "last-run.json"
    last = _through_dir(lambda d: write_last_run(d / name, facts), lambda d: read_last_run(d / name))
    return last.guards[0] if last is not None and last.guards else None


CODECS = [
    Codec(
        "state_sqlite resolution_to_json / resolution_from_json",
        Resolution,
        lambda r: resolution_from_json(resolution_to_json(r)),
        dropped={
            "release_group.main_artist_mbids": _RG_UNSTORED,
            "single_release_group.main_artist_mbids": _RG_UNSTORED,
            "source_release_group.main_artist_mbids": _RG_UNSTORED,
        },
    ),
    Codec(
        "state_sqlite _release_group_to_json / _release_group_from_json",
        ReleaseGroup,
        lambda rg: _release_group_from_json(_via_json(_release_group_to_json(rg))),
        dropped={"main_artist_mbids": _RG_UNSTORED},
    ),
    Codec(
        "state_sqlite _health_record_to_json / _health_record_from_json",
        HealthRecord,
        lambda h: _health_record_from_json(_health_record_to_json(h)),
    ),
    Codec(
        "state_sqlite runs.diff_json (_diff_to_json) / diff_io.diff_from_run_dict",
        Diff,
        lambda d: diff_from_run_dict(json.loads(_diff_to_json(d))),
        dropped={
            "update_reasons": "stored positionally by asdict; the Status page omits it",
            "pending": "no display reader needs it (diff_from_run_dict)",
            "unmapped": "no `_kind` discriminator in the asdict shape, so it is not decoded (diff_from_run_dict)",
            "claim": "no display reader needs it; the health record counts it (diff_from_run_dict)",
        },
    ),
    Codec(
        "diff_io diff_to_dict / diff_from_dict (diff.json)",
        Diff,
        lambda d: diff_from_dict(_via_json(diff_to_dict(d))),
        dropped={
            "pending.release_group.main_artist_mbids": _RG_UNSTORED,
            "pending.single_release_group.main_artist_mbids": _RG_UNSTORED,
            "pending.source_release_group.main_artist_mbids": _RG_UNSTORED,
            "unmapped.release_group.main_artist_mbids": _RG_UNSTORED,
            "unmapped.single_release_group.main_artist_mbids": _RG_UNSTORED,
            "unmapped.source_release_group.main_artist_mbids": _RG_UNSTORED,
        },
    ),
    Codec(
        "last_run snapshot_to_dict / snapshot_from_dict",
        SourceSnapshot,
        lambda s: snapshot_from_dict(_via_json(snapshot_to_dict(s))),
    ),
    Codec(
        "last_run _desired_to_dict / _desired_from_dict",
        DesiredState,
        lambda d: _desired_from_dict(_via_json(_desired_to_dict(d))),
        dropped={
            "releases.release_group.main_artist_mbids": _RG_UNSTORED,
            "pending": "Explain after a run reads the releases, not the pending list",
            "unmapped": "Explain after a run reads the releases, not the unmapped list",
            "catalogue_counts": "Only the shrink guard and apply baselines read it, from a fresh plan; not explain",
        },
        prepare=_desired_consistent,
    ),
    Codec(
        "last_run _artist_to_dict / _artist_from_dict",
        LidarrArtist,
        lambda a: _artist_from_dict(_via_json(_artist_to_dict(a))),
        dropped={
            "tags": "Explain after a run does not read Lidarr tags",
            "path": "Explain after a run does not read the artist folder",
        },
    ),
    Codec(
        "last_run _album_to_dict / _album_from_dict",
        LidarrAlbum,
        lambda a: _album_from_dict(_via_json(_album_to_dict(a))),
        dropped={"size_on_disk": "Explain after a run does not read sizes; prune reads Lidarr live"},
    ),
    Codec(
        "adopt_io adopt_plan_to_dict / adopt_plan_from_dict",
        AdoptPlanFile,
        lambda p: adopt_plan_from_dict(_via_json(adopt_plan_to_dict(p))),
        dropped={"adoption.unmonitor.lost_reasons": "an adopt unmonitor has no reasons; the file does not carry them"},
    ),
    Codec(
        "promote_save plan_to_dict / plan_from_dict",
        PromoteSavePlan,
        lambda p: plan_from_dict(_via_json(plan_to_dict(p))),
        fixed={"version": PROMOTE_SAVE_PLAN_VERSION},
    ),
    Codec(
        "prune_commands _row_to_dict / _row_from_dict (prune.json)",
        PruneRow,
        lambda r: _row_from_dict(_via_json(_row_to_dict(r))),
        dropped={
            "release_date": "prune-stage selects and trashes by id and path; it never reads the date",
            "protection": "written for the review's words; prune-stage reads protected rows by artist only",
        },
    ),
    Codec(
        "web.jobs JobMeta.to_json / JobMeta.from_mapping (meta.json)",
        JobMeta,
        lambda m: JobMeta.from_mapping(json.loads(m.to_json())),
    ),
    Codec(
        "adapters.spotify TokenSet.to_json / TokenSet.from_mapping (token file)",
        TokenSet,
        lambda t: TokenSet.from_mapping(json.loads(t.to_json())),
    ),
    Codec(
        "prune_ledger Entry.to_dict / parse_entry (ledger)",
        Entry,
        lambda e: parse_entry(_via_json(e.to_dict()), (e.decision,)),
        fixed={"on": "2026-01-15"},
    ),
    Codec(
        "state_sqlite record_health_baseline / health_baseline (health_baseline_meta)",
        HealthBaseline,
        _baseline_via_state,
    ),
    Codec(
        "last_run last_run_facts / read_last_run guards (last-run.json)",
        Guard,
        _guard_via_last_run,
    ),
    Codec(
        "web.cleanup write_binding / read_binding (previews.json)",
        Binding,
        lambda b: _through_dir(lambda d: write_binding(d, b), read_binding),
    ),
]


def _sample(codec: Codec) -> Any:
    return codec.prepare(_Builder(codec.fixed).build(codec.model))


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_every_field_survives_the_codec(codec: Codec) -> None:
    original = _sample(codec)

    lost = _losses(original, codec.round_trip(original))

    unexpected = sorted(lost - set(codec.dropped))
    assert not unexpected, (
        f"{codec.name} loses {codec.model.__name__} field(s) {unexpected}: carry them in the codec, "
        "or add them to this codec's `dropped` list with the reason"
    )
    stale = sorted(set(codec.dropped) - lost)
    assert not stale, f"{codec.name} now carries {stale}: take them off its `dropped` list"


@pytest.mark.parametrize("codec", CODECS, ids=lambda c: c.name)
def test_the_builder_leaves_no_field_at_its_default(codec: Codec) -> None:
    """A field left at its default would read back "equal" even when the codec dropped it."""
    assert _at_default(_sample(codec)) <= set(codec.fixed)


def test_a_dropped_field_is_named() -> None:
    """The failure is only useful if it says which field went missing, and where."""
    resolution = _Builder({}).build(Resolution)

    lost = _losses(resolution, dataclasses.replace(resolution, rules=""))

    assert lost == {"rules"}


def test_the_builder_covers_both_arms_of_a_polymorphic_list() -> None:
    diff = _Builder({}).build(Diff)

    assert {type(u) for u in diff.unmapped} == {Resolution, ArtistResolution}
