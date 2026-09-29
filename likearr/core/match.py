"""Deciding which Spotify id belongs to a MusicBrainz artist or release group.

`promote-save` has to cross from MusicBrainz to Spotify. There are three ways to do it, tried in
this order, and this module is the pure decision behind each - no idea where its inputs came from:

1. **The MusicBrainz relationship** (:func:`spotify_id_from_url`). MusicBrainz records a
   "free streaming" URL relationship pointing straight at ``open.spotify.com``. That is an
   editor-curated identity claim, not a guess, so it needs no title comparison at all - only
   parsing and validation.
2. **A UPC search** (:func:`match_album`, step ``album:upc``). A barcode identifies a release;
   a title only describes one.
3. **A title search** (:func:`match_album` / :func:`match_artist`), the conservative fallback.

The bar for tiers 2 and 3 is deliberately high. A wrong match writes the wrong album into the
user's Spotify library, which is worse than reporting a miss, so every rule here fails towards
:class:`MatchResult` with no id and a reason a human can act on:

1. **Artist first.** An album candidate whose main artist credit (the first one Spotify lists)
   does not match is not a candidate at all, whatever its title says: an album where the artist is
   only a secondary credit is someone else's record.
2. **Normalised equality, never similarity.** :func:`likearr.core.normalize.normalize_title` and
   :func:`~likearr.core.normalize.normalize_name` fold case, accents, punctuation and the
   bracketed qualifiers that distinguish pressings ("(Deluxe Edition)", "- Remastered 2011").
   There is no edit distance and no prefix rule: "Ghosts" does not match "Ghosts I-IV". Only an
   edition qualifier may differ (:func:`~likearr.core.normalize.fold_edition_title`): "Blue"
   matches "Blue (Deluxe Edition)" but not "Blue (Live)", "Blue (Acoustic)" or "Blue (Demo)".
3. **A tie is a miss, unless one candidate is the literal title.** Folding "Blue" and
   "Blue (Deluxe Edition)" to the same string is the point of step 2, so when several candidates
   survive it the one whose raw title is literally the one asked for wins. If none is, or more
   than one is, the result is `ambiguous` and the caller reports it.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, replace

from likearr.core.normalize import fold_edition_title, normalize_name, normalize_title
from likearr.models import SpotifyAlbumRef, SpotifyArtistRef

__all__ = [
    "STEP_ALBUM_LINK",
    "STEP_ALBUM_NAME",
    "STEP_ALBUM_UPC",
    "STEP_ARTIST_LINK",
    "STEP_ARTIST_NAME",
    "MatchResult",
    "match_album",
    "match_artist",
    "spotify_id_from_url",
]

STEP_ARTIST_LINK = "artist:mb-rel"
"""The artist came from MusicBrainz's own Spotify URL relationship."""

STEP_ARTIST_NAME = "artist:name"
STEP_ALBUM_LINK = "album:mb-rel"
"""The album came from MusicBrainz's own Spotify URL relationship."""

STEP_ALBUM_UPC = "album:upc"
STEP_ALBUM_NAME = "album:name"

_MAX_NAMED_CANDIDATES = 4
"""How many candidate names an 'ambiguous' reason lists before it says "and N more"."""

_SPOTIFY_HOSTS = frozenset({"open.spotify.com", "play.spotify.com", "spotify.com", "www.spotify.com"})
"""Hosts a Spotify entity link may legitimately use. Anything else is not a Spotify link."""

_SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")
"""Spotify ids are 22 base62 characters. Anything else did not come out of Spotify."""

_LOCALE_SEGMENT = re.compile(r"^intl-[a-z]{2}$")
"""``open.spotify.com/intl-de/album/...`` - a locale prefix Spotify adds to shared links."""


def spotify_id_from_url(url: str, kind: str) -> str | None:
    """The Spotify id a MusicBrainz URL relationship points at, or ``None`` if it is not one.

    `kind` is the entity the caller wants, ``"artist"`` or ``"album"``. A relationship pointing at
    a *different* entity - a track, a playlist, an artist when an album was asked for - returns
    ``None`` and the caller falls through to searching, because a link to the wrong kind of thing
    is not a weaker answer, it is a different question.

    Both spellings MusicBrainz carries are accepted: the web URL (with an optional ``intl-xx``
    locale segment and any query string) and the ``spotify:`` URI.

    >>> spotify_id_from_url("https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb", "artist")
    '4Z8W4fKeB5YxbusRsdQVPb'
    >>> spotify_id_from_url("https://open.spotify.com/track/4Z8W4fKeB5YxbusRsdQVPb", "album") is None
    True
    """
    text = (url or "").strip()
    if not text:
        return None
    if text.lower().startswith("spotify:"):
        parts = text.split(":")
        if len(parts) >= 3 and parts[1].lower() == kind:
            return parts[2] if _SPOTIFY_ID.match(parts[2]) else None
        return None

    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in _SPOTIFY_HOSTS:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if segments and _LOCALE_SEGMENT.match(segments[0]):
        segments = segments[1:]
    if len(segments) < 2 or segments[0].lower() != kind:
        return None
    return segments[1] if _SPOTIFY_ID.match(segments[1]) else None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Either a confident Spotify id with the step that produced it, or a reason it is a miss."""

    spotify_id: str = ""
    step: str = ""
    reason: str = ""
    title: str = ""
    """The matched Spotify album's title, for a name or UPC match; empty otherwise."""
    artists: tuple[str, ...] = ()
    """The matched Spotify album's artists, for a name or UPC match; empty otherwise."""

    @property
    def matched(self) -> bool:
        return bool(self.spotify_id)


