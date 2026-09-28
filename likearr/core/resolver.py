"""Turn source intents into MusicBrainz release groups.

This is the novel part of likearr and the part most likely to be wrong, so it is pure,
deterministic and versioned by :data:`likearr.models.RESOLVER_VERSION`. Given the same intents
and the same `MetadataLookup` answers it always produces the same resolutions, including the
same `step` and the same `detail`. Nothing here reads the clock: `now` is always a parameter.

The Singles rule (see ``docs/dev/DESIGN.md``) is implemented in :func:`resolve_track`. In short: a
liked song should put the *studio album or EP* the song lives on into Lidarr, not the single,
because singles mostly duplicate a track the album will bring anyway.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from likearr.core.normalize import (
    credits_match,
    fold_title,
    has_remix_marker,
    normalize_name,
    normalize_title,
    strip_release_qualifiers,
)
from likearr.models import (
    LIKED_TRACK_SCOPE_ALBUM,
    LIKED_TRACK_SCOPE_SMALLEST,
    NO_EXCLUSIONS,
    RESOLVER_VERSION,
    VARIOUS_ARTISTS_MBID,
    AlbumIntent,
    ArtistCandidate,
    ArtistIntent,
    ArtistRelation,
    ArtistResolution,
    BarcodeMatch,
    ExclusionRules,
    PrimaryType,
    ReleaseGroup,
    Resolution,
    ResolutionStatus,
    SecondaryType,
    SourceSnapshot,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.ports import ArtistLinks, CatalogueTooLarge, CreditRelations, MetadataError, MetadataLookup

__all__ = [
    "AMBIGUOUS_SAME_NAME_STEP",
    "ARTIST_AMBIGUOUS_NAME_STEP",
    "EXCLUDED_COMPILATION_STEP",
    "EXCLUDED_DENIED_STEP",
    "EXCLUDED_REMIX_STEP",
    "EXCLUDED_STEP_PREFIX",
    "JOINING_RELATIONSHIPS",
    "REMIX_ONLY_STEP",
    "UNAVAILABLE_STEP",
    "ResolveResult",
    "is_excluded",
    "resolve_album",
    "resolve_all",
    "resolve_artist",
    "resolve_track",
]

_VARIOUS_ARTISTS_NAMES = frozenset({normalize_name("Various Artists"), normalize_name("Various")})


@dataclass(slots=True)
class ResolveResult:
    """Everything one resolve pass produced, plus whether the metadata backend misbehaved."""

    artist_resolutions: dict[str, ArtistResolution] = field(default_factory=dict)
    """intent key -> resolution, for followed artists."""
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    """intent key -> resolution, for saved albums and liked/playlist tracks."""
    metadata_errors: int = 0
    """Intents abandoned because the metadata lookup raised. Never zero silently."""
    provisional: set[str] = field(default_factory=set)
    """Intent keys resolved this run while the lookup reported a failure (`resolve_all`'s
    `lookup_failures` moved), whatever the answer. Acted on this run, never cached."""

    @property
    def degraded(self) -> bool:
        """True when at least one intent could not be resolved because of a backend failure."""
        return self.metadata_errors > 0


# --------------------------------------------------------------------------- artists


def _from_links(
    intent: ArtistIntent,
    linked: Sequence[ArtistCandidate],
    known_artist_mbids: frozenset[str],
) -> ArtistResolution:
    """Choose between the MusicBrainz artists a Spotify page links to, or refuse to."""
    chosen = linked[0] if len(linked) == 1 else None
    step = "artist:spotify-url"
    if chosen is None:
        in_library = [c for c in linked if c.mbid in known_artist_mbids]
        if len(in_library) == 1:
            chosen, step = in_library[0], "artist:spotify-url:in-library"
    if chosen is None:
        return ArtistResolution(
            intent_key=intent.reason.key,
            status=ResolutionStatus.UNMAPPED,
            artist_name=intent.name,
            step=ARTIST_AMBIGUOUS_STEP,
            detail=(
                f"MusicBrainz links the Spotify artist {intent.name!r} to {len(linked)} artists and "
                f"none of them is in Lidarr, so which one you mean cannot be decided: "
                f"{'; '.join(c.describe() for c in linked)}. likearr adds nothing new for this follow "
                "until it's settled; anything already monitored stays as it is. Add the right one in "
                "Lidarr, or fix the incorrect link in MusicBrainz, and re-run."
            ),
        )
    return ArtistResolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.RESOLVED,
        artist_mbid=chosen.mbid,
        artist_name=chosen.name or intent.name,
        step=step,
        detail=f"MusicBrainz links the Spotify artist page to {chosen.describe()}",
    )


ARTIST_AMBIGUOUS_STEP = "artist:ambiguous-link"
"""MusicBrainz links this Spotify artist to several artists and none is in the library."""

ARTIST_AMBIGUOUS_NAME_STEP = "artist:ambiguous-name"
"""No link, and several MusicBrainz artists carry exactly the followed artist's name.

Always UNMAPPED, even when one of them is already in Lidarr. MusicBrainz's search score only says
how well a name matched, and every one of these matched it perfectly, so taking the top-scored one
guessed a stranger and pulled their whole studio catalogue into Lidarr. Lidarr holding one of them
is no evidence either: it may be a namesake added by hand, or by an earlier version's guess, and
preferring it would monitor that artist's whole catalogue. Artist answers are worked out afresh
every run, so a link added on MusicBrainz settles it on the next run.
"""


def resolve_artist(
    intent: ArtistIntent,
    lookup: MetadataLookup,
    *,
    links: ArtistLinks | None = None,
    known_artist_mbids: frozenset[str] = frozenset(),
) -> ArtistResolution:
    """Map a followed Spotify artist to a MusicBrainz artist.

    **The Spotify URL relationship first.** MusicBrainz records which artist a given Spotify
    artist page belongs to, and an editor asserting that identity cannot confuse two artists who
    merely share a name. A **name search can**: it can resolve a followed "Lawrence" (the New York
    sibling band) to a German DJ and pull in the DJ's releases, and a followed "Evangeline" (an
    L.A. singer-songwriter) to a Seattle alt-country band - starving the artists the user follows
    while monitoring strangers. Liked tracks are not affected, because they resolve through an
    ISRC to a recording; only this artist-level path was name-based.

    Several linked artists (MusicBrainz carries bad links too - the "Lawrence" page also points
    at an unrelated eurobeat artist) are broken by preferring one already in the Lidarr library,
    which is the artist the user demonstrably has. If that does not decide it, the answer is
    **UNMAPPED**, naming every candidate and its disambiguation: a wrong artist pulls a whole
    catalogue into Lidarr, so not guessing is cheap by comparison.

    Only with no link at all does it fall back to the name search, and that is still conservative:
    a candidate is accepted only when its normalised name is exactly equal to the normalised
    Spotify name, and a near-miss is reported with the candidate in `detail`. Several exact-name
    candidates are namesakes the search score cannot tell apart, so the answer is **UNMAPPED** at
    :data:`ARTIST_AMBIGUOUS_NAME_STEP`, naming each of them. Unlike several links, the library does
    not break the tie here: a link is an editor's claim about this Spotify page, and a name is not.
    """
    if links is not None and intent.spotify_id:
        linked = tuple(links.artists_for_spotify_artist(intent.spotify_id))
        if linked:
            return _from_links(intent, linked, known_artist_mbids)
    found = tuple(lookup.search_artist_candidates(intent.name))
    if not found:
        return ArtistResolution(
            intent_key=intent.reason.key,
            status=ResolutionStatus.UNMAPPED,
            artist_name=intent.name,
            step="artist:search",
            detail=f"MusicBrainz has no artist matching {intent.name!r}",
        )
    want = normalize_name(intent.name)
    exact = [(mbid, name) for mbid, name in found if normalize_name(name) == want]
    if not exact:
        mbid, name = found[0]
        return ArtistResolution(
            intent_key=intent.reason.key,
            status=ResolutionStatus.UNMAPPED,
            artist_name=intent.name,
            step="artist:search",
            detail=(
                f"closest MusicBrainz match for {intent.name!r} is {name!r} ({mbid}), which is not an exact name match"
            ),
        )
    if len(exact) > 1:
        return ArtistResolution(
            intent_key=intent.reason.key,
            status=ResolutionStatus.UNMAPPED,
            artist_name=intent.name,
            step=ARTIST_AMBIGUOUS_NAME_STEP,
            detail=(
                f"MusicBrainz has {len(exact)} artists named {intent.name!r} and no link from the Spotify "
                f"artist to settle it, so which one you mean cannot be decided: "
                f"{'; '.join(f'{n} ({m})' for m, n in exact)}. likearr adds nothing new for this follow "
                "until it's settled; anything already monitored stays as it is. Link the right one to "
                "the Spotify artist on MusicBrainz and re-run, or add them in Lidarr by hand."
            ),
        )
    mbid, name = exact[0]
    return ArtistResolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.RESOLVED,
        artist_mbid=mbid,
        artist_name=name,
        step="artist:search",
        detail=f"{intent.name!r} matched MusicBrainz artist {name!r} ({mbid}) by name",
    )


# --------------------------------------------------------------------------- albums


def _looks_like_various_artists(album: SpotifyAlbumRef) -> bool:
    return any(normalize_name(n) in _VARIOUS_ARTISTS_NAMES for n in album.artist_names)


def _primary_artist(album: SpotifyAlbumRef) -> str:
    """The first credited artist. Featured credits beyond the first are ignored throughout."""
    return album.artist_names[0] if album.artist_names else ""


def _titles_match(mb_title: str, spotify_title: str) -> bool:
    """True when a MusicBrainz release title and a Spotify title name the same release.

    Compared once on the raw strings, and - only if that fails - once more after
    :func:`~likearr.core.normalize.strip_release_qualifiers` on **both** sides. Stripping only
    the Spotify side (the previous behaviour) missed a release group whenever MusicBrainz, not
    Spotify, was the side carrying the decoration - ``"Kangaroo"`` (Spotify) never matched
    ``"Kangaroo EP"`` (MusicBrainz), because only Spotify's already-bare title was ever stripped.
    The raw comparison is tried first and kept unconditionally, because stripping can also
    *break* a match: ``"Elf (Music from the Major Motion Picture)"`` (Spotify) already equals
    MusicBrainz's ``"Elf: Music From the Major Motion Picture"`` under :func:`normalize_title`
    alone, since that phrase is now qualifier vocabulary and stripping it from only one side (the
    only side that has it in brackets at all) would separate them again.
    """
    if normalize_title(mb_title) == normalize_title(spotify_title):
        return True
    return normalize_title(strip_release_qualifiers(mb_title)) == normalize_title(
        strip_release_qualifiers(spotify_title)
    )


AMBIGUOUS_SAME_NAME_STEP = "ambiguous:same-name-artists"
"""Different MusicBrainz artists share the Spotify artist's name *and* the album title.

