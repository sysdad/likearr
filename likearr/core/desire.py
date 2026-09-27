"""Turn resolutions into the desired state: what Lidarr should be monitoring right now.

Pure. The only outside call is `MetadataLookup.artist_release_groups` for followed artists,
whose whole studio catalogue is wanted and so cannot be known from the snapshot alone.

A release carries a *set* of reasons. Two liked tracks off the same album and a saved album of
the same record all land on one `DesiredRelease` with three reasons, and the release stays
wanted while any one of them survives.
"""

from __future__ import annotations

from likearr.models import (
    ArtistIntent,
    ArtistResolution,
    DesiredRelease,
    DesiredState,
    PrimaryType,
    Profile,
    Reason,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SourceSnapshot,
)
from likearr.ports import CatalogueTooLarge, MetadataError, MetadataLookup

from .resolver import METADATA_ERROR_STEP, ResolveResult

__all__ = ["CATALOGUE_ERROR_STEP", "CATALOGUE_TOO_LARGE_STEP", "CATALOGUE_UNREAD_STEPS", "build_desired"]

CATALOGUE_TOO_LARGE_STEP = "error:catalogue-too-large"
"""Step for a followed artist past MusicBrainz's browse ceiling: permanent, not an outage.

Its own step because it recurs on every run for that artist and must be reported once as a
named, actionable condition rather than counted as a metadata failure. The health layer routes
on this string, which keeps it pure - the same trick `core.diff.is_catalogue_gap` uses.
"""

CATALOGUE_ERROR_STEP = METADATA_ERROR_STEP
"""Step for a followed artist whose catalogue lookup raised `MetadataError` this run."""

CATALOGUE_UNREAD_STEPS = frozenset({CATALOGUE_TOO_LARGE_STEP, CATALOGUE_ERROR_STEP})
"""The steps of a followed artist in `unmapped` whose catalogue was not read, so none of their
releases are in the desired set: whether a source wants any of them is unknown, not "no" (#6)."""


def _add(
    releases: dict[ReleaseKey, DesiredRelease],
    rg: ReleaseGroup,
    reason: Reason,
    step: str,
) -> ReleaseKey:
    """Record one reason for wanting one release group, merging into any existing entry."""
    key = ReleaseKey(artist_mbid=rg.artist_mbid, rg_mbid=rg.mbid)
    entry = releases.get(key)
    if entry is None:
        entry = DesiredRelease(key=key, release_group=rg)
        releases[key] = entry
    entry.reasons.add(reason)
    entry.steps[reason.key] = step
    return key


