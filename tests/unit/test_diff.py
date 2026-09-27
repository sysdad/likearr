"""Tests for the diff: the ownership boundary, the lost-reason rule, every guard, staleness."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from likearr.config import GuardsConfig
from likearr.core.diff import (
    CATALOGUE_GAP_STEP,
    RECENT_GAP_STEP,
    build_diff,
    config_changes,
    is_catalogue_gap,
    is_recent_catalogue_gap,
    is_stale,
    lidarr_digest,
)
from likearr.core.resolver import EXCLUDED_COMPILATION_STEP
from likearr.models import (
    DesiredRelease,
    DesiredState,
    OwnedArtist,
    Profile,
    Reason,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SecondaryType,
)
from tests.unit.fakes import (
    FULL_PROFILE_ID,
    LEAN_PROFILE_ID,
    NOW,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    owned,
    reason,
    rg,
)

GUARDS = GuardsConfig()
ARTIST = "artist-1"


def desired_state(
    *releases: tuple,
    artists: dict[str, str] | None = None,
    followed: set[str] | None = None,
    followed_counts: dict[str, int] | None = None,
    profile_needs: dict[str, Profile] | None = None,
    unmapped: list | None = None,
    catalogue_counts: dict[str, int] | None = None,
) -> DesiredState:
    """`releases` is a sequence of (ReleaseGroup, [Reason, ...]) pairs. `catalogue_counts` defaults
    to `followed_counts`: no filter of the user's own left anything out."""
    built: dict[ReleaseKey, DesiredRelease] = {}
    names: dict[str, str] = dict(artists or {})
    for release, reasons in releases:
        key = ReleaseKey(release.artist_mbid, release.mbid)
        built[key] = DesiredRelease(
            key=key,
            release_group=release,
            reasons=set(reasons),
            steps={r.key: "test" for r in reasons},
        )
        names.setdefault(release.artist_mbid, release.artist_name)
    needs = profile_needs or {m: Profile.LEAN for m in names}
    for entry in built.values():
        if entry.needs_full_profile:
            needs[entry.key.artist_mbid] = Profile.FULL
    return DesiredState(
        releases=built,
        artists=names,
        followed_artists=followed or set(),
        pending=[],
        unmapped=list(unmapped or []),
        profile_needs=needs,
        followed_counts=followed_counts or {},
        catalogue_counts=dict(followed_counts or {}) if catalogue_counts is None else catalogue_counts,
    )


def diff(
    desired: DesiredState,
    view,
    owned_map=None,
    owned_artists=None,
    *,
    last_source_counts=None,
    last_followed_counts=None,
    source_counts=None,
    live_reason_keys=None,
    guards: GuardsConfig = GUARDS,
    scheduled: bool = False,
    schema_ok: bool = True,
    accept_shrink: bool = False,
    source_digest: str = "src",
    lean: int | None = LEAN_PROFILE_ID,
    full: int | None = FULL_PROFILE_ID,
    recent_release_days: int = 60,
    max_refreshes: int = 10,
    last_gap_refreshes=None,
    gap_refresh_interval_hours: float = 24.0,
):
    return build_diff(
        desired,
        view,
        owned_map or {},
        owned_artists or {},
        last_source_counts=last_source_counts or {},
        last_followed_counts=last_followed_counts or {},
        source_counts=source_counts or {},
        live_reason_keys=set() if live_reason_keys is None else live_reason_keys,
        guards=guards,
        scheduled=scheduled,
        schema_ok=schema_ok,
        accept_shrink=accept_shrink,
        now=NOW,
        source_digest=source_digest,
        lean_profile_id=lean,
        full_profile_id=full,
        recent_release_days=recent_release_days,
        max_refreshes=max_refreshes,
        last_gap_refreshes=last_gap_refreshes,
        gap_refresh_interval_hours=gap_refresh_interval_hours,
    )


LIKED = reason(ReasonKind.LIKED, "t1")
SAVED = reason(ReasonKind.SAVED, "al1")
FOLLOWED = reason(ReasonKind.FOLLOWED, "ar1")


# --------------------------------------------------------------------------- adds and monitors


def test_an_artist_not_in_lidarr_is_added_with_the_profile_they_need() -> None:
    album = rg("rg-1", "Record")
    result = diff(desired_state((album, [SAVED])), lidarr_view())
    assert [a.artist_mbid for a in result.add_artists] == [ARTIST]
    assert result.add_artists[0].profile is Profile.LEAN


def test_an_artist_needing_a_non_studio_release_is_added_as_full() -> None:
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    result = diff(desired_state((comp, [LIKED])), lidarr_view())
    assert result.add_artists[0].profile is Profile.FULL


def test_an_unmonitored_album_that_is_wanted_is_monitored() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=False)])
    result = diff(desired_state((album, [SAVED])), view)
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-1"]
    assert result.monitor[0].reasons == frozenset({SAVED})
    assert not result.add_artists


def test_an_already_monitored_album_produces_nothing() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    result = diff(desired_state((album, [SAVED])), view)
    assert not result.monitor
    assert result.is_empty


def test_a_release_for_an_artist_not_yet_in_lidarr_still_shows_as_intent() -> None:
    """apply does add -> refresh -> monitor; the diff must show what it will end up doing."""
    album = rg("rg-1", "Record")
    result = diff(desired_state((album, [SAVED])), lidarr_view())
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-1"]


def test_a_release_group_lidarr_does_not_know_is_reported_not_monitored() -> None:
    album = rg("rg-1", "Record")
    other = rg("rg-2", "Other Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(other)])
    result = diff(desired_state((album, [SAVED])), view)
    assert not result.monitor
    reported = [u for u in result.unmapped if u.step == "lidarr:missing-release-group"]
    assert len(reported) == 1
    assert "rg-1" in reported[0].detail


def test_a_followed_only_catalogue_gap_is_informational_not_unmapped() -> None:
    """Promos/bootlegs MusicBrainz lists but Lidarr never tracks must not hold health at amber."""
    album = rg("rg-1", "Promo Sampler")
    other = rg("rg-2", "Other Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(other)])
    result = diff(desired_state((album, [FOLLOWED])), view)
    assert not result.monitor
    assert [u.step for u in result.unmapped] == [CATALOGUE_GAP_STEP]
    assert all(is_catalogue_gap(u) for u in result.unmapped)
    assert result.refresh_artists == [], "an old promo is not waiting on Lidarr's refresh"


# --------------------------------------------------------- recent catalogue gaps (issue #8)


def _gap_view():
    """Lidarr has the artist and one unrelated album, so anything else is a catalogue gap."""
    return lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(rg("rg-other", "Other Record"))])


def test_a_recently_released_catalogue_gap_is_reported_on_its_own_and_refreshes_the_artist() -> None:
    """The new-album case: Lidarr's own refresh has not caught up, and nothing said so."""
    fresh = rg("rg-new", "Brand New", released="2026-09-11")  # a week before NOW
    result = diff(desired_state((fresh, [FOLLOWED])), _gap_view())
    assert [u.step for u in result.unmapped] == [RECENT_GAP_STEP]
    assert all(is_catalogue_gap(u) for u in result.unmapped), "still informational, never unmapped"
    assert all(is_recent_catalogue_gap(u) for u in result.unmapped)
    assert result.refresh_artists == [ARTIST]
    assert not result.is_empty, "a plan that only refreshes is still a plan"


