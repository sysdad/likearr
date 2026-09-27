"""`shell.promote_save`: the plan, the digest guard, idempotency, scopes and the search budget.

The point of this command is that it never writes something it is not sure about, so most of
these tests assert on what it did *not* do: `library.writes()` is empty, the near miss is in
`unmatched`, the stale plan changed nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from likearr.models import (
    EXIT_OK,
    EXIT_STALE,
    PrimaryType,
    SpotifyAlbumRef,
    SpotifyArtistRef,
)
from likearr.ports import ScopeError
from likearr.shell import promote_save as ps
from tests.shell.conftest import (
    NOW,
    CapturingSink,
    FakeLibrary,
    FakeLidarr,
    FakeLinks,
    make_context,
)
from tests.unit.fakes import lidarr_album, lidarr_artist, rg

PROMOTE_MBID = "artist-promote"
SAVE_MBID = "artist-save"

KEPT = rg("rg-kept", "In Rainbows", artist_mbid=SAVE_MBID, artist_name="Radiohead")
ALSO_KEPT = rg("rg-also", "Kid A", artist_mbid=SAVE_MBID, artist_name="Radiohead", primary=PrimaryType.ALBUM)
PRUNED = rg("rg-pruned", "Pablo Honey", artist_mbid=SAVE_MBID, artist_name="Radiohead")


def sp_album(spotify_id: str, name: str, artist: str = "Radiohead") -> SpotifyAlbumRef:
    return SpotifyAlbumRef(
        spotify_id=spotify_id,
        name=name,
        artist_names=(artist,),
        upc=None,
        album_type="album",
        release_date=None,
    )


def write_reviewed(
    tmp_path: Path, artists: dict[str, list[tuple[str, int]]] | None = None, *, saves: dict[str, bool] | None = None
) -> Path:
    """A review snapshot in the review page's own shape: artists[] -> releases[] -> rg/files, and
    the per-album ``save`` flag a snapshot written since #55 carries (`saves`; absent otherwise)."""
    if artists is None:
        artists = {SAVE_MBID: [("rg-kept", 10), ("rg-also", 11), ("rg-pruned", 0)]}
    saves = saves or {}
    path = tmp_path / "review-data.json"
    path.write_text(
        json.dumps(
            {
                "artists": [
                    {
                        "mbid": mbid,
                        "releases": [
                            {"rg": rg, "files": files, "title": rg, "type": "Album"}
                            | ({"save": saves[rg]} if rg in saves else {})
                            for rg, files in releases
                        ],
                    }
                    for mbid, releases in artists.items()
                ]
            }
        )
    )
    return path


def write_decisions(tmp_path: Path, **fields: object) -> Path:
    path = tmp_path / "decisions.json"
    payload: dict[str, object] = {"version": 1, "trash": [], "trash_artists": [], "promote": [], "save": []}
    payload.update(fields)
    path.write_text(json.dumps(payload))
    return path


def a_library() -> tuple[FakeLidarr, FakeLibrary, FakeLinks]:
    """One promote artist, one save artist with two kept albums and one pruned (no files)."""
    lidarr = FakeLidarr()
    lidarr.seed(lidarr_artist(PROMOTE_MBID, id=1, name="Boards of Canada"))
    lidarr.seed(
        lidarr_artist(SAVE_MBID, id=2, name="Radiohead"),
        lidarr_album(KEPT, id=100, artist_id=2, files=10),
        lidarr_album(ALSO_KEPT, id=101, artist_id=2, files=11),
        lidarr_album(PRUNED, id=102, artist_id=2, files=0),
    )
    library = FakeLibrary(
        artist_hits={"Boards of Canada": [SpotifyArtistRef("sp-boc", "Boards of Canada")]},
        upc_hits={"0634904032524": [sp_album("sp-rainbows", "In Rainbows")]},
        album_hits={("Radiohead", "Kid A"): [sp_album("sp-kida", "Kid A")]},
    )
    links = FakeLinks(barcodes={"rg-kept": ["0634904032524"]})
    return lidarr, library, links


def plan_once(
    tmp_path: Path,
    decisions: Path,
    lidarr: FakeLidarr,
    library: FakeLibrary,
    links: FakeLinks | None = None,
    reviewed: Path | None = None,
) -> tuple[int, ps.PromoteSavePlan]:
    out = tmp_path / "plan.json"
    reviewed = reviewed if reviewed is not None else write_reviewed(tmp_path)
    with make_context(tmp_path, lidarr=lidarr, sink=CapturingSink(), library=library) as ctx:
        code = ps.promote_save_command(
            ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, library=library, links=links
        )
    return code, ps.read_plan(out)


