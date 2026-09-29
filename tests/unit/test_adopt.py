"""Tests for adoption: the one-time bridge from a hand-curated library to owned state."""

from __future__ import annotations

import pytest

from likearr.core.adopt import HELD_ARTIST, HELD_ITEM, choose, plan_adoption
from likearr.core.desire import CATALOGUE_TOO_LARGE_STEP
from likearr.core.resolver import METADATA_ERROR_STEP
from likearr.models import ArtistResolution, ReasonKind, ReleaseKey, Resolution, ResolutionStatus
from tests.unit.fakes import (
    NOW,
    album_intent,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    owned,
    reason,
    rg,
    snapshot,
    spotify_album,
    track_intent,
)
from tests.unit.test_diff import desired_state

ARTIST = "artist-1"
SAVED = reason(ReasonKind.SAVED, "al1")


def _view(*albums):
    return lidarr_view(artists=[lidarr_artist(ARTIST)], albums=list(albums))


def test_a_monitored_unwanted_release_off_the_keep_list_is_unmonitored() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(
        desired_state(), _view(lidarr_album(album, monitored=True)), {}, set(), now=NOW, unmonitor_rest=True
    )
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert not plan.keep_as_manual
    assert not plan.claim


def test_a_kept_release_becomes_owned_and_manual() -> None:
    album = rg("rg-1", "Record")
    plan = plan_adoption(
        desired_state(), _view(lidarr_album(album, monitored=True)), {}, {"rg-1"}, now=NOW, unmonitor_rest=True
    )
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
    plan = plan_adoption(desired_state(), view, {}, {f"artist:{ARTIST}"}, now=NOW, unmonitor_rest=True)
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
            desired_state((album, [SAVED])),
            _view(lidarr_album(album, monitored=True)),
            {},
            keep,
            now=NOW,
            unmonitor_rest=True,
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
    plan = plan_adoption(
        desired_state(), _view(lidarr_album(album, monitored=False)), {}, set(), now=NOW, unmonitor_rest=True
    )
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
        unmonitor_rest=True,
    )
    assert not (plan.unmonitor or plan.keep_as_manual or plan.claim)


def test_adoption_is_deterministic_and_sorted() -> None:
    groups = [rg(f"rg-{i}", f"Record {i}") for i in (2, 0, 1)]
    view = _view(*[lidarr_album(g, id=100 + i, monitored=True) for i, g in enumerate(groups)])
    plan = plan_adoption(desired_state(), view, {}, set(), now=NOW, unmonitor_rest=True)
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-0", "rg-1", "rg-2"]


def test_keeping_and_unmonitoring_split_cleanly() -> None:
    keep = rg("rg-keep", "Keep This")
    drop = rg("rg-drop", "Drop This")
    view = _view(lidarr_album(keep, id=101, monitored=True), lidarr_album(drop, id=102, monitored=True))
    plan = plan_adoption(desired_state(), view, {}, {"rg-keep"}, now=NOW, unmonitor_rest=True)
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


# ------------------------------------------- an artist whose catalogue could not be read


def _unread(artist_mbid: str, step: str) -> ArtistResolution:
    return ArtistResolution(
        intent_key=f"artist:{artist_mbid}",
        status=ResolutionStatus.UNMAPPED,
        artist_mbid=artist_mbid,
        artist_name="Test Artist",
        step=step,
    )


@pytest.mark.parametrize("step", [CATALOGUE_TOO_LARGE_STEP, METADATA_ERROR_STEP])
def test_an_unread_catalogue_holds_back_only_what_would_have_been_unmonitored(step: str) -> None:
    """None of the artist's catalogue reached the desired set, so "no source wants it" is not
    known for a plain hand-monitored album: it is held, with the reason, not unmonitored. A
    keep-list album and one a source wants anyway (a saved album) do not depend on the catalogue,
    so they are kept and claimed as usual."""
    wanted = rg("rg-1", "Saved Anyway")
    plain = rg("rg-2", "By Hand")
    kept = rg("rg-3", "On The Keep List")
    view = _view(
        lidarr_album(wanted, id=101, monitored=True),
        lidarr_album(plain, id=102, monitored=True),
        lidarr_album(kept, id=103, monitored=True),
    )
    desired = desired_state((wanted, [SAVED]), unmapped=[_unread(ARTIST, step)])

    plan = plan_adoption(desired, view, {}, {"rg-3"}, now=NOW, unmonitor_rest=True)

    assert [r.key.rg_mbid for r in plan.claim] == ["rg-1"]
    assert [r.key.rg_mbid for r in plan.keep_as_manual] == ["rg-3"]
    assert plan.keep_as_manual[0].is_manual
    assert plan.unmonitor == []
    assert [(h.key.rg_mbid, h.title, h.step) for h in plan.held] == [("rg-2", "By Hand", step)]
    assert plan.held[0].reason


