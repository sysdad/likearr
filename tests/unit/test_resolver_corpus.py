"""The resolver against a golden corpus of real MusicBrainz data.

`tests/fixtures/mb/corpus.json` was recorded live from the public MusicBrainz web service (see
its `_readme`). These tests build the Spotify side of each case the way Spotify actually presents
it - the album Spotify files the track under, its barcode, the track's ISRC, the featured-artist
credits - and assert the resolver's status, step and target release group.

Every case is a real public release. Nothing here comes from anyone's library.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from likearr.core.normalize import normalize_title
from likearr.core.resolver import (
    EXCLUDED_COMPILATION_STEP,
    EXCLUDED_REMIX_STEP,
    REMIX_ONLY_STEP,
    resolve_album,
    resolve_all,
    resolve_track,
)
from likearr.models import (
    LIKED_TRACK_SCOPE_SMALLEST,
    ExclusionRules,
    PrimaryType,
    ResolutionStatus,
    SecondaryType,
)
from tests.unit.fakes import (
    NOW,
    Corpus,
    album_intent,
    artist_intent,
    load_corpus,
    snapshot,
    spotify_album,
    track_intent,
)

FALLBACK_DAYS = 180


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def _spotify_album_for(corpus: Corpus, case: str, *, album_type: str, artists: tuple[str, ...]):
    """The Spotify album ref for a case, carrying the release's real barcode and date."""
    release = corpus.rg(case)
    return spotify_album(
        release.title,
        spotify_id=f"sp-{case}",
        artists=artists,
        upc=corpus.barcode(case),
        album_type=album_type,
        released=release.first_release_date.isoformat() if release.first_release_date else None,
    )


def _resolve(corpus: Corpus, intent, *, now: datetime = NOW, pending_since=None):
    return resolve_track(
        intent,
        corpus.lookup(),
        now=now,
        pending_since=pending_since,
        fallback_days=FALLBACK_DAYS,
    )


# --------------------------------------------------------------------------- the corpus itself


def test_the_corpus_records_what_the_tests_rely_on(corpus: Corpus) -> None:
    """A guard on the fixture, so a bad re-record fails loudly instead of quietly changing answers."""
    assert corpus.rg("ok_computer").primary_type is PrimaryType.ALBUM
    assert corpus.rg("ok_computer").is_studio_album_or_ep
    assert corpus.rg("blinding_lights_single").primary_type is PrimaryType.SINGLE
    assert corpus.rg("blood_bank").primary_type is PrimaryType.EP
    assert corpus.rg("hey_jude_comp").secondary_types
    assert corpus.rg("skyfall_single").is_single
    assert corpus.rg("awesome_mix_1").is_various_artists


# --------------------------------------------------------------------------- 1. the easy case


def test_a_liked_track_on_a_studio_album_resolves_to_that_album(corpus: Corpus) -> None:
    """Radiohead - "Karma Police", filed by Spotify on OK Computer, which is the album."""
    album = _spotify_album_for(corpus, "ok_computer", album_type="album", artists=("Radiohead",))
    result = _resolve(corpus, track_intent("Karma Police", album, isrc=corpus.isrc("ok_computer", "Karma Police")))
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == corpus.rg("ok_computer")


# --------------------------------------------------------------------------- 2. single -> album by ISRC


def test_a_liked_single_resolves_to_the_album_holding_the_same_recording(corpus: Corpus) -> None:
    """The Weeknd - "Blinding Lights": Spotify's album is the single, the song is on After Hours.

    The corpus holds two studio albums carrying that ISRC (After Hours 2020 and a 2026 Greatest
    Hits), so this also exercises the earliest-release-date tie-break on real data.
    """
    album = _spotify_album_for(corpus, "blinding_lights_single", album_type="single", artists=("The Weeknd",))
    intent = track_intent(
        "Blinding Lights",
        album,
        isrc=corpus.isrc("blinding_lights_single", "Blinding Lights"),
        artists=("The Weeknd",),
    )
    result = _resolve(corpus, intent)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:isrc->album"
    assert result.release_group == corpus.rg("after_hours")
    assert result.source_release_group == corpus.rg("blinding_lights_single")


def test_the_tie_break_really_had_something_to_break(corpus: Corpus) -> None:
    isrc = corpus.isrc("blinding_lights_single", "Blinding Lights")
    weeknd = corpus.rg("after_hours").artist_mbid
    candidates = [
        corpus.groups[m]
        for m in corpus.raw["isrc_release_groups"][isrc]
        if corpus.groups[m].is_studio_album_or_ep and corpus.groups[m].artist_mbid == weeknd
    ]
    assert len(candidates) > 1, "the corpus no longer exercises the tie-break"