def test_a_future_dated_catalogue_gap_is_recent_too() -> None:
    """An announced album Lidarr has not got yet is the same problem, earlier."""
    announced = rg("rg-soon", "Coming Soon", released="2026-12-01")
    result = diff(desired_state((announced, [FOLLOWED])), _gap_view())
    assert [u.step for u in result.unmapped] == [RECENT_GAP_STEP]
    assert result.refresh_artists == [ARTIST]


def test_a_gap_just_past_the_window_is_an_ordinary_gap() -> None:
    stale = rg("rg-old", "Old Promo", released="2026-07-01")  # 79 days before NOW
    result = diff(desired_state((stale, [FOLLOWED])), _gap_view())
    assert [u.step for u in result.unmapped] == [CATALOGUE_GAP_STEP]
    assert result.refresh_artists == []


def test_a_gap_with_no_release_date_is_never_treated_as_recent() -> None:
    """MusicBrainz leaves promos and bootlegs undated; guessing 'new' would refresh for ever."""
    undated = rg("rg-undated", "Untitled", released=None)
    result = diff(desired_state((undated, [FOLLOWED])), _gap_view())
    assert [u.step for u in result.unmapped] == [CATALOGUE_GAP_STEP]
    assert result.refresh_artists == []


def test_a_recent_gap_for_an_artist_not_in_lidarr_is_not_refreshed() -> None:
    """There is nothing to refresh: the artist is being added, and the add refreshes anyway."""
    fresh = rg("rg-new", "Brand New", released="2026-09-11")
    result = diff(desired_state((fresh, [FOLLOWED])), lidarr_view())
    assert result.refresh_artists == []
    assert [a.artist_mbid for a in result.add_artists] == [ARTIST]


def _many_gap_artists(count: int = 5):
    """`count` followed artists, each with one recent gap, all already in Lidarr."""
    groups = [
        rg(f"rg-{i}", f"New {i}", artist_mbid=f"artist-{i}", artist_name=f"Artist {i}", released=f"2026-09-{i + 1:02d}")
        for i in range(count)
    ]
    view = lidarr_view(artists=[lidarr_artist(g.artist_mbid, id=i + 1) for i, g in enumerate(groups)])
    for artist_mbid in (g.artist_mbid for g in groups):
        view.albums[artist_mbid] = {}
    return groups, view


def test_refreshes_are_capped_per_run_newest_release_first() -> None:
    """A cap keeps one run from queueing hundreds of Lidarr commands; the rest wait a run."""
    groups, view = _many_gap_artists()
    result = diff(desired_state(*((g, [FOLLOWED]) for g in groups)), view, max_refreshes=2)
    assert result.refresh_artists == ["artist-4", "artist-3"], "the freshest releases first"


def test_an_artist_refreshed_recently_is_not_refreshed_again() -> None:
    """MusicBrainz dates promos Lidarr will never carry; without a backoff that is 4 asks a day."""
    fresh = rg("rg-new", "Brand New", released="2026-09-11")
    just_now = NOW - timedelta(hours=2)
    result = diff(desired_state((fresh, [FOLLOWED])), _gap_view(), last_gap_refreshes={ARTIST: just_now})

    assert result.refresh_artists == []
    assert [u.step for u in result.unmapped] == [RECENT_GAP_STEP], "still reported, just not asked again"


def test_the_backoff_expires_and_the_artist_is_refreshed_again() -> None:
    fresh = rg("rg-new", "Brand New", released="2026-09-11")
    long_ago = NOW - timedelta(hours=25)
    result = diff(desired_state((fresh, [FOLLOWED])), _gap_view(), last_gap_refreshes={ARTIST: long_ago})

    assert result.refresh_artists == [ARTIST]


def test_the_backoff_interval_is_configurable() -> None:
    fresh = rg("rg-new", "Brand New", released="2026-09-11")
    result = diff(
        desired_state((fresh, [FOLLOWED])),
        _gap_view(),
        last_gap_refreshes={ARTIST: NOW - timedelta(hours=2)},
        gap_refresh_interval_hours=1.0,
    )
    assert result.refresh_artists == [ARTIST]


def test_the_cap_takes_the_artist_we_have_left_alone_longest() -> None:
    """Otherwise the freshest release starves everyone behind a permanently stuck gap."""
    groups, view = _many_gap_artists()
    last = {
        "artist-4": NOW - timedelta(days=30),  # freshest release, but asked most recently
        "artist-3": NOW - timedelta(days=90),
        "artist-2": NOW - timedelta(days=60),
    }
    result = diff(desired_state(*((g, [FOLLOWED]) for g in groups)), view, max_refreshes=3, last_gap_refreshes=last)

    assert result.refresh_artists[:2] == ["artist-1", "artist-0"], "never asked at all comes first"
    assert result.refresh_artists[2] == "artist-3", "then the longest since we last asked"


def test_a_refresh_is_part_of_the_lidarr_digest() -> None:
    """The reviewed apply must refuse a plan whose refresh target has moved."""
    fresh = rg("rg-new", "Brand New", released="2026-09-11")
    result = diff(desired_state((fresh, [FOLLOWED])), _gap_view())
    moved = lidarr_view(artists=[lidarr_artist(ARTIST, id=999)], albums=[lidarr_album(rg("rg-other", "Other Record"))])
    now = lidarr_digest(
        moved,
        result.monitor,
        result.unmonitor,
        result.ratchets,
        result.set_new_items_none,
        result.monitor_artists,
        result.refresh_artists,
    )
    assert is_stale(result, "src", now) is True


# --------------------------------------------------------------------------- the ownership boundary


def test_an_unowned_monitored_release_is_never_unmonitored() -> None:
    """Everything monitored before likearr existed is invisible to it until adopt runs."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    result = diff(desired_state(), view)
    assert not result.unmonitor


def test_a_manual_release_is_never_unmonitored() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, Reason(kind=ReasonKind.MANUAL, source_id="adopt"))
    result = diff(desired_state(), view, {key: record})
    assert not result.unmonitor


def test_a_manual_release_keeps_its_manual_reason_when_a_source_also_wants_it() -> None:
    """Kept by hand at adoption, now also liked: the like joins the manual reason, never replaces it."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    manual = Reason(kind=ReasonKind.MANUAL, source_id="adopt")
    key, record = owned(album, manual)

    result = diff(desired_state((album, [LIKED])), view, {key: record})

    assert result.update_reasons == [(key, frozenset({manual, LIKED}))]


def test_a_manual_release_with_its_source_reason_already_recorded_needs_no_update() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    manual = Reason(kind=ReasonKind.MANUAL, source_id="adopt")
    key, record = owned(album, manual, LIKED)

    assert diff(desired_state((album, [LIKED])), view, {key: record}).update_reasons == []