def build_desired(
    snapshot: SourceSnapshot,
    resolve_result: ResolveResult,
    lookup: MetadataLookup,
    *,
    albums_only_artists: set[str],
    deny_releases: frozenset[str] = frozenset(),
) -> DesiredState:
    """Build the desired state from a snapshot and its resolutions.

    Followed artists contribute their whole studio Album/EP catalogue. An artist whose MusicBrainz
    ID carries the `albums-only` Lidarr tag (passed in `albums_only_artists`) contributes studio
    Albums only, no EPs. Release groups where the followed artist is not the *primary* credit are
    skipped: a featured appearance on someone else's record is not part of a catalogue, and
    monitoring it would quietly add that other artist to Lidarr. A release group on `deny_releases`
    (``[rules] deny_releases``, "Not this one") is left out of every followed catalogue (issue #153):
    it is the user's explicit "not this one", and unfollowing would be too blunt a way to drop one
    album. A saved album is never filtered: saving it on Spotify overrides the opt-outs.

    Saved albums and liked/playlist tracks contribute whatever their resolution says: RESOLVED
    adds a reason to a release, PENDING_ALBUM goes to `pending` (nothing is monitored, the run
    re-checks next time) and UNMAPPED goes to `unmapped` for the report.

    A `MetadataError` while listing one followed artist's catalogue is reported as an
    `ArtistResolution` with step ``error:metadata`` in `unmapped`, never dropped: an artist that
    silently vanished from the desired state would look exactly like an unfollowed artist and
    would cost the user their whole catalogue.
    """
    releases: dict[ReleaseKey, DesiredRelease] = {}
    artists: dict[str, str] = {}
    followed: set[str] = set()
    followed_counts: dict[str, int] = {}
    catalogue_counts: dict[str, int] = {}
    pending: list[Resolution] = []
    unmapped: list[Resolution | ArtistResolution] = []

    artist_intents: dict[str, ArtistIntent] = {i.reason.key: i for i in snapshot.artists}
    reasons: dict[str, Reason] = {}
    for artist_intent in snapshot.artists:
        reasons[artist_intent.reason.key] = artist_intent.reason
    for album_intent in snapshot.albums:
        reasons[album_intent.reason.key] = album_intent.reason
    for track_intent in snapshot.tracks:
        reasons[track_intent.reason.key] = track_intent.reason

    # ------------------------------------------------------------------ followed artists
    for key in sorted(resolve_result.artist_resolutions):
        resolution = resolve_result.artist_resolutions[key]
        if resolution.status != ResolutionStatus.RESOLVED or not resolution.artist_mbid:
            unmapped.append(resolution)
            continue
        intent = artist_intents.get(key)
        if intent is None:
            continue
        mbid = resolution.artist_mbid
        artists[mbid] = resolution.artist_name or intent.name
        followed.add(mbid)
        albums_only = mbid in albums_only_artists
        try:
            catalogue = lookup.artist_release_groups(mbid)
        except CatalogueTooLarge as e:
            unmapped.append(
                ArtistResolution(
                    intent_key=key,
                    status=ResolutionStatus.UNMAPPED,
                    artist_mbid=mbid,
                    artist_name=artists[mbid],
                    step=CATALOGUE_TOO_LARGE_STEP,
                    detail=(
                        f"the catalogue of {artists[mbid]!r} ({mbid}) is too large to browse: {e}. "
                        "The artist keeps whatever likearr already monitors for them; nothing new "
                        "is wanted from them until the ceiling is raised or they are monitored by hand"
                    ),
                )
            )
            continue
        except MetadataError as e:
            unmapped.append(
                ArtistResolution(
                    intent_key=key,
                    status=ResolutionStatus.UNMAPPED,
                    artist_mbid=mbid,
                    artist_name=artists[mbid],
                    step=CATALOGUE_ERROR_STEP,
                    detail=(
                        f"could not list the catalogue of {artists[mbid]!r} ({mbid}): {e}. "
                        "The artist keeps whatever likearr already monitors for them"
                    ),
                )
            )
            continue
        wanted = 0
        studio = 0
        for rg in catalogue:
            if not rg.is_studio_album_or_ep:
                continue
            if rg.artist_mbid != mbid:
                continue
            studio += 1  # counted before the user's own filters below: see `catalogue_counts`
            if albums_only and rg.primary_type is not PrimaryType.ALBUM:
                continue
            if rg.mbid in deny_releases:
                continue
            step = "followed:catalogue" + (":albums-only" if albums_only else "")
            _add(releases, rg, intent.reason, step)
            wanted += 1
        followed_counts[mbid] = wanted
        catalogue_counts[mbid] = studio

    # ------------------------------------------------------------------ albums and tracks
    for key in sorted(resolve_result.resolutions):
        resolution = resolve_result.resolutions[key]
        if resolution.status == ResolutionStatus.PENDING_ALBUM:
            pending.append(resolution)
            continue
        if resolution.status != ResolutionStatus.RESOLVED or resolution.release_group is None:
            unmapped.append(resolution)
            continue
        reason = reasons.get(key)
        if reason is None:
            # The resolution outlived its intent (a cached entry for something no longer in the
            # snapshot). It contributes no reason, so it contributes nothing.
            continue
        rg = resolution.release_group
        artists.setdefault(rg.artist_mbid, rg.artist_name)
        _add(releases, rg, reason, resolution.step)

    # ------------------------------------------------------------------ profiles
    profile_needs: dict[str, Profile] = {mbid: Profile.LEAN for mbid in artists}
    for release in releases.values():
        if release.needs_full_profile:
            profile_needs[release.key.artist_mbid] = Profile.FULL

    return DesiredState(
        releases=releases,
        artists=artists,
        followed_artists=followed,
        pending=pending,
        unmapped=unmapped,
        profile_needs=profile_needs,
        followed_counts=followed_counts,
        catalogue_counts=catalogue_counts,
    )