# --------------------------------------------------------------------------- 3. radio edit -> title fallback


def test_a_radio_edit_with_its_own_isrc_resolves_by_title(corpus: Corpus) -> None:
    """Daft Punk - "Get Lucky - Radio Edit".

    The radio edit is a separate MusicBrainz recording with its own ISRC (USQX91300809), which
    appears on no studio album, so only the normalised title connects it to Random Access
    Memories.
    """
    edit_isrc = corpus.isrc("get_lucky_single", "Get Lucky (radio edit)")
    album_isrc = corpus.isrc("get_lucky_single", "Get Lucky")
    assert edit_isrc != album_isrc, "the corpus no longer distinguishes the edit from the album cut"

    album = _spotify_album_for(corpus, "get_lucky_single", album_type="single", artists=("Daft Punk",))
    intent = track_intent("Get Lucky - Radio Edit", album, isrc=edit_isrc, artists=("Daft Punk", "Pharrell Williams"))
    result = _resolve(corpus, intent)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:title->album"
    assert result.release_group == corpus.rg("random_access_memories")


def test_the_radio_edits_isrc_really_is_on_no_studio_album(corpus: Corpus) -> None:
    edit_isrc = corpus.isrc("get_lucky_single", "Get Lucky (radio edit)")
    hits = [corpus.groups[m] for m in corpus.raw["isrc_release_groups"][edit_isrc]]
    assert hits, "the corpus records no release groups for the radio edit at all"
    assert not [h for h in hits if h.is_studio_album_or_ep], "the ISRC path would have answered first"


def test_the_album_cut_of_the_same_song_takes_the_isrc_path(corpus: Corpus) -> None:
    """The unedited recording is on Random Access Memories, so the ISRC answers directly."""
    album = _spotify_album_for(corpus, "get_lucky_single", album_type="single", artists=("Daft Punk",))
    intent = track_intent("Get Lucky", album, isrc=corpus.isrc("get_lucky_single", "Get Lucky"))
    result = _resolve(corpus, intent)
    assert result.step == "track:isrc->album"
    assert result.release_group == corpus.rg("random_access_memories")


def test_the_title_fallback_matched_a_real_tracklist(corpus: Corpus) -> None:
    titles = {normalize_title(t) for t in corpus.raw["tracklists"][corpus.rg("random_access_memories").mbid]}
    assert normalize_title("Get Lucky - Radio Edit") in titles


# --------------------------------------------------------------------------- 4. an EP Spotify calls a single


def test_an_ep_spotify_files_as_a_single_is_monitored_as_an_ep(corpus: Corpus) -> None:
    """Bon Iver - "Blood Bank". Spotify's album_type is 'single'; MusicBrainz says EP."""
    album = _spotify_album_for(corpus, "blood_bank", album_type="single", artists=("Bon Iver",))
    assert album.album_type == "single"
    intent = track_intent("Blood Bank", album, isrc=corpus.isrc("blood_bank", "Blood Bank"), artists=("Bon Iver",))
    result = _resolve(corpus, intent)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == corpus.rg("blood_bank")
    assert result.release_group is not None
    assert result.release_group.primary_type is PrimaryType.EP


# --------------------------------------------------------------------------- 5. a single with no album


def test_a_single_with_no_album_is_pending_then_falls_back(corpus: Corpus) -> None:
    """Adele - "Skyfall": a soundtrack single that never landed on one of her studio albums."""
    album = _spotify_album_for(corpus, "skyfall_single", album_type="single", artists=("Adele",))
    intent = track_intent("Skyfall", album, isrc=corpus.isrc("skyfall_single", "Skyfall"), artists=("Adele",))

    soon = _resolve(corpus, intent, now=datetime(2012, 11, 1, tzinfo=UTC))
    assert soon.status == ResolutionStatus.PENDING_ALBUM
    assert soon.release_group is None
    assert soon.single_release_group == corpus.rg("skyfall_single")
    assert soon.single_release_date is not None
    assert soon.single_release_date.isoformat() == "2012-10-04"

    on_the_day = _resolve(corpus, intent, now=datetime(2013, 4, 2, tzinfo=UTC))  # 180 days later
    assert on_the_day.status == ResolutionStatus.RESOLVED
    assert on_the_day.step == "track:single-fallback"
    assert on_the_day.release_group == corpus.rg("skyfall_single")

    day_before = _resolve(corpus, intent, now=datetime(2013, 4, 1, tzinfo=UTC))
    assert day_before.status == ResolutionStatus.PENDING_ALBUM