UNMAPPED, deliberately. The name search cannot tell two same-named artists apart, and the tie-break
it used to fall back on - earliest release date - is no evidence of which one the user meant: it
matched the London band Jungle's "Busy Earnin'" to a US band's 1969 album every time. A liked track
gets one more chance, its ISRC (:func:`_pick_by_isrc`); a saved album has none to give, because its
barcode, the one identifier it carries, was already tried before any name search ran. Like every
UNMAPPED answer it is re-resolved each run, so MusicBrainz gaining the evidence fixes it unaided.
"""


def _named(candidates: Sequence[ReleaseGroup], title: str) -> list[ReleaseGroup]:
    """The name-search candidates whose title passes :func:`_titles_match`, in their given order."""
    return [rg for rg in candidates if _titles_match(rg.title, title)]


def _same_name_detail(artist: str, title: str, candidates: Sequence[ReleaseGroup]) -> str:
    """Name every candidate, so a human can see exactly what likearr declined to choose between."""
    listed = "; ".join(
        f"{rg.title!r} ({rg.mbid}, {rg.first_release_date or 'date unknown'}) by {rg.artist_name} ({rg.artist_mbid})"
        for rg in candidates
    )
    return f"{len(candidates)} different artists named {artist!r} each have a release titled {title!r}: {listed}"


def _title_tier(mb_title: str, album: SpotifyAlbumRef) -> int:
    """How literally a release group's title is the one Spotify printed; the smallest is closest.

    0 when the two fold equal (:func:`~likearr.core.normalize.fold_title`: case, diacritics and
    punctuation only, no qualifier dropped); 1 when MusicBrainz's title folds equal to Spotify's
    with *Spotify's* trailing decorations stripped (``"I'm Ready - EP"`` is MusicBrainz's plain
    ``"I'm Ready"``); 2 for everything else, which is every match that only holds once a qualifier
    is dropped from MusicBrainz's side - the fallback, never ranked above a literal match.

    `_titles_match` rightly calls CRUISR's plain EP *All Over* and the earlier single
    *All Over (Bear//Face Remix)* the same release, because :func:`normalize_title` reads
    "(... Remix)" as a qualifier, and the earliest date then took the remix for a song Spotify
    filed on the plain EP. The same holds for an earlier "X (Live)", "X (Demo)" or "X (Acoustic)"
    against a plain "X", so this is a rule about titles, not about remixes. And it is symmetric: a
    Spotify album that *is* "X (Live)" prefers MusicBrainz's "X (Live)" over a plain "X".
    """
    folded = fold_title(mb_title)
    if folded == fold_title(album.name):
        return 0
    if folded == fold_title(strip_release_qualifiers(album.name)):
        return 1
    return 2


_SAVED_ALBUM_TYPE_RANK = {PrimaryType.ALBUM: 0, PrimaryType.EP: 1, PrimaryType.SINGLE: 2}
"""How well a release group's primary type fits a *saved* album: Album, then EP, then Single, then
anything else (Broadcast, Other, untyped)."""


def _saved_album_fit(rg: ReleaseGroup, album: SpotifyAlbumRef) -> tuple[int, int, int, int, bool, date, str]:
    """Order one artist's same-titled release groups for a saved album; the smallest fits best.

    Album before EP before Single before anything else; a studio release (no secondary type)
    before a Demo, Live or Compilation; then the title Spotify printed over one that only matches
    once a qualifier is dropped (`_title_tier`); then the first-release year closest to
    the year Spotify gives the album, when both are known; only then the earliest date and the
    lowest MBID. Ranking by date and MBID alone sent saved albums to a
    same-titled earlier release by the same artist - Yellowcard's *Lights and Sounds* to its 2005
    lead single, Sublime's *Sublime* to a 1988 demo - once RESOLVER_VERSION 5 made them re-resolve.

    The title tier comes *after* type and studio-ness, not first, because this path had the same
    title-matching flaw only in the narrow case they leave tied: two studio EPs, *The Feeling* and an untagged
    *The Feeling (Remixes)*, went to the earlier. Putting it first would let an exact-titled
    single beat Spotify's "Kangaroo" as MusicBrainz's "Kangaroo EP", which the type rank decides.
    One exception runs before this order, in `_studio_title_first`: a studio release titled exactly
    as Spotify prints it removes every secondary-typed candidate, so a saved EP "X" is
    no longer outranked by the same artist's live Album "X (Live)".
    """
    other = len(_SAVED_ALBUM_TYPE_RANK)
    rank = _SAVED_ALBUM_TYPE_RANK.get(rg.primary_type, other) if rg.primary_type is not None else other
    spotify_year = album.release_date.year if album.release_date else None
    if spotify_year is None:
        distance = 0
    elif rg.first_release_date is None:
        distance = 10_000
    else:
        distance = abs(rg.first_release_date.year - spotify_year)
    return (
        rank,
        1 if rg.secondary_types else 0,
        _title_tier(rg.title, album),
        distance,
        rg.first_release_date is None,
        rg.first_release_date or date.min,
        rg.mbid,
    )


def _studio_title_first(candidates: Sequence[ReleaseGroup], album: SpotifyAlbumRef) -> Sequence[ReleaseGroup]:
    """One artist's candidates for a saved album, without the secondary-typed ones when a studio
    release carries exactly the title Spotify printed.

    `normalize_title` drops "(Live)" and an Album outranks an EP, so a saved EP "X" went to the same
    artist's live Album "X (Live)". A studio release titled as Spotify prints it (`_title_tier` 0)
    therefore comes before every Live, Demo or Compilation release; between studio releases the
    type rank in `_saved_album_fit` still decides, so an exact-titled single never beats the EP
    MusicBrainz calls "Kangaroo EP".
    """
    if any(rg.is_studio and _title_tier(rg.title, album) == 0 for rg in candidates):
        return [rg for rg in candidates if rg.is_studio]
    return candidates


def _per_artist(
    named: Sequence[ReleaseGroup],
    album: SpotifyAlbumRef,
    *,
    saved_album: bool,
    holds_song: Callable[[ReleaseGroup], bool] | None = None,
) -> list[ReleaseGroup]:
    """Each artist's best candidate, in first-seen order.

    A saved album prefers the release that *is* an album (`_saved_album_fit`). A liked or playlist
    track prefers the title Spotify printed (`_title_tier`), then keeps the earliest
    date, then the lowest MBID: the `smallest` scope and the Singles rule reason from that release
    onward and choose among the artist's releases themselves, and changing what they start from was
    not the fix here. Only the choice *within* an artist moves: every artist still gets exactly one
    candidate, so the same-name handling downstream sees the same artists it always did.

    `holds_song` - a track's only, see `_song_evidence` - says whether a release is shown to carry
    the song. It is asked only when one artist has **several** candidates tied in a literal title
    tier (0 or 1), and then one that carries the song beats one that is not shown to, before the
    date. Eminem's "Encore" is an Album (2004-11-12) and a same-titled Single (2004-11-09): the
    title cannot choose, the date chose the Single, and the `smallest` scope then trusted it as the
    release holding "Mockingbird", which it does not. Never asked for tier 2, where the old rule
    stands unchanged, and never above the title tier: CRUISR's ISRC is on the remix single, not on
    the EP Spotify named.
    """
    groups: dict[str, list[ReleaseGroup]] = {}
    for rg in named:
        groups.setdefault(rg.artist_mbid, []).append(rg)
    if saved_album:
        return [
            min(_studio_title_first(gs, album), key=lambda rg: _saved_album_fit(rg, album)) for gs in groups.values()
        ]
    chosen: list[ReleaseGroup] = []
    for gs in groups.values():
        tiers = {rg.mbid: _title_tier(rg.title, album) for rg in gs}
        best = min(tiers.values())
        tied = holds_song is not None and best < 2 and sum(t == best for t in tiers.values()) > 1
        lacking = {rg.mbid for rg in gs if tied and holds_song is not None and not holds_song(rg)}
        chosen.append(min(gs, key=lambda rg: (tiers[rg.mbid], rg.mbid in lacking, *_release_group_order(rg))))
    return chosen


def _with_rivals(survivor: ReleaseGroup, candidates: Sequence[ReleaseGroup]) -> tuple[ReleaseGroup, ...]:
    """The survivor plus one candidate from each *other* artist the name search returned, or
    ``()`` when there is no other artist - the ordinary, uncontested case."""
    rivals: dict[str, ReleaseGroup] = {}
    for rg in candidates:
        if rg.artist_mbid != survivor.artist_mbid:
            rivals.setdefault(rg.artist_mbid, rg)
    return (survivor, *rivals.values()) if rivals else ()


def _pick_by_barcode(
    album: SpotifyAlbumRef, upc: str, matches: Sequence[BarcodeMatch]
) -> tuple[ReleaseGroup | None, str]:
    """The release group a saved album's barcode names, or ``None`` and why not.

    Returns ``(release_group, note)``: the note is appended to a hit's detail, and for a miss it is
    the whole reason, which opens the UNMAPPED detail.

    **One release group** holds the barcode: it is the answer, with no title check. A barcode is an
    exact identifier, and a real album can fail a title comparison with Spotify's own title
    ("Chet Baker in New York" against "In New York [Original Jazz Classics Remasters]"),
    so checking it would undo the fix.

    **Several** release groups hold it - the same pressing filed under an album and under the
    artist's compilation, as "Tease Me" is under "All She Wrote" - so MusicBrainz's result order
    must not choose. Only those whose title passes `_titles_match` stay; then those credited to
    Spotify's primary artist, when any is; then those holding an Official release, when any does.
    Exactly one must remain, or the barcode names nothing and the name search decides.
    """
    if not matches:
        return None, f"no release with barcode {upc}"
    if len(matches) == 1:
        return matches[0].release_group, ""
    titled = [m for m in matches if _titles_match(m.release_group.title, album.name)]
    artist = _primary_artist(album)
    credited = [m for m in titled if credits_match(m.release_group.artist_name, artist)] or titled
    official = [m for m in credited if m.official] or credited
    on = f"barcode {upc} is on {len(matches)} release groups"
    if len(official) == 1:
        return official[0].release_group, f", chosen by its title from the {len(matches)} release groups carrying it"
    if not official:
        return None, f"{on}, none of them titled {album.name!r}"
    return None, f"{on}, and {len(official)} of them are titled {album.name!r}, so none is chosen"


def _map_spotify_album(
    album: SpotifyAlbumRef,
    lookup: MetadataLookup,
    prefix: str,
    *,
    saved_album: bool = False,
    holds_song: Callable[[ReleaseGroup], bool] | None = None,
) -> tuple[ReleaseGroup | None, str, str, tuple[ReleaseGroup, ...]]:
    """Map the release Spotify named to a release group.

    Returns ``(release_group, step, detail, same_name)``. The UPC is tried first because a barcode
    is an exact identifier; the name search is a first-class fallback because Spotify has dropped
    `external_ids` from its responses before and may again. The name search result is accepted
    only on :func:`_titles_match` equality - exact, never containment: a title match on a wrong
    artist is refused elsewhere (the credit gate `search_release_group_candidates` itself
    applies), and loosening this comparison is only ever safe paired with that gate staying exact.

    `same_name` is empty unless the name search returned releases by **more than one** artist - two
    MusicBrainz artists sharing the Spotify name - and it has two shapes:

    One artist's several same-titled releases are chosen between by `_per_artist`, which is where
    `saved_album` matters: a saved album prefers the Album over a same-titled single, EP or demo.
    `holds_song` is a track's evidence for the same choice (see `_per_artist`).

    - **Several survive the title check.** The release group is ``None`` and `same_name` holds
      every survivor, one per artist: which of them Spotify meant is not something a name can
      answer, and the caller decides on better evidence or refuses.
    - **One survives.** The release group is that survivor and `same_name` is the survivor followed
      by the other artists' candidates, which failed the title check. Failing it is not proof they
      are the wrong artist - the right artist's "X (Deluxe 2020)" can fail where a same-named
      stranger's plain "X" passes - so the caller checks the survivor against the track's ISRC
      before trusting it (:func:`_contested_by_isrc`).

    Spotify decorates some album titles with a trailing qualifier MusicBrainz's plain title does
    not carry - ``"The Beatles (Remastered)"``, ``"I'm Ready - EP"`` - which makes the lucene
    phrase search above miss a release group that is really there. Only on failure (no hit, or a
    hit that fails the equality check) is a second search tried against
    :func:`~likearr.core.normalize.strip_release_qualifiers`'s output, so the extra lookup is
    never paid when the raw title already matched.
    """
    missing = "no barcode"
    if album.upc:
        rg, missing = _pick_by_barcode(album, album.upc, lookup.release_groups_by_barcode(album.upc))
        if rg is not None:
            return rg, f"{prefix}:upc", f"barcode {album.upc} is release group {rg.title!r} ({rg.mbid}){missing}", ()
    artist = _primary_artist(album)
    step = f"{prefix}:search"
    candidates = lookup.search_release_group_candidates(artist, album.name)
    named = _named(_per_artist(candidates, album, saved_album=saved_album, holds_song=holds_song), album.name)
    if len(named) > 1:
        return None, step, _same_name_detail(artist, album.name, named), tuple(named)
    if named:
        rg = named[0]
        return (
            rg,
            step,
            f"{artist!r} - {album.name!r} matched release group {rg.title!r} ({rg.mbid}) by name",
            _with_rivals(rg, candidates),
        )

    rg = candidates[0] if candidates else None
    if rg is None:
        detail = f"{missing}; no release group found for {artist!r} - {album.name!r}"
    else:
        detail = (
            f"closest release group for {artist!r} - {album.name!r} is {rg.title!r} ({rg.mbid}), "
            "which is not an exact title match"
        )

    stripped = strip_release_qualifiers(album.name)
    if stripped == album.name:
        return None, step, detail, ()

    candidates2 = lookup.search_release_group_candidates(artist, stripped)
    named2 = _named(_per_artist(candidates2, album, saved_album=saved_album, holds_song=holds_song), stripped)
    if len(named2) > 1:
        return (
            None,
            step,
            f"{detail}; after stripping qualifiers, {_same_name_detail(artist, stripped, named2)}",
            tuple(named2),
        )
    if named2:
        rg2 = named2[0]
        removed = _removed_qualifier(album.name, stripped)
        return (
            rg2,
            step,
            (
                f"{artist!r} - {album.name!r} matched release group {rg2.title!r} ({rg2.mbid}) "
                f"by name after stripping {removed!r}"
            ),
            _with_rivals(rg2, (*candidates, *candidates2)),
        )

    rg2 = candidates2[0] if candidates2 else None
    if rg2 is None:
        retry = f"no release group found for {artist!r} - {stripped!r} either"
    else:
        retry = (
            f"closest release group for {artist!r} - {stripped!r} is {rg2.title!r} ({rg2.mbid}), "
            "which is not an exact title match either"
        )
    return None, step, f"{detail}; retried after stripping qualifiers to {stripped!r}: {retry}", ()


def _removed_qualifier(original: str, stripped: str) -> str:
    """What :func:`~likearr.core.normalize.strip_release_qualifiers` took off, for a match detail.

    Falls back to the whole original title when it is not a clean prefix of `stripped`, which
    should not happen but must never crash a detail message over it.
    """
    if original.startswith(stripped):
        removed = original[len(stripped) :].strip()
        if removed:
            return removed
    return original


def resolve_album(intent: AlbumIntent, lookup: MetadataLookup) -> Resolution:
    """Map a saved Spotify album to its release group.

    A saved album is monitored whatever its type: the user explicitly saved it, so a single or a
    compilation is a legitimate answer here (unlike the Singles rule for liked tracks).
    Various-Artists releases are UNMAPPED: Lidarr files them under a placeholder artist and
    monitoring them is never what the user meant.
    """
    album = intent.album
    key = intent.reason.key
    if _looks_like_various_artists(album):
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.UNMAPPED,
            step="album:various-artists",
            detail=f"{album.name!r} is credited to Various Artists on Spotify; likearr does not monitor compilations",
        )
    rg, step, detail, same_name = _map_spotify_album(album, lookup, "album", saved_album=True)
    if same_name and rg is None:
        # A tie, with no ISRC to consult, and the barcode - the album's own identifier - already
        # missed. (A lone title survivor is taken: a saved album has nothing that could contest it.)
        step = AMBIGUOUS_SAME_NAME_STEP
        detail = f"{detail}; a saved album carries no ISRC to tell them apart, so none is chosen"
    if rg is None:
        return Resolution(intent_key=key, status=ResolutionStatus.UNMAPPED, step=step, detail=detail)
    if rg.is_various_artists:
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.UNMAPPED,
            step="album:various-artists",
            detail=f"{detail}, which MusicBrainz credits to Various Artists",
        )
    return Resolution(
        intent_key=key,
        status=ResolutionStatus.RESOLVED,
        release_group=rg,
        step=step,
        detail=detail,
        source_release_group=rg,
    )


# --------------------------------------------------------------------------- the opt-outs

EXCLUDED_STEP_PREFIX = "track:excluded:"
"""Steps for a track the user has opted out of, and only those.

An excluded track is UNMAPPED, so it is reported exactly as any other unresolved intent is - it
never silently vanishes. The prefix is what tells it apart from a *failure* to resolve, and two
things read it: `core.diff`, which treats an excluded intent's reason as lost so the release it
used to hold is actually unmonitored, and a human scanning `diff.json`.

The distinction matters because the two need opposite handling. A track that failed to map has
not been unliked and its release must stay monitored, or every transient MusicBrainz wobble would
read as a deletion. A track that was *excluded* was excluded by a deterministic rule the user
wrote down, which no outage can produce, so keeping its release monitored would mean the opt-out
never removed anything and the box set stayed in the library for ever.
"""

EXCLUDED_COMPILATION_STEP = f"{EXCLUDED_STEP_PREFIX}compilation"
EXCLUDED_REMIX_STEP = f"{EXCLUDED_STEP_PREFIX}remix"
EXCLUDED_DENIED_STEP = f"{EXCLUDED_STEP_PREFIX}denied"

_REFUSAL_PHRASE = {
    EXCLUDED_COMPILATION_STEP: "is a compilation, and [rules] allow_compilation_fallback is off",
    EXCLUDED_REMIX_STEP: "is a remix release, and [rules] allow_remix_releases is off",
    EXCLUDED_DENIED_STEP: "is on the [rules] deny_releases list",
}


def is_excluded(resolution: Resolution | ArtistResolution) -> bool:
    """True for a resolution the user opted out of, rather than one that failed to resolve."""
    return resolution.step.startswith(EXCLUDED_STEP_PREFIX)


def _is_remix_release(rg: ReleaseGroup) -> bool:
    """A remix record, by MusicBrainz's type **or** by its own title.

    The title half is not belt and braces, it is the half that works. `is_studio` already refuses
    anything carrying the `Remix` secondary type, so a type-only rule would have changed nothing
    at all: the release groups this catches are typed `EP` with **no** secondary types, which is
    exactly why they beat the real album on size under the `smallest` scope.
    """
    return SecondaryType.REMIX in rg.secondary_types or has_remix_marker(rg.title)


class _DenyProbe(frozenset[str]):
    """A `deny_releases` set that remembers which of its members the resolver asked about.

    `resolve_all` resolves each track with the deny list swapped for one of these, so the
    `Resolution` can record every denied release it was refused on the way to its answer
    (`Resolution.denied_skipped`). `_refusal` is the only place the resolver reads the
    deny list, and it reads it by membership, so recording there covers every path without
    threading a collector through each of them. Recording a denied release that did not end up
    mattering costs one extra re-resolve after it is un-denied; missing one would be the bug.
    """

    hits: set[str]

    def __new__(cls, members: frozenset[str]) -> _DenyProbe:
        probe = super().__new__(cls, members)
        probe.hits = set()
        return probe

    def __contains__(self, item: object) -> bool:
        found = super().__contains__(item)
        if found and isinstance(item, str):
            self.hits.add(item)
        return found


def _refusal(rg: ReleaseGroup, rules: ExclusionRules, *, track_is_remix: bool) -> str:
    """The `EXCLUDED_*` step refusing this release group, or `""` when it may be chosen.

    One predicate for all three opt-outs, read in two different ways by design: as a **candidate
    filter** wherever the resolver has a set to choose from, so it falls through to its next-best
    answer, and as a **terminal gate** on the release Spotify named, where there is nothing to
    fall through to and the honest answer is to report the refusal.

    `track_is_remix` suspends the remix rule: a user who liked "Blinding Lights - Chromatics
    Remix" asked for a remix, and refusing every remix release would leave them with nothing.
    """
    if rg.mbid in rules.deny_releases:
        return EXCLUDED_DENIED_STEP
    if not rules.allow_remix_releases and not track_is_remix and _is_remix_release(rg):
        return EXCLUDED_REMIX_STEP
    if not rules.allow_compilation_fallback and SecondaryType.COMPILATION in rg.secondary_types:
        return EXCLUDED_COMPILATION_STEP
    return ""


METADATA_ERROR_STEP = "error:metadata"
"""An intent whose lookup raised `MetadataError`: UNMAPPED for this run only, never cached."""

SINGLE_FALLBACK_STEP = "track:single-fallback"
"""A pending track settled on its single once the fallback window passed. RESOLVED, and the one
answer only a clock can reach: see `_due` for why an expiry must never undo it."""

REMIX_ONLY_STEP = "track:remix-only"
"""A track kept on a remix release because every release that could hold it is one.

RESOLVED, and deliberately *not* under `EXCLUDED_STEP_PREFIX`: the release is monitored. Produced
only while `allow_remix_releases` is off and `keep_remix_only_tracks` is on, and only where the
answer would otherwise have been `EXCLUDED_REMIX_STEP` with every candidate refused for that alone.
"""

_ALBUM_OR_EP = frozenset({PrimaryType.ALBUM, PrimaryType.EP})


def _remix_only(
    intent: TrackIntent,
    rules: ExclusionRules,
    refusal: Resolution,
    *,
    proven: Sequence[ReleaseGroup],
    named: ReleaseGroup | None,
    track_is_remix: bool,
) -> Resolution | None:
    """`refusal` kept after all, on one of the releases it refused, or ``None`` to report it.

    Asked at the one exit where :func:`resolve_track` gives up on an opt-out, with the releases
    each rule refused on the way there: `proven` are the ones the recording is shown to be on -
    the ISRC stand-in's (:func:`_isrc_stand_in`) or the tracklist-checked releases of a joined
    credit (:func:`_related_credit`) - and `named` is the release the name search mapped
    Spotify's album to. A remix is kept only when **every** one of them is refused *only* by the
    remix rule, i.e. allowed once `allow_remix_releases` is read as on. A single denied release or
    compilation among them and the refusal stands, because the setting is about a song with
    nowhere else to go, not about overriding the other two opt-outs. A Various Artists release is
    never kept, and one that is *not* a remix means the song has somewhere else to go - it is
    simply a place likearr never monitors - so the refusal stands then too. That is the Grease
    case: the original "You're the One That I Want" is on the Various Artists
    soundtrack and, among monitorable releases, only on *Grease (The Remix EP)*; the remix rule
    was switched on for exactly that song, and keeping the EP would undo it.

    Which one: a proven release before `named` (a title match with qualifiers dropped is how The
    Knocks' "Learn To Fly" reached *The Feeling (TheFatRat Remix)*, a release that does not carry
    the song at all), then one without MusicBrainz's `Remix` secondary type (no Lidarr metadata
    profile likearr sets up allows that type, so such a release never reaches the catalogue), then
    an Album or EP before anything smaller, then `_earliest`'s order.
    """
    if not rules.keep_remix_only_tracks or rules.allow_remix_releases or track_is_remix:
        return None
    relaxed = replace(rules, allow_remix_releases=True)
    ranked: dict[str, tuple[int, ReleaseGroup]] = {}
    for tier, group in ((0, proven), (1, (named,) if named is not None else ())):
        for rg in group:
            if rg.is_various_artists:
                if not _is_remix_release(rg):
                    return None  # the song has a home that is not a remix; it is just never monitored
                continue
            if _refusal(rg, rules, track_is_remix=False) != EXCLUDED_REMIX_STEP or _refusal(
                rg, relaxed, track_is_remix=False
            ):
                return None
            ranked.setdefault(rg.mbid, (tier, rg))
    if not ranked:
        return None

    def order(entry: tuple[int, ReleaseGroup]) -> tuple[int, bool, bool, bool, date, str]:
        tier, rg = entry
        return (
            tier,
            SecondaryType.REMIX in rg.secondary_types,
            rg.primary_type not in _ALBUM_OR_EP,
            *_release_group_order(rg),
        )

    ordered = [rg for _tier, rg in sorted(ranked.values(), key=order)]
    chosen = ordered[0]
    considered = ", ".join(f"{rg.title!r} ({rg.mbid}, {_describe(rg)})" for rg in ordered)
    return Resolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.RESOLVED,
        release_group=chosen,
        step=REMIX_ONLY_STEP,
        detail=(
            f"{intent.name!r} is kept on the remix {_describe(chosen)} {chosen.title!r} ({chosen.mbid}), because "
            f"it is the only release for a song you saved: every release that could hold it is a remix, and "
            f"[rules] keep_remix_only_tracks keeps such a song although allow_remix_releases is off; "
            f"{len(ranked)} candidate(s) considered: {considered}. Without it: {refusal.detail}"
        ),
        source_release_group=named or chosen,
    )


def _excluded(intent: TrackIntent, step: str, rg: ReleaseGroup, detail: str, *, map_detail: str = "") -> Resolution:
    """The one place an opted-out track becomes a `Resolution`, so every one reads the same."""
    tail = f"; {map_detail}" if map_detail else ""
    return Resolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.UNMAPPED,
        step=step,
        detail=(
            f"{intent.name!r} would have been monitored on {_describe(rg)} {rg.title!r} "
            f"({rg.mbid}), which {_REFUSAL_PHRASE[step]}. {detail}{tail}"
        ),
        source_release_group=rg,
    )


# --------------------------------------------------------------------------- tracks


def _earliest(candidates: list[ReleaseGroup]) -> ReleaseGroup:
    """Earliest first release date wins; an unknown date sorts last; MBID breaks the tie."""
    return min(
        candidates,
        key=lambda rg: (rg.first_release_date is None, rg.first_release_date or date.min, rg.mbid),
    )


def _days_since(now: datetime, then: datetime) -> int | None:
    """Whole days between two datetimes, or None when their awareness does not match."""
    try:
        return (now - then).days
    except TypeError:
        return None


# ----------------------------------------------------------------- the `smallest` liked-track scope

SMALLEST_STEP_PREFIX = "track:smallest:"
"""Steps produced by :func:`_resolve_smallest`, and only by it."""

COVERED_BY_FOLLOW_STEP = "track:smallest:covered-by-follow"
"""The one `smallest` step that is only ever produced for a **followed** artist.

