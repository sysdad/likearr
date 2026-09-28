"""Tests for CompositeLookup: MusicBrainz first, Lidarr's metadata as the fallback."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from likearr.adapters.lookup import CompositeLookup
from likearr.config import LidarrConfig
from likearr.models import ArtistRelation, BarcodeMatch, IsrcRecording, PrimaryType, ReleaseGroup, SecondaryType
from likearr.ports import CatalogueTooLarge, LidarrMetadataError, MetadataError, MetadataLookup

from .conftest import FAKE_API_KEY, LIDARR_URL

MB_GROUP = ReleaseGroup(
    mbid="00000000-0000-4000-8000-000000000001",
    title="Fake Album",
    artist_mbid="00000000-0000-4000-8000-0000000000a1",
    artist_name="Fake Band",
    primary_type=PrimaryType.ALBUM,
)
LIDARR_GROUP = ReleaseGroup(
    mbid="00000000-0000-4000-8000-000000000002",
    title="Fake Album",
    artist_mbid="00000000-0000-4000-8000-0000000000a1",
    artist_name="Fake Band",
    primary_type=PrimaryType.ALBUM,
)


class FakeMusicBrainz:
    """A MetadataLookup that returns or raises whatever the test asked for, and counts calls."""

    def __init__(
        self,
        *,
        result: ReleaseGroup | None = MB_GROUP,
        error: Exception | None = None,
        candidates: Sequence[ReleaseGroup] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.candidates = candidates
        """What the name search finds, when a test needs more than one; defaults to `result`."""
        self.calls: list[str] = []
        self.cache_hits = 0
        """Unset by every other test; only `test_mb_cache_hits_and_live_calls_are_read_off_the_primary`
        writes to it, to check `CompositeLookup.mb_cache_hits` reads it back."""
        self.live_calls = 0
        """Same as `cache_hits`, for `CompositeLookup.mb_live_calls`."""

    def _answer(self, what: str) -> ReleaseGroup | None:
        self.calls.append(what)
        if self.error is not None:
            raise self.error
        return self.result

    def release_groups_by_barcode(self, upc: str) -> Sequence[BarcodeMatch]:
        found = self._answer(f"barcode:{upc}")
        return (BarcodeMatch(release_group=found, official=True),) if found else ()

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        found = self._answer(f"isrc:{isrc}")
        return (found,) if found else ()

    def recordings_for_isrc(self, isrc: str) -> Sequence[IsrcRecording]:
        found = self._answer(f"recordings:{isrc}")
        return (IsrcRecording(title="One", release_groups=(found,)),) if found else ()

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        return self._answer(f"search:{artist}/{title}")

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        found = self._answer(f"search:{artist}/{title}")
        if self.candidates is not None:
            return tuple(self.candidates)
        return (found,) if found else ()

    def search_artist(self, name: str) -> tuple[str, str] | None:
        found = self._answer(f"artist:{name}")
        return (found.artist_mbid, found.artist_name) if found else None

    def search_artist_candidates(self, name: str) -> Sequence[tuple[str, str]]:
        found = self._answer(f"artists:{name}")
        return ((found.artist_mbid, found.artist_name),) if found else ()

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        found = self._answer(f"artist-rgs:{artist_mbid}")
        return (found,) if found else ()

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        self._answer(f"tracks:{rg_mbid}")
        return ("One", "Two")


class FakeLidarr:
    """Only the two LidarrPort methods CompositeLookup uses; everything else would be dead weight."""

    def __init__(self, *, result: ReleaseGroup | None = LIDARR_GROUP, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.candidates: Sequence[ReleaseGroup] | None = None
        """What the name search finds, when a test needs more than one; defaults to `result`."""
        self.calls: list[str] = []

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        self.calls.append(f"lookup:{rg_mbid}")
        if self.error is not None:
            raise self.error
        return self.result

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        self.calls.append(f"search:{artist}/{title}")
        if self.error is not None:
            raise self.error
        return self.result

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        found = self.search_release_group(artist, title)
        if self.candidates is not None:
            return tuple(self.candidates)
        return (found,) if found else ()


def make(mb: FakeMusicBrainz, lidarr: FakeLidarr) -> CompositeLookup:
    return CompositeLookup(mb, lidarr)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- happy path


def test_musicbrainz_wins_and_lidarr_is_never_called() -> None:
    mb, lidarr = FakeMusicBrainz(), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.search_release_group("Fake Band", "Fake Album") is MB_GROUP
    assert lidarr.calls == []
    assert composite.mb_ok
    assert composite.lidarr_metadata_ok


def test_lidarr_fills_in_when_musicbrainz_found_nothing() -> None:
    mb, lidarr = FakeMusicBrainz(result=None), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.search_release_group("Fake Band", "Fake Album") is LIDARR_GROUP
    assert lidarr.calls == ["search:Fake Band/Fake Album"]
    assert composite.mb_ok  # "not found" is an answer, not a failure


def test_lidarr_fills_in_when_musicbrainz_failed() -> None:
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    lidarr = FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.search_release_group("Fake Band", "Fake Album") is LIDARR_GROUP
    assert not composite.mb_ok
    assert composite.lidarr_metadata_ok


def test_both_backends_failing_raises_a_metadata_error() -> None:
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make(mb, lidarr)

    with pytest.raises(MetadataError, match="both MusicBrainz and Lidarr"):
        composite.search_release_group("Fake Band", "Fake Album")
    assert not composite.mb_ok
    assert not composite.lidarr_metadata_ok


def test_musicbrainz_failing_and_lidarr_finding_nothing_is_not_an_error() -> None:
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    lidarr = FakeLidarr(result=None)
    composite = make(mb, lidarr)

    assert composite.search_release_group("Fake Band", "Fake Album") is None
    assert not composite.mb_ok
    assert composite.lidarr_metadata_ok


def test_same_name_artists_from_musicbrainz_pass_through_and_lidarr_is_never_asked() -> None:
    """Two candidates is MusicBrainz *answering*, not missing: Lidarr's own name search
    would only pick one of the same two artists by name, which is the guess being refused."""
    other = ReleaseGroup(
        mbid="00000000-0000-4000-8000-000000000003",
        title="Fake Album",
        artist_mbid="00000000-0000-4000-8000-0000000000a2",
        artist_name="Fake Band",
        primary_type=PrimaryType.ALBUM,
    )
    mb, lidarr = FakeMusicBrainz(candidates=(MB_GROUP, other)), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.search_release_group_candidates("Fake Band", "Fake Album") == (MB_GROUP, other)
    assert composite.search_release_group("Fake Band", "Fake Album") is None, "two artists is doubt"
    assert lidarr.calls == []


def test_lidarr_fills_in_the_candidates_when_musicbrainz_found_nothing() -> None:
    mb, lidarr = FakeMusicBrainz(result=None), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.search_release_group_candidates("Fake Band", "Fake Album") == (LIDARR_GROUP,)


def test_lidarr_outage_on_a_plain_miss_is_recorded_not_raised() -> None:
    mb = FakeMusicBrainz(result=None)
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make(mb, lidarr)

    assert composite.search_release_group("Fake Band", "Fake Album") is None
    assert composite.mb_ok
    assert not composite.lidarr_metadata_ok


# ---------------------------------------------------------------------------- lookup by mbid


def test_lookup_release_group_delegates_to_lidarr() -> None:
    mb, lidarr = FakeMusicBrainz(), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.lookup_release_group(MB_GROUP.mbid) is LIDARR_GROUP
    assert lidarr.calls == [f"lookup:{MB_GROUP.mbid}"]


def test_lookup_release_group_swallows_a_lidarr_outage() -> None:
    mb = FakeMusicBrainz()
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make(mb, lidarr)

    assert composite.lookup_release_group(MB_GROUP.mbid) is None
    assert not composite.lidarr_metadata_ok


# ---------------------------------------------------------------------------- MusicBrainz-only paths


def test_barcode_has_no_lidarr_equivalent() -> None:
    mb = FakeMusicBrainz(error=MetadataError("down"))
    lidarr = FakeLidarr()
    composite = make(mb, lidarr)

    with pytest.raises(MetadataError):
        composite.release_groups_by_barcode("0000000000001")
    assert lidarr.calls == []
    assert not composite.mb_ok


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.release_groups_for_isrc("XX0000000001"),
        lambda c: c.recordings_for_isrc("XX0000000001"),
        lambda c: c.search_artist("Fake Band"),
        lambda c: c.search_artist_candidates("Fake Band"),
        lambda c: c.artist_release_groups("00000000-0000-4000-8000-0000000000a1"),
        lambda c: c.release_group_track_titles("00000000-0000-4000-8000-000000000001"),
    ],
)
def test_musicbrainz_only_methods_propagate_and_mark_mb_not_ok(
    call: Callable[[CompositeLookup], object],
) -> None:
    composite = make(FakeMusicBrainz(error=MetadataError("down")), FakeLidarr())
    with pytest.raises(MetadataError):
        call(composite)
    assert not composite.mb_ok


def test_musicbrainz_only_methods_pass_results_through() -> None:
    composite = make(FakeMusicBrainz(), FakeLidarr())
    assert composite.release_groups_for_isrc("XX0000000001") == (MB_GROUP,)
    assert composite.recordings_for_isrc("XX0000000001") == (IsrcRecording(title="One", release_groups=(MB_GROUP,)),)
    assert composite.search_artist("Fake Band") == (MB_GROUP.artist_mbid, MB_GROUP.artist_name)
    assert composite.search_artist_candidates("Fake Band") == ((MB_GROUP.artist_mbid, MB_GROUP.artist_name),)
    assert composite.artist_release_groups(MB_GROUP.artist_mbid) == (MB_GROUP,)
    assert composite.release_group_track_titles(MB_GROUP.mbid) == ("One", "Two")
    barcode = composite.release_groups_by_barcode("0000000000001")
    assert barcode == (BarcodeMatch(release_group=MB_GROUP, official=True),)
    assert composite.mb_ok


# ---------------------------------------------------------------------------- port conformance


def test_composite_satisfies_metadata_lookup() -> None:
    """Statically checked by pyright: the resolver must be able to take this as a MetadataLookup."""
    port: MetadataLookup = make(FakeMusicBrainz(), FakeLidarr())
    assert callable(port.search_release_group)


# ---------------------------------------------------------------- catalogue too large is not an outage


def test_catalogue_too_large_propagates_but_leaves_musicbrainz_ok() -> None:
    """A catalogue past the browse ceiling is huge, not unreachable, and must not degrade the run.

    `CatalogueTooLarge` subclasses `MetadataError`, so without this carve-out the first followed
    artist over the ceiling would set `mb_ok` false on every run for ever - a permanent `degraded`,
    which is the bug this whole change exists to remove. The caller still gets the exception and
    still reports the artist; only the health flag is spared.
    """
    composite = make(FakeMusicBrainz(error=CatalogueTooLarge("more than 3000 release groups")), FakeLidarr())

    with pytest.raises(CatalogueTooLarge):
        composite.artist_release_groups(MB_GROUP.artist_mbid)
    assert composite.mb_ok
    assert composite.catalogue_too_large == (MB_GROUP.artist_mbid,)


def test_an_ordinary_metadata_error_on_the_same_call_still_marks_mb_not_ok() -> None:
    composite = make(FakeMusicBrainz(error=MetadataError("musicbrainz is down")), FakeLidarr())

    with pytest.raises(MetadataError):
        composite.artist_release_groups(MB_GROUP.artist_mbid)
    assert not composite.mb_ok
    assert composite.catalogue_too_large == ()


# ---------------------------------------------------------------- which lookup failed, not just that one did


def test_a_failed_search_records_the_term_that_failed() -> None:
    """Two terms 503 on every run for ever, so "a lookup failed" cannot be the alarm - which did.

    The identity is the term, so a chronically poisoned term is recognised next run and counted
    rather than reported as news, while a real outage produces a flood of terms nobody has seen.
    """
    mb = FakeMusicBrainz(result=None)
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    assert composite.search_release_group("Leopold Stokowski", "Rhapsody") is None
    assert composite.lidarr_metadata_failures == ("album-search:Leopold Stokowski|Rhapsody",)


def test_a_failed_lookup_by_mbid_records_the_release_group() -> None:
    composite = make(FakeMusicBrainz(), FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    assert composite.lookup_release_group(MB_GROUP.mbid) is None
    assert composite.lidarr_metadata_failures == (f"album-lookup:{MB_GROUP.mbid}",)


def test_the_same_term_failing_twice_is_one_identity() -> None:
    mb = FakeMusicBrainz(result=None)
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")
    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_failures == ("album-search:Leopold Stokowski|Rhapsody",)


def test_a_double_failure_records_the_term_too() -> None:
    """Both backends down still names what was being asked for."""
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    with pytest.raises(MetadataError):
        composite.search_release_group("Fake Band", "Fake Album")
    assert composite.lidarr_metadata_failures == ("album-search:Fake Band|Fake Album",)


def test_a_healthy_run_records_nothing() -> None:
    composite = make(FakeMusicBrainz(), FakeLidarr())

    assert composite.search_release_group("Fake Band", "Fake Album") is MB_GROUP
    assert composite.lidarr_metadata_failures == ()


def test_mb_cache_hits_and_live_calls_are_read_off_the_primary() -> None:
    """The shell's progress line reads these off `ctx.composite`, the same way it
    already reads `mb_stale_served`. `FakeMusicBrainz` carries neither counter by default, so the
    property must default to 0 rather than raising."""
    mb = FakeMusicBrainz()
    composite = make(mb, FakeLidarr())
    assert composite.mb_cache_hits == 0
    assert composite.mb_live_calls == 0

    mb.cache_hits = 7
    mb.live_calls = 3
    assert composite.mb_cache_hits == 7
    assert composite.mb_live_calls == 3


# ---------------------------------------------------------------- negative-caching a term


class _FailThenSucceedLidarr:
    """A search always 503s; a lookup by mbid always succeeds - two distinct calls, one run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        self.calls.append(f"lookup:{rg_mbid}")
        return LIDARR_GROUP

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        self.calls.append(f"search:{artist}/{title}")
        raise LidarrMetadataError("skyhook is down")

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        found = self.search_release_group(artist, title)
        return (found,) if found else ()


