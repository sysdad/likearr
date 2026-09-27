"""The Unmatched page (#60): what the last run monitors nothing for, one row per release, by reason.

The page used to print one bullet per song - hundreds of them under "Couldn't be matched" on a
large library - which no one could act on. This module turns the same last-run facts into rows a
person can work through:

- **one row per release, not per song.** Several liked songs from one live album are one
  row, "Some Artist - Live In Concert - 5 liked songs", with the songs listed on expand. A
  followed artist likearr could not find is a row of its own kind.
- **grouped by what happened** (`GROUPS`: couldn't be matched, two artists share the name, left out
  by your settings, waiting for an album, lookup failed), and inside each group **by why**
  (`reason_for`): the resolver's step in the reviewer's words, because what can be done differs -
  add the album to MusicBrainz, wait for Lidarr's metadata, change a setting, nothing.
- **where it came from**: liked song, saved album, followed artist, or the playlist by name.

"Couldn't be matched" also holds the releases MusicBrainz has and Lidarr's catalogue does not
(`lidarr:*` in a plan's unmapped list). They are not resolver misses, so the last run does not
record them as resolutions; they are worked out again here from what it did record: a wanted
release whose artist Lidarr holds, with no album for it, that the plan was not going to monitor.

It only reads. It never calls Spotify or MusicBrainz and has no write of its own: every action on
a row is a link - Look up, a MusicBrainz search, Lidarr, Settings.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode

from likearr.config import is_mbid
from likearr.core.explain import resolution_outcome
from likearr.core.resolver import UNAVAILABLE_STEP
from likearr.models import (
    AlbumIntent,
    ArtistIntent,
    ArtistResolution,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    TrackIntent,
)
from likearr.models import Reason as SpotifyReason
from likearr.shell.last_run import LastRun

__all__ = [
    "GROUPS",
    "PAGE_SIZE",
    "REASONS",
    "SORTS",
    "Card",
    "Filters",
    "Group",
    "Part",
    "Reason",
    "Row",
    "build_rows",
    "cards",
    "filters_from",
    "part",
    "reason_for",
    "reason_options",
    "sections",
    "source_options",
]

PAGE_SIZE = 25
MAX_QUERY = 200

_MUSICBRAINZ = "https://musicbrainz.org"


@dataclass(frozen=True, slots=True)
class Group:
    code: str
    title: str
    """The section heading and the stat card's label."""
    guidance: str
    """One line: what, if anything, you can do."""
    tone: str = "warn"


GROUPS: tuple[Group, ...] = (
    Group(
        "unmatched",
        "Couldn't be matched",
        "likearr has nothing it can monitor for these. What helps depends on why - each part says.",
    ),
    Group(
        "ambiguous",
        "Two artists share the name",
        "Two different MusicBrainz artists share this name and album title, so likearr didn't guess. "
        "Look up shows what it saw.",
    ),
    Group(
        "excluded",
        "Left out by your settings",
        "Their only release is one your settings leave out. Change the setting if you want them.",
        tone="quiet",
    ),
    Group(
        "pending",
        "Waiting for an album",
        "Liked singles: likearr waits for the album before monitoring anything. Nothing to do.",
        tone="quiet",
    ),
    Group(
        "failed",
        "Lookup failed this run",
        "Looking these up failed this time. likearr tries again at the next run; nothing to do yet.",
        tone="bad",
    ),
)
_GROUP = {g.code: g for g in GROUPS}


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    label: str
    """Why, in a few words: every row shows it."""
    guidance: str
    """What you can do about it, for the part's heading."""
    setting: str = ""
    """The Settings field that decides it (``rules.allow_remix_releases``), for Left out."""


