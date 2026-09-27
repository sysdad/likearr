"""Tests for the health comparison: what makes a run `ok`, and what makes it `degraded`.

The whole rule set lives here because `core.health` is pure. Every scenario below is expressible
without a Lidarr, a Spotify or a clock, which is the point of putting the rules in the core.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from likearr.core.health import (
    CLASS_A,
    CLASS_B,
    LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS,
    LIDARR_METADATA_OUTAGE_RATIO,
    PROBLEM_STATUSES,
    Fingerprint,
    HealthBaseline,
    Observation,
    classify,
    compare,
    lidarr_metadata_outage,
    next_baseline,
    notification,
    should_notify,
)
from likearr.models import RunStatus

RATIO = 0.05

FINGERPRINT = Fingerprint(resolver_version=4, liked_track_scope="album", source_set=("liked_tracks",))


def observation(
    *,
    unmapped: Mapping[str, str] | None = None,
    catalogue_gaps: Mapping[str, str] | None = None,
    catalogue_gaps_recent: Mapping[str, str] | None = None,
    absent_in_lidarr: Mapping[str, str] | None = None,
    lidarr_metadata: Sequence[str] = (),
    skipped_artists: Sequence[str] | None = None,
    name_collisions: Sequence[str] = (),
    catalogue_too_large: Sequence[str] = (),
    intents: Sequence[str] = (),
    applied: bool = True,
) -> Observation:
    """An observation, with `None` meaning "this run could not see that dimension"."""
    identities: dict[str, Mapping[str, str]] = {
        "unmapped": dict(unmapped or {}),
        "catalogue_gaps": dict(catalogue_gaps or {}),
        "catalogue_gaps_recent": dict(catalogue_gaps_recent or {}),
        "lidarr_metadata": dict.fromkeys(lidarr_metadata, ""),
        "name_collisions": dict.fromkeys(name_collisions, ""),
        "catalogue_too_large": dict.fromkeys(catalogue_too_large, ""),
    }
    observed = set(identities)
    if applied:
        identities["absent_in_lidarr"] = dict(absent_in_lidarr or {})
        identities["skipped_artists"] = dict.fromkeys(skipped_artists or (), "")
        observed |= {"absent_in_lidarr", "skipped_artists"}
    return Observation(identities=identities, intents=frozenset(intents), observed=frozenset(observed))


def baseline_of(observation_: Observation, *, fingerprint: Fingerprint = FINGERPRINT) -> HealthBaseline:
    return next_baseline(observation_, None, fingerprint, accept=True)


def verdict(
    current: Observation,
    baseline: HealthBaseline | None,
    *,
    fingerprint: Fingerprint = FINGERPRINT,
    spotify_schema_ok: bool = True,
    mb_ok: bool = True,
    lidarr_outage: bool = False,
    guarded: bool = False,
):
    delta = compare(current, baseline, fingerprint)
    return delta, classify(
        delta,
        spotify_schema_ok=spotify_schema_ok,
        mb_ok=mb_ok,
        lidarr_outage=lidarr_outage,
        guarded=guarded,
        ratio=RATIO,
    )


def spread(prefix: str, count: int, *, intent: str = "") -> dict[str, str]:
    """`count` distinct identities, each mapped to its intent key (its own by default)."""
    return {f"{prefix}-{i}": (intent or f"{prefix}-intent-{i}") for i in range(count)}


# --------------------------------------------------------------------------- no baseline to compare


def test_a_first_run_never_alarms_however_bad_the_chronic_set_is() -> None:
    """A first run: hundreds unmapped and a fistful of collisions must publish `ok`, not the old `degraded`."""
    current = observation(
        unmapped=spread("u", 240),
        name_collisions=["a|b", "c|d"],
        intents=[f"i-{n}" for n in range(1200)],
    )

    delta, out = verdict(current, None)

    assert delta.state == "first-run"
    assert not delta.comparable
    assert out.status is RunStatus.OK
    assert out.conditions == ()
    assert delta.totals["unmapped"] == 240


def test_a_resolver_version_bump_re_baselines_instead_of_alarming() -> None:
    old = baseline_of(observation(unmapped=spread("u", 10), intents=["i-1"]))
    current = observation(unmapped=spread("v", 10), intents=["i-1"])

    delta, out = verdict(current, old, fingerprint=Fingerprint(5, "album", ("liked_tracks",)))

    assert delta.state == "resolver-version-changed"
    assert out.status is RunStatus.OK


def test_a_liked_track_scope_change_re_baselines() -> None:
    old = baseline_of(observation(unmapped=spread("u", 10), intents=["i-1"]))
    current = observation(unmapped=spread("v", 10), intents=["i-1"])

    delta, out = verdict(current, old, fingerprint=Fingerprint(4, "smallest", ("liked_tracks",)))

    assert delta.state == "scope-changed"
    assert out.status is RunStatus.OK


def test_turning_on_an_opt_out_re_baselines_rather_than_reporting_a_jump() -> None:
    """Issue #15: an opt-out unmaps mapped tracks on purpose, which is not a fault.

    One switch can move well over 5% of a library of a few thousand intents in a single run, so without this
    the run that finally applies the user's preference is also the run that reports itself broken.
    """
    old = baseline_of(observation(unmapped=spread("u", 10), intents=[f"i-{n}" for n in range(100)]))
    current = observation(unmapped=spread("u", 60), intents=[f"i-{n}" for n in range(100)])

    delta, out = verdict(current, old, fingerprint=Fingerprint(4, "album", ("liked_tracks",), "c0r1"))

    assert delta.state == "rules-changed"
    assert out.status is RunStatus.OK


def test_a_baseline_written_before_the_opt_outs_still_compares_against_the_defaults() -> None:
    """Deploying the code must be a no-op: an old baseline carries no rules, and nor do defaults."""
    old = baseline_of(observation(unmapped=spread("u", 10), intents=["i-1"]))
    current = observation(unmapped=spread("u", 10), intents=["i-1"])

    delta, out = verdict(current, old, fingerprint=Fingerprint(4, "album", ("liked_tracks",)))

    assert delta.state == "compared"
    assert out.status is RunStatus.OK


def test_adding_a_playlist_re_baselines_rather_than_reporting_a_jump() -> None:
    """Adding many playlists at once produces hundreds of new identities. That is the user, not a fault."""
    old = baseline_of(observation(unmapped=spread("u", 10), intents=[f"i-{n}" for n in range(100)]))
    current = observation(unmapped=spread("u", 600), intents=[f"i-{n}" for n in range(700)])

    delta, out = verdict(current, old, fingerprint=Fingerprint(4, "album", ("liked_tracks", "playlist:abc")))

    assert delta.state == "sources-changed"
    assert out.status is RunStatus.OK


def test_removing_a_playlist_takes_the_same_path() -> None:
    old = next_baseline(
        observation(intents=["i-1"]),
        None,
        Fingerprint(4, "album", ("liked_tracks", "playlist:abc")),
        accept=True,
    )
    current = observation(intents=["i-1"])

    delta, _ = verdict(current, old, fingerprint=Fingerprint(4, "album", ("liked_tracks",)))

    assert delta.state == "sources-changed"


# --------------------------------------------------------------------------- chronic, unchanged


def test_an_identical_chronic_set_is_ok_and_reports_its_counts() -> None:
    current = observation(
        unmapped=spread("u", 240),
        catalogue_gaps=spread("g", 40),
        absent_in_lidarr=spread("a", 37),
        lidarr_metadata=["album-search:Leopold Stokowski|", "album-search:'Carousel' 2018|"],
        intents=[f"i-{n}" for n in range(1200)],
    )
    old = baseline_of(current)

    delta, out = verdict(current, old)

    assert delta.state == "compared"
    assert out.status is RunStatus.OK
    assert out.conditions == ()
    assert all(delta.new[dimension] == 0 for dimension in CLASS_A)
    assert delta.totals["unmapped"] == 240
    assert delta.totals["catalogue_gaps"] == 40
    assert delta.totals["lidarr_metadata"] == 2


def test_the_same_count_with_different_members_is_not_unchanged() -> None:
    """Identity, not count. A swapped-out intent is the failure a count comparison cannot see."""
    old = baseline_of(observation(unmapped=spread("u", 50), intents=["keeper"]))
    swapped = dict(spread("u", 49))
    swapped["u-brand-new"] = "keeper"
    current = observation(unmapped=swapped, intents=["keeper"])

    delta, _ = verdict(current, old)

    assert delta.totals["unmapped"] == 50
    assert delta.new["unmapped"] == 1
    assert delta.resolved["unmapped"] == 1


def test_a_shrinking_chronic_set_is_ok_and_says_what_cleared() -> None:
    old = baseline_of(observation(unmapped=spread("u", 50), intents=["i-1"]))
    current = observation(unmapped=spread("u", 20), intents=["i-1"])

    delta, out = verdict(current, old)

    assert out.status is RunStatus.OK
    assert delta.new["unmapped"] == 0
    assert delta.resolved["unmapped"] == 30


# --------------------------------------------------------------------------- class B: any new one alarms


def test_one_new_name_collision_degrades() -> None:
    old = baseline_of(observation(name_collisions=["a|b"], intents=["i-1"]))
    current = observation(name_collisions=["a|b", "c|d"], intents=["i-1"])

    delta, out = verdict(current, old)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("new-name-collision",)
    assert delta.new["name_collisions"] == 1


def test_one_new_skipped_artist_degrades() -> None:
    old = baseline_of(observation(skipped_artists=[], intents=["i-1"]))
    current = observation(skipped_artists=["artist-1"], intents=["i-1"])

    _, out = verdict(current, old)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("new-skipped-artist",)


def test_one_new_oversized_catalogue_degrades_and_is_class_b() -> None:
    """It costs a whole followed catalogue, so it is discrete and actionable, not a shortfall."""
    assert "catalogue_too_large" in CLASS_B
    assert "catalogue_too_large" not in CLASS_A

    old = baseline_of(observation(intents=["i-1"]))
    current = observation(catalogue_too_large=["artist-9"], intents=["i-1"])

    _, out = verdict(current, old)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("new-catalogue-too-large",)


# --------------------------------------------------------------------------- class A: only a jump alarms


def test_one_newly_unmapped_intent_is_reported_but_does_not_alarm() -> None:
    intents = ["keeper", *(f"i-{n}" for n in range(999))]
    old = baseline_of(observation(unmapped=spread("u", 10), intents=intents))
    grown = dict(spread("u", 10))
    grown["u-new"] = "keeper"
    current = observation(unmapped=grown, intents=intents)

    delta, out = verdict(current, old)

    assert out.status is RunStatus.OK
    assert delta.new["unmapped"] == 1
    assert delta.regressions == 1


def test_the_threshold_is_a_fraction_of_the_library_not_a_flat_count() -> None:
    """Scale-free by construction: one broken intent out of four is a catastrophe, out of 1,000 is not."""
    tiny = ["a", "b", "c", "d"]
    old = baseline_of(observation(intents=tiny))
    current = observation(unmapped={"u-1": "a"}, intents=tiny)

    _, out = verdict(current, old)

    assert out.status is RunStatus.DEGRADED


def test_a_jump_in_regressions_over_the_ratio_degrades() -> None:
    intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(unmapped={}, intents=intents))
    broken = {f"u-{n}": f"i-{n}" for n in range(60)}
    current = observation(unmapped=broken, intents=intents)

    delta, out = verdict(current, old)

    assert delta.regressions == 60
    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("mapping-shortfall-jump",)


def test_the_jump_is_summed_across_every_class_a_dimension() -> None:
    """No single dimension crosses the line; together they do. One threshold, one rule."""
    intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=intents))
    current = observation(
        unmapped={f"u-{n}": f"i-{n}" for n in range(20)},
        catalogue_gaps={f"g-{n}": f"i-{n}" for n in range(20)},
        absent_in_lidarr={f"a-{n}": f"i-{n}" for n in range(20)},
        lidarr_metadata=["album-search:x|y"],
        intents=intents,
    )

    delta, out = verdict(current, old)

    assert delta.regressions == 61
    assert out.status is RunStatus.DEGRADED


def test_bulk_new_intents_that_fail_to_map_are_the_base_rate_not_an_alarm() -> None:
    """A user likes 300 songs; ~60 do not map. Nothing regressed - those intents are brand new."""
    old_intents = [f"old-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=old_intents))
    fresh = {f"u-{n}": f"new-{n}" for n in range(60)}
    current = observation(unmapped=fresh, intents=[*old_intents, *(f"new-{n}" for n in range(300))])

    delta, out = verdict(current, old)

    assert delta.new["unmapped"] == 60
    assert delta.regressions == 0
    assert out.status is RunStatus.OK


def test_previously_present_intents_going_unmapped_is_a_regression() -> None:
    intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=intents))
    current = observation(unmapped={f"u-{n}": f"i-{n}" for n in range(70)}, intents=intents)

    delta, out = verdict(current, old)

    assert delta.regressions == 70
    assert out.status is RunStatus.DEGRADED


def test_a_previously_followed_artist_gaining_gaps_still_fires() -> None:
    """A followed artist's Lidarr catalogue empties. The follow is the intent."""
    intents = ["follow:radiohead", *(f"i-{n}" for n in range(999))]
    old = baseline_of(observation(intents=intents))
    current = observation(catalogue_gaps={f"g-{n}": "follow:radiohead" for n in range(70)}, intents=intents)

    delta, out = verdict(current, old)

    assert delta.regressions == 70
    assert out.status is RunStatus.DEGRADED


