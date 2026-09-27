"""Plans in the web UI: the lifecycle of a saved dry run, and the diff as plain-language sections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from likearr.models import (
    AddArtist,
    Guard,
    MonitorRelease,
    NameCollision,
    Profile,
    ProfileRatchet,
    Reason,
    ReasonKind,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    UnmonitorRelease,
)
from likearr.web import plans
from likearr.web.jobs import JobMeta, JobState
from likearr.web.plans import (
    EXPIRE_AFTER,
    PAGE_SIZE,
    SECTIONS,
    PlanState,
    describe_reasons,
    describe_step,
    plan_state,
    section_rows,
    select_rows,
)
from tests.adapters.test_state_sqlite import _diff

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
A1 = "a1a1a1a1-1111-2222-3333-444444444444"
RG1 = "b1b1b1b1-1111-2222-3333-444444444444"


def _meta(state: JobState = JobState.DONE, finished: datetime = NOW - timedelta(hours=1)) -> JobMeta:
    return JobMeta(
        id="2026-09-23T17-00-00Z-a1b2c3",
        kind="plan",
        argv=[],
        label="",
        started_at=(finished - timedelta(minutes=5)).isoformat(),
        finished_at=finished.isoformat(),
        exit_code=0,
        state=state,
        drain=False,
    )


FINGERPRINT = {"rules": {"deny_releases": []}, "guards": {"max_unmonitors_scheduled": 100}}


# ---------------------------------------------------------------- lifecycle


def test_a_fresh_finished_plan_is_reviewable() -> None:
    state = plan_state(_meta(), FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW)

    assert state == PlanState("reviewable", "")


def test_a_guarded_plan_is_reviewable_too() -> None:
    assert plan_state(_meta(JobState.GUARDED), FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW).reviewable


def test_an_apply_since_supersedes_a_plan() -> None:
    state = plan_state(_meta(), FINGERPRINT, FINGERPRINT, applied_since=True, now=NOW)

    assert state.name == "superseded"
    assert "applied since" in state.why


def test_a_config_change_since_supersedes_a_plan() -> None:
    changed = {"rules": {"deny_releases": [RG1]}, "guards": {"max_unmonitors_scheduled": 100}}

    state = plan_state(_meta(), FINGERPRINT, changed, applied_since=False, now=NOW)

    assert state.name == "superseded"
    assert "deny_releases" in state.why


def test_a_plan_that_recorded_no_settings_is_superseded() -> None:
    # `apply` refuses a diff without a config fingerprint (#27); the page must not offer it.
    assert plan_state(_meta(), None, FINGERPRINT, applied_since=False, now=NOW).name == "superseded"


def test_a_plan_left_a_week_expires() -> None:
    old = _meta(finished=NOW - EXPIRE_AFTER - timedelta(minutes=1))

    assert plan_state(old, FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW).name == "expired"


def test_a_plan_that_did_not_finish_well_is_not_a_plan() -> None:
    for job_state in (JobState.RUNNING, JobState.FAILED, JobState.BUSY, JobState.CANCELLED, JobState.INTERRUPTED):
        state = plan_state(_meta(job_state), FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW)
        assert not state.reviewable
        assert state.name == str(job_state)


# ---------------------------------------------------------------- plain language


def test_reasons_read_as_what_the_user_did() -> None:
    reasons = frozenset(
        {
            Reason(ReasonKind.LIKED, "t1"),
            Reason(ReasonKind.LIKED, "t2"),
            Reason(ReasonKind.SAVED, "al1"),
            Reason(ReasonKind.PLAYLIST, "t3", playlist_id="pl1"),
            Reason(ReasonKind.FOLLOWED, "ar1"),
        }
    )

    text = describe_reasons(reasons, playlist_names={"pl1": "Road trip"})

    assert text == "you follow the artist; you saved the album; 2 liked songs; a song in Road trip"


def test_steps_read_as_how_it_was_found() -> None:
    assert describe_step("track:album:isrc") == "a liked song's album, found by its ISRC"
    assert describe_step("followed:catalogue") == "a followed artist's catalogue"
    assert describe_step("something:new") == "something:new"


def test_every_step_the_resolver_emits_is_said_in_words() -> None:
    """#76 showed `track:title->album` raw in "Found by". Every literal step in the resolver must
    have words; a new step without them fails here instead of on the page."""
    import re
    from pathlib import Path

    import likearr.core.resolver as resolver

    steps = set(re.findall(r'"((?:track|album|artist|followed):[a-z:>_-]+)"', Path(resolver.__file__).read_text()))
    raw = [s for s in steps if not s.endswith(":") and describe_step(s) == s]
    assert raw == []


def test_every_section_has_rows_in_plain_language() -> None:
    diff = _diff()
    diff.ratchets.append(
        ProfileRatchet(artist_mbid=A1, name="Artist", to_profile=Profile.FULL, because="a liked single")
    )
    diff.monitor_artists.append(A1)
    diff.refresh_artists.append(A1)
    diff.update_reasons.append((ReleaseKey(A1, RG1), frozenset({Reason(ReasonKind.SAVED, "al9")})))
    diff.name_collisions.append(NameCollision(name="Jungle", wanted_mbid=A1, existing_lidarr_id=9003))
    diff.unmapped.append(
        Resolution(intent_key="liked:t9", status=ResolutionStatus.UNMAPPED, detail="no MusicBrainz match")
    )

    rows = {name: section_rows(diff, name, artist_names={"a1": "Fake Band"}, playlist_names={}) for name in SECTIONS}

    assert rows["add_artists"][0]["Artist"] == "Artist"
    assert rows["monitor"][0]["Release"] == "Some Album"
    assert rows["monitor"][0]["Artist"] == "Fake Band"
    assert "Found by" in rows["monitor"][0]
    assert rows["unmonitor"][0]["Why it's no longer needed"]
    assert rows["ratchets"][0]["Why"] == "a liked single"
    assert rows["monitor_artists"][0]["Artist"]
    assert rows["set_new_items_none"] == [{"Artist": "Fake Band"}]
    assert rows["refresh_artists"][0]["Artist"]
    assert rows["update_reasons"][0]["Now wanted because"] == "you saved the album"
    assert rows["guards"][0]["Held back"] == "2 unmonitors"
    assert rows["name_collisions"][0]["Name"] == "Jungle"
    assert rows["pending"][0]
    assert rows["unmapped"][-1]["Why"] == "no MusicBrainz match"


def test_an_artist_nobody_named_is_shown_by_its_mbid() -> None:
    diff = _diff()
    diff.monitor.append(
        MonitorRelease(key=ReleaseKey(A1, RG1), title="X", reasons=frozenset({Reason(ReasonKind.LIKED, "t")}), step="")
    )

    rows = section_rows(diff, "monitor", artist_names={}, playlist_names={})

    assert rows[-1]["Artist"] == A1


def test_filtering_matches_any_column_case_insensitively_and_pages() -> None:
    rows = [{"Release": f"Album {i}", "Artist": "Band" if i % 2 else "Other"} for i in range(PAGE_SIZE * 3)]

    page, total, pages = select_rows(rows, query="band", page=2)

    assert total == PAGE_SIZE * 3 // 2
    assert pages == 2
    assert len(page) == total - PAGE_SIZE
    assert all(r["Artist"] == "Band" for r in page)


@pytest.mark.parametrize("page", [0, -3, 999])
def test_an_out_of_range_page_is_clamped(page: int) -> None:
    rows = [{"x": str(i)} for i in range(5)]

    shown, total, pages = select_rows(rows, query="", page=page)

    assert total == 5
    assert pages == 1
    assert shown == rows


def test_guards_rows_name_the_guard(tmp_path: object) -> None:
    diff = _diff()
    diff.guards.append(Guard(code="artist-shrink", message="an artist shrank", blocked_unmonitors=1, subject=A1))

    rows = section_rows(diff, "guards", artist_names={}, playlist_names={})

    assert rows[-1]["Guard"] == "an artist shrank"
    assert rows[-1]["Held back"] == "1 unmonitor"


def test_the_add_rows_say_which_profile() -> None:
    diff = _diff()
    diff.add_artists.append(AddArtist(artist_mbid=A1, name="New", profile=Profile.FULL))

    rows = section_rows(diff, "add_artists", artist_names={}, playlist_names={})

    assert rows[-1]["Profile"] == "Full: every release type"
    assert rows[0]["Profile"] == "Lean: studio albums and EPs"


def test_the_unmonitor_rows_carry_the_lost_reasons() -> None:
    diff = _diff()
    diff.unmonitor.append(
        UnmonitorRelease(key=ReleaseKey(A1, RG1), title="Gone", lost_reasons=frozenset({Reason(ReasonKind.LIKED, "t")}))
    )

    rows = section_rows(diff, "unmonitor", artist_names={A1: "Band"}, playlist_names={})

    assert rows[-1] == {
        "Release": "Gone",
        "Artist": "Band",
        "Why it's no longer needed": "Your liked song no longer points here",
    }


# ---------------------------------------------------------------- why no longer needed

SAVED = Reason(ReasonKind.SAVED, "sp-tease-me")
SINGLE = "214fcd9f-0000-0000-0000-000000000000"
ALBUM = "78f6013b-0000-0000-0000-000000000000"
EP = "258dda10-0000-0000-0000-000000000000"


def _moved_diff():
    diff = _diff()
    diff.monitor[:] = [MonitorRelease(ReleaseKey(A1, ALBUM), "Tease Me", frozenset({SAVED}), "album:upc")]
    diff.unmonitor[:] = [UnmonitorRelease(ReleaseKey(A1, SINGLE), "Tease Me", frozenset({SAVED}))]
    return diff


def test_a_saved_album_that_moved_to_a_release_in_this_plan_says_so_in_plain_text() -> None:
    from likearr.core.explain import release_label
    from likearr.web.plans import whereabouts

    diff = _moved_diff()
    labels = {
        ALBUM: release_label("Tease Me", "Album", [], "1992-06-15"),
        SINGLE: release_label("Tease Me", "Single", [], "1993-01-01"),
    }

    unmonitor = section_rows(
        diff, "unmonitor", artist_names={}, playlist_names={}, where=whereabouts(diff, labels=labels)
    )[0]
    monitor = section_rows(diff, "monitor", artist_names={}, playlist_names={}, where=whereabouts(diff, labels=labels))[
        0
    ]

    assert unmonitor["Release"] == "Tease Me (single, 1993)"
    assert unmonitor["Why it's no longer needed"] == "Your saved album now matches Tease Me (album, 1992) instead"
    assert "_href:Why it's no longer needed" not in unmonitor  # no link: the row may be on another page
    assert monitor["Release"] == "Tease Me (album, 1992)"
    assert monitor["_id"] == f"monitor-{ALBUM}"

    html = (
        Environment(loader=FileSystemLoader(Path(plans.__file__).parent / "templates"), autoescape=True)
        .get_template("_section.html")
        .render(
            section={"name": "unmonitor"},
            rows=[unmonitor],
            columns=[c for c in unmonitor if not c.startswith("_")],
            job_id="j",
            pages=1,
        )
    )
    why_cell = html.split("<td>")[-1]
    assert "Tease Me (album, 1992)" in why_cell and "<a" not in why_cell


def test_a_saved_album_that_moved_to_a_release_already_wanted_says_so() -> None:
    from likearr.core.explain import release_label
    from likearr.web.plans import whereabouts

    diff = _diff()
    diff.monitor[:] = []
    diff.unmonitor[:] = [UnmonitorRelease(ReleaseKey(A1, SINGLE), "I'm With You", frozenset({SAVED}))]
    where = whereabouts(
        diff, desired_reasons={SAVED.key: EP}, labels={EP: release_label("I'm With You", "EP", [], "2011")}
    )

    row = section_rows(diff, "unmonitor", artist_names={}, playlist_names={}, where=where)[0]

    assert (
        row["Why it's no longer needed"]
        == "Your saved album now matches I'm With You (EP, 2011) instead, which is already wanted"
    )


def test_a_reason_gone_from_spotify_or_unmatched_says_which() -> None:
    from likearr.web.plans import whereabouts

    liked, listed = Reason(ReasonKind.LIKED, "t1"), Reason(ReasonKind.PLAYLIST, "t2", playlist_id="pl1")
    diff = _diff()
    diff.unmonitor[:] = [
        UnmonitorRelease(ReleaseKey(A1, SINGLE), "A", frozenset({SAVED})),
        UnmonitorRelease(ReleaseKey(A1, ALBUM), "B", frozenset({liked})),
        UnmonitorRelease(ReleaseKey(A1, EP), "C", frozenset({listed})),
    ]
    where = whereabouts(diff, live=frozenset({liked.key}), unmatched=frozenset({liked.key}))

    rows = section_rows(diff, "unmonitor", artist_names={}, playlist_names={"pl1": "Road trip"}, where=where)

    assert [r["Why it's no longer needed"] for r in rows] == [
        "You no longer have this album saved",
        "Likearr can no longer match your liked song (see Missing)",
        'The song is no longer in "Road trip"',
    ]


def test_a_followed_artists_catalogue_never_counts_as_a_move() -> None:
    from likearr.web.plans import whereabouts

    follow = Reason(ReasonKind.FOLLOWED, "ar1")
    diff = _diff()
    diff.monitor[:] = [MonitorRelease(ReleaseKey(A1, ALBUM), "New", frozenset({follow}), "followed:catalogue")]
    diff.unmonitor[:] = [UnmonitorRelease(ReleaseKey(A1, SINGLE), "Old", frozenset({follow}))]

    live = section_rows(
        diff, "unmonitor", artist_names={}, playlist_names={}, where=whereabouts(diff, live=frozenset({follow.key}))
    )
    gone = section_rows(
        diff, "unmonitor", artist_names={}, playlist_names={}, where=whereabouts(diff, live=frozenset())
    )

    assert live[0]["Why it's no longer needed"].startswith("It no longer counts among the studio albums and EPs")
    assert gone[0]["Why it's no longer needed"] == "You no longer follow the artist"


def test_a_playlist_with_no_name_yet_is_loading_never_a_raw_id() -> None:
    diff = _diff()
    listed = Reason(ReasonKind.PLAYLIST, "t2", playlist_id="fakeplaylist0000000044")
    diff.monitor[:] = [MonitorRelease(ReleaseKey(A1, ALBUM), "X", frozenset({listed}), "")]
    diff.update_reasons[:] = [(ReleaseKey(A1, EP), frozenset({listed}))]

    monitor = section_rows(diff, "monitor", artist_names={}, playlist_names={})[0]
    update = section_rows(diff, "update_reasons", artist_names={}, playlist_names={})[0]

    for row, column in ((monitor, "Wanted because"), (update, "Now wanted because")):
        assert row[column] == "a song in a playlist (loading names…)"
        assert "fakeplaylist0000000044" not in row[column]
        assert row[f"_href:{column}"] == "https://open.spotify.com/playlist/fakeplaylist0000000044"


def test_release_labels_say_the_type_and_year() -> None:
    from likearr.core.explain import release_label

    assert release_label("Tease Me", "Single", [], "1993-02-01") == "Tease Me (single, 1993)"
    assert release_label("Alive", "Album", ["Live"], "2001") == "Alive (live album, 2001)"
    assert release_label("Mystery", "", [], "") == "Mystery (release)"


def test_an_apply_after_the_plan_supersedes_it_and_a_dry_run_does_not() -> None:
    from likearr.models import RunStatus
    from likearr.web.plans import applied_since
    from tests.adapters.test_state_sqlite import _health_record

    finished = datetime(2026, 9, 23, 17, 0, tzinfo=UTC)
    later = int(finished.timestamp()) + 60
    earlier = int(finished.timestamp()) - 60

    changed = {"followed_artists": 3, "monitored": 2}
    assert applied_since([_health_record(ts=later, dry_run=False, counts=changed)], finished.isoformat())
    assert not applied_since([_health_record(ts=later, dry_run=True, counts=changed)], finished.isoformat())
    assert not applied_since([_health_record(ts=earlier, dry_run=False, counts=changed)], finished.isoformat())
    assert not applied_since(
        [_health_record(ts=later, dry_run=False, status=RunStatus.STALE, exit_code=3, counts=changed)],
        finished.isoformat(),
    )


def test_an_apply_that_changed_nothing_does_not_supersede_a_plan() -> None:
    # A cron apply every 6 h with nothing to do would otherwise kill every plan (#35 review).
    from likearr.web.plans import applied_since
    from tests.adapters.test_state_sqlite import _health_record

    finished = datetime(2026, 9, 23, 17, 0, tzinfo=UTC)
    later = int(finished.timestamp()) + 60
    idle = {"followed_artists": 3, "monitored": 0, "unmonitored": 0, "added": 0, "new_items_none": 0}

    assert not applied_since([_health_record(ts=later, dry_run=False, counts=idle)], finished.isoformat())
    for key in ("monitored", "unmonitored", "added", "new_items_none"):
        busy = {**idle, key: 1}
        assert applied_since([_health_record(ts=later, dry_run=False, counts=busy)], finished.isoformat())


def test_a_plan_from_an_older_resolver_is_superseded() -> None:
    from likearr.models import RESOLVER_VERSION

    state = plan_state(
        _meta(), FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW, resolver_version=RESOLVER_VERSION - 1
    )

    assert state.name == "superseded"
    assert "older likearr" in state.why
    assert plan_state(
        _meta(), FINGERPRINT, FINGERPRINT, applied_since=False, now=NOW, resolver_version=RESOLVER_VERSION
    ).reviewable


def test_a_reason_update_row_names_its_release() -> None:
    diff = _diff()
    diff.update_reasons.append((ReleaseKey(A1, RG1), frozenset({Reason(ReasonKind.SAVED, "al9")})))

    named = section_rows(diff, "update_reasons", artist_names={}, playlist_names={}, release_titles={RG1: "Kid A"})
    unnamed = section_rows(diff, "update_reasons", artist_names={}, playlist_names={})

    assert named[-1]["Release"] == "Kid A"
    assert unnamed[-1]["Release"] == RG1
    assert unnamed[-1]["_href:Release"] == f"https://musicbrainz.org/release-group/{RG1}"
    assert "_href:Release" not in named[-1]


# ---------------------------------------------------------------- the plan token (#30)


def test_the_plan_token_binds_the_job_to_what_its_diff_says() -> None:
    from likearr.web.plans import plan_token

    raw = {
        "source_digest": "s1",
        "lidarr_digest": "l1",
        "resolver_version": 9,
        "accept_shrink": False,
        "config_fingerprint": {"rules": {"b": 1, "a": 2}, "guards": {}},
    }
    token = plan_token("job-1", raw)

    assert token == plan_token("job-1", dict(raw, config_fingerprint={"guards": {}, "rules": {"a": 2, "b": 1}}))
    for changed in (
        ("job", "job-2"),
        ("source_digest", "s2"),
        ("lidarr_digest", "l2"),
        ("resolver_version", 10),
        ("accept_shrink", True),
        ("config_fingerprint", {"rules": {}, "guards": {}}),
    ):
        key, value = changed
        other = plan_token(str(value), raw) if key == "job" else plan_token("job-1", dict(raw, **{key: value}))
        assert other != token, key


def test_the_plan_token_of_a_file_covers_its_contents(tmp_path: object) -> None:
    import json as _json
    from pathlib import Path as _Path

    from likearr.web.plans import plan_token_of_file

    path = _Path(str(tmp_path)) / "diff.json"
    raw = {"source_digest": "s", "lidarr_digest": "l", "resolver_version": 9, "monitor": [{"k": 1}, {"k": 2}]}
    path.write_text(_json.dumps(raw))
    before = plan_token_of_file("job-1", path)
    path.write_text(_json.dumps({**raw, "monitor": [{"k": 1}]}))

    assert plan_token_of_file("job-1", path) != before


def test_the_summary_counts_unmonitors_that_were_replaced_not_dropped() -> None:
    from likearr.web.plans import replaced_count, whereabouts

    diff = _moved_diff()
    diff.unmonitor.append(UnmonitorRelease(ReleaseKey(A1, EP), "Gone", frozenset({Reason(ReasonKind.LIKED, "t9")})))

    assert replaced_count(diff, whereabouts(diff)) == 1


def test_the_on_disk_column_says_what_stays_on_disk() -> None:
    from likearr.models import ReleaseKey, UnmonitorRelease
    from likearr.web.plans import FILES_UNAVAILABLE, on_disk_labels

    diff = _diff()
    diff.unmonitor[:] = [
        UnmonitorRelease(ReleaseKey("a1", rg), rg, frozenset()) for rg in ("many", "one", "none", "gone", "odd")
    ]
    answer = {
        "albums": {
            "many": {"track_files": 12, "size_on_disk": 1},
            "one": {"track_files": 1, "size_on_disk": 1},
            "none": {"track_files": 0, "size_on_disk": 0},
            "odd": {"track_files": "12"},
        },
        "missing": ["gone"],
    }

    assert on_disk_labels(diff, answer) == {
        "many": "12 files on disk (stay where they are)",
        "one": "1 file on disk (stay where they are)",
        "none": "no files",
        "gone": "not in Lidarr any more",
        "odd": FILES_UNAVAILABLE,  # never a number that was not one
    }
    assert set(on_disk_labels(diff, FILES_UNAVAILABLE).values()) == {FILES_UNAVAILABLE}
    rows = section_rows(diff, "unmonitor", artist_names={}, playlist_names={}, on_disk=on_disk_labels(diff, answer))
    assert rows[0]["On disk"] == "12 files on disk (stay where they are)"
    assert "On disk" not in section_rows(diff, "unmonitor", artist_names={}, playlist_names={})[0]


def test_an_apply_that_stopped_part_way_supersedes_a_plan() -> None:
    """#54: it changed Lidarr before it failed, so the plan no longer describes Lidarr."""
    from likearr.models import RunStatus
    from likearr.web.plans import applied_since
    from tests.adapters.test_state_sqlite import _health_record

    finished = datetime(2026, 9, 23, 17, 0, tzinfo=UTC)
    later = int(finished.timestamp()) + 60
    partial = {"monitored": 12}

    def failed(made: int | None) -> list:
        return [
            _health_record(
                ts=later, dry_run=False, status=RunStatus.ERROR, exit_code=1, counts=partial, changes_made=made
            )
        ]

    assert applied_since(failed(12), finished.isoformat())
    assert not applied_since(failed(0), finished.isoformat())
    assert not applied_since(failed(None), finished.isoformat())