def test_an_owned_release_with_no_remaining_reason_is_unmonitored() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED)
    result = diff(desired_state(), view, {key: record})
    assert [u.key.rg_mbid for u in result.unmonitor] == ["rg-1"]
    assert result.unmonitor[0].lost_reasons == frozenset({LIKED})


def test_an_owned_release_that_is_not_monitored_in_lidarr_is_left_alone() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=False)])
    key, record = owned(album, LIKED)
    assert not diff(desired_state(), view, {key: record}).unmonitor


def test_one_surviving_reason_keeps_a_release_monitored() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED, SAVED)
    result = diff(desired_state((album, [SAVED])), view, {key: record})
    assert not result.unmonitor


# --------------------------------------------------------------------------- the lost-reason rule


def test_a_reason_still_in_the_source_is_not_lost_when_it_fails_to_resolve() -> None:
    """A liked track whose MusicBrainz lookup failed has not been unliked."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED)
    result = diff(desired_state(), view, {key: record}, live_reason_keys={LIKED.key})
    assert not result.unmonitor


def test_a_reason_that_left_the_source_is_lost() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED)
    result = diff(desired_state(), view, {key: record}, live_reason_keys={SAVED.key})
    assert len(result.unmonitor) == 1


def test_a_partially_live_reason_set_blocks_the_unmonitor() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED, SAVED)
    result = diff(desired_state(), view, {key: record}, live_reason_keys={SAVED.key})
    assert not result.unmonitor


# ------------------------------------------- the one exception: an opted-out intent (#15)


def _excluded(intent_key: str) -> Resolution:
    return Resolution(
        intent_key=intent_key,
        status=ResolutionStatus.UNMAPPED,
        step=EXCLUDED_COMPILATION_STEP,
        detail="[rules] allow_compilation_fallback is off",
    )


def test_an_opted_out_intent_releases_the_box_set_it_was_holding() -> None:
    """Without this the opt-out is inert: the song is still liked, so the reason stays live.

    The user would read an empty `unmonitor` list, conclude the setting did nothing, and the box
    set would keep its monitor for ever.
    """
    box = rg("rg-1", "The Complete Decca Masters")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(box, monitored=True)])
    key, record = owned(box, LIKED)

    result = diff(
        desired_state(unmapped=[_excluded(LIKED.key)]),
        view,
        {key: record},
        live_reason_keys={LIKED.key},
    )

    assert [u.key for u in result.unmonitor] == [key]
    assert result.unmonitor[0].lost_reasons == frozenset({LIKED})


def test_an_ordinary_unmapped_intent_still_holds_its_release() -> None:
    """The contrast that makes the exception safe: a lookup failure is not a decision."""
    box = rg("rg-1", "The Complete Decca Masters")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(box, monitored=True)])
    key, record = owned(box, LIKED)
    failed = Resolution(intent_key=LIKED.key, status=ResolutionStatus.UNMAPPED, step="error:metadata")

    result = diff(desired_state(unmapped=[failed]), view, {key: record}, live_reason_keys={LIKED.key})

    assert not result.unmonitor


def test_a_release_another_live_reason_still_wants_is_kept() -> None:
    """Opting a like out must not take a release the user separately saved."""
    box = rg("rg-1", "The Complete Decca Masters")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(box, monitored=True)])
    key, record = owned(box, LIKED, SAVED)

    result = diff(
        desired_state(unmapped=[_excluded(LIKED.key)]),
        view,
        {key: record},
        live_reason_keys={LIKED.key, SAVED.key},
    )

    assert not result.unmonitor


def test_an_opted_out_intent_is_still_reported_as_unmapped() -> None:
    """It never silently vanishes: dropping the reason and reporting the track are separate."""
    box = rg("rg-1", "The Complete Decca Masters")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(box, monitored=True)])
    key, record = owned(box, LIKED)

    result = diff(
        desired_state(unmapped=[_excluded(LIKED.key)]),
        view,
        {key: record},
        live_reason_keys={LIKED.key},
    )

    assert [u.step for u in result.unmapped] == [EXCLUDED_COMPILATION_STEP]


# --------------------------------------------------------------------------- reason updates


def test_a_changed_reason_set_on_a_still_wanted_release_is_recorded() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED)
    result = diff(desired_state((album, [LIKED, SAVED])), view, {key: record})
    assert result.update_reasons == [(key, frozenset({LIKED, SAVED}))]
    assert not result.monitor
    assert not result.unmonitor


def test_an_unchanged_reason_set_is_not_recorded() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, LIKED)
    assert diff(desired_state((album, [LIKED])), view, {key: record}).update_reasons == []


# A release still desired under a wholly different reason set is re-tagged, never unmonitored
# (#69: AURORA, followed -> saved, was planned as both an update_reasons and an unmonitor).
_SWAPS = [
    pytest.param(FOLLOWED, SAVED, id="followed-to-saved"),
    pytest.param(LIKED, reason(ReasonKind.PLAYLIST, "t1", playlist_id="pl-a"), id="liked-to-playlist"),
    pytest.param(
        reason(ReasonKind.PLAYLIST, "t1", playlist_id="pl-a"),
        reason(ReasonKind.PLAYLIST, "t1", playlist_id="pl-b"),
        id="playlist-a-to-playlist-b",
    ),
]


@pytest.mark.parametrize(("was", "now"), _SWAPS)
def test_a_still_desired_release_whose_reasons_all_changed_is_not_unmonitored(was: Reason, now: Reason) -> None:
    album = rg("rg-1", "AURORA")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, was)
    result = diff(desired_state((album, [now])), view, {key: record}, live_reason_keys={now.key})
    assert result.unmonitor == []
    assert result.update_reasons == [(key, frozenset({now}))]
    assert not result.monitor


@pytest.mark.parametrize(("was", "now"), _SWAPS)
def test_a_release_no_longer_desired_is_still_let_go_when_its_reason_moved(was: Reason, now: Reason) -> None:
    """The regression guard for #69: only a desired release is exempt. The old release is not."""
    old, new = rg("rg-old", "Old"), rg("rg-new", "New")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[lidarr_album(old, id=1, monitored=True), lidarr_album(new, id=2, monitored=True)],
    )
    key, record = owned(old, was)
    result = diff(desired_state((new, [now, was])), view, {key: record}, live_reason_keys={now.key, was.key})
    assert [u.key.rg_mbid for u in result.unmonitor] == ["rg-old"]
    assert result.unmonitor[0].lost_reasons == frozenset({was})


# --------------------------------------------------------------------------- ratchets and new items


def test_a_full_artist_on_the_lean_profile_is_ratcheted() -> None:
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, metadata_profile_id=LEAN_PROFILE_ID)],
        albums=[lidarr_album(comp, monitored=True)],
    )
    result = diff(desired_state((comp, [LIKED])), view)
    assert [r.artist_mbid for r in result.ratchets] == [ARTIST]
    assert result.ratchets[0].to_profile is Profile.FULL
    assert "Hits" in result.ratchets[0].because