def test_a_new_release_from_a_followed_artist_is_counted_but_never_a_regression() -> None:
    """Issue #8: Lidarr's metadata lag on a brand-new album is normal, not a fault.

    Its intent - the follow - existed last run, so left in `catalogue_gaps` every new release
    would count toward the jump. `catalogue_gaps_recent` is in neither class A nor class B, so it
    is reported and never alarms.
    """
    intents = ["follow:radiohead", *(f"i-{n}" for n in range(999))]
    old = baseline_of(observation(intents=intents))
    current = observation(
        catalogue_gaps_recent={f"g-{n}": "follow:radiohead" for n in range(70)},
        intents=intents,
    )

    delta, out = verdict(current, old)

    assert delta.totals["catalogue_gaps_recent"] == 70
    assert delta.new["catalogue_gaps_recent"] == 70
    assert delta.regressions == 0
    assert out.status is RunStatus.OK
    assert "catalogue_gaps_recent" not in CLASS_A
    assert "catalogue_gaps_recent" not in CLASS_B


def test_a_recent_gap_that_survived_the_last_apply_is_not_counted_as_new() -> None:
    """`total - new` is what says a refresh was queued last run and Lidarr still has not caught up."""
    current = observation(catalogue_gaps_recent={"g-1": "follow:a", "g-2": "follow:a"}, intents=["follow:a"])
    old = baseline_of(observation(catalogue_gaps_recent={"g-1": "follow:a"}, intents=["follow:a"]))

    delta, out = verdict(current, old)

    assert delta.totals["catalogue_gaps_recent"] == 2
    assert delta.new["catalogue_gaps_recent"] == 1, "one persisted across the apply"
    assert out.status is RunStatus.OK