# --------------------------------------------------------------------------- the three tiers

SP_ARTIST = "4Z8W4fKeB5YxbusRsdQVPb"
SP_ALBUM = "6dVIqQ8qmQ5GBnJ9shOYGE"


def test_a_musicbrainz_relationship_maps_an_artist_without_searching(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    links.artist_links[PROMOTE_MBID] = [f"https://open.spotify.com/artist/{SP_ARTIST}"]
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert [(f.spotify_id, f.step) for f in plan.follow] == [(SP_ARTIST, "artist:mb-rel")]
    assert library.searches == 0, "tier 1 costs no Spotify quota at all"


def test_a_musicbrainz_relationship_maps_an_album_without_searching(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    links.album_links["rg-kept"] = [f"https://open.spotify.com/album/{SP_ALBUM}"]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    kept = next(s for s in plan.save if s.key.rg_mbid == "rg-kept")
    assert (kept.spotify_id, kept.step) == (SP_ALBUM, "album:mb-rel")
    assert ("search_albums_by_upc", "0634904032524") not in library.calls, "no UPC search was needed"


def test_the_relationship_beats_the_upc_which_beats_the_title(tmp_path: Path) -> None:
    """All three tiers can answer this album; the most authoritative one must win."""
    lidarr, library, links = a_library()
    links.album_links["rg-kept"] = [f"https://open.spotify.com/album/{SP_ALBUM}"]
    library.album_hits[("Radiohead", "In Rainbows")] = [sp_album("sp-by-title", "In Rainbows")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, first = plan_once(tmp_path, decisions, lidarr, library, links)
    assert next(s for s in first.save if s.key.rg_mbid == "rg-kept").spotify_id == SP_ALBUM

    # Drop the relationship: the UPC tier takes over.
    links.album_links.clear()
    _, second = plan_once(tmp_path, decisions, lidarr, library, links)
    rainbows = next(s for s in second.save if s.key.rg_mbid == "rg-kept")
    assert (rainbows.spotify_id, rainbows.step) == ("sp-rainbows", "album:upc")

    # Drop the barcode too: only the title search is left.
    links.barcodes.clear()
    _, third = plan_once(tmp_path, decisions, lidarr, library, links)
    rainbows = next(s for s in third.save if s.key.rg_mbid == "rg-kept")
    assert (rainbows.spotify_id, rainbows.step) == ("sp-by-title", "album:name")


# --------------------------------------------------------------------------- the review snapshot
#
# The rule: promote-save carries out decisions a human made in a review. likearr never
# saves an album or follows an artist on its own, on any schedule, as a side effect of any rule.
# So the save set is what was REVIEWED, never what the library happens to hold today.


def test_planning_a_save_without_the_review_snapshot_is_refused(tmp_path: Path) -> None:
    """There is no fallback to library state. That fallback *was* the bug."""
    lidarr, library, _ = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx, pytest.raises(ps.PromoteSaveError) as caught:
        ps.promote_save_command(ctx, decisions=decisions, out=out, now=NOW)

    message = str(caught.value)
    assert "--reviewed" in message
    assert "reviewed and kept" in message
    assert library.writes() == []
    assert not out.exists(), "the refusal happens before anything is planned or written"


def test_promote_only_decisions_need_no_review_snapshot(tmp_path: Path) -> None:
    """Follows come from `promote` alone, so nothing needs constraining."""
    lidarr, library, _ = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID])
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        code = ps.promote_save_command(ctx, decisions=decisions, out=out, now=NOW)

    assert code == EXIT_OK
    assert [f.spotify_id for f in ps.read_plan(out).follow] == ["sp-boc"]


def test_an_album_that_arrived_after_the_review_is_never_saveable(tmp_path: Path) -> None:
    """Albums a library gains after a review: a followed artist's catalogue, liked tracks."""
    lidarr, library, links = a_library()
    catalogue_fill = rg("rg-catalogue", "Amnesiac", artist_mbid=SAVE_MBID, artist_name="Radiohead")
    lidarr.albums[SAVE_MBID]["rg-catalogue"] = lidarr_album(catalogue_fill, id=103, artist_id=2, files=9)
    library.album_hits[("Radiohead", "Amnesiac")] = [sp_album("sp-amnesiac", "Amnesiac")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert "sp-amnesiac" not in [s.spotify_id for s in plan.save]
    assert ("search_albums", ("Radiohead", "Amnesiac")) not in library.calls, "not even looked up"
    assert [u.rg_mbid for u in plan.excluded_unreviewed] == ["rg-catalogue"]
    assert "never saves an album nobody decided on" in plan.excluded_unreviewed[0].reason


def test_the_exclusion_count_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lidarr, library, links = a_library()
    for index, title in enumerate(("Amnesiac", "Hail to the Thief", "The Bends")):
        extra = rg(f"rg-extra-{index}", title, artist_mbid=SAVE_MBID, artist_name="Radiohead")
        lidarr.albums[SAVE_MBID][extra.mbid] = lidarr_album(extra, id=200 + index, artist_id=2, files=8)
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert len(plan.excluded_unreviewed) == 3
    assert "3 excluded as unreviewed" in capsys.readouterr().out


def test_an_album_reviewed_without_files_is_not_saveable(tmp_path: Path) -> None:
    """`rg-pruned` was in front of the reviewer with zero files; that was not a keep."""
    lidarr, library, links = a_library()
    lidarr.albums[SAVE_MBID]["rg-pruned"] = lidarr_album(PRUNED, id=102, artist_id=2, files=4)
    library.album_hits[("Radiohead", "Pablo Honey")] = [sp_album("sp-pablo", "Pablo Honey")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert "sp-pablo" not in [s.spotify_id for s in plan.save]
    assert "rg-pruned" in [u.rg_mbid for u in plan.excluded_unreviewed]


def test_apply_refuses_a_plan_built_from_a_different_review_snapshot(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        write_reviewed(tmp_path, {SAVE_MBID: [("rg-kept", 10)]})  # someone re-exported the review
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert code == EXIT_STALE
    assert library.writes() == []


def test_a_review_snapshot_that_is_not_one_is_refused(tmp_path: Path) -> None:
    lidarr, library, _ = a_library()
    bad = tmp_path / "review-data.json"
    bad.write_text(json.dumps({"nope": []}))
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    with (
        make_context(tmp_path, lidarr=lidarr, library=library) as ctx,
        pytest.raises(ps.PromoteSaveError, match="not a review snapshot"),
    ):
        ps.promote_save_command(ctx, decisions=decisions, reviewed=bad, now=NOW)


def test_a_version_1_plan_is_refused_rather_than_applied(tmp_path: Path) -> None:
    """Version 1 plans were built from current library state, before the review snapshot existed."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"version": 1, "created_at": NOW.isoformat()}))
    with pytest.raises(ps.PromoteSaveError, match="version 1"):
        ps.read_plan(path)


def test_a_malformed_relationship_url_falls_through_to_searching(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    links.artist_links[PROMOTE_MBID] = ["https://open.spotify.com/artist/not-a-real-id"]
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert [(f.spotify_id, f.step) for f in plan.follow] == [("sp-boc", "artist:name")]


def test_a_relationship_to_the_wrong_entity_type_falls_through(tmp_path: Path) -> None:
    """A track link is not a weaker album answer - it is a different question."""
    lidarr, library, links = a_library()
    links.album_links["rg-kept"] = [f"https://open.spotify.com/track/{SP_ALBUM}"]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    rainbows = next(s for s in plan.save if s.key.rg_mbid == "rg-kept")
    assert (rainbows.spotify_id, rainbows.step) == ("sp-rainbows", "album:upc")


def test_several_different_relationship_targets_fall_through(tmp_path: Path) -> None:
    """Regional catalogue duplicates: probably the same record, but 'probably' is not the bar."""
    lidarr, library, links = a_library()
    links.album_links["rg-kept"] = [
        f"https://open.spotify.com/album/{SP_ALBUM}",
        "https://open.spotify.com/album/7dxKtc08dYeRVHt3p9CZJn",
    ]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert next(s for s in plan.save if s.key.rg_mbid == "rg-kept").step == "album:upc"


def test_the_plan_reports_which_tier_matched_each_item(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    links.artist_links[PROMOTE_MBID] = [f"https://open.spotify.com/artist/{SP_ARTIST}"]
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert ps.tier_breakdown(plan) == {"album:name": 1, "album:upc": 1, "artist:mb-rel": 1}


def test_the_dry_run_prints_the_tier_breakdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lidarr, library, links = a_library()
    links.artist_links[PROMOTE_MBID] = [f"https://open.spotify.com/artist/{SP_ARTIST}"]
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    plan_once(tmp_path, decisions, lidarr, library, links)

    out = capsys.readouterr().out
    assert "matched by (best tier first" in out
    assert out.index("artist:mb-rel") < out.index("album:name"), "the weakest tier reads last"


# --------------------------------------------------------------------------- planning


def test_a_plan_follows_promote_and_saves_the_kept_albums(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    code, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert code == EXIT_OK
    assert [f.spotify_id for f in plan.follow] == ["sp-boc"]
    assert sorted(s.spotify_id for s in plan.save) == ["sp-kida", "sp-rainbows"]
    assert library.writes() == [], "planning never writes to Spotify"


def test_a_plan_only_saves_albums_that_still_have_files(tmp_path: Path) -> None:
    """The prune has already run, so an album with no files is one the user threw away."""
    lidarr, library, links = a_library()
    library.album_hits[("Radiohead", "Pablo Honey")] = [sp_album("sp-pablo", "Pablo Honey")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert "sp-pablo" not in [s.spotify_id for s in plan.save]
    assert ("search_albums", ("Radiohead", "Pablo Honey")) not in library.calls, "not even searched for"


def test_a_upc_match_is_preferred_over_a_title_search(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    rainbows = next(s for s in plan.save if s.spotify_id == "sp-rainbows")
    assert rainbows.step == "album:upc"
    assert ("search_albums", ("Radiohead", "In Rainbows")) not in library.calls, "a UPC hit ends the search"


def test_a_near_miss_is_reported_not_guessed(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    library.album_hits[("Radiohead", "Kid A")] = [sp_album("sp-amnesiac", "Kid A Mnesia")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert "sp-amnesiac" not in [s.spotify_id for s in plan.save]
    unmatched = next(u for u in plan.unmatched if u.rg_mbid == "rg-also")
    assert "no Spotify album titled 'Kid A'" in unmatched.reason
    assert "Kid A Mnesia" in unmatched.reason


def test_an_artist_lidarr_does_not_have_is_unmatched_not_skipped(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=["artist-nobody"])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert plan.follow == []
    assert "Lidarr has no artist with this MBID" in plan.unmatched[0].reason


def test_a_save_artist_whose_albums_all_lost_their_files_is_reported(tmp_path: Path) -> None:
    lidarr = FakeLidarr()
    lidarr.seed(lidarr_artist(SAVE_MBID, id=2, name="Radiohead"), lidarr_album(PRUNED, id=102, artist_id=2, files=0))
    library = FakeLibrary()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library)

    assert plan.save == []
    assert "no reviewed album of theirs still has files" in plan.unmatched[0].reason


def test_a_plan_without_barcodes_falls_back_to_the_title_search(tmp_path: Path) -> None:
    lidarr, library, _ = a_library()
    library.album_hits[("Radiohead", "In Rainbows")] = [sp_album("sp-rainbows", "In Rainbows")]
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, None)

    assert {s.step for s in plan.save} == {"album:name"}


# --------------------------------------------------------------------------- idempotency


def test_what_is_already_followed_or_saved_is_not_in_the_plan(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    library.followed.add("sp-boc")
    library.saved.add("sp-rainbows")
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert plan.follow == []
    assert [f.spotify_id for f in plan.already_followed] == ["sp-boc"]
    assert [s.spotify_id for s in plan.save] == ["sp-kida"]
    assert [s.spotify_id for s in plan.already_saved] == ["sp-rainbows"]


def test_membership_is_decided_against_the_whole_library(tmp_path: Path) -> None:
    """The follow and save lists are read whole; only the plan's own ids may be picked out of them."""
    lidarr, library, links = a_library()
    library.followed |= {f"unrelated-artist-{i}" for i in range(120)}
    library.saved |= {f"unrelated-album-{i}" for i in range(200)}
    library.saved.add("sp-kida")
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert [f.spotify_id for f in plan.follow] == ["sp-boc"], "a crowded follow list is not a match"
    assert [s.spotify_id for s in plan.save] == ["sp-rainbows"]
    assert [s.spotify_id for s in plan.already_saved] == ["sp-kida"]


def test_applying_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        assert (
            ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
            == EXIT_OK
        )
        assert ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW) == EXIT_OK
        first = library.writes()
        assert ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW) == EXIT_OK

    assert sorted(first[0][1]) == ["sp-boc"]
    assert sorted(first[1][1]) == ["sp-kida", "sp-rainbows"]
    assert library.writes()[2:] == [("follow_artists", []), ("save_albums", [])], (
        "the contains checks are re-run on apply, so a resumed run rewrites nothing"
    )


def test_apply_takes_the_run_lock(tmp_path: Path) -> None:
    from likearr.adapters.lock import LockHeld, run_lock

    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        with run_lock(ctx.lock_path), pytest.raises(LockHeld):
            ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert library.writes() == [], "a run that cannot take the lock writes nothing to Spotify"


# --------------------------------------------------------------------------- the digest guard


def test_apply_refuses_a_plan_whose_decisions_changed(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        write_decisions(tmp_path, promote=[PROMOTE_MBID, "artist-new"], save=[SAVE_MBID])
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert code == EXIT_STALE
    assert library.writes() == [], "a stale plan writes nothing at all"


def test_apply_refuses_a_plan_whose_lidarr_albums_changed(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        # Someone pruned another album between the plan and the apply.
        lidarr.albums[SAVE_MBID].pop("rg-also")
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert code == EXIT_STALE
    assert library.writes() == []


def test_force_applies_a_stale_plan_anyway(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        lidarr.albums[SAVE_MBID].pop("rg-also")
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, force=True, now=NOW)

    assert code == EXIT_OK
    assert sorted(library.saved) == ["sp-kida", "sp-rainbows"]


def test_unrelated_lidarr_activity_does_not_invalidate_a_plan(tmp_path: Path) -> None:
    """The digest covers what the plan looked at, and nothing else."""
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        stranger = rg("rg-stranger", "Elsewhere", artist_mbid="artist-stranger", artist_name="Someone Else")
        lidarr.seed(
            lidarr_artist("artist-stranger", id=9, name="Someone Else"),
            lidarr_album(stranger, id=900, files=3),
        )
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert code == EXIT_OK


# --------------------------------------------------------------------------- scopes


def test_a_token_without_the_write_scopes_is_refused_with_instructions(tmp_path: Path) -> None:
    lidarr, library, _ = a_library()
    library.scopes = frozenset({"user-follow-read", "user-library-read", "playlist-read-private"})
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID])

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx, pytest.raises(ScopeError) as caught:
        ps.promote_save_command(ctx, decisions=decisions, out=tmp_path / "plan.json", now=NOW)

    message = str(caught.value)
    assert "user-follow-modify" in message and "user-library-modify" in message
    assert "likearr auth --manual --promote-save" in message
    assert library.writes() == []
    assert not (tmp_path / "plan.json").exists(), "the scope check runs before anything is read or written"


def test_a_token_that_recorded_no_scopes_is_refused_too(tmp_path: Path) -> None:
    """A token written before the scope change reports nothing; that is not permission to write."""
    lidarr, library, _ = a_library()
    library.scopes = frozenset()

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx, pytest.raises(ScopeError) as caught:
        ps.promote_save_command(ctx, decisions=write_decisions(tmp_path, promote=[PROMOTE_MBID]), now=NOW)

    assert "(none recorded)" in str(caught.value)


# --------------------------------------------------------------------------- the search budget


def test_a_spent_search_budget_keeps_what_it_matched_and_reports_the_rest(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    library.budget = 1  # enough for the promote artist, nothing more
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    code, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    assert code == EXIT_OK, "a spent budget still writes a reviewable plan"
    assert [f.spotify_id for f in plan.follow] == ["sp-boc"], "matched work survives"
    assert plan.budget_exhausted
    assert {u.rg_mbid for u in plan.unmatched} == {"rg-kept", "rg-also"}
    assert all("budget" in u.reason for u in plan.unmatched)


# --------------------------------------------------------------------------- files and errors


def test_a_decisions_file_with_neither_field_is_refused(tmp_path: Path) -> None:
    lidarr, library, _ = a_library()
    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx, pytest.raises(ps.PromoteSaveError):
        ps.promote_save_command(ctx, decisions=write_decisions(tmp_path), now=NOW)


def test_a_plan_from_a_future_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"version": 99, "created_at": NOW.isoformat()}))
    with pytest.raises(ps.PromoteSaveError, match="version 99"):
        ps.read_plan(path)