_IDENTITY = "album-search:Leopold Stokowski|Rhapsody"
_NOW = datetime(2026, 9, 21, tzinfo=UTC)


def make_cached(mb: FakeMusicBrainz, lidarr: FakeLidarr, *, cached_at: datetime) -> CompositeLookup:
    return CompositeLookup(mb, lidarr, negative_cache={_IDENTITY: cached_at}, negative_cache_days=7, now=lambda: _NOW)  # type: ignore[arg-type]


def test_a_cached_failure_is_not_re_requested() -> None:
    mb = FakeMusicBrainz(result=None)
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=1))

    assert composite.search_release_group("Leopold Stokowski", "Rhapsody") is None
    assert lidarr.calls == [], "still inside its 7-day TTL: Lidarr must not be asked again"
    assert composite.lidarr_metadata_attempts == 0


def test_a_cached_failure_still_keeps_its_health_identity() -> None:
    """Skipping the retry must not make the term look 'resolved' to `core.health`."""
    mb = FakeMusicBrainz(result=None)
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=1))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_failures == (_IDENTITY,)


def test_a_cache_hit_is_never_a_candidate_to_recache() -> None:
    mb = FakeMusicBrainz(result=None)
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=1))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_new_failures == ()


def test_an_expired_cache_entry_is_retried() -> None:
    mb = FakeMusicBrainz(result=None)
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=8))

    assert composite.search_release_group("Leopold Stokowski", "Rhapsody") is None
    assert lidarr.calls == ["search:Leopold Stokowski/Rhapsody"]
    assert composite.lidarr_metadata_attempts == 1