def test_a_recent_gap_is_overwritten_in_the_baseline_not_sticky() -> None:
    """A release Lidarr finally picked up must leave the baseline, exactly as class A does."""
    old = baseline_of(observation(catalogue_gaps_recent={"g-1": "follow:a"}, intents=["follow:a"]))
    healed = observation(intents=["follow:a"])

    written = next_baseline(healed, old, FINGERPRINT, accept=False)

    assert written.of("catalogue_gaps_recent") == frozenset()


def test_a_newly_followed_artist_arriving_with_gaps_does_not_fire() -> None:
    old_intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=old_intents))
    current = observation(
        catalogue_gaps={f"g-{n}": "follow:queen" for n in range(70)},
        intents=[*old_intents, "follow:queen"],
    )

    delta, out = verdict(current, old)

    assert delta.new["catalogue_gaps"] == 70
    assert delta.regressions == 0
    assert out.status is RunStatus.OK


def test_regressions_never_exceed_the_raw_new_count() -> None:
    old_intents = [f"old-{n}" for n in range(100)]
    old = baseline_of(observation(intents=old_intents))
    mixed = {"u-a": "old-1", "u-b": "brand-new"}
    current = observation(unmapped=mixed, intents=[*old_intents, "brand-new"])

    delta, _ = verdict(current, old)

    assert delta.new["unmapped"] == 2
    assert delta.regressions == 1