# ---------------------------------------------------------------- run_change_sections (#76)


def test_run_change_sections_names_every_row_and_leaves_empty_sections_out() -> None:
    from likearr.web.plans import run_change_sections

    # add_artists, monitor, unmonitor and set_new_items_none: 1 each; ratchets, monitor_artists and
    # refresh_artists empty
    diff = _diff()

    sections = run_change_sections(diff, artist_names={"a1": "Some Band"}, playlist_names={})

    names = {ctx["section"].name for ctx in sections}
    assert names == {"add_artists", "monitor", "unmonitor", "set_new_items_none"}
    add_artists = next(ctx for ctx in sections if ctx["section"].name == "add_artists")
    assert add_artists["rows"][0]["Artist"] == "Artist"
    assert add_artists["total"] == 1
    assert add_artists["capped"] is False


def test_run_change_sections_omits_update_reasons_even_when_present() -> None:
    from likearr.web.plans import run_change_sections

    diff = _diff()
    diff.update_reasons.append((ReleaseKey(A1, RG1), frozenset({Reason(ReasonKind.SAVED, "al9")})))

    sections = run_change_sections(diff, artist_names={}, playlist_names={})

    assert "update_reasons" not in {ctx["section"].name for ctx in sections}


def test_run_change_sections_caps_a_long_section_and_says_so() -> None:
    from likearr.web.plans import run_change_sections

    diff = _diff()
    diff.monitor.clear()
    for i in range(12):
        diff.monitor.append(
            MonitorRelease(
                key=ReleaseKey(A1, f"rg-{i}"),
                title=f"Album {i}",
                reasons=frozenset({Reason(ReasonKind.LIKED, f"t{i}")}),
                step="",
            )
        )

    capped = run_change_sections(diff, artist_names={}, playlist_names={}, cap=10)
    uncapped = run_change_sections(diff, artist_names={}, playlist_names={}, cap=None)

    monitor = next(ctx for ctx in capped if ctx["section"].name == "monitor")
    assert monitor["total"] == 12
    assert len(monitor["rows"]) == 10
    assert monitor["capped"] is True
    monitor_full = next(ctx for ctx in uncapped if ctx["section"].name == "monitor")
    assert len(monitor_full["rows"]) == 12
    assert monitor_full["capped"] is False