def test_the_fallback_release_needs_the_full_profile(corpus: Corpus) -> None:
    """Monitoring a single is exactly the case the Full metadata profile exists for."""
    assert not corpus.rg("skyfall_single").is_studio_album_or_ep


# --------------------------------------------------------------------------- 6. only on a compilation


def test_a_track_that_lives_only_on_a_compilation_monitors_the_compilation(corpus: Corpus) -> None:
    """The Beatles - "Hey Jude": a non-album single, collected only on compilations."""
    release = corpus.rg("hey_jude_comp")
    album = spotify_album(
        release.title,
        spotify_id="sp-hey-jude",
        artists=("The Beatles",),
        upc=None,
        album_type="compilation",
        released="1988-01-01",
    )
    lookup = corpus.lookup(searches={("The Beatles", release.title): release.mbid})
    result = resolve_track(
        track_intent("Hey Jude", album, isrc=None, artists=("The Beatles",)),
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
    )
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == release
    assert result.source_release_group == release


def test_no_beatles_studio_album_in_the_corpus_claims_hey_jude(corpus: Corpus) -> None:
    beatles = corpus.rg("hey_jude_comp").artist_mbid
    wanted = normalize_title("Hey Jude")
    for mbid in corpus.raw["artist_release_groups"].get(beatles, []):
        release = corpus.groups[mbid]
        if not release.is_studio_album_or_ep:
            continue
        titles = {normalize_title(t) for t in corpus.raw["tracklists"].get(mbid, [])}
        assert wanted not in titles


# --------------------------------------------------------------------------- 7. a featured credit


def test_a_featured_track_uses_the_primary_credit(corpus: Corpus) -> None:
    """Mark Ronson feat. Bruno Mars - "Uptown Funk": the album must be Mark Ronson's, not Bruno's."""
    album = _spotify_album_for(corpus, "uptown_funk_single", album_type="single", artists=("Mark Ronson", "Bruno Mars"))
    intent = track_intent(
        "Uptown Funk (feat. Bruno Mars)",
        album,
        isrc=corpus.isrc("uptown_funk_single", "Uptown Funk"),
        artists=("Mark Ronson", "Bruno Mars"),
    )
    result = _resolve(corpus, intent)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:isrc->album"
    assert result.release_group == corpus.rg("uptown_special")
    assert result.release_group is not None
    assert result.release_group.artist_name == "Mark Ronson"


# --------------------------------------------------------------------------- 8. Various Artists


def test_a_various_artists_album_is_unmapped(corpus: Corpus) -> None:
    """Guardians of the Galaxy: Awesome Mix, Vol. 1 - a real Various Artists soundtrack."""
    album = _spotify_album_for(corpus, "awesome_mix_1", album_type="compilation", artists=("Blue Swede",))
    result = resolve_album(album_intent(album), corpus.lookup())
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "album:various-artists"
    assert result.release_group is None
    assert "Various Artists" in result.detail


def test_a_various_artists_album_spotify_credits_as_various_is_unmapped_without_a_lookup(corpus: Corpus) -> None:
    album = _spotify_album_for(corpus, "awesome_mix_1", album_type="compilation", artists=("Various Artists",))
    lookup = corpus.lookup()
    result = resolve_album(album_intent(album), lookup)
    assert result.status == ResolutionStatus.UNMAPPED
    assert lookup.calls == {}


# --------------------------------------------------------------------------- saved albums, for contrast


@pytest.mark.parametrize(
    ("case", "artists"),
    [
        ("ok_computer", ("Radiohead",)),
        ("after_hours", ("The Weeknd",)),
        ("random_access_memories", ("Daft Punk",)),
        ("blood_bank", ("Bon Iver",)),
        ("uptown_special", ("Mark Ronson",)),
    ],
)
def test_a_saved_album_resolves_by_its_real_barcode(corpus: Corpus, case: str, artists: tuple[str, ...]) -> None:
    album = _spotify_album_for(corpus, case, album_type="album", artists=artists)
    result = resolve_album(album_intent(album), corpus.lookup())
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "album:upc"
    assert result.release_group == corpus.rg(case)


