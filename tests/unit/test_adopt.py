"""Tests for adoption: the one-time bridge from a hand-curated library to owned state."""

from __future__ import annotations

import pytest

from likearr.core.adopt import plan_adoption
from likearr.core.desire import CATALOGUE_ERROR_STEP, CATALOGUE_TOO_LARGE_STEP
from likearr.models import ArtistResolution, ReasonKind, ReleaseKey, ResolutionStatus
from tests.unit.fakes import (
    NOW,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    owned,
    reason,
    rg,
)
from tests.unit.test_diff import desired_state

ARTIST = "artist-1"
SAVED = reason(ReasonKind.SAVED, "al1")


def _view(*albums):
    return lidarr_view(artists=[lidarr_artist(ARTIST)], albums=list(albums))


def test_a_monitored_unwanted_release_off_the_keep_list_is_unmonitored() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(desired_state(), _view(lidarr_album(album, monitored=True)), {}, set(), now=NOW)
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert not plan.keep_as_manual
    assert not plan.claim


def test_a_kept_release_becomes_owned_and_manual() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(desired_state(), _view(lidarr_album(album, monitored=True)), {}, {"rg-1"}, now=NOW)
    assert not plan.unmonitor
    assert len(plan.keep_as_manual) == 1
    kept = plan.keep_as_manual[0]
    assert kept.is_manual is True
    assert kept.key == ReleaseKey(ARTIST, "rg-1")
    assert kept.lidarr_album_id == 100
    assert kept.monitored_at == NOW


def test_an_artist_entry_on_the_keep_list_keeps_all_of_their_releases() -> None:
    one = rg("rg-1", "One")
    two = rg("rg-2", "Two")
    view = _view(lidarr_album(one, id=101, monitored=True), lidarr_album(two, id=102, monitored=True))
    plan = plan_adoption(desired_state(), view, {}, {f"artist:{ARTIST}"}, now=NOW)
    assert {k.key.rg_mbid for k in plan.keep_as_manual} == {"rg-1", "rg-2"}
    assert not plan.unmonitor


def test_a_monitored_release_a_source_wants_is_claimed_without_touching_lidarr() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(
        desired_state((album, [SAVED])),
        _view(lidarr_album(album, monitored=True)),
        {},
        set(),
        now=NOW,
    )
    assert not plan.unmonitor
    assert not plan.keep_as_manual
    assert len(plan.claim) == 1
    assert plan.claim[0].reasons == frozenset({SAVED})
    assert plan.claim[0].is_manual is False


def test_a_claimed_release_on_the_keep_list_is_also_kept_by_hand() -> None:
    """On the keep list and wanted by a source: claimed with both reasons, so losing the source
    later does not unmonitor a release the user asked adopt to keep."""
    album = rg("rg-1", "Record")
    for keep in ({"rg-1"}, {f"artist:{ARTIST}"}):
        plan = plan_adoption(
            desired_state((album, [SAVED])), _view(lidarr_album(album, monitored=True)), {}, keep, now=NOW
        )
        assert not plan.keep_as_manual
        (claimed,) = plan.claim
        assert claimed.is_manual is True
        assert SAVED in claimed.reasons
        assert {(r.kind, r.source_id) for r in claimed.reasons} == {
            (ReasonKind.SAVED, SAVED.source_id),
            (ReasonKind.MANUAL, "adopt"),
        }


def test_an_unmonitored_release_is_not_a_candidate() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(desired_state(), _view(lidarr_album(album, monitored=False)), {}, set(), now=NOW)
    assert not (plan.unmonitor or plan.keep_as_manual or plan.claim)


def test_an_already_owned_release_is_left_alone() -> None:
    """Re-adopting would overwrite real reasons with `manual` and make it unremovable."""
    album = rg("rg-1", "Record")
    key, record = owned(album, SAVED)
    plan = plan_adoption(
        desired_state(),
        _view(lidarr_album(album, monitored=True)),
        {key: record},
        {"rg-1"},
        now=NOW,
    )
    assert not (plan.unmonitor or plan.keep_as_manual or plan.claim)


def test_adoption_is_deterministic_and_sorted() -> None:
    groups = [rg(f"rg-{i}", f"Record {i}") for i in (2, 0, 1)]
    view = _view(*[lidarr_album(g, id=100 + i, monitored=True) for i, g in enumerate(groups)])
    plan = plan_adoption(desired_state(), view, {}, set(), now=NOW)
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-0", "rg-1", "rg-2"]