def test_a_genuine_failure_is_a_negative_cache_candidate() -> None:
    mb = FakeMusicBrainz(result=None)
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_new_failures == (_IDENTITY,)
    assert composite.lidarr_metadata_attempts == 1
    assert composite.lidarr_metadata_attempt_failures == 1


def test_a_second_failure_of_the_same_term_is_still_one_candidate() -> None:
    mb = FakeMusicBrainz(result=None)
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")
    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_new_failures == (_IDENTITY,)
    assert composite.lidarr_metadata_attempts == 2


def test_no_lidarr_success_means_the_cache_must_not_be_written() -> None:
    mb = FakeMusicBrainz(result=None)
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    composite.search_release_group("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_any_success is False


def test_a_successful_lidarr_lookup_makes_it_safe_to_write_the_cache() -> None:
    composite = make(FakeMusicBrainz(result=None), FakeLidarr())

    composite.search_release_group("Fake Band", "Fake Album")

    assert composite.lidarr_metadata_any_success is True


def test_a_different_lookup_succeeding_is_what_the_gate_checks() -> None:
    """The api.lidarr.audio-outage scenario: one term fails, but the run is not entirely blind."""
    mb = FakeMusicBrainz(result=None)
    lidarr = _FailThenSucceedLidarr()
    composite = make(mb, lidarr)  # type: ignore[arg-type]

    composite.search_release_group("Leopold Stokowski", "Rhapsody")
    composite.lookup_release_group(MB_GROUP.mbid)

    assert composite.lidarr_metadata_any_success is True
    assert composite.lidarr_metadata_new_failures == (_IDENTITY,)


# ---------------------------------------------------------------- same-named artists behind a fallback

LONDON = ReleaseGroup(
    mbid="3c5834e7-0000-4000-8000-000000000002",
    title="Jungle",
    artist_mbid="6bbb3983-ce8a-4971-96e0-7cae73268fc4",
    artist_name="Jungle",
    primary_type=PrimaryType.ALBUM,
)
US_1969 = ReleaseGroup(
    mbid="297a768d-0000-4000-8000-000000000001",
    title="Jungle",
    artist_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095",
    artist_name="Jungle",
    primary_type=PrimaryType.ALBUM,
)


def _two_jungles() -> FakeLidarr:
    lidarr = FakeLidarr(result=US_1969)
    lidarr.candidates = (US_1969, LONDON)
    return lidarr


def test_a_fallback_after_a_musicbrainz_error_passes_both_same_named_artists_on() -> None:
    """The first name match used to be the answer: a guess between two artists called "Jungle"."""
    composite = make(FakeMusicBrainz(error=MetadataError("musicbrainz is down")), _two_jungles())

    assert composite.search_release_group_candidates("Jungle", "Jungle") == (US_1969, LONDON)
    assert composite.search_release_group("Jungle", "Jungle") is None, "two artists is doubt"
    assert composite.provisional_release_groups == frozenset({US_1969.mbid, LONDON.mbid})


def test_a_fallback_after_a_plain_miss_passes_both_on_and_is_not_provisional() -> None:
    """MusicBrainz answered "nothing", which is not an outage: the answer is as good as any."""
    composite = make(FakeMusicBrainz(result=None), _two_jungles())

    assert composite.search_release_group_candidates("Jungle", "Jungle") == (US_1969, LONDON)
    assert composite.provisional_release_groups == frozenset()


def test_a_single_artist_fallback_is_unchanged_but_provisional_after_an_error() -> None:
    composite = make(FakeMusicBrainz(error=MetadataError("musicbrainz is down")), FakeLidarr())

    assert composite.search_release_group_candidates("Fake Band", "Fake Album") == (LIDARR_GROUP,)
    assert composite.search_release_group("Fake Band", "Fake Album") is LIDARR_GROUP
    assert composite.provisional_release_groups == frozenset({LIDARR_GROUP.mbid})


class _SearchDown(FakeMusicBrainz):
    """MusicBrainz whose name search is down but whose ISRC answer is still cached."""

    def __init__(self, isrc_hits: Sequence[ReleaseGroup]) -> None:
        super().__init__(result=None)
        self.isrc_hits = tuple(isrc_hits)

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        raise MetadataError("musicbrainz search is down")

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        return self.isrc_hits


def _busy_earnin(isrc: str | None):
    from tests.unit.fakes import spotify_album, track_intent

    return track_intent("Busy Earnin'", spotify_album("Jungle", artists=("Jungle",)), isrc=isrc, artists=("Jungle",))


def test_behind_a_fallback_the_isrc_still_decides_between_same_named_artists() -> None:
    from likearr.core.resolver import resolve_track
    from tests.unit.fakes import NOW

    composite = make(_SearchDown([LONDON]), _two_jungles())

    result = resolve_track(_busy_earnin("GBBKS1400112"), composite, now=NOW, pending_since=None, fallback_days=180)

    assert result.release_group == LONDON
    assert "ISRC GBBKS1400112" in result.detail


def test_behind_a_fallback_with_nothing_to_decide_it_is_ambiguous_never_a_guess() -> None:
    from likearr.core.resolver import AMBIGUOUS_SAME_NAME_STEP, resolve_track
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import NOW

    composite = make(_SearchDown([]), _two_jungles())

    result = resolve_track(_busy_earnin(None), composite, now=NOW, pending_since=None, fallback_days=180)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP


def test_behind_a_fallback_a_saved_album_by_two_same_named_artists_is_ambiguous() -> None:
    """The likeliest way into an unmatched intent: a saved album carries no ISRC, and its
    barcode already missed, so two artists behind the fallback are never chosen between."""
    from likearr.core.resolver import AMBIGUOUS_SAME_NAME_STEP, resolve_album
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import album_intent, spotify_album

    composite = make(_SearchDown([]), _two_jungles())

    result = resolve_album(album_intent(spotify_album("Jungle", artists=("Jungle",))), composite)

    assert result.status is ResolutionStatus.UNMAPPED
    assert result.step == AMBIGUOUS_SAME_NAME_STEP
    assert US_1969.mbid in result.detail and LONDON.mbid in result.detail, "both candidates are named"


def test_the_ambiguity_detail_does_not_say_musicbrainz_when_lidarr_found_the_artists() -> None:
    """The candidates here came from Lidarr's own search, not MusicBrainz's."""
    from likearr.core.resolver import resolve_album
    from tests.unit.fakes import album_intent, spotify_album

    composite = make(_SearchDown([]), _two_jungles())

    result = resolve_album(album_intent(spotify_album("Jungle", artists=("Jungle",))), composite)

    assert result.detail.startswith("2 different artists named 'Jungle' each have a release titled 'Jungle'")
    assert "MusicBrainz artists" not in result.detail


# ---------------------------------------------------------------- what an MB failure reaches


class _LinksDown(FakeMusicBrainz):
    """MusicBrainz that is down for the outward links too."""

    def spotify_artist_id(self, artist_mbid: str) -> str | None:
        raise MetadataError("musicbrainz is down")


def test_every_musicbrainz_failure_is_counted_including_the_ones_answered_another_way() -> None:
    mb = _LinksDown(error=MetadataError("musicbrainz is down"))
    composite = make(mb, FakeLidarr())

    assert composite.mb_failure_count == 0
    assert composite.search_release_group("Fake Band", "Fake Album") is LIDARR_GROUP, "answered by Lidarr"
    assert composite.spotify_artist_id("some-artist") is None, "answered as 'no link'"
    with pytest.raises(MetadataError):
        composite.release_groups_for_isrc("GBAAA0000001")

    assert composite.mb_failure_count == 3


def test_a_musicbrainz_answer_moves_no_failure_count() -> None:
    composite = make(FakeMusicBrainz(result=None), FakeLidarr())

    composite.search_release_group("Fake Band", "Fake Album")
    composite.release_groups_for_isrc("GBAAA0000001")

    assert composite.mb_failure_count == 0, "a plain miss is an answer"


def test_after_the_error_the_isrc_stand_in_answer_is_provisional_not_only_lidarrs() -> None:
    """MusicBrainz's name search errors, Lidarr finds nothing, and the track's
    ISRC - still answered from MusicBrainz's cache - names the release. It resolves, but its
    answer rests on no Lidarr release group, so the fallback check alone would have cached it."""
    from likearr.core.resolver import resolve_all
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import NOW, snapshot

    composite = make(_SearchDown([LONDON]), FakeLidarr(result=None))
    intent = _busy_earnin("GBBKS1400112")

    result = resolve_all(
        snapshot(tracks=[intent]),
        composite,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=180,
        lookup_failures=lambda: composite.mb_failure_count,
    )

    resolution = result.resolutions[intent.reason.key]
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.release_group == LONDON
    assert "ISRC GBBKS1400112" in resolution.detail, "the ISRC stand-in found it"
    assert composite.provisional_release_groups == frozenset(), "no Lidarr release group was involved"
    assert result.provisional == {intent.reason.key}


def test_after_the_error_a_negative_cached_term_is_not_asked_of_lidarr_again() -> None:
    """The fallback after an error skips a term Lidarr is known to 503 on, like a miss does.
    With MusicBrainz failed too, that is both backends failing."""
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    lidarr = FakeLidarr(error=LidarrMetadataError("skyhook is down"))
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=1))

    with pytest.raises(MetadataError, match="both MusicBrainz and Lidarr"):
        composite.search_release_group_candidates("Leopold Stokowski", "Rhapsody")

    assert lidarr.calls == [], "still inside its 7-day TTL: Lidarr must not be asked again"
    assert composite.lidarr_metadata_attempts == 0
    assert composite.lidarr_metadata_failures == (_IDENTITY,), "it keeps its health identity"
    assert composite.lidarr_metadata_new_failures == (), "nothing was asked, so nothing to re-cache"


