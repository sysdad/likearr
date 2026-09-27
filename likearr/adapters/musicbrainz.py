"""MusicBrainz metadata adapter.

Implements :class:`likearr.ports.MetadataLookup` against the MusicBrainz web service (``fmt=json``).

Three behaviours matter more than the queries themselves:

- **Rate limiting.** One shared token bucket across every call (``min_interval_s``, default 1.0)
  and a User-Agent carrying a contact address, per MusicBrainz's policy. 503 is retried with
  backoff by the shared HTTP layer.
- **Persistent cache.** Positive answers expire after ``max_age_days`` (the shell passes
  ``[musicbrainz] positive_cache_days``, 90 by default), deterministically jittered per key so the
  whole cache does not fall due on one run; negative answers expire after ``negative_cache_days``
  so a not-yet-added release is picked up later.
- **Failure keeps the last known mapping.** If a lookup fails but the cache has an answer, the
  cached answer is returned and ``errors`` is incremented. A transient MusicBrainz outage must
  never look like "the user un-liked this", which is what an empty result would imply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from likearr import __version__
from likearr.adapters.http import HttpError, request_with_retries
from likearr.config import MusicBrainzConfig
from likearr.core.match import spotify_id_from_url
from likearr.core.normalize import credits_match, fold_title, normalize_title, strip_release_qualifiers
from likearr.models import (
    VARIOUS_ARTISTS_MBID,
    ArtistCandidate,
    ArtistRelation,
    BarcodeMatch,
    IsrcRecording,
    PrimaryType,
    ReleaseGroup,
    SecondaryType,
)
from likearr.ports import CatalogueTooLarge, MetadataError

__all__ = [
    "POSITIVE_TTL_JITTER",
    "CachedDisambiguations",
    "MusicBrainzLookup",
    "RateLimiter",
    "build_user_agent",
    "jittered_max_age",
]

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mb_cache (
    key        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    negative   INTEGER NOT NULL DEFAULT 0
)
"""

_BROWSE_PAGE = 100
_MAX_CATALOGUE_PAGES = 30
"""3,000 release groups: far beyond any real artist's catalogue, far short of a runaway crawl."""
"""MusicBrainz's maximum for a browse request."""

_TRACKLIST_RELEASE_LIMIT = 5
"""How many releases of a group to fetch when looking for a representative tracklist.

A browse request cannot sort by status, so fetching exactly one release would make "prefer
Official" impossible. Five is enough to find an Official release in practice while keeping the
``inc=recordings`` payload small.
"""

_SEARCH_LIMIT = 5

_RELEASE_PAGE = 100
"""Releases of a group to read links and barcodes from - MusicBrainz's maximum for one browse.

Not a handful, unlike `_TRACKLIST_RELEASE_LIMIT`: any release may be the one carrying the Spotify
link, a browse cannot sort, and one page of 100 covers every real release group.
"""

_ARTIST_BROWSE_TTL_DAYS = 0.0
"""An artist's release-group list is cached but always refetched when MusicBrainz is reachable.

Every other query answers a question whose answer changes rarely (which release group carries this
barcode?), so its positive entry keeps the instance's ``max_age_days``. This one answers "what has
this artist put out?" - the question a followed artist exists to ask - so a stale hit would hide
exactly the new release the run is looking for. TTL 0 keeps the entry on disk as an outage
fallback while never serving it to a healthy run.
"""

type _FieldSpec = Mapping[str, _FieldSpec | None]
"""Which keys of a cached body to keep: ``None`` keeps a value whole, a nested spec trims it too.

A nested spec applies to a dict value directly and to each element of a list value, so
``{"recordings": {"title": None}}`` keeps ``recordings[].title``. See `_project`.
"""

_ISRC_SEARCH_FIELDS: _FieldSpec = {
    "recordings": {
        "title": None,
        "isrcs": None,
        "artist-credit": {"name": None},
        "releases": {"release-group": {"id": None}},
    }
}
"""What an ``isrc-search:`` row keeps (issue #123), and who reads each field:

- ``recordings[].isrcs``: `MusicBrainzLookup.release_groups_for_isrc` (skips a fuzzy hit for
  another ISRC) and `scripts/replay_resolver.py` ``_isrc_index`` (the same filter).
- ``recordings[].releases[].release-group.id``: both of them, for the release groups to fetch or
  index.
- ``recordings[].title``: `MusicBrainzLookup.recordings_for_isrc` (issue #163), and the replay's
  ``by_title`` index.
- ``recordings[].artist-credit[].name``: the replay only, for that index (it reads the first
  credit's name).

A hit song's recording can sit on hundreds of releases, each stored in full, so an untrimmed row
reaches 800 KB; trimmed it is a list of release-group ids. A parser that starts reading any other
field must add it here first, or it reads nothing from a cached row -
``tests/adapters/test_mb_cache_trim.py`` fails until it does.
"""

_RG_TRACKS_FIELDS: _FieldSpec = {"releases": {"status": None, "media": {"tracks": {"title": None}}}}
"""What an ``rg-tracks:`` row keeps (issue #123): ``releases[].status`` (to prefer an Official
release) and ``releases[].media[].tracks[].title``, which is all
`MusicBrainzLookup.release_group_track_titles` and `_track_titles` read."""


