"""The Status page's view model: run history in prose, without a single field name."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from likearr.adapters.state_sqlite import RunRow
from likearr.models import HealthRecord, NameCollision, RunStatus
from likearr.web.status import (
    ago,
    build_status,
    collision_cards,
    describe_run,
    first_run_checklist,
    lost_state_sentence,
    reauth_banner_note,
    reauth_view,
    short_message,
    source_counts,
)

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
NY = ZoneInfo("America/New_York")


def _record(**overrides: object) -> HealthRecord:
    defaults: dict[str, object] = dict(
        ts=int((NOW - timedelta(hours=3)).timestamp()),
        version="0.1.0",
        resolver_version=9,
        exit_code=0,
        status=RunStatus.OK,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={"monitored": 4, "unmonitored": 1, "added": 2},
        unmapped=0,
        pending_album=0,
        message="",
        dry_run=False,
        baseline="compared",
    )
    defaults.update(overrides)
    return HealthRecord(**defaults)  # type: ignore[arg-type]


def _row(**overrides: object) -> RunRow:
    guards = overrides.pop("guards", ())
    blocked = overrides.pop("guard_blocked", tuple(1 for _ in guards))  # type: ignore[arg-type]
    projected = overrides.pop("projected_wanted", None)
    collisions = overrides.pop("name_collisions", ())
    return RunRow(
        record=_record(**overrides),
        guards=guards,  # type: ignore[arg-type]
        guard_blocked=blocked,  # type: ignore[arg-type]
        projected_wanted=projected,  # type: ignore[arg-type]
        name_collisions=collisions,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- one run


def test_a_problem_about_the_run_links_to_that_run_s_page() -> None:
    row = dataclasses.replace(_row(guards=("source-shrink: liked_tracks fell by 40%",), guard_blocked=(12,)), id=42)

    glance = health_glance(row, now=NOW, tz=NY)

    assert ("source-shrink: liked_tracks fell by 40%", "/runs/42") in glance.problems


def test_an_apply_is_described_by_what_it_did() -> None:
    summary = describe_run(_row(), now=NOW, tz=NY)

    assert summary.headline == "Applied: 4 releases monitored, 1 unmonitored, 2 artists added."
    assert summary.ago == "3 h ago"
    assert summary.applied
    assert summary.tone == "ok"


def test_a_dry_run_says_what_it_would_have_done() -> None:
    summary = describe_run(_row(dry_run=True), now=NOW, tz=NY)

    assert summary.headline == "Dry run: would monitor 4 releases, unmonitor 1, add 2 artists."
    assert not summary.applied


def test_the_headline_counts_artists_set_to_monitor_new_albums_none() -> None:
    """An apply whose only change is this write must not read as zeros everywhere."""
    counts = {"monitored": 0, "unmonitored": 0, "added": 0, "new_items_none": 2}

    applied = describe_run(_row(counts=counts), now=NOW, tz=NY)
    dry = describe_run(_row(counts=counts, dry_run=True), now=NOW, tz=NY)

    assert applied.headline == (
        'Applied: 0 releases monitored, 0 unmonitored, 0 artists added, 2 artists set to "Monitor New Albums: None".'
    )
    assert dry.headline == (
        'Dry run: would monitor 0 releases, unmonitor 0, add 0 artists, set 2 artists to "Monitor New Albums: None".'
    )


def test_a_failed_run_leads_with_its_message() -> None:
    summary = describe_run(
        _row(status=RunStatus.ERROR, exit_code=1, message="spotify: token expired", counts={}), now=NOW, tz=NY
    )

    assert summary.headline == "Failed: spotify: token expired"
    assert summary.tone == "bad"


def test_a_skipped_run_is_not_a_failure() -> None:
    summary = describe_run(_row(status=RunStatus.SKIPPED, message="another run holds the lock"), now=NOW, tz=NY)

    assert summary.headline == "Skipped: another run was already in progress."
    assert summary.tone == "quiet"


def test_a_paused_run_is_not_a_failure() -> None:
    summary = describe_run(
        _row(status=RunStatus.PAUSED, message="scheduled runs are paused: maintenance window"), now=NOW, tz=NY
    )

    assert summary.headline == "Paused: maintenance window"
    assert summary.tone == "quiet"
    assert not summary.applied


def test_what_is_newly_wrong_is_spelled_out_and_standing_counts_are_not() -> None:
    summary = describe_run(
        _row(
            status=RunStatus.DEGRADED,
            new_conditions=["new-name-collision"],
            name_collisions=7,
            name_collisions_new=1,
            unmapped=480,
            unmapped_new=3,
            regressions=2,
        ),
        now=NOW,
        tz=NY,
    )

    assert summary.conditions == ["name collision(s) not seen last run"]
    assert "1 new name collision" in summary.newly
    assert "3 newly unmapped songs or albums" in summary.newly
    assert "2 releases that mapped last run no longer do" in summary.newly
    assert not any("480" in line for line in summary.newly)
    assert summary.tone == "warn"


def test_guards_come_from_the_diff() -> None:
    summary = describe_run(_row(status=RunStatus.GUARDED, guards=("liked_tracks shrank 40%",)), now=NOW, tz=NY)

    assert summary.guards == ("liked_tracks shrank 40%",)
    assert summary.tone == "warn"


def test_a_run_that_could_not_compare_says_why() -> None:
    summary = describe_run(_row(baseline="rules-changed"), now=NOW, tz=NY)

    assert summary.baseline_note == "Nothing to compare with this time: the rules changed since the last apply."


# ---------------------------------------------------------------- the page


def test_the_last_applied_run_is_kept_apart_from_the_last_run_of_any_kind() -> None:
    rows = [
        _row(dry_run=True, ts=int((NOW - timedelta(minutes=5)).timestamp())),
        _row(status=RunStatus.SKIPPED, ts=int((NOW - timedelta(hours=1)).timestamp())),
        _row(ts=int((NOW - timedelta(hours=6)).timestamp()), projected_wanted=12),
    ]

    view = build_status(rows, now=NOW, tz=NY)

    assert view.last_any is not None
    assert view.last_any.headline.startswith("Dry run")
    assert view.last_applied is not None
    assert view.last_applied.headline.startswith("Applied")
    assert view.last_applied.ago == "6 h ago"
    assert len(view.history) == 3


def test_a_skipped_or_stale_run_is_never_the_last_applied_run() -> None:
    rows = [
        _row(status=RunStatus.STALE, exit_code=3),
        _row(status=RunStatus.SKIPPED),
        _row(status=RunStatus.ERROR, exit_code=1),
    ]

    assert build_status(rows, now=NOW, tz=NY).last_applied is None


def test_the_lost_state_count_comes_from_the_newest_run_that_planned() -> None:
    """A failed run after it read nothing from Lidarr, so it must not hide the warning."""
    planned = {"followed_artists": 3, "monitored": 0}
    rows = [
        _row(status=RunStatus.ERROR, exit_code=1, counts={}),
        _row(counts=planned, tagged_without_state=4),
        _row(counts=planned),
    ]

    assert build_status(rows, now=NOW, tz=NY).tagged_without_state == 4
    assert build_status(rows[2:], now=NOW, tz=NY).tagged_without_state == 0
    assert build_status([], now=NOW, tz=NY).tagged_without_state == 0


def test_the_lost_state_sentence() -> None:
    assert lost_state_sentence(0, "likearr") == ""
    assert lost_state_sentence(3, "likearr") == (
        "Lidarr has 3 artists tagged likearr that likearr's state database doesn't know. If you lost the "
        "database, restore it from backup. Until then, likearr never unmonitors anything it monitored before."
    )
    assert lost_state_sentence(1, "mine").startswith("Lidarr has 1 artist tagged mine that ")


def test_projected_wanted_comes_from_the_newest_run_with_a_diff() -> None:
    rows = [_row(status=RunStatus.ERROR, exit_code=1), _row(projected_wanted=37), _row(projected_wanted=5)]

    view = build_status(rows, now=NOW, tz=NY)

    assert view.projected_wanted == 37


def test_source_counts_name_playlists_when_they_can() -> None:
    counts = {
        "followed_artists": 250,
        "saved_albums": 900,
        "liked_tracks": 2500,
        "playlist:abc": 40,
        "playlist:gone": 3,
        "monitored": 4,
    }

    assert source_counts(counts, {"abc": "Road trip"}) == [
        ("Followed artists", 250, None),
        ("Saved albums", 900, None),
        ("Liked songs", 2500, None),
        ("Playlist: Road trip", 40, None),
        ("Playlist gone", 3, "https://open.spotify.com/playlist/gone"),
    ]


# ---------------------------------------------------------------- small helpers


def test_ago_reads_like_a_person_wrote_it() -> None:
    assert ago(NOW, NOW - timedelta(seconds=20)) == "just now"
    assert ago(NOW, NOW - timedelta(minutes=5)) == "5 min ago"
    assert ago(NOW, NOW - timedelta(hours=26)) == "1 day ago"
    assert ago(NOW, NOW - timedelta(days=9)) == "9 days ago"
    assert ago(NOW, NOW + timedelta(hours=2)) == "in 2 h"


def test_the_reauth_countdown() -> None:
    assert reauth_view(None, None, now=NOW).tone == "warn"
    fine = reauth_view(datetime(2026, 9, 20, tzinfo=UTC), datetime(2027, 3, 20, tzinfo=UTC), now=NOW)
    assert fine.tone == "ok"
    assert fine.days_left == 177
    soon = reauth_view(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC), now=NOW)
    assert soon.tone == "warn"
    late = reauth_view(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), now=NOW)
    assert late.tone == "bad"
    assert late.days_left is not None
    assert late.days_left < 0


# ---------------------------------------------------------------- reauth banner note


def test_reauth_banner_note_is_empty_well_before_the_warn_window() -> None:
    """Tone "ok": the banner's "nothing needs you" stays true, so there is nothing to add."""
    fine = reauth_view(datetime(2026, 9, 20, tzinfo=UTC), datetime(2027, 3, 20, tzinfo=UTC), now=NOW)

    assert reauth_banner_note(fine, has_token=True) == ""