Which is what makes a cached resolution's step readable as evidence of the follow state it was
made under, and so what lets :func:`_reusable` back-fill an unrecorded `Resolution.followed`
instead of paying a re-resolution for it.
"""

_SIZE_RANK = {PrimaryType.SINGLE: 0, PrimaryType.EP: 1, PrimaryType.ALBUM: 2}
"""How small an official studio release is. A smaller number wins under ``liked_track_scope = "smallest"``."""


def _size_key(rg: ReleaseGroup) -> tuple[int, bool, date, str]:
    """Order for the `smallest` scope: Single < EP < Album, then earliest date, then MBID.

    Anything else sorts last, which by itself is what keeps a compilation or a live album from
    ever beating a studio release; `_is_smallest_candidate` then refuses it outright.
    """
    primary = rg.primary_type
    rank = _SIZE_RANK[primary] if primary is not None and primary in _SIZE_RANK else len(_SIZE_RANK)
    return (rank, rg.first_release_date is None, rg.first_release_date or date.min, rg.mbid)


def _is_smallest_candidate(rg: ReleaseGroup, artist_mbid: str, rules: ExclusionRules, *, track_is_remix: bool) -> bool:
    """Whether a release group may be chosen by the `smallest` scope.

    Only *studio* Singles, EPs and Albums by this exact primary artist qualify. A secondary-typed
    release (a compilation, a live album, a remix record) ranks after every studio release by
    construction, so the only way one could ever be chosen is if there were no studio release at
    all - and in that case the `album` scope's own non-studio handling (``track:non-studio``,
    ``track:various-artists``) is the better answer, so `smallest` defers to it instead. Various
    Artists is never a candidate for the same reason it is never monitored elsewhere.

    That type check is also why this scope needed the remix rule, since the `Remix` secondary
    type did not give it: ``Grease (The Remix EP)`` is typed `EP` with no secondary types, so it
    is "studio" here, and an EP outranks an Album, so a remix record containing the original
    recording beat the film soundtrack on size. `_refusal` is what refuses it now.
    """
    if _refusal(rg, rules, track_is_remix=track_is_remix):
        return False
    return (
        rg.artist_mbid == artist_mbid and not rg.is_various_artists and rg.is_studio and rg.primary_type in _SIZE_RANK
    )


def _smallest_candidates(
    intent: TrackIntent,
    lookup: MetadataLookup,
    spotify_rg: ReleaseGroup,
    artist_mbid: str,
    rules: ExclusionRules,
    *,
    track_is_remix: bool,
) -> tuple[list[ReleaseGroup], str]:
    """Every release the `smallest` scope may pick from, deduplicated, in discovery order, and
    `_isrc_release_groups`'s note on what it left out.

    Two sources: the release Spotify named (already mapped by UPC or name) and every release group
    MusicBrainz files the track's ISRC under. The ISRC search is paid on every liked track here,
    unlike the `album` scope which skips it whenever Spotify already named a studio Album/EP -
    there is no way to find a smaller release without looking for one.

    A release the user opted out of is dropped from this set rather than vetoing the answer, so
    the scope simply picks the next smallest release instead. Only when the set empties does the
    `album` scope's own handling take over, exactly as it does for a song no studio release holds.
    """
    found, other_songs = _isrc_release_groups(intent, lookup)
    candidates: dict[str, ReleaseGroup] = {}
    for rg in (spotify_rg, *found):
        if rg.mbid not in candidates and _is_smallest_candidate(rg, artist_mbid, rules, track_is_remix=track_is_remix):
            candidates[rg.mbid] = rg
    return list(candidates.values()), other_songs


_TITLE_FILLER = frozenset(
    {"a", "an", "and", "at", "by", "for", "from", "i", "in", "is", "it", "me", "my", "of", "on", "the", "to", "you"}
)
"""Words too common to show two song titles are the same song: "Kiss Me" and "Talk To Me" share
only "me". Anything else shared counts, and so do the words of a title made only of these."""


def _title_words(title: str) -> frozenset[str]:
    """The words of a song title that can tell it apart, after `normalize_title`."""
    words = normalize_title(title).split()
    significant = frozenset(w for w in words if w not in _TITLE_FILLER)
    return significant or frozenset(words)


def _isrc_release_groups(intent: TrackIntent, lookup: MetadataLookup) -> tuple[list[ReleaseGroup], str]:
    """The release groups of this track's ISRC, without recordings of clearly another song.

    MusicBrainz sometimes files one ISRC on two of an artist's recordings - Dean Martin's "Good
    Mornin' Life" and "Kiss" share ``USCA29600867`` - and the release groups of both then read as
    homes of the liked song: the `smallest` scope took the 1952 "Kiss" single, which does not hold
    it. So when the ISRC's recordings carry **different** titles, a recording whose title shares no
    word with the liked song's (`_title_words`) is left out. A spelling or subtitle variant shares
    one ("Feelin' Alright" / "Feeling Alright"), and a strict equality rule would have lost
    the album filed only under the variant. An untitled recording is always kept, and if every
    titled one would go, all are kept: then the titles cannot tell anything apart.

    Returns ``(release_groups, note)``; the note names what was left out, for the detail, and is
    ``""`` when nothing was. Only the two steps that pick a release *for the song* by its ISRC use
    this - the `smallest` scope and ``track:isrc->album``; the other ISRC checks are unchanged.
    """
    if not intent.isrc:
        return [], ""
    recordings = lookup.recordings_for_isrc(intent.isrc)
    kept = list(recordings)
    if len({normalize_title(r.title) for r in recordings if r.title.strip()}) > 1:
        wanted = _title_words(intent.name)
        other = [r for r in recordings if r.title.strip() and not (_title_words(r.title) & wanted)]
        if len(other) < sum(1 for r in recordings if r.title.strip()):
            kept = [r for r in recordings if r not in other]
    groups: dict[str, ReleaseGroup] = {}
    for recording in kept:
        for rg in recording.release_groups:
            groups.setdefault(rg.mbid, rg)
    dropped = sorted({r.title for r in recordings if r not in kept})
    note = ""
    if dropped:
        titles = ", ".join(repr(t) for t in dropped)
        note = f"ISRC {intent.isrc} is also on a recording of {titles}, another song, whose releases are left out"
    return list(groups.values()), note


def _resolve_smallest(
    intent: TrackIntent,
    lookup: MetadataLookup,
    *,
    spotify_rg: ReleaseGroup,
    artist_mbid: str | None,
    followed_artist_mbids: frozenset[str],
    rules: ExclusionRules,
    track_is_remix: bool,
) -> Resolution | None:
    """The `smallest` scope: the smallest official release that holds this song.

    Returns None when no candidate qualifies, which is the caller's signal to fall back to the
    `album` scope. `smallest` therefore never resolves *less* than `album` would.

    The dedupe rule: when the song's artist is followed, likearr already monitors that artist's
    whole studio Album/EP catalogue, so resolving the like to the single as well would mean the
    same song twice on disk for one like. A song that is on a studio Album/EP by a followed
    artist resolves to that release (``track:smallest:covered-by-follow``) instead.
    """
    if artist_mbid is None or artist_mbid == VARIOUS_ARTISTS_MBID:
        return None
    candidates, other_songs = _smallest_candidates(
        intent, lookup, spotify_rg, artist_mbid, rules, track_is_remix=track_is_remix
    )
    if not candidates:
        return None

    covered = [rg for rg in candidates if rg.is_studio_album_or_ep]
    if covered and artist_mbid in followed_artist_mbids:
        chosen, step = min(covered, key=_size_key), COVERED_BY_FOLLOW_STEP
        picked = (
            f"{chosen.artist_name!r} is a followed artist, so the studio {chosen.primary_type} "
            f"{chosen.title!r} ({chosen.mbid}) already covers the song and no single is added"
        )
    else:
        chosen = min(candidates, key=_size_key)
        step = f"track:smallest:{str(chosen.primary_type).lower()}"
        picked = (
            f"the smallest release holding it is the {str(chosen.primary_type).lower()} "
            f"{chosen.title!r} ({chosen.mbid})"
        )
    alternatives = ", ".join(
        f"{rg.title!r} ({rg.mbid}, {rg.primary_type}, {rg.first_release_date or 'date unknown'})"
        for rg in sorted(candidates, key=_size_key)
    )
    return Resolution(
        intent_key=intent.reason.key,
        status=ResolutionStatus.RESOLVED,
        release_group=chosen,
        step=step,
        detail=(
            f"Spotify filed {intent.name!r} on {_describe(spotify_rg)} {spotify_rg.title!r} "
            f"({spotify_rg.mbid}); {picked}; {len(candidates)} candidate(s) considered: {alternatives}"
            + (f"; {other_songs}" if other_songs else "")
        ),
        source_release_group=spotify_rg,
    )


def _pick_by_isrc(
    intent: TrackIntent, lookup: MetadataLookup, same_name: Sequence[ReleaseGroup]
) -> tuple[ReleaseGroup | None, str]:
    """Choose between same-named artists' same-titled releases by the track's own ISRC.

    The name search found one release per artist and no way to tell the artists apart. The ISRC
    identifies *this recording*, and MusicBrainz files it under release groups credited to the
    artist who made it, so a candidate whose artist holds the ISRC is the one Spotify meant. That
    is evidence about the track; the release date that used to decide this was evidence about
    nothing, which is how the London band Jungle's "Busy Earnin'" kept landing on a US band's 1969
    album.

    Returns ``(release_group, why)``. Exactly one candidate artist must hold the ISRC. None of
    them (no ISRC, MusicBrainz does not know it, or only a third artist's release carries it) or
    more than one is no answer, and the release group is ``None``: correctness beats completeness,
    and an ambiguous intent is reported rather than guessed. Paid only in this rare case, and the
    ISRC search is cached and asked again by the `smallest` scope anyway.
    """
    if not intent.isrc:
        return None, "the track has no ISRC to tell them apart, so none is chosen"
    hits = lookup.release_groups_for_isrc(intent.isrc)
    holders = {rg.artist_mbid for rg in hits}
    theirs = [rg for rg in same_name if rg.artist_mbid in holders]
    searched = f"ISRC {intent.isrc} matched {len(hits)} release group(s)"
    if len(theirs) == 1:
        chosen = theirs[0]
        return chosen, (
            f"{searched}, by {chosen.artist_name} ({chosen.artist_mbid}) and none of the others, "
            f"so the release is {chosen.title!r} ({chosen.mbid})"
        )
    if not theirs:
        return None, f"{searched}, none of them by any of these artists, so none is chosen"
    return None, f"{searched}, by {len(theirs)} of these artists, so none is chosen"


def _contested_by_isrc(intent: TrackIntent, lookup: MetadataLookup, same_name: Sequence[ReleaseGroup]) -> str:
    """Why the lone title survivor ``same_name[0]`` must not be taken, or ``""`` when it may.

    The name search returned several same-named artists and only one passed the title check. That
    one is contested only when the ISRC says **another of those artists** made the recording and
    the survivor's artist did not: the right artist's "X (Deluxe 2020)" can fail a title check a
    stranger's plain "X" passes. No ISRC, an ISRC MusicBrainz does not know, or one only a third
    artist carries (a compilation, typically) is no evidence against the survivor, which then
    stands on its title match exactly as an uncontested one does.
    """
    if not intent.isrc:
        return ""
    survivor, *rivals = same_name
    holders = {rg.artist_mbid for rg in lookup.release_groups_for_isrc(intent.isrc)}
    named_by_isrc = [rg for rg in rivals if rg.artist_mbid in holders]
    if survivor.artist_mbid in holders or not named_by_isrc:
        return ""
    other = named_by_isrc[0]
    return (
        f"but ISRC {intent.isrc} is on releases by {other.artist_name} ({other.artist_mbid}), a different "
        f"artist of the same name whose {other.title!r} ({other.mbid}) did not pass the title check, and not "
        f"by {survivor.artist_name} ({survivor.artist_mbid}), so that match is not taken"
    )


def _isrc_stand_in(
    intent: TrackIntent, lookup: MetadataLookup, rules: ExclusionRules, *, track_is_remix: bool
) -> tuple[ReleaseGroup | None, str, Resolution | None, tuple[ReleaseGroup, ...]]:
    """The release group to reason about when mapping the album Spotify named has failed.

    Spotify's track objects never carry the album's UPC, so a liked or playlist track reaches
    :func:`_map_spotify_album` with nothing but a title and a credit to search on - and on a large
    library hundreds of tracks can end UNMAPPED at ``track:album:search`` for want of an exact match.
    The track's own **ISRC** is a real identifier and was never consulted, because
    every step that reads it runs *after* the album has mapped.

    Returns ``(release_group, why, excluded, refused)``; the caller uses the release group exactly as if
    :func:`_map_spotify_album` had produced it, so every rule after this point - the
    `liked_track_scope`, the same-primary-artist check, the Various Artists refusal, the
    deterministic tie-breaks - applies unchanged. `why` is appended to the mapping detail either
    way, so a track that is still UNMAPPED says what the fallback tried.

    `excluded` is a ready-made `track:excluded:*` resolution, set only when every candidate that
    survived the Various Artists filter was then refused by an opt-out. Without it a
    track whose only homes are all box sets would report the *mapping* failure that preceded the
    fallback, which says nothing about the user's own setting being what refused it. The refused
    candidates are filtered rather than vetoed, so a set holding one allowed release and forty
    box sets still resolves to the allowed one. `refused` travels with it: the refused candidates
    either tier below would have accepted - the Spotify title, or the track's own artist by credit
    name, so a stranger's release carrying the recording is not among them - plus every Various
    Artists hit, which is never kept but can show the song has a home that is not a remix, for
    :func:`_remix_only` to decide on. Otherwise empty, except when every hit is
    credited to Various Artists: those are returned for the same reason.

    Two tiers, both over release groups MusicBrainz files this exact recording under, Various
    Artists excluded (Lidarr files those under a placeholder artist, so they are never monitored):

    1. **The release Spotify named.** A candidate whose normalised title equals the Spotify album
       title, or the title with its release qualifiers stripped. The ISRC already proves the
       candidate holds *this* recording, so the title identifies it whatever name the two
       catalogues print for the credit - which is the common case: MusicBrainz credits John
       Mayer's "TRY! - Live In Concert" to "John Mayer Trio", and that mismatch alone is what
       made the name search refuse a release group it had already found.
    2. **The track's own artist.** No candidate carries that title, so fall back to the artist
       the track is credited to, found by name - the same move :func:`_track_artist_mbid` makes
       for a Various Artists compilation. A studio Album/EP wins, then the earliest release date,
       then the lowest MBID.

    Nothing is guessed: with no ISRC, no candidate, no artist and no match the answer is ``None``
    and the track stays exactly as UNMAPPED as it was before.
    """
    if not intent.isrc:
        return None, "and the track has no ISRC to fall back on", None, ()

    hits = lookup.release_groups_for_isrc(intent.isrc)
    theirs = [rg for rg in hits if not rg.is_various_artists]
    found = [rg for rg in theirs if not _refusal(rg, rules, track_is_remix=track_is_remix)]
    searched = f"ISRC {intent.isrc} matched {len(hits)} release group(s)"
    titles = {normalize_title(intent.album.name), normalize_title(strip_release_qualifiers(intent.album.name))}
    artist_name = intent.artist_names[0] if intent.artist_names else ""
    if not found:
        if theirs:
            # Every candidate was refused by an opt-out. Report the earliest one by name: it is
            # the release this track WOULD have been monitored on, and the user needs to see it
            # to judge whether the setting is doing what they wanted.
            refused = _earliest(theirs)
            step = _refusal(refused, rules, track_is_remix=track_is_remix)
            ours = tuple(
                rg
                for rg in hits
                if rg.is_various_artists
                or normalize_title(rg.title) in titles
                or credits_match(rg.artist_name, artist_name)
            )
            return (
                None,
                f"and {searched}, every one of them refused by an opt-out",
                _excluded(
                    intent,
                    step,
                    refused,
                    f"{searched}, and all {len(theirs)} of them are refused the same way",
                ),
                ours,
            )
        dropped = ", all of them credited to Various Artists" if hits else ""
        # Every hit is Various Artists: never monitored, but a non-remix one is still a home for
        # the song, which `_remix_only` must see when the release Spotify named was refused.
        return None, f"and {searched}{dropped}", None, tuple(hits)

    named = [rg for rg in found if normalize_title(rg.title) in titles]
    if named:
        chosen = _earliest(named)
        return (
            chosen,
            (
                f"but {searched}, one of them {chosen.title!r} ({chosen.mbid}), "
                f"credited to {chosen.artist_name!r} - the release Spotify named"
            ),
            None,
            (),
        )

    hit = lookup.search_artist(artist_name) if artist_name else None
    artist_mbid = hit[0] if hit else None
    if artist_mbid is None or artist_mbid == VARIOUS_ARTISTS_MBID:
        return (
            None,
            (
                f"and {searched}, none carrying that title, with no MusicBrainz artist "
                f"for {artist_name!r} to check the rest against"
            ),
            None,
            (),
        )
    mine = [rg for rg in found if rg.artist_mbid == artist_mbid]
    if not mine:
        return None, f"and {searched}, none of them carrying that title or credited to {artist_name!r}", None, ()

    # A studio Album/EP first, then `_earliest`'s deterministic order within whichever set wins.
    chosen = _earliest([rg for rg in mine if rg.is_studio_album_or_ep] or mine)
    return (
        chosen,
        f"but {searched}, one of them {chosen.title!r} ({chosen.mbid}), by {artist_name!r}, the track's own artist",
        None,
        (),
    )


JOINING_RELATIONSHIPS = frozenset({"member of band", "collaboration"})
"""The MusicBrainz artist-artist relationship types that make two credits one act.

``member of band`` (John Mayer is a member of John Mayer Trio) and ``collaboration`` (an artist is
one of those behind a named project) say the two artists made the music together. No other type
does: ``sibling`` joins Clyde Lawrence to Gracie Lawrence, not to her records, and ``tribute`` or
``supporting musician`` join two acts that are plainly not one. A type MusicBrainz renames stops
matching, which fails closed: the intent stays UNMAPPED, as it was before this rule.
"""

RELATED_CREDIT_STEP = "track:album:related-credit"
"""How the release Spotify named was mapped when a relationship decided the credit.

Never a resolution's final `step` - the Singles rule decides that from the release group, as it
does after the ISRC stand-in - so it is recorded in the detail, which always says which
relationship the answer rests on.
"""


def _joining_relations(
    relations: Sequence[ArtistRelation], credited_mbid: str, spotify_credit: str
) -> list[ArtistRelation]:
    """The relationships joining the credited artist to an artist Spotify credits, one per artist.

    One of `JOINING_RELATIONSHIPS`, in either direction, to an artist whose name is the Spotify
    credit under :func:`~likearr.core.normalize.credits_match` - full equality, never containment,
    exactly the comparison the name search's credit gate makes. The name only says *which* of the
    credited artist's relations is the Spotify credit; that the two are one act is what
    MusicBrainz's relationship asserts. A relation back to the credited artist itself is ignored.

    Deduplicated by the related artist's MBID, so the caller can tell one joined artist (the
    answer) from **two different artists who both carry Spotify's name** (doubt: which of them is
    Spotify's is exactly what a name cannot say).
    """
    found: dict[str, ArtistRelation] = {}
    for relation in relations:
        if (
            relation.relationship in JOINING_RELATIONSHIPS
            and relation.artist_mbid
            and relation.artist_mbid != credited_mbid
            and credits_match(relation.artist_name, spotify_credit)
        ):
            found.setdefault(relation.artist_mbid, relation)
    return list(found.values())


def _isrc_names_another(intent: TrackIntent, lookup: MetadataLookup, spotify_credit: str, joined_mbid: str) -> str:
    """Why the track's ISRC contradicts the joined artist, or ``""`` when it does not.

    The ISRC stand-in already asked MusicBrainz for this ISRC, so this is a cache hit. If it files
    the recording under an artist carrying Spotify's name whose MBID is *not* the related artist's,
    the Spotify credit is somebody else of that name, and the relationship is about the wrong one.
    Only that contradicts: an ISRC MusicBrainz does not know (Try!'s case) says nothing.
    """
    if not intent.isrc:
        return ""
    for rg in lookup.release_groups_for_isrc(intent.isrc):
        if rg.artist_mbid and rg.artist_mbid != joined_mbid and credits_match(rg.artist_name, spotify_credit):
            return (
                f"ISRC {intent.isrc} is on {rg.title!r} ({rg.mbid}) by {rg.artist_name!r} ({rg.artist_mbid}), "
                f"a different artist of that name, so none is chosen"
            )
    return ""


def _related_credit(
    intent: TrackIntent,
    lookup: MetadataLookup,
    relations: CreditRelations,
    rules: ExclusionRules,
    *,
    track_is_remix: bool,
) -> tuple[ReleaseGroup | None, str, Resolution | None, tuple[ReleaseGroup, ...]]:
    """The release Spotify named, found under a credit MusicBrainz joins to Spotify's.

    Asked only once the name search and the ISRC stand-in have both failed, so it can change no
    answer they reach. Spotify credits *TRY! - Live In Concert* to "John Mayer" and MusicBrainz
    holds *Try!* under "John Mayer Trio", a different artist: the name search found it with a
    perfect score and refused it on the credit, and MusicBrainz knows no ISRC for it.

    A release group whose title matches but whose credit differs is taken only when all of these
    hold, and otherwise nothing is chosen:

    - MusicBrainz records a `JOINING_RELATIONSHIPS` relationship between its credited artist and
      exactly one artist whose name *is* Spotify's credit (:func:`_joining_relations`). **Never on
      the names alone**: containment (``"Lawrence"`` inside ``"Clyde Lawrence"``) is the same
      string shape as ``"Lawrence"`` inside ``"Lawrence Welk"``, and the name-collision guard cannot
      catch a containment mistake, because by construction the two names are not equal.
    - Exactly one credited artist is joined, and every candidate artist's relationships could be
      read. An unreadable one might be a second joined artist, so it is doubt, not "no".
    - The track's ISRC does not name a different artist of Spotify's name (:func:`_isrc_names_another`).
    - **The liked song is on the record**: its normalised title is in the release group's
      tracklist, folded exactly as the title fallback folds it. A title alone is weak evidence
      under a different credit - a related act's "Live" or "Greatest Hits" is not this record.

    The search results are the ones `_map_spotify_album` already asked for - the title as Spotify
    gives it, then with its qualifiers stripped - read from the lookup's cache and never the
    network. One relationship lookup is paid per distinct credited artist with a matching title,
    one tracklist per release of the joined artist, and nothing at all when no other credit carries
    the title, which is the case for almost every UNMAPPED track.

    Returns ``(release_group, why, excluded, refused)`` like :func:`_isrc_stand_in`, `refused` being
    the refused releases that hold the song whenever `excluded` is set. `why` is ``""`` when
    no other credit carries the title, so the detail of such a track does not change. Among the
    joined artist's releases holding the song the earliest wins, as the name search's do for a
    track. One an opt-out refuses is dropped, and if that empties the set the answer is the
    ``track:excluded:*`` resolution naming it.
    """
    album = intent.album
    spotify_credit = _primary_artist(album)
    if not spotify_credit.strip() or _looks_like_various_artists(album):
        # Nobody can be a member of "Various Artists", and asking would only cost requests.
        return None, "", None, ()
    stripped = strip_release_qualifiers(album.name)
    titles = (album.name, stripped) if stripped != album.name else (album.name,)
    found: list[ReleaseGroup] = []
    for title in titles:
        found = [
            rg
            for rg in _named(relations.release_groups_under_other_credits(spotify_credit, title), title)
            if rg.artist_mbid and not rg.is_various_artists
        ]
        if found:
            break
    if not found:
        return None, "", None, ()

    credited: dict[str, str] = {}
    for rg in found:
        credited.setdefault(rg.artist_mbid, rg.artist_name)
    listed = ", ".join(f"{name!r} ({mbid})" for mbid, name in credited.items())
    seen = f"MusicBrainz holds {album.name!r} under {listed}, not {spotify_credit!r}"

    joined: dict[str, ArtistRelation] = {}
    for mbid in credited:
        known = relations.artist_relations(mbid)
        if known is None:
            return (
                None,
                f"{seen}; the relationships of {credited[mbid]!r} ({mbid}) could not be read, so none is chosen",
                None,
                (),
            )
        matches = _joining_relations(known, mbid, spotify_credit)
        if len(matches) > 1:
            names = ", ".join(f"{r.artist_name!r} ({r.artist_mbid})" for r in matches)
            return (
                None,
                f"{seen}; {credited[mbid]!r} is related to {len(matches)} artists of that name "
                f"({names}), so none is chosen",
                None,
                (),
            )
        if matches:
            joined[mbid] = matches[0]

    kinds = " or ".join(repr(k) for k in sorted(JOINING_RELATIONSHIPS))
    if not joined:
        return None, f"{seen}, and records no {kinds} relationship joining them to {spotify_credit!r}", None, ()
    if len(joined) > 1:
        return None, f"{seen}, and {len(joined)} of them are related to {spotify_credit!r}, so none is chosen", None, ()

    ((mbid, relation),) = joined.items()
    why = (
        f"{seen}; MusicBrainz records a {relation.relationship!r} relationship between "
        f"{credited[mbid]!r} ({mbid}) and {relation.artist_name!r} ({relation.artist_mbid})"
    )
    contradicted = _isrc_names_another(intent, lookup, spotify_credit, relation.artist_mbid)
    if contradicted:
        return None, f"{why}, but {contradicted}", None, ()

    wanted = normalize_title(intent.name)
    theirs = sorted((rg for rg in found if rg.artist_mbid == mbid), key=_release_group_order)
    holding = [
        rg for rg in theirs if wanted in {normalize_title(t) for t in lookup.release_group_track_titles(rg.mbid)}
    ]
    if not holding:
        return None, f"{why}, but no tracklist there lists {intent.name!r}, so none is chosen", None, ()
    why = f"{why}, and its tracklist lists {intent.name!r}, so the release is taken under {credited[mbid]!r}"
    allowed = [rg for rg in holding if not _refusal(rg, rules, track_is_remix=track_is_remix)]
    if not allowed:
        refused = holding[0]
        step = _refusal(refused, rules, track_is_remix=track_is_remix)
        return None, why, _excluded(intent, step, refused, why), tuple(holding)
    chosen = allowed[0]
    return chosen, f"{why}: {chosen.title!r} ({chosen.mbid})", None, ()


def _release_group_order(rg: ReleaseGroup) -> tuple[bool, date, str]:
    """`_earliest`'s order as a sort key: earliest date, unknown dates last, then MBID."""
    return (rg.first_release_date is None, rg.first_release_date or date.min, rg.mbid)


def _track_artist_mbid(intent: TrackIntent, spotify_rg: ReleaseGroup, lookup: MetadataLookup) -> tuple[str | None, str]:
    """Whose studio releases could hold this song? Returns ``(artist_mbid, withheld)``.

    Normally the mapped release's primary artist. On a Various Artists compilation that credit is
    the VA placeholder, whose "catalogue" is millions of release groups (paging through them would
    take hours) - so use the TRACK's own artist instead. Costs lookups only in that
    Various Artists case, and names an artist only on evidence:

    1. **The ISRC.** The release groups MusicBrainz files this recording under, Various Artists
       ones aside, credited to Spotify's artist name (`credits_match`): when they are all one
       artist, that is the artist. It is the evidence `_pick_by_isrc` trusts, and the call is
       cached for the ISRC step that follows.
    2. **The name, only when it is unambiguous.** Exactly one MusicBrainz artist of that name.
       Several are namesakes - "Evangeline" is a Seattle band the search ranks first and an L.A.
       singer it ranks third - and taking the top one let a stranger's same-titled song win the
       title fallback, putting a stranger's album in Lidarr.
    3. Otherwise ``None``, and `withheld` says why: the track stays UNMAPPED at
       ``track:various-artists``, which is the honest answer for a song found only there.
    """
    if not spotify_rg.is_various_artists:
        return spotify_rg.artist_mbid, ""
    name = intent.artist_names[0] if intent.artist_names else ""
    if not name.strip():
        return None, ""
    if intent.isrc:
        by_isrc = {
            rg.artist_mbid
            for rg in lookup.release_groups_for_isrc(intent.isrc)
            if rg.artist_mbid and not rg.is_various_artists and credits_match(rg.artist_name, name)
        }
        if len(by_isrc) == 1:
            return next(iter(by_isrc)), ""
    found = lookup.search_artist_candidates(name)
    if len(found) == 1:
        return found[0][0], ""
    if not found:
        return None, ""
    evidence = f"ISRC {intent.isrc} does not say which" if intent.isrc else "the track has no ISRC to say which"
    return None, (
        f"{len(found)} MusicBrainz artists are named {name!r} and {evidence}, so none of their albums is searched"
    )


def _song_evidence(intent: TrackIntent, lookup: MetadataLookup) -> Callable[[ReleaseGroup], bool]:
    """Whether a release group is shown to carry this track, for `_per_artist`'s rare tie.

    Two kinds of evidence, either enough: the track's ISRC is filed under it, or its title *is*
    the song's (`normalize_title` equality) - a single named after the song. The second is not
    decoration. MusicBrainz often files a title-track single's recording without the ISRC Spotify
    reports, and on a replay over a real library the ISRC alone moved several title tracks -
    "The Joker", "Harvest Moon", "Nick Of Time" - off their own single, onto the album or onto
    another single. The ISRC is asked at most once, and only if a tie reaches it.
    """
    wanted = normalize_title(intent.name)
    memo: list[frozenset[str]] = []

    def holds_song(rg: ReleaseGroup) -> bool:
        if normalize_title(rg.title) == wanted:
            return True
        if not intent.isrc:
            return False
        if not memo:
            memo.append(frozenset(g.mbid for g in lookup.release_groups_for_isrc(intent.isrc)))
        return rg.mbid in memo[0]

    return holds_song


UNAVAILABLE_STEP = "source:unavailable"
"""A liked or playlist track Spotify no longer serves.

Spotify returns a taken-down or region-locked track with an empty name, an empty artist and an
empty "Various Artists" album. There is nothing to look up, so :func:`resolve_track` answers
UNMAPPED at this step before any lookup, instead of a name search for "Various Artists - ''"
whose failure the Unmatched page then explained as a release missing from MusicBrainz.
"""


def _unavailable(intent: TrackIntent) -> bool:
    return not intent.name.strip() or not any(name.strip() for name in intent.artist_names)


def resolve_track(
    intent: TrackIntent,
    lookup: MetadataLookup,
    *,
    now: datetime,
    pending_since: datetime | None,
    fallback_days: int,
    scope: str = LIKED_TRACK_SCOPE_ALBUM,
    followed_artist_mbids: frozenset[str] = frozenset(),
    rules: ExclusionRules = NO_EXCLUSIONS,
    relations: CreditRelations | None = None,
) -> Resolution:
    """Apply the Singles rule to one liked or playlist track.

    `scope` is ``[rules] liked_track_scope``. Under ``"smallest"`` the smallest official release
    holding the song wins instead (:func:`_resolve_smallest`, steps ``track:smallest:*``), and the
    steps below are its fallback for a song no studio release holds. `followed_artist_mbids` are
    the MusicBrainz IDs of the user's followed artists, which only the `smallest` scope reads (see
    its dedupe rule). Under the default ``"album"`` scope both are ignored entirely.

    The steps, in order:

    a. Map the release Spotify says the track is on, by UPC (``track:album:upc``) and then by
       name (``track:album:search``). Spotify never sends the album's UPC with a track, so this
       is a title-and-credit search in practice and it misses often; when it does, the track's
       **ISRC** names the release instead (``track:album:isrc``, :func:`_isrc_stand_in`). Only
       when that finds nothing either is the answer UNMAPPED - there is nothing to reason about.
       When the name search instead finds the title under **two same-named artists**, the ISRC
       chooses between them (:func:`_pick_by_isrc`), and if it cannot the answer is UNMAPPED at
       `AMBIGUOUS_SAME_NAME_STEP` - never the earliest of them. When only one of
       those artists' titles passes, it is taken unless the ISRC names one of the *others*
       (:func:`_contested_by_isrc`); then the ISRC fallback looks for that artist's release, and
       failing that the answer is ambiguous too. When the ISRC finds nothing either and
       `relations` is given, a release group the name search refused **only on the credit** is
       taken if MusicBrainz records a ``member of band`` or ``collaboration`` relationship joining
       its artist to Spotify's (:func:`_related_credit`) - *Try!* under John Mayer Trio
       for Spotify's "John Mayer". Never on the names alone.
    b. If that release group is a studio Album or EP, it is the answer (``track:album``).
       MusicBrainz's type decides this, never Spotify's `album_type`, which files EPs as
       ``single``.
    c. Otherwise the song was liked on a single, compilation, live album or similar, so look for
       the studio Album/EP that holds the same song:

       - the track's ISRC, through every release group holding that recording, kept only when
         it is a studio Album/EP credited to the *same primary artist* as the mapped release
         (``track:isrc->album``);
       - failing that, a normalised-title match against the tracklists of that artist's studio
         Albums and EPs (``track:title->album``). This is what catches radio edits and single
         versions, which carry their own ISRC and so are invisible to the step above.

       Several candidates: earliest first release date, then lowest MBID. Deterministic.
    d. No candidate at all:

       - the mapped release is a **single** -> PENDING_ALBUM. Nothing is monitored; the run
         re-checks every time, because the album may simply not be out yet.
       - the mapped release is a compilation, live album or anything else that is not a single
         -> RESOLVED to that release with step ``track:non-studio``. The song exists nowhere
         else, so monitoring the compilation is the honest answer. It sets `needs_full_profile`
         through the model property and ratchets the artist to the Full metadata profile.
    e. A pending track whose single came out at least `fallback_days` ago is RESOLVED to the
       single itself (``track:single-fallback``). The single's release date is the clock;
       `pending_since` is used only when that date is unknown.

    `rules` are the exclusion opt-outs. They never *change* which release wins; they remove
    releases from the running, and the steps above then decide among what is left. A refused
    release Spotify named still gets step c's studio Album/EP search when the ISRC finds nothing
    allowed: it is not monitored, but the album that lists the song is. Where nothing
    is left the answer is UNMAPPED at a ``track:excluded:*`` step naming the release that would
    have been monitored and the setting that refused it.
    """
    key = intent.reason.key
    if _unavailable(intent):
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.UNMAPPED,
            step=UNAVAILABLE_STEP,
            detail=(
                f"Spotify no longer serves this track ({intent.spotify_id}): it comes back with no "
                f"{'name' if not intent.name.strip() else 'artist'}, so there is nothing to look up. "
                "Unlike it in Spotify, or like it again if it is back under another ID"
            ),
        )
    track_is_remix = has_remix_marker(intent.name)
    spotify_rg, map_step, map_detail, same_name = _map_spotify_album(
        intent.album, lookup, "track:album", holds_song=_song_evidence(intent, lookup)
    )

    contested = False
    if same_name and spotify_rg is None:
        # Two same-named artists both have a release by this title. The ISRC chooses, or nothing
        # does - and then the answer is UNMAPPED here rather than a fall through to the ISRC
        # stand-in, whose second tier would search for the artist by the very same name.
        spotify_rg, why = _pick_by_isrc(intent, lookup, same_name)
        map_detail = f"{map_detail}; {why}"
        if spotify_rg is None:
            return Resolution(
                intent_key=key, status=ResolutionStatus.UNMAPPED, step=AMBIGUOUS_SAME_NAME_STEP, detail=map_detail
            )
    elif same_name:
        # One title survived, but the search also found same-named artists whose titles did not.
        # If the ISRC names one of *them*, the survivor is not taken: the ISRC stand-in below looks
        # for the release among the ISRC's own release groups, and failing that it is ambiguous.
        why = _contested_by_isrc(intent, lookup, same_name)
        if why:
            map_detail, spotify_rg, contested = f"{map_detail}; {why}", None, True
        else:
            map_detail = f"{map_detail} (other artists named the same were found too; the ISRC does not contradict it)"

    refused_rg, refused_step = None, ""
    if spotify_rg is not None:
        refused_step = _refusal(spotify_rg, rules, track_is_remix=track_is_remix)
        if refused_step:
            # Spotify named a release the user opted out of. Treat the mapping as having failed,
            # so the ISRC fallback gets its turn and may find an allowed release instead; only if
            # it finds nothing is the refusal itself the answer.
            refused_rg, spotify_rg = spotify_rg, None

    related = ""
    if spotify_rg is None:
        spotify_rg, why, excluded, refused = _isrc_stand_in(intent, lookup, rules, track_is_remix=track_is_remix)
        map_detail = f"{map_detail}; {why}"
        if spotify_rg is None and excluded is None and refused_rg is None and not contested and relations is not None:
            # Only where every rule before this answered UNMAPPED at the name search, so nothing
            # that resolves without it can resolve differently with it.
            spotify_rg, related, excluded, refused = _related_credit(
                intent, lookup, relations, rules, track_is_remix=track_is_remix
            )
            if related and excluded is None:
                map_detail = f"{map_detail}; {related}"
        if spotify_rg is None:

            def allowed_home(
                refused_release: ReleaseGroup, *, from_scope: str, followed: frozenset[str]
            ) -> Resolution | None:
                """Steps b-e run from a refused release, kept only when they land somewhere allowed:
                every other answer from there is the refusal itself (the terminal gate's)."""
                found = _resolve_mapped(
                    intent,
                    lookup,
                    refused_release,
                    map_detail,
                    same_name,
                    now=now,
                    pending_since=pending_since,
                    fallback_days=fallback_days,
                    scope=from_scope,
                    followed_artist_mbids=followed,
                    rules=rules,
                    track_is_remix=track_is_remix,
                )
                home = found.release_group
                if found.status != ResolutionStatus.RESOLVED or home is None:
                    return None
                return None if _refusal(home, rules, track_is_remix=track_is_remix) else found

            refusal = None
            if refused_rg is not None:
                # The release Spotify named is refused and the ISRC names nothing allowed, but the
                # Singles rule's studio Album/EP search still runs from it: a song
                # Spotify filed on a box set or a remix single whose artist's album lists it lands
                # on the album, as it would from any single.
                from_named = allowed_home(refused_rg, from_scope=scope, followed=followed_artist_mbids)
                if from_named is not None:
                    return replace(
                        from_named,
                        detail=(
                            f"{from_named.detail}; the release Spotify named, {refused_rg.title!r} "
                            f"({refused_rg.mbid}), {_REFUSAL_PHRASE[refused_step]}, so it was searched "
                            "from but is not monitored"
                        ),
                    )
                refusal = _excluded(
                    intent, refused_step, refused_rg, "It is the release Spotify named", map_detail=map_detail
                )
            elif excluded is not None:
                refusal = replace(excluded, detail=f"{excluded.detail}; {map_detail}")
            if refusal is not None:
                # The one exit where an opt-out is the answer (the terminal gate in `_resolve_mapped`
                # refuses only for the searches made from a refused release, whose refusal is this
                # one), so the one place a song whose every release is a remix is kept.
                kept = _remix_only(
                    intent, rules, refusal, proven=refused, named=refused_rg, track_is_remix=track_is_remix
                )
                if (
                    kept is not None
                    and kept.release_group is not None
                    and (refused_rg is None or kept.release_group.mbid != refused_rg.mbid)
                    and allowed_home(kept.release_group, from_scope=LIKED_TRACK_SCOPE_ALBUM, followed=frozenset())
                ):
                    # "Every release is a remix" must also hold for the studio Album/EP search the
                    # Singles rule makes from that release: a remix single whose artist's album
                    # lists the song has a home that is not a remix, and keeping the single there
                    # would monitor exactly what `allow_remix_releases = false` was set to stop.
                    # From the release Spotify named that search has already run, just above.
                    kept = None
                return kept or refusal
            step = AMBIGUOUS_SAME_NAME_STEP if contested else map_step
            return Resolution(intent_key=key, status=ResolutionStatus.UNMAPPED, step=step, detail=map_detail)
        map_step = RELATED_CREDIT_STEP if related else "track:album:isrc"

    resolution = _resolve_mapped(
        intent,
        lookup,
        spotify_rg,
        map_detail,
        same_name,
        now=now,
        pending_since=pending_since,
        fallback_days=fallback_days,
        scope=scope,
        followed_artist_mbids=followed_artist_mbids,
        rules=rules,
        track_is_remix=track_is_remix,
    )
    if related and resolution.step != "track:album":
        # `track:album` already quotes the mapping detail. Every other step describes the release
        # it chose, and here *which artist* it is was itself a decision `explain` must show.
        resolution = replace(resolution, detail=f"{resolution.detail}; {related}")
    return resolution


