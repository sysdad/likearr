"""Take responsibility for what was already monitored before likearr existed.

likearr never unmonitors a release it does not own, so albums monitored by hand before it was
installed are invisible to it and a plain `run` leaves them alone; nothing needs adopting to be
protected. `adopt` is for a library that grew mostly from Lidarr's own import lists rather than by
hand: the user supplies a keep-list, everything on it becomes a `manual` owned release (owned, so
recorded, but never removable by the tool), what a Spotify source still backs is claimed, and the
rest - including anything monitored by hand that is not on the keep-list - is unmonitored once.

A followed artist whose catalogue could not be read this run (too large to browse, or a
MusicBrainz error) has none of their releases in the desired set, so for them "not wanted" means
"not known". Their monitored albums are held back, neither claimed nor unmonitored, and the plan
lists them with the reason (#6).

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
from likearr.models import (
    RESOLVER_VERSION,
    ArtistResolution,
    DesiredState,
    LidarrView,
    OwnedRelease,
    Reason,
    ReasonKind,
    ReleaseKey,
    UnmonitorRelease,
)

__all__ = ["AdoptPlan", "HeldRelease", "adopt_digest", "plan_adoption"]

ADOPT_SOURCE_ID = "adopt"
"""The `Reason.source_id` every manual keep carries, so its origin is obvious in `explain`."""

_KEPT_BY_HAND = Reason(kind=ReasonKind.MANUAL, source_id=ADOPT_SOURCE_ID)


@dataclass(frozen=True, slots=True)
class HeldRelease:
    """A monitored release adopt leaves exactly as it is this time, because its artist's catalogue
    was not read: whether a source wants it is unknown (#6)."""

    key: ReleaseKey
    title: str
    step: str
    """The catalogue step that held it back, one of `CATALOGUE_UNREAD_STEPS`."""

    @property
    def reason(self) -> str:
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
    """Monitored, by an artist whose catalogue was not read. Neither claimed nor unmonitored, only
    listed: a later adopt, once the catalogue reads, sorts them. Never executed by `--apply`."""


def plan_adoption(
    desired: DesiredState,
    view: LidarrView,
    owned: Mapping[ReleaseKey, OwnedRelease],
    keep: set[str],
    *,
    now: datetime,
) -> AdoptPlan:
    """Plan adoption of everything Lidarr monitors that likearr does not already own.

    `keep` holds either a release group MBID (keep that release) or ``artist:<artist_mbid>``
    (keep everything by that artist). Anything already in `owned` is skipped: it has been through
    adoption or was monitored by likearr itself, and re-adopting it would overwrite real reasons
    with `manual` and make it permanently unremovable.

    An album whose artist is in `desired.unmapped` at a catalogue step (`CATALOGUE_UNREAD_STEPS`)
    is held back instead, the keep list notwithstanding: see `HeldRelease`.

    `now` stamps the `monitored_at` of the records this creates; the core never reads the clock.
    """
    plan = AdoptPlan()
    unread = {
        u.artist_mbid: u.step
        for u in desired.unmapped
        if isinstance(u, ArtistResolution) and u.artist_mbid and u.step in CATALOGUE_UNREAD_STEPS
    }
    for artist_mbid in sorted(view.albums):
        albums = view.albums[artist_mbid]
        for rg_mbid in sorted(albums):
            album = albums[rg_mbid]
            if not album.monitored:
                continue
            key = ReleaseKey(artist_mbid=artist_mbid, rg_mbid=rg_mbid)
            if key in owned:
                continue
            if artist_mbid in unread:
                plan.held.append(HeldRelease(key=key, title=album.title, step=unread[artist_mbid]))
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
            plan.unmonitor.append(UnmonitorRelease(key=key, title=album.title, lost_reasons=frozenset()))
    return plan


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