def test_reauth_banner_note_tells_no_token_from_an_undated_one() -> None:
    """The wording for these two is already decided; this only decides which one applies."""
    unknown = reauth_view(None, None, now=NOW)

    assert reauth_banner_note(unknown, has_token=False) == "no-token"
    assert reauth_banner_note(unknown, has_token=True) == "unknown-date"


def test_reauth_banner_note_names_due_soon_and_overdue() -> None:
    soon = reauth_view(datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC), now=NOW)
    late = reauth_view(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), now=NOW)

    assert reauth_banner_note(soon, has_token=True) == "due-soon"
    assert reauth_banner_note(late, has_token=True) == "overdue"


def test_a_playlists_only_config_still_shows_its_source_counts() -> None:
    rows = [_row(counts={"playlist:abc": 40, "monitored": 1})]

    view = build_status(rows, now=NOW, tz=NY, playlist_names={"abc": "Road trip"})

    assert view.sources == [("Playlist: Road trip", 40, None)]


# ---------------------------------------------------------------- name collisions

JUNGLE = NameCollision(
    name="Jungle",
    wanted_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095",
    existing_mbid="6bbb3983-ce8a-4971-96e0-7cae73268fc4",
    existing_lidarr_id=9003,
    existing_name="Jungle",
    wanted_disambiguation="US psychedelic rock",
    existing_disambiguation="London modern soul collective",
    dropped_releases=3,
)