REASONS: tuple[Reason, ...] = (
    Reason(
        "no-release",
        "MusicBrainz has no release by this artist with this title",
        "If the album is real, add it to MusicBrainz (or fix its title there); likearr looks again every run.",
    ),
    Reason(
        "missing-in-lidarr",
        "Lidarr is missing this release",
        "Lidarr has the artist but not this release: refresh the artist in Lidarr, then run again.",
    ),
    Reason(
        "catalogue-new",
        "New on MusicBrainz; Lidarr's catalogue hasn't caught up yet",
        "likearr asks Lidarr to refresh the artist, so it's usually picked up within a run or two.",
    ),
    Reason(
        "not-in-catalogue",
        "MusicBrainz has it, but Lidarr's catalogue doesn't yet",
        "A followed artist's release Lidarr doesn't list - usually a promo, bootleg or other unofficial "
        "release it never will. Nothing to do.",
    ),
    Reason(
        "unavailable",
        "Spotify no longer serves this track",
        "Spotify returns it with no name or artist - taken down or region-locked - so there is nothing "
        "to look up. Unlike it in Spotify, or like it again if it's back under another ID.",
    ),
    Reason(
        "various-artists",
        "A Various Artists compilation",
        "Lidarr files these under a placeholder artist, so likearr leaves them alone. Add one in Lidarr by "
        "hand if you want it.",
    ),
    Reason(
        "no-artist",
        "MusicBrainz has no artist that clearly matches",
        "Check the artist on MusicBrainz: missing, or several with the name and nothing to tell them apart.",
    ),
    Reason(
        "artist-link",
        "Spotify's link points at several MusicBrainz artists",
        "Several MusicBrainz artists claim this Spotify artist; fixing the links on MusicBrainz sorts it.",
    ),
    Reason(
        "too-large",
        "Too many releases on MusicBrainz to read",
        "MusicBrainz won't list this artist's whole catalogue in one go; likearr tries again every run.",
    ),
    Reason(
        "same-name",
        "Two artists share this name and title",
        "Look it up to see both; likearr matches it once MusicBrainz can tell them apart.",
    ),
    Reason(
        "remix",
        "Left out: a remix",
        "Remix releases are off in your settings. Keep remix-only songs brings a song back only when "
        "every release it is on is a remix.",
        setting="rules.allow_remix_releases",
    ),
    Reason(
        "compilation",
        "Left out: a compilation",
        "Compilations are off in your settings.",
        setting="rules.allow_compilation_fallback",
    ),
    Reason(
        "denied",
        "Left out: you chose Not this one",
        "The release is in your refused releases.",
        setting="rules.deny_releases",
    ),
    Reason("left-out", "Left out by your settings", "A setting leaves this release out."),
    Reason("waiting", "A single waiting for its album", "Nothing to do: it's monitored once the album is out."),
    Reason("lookup-failed", "The MusicBrainz lookup failed this run", "likearr tries again at the next run."),
    Reason("other", "likearr couldn't match it", "Look it up for what likearr tried."),
)
_REASON = {r.code: r for r in REASONS}
_GENERIC = _REASON["other"]


def reason_for(step: str, group: str) -> Reason:
    """The plain reason for a resolver (or plan) step. Never raises: a step this table does not
    know - a newer resolver's - reads as the generic "likearr couldn't match it"."""
    if group == "ambiguous":
        return _REASON["same-name"]
    if group == "pending":
        return _REASON["waiting"]
    if group == "failed":
        return _REASON["lookup-failed"]
    if group == "excluded":
        kind = step.rsplit(":", 1)[-1]
        return _REASON[kind] if kind in ("remix", "compilation", "denied") else _REASON["left-out"]
    if step == "lidarr:missing-release-group":
        return _REASON["missing-in-lidarr"]
    if step == "lidarr:not-in-catalogue-yet":
        return _REASON["catalogue-new"]
    if step == "lidarr:not-in-catalogue":
        return _REASON["not-in-catalogue"]
    if step == "error:catalogue-too-large":
        return _REASON["too-large"]
    if step == UNAVAILABLE_STEP:
        return _REASON["unavailable"]
    if step in ("track:various-artists", "album:various-artists"):
        return _REASON["various-artists"]
    if step == "artist:ambiguous-link":
        return _REASON["artist-link"]
    if step.startswith("artist:"):
        return _REASON["no-artist"]
    if step.startswith(("track:album", "album:", "track:isrc")):
        return _REASON["no-release"]
    return _GENERIC


# ---------------------------------------------------------------- rows


