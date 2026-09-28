"""The prune review: the report by artist, decisions, carried-over ones, the two exported files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from likearr.core.prune import Protection
from likearr.prune_ledger import Entry, Ledger
from likearr.web.prune import (
    DecisionError,
    Draft,
    PruneArtist,
    PruneRelease,
    PruneView,
    StaleChange,
    decide,
    decisions_file,
    effective_decision,
    kept_why,
    ledger_changes,
    ledger_source,
    net_effect,
    prefill,
    read_draft,
    read_report,
    review_data,
    rows_for_page,
    select_artists,
    summary,
    why_unasked,
    write_draft,
)

BIG = "11111111-1111-1111-1111-111111111111"
SMALL = "22222222-2222-2222-2222-222222222222"
GUARDED = "33333333-3333-3333-3333-333333333333"
RG = [f"{i:08d}-aaaa-bbbb-cccc-dddddddddddd" for i in range(6)]


def dec(draft: Draft, view, artist: str, decision: str | None = None, **albums: str) -> Draft:
    """One change at a time, each against the artist's current revision, as the page does."""
    if decision is not None:
        draft = decide(draft, view, artist, rev=draft.rev(artist), decision=decision)
    for rg, value in albums.items():
        draft = decide(draft, view, artist, rev=draft.rev(artist), release=(rg, value))
    return draft


def _row(artist: str, name: str, rg: str, title: str, size: int, reason: str | None = None) -> dict[str, object]:
    return {
        "artist_mbid": artist,
        "artist_name": name,
        "lidarr_artist_id": 1,
        "rg_mbid": rg,
        "title": title,
        "primary_type": "Album",
        "secondary_types": ["Live"] if title.endswith("Live") else [],
        "release_date": "2010-05-01",
        "track_file_count": 10,
        "size_on_disk": size,
        "lidarr_album_id": 1,
        "path": "",
        "protected_reason": reason,
    }


REPORT = {
    "created_at": "2026-09-23T18:00:00+00:00",
    "summary": {},
    "candidates": [
        _row(BIG, "Big Band", RG[0], "First", 900),
        _row(BIG, "Big Band", RG[1], "Second Live", 800),
        _row(SMALL, "Small Band", RG[2], "Only", 100),
        _row(GUARDED, "Guarded", RG[3], "Loose", 50),
    ],
    # An older report: the protected row says why in `protected_reason` alone.
    "protected": [
        _row(
            GUARDED,
            "Guarded",
            RG[4],
            "Kept",
            60,
            "holds a liked track (liked:faketrack0000000000064) whose album 'Respect' "
            "(bbbbbbbb-0000-4000-8000-000000000064) has no files yet; this is the only copy on disk",
        )
    ],
}


@pytest.fixture
def view(tmp_path: Path):
    path = tmp_path / "prune.json"
    path.write_text(json.dumps(REPORT))
    report = read_report(path)
    assert report is not None
    return report


def test_the_report_reads_grouped_by_artist_largest_first(view) -> None:
    assert [a.name for a in view.artists] == ["Big Band", "Small Band", "Guarded"]
    big = view.artists[0]
    assert big.size == 1700
    assert [r.kind for r in big.releases] == ["Album", "Album + Live"]
    guarded = view.artists[2]
    assert {r.title: r.protected for r in guarded.releases} == {"Kept": True, "Loose": False}
    assert guarded.size == 50  # a protected row is not a candidate