def test_a_collision_card_links_both_artists_and_lidarr() -> None:
    (card,) = collision_cards([JUNGLE], lidarr_url="https://lidarr.example.org")

    assert card.name == "Jungle"
    assert card.wanted_label == "Jungle (US psychedelic rock)"
    assert card.existing_label == "Jungle (London modern soul collective)"
    assert card.existing_lidarr_id == 9003
    assert card.dropped_releases == 3
    assert card.wanted_musicbrainz == "https://musicbrainz.org/artist/59074e0f-ede4-4ff1-bee2-cbfd3a273095"
    assert card.existing_musicbrainz == "https://musicbrainz.org/artist/6bbb3983-ce8a-4971-96e0-7cae73268fc4"
    # Lidarr 3.x routes (frontend/src/App/AppRoutes.js at v3.1.0.4875): the artist page is keyed by
    # the MusicBrainz id, and the add page reads ?term= and searches on load; "lidarr:<mbid>" is an
    # id lookup (SkyHookProxy.SearchForNewArtist).
    assert card.existing_in_lidarr == "https://lidarr.example.org/artist/6bbb3983-ce8a-4971-96e0-7cae73268fc4"
    assert card.add_in_lidarr == (
        "https://lidarr.example.org/add/search?term=lidarr%3A59074e0f-ede4-4ff1-bee2-cbfd3a273095"
    )


def test_a_collision_without_disambiguations_or_an_existing_mbid_links_what_it_can() -> None:
    bare = NameCollision(name="Lawrence", wanted_mbid="0f0f0f0f-1111-2222-3333-444444444444", existing_lidarr_id=7)

    (card,) = collision_cards([bare], lidarr_url="http://lidarr:8686")

    assert card.wanted_label == "Lawrence"
    assert card.existing_label == "Lawrence"
    assert card.existing_musicbrainz == ""
    assert card.existing_in_lidarr == ""