def test_after_the_error_an_expired_cache_entry_is_retried() -> None:
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    lidarr = FakeLidarr()
    composite = make_cached(mb, lidarr, cached_at=_NOW - timedelta(days=8))

    assert composite.search_release_group_candidates("Leopold Stokowski", "Rhapsody") == (LIDARR_GROUP,)
    assert lidarr.calls == ["search:Leopold Stokowski/Rhapsody"]


def test_after_the_error_a_genuine_lidarr_failure_is_counted_and_is_a_cache_candidate() -> None:
    """It used to be recorded by name only - no attempt, no attempt failure, no candidate - so
    the outage rule could not see it and the term was re-asked every run."""
    mb = FakeMusicBrainz(error=MetadataError("musicbrainz is down"))
    composite = make(mb, FakeLidarr(error=LidarrMetadataError("skyhook is down")))

    with pytest.raises(MetadataError):
        composite.search_release_group_candidates("Leopold Stokowski", "Rhapsody")

    assert composite.lidarr_metadata_attempts == 1
    assert composite.lidarr_metadata_attempt_failures == 1
    assert composite.lidarr_metadata_new_failures == (_IDENTITY,)
    assert composite.lidarr_metadata_failures == (_IDENTITY,)
    assert not composite.lidarr_metadata_ok