def test_apply_without_a_decisions_path_anywhere_is_refused(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID])
    reviewed = write_reviewed(tmp_path)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        raw = json.loads(out.read_text())
        raw["decisions_path"] = ""
        out.write_text(json.dumps(raw))
        with pytest.raises(ps.PromoteSaveError, match="--decisions"):
            ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)


def test_the_plan_file_round_trips(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])
    _, plan = plan_once(tmp_path, decisions, lidarr, library, links)

    again = ps.plan_from_dict(ps.plan_to_dict(plan))

    assert again.follow == plan.follow
    assert again.save == plan.save
    assert again.unmatched == plan.unmatched
    assert (again.decisions_digest, again.lidarr_digest) == (plan.decisions_digest, plan.lidarr_digest)


def test_the_dry_run_prints_every_unmatched_item(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lidarr, library, links = a_library()
    library.album_hits[("Radiohead", "Kid A")] = []
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save=[SAVE_MBID])

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(
            ctx,
            decisions=decisions,
            reviewed=write_reviewed(tmp_path),
            out=tmp_path / "plan.json",
            now=NOW,
            library=library,
            links=links,
        )

    out = capsys.readouterr().out
    assert "1 artists to follow" in out
    assert "1 albums to save" in out
    assert "Radiohead - Kid A:" in out


