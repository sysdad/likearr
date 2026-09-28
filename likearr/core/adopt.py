"""Take responsibility for what was already monitored before likearr existed.

likearr never unmonitors a release it does not own, so albums monitored by hand before it was
installed are invisible to it and a plain `run` leaves them alone; nothing needs adopting to be
protected. `adopt` is for a library that grew mostly from Lidarr's own import lists rather than by
hand: the user supplies a keep-list, everything on it becomes a `manual` owned release (owned, so
recorded, but never removable by the tool), what a Spotify source still backs is claimed, and the
rest - including anything monitored by hand that is not on the keep-list - is unmonitored once.

A followed artist whose catalogue could not be read this run (too large to browse, or a
MusicBrainz error) has none of their catalogue in the desired set, so for them "not wanted" means
"not known". What the keep list or another source already settles is kept or claimed as usual;
only the albums that would otherwise be unmonitored are held back, left monitored and unowned,
and the plan lists them with the reason. The same holds for an album a liked song, playlist song or
saved album points at by title and artist name when that item's own lookup failed this run. An
item that cannot be tied to an album that way is left to the plan's degraded-run warning.

It is not a casual recovery step after losing the state database. Lost state means owning
nothing, which is safe for `run` (nothing is unmonitored), but re-running `adopt` treats every
hand-monitored release as unwanted: restore the database from a backup instead, or rebuild the
keep-list first and read the plan before applying it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from likearr.core.desire import CATALOGUE_TOO_LARGE_STEP, CATALOGUE_UNREAD_STEPS
from likearr.core.normalize import normalize_name, normalize_title, strip_release_qualifiers
from likearr.core.resolver import METADATA_ERROR_STEP
from likearr.models import (
    RESOLVER_VERSION,
    ArtistResolution,
    DesiredState,
    LidarrView,
    OwnedRelease,
    Reason,
    ReasonKind,
    ReleaseKey,
    Resolution,
    SourceSnapshot,
    UnmonitorRelease,
)

__all__ = ["AdoptPlan", "HeldRelease", "adopt_digest", "plan_adoption"]

ADOPT_SOURCE_ID = "adopt"
"""The `Reason.source_id` every manual keep carries, so its origin is obvious in `explain`."""

_KEPT_BY_HAND = Reason(kind=ReasonKind.MANUAL, source_id=ADOPT_SOURCE_ID)


@dataclass(frozen=True, slots=True)
class HeldRelease:
    """A monitored release adopt would have unmonitored, left exactly as it is this time because
    whether a source wants it is unknown: its artist's catalogue was not read, or a source item
    pointing at it could not be looked up. Not on the keep list and not wanted by any source, or it
    would be kept or claimed instead."""

    key: ReleaseKey
    title: str
    step: str
    """The step that held it back: one of `CATALOGUE_UNREAD_STEPS`, or `METADATA_ERROR_STEP` for a
    failed source lookup."""
    own_lookup: bool = False
    """True when a source item pointing at this album failed its lookup, rather than the artist's
    catalogue going unread."""

    @property
    def reason(self) -> str:
        if self.own_lookup:
            return "a Spotify song or album that points here could not be looked up on MusicBrainz this run"
        if self.step == CATALOGUE_TOO_LARGE_STEP:
            return "the artist's catalogue is too large to browse on MusicBrainz"
        return "the artist's catalogue could not be read from MusicBrainz this run"


@dataclass(slots=True)
class AdoptPlan:
    """What adoption would do. Nothing here has happened yet."""

    keep_as_manual: list[OwnedRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, on the keep-list. Recorded as owned + manual, so the
    tool will never unmonitor them, and left monitored in Lidarr."""
    unmonitor: list[UnmonitorRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, not on the keep-list. Unmonitored once."""
    claim: list[OwnedRelease] = field(default_factory=list)
    """Monitored and also wanted by a source. Ownership is recorded with the real reasons, plus
    `manual` when the release is on the keep-list too; Lidarr is not touched, because the release
    is already in the state the desired state asks for."""
    held: list[HeldRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, not on the keep list, by an artist whose catalogue was
    not read or pointed at by a source item whose lookup failed: what would otherwise be in
    `unmonitor`. Neither claimed nor unmonitored, only listed: a later adopt, once MusicBrainz
    answers, sorts them. Never executed by `--apply`."""


def plan_adoption(
    desired: DesiredState,
    view: LidarrView,
    owned: Mapping[ReleaseKey, OwnedRelease],
    keep: set[str],
    *,
    now: datetime,
    snapshot: SourceSnapshot | None = None,
) -> AdoptPlan:
    """Plan adoption of everything Lidarr monitors that likearr does not already own.

    `keep` holds either a release group MBID (keep that release) or ``artist:<artist_mbid>``
    (keep everything by that artist). Anything already in `owned` is skipped: it has been through
    adoption or was monitored by likearr itself, and re-adopting it would overwrite real reasons
    with `manual` and make it permanently unremovable.

    An album that would be unmonitored is held back instead when its artist is in
    `desired.unmapped` at a catalogue step (`CATALOGUE_UNREAD_STEPS`): see `HeldRelease`. Claims
    and keeps for that artist go ahead, since neither depends on the unread catalogue.

    It is also held when a saved album, liked song or playlist song in `snapshot` is in
    `desired.unmapped` at `METADATA_ERROR_STEP` and names it: the Spotify album's title and one of
    its artists match the Lidarr album's title and artist by name. Without `snapshot` no such item
    can be tied to an album.

    `now` stamps the `monitored_at` of the records this creates; the core never reads the clock.
    """
    plan = AdoptPlan()
    unread = {
        u.artist_mbid: u.step
        for u in desired.unmapped
        if isinstance(u, ArtistResolution) and u.artist_mbid and u.step in CATALOGUE_UNREAD_STEPS
    }
    failed = _failed_lookup_titles(desired, snapshot)
    for artist_mbid in sorted(view.albums):
        albums = view.albums[artist_mbid]
        artist = view.artists.get(artist_mbid)
        failed_titles = failed.get(normalize_name(artist.name)) if failed and artist is not None else None
        for rg_mbid in sorted(albums):
            album = albums[rg_mbid]
            if not album.monitored:
                continue
            key = ReleaseKey(artist_mbid=artist_mbid, rg_mbid=rg_mbid)
            if key in owned:
                continue
            release = desired.releases.get(key)
            kept = rg_mbid in keep or f"artist:{artist_mbid}" in keep
            if release is not None and release.reasons:
                # On the keep list too: the manual reason rides along, so losing the source later
                # does not unmonitor a release the user asked adopt to keep.
                plan.claim.append(
                    OwnedRelease(
                        key=key,
                        reasons=frozenset(release.reasons) | ({_KEPT_BY_HAND} if kept else set()),
                        step=_step_of(release.steps),
                        resolver_version=RESOLVER_VERSION,
                        monitored_at=now,
                        lidarr_album_id=album.id,
                    )
                )
                continue
            if kept:
                plan.keep_as_manual.append(
                    OwnedRelease(
                        key=key,
                        reasons=frozenset({_KEPT_BY_HAND}),
                        step="adopt:keep",
                        resolver_version=RESOLVER_VERSION,
                        monitored_at=now,
                        lidarr_album_id=album.id,
                    )
                )
                continue
            if artist_mbid in unread:
                plan.held.append(HeldRelease(key=key, title=album.title, step=unread[artist_mbid]))
                continue
            if failed_titles is not None and _names(failed_titles, album.title):
                plan.held.append(HeldRelease(key=key, title=album.title, step=METADATA_ERROR_STEP, own_lookup=True))
                continue
            plan.unmonitor.append(UnmonitorRelease(key=key, title=album.title, lost_reasons=frozenset()))
    return plan


def _failed_lookup_titles(
    desired: DesiredState, snapshot: SourceSnapshot | None
) -> dict[str, tuple[set[str], set[str]]]:
    """Normalised artist name -> the Spotify album titles of source items whose lookup failed,
    normalised raw and again with trailing release qualifiers stripped, as the resolver does."""
    if snapshot is None:
        return {}
    keys = {u.intent_key for u in desired.unmapped if isinstance(u, Resolution) and u.step == METADATA_ERROR_STEP}
    if not keys:
        return {}
    albums = [i.album for i in snapshot.albums if i.reason.key in keys]
    albums += [i.album for i in snapshot.tracks if i.reason.key in keys]
    out: dict[str, tuple[set[str], set[str]]] = {}
    for album in albums:
        plain, stripped = normalize_title(album.name), normalize_title(strip_release_qualifiers(album.name))
        for name in album.artist_names:
            plains, strippeds = out.setdefault(normalize_name(name), (set(), set()))
            plains.add(plain)
            strippeds.add(stripped)
    return out


def _names(titles: tuple[set[str], set[str]], title: str) -> bool:
    """True when one of a failed item's album titles is this album's title, compared raw or with
    trailing release qualifiers stripped from both."""
    plains, strippeds = titles
    return normalize_title(title) in plains or normalize_title(strip_release_qualifiers(title)) in strippeds


def _step_of(steps: dict[str, str]) -> str:
    return steps[sorted(steps)[0]] if steps else "adopt:claim"


def adopt_digest(view: LidarrView, adoption: AdoptPlan, owned: Mapping[ReleaseKey, OwnedRelease]) -> str:
    """Hash the Lidarr and ownership state an adoption plan depends on, so a stale plan is refused.

    Like `core.diff.lidarr_digest` it is deliberately narrow: only the releases the plan names
    count (their album id and monitored flag, and whether likearr has since come to own them), so
    unrelated Lidarr activity between planning and applying does not invalidate a reviewed plan,
    while a release the plan would touch having moved does.
    """
    h = hashlib.sha256()
    groups = (
        ("c", [r.key for r in adoption.claim]),
        ("k", [r.key for r in adoption.keep_as_manual]),
        ("u", [u.key for u in adoption.unmonitor]),
    )
    for tag, keys in groups:
        for key in sorted(keys, key=lambda k: (k.artist_mbid, k.rg_mbid)):
            album = view.album(key)
            state = f"{album.id}:{int(album.monitored)}" if album is not None else "gone"
            h.update(f"{tag}:{key.artist_mbid}/{key.rg_mbid}:{state}:{int(key in owned)};".encode())
    return h.hexdigest()