def test_a_saved_single_is_kept_as_a_single(corpus: Corpus) -> None:
    """A saved album is honoured whatever its type; the Singles rule is only for liked tracks."""
    album = _spotify_album_for(corpus, "blinding_lights_single", album_type="single", artists=("The Weeknd",))
    result = resolve_album(album_intent(album), corpus.lookup())
    assert result.status == ResolutionStatus.RESOLVED
    assert result.release_group == corpus.rg("blinding_lights_single")


# --------------------------------------------------------------------------- determinism


@pytest.mark.parametrize(
    ("case", "track", "artists", "album_type"),
    [
        ("ok_computer", "Karma Police", ("Radiohead",), "album"),
        ("blinding_lights_single", "Blinding Lights", ("The Weeknd",), "single"),
        ("blood_bank", "Blood Bank", ("Bon Iver",), "single"),
        ("uptown_funk_single", "Uptown Funk", ("Mark Ronson", "Bruno Mars"), "single"),
    ],
)
def test_resolution_over_real_data_is_repeatable(
    corpus: Corpus, case: str, track: str, artists: tuple[str, ...], album_type: str
) -> None:
    album = _spotify_album_for(corpus, case, album_type=album_type, artists=artists)
    intent = track_intent(track, album, isrc=corpus.isrc(case, track), artists=artists)
    first = _resolve(corpus, intent)
    second = _resolve(corpus, intent)
    assert first == second


def test_the_relationship_rule_changes_no_other_answer_in_the_corpus(corpus: Corpus) -> None:
    """The relationship rule's claim, in miniature, in two halves over every recorded track.

    As Spotify credits it (MusicBrainz's own credit), the name search resolves it and the rule is
    never reached. Under a credit MusicBrainz does not hold, with no ISRC, the rule *is* reached,
    finds the title under the real artist, finds no relationship joining the two - the corpus
    records only the Trio's - and the answer is the one without the rule; an UNMAPPED one only
    gains a line saying so.
    """
    reached = 0
    for key, isrc in sorted(corpus.raw["track_isrcs"].items()):
        case, _, track = key.partition(":")
        release = corpus.rg(case)
        for credit, track_isrc in ((release.artist_name, isrc), (f"{release.artist_name} Tribute Act", None)):
            album = _unmapped_spotify_album(release.title, (credit,))
            intent = track_intent(track, album, isrc=track_isrc, artists=(credit,))
            lookup = corpus.lookup()
            with_rule = resolve_track(
                intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, relations=lookup
            )
            without = _resolve(corpus, intent)
            same = (with_rule.status, with_rule.step, with_rule.release_group)
            assert same == (without.status, without.step, without.release_group), (key, credit)
            if with_rule.detail != without.detail:
                # The only visible change: an UNMAPPED detail says no relationship joins them.
                assert with_rule.detail.startswith(without.detail), key
                assert "relationship joining them" in with_rule.detail, key
            asked = lookup.calls.get("release_groups_under_other_credits", 0)
            if credit == release.artist_name:
                assert asked == 0, f"{key}: the rule ran although the name search resolved it"
            reached += asked > 0
    assert reached > 10, "the rule was hardly exercised; this test proves nothing"


# ------------------------------------------- 8. the album title lookup misses, and the ISRC answers


def _unmapped_spotify_album(name: str, artists: tuple[str, ...]):
    """A Spotify album as a *track* carries one: no barcode, because Spotify never sends the UPC.

    This is a shape recorded across the corpus. Every liked and playlist track that a run left UNMAPPED at
    that step reached the resolver like this, which is why the
    detail on every one of them said "no barcode".
    """
    return spotify_album(name, spotify_id="sp-unmapped", artists=artists, upc=None, album_type="album")


def test_the_isrc_recognises_the_album_when_the_credit_is_what_missed(corpus: Corpus) -> None:
    """The search declines a release group it found, because the two credits differ.

    MusicBrainz's search is refused on artist-credit equality (Spotify's "John Mayer" against
    MusicBrainz's "John Mayer Trio" is the real example from the issue). The ISRC proves the
    recording is on After Hours and the title says which release Spotify meant, so the track
    resolves exactly as a barcode would have resolved it.
    """
    isrc = corpus.isrc("after_hours", "Blinding Lights")
    album = _unmapped_spotify_album("After Hours", ("The Weeknd",))
    lookup = corpus.lookup(searches={("The Weeknd", "After Hours"): None})
    result = resolve_track(
        track_intent("Blinding Lights", album, isrc=isrc, artists=("The Weeknd",)),
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
    )
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == corpus.rg("after_hours")
    assert isrc in result.detail