# --------------------------------------------------------------------------- save_releases (#55)
# One album saved on its own: the same rules as an artist's `save`, one album at a time - and the
# snapshot's own per-album `save` flag must agree, or nothing is saved.

SAVED_ALSO = {"rg-kept": False, "rg-also": True}


def test_one_album_saved_on_its_own_is_planned_and_nothing_else_of_the_artist(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save_releases=["rg-also"])
    reviewed = write_reviewed(tmp_path, saves=SAVED_ALSO)

    code, plan = plan_once(tmp_path, decisions, lidarr, library, links, reviewed=reviewed)

    assert code == EXIT_OK
    assert [(s.key.rg_mbid, s.spotify_id) for s in plan.save] == [("rg-also", "sp-kida")]
    assert plan.excluded_unreviewed == [], "a single album never widens to the artist's other albums"
    assert library.writes() == [], "planning is a dry run"


def test_saving_one_album_without_the_review_snapshot_is_refused(tmp_path: Path) -> None:
    lidarr, library, _ = a_library()
    decisions = write_decisions(tmp_path, save_releases=["rg-also"])
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx, pytest.raises(ps.PromoteSaveError) as caught:
        ps.promote_save_command(ctx, decisions=decisions, out=out, now=NOW)

    assert "--reviewed" in str(caught.value) and "1 single album(s)" in str(caught.value)
    assert library.writes() == [] and not out.exists()