def test_after_the_error_a_lidarr_answer_is_an_attempt_and_a_success() -> None:
    composite = make(FakeMusicBrainz(error=MetadataError("musicbrainz is down")), FakeLidarr())

    composite.search_release_group_candidates("Fake Band", "Fake Album")

    assert composite.lidarr_metadata_attempts == 1
    assert composite.lidarr_metadata_attempt_failures == 0
    assert composite.lidarr_metadata_any_success is True


# ---------------------------------------------------------------- the relationship join

TRY = ReleaseGroup(
    mbid="00000000-0000-4000-8000-000000000014",
    title="Try!",
    artist_mbid="00000000-0000-4000-8000-0000000000b1",
    artist_name="John Mayer Trio",
    primary_type=PrimaryType.ALBUM,
    secondary_types=frozenset({SecondaryType.LIVE}),
)
MAYER = ArtistRelation(
    relationship="member of band",
    direction="backward",
    artist_mbid="00000000-0000-4000-8000-0000000000b2",
    artist_name="John Mayer",
)


class _Joined(_SearchDown):
    """MusicBrainz whose name search is down, but which still answers the relationship questions."""

    def __init__(self, *, relations_error: Exception | None = None) -> None:
        super().__init__([])
        self.relations_error = relations_error

    def release_groups_under_other_credits(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        self.calls.append(f"other-credits:{artist}/{title}")
        return (TRY,)

    def artist_relations(self, artist_mbid: str) -> Sequence[ArtistRelation]:
        self.calls.append(f"relations:{artist_mbid}")
        if self.relations_error is not None:
            raise self.relations_error
        return (MAYER,)

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        self.calls.append(f"tracks:{rg_mbid}")
        return ("Who Did You Think I Was", "Gravity")


def test_the_relationship_questions_are_passed_to_musicbrainz() -> None:
    mb, lidarr = _Joined(), FakeLidarr()
    composite = make(mb, lidarr)

    assert composite.release_groups_under_other_credits("John Mayer", "Try!") == (TRY,)
    assert composite.artist_relations(TRY.artist_mbid) == (MAYER,)
    assert lidarr.calls == [], "Lidarr holds no artist relationships"
    assert composite.mb_failure_count == 0


def test_a_failed_relationship_lookup_is_counted_and_is_unknown_not_none() -> None:
    """A failure costs the intent the rule, never a wrong artist - and moves the failure count. It is
    ``None``, "could not be read", not ``()``: the resolver must not read it as "no relationship"."""
    composite = make(_Joined(relations_error=MetadataError("musicbrainz is down")), FakeLidarr())

    assert composite.artist_relations(TRY.artist_mbid) is None
    assert composite.mb_failure_count == 1
    assert not composite.mb_ok


def test_a_failed_other_credit_search_is_counted_and_answers_nothing() -> None:
    class _Down(_Joined):
        def release_groups_under_other_credits(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
            raise MetadataError("musicbrainz is down")

    composite = make(_Down(), FakeLidarr())

    assert composite.release_groups_under_other_credits("John Mayer", "Try!") == ()
    assert composite.mb_failure_count == 1


def test_a_relationship_answer_reached_after_a_musicbrainz_error_is_provisional() -> None:
    """On this path the name search errored, so whatever this intent reaches - here
    *Try!* through the relationship - is acted on this run and not cached."""
    from likearr.core.resolver import resolve_all
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import NOW, snapshot, spotify_album, track_intent

    composite = make(_Joined(), FakeLidarr(result=None))
    intent = track_intent(
        "Gravity", spotify_album("TRY! - Live In Concert", artists=("John Mayer",)), artists=("John Mayer",)
    )

    result = resolve_all(
        snapshot(tracks=[intent]),
        composite,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=180,
        lookup_failures=lambda: composite.mb_failure_count,
        relations=composite,
    )

    resolution = result.resolutions[intent.reason.key]
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.release_group == TRY
    assert result.provisional == {intent.reason.key}


def test_a_failed_relationship_lookup_leaves_the_intent_unmapped_not_errored() -> None:
    from likearr.core.resolver import resolve_all
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import NOW, snapshot, spotify_album, track_intent

    mb = _Joined(relations_error=MetadataError("musicbrainz is down"))
    mb.search_release_group_candidates = lambda artist, title: ()  # type: ignore[method-assign]
    composite = make(mb, FakeLidarr(result=None))
    intent = track_intent(
        "Gravity", spotify_album("TRY! - Live In Concert", artists=("John Mayer",)), artists=("John Mayer",)
    )

    result = resolve_all(
        snapshot(tracks=[intent]),
        composite,
        now=NOW,
        cache={},
        pending_since={},
        fallback_days=180,
        lookup_failures=lambda: composite.mb_failure_count,
        relations=composite,
    )

    resolution = result.resolutions[intent.reason.key]
    assert resolution.status is ResolutionStatus.UNMAPPED
    assert resolution.step == "track:album:search"
    assert result.metadata_errors == 0
    assert result.provisional == {intent.reason.key}


# ---------------------------------------------------------------- non-Latin names behind the fallback


@respx.mock
def test_behind_a_fallback_a_non_latin_stranger_with_the_same_title_stays_unmapped(
    lidarr_config: LidarrConfig, client: httpx.Client
) -> None:
    """End to end through the real Lidarr adapter. MusicBrainz finds nothing, and Lidarr's
    search returns a same-titled album by a different artist whose name is also non-Latin. The old
    ASCII fold read both names as "" and resolved the saved album to that stranger."""
    from likearr.adapters.lidarr import LidarrClient
    from likearr.core.resolver import resolve_album
    from likearr.models import ResolutionStatus
    from tests.unit.fakes import album_intent, spotify_album

    title, wanted, stranger = "夜明けの歌", "青い鳥", "赤い月"
    lookup_route = respx.get(f"{LIDARR_URL}/api/v1/album/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "foreignAlbumId": "00000000-0000-4000-8000-0000000000f5",
                    "title": title,
                    "albumType": "Album",
                    "artist": {"foreignArtistId": "00000000-0000-4000-8000-0000000000e5", "artistName": stranger},
                }
            ],
        )
    )
    lidarr = LidarrClient(lidarr_config, client, api_key=FAKE_API_KEY)
    composite = CompositeLookup(FakeMusicBrainz(result=None), lidarr)

    result = resolve_album(album_intent(spotify_album(title, artists=(wanted,))), composite)

    assert lookup_route.called, "the fallback was taken"
    assert result.status is ResolutionStatus.UNMAPPED
    assert result.release_group is None
    assert composite.provisional_release_groups == frozenset()
    writes = [c.request for c in respx.calls if c.request.method != "GET"]
    assert writes == [], "nothing was written to Lidarr"