def test_a_collision_between_two_new_artists_has_no_lidarr_side() -> None:
    both_new = NameCollision(
        name="Jungle",
        wanted_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095",
        existing_mbid="6bbb3983-ce8a-4971-96e0-7cae73268fc4",
        existing_name="Jungle",
        dropped_releases=2,
    )

    (card,) = collision_cards([both_new], lidarr_url="http://lidarr:8686")

    assert card.other_in_lidarr is False
    assert card.existing_in_lidarr == "", "the other artist has no Lidarr page to open"
    assert card.existing_musicbrainz == "https://musicbrainz.org/artist/6bbb3983-ce8a-4971-96e0-7cae73268fc4"


def test_a_malformed_mbid_never_becomes_a_link() -> None:
    odd = NameCollision(name="X", wanted_mbid="../../settings", existing_mbid="javascript:alert(1)")

    (card,) = collision_cards([odd], lidarr_url="http://lidarr:8686")

    assert card.wanted_musicbrainz == ""
    assert card.add_in_lidarr == ""
    assert card.existing_musicbrainz == ""
    assert card.existing_in_lidarr == ""


def test_the_cards_come_from_the_newest_run_that_planned() -> None:
    rows = [
        _row(status=RunStatus.ERROR, exit_code=1),
        _row(projected_wanted=5, name_collisions=(JUNGLE,)),
        _row(projected_wanted=5, name_collisions=()),
    ]

    view = build_status(rows, now=NOW, tz=NY, lidarr_url="http://lidarr:8686")

    assert [c.name for c in view.collisions] == ["Jungle"]


def test_a_collision_fixed_since_is_not_shown() -> None:
    rows = [_row(projected_wanted=5, name_collisions=()), _row(projected_wanted=5, name_collisions=(JUNGLE,))]

    assert build_status(rows, now=NOW, tz=NY, lidarr_url="http://lidarr:8686").collisions == []


def test_without_a_lidarr_address_there_are_no_lidarr_links() -> None:
    (card,) = collision_cards([JUNGLE], lidarr_url="")

    assert card.existing_in_lidarr == ""
    assert card.add_in_lidarr == ""
    assert card.wanted_musicbrainz  # MusicBrainz links need no configured address


def test_waiting_for_a_download_links_to_wanted_missing_when_lidarr_url_is_set() -> None:
    view = build_status([], now=NOW, tz=NY, lidarr_url="http://lidarr:8686")

    assert view.wanted_missing_url == "http://lidarr:8686/wanted/missing"


def test_without_a_lidarr_address_waiting_for_a_download_has_no_link() -> None:
    view = build_status([], now=NOW, tz=NY)

    assert view.wanted_missing_url == ""


def test_a_stale_refusal_is_not_the_plan_the_cards_come_from() -> None:
    rows = [
        _row(status=RunStatus.STALE, exit_code=3, projected_wanted=5, name_collisions=(JUNGLE,)),
        _row(projected_wanted=7, name_collisions=()),
    ]

    view = build_status(rows, now=NOW, tz=NY, lidarr_url="http://lidarr:8686")

    assert view.collisions == []
    assert view.projected_wanted == 7


# ---------------------------------------------------------------- at a glance (PR B)

from likearr.web.status import STALE_AFTER, coverage, health_glance  # noqa: E402


def test_health_is_all_good_when_the_last_published_run_was() -> None:
    glance = health_glance(_row(), now=NOW, tz=NY)

    assert glance.healthy
    assert glance.problems == []


def test_health_names_what_home_assistant_would_show_amber_for() -> None:
    row = _row(
        status=RunStatus.GUARDED,
        new_conditions=("new-name-collision",),
        guards=("source-shrink: liked_tracks fell by 40%",),
        guard_blocked=(12,),
    )

    glance = health_glance(row, now=NOW, tz=NY)

    assert not glance.healthy
    assert (
        "likearr skipped 1 artist: a different artist with the same name is already in Lidarr - see below.",
        "#collisions",
    ) in glance.problems
    assert ("source-shrink: liked_tracks fell by 40%", "#history") in glance.problems  # no run id: the table


def test_an_advisory_guard_is_a_note_not_a_problem() -> None:
    # projected-wanted holds nothing back and the status stays ok: Home Assistant is green.
    row = _row(guards=("projected wanted is 900, over 500",), guard_blocked=(0,))

    glance = health_glance(row, now=NOW, tz=NY)

    assert glance.healthy
    assert glance.notes == ["projected wanted is 900, over 500"]