@pytest.mark.parametrize(
    ("reviewed", "saves", "lidarr_files", "why"),
    [
        ({SAVE_MBID: [("rg-kept", 10)]}, {}, 11, "not in the review snapshot with files"),  # never reviewed
        ({SAVE_MBID: [("rg-also", 0)]}, {}, 11, "not in the review snapshot with files"),  # reviewed, no files
        ({SAVE_MBID: [("rg-also", 11)]}, {"rg-also": True}, 0, "no files for it any more"),  # trashed since
        ({SAVE_MBID: [("rg-also", 11)]}, {"rg-also": False}, 11, "disagree"),  # the snapshot says not saved
        ({SAVE_MBID: [("rg-also", 11)]}, {}, 11, "disagree"),  # a snapshot from before the flag
    ],
)
def test_an_album_saved_on_its_own_must_be_reviewed_marked_saved_and_still_have_files(
    tmp_path: Path, reviewed: dict[str, list[tuple[str, int]]], saves: dict[str, bool], lidarr_files: int, why: str
) -> None:
    lidarr, library, links = a_library()
    lidarr.albums[SAVE_MBID]["rg-also"] = lidarr_album(ALSO_KEPT, id=101, artist_id=2, files=lidarr_files)
    decisions = write_decisions(tmp_path, save_releases=["rg-also"])
    snapshot = write_reviewed(tmp_path, reviewed, saves=saves)

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links, reviewed=snapshot)

    assert plan.save == [] and plan.already_saved == []
    assert [u.rg_mbid for u in plan.unmatched] == ["rg-also"]
    assert why in plan.unmatched[0].reason
    assert ("search_albums", ("Radiohead", "Kid A")) not in library.calls, "not even looked up"


