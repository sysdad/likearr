"""The whole pure pipeline, composed, over the real corpus.

resolve -> desire -> diff -> adopt -> prune -> explain, with no I/O anywhere. This is the test
that catches a module drifting out of step with its neighbours, and it doubles as the worked
example of how the shell is meant to drive the core.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from likearr.config import GuardsConfig
from likearr.core.adopt import plan_adoption
from likearr.core.desire import build_desired
from likearr.core.diff import build_diff, is_stale, lidarr_digest
from likearr.core.explain import explain
from likearr.core.prune import build_prune_report
from likearr.core.resolver import EXCLUDED_COMPILATION_STEP, resolve_all
from likearr.models import (
    NO_EXCLUSIONS,
    RESOLVER_VERSION,
    ExclusionRules,
    Profile,
    ReasonKind,
    ReleaseKey,
    ResolutionStatus,
)
from tests.unit.fakes import (
    FULL_PROFILE_ID,
    LEAN_PROFILE_ID,
    NOW,
    Corpus,
    album_intent,
    artist_intent,
    lidarr_album,
    lidarr_artist,
    lidarr_view,
    live_reason_keys,
    load_corpus,
    owned,
    snapshot,
    spotify_album,
    track_intent,
)

FALLBACK_DAYS = 180


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def library(corpus: Corpus):
    """A small, realistic set of Spotify intents over the corpus.

    - follows Bon Iver (a whole catalogue)
    - saved Radiohead's OK Computer
    - liked The Weeknd's "Blinding Lights", on the single (resolves to After Hours)
    - liked Adele's "Skyfall", a single with no album (pending in 2012)
    """
    weeknd_single = corpus.rg("blinding_lights_single")
    skyfall = corpus.rg("skyfall_single")
    return snapshot(
        artists=[artist_intent("Bon Iver", spotify_id="sp-bon-iver")],
        albums=[
            album_intent(
                spotify_album(
                    "OK Computer",
                    spotify_id="sp-okc",
                    artists=("Radiohead",),
                    upc=corpus.barcode("ok_computer"),
                )
            )
        ],
        tracks=[
            track_intent(
                "Blinding Lights",
                spotify_album(
                    weeknd_single.title,
                    spotify_id="sp-bl-album",
                    artists=("The Weeknd",),
                    upc=corpus.barcode("blinding_lights_single"),
                    album_type="single",
                ),
                spotify_id="sp-bl",
                isrc=corpus.isrc("blinding_lights_single", "Blinding Lights"),
                artists=("The Weeknd",),
            ),
            track_intent(
                "Skyfall",
                spotify_album(
                    skyfall.title,
                    spotify_id="sp-sky-album",
                    artists=("Adele",),
                    upc=corpus.barcode("skyfall_single"),
                    album_type="single",
                ),
                spotify_id="sp-sky",
                isrc=corpus.isrc("skyfall_single", "Skyfall"),
                artists=("Adele",),
            ),
        ],
    )


def run_core(
    corpus: Corpus,
    snap,
    view,
    owned_map=None,
    *,
    now=NOW,
    scheduled=False,
    rules: ExclusionRules = NO_EXCLUSIONS,
    searches=None,
    relations: bool = True,
):
    """Exactly the sequence the shell performs, minus every side effect."""
    lookup = corpus.lookup(searches=searches) if searches else corpus.lookup()
    # The corpus records no tracklist for Try!; issue #14's rule needs the liked song to be on it.
    lookup.tracklists.setdefault(corpus.rg("try_live").mbid, ["Gravity"])
    resolved = resolve_all(
        snap,
        lookup,
        now=now,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        rules=rules,
        relations=lookup if relations else None,
    )
    desired = build_desired(snap, resolved, lookup, albums_only_artists=set())
    diff = build_diff(
        desired,
        view,
        owned_map or {},
        {},
        last_source_counts={},
        last_followed_counts={},
        source_counts=snap.counts,
        live_reason_keys=live_reason_keys(snap),
        guards=GuardsConfig(),
        scheduled=scheduled,
        schema_ok=snap.schema_ok,
        now=now,
        source_digest=snap.digest(),
        lean_profile_id=LEAN_PROFILE_ID,
        full_profile_id=FULL_PROFILE_ID,
    )
    return resolved, desired, diff


# --------------------------------------------------------------------------- a cold start


def test_a_cold_start_adds_artists_and_monitors_nothing_it_does_not_own(corpus: Corpus, library) -> None:
    resolved, _, diff = run_core(corpus, library, lidarr_view(), now=datetime(2012, 11, 1, tzinfo=UTC))

    assert resolved.metadata_errors == 0
    added = {a.artist_mbid for a in diff.add_artists}
    assert corpus.rg("blood_bank").artist_mbid in added, "the followed artist"
    assert corpus.rg("ok_computer").artist_mbid in added, "the saved album's artist"
    assert corpus.rg("after_hours").artist_mbid in added, "the liked track's album artist"

    assert diff.monitor, "everything desired is unmonitored, because Lidarr is empty"
    assert not diff.unmonitor, "nothing is owned yet, so nothing can be unmonitored"
    assert not diff.guards

    # the liked single resolved to the album, not the single
    monitored = {m.key.rg_mbid for m in diff.monitor}
    assert corpus.rg("after_hours").mbid in monitored
    assert corpus.rg("blinding_lights_single").mbid not in monitored

    # Skyfall is pending in 2012, so nothing is monitored for it at all
    assert len(diff.pending) == 1
    assert diff.pending[0].single_release_group == corpus.rg("skyfall_single")
    assert corpus.rg("skyfall_single").mbid not in monitored


def test_the_followed_artists_whole_studio_catalogue_is_wanted(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    bon_iver = corpus.rg("blood_bank").artist_mbid
    wanted = {k.rg_mbid for k in desired.releases if k.artist_mbid == bon_iver}
    assert corpus.rg("blood_bank").mbid in wanted
    assert desired.followed_counts[bon_iver] == len(wanted)
    assert desired.followed_counts[bon_iver] > 1
    for key in desired.releases:
        if key.artist_mbid == bon_iver:
            assert desired.releases[key].release_group.is_studio_album_or_ep


def test_the_pending_single_falls_back_and_ratchets_the_artist_to_full(corpus: Corpus, library) -> None:
    adele = corpus.rg("skyfall_single").artist_mbid
    view = lidarr_view(artists=[lidarr_artist(adele, id=3, name="Adele", metadata_profile_id=LEAN_PROFILE_ID)])
    _, desired, diff = run_core(corpus, library, view, now=NOW)

    assert not diff.pending, "2026 is well past the 180 day window"
    assert desired.profile_needs[adele] is Profile.FULL
    assert [r.artist_mbid for r in diff.ratchets] == [adele]


# --------------------------------------------------------------------------- a converged run


def _converged_view(corpus: Corpus, desired):
    """A Lidarr in exactly the state the desired state asks for."""
    artists, albums = [], []
    for index, (mbid, name) in enumerate(sorted(desired.artists.items()), start=1):
        profile = FULL_PROFILE_ID if desired.profile_needs[mbid] is Profile.FULL else LEAN_PROFILE_ID
        artists.append(lidarr_artist(mbid, id=index, name=name, metadata_profile_id=profile))
    for index, (_key, release) in enumerate(sorted(desired.releases.items(), key=lambda kv: kv[0].rg_mbid), start=1):
        albums.append(lidarr_album(release.release_group, id=1000 + index, artist_id=1, monitored=True, files=3))
    return lidarr_view(artists=artists, albums=albums)


def _own_everything(desired, view):
    out = {}
    for key, release in desired.releases.items():
        album = view.album(key)
        owned_key, record = owned(
            release.release_group,
            *sorted(release.reasons, key=lambda r: r.key),
            step=next(iter(sorted(release.steps.values())), "test"),
            album_id=album.id if album else None,
        )
        out[owned_key] = record
    return out


def test_a_converged_state_produces_an_empty_diff(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)
    _, _, diff = run_core(corpus, library, view, owned_map)
    assert diff.is_empty, f"expected no work, got {diff.monitor} {diff.unmonitor} {diff.ratchets}"
    assert not diff.update_reasons
    assert not diff.guards
    assert diff.projected_wanted == 0


def test_running_twice_over_a_converged_state_is_still_empty(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)
    first = run_core(corpus, library, view, owned_map)[2]
    second = run_core(corpus, library, view, owned_map)[2]
    assert first.is_empty and second.is_empty
    assert first.lidarr_digest == second.lidarr_digest
    assert first.source_digest == second.source_digest


# --------------------------------------------------------------------------- unliking something


def test_unliking_a_track_unmonitors_exactly_its_album(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)

    smaller = snapshot(
        artists=library.artists,
        albums=library.albums,
        tracks=tuple(t for t in library.tracks if t.spotify_id != "sp-bl"),
    )
    _, _, diff = run_core(corpus, smaller, view, owned_map)
    assert [u.key.rg_mbid for u in diff.unmonitor] == [corpus.rg("after_hours").mbid]
    assert not diff.monitor


def test_a_source_that_fails_to_resolve_does_not_unmonitor_anything(corpus: Corpus, library) -> None:
    """The reason is still in Spotify; only the lookup broke. Nothing may be lost."""
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)

    lookup = corpus.lookup(
        fail={
            "release_groups_by_barcode",
            "search_release_group_candidates",
            "search_artist",
            "search_artist_candidates",
        }
    )
    resolved = resolve_all(library, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS)
    assert resolved.metadata_errors > 0
    broken = build_desired(library, resolved, lookup, albums_only_artists=set())
    assert not broken.releases, "the failure really did wipe the desired state"

    diff = build_diff(
        broken,
        view,
        owned_map,
        {},
        last_source_counts={},
        last_followed_counts={},
        source_counts=library.counts,
        live_reason_keys=live_reason_keys(library),
        guards=GuardsConfig(),
        scheduled=False,
        schema_ok=True,
        now=NOW,
        source_digest=library.digest(),
        lean_profile_id=LEAN_PROFILE_ID,
        full_profile_id=FULL_PROFILE_ID,
    )
    assert not diff.unmonitor, "every reason is still live in the snapshot"


# --------------------------------------------------------------------------- staleness


def test_a_diff_goes_stale_when_the_snapshot_changes(corpus: Corpus, library) -> None:
    _, _, diff = run_core(corpus, library, lidarr_view())
    smaller = snapshot(artists=library.artists, albums=library.albums, tracks=())
    assert is_stale(diff, smaller.digest(), diff.lidarr_digest) is True
    assert is_stale(diff, library.digest(), diff.lidarr_digest) is False


def test_a_diff_goes_stale_when_a_touched_album_is_monitored_by_someone_else(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    unmonitored = lidarr_view(
        artists=list(view.artists.values()),
        albums=[
            lidarr_album(
                desired.releases[ReleaseKey(a.artist_mbid, a.rg_mbid)].release_group,
                id=a.id,
                monitored=False,
            )
            for albums in view.albums.values()
            for a in albums.values()
        ],
    )
    _, _, diff = run_core(corpus, library, unmonitored)
    assert diff.monitor
    current = lidarr_digest(view, diff.monitor, diff.unmonitor, diff.ratchets, diff.set_new_items_none)
    assert is_stale(diff, diff.source_digest, current) is True


# --------------------------------------------------------------------------- adopt, prune, explain


def test_adoption_claims_what_is_wanted_and_offers_the_rest(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    stranger = corpus.rg("hey_jude_comp")
    view.artists[stranger.artist_mbid] = lidarr_artist(stranger.artist_mbid, id=99, name=stranger.artist_name)
    view.albums[stranger.artist_mbid] = {stranger.mbid: lidarr_album(stranger, id=999, monitored=True, files=12)}

    plan = plan_adoption(desired, view, {}, {f"artist:{stranger.artist_mbid}"}, now=NOW)
    assert not plan.unmonitor
    assert [k.key.rg_mbid for k in plan.keep_as_manual] == [stranger.mbid]
    assert len(plan.claim) == len(desired.releases)
    assert all(not c.is_manual for c in plan.claim)

    dropped = plan_adoption(desired, view, {}, set(), now=NOW)
    assert [u.key.rg_mbid for u in dropped.unmonitor] == [stranger.mbid]


def test_the_pending_singles_files_are_protected_from_prune(corpus: Corpus, library) -> None:
    now = datetime(2012, 11, 1, tzinfo=UTC)
    resolved, desired, _ = run_core(corpus, library, lidarr_view(), now=now)
    single = corpus.rg("skyfall_single")
    view = lidarr_view(
        artists=[lidarr_artist(single.artist_mbid, id=3, name=single.artist_name)],
        albums=[lidarr_album(single, id=300, monitored=False, files=1, size=5_000_000)],
    )
    report = build_prune_report(desired, view, {}, resolved.resolutions.values(), now=now, tracks=library.tracks)
    assert not report.candidates, "deleting it would lose the only copy of a liked song"
    assert [r.rg_mbid for r in report.protected] == [single.mbid]
    assert report.total_bytes == 0
    protection = report.protected[0].protection  # why, by the song's title (#64)
    assert protection is not None
    assert (protection.kind, protection.source, protection.song) == ("pending_album", "liked", "Skyfall")


def test_the_same_single_becomes_prunable_once_it_is_no_longer_pending(corpus: Corpus, library) -> None:
    """In 2026 the fallback fired, so the single is itself the monitored, wanted release."""
    resolved, desired, _ = run_core(corpus, library, lidarr_view(), now=NOW)
    single = corpus.rg("skyfall_single")
    view = lidarr_view(
        artists=[lidarr_artist(single.artist_mbid, id=3, name=single.artist_name)],
        albums=[lidarr_album(single, id=300, monitored=True, files=1, size=5_000_000)],
    )
    report = build_prune_report(desired, view, {}, resolved.resolutions.values(), now=NOW)
    assert not report.candidates, "it is wanted now, which is a stronger reason to keep it"
    assert not report.protected


def test_explain_walks_the_whole_chain_for_a_liked_track(corpus: Corpus, library) -> None:
    resolved, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)
    out = explain(
        "After Hours",
        desired=desired,
        owned=owned_map,
        view=view,
        resolutions=resolved.resolutions,
        artist_resolutions=resolved.artist_resolutions,
    )
    assert "After Hours" in out
    assert "liked:sp-bl" in out
    assert "track:isrc->album" in out
    assert "owned by likearr: yes" in out
    assert "in Lidarr: monitored" in out


def test_explain_says_why_a_single_is_not_monitored(corpus: Corpus, library) -> None:
    now = datetime(2012, 11, 1, tzinfo=UTC)
    resolved, desired, _ = run_core(corpus, library, lidarr_view(), now=now)
    out = explain(
        "Skyfall",
        desired=desired,
        owned={},
        view=lidarr_view(),
        resolutions=resolved.resolutions,
        artist_resolutions=resolved.artist_resolutions,
    )
    assert "pending album: liked:sp-sky" in out
    assert "waiting on:" in out


def test_explain_finds_a_followed_artist_by_name(corpus: Corpus, library) -> None:
    resolved, desired, _ = run_core(corpus, library, lidarr_view())
    out = explain(
        "Bon Iver",
        desired=desired,
        owned={},
        view=lidarr_view(),
        resolutions=resolved.resolutions,
        artist_resolutions=resolved.artist_resolutions,
    )
    assert "followed on Spotify: yes" in out
    assert "a followed artist, by name" in out  # the artist:search step, in words


# --------------------------------------------------------------------------- the whole thing is pure


def test_the_pipeline_never_reads_the_clock(corpus: Corpus, library) -> None:
    """Two runs with different `now` differ only where a date rule says they should."""
    early = run_core(corpus, library, lidarr_view(), now=datetime(2012, 11, 1, tzinfo=UTC))[2]
    late = run_core(corpus, library, lidarr_view(), now=NOW)[2]
    assert early.created_at != late.created_at
    assert early.source_digest == late.source_digest
    assert {m.key.rg_mbid for m in late.monitor} - {m.key.rg_mbid for m in early.monitor} == {
        corpus.rg("skyfall_single").mbid
    }


def test_every_resolution_carries_a_readable_detail(corpus: Corpus, library) -> None:
    """`explain` is only as good as the detail strings, so none may be empty."""
    resolved, _, _ = run_core(corpus, library, lidarr_view(), now=datetime(2012, 11, 1, tzinfo=UTC))
    for resolution in [*resolved.resolutions.values(), *resolved.artist_resolutions.values()]:
        assert resolution.step, resolution
        assert resolution.detail, resolution
        assert resolution.resolver_version == RESOLVER_VERSION


def test_reason_kinds_survive_the_whole_pipeline(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    kinds = {r.kind for release in desired.releases.values() for r in release.reasons}
    assert kinds == {ReasonKind.FOLLOWED, ReasonKind.SAVED, ReasonKind.LIKED}


def test_nothing_resolved_to_a_various_artists_release(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    assert not [r for r in desired.releases.values() if r.release_group.is_various_artists]


def test_a_schema_failure_stops_every_unmonitor_but_not_the_adds(corpus: Corpus, library) -> None:
    _, desired, _ = run_core(corpus, library, lidarr_view())
    view = _converged_view(corpus, desired)
    owned_map = _own_everything(desired, view)
    degraded = snapshot(
        artists=library.artists,
        albums=library.albums,
        tracks=tuple(t for t in library.tracks if t.spotify_id != "sp-bl"),
        schema_ok=False,
    )
    _, _, diff = run_core(corpus, degraded, view, owned_map)
    assert not diff.unmonitor
    assert [g.code for g in diff.guards] == ["schema"]
    assert diff.guarded is True


def test_a_pending_resolution_is_visible_in_the_diff(corpus: Corpus, library) -> None:
    _, _, diff = run_core(corpus, library, lidarr_view(), now=datetime(2012, 11, 1, tzinfo=UTC))
    assert len(diff.pending) == 1
    assert diff.pending[0].status == ResolutionStatus.PENDING_ALBUM


# ------------------------------------------- the issue #15 opt-outs, end to end


@pytest.fixture
def liked_a_compilation_only_song(corpus: Corpus):
    """The Beatles' "Hey Jude": a non-album single collected only on compilations.

    Release group 0e986744-f2d8-4066-b6d2-51487aee38df, typed Album + Compilation, recorded live
    from MusicBrainz. It is the corpus's own `track:non-studio` case, which is the rule issue #15
    asks to be able to switch off.
    """
    release = corpus.rg("hey_jude_comp")
    snap = snapshot(
        tracks=[
            track_intent(
                "Hey Jude",
                spotify_album(
                    release.title, spotify_id="sp-hj-album", artists=("The Beatles",), album_type="compilation"
                ),
                spotify_id="sp-hj",
                artists=("The Beatles",),
            )
        ]
    )
    return snap, release, {("The Beatles", release.title): release.mbid}


def test_the_whole_pipeline_monitors_the_compilation_by_default(corpus: Corpus, liked_a_compilation_only_song) -> None:
    snap, release, searches = liked_a_compilation_only_song
    view = lidarr_view(artists=[lidarr_artist(release.artist_mbid)])

    resolved, desired, diff = run_core(corpus, snap, view, searches=searches)

    assert [r.step for r in resolved.resolutions.values()] == ["track:non-studio"]
    assert ReleaseKey(release.artist_mbid, release.mbid) in desired.releases
    assert not [u for u in diff.unmapped if u.step.startswith("track:excluded:")]


def test_opting_out_unmonitors_the_compilation_and_reports_the_track(
    corpus: Corpus, liked_a_compilation_only_song
) -> None:
    """The whole point of the feature: the box set actually leaves the library.

    The song is still liked, so before issue #15 the reason stayed live and nothing was ever
    unmonitored however the track resolved. `core.diff` now treats an opted-out intent's reason
    as lost, and this is the test that the two halves meet.
    """
    snap, release, searches = liked_a_compilation_only_song
    view = lidarr_view(
        artists=[lidarr_artist(release.artist_mbid)],
        albums=[lidarr_album(release, monitored=True)],
    )
    key, record = owned(release, next(iter(snap.tracks)).reason)

    _resolved, _desired, diff = run_core(
        corpus,
        snap,
        view,
        {key: record},
        rules=ExclusionRules(allow_compilation_fallback=False),
        searches=searches,
    )

    assert [u.key for u in diff.unmonitor] == [key]
    assert [u.step for u in diff.unmapped] == [EXCLUDED_COMPILATION_STEP]
    assert release.title in diff.unmapped[0].detail, "the report names what it refused"
    assert not diff.monitor


def test_a_denied_release_group_reaches_the_diff_the_same_way(corpus: Corpus, liked_a_compilation_only_song) -> None:
    """The deny list is the tool for cases no rule can express - see the Cannonball example."""
    snap, release, searches = liked_a_compilation_only_song
    view = lidarr_view(
        artists=[lidarr_artist(release.artist_mbid)],
        albums=[lidarr_album(release, monitored=True)],
    )
    key, record = owned(release, next(iter(snap.tracks)).reason)

    _resolved, _desired, diff = run_core(
        corpus,
        snap,
        view,
        {key: record},
        rules=ExclusionRules(deny_releases=frozenset({release.mbid})),
        searches=searches,
    )

    assert [u.key for u in diff.unmonitor] == [key]
    assert diff.unmapped[0].step == "track:excluded:denied"


# ------------- the diff exception, pinned: an outage must never look like an opt-out


ALL_OPT_OUTS = ExclusionRules(
    allow_compilation_fallback=False,
    allow_remix_releases=False,
    deny_releases=frozenset({"2f26958e-b86d-3b3c-8a15-57253046ea58"}),  # Grease (The Remix EP)
)

ISRC_TRACK_LOOKUPS = [
    "release_groups_by_barcode",
    "search_release_group_candidates",
    "release_groups_for_isrc",
    "search_artist",
    "artist_release_groups",
    "release_group_track_titles",
]


@pytest.fixture
def liked_the_grease_original(corpus: Corpus):
    """A like that has to travel the whole track path, so every lookup is genuinely reachable.

    Spotify files the original recording on the soundtrack, which MusicBrainz credits to Various
    Artists, so the name search is refused, the ISRC fallback runs, and the artist and catalogue
    lookups follow it.
    """
    soundtrack = corpus.rg("grease_soundtrack")
    snap = snapshot(
        tracks=[
            track_intent(
                "You're the One That I Want",
                spotify_album(
                    soundtrack.title,
                    spotify_id="sp-grease",
                    artists=("John Travolta",),
                    upc=None,
                    album_type="compilation",
                ),
                spotify_id="sp-grease-track",
                isrc=corpus.isrc("grease_remix_ep", "You're the One That I Want (original version)"),
                artists=("John Travolta",),
            )
        ]
    )
    return snap, {("John Travolta", soundtrack.title): None}


@pytest.mark.parametrize("failing", ISRC_TRACK_LOOKUPS)
def test_a_lookup_failure_either_says_so_or_changes_nothing(
    corpus: Corpus, liked_the_grease_original, failing: str
) -> None:
    """The guard on `core.diff`'s single exception to the lost-reason rule.

    An opted-out intent lets go of the release it was holding, and that is safe only while a
    `track:excluded:*` step is impossible to reach by accident: it must come from
    `ExclusionRules` and from nothing else. Every lookup on the track path re-raises
    `MetadataError`, and `resolve_all` turns that into `error:metadata`, which `is_excluded` does
    not match.

    So failing any one lookup has exactly two honest outcomes, and this asserts both rather than
    skipping the ones that do not apply:

    - the lookup was on this track's path, so the answer is `error:metadata` and the owned
      release keeps its monitor; or
    - it was never consulted, so the answer is bit-for-bit the one the working run gives.

    What is excluded is the third outcome - a failure turning some other answer INTO an
    exclusion, which would plan an unmonitor and take a record off the library for the length of
    an outage. All three opt-outs are on, including a deny entry on the very release this track
    resolves to, so an exclusion is sitting there waiting to be reached if any lookup is ever
    softened into "return empty on failure".
    """
    snap, searches = liked_the_grease_original
    owned_rg = corpus.rg("grease_remix_ep")
    view = lidarr_view(
        artists=[lidarr_artist(owned_rg.artist_mbid)],
        albums=[lidarr_album(owned_rg, monitored=True)],
    )
    key, record = owned(owned_rg, next(iter(snap.tracks)).reason)

    working = next(
        iter(
            resolve_all(
                snap,
                corpus.lookup(searches=searches),
                now=NOW,
                cache={},
                pending_since={},
                fallback_days=FALLBACK_DAYS,
                rules=ALL_OPT_OUTS,
            ).resolutions.values()
        )
    )

    lookup = corpus.lookup(searches=searches)
    lookup.fail = {failing}
    resolved = resolve_all(
        snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS, rules=ALL_OPT_OUTS
    )
    resolution = next(iter(resolved.resolutions.values()))

    if resolved.metadata_errors == 0:
        assert resolution == working, f"{failing} was never consulted, so it cannot have moved the answer"
        return

    assert resolution.step == "error:metadata", "an outage must never read as a preference"
    assert not resolution.step.startswith("track:excluded:")

    desired = build_desired(snap, resolved, lookup, albums_only_artists=set())
    diff = build_diff(
        desired,
        view,
        {key: record},
        {},
        last_source_counts={},
        last_followed_counts={},
        source_counts=snap.counts,
        live_reason_keys=live_reason_keys(snap),
        guards=GuardsConfig(),
        scheduled=False,
        schema_ok=snap.schema_ok,
        now=NOW,
        source_digest=snap.digest(),
        lean_profile_id=LEAN_PROFILE_ID,
        full_profile_id=FULL_PROFILE_ID,
    )
    assert not diff.unmonitor, "the reason is still live, so the release keeps its monitor"


def test_the_failure_test_really_does_exercise_the_lookups(corpus: Corpus, liked_the_grease_original) -> None:
    """A guard on the guard: if no parametrised case reaches a lookup, the test above proves nothing."""
    snap, searches = liked_the_grease_original
    reached = []
    for name in ISRC_TRACK_LOOKUPS:
        lookup = corpus.lookup(searches=searches)
        lookup.fail = {name}
        result = resolve_all(
            snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS, rules=ALL_OPT_OUTS
        )
        if result.metadata_errors:
            reached.append(name)

    assert reached == ["search_release_group_candidates", "release_groups_for_isrc"], (
        "the lookups this track's path actually consults; update the list if the path changes"
    )


def test_the_same_track_with_every_lookup_working_really_is_excluded(corpus: Corpus, liked_the_grease_original) -> None:
    """The control. Without it the two tests above pass just as well on a track nothing refuses."""
    snap, searches = liked_the_grease_original

    resolved = resolve_all(
        snap,
        corpus.lookup(searches=searches),
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        rules=ALL_OPT_OUTS,
    )
    resolution = next(iter(resolved.resolutions.values()))

    assert resolution.step.startswith("track:excluded:")
    assert resolved.metadata_errors == 0


@pytest.mark.parametrize("failing", ["artist_release_groups", "release_group_track_titles"])
def test_a_deny_on_the_release_spotify_named_is_never_reached_through_an_outage(corpus: Corpus, failing: str) -> None:
    """The other half of the invariant, stated rather than left implicit.

    When the deny list refuses the release the name search already mapped, the Singles rule's
    studio Album/EP search still runs from it (issue #96), so the refusal is the answer only once
    that search has come back empty. A failure in it reads as a failure, never as the preference:
    the diff acts on a `track:excluded:*` answer, so an outage must not be able to produce one.
    """
    release = corpus.rg("hey_jude_comp")
    snap = snapshot(
        tracks=[
            track_intent(
                "Hey Jude",
                spotify_album(release.title, spotify_id="sp-hj-album", artists=("The Beatles",)),
                spotify_id="sp-hj",
                artists=("The Beatles",),
            )
        ]
    )
    rules = ExclusionRules(deny_releases=frozenset({release.mbid}))

    def run(fail: set[str]) -> tuple[str, int]:
        lookup = corpus.lookup(searches={("The Beatles", release.title): release.mbid})
        lookup.fail = fail
        resolved = resolve_all(
            snap, lookup, now=NOW, cache={}, pending_since={}, fallback_days=FALLBACK_DAYS, rules=rules
        )
        return next(iter(resolved.resolutions.values())).step, resolved.metadata_errors

    assert run(set()) == ("track:excluded:denied", 0), "the control: with every lookup working it is refused"
    assert run({failing}) == ("error:metadata", 1), "an outage must never read as a preference"


# ------------------------------------------- a credit joined by a MusicBrainz relationship (issue #14)


@pytest.fixture
def liked_a_try_track(corpus: Corpus):
    """John Mayer - "Gravity", liked on Spotify's "TRY! - Live In Concert" (issue #14's 12 intents).

    MusicBrainz holds it as *Try!* by John Mayer Trio (recorded in the corpus), with no ISRC, and
    records John Mayer as a `member of band` of the Trio.
    """
    snap = snapshot(
        tracks=[
            track_intent(
                "Gravity",
                spotify_album("TRY! - Live In Concert", spotify_id="sp-try", artists=("John Mayer",)),
                spotify_id="sp-gravity-live",
                isrc="ZZZZZ0500001",
                artists=("John Mayer",),
            )
        ]
    )
    return snap, corpus.rg("try_live")


def test_try_adds_john_mayer_trio_on_the_full_profile_and_monitors_try(corpus: Corpus, liked_a_try_track) -> None:
    """The outcome #14 names: a new Lidarr artist, John Mayer Trio, added straight onto the Full
    metadata profile because *Try!* is a live album, and *Try!* itself monitored."""
    snap, try_live = liked_a_try_track

    resolved, desired, diff = run_core(corpus, snap, lidarr_view())

    assert [r.step for r in resolved.resolutions.values()] == ["track:non-studio"]
    assert desired.profile_needs[try_live.artist_mbid] is Profile.FULL
    assert [(a.artist_mbid, a.name, a.profile) for a in diff.add_artists] == [
        (try_live.artist_mbid, "John Mayer Trio", Profile.FULL)
    ]
    assert [m.key.rg_mbid for m in diff.monitor] == [try_live.mbid]
    assert not diff.guards


def test_an_existing_trio_on_lean_is_ratcheted_to_full(corpus: Corpus, liked_a_try_track) -> None:
    snap, try_live = liked_a_try_track
    view = lidarr_view(
        artists=[lidarr_artist(try_live.artist_mbid, name="John Mayer Trio", metadata_profile_id=LEAN_PROFILE_ID)]
    )
    _, _, diff = run_core(corpus, snap, view)
    assert [r.artist_mbid for r in diff.ratchets] == [try_live.artist_mbid]


def test_the_name_collision_guard_applies_to_an_artist_the_relationship_brings(
    corpus: Corpus, liked_a_try_track
) -> None:
    """A different Lidarr artist already named "John Mayer Trio" blocks the add, exactly as it
    blocks any other new artist: nothing about the relationship route bypasses the guard."""
    snap, try_live = liked_a_try_track
    view = lidarr_view(artists=[lidarr_artist("some-other-mbid", id=7, name="John Mayer Trio")])

    _, _, diff = run_core(corpus, snap, view)

    assert diff.add_artists == []
    assert [g.code for g in diff.guards] == ["name-collision"]
    assert try_live.mbid not in {m.key.rg_mbid for m in diff.monitor}
    assert [(c.wanted_mbid, c.name) for c in diff.name_collisions] == [(try_live.artist_mbid, "John Mayer Trio")]


def test_without_the_relationship_lookup_try_is_reported_unmapped(corpus: Corpus, liked_a_try_track) -> None:
    snap, _ = liked_a_try_track
    resolved, _, diff = run_core(corpus, snap, lidarr_view(), relations=False)
    assert [r.step for r in resolved.resolutions.values()] == ["track:album:search"]
    assert not diff.add_artists
    assert not diff.monitor