def test_an_artist_whose_catalogue_reads_is_not_held() -> None:
    """The control: another artist's unread catalogue holds back only that artist's albums."""
    ours = rg("rg-1", "Record")
    theirs = rg("rg-9", "Other Record", artist_mbid="artist-9", artist_name="Other")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST), lidarr_artist("artist-9", id=9)],
        albums=[lidarr_album(ours, id=101, monitored=True), lidarr_album(theirs, id=901, monitored=True)],
    )
    desired = desired_state(unmapped=[_unread("artist-9", METADATA_ERROR_STEP)])

    plan = plan_adoption(desired, view, {}, set(), now=NOW, unmonitor_rest=True)

    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert [h.key.rg_mbid for h in plan.held] == ["rg-9"]


def test_an_artist_unmapped_for_another_reason_is_not_held() -> None:
    """Only a catalogue step says the releases are unknown. An artist that did not match is simply
    not followed, and their hand-monitored albums are planned as before."""
    album = rg("rg-1", "Record")
    desired = desired_state(unmapped=[_unread(ARTIST, "search:no-match")])
    plan = plan_adoption(desired, _view(lidarr_album(album, monitored=True)), {}, set(), now=NOW, unmonitor_rest=True)
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]
    assert plan.held == []


def _failed(intent_key: str, step: str = METADATA_ERROR_STEP) -> Resolution:
    return Resolution(intent_key=intent_key, status=ResolutionStatus.UNMAPPED, step=step)


def test_a_failed_lookup_holds_the_album_it_names_by_title_and_artist() -> None:
    """A liked song on "Record (Deluxe Edition)" and a saved "Other Record" whose lookups failed
    hold the Lidarr albums of that name by that artist; the keep list still wins, and an album
    nothing names is unmonitored as before."""
    liked = track_intent("Song", spotify_album("Record (Deluxe Edition)", spotify_id="sp-1"), spotify_id="sp-t")
    saved = album_intent(spotify_album("Other Record", spotify_id="sp-2"))
    view = _view(
        lidarr_album(rg("rg-1", "Record"), id=101, monitored=True),
        lidarr_album(rg("rg-2", "Other Record"), id=102, monitored=True),
        lidarr_album(rg("rg-3", "Unnamed"), id=103, monitored=True),
    )
    desired = desired_state(unmapped=[_failed(liked.reason.key), _failed(saved.reason.key)])
    source = snapshot(albums=[saved], tracks=[liked])

    plan = plan_adoption(desired, view, {}, set(), now=NOW, unmonitor_rest=True, snapshot=source)

    assert [(h.key.rg_mbid, h.step, h.cause == HELD_ITEM) for h in plan.held] == [
        ("rg-1", METADATA_ERROR_STEP, True),
        ("rg-2", METADATA_ERROR_STEP, True),
    ]
    assert "could not be looked up" in plan.held[0].reason
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-3"]

    kept = plan_adoption(desired, view, {}, {"rg-2"}, now=NOW, unmonitor_rest=True, snapshot=source)
    assert [r.key.rg_mbid for r in kept.keep_as_manual] == ["rg-2"]
    assert [h.key.rg_mbid for h in kept.held] == ["rg-1"]


@pytest.mark.parametrize(
    ("step", "artists", "with_snapshot"),
    [
        ("search:no-match", ("Test Artist",), True),
        (METADATA_ERROR_STEP, ("Someone Else",), True),
        (METADATA_ERROR_STEP, ("Test Artist",), False),
    ],
    ids=["unmapped-for-another-reason", "another-artist", "no-snapshot"],
)
def test_a_failed_lookup_that_names_no_album_holds_nothing(
    step: str, artists: tuple[str, ...], with_snapshot: bool
) -> None:
    """Only a lookup error ties an unmapped item to an album, and only by the same artist and
    title; anything else leaves the album to be unmonitored, under the degraded-run warning."""
    saved = album_intent(spotify_album("Record", spotify_id="sp-1", artists=artists))
    desired = desired_state(unmapped=[_failed(saved.reason.key, step)])
    view = _view(lidarr_album(rg("rg-1", "Record"), monitored=True))
    source = snapshot(albums=[saved]) if with_snapshot else None

    plan = plan_adoption(desired, view, {}, set(), now=NOW, unmonitor_rest=True, snapshot=source)

    assert plan.held == []
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]


def test_a_fallback_that_found_nothing_after_a_failed_lookup_holds_the_album() -> None:
    """MusicBrainz failed and Lidarr's fallback found nothing: the item is unmapped at a plain
    not-found step, but its key is in `lookup_failed`, so the album it names is held."""
    saved = album_intent(spotify_album("Record", spotify_id="sp-1"))
    desired = desired_state(unmapped=[_failed(saved.reason.key, "search:no-match")])
    view = _view(lidarr_album(rg("rg-1", "Record"), monitored=True))

    plan = plan_adoption(
        desired,
        view,
        {},
        set(),
        now=NOW,
        unmonitor_rest=True,
        snapshot=snapshot(albums=[saved]),
        lookup_failed=frozenset({saved.reason.key}),
    )

    assert [(h.key.rg_mbid, h.cause) for h in plan.held] == [("rg-1", HELD_ITEM)]
    assert plan.unmonitor == []