@dataclass(slots=True)
class Row:
    group: str
    reason: Reason
    kind: str
    """``release``, or ``artist`` for a followed artist likearr could not find."""
    artist: str
    title: str
    """The album as Spotify (or MusicBrainz, for a Lidarr gap) names it; the artist's name for an artist row."""
    songs: list[str] = field(default_factory=list)
    """The songs behind it, as Spotify names them; empty for a saved album or a followed artist."""
    sources: dict[str, str] = field(default_factory=dict)
    """Source code (``liked``, ``saved``, ``followed``, ``playlist:<id>``, ``kept``) -> its label."""
    items: int = 0
    """How many Spotify items - songs, albums, follows - this one row stands for."""
    release_group: str = ""
    """The MusicBrainz release group, when likearr knows one (a Lidarr gap, a left-out release)."""
    artist_mbid: str = ""
    lidarr_path: str = ""
    """``/album/<rg>`` or ``/artist/<mbid>`` when Lidarr holds it, to open it there."""
    gap: bool = False
    """A release Lidarr's catalogue lacks: matched, so Status counts its songs as matched, and the
    card counts it beside its number rather than in it."""

    @property
    def headline(self) -> str:
        return self.title if self.kind == "artist" else f"{self.artist} - {self.title}" if self.artist else self.title

    @property
    def what(self) -> str:
        """ "12 liked songs", "Followed artist": what the row stands for, beyond its source."""
        if self.kind == "artist":
            return "Followed artist"
        if not self.songs:
            return ""  # a saved album or a catalogue release: the source says what it is
        n = len(self.songs)
        liked = all(code == "liked" for code in self.sources if code != "saved")
        noun = "liked song" if liked else "song"
        return f"{n} {noun}{'' if n == 1 else 's'}"

    @property
    def source_text(self) -> str:
        return ", ".join(self.sources[code] for code in sorted(self.sources, key=_source_order))

    @property
    def lookup(self) -> str:
        """What to type into Look up to find it: the album (or artist) as Spotify names it, cut to
        what Look up takes. Look up matches one name at a time, so the artist can't narrow it."""
        return self.title[:MAX_QUERY]

    @property
    def musicbrainz(self) -> tuple[str, str]:
        """(label, url): the release group when likearr knows it, else a search for it."""
        if self.release_group and is_mbid(self.release_group):
            return "On MusicBrainz", f"{_MUSICBRAINZ}/release-group/{self.release_group}"
        if self.kind == "artist":
            return "Search MusicBrainz", _search("artist", f'artist:"{_quoted(self.title)}"')
        query = f'releasegroup:"{_quoted(self.title)}"'
        if self.artist:
            query += f' AND artist:"{_quoted(self.artist)}"'
        return "Search MusicBrainz", _search("release_group", query)

    @property
    def setting(self) -> str:
        return self.reason.setting

    def matches(self, needle: str) -> bool:
        return (
            needle in self.artist.casefold()
            or needle in self.title.casefold()
            or any(needle in song.casefold() for song in self.songs)
        )


def _quoted(value: str) -> str:
    """A value safe inside a quoted MusicBrainz search term: no quote or backslash to end it early."""
    return value.replace("\\", " ").replace('"', " ").strip()


def _search(kind: str, query: str) -> str:
    return f"{_MUSICBRAINZ}/search?" + urlencode({"query": query, "type": kind, "method": "advanced"})


def _source_order(code: str) -> tuple[int, str]:
    order = {"liked": 0, "saved": 1, "followed": 2, "kept": 4}
    return (order.get(code, 3), code)


def _source(reason: SpotifyReason, playlist_names: Mapping[str, str]) -> tuple[str, str]:
    kind = reason.kind
    if kind is ReasonKind.PLAYLIST:
        pid = reason.playlist_id or ""
        name = playlist_names.get(pid)
        return f"playlist:{pid}", f'Playlist "{name}"' if name else "A playlist"
    if kind is ReasonKind.SAVED:
        return "saved", "Saved album"
    if kind is ReasonKind.FOLLOWED:
        return "followed", "Followed artist"
    if kind is ReasonKind.MANUAL:
        return "kept", "Kept by hand"
    return "liked", "Liked song"