def test_the_isrc_falls_back_to_the_artist_and_never_to_a_compilation(corpus: Corpus) -> None:
    """Mark Ronson - "Uptown Funk", filed by Spotify on a release MusicBrainz does not carry.

    The real ISRC lands on dozens of Various Artists compilations, which is exactly the risk this
    fallback has to survive: the answer is Uptown Special, Mark Ronson's studio album, and not
    one of the eighty-odd hit collections that also hold the recording.
    """
    isrc = corpus.isrc("uptown_special", "Uptown Funk")
    album = _unmapped_spotify_album("Uptown Funk (feat. Bruno Mars) [The Remixes]", ("Mark Ronson", "Bruno Mars"))
    result = resolve_track(
        track_intent("Uptown Funk", album, isrc=isrc, artists=("Mark Ronson", "Bruno Mars")),
        corpus.lookup(),
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
    )
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:album"
    assert result.release_group == corpus.rg("uptown_special")


def test_that_isrc_really_is_dominated_by_various_artists_compilations(corpus: Corpus) -> None:
    """A guard on the fixture: without the compilations the test above proves nothing."""
    isrc = corpus.isrc("uptown_special", "Uptown Funk")
    hits = [corpus.groups[m] for m in corpus.raw["isrc_release_groups"][isrc]]
    various = [h for h in hits if h.is_various_artists]
    assert len(various) > 50, "the corpus no longer exercises the Various Artists risk"
    assert corpus.rg("uptown_special") in hits


def test_a_release_musicbrainz_records_no_isrcs_for_stays_unmapped(corpus: Corpus) -> None:
    """John Mayer - "TRY! - Live In Concert", which the ISRC does NOT fix.

    Spotify credits the album to "John Mayer"; MusicBrainz credits the release group to "John
    Mayer Trio", a different artist with a different MBID, and that credit mismatch alone is what
    makes the name search refuse a release group it has already found with a perfect score.

    The ISRC cannot rescue it, because MusicBrainz carries **no ISRC for any recording on Try!**
    (see the corpus `_readme`). So without the relationship lookup (see the next test) the track
    stays UNMAPPED at `track:album:search` - and the detail says the ISRC was tried and answered
    nothing, instead of stopping at "no barcode".
    """
    album = _unmapped_spotify_album("TRY! - Live In Concert", ("John Mayer",))
    result = resolve_track(
        track_intent("Gravity", album, isrc="ZZZZZ0500001", artists=("John Mayer",)),
        corpus.lookup(),
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
    )
    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == "track:album:search"
    assert "ZZZZZ0500001" in result.detail


def test_try_resolves_under_john_mayer_trio_through_the_member_of_band_relationship(corpus: Corpus) -> None:
    """On the real MBIDs: John Mayer is a `member of band` of John Mayer Trio, so the
    release group the name search refused on the credit is taken under the Trio's credit.

    It is Album + Live, MusicBrainz knows no ISRC reaching it and the corpus holds no studio album
    by the Trio, so it lands on the Singles rule's step (d): `track:non-studio` to *Try!* itself,
    which is what ratchets the Trio to the Full profile (see `test_pipeline`).

    The corpus records no tracklist for *Try!*, so this stages the one fact the rule now checks:
    "Gravity" is on it (it is track 4 of the public release). Without it the rule refuses.
    """
    lookup = corpus.lookup()
    lookup.tracklists[corpus.rg("try_live").mbid] = ["Gravity"]
    album = _unmapped_spotify_album("TRY! - Live In Concert", ("John Mayer",))
    result = resolve_track(
        track_intent("Gravity", album, isrc="ZZZZZ0500001", artists=("John Mayer",)),
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
        relations=lookup,
    )
    try_live = corpus.rg("try_live")
    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:non-studio"
    assert result.release_group == try_live
    assert try_live.artist_name == "John Mayer Trio"
    assert "'member of band' relationship between 'John Mayer Trio'" in result.detail
    assert lookup.calls["artist_relations"] == 1, "one relationship lookup, for the one other credit"


