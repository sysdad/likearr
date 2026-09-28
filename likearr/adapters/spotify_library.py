"""Spotify from the writing side: search, the two library listings, and the two writes.

Implements :class:`likearr.ports.SpotifyLibraryPort`. This is the only module in likearr that
changes anything on Spotify, and every endpoint it touches is taken from Spotify's own reference:

| What | Call | Scope | Per request |
|---|---|---|---|
| Follow an artist | ``PUT /me/library?uris=spotify:artist:<id>`` | ``user-follow-modify`` | 20 URIs |
| Save an album | ``PUT /me/library?uris=spotify:album:<id>`` | ``user-library-modify`` | 20 URIs |
| What is followed? | ``GET /me/following?type=artist`` | ``user-follow-read`` | ``limit`` max 50, cursor-paged |
| What is saved? | ``GET /me/albums`` | ``user-library-read`` | ``limit`` max 50, offset-paged |
| Find either | ``GET /search`` | none | ``limit`` 0-10, default 5 |
| Playlists | ``GET /me``, ``GET /me/playlists`` | ``playlist-read-private`` + ``-collaborative`` | ``limit`` 50 |

**The published reference is wrong for this app, twice. Do not "fix" either of these back.**
Measured against a Development Mode app, with every scope granted:

- **Writes.** The reference documents ``PUT /me/following?type=artist`` and ``PUT /me/albums``.
  Both answer **403 Forbidden** here - those library writes are deprecated and, for an app in
  development mode, blocked outright. What works is ``PUT /me/library?uris=…``: 200, and the
  album really is saved / the artist really is followed, confirmed by re-reading the list
  endpoints. It is one unified endpoint for both, told apart by the ``spotify:album:`` or
  ``spotify:artist:`` prefix, which is why a follow and a save share one code path here. The URIs
  go in the **query string**; a body-only call answers 400 "Missing required field: uris".
  ``GET /me/library`` is 405, so it is write-only and reads stay where they are.
- **Membership.** ``GET /me/following/contains`` and ``GET /me/albums/contains`` also answer
  **403**, while ``GET /me``, ``GET /me/following``, ``GET /me/albums`` and ``GET /search`` all
  answer 200 on the same token - the same development-mode family as non-owned playlists
  returning no items, and not something a scope or a consent screen fixes. Membership comes from
  paging the list endpoints once per run. There is no ``/contains`` fast path: a fallback that
  never runs is a fallback that is never known to work.

Three behaviours matter as much as the endpoints:

- **Quota.** Development Mode quota is per developer account and small - a burst of ~700
  ``search`` calls exhausted it on a fresh app. Searches are rate-limited to one every
  ``min_interval_s`` and counted against ``max_searches``; exceeding the budget raises
  :class:`~likearr.ports.SearchBudgetExceeded` rather than grinding on.
- **Every search is cached on disk.** A re-plan after a quota error, an interrupt or a review
  costs zero API calls for everything already looked up, which is what makes `promote-save`
  resumable. Negative results expire, so an album Spotify adds later is found on a later run.
- **Nothing is written that is already there.** The caller filters on the two library listings;
  the writes are idempotent anyway, so a resumed apply cannot double-write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from likearr.adapters.musicbrainz import RateLimiter
from likearr.adapters.spotify import API_BASE, SpotifyAuth, album_ref, authorized_request, can_read_collaborative
from likearr.models import SpotifyAlbumRef, SpotifyArtistRef
from likearr.ports import SchemaError, SearchBudgetExceeded, SourceError

__all__ = [
    "LIBRARY_BATCH",
    "PAGE_LIMIT",
    "SEARCH_LIMIT",
    "OwnedPlaylist",
    "PlaylistEntry",
    "SpotifyLibrary",
]

log = logging.getLogger(__name__)

LIBRARY_BATCH = 20
"""URIs per ``PUT /me/library`` call.