def _resolve_mapped(
    intent: TrackIntent,
    lookup: MetadataLookup,
    spotify_rg: ReleaseGroup,
    map_detail: str,
    same_name: Sequence[ReleaseGroup],
    *,
    now: datetime,
    pending_since: datetime | None,
    fallback_days: int,
    scope: str,
    followed_artist_mbids: frozenset[str],
    rules: ExclusionRules,
    track_is_remix: bool,
) -> Resolution:
    """:func:`resolve_track`'s steps b-e, once the release Spotify named has a release group."""
    key = intent.reason.key
    artist_mbid, withheld = _track_artist_mbid(intent, spotify_rg, lookup)

    if scope == LIKED_TRACK_SCOPE_SMALLEST:
        smallest = _resolve_smallest(
            intent,
            lookup,
            spotify_rg=spotify_rg,
            artist_mbid=artist_mbid,
            followed_artist_mbids=followed_artist_mbids,
            rules=rules,
            track_is_remix=track_is_remix,
        )
        if smallest is not None:
            # The `smallest` detail describes the choice of release, not how the album mapped -
            # except here, where which *artist* it is was itself a decision `explain` must show.
            return replace(smallest, detail=f"{smallest.detail}; {map_detail}") if same_name else smallest
        # No studio release holds the song. Fall through: the steps below are the honest answer
        # for a compilation-only track, and `smallest` must never resolve less than `album` does.

    if spotify_rg.is_studio_album_or_ep and not spotify_rg.is_various_artists:
        # A Various Artists release is never "the artist's studio album", even when MusicBrainz
        # types it Album with no Compilation tag (an early version claimed one that way).
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.RESOLVED,
            release_group=spotify_rg,
            step="track:album",
            detail=(
                f"{intent.name!r} is on {spotify_rg.title!r} ({spotify_rg.mbid}), "
                f"a studio {spotify_rg.primary_type}; {map_detail}"
            ),
            source_release_group=spotify_rg,
        )

    kind = _describe(spotify_rg)
    candidates: list[ReleaseGroup] = []
    step = ""
    isrc_hits = 0

    # Both candidate steps drop anything an opt-out refuses. Filtering here rather than vetoing
    # the winner is what lets a deny-list entry act as "not this one, the next one": the four
    # copies of "Mercy, Mercy, Mercy" that resolve correctly and the fifth that found an untagged
    # live album differ only in which candidates their credit put in front of them.
    def allowed(rg: ReleaseGroup) -> bool:
        return not _refusal(rg, rules, track_is_remix=track_is_remix)

    other_songs = ""
    other_performers = 0
    too_large = False
    if intent.isrc and artist_mbid:
        found, other_songs = _isrc_release_groups(intent, lookup)
        isrc_hits = len(found)
        candidates = [rg for rg in found if rg.is_studio_album_or_ep and rg.artist_mbid == artist_mbid and allowed(rg)]
        if candidates:
            step = "track:isrc->album"

    if not candidates and artist_mbid and artist_mbid != VARIOUS_ARTISTS_MBID:
        # The ISRC path found nothing usable. Radio edits and single versions get their own
        # ISRC, so the recording on the album is a different recording entirely; fall back to
        # matching normalised titles against the artist's studio Album/EP tracklists.
        wanted = normalize_title(intent.name)
        try:
            catalogue = lookup.artist_release_groups(artist_mbid)
        except CatalogueTooLarge:
            # Permanent, not an outage: a composer's catalogue is too long to page through,
            # so there is no title search to make, and the answers below stand on what is known.
            catalogue, too_large = (), True
        for rg in catalogue:
            if not rg.is_studio_album_or_ep or not allowed(rg):
                continue
            if _other_performers(spotify_rg, rg):
                other_performers += 1
                continue
            titles = {normalize_title(t) for t in lookup.release_group_track_titles(rg.mbid)}
            if wanted in titles:
                candidates.append(rg)
        if candidates:
            step = "track:title->album"

    if candidates:
        chosen = _earliest(candidates)
        others = len(candidates) - 1
        via = (
            f"ISRC {intent.isrc}"
            if step == "track:isrc->album"
            else f"normalised title {normalize_title(intent.name)!r}"
        )
        tie = f"; {others} other candidate(s), earliest release date wins" if others else ""
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.RESOLVED,
            release_group=chosen,
            step=step,
            detail=(
                f"Spotify filed {intent.name!r} on {kind} {spotify_rg.title!r} ({spotify_rg.mbid}); "
                f"{via} puts the song on studio {chosen.primary_type} {chosen.title!r} ({chosen.mbid}){tie}"
                + (f"; {other_songs}" if other_songs else "")
            ),
            source_release_group=spotify_rg,
        )

    searched = f"ISRC {intent.isrc} matched {isrc_hits} release group(s), none a studio album or EP by this artist"
    if other_songs:
        searched = f"{searched} ({other_songs})"
    if not intent.isrc:
        searched = "the track has no ISRC"
    if too_large:
        searched = (
            f"{searched}; the title search was skipped: {spotify_rg.artist_name}'s catalogue is too large "
            "to browse on MusicBrainz"
        )
    if other_performers:
        searched = (
            f"{searched}; {other_performers} studio release{'' if other_performers == 1 else 's'} credited to "
            f"other performers than {spotify_rg.title!r} {'was' if other_performers == 1 else 'were'} not searched"
        )

    if spotify_rg.is_various_artists:
        # Never monitor a Various Artists compilation (Lidarr files it under a placeholder artist,
        # exactly as the saved-album path refuses). The song is reported, not silently dropped.
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.UNMAPPED,
            step="track:various-artists",
            detail=(
                f"{intent.name!r} is only on the Various Artists compilation {spotify_rg.title!r} "
                f"({spotify_rg.mbid}): {searched}; "
                + (f"{withheld}; " if withheld else "")
                + "likearr does not monitor compilations"
            ),
            source_release_group=spotify_rg,
        )

    terminal = _refusal(spotify_rg, rules, track_is_remix=track_is_remix)
    if terminal:
        # No candidate survived, and the release Spotify named - the only thing left to monitor -
        # is one the user opted out of. There is nothing to fall through to, so say so plainly.
        return _excluded(intent, terminal, spotify_rg, searched)

    if not spotify_rg.is_single:
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.RESOLVED,
            release_group=spotify_rg,
            step="track:non-studio",
            detail=(
                f"{intent.name!r} exists only on {kind} {spotify_rg.title!r} ({spotify_rg.mbid}): "
                f"{searched}, and no studio album or EP by {spotify_rg.artist_name} lists the title. "
                "Monitoring that release is the only way to get the song"
            ),
            source_release_group=spotify_rg,
        )

    single_date = spotify_rg.first_release_date or intent.album.release_date
    pending = Resolution(
        intent_key=key,
        status=ResolutionStatus.PENDING_ALBUM,
        step="track:pending",
        detail=(
            f"{intent.name!r} was liked on the single {spotify_rg.title!r} ({spotify_rg.mbid}); "
            f"{searched}. Waiting for an album: nothing is monitored"
            + (f", single released {single_date.isoformat()}" if single_date else ", single release date unknown")
        ),
        single_release_date=single_date,
        single_release_group=spotify_rg,
        source_release_group=spotify_rg,
    )

    elapsed, clock = None, ""
    if single_date is not None:
        elapsed, clock = (now.date() - single_date).days, f"released {single_date.isoformat()}"
    elif pending_since is not None:
        elapsed = _days_since(now, pending_since)
        clock = f"pending since {pending_since.date().isoformat()}"
    if elapsed is not None and elapsed >= fallback_days:
        return Resolution(
            intent_key=key,
            status=ResolutionStatus.RESOLVED,
            release_group=spotify_rg,
            step=SINGLE_FALLBACK_STEP,
            detail=(
                f"the single {spotify_rg.title!r} ({spotify_rg.mbid}) holding {intent.name!r} was {clock}, "
                f"{elapsed} days ago and still on no album after {fallback_days}; monitoring the single itself"
            ),
            single_release_date=single_date,
            single_release_group=spotify_rg,
            source_release_group=spotify_rg,
        )
    return pending