def test_a_failed_or_stale_run_needs_attention() -> None:
    failed = health_glance(_row(status=RunStatus.ERROR, message="spotify: token expired"), now=NOW, tz=NY)
    stale = health_glance(_row(status=RunStatus.STALE, projected_wanted=3), now=NOW, tz=NY)

    assert "The last run failed: spotify: token expired." in [t for t, _ in failed.problems]
    assert not stale.healthy


def test_no_run_for_longer_than_home_assistants_stale_sensor_needs_attention() -> None:
    assert STALE_AFTER.total_seconds() == 13 * 3600  # binary_sensor.likearr_stale
    fine = int((NOW - timedelta(hours=12)).timestamp())
    late = int((NOW - timedelta(hours=14)).timestamp())

    assert health_glance(_row(ts=fine), now=NOW, tz=NY).healthy
    glance = health_glance(_row(ts=late), now=NOW, tz=NY)
    assert any("No run for 14 h" in text for text, _ in glance.problems)


def test_a_paused_published_run_is_healthy_not_amber() -> None:
    """Paused is its own state, not a stale or a failure - HA must not light
    amber just because the schedule is off on purpose."""
    row = _row(status=RunStatus.PAUSED, message="scheduled runs are paused: maintenance window")

    glance = health_glance(row, now=NOW, tz=NY)

    assert glance.healthy
    assert glance.problems == []
    assert any("paused" in note for note in glance.notes)


def test_with_no_published_run_the_page_shows_a_problem() -> None:
    glance = health_glance(None, now=NOW, tz=NY)

    assert not glance.healthy
    assert "No run has finished yet" in glance.problems[0][0]
    assert "Home Assistant" not in glance.problems[0][0]


def test_the_stale_no_run_message_points_at_the_scheduler_not_a_cron_job() -> None:
    """The scheduler moved in-process; there is no cron job to check any more."""
    late = int((NOW - timedelta(hours=14)).timestamp())

    glance = health_glance(_row(ts=late), now=NOW, tz=NY)

    text = next(t for t, _ in glance.problems if t.startswith("No run for"))
    assert "check the scheduler (Settings" in text
    assert "cron" not in text and "Home Assistant" not in text


def test_a_new_collision_links_to_the_cards_only_when_they_are_shown() -> None:
    row = _row(status=RunStatus.DEGRADED, new_conditions=("new-name-collision",))

    glance = health_glance(row, now=NOW, tz=NY, collisions_shown=False)

    assert glance.problems == [
        ("likearr skipped 1 artist: a different artist with the same name is already in Lidarr", "#history")
    ]


# ---------------------------------------------------------------- first-run checklist


def test_the_checklist_is_gone_once_a_token_exists_and_a_run_published() -> None:
    assert first_run_checklist(has_token=True, published=True) is None


def test_a_fresh_install_shows_every_step_unticked_but_lidarr_is_link_only() -> None:
    steps = first_run_checklist(has_token=False, published=False)

    assert steps is not None
    labels = [s.label for s in steps]
    assert labels == ["Connect Spotify", "Set up Lidarr", "Check for changes"]
    assert [s.done for s in steps] == [False, None, False]


def test_the_checklist_ticks_connect_spotify_once_a_token_exists() -> None:
    steps = first_run_checklist(has_token=True, published=False)

    assert steps is not None
    connect, lidarr_step, check = steps
    assert connect.done is True
    assert connect.url == "/settings#spotify"
    assert lidarr_step.done is None  # never a live Lidarr call from Status
    assert lidarr_step.url == "/settings#lidarr-setup"
    assert check.done is False
    assert check.url == "/plan"


def test_the_checklist_ticks_check_for_changes_once_a_run_has_published() -> None:
    steps = first_run_checklist(has_token=False, published=True)

    assert steps is not None
    assert [s.done for s in steps] == [False, None, True]


