"""MusicBrainz first, Lidarr's own metadata as a first-class fallback.

MusicBrainz is the authority: it is the only source with the release-group structure the
resolver reasons about. But it goes down, and Lidarr already carries a cached view of the same
catalogue through ``api.lidarr.audio``. :class:`CompositeLookup` uses the second when the first
has nothing to say, so a MusicBrainz outage degrades resolution instead of stopping it.

Only the two lookups Lidarr can actually answer are delegated:
:meth:`release_groups_by_barcode` (via ``lidarr:<mbid>``, see below) and the name search
(:meth:`search_release_group_candidates`, and :meth:`search_release_group` over it). ISRC
lookups, artist browse and tracklists have no Lidarr equivalent, so those failures propagate.

**Negative-caching a term Lidarr's metadata server always 503s on** (issue #18). A handful of
search terms - a mangled artist/title pair SkyHook chokes on - fail server-side on every run for
ever, and re-asking four times a day is pure waste. `_lidarr_search` / `_lidarr_lookup` skip a
term still inside its cache TTL rather than calling Lidarr again, but the *decision to write* a
new negative entry is made by the caller (`likearr.shell.plan.plan`) at the end of the run, not
here: only when at least one *other* Lidarr metadata lookup succeeded this run, so an
``api.lidarr.audio`` outage cannot poison the cache for a week. This class only tracks what would
be written (`lidarr_metadata_new_failures`) and whether writing is safe
(`lidarr_metadata_any_success`); it never touches the state database itself. The same cache and
the same counters apply to the fallback after a MusicBrainz *error* as after a plain miss (#53):
an outage is exactly when every term is asked of Lidarr, so it is no time to re-ask the ones known
to fail.

**What was reached after a MusicBrainz failure** (issue #53). `mb_failure_count` counts every
MusicBrainz failure this class saw, including the ones it answered some other way - Lidarr's
name search, "no link". `core.resolver.resolve_all` reads it around each intent, and the shell
does not cache an answer reached while it moved: it is this run's best answer, but MusicBrainz,
the authority, never finished answering the question.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from likearr.models import ArtistCandidate, ArtistRelation, BarcodeMatch, IsrcRecording, ReleaseGroup
from likearr.ports import CatalogueTooLarge, LidarrMetadataError, LidarrPort, MetadataError, MetadataLookup

__all__ = ["CompositeLookup"]


class CompositeLookup:
    """A :class:`likearr.ports.MetadataLookup` that falls back from MusicBrainz to Lidarr."""

    def __init__(
        self,
        primary: MetadataLookup,
        lidarr: LidarrPort,
        *,
        negative_cache: Mapping[str, datetime] | None = None,
        negative_cache_days: float = 7.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """
        Args:
            negative_cache: identity -> when it was last cached as a server-side Lidarr metadata
                failure (``SqliteState.lidarr_negative_cache()``). A snapshot taken once, at the
                start of the run - nothing else writes to this table while the run is in progress.
            negative_cache_days: how long a cached failure is trusted before it is retried
                (``[musicbrainz] negative_cache_days``, reused rather than a new knob).
            now: injected clock, for tests; defaults to the wall clock.
        """
        self._primary = primary
        self._lidarr = lidarr
        self.mb_ok = True
        """False once MusicBrainz has failed a lookup this run - feeds ``HealthRecord.mb_ok``."""
        self.mb_failure_count = 0
        """MusicBrainz lookups that failed this run, whether the failure was raised or answered some
        other way. Only ever grows; `resolve_all` compares it before and after each intent (#53)."""
        self.lidarr_metadata_ok = True
        """False once Lidarr's metadata proxy has failed - feeds ``HealthRecord.lidarr_metadata_ok``."""
        self._catalogue_too_large: list[str] = []
        self._lidarr_metadata_failures: list[str] = []
        self._negative_cache = negative_cache or {}
        self._negative_cache_days = negative_cache_days
        self._now = now
        self.lidarr_metadata_attempts = 0
        """Lidarr metadata lookups actually asked of Lidarr this run. Excludes a term skipped
        because it is still inside its negative-cache TTL - it was not asked, so it cannot count
        toward an outage (``core.health.lidarr_metadata_outage``) or toward `mass poisoning`."""
        self.lidarr_metadata_attempt_failures = 0
        """Of `lidarr_metadata_attempts`, how many failed. Same exclusion as above."""
        self._lidarr_new_failures: list[str] = []
        self._lidarr_metadata_any_success = False
        self._provisional: set[str] = set()

    # ---------------------------------------------------------------- delegated with fallback

    def release_groups_by_barcode(self, upc: str) -> Sequence[BarcodeMatch]:
        """MusicBrainz by barcode; Lidarr has no barcode search, so it cannot help here.

        A barcode is only ever resolvable by MusicBrainz, so the fallback is a no-op and this
        method exists to record the failure rather than to recover from it.
        """
        try:
            return self._primary.release_groups_by_barcode(upc)
        except MetadataError:
            self._mb_failed()
            raise

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        """The earliest of `search_release_group_candidates`; ``None`` for none, or for candidates
        by two different artists."""
        found = self.search_release_group_candidates(artist, title)
        return found[0] if found and len({g.artist_mbid for g in found}) == 1 else None

    def search_release_group_candidates(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """Name search: MusicBrainz first, then Lidarr's own ``album/lookup``.

        Lidarr is asked only when MusicBrainz failed or found nothing. Several MusicBrainz
        candidates - different artists sharing the name and the title (issue #32) - are an answer,
        not a miss, and are passed through as they are: Lidarr's search matches on the same names
        and would only pick one of the same artists without any better reason to.

        Lidarr's answer is its candidates too - one per artist whose name and title match - not
        its first hit (issue #42): the first of two same-named artists' albums is the same guess
        #32 took out of the MusicBrainz path, and the resolver treats Lidarr's candidates exactly
        as it treats MusicBrainz's, so the track's ISRC decides between them or nothing does.

        A release group this fallback returns after MusicBrainz *errored* is recorded in
        `provisional_release_groups`, so the shell can use it this run without caching it as
        settled: it was never checked against MusicBrainz, and the next run should ask again.
        The error also moves `mb_failure_count`, which keeps *any* answer this intent reaches
        out of the cache, not only one resting on these release groups (#53).

        After an error, Lidarr is asked through the same negative cache and counters as after a
        miss (#53): a term still inside its TTL is not asked again, and counts as Lidarr failing.

        Raises:
            MetadataError: only when *both* backends failed, a negative-cached Lidarr term
                included. If Lidarr answered but found nothing, that is a real "not found" and
                ``()`` is returned - the resolver reports it as unmapped, which never costs a
                monitored release.
        """
        try:
            found = tuple(self._primary.search_release_group_candidates(artist, title))
        except MetadataError as exc:
            self._mb_failed()
            try:
                fallback = self._lidarr_metadata_attempt(
                    f"album-search:{artist}|{title}",
                    lambda: tuple(self._lidarr.search_release_group_candidates(artist, title)),
                )
            except LidarrMetadataError as lidarr_exc:
                raise MetadataError(
                    f"both MusicBrainz and Lidarr metadata failed for {artist!r} / {title!r}: "
                    f"{exc}; lidarr: {lidarr_exc}"
                ) from exc
            self._provisional.update(rg.mbid for rg in fallback)
            return fallback
        if found:
            return found
        return self._lidarr_search(artist, title)

    @property
    def provisional_release_groups(self) -> frozenset[str]:
        """Release groups this run found only through Lidarr, after MusicBrainz *failed* (#42).

        Good enough to act on this run - it is Lidarr's own catalogue - but not to cache as a
        settled resolution: MusicBrainz, the authority, never saw the question. A plain
        MusicBrainz miss followed by a Lidarr hit is not here; that answer is as good as any.
        """
        return frozenset(self._provisional)

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        """Resolve a release group by mbid through Lidarr, for callers that already have one.

        Not part of :class:`likearr.ports.MetadataLookup`; the shell uses it to confirm that a
        release group MusicBrainz produced is one Lidarr can actually add.
        """
        return self._lidarr_lookup(rg_mbid)

    @property
    def lidarr_metadata_failures(self) -> tuple[str, ...]:
        """Lidarr metadata lookups that failed this run, by what was asked for, in first-seen order.

        Includes a term skipped because its negative-cache entry is still fresh: it keeps its
        identity here even though Lidarr was not asked again, so `core.health` sees the same
        chronic `lidarr_metadata` identity every run rather than "resolved" then "new" each time
        the entry expires and fails again (issue #18).
        """
        return tuple(self._lidarr_metadata_failures)

    @property
    def lidarr_metadata_new_failures(self) -> tuple[str, ...]:
        """Identities that failed a genuine Lidarr metadata attempt this run (issue #18).

        The candidates for `SqliteState.record_lidarr_negative_cache` - never a term that was
        already cached and simply skipped, since nothing was asked of Lidarr for it this run.
        """
        return tuple(self._lidarr_new_failures)

    @property
    def lidarr_metadata_any_success(self) -> bool:
        """Whether at least one Lidarr metadata call attempted this run got a real answer.

        The gate the caller checks before writing anything to the negative cache: without it, an
        `api.lidarr.audio` outage would cache every term it touched as unanswerable for
        `negative_cache_days`, which is exactly the false positive issue #18 exists to avoid.
        """
        return self._lidarr_metadata_any_success

    @property
    def catalogue_too_large(self) -> tuple[str, ...]:
        """Artists whose catalogue is past MusicBrainz's browse ceiling, in first-seen order."""
        return tuple(self._catalogue_too_large)

    @property
    def mb_stale_served(self) -> int:
        """MusicBrainz lookups this run that failed and were answered from an expired cache entry.

        Nothing was lost - that is the adapter's standing rule - so this does **not** touch
        `mb_ok`. It is reported in `HealthRecord.mb_errors` because a run that quietly served
        stale answers to half the library is something an operator wants to see, and before
        positive entries had a max age it could not happen at all.
        """
        return int(getattr(self._primary, "stale_served", 0))

    @property
    def mb_cache_hits(self) -> int:
        """MusicBrainz queries this run answered from a fresh cache entry (issue #119)."""
        return int(getattr(self._primary, "cache_hits", 0))

    @property
    def mb_live_calls(self) -> int:
        """MusicBrainz queries this run that actually went out over the network (issue #119)."""
        return int(getattr(self._primary, "live_calls", 0))

    # ---------------------------------------------------------------- MusicBrainz only

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        """ISRC -> release groups. Lidarr has no equivalent, so a failure propagates."""
        try:
            return self._primary.release_groups_for_isrc(isrc)
        except MetadataError:
            self._mb_failed()
            raise

    def recordings_for_isrc(self, isrc: str) -> Sequence[IsrcRecording]:
        """ISRC -> recordings with their titles and release groups. MusicBrainz only, as above."""
        try:
            return self._primary.recordings_for_isrc(isrc)
        except MetadataError:
            self._mb_failed()
            raise

    def search_artist(self, name: str) -> tuple[str, str] | None:
        try:
            return self._primary.search_artist(name)
        except MetadataError:
            self._mb_failed()
            raise

    def search_artist_candidates(self, name: str) -> Sequence[tuple[str, str]]:
        try:
            return self._primary.search_artist_candidates(name)
        except MetadataError:
            self._mb_failed()
            raise

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        """Browse an artist's catalogue. A catalogue too large to browse is not an outage.

        `CatalogueTooLarge` means "this artist has more releases than we are willing to page
        through", which is a permanent property of that artist, not a MusicBrainz failure. Left in
        `mb_ok` it would latch the flag false on every run for ever, and a health signal that is
        always on is the same as one that is off. It is recorded by artist instead, so the shell
        can report it once as a new, named, actionable condition.
        """
        try:
            return self._primary.artist_release_groups(artist_mbid)
        except CatalogueTooLarge:
            if artist_mbid not in self._catalogue_too_large:
                self._catalogue_too_large.append(artist_mbid)
            raise
        except MetadataError:
            self._mb_failed()
            raise

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        try:
            return self._primary.release_group_track_titles(rg_mbid)
        except MetadataError:
            self._mb_failed()
            raise

    # ---------------------------------------------------------------- outward links

    # `promote-save`'s mapping tiers (:class:`likearr.ports.ReleaseLinkLookup`). Lidarr can answer
    # none of them - it holds neither streaming links nor barcodes - so there is nothing to fall
    # back to. A MusicBrainz failure is recorded and answered with "nothing known", which costs
    # the caller a tier and can never produce a wrong match.

    def spotify_artist_id(self, artist_mbid: str) -> str | None:
        """Tier 1 for an artist: MusicBrainz's own Spotify URL relationship."""
        return self._link("spotify_artist_id", artist_mbid, None)

    def spotify_album_id(self, rg_mbid: str) -> str | None:
        """Tier 1 for an album: MusicBrainz's own Spotify URL relationship."""
        return self._link("spotify_album_id", rg_mbid, None)

    def release_group_barcodes(self, rg_mbid: str) -> Sequence[str]:
        """Tier 2's input: barcodes to search Spotify by when there is no relationship."""
        return self._link("release_group_barcodes", rg_mbid, ())

    def artists_for_spotify_artist(self, spotify_artist_id: str) -> Sequence[ArtistCandidate]:
        """The authoritative artist mapping: which MusicBrainz artist(s) that Spotify page is.

        Lidarr has nothing equivalent. A failure degrades to "no link", which costs the resolver
        its best tier and sends it to the name search. That search resolves only an exact-name
        match nothing else shares, so it refuses rather than guesses between namesakes, but it
        cannot tell an unlinked artist from the only MusicBrainz artist of that name.
        """
        return self._link("artists_for_spotify_artist", spotify_artist_id, ())

    def artist_disambiguation(self, artist_mbid: str) -> str:
        """MusicBrainz's one-liner telling two same-named artists apart, for the run summary.

        Lidarr has no equivalent, and a missing disambiguation is a perfectly normal answer, so a
        failure degrades to ``""`` rather than propagating: a name collision is reported either
        way, just with less to go on.
        """
        return self._link("artist_disambiguation", artist_mbid, "")

    # :class:`likearr.ports.CreditRelations` (issue #14). MusicBrainz only, like the outward links:
    # Lidarr's catalogue holds no artist relationships, and its name search already had its turn
    # through `search_release_group_candidates`. A failure is recorded (#53) and never answered as
    # "nothing": the other-credit search as no candidates, the relationships as ``None``, "could
    # not be read", on which the resolver chooses nothing. The intent stays UNMAPPED as it was.

    def release_groups_under_other_credits(self, artist: str, title: str) -> Sequence[ReleaseGroup]:
        """The name search's title matches under another credit, for the relationship join."""
        getter = getattr(self._primary, "release_groups_under_other_credits", None)
        if getter is None:  # pragma: no cover - only a lookup that predates the method
            return ()
        try:
            return tuple(getter(artist, title))
        except MetadataError:
            self._mb_failed()
            return ()

    def artist_relations(self, artist_mbid: str) -> Sequence[ArtistRelation] | None:
        """MusicBrainz's artist-artist relationships for one artist; ``None`` when it cannot say.

        Not ``()``: an unreadable artist might be a second joined one, and answering "no
        relationships" would let the other be chosen as if it were the only one.
        """
        return self._link("artist_relations", artist_mbid, None)

    def _link[T](self, name: str, mbid: str, missing: T) -> T:
        """Ask the primary lookup one outward question, answering `missing` when it cannot."""
        getter = getattr(self._primary, name, None)
        if getter is None:  # pragma: no cover - only a lookup that predates these methods
            return missing
        try:
            return getter(mbid)
        except MetadataError:
            self._mb_failed()
            return missing

    # ---------------------------------------------------------------- Lidarr side

    def _lidarr_search(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        identity = f"album-search:{artist}|{title}"
        return self._lidarr_metadata_call(
            identity, lambda: tuple(self._lidarr.search_release_group_candidates(artist, title)), ()
        )

    def _lidarr_lookup(self, rg_mbid: str) -> ReleaseGroup | None:
        identity = f"album-lookup:{rg_mbid}"
        return self._lidarr_metadata_call(identity, lambda: self._lidarr.lookup_release_group(rg_mbid), None)

    def _lidarr_metadata_call[T](self, identity: str, call: Callable[[], T], missing: T) -> T:
        """`_lidarr_metadata_attempt`, answering `missing` where it raises."""
        try:
            return self._lidarr_metadata_attempt(identity, call)
        except LidarrMetadataError:
            return missing

    def _lidarr_metadata_attempt[T](self, identity: str, call: Callable[[], T]) -> T:
        """One Lidarr metadata call, skipped when `identity` is still inside its negative-cache TTL.

        A cache hit is not an *attempt*: `lidarr_metadata_attempts` and `_any_success` are
        untouched, and the identity is never added to `lidarr_metadata_new_failures` - nothing was
        asked of Lidarr this run, so there is nothing new to (re)cache. It still joins
        `lidarr_metadata_failures`, so the term keeps the same health identity every run.

        Raises:
            LidarrMetadataError: Lidarr failed, or `identity` is a cached failure and was not asked.
        """
        if self._cached_failure(identity):
            self._record_lidarr_failure(identity)
            raise LidarrMetadataError(
                f"{identity} failed on Lidarr's metadata server within the last "
                f"{self._negative_cache_days:g} day(s); not asked again this run"
            )
        self.lidarr_metadata_attempts += 1
        try:
            result = call()
        except LidarrMetadataError:
            self.lidarr_metadata_attempt_failures += 1
            self._record_lidarr_failure(identity)
            if identity not in self._lidarr_new_failures:
                self._lidarr_new_failures.append(identity)
            raise
        self._lidarr_metadata_any_success = True
        return result

    def _cached_failure(self, identity: str) -> bool:
        """True when `identity` failed within the last `negative_cache_days` (issue #18)."""
        cached_at = self._negative_cache.get(identity)
        if cached_at is None:
            return False
        age_days = (self._now() - cached_at).total_seconds() / 86400.0
        return age_days < self._negative_cache_days

    def _mb_failed(self) -> None:
        """Flag the run, and count the failure for the intent being resolved (#53)."""
        self.mb_ok = False
        self.mb_failure_count += 1

    def _record_lidarr_failure(self, identity: str) -> None:
        """Flag the run and remember *what* failed, deduplicated.

        The boolean alone cannot distinguish "SkyHook is down" from "these two search terms have
        returned 503 on every run for weeks", so wherever such terms exist it is false every run and
        useless as a health signal. The identity can.
        """
        self.lidarr_metadata_ok = False
        if identity not in self._lidarr_metadata_failures:
            self._lidarr_metadata_failures.append(identity)