def _artist_failed(name: str, step: str = METADATA_ERROR_STEP) -> ArtistResolution:
    return ArtistResolution(
        intent_key=f"artist:sp-{name}", status=ResolutionStatus.UNMAPPED, artist_mbid="", artist_name=name, step=step
    )


def test_a_followed_artist_whose_lookup_failed_holds_their_albums_by_name() -> None:
    """No MusicBrainz id to match, so the artist's hand-monitored albums are matched by name; an
    artist of another name is unmonitored as before."""
    ours = rg("rg-1", "Record")
    theirs = rg("rg-9", "Other Record", artist_mbid="artist-9", artist_name="Other")
    view = lidarr_view(
        artists=[lidarr_artist(ARTIST, name="The Test Artist"), lidarr_artist("artist-9", id=9, name="Other")],
        albums=[lidarr_album(ours, id=101, monitored=True), lidarr_album(theirs, id=901, monitored=True)],
    )
    desired = desired_state(unmapped=[_artist_failed("Test Artist")])

    plan = plan_adoption(desired, view, {}, set(), now=NOW, unmonitor_rest=True)

    assert [(h.key.rg_mbid, h.cause) for h in plan.held] == [("rg-1", HELD_ARTIST)]
    assert "followed artist" in plan.held[0].reason
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-9"]


def test_a_followed_artist_that_simply_did_not_match_holds_nothing() -> None:
    album = rg("rg-1", "Record")
    desired = desired_state(unmapped=[_artist_failed("Test Artist", "artist:search")])
    plan = plan_adoption(desired, _view(lidarr_album(album, monitored=True)), {}, set(), now=NOW, unmonitor_rest=True)
    assert plan.held == []
    assert [u.key.rg_mbid for u in plan.unmonitor] == ["rg-1"]


# ------------------------------------------- claim only, the default


def test_by_default_adoption_claims_and_leaves_everything_else() -> None:
    wanted = rg("rg-1", "Saved")
    plain = rg("rg-2", "By Hand")
    view = _view(lidarr_album(wanted, id=101, monitored=True), lidarr_album(plain, id=102, monitored=True))
    desired = desired_state((wanted, [SAVED]), unmapped=[_unread(ARTIST, METADATA_ERROR_STEP)])

    plan = plan_adoption(desired, view, {}, set(), now=NOW)

    assert plan.unmonitor_rest is False
    assert [r.key.rg_mbid for r in plan.claim] == ["rg-1"]
    assert [(r.key.rg_mbid, r.title) for r in plan.left] == [("rg-2", "By Hand")]
    assert not (plan.unmonitor or plan.held or plan.keep_as_manual)


def test_a_keep_list_without_unmonitoring_the_rest_is_refused() -> None:
    with pytest.raises(ValueError, match="keep list"):
        plan_adoption(desired_state(), _view(), {}, {"rg-1"}, now=NOW)


def _full_plan():
    """One match (rg-1), two albums nothing wants (rg-2, rg-3) and one held (rg-4)."""
    saved = album_intent(spotify_album("Held", spotify_id="sp-4"))
    view = _view(
        lidarr_album(rg("rg-1", "Saved"), id=101, monitored=True),
        lidarr_album(rg("rg-2", "Two"), id=102, monitored=True),
        lidarr_album(rg("rg-3", "Three"), id=103, monitored=True),
        lidarr_album(rg("rg-4", "Held"), id=104, monitored=True),
    )
    desired = desired_state((rg("rg-1", "Saved"), [SAVED]), unmapped=[_failed(saved.reason.key)])
    return plan_adoption(desired, view, {}, set(), now=NOW, unmonitor_rest=True, snapshot=snapshot(albums=[saved]))


@pytest.mark.parametrize("claim", [True, False])
def test_choosing_claim_only_unmonitors_nothing(claim: bool) -> None:
    chosen = choose(_full_plan(), claim=claim, unmonitor_rest=False)
    assert [r.key.rg_mbid for r in chosen.claim] == (["rg-1"] if claim else [])
    assert not (chosen.unmonitor or chosen.held or chosen.unmonitor_rest)
    assert [r.key.rg_mbid for r in chosen.left] == ["rg-2", "rg-3", "rg-4"]


def test_choosing_to_unmonitor_the_rest_spares_the_kept_and_the_held() -> None:
    chosen = choose(_full_plan(), claim=True, unmonitor_rest=True, keep={"rg-3", "rg-4", "not-listed"})
    assert chosen.unmonitor_rest is True
    assert [u.key.rg_mbid for u in chosen.unmonitor] == ["rg-2"]
    assert [h.key.rg_mbid for h in chosen.held] == ["rg-4"], "held whatever the keep ticks say"
    assert [r.key.rg_mbid for r in chosen.left] == ["rg-3"]
    assert [r.key.rg_mbid for r in chosen.claim] == ["rg-1"]
