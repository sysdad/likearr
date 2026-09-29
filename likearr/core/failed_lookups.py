"""Which Lidarr albums a lookup that failed this run may point at, matched by name.

A followed artist whose own lookup failed has no MusicBrainz id this run, and a liked song,
playlist song or saved album whose lookup failed points at no release group. Neither can be tied
to a Lidarr album by id, so whether a source wants that album is unknown rather than "no". These
helpers tie them back by name: the artist by its normalised name, the item by its Spotify album's
title and one of its artists. `adopt` holds such albums back from an unmonitor, and Clean up keeps
them.

A lookup failed when the item is unmapped at `METADATA_ERROR_STEP`, or unmapped with its key in
`lookup_failed` (`ResolveResult.provisional`): MusicBrainz failed during it and Lidarr's fallback
found nothing. An item that cannot be tied by name (a Various Artists album, an artist spelled
differently in Lidarr) is not found here.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from likearr.core.normalize import normalize_name, normalize_title, strip_release_qualifiers
from likearr.core.resolver import is_lookup_failed
from likearr.models import ArtistResolution, DesiredState, Resolution, SourceSnapshot

__all__ = ["FailedItems", "failed_artist_names", "failed_items"]


def failed_artist_names(desired: DesiredState, lookup_failed: AbstractSet[str] = frozenset()) -> dict[str, str]:
    """Followed artists whose own lookup failed this run: normalised name -> the follow's intent key."""
    out: dict[str, str] = {}
    for u in sorted(desired.unmapped, key=lambda u: u.intent_key):
        if (
            isinstance(u, ArtistResolution)
            and not u.artist_mbid
            and u.artist_name
            and (is_lookup_failed(u.step) or u.intent_key in lookup_failed)
        ):
            out.setdefault(normalize_name(u.artist_name), u.intent_key)
    return out


@dataclass(frozen=True, slots=True)
class _FailedTitle:
    plain: str
    stripped: str
    intent_key: str
    name: str
    """The item's own title: the song's for a liked or playlist song, the album's for a saved album."""
    artists: tuple[str, ...]
    """The item's own artists."""


@dataclass(frozen=True, slots=True)
class FailedItems:
    """Songs and saved albums whose lookup failed this run, by the album they name."""

    by_artist: dict[str, list[_FailedTitle]] = field(default_factory=dict)
    """Normalised artist name -> the failed items whose Spotify album credits that artist."""

    def naming(self, artist_name: str, title: str) -> _FailedTitle | None:
        """The first failed item (by intent key) whose Spotify album is this album: its title, raw
        or with trailing release qualifiers stripped from both, and one of its artists match."""
        items = self.by_artist.get(normalize_name(artist_name)) if artist_name else None
        if not items:
            return None
        plain, stripped = normalize_title(title), normalize_title(strip_release_qualifiers(title))
        for item in items:
            if item.plain == plain or item.stripped == stripped:
                return item
        return None


def failed_items(
    desired: DesiredState, snapshot: SourceSnapshot | None, lookup_failed: AbstractSet[str] = frozenset()
) -> FailedItems:
    """The saved albums, liked songs and playlist songs in `snapshot` whose lookup failed this run.
    Without `snapshot` none can be tied to an album."""
    if snapshot is None:
        return FailedItems()
    keys = {
        u.intent_key
        for u in desired.unmapped
        if isinstance(u, Resolution) and (is_lookup_failed(u.step) or u.intent_key in lookup_failed)
    }
    if not keys:
        return FailedItems()
    found = [
        (i.reason.key, i.album.name, i.album.artist_names, i.album) for i in snapshot.albums if i.reason.key in keys
    ]
    found += [(i.reason.key, i.name, i.artist_names, i.album) for i in snapshot.tracks if i.reason.key in keys]
    out: dict[str, list[_FailedTitle]] = {}
    for key, name, artists, album in sorted(found, key=lambda f: f[0]):
        item = _FailedTitle(
            plain=normalize_title(album.name),
            stripped=normalize_title(strip_release_qualifiers(album.name)),
            intent_key=key,
            name=name,
            artists=artists,
        )
        for artist in album.artist_names:
            out.setdefault(normalize_name(artist), []).append(item)
    return FailedItems(by_artist=out)
