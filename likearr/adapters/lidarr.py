"""Lidarr API v1 adapter.

Implements :class:`likearr.ports.LidarrPort` against Lidarr 2.x/3.x (header ``X-Api-Key``).

Two rules are load-bearing:

- **Never ``GET /api/v1/album`` unfiltered.** On a large library that response runs to hundreds of MB.
  Albums are always fetched per artist via ``?artistId=``; :meth:`LidarrClient.load_view` only
  loads albums for the artists the diff actually asked about.
- **Lidarr's metadata proxy is a separate failure domain.** ``album/lookup`` and ``RefreshArtist``
  talk to ``api.lidarr.audio``, which has had multi-month outages. Those failures raise
  :class:`likearr.ports.LidarrMetadataError` so the run can skip an artist rather than treat a
  missing catalogue as "the user does not want this any more".
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from likearr.adapters.http import HttpError, RedirectRefused, redact, request_with_retries, safe_url
from likearr.config import LidarrConfig
from likearr.core.normalize import credits_match, normalize_name, normalize_title, strip_bare_featuring
from likearr.models import (
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    PrimaryType,
    Profile,
    ReleaseGroup,
    SecondaryType,
)
from likearr.ports import LidarrArtistExists, LidarrArtistUnknown, LidarrError, LidarrMetadataError

__all__ = [
    "BATCH_SIZE",
    "SUPPORTED_MAJORS",
    "LidarrClient",
    "expected_metadata_profile_types",
    "metadata_profile_diff",
]

SUPPORTED_MAJORS = (2, 3)
"""Lidarr major versions this adapter has been written against."""

BATCH_SIZE = 25
"""Batching keeps a failed call from losing the whole run. 25, not 100: on a large library
a `PUT /album/monitor` with 100 ids took Lidarr over 60 s to answer (it does per-album work), so
the client timed out on a change the server had already applied."""

LIDARR_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
"""Lidarr writes can be slow on a large library; give them five minutes rather than one."""

_COMMAND_POLL_S = 2.0
_TERMINAL_COMMAND_STATES = {"completed", "failed", "aborted", "cancelled"}

_LEAN_PRIMARY = frozenset({"Album", "EP"})
_FULL_PRIMARY = frozenset({"Album", "EP", "Single"})
_LEAN_SECONDARY = frozenset({"Studio"})
_FULL_SECONDARY = frozenset({"Studio", "Compilation", "Soundtrack", "Live"})
_ALLOWED_RELEASE_STATUSES = frozenset({"Official"})

_VERSION_RE = re.compile(r"^(\d+)\.")


def _as_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
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
            continue
    return frozenset(out)


def _batched(items: Sequence[int], size: int = BATCH_SIZE) -> Iterable[Sequence[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


_ARTIST_UNKNOWN = "an artist with this id was not found"
"""What Lidarr's 400 says when its metadata server does not know an MBID (Lidarr 3.1, issue #173).