def test_the_corpus_records_the_try_credit_mismatch(corpus: Corpus) -> None:
    """A guard on the fixture, and the evidence for the follow-up issue.

    `ZZZZZ0500001` is a placeholder standing in for whatever ISRC Spotify sends: MusicBrainz
    knows none of the Try! recordings' ISRCs, so the value cannot matter.
    """
    release = corpus.rg("try_live")
    assert release.title == "Try!"
    assert release.artist_name == "John Mayer Trio"
    assert not release.is_studio_album_or_ep, "Try! is a live album"
    assert corpus.raw["artists"][release.artist_mbid] == "John Mayer Trio"
    john_mayer = [m for m, n in corpus.raw["artists"].items() if n == "John Mayer"]
    assert john_mayer and john_mayer[0] != release.artist_mbid, "the two artists are no longer distinct"
    assert all(release.mbid not in groups for groups in corpus.raw["isrc_release_groups"].values()), (
        "MusicBrainz now records an ISRC reaching Try!; this case needs re-recording"
    )
    joined = corpus.raw["artist_relations"][release.artist_mbid]
    assert [(r["type"], r["artist"]["id"]) for r in joined] == [("member of band", john_mayer[0])], (
        "the Trio's recorded relationship to John Mayer is what this test rests on"
    )


# ------------------------------------ following the artist, on real data


def _smallest(corpus: Corpus, intent, *, artists=(), cache=None):
    """`resolve_all` under the `smallest` scope, deriving the follow set the way the shell does."""
    return resolve_all(
        snapshot(artists=artists, tracks=[intent]),
        corpus.lookup(),
        now=NOW,
        cache=cache or {},
        pending_since={},
        fallback_days=FALLBACK_DAYS,
        scope=LIKED_TRACK_SCOPE_SMALLEST,
    )


def test_following_the_weeknd_swaps_the_cached_single_for_after_hours(corpus: Corpus) -> None:
    """Real data: the same like, resolved before and after the follow.

    Under `liked_track_scope = "smallest"` a like on "Blinding Lights" resolves to the single,
    which is the smallest official release holding the song. Following The Weeknd then monitors
    After Hours anyway, so the dedupe rule says the like should resolve to the album instead - and
    before this it never did, because the single was cached permanently.
    """
    single = corpus.rg("blinding_lights_single")
    album = corpus.rg("after_hours")
    assert album.artist_mbid == single.artist_mbid, "the corpus must credit both to the same artist"

    spotify = _spotify_album_for(corpus, "blinding_lights_single", album_type="single", artists=("The Weeknd",))
    intent = track_intent(
        "Blinding Lights",
        spotify,
        isrc=corpus.isrc("blinding_lights_single", "Blinding Lights"),
        artists=("The Weeknd",),
    )

    before = _smallest(corpus, intent)
    cached = next(iter(before.resolutions.values()))
    assert cached.release_group == single
    assert cached.followed is False

    unchanged = _smallest(corpus, intent, cache=dict(before.resolutions))
    assert next(iter(unchanged.resolutions.values())) == cached, "nothing moved, so nothing is re-resolved"

    follow = artist_intent("The Weeknd", spotify_id="sp-weeknd")
    after = _smallest(corpus, intent, artists=[follow], cache=dict(before.resolutions))
    swapped = after.resolutions[intent.reason.key]

    assert after.artist_resolutions[follow.reason.key].artist_mbid == album.artist_mbid
    assert swapped.step == "track:smallest:covered-by-follow"
    assert swapped.release_group == album
    assert swapped.followed is True


# ------------------------------------ opting out of box sets and remix EPs

NO_COMPILATIONS = ExclusionRules(allow_compilation_fallback=False)
NO_REMIXES = ExclusionRules(allow_remix_releases=False)
KEEP_NO_REMIX_ONLY = ExclusionRules(allow_remix_releases=False, keep_remix_only_tracks=False)


def test_the_corpus_records_what_the_opt_outs_rely_on(corpus: Corpus) -> None:
    """A fixture guard. Every one of these is the reason a rule exists, so a re-record must fail
    loudly rather than quietly turning a test into a tautology."""
    remix_ep = corpus.rg("grease_remix_ep")
    assert remix_ep.primary_type is PrimaryType.EP
    assert not remix_ep.secondary_types, "MusicBrainz does not type it Remix, which is the whole point"
    assert remix_ep.is_studio, "so `is_studio` lets it through and only its title says otherwise"

    soundtrack = corpus.rg("grease_soundtrack")
    assert soundtrack.is_various_artists, "and the release it should lose to is unmonitorable anyway"
    assert not soundtrack.is_studio

    assert not corpus.rg("the_feeling_remixes").secondary_types
    assert SecondaryType.COMPILATION in corpus.rg("dinah_box_set").secondary_types
    assert SecondaryType.LIVE in corpus.rg("mercy_live_at_the_club").secondary_types
    assert not corpus.rg("cannonball_phenix").secondary_types, "an untagged album that holds a live song"
    assert corpus.rg("mercy_live_at_the_club").artist_mbid != corpus.rg("cannonball_phenix").artist_mbid, (
        "two MusicBrainz artists for one performer is the mechanism behind the oddity"
    )