def test_an_album_of_a_save_artist_also_saved_on_its_own_is_saved_once(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID], save_releases=["rg-also"])
    reviewed = write_reviewed(tmp_path, saves={"rg-kept": True, "rg-also": True})

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links, reviewed=reviewed)

    assert sorted(s.key.rg_mbid for s in plan.save) == ["rg-also", "rg-kept"]


def test_an_album_kept_with_no_change_is_not_saved_with_its_artist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID], save_exclude_releases=["rg-also"])
    reviewed = write_reviewed(tmp_path, saves={"rg-kept": True, "rg-also": False})

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links, reviewed=reviewed)

    assert [s.key.rg_mbid for s in plan.save] == ["rg-kept"]  # "Same as artist": saved
    assert plan.unmatched == []
    assert ("search_albums", ("Radiohead", "Kid A")) not in library.calls
    assert "Radiohead - Kid A" in capsys.readouterr().out.split("kept with no change on Spotify, by hand:")[1]


@pytest.mark.parametrize(
    ("exclude", "saves"),
    [
        (["rg-also"], {"rg-kept": True, "rg-also": True}),  # the file says keep, the snapshot says save
        ([], {"rg-kept": True, "rg-also": False}),  # the file says save, the snapshot says keep
    ],
)
def test_a_save_the_file_and_the_snapshot_disagree_on_is_refused(
    tmp_path: Path, exclude: list[str], saves: dict[str, bool]
) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save=[SAVE_MBID], save_exclude_releases=exclude)

    _, plan = plan_once(tmp_path, decisions, lidarr, library, links, reviewed=write_reviewed(tmp_path, saves=saves))

    assert [s.key.rg_mbid for s in plan.save] == ["rg-kept"]
    assert [(u.rg_mbid, "disagree" in u.reason) for u in plan.unmatched] == [("rg-also", True)]