def test_an_artist_already_on_full_is_not_ratcheted_again() -> None:
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, metadata_profile_id=FULL_PROFILE_ID)],
        albums=[lidarr_album(comp, monitored=True)],
    )
    assert not diff(desired_state((comp, [LIKED])), view).ratchets


def test_a_lean_artist_is_never_downgraded_from_full() -> None:
    """The ratchet is one-way; nothing in the diff can move an artist back to Lean."""
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, metadata_profile_id=FULL_PROFILE_ID)],
        albums=[lidarr_album(album, monitored=True)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert not result.ratchets


def test_ratchets_are_skipped_when_the_profile_ids_are_unknown() -> None:
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(comp, monitored=True)])
    assert not diff(desired_state((comp, [LIKED])), view, lean=None).ratchets


def test_a_hand_managed_artist_whose_wanted_album_is_already_monitored_keeps_its_monitor_new_items() -> None:
    """#172: a saved album the user already monitors by hand gives likearr nothing to own under the
    artist, so its "Monitor New Albums" setting stays the user's."""
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="all")],
        albums=[lidarr_album(album, monitored=True)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert result.set_new_items_none == []
    assert result.is_empty


@pytest.mark.parametrize(
    "held",
    [SAVED, reason(ReasonKind.MANUAL, "hand")],
    ids=["claimed-with-its-reason", "kept-as-manual"],
)
def test_monitor_new_items_is_set_to_none_on_an_artist_holding_an_owned_release(held: Reason) -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="all")],
        albums=[lidarr_album(album, monitored=True)],
    )
    key, record = owned(album, held)
    wanted = desired_state((album, [held])) if held is SAVED else desired_state()
    result = diff(wanted, view, {key: record}, live_reason_keys={held.key})
    assert result.set_new_items_none == [ARTIST]


def test_monitor_new_items_is_set_to_none_on_an_artist_likearr_added() -> None:
    view = lidarr_view(artists=[lidarr_artist(ARTIST, monitor_new_items="new")])
    added = {ARTIST: OwnedArtist(artist_mbid=ARTIST, lidarr_artist_id=1, added_by_us=True, profile=Profile.LEAN)}
    assert diff(desired_state(), view, owned_artists=added).set_new_items_none == [ARTIST]


def test_monitor_new_items_is_set_to_none_on_an_artist_whose_release_is_claimed_this_run() -> None:
    """The claim and the write land in one run, so the plan after the apply is empty."""
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="all")],
        albums=[lidarr_album(album, monitored=False)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert [m.key.artist_mbid for m in result.monitor] == [ARTIST]
    assert result.set_new_items_none == [ARTIST]


def test_monitor_new_items_is_set_to_none_on_an_artist_whose_profile_is_widened() -> None:
    """Apply refreshes a ratcheted artist; left on "all", Lidarr would monitor every release type
    the Full profile shows. Nothing is owned or claimed here: the ratchet alone puts it in."""
    studio = rg("rg-1", "Record")
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="all", metadata_profile_id=LEAN_PROFILE_ID)],
        albums=[lidarr_album(studio, monitored=True)],
    )
    result = diff(desired_state((comp, [LIKED])), view)
    assert [r.artist_mbid for r in result.ratchets] == [ARTIST]
    assert result.monitor == []
    assert result.set_new_items_none == [ARTIST]


def test_an_artist_already_on_none_is_never_listed() -> None:
    album = rg("rg-1", "Record")
    comp = rg("rg-c", "Hits", secondary=[SecondaryType.COMPILATION])
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="none", metadata_profile_id=LEAN_PROFILE_ID)],
        albums=[lidarr_album(album, monitored=False)],
    )
    key, record = owned(album, SAVED)
    added = {ARTIST: OwnedArtist(artist_mbid=ARTIST, lidarr_artist_id=1, added_by_us=True, profile=Profile.LEAN)}
    result = diff(desired_state((album, [SAVED]), (comp, [LIKED])), view, {key: record}, added)
    assert result.monitor and result.ratchets, "every reason to list the artist is there"
    assert result.set_new_items_none == []


def test_monitor_new_items_is_set_to_none_on_an_artist_likearr_re_monitors() -> None:
    """#172: turning an unmonitored artist back on is likearr's own action, so it must not
    let Lidarr start auto-monitoring that artist's future albums, even with nothing claimed there."""
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitored=False, monitor_new_items="all")],
        albums=[lidarr_album(album, monitored=True)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert result.monitor_artists == [ARTIST]
    assert result.set_new_items_none == [ARTIST]


def test_monitor_new_items_is_left_alone_on_unrelated_artists() -> None:
    other = lidarr_artist("artist-9", id=9, monitor_new_items="all")
    view = lidarr_view(artists=[other])
    assert diff(desired_state(), view).set_new_items_none == []


# --------------------------------------------------------------------------- unmonitored artists


def test_an_unmonitored_artist_holding_a_desired_release_is_remonitored() -> None:
    """Lidarr never searches or lists as wanted an album whose artist is unmonitored."""
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitored=False)],
        albums=[lidarr_album(album, monitored=True)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert result.monitor_artists == [ARTIST]
    assert not result.is_empty


def test_an_unmonitored_artist_is_remonitored_even_when_the_album_needs_monitoring_too() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitored=False)],
        albums=[lidarr_album(album, monitored=False)],
    )
    result = diff(desired_state((album, [SAVED])), view)
    assert result.monitor_artists == [ARTIST]
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-1"]


def test_a_monitored_artist_is_left_alone() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    assert diff(desired_state((album, [SAVED])), view).monitor_artists == []


def test_an_unmonitored_artist_with_nothing_desired_is_left_alone() -> None:
    """A human unmonitored them and likearr wants nothing from them: not likearr's call."""
    view = lidarr_view(artists=[lidarr_artist(ARTIST, monitored=False)])
    assert diff(desired_state(), view).monitor_artists == []


def test_remonitoring_an_artist_is_part_of_the_lidarr_digest() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST, monitored=False)], albums=[lidarr_album(album, monitored=True)])
    result = diff(desired_state((album, [SAVED])), view)
    fixed = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    now = lidarr_digest(
        fixed, result.monitor, result.unmonitor, result.ratchets, result.set_new_items_none, result.monitor_artists
    )
    assert is_stale(result, "src", now) is True


def _digest_now(result, view) -> str:
    """`result`'s Lidarr digest recomputed against `view`, the way a reviewed apply does."""
    return lidarr_digest(
        view,
        result.monitor,
        result.unmonitor,
        result.ratchets,
        result.set_new_items_none,
        result.monitor_artists,
        result.refresh_artists,
        [a.artist_mbid for a in result.add_artists],
    )