def _in_lidarr(rg: ReleaseGroup | None, last: LastRun) -> str:
    if rg is None or not is_mbid(rg.mbid) or not is_mbid(rg.artist_mbid):
        return ""
    if last.view.album(ReleaseKey(rg.artist_mbid, rg.mbid)) is not None:
        return f"/album/{rg.mbid}"
    if rg.artist_mbid in last.view.artists:
        return f"/artist/{rg.artist_mbid}"
    return ""


def _rg_of(resolution: Resolution | ArtistResolution) -> ReleaseGroup | None:
    if isinstance(resolution, ArtistResolution):
        return None
    return resolution.release_group or resolution.single_release_group or resolution.source_release_group


class _Rows:
    """Rows keyed by (group, reason, release group) when likearr knows the release group, else by
    (group, reason, kind, artist, title): the songs of one album land together, and two different
    releases that share an artist name and a title ("Live") stay two rows."""

    def __init__(self, playlist_names: Mapping[str, str]) -> None:
        self.rows: dict[tuple[str, str, str, str, str], Row] = {}
        self.playlist_names = playlist_names

    def add(
        self,
        *,
        group: str,
        reason: Reason,
        kind: str,
        artist: str,
        title: str,
        songs: Iterable[str] = (),
        sources: Iterable[SpotifyReason] = (),
        rg: ReleaseGroup | None = None,
        lidarr_path: str = "",
        count: int = 1,
        gap: bool = False,
    ) -> None:
        key = (
            (group, reason.code, "rg", rg.mbid, "")
            if rg is not None and is_mbid(rg.mbid)
            else (group, reason.code, kind, artist.casefold(), title.casefold())
        )
        row = self.rows.get(key)
        if row is None:
            row = self.rows[key] = Row(group=group, reason=reason, kind=kind, artist=artist, title=title, gap=gap)
        row.songs.extend(song for song in songs if song)
        for source in sources:
            code, label = _source(source, self.playlist_names)
            row.sources.setdefault(code, label)
        row.items += count
        if rg is not None and not row.release_group:
            row.release_group, row.artist_mbid = rg.mbid, rg.artist_mbid
        if lidarr_path and not row.lidarr_path:
            row.lidarr_path = lidarr_path


def _intents(last: LastRun) -> dict[str, TrackIntent | AlbumIntent | ArtistIntent]:
    out: dict[str, TrackIntent | AlbumIntent | ArtistIntent] = {}
    for intent in (*last.snapshot.tracks, *last.snapshot.albums, *last.snapshot.artists):
        out.setdefault(intent.reason.key, intent)
    return out


def _unresolved(
    last: LastRun, intents: Mapping[str, TrackIntent | AlbumIntent | ArtistIntent]
) -> Iterable[tuple[str, Resolution | ArtistResolution | None]]:
    """Each Spotify item the last run read that matched nothing, with its resolution - counted
    exactly as Status's coverage counts them (`likearr.web.status.coverage`), so the two pages
    give the same numbers for the same run: every reason key once, and a key with no resolution
    at all is a miss."""
    for key in intents:
        resolution = last.resolutions.get(key) or last.artist_resolutions.get(key)
        if resolution is None or resolution.status is not ResolutionStatus.RESOLVED:
            yield key, resolution


def _catalogue_gaps(last: LastRun, recent_release_days: int) -> Iterable[tuple[ReleaseKey, str]]:
    """The wanted releases Lidarr's catalogue does not hold, with the plan's step for each.

    The plan lists them as unmapped (`core.diff`): the artist is in Lidarr and was read, no album
    has the release group, so nothing is monitored. The last run keeps what that needs - the
    wanted releases, Lidarr's view and the plan's monitors - so they are found again here: an
    artist the plan was adding (or had not read) would have the release among its monitors. A
    file from before the monitors were recorded (`monitor` is ``None``) cannot tell, so it lists none.
    """
    if last.monitor is None:
        return
    for key, release in sorted(last.desired.releases.items(), key=lambda kv: (kv[0].artist_mbid, kv[0].rg_mbid)):
        if key in last.monitor or key.artist_mbid not in last.view.artists or last.view.album(key) is not None:
            continue
        if all(r.kind is ReasonKind.FOLLOWED for r in release.reasons):
            released = release.release_group.first_release_date
            recent = released is not None and (last.ran_at.date() - released) <= timedelta(days=recent_release_days)
            yield key, "lidarr:not-in-catalogue-yet" if recent else "lidarr:not-in-catalogue"
        else:
            yield key, "lidarr:missing-release-group"