Measured, not documented: 20 comma-separated album URIs in one call answered 200 and all 20
landed. The endpoint is not in the public reference at all, so there is no published maximum to
read; 20 is the largest figure actually verified against Spotify and stays until a bigger one
is.
"""

SEARCH_LIMIT = 10
"""``GET /search``: ``limit`` is documented "Range: 0 - 10"."""

PAGE_LIMIT = 50
"""``GET /me/following``, ``GET /me/albums`` and ``GET /me/playlists``: all document ``limit`` "Maximum: 50"."""

_MAX_LIBRARY_PAGES = 200
"""10,000 items. A bound on a ``next`` that never turns null, not a real-library limit."""

DEFAULT_SEARCH_INTERVAL_S = 0.5
"""One search every half second. A couple of hundred items take a couple of minutes, nowhere near
the quota."""

DEFAULT_MAX_SEARCHES = 600
"""Budget for one planning pass. Below the ~700-search burst that exhausted the quota, above what a
typical promote-save costs, so a retry storm or a bug stops instead of burning the account's quota."""

_SEARCH_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_search_cache (
    key        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    negative   INTEGER NOT NULL DEFAULT 0
)
"""

_NEGATIVE_CACHE_DAYS = 7.0
"""A search that found nothing is retried a week later; a hit is kept (ids do not move)."""


@dataclass(frozen=True, slots=True)
class OwnedPlaylist:
    """A playlist a run can read: one the authorized user owns, or collaborates on once the token
    has ``playlist-read-collaborative`` - the only kinds Development Mode reads items from."""

    id: str
    name: str
    track_count: int
    """What the playlist object itself reports; 0 when it reports nothing."""


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    """One row of ``GET /me/playlists``, owned or not - what the picker shows.

    Unlike :class:`OwnedPlaylist`, nothing is dropped for being someone else's: the picker (and
    ``likearr playlists``) need every playlist the account can see, so the ones Development Mode
    will not read items from can be shown, greyed out, with the reason. A playlist you collaborate
    on but do not own is `owned=False`, `collaborative=True`, and `readable` once the stored token
    has ``playlist-read-collaborative``.
    """

    id: str
    name: str
    track_count: int
    """What the playlist object itself reports; 0 when it reports nothing."""
    owned: bool
    """Whether ``owner.id`` matches the authorized user. ``False`` covers followed, other users'
    (collaborative ones too), and Spotify's own algorithmic and editorial playlists alike."""
    collaborative: bool = False
    """Spotify's own ``collaborative`` flag on the playlist."""
    collaborative_scope: bool = False
    """Whether the stored token had ``playlist-read-collaborative`` when this was listed."""

    @property
    def readable(self) -> bool:
        """Whether a run can read its songs, so the picker may offer it: owned, or collaborative
        with a token that has ``playlist-read-collaborative``. Development Mode returns no items
        for any other playlist."""
        return self.owned or (self.collaborative and self.collaborative_scope)

    @property
    def needs_reauth(self) -> bool:
        """Someone else's collaborative playlist, listed by a token granted before likearr asked for
        ``playlist-read-collaborative``: a re-authorization, not a copy, makes it readable."""
        return self.collaborative and not self.owned and not self.collaborative_scope


def _track_total(entry: Mapping[str, Any]) -> int:
    """A simplified playlist's track count: ``tracks.total``, else ``items.total``, else 0.

    ``tracks.total`` is the documented shape. Development Mode renamed the playlist's nested
    collection to ``items`` once already (see ``SpotifySource``), and the count may follow it
    there, so both are read - preferring ``tracks`` because it is the one known to be live.
    """
    for key in ("tracks", "items"):
        block = entry.get(key)
        total = block.get("total") if isinstance(block, Mapping) else None
        if isinstance(total, int) and not isinstance(total, bool):
            return total
    return 0


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _quote(value: str) -> str:
    """A value safe inside a ``field:"..."`` search filter (Spotify has no escape syntax)."""
    return value.replace('"', " ").strip()


def _artist_refs(payload: Mapping[str, Any]) -> list[SpotifyArtistRef]:
    block = payload.get("artists")
    items = block.get("items") if isinstance(block, Mapping) else None
    if not isinstance(items, list):
        return []
    out: list[SpotifyArtistRef] = []
    for item in items:
        if isinstance(item, Mapping) and item.get("id"):
            out.append(SpotifyArtistRef(spotify_id=str(item["id"]), name=str(item.get("name") or "")))
    return out


def _album_refs(payload: Mapping[str, Any]) -> list[SpotifyAlbumRef]:
    block = payload.get("albums")
    items = block.get("items") if isinstance(block, Mapping) else None
    if not isinstance(items, list):
        return []
    return [album_ref(item) for item in items if isinstance(item, Mapping) and item.get("id")]