def _last_run(kind: str = "apply", monitor: frozenset | None = None):  # type: ignore[type-arg]
    from likearr.models import ArtistResolution, ReasonKind, Resolution, ResolutionStatus, SourceSnapshot
    from likearr.shell.last_run import LastRun
    from tests.unit.fakes import artist_intent, lidarr_album, lidarr_view, reason, rg, spotify_album, track_intent
    from tests.unit.test_diff import desired_state

    downloaded, waiting, held = rg("rg-1", "One"), rg("rg-2", "Two"), rg("rg-3", "Three")
    liked = [reason(ReasonKind.LIKED, f"t{i}") for i in range(7)]
    tracks = [track_intent(f"Song {i}", spotify_album("A"), spotify_id=f"t{i}") for i in range(7)]
    tracks.append(tracks[0])  # the same song twice (a playlist duplicate): one intent
    follow = artist_intent("Band", spotify_id="sp-band")
    resolutions = {
        liked[0].key: Resolution(liked[0].key, ResolutionStatus.RESOLVED, release_group=downloaded),
        liked[1].key: Resolution(liked[1].key, ResolutionStatus.RESOLVED, release_group=waiting),
        liked[2].key: Resolution(liked[2].key, ResolutionStatus.RESOLVED, release_group=held),
        liked[3].key: Resolution(liked[3].key, ResolutionStatus.UNMAPPED, step="track:none"),
        liked[4].key: Resolution(liked[4].key, ResolutionStatus.UNMAPPED, step="track:excluded:remix"),
        liked[5].key: Resolution(liked[5].key, ResolutionStatus.UNMAPPED, step="error:metadata"),
        liked[6].key: Resolution(liked[6].key, ResolutionStatus.PENDING_ALBUM),
    }
    artists = {
        follow.reason.key: ArtistResolution(
            follow.reason.key, ResolutionStatus.RESOLVED, artist_mbid="artist-1", artist_name="Band"
        )
    }
    return LastRun(
        ran_at=NOW - timedelta(hours=2),
        kind=kind,
        snapshot=SourceSnapshot(
            fetched_at=NOW, artists=(follow,), albums=(), tracks=tuple(tracks), counts={"liked_tracks": 8}
        ),
        resolutions=resolutions,
        artist_resolutions=artists,
        desired=desired_state((downloaded, [liked[0]]), (waiting, [liked[1]]), (held, [liked[2]])),
        view=lidarr_view(
            albums=[
                lidarr_album(downloaded, id=1, monitored=True, files=10),
                lidarr_album(waiting, id=2, monitored=True),
                lidarr_album(held, id=3),
            ]
        ),
        collisions=(),
        monitor=monitor,
    )


def test_coverage_counts_each_intent_once_and_the_outcomes_add_up() -> None:
    c = coverage(_last_run())

    assert c.intents == 8  # 7 songs (one listed twice) and a followed artist
    assert (c.matched_releases, c.matched_artists, c.unmatched, c.excluded, c.lookup_failed, c.pending) == (
        3,
        1,
        1,
        1,
        1,
        1,
    )
    assert c.matched_releases + c.matched_artists + c.unmatched + c.excluded + c.lookup_failed + c.pending == c.intents
    assert (c.releases, c.monitored, c.downloaded, c.waiting, c.not_monitored) == (3, 2, 1, 1, 1)
    assert not c.dry


def test_not_monitored_matches_a_naive_membership_check() -> None:
    """The monitored set is built once, before the comprehension. Its count must still match
    a naive per-item membership check over the unbuilt list."""
    last = _last_run()
    c = coverage(last)

    wanted = list(last.desired.releases)
    albums = {key: last.view.album(key) for key in wanted}
    monitored = [k for k, a in albums.items() if a is not None and a.monitored]
    naive_unmonitored = [k for k in wanted if k not in monitored]

    assert c.not_monitored == len(naive_unmonitored)


def test_an_ambiguous_same_name_match_has_its_own_line() -> None:
    """Two artists share the name, so nothing is monitored. It gets its own line, so
    each line equals the Not added page's card for it."""
    from dataclasses import replace

    from likearr.models import ReasonKind, Resolution, ResolutionStatus
    from tests.unit.fakes import reason

    last = _last_run()
    key = reason(ReasonKind.LIKED, "t0").key
    ambiguous = Resolution(key, ResolutionStatus.UNMAPPED, step="ambiguous:same-name-artists")
    c = coverage(replace(last, resolutions={**last.resolutions, key: ambiguous}))

    assert (c.matched_releases, c.unmatched, c.ambiguous) == (2, 1, 1)
    total = (
        c.matched_releases + c.matched_artists + c.unmatched + c.ambiguous + c.excluded + c.lookup_failed + c.pending
    )
    assert total == c.intents


def test_after_a_dry_run_not_monitored_says_what_the_plan_would_monitor() -> None:
    from likearr.models import ReleaseKey

    c = coverage(_last_run(kind="dry run", monitor=frozenset({ReleaseKey("artist-1", "rg-3")})))

    assert c.dry
    assert c.would_monitor == 1


def test_an_advisory_guard_leaves_a_run_green() -> None:
    # The Jungle name-collision guard holds nothing back; the run is ok, as Home Assistant sees it.
    summary = describe_run(_row(guards=("skipped Jungle",), guard_blocked=(0,)), now=NOW, tz=NY)

    assert summary.tone == "ok"
    assert summary.guards == ()