def _other_performers(named: ReleaseGroup, candidate: ReleaseGroup) -> bool:
    """True when the title search must skip `candidate`: another performance of the work.

    MusicBrainz credits a classical release group to the composer first ("Jean Sibelius; London
    Philharmonic Orchestra, Paavo Berglund"), and the title search walks the first credit's
    catalogue, so every orchestra's studio album of the work looked like "the artist's": Berglund's
    live "The Swan of Tuonela" went to Neeme Jarvi's 1985 studio album. So when the release Spotify
    named carries **more than one** main credited artist, a candidate is taken only with the same
    set of main artists. Featured guests are no part of either set (`main_artist_mbids`), so a remix
    single "X feat. Y" is X's alone and its album credited to X is still found. MusicBrainz credits
    are compared with MusicBrainz credits, never with Spotify's performer names. A single-artist
    credit is never checked - a Duke Ellington compilation track still maps to his studio album,
    the "same song" rule - and neither is an unknown credit on either side (a release group from
    Lidarr's metadata, or read back from state). Asked before the candidate's tracklist is
    fetched, so it also spares a composer's hundreds of fetches.
    """
    credit = frozenset(named.main_artist_mbids)
    theirs = frozenset(candidate.main_artist_mbids)
    return len(credit) > 1 and bool(theirs) and theirs != credit