def test_a_lidarr_lookup_failure_has_no_intent_so_it_always_counts() -> None:
    """Its identity carries no intent, and a SkyHook outage is the only thing that floods it."""
    intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=intents))
    current = observation(lidarr_metadata=[f"album-search:artist-{n}|title-{n}" for n in range(61)], intents=intents)

    delta, out = verdict(current, old)

    assert delta.regressions == 61
    assert out.status is RunStatus.DEGRADED


# --------------------------------------------------------------------------- the chronic flag itself


def test_a_chronically_failing_lookup_is_ok_once_it_is_in_the_baseline() -> None:
    """The defect this whole change exists to remove: two poisoned terms, degraded for ever."""
    poisoned = ["album-search:Leopold Stokowski|", "album-search:'Carousel' 2018 Broadway Cast|"]
    current = observation(lidarr_metadata=poisoned, intents=["i-1"])
    old = baseline_of(current)

    delta, out = verdict(current, old)

    assert out.status is RunStatus.OK
    assert delta.totals["lidarr_metadata"] == 2
    assert delta.new["lidarr_metadata"] == 0


# --------------------------------------------------------------------------- class C: real outages


def test_a_spotify_schema_failure_degrades_with_nothing_new() -> None:
    current = observation(intents=["i-1"])
    _, out = verdict(current, baseline_of(current), spotify_schema_ok=False)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("spotify-schema",)