def test_an_artist_to_add_appearing_in_lidarr_makes_the_diff_stale() -> None:
    """Issue #4: someone added the artist by hand after the plan. Applying it would record their
    artist as likearr's, so the reviewed plan must be refused and re-planned instead."""
    album = rg("rg-1", "Record")
    result = diff(desired_state((album, [SAVED])), lidarr_view())
    assert [a.artist_mbid for a in result.add_artists] == [ARTIST]

    appeared = lidarr_view(artists=[lidarr_artist(ARTIST)])
    assert is_stale(result, "src", _digest_now(result, lidarr_view())) is False
    assert is_stale(result, "src", _digest_now(result, appeared)) is True


def test_covering_the_adds_leaves_every_digest_as_it_was_at_plan_time() -> None:
    """An artist to add is absent when planned, so it hashes nothing: a plan with adds digests
    exactly as it did before adds were covered, and so does one without, so a reviewed plan made
    by an earlier likearr is not refused as stale merely for the upgrade."""
    album = rg("rg-1", "Record")
    kept = rg("rg-2", "Kept", artist_mbid="artist-2", artist_name="Other")
    view = lidarr_view(artists=[lidarr_artist("artist-2")], albums=[lidarr_album(kept, monitored=False)])
    for desired in (desired_state((album, [SAVED]), (kept, [SAVED])), desired_state((kept, [SAVED]))):
        result = diff(desired, view)
        before = lidarr_digest(
            view,
            result.monitor,
            result.unmonitor,
            result.ratchets,
            result.set_new_items_none,
            result.monitor_artists,
            result.refresh_artists,
        )
        assert result.lidarr_digest == before


# --------------------------------------------------------------------------- guards


def _unmonitor_setup(count: int):
    """`count` owned, monitored releases that nothing wants any more."""
    groups = [rg(f"rg-{i}", f"Record {i}") for i in range(count)]
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[lidarr_album(g, id=100 + i, monitored=True) for i, g in enumerate(groups)],
    )
    owned_map = {}
    for i, g in enumerate(groups):
        key, record = owned(g, reason(ReasonKind.LIKED, f"t{i}"), album_id=100 + i)
        owned_map[key] = record
    return groups, view, owned_map


def test_the_schema_guard_blocks_every_unmonitor() -> None:
    _, view, owned_map = _unmonitor_setup(3)
    result = diff(desired_state(), view, owned_map, schema_ok=False)
    assert not result.unmonitor
    assert [g.code for g in result.guards] == ["schema"]
    assert result.guards[0].blocked_unmonitors == 3
    assert result.guarded is True


def test_a_short_spotify_read_refuses_every_unmonitor_but_adds_and_monitors_still_apply() -> None:
    """#176: a read that fell short of Spotify's reported total arrives as `schema_ok=False`.

    The likes it missed look like un-likes, so every unmonitor is refused, from every source, not
    only the short one. What the read did return is still acted on.
    """
    groups, view, owned_map = _unmonitor_setup(2)
    wanted = rg("rg-wanted", "Still Liked")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[
            *(lidarr_album(g, id=100 + i, monitored=True) for i, g in enumerate(groups)),
            lidarr_album(wanted, id=200),
        ],
    )
    new_artist = rg("rg-new", "New Artist Record", artist_mbid="artist-2", artist_name="New Artist")
    desired = desired_state((wanted, [SAVED]), (new_artist, [FOLLOWED]))

    result = diff(desired, view, owned_map, schema_ok=False)

    assert not result.unmonitor
    assert [g.code for g in result.guards] == ["schema"]
    assert result.guards[0].blocked_unmonitors == 2
    assert sorted(m.key.rg_mbid for m in result.monitor) == ["rg-new", "rg-wanted"]
    assert [a.artist_mbid for a in result.add_artists] == ["artist-2"]


def test_the_source_shrink_guard_blocks_only_that_sources_unmonitors() -> None:
    _, view, owned_map = _unmonitor_setup(2)
    # re-own the second release under a saved-album reason so the two sources differ
    key = ReleaseKey(ARTIST, "rg-1")
    owned_map[key] = replace(owned_map[key], reasons=frozenset({SAVED}))
    result = diff(
        desired_state(),
        view,
        owned_map,
        last_source_counts={"liked_tracks": 100, "saved_albums": 10},
        source_counts={"liked_tracks": 50, "saved_albums": 10},
    )
    assert [u.key.rg_mbid for u in result.unmonitor] == ["rg-1"]
    assert [g.code for g in result.guards] == ["source-shrink"]
    assert result.guards[0].blocked_unmonitors == 1
    assert "liked_tracks" in result.guards[0].message


def test_a_source_shrink_inside_the_limit_blocks_nothing() -> None:
    _, view, owned_map = _unmonitor_setup(1)
    result = diff(
        desired_state(),
        view,
        owned_map,
        last_source_counts={"liked_tracks": 100},
        source_counts={"liked_tracks": 95},
    )
    assert len(result.unmonitor) == 1
    assert not result.guards


def test_a_source_with_no_previous_count_cannot_shrink() -> None:
    """A first run has no baseline, so the guard must not fire on every unmonitor."""
    _, view, owned_map = _unmonitor_setup(1)
    result = diff(
        desired_state(),
        view,
        owned_map,
        last_source_counts={"liked_tracks": 0},
        source_counts={"liked_tracks": 0},
    )
    assert len(result.unmonitor) == 1
    assert not result.guards


def test_the_playlist_shrink_guard_is_keyed_per_playlist() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, reason(ReasonKind.PLAYLIST, "t1", playlist_id="pl-A"))
    result = diff(
        desired_state(),
        view,
        {key: record},
        last_source_counts={"playlist:pl-A": 50, "playlist:pl-B": 50},
        source_counts={"playlist:pl-A": 1, "playlist:pl-B": 50},
    )
    assert not result.unmonitor
    assert result.guards[0].code == "source-shrink"
    assert "playlist:pl-A" in result.guards[0].message


def test_the_artist_shrink_guard_blocks_that_artists_unmonitors() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(
        desired_state(followed_counts={ARTIST: 2}, followed={ARTIST}, artists={ARTIST: "Test Artist"}),
        view,
        {key: record},
        last_followed_counts={ARTIST: 10},
    )
    assert not result.unmonitor
    assert [g.code for g in result.guards] == ["artist-shrink"]
    assert result.guards[0].blocked_unmonitors == 1


def test_a_denied_release_of_a_followed_artist_is_not_an_artist_shrink() -> None:
    """Denying one of three releases is the user's filter, not a catalogue that shrank: the guard
    compares the catalogue before the user's own filters, and the denied release is let go."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(
        desired_state(
            followed_counts={ARTIST: 2},
            catalogue_counts={ARTIST: 3},
            followed={ARTIST},
            artists={ARTIST: "Test Artist"},
        ),
        view,
        {key: record},
        last_followed_counts={ARTIST: 3},
        live_reason_keys=set(),
    )
    assert [u.key for u in result.unmonitor] == [key]
    assert not result.guards


def test_an_unfollowed_artist_is_not_an_artist_shrink() -> None:
    """Absent from the followed source is an unfollow, not a catalogue that shrank N -> 0."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(desired_state(), view, {key: record}, last_followed_counts={ARTIST: 10})
    assert [u.key for u in result.unmonitor] == [key]
    assert not result.guards