def test_keeping_and_unmonitoring_split_cleanly() -> None:
    keep = rg("rg-keep", "Keep This")
    drop = rg("rg-drop", "Drop This")
    view = _view(lidarr_album(keep, id=101, monitored=True), lidarr_album(drop, id=102, monitored=True))
    plan = plan_adoption(desired_state(), view, {}, {"rg-keep"}, now=NOW)
    assert [k.key.rg_mbid for k in plan.keep_as_manual] == ["rg-keep"]
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-drop"]


def test_losing_the_state_database_means_owning_nothing_and_unmonitoring_nothing_by_diff() -> None:
    """Adoption is the recovery path: an empty owned map makes the diff harmless."""
    from likearr.config import GuardsConfig
    from likearr.core.diff import build_diff

    album = rg("rg-1", "Record")
    view = _view(lidarr_album(album, monitored=True))
    result = build_diff(
        desired_state(),
        view,
        {},
        {},
        last_source_counts={},
        last_followed_counts={},
        source_counts={},
        live_reason_keys=set(),
        guards=GuardsConfig(),
        scheduled=False,
        schema_ok=True,
        now=NOW,
        source_digest="src",
        lean_profile_id=10,
        full_profile_id=20,
    )
    assert not result.unmonitor


# ------------------------------------------- an artist whose catalogue could not be read (issue #6)


def _unread(artist_mbid: str, step: str) -> ArtistResolution:
    return ArtistResolution(
        intent_key=f"artist:{artist_mbid}",
        status=ResolutionStatus.UNMAPPED,
        artist_mbid=artist_mbid,
        artist_name="Test Artist",
        step=step,
    )


@pytest.mark.parametrize("step", [CATALOGUE_TOO_LARGE_STEP, CATALOGUE_ERROR_STEP])
def test_an_unread_catalogue_holds_back_every_album_of_that_artist(step: str) -> None:
    """None of the artist's releases reached the desired set, so "no source wants it" is not known:
    neither unmonitored nor claimed, but listed as held with the reason."""
    wanted = rg("rg-1", "Saved Anyway")
    unwanted = rg("rg-2", "Two")
    kept = rg("rg-3", "Three")
    view = _view(
        lidarr_album(wanted, id=101, monitored=True),
        lidarr_album(unwanted, id=102, monitored=True),
        lidarr_album(kept, id=103, monitored=True),
    )
    desired = desired_state((wanted, [SAVED]), unmapped=[_unread(ARTIST, step)])

    plan = plan_adoption(desired, view, {}, {"rg-3"}, now=NOW)

    assert plan.unmonitor == []
    assert plan.claim == []
    assert plan.keep_as_manual == []
    assert [(h.key.rg_mbid, h.title, h.step) for h in plan.held] == [
        ("rg-1", "Saved Anyway", step),
        ("rg-2", "Two", step),
        ("rg-3", "Three", step),
    ]
    assert all(h.reason for h in plan.held)


def test_an_artist_whose_catalogue_reads_is_not_held() -> None:
    """The control: another artist's unread catalogue holds back only that artist's albums."""
    ours = rg("rg-1", "Record")
    theirs = rg("rg-9", "Other Record", artist_mbid="artist-9", artist_name="Other")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST), lidarr_artist("artist-9", id=9)],
        albums=[lidarr_album(ours, id=101, monitored=True), lidarr_album(theirs, id=901, monitored=True)],
    )
    desired = desired_state(unmapped=[_unread("artist-9", CATALOGUE_ERROR_STEP)])

    plan = plan_adoption(desired, view, {}, set(), now=NOW)

    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert [h.key.rg_mbid for h in plan.held] == ["rg-9"]


def test_an_artist_unmapped_for_another_reason_is_not_held() -> None:
    """Only a catalogue step says the releases are unknown. An artist that did not match is simply
    not followed, and their hand-monitored albums are planned as before."""
    album = rg("rg-1", "Record")
    desired = desired_state(unmapped=[_unread(ARTIST, "search:no-match")])
    plan = plan_adoption(desired, _view(lidarr_album(album, monitored=True)), {}, set(), now=NOW)
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert plan.held == []
