"""Take responsibility for what was already monitored before likearr existed.

likearr never unmonitors a release it does not own, so albums monitored by hand before it was
installed are invisible to it and a plain `run` leaves them alone. Adoption **claims** the ones a
Spotify source wants: they become owned, with their real reasons, so unliking one later unmonitors
it. Everything else stays monitored and unowned, listed in the plan as left alone.

Unmonitoring the rest is an explicit opt-in (`unmonitor_rest`), for a library that grew mostly
from Lidarr's own import lists rather than by hand: the user may supply a keep-list, everything on
it becomes a `manual` owned release (owned, so recorded, but never removable by the tool), and the
rest - including anything monitored by hand that is not on the keep-list - is unmonitored once.

Whether a source wants an album is sometimes unknown this run, and such an album is held back
from the unmonitor, left monitored and unowned, and the plan lists it with the reason:

- a followed artist whose catalogue could not be read (too large to browse, or a MusicBrainz
  error): none of their catalogue is in the desired set, so "not wanted" means "not known";
- a followed artist whose own lookup failed: their albums, matched by artist name;
- an album a liked song, playlist song or saved album points at by title and artist name, when
  that item's lookup failed, including when MusicBrainz failed and Lidarr's fallback found nothing.

What the keep list or another source already settles is kept or claimed as usual. An item that
cannot be tied to an album by name (a Various Artists album, an artist spelled differently in
Lidarr) is left to the plan's degraded-run warning.

It is not a casual recovery step after losing the state database. Lost state means owning
nothing, which is safe for `run` (nothing is unmonitored), but unmonitoring the rest then treats
every hand-monitored release as unwanted: restore the database from a backup instead, or rebuild
the keep-list first and read the plan before applying it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
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

__all__ = [
    "HELD_ARTIST",
    "HELD_CATALOGUE",
    "HELD_ITEM",
    "AdoptPlan",
    "HeldRelease",
    "LeftRelease",
    "adopt_digest",
    "choose",
    "plan_adoption",
]

ADOPT_SOURCE_ID = "adopt"
"""The `Reason.source_id` every manual keep carries, so its origin is obvious in `explain`."""

_KEPT_BY_HAND = Reason(kind=ReasonKind.MANUAL, source_id=ADOPT_SOURCE_ID)


HELD_CATALOGUE = "catalogue"
HELD_ITEM = "item"
HELD_ARTIST = "artist"


@dataclass(frozen=True, slots=True)
class HeldRelease:
    """A monitored release unmonitoring the rest would have unmonitored, left exactly as it is
    this time because whether a source wants it is unknown (see the module docstring). Not on the
    keep list and not wanted by any source, or it would be kept or claimed instead."""

    key: ReleaseKey
    title: str
    step: str
    """The step that held it back: one of `CATALOGUE_UNREAD_STEPS`, or `METADATA_ERROR_STEP` for a
    failed lookup."""
    cause: str = HELD_CATALOGUE
    """`HELD_CATALOGUE` (the artist's catalogue was not read), `HELD_ARTIST` (the followed artist's
    own lookup failed) or `HELD_ITEM` (a song or album pointing here failed its lookup)."""

    @property
    def reason(self) -> str:
        if self.cause == HELD_ITEM:
            return "a Spotify song or album that points here could not be looked up on MusicBrainz this run"
        if self.cause == HELD_ARTIST:
            return "a followed artist of this name could not be looked up on MusicBrainz this run"
        if self.step == CATALOGUE_TOO_LARGE_STEP:
            return "the artist's catalogue is too large to browse on MusicBrainz"
        return "the artist's catalogue could not be read from MusicBrainz this run"


@dataclass(frozen=True, slots=True)
class LeftRelease:
    """A monitored release no source wants, left monitored and unowned: likearr never touches it."""

    key: ReleaseKey
    title: str


@dataclass(slots=True)
class AdoptPlan:
    """What adoption would do. Nothing here has happened yet."""

    keep_as_manual: list[OwnedRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, on the keep-list. Recorded as owned + manual, so the
    tool will never unmonitor them, and left monitored in Lidarr. Only with `unmonitor_rest`."""
    unmonitor: list[UnmonitorRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, not on the keep-list. Unmonitored once. Only with
    `unmonitor_rest`."""
    claim: list[OwnedRelease] = field(default_factory=list)
    """Monitored and also wanted by a source. Ownership is recorded with the real reasons, plus
    `manual` when the release is on the keep-list too; Lidarr is not touched, because the release
    is already in the state the desired state asks for."""
    held: list[HeldRelease] = field(default_factory=list)
    """What would otherwise be in `unmonitor`, held back because whether a source wants it is
    unknown. Neither claimed nor unmonitored, only listed: a later adopt, once MusicBrainz
    answers, sorts them. Never executed by `--apply`. Only with `unmonitor_rest`."""
    left: list[LeftRelease] = field(default_factory=list)
    """Monitored, not wanted by any source, left as they are. Without `unmonitor_rest`, every
    such release is here and `keep_as_manual`, `unmonitor` and `held` are empty."""
    unmonitor_rest: bool = False
    """The mode: unmonitor what no source wants, or (the default) only claim."""

    def claim_only(self) -> AdoptPlan:
        """This plan with nothing unmonitored: what `unmonitor` and `held` hold is left as it is."""
        left = [LeftRelease(key=u.key, title=u.title) for u in self.unmonitor]
        left += [LeftRelease(key=h.key, title=h.title) for h in self.held]
        return AdoptPlan(claim=list(self.claim), left=sorted([*self.left, *left], key=_order))


def choose(plan: AdoptPlan, *, claim: bool, unmonitor_rest: bool, keep: AbstractSet[str] = frozenset()) -> AdoptPlan:
    """The part of a full plan (made with `unmonitor_rest` and no keep list) a reviewer chose:
    claim the matching releases or not, and unmonitor the rest or not, leaving the release groups
    in `keep` as they are. Held releases are never unmonitored."""
    if not unmonitor_rest:
        chosen = plan.claim_only()
    else:
        kept = [LeftRelease(key=u.key, title=u.title) for u in plan.unmonitor if u.key.rg_mbid in keep]
        chosen = AdoptPlan(
            claim=list(plan.claim),
            unmonitor=[u for u in plan.unmonitor if u.key.rg_mbid not in keep],
            held=list(plan.held),
            left=sorted([*plan.left, *kept], key=_order),
            unmonitor_rest=True,
        )
    if not claim:
        chosen.claim = []
    return chosen


def _order(item: LeftRelease) -> tuple[str, str]:
    return item.key.artist_mbid, item.key.rg_mbid


def plan_adoption(
    desired: DesiredState,
    view: LidarrView,
    owned: Mapping[ReleaseKey, OwnedRelease],
    keep: AbstractSet[str],
    *,
    now: datetime,
    unmonitor_rest: bool = False,
    snapshot: SourceSnapshot | None = None,
    lookup_failed: AbstractSet[str] = frozenset(),
) -> AdoptPlan:
    """Plan adoption of everything Lidarr monitors that likearr does not already own.

    Without `unmonitor_rest` it only claims: what no source wants goes to `left`, and `keep` must
    be empty (the keep list only means something to the unmonitor).

    `keep` holds either a release group MBID (keep that release) or ``artist:<artist_mbid>``
    (keep everything by that artist). Anything already in `owned` is skipped: it has been through
    adoption or was monitored by likearr itself, and re-adopting it would overwrite real reasons
    with `manual` and make it permanently unremovable.

    An album that would be unmonitored is held back instead (see `HeldRelease`) when:

    - its artist is in `desired.unmapped` at a catalogue step (`CATALOGUE_UNREAD_STEPS`);
    - a followed artist in `desired.unmapped` whose lookup failed has its artist's name;
    - a saved album, liked song or playlist song in `snapshot` whose lookup failed names it: the
      Spotify album's title and one of its artists match the Lidarr album's title and artist by
      name. Without `snapshot` no such item can be tied to an album.

    A lookup failed when the item is unmapped at `METADATA_ERROR_STEP`, or unmapped with its key
    in `lookup_failed`: MusicBrainz failed during it and Lidarr's fallback found nothing.

    Claims and keeps go ahead regardless, since neither depends on what could not be read.

    `now` stamps the `monitored_at` of the records this creates; the core never reads the clock.
    """
    if keep and not unmonitor_rest:
        raise ValueError("a keep list only applies when unmonitoring the rest")
    plan = AdoptPlan(unmonitor_rest=unmonitor_rest)
    unread = {
        u.artist_mbid: u.step
        for u in desired.unmapped
        if isinstance(u, ArtistResolution)
        and u.artist_mbid
        and (u.step in CATALOGUE_UNREAD_STEPS or u.intent_key in lookup_failed)
    }
    # A followed artist whose own lookup failed has no MusicBrainz id to match, only a name.
    failed_artists = {
        normalize_name(u.artist_name)
        for u in desired.unmapped
        if isinstance(u, ArtistResolution)
        and not u.artist_mbid
        and u.artist_name
        and (u.step == METADATA_ERROR_STEP or u.intent_key in lookup_failed)
    }
    failed = _failed_lookup_titles(desired, snapshot, lookup_failed)
    for artist_mbid in sorted(view.albums):
        albums = view.albums[artist_mbid]
        artist = view.artists.get(artist_mbid)
        name = normalize_name(artist.name) if artist is not None and artist.name else None
        failed_titles = failed.get(name) if name is not None else None
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
            if not unmonitor_rest:
                plan.left.append(LeftRelease(key=key, title=album.title))
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
            if name is not None and name in failed_artists:
                plan.held.append(HeldRelease(key=key, title=album.title, step=METADATA_ERROR_STEP, cause=HELD_ARTIST))
                continue
            if failed_titles is not None and _names(failed_titles, album.title):
                plan.held.append(HeldRelease(key=key, title=album.title, step=METADATA_ERROR_STEP, cause=HELD_ITEM))
                continue
            plan.unmonitor.append(UnmonitorRelease(key=key, title=album.title, lost_reasons=frozenset()))
    return plan


def _failed_lookup_titles(
    desired: DesiredState, snapshot: SourceSnapshot | None, lookup_failed: AbstractSet[str]
) -> dict[str, tuple[set[str], set[str]]]:
    """Normalised artist name -> the Spotify album titles of source items whose lookup failed,
    normalised raw and again with trailing release qualifiers stripped, as the resolver does."""
    if snapshot is None:
        return {}
    keys = {
        u.intent_key
        for u in desired.unmapped
        if isinstance(u, Resolution) and (u.step == METADATA_ERROR_STEP or u.intent_key in lookup_failed)
    }
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