def test_a_still_followed_artist_whose_catalogue_could_not_be_read_is_still_guarded() -> None:
    """Followed but with no count (the catalogue lookup failed): not an unfollow, so still held."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(
        desired_state(followed={ARTIST}, artists={ARTIST: "Test Artist"}),
        view,
        {key: record},
        last_followed_counts={ARTIST: 10},
    )
    assert not result.unmonitor
    assert [g.code for g in result.guards] == ["artist-shrink"]


def test_accept_shrink_lets_a_source_shrink_through_and_says_so() -> None:
    _, view, owned_map = _unmonitor_setup(1)
    result = diff(
        desired_state(),
        view,
        owned_map,
        last_source_counts={"liked_tracks": 100},
        source_counts={"liked_tracks": 50},
        accept_shrink=True,
    )
    assert len(result.unmonitor) == 1
    assert not result.guards
    assert result.accept_shrink is True


def test_accept_shrink_lets_an_artist_shrink_through() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(
        desired_state(followed_counts={ARTIST: 2}, followed={ARTIST}, artists={ARTIST: "Test Artist"}),
        view,
        {key: record},
        last_followed_counts={ARTIST: 10},
        accept_shrink=True,
    )
    assert len(result.unmonitor) == 1
    assert not result.guards


def test_accept_shrink_does_not_lift_the_schema_guard_or_the_cap() -> None:
    _, view, owned_map = _unmonitor_setup(2)
    result = diff(desired_state(), view, owned_map, schema_ok=False, accept_shrink=True)
    assert [g.code for g in result.guards] == ["schema"]
    assert not result.unmonitor


def test_an_artist_shrink_inside_the_limit_blocks_nothing() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    key, record = owned(album, FOLLOWED)
    result = diff(
        desired_state(followed_counts={ARTIST: 9}, artists={ARTIST: "Test Artist"}),
        view,
        {key: record},
        last_followed_counts={ARTIST: 10},
    )
    assert len(result.unmonitor) == 1
    assert not result.guards


def test_the_scheduled_cap_blocks_every_unmonitor() -> None:
    _, view, owned_map = _unmonitor_setup(5)
    guards = GuardsConfig(max_unmonitors_scheduled=3)
    result = diff(desired_state(), view, owned_map, guards=guards, scheduled=True)
    assert not result.unmonitor
    assert [g.code for g in result.guards] == ["scheduled-cap"]
    assert result.guards[0].blocked_unmonitors == 5


def test_the_scheduled_cap_does_not_apply_to_an_interactive_run() -> None:
    _, view, owned_map = _unmonitor_setup(5)
    guards = GuardsConfig(max_unmonitors_scheduled=3)
    result = diff(desired_state(), view, owned_map, guards=guards, scheduled=False)
    assert len(result.unmonitor) == 5
    assert not result.guards


def test_a_scheduled_run_under_the_cap_proceeds() -> None:
    _, view, owned_map = _unmonitor_setup(2)
    guards = GuardsConfig(max_unmonitors_scheduled=3)
    result = diff(desired_state(), view, owned_map, guards=guards, scheduled=True)
    assert len(result.unmonitor) == 2
    assert not result.guards


def test_the_projected_wanted_guard_is_informational_only() -> None:
    groups = [rg(f"rg-{i}", f"Record {i}") for i in range(4)]
    view = lidarr_view()  # the artist is not in Lidarr yet, so every release would be wanted
    guards = GuardsConfig(projected_wanted_max=2)
    result = diff(desired_state(*[(g, [SAVED]) for g in groups]), view, guards=guards)
    assert result.projected_wanted == 4
    assert [g.code for g in result.guards] == ["projected-wanted"]
    assert result.guards[0].blocked_unmonitors == 0
    assert result.guarded is False, "an informational guard must not mark the run guarded"


def test_guards_stack_and_each_reports_what_it_blocked() -> None:
    _, view, owned_map = _unmonitor_setup(4)
    guards = GuardsConfig(max_unmonitors_scheduled=1)
    result = diff(
        desired_state(),
        view,
        owned_map,
        guards=guards,
        scheduled=True,
        last_source_counts={"liked_tracks": 100},
        source_counts={"liked_tracks": 1},
    )
    assert not result.unmonitor
    codes = [g.code for g in result.guards]
    assert codes == ["source-shrink"], "the first guard took them all, so the cap had nothing left"
    assert result.guards[0].blocked_unmonitors == 4


# --------------------------------------------------------------------------- projected wanted


def test_projected_wanted_counts_monitored_releases_with_no_files() -> None:
    with_files = rg("rg-1", "Have It")
    without = rg("rg-2", "Want It")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[
            lidarr_album(with_files, id=101, monitored=True, files=10),
            lidarr_album(without, id=102, monitored=True, files=0),
        ],
    )
    result = diff(desired_state((with_files, [SAVED]), (without, [SAVED])), view)
    assert result.projected_wanted == 1


def test_a_release_reported_as_missing_in_lidarr_is_not_counted_as_wanted() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(rg("rg-2", "Other"))])
    result = diff(desired_state((album, [SAVED])), view)
    assert result.projected_wanted == 0


def test_an_artist_whose_albums_were_loaded_but_is_empty_reports_rather_than_monitors() -> None:
    """An empty album map means "loaded, and Lidarr has nothing", not "not loaded"."""
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)])
    result = diff(desired_state((album, [SAVED])), view)
    assert not result.monitor
    assert [u.step for u in result.unmapped] == ["lidarr:missing-release-group"]


# --------------------------------------------------------------------------- idempotence and staleness


def test_a_state_that_already_matches_produces_an_empty_diff() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, monitor_new_items="none", metadata_profile_id=LEAN_PROFILE_ID)],
        albums=[lidarr_album(album, monitored=True, files=5)],
    )
    key, record = owned(album, SAVED)
    result = diff(desired_state((album, [SAVED])), view, {key: record}, live_reason_keys={SAVED.key})
    assert result.is_empty
    assert not result.update_reasons
    assert not result.guards


def test_applying_the_same_inputs_twice_gives_the_same_diff() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album)])
    a = diff(desired_state((album, [SAVED])), view)
    b = diff(desired_state((album, [SAVED])), view)
    assert a.monitor == b.monitor
    assert a.lidarr_digest == b.lidarr_digest


def test_a_diff_is_stale_when_the_source_snapshot_changed() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album)])
    result = diff(desired_state((album, [SAVED])), view, source_digest="src-1")
    assert is_stale(result, "src-2", result.lidarr_digest) is True
    assert is_stale(result, "src-1", result.lidarr_digest) is False


def test_a_diff_is_stale_when_a_touched_album_changed_in_lidarr() -> None:
    album = rg("rg-1", "Record")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=False)])
    result = diff(desired_state((album, [SAVED])), view)
    moved = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(album, monitored=True)])
    current = lidarr_digest(moved, result.monitor, result.unmonitor, result.ratchets, result.set_new_items_none)
    assert is_stale(result, result.source_digest, current) is True


def test_an_untouched_album_changing_does_not_make_the_diff_stale() -> None:
    wanted = rg("rg-1", "Record")
    unrelated = rg("rg-2", "Unrelated")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[lidarr_album(wanted, id=101), lidarr_album(unrelated, id=102, monitored=False)],
    )
    result = diff(desired_state((wanted, [SAVED])), view)
    moved = lidarr_view(
        artists=[lidarr_artist(ARTIST)],
        albums=[lidarr_album(wanted, id=101), lidarr_album(unrelated, id=102, monitored=True)],
    )
    current = lidarr_digest(moved, result.monitor, result.unmonitor, result.ratchets, result.set_new_items_none)
    assert is_stale(result, result.source_digest, current) is False


def test_config_changes_names_each_setting_that_moved() -> None:
    recorded = {"rules": {"deny_releases": [], "liked_track_scope": "album"}, "guards": {"source_shrink_pct": 10.0}}
    current = {"rules": {"deny_releases": ["rg-x"], "liked_track_scope": "album"}, "guards": {"source_shrink_pct": 5.0}}

    assert config_changes(recorded, current) == ["[guards] source_shrink_pct", "[rules] deny_releases"]
    assert config_changes(current, current) == []


def test_a_setting_only_one_side_knows_about_counts_as_changed() -> None:
    """A setting added by a later likearr may change the plan, so the older diff cannot vouch for it."""
    recorded = {"rules": {"liked_track_scope": "album"}}
    current = {"rules": {"liked_track_scope": "album", "new_knob": True}}

    assert config_changes(recorded, current) == ["[rules] new_knob"]


def test_a_diff_that_recorded_no_config_cannot_say_what_changed() -> None:
    """`None`, not `[]`: a diff written before the fingerprint existed is not evidence of no change."""
    assert config_changes(None, {"rules": {}}) is None


@pytest.mark.parametrize("scheduled", [True, False])
def test_the_diff_carries_the_resolver_version(scheduled: bool) -> None:
    from likearr.models import RESOLVER_VERSION

    result = diff(desired_state(), lidarr_view(), scheduled=scheduled)
    assert result.resolver_version == RESOLVER_VERSION


def test_a_reason_that_resolved_to_another_release_releases_its_old_one() -> None:
    """Moved (re-resolved) is not the same as failed: the old release is unmonitored."""
    from datetime import UTC, datetime

    from likearr.models import OwnedRelease, ReleaseKey

    old = rg("rg-old", "Compilation It Was Filed On")
    new = rg("rg-new", "The Real Album")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(old, id=1, monitored=True), lidarr_album(new, id=2)]
    )
    owned = {
        ReleaseKey(ARTIST, "rg-old"): OwnedRelease(
            key=ReleaseKey(ARTIST, "rg-old"),
            reasons=frozenset({LIKED}),
            step="track:album",
            resolver_version=1,
            monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
            lidarr_album_id=1,
        )
    }
    result = diff(desired_state((new, [LIKED])), view, owned, live_reason_keys={LIKED.key})
    assert [u.key.rg_mbid for u in result.unmonitor] == ["rg-old"]
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-new"]


def test_a_reason_that_merely_failed_to_resolve_keeps_its_old_release() -> None:
    from datetime import UTC, datetime

    from likearr.models import OwnedRelease, ReleaseKey

    old = rg("rg-old", "Album")
    view = lidarr_view(artists=[lidarr_artist(ARTIST)], albums=[lidarr_album(old, monitored=True)])
    owned = {
        ReleaseKey(ARTIST, "rg-old"): OwnedRelease(
            key=ReleaseKey(ARTIST, "rg-old"),
            reasons=frozenset({LIKED}),
            step="track:album",
            resolver_version=1,
            monitored_at=datetime(2026, 1, 1, tzinfo=UTC),
            lidarr_album_id=1,
        )
    }
    result = diff(desired_state(), view, owned, live_reason_keys={LIKED.key})
    assert result.unmonitor == []


# --------------------------------------------------------------------------- name collisions
#
# Lidarr matches an incoming download to an artist by NAME. Two artists sharing one name make
# that ambiguous, Lidarr refuses to guess, and the queue item can never import. Left alone,
# such a pair strands downloads on every protocol until someone notices.

DUPLICATE_A = "artist-lawrence-a"
DUPLICATE_B = "artist-lawrence-b"


def test_an_add_that_would_collide_on_name_is_skipped() -> None:
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    assert result.add_artists == [], "adding this would give Lidarr two artists called 'Lawrence'"
    assert [g.code for g in result.guards] == ["name-collision"]
    message = result.guards[0].message
    assert "Lawrence" in message
    assert DUPLICATE_B in message, "the message names the artist we skipped"
    assert "9001" in message and DUPLICATE_A in message, "and the one already there"


def test_the_guard_message_does_not_call_it_a_duplicate() -> None:
    """It is usually a genuinely different artist, and the message must not imply otherwise."""
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    message = result.guards[0].message.lower()
    assert "duplicate" not in message
    assert "a different artist that shares a name" in message
    assert "lidarr matches incoming downloads by name" in message, "the constraint is Lidarr's"


def test_the_guard_message_offers_only_what_lidarr_can_do() -> None:
    """Issue #32: Lidarr cannot rename an artist, so "add them under a distinct name" is gone."""
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Guns N' Roses")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Guns N' Roses")

    message = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing])).guards[0].message

    assert "distinct name" not in message
    assert "keep one, or add both and import the other's downloads by hand" in message
    assert "Manual Import" in message
    assert '`likearr explain "Guns N\' Roses"` shows why' in message, "quoted so it can be pasted"