def _describe(rg: ReleaseGroup) -> str:
    """A short human label for a release group's type, e.g. 'compilation album'."""
    secondary = " ".join(sorted(str(s).lower() for s in rg.secondary_types))
    primary = str(rg.primary_type).lower() if rg.primary_type else "release"
    return f"{secondary} {primary}".strip()


# --------------------------------------------------------------------------- the whole snapshot


def _followed_release(resolution: Resolution, followed_artist_mbids: frozenset[str]) -> bool:
    """Is the artist this resolution landed on currently followed?

    The `smallest` scope only ever chooses a release credited to the track's own artist (see
    `_is_smallest_candidate`), so the chosen release group's primary artist *is* the artist whose
    follow state the dedupe rule read.
    """
    release_group = resolution.release_group
    return release_group is not None and release_group.artist_mbid in followed_artist_mbids


def _reusable(
    cache: Mapping[str, Resolution | ArtistResolution],
    key: str,
    kind: type[Resolution] | type[ArtistResolution],
    *,
    scope: str | None = None,
    followed_artist_mbids: frozenset[str] | None = None,
    rules: ExclusionRules | None = None,
) -> Resolution | ArtistResolution | None:
    """A cached resolution is reused only when it is RESOLVED and from this resolver version.

    PENDING_ALBUM must be re-resolved every run (the album may have landed, or the fallback
    window may have closed) and UNMAPPED must be re-resolved every run (MusicBrainz gains data,
    and a lookup that failed once should not stick).

    `rules` makes reuse sensitive to the exclusion opt-outs, in two different ways for two
    different costs. The two switches are compared through `ExclusionRules.token`, so flipping one
    re-resolves every liked and playlist track - which is right, because it is a rule change - and
    because the default token is `""`, a resolution written before the field existed still matches
    a default configuration and deploying the code re-resolves nothing at all. The deny list is
    compared against the release the cached answer *chose*, so adding one MBID re-resolves only
    the intents that landed on it rather than the whole library, and against the denied releases
    it was refused on the way (`Resolution.denied_skipped`), so removing one re-resolves only the
    intents that were kept off it.

    `followed_artist_mbids` makes reuse sensitive to **follow state**, and is passed only under the
    `smallest` scope, where the dedupe rule reads it. Without this a resolution made before the
    artist was followed is permanent, so following them later never swaps their single for the
    album the follow already brings - a departure from the rule the scope is written around.
    Only a `track:smallest:*` resolution is checked: any other step means no studio
    release held the song at all, which no follow changes.

    A resolution whose `followed` was never recorded is **back-filled rather than re-resolved**
    whenever its step already agrees with today's follow state, and the back-filled copy is what
    is returned (and therefore what gets written back to the cache). On a `smallest` library the
    great majority of cached resolutions are `covered-by-follow` for artists who are still
    followed, so re-resolving them all would cost thousands of lookups to arrive at the answers
    already on disk. The ones whose step *disagrees* are re-resolved, since those are exactly the
    stale answers that must not be reused.

    Age is not checked here: an answer that is merely old is still valid, and `resolve_all` keeps
    it when its re-check fails. See `_due`.
    """
    hit = cache.get(key)
    if not isinstance(hit, kind):
        return None
    if hit.resolver_version != RESOLVER_VERSION:
        return None
    if hit.status != ResolutionStatus.RESOLVED:
        return None
    if scope is not None and isinstance(hit, Resolution) and hit.scope != scope:
        return None  # the liked_track_scope setting changed: resolve again under the new rule
    if rules is not None and isinstance(hit, Resolution):
        if hit.rules != rules.token:
            return None  # an opt-out switch moved: every track answers under the new rules
        if hit.release_group is not None and hit.release_group.mbid in rules.deny_releases:
            # The deny list is checked against the answer rather than folded into the token, so
            # adding one MBID re-resolves the handful of intents that landed on it instead of
            # every liked track in the library.
            return None
        if not hit.denied_skipped <= rules.deny_releases:
            # And removing one re-resolves only the intents that were kept off it:
            # the answer they fell through to is not what they would get now.
            return None
    if followed_artist_mbids is not None and isinstance(hit, Resolution) and hit.step.startswith(SMALLEST_STEP_PREFIX):
        now_followed = _followed_release(hit, followed_artist_mbids)
        if hit.followed is None:
            # Written before the field existed. The step says which branch of the dedupe rule ran:
            # `covered-by-follow` happens only for a followed artist. Where that agrees with today,
            # the cached answer is the answer this run would compute, so record it and move on.
            if (hit.step == COVERED_BY_FOLLOW_STEP) != now_followed:
                return None
            hit = replace(hit, followed=now_followed)
        elif now_followed != hit.followed:
            return None  # followed or unfollowed since: the dedupe rule answers differently now
    return hit