def test_following_an_artist_and_saving_one_album_are_both_planned(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, promote=[PROMOTE_MBID], save_releases=["rg-also"])

    _, plan = plan_once(
        tmp_path, decisions, lidarr, library, links, reviewed=write_reviewed(tmp_path, saves=SAVED_ALSO)
    )

    assert [f.spotify_id for f in plan.follow] == ["sp-boc"]
    assert [s.spotify_id for s in plan.save] == ["sp-kida"]


def test_a_decisions_file_and_snapshot_from_before_55_digest_as_they_did(tmp_path: Path) -> None:
    """Literal digests computed by the code before the new fields existed: a plan made then
    must not read as stale after the upgrade."""
    decisions = ps.read_decisions(
        write_decisions(tmp_path, promote=["artist-promote", "artist-b"], save=["artist-save"])
    )
    snapshot = ps.read_reviewed(write_reviewed(tmp_path, {"artist-save": [("rg-kept", 10), ("rg-also", 11)]}))

    assert decisions.digest() == "bb0e1edbe960f5b4da0875ac83cff1f2c8825be86bc60157997642867dc26e85"
    assert snapshot.digest() == "3fe0694c01a37b38f12caf0d87ad40c59275eb793c0505c6044999dcfc3209c9"
    with_more = ps.read_decisions(
        write_decisions(tmp_path, promote=["artist-promote"], save_releases=["c"], save_exclude_releases=["d"])
    )
    assert (with_more.save_releases, with_more.save_exclude_releases) == (("c",), ("d",))


def test_apply_refuses_a_plan_whose_single_album_saves_changed(tmp_path: Path) -> None:
    lidarr, library, links = a_library()
    decisions = write_decisions(tmp_path, save_releases=["rg-also"])
    reviewed = write_reviewed(tmp_path, saves=SAVED_ALSO)
    out = tmp_path / "plan.json"

    with make_context(tmp_path, lidarr=lidarr, library=library) as ctx:
        ps.promote_save_command(ctx, decisions=decisions, reviewed=reviewed, out=out, now=NOW, links=links)
        write_decisions(tmp_path, save_releases=["rg-also", "rg-kept"])
        code = ps.promote_save_command(ctx, apply_path=out, do_apply=True, now=NOW)

    assert code == EXIT_STALE
    assert library.writes() == []