def test_a_collision_is_recorded_as_data_with_what_it_cost() -> None:
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    first = rg("rg-1", "One", artist_mbid=DUPLICATE_B, artist_name="Lawrence")
    second = rg("rg-2", "Two", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((first, [LIKED]), (second, [SAVED])), lidarr_view(artists=[existing]))

    assert len(result.name_collisions) == 1
    collision = result.name_collisions[0]
    assert collision.name == "Lawrence"
    assert collision.wanted_mbid == DUPLICATE_B
    assert (collision.existing_mbid, collision.existing_lidarr_id) == (DUPLICATE_A, 9001)
    assert collision.dropped_releases == 2, "both releases went unmonitored because of the skip"
    assert collision.wanted_disambiguation == "", "the core is pure; the shell fills these in"


def test_the_dropped_count_only_counts_the_collided_artists_releases() -> None:
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    theirs = rg("rg-1", "One", artist_mbid=DUPLICATE_B, artist_name="Lawrence")
    unrelated = rg("rg-9", "Nine", artist_mbid="artist-other", artist_name="Someone Else")

    result = diff(desired_state((theirs, [LIKED]), (unrelated, [SAVED])), lidarr_view(artists=[existing]))

    assert result.name_collisions[0].dropped_releases == 1
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-9"], "the unrelated artist is untouched"