def _project(value: Any, spec: _FieldSpec) -> Any:
    """`value` with only the keys `spec` names, recursively; see `_FieldSpec`.

    Anything that is not the shape the spec expects is kept as it is rather than dropped, so a
    parser meets exactly what it met in the full body and treats it the same way. Keys keep their
    order.
    """
    if isinstance(value, list):
        return [_project(item, spec) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in spec:
                sub = spec[key]
                out[key] = item if sub is None else _project(item, sub)
        return out
    return value


POSITIVE_TTL_JITTER = 0.25
"""How far past its nominal age a positive entry may live, as a fraction of that age.

Entries written on the same day - on a first run that is all of them - would otherwise all fall
due on the same run, which at 1 request/second is hours of refetching in one go and a plausible
way to earn a MusicBrainz ban. Spreading the 90-day default over 90-112 days turns that cliff into
a trickle. Deterministic per key, so an entry's expiry does not move about between runs and a
re-plan repeats no calls.
"""


def jittered_max_age(max_age_days: float, key: str) -> float:
    """This key's effective maximum age: `max_age_days` plus up to `POSITIVE_TTL_JITTER` of it.

    Derived from a hash of the key rather than from `random`, so it is stable across runs,
    processes and machines: the same entry always expires at the same moment.
    """
    if max_age_days <= 0:
        return max_age_days
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return max_age_days * (1.0 + POSITIVE_TTL_JITTER * fraction)


def build_user_agent(contact: str) -> str:
    """MusicBrainz requires ``app/version ( contact )``; an anonymous UA gets blocked."""
    return f"likearr/{__version__} ( {contact} )"


# ---------------------------------------------------------------------------- rate limiting


class RateLimiter:
    """Minimum-interval limiter shared by every MusicBrainz call.

    ``monotonic`` and ``sleep`` are injected so tests can assert on the timing with a fake clock
    instead of actually waiting.
    """

    __slots__ = ("_last", "_min_interval", "_monotonic", "_sleep")

    def __init__(
        self,
        min_interval_s: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, min_interval_s)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None

    def acquire(self) -> None:
        """Block until at least ``min_interval_s`` has passed since the previous acquire."""
        now = self._monotonic()
        if self._last is not None:
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._monotonic()
        self._last = now


# ---------------------------------------------------------------------------- normalisation

_PAREN_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def _normalize(value: str) -> str:
    """A deliberately simple local normaliser for conservative equality checks, and cache keys.

    Drops every parenthetical wholesale, then folds exactly as the core does (`fold_title`): case,
    diacritics, "&" as "and", and every run of characters that are not letters or digits turned
    into one space - MusicBrainz's U+2010 hyphen included, so "All<U+2010>American" matches
    Spotify's "All-American". Letters outside plain Latin are kept, in any script, so "MØ" is not
    "M" (issue #166).
    """
    return fold_title(_PAREN_RE.sub(" ", value))


def _titles_match_exact(a: str, b: str) -> bool:
    """The first two of `_titles_match`'s three passes - no qualifier stripping on either side.

    1. `_normalize` equality, kept verbatim for backward compatibility: it deletes *any*
       parenthetical wholesale, qualifier or not, which is what makes ``"Fake Album"`` match
       ``"Fake Album (Deluxe Reissue 2020)"`` even though "Deluxe Reissue 2020" is not in
       :mod:`likearr.core.normalize`'s qualifier vocabulary (a trailing year defeats it).
    2. Core :func:`~likearr.core.normalize.normalize_title` equality on the **raw** strings. This
       is word-preserving where `_normalize` is not: ``"Elf (Music from the Major Motion
       Picture)"`` (Spotify) and ``"Elf: Music From the Major Motion Picture"`` (MusicBrainz)
       fold to the same words once punctuation is turned to spaces, even though neither side
       strips anything.

    Split out from the qualifier-stripped pass so `search_release_group` can prefer a candidate
    that matches here over one that only matches after stripping - see its docstring for why.
    """
    if _normalize(a) == _normalize(b):
        return True
    return normalize_title(a) == normalize_title(b)


def _titles_match_stripped(a: str, b: str) -> bool:
    """`_titles_match`'s third pass: `normalize_title` after `strip_release_qualifiers` on
    **both** sides - never one side only, which is the bug this exists to fix. MusicBrainz
    stores ``"Kangaroo EP"`` where Spotify's own title is the bare ``"Kangaroo"``; stripping
    only Spotify's (already bare) side can never close that gap.

    Additive over `_titles_match_exact`: it can only accept a pairing that pass refused, never a
    wrong one, because `search_release_group` still requires the artist credit to match on top of
    this - see its docstring - and only falls back to this pass when no candidate matches exactly.
    """
    return normalize_title(strip_release_qualifiers(a)) == normalize_title(strip_release_qualifiers(b))


def _credits_match(a: str, b: str) -> bool:
    """True when two artist credits name the same artist, for `search_release_group`'s gate.

    `normalize_name` (core) rather than `_normalize` (this module): it drops a leading ``the``,
    which `_normalize` does not, so ``"The Branford Marsalis Quartet"`` now matches Spotify's
    bare ``"Branford Marsalis Quartet"``. Each side also has a bare ``feat.``/``ft.``/
    ``featuring`` credit dropped first (:func:`~likearr.core.normalize.strip_bare_featuring`),
    the same vocabulary :func:`~likearr.core.normalize.normalize_title` uses for a track's
    featured credit, so Spotify's ``"The Marty Paich Quartet featuring Art Pepper"`` matches
    MusicBrainz's plain ``"The Marty Paich Quartet"``.

    Still full equality, never containment: ``"John Mayer"`` still does not match ``"John Mayer
    Trio"``, which is the whole reason this stays a fold rather than a loosening - see
    `search_release_group`'s docstring. The rule itself is core's
    :func:`~likearr.core.normalize.credits_match`, shared with the resolver's relationship join.
    """
    return credits_match(a, b)


def _title_tier(groups: Iterable[ReleaseGroup], title: str) -> tuple[ReleaseGroup, ...]:
    """The candidates whose title matches `title`: the exact tier (`_titles_match_exact`) when it
    has any, otherwise the qualifier-stripped one (`_titles_match_stripped`); deduplicated and in
    `_release_group_tie_break` order. See `MusicBrainzLookup.search_release_group_candidates`."""
    pool = list(groups)
    tier = [g for g in pool if _titles_match_exact(g.title, title)] or [
        g for g in pool if _titles_match_stripped(g.title, title)
    ]
    return tuple(sorted(_dedupe(tier), key=_release_group_tie_break))


def _release_group_tie_break(group: ReleaseGroup) -> tuple[bool, date, str]:
    """Deterministic order for a candidate list: earliest first-release date (undated last), then
    lowest MBID. The name search only *orders* by it; `core.resolver` chooses."""
    return (group.first_release_date is None, group.first_release_date or date.min, group.mbid)


# ---------------------------------------------------------------------------- MB -> models


def _parse_partial_date(value: object) -> date | None:
    """MusicBrainz dates are 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'; coarse values pad to the first."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return date(year, month, day)
    except ValueError:
        return None


def _primary_type(value: object) -> PrimaryType | None:
    if not isinstance(value, str):
        return None
    try:
        return PrimaryType(value)
    except ValueError:
        return None


def _secondary_types(values: object) -> frozenset[SecondaryType]:
    if not isinstance(values, list):
        return frozenset()
    out: set[SecondaryType] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        try:
            out.add(SecondaryType(raw))
        except ValueError:
            continue  # an unknown secondary type is ignored, not an error
    return frozenset(out)


def _artist_credit(*candidates: object) -> tuple[str, str]:
    """First credited artist as ``(mbid, name)`` from whichever candidate carries a credit."""
    for candidate in candidates:
        if not isinstance(candidate, list) or not candidate:
            continue
        first = candidate[0]
        if not isinstance(first, Mapping):
            continue
        artist = first.get("artist")
        if isinstance(artist, Mapping) and artist.get("id"):
            return str(artist["id"]), str(artist.get("name") or first.get("name") or "")
    return "", ""


_FEATURING_JOINS = frozenset({"feat", "ft", "featuring"})
"""Join phrases, trimmed and lowercased, that introduce a featured guest rather than a main artist
(issue #164). "with" is deliberately not one: in MusicBrainz credits it joins co-billed performers
as often as guests ("Stan Getz With Arthur Fiedler", "Elvis Presley with the Royal Philharmonic
Orchestra"), so it stays a main credit."""


def _is_featuring(joinphrase: object) -> bool:
    """True for a join phrase such as " feat. ", " ft. " or " Featuring ", case and the spaces and
    punctuation around it aside."""
    return str(joinphrase or "").strip(" \t.,;:()[]").lower() in _FEATURING_JOINS


def _main_artist_mbids(*candidates: object) -> tuple[str, ...]:
    """The main credited artists' MBIDs, in order, from the same candidate `_artist_credit` reads.

    A credit's ``joinphrase`` joins it to the next one, so everything after the first featuring
    join phrase is a guest and left out: "Jon Batiste feat. JID, NewJeans & Camilo" is Jon Batiste
    alone, and "Jean Sibelius; London Philharmonic Orchestra, Paavo Berglund" is all three.
    """
    for candidate in candidates:
        if not isinstance(candidate, list) or not candidate:
            continue
        first = candidate[0]
        artist = first.get("artist") if isinstance(first, Mapping) else None
        if not isinstance(artist, Mapping) or not artist.get("id"):
            continue
        out: list[str] = []
        for entry in candidate:
            credited = entry.get("artist") if isinstance(entry, Mapping) else None
            mbid = str(credited.get("id") or "") if isinstance(credited, Mapping) else ""
            if mbid and mbid not in out:
                out.append(mbid)
            if isinstance(entry, Mapping) and _is_featuring(entry.get("joinphrase")):
                break
        return tuple(out)
    return ()


def _release_group(raw: Mapping[str, Any]) -> ReleaseGroup | None:
    """A release group from its own JSON, credited by its own ``artist-credit`` and nothing else
    (issue #268: a release's credit is not its release group's)."""
    mbid = raw.get("id")
    if not mbid:
        return None
    artist_mbid, artist_name = _artist_credit(raw.get("artist-credit"))
    return ReleaseGroup(
        main_artist_mbids=_main_artist_mbids(raw.get("artist-credit")),
        mbid=str(mbid),
        title=str(raw.get("title") or ""),
        artist_mbid=artist_mbid,
        artist_name=artist_name,
        primary_type=_primary_type(raw.get("primary-type")),
        secondary_types=_secondary_types(raw.get("secondary-types")),
        first_release_date=_parse_partial_date(raw.get("first-release-date")),
    )


def _gtin(value: object) -> str:
    """A barcode as a GTIN: its leading zeros dropped, since they carry no meaning (issue #150).

    Spotify sends 13- and 14-digit forms, MusicBrainz stores 12-digit UPC-As and 13-digit EANs,
    so both sides are stripped rather than one side padded. All zeros is ``""``, which matches
    nothing.
    """
    return str(value or "").strip().lstrip("0")


def _is_official(release: Mapping[str, Any]) -> bool:
    return str(release.get("status") or "").lower() == "official"


def _is_digital(release: Mapping[str, Any]) -> bool:
    """True when every medium is Digital Media - the pressing streaming links belong to."""
    media = release.get("media")
    if not isinstance(media, list) or not media:
        return False
    formats = {str(m.get("format") or "") for m in media if isinstance(m, Mapping)}
    return formats == {"Digital Media"}


def _relationship_urls(entity: Mapping[str, Any]) -> list[str]:
    """Every ``relations[].url.resource`` on an ``inc=url-rels`` payload.

    The relationship *type* is deliberately not filtered on. MusicBrainz files Spotify links under
    ``free streaming`` today, but type names have been renamed before and a link is judged on
    where it points, not on what it is called - `spotify_id_from_url` is the actual gate.
    """
    relations = entity.get("relations")
    if not isinstance(relations, list):
        return []
    out: list[str] = []
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        url = relation.get("url")
        resource = url.get("resource") if isinstance(url, Mapping) else None
        if isinstance(resource, str) and resource:
            out.append(resource)
    return out


def _dedupe(groups: Iterable[ReleaseGroup]) -> list[ReleaseGroup]:
    seen: dict[str, ReleaseGroup] = {}
    for group in groups:
        seen.setdefault(group.mbid, group)
    return list(seen.values())


# ---------------------------------------------------------------------------- the adapter


class MusicBrainzLookup:
    """Cached, rate-limited MusicBrainz queries. Implements :class:`likearr.ports.MetadataLookup`."""

    def __init__(
        self,
        config: MusicBrainzConfig,
        client: httpx.Client,
        *,
        cache_path: Path | str,
        max_age_days: int | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            cache_path: SQLite file for ``mb_cache``. The shell passes the state DB path; this
                adapter opens its own connection (WAL, 5s busy timeout) so it never contends
                with the state adapter's transactions.
            max_age_days: expiry for positive entries, in days, jittered per key by up to
                `POSITIVE_TTL_JITTER`. ``None`` means "never expire", which is only for a caller
                that has its own reason to pin the cache: the shell always passes
                ``[musicbrainz] positive_cache_days``.
        """
        self._config = config
        self._client = client
        self._limiter = RateLimiter(config.min_interval_s, monotonic=monotonic, sleep=sleep)
        self._catalogue_memo: dict[str, tuple[ReleaseGroup, ...]] = {}
        self._too_large: dict[str, CatalogueTooLarge] = {}
        """Artists found over `_MAX_CATALOGUE_PAGES` this run, so the crawl is not repeated (#151)."""
        self._sleep = sleep
        self._now = now
        self._max_age_days = max_age_days
        self._user_agent = build_user_agent(config.contact)
        self.errors = 0
        """Number of lookups this run that fell back to a cached (or missing) answer."""
        self.stale_served = 0
        """Of those, the ones a cached answer covered, so nothing was actually lost.

        Published as part of `HealthRecord.mb_errors` and deliberately **not** part of `mb_ok`: an
        expired entry whose refetch failed keeps serving the last known mapping, which is the rule
        this adapter has always had. The count says MusicBrainz misbehaved; the boolean is reserved
        for a lookup that had no answer at all, which is when something is genuinely lost.
        """
        self.cache_hits = 0
        """Queries this run answered from a fresh cache entry, no request sent (issue #119)."""
        self.live_calls = 0
        """Queries this run that actually went out over the network, whatever the answer. Together
        with `cache_hits` this is what the shell's progress line and ETA are built from: the ratio
        of live calls to intents resolved so far is the best estimate of how much rate-limited work
        is left, and a query this adapter merely answered from cache costs the run nothing."""

        self._db = sqlite3.connect(str(cache_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(_SCHEMA)
        self._db.commit()

    @property
    def ok(self) -> bool:
        """False once any lookup this run has failed - feeds ``HealthRecord.mb_ok``."""
        return self.errors == 0

    def close(self) -> None:
        self._db.close()

    # ---------------------------------------------------------------- MetadataLookup

    def release_groups_by_barcode(self, upc: str) -> tuple[BarcodeMatch, ...]:
        """Every distinct release group holding a release that carries this barcode.

        A lucene barcode query is fuzzy, so only an exact barcode is trusted - compared as a GTIN
        (`_gtin`), because Spotify pads its UPC with leading zeros MusicBrainz does not store
        (issue #150). Release groups with an Official release come first. When they are several,
        choosing one needs the album's title and credit, which only the resolver has.

        Each match carries its release group's **own** credit, never the release's (issue #268):
        Lidarr files an album under its release group's artist, and a search result's embedded
        release group has no credit, so it is fetched (cached) as the ISRC path fetches it. Art
        Blakey's *At the Jazz Corner of the World* is the case: the digital release on Spotify's
        barcode is credited to Art Blakey, its release group only to Art Blakey & The Jazz
        Messengers. A release group that cannot be fetched is dropped, not credited from the
        release - a wrong artist is worse than no barcode answer, and the name search still runs.
        A failed fetch raises `MetadataError` like any other lookup here.
        """
        want = _gtin(upc)
        if not want:
            return ()
        payload = self._fetch(
            "release",
            {"query": f"barcode:{upc}", "inc": "release-groups artist-credits", "limit": _SEARCH_LIMIT},
            cache_key=f"barcode:{upc}",
            result_key="releases",
        )
        releases = payload.get("releases") if isinstance(payload, Mapping) else None
        if not isinstance(releases, list):
            return ()
        exact = [r for r in releases if isinstance(r, Mapping) and _gtin(r.get("barcode")) == want]
        exact.sort(key=lambda r: not _is_official(r))
        seen: set[str] = set()
        found: dict[str, BarcodeMatch] = {}
        for release in exact:
            raw = release.get("release-group")
            if not isinstance(raw, Mapping) or not raw.get("id") or str(raw["id"]) in seen:
                continue
            seen.add(str(raw["id"]))
            if _artist_credit(raw.get("artist-credit"))[0]:
                group = _release_group(raw)
            else:
                group = self.release_group_by_id(str(raw["id"]))
            if group is not None and group.mbid not in found:
                found[group.mbid] = BarcodeMatch(release_group=group, official=_is_official(release))
        return tuple(found.values())

    def release_groups_for_isrc(self, isrc: str) -> Sequence[ReleaseGroup]:
        """Every distinct release group containing a recording with this ISRC.

        The ``/isrc/<isrc>`` lookup rejects ``inc=release-groups`` (HTTP 400) and without it
        returns no release groups, so this uses a recording SEARCH (``query=isrc:<isrc>``), whose
        hits carry ``releases[].release-group`` ids, then fetches each distinct release group once
        (cached) for the secondary types and first-release date the singles rule needs.
        """
        return tuple(_dedupe(g for recording in self.recordings_for_isrc(isrc) for g in recording.release_groups))

    def recordings_for_isrc(self, isrc: str) -> tuple[IsrcRecording, ...]:
        """`release_groups_for_isrc`, kept per recording with the recording's title (issue #163).

        The same cached search and the same release-group fetches, so it costs nothing extra. A
        recording's title is already in the trimmed ``isrc-search:`` row (`_ISRC_SEARCH_FIELDS`).
        """
        payload = self._fetch(
            "recording",
            {"query": f"isrc:{isrc}", "limit": _SEARCH_LIMIT},
            cache_key=f"isrc-search:{isrc}",
            result_key="recordings",
            keep_fields=_ISRC_SEARCH_FIELDS,
        )
        recordings = payload.get("recordings") if isinstance(payload, Mapping) else None
        if not isinstance(recordings, list):
            return ()
        fetched: dict[str, ReleaseGroup | None] = {}
        out: list[IsrcRecording] = []
        for recording in recordings:
            if not isinstance(recording, Mapping):
                continue
            isrcs = recording.get("isrcs")
            if isinstance(isrcs, list) and isrcs and isrc not in isrcs:
                continue  # a fuzzy search hit for a different ISRC
            rg_ids: list[str] = []
            for release in recording.get("releases") or []:
                raw = release.get("release-group") if isinstance(release, Mapping) else None
                rg_id = raw.get("id") if isinstance(raw, Mapping) else None
                if isinstance(rg_id, str) and rg_id not in rg_ids:
                    rg_ids.append(rg_id)
            groups: list[ReleaseGroup] = []
            for rg_id in rg_ids:
                if rg_id not in fetched:
                    fetched[rg_id] = self.release_group_by_id(rg_id)
                group = fetched[rg_id]
                if group is not None:
                    groups.append(group)
            out.append(IsrcRecording(title=str(recording.get("title") or ""), release_groups=tuple(_dedupe(groups))))
        return tuple(out)

    def release_group_by_id(self, rg_mbid: str) -> ReleaseGroup | None:
        """One release group with its type, secondary types, date and artist credit (cached)."""
        payload = self._fetch(
            f"release-group/{rg_mbid}",
            {"inc": "artist-credits"},
            cache_key=f"rg:{rg_mbid}",
            result_key="id",
            not_found_status=(404,),
        )
        if not isinstance(payload, Mapping) or not payload.get("id"):
            return None
        return _release_group(payload)

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        """Name-based release-group lookup. Returns ``None`` unless both names match exactly.

        The earliest of `search_release_group_candidates`, or ``None`` when it has none - or when
        they span two different artists, because two artists sharing a name and a title is doubt,
        and this method's contract is to return ``None`` on doubt. The resolver does not use this:
        it chooses among the candidates itself, knowing what Spotify says the release is.
        """
        found = self.search_release_group_candidates(artist, title)
        return found[0] if found and len({g.artist_mbid for g in found}) == 1 else None

    def search_release_group_candidates(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        """Every release group that matches the name search exactly, for the caller to choose from.

        A quoted lucene phrase query is tried first; MusicBrainz matches it literally against the
        stored title, so a smart quote, an en-dash or a colon where Spotify wrote something else
        makes it return nothing at all. Only when that quoted query finds **no** candidates is an
        unquoted, field-scoped retry issued - never when it found candidates that simply failed
        the equality gate below, which stays exactly as strict either way.

        That gate - `_titles_match_exact` / `_titles_match_stripped` and `_credits_match` - is
        what makes loosening the query safe, and none of it must be loosened independently of the
        rest. Re-asking every "nothing found" term as free text turns up many tracks where an
        *unrelated* artist shares the album title at a perfect score (say, ``'Pellucid Varnish' -
        'Harbour Lights'`` where that title also names an album by Ondine Karsk; both artists are
        invented); every one of those is refused here on the credit, not the title, which is why
        the credit side of this gate must stay full equality and never containment.

        Selection among several credit-matching candidates is two-tier, not "first in
        MusicBrainz's result order": an exact title match (`_titles_match_exact`) always wins over
        one that only matches after stripping qualifiers from both sides
        (`_titles_match_stripped`), regardless of which one MusicBrainz ranked first. Without this
        a query for Spotify's "Kangaroo" could return MusicBrainz's own "Kangaroo" album *and* an
        unrelated "Kangaroo EP" in either order, and letting rank alone decide would sometimes
        prefer the EP - which the qualifier-stripped pass exists to *rescue* a match, not to
        outrank an exact one.

        **Every** candidate in the winning tier is returned, and the adapter chooses none of them.
        Choosing needs two things only the caller knows. One artist often has several releases by
        one title - Yellowcard's *Lights and Sounds* is an album (2006) and a single (2005) - and
        which of them Spotify meant depends on what Spotify says the release *is*: the earliest
        date, which this method used to pick by (#23), sent a saved album to its lead single.
        And two MusicBrainz artists can share a name and a title - "Jungle" by Jungle is both a
        London band's 2014 album and a US band's 1969 one - which only evidence tied to the track,
        its ISRC, can settle (issue #32). `core.resolver` does both. Returned in date/MBID order,
        which is only there to make the result deterministic.
        """
        groups = [g for g in self._searched_release_groups(artist, title) if _credits_match(g.artist_name, artist)]
        return _title_tier(groups, title)

    def release_groups_under_other_credits(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        """The release groups `search_release_group_candidates` refused on the credit alone.

        **Read from the cache only, never the network**, at any age: the entries that search left
        behind - the quoted one, then the unquoted one only when the quoted one holds no candidates,
        as the search itself asks them. The resolver asks this only after that search, so it sees
        exactly what the search saw, including a stale entry served through an outage, and it never
        adds a request of its own (not even a failing one during that outage). A search that was
        never answered leaves nothing here, which is "none".

        The same two-tier title gate as the search; only the credit comparison is inverted: these
        are the title matches whose first credited artist is **not** `artist` under
        `_credits_match`, and which carry an artist MBID to ask MusicBrainz about.

        This is not a loosening of the credit gate, and nothing here accepts anything. Most of what
        it returns is an unrelated artist who happens to share the title - the same-title matches in
        `search_release_group_candidates`'s docstring are exactly that - and `core.resolver` takes
        one only on a MusicBrainz relationship joining the two artists (issue #14).
        """
        groups = [
            g
            for g in self._searched_release_groups(artist, title, cached_only=True)
            if g.artist_mbid and not _credits_match(g.artist_name, artist)
        ]
        return _title_tier(groups, title)

    def _searched_release_groups(self, artist: str, title: str, *, cached_only: bool = False) -> list[ReleaseGroup]:
        """Every release group the name search returned, before any title or credit gate.

        The quoted phrase query first, then the unquoted one only when the quoted one returned no
        candidates at all - see `search_release_group_candidates`. `cached_only` answers both from
        ``mb_cache`` at any age and never asks MusicBrainz.
        """
        if not artist.strip() or not title.strip():
            return []
        candidates = self._release_group_search_candidates(artist, title, quoted=True, cached_only=cached_only)
        if not candidates:
            candidates = self._release_group_search_candidates(artist, title, quoted=False, cached_only=cached_only)
        groups: list[ReleaseGroup] = []
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            group = _release_group(raw)
            if group is not None:
                groups.append(group)
        return groups

    def _release_group_search_candidates(
        self, artist: str, title: str, *, quoted: bool, cached_only: bool = False
    ) -> list[Mapping[str, Any]]:
        """One release-group search, quoted (an exact lucene phrase) or unquoted (field-scoped terms)."""
        if quoted:
            query = f'releasegroup:"{_escape_lucene(title)}" AND artist:"{_escape_lucene(artist)}"'
            cache_key = f"rg-search:{_normalize(artist)}|{_normalize(title)}"
        else:
            query = f"releasegroup:({_escape_lucene(title)}) AND artist:({_escape_lucene(artist)})"
            cache_key = f"rg-search-free:{_normalize(artist)}|{_normalize(title)}"
        if cached_only:
            cached = self._cache_get(cache_key)
            payload: Mapping[str, Any] = cached.body if cached is not None else {}
        else:
            payload = self._fetch(
                "release-group",
                {"query": query, "limit": _SEARCH_LIMIT},
                cache_key=cache_key,
                result_key="release-groups",
            )
        candidates = payload.get("release-groups") if isinstance(payload, Mapping) else None
        return candidates if isinstance(candidates, list) else []

    def search_artist(self, name: str) -> tuple[str, str] | None:
        """Conservative name-based artist lookup. Prefers a perfect score, then exact name equality."""
        found = self.search_artist_candidates(name)
        return found[0] if found else None

    def search_artist_candidates(self, name: str) -> tuple[tuple[str, str], ...]:
        """Every artist whose name (or sort name) is exactly `name`, best MusicBrainz score first.

        The same cached search `search_artist` reads, so asking both costs one request. Several
        answers are namesakes - "Evangeline" is a Seattle band, a New Orleans artist and an L.A.
        singer - which only evidence about the track can tell apart (issue #152).
        """
        if not name.strip():
            return ()
        payload = self._fetch(
            "artist",
            {"query": f'artist:"{_escape_lucene(name)}"', "limit": _SEARCH_LIMIT},
            cache_key=f"artist-search:{_normalize(name)}",
            result_key="artists",
        )
        candidates = payload.get("artists") if isinstance(payload, Mapping) else None
        if not isinstance(candidates, list):
            return ()
        want = _normalize(name)
        matches = [
            c
            for c in candidates
            if isinstance(c, Mapping)
            and c.get("id")
            and (_normalize(str(c.get("name") or "")) == want or _normalize(str(c.get("sort-name") or "")) == want)
        ]
        matches.sort(key=lambda c: -_score(c))  # stable: MusicBrainz's order breaks a tied score
        found: dict[str, str] = {}
        for c in matches:
            found.setdefault(str(c["id"]), str(c.get("name") or ""))
        return tuple(found.items())

    def artist_release_groups(self, artist_mbid: str) -> Sequence[ReleaseGroup]:
        """Every release group credited to the artist, of any type, fully paginated.

        Two guards against a runaway crawl: Various Artists is refused outright (millions of
        release groups), and a catalogue longer than `_MAX_CATALOGUE_PAGES` pages raises
        `MetadataError` instead of paging on. Results are memoised for the life of this object
        (one run), so an artist with several liked singles is browsed once per run, while the
        on-disk TTL of 0 still means every new run sees newly released albums.
        """
        if artist_mbid == VARIOUS_ARTISTS_MBID:
            raise MetadataError("refusing to browse the Various Artists catalogue")
        memo = self._catalogue_memo.get(artist_mbid)
        if memo is not None:
            return memo
        too_large = self._too_large.get(artist_mbid)
        if too_large is not None:
            raise too_large  # remembered for the run: the 30-page crawl is paid once (#151)
        groups: list[ReleaseGroup] = []
        offset = 0
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_CATALOGUE_PAGES:
                error = CatalogueTooLarge(
                    f"artist {artist_mbid} has more than {_MAX_CATALOGUE_PAGES * _BROWSE_PAGE} release groups; "
                    "not browsing further"
                )
                self._too_large[artist_mbid] = error
                raise error
            payload = self._fetch(
                "release-group",
                {
                    "artist": artist_mbid,
                    "inc": "artist-credits",
                    "limit": _BROWSE_PAGE,
                    "offset": offset,
                },
                cache_key=f"artist-rgs:{artist_mbid}:{offset}",
                result_key="release-groups",
                ttl_days=_ARTIST_BROWSE_TTL_DAYS,
            )
            page = payload.get("release-groups") if isinstance(payload, Mapping) else None
            if not isinstance(page, list) or not page:
                break
            for raw in page:
                if isinstance(raw, Mapping):
                    group = _release_group(raw)
                    if group is not None:
                        groups.append(group)
            total = payload.get("release-group-count")
            offset += len(page)
            if not isinstance(total, int) or offset >= total:
                break
        result = tuple(_dedupe(groups))
        self._catalogue_memo[artist_mbid] = result
        return result

    def _release_group_releases(self, rg_mbid: str) -> list[Mapping[str, Any]]:
        """Every release of the group, with its URL relationships, best pressing first.

        One request answers both of `promote-save`'s MusicBrainz questions - "is there a Spotify
        link?" and "what barcodes could I search by?" - so mapping an album costs one MusicBrainz
        call rather than two, and both answers share one cache entry.

        ``limit=_RELEASE_PAGE`` rather than a handful, because a browse cannot sort and the one
        release carrying the Spotify link is the Digital Media pressing, which is rarely among the
        first few (on the OK Computer group it is 1 of 39). Deliberately a single page: a group
        with more than 100 releases is a reissue-heavy outlier, and paging it would cost more
        MusicBrainz calls than the answer is worth. Ordered Official-and-digital first, which is
        both the pressing most likely to be on Spotify and the barcode most likely to match there.
        """
        payload = self._fetch(
            "release",
            {"release-group": rg_mbid, "inc": "url-rels media", "limit": _RELEASE_PAGE},
            cache_key=f"rg-releases:{rg_mbid}",
            result_key="releases",
        )
        releases = payload.get("releases") if isinstance(payload, Mapping) else None
        if not isinstance(releases, list):
            return []
        return sorted(
            (r for r in releases if isinstance(r, Mapping)),
            key=lambda r: (str(r.get("status") or "").lower() != "official", not _is_digital(r)),
        )

    def spotify_album_id(self, rg_mbid: str) -> str | None:
        """The Spotify album id MusicBrainz links this release group to, if it names exactly one.

        Spotify album links live on *releases* - the Digital Media ones - and never on the release
        group itself. That was checked against the MusicBrainz API rather than assumed, and it follows
        from the style guideline that streaming links belong to digital releases. So the group's
        releases are what is read, and the release group is not fetched at all.

        A group whose releases name **several distinct** Spotify albums (regional catalogue
        duplicates, typically) returns ``None``. They are probably the same record, but "probably"
        is not the bar here, and the UPC and title tiers answer it perfectly well.
        """
        found: list[str] = []
        for release in self._release_group_releases(rg_mbid):
            for url in _relationship_urls(release):
                spotify_id = spotify_id_from_url(url, "album")
                if spotify_id and spotify_id not in found:
                    found.append(spotify_id)
        return found[0] if len(found) == 1 else None

    def artists_for_spotify_artist(self, spotify_artist_id: str) -> Sequence[ArtistCandidate]:
        """Which MusicBrainz artist(s) a Spotify artist page is linked to.

        ``GET /url?resource=https://open.spotify.com/artist/<id>&inc=artist-rels``, reading
        ``relations[].artist``. This is the authoritative direction: an editor linked *that*
        Spotify page to *that* artist, so it cannot confuse two artists who merely share a name -
        which a name search does, catastrophically (see docs/dev/DESIGN.md, "Resolving a followed
        artist"). Verified by hand: the Spotify "Lawrence" page links to the New York group and
        one bad eurobeat link, and not at all to the German DJ a name search had been picking.

        Returns every linked artist, in MusicBrainz's order, for the caller to choose between.
        Empty when MusicBrainz has no such URL or no artist relation on it - a plain "not found",
        not an error, so the caller falls back to the name search.
        """
        payload = self._fetch(
            "url",
            {"resource": f"https://open.spotify.com/artist/{spotify_artist_id}", "inc": "artist-rels"},
            cache_key=f"spotify-artist-url:{spotify_artist_id}",
            result_key="relations",
            not_found_status=(404,),
        )
        relations = payload.get("relations") if isinstance(payload, Mapping) else None
        if not isinstance(relations, list):
            return ()
        out: list[ArtistCandidate] = []
        seen: set[str] = set()
        for relation in relations:
            artist = relation.get("artist") if isinstance(relation, Mapping) else None
            if not isinstance(artist, Mapping):
                continue
            mbid = str(artist.get("id") or "")
            if not mbid or mbid in seen:
                continue
            seen.add(mbid)
            out.append(
                ArtistCandidate(
                    mbid=mbid,
                    name=str(artist.get("name") or ""),
                    disambiguation=str(artist.get("disambiguation") or "").strip(),
                )
            )
        return tuple(out)

    def artist_relations(self, artist_mbid: str) -> tuple[ArtistRelation, ...]:
        """Every artist-artist relationship MusicBrainz records for this artist, in its order.

        ``artist/<mbid>?inc=artist-rels``, cached under ``artist-rels:<mbid>`` - its own key, not
        `_artist`'s ``inc=url-rels`` entry, whose payload carries no artist relationships. An artist
        with none is a negative entry, so it expires after ``negative_cache_days`` and an editor
        adding the missing ``member of band`` (Sister Sparrow & The Dirty Birds has none today) is
        picked up without waiting out the positive TTL. A 404 is "none" too.

        Every type is returned as MusicBrainz names it; which ones count is `core.resolver`'s rule,
        not this adapter's.
        """
        payload = self._fetch(
            f"artist/{artist_mbid}",
            {"inc": "artist-rels"},
            cache_key=f"artist-rels:{artist_mbid}",
            result_key="relations",
            not_found_status=(404,),
        )
        relations = payload.get("relations") if isinstance(payload, Mapping) else None
        if not isinstance(relations, list):
            return ()
        out: list[ArtistRelation] = []
        for relation in relations:
            if not isinstance(relation, Mapping):
                continue
            artist = relation.get("artist")
            if not isinstance(artist, Mapping) or not artist.get("id"):
                continue
            out.append(
                ArtistRelation(
                    relationship=str(relation.get("type") or ""),
                    direction=str(relation.get("direction") or ""),
                    artist_mbid=str(artist["id"]),
                    artist_name=str(artist.get("name") or ""),
                )
            )
        return tuple(out)

    def artist_disambiguation(self, artist_mbid: str) -> str:
        """MusicBrainz's one-line disambiguation for an artist, or ``""`` when it has none.

        "Germany DJ & producer" versus "Clyde Lawrence and Gracie Lawrence" is what turns a
        name-collision report from cryptic into actionable. It rides on the **same** cached
        ``artist/<mbid>?inc=url-rels`` fetch `spotify_artist_id` uses - the disambiguation is a
        top-level field of that payload - so asking for it costs a request only the first time
        anything asks about this artist at all, and nothing thereafter.

        Plenty of artists have no disambiguation; that is an empty string, not an error.
        """
        payload = self._artist(artist_mbid)
        value = payload.get("disambiguation")
        return str(value).strip() if isinstance(value, str) else ""

    def _artist(self, artist_mbid: str) -> Mapping[str, Any]:
        """One artist lookup with its URL relationships, cached under ``artist-urls:<mbid>``."""
        return self._fetch(
            f"artist/{artist_mbid}",
            {"inc": "url-rels"},
            cache_key=f"artist-urls:{artist_mbid}",
            result_key="relations",
            not_found_status=(404,),
        )

    def spotify_artist_id(self, artist_mbid: str) -> str | None:
        """The Spotify artist id MusicBrainz links this artist to, if it names exactly one.

        ``artist/<mbid>?inc=url-rels``; the relationship MusicBrainz uses is ``free streaming``,
        but the type name is not trusted - what is trusted is a URL that parses to a Spotify
        *artist* id, which `spotify_id_from_url` decides.
        """
        payload = self._artist(artist_mbid)
        found: list[str] = []
        for url in _relationship_urls(payload):
            spotify_id = spotify_id_from_url(url, "artist")
            if spotify_id and spotify_id not in found:
                found.append(spotify_id)
        return found[0] if len(found) == 1 else None

    def release_group_barcodes(self, rg_mbid: str) -> Sequence[str]:
        """Distinct barcodes of the group's releases, best pressing first.

        The UPC search is `promote-save`'s second tier, used when MusicBrainz records no Spotify
        relationship for the album: a barcode identifies a release, a title only describes one.
        """
        out: list[str] = []
        for release in self._release_group_releases(rg_mbid):
            barcode = str(release.get("barcode") or "").strip()
            if barcode and barcode not in out:
                out.append(barcode)
        return tuple(out)

    def release_group_track_titles(self, rg_mbid: str) -> Sequence[str]:
        """Track titles of one representative release, preferring an Official one."""
        payload = self._fetch(
            "release",
            {"release-group": rg_mbid, "inc": "recordings", "limit": _TRACKLIST_RELEASE_LIMIT},
            cache_key=f"rg-tracks:{rg_mbid}",
            result_key="releases",
            keep_fields=_RG_TRACKS_FIELDS,
        )
        releases = payload.get("releases") if isinstance(payload, Mapping) else None
        if not isinstance(releases, list) or not releases:
            return ()
        ordered = sorted(
            (r for r in releases if isinstance(r, Mapping)),
            key=lambda r: (str(r.get("status") or "").lower() != "official",),
        )
        for release in ordered:
            titles = _track_titles(release)
            if titles:
                return tuple(titles)
        return ()

    # ---------------------------------------------------------------- transport + cache

    def _fetch(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        cache_key: str,
        result_key: str,
        ttl_days: float | None = None,
        not_found_status: tuple[int, ...] = (),
        keep_fields: _FieldSpec | None = None,
    ) -> Mapping[str, Any]:
        """Return the JSON body for one query, from cache when possible.

        Args:
            result_key: the response field holding the results. An empty (or absent) list is a
                "not found" and is cached as a negative entry, so it expires and is retried;
                a non-empty result is cached positively.
            ttl_days: how long a positive entry stays fresh for this query, in days.
                ``None`` (the default) defers to the instance's ``max_age_days``.
            not_found_status: statuses that mean "no such entity" rather than a failure.
            keep_fields: when given, the cached copy keeps only these fields (see `_project`);
                the caller still gets the whole body this time, and the trimmed one from cache
                afterwards, so the two must parse alike. The negative check reads the whole body,
                and the result key survives the trim even when empty.

        On failure with any cached value present (fresh or stale) the cached value is returned
        and ``errors`` is incremented: a MusicBrainz outage keeps the last known mapping instead
        of unmapping everything the resolver could not look up this run.
        """
        cached = self._cache_get(cache_key, ttl_days)
        if cached is not None and not cached.stale:
            self.cache_hits += 1
            return cached.body

        url = f"{self._config.base_url}/{path}"
        query = {**params, "fmt": "json"}
        self.live_calls += 1
        try:
            response = request_with_retries(
                self._client,
                "GET",
                url,
                params=query,
                headers={"User-Agent": self._user_agent},
                allow_status=not_found_status,
                sleep=self._sleep,
                before_attempt=self._limiter.acquire,
            )
            body: object = {} if response.status_code in not_found_status else response.json()
        except (HttpError, ValueError) as exc:
            self.errors += 1
            if cached is not None:
                self.stale_served += 1
                return cached.body
            raise MetadataError(f"musicbrainz {path}: {exc}") from exc

        if not isinstance(body, Mapping):
            self.errors += 1
            if cached is not None:
                self.stale_served += 1
                return cached.body
            raise MetadataError(f"musicbrainz {path}: expected a JSON object, got {type(body).__name__}")

        results = body.get(result_key)
        # A list result is empty when it has no items; a scalar result (a lookup's `id`) when absent.
        empty = not results if isinstance(results, list) else results in (None, "")
        stored = dict(body) if keep_fields is None else _project(dict(body), keep_fields)
        self._cache_put(cache_key, stored, negative=empty)
        return body

    def _cache_get(self, key: str, ttl_days: float | None = None) -> _CacheEntry | None:
        max_age = self._max_age_days if ttl_days is None else ttl_days
        row = self._db.execute("SELECT body, fetched_at, negative FROM mb_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        body_text, fetched_at, negative = row
        try:
            body = json.loads(body_text)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        age_days = max(0.0, (self._now() - float(fetched_at)) / 86400.0)
        if negative:
            # Jittered by key like a positive entry (#166): rows written in one run would otherwise
            # fall due together, be refetched together, and stay in step every week after.
            stale = age_days >= jittered_max_age(self._config.negative_cache_days, key)
        else:
            stale = max_age is not None and age_days >= jittered_max_age(max_age, key)
        return _CacheEntry(body=body, stale=stale)

    def _cache_put(self, key: str, body: Mapping[str, Any], *, negative: bool) -> None:
        self._db.execute(
            "INSERT INTO mb_cache (key, body, fetched_at, negative) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at, "
            "negative = excluded.negative",
            (key, json.dumps(body), int(self._now()), 1 if negative else 0),
        )
        self._db.commit()


class _CacheEntry:
    __slots__ = ("body", "stale")

    def __init__(self, *, body: Mapping[str, Any], stale: bool) -> None:
        self.body = body
        self.stale = stale


def _escape_lucene(value: str) -> str:
    """Escape the lucene metacharacters that would otherwise break a quoted phrase."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", value)


def _score(candidate: Mapping[str, Any]) -> int:
    """MusicBrainz search score, 0-100. Absent or unparseable scores sort last."""
    raw = candidate.get("score")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return 0
    return 0


def _track_titles(release: Mapping[str, Any]) -> list[str]:
    media = release.get("media")
    if not isinstance(media, list):
        return []
    titles: list[str] = []
    for medium in media:
        if not isinstance(medium, Mapping):
            continue
        tracks = medium.get("tracks")
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            if isinstance(track, Mapping) and track.get("title"):
                titles.append(str(track["title"]))
    return titles


class CachedDisambiguations:
    """Artist disambiguations from ``mb_cache`` alone, for `explain --from-last-run`: never a request.

    Reads the ``artist-urls:<mbid>`` entries `MusicBrainzLookup.artist_disambiguation` caches, at
    any age, on a read-only connection - so it can run in the web server, and a database that is
    not there is never created. An artist the cache has not seen, like any failure, is ``None``.
    A context manager: the connection is closed on the way out.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            self._conn = None

    def __call__(self, artist_mbid: str) -> str | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT body FROM mb_cache WHERE key = ?", (f"artist-urls:{artist_mbid}",)
            ).fetchone()
            value = json.loads(row[0]).get("disambiguation") if row is not None else None
        except (sqlite3.Error, ValueError, AttributeError):
            return None
        return value.strip() or None if isinstance(value, str) else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> CachedDisambiguations:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