def _due(
    hit: Resolution, key: str, *, now: datetime, max_age: Callable[[str], timedelta] | None
) -> tuple[Resolution, bool]:
    """`hit`, with its clock started if it had none, and whether it is old enough to check again.

    Age makes reuse expire: a track or saved-album answer whose `checked_at` is at
    least ``max_age(key)`` before `now` is looked up again, so a MusicBrainz correction or merge
    reaches an intent that already resolved instead of waiting for the next `RESOLVER_VERSION`.
    `max_age` is asked per key because the shell jitters it by key, so a library cached in one run
    does not fall due in one run. ``None`` never expires anything.

    An answer with no `checked_at` (written before the field), or one that cannot be compared with
    `now` (naive against aware, as `_days_since` reads the pending clock), is reused and stamped
    `now`, back-filled like `followed`: an upgrade re-resolves nothing and every clock starts that
    day. A due answer is still a valid one. `resolve_all` keeps it when the re-check fails, as
    `mb_cache` serves a stale entry, and never lets the re-check step it backwards.
    """
    if max_age is None:
        return hit, False
    try:
        age = None if hit.checked_at is None else now - hit.checked_at
    except TypeError:
        age = None
    if age is None:
        return replace(hit, checked_at=now), False
    return hit, age >= max_age(key)