def test_a_musicbrainz_failure_degrades_with_nothing_new() -> None:
    current = observation(intents=["i-1"])
    _, out = verdict(current, baseline_of(current), mb_ok=False)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("mb-outage",)


def test_a_real_outage_degrades_even_on_a_first_run() -> None:
    """Rules 5 and 6 are not gated on the baseline: an outage on day one is still an outage."""
    _, out = verdict(observation(intents=["i-1"]), None, mb_ok=False, spotify_schema_ok=False)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("spotify-schema", "mb-outage")


# --------------------------------------------------------------------------- lidarr metadata outage (issue #18)


def test_lidarr_metadata_outage_needs_the_attempt_floor() -> None:
    """9 attempts, all failed, is still too few to call it an outage."""
    assert lidarr_metadata_outage(9, 9) is False


def test_lidarr_metadata_outage_needs_a_majority_to_fail() -> None:
    """10 attempts, exactly half failed, is not yet "more than half"."""
    half = int(LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS * LIDARR_METADATA_OUTAGE_RATIO)
    assert lidarr_metadata_outage(LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS, half) is False


def test_lidarr_metadata_outage_fires_once_both_bars_clear() -> None:
    assert lidarr_metadata_outage(LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS, 6) is True


def test_a_realistic_total_outage_trips_it() -> None:
    """The scenario from issue #18: ~98 attempted, all failed - the regression jump cannot see it."""
    assert lidarr_metadata_outage(98, 98) is True


def test_two_chronic_terms_never_look_like_an_outage() -> None:
    """The everyday shape (issue #18): 2 of ~80 lookups fail, every run, for ever."""
    assert lidarr_metadata_outage(80, 2) is False


def test_a_lidarr_metadata_outage_degrades_with_nothing_new() -> None:
    current = observation(intents=["i-1"])
    _, out = verdict(current, baseline_of(current), lidarr_outage=True)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("lidarr-metadata-outage",)


def test_a_lidarr_metadata_outage_degrades_even_on_a_first_run() -> None:
    """Class C, like rules 5 and 6: not gated on the baseline."""
    _, out = verdict(observation(intents=["i-1"]), None, lidarr_outage=True)

    assert out.status is RunStatus.DEGRADED
    assert out.conditions == ("lidarr-metadata-outage",)


# --------------------------------------------------------------------------- precedence


def test_guarded_wins_over_every_degrade_reason() -> None:
    """Exit 2 means unmonitors were refused, which is the more urgent thing to tell the operator."""
    old = baseline_of(observation(intents=["i-1"]))
    current = observation(name_collisions=["a|b"], skipped_artists=["x"], intents=["i-1"])

    _, out = verdict(current, old, guarded=True, mb_ok=False, spotify_schema_ok=False, lidarr_outage=True)

    assert out.status is RunStatus.GUARDED