def build_rows(
    last: LastRun, *, playlist_names: Mapping[str, str] | None = None, recent_release_days: int = 60
) -> list[Row]:
    """Every row of the page, in no particular order: `sections` sorts and pages them."""
    names = playlist_names or {}
    intents = _intents(last)
    rows = _Rows(names)
    for key, resolution in _unresolved(last, intents):
        group = resolution_outcome(resolution) if resolution is not None else "unmatched"
        reason = reason_for(resolution.step, group) if resolution is not None else _GENERIC
        intent = intents[key]
        rg = _rg_of(resolution) if resolution is not None else None
        lidarr_path = _in_lidarr(rg, last)
        if isinstance(intent, TrackIntent) and reason.code == "unavailable":
            # No name, no artist, no album: the Spotify ID is all there is to tell one from another.
            label = f"Spotify track {intent.spotify_id}"
            rows.add(group=group, reason=reason, kind="release", artist="", title=label, sources=(intent.reason,))
        elif isinstance(intent, TrackIntent):
            album_artists = intent.album.artist_names or intent.artist_names
            rows.add(
                group=group,
                reason=reason,
                kind="release",
                artist=album_artists[0] if album_artists else "",
                title=intent.album.name,
                songs=(intent.name,),
                sources=(intent.reason,),
                rg=rg,
                lidarr_path=lidarr_path,
            )
        elif isinstance(intent, AlbumIntent):
            artists = intent.album.artist_names
            rows.add(
                group=group,
                reason=reason,
                kind="release",
                artist=artists[0] if artists else "",
                title=intent.album.name,
                sources=(intent.reason,),
                rg=rg,
                lidarr_path=lidarr_path,
            )
        else:
            name, sources = intent.name, (intent.reason,)
            rows.add(group=group, reason=reason, kind="artist", artist=name, title=name, sources=sources)
    for key, step in _catalogue_gaps(last, recent_release_days):
        release = last.desired.releases[key]
        rg = release.release_group
        rows.add(
            group="unmatched",
            reason=reason_for(step, "unmatched"),
            kind="release",
            artist=rg.artist_name,
            title=rg.title,
            songs=[i.name for r in sorted(release.reasons) if isinstance(i := intents.get(r.key), TrackIntent)],
            sources=release.reasons,
            rg=rg,
            lidarr_path=f"/artist/{key.artist_mbid}" if is_mbid(key.artist_mbid) else "",
            count=len(release.reasons),
            gap=True,
        )
    return list(rows.rows.values())


# ---------------------------------------------------------------- the page


SORTS = (("artist", "Artist A-Z"), ("songs", "Most liked songs first"))


@dataclass(frozen=True, slots=True)
class Filters:
    query: str = ""
    group: str = ""
    """A group code, a reason code, or ``""`` for everything."""
    source: str = ""
    """``liked``, ``saved``, ``followed``, ``playlist`` (any), ``playlist:<id>``, or ``""``."""
    sort: str = "artist"

    def keeps(self, row: Row) -> bool:
        if self.group and self.group not in (row.group, row.reason.code):
            return False
        if self.source == "playlist":
            if not any(code.startswith("playlist:") for code in row.sources):
                return False
        elif self.source and self.source not in row.sources:
            return False
        needle = self.query.strip().casefold()
        return not needle or row.matches(needle)


def filters_from(params: Mapping[str, str], rows: Sequence[Row]) -> Filters:
    """The filters a request asked for, each checked against what the page offers: anything else
    is dropped, never echoed."""
    group = params.get("show", "")
    if group not in _GROUP and group not in _REASON:
        group = ""
    source = params.get("source", "")
    if source and source != "playlist" and source not in {code for row in rows for code in row.sources}:
        source = ""
    sort = params.get("sort", "artist")
    if sort not in dict(SORTS):
        sort = "artist"
    return Filters(query=params.get("q", "")[:MAX_QUERY], group=group, source=source, sort=sort)