def _settle(result: ResolveResult, key: str, due: Resolution | None, *, failed: bool) -> None:
    """Record this run's answer for `key`, keeping a due answer where its re-check fell short.

    `due` is a cached answer looked up again only because it was old (`_due`). It is kept, unmoved
    and still due, when the re-check failed or only reached a provisional answer: an answer
    MusicBrainz confirmed is not traded for one it did not, as `mb_cache` serves a stale entry when
    its refetch fails. It is also kept, with its clock restarted, when the re-check would have
    stepped it backwards from a single fallback to waiting: an expiry changes when an answer is
    trusted, never what the library holds. Anything else is this run's answer, provisional as ever
    when a lookup failed during it.
    """
    fresh = result.resolutions[key]
    if due is not None and (failed or fresh.step == METADATA_ERROR_STEP):
        result.resolutions[key] = due
        return
    if due is not None and due.step == SINGLE_FALLBACK_STEP and fresh.status is ResolutionStatus.PENDING_ALBUM:
        result.resolutions[key] = replace(due, checked_at=fresh.checked_at)
        return
    if failed:
        result.provisional.add(key)


def resolve_all(
    snapshot: SourceSnapshot,
    lookup: MetadataLookup,
    *,
    now: datetime,
    cache: Mapping[str, Resolution | ArtistResolution],
    pending_since: Mapping[str, datetime],
    fallback_days: int,
    scope: str = LIKED_TRACK_SCOPE_ALBUM,
    links: ArtistLinks | None = None,
    known_artist_mbids: frozenset[str] = frozenset(),
    rules: ExclusionRules = NO_EXCLUSIONS,
    lookup_failures: Callable[[], int] | None = None,
    relations: CreditRelations | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_age: Callable[[str], timedelta] | None = None,
) -> ResolveResult:
    """Resolve every intent in a snapshot, surviving individual metadata failures.

    `links` resolves a followed artist through MusicBrainz's Spotify URL relationship, which is
    authoritative where a name search is only a guess; `known_artist_mbids` (Lidarr's artists)
    breaks a tie between several linked candidates. Both default to "not available", which is
    exactly the old name-search behaviour, so every existing call site is unaffected.

    `scope` is ``[rules] liked_track_scope`` and is passed to every track. Followed artists are
    resolved first on purpose: their MusicBrainz IDs are what the `smallest` scope's dedupe rule
    needs, and a track cannot know whether its artist is followed until they are known.

    `cache` holds resolutions from previous runs keyed by intent key; an entry is reused only
    when its `resolver_version` matches, its status is RESOLVED, and - under the `smallest` scope -
    its artist's follow state has not changed since (see :func:`_reusable`).
    `pending_since` holds, per intent key, when a track first went PENDING_ALBUM; it is the
    fallback clock for singles whose release date MusicBrainz does not know.

    A `MetadataError` on one intent marks that intent UNMAPPED with step ``error:metadata`` and
    increments `metadata_errors`. One bad lookup never aborts the run, and the count lets the
    shell report the run as degraded rather than pretending the sources shrank.

    `lookup_failures` is a running count of the lookup's failures this run, including the ones it
    answered some other way (``CompositeLookup.mb_failure_count``). It is read before and after
    each intent actually resolved - never around a cache hit - and an intent during which it moved
    is added to `ResolveResult.provisional`. The answer itself is unchanged: this only
    tells the caller that it was reached after MusicBrainz failed. ``None``, the default, means
    the lookup reports nothing, and nothing is provisional.

    `relations` lets a liked or playlist track take a release group MusicBrainz credits to an
    artist it records as joined to Spotify's credit (:func:`_related_credit`). ``None``,
    the default, is the resolver without that rule.

    `progress` is called with ``(intents done, intents total)`` right after each artist, album or
    track intent is handled - a cache hit counts as done too, since the caller's progress line is
    about how much of the snapshot is behind it, not how much MusicBrainz work happened. Core stays
    pure either way: it calls the function and never logs anything itself. ``None``,
    the default, means nobody is listening.

    `max_age` is how old a cached track or saved-album answer may get before it is looked up again,
    per intent key (:func:`_due`). Every answer resolved here is stamped
    `checked_at=now`. ``None``, the default, reuses an answer however old it is.
    """
    result = ResolveResult()
    failures = lookup_failures or (lambda: 0)
    total_intents = len(snapshot.artists) + len(snapshot.albums) + len(snapshot.tracks)
    done_intents = 0

    def _advance() -> None:
        nonlocal done_intents
        done_intents += 1
        if progress is not None:
            progress(done_intents, total_intents)

    for artist_intent in snapshot.artists:
        key = artist_intent.reason.key
        cached = _reusable(cache, key, ArtistResolution)
        if isinstance(cached, ArtistResolution):
            result.artist_resolutions[key] = cached
            _advance()
            continue
        before = failures()
        try:
            result.artist_resolutions[key] = resolve_artist(
                artist_intent, lookup, links=links, known_artist_mbids=known_artist_mbids
            )
        except MetadataError as e:
            result.metadata_errors += 1
            result.artist_resolutions[key] = ArtistResolution(
                intent_key=key,
                status=ResolutionStatus.UNMAPPED,
                artist_name=artist_intent.name,
                step=METADATA_ERROR_STEP,
                detail=f"metadata lookup failed for artist {artist_intent.name!r}: {e}",
            )
        if failures() != before:
            result.provisional.add(key)
        _advance()

    followed_artist_mbids = frozenset(
        r.artist_mbid
        for r in result.artist_resolutions.values()
        if r.status == ResolutionStatus.RESOLVED and r.artist_mbid
    )

    for album_intent in snapshot.albums:
        key = album_intent.reason.key
        cached = _reusable(cache, key, Resolution)
        due: Resolution | None = None
        if isinstance(cached, Resolution):
            cached, is_due = _due(cached, key, now=now, max_age=max_age)
            if not is_due:
                result.resolutions[key] = cached
                _advance()
                continue
            due = cached
        before = failures()
        try:
            result.resolutions[key] = replace(resolve_album(album_intent, lookup), checked_at=now)
        except MetadataError as e:
            result.metadata_errors += 1
            result.resolutions[key] = Resolution(
                intent_key=key,
                status=ResolutionStatus.UNMAPPED,
                step=METADATA_ERROR_STEP,
                detail=f"metadata lookup failed for album {album_intent.album.name!r}: {e}",
            )
        _settle(result, key, due, failed=failures() != before)
        _advance()

    smallest = scope == LIKED_TRACK_SCOPE_SMALLEST
    for track_intent in snapshot.tracks:
        key = track_intent.reason.key
        cached = _reusable(
            cache,
            key,
            Resolution,
            scope=scope,
            followed_artist_mbids=followed_artist_mbids if smallest else None,
            rules=rules,
        )
        due = None
        if isinstance(cached, Resolution):
            cached, is_due = _due(cached, key, now=now, max_age=max_age)
            if not is_due:
                result.resolutions[key] = cached
                _advance()
                continue
            due = cached
        clock = pending_since.get(key)
        if due is not None and due.step == SINGLE_FALLBACK_STEP:
            # Settled on its single by the fallback window, whose clock the shell cleared when it
            # settled: that window has already run out, so the re-check starts from there rather
            # than from a fresh wait. An album that has appeared still wins.
            ran_out = now - timedelta(days=fallback_days)
            since = None if clock is None else _days_since(now, clock)
            if since is None or since < fallback_days:
                clock = ran_out  # none, younger than the window, or naive against aware
        before = failures()
        # No deny list, nothing to record: skip the probe's per-candidate Python-level lookup.
        denied = _DenyProbe(rules.deny_releases) if rules.deny_releases else None
        try:
            fresh = resolve_track(
                track_intent,
                lookup,
                now=now,
                pending_since=clock,
                fallback_days=fallback_days,
                scope=scope,
                followed_artist_mbids=followed_artist_mbids,
                rules=rules if denied is None else replace(rules, deny_releases=denied),
                relations=relations,
            )
            result.resolutions[key] = replace(
                fresh,
                scope=scope,
                followed=_followed_release(fresh, followed_artist_mbids) if smallest else None,
                rules=rules.token,
                denied_skipped=frozenset() if denied is None else frozenset(denied.hits),
                checked_at=now,
            )
        except MetadataError as e:
            result.metadata_errors += 1
            result.resolutions[key] = Resolution(
                intent_key=key,
                status=ResolutionStatus.UNMAPPED,
                step=METADATA_ERROR_STEP,
                detail=f"metadata lookup failed for track {track_intent.name!r}: {e}",
            )
        _settle(result, key, due, failed=failures() != before)
        _advance()

    return result