def test_a_collided_artists_releases_are_not_planned_either() -> None:
    """The artist was not added, so nothing of theirs can ever be found in Lidarr."""
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    assert result.monitor == []


def test_two_new_artists_sharing_a_name_are_both_skipped() -> None:
    """Neither is in Lidarr yet, so the Lidarr-side check alone would add both in one run.

    Neither is preferred: the one a follow brought in may be a namesake the name search guessed,
    so picking "the followed one" could keep the stranger. Both are reported instead.
    """
    one = rg("rg-1", "One", artist_mbid=DUPLICATE_A, artist_name="Lawrence")
    other = rg("rg-2", "Two", artist_mbid=DUPLICATE_B, artist_name="lawrence")
    unrelated = rg("rg-9", "Nine", artist_mbid="artist-other", artist_name="Someone Else")

    result = diff(desired_state((one, [LIKED]), (other, [SAVED]), (unrelated, [SAVED])), lidarr_view())

    assert [a.artist_mbid for a in result.add_artists] == ["artist-other"]
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-9"], "nothing of either namesake is planned"
    assert sorted((c.wanted_mbid, c.existing_mbid) for c in result.name_collisions) == [
        (DUPLICATE_A, DUPLICATE_B),
        (DUPLICATE_B, DUPLICATE_A),
    ]
    assert all(c.existing_lidarr_id == 0 and not c.in_lidarr for c in result.name_collisions)
    assert [g.code for g in result.guards] == ["name-collision", "name-collision"]
    message = result.guards[0].message
    assert "also wanted this run" in message
    assert "Lidarr artist 0" not in message, "there is no Lidarr artist to point at yet"
    assert not result.guarded


def test_a_new_namesake_of_an_artist_already_in_lidarr_is_reported_against_lidarrs() -> None:
    """Three of one name, one already in Lidarr: both new ones collide with the one Lidarr has."""
    existing = lidarr_artist(DUPLICATE_A, id=761, name="Lawrence")
    b = rg("rg-b", "Bee", artist_mbid=DUPLICATE_B, artist_name="Lawrence")
    c = rg("rg-c", "Sea", artist_mbid="artist-lawrence-c", artist_name="Lawrence")

    result = diff(desired_state((b, [LIKED]), (c, [SAVED])), lidarr_view(artists=[existing]))

    assert result.add_artists == []
    assert {(c.wanted_mbid, c.existing_lidarr_id) for c in result.name_collisions} == {
        (DUPLICATE_B, 761),
        ("artist-lawrence-c", 761),
    }


def test_re_adding_the_same_mbid_is_not_a_collision() -> None:
    """The commonest case by far: the artist is already there, under the same MusicBrainz id."""
    existing = lidarr_artist(ARTIST, id=1, name="Test Artist")
    album = rg("rg-1", "First Album")

    result = diff(
        desired_state((album, [LIKED])),
        lidarr_view(artists=[existing], albums=[lidarr_album(album, id=100)]),
    )

    assert result.guards == []
    assert result.add_artists == []
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-1"]


@pytest.mark.parametrize(
    "existing_name",
    ["lawrence", "LAWRENCE", "Lawrence.", "Lawrence!", "  Lawrence  ", "The Lawrence"],
)
def test_case_punctuation_and_a_leading_the_still_collide(existing_name: str) -> None:
    """Lidarr's own matcher is looser than this fold, so anything folding together collides."""
    existing = lidarr_artist(DUPLICATE_A, id=9001, name=existing_name)
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    assert result.add_artists == []
    assert [g.code for g in result.guards] == ["name-collision"]


def test_a_genuinely_new_name_is_still_added() -> None:
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Evangeline")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    assert [a.artist_mbid for a in result.add_artists] == [DUPLICATE_B]
    assert result.guards == []


def test_the_collision_fires_even_when_the_existing_artist_has_no_files() -> None:
    """The ambiguity is in the name; Lidarr's import matcher never looks at files.

    A duplicate with zero files still stalls downloads.
    """
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    empty = rg("rg-old", "Old", artist_mbid=DUPLICATE_A, artist_name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(
        desired_state((wanted, [LIKED])),
        lidarr_view(artists=[existing], albums=[lidarr_album(empty, id=1, files=0)]),
    )

    assert result.add_artists == []
    assert [g.code for g in result.guards] == ["name-collision"]


def test_a_name_collision_blocks_no_unmonitors() -> None:
    """It refuses an add. Exit 2 means "unmonitors were refused" and must keep meaning that."""
    existing = lidarr_artist(DUPLICATE_A, id=9001, name="Lawrence")
    wanted = rg("rg-new", "Anything", artist_mbid=DUPLICATE_B, artist_name="Lawrence")

    result = diff(desired_state((wanted, [LIKED])), lidarr_view(artists=[existing]))

    assert result.guards[0].blocked_unmonitors == 0
    assert not result.guarded


def test_re_identifying_an_artist_releases_the_old_ones_rows() -> None:
    """The orphan class: releases owned against the wrong "Lawrence".

    When a followed artist re-resolves to a different MusicBrainz artist, the reason moves to the
    new artist's releases. The old ones lose it for real - it resolved somewhere else, so this is
    not the transient-failure case `live_reason_keys` protects - and are unmonitored.
    """
    wrong = rg("rg-wrong", "By the German DJ", artist_mbid="mbid-german-dj", artist_name="Lawrence")
    right = rg("rg-right", "Family Business", artist_mbid="mbid-ny-band", artist_name="Lawrence")
    view = lidarr_view(
        artists=[lidarr_artist("mbid-german-dj", id=9002), lidarr_artist("mbid-ny-band", id=9001)],
        albums=[lidarr_album(wrong, id=1, monitored=True), lidarr_album(right, id=2)],
    )
    key, record = owned(wrong, FOLLOWED, album_id=1)

    result = diff(
        desired_state((right, [FOLLOWED]), artists={"mbid-ny-band": "Lawrence"}),
        view,
        {key: record},
        live_reason_keys={FOLLOWED.key},
    )

    assert [u.key.rg_mbid for u in result.unmonitor] == ["rg-wrong"]
    assert [m.key.rg_mbid for m in result.monitor] == ["rg-right"]


def test_an_artist_that_failed_to_resolve_keeps_its_old_rows() -> None:
    """The conservative half of the same rule: unresolved is not the same as unwanted.

    An ambiguous Spotify link leaves the artist UNMAPPED, and nothing of theirs is in the desired
    state. That is a failure to decide, not a decision, so nothing is unmonitored - the ambiguity
    is reported instead and a human resolves it.
    """
    wrong = rg("rg-wrong", "By the German DJ", artist_mbid="mbid-german-dj", artist_name="Lawrence")
    view = lidarr_view(
        artists=[lidarr_artist("mbid-german-dj", id=9002)],
        albums=[lidarr_album(wrong, id=1, monitored=True)],
    )
    key, record = owned(wrong, FOLLOWED, album_id=1)

    result = diff(desired_state(artists={}), view, {key: record}, live_reason_keys={FOLLOWED.key})

    assert result.unmonitor == []