def test_every_new_condition_is_named_in_a_stable_order() -> None:
    old = baseline_of(observation(intents=["i-1"]))
    current = observation(name_collisions=["a|b"], skipped_artists=["x"], catalogue_too_large=["y"], intents=["i-1"])

    _, out = verdict(current, old, mb_ok=False, lidarr_outage=True)

    assert out.conditions == (
        "mb-outage",
        "lidarr-metadata-outage",
        "new-skipped-artist",
        "new-catalogue-too-large",
        "new-name-collision",
    )


# --------------------------------------------------------------------------- unobserved is not empty


def test_a_dry_run_does_not_report_apply_only_dimensions_as_cleared() -> None:
    """A plan cannot see skipped artists. Absent because unobserved is not absent because fixed."""
    applied = observation(skipped_artists=["artist-1"], absent_in_lidarr=spread("a", 5), intents=["i-1"])
    old = baseline_of(applied)
    dry = observation(applied=False, intents=["i-1"])

    delta, out = verdict(dry, old)

    assert delta.resolved["skipped_artists"] == 0
    assert delta.new["skipped_artists"] == 0
    assert delta.totals["skipped_artists"] == 1, "the baseline's count stands in for what we cannot see"
    assert out.status is RunStatus.OK


# --------------------------------------------------------------------------- what the next baseline is


def test_class_a_advances_but_class_b_does_not_absorb_a_new_fault() -> None:
    """A new collision keeps degrading every run: amber here has no push, so one run is invisible."""
    old = baseline_of(observation(unmapped=spread("u", 5), name_collisions=["a|b"], intents=["i-1"]))
    current = observation(unmapped=spread("v", 5), name_collisions=["a|b", "c|d"], intents=["i-1"])

    written = next_baseline(current, old, FINGERPRINT, accept=False)

    assert written.identities["unmapped"] == frozenset(spread("v", 5))
    assert written.identities["name_collisions"] == frozenset({"a|b"})

    _, out = verdict(current, written)
    assert out.status is RunStatus.DEGRADED, "still degraded on the next run, until fixed or accepted"


def test_a_class_b_fault_that_heals_itself_clears_without_being_accepted() -> None:
    """The measured common case: a skip on one run, gone on the next. No human action needed."""
    old = baseline_of(observation(intents=["i-1"]))
    broken = observation(skipped_artists=["artist-1"], intents=["i-1"])
    after_break = next_baseline(broken, old, FINGERPRINT, accept=False)

    healed = observation(skipped_artists=[], intents=["i-1"])
    _, out = verdict(healed, after_break)

    assert out.status is RunStatus.OK


def test_accepting_folds_the_whole_observation_in_including_class_b() -> None:
    old = baseline_of(observation(intents=["i-1"]))
    current = observation(name_collisions=["c|d"], skipped_artists=["artist-1"], intents=["i-1"])

    accepted = next_baseline(current, old, FINGERPRINT, accept=True)

    _, out = verdict(current, accepted)
    assert out.status is RunStatus.OK


def test_an_unobserved_dimension_keeps_the_baseline_it_had() -> None:
    """A dry run never writes, but `--accept-health` on a plan must not wipe what it cannot see."""
    old = baseline_of(observation(skipped_artists=["artist-1"], intents=["i-1"]))
    dry = observation(applied=False, intents=["i-1"])

    written = next_baseline(dry, old, FINGERPRINT, accept=True)

    assert written.identities["skipped_artists"] == frozenset({"artist-1"})


def test_the_baseline_records_the_intent_set_for_the_next_run() -> None:
    current = observation(intents=["i-1", "i-2"])
    written = next_baseline(current, None, FINGERPRINT, accept=True)

    assert written.identities["intents"] == frozenset({"i-1", "i-2"})
    assert written.fingerprint == FINGERPRINT


def test_an_intent_the_caller_could_not_determine_is_not_counted_as_a_regression() -> None:
    """Only genuinely intent-less dimensions get the always-count exemption.

    An empty intent anywhere else means the shell failed to work out which intent was responsible,
    which is a defect in the caller. Counting it would turn that bug into a `degraded` run - the
    same false signal this module exists to remove - so it is treated conservatively.
    """
    intents = [f"i-{n}" for n in range(1000)]
    old = baseline_of(observation(intents=intents))
    current = observation(absent_in_lidarr={f"a-{n}": "" for n in range(70)}, intents=intents)

    delta, out = verdict(current, old)

    assert delta.new["absent_in_lidarr"] == 70, "still reported in full"
    assert delta.regressions == 0
    assert out.status is RunStatus.OK