def test_an_unreadable_report_is_none(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{")
    assert read_report(tmp_path / "bad.json") is None
    assert read_report(tmp_path / "missing.json") is None


def test_an_artist_moved_out_whole_is_listed_album_by_album_never_in_trash_artists(view) -> None:
    # prune-stage reads trash_artists as "every candidate in whatever report it is given": a rebuilt
    # report would move albums nobody reviewed. So every album is named.
    draft = dec(Draft(), view, BIG, "trash")

    out = decisions_file(view, draft)

    assert out["trash_artists"] == []
    assert out["trash"] == [RG[0], RG[1]]
    assert set(out) == {
        "version",
        "trash",
        "trash_artists",
        "promote",
        "save",
        "save_releases",
        "save_exclude_releases",
        "notes",
    }


def test_a_kept_album_of_a_trashed_artist_spells_the_trash_out_release_by_release(view) -> None:
    draft = dec(Draft(), view, BIG, "trash", **{RG[0]: "keep"})

    out = decisions_file(view, draft)

    assert out["trash_artists"] == []
    assert out["trash"] == [RG[1]]


def test_one_album_of_a_kept_artist_can_be_trashed(view) -> None:
    draft = dec(Draft(), view, SMALL, "save", **{RG[2]: "trash"})

    out = decisions_file(view, draft)

    assert out["trash"] == [RG[2]]
    assert out["save"] == [SMALL]


def test_promote_and_save_are_artist_decisions(view) -> None:
    draft = dec(dec(Draft(), view, BIG, "promote"), view, SMALL, "save")

    out = decisions_file(view, draft)

    assert out["promote"] == [BIG] and out["save"] == [SMALL]
    assert out["trash"] == [] and out["trash_artists"] == []


def test_a_protected_row_is_never_trashed(view) -> None:
    with pytest.raises(DecisionError, match="Kept is always kept: it holds the only copy of a song you liked"):
        dec(Draft(), view, GUARDED, "keep", **{RG[4]: "trash"})

    draft = dec(Draft(), view, GUARDED, "trash")
    out = decisions_file(view, draft)

    assert out["trash_artists"] == []
    assert out["trash"] == [RG[3]]  # its candidate, never the protected album


@pytest.mark.parametrize(
    ("artist", "decision", "overrides"),
    [
        ("not-an-mbid", "keep", {}),
        ("44444444-4444-4444-4444-444444444444", "keep", {}),
        (BIG, "delete everything", {}),
        (BIG, "keep", {RG[2]: "trash"}),  # another artist's release
        (BIG, "keep", {RG[0]: "burn"}),
    ],
)
def test_only_the_reports_ids_and_the_fixed_choices_are_accepted(view, artist, decision, overrides) -> None:
    with pytest.raises(DecisionError):
        if overrides:
            ((rg, value),) = overrides.items()
            decide(Draft(), view, artist, rev=0, release=(rg, value))
        else:
            decide(Draft(), view, artist, rev=0, decision=decision)


def test_one_change_at_a_time(view) -> None:
    with pytest.raises(DecisionError, match="one change"):
        decide(Draft(), view, BIG, rev=0)
    with pytest.raises(DecisionError, match="one change"):
        decide(Draft(), view, BIG, rev=0, decision="keep", release=(RG[0], "keep"))


def test_a_stale_tab_cannot_undo_a_keep(view) -> None:
    # The exact two-tab case: tab B keeps album First under a moved-out artist; tab A, still
    # showing the row before that, then changes album Second.
    before = dec(Draft(), view, BIG, "trash")
    shown_in_a = before.rev(BIG)
    after_b = decide(before, view, BIG, rev=before.rev(BIG), release=(RG[0], "keep"))

    with pytest.raises(StaleChange):
        decide(after_b, view, BIG, rev=shown_in_a, release=(RG[1], "keep"))

    assert after_b.releases[RG[0]] == "keep"
    assert decisions_file(view, after_b)["trash"] == [RG[1]]


def test_a_change_touches_only_what_it_names(view) -> None:
    draft = dec(Draft(), view, BIG, "trash", **{RG[0]: "keep"})

    draft = decide(draft, view, BIG, rev=draft.rev(BIG), release=(RG[1], "keep"))
    draft = decide(draft, view, BIG, rev=draft.rev(BIG), decision="save")

    assert draft.releases == {RG[0]: "keep", RG[1]: "keep"}
    assert draft.artists == {BIG: "save"}


def test_undecided_takes_the_decision_back(view) -> None:
    draft = dec(dec(Draft(), view, BIG, "trash"), view, BIG, "undecided")

    assert BIG not in draft.artists
    assert decisions_file(view, draft)["trash_artists"] == []


def test_the_review_snapshot_is_what_promote_save_reads(view, tmp_path: Path) -> None:
    from likearr.shell.promote_save import read_reviewed

    draft = dec(Draft(), view, BIG, "save", **{RG[1]: "trash"})
    path = tmp_path / "review-data.json"
    path.write_text(json.dumps(review_data(view, draft)))

    snapshot = read_reviewed(path)

    assert snapshot.allows(BIG, RG[0])
    assert not snapshot.allows(BIG, RG[1])  # moved out: never a Spotify save
    assert snapshot.allows(GUARDED, RG[4])
    big = next(a for a in review_data(view, draft)["artists"] if a["mbid"] == BIG)
    assert big["decision"] == "save"
    assert [r["trash"] for r in big["releases"]] == [False, True]


def test_the_decisions_file_is_what_prune_stage_and_promote_save_read(view, tmp_path: Path) -> None:
    from likearr.shell.promote_save import read_decisions

    draft = dec(dec(Draft(), view, BIG, "promote", **{RG[1]: "trash"}), view, SMALL, "save")
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(decisions_file(view, draft)))

    decisions = read_decisions(path)

    assert decisions.promote == (BIG,) and decisions.save == (SMALL,)


def test_the_draft_survives_a_round_trip_and_bad_values_are_dropped(view, tmp_path: Path) -> None:
    path = tmp_path / "draft.json"
    draft = dec(Draft(notes="checked the live albums"), view, BIG, "trash", **{RG[0]: "keep"})
    write_draft(path, draft)

    assert read_draft(path) == draft
    path.write_text(json.dumps({"artists": {BIG: "nuke"}, "releases": {RG[0]: "trash"}, "notes": 5}))
    assert read_draft(path) == Draft(artists={}, releases={RG[0]: "trash"}, notes="5")
    assert read_draft(tmp_path / "none.json") == Draft()


def test_the_table_filters_by_name_title_and_decision(view) -> None:
    draft = dec(Draft(), view, BIG, "trash")

    by_title, *_ = select_artists(view, draft, query="only", show="", page=1)
    trashed, *_ = select_artists(view, draft, query="", show="trash", page=1)
    undecided, *_ = select_artists(view, draft, query="", show="undecided", page=1)

    assert [a.name for a in by_title] == ["Small Band"]
    assert [a.name for a in trashed] == ["Big Band"]
    assert [a.name for a in undecided] == ["Small Band", "Guarded"]


def test_the_summary_counts_progress_and_what_would_move(view) -> None:
    draft = dec(dec(Draft(), view, BIG, "trash", **{RG[0]: "keep"}), view, SMALL, "save")

    s = summary(view, draft)

    assert (s["artists"], s["decided"], s["trash_albums"], s["trash_bytes"], s["save"]) == (3, 2, 1, 800, 1)


def test_the_export_never_contains_trash_artists(view) -> None:
    draft = dec(dec(dec(Draft(), view, BIG, "trash"), view, SMALL, "trash"), view, GUARDED, "trash")

    assert decisions_file(view, draft)["trash_artists"] == []


def test_albums_moved_out_under_undecided_artists_are_counted(view) -> None:
    draft = decide(Draft(), view, SMALL, rev=0, release=(RG[2], "trash"))

    assert summary(view, draft)["undecided_trash"] == 1
    assert summary(view, draft)["decided"] == 0
    assert decisions_file(view, draft)["trash"] == [RG[2]]  # an explicit choice is still exported


# ---------------------------------------------------------------- why, follows, one-album saves

QUEEN = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
LAWRENCE = "55555555-5555-5555-5555-555555555555"
NOBODY = "66666666-6666-6666-6666-666666666666"
FRG = [f"{i:08d}-eeee-ffff-aaaa-bbbbbbbbbbbb" for i in range(7)]


def _frow(
    artist: str, name: str, rg: str, title: str, primary: str, secondary: list[str], followed: bool, **extra: object
) -> dict[str, object]:
    row = _row(artist, name, rg, title, 100)
    row.update(primary_type=primary, secondary_types=secondary, artist_followed=followed, **extra)
    return row


FOLLOWED_REPORT = {
    "created_at": "2026-09-23T18:00:00+00:00",
    "summary": {},
    "candidates": [
        _frow(QUEEN, "Queen", FRG[0], "Greatest Hits", "Album", ["Compilation"], True),
        _frow(QUEEN, "Queen", FRG[1], "Live Killers", "Album", ["Live"], True),
        _frow(LAWRENCE, "Lawrence", FRG[2], "Casual", "Single", [], True),
        _frow(LAWRENCE, "Lawrence", FRG[3], "Hotel TV", "Album", [], True),
        _frow(NOBODY, "Nobody Much", FRG[4], "Stray", "Album", [], False),
    ],
    "protected": [
        _frow(
            QUEEN,
            "Queen",
            FRG[5],
            "Innuendo",
            "Single",
            [],
            True,
            protected_reason="holds a liked track (liked:t-inn) that is still waiting for an album; ...",
            protection={"kind": "pending_album", "intent_key": "liked:t-inn", "song": "Innuendo"},
        )
    ],
}


@pytest.fixture
def fview(tmp_path: Path):
    path = tmp_path / "followed.json"
    path.write_text(json.dumps(FOLLOWED_REPORT))
    report = read_report(path)
    assert report is not None
    return report


def _why(view, rg: str) -> str:
    return next(r.why for a in view.artists for r in a.releases if r.rg_mbid == rg)


def test_every_candidate_says_why_nothing_asks_for_it(fview) -> None:
    assert _why(fview, FRG[0]) == "Compilation: following Queen brings studio albums and EPs only"
    assert _why(fview, FRG[1]) == "Live: following Queen brings studio albums and EPs only"
    assert _why(fview, FRG[2]) == "Single: following Lawrence brings studio albums and EPs only"
    assert "You follow Lawrence, but this isn't among the studio albums and EPs" in _why(fview, FRG[3])
    assert _why(fview, FRG[4]) == (
        "You don't follow Nobody Much on Spotify, and no liked song, saved album or playlist track "
        "is matched to this release"
    )
    assert _why(fview, FRG[5]) == ""  # protected: its own reason says why it stays
    assert {a.name: a.followed for a in fview.artists} == {"Queen": True, "Lawrence": True, "Nobody Much": False}


def test_a_report_from_before_the_follow_field_renders_with_no_reason(view) -> None:
    assert {a.followed for a in view.artists} == {None}
    assert {r.why for a in view.artists for r in a.releases} == {""}


def test_when_follows_are_unknown_nothing_is_said_about_following() -> None:
    not_read = {"primary_type": "Album", "secondary_types": [], "artist_followed": None}
    unmatched = {**not_read, "artist_followed": False, "follow_unmatched": True}

    assert "follow" not in why_unasked(not_read, "Queen").replace("follows aren't read", "")
    assert why_unasked(not_read, "Queen").startswith("No liked song, saved album or playlist track")
    assert why_unasked(unmatched, "Queen").startswith(
        "likearr couldn't match a Spotify follow named Queen to a MusicBrainz artist"
    )
    assert "You don't follow" not in why_unasked(unmatched, "Queen")


def test_an_artist_already_followed_cannot_be_followed_again(fview) -> None:
    with pytest.raises(DecisionError, match="already followed"):
        decide(Draft(), fview, QUEEN, rev=0, decision="promote")

    assert decide(Draft(), fview, NOBODY, rev=0, decision="promote").artists == {NOBODY: "promote"}


def test_a_draft_follow_of_an_artist_followed_now_is_exported_as_a_keep(fview) -> None:
    draft = Draft(artists={QUEEN: "promote"})

    assert effective_decision(fview.artist(QUEEN), draft) == "keep"
    assert decisions_file(fview, draft)["promote"] == []
    assert rows_for_page([fview.artist(QUEEN)], draft)[0]["follow_done"] is True


def _saved_by_snapshot(view, draft) -> dict[str, bool]:
    return {r["rg"]: r["save"] for a in review_data(view, draft)["artists"] for r in a["releases"]}


def test_one_album_can_be_kept_and_saved_on_its_own(fview, tmp_path: Path) -> None:
    from likearr.shell.promote_save import read_decisions, read_reviewed

    # Trash all of Queen, but keep and save the compilation - and save the protected single too.
    draft = dec(Draft(), fview, QUEEN, "trash", **{FRG[0]: "save", FRG[5]: "save"})

    out = decisions_file(fview, draft)
    snapshot = review_data(fview, draft)

    assert out["trash"] == [FRG[1]]  # a save shields its album from the artist's trash
    assert out["save_releases"] == [FRG[0], FRG[5]]
    assert out["save"] == [] and out["save_exclude_releases"] == []
    (tmp_path / "decisions.json").write_text(json.dumps(out))
    (tmp_path / "review-data.json").write_text(json.dumps(snapshot))
    assert read_decisions(tmp_path / "decisions.json").save_releases == (FRG[0], FRG[5])
    reviewed = read_reviewed(tmp_path / "review-data.json")
    assert reviewed.allows(QUEEN, FRG[0]) and reviewed.allows(QUEEN, FRG[5])
    assert not reviewed.allows(QUEEN, FRG[1])
    assert (reviewed.save_flag(FRG[0]), reviewed.save_flag(FRG[5])) == (True, True)
    saved = _saved_by_snapshot(fview, draft)
    assert {rg: saved[rg] for rg in (FRG[0], FRG[1], FRG[5])} == {FRG[0]: True, FRG[1]: False, FRG[5]: True}


def test_an_album_kept_with_no_change_is_left_out_of_its_artists_save(view) -> None:
    draft = dec(Draft(), view, BIG, "save", **{RG[1]: "keep"})

    out = decisions_file(view, draft)

    assert out["save"] == [BIG]
    assert out["save_exclude_releases"] == [RG[1]]
    assert _saved_by_snapshot(view, draft)[RG[0]] is True  # "Same as artist": saved with it
    assert _saved_by_snapshot(view, draft)[RG[1]] is False
    assert summary(view, draft)["save_albums"] == 1


def test_following_an_artist_and_saving_one_album_are_both_exported(view) -> None:
    draft = dec(Draft(), view, BIG, "promote", **{RG[0]: "save"})

    out = decisions_file(view, draft)

    assert out["promote"] == [BIG] and out["save"] == []
    assert out["save_releases"] == [RG[0]]
    assert _saved_by_snapshot(view, draft) == {RG[0]: True, RG[1]: False, RG[2]: False, RG[3]: False, RG[4]: False}


def test_same_as_artist_says_what_it_resolves_to(view) -> None:
    def label(draft: Draft, artist: str, rg: str) -> str:
        return rows_for_page([view.artist(artist)], draft)[0]["same_as_artist"][rg]

    assert label(Draft(), BIG, RG[0]) == "Same as artist: not decided yet"
    assert label(dec(Draft(), view, BIG, "keep"), BIG, RG[0]) == "Same as artist: keep"
    assert label(dec(Draft(), view, BIG, "save"), BIG, RG[0]) == "Same as artist: keep and save on Spotify"
    assert label(dec(Draft(), view, BIG, "promote"), BIG, RG[0]) == "Same as artist: keep"
    assert label(dec(Draft(), view, BIG, "trash"), BIG, RG[0]) == "Same as artist: trash"
    assert label(dec(Draft(), view, GUARDED, "trash"), GUARDED, RG[4]) == "Same as artist: keep (always kept)"


# ---------------------------------------------------------------- carried over from earlier reviews

OWN = ledger_source("this-job")


def _ledger(source: str = "review of 2026-01-15", **releases: str) -> Ledger:
    return Ledger(releases={rg: Entry(d, "2026-01-15", source) for rg, d in releases.items()})


def test_what_was_kept_before_is_filled_in_and_what_was_trashed_is_not(view) -> None:
    ledger = _ledger(**{RG[0]: "keep", RG[1]: "save", RG[2]: "trash"})

    draft = prefill(Draft(), view, ledger, own_source=OWN)

    # Big Band: both albums kept before, so the artist is decided too: keep - never a save.
    assert draft.artists == {BIG: "keep"}
    assert draft.releases == {}  # the artist's keep covers them; explicit ones would shield them
    # Small Band's only album was trashed on 2026-01-15 and is back: undecided, with the note.
    assert SMALL not in draft.artists and RG[2] not in draft.releases
    assert draft.past_releases[RG[2]].decision == "trash"
    assert draft.rev(BIG) == 1 and draft.rev(SMALL) == 0
    s = summary(view, draft)
    assert (s["carried"], s["needs"], s["needs_artists"], s["returning"]) == (2, 2, 2, 1)
    assert s["save_albums"] == 0 and s["promote"] == 0
    needs, *_ = select_artists(view, draft, query="", show="needs", page=1)
    assert [a.name for a in needs] == ["Small Band", "Guarded"]


def test_an_earlier_follow_or_save_never_becomes_a_new_spotify_write(view, fview) -> None:
    # (a) an artist saved before, with an album nobody reviewed then: kept, nothing to save.
    ledger = _ledger(**{RG[0]: "save"})
    ledger.artists[BIG] = Entry("save", "2026-01-15", "review of 2026-01-15")
    draft = prefill(Draft(), view, ledger, own_source=OWN)
    out = decisions_file(view, draft)
    assert out["save"] == [] and out["save_releases"] == [] and BIG not in draft.artists
    assert draft.carried == {RG[0]} and draft.past_artists[BIG].decision == "save"
    # (b) an artist followed before (and maybe unfollowed on Spotify since): kept, nothing to follow.
    ledger = _ledger(**{FRG[4]: "keep"})
    ledger.artists[NOBODY] = Entry("promote", "2026-01-15", "review of 2026-01-15")
    draft = prefill(Draft(), fview, ledger, own_source=OWN)
    assert draft.artists[NOBODY] == "keep"
    assert decisions_file(fview, draft)["promote"] == []


def test_an_artist_with_a_new_album_stays_undecided(view) -> None:
    draft = prefill(Draft(), view, _ledger(**{RG[0]: "keep"}), own_source=OWN)

    assert BIG not in draft.artists
    assert draft.releases == {} and draft.carried == {RG[0]}  # decided before, and says so
    row = rows_for_page([view.artist(BIG)], draft)[0]
    assert row["overrides"][RG[0]] == ""  # "Same as artist" is selected, never a Keep nobody chose
    assert row["same_as_artist"][RG[0]] == "Same as artist: not decided yet"
    assert row["past_notes"][RG[0]] == "You kept this on 15 Jan 2026."
    assert decisions_file(view, draft)["trash"] == []
    assert [a.name for a in select_artists(view, draft, query="", show="needs", page=1)[0]] == [
        "Big Band",
        "Small Band",
        "Guarded",
    ]


def test_this_reviews_own_artist_decision_is_never_narrowed_by_old_keeps(view) -> None:
    started = Draft(artists={BIG: "trash"}, releases={RG[1]: "trash"}, revs={BIG: 3})

    draft = prefill(started, view, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)

    assert draft.artists[BIG] == "trash"
    assert draft.releases == {RG[1]: "trash"}  # no keep filled in under a Trash all
    assert decisions_file(view, draft)["trash"] == [RG[0], RG[1]]
    assert draft.rev(BIG) == 3, "nothing changed, so no new revision"
    assert draft.past_releases[RG[0]].decision == "keep"  # the note is still there


def test_trash_all_with_earlier_keeps_trashes_the_artists_candidates(view) -> None:
    draft = prefill(Draft(artists={BIG: "trash"}), view, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)

    assert decisions_file(view, draft)["trash"] == [RG[0], RG[1]]


def test_a_follow_made_here_keeps_its_albums_free_for_a_later_trash_all(view) -> None:
    draft = prefill(Draft(artists={BIG: "promote"}), view, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)
    draft = decide(draft, view, BIG, rev=draft.rev(BIG), decision="trash")

    assert decisions_file(view, draft)["trash"] == [RG[0], RG[1]]


def test_an_empty_or_missing_ledger_uses_nothing_up_and_a_later_seed_still_arrives(view) -> None:
    first = prefill(Draft(artists={SMALL: "promote"}), view, Ledger(), own_source=OWN)
    seeded = prefill(first, view, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)

    assert seeded.artists == {SMALL: "promote", BIG: "keep"}
    assert seeded.past_releases[RG[0]].on == "2026-01-15"


def test_an_entry_is_applied_once_so_a_changed_choice_stays_changed(view) -> None:
    ledger = _ledger(**{RG[0]: "keep"})
    draft = prefill(Draft(), view, ledger, own_source=OWN)
    draft = decide(draft, view, BIG, rev=draft.rev(BIG), release=(RG[0], "trash"))

    again = prefill(draft, view, ledger, own_source=OWN)

    assert again is draft
    assert again.releases[RG[0]] == "trash"


def test_this_reviews_own_exports_are_never_read_back_as_earlier_decisions(view) -> None:
    ledger = _ledger(OWN, **{RG[0]: "keep", RG[1]: "keep", RG[2]: "keep"})

    draft = prefill(Draft(), view, ledger, own_source=OWN)

    assert draft is not None and draft.past_releases == {} and draft.artists == {}
    assert summary(view, draft)["carried"] == 0


def test_an_export_tells_the_ledger_what_was_decided_and_nothing_else(view) -> None:
    draft = dec(Draft(), view, BIG, "save", **{RG[1]: "trash"})
    draft = decide(draft, view, GUARDED, rev=draft.rev(GUARDED), release=(RG[4], "save"))

    releases, artists = ledger_changes(view, draft)

    assert releases == {RG[0]: "save", RG[1]: "trash", RG[4]: "save"}  # Small Band and Loose: undecided
    assert artists == {BIG: "save"}
    releases, artists = ledger_changes(view, dec(Draft(), view, GUARDED, "keep"))
    assert releases == {RG[3]: "keep"}  # the protected album is always kept by rule, not by decision
    assert artists == {GUARDED: ""}
    releases, _ = ledger_changes(view, dec(Draft(), view, BIG, "save", **{RG[0]: "keep"}))
    assert releases == {RG[0]: "keep", RG[1]: "save"}  # kept with no change: not a save


def test_the_carried_over_draft_survives_a_round_trip(view, tmp_path: Path) -> None:
    path = tmp_path / "draft.json"
    draft = prefill(Draft(), view, _ledger(**{RG[0]: "keep", RG[2]: "trash"}), own_source=OWN)
    write_draft(path, draft)

    assert read_draft(path) == draft
    raw = json.loads(path.read_text())
    raw["past_releases"][RG[3]] = {"decision": "nuke", "on": "2026-01-15"}
    path.write_text(json.dumps(raw))
    assert RG[3] not in read_draft(path).past_releases


def test_a_save_all_clicked_later_covers_the_albums_kept_before(view) -> None:
    """Round-three repro: carried keeps are not hand keeps, so they never shrink a save-all.
    Big Band has two albums kept before and one new one."""
    big = PruneArtist(
        mbid=BIG,
        name="Big Band",
        releases=(
            *view.artist(BIG).releases,
            PruneRelease(rg_mbid=RG[5], title="Third", kind="Album", released="2010", files=1, size=10),
        ),
    )
    three = PruneView(created_at=view.created_at, artists=(big,))
    draft = prefill(Draft(), three, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)
    assert BIG not in draft.artists  # Third is new

    draft = decide(draft, three, BIG, rev=draft.rev(BIG), decision="save")

    out = decisions_file(three, draft)
    assert out["save"] == [BIG] and out["save_exclude_releases"] == []
    assert _saved_by_snapshot(three, draft) == {RG[0]: True, RG[1]: True, RG[5]: True}
    row = rows_for_page([big], draft)[0]
    assert row["overrides"][RG[0]] == "" and row["same_as_artist"][RG[0]] == "Same as artist: keep and save on Spotify"
    # A hand Keep on one of them is a different thing: that one stays out of the save.
    draft = decide(draft, three, BIG, rev=draft.rev(BIG), release=(RG[0], "keep"))
    assert decisions_file(three, draft)["save_exclude_releases"] == [RG[0]]


def test_the_ledger_all_at_once_or_in_two_reads_gives_the_same_review(view) -> None:
    both = _ledger(**{RG[0]: "keep", RG[1]: "keep", RG[2]: "keep"})
    at_once = prefill(Draft(), view, both, own_source=OWN)
    first = prefill(Draft(), view, _ledger(**{RG[0]: "keep"}), own_source=OWN)
    in_two = prefill(first, view, both, own_source=OWN)

    assert (in_two.artists, in_two.releases, in_two.carried) == (at_once.artists, at_once.releases, at_once.carried)
    for draft in (at_once, in_two):
        trashed = decide(draft, view, BIG, rev=draft.rev(BIG), decision="trash")
        assert decisions_file(view, trashed)["trash"] == [RG[0], RG[1]]


def test_an_artist_set_back_to_undecided_by_hand_is_not_decided_again(view) -> None:
    draft = prefill(Draft(), view, _ledger(**{RG[0]: "keep", RG[1]: "keep"}), own_source=OWN)
    assert draft.artists == {BIG: "keep"}
    draft = decide(draft, view, BIG, rev=draft.rev(BIG), decision="undecided")

    # An export elsewhere later records one of their albums again.
    later = Ledger(
        releases={
            RG[0]: Entry("keep", "2026-01-15", "review of 2026-01-15"),
            RG[1]: Entry("save", "2026-10-02", "Clean up another-job"),
        }
    )
    again = prefill(draft, view, later, own_source=OWN)

    assert BIG not in again.artists
    assert BIG in read_draft_round_trip(again).reset_artists


def read_draft_round_trip(draft: Draft) -> Draft:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "draft.json"
        write_draft(path, draft)
        return read_draft(path)


def test_a_carried_album_can_be_made_a_hand_keep_that_survives_trash_all(view) -> None:
    draft = prefill(Draft(), view, _ledger(**{RG[0]: "keep"}), own_source=OWN)

    trashed_all = decide(draft, view, BIG, rev=draft.rev(BIG), decision="trash")
    assert decisions_file(view, trashed_all)["trash"] == [RG[0], RG[1]]  # carried: goes as the artist

    kept = decide(draft, view, BIG, rev=draft.rev(BIG), release=(RG[0], "keep"))
    assert RG[0] not in kept.carried and kept.releases == {RG[0]: "keep"}
    kept = decide(kept, view, BIG, rev=kept.rev(BIG), decision="trash")
    assert decisions_file(view, kept)["trash"] == [RG[1]]  # the hand Keep protects it


# ---------------------------------------------------------------- the net effect of an artist decision

TRASH_QUEEN = (
    "Trashes the 2 albums listed here (1 compilation and 1 live album). You follow Queen on Spotify, "
    "so their studio albums and EPs aren't listed and stay."
)
ONE_PROTECTED = "1 album holds the only copy of a song you liked and is always kept."
KEEP_FOLLOWED = (
    "Keeps the listed albums on disk. Following brings in studio albums and EPs only, so likearr "
    "won't fetch more like these."
)


def test_nothing_is_said_until_an_artist_decision_is_picked(fview) -> None:
    assert net_effect(fview.artist(QUEEN), Draft()) == []


def test_trashing_a_followed_artist_says_what_goes_and_what_stays(fview) -> None:
    queen = fview.artist(QUEEN)

    assert net_effect(queen, dec(Draft(), fview, QUEEN, "trash")) == [TRASH_QUEEN, ONE_PROTECTED]
    one_saved = dec(Draft(), fview, QUEEN, "trash", **{FRG[0]: "save"})
    assert net_effect(queen, one_saved) == [
        "Trashes the 1 album listed here (1 live album). You follow Queen on Spotify, so their studio "
        "albums and EPs aren't listed and stay.",
        ONE_PROTECTED,
        "1 album you set individually keeps its own choice.",
    ]


def test_keeping_a_followed_artist_says_likearr_fetches_no_more_like_these(fview) -> None:
    lawrence = fview.artist(LAWRENCE)

    assert net_effect(lawrence, dec(Draft(), fview, LAWRENCE, "keep")) == [KEEP_FOLLOWED]
    assert net_effect(lawrence, Draft(artists={LAWRENCE: "promote"})) == [KEEP_FOLLOWED]  # followed now
    assert net_effect(lawrence, dec(Draft(), fview, LAWRENCE, "save")) == []


def test_an_artist_nobody_follows_gets_no_follow_lines(fview) -> None:
    nobody = fview.artist(NOBODY)

    assert net_effect(nobody, dec(Draft(), fview, NOBODY, "trash")) == []
    assert net_effect(nobody, dec(Draft(), fview, NOBODY, "keep")) == []


def test_when_follows_are_unknown_only_the_other_lines_show(view) -> None:
    assert net_effect(view.artist(BIG), dec(Draft(), view, BIG, "trash")) == []  # followed: None
    assert net_effect(view.artist(GUARDED), dec(Draft(), view, GUARDED, "keep")) == [ONE_PROTECTED]


def test_save_all_counts_the_albums_set_to_keep(view) -> None:
    big = view.artist(BIG)

    one = dec(Draft(), view, BIG, "save", **{RG[1]: "keep"})
    assert net_effect(big, one) == [
        "1 album you set individually keeps its own choice.",
        "1 album you set to Keep isn't saved.",
    ]
    two = dec(Draft(), view, BIG, "save", **{RG[0]: "keep", RG[1]: "keep"})
    assert net_effect(big, two) == [
        "2 albums you set individually keep their own choice.",
        "2 albums you set to Keep aren't saved.",
    ]
    assert net_effect(big, dec(Draft(), view, BIG, "keep", **{RG[1]: "keep"})) == [
        "1 album you set individually keeps its own choice."
    ]


# ---------------------------------------------------------------- why an album is always kept, in words

ARETHA = "aaaaaaaa-0000-4000-8000-000000000064"
RESPECT = "bbbbbbbb-0000-4000-8000-000000000064"
PLAYLIST = "fakeplaylist0000000064"
TRACK = "faketrack0000000000064"
LIVE_LINE = (
    f"holds a liked track (playlist:{PLAYLIST}:{TRACK}) whose album 'Respect' ({RESPECT}) has no files yet; "
    "this is the only copy on disk"
)
"""`protected_reason` in the plain-string shape a report writes it."""


def _kept(tmp_path: Path, *protected: dict[str, object]) -> list[PruneRelease]:
    report = {"created_at": "", "summary": {}, "candidates": [], "protected": list(protected)}
    path = tmp_path / "kept.json"
    path.write_text(json.dumps(report))
    view = read_report(path)
    assert view is not None
    return sorted((r for a in view.artists for r in a.releases), key=lambda r: r.rg_mbid)


def _protected(rg: str, title: str, reason: str, **extra: object) -> dict[str, object]:
    row = _row(ARETHA, "Aretha Franklin", rg, title, 10, reason)
    row.update(extra)
    return row


def _words(release: PruneRelease, names: dict[str, str] | None = None, songs: dict[str, str] | None = None) -> str:
    return kept_why(release, playlist_names=names, songs=songs).text


def test_the_aretha_franklin_line_from_a_report_reads_without_ids(tmp_path: Path) -> None:
    [release] = _kept(tmp_path, _protected(RG[0], "Aretha Now", LIVE_LINE))
    key = f"playlist:{PLAYLIST}:{TRACK}"

    assert release.protection == Protection("album_not_downloaded", key, album="Respect", album_mbid=RESPECT)
    why = kept_why(release, playlist_names={PLAYLIST: "Road trip"}, songs={key: "Think"})
    assert why.text == (
        'Only copy of "Think", a song in your playlist "Road trip". Its album, Respect, isn\'t downloaded yet.'
    )
    assert (why.lead, why.album, why.album_url, why.tail) == (
        'Only copy of "Think", a song in your playlist "Road trip". Its album, ',
        "Respect",
        f"https://musicbrainz.org/release-group/{RESPECT}",
        ", isn't downloaded yet.",
    )
    # Nothing known beyond the report itself: still no id.
    bare = _words(release)
    assert bare == "Only copy of a song in one of your playlists. Its album, Respect, isn't downloaded yet."
    assert PLAYLIST not in bare and TRACK not in bare and RESPECT not in bare


def test_the_structured_field_words_each_kind_and_source(tmp_path: Path) -> None:
    think = {"song": "Think", "album": "Respect", "album_mbid": RESPECT}
    liked, listed, waiting, untitled = _kept(
        tmp_path,
        _protected(
            RG[0],
            "A",
            "not read when there is a protection object",
            protection={"kind": "album_not_downloaded", "intent_key": "liked:t1", **think},
        ),
        _protected(
            RG[1],
            "B",
            "x",
            protection={"kind": "album_not_downloaded", "intent_key": f"playlist:{PLAYLIST}:t2", **think},
        ),
        _protected(RG[2], "C", "x", protection={"kind": "pending_album", "intent_key": "liked:t3", "song": "X"}),
        _protected(RG[3], "D", "x", protection={"kind": "album_not_downloaded", "intent_key": "liked:t4"}),
    )

    assert _words(liked) == 'Only copy of "Think", a song you liked. Its album, Respect, isn\'t downloaded yet.'
    assert _words(listed, {PLAYLIST: "Road trip"}) == (
        'Only copy of "Think", a song in your playlist "Road trip". Its album, Respect, isn\'t downloaded yet.'
    )
    assert _words(listed, {"another": "Gym"}) == (
        'Only copy of "Think", a song in one of your playlists. Its album, Respect, isn\'t downloaded yet.'
    )
    assert _words(waiting) == 'Only copy of "X", a song you liked. likearr is waiting for its album to be released.'
    assert _words(untitled) == "Only copy of a song you liked. Its album isn't downloaded yet."
    # The report's title wins over the last run's; the last run's fills a gap.
    assert _words(liked, songs={"liked:t1": "Other"}).startswith('Only copy of "Think",')
    assert _words(untitled, songs={"liked:t4": "Rock Steady"}).startswith('Only copy of "Rock Steady",')


def test_an_old_line_is_read_back_for_both_kinds_and_both_sources(tmp_path: Path) -> None:
    only = "this is the only copy on disk"
    waiting, liked, quoted, unreadable = _kept(
        tmp_path,
        _protected(RG[0], "A", f"holds a liked track (liked:t1) that is still waiting for an album; {only}"),
        _protected(
            RG[1],
            "B",
            f"holds a liked track (liked:t2) whose album 'Respect (Live)' ({RESPECT}) has no files yet; {only}",
        ),
        _protected(
            RG[2],
            "C",
            f"holds a liked track (playlist:{PLAYLIST}:t3) whose album {"Don't"!r} ({RESPECT}) has no files yet",
        ),
        _protected(RG[3], "D", "the only local copy of a liked song"),
    )

    assert waiting.protection == Protection("pending_album", "liked:t1")
    assert _words(waiting) == "Only copy of a song you liked. likearr is waiting for its album to be released."
    assert liked.protection is not None and liked.protection.album == "Respect (Live)"
    assert quoted.protection is not None and quoted.protection.album == "Don't"
    assert quoted.protection.source == "playlist"
    assert unreadable.protection is None
    assert _words(unreadable) == "Only copy of a song from your liked songs or playlists."


def test_an_old_lines_kind_is_read_where_it_was_written_not_from_the_album_title(tmp_path: Path) -> None:
    only = "this is the only copy on disk"
    title = "Songs That Is Still Waiting for an Album; that is still waiting for an album; B-Sides"
    odd, not_a_line = _kept(
        tmp_path,
        _protected(
            RG[0], "A", f"holds a liked track (liked:t1) whose album {title!r} ({RESPECT}) has no files yet; {only}"
        ),
        _protected(RG[1], "B", f"see: holds a liked track (liked:t2) that is still waiting for an album; {only}"),
    )

    assert odd.protection == Protection("album_not_downloaded", "liked:t1", album=title, album_mbid=RESPECT)
    assert not_a_line.protection is None  # the line always began with its key


def test_an_old_line_with_no_album_still_says_the_rest(tmp_path: Path) -> None:
    [release] = _kept(
        tmp_path,
        _protected(RG[0], "A", "holds a liked track (liked:t1) whose album its resolved album has no files yet; ..."),
    )
    assert _words(release) == "Only copy of a song you liked. Its album isn't downloaded yet."


def test_a_malformed_protection_object_falls_back_or_is_read_safely(tmp_path: Path) -> None:
    bad_kind, odd_fields = _kept(
        tmp_path,
        _protected(
            RG[0],
            "A",
            "holds a liked track (liked:t1) that is still waiting for an album; ...",
            protection={"kind": "deleted", "intent_key": "liked:t1"},
        ),
        _protected(
            RG[1],
            "B",
            "x",
            protection={
                "kind": "album_not_downloaded",
                "intent_key": "liked:t2",
                "album": "Respect",
                "album_mbid": "javascript:alert(1)",
                "song": ["not", "a", "string"],
                "song_artists": "Aretha",
            },
        ),
    )
    assert bad_kind.protection == Protection("pending_album", "liked:t1")  # read from the line instead
    why = kept_why(odd_fields)
    assert why.text == "Only copy of a song you liked. Its album, Respect, isn't downloaded yet."
    assert why.album_url == ""  # a link only ever to a real MusicBrainz id


def test_the_net_effect_says_whose_song_it_is(tmp_path: Path) -> None:
    def artist(*keys: str) -> PruneArtist:
        rows = [
            _protected(RG[i], f"T{i}", "x", protection={"kind": "pending_album", "intent_key": key})
            for i, key in enumerate(keys)
        ]
        return PruneArtist(mbid=ARETHA, name="Aretha Franklin", releases=tuple(_kept(tmp_path, *rows)))

    keep = Draft(artists={ARETHA: "keep"})
    listed = f"playlist:{PLAYLIST}:t1"
    assert net_effect(artist("liked:t0"), keep) == [ONE_PROTECTED]
    assert net_effect(artist(listed), keep) == [
        "1 album holds the only copy of a song in your playlists and is always kept."
    ]
    assert net_effect(artist("liked:t0", listed), keep) == [
        "2 albums hold the only copy of a song from your liked songs or playlists and are always kept."
    ]


def test_the_rows_carry_each_protected_albums_words(fview) -> None:
    [row] = rows_for_page([fview.artist(QUEEN)], Draft(), playlist_names={}, songs={})
    assert list(row["kept"]) == [FRG[5]]
    assert row["kept"][FRG[5]].text == (
        'Only copy of "Innuendo", a song you liked. likearr is waiting for its album to be released.'
    )