def _item_id(item: object) -> str:
    """The Spotify id of one library item.

    Followed artists are the entity itself; a saved album is nested under ``album``, because the
    item is the *saving* and carries ``added_at`` of its own. One reader handles both.
    """
    if not isinstance(item, Mapping):
        return ""
    nested = item.get("album")
    entity = nested if isinstance(nested, Mapping) else item
    return str(entity.get("id") or "")


class SpotifyLibrary:
    """Implements :class:`likearr.ports.SpotifyLibraryPort` against the Spotify Web API."""

    def __init__(
        self,
        auth: SpotifyAuth,
        client: httpx.Client,
        *,
        cache_path: Path | str | None = None,
        min_interval_s: float = DEFAULT_SEARCH_INTERVAL_S,
        max_searches: int = DEFAULT_MAX_SEARCHES,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            cache_path: SQLite file for the search cache (the shell passes the state DB path, and
                this adapter opens its own WAL connection, like the MusicBrainz cache does).
                ``None`` disables caching, which only the tests want.
            max_searches: the per-instance ``search`` budget. See `DEFAULT_MAX_SEARCHES`.
        """
        self._auth = auth
        self._client = client
        self._limiter = RateLimiter(min_interval_s, monotonic=monotonic, sleep=sleep)
        self._sleep = sleep
        self._now = now
        self._max_searches = max_searches
        self._followed: frozenset[str] | None = None
        self._saved: frozenset[str] | None = None
        self.searches = 0
        """``search`` calls actually sent this run - cache hits do not count."""

        self._db: sqlite3.Connection | None = None
        if cache_path is not None:
            self._db = sqlite3.connect(str(cache_path))
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute(_SEARCH_CACHE_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ---------------------------------------------------------------- scopes

    def granted_scopes(self) -> frozenset[str]:
        """Scopes the stored token carries. Reads the token file; never prints or returns it."""
        return self._auth.granted_scopes()

    # ---------------------------------------------------------------- search

    def search_artists(self, name: str) -> Sequence[SpotifyArtistRef]:
        cleaned = _quote(name)
        if not cleaned:
            return ()
        payload = self._search(f'artist:"{cleaned}"', "artist", cache_key=f"artist:{cleaned.casefold()}")
        return tuple(_artist_refs(payload))

    def search_albums_by_upc(self, upc: str) -> Sequence[SpotifyAlbumRef]:
        cleaned = _quote(upc)
        if not cleaned:
            return ()
        payload = self._search(f"upc:{cleaned}", "album", cache_key=f"upc:{cleaned}")
        return tuple(_album_refs(payload))

    def search_albums(self, artist: str, title: str) -> Sequence[SpotifyAlbumRef]:
        artist_q, title_q = _quote(artist), _quote(title)
        if not title_q:
            return ()
        query = f'album:"{title_q}" artist:"{artist_q}"' if artist_q else f'album:"{title_q}"'
        payload = self._search(query, "album", cache_key=f"album:{artist_q.casefold()}|{title_q.casefold()}")
        return tuple(_album_refs(payload))

    def _search(self, query: str, kind: str, *, cache_key: str) -> Mapping[str, Any]:
        """One `GET /search`, from the on-disk cache when it can be.

        Raises:
            SearchBudgetExceeded: the budget is spent. Everything already searched stays cached,
                so the next run resumes instead of repeating the work.
        """
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        if self.searches >= self._max_searches:
            raise SearchBudgetExceeded(
                f"spotify search budget of {self._max_searches} calls is spent; every result so far is "
                "cached, so re-running continues from here rather than starting over"
            )
        self._limiter.acquire()
        self.searches += 1
        payload = authorized_request(
            self._client,
            self._auth,
            "GET",
            f"{API_BASE}/search",
            params={"q": query, "type": kind, "limit": SEARCH_LIMIT},
            context=f"search {kind}",
            sleep=self._sleep,
        )
        block = payload.get(f"{kind}s")
        items = block.get("items") if isinstance(block, Mapping) else None
        self._cache_put(cache_key, payload, negative=not isinstance(items, list) or not items)
        return payload

    # ---------------------------------------------------------------- what is already there

    def followed_artist_ids(self) -> frozenset[str]:
        """Every artist id the user follows, by paging ``GET /me/following?type=artist``.

        Cursor-paged: the page lives under ``artists``, and ``artists.next`` is an absolute URL
        that already carries the ``after`` cursor, so it is followed directly rather than
        rebuilt from ``artists.cursors.after``.

        Read once and memoised for the life of this object, because every membership test in a
        run asks the same question of the same list.
        """
        if self._followed is None:
            ids = self._page_ids(
                f"{API_BASE}/me/following", {"type": "artist", "limit": PAGE_LIMIT}, "followed artists"
            )
            self._followed = frozenset(ids)
            log.info("spotify: %d followed artists", len(self._followed))
        return self._followed

    def saved_album_ids(self) -> frozenset[str]:
        """Every saved album id, by paging ``GET /me/albums``.

        Offset-paged, with the album nested under ``items[].album`` (the item itself is the
        *saving*, carrying ``added_at``). ``next`` is an absolute URL, so offsets are never
        computed here. Memoised like the follows.
        """
        if self._saved is None:
            self._saved = frozenset(self._page_ids(f"{API_BASE}/me/albums", {"limit": PAGE_LIMIT}, "saved albums"))
            log.info("spotify: %d saved albums", len(self._saved))
        return self._saved

    # ---------------------------------------------------------------- playlists

    def all_playlists(self) -> list[PlaylistEntry]:
        """Every playlist ``GET /me/playlists`` lists, owned and not, sorted by name (casefolded),
        then id.

        ``GET /me/playlists`` lists owned *and followed* playlists alike (Spotify's own algorithmic
        and editorial playlists too, once followed), so the user's own id comes from ``GET /me``
        and each entry is marked owned or not by comparing ``owner.id``, and readable when owned or
        collaborative with a token that has ``playlist-read-collaborative``. Nothing
        is dropped for being someone else's: this is what the picker shows, greying out anything
        not readable with the reason, so a person never has to guess why a playlist did not sync.
        Read-only; the scopes come from the token file, with no extra request. Every call goes
        through `authorized_request`, so any token refresh happens under the token lock like every
        other Spotify call.

        A ``null`` entry, or one without an id, is skipped rather than failing the listing:
        Spotify sends those for playlists it will not show at all, and this is a picker, not a
        source.

        Raises:
            SchemaError: ``GET /me`` has no ``id``, or a page lost ``items`` or ``next``.
            SourceError: auth, quota or transport failure, or more than `_MAX_LIBRARY_PAGES` pages.
        """
        me = authorized_request(
            self._client, self._auth, "GET", f"{API_BASE}/me", context="current user", sleep=self._sleep
        )
        user_id = str(me.get("id") or "")
        if not user_id:
            raise SchemaError("spotify current user: GET /me has no 'id'")
        try:
            collaborative_ok = can_read_collaborative(self.granted_scopes())
        except SourceError:
            collaborative_ok = False

        out: list[PlaylistEntry] = []
        for entry in self._page_items(f"{API_BASE}/me/playlists", {"limit": PAGE_LIMIT}, "playlists"):
            if not isinstance(entry, Mapping) or not entry.get("id"):
                continue
            owner = entry.get("owner")
            owned = isinstance(owner, Mapping) and str(owner.get("id") or "") == user_id
            collaborative = entry.get("collaborative") is True
            out.append(
                PlaylistEntry(
                    id=str(entry["id"]),
                    name=str(entry.get("name") or ""),
                    track_count=_track_total(entry),
                    owned=owned,
                    collaborative=collaborative,
                    collaborative_scope=collaborative_ok,
                )
            )
        out.sort(key=lambda p: (p.name.casefold(), p.id))
        log.info(
            "spotify: %d playlists, %d owned, %d readable",
            len(out),
            sum(1 for p in out if p.owned),
            sum(1 for p in out if p.readable),
        )
        return out

    def owned_playlists(self) -> list[OwnedPlaylist]:
        """Every playlist a run can read - owned, or collaborative with the scope - sorted
        by name (casefolded), then id.

        The selectable subset of `all_playlists`: Development Mode returns zero items for any other
        playlist, so offering one would only set up a run that fails on it.
        """
        return [
            OwnedPlaylist(id=p.id, name=p.name, track_count=p.track_count) for p in self.all_playlists() if p.readable
        ]

    # ---------------------------------------------------------------- paging

    def _page_ids(self, url: str, first_params: Mapping[str, Any], context: str) -> list[str]:
        """Walk a paged library endpoint and return every item's Spotify id. See `_page_items`."""
        return [spotify_id for item in self._page_items(url, first_params, context) if (spotify_id := _item_id(item))]

    def _page_items(self, url: str, first_params: Mapping[str, Any], context: str) -> Iterator[object]:
        """Walk a paged library endpoint and yield every raw item.

        One function for every shape: the followed-artists page is nested under ``artists`` and
        cursor-paged, the saved-albums and playlists pages are top level and offset-paged, but all
        answer a paging object with ``items`` and an absolute ``next``, and following ``next`` is
        correct for each. The ``limit`` is only sent on the first request; ``next`` carries its own.

        `_MAX_LIBRARY_PAGES` bounds it: a response whose ``next`` never becomes null would
        otherwise loop forever, and 200 pages is 10,000 items - far past any real library.
        """
        next_url: str | None = url
        params: Mapping[str, Any] | None = first_params
        pages = 0
        while next_url:
            pages += 1
            if pages > _MAX_LIBRARY_PAGES:
                raise SourceError(
                    f"spotify {context}: more than {_MAX_LIBRARY_PAGES * PAGE_LIMIT} items; refusing to page further"
                )
            payload = authorized_request(
                self._client, self._auth, "GET", next_url, params=params, context=context, sleep=self._sleep
            )
            params = None
            block = payload.get("artists") if isinstance(payload.get("artists"), Mapping) else payload
            items = block.get("items")
            if not isinstance(items, list):
                raise SchemaError(f"spotify {context}: 'items' is missing or not a list")
            if "next" not in block:
                raise SchemaError(f"spotify {context}: 'next' is missing (pagination shape changed?)")
            yield from items
            nxt = block.get("next")
            next_url = str(nxt) if isinstance(nxt, str) and nxt else None

    # ---------------------------------------------------------------- writes

    def follow_artists(self, artist_ids: Sequence[str]) -> None:
        """Follow artists through `PUT /me/library?uris=spotify:artist:...`. See `_write_library`."""
        self._write_library(artist_ids, "artist", "follow artists")

    def save_albums(self, album_ids: Sequence[str]) -> None:
        """Save albums through `PUT /me/library?uris=spotify:album:...`. See `_write_library`."""
        self._write_library(album_ids, "album", "save albums")

    def _write_library(self, spotify_ids: Sequence[str], kind: str, context: str) -> None:
        """The one endpoint that actually writes: `PUT /me/library?uris=<comma-separated>`.

        A follow and a save are the same call, told apart only by the `spotify:artist:` or
        `spotify:album:` prefix on each URI - which is why this is one method rather than two.

        The URIs go in the **query string**. A body-only request answers 400 "Missing required
        field: uris", so `json_body` is deliberately never used here. Batched at `LIBRARY_BATCH`.
        """
        unique = list(dict.fromkeys(i for i in spotify_ids if i))
        for chunk in _chunks(unique, LIBRARY_BATCH):
            authorized_request(
                self._client,
                self._auth,
                "PUT",
                f"{API_BASE}/me/library",
                params={"uris": ",".join(f"spotify:{kind}:{i}" for i in chunk)},
                context=context,
                sleep=self._sleep,
            )
            log.info("%s: wrote %d %s URI(s) to the Spotify library", context, len(chunk), kind)

    # ---------------------------------------------------------------- cache

    def _cache_get(self, key: str) -> Mapping[str, Any] | None:
        if self._db is None:
            return None
        row = self._db.execute(
            "SELECT body, fetched_at, negative FROM spotify_search_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        body_text, fetched_at, negative = row
        try:
            body = json.loads(body_text)
        except ValueError:  # pragma: no cover - defensive
            return None
        if not isinstance(body, dict):  # pragma: no cover - defensive
            return None
        if negative and (self._now() - float(fetched_at)) / 86400.0 >= _NEGATIVE_CACHE_DAYS:
            return None
        return body

    def _cache_put(self, key: str, body: Mapping[str, Any], *, negative: bool) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO spotify_search_cache (key, body, fetched_at, negative) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at, "
            "negative = excluded.negative",
            (key, json.dumps(dict(body)), int(self._now()), 1 if negative else 0),
        )
        self._db.commit()