def _grease_like(corpus: Corpus):
    """A like on the original "You're the One That I Want", as Spotify presents it.

    Spotify files it on the soundtrack, which MusicBrainz credits to Various Artists, so the
    resolver falls back to the track's own artist exactly as it does for any compilation.
    """
    soundtrack = corpus.rg("grease_soundtrack")
    album = spotify_album(
        soundtrack.title,
        spotify_id="sp-grease",
        artists=("John Travolta", "Olivia Newton-John"),
        upc=None,
        album_type="compilation",
    )
    return track_intent(
        "You're the One That I Want",
        album,
        isrc=corpus.isrc("grease_remix_ep", "You're the One That I Want (original version)"),
        artists=("John Travolta", "Olivia Newton-John"),
    )


def _lookup(corpus: Corpus, **overrides):
    return corpus.lookup(**overrides)


def test_the_remix_ep_really_is_the_only_studio_candidate_for_that_isrc(corpus: Corpus) -> None:
    """The measurement behind the bug, asserted on the recorded data rather than asserted at."""
    isrc = corpus.isrc("grease_remix_ep", "You're the One That I Want (original version)")
    hits = [corpus.groups[m] for m in corpus.raw["isrc_release_groups"][isrc]]
    studio = [h for h in hits if h.is_studio and not h.is_various_artists]
    assert [h.title for h in studio] == ["Grease (The Remix EP)"]


def test_the_smallest_scope_picks_the_remix_ep_on_real_data(corpus: Corpus) -> None:
    intent = _grease_like(corpus)
    lookup = _lookup(corpus, searches={("John Travolta", corpus.rg("grease_soundtrack").title): None})

    result = resolve_track(
        intent,
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
        scope=LIKED_TRACK_SCOPE_SMALLEST,
    )

    assert result.status == ResolutionStatus.RESOLVED
    assert result.step == "track:smallest:ep"
    assert result.release_group == corpus.rg("grease_remix_ep")


def test_opting_out_of_remixes_refuses_the_grease_ep(corpus: Corpus) -> None:
    """There is no allowed release left - the soundtrack is Various Artists - so it is reported.

    Even with `keep_remix_only_tracks` on, its default: the song *has* a home that is
    not a remix, the soundtrack, so it is not a remix-only song, only one likearr cannot monitor."""
    intent = _grease_like(corpus)
    lookup = _lookup(corpus, searches={("John Travolta", corpus.rg("grease_soundtrack").title): None})

    result = resolve_track(
        intent,
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
        scope=LIKED_TRACK_SCOPE_SMALLEST,
        rules=NO_REMIXES,
    )

    assert result.status == ResolutionStatus.UNMAPPED
    assert result.step == EXCLUDED_REMIX_STEP
    assert result.source_release_group == corpus.rg("grease_remix_ep")
    assert "Grease (The Remix EP)" in result.detail


def test_the_knocks_case_shows_what_the_opt_out_costs(corpus: Corpus) -> None:
    """MusicBrainz knows that ISRC from nowhere else, so refusing the EP costs the song.

    Recorded deliberately. An opt-out that only ever improved things would not need a switch.
    """
    isrc = corpus.isrc("the_feeling_remixes", "The Feeling")
    assert corpus.raw["isrc_release_groups"][isrc] == [corpus.rg("the_feeling_remixes").mbid]

    ep = corpus.rg("the_feeling_remixes")
    intent = track_intent(
        "The Feeling",
        spotify_album(ep.title, spotify_id="sp-feeling", artists=("The Knocks",), upc=None),
        isrc=isrc,
        artists=("The Knocks",),
    )
    lookup = _lookup(corpus, searches={("The Knocks", ep.title): ep.mbid})

    kept = resolve_track(intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS)
    assert kept.status == ResolutionStatus.RESOLVED
    assert kept.release_group == ep

    # It is the song's only release, so by default the opt-out keeps it after all ...
    remix_only = resolve_track(
        intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, rules=NO_REMIXES
    )
    assert remix_only.status == ResolutionStatus.RESOLVED
    assert remix_only.step == REMIX_ONLY_STEP
    assert remix_only.release_group == ep

    # ... and only with keep_remix_only_tracks off too does refusing the EP cost the song.
    lost = resolve_track(
        intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, rules=KEEP_NO_REMIX_ONLY
    )
    assert lost.status == ResolutionStatus.UNMAPPED
    assert lost.step == EXCLUDED_REMIX_STEP