def _squash(value: str) -> str:
    """Case-folded, whitespace-collapsed raw text: the literal-title tie-breaker's comparison."""
    return " ".join(value.casefold().split())


def _describe(names: Sequence[str]) -> str:
    shown = list(names[:_MAX_NAMED_CANDIDATES])
    extra = len(names) - len(shown)
    text = ", ".join(repr(n) for n in shown)
    return f"{text} and {extra} more" if extra > 0 else text


def _one_of(
    wanted_raw: str, exact: Sequence[tuple[str, str]], *, step: str, artists: Sequence[tuple[str, ...]] = ()
) -> MatchResult:
    """Turn the surviving ``(id, raw name)`` candidates into a match or an ambiguity.

    One candidate is the answer. Several means they all normalised alike - "Blue" and
    "Blue (Deluxe Edition)" do, which is what the normaliser is for - so the one whose raw text is
    literally the one asked for wins. Anything else is reported rather than guessed at. `artists`,
    when given, is each candidate's credit, recorded on the match with its title.
    """
    credits = list(artists) or [() for _ in exact]
    if len(exact) == 1:
        return MatchResult(spotify_id=exact[0][0], step=step, title=exact[0][1], artists=credits[0])
    literal = [i for i, (_, raw) in enumerate(exact) if _squash(raw) == _squash(wanted_raw)]
    if len(literal) == 1:
        cid, raw = exact[literal[0]]
        return MatchResult(spotify_id=cid, step=f"{step}:literal", title=raw, artists=credits[literal[0]])
    return MatchResult(reason=f"ambiguous: {len(exact)} Spotify matches ({_describe([raw for _, raw in exact])})")


def match_artist(name: str, candidates: Sequence[SpotifyArtistRef]) -> MatchResult:
    """The Spotify artist that *is* `name`, or a reason there isn't exactly one.

    >>> match_artist("Radiohead", [SpotifyArtistRef("abc", "Radiohead")]).spotify_id
    'abc'
    >>> match_artist("Radiohead", [SpotifyArtistRef("abc", "Radiohead Tribute")]).matched
    False
    """
    if not name.strip():
        return MatchResult(reason="no artist name to search for")
    wanted = normalize_name(name)
    usable = [c for c in candidates if c.spotify_id]
    exact = [(c.spotify_id, c.name) for c in usable if normalize_name(c.name) == wanted]
    if not exact:
        return MatchResult(reason=f"no Spotify artist named {name!r} ({len(usable)} search hits)")
    return replace(_one_of(name, exact, step=STEP_ARTIST_NAME), title="")


def match_album(
    artist_name: str,
    title: str,
    candidates: Sequence[SpotifyAlbumRef],
    *,
    step: str,
) -> MatchResult:
    """The Spotify album that *is* `title` by `artist_name`, or a reason there isn't exactly one.

    Candidates are gated on the artist credit first: an album is a candidate only when its main
    (first) credited artist normalises to `artist_name`. Its title must then normalise alike and
    differ from `title` by nothing but edition qualifiers ("(Deluxe Edition)", "- Remastered"), so
    a kept "Blue" never matches a lone "Blue (Live)". `step` is recorded on a match so the plan says
    whether it came from a UPC search or a name search, with the matched title and artists.
    """
    if not title.strip():
        return MatchResult(reason="no album title to search for")
    wanted_artist = normalize_name(artist_name)
    gated = [
        c for c in candidates if c.spotify_id and c.artist_names and normalize_name(c.artist_names[0]) == wanted_artist
    ]
    if not gated:
        if candidates:
            return MatchResult(
                reason=f"no Spotify album credited to {artist_name!r} among {len(candidates)} hits for {title!r}"
            )
        return MatchResult(reason=f"no Spotify album found for {artist_name!r} - {title!r}")

    wanted_title, wanted_edition = normalize_title(title), fold_edition_title(title)
    alike = [c for c in gated if normalize_title(c.name) == wanted_title]
    same = [c for c in alike if fold_edition_title(c.name) == wanted_edition]
    if alike and not same:
        return MatchResult(
            reason=(
                f"no Spotify album titled {title!r} by {artist_name!r}; only other versions: "
                f"{_describe([c.name for c in alike])}"
            )
        )
    exact = [(c.spotify_id, c.name) for c in same]
    if not exact:
        return MatchResult(
            reason=(
                f"no Spotify album titled {title!r} by {artist_name!r}; nearest: {_describe([c.name for c in gated])}"
            )
        )
    return _one_of(title, exact, step=step, artists=[c.artist_names for c in same])