Matched on the exact phrase, not on "not found": Lidarr says "not found" about other things
likearr sends (a quality or metadata profile, a root folder), and those are likearr's own mistakes,
which must stop the apply rather than quietly skip an artist on every run."""


def _is_artist_unknown(exc: LidarrError) -> bool:
    cause = exc.__cause__
    return isinstance(cause, HttpError) and cause.status_code == 400 and _ARTIST_UNKNOWN in cause.body_excerpt.lower()


class LidarrClient:
    """Lidarr API v1 client. Implements :class:`likearr.ports.LidarrPort`."""

    def __init__(
        self,
        config: LidarrConfig,
        client: httpx.Client,
        *,
        api_key: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            api_key: overrides ``LIKEARR_LIDARR_API_KEY``; tests pass an obviously fake key so
                they never depend on the environment.
            sleep/monotonic: injected so command polling can be tested without waiting.
        """
        self._config = config
        self._client = client
        self._api_key = api_key if api_key is not None else config.api_key
        self._sleep = sleep
        self._monotonic = monotonic
        self._version: str | None = None

    # ---------------------------------------------------------------- transport

    def _url(self, path: str) -> str:
        return f"{self._config.url}/api/v1/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        allow_status: tuple[int, ...] = (),
    ) -> httpx.Response:
        try:
            return request_with_retries(
                self._client,
                method,
                self._url(path),
                params=params,
                json=json,
                headers={"X-Api-Key": self._api_key},
                allow_status=allow_status,
                sleep=self._sleep,
            )
        except RedirectRefused as exc:
            # A login portal in front of Lidarr, usually: the key must not follow it there.
            raise LidarrError(
                f"Lidarr at {safe_url(self._config.url).rstrip('/')} redirected to {exc.origin}; set [lidarr] url to "
                "Lidarr itself (for example the container address), not a login page"
            ) from exc
        except HttpError as exc:
            raise LidarrError(f"lidarr {method} /{path.lstrip('/')}: {exc}") from exc
        except UnicodeError as exc:
            # httpx raises this (idna.IDNAError), not an HTTPError, for a redirect to a host that
            # is not valid IDNA, before any request is sent there. An error, not a crash.
            raise LidarrError(f"lidarr {method} /{path.lstrip('/')}: {type(exc).__name__}: {redact(str(exc))}") from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        response = self._request(method, path, params=params, json=json)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise LidarrError(f"lidarr {method} /{path.lstrip('/')}: response body is not valid JSON") from exc

    def _list(self, path: str, *, params: Mapping[str, Any] | None = None) -> list[Any]:
        payload = self._json("GET", path, params=params)
        if not isinstance(payload, list):
            raise LidarrError(f"lidarr GET /{path.lstrip('/')}: expected a JSON array")
        return payload

    def _metadata_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Like :meth:`_json` but maps outages of Lidarr's metadata proxy to LidarrMetadataError."""
        try:
            return self._json(method, path, params=params, json=json)
        except LidarrError as exc:
            cause = exc.__cause__
            if isinstance(cause, HttpError) and cause.is_server_side:
                raise LidarrMetadataError(f"lidarr metadata lookup failed (api.lidarr.audio outage?): {exc}") from exc
            raise

    # ---------------------------------------------------------------- version

    def version(self) -> str:
        """Lidarr's reported version string, fetched once per client."""
        if self._version is None:
            payload = self._json("GET", "system/status")
            if not isinstance(payload, Mapping) or not payload.get("version"):
                raise LidarrError("lidarr GET /system/status: no 'version' in the response")
            self._version = str(payload["version"])
        return self._version

    def check_version(self) -> str:
        """Refuse to run against a major version this adapter has not been written for.

        Raises:
            LidarrError: unparseable or unsupported major version.
        """
        version = self.version()
        match = _VERSION_RE.match(version)
        if not match:
            raise LidarrError(f"lidarr reported an unparseable version {version!r}")
        major = int(match.group(1))
        if major not in SUPPORTED_MAJORS:
            supported = " or ".join(f"{m}.x" for m in SUPPORTED_MAJORS)
            raise LidarrError(
                f"lidarr {version} is major version {major}; likearr has only been verified "
                f"against {supported}. Upgrade likearr before pointing it at this instance."
            )
        return version

    # ---------------------------------------------------------------- reads

    def load_view(self, artist_mbids: Iterable[str] | None = None) -> LidarrView:
        """Artists, profiles and tags, plus albums for exactly the requested artists."""
        artists: dict[str, LidarrArtist] = {}
        for raw in self._list("artist"):
            if isinstance(raw, Mapping):
                artist = _artist_from(raw)
                if artist is not None:
                    artists[artist.mbid] = artist

        albums: dict[str, dict[str, LidarrAlbum]] = {}
        for mbid in dict.fromkeys(artist_mbids or ()):
            artist = artists.get(mbid)
            if artist is not None:
                albums[mbid] = self.load_albums(artist)

        return LidarrView(
            artists=artists,
            albums=albums,
            metadata_profiles=self._profiles("metadataprofile"),
            quality_profiles=self._profiles("qualityprofile"),
            tags=self._tags(),
            version=self.version(),
        )

    def load_albums(self, artist: LidarrArtist) -> dict[str, LidarrAlbum]:
        """rg_mbid -> album for one artist. Always filtered by ``artistId``."""
        out: dict[str, LidarrAlbum] = {}
        for raw in self._list("album", params={"artistId": artist.id}):
            if not isinstance(raw, Mapping):
                continue
            album = _album_from(raw, default_artist_mbid=artist.mbid)
            if album is not None:
                out[album.rg_mbid] = album
        return out

    def _profiles(self, path: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for raw in self._list(path):
            if isinstance(raw, Mapping) and raw.get("name") and isinstance(raw.get("id"), int):
                out[str(raw["name"])] = int(raw["id"])
        return out

    def _tags(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for raw in self._list("tag"):
            if isinstance(raw, Mapping) and raw.get("label") and isinstance(raw.get("id"), int):
                out[str(raw["label"])] = int(raw["id"])
        return out

    def root_folders(self) -> list[dict[str, Any]]:
        """Raw root folder resources, for `setup-profiles` and `doctor`."""
        return [dict(raw) for raw in self._list("rootfolder") if isinstance(raw, Mapping)]

    def metadata_profile_details(self) -> list[dict[str, Any]]:
        """Raw metadata profile resources (with their allowed album types), for `setup-profiles`'s
        preview: whether an existing profile of the same name matches what likearr would create."""
        return [dict(raw) for raw in self._list("metadataprofile") if isinstance(raw, Mapping)]

    def track_files(self, album_id: int) -> list[dict[str, Any]]:
        """Track file resources for one album (``GET /trackfile?albumId=``).

        `prune-stage` needs the real paths on disk; the album resource carries only counts and
        sizes. Always filtered by ``albumId``, for the same reason albums are always filtered by
        ``artistId``.
        """
        return [dict(raw) for raw in self._list("trackfile", params={"albumId": album_id}) if isinstance(raw, Mapping)]

    def import_lists(self) -> list[dict[str, Any]]:
        """Import list resources (``GET /importlist``), for Clean up's pre-flight check (#58): one
        with automatic add re-adds an artist `prune-stage` removes."""
        return [dict(raw) for raw in self._list("importlist") if isinstance(raw, Mapping)]

    def command_queue(self) -> list[dict[str, Any]]:
        """Lidarr's commands still queued or running (``GET /command``), for the same check: a
        rescan or refresh in flight while files move out can re-import what just left."""
        return [
            dict(raw)
            for raw in self._list("command")
            if isinstance(raw, Mapping) and str(raw.get("status") or "").lower() in {"queued", "started"}
        ]

    # ---------------------------------------------------------------- metadata lookup

    def lookup_release_group(self, rg_mbid: str) -> ReleaseGroup | None:
        """Lidarr's own metadata lookup by MusicBrainz release-group id.

        Lidarr accepts a ``lidarr:<mbid>`` term (also ``lidarrid:`` / ``mbid:``) and resolves it
        directly rather than running a text search.
        """
        results = self._metadata_request("GET", "album/lookup", params={"term": f"lidarr:{rg_mbid}"})
        if not isinstance(results, list):
            return None
        for raw in results:
            if isinstance(raw, Mapping) and str(raw.get("foreignAlbumId") or "") == rg_mbid:
                return _release_group_from(raw)
        return None

    def search_release_group(self, artist: str, title: str) -> ReleaseGroup | None:
        """Free-text fallback for Lidarr's metadata lookup. Conservative: both names must match.

        The first match, in Lidarr's order. `CompositeLookup` asks `search_release_group_candidates`
        instead, because the first match of two same-named artists is a guess (issue #42).
        """
        found = self.search_release_group_candidates(artist, title)
        return found[0] if found else None

    def search_release_group_candidates(self, artist: str, title: str) -> tuple[ReleaseGroup, ...]:
        """Every artist's first match, in Lidarr's order; one artist's is `search_release_group`'s.

        Only the first hit per artist is kept, so a single artist's answer is exactly what this
        search always gave, and only a *second artist* - Jungle the London band and Jungle the US
        one, say - changes anything: the resolver then decides between them on the track's ISRC,
        or refuses to, as it does for MusicBrainz's candidates.

        Names are compared with the core normaliser - `normalize_title` for the title, `credits_match`
        for the artist - which keeps letters in every script (issue #5). This adapter used to fold
        to ASCII, so a name written wholly in Japanese, Cyrillic or Greek folded to "" and any two
        such artists compared equal. A side that still folds to "" matches nothing.
        """
        if not artist.strip() or not title.strip():
            return ()
        results = self._metadata_request("GET", "album/lookup", params={"term": f"{artist} {title}"})
        if not isinstance(results, list):
            return ()
        want_title = normalize_title(title)
        if not want_title or not normalize_name(strip_bare_featuring(artist)):
            # Nothing left to compare once folded ("(Live)", or combining marks alone). An empty
            # string equals every other empty string, so it can never be evidence of a match.
            return ()
        found: dict[str, ReleaseGroup] = {}
        for raw in results:
            if not isinstance(raw, Mapping):
                continue
            group = _release_group_from(raw)
            if group is None:
                continue
            if normalize_title(group.title) == want_title and credits_match(group.artist_name, artist):
                found.setdefault(group.artist_mbid, group)
        return tuple(found.values())

    # ---------------------------------------------------------------- writes

    def add_artist(
        self,
        artist_mbid: str,
        name: str,
        *,
        root_folder: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        tag_ids: Sequence[int],
    ) -> LidarrArtist:
        """Add an artist monitored but with nothing selected and no search triggered.

        ``monitorNewItems: none`` plus ``addOptions.monitor: none`` means Lidarr adds the artist
        and its catalogue without monitoring a single album; likearr then monitors exactly what
        the diff asked for.

        Raises:
            LidarrArtistUnknown: Lidarr's metadata server does not know this MBID yet (a 400).
            LidarrArtistExists: the artist is already in Lidarr (a 400), carrying that artist.
                Whether it is likearr's own is the caller's call, by its tag (issue #4).
            LidarrMetadataError: Lidarr's metadata server failed the add (a 5xx or no answer).
            LidarrError: anything else, including a 400 about something likearr sent.
        """
        body = {
            "foreignArtistId": artist_mbid,
            "artistName": name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "monitorNewItems": "none",
            "tags": list(tag_ids),
            "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
        }
        try:
            # Adding an artist makes Lidarr fetch it from its metadata server, so an outage there
            # comes back as a 5xx here and is a LidarrMetadataError: skip the artist, not the apply.
            payload = self._metadata_request("POST", "artist", json=body)
        except LidarrMetadataError:
            raise
        except LidarrError as exc:
            existing = self._find_existing_artist(exc, artist_mbid)
            if existing is not None:
                raise LidarrArtistExists(
                    f"lidarr already has this artist ({name}, {artist_mbid}): {exc}", existing
                ) from exc
            if _is_artist_unknown(exc):
                raise LidarrArtistUnknown(
                    f"Lidarr's metadata does not know this artist yet ({name}, {artist_mbid}); "
                    f"skipped this run and tried again next run: {exc}"
                ) from exc
            raise
        if not isinstance(payload, Mapping):
            raise LidarrError("lidarr POST /artist: expected the created artist resource")
        artist = _artist_from(payload)
        if artist is None:
            raise LidarrError("lidarr POST /artist: created artist has no id or foreignArtistId")
        if not artist.monitored:
            # Lidarr's `addOptions.monitor: "none"` also unmonitors the ARTIST (every artist added that
            # way comes back monitored=false), and an unmonitored artist is never searched, whatever
            # its albums say. Flip it back; album monitoring stays "none".
            self._request("PUT", "artist/editor", json={"artistIds": [artist.id], "monitored": True})
            artist = replace(artist, monitored=True)
        return artist

    def _find_existing_artist(self, exc: LidarrError, artist_mbid: str) -> LidarrArtist | None:
        """The artist Lidarr holds under `artist_mbid` when `exc` is its 400 'already exists'."""
        cause = exc.__cause__
        if not isinstance(cause, HttpError) or cause.status_code != 400:
            return None
        if "already" not in cause.body_excerpt.lower():
            return None
        for raw in self._list("artist"):
            if isinstance(raw, Mapping) and str(raw.get("foreignArtistId") or "") == artist_mbid:
                return _artist_from(raw)
        return None

    def refresh_artist(self, artist: LidarrArtist, *, timeout_s: float = 300) -> None:
        """Queue ``RefreshArtist`` and poll the command until it finishes.

        Raises:
            LidarrMetadataError: the command failed, was aborted, or did not finish in time.
                The caller skips this artist for the run rather than unmonitoring from it.
        """
        # `isNewArtist: true` limits Lidarr's follow-up RescanFolders to the artist's own folder.
        # Without it, "Rescan after refresh: Always" (Lidarr's default) rescans EVERY root folder
        # once per refreshed artist - many minutes each on a large network-mounted library - so an
        # apply that adds many artists can jam Lidarr's command queue for hours. API pushes are
        # always trigger=manual, so the "After Manual Refresh" setting would not help either.
        payload = self._json(
            "POST", "command", json={"name": "RefreshArtist", "artistId": artist.id, "isNewArtist": True}
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("id"), int):
            raise LidarrError("lidarr POST /command: RefreshArtist returned no command id")
        command_id = int(payload["id"])
        deadline = self._monotonic() + timeout_s

        while True:
            status_payload = self._json("GET", f"command/{command_id}")
            if not isinstance(status_payload, Mapping):
                raise LidarrError(f"lidarr GET /command/{command_id}: expected a JSON object")
            status = str(status_payload.get("status") or "").lower()
            if status in _TERMINAL_COMMAND_STATES:
                if status == "completed":
                    return
                message = str(status_payload.get("message") or status_payload.get("exception") or "")
                raise LidarrMetadataError(
                    f"lidarr RefreshArtist for {artist.name} ({artist.mbid}) ended as {status}"
                    + (f": {message}" if message else "")
                )
            if self._monotonic() >= deadline:
                raise LidarrMetadataError(
                    f"lidarr RefreshArtist for {artist.name} ({artist.mbid}) did not finish within "
                    f"{timeout_s:.0f}s (last status: {status or 'unknown'})"
                )
            self._sleep(_COMMAND_POLL_S)

    def set_albums_monitored(self, album_ids: Sequence[int], monitored: bool) -> None:
        """Flip the monitored flag on albums, in batches of 100."""
        for batch in _batched(list(album_ids)):
            self._request("PUT", "album/monitor", json={"albumIds": list(batch), "monitored": monitored})

    def set_artist_profile(self, artist: LidarrArtist, metadata_profile_id: int) -> None:
        """Ratchet one artist to another metadata profile via the bulk editor endpoint."""
        self._request(
            "PUT",
            "artist/editor",
            json={"artistIds": [artist.id], "metadataProfileId": metadata_profile_id},
        )

    def set_artists_new_items_none(self, artist_ids: Sequence[int]) -> None:
        """Stop Lidarr auto-monitoring new releases; likearr decides what is monitored."""
        for batch in _batched(list(artist_ids)):
            self._request("PUT", "artist/editor", json={"artistIds": list(batch), "monitorNewItems": "none"})

    def set_artists_monitored(self, artist_ids: Sequence[int]) -> None:
        """Set the artists monitored. An unmonitored artist is never searched, whatever its albums say."""
        for batch in _batched(list(artist_ids)):
            self._request("PUT", "artist/editor", json={"artistIds": list(batch), "monitored": True})

    def ensure_tag(self, label: str) -> int:
        """Return the id of the tag, creating it if it does not exist. Idempotent."""
        want = label.strip().lower()
        for raw in self._list("tag"):
            if isinstance(raw, Mapping) and str(raw.get("label") or "").strip().lower() == want:
                return int(raw["id"])
        created = self._json("POST", "tag", json={"label": label})
        if not isinstance(created, Mapping) or not isinstance(created.get("id"), int):
            raise LidarrError(f"lidarr POST /tag: could not create tag {label!r}")
        return int(created["id"])

    def ensure_metadata_profile(self, profile: Profile, name: str) -> int:
        """Return the id of the Lean/Full metadata profile, creating it from the schema if missing.

        Lean allows Album + EP, Studio only. Full adds Single plus Compilation, Soundtrack and
        Live. Neither ever allows Remix, DJ-mix, Mixtape or Demo, and both accept Official
        releases only.
        """
        want = name.strip().lower()
        for raw in self._list("metadataprofile"):
            if isinstance(raw, Mapping) and str(raw.get("name") or "").strip().lower() == want:
                return int(raw["id"])

        schema = self._json("GET", "metadataprofile/schema")
        if not isinstance(schema, Mapping):
            raise LidarrError("lidarr GET /metadataprofile/schema: expected a JSON object")

        primary = _FULL_PRIMARY if profile is Profile.FULL else _LEAN_PRIMARY
        secondary = _FULL_SECONDARY if profile is Profile.FULL else _LEAN_SECONDARY
        body: dict[str, Any] = {
            "name": name,
            "primaryAlbumTypes": _set_allowed(schema.get("primaryAlbumTypes"), "albumType", primary),
            "secondaryAlbumTypes": _set_allowed(schema.get("secondaryAlbumTypes"), "albumType", secondary),
            "releaseStatuses": _set_allowed(schema.get("releaseStatuses"), "releaseStatus", _ALLOWED_RELEASE_STATUSES),
        }
        created = self._json("POST", "metadataprofile", json=body)
        if not isinstance(created, Mapping) or not isinstance(created.get("id"), int):
            raise LidarrError(f"lidarr POST /metadataprofile: could not create profile {name!r}")
        return int(created["id"])

    def add_root_folder(self, path: str) -> dict[str, Any]:
        """Create a root folder, defaulting new artists to monitoring nothing.

        Returns the existing resource unchanged when a root folder with that path is already
        configured, so it is safe to call repeatedly.
        """
        for folder in self.root_folders():
            if str(folder.get("path") or "").rstrip("/") == path.rstrip("/"):
                return folder
        body: dict[str, Any] = {
            "path": path,
            "name": Path(path).name or path,
            "defaultMonitorOption": "none",
            "defaultNewItemMonitorOption": "none",
            "defaultQualityProfileId": _first_id(self._list("qualityprofile")),
            "defaultMetadataProfileId": _first_id(self._list("metadataprofile")),
            "defaultTags": [],
        }
        created = self._json("POST", "rootfolder", json=body)
        if not isinstance(created, Mapping):
            raise LidarrError(f"lidarr POST /rootfolder: could not create a root folder at {path!r}")
        return dict(created)

    def delete_artist(self, artist_id: int, *, delete_files: bool = False) -> None:
        """Remove an artist from Lidarr, always leaving the files alone.

        Raises:
            LidarrError: `delete_files` is True. likearr never deletes files; `prune-stage` moves
                them to a holding directory and ``rm`` stays a human's job.
        """
        if delete_files:
            raise LidarrError("likearr never deletes files: delete_artist(delete_files=True) is refused")
        self._request(
            "DELETE",
            f"artist/{artist_id}",
            params={"deleteFiles": "false", "addImportListExclusion": "false"},
        )

    def _artist_raw(self, artist_id: int) -> Mapping[str, Any]:
        raw = self._json("GET", f"artist/{artist_id}")
        if not isinstance(raw, Mapping):
            raise LidarrError(f"lidarr GET /artist/{artist_id}: expected a JSON object")
        return raw

    def artist_track_file_records(self, artist_id: int) -> int:
        """How many track-file records Lidarr holds for the artist, across every album
        (``GET /trackfile?artistId=``): the same unit as `track_files` per album, which is what
        `prune-stage` compares it with. Not ``statistics.trackFileCount``, a different figure that
        need not agree (a whole-album file mapped to many tracks, a leftover record)."""
        return sum(1 for raw in self._list("trackfile", params={"artistId": artist_id}) if isinstance(raw, Mapping))

    def rescan_artist(self, artist_id: int) -> None:
        """Queue a ``RescanFolders`` of the artist's own folder, without waiting, so Lidarr notices
        moved files.

        Lidarr has no ``RescanArtist`` command. ``RescanFolders`` needs the folder spelled out: with
        no ``folders`` it scans every root folder, which is the same slow full library walk that a
        bare RefreshArtist triggers (see `refresh_artist`).
        """
        path = str(self._artist_raw(artist_id).get("path") or "")
        if not path:
            raise LidarrError(f"lidarr artist {artist_id} has no path; refusing a root-wide rescan")
        self._request(
            "POST",
            "command",
            json={"name": "RescanFolders", "folders": [path], "addNewArtists": False, "artistIds": [artist_id]},
        )

    def set_root_folder_defaults(self, path: str, monitor: str = "none", new_items: str = "none") -> None:
        """Set a root folder's default monitor options so manual adds do not monitor everything.

        Raises:
            LidarrError: no root folder with that path exists.
        """
        for folder in self.root_folders():
            if str(folder.get("path") or "").rstrip("/") != path.rstrip("/"):
                continue
            body = {**folder, "defaultMonitorOption": monitor, "defaultNewItemMonitorOption": new_items}
            self._request("PUT", f"rootfolder/{int(folder['id'])}", json=body)
            return
        raise LidarrError(f"lidarr has no root folder at {path!r}")


# ---------------------------------------------------------------------------- JSON -> models


def _first_id(items: list[Any]) -> int:
    """The lowest id in a list of Lidarr resources, or 1 when there is none.

    Used only for the *default* profile ids a root folder must carry; likearr always names the
    profile it wants explicitly when it adds an artist, so this value never decides anything.
    """
    ids = [int(raw["id"]) for raw in items if isinstance(raw, Mapping) and isinstance(raw.get("id"), int)]
    return min(ids) if ids else 1


def expected_metadata_profile_types(profile: Profile) -> tuple[frozenset[str], frozenset[str]]:
    """The (primary, secondary) album type names `ensure_metadata_profile` sets up for `profile`."""
    primary = _FULL_PRIMARY if profile is Profile.FULL else _LEAN_PRIMARY
    secondary = _FULL_SECONDARY if profile is Profile.FULL else _LEAN_SECONDARY
    return primary, secondary


def _allowed_names_from(items: object, type_key: str) -> frozenset[str]:
    """The type names an existing profile's `primaryAlbumTypes`/`secondaryAlbumTypes` list allows."""
    if not isinstance(items, list):
        return frozenset()
    names: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping) or not raw.get("allowed"):
            continue
        block = raw.get(type_key)
        name = str(block.get("name") or "") if isinstance(block, Mapping) else str(block or "")
        if name:
            names.add(name)
    return frozenset(names)


def metadata_profile_diff(raw: Mapping[str, Any], profile: Profile) -> dict[str, list[str]] | None:
    """``None`` when `raw` (a `metadata_profile_details` entry) already allows exactly the album
    types `ensure_metadata_profile` would set up for `profile`; otherwise what differs, each side
    sorted for a stable preview. `ensure_metadata_profile` never edits an existing profile - it
    reuses it by name as-is - so this is preview-only: it tells a human what will silently be kept
    rather than silently changing it.
    """
    expected_primary, expected_secondary = expected_metadata_profile_types(profile)
    actual_primary = _allowed_names_from(raw.get("primaryAlbumTypes"), "albumType")
    actual_secondary = _allowed_names_from(raw.get("secondaryAlbumTypes"), "albumType")
    if actual_primary == expected_primary and actual_secondary == expected_secondary:
        return None
    return {
        "expected_primary": sorted(expected_primary),
        "actual_primary": sorted(actual_primary),
        "expected_secondary": sorted(expected_secondary),
        "actual_secondary": sorted(actual_secondary),
    }


def _set_allowed(items: object, type_key: str, allowed_names: frozenset[str]) -> list[dict[str, Any]]:
    """Copy a schema's type list, flipping ``allowed`` for the named types."""
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        type_block = entry.get(type_key)
        type_name = str(type_block.get("name") or "") if isinstance(type_block, Mapping) else str(type_block or "")
        entry["allowed"] = type_name in allowed_names
        out.append(entry)
    return out


def _artist_from(raw: Mapping[str, Any]) -> LidarrArtist | None:
    artist_id = raw.get("id")
    mbid = raw.get("foreignArtistId")
    if not isinstance(artist_id, int) or not mbid:
        return None
    tags = raw.get("tags")
    return LidarrArtist(
        id=artist_id,
        mbid=str(mbid),
        name=str(raw.get("artistName") or ""),
        monitored=bool(raw.get("monitored")),
        monitor_new_items=str(raw.get("monitorNewItems") or ""),
        metadata_profile_id=int(raw.get("metadataProfileId") or 0),
        quality_profile_id=int(raw.get("qualityProfileId") or 0),
        tags=frozenset(t for t in tags if isinstance(t, int)) if isinstance(tags, list) else frozenset(),
        path=str(raw.get("path") or ""),
    )


def _album_from(raw: Mapping[str, Any], *, default_artist_mbid: str = "") -> LidarrAlbum | None:
    album_id = raw.get("id")
    rg_mbid = raw.get("foreignAlbumId")
    if not isinstance(album_id, int) or not rg_mbid:
        return None
    nested_artist = raw.get("artist")
    artist_mbid = ""
    if isinstance(nested_artist, Mapping):
        artist_mbid = str(nested_artist.get("foreignArtistId") or "")
    stats = raw.get("statistics")
    stats_map: Mapping[str, Any] = stats if isinstance(stats, Mapping) else {}
    return LidarrAlbum(
        id=album_id,
        rg_mbid=str(rg_mbid),
        artist_id=int(raw.get("artistId") or 0),
        artist_mbid=artist_mbid or default_artist_mbid,
        title=str(raw.get("title") or ""),
        monitored=bool(raw.get("monitored")),
        primary_type=_primary_type(raw.get("albumType")),
        secondary_types=_secondary_types(raw.get("secondaryTypes")),
        release_date=_as_date(raw.get("releaseDate")),
        track_file_count=int(stats_map.get("trackFileCount") or 0),
        size_on_disk=int(stats_map.get("sizeOnDisk") or 0),
    )


def _release_group_from(raw: Mapping[str, Any]) -> ReleaseGroup | None:
    """Map an ``album/lookup`` result onto the same ReleaseGroup the resolver gets from MusicBrainz."""
    mbid = raw.get("foreignAlbumId")
    if not mbid:
        return None
    artist = raw.get("artist")
    artist_map: Mapping[str, Any] = artist if isinstance(artist, Mapping) else {}
    return ReleaseGroup(
        mbid=str(mbid),
        title=str(raw.get("title") or ""),
        artist_mbid=str(artist_map.get("foreignArtistId") or ""),
        artist_name=str(artist_map.get("artistName") or ""),
        primary_type=_primary_type(raw.get("albumType")),
        secondary_types=_secondary_types(raw.get("secondaryTypes")),
        first_release_date=_as_date(raw.get("releaseDate")),
    )