# ---------------------------------------------------------------- the banner's sentences

from likearr.web.status import condition_sentence  # noqa: E402

SKIPPED_JUNGLE = NameCollision(name="Jungle", wanted_mbid="w", existing_lidarr_id=9003)


@pytest.mark.parametrize(
    ("condition", "fields", "collisions", "expected", "anchor"),
    [
        (
            "new-name-collision",
            {"name_collisions_new": 1},
            (SKIPPED_JUNGLE,),
            "likearr skipped Jungle: a different artist with the same name is already in Lidarr - see below.",
            "#collisions",
        ),
        (
            "new-name-collision",
            {"name_collisions_new": 2},
            (SKIPPED_JUNGLE, NameCollision(name="Lawrence", wanted_mbid="l")),
            "likearr skipped 2 artists (Jungle, Lawrence): a different artist with the same name is already in "
            "Lidarr - see below.",
            "#collisions",
        ),
        (
            "mapping-shortfall-jump",
            {"regressions": 12, "unmapped_new": 3},
            (),
            "12 releases that matched at the last run no longer match, and 3 songs or albums newly couldn't be "
            "matched.",
            "/unmatched",
        ),
        (
            "mapping-shortfall-jump",
            {"regressions": 1},
            (),
            "1 release that matched at the last run no longer match.",
            "/unmatched",
        ),
        (
            "mb-outage",
            {},
            (),
            "MusicBrainz lookups failed this run, so some songs weren't matched; likearr tries again next run.",
            "/unmatched",
        ),
        (
            "lidarr-metadata-outage",
            {"lidarr_metadata_errors_new": 40},
            (),
            "Lidarr's metadata server failed most lookups this run (40 failed); the artists affected are tried "
            "again next run.",
            "run",
        ),
        (
            "new-skipped-artist",
            {"skipped_artists_new": 2},
            (),
            "2 artists were skipped because Lidarr couldn't look them up; likearr tries again next run.",
            "run",
        ),
        (
            "new-catalogue-too-large",
            {"catalogue_too_large_new": 1},
            (),
            "1 followed artist has more releases than MusicBrainz lets likearr read, so only part of their "
            "catalogue is wanted.",
            "run",
        ),
        (
            "spotify-schema",
            {},
            (),
            "Spotify's answer was incomplete, so this run held back every unmonitor. If it keeps happening, "
            "Spotify has changed something.",
            "run",
        ),
    ],
)
def test_each_new_condition_reads_as_one_plain_sentence(condition, fields, collisions, expected, anchor) -> None:
    assert condition_sentence(condition, _record(**fields), collisions) == (expected, anchor)


def test_an_unknown_condition_falls_back_to_the_clis_words() -> None:
    assert condition_sentence("something-new", _record()) == ("something-new", "run")


def test_the_banner_uses_the_sentences_and_names_the_collision() -> None:
    row = _row(
        status=RunStatus.DEGRADED,
        new_conditions=("new-name-collision",),
        name_collisions_new=1,
        name_collisions=(SKIPPED_JUNGLE,),
    )

    glance = health_glance(row, now=NOW, tz=NY)
    hidden = health_glance(row, now=NOW, tz=NY, collisions_shown=False)

    assert glance.problems == [
        (
            "likearr skipped Jungle: a different artist with the same name is already in Lidarr - see below.",
            "#collisions",
        )
    ]
    assert hidden.problems == [
        ("likearr skipped Jungle: a different artist with the same name is already in Lidarr", "#history")
    ]


# ---------------------------------------------------------------- part-way and part-failed applies


def test_the_collision_card_goes_once_a_newer_check_no_longer_reports_it() -> None:
    older = _row(ts=int(NOW.timestamp()) - 3600, projected_wanted=5, name_collisions=(JUNGLE,))
    newer = _row(ts=int(NOW.timestamp()) - 60, dry_run=True, projected_wanted=5, name_collisions=())

    assert build_status([older], now=NOW, tz=NY).collisions[0].wanted_mbid == JUNGLE.wanted_mbid
    assert build_status([newer, older], now=NOW, tz=NY).collisions == []


def test_an_apply_that_stopped_part_way_is_the_last_applied_and_says_how_far_it_got() -> None:
    partial = _row(
        status=RunStatus.ERROR,
        exit_code=1,
        message="the apply stopped part-way: 12 of 40 changes made: lidarr PUT /album/monitor: 500",
        changes_made=12,
        changes_planned=40,
    )

    view = build_status([partial], now=NOW, tz=NY)

    assert view.last_applied is not None
    assert view.last_applied.headline == "Stopped part-way: 12 of 40 changes made. lidarr PUT /album/monitor: 500"
    assert view.last_applied.tone == "bad"