def _sort_key(sort: str):
    if sort == "songs":
        return lambda r: (-len(r.songs), r.artist.casefold(), r.title.casefold())
    return lambda r: (r.artist.casefold(), r.title.casefold())


@dataclass(slots=True)
class Part:
    """One reason's rows inside a group, paged on their own."""

    group: Group
    reason: Reason
    rows: list[Row]
    matched: int
    total: int
    page: int
    pages: int
    kinds: frozenset[str] = frozenset({"release"})
    """What its rows are: releases, followed artists, or both - the unit its heading counts in."""

    @property
    def unit(self) -> str:
        if self.kinds == {"artist"}:
            return "artists"
        return "releases" if self.kinds == {"release"} else "releases and artists"

    @property
    def unit_one(self) -> str:
        if self.kinds == {"artist"}:
            return "artist"
        return "release" if self.kinds == {"release"} else "release or artist"

    @property
    def anchor(self) -> str:
        return f"part-{self.group.code}-{self.reason.code}"


def _page(rows: Sequence[Row], page: int) -> tuple[list[Row], int, int]:
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    return list(rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]), page, pages


def part(rows: Sequence[Row], filters: Filters, group: str, reason: str, page: int) -> Part | None:
    """One part, for its pager; ``None`` for a group or reason the page does not have."""
    if group not in _GROUP or reason not in _REASON:
        return None
    all_rows = [r for r in rows if r.group == group and r.reason.code == reason]
    kept = sorted((r for r in all_rows if filters.keeps(r)), key=_sort_key(filters.sort))
    shown, page, pages = _page(kept, page)
    kinds = frozenset(r.kind for r in all_rows) or frozenset({"release"})
    return Part(_GROUP[group], _REASON[reason], shown, len(kept), len(all_rows), page, pages, kinds)


def sections(rows: Sequence[Row], filters: Filters) -> list[tuple[Group, list[Part]]]:
    """Each group with rows after the filters, its parts in the order of `REASONS`, first pages."""
    out: list[tuple[Group, list[Part]]] = []
    for group in GROUPS:
        parts = []
        for reason in REASONS:
            p = part(rows, filters, group.code, reason.code, 1)
            if p is not None and p.matched:
                parts.append(p)
        if parts:
            out.append((group, parts))
    return out


@dataclass(frozen=True, slots=True)
class Card:
    group: Group
    items: int
    """Songs, saved albums and follows: the card's number, the same as Status's for the group."""
    releases: int
    """The rows those stand for (a release, or a followed artist)."""
    gaps: int = 0
    """Releases Lidarr's catalogue lacks, listed in the same section: matched, so not in `items`."""


def cards(rows: Sequence[Row]) -> list[Card]:
    """One stat card per group, in page order, empty ones included. The number counts what Status
    counts (`_unresolved`); a Lidarr catalogue gap is counted beside it, never in it."""
    out = []
    for group in GROUPS:
        theirs = [r for r in rows if r.group == group.code and not r.gap]
        gaps = sum(1 for r in rows if r.group == group.code and r.gap)
        out.append(Card(group, sum(r.items for r in theirs), len(theirs), gaps))
    return out


def reason_options(rows: Sequence[Row]) -> list[tuple[Group, list[Reason]]]:
    """For the group select: each group present, with its reasons present, in page order."""
    present = {(r.group, r.reason.code) for r in rows}
    return [
        (g, [r for r in REASONS if (g.code, r.code) in present])
        for g in GROUPS
        if any(code == g.code for code, _ in present)
    ]


_KIND_OPTIONS = (
    ("liked", "Liked songs"),
    ("saved", "Saved albums"),
    ("followed", "Followed artists"),
    ("kept", "Kept by hand"),
)


def source_options(rows: Sequence[Row]) -> list[tuple[str, str]]:
    """For the source select: the kinds present, then each playlist by name."""
    present: dict[str, str] = {}
    for row in rows:
        present.update(row.sources)
    fixed = [(code, label) for code, label in _KIND_OPTIONS if code in present]
    playlists = [(code, label) for code, label in present.items() if code.startswith("playlist:")]
    playlists.sort(key=lambda cl: cl[1].casefold())
    if playlists:
        fixed.append(("playlist", "Any playlist"))
    return fixed + playlists