# --------------------------------------------------------------------------- webhook notifications (#112)

OK, ERROR, GUARDED, DEGRADED, STALE = (
    RunStatus.OK,
    RunStatus.ERROR,
    RunStatus.GUARDED,
    RunStatus.DEGRADED,
    RunStatus.STALE,
)


def test_the_problem_statuses_are_exactly_error_guarded_degraded_and_stale() -> None:
    assert frozenset({ERROR, GUARDED, DEGRADED, STALE}) == PROBLEM_STATUSES


@pytest.mark.parametrize(
    ("status", "message", "previous", "expected"),
    [
        # A new problem after a clean run always notifies.
        (ERROR, "lidarr down", (OK, ""), True),
        (GUARDED, "shrank", (OK, ""), True),
        (DEGRADED, "1 new skipped artist", (OK, ""), True),
        (STALE, "re-plan", (OK, ""), True),
        # ...and so does one with nothing before it.
        (ERROR, "lidarr down", None, True),
        # The same status and message twice in a row notifies once.
        (ERROR, "lidarr down", (ERROR, "lidarr down"), False),
        (GUARDED, "shrank", (GUARDED, "shrank"), False),
        (DEGRADED, "x", (DEGRADED, "x"), False),
        (STALE, "re-plan", (STALE, "re-plan"), False),
        # A different message, or a different problem, is news.
        (ERROR, "spotify down", (ERROR, "lidarr down"), True),
        (GUARDED, "shrank", (ERROR, "shrank"), True),
        # Recovery notifies once, from any problem.
        (OK, "", (ERROR, "lidarr down"), True),
        (OK, "", (GUARDED, "shrank"), True),
        (OK, "", (DEGRADED, "x"), True),
        (OK, "", (STALE, "re-plan"), True),
        # A clean run after a clean run, or with nothing before it, never does.
        (OK, "", (OK, ""), False),
        (OK, "3 artists added", (OK, ""), False),
        (OK, "", None, False),
        # Paused and skipped never notify, whatever came before.
        (RunStatus.PAUSED, "paused", (ERROR, "lidarr down"), False),
        (RunStatus.PAUSED, "paused", None, False),
        (RunStatus.SKIPPED, "lock held", (OK, ""), False),
        (RunStatus.SKIPPED, "lock held", (ERROR, "lidarr down"), False),
    ],
)
def test_should_notify(status: RunStatus, message: str, previous: tuple[RunStatus, str] | None, expected: bool) -> None:
    assert should_notify(status, message, previous) is expected


@pytest.mark.parametrize(
    ("status", "recovered", "title", "kind"),
    [
        (ERROR, False, "likearr: run failed", "failure"),
        (GUARDED, False, "likearr: guard held back changes", "warning"),
        (DEGRADED, False, "likearr: new problems", "warning"),
        (STALE, False, "likearr: plan is stale", "failure"),
        (OK, True, "likearr: back to ok", "success"),
        (OK, False, "likearr: run ok", "success"),
        (RunStatus.PAUSED, False, "likearr: scheduled runs are paused", "info"),
        (RunStatus.SKIPPED, False, "likearr: scheduled run skipped", "info"),
    ],
)
def test_every_status_has_a_title_and_a_type(status: RunStatus, recovered: bool, title: str, kind: str) -> None:
    note = notification(status, "something happened", recovered=recovered)

    assert note.title == title
    assert note.type == kind
    assert note.body == "something happened"


@pytest.mark.parametrize("status", list(RunStatus))
@pytest.mark.parametrize("recovered", [True, False])
@pytest.mark.parametrize("message", ["", "   "])
def test_the_body_is_never_empty(status: RunStatus, recovered: bool, message: str) -> None:
    assert notification(status, message, recovered=recovered).body.strip()


def test_the_types_are_ones_apprise_accepts() -> None:
    """Apprise API refuses any `type` outside these four with a 400 (`apprise.NOTIFY_TYPES`)."""
    kinds = {notification(s, "", recovered=r).type for s in RunStatus for r in (True, False)}

    assert kinds <= {"info", "success", "warning", "failure"}