def test_an_apply_that_fully_landed_but_lost_its_reply_is_not_read_as_part_way() -> None:
    """A batch Lidarr fully applied, whose confirmation was then lost, is not "3 of 3
    changes made" - that reads as partial when nothing was actually left undone."""
    landed = _row(
        status=RunStatus.ERROR,
        exit_code=1,
        message="the apply finished: all 3 planned changes were made, but confirming it failed: "
        "lidarr PUT /album/monitor: 500",
        changes_made=3,
        changes_planned=3,
    )

    view = build_status([landed], now=NOW, tz=NY)

    assert view.last_applied is not None
    assert "part-way" not in view.last_applied.headline
    assert view.last_applied.headline == (
        "Finished: all 3 planned changes made, but confirming it failed. lidarr PUT /album/monitor: 500"
    )
    assert view.last_applied.tone == "bad"


def test_an_apply_that_failed_before_changing_anything_is_not_last_applied_and_says_so() -> None:
    failed = _row(
        status=RunStatus.ERROR,
        exit_code=1,
        message="the apply failed before changing anything: lidarr GET /artist: 503",
        changes_made=0,
        changes_planned=40,
        lidarr_changed=False,
    )
    older_record = _row(status=RunStatus.ERROR, exit_code=1, message="lidarr down")  # older record: unknown

    view = build_status([failed], now=NOW, tz=NY)

    assert view.last_applied is None
    assert view.history[0].headline == "Failed, and changed nothing: lidarr GET /artist: 503"
    assert describe_run(older_record, now=NOW, tz=NY).headline == "Failed: lidarr down"


def test_an_apply_that_changed_settings_but_no_counted_change_says_so_honestly() -> None:
    written = _row(
        status=RunStatus.ERROR,
        exit_code=1,
        message=(
            "the apply stopped part-way: Lidarr settings may have changed, but none of the 3 planned changes "
            "was made: lidarr POST /command: 502"
        ),
        changes_made=0,
        changes_planned=3,
        lidarr_changed=True,
    )

    view = build_status([written], now=NOW, tz=NY)

    assert view.last_applied is not None  # it changed Lidarr, so it is the last applied
    assert view.last_applied.headline == (
        "Stopped part-way: Lidarr settings may have changed, no planned change made. lidarr POST /command: 502"
    )
    assert "changed nothing" not in view.last_applied.headline


def test_a_hand_run_that_found_the_lock_held_reads_as_busy_not_as_a_failure() -> None:
    busy = _row(
        status=RunStatus.ERROR,
        exit_code=4,
        message="another run holds the lock",
        changes_made=0,
        lidarr_changed=False,
    )

    summary = describe_run(busy, now=NOW, tz=NY)

    assert summary.headline == "Didn't run: another run held the lock."
    assert summary.tone == "quiet" and not summary.applied


def test_a_successful_apply_that_made_fewer_than_planned_reads_as_applied() -> None:
    summary = describe_run(_row(changes_made=3, changes_planned=5, lidarr_changed=True), now=NOW, tz=NY)

    assert summary.applied and summary.headline.startswith("Applied:")


def test_changed_nothing_is_said_only_when_the_record_knows_nothing_was_written() -> None:
    """changes_made 0 without lidarr_changed (a record that cannot say) is a plain failure."""
    unsure = _row(status=RunStatus.ERROR, exit_code=1, message="lidarr down", changes_made=0, lidarr_changed=None)

    assert describe_run(unsure, now=NOW, tz=NY).headline == "Failed: lidarr down"


# ---------------------------------------------------------------- run history's Message column


def test_short_message_leaves_a_short_message_unchanged() -> None:
    assert short_message("all good") == "all good"
    assert short_message("") == ""


def test_short_message_cuts_at_the_first_sentence_within_the_limit() -> None:
    message = "Spotify quota exceeded. " + "x" * 200

    assert short_message(message) == "Spotify quota exceeded."


def test_short_message_falls_back_to_a_character_cut_with_an_ellipsis() -> None:
    """No sentence break within the limit at all - one long word, say a URL."""
    message = "a" * 200

    result = short_message(message)

    assert result == "a" * 120 + "…"


def test_short_message_never_invents_text_it_did_not_truncate_from() -> None:
    message = "x" * 500

    result = short_message(message)

    assert result != message
    assert message.startswith(result.rstrip("…"))