# ---------------------------------------------------------------- "Monitor New Albums" (#172)


def test_the_monitor_new_albums_write_is_a_section_right_after_the_re_monitored_artists() -> None:
    from likearr.web.plans import RUN_CHANGE_SECTIONS

    names = list(SECTIONS)
    assert names[names.index("monitor_artists") + 1] == "set_new_items_none"
    assert SECTIONS["set_new_items_none"].title == "Artists to stop auto-monitoring"
    assert "set_new_items_none" in RUN_CHANGE_SECTIONS
    assert list(RUN_CHANGE_SECTIONS) == [n for n in SECTIONS if n in RUN_CHANGE_SECTIONS], "SECTIONS' own order"


def test_a_profile_widening_says_what_happens_to_monitor_new_albums_when_it_goes_to_none() -> None:
    """#172: widening to Full must not auto-monitor what it shows, and the review says so."""
    diff = _diff()
    diff.set_new_items_none[:] = [A1]
    diff.ratchets[:] = [
        ProfileRatchet(artist_mbid=A1, name="Was On All", to_profile=Profile.FULL, because="a liked compilation"),
        ProfileRatchet(artist_mbid="a2", name="Already None", to_profile=Profile.FULL, because="a live album"),
    ]

    rows = section_rows(diff, "ratchets", artist_names={}, playlist_names={})

    assert rows[0]["Why"] == (
        'a liked compilation. "Monitor New Albums" is set to None first, so the release types this shows are '
        "not monitored; albums already monitored stay monitored."
    )
    assert rows[1]["Why"] == "a live album"