def test_a_box_set_is_monitored_by_default_and_refused_when_opted_out(corpus: Corpus) -> None:
    """Dinah Washington, 3 discs and 53 tracks for one liked song."""
    box = corpus.rg("dinah_box_set")
    intent = track_intent(
        "I Won't Cry Anymore",
        spotify_album(
            box.title, spotify_id="sp-dinah", artists=("Dinah Washington",), upc=corpus.barcode("dinah_box_set")
        ),
        artists=("Dinah Washington",),
    )
    lookup = _lookup(corpus)

    kept = resolve_track(intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS)
    assert kept.status == ResolutionStatus.RESOLVED
    assert kept.step == "track:non-studio"
    assert kept.release_group == box

    refused = resolve_track(
        intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS, rules=NO_COMPILATIONS
    )
    assert refused.status == ResolutionStatus.UNMAPPED
    assert refused.step == EXCLUDED_COMPILATION_STEP
    assert refused.source_release_group == box


def test_one_song_two_albums_is_the_artist_credit_deciding_it(corpus: Corpus) -> None:
    """A related oddity, reproduced on real data.

    The same song, liked twice, landing on two different releases. Not a tie-break wobble: the
    two copies map to two different MusicBrainz artists for one performer, and each artist's
    catalogue is what the title fallback can see. Under the Quintet the only release carrying the
    song is the Live-typed album, so `track:non-studio` monitors it. Under plain Cannonball
    Adderley the catalogue holds *Phenix*, typed Album with no secondary types, whose tracklist
    carries the same title - so `track:title->album` prefers it and the two copies diverge.
    """
    club = corpus.rg("mercy_live_at_the_club")
    phenix = corpus.rg("cannonball_phenix")

    as_quintet = track_intent(
        "Mercy, Mercy, Mercy",
        spotify_album(
            club.title,
            spotify_id="sp-club",
            artists=("Cannonball Adderley",),
            upc=corpus.barcode("mercy_live_at_the_club"),
        ),
        artists=("Cannonball Adderley",),
    )
    quintet_result = resolve_track(
        as_quintet, _lookup(corpus), now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS
    )
    assert quintet_result.step == "track:non-studio"
    assert quintet_result.release_group == club

    best_of = corpus.groups["14c51130-3679-307d-a1ab-3a1de2ee3a94"]  # Best of Cannonball Adderley
    assert best_of.artist_mbid == phenix.artist_mbid, "the other copy maps under the plain credit"
    as_adderley = track_intent(
        "Mercy, Mercy, Mercy",
        spotify_album(best_of.title, spotify_id="sp-best-of", artists=("Cannonball Adderley",), upc=None),
        artists=("Cannonball Adderley",),
    )
    lookup = _lookup(corpus, searches={("Cannonball Adderley", best_of.title): best_of.mbid})

    adderley_result = resolve_track(as_adderley, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS)
    assert adderley_result.step == "track:title->album"
    assert adderley_result.release_group == phenix
    assert adderley_result.release_group != quintet_result.release_group, "one song, two albums"


def test_denying_phenix_sends_that_copy_back_to_the_live_album(corpus: Corpus) -> None:
    """The deny list is the only tool for this: nothing in the data marks Phenix as the odd one."""
    phenix = corpus.rg("cannonball_phenix")
    best_of = corpus.groups["14c51130-3679-307d-a1ab-3a1de2ee3a94"]
    intent = track_intent(
        "Mercy, Mercy, Mercy",
        spotify_album(best_of.title, spotify_id="sp-best-of", artists=("Cannonball Adderley",), upc=None),
        artists=("Cannonball Adderley",),
    )
    lookup = _lookup(corpus, searches={("Cannonball Adderley", best_of.title): best_of.mbid})

    before = resolve_track(intent, lookup, now=NOW, pending_since=None, fallback_days=FALLBACK_DAYS)
    assert before.release_group == phenix

    after = resolve_track(
        intent,
        lookup,
        now=NOW,
        pending_since=None,
        fallback_days=FALLBACK_DAYS,
        rules=ExclusionRules(deny_releases=frozenset({phenix.mbid})),
    )
    assert after.status == ResolutionStatus.RESOLVED
    assert after.step == "track:non-studio", "it falls through to the compilation Spotify named"
    assert after.release_group == best_of
