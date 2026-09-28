"""Answer "why is this monitored?" (or "why isn't it?"), first in plain words, then in detail.

`explain` is the tool's accountability surface. Every release it monitors got there through a
chain of a source intent, a resolver step and an ownership record. The report leads with a short
summary per match - what you did on Spotify, what likearr matched it to, what happened in Lidarr
and why, and whether the match looks wrong - and keeps the full chain as the detail below it.
Pure and deterministic: the same inputs always produce the same report, byte for byte.

Two things the summary must never do:

- **Claim something the run will not do.** An artist the name-collision guard skips is not "an
  artist likearr would add": the summary says it won't be added, and why.
- **Hide a mismatch.** What Spotify credits (the song, the album, its year) is shown beside what
  likearr matched (the MusicBrainz release, its year, the artist's disambiguation), and a match
  whose years are far apart, or whose artist differs from the one other songs by the same Spotify
  artist matched, is flagged as a likely wrong match. Likely, never certain.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

from likearr.core.normalize import normalize_name, normalize_title
from likearr.core.resolver import AMBIGUOUS_SAME_NAME_STEP, REMIX_ONLY_STEP, UNAVAILABLE_STEP, is_excluded
from likearr.models import (
    AlbumIntent,
    ArtistIntent,
    ArtistResolution,
    DesiredRelease,
    DesiredState,
    Guard,
    LidarrView,
    NameCollision,
    OwnedRelease,
    Profile,
    Reason,
    ReasonKind,
    ReleaseGroup,
    ReleaseKey,
    Resolution,
    ResolutionStatus,
    SourceSnapshot,
    TrackIntent,
)
from likearr.playlist_names import playlist_url

__all__ = [
    "STATUSES",
    "Explanation",
    "SummaryItem",
    "deniable",
    "deny_note",
    "describe_step",
    "explain",
    "explain_report",
    "more_note",
    "release_label",
    "render_report",
    "resolution_outcome",
]

_INDENT = "  "
_MUSICBRAINZ = "https://musicbrainz.org"

WRONG_MATCH_YEARS = 2
"""A matched release this many years or more from the album Spotify credits is flagged."""

WRONG_MATCH_NOTE = "If it is, use Not this one to stop likearr choosing it, or check the match on MusicBrainz."

_STEPS = {
    "track:album": "a liked song's album",
    "track:album:isrc": "a liked song's album, found by its ISRC",
    "track:album:related-credit": "a liked song's album, credited to a band or collaboration the artist belongs to",
    "track:isrc->album": "a liked song's studio album, found by its ISRC",
    "track:title->album": "a liked song's studio album, found by the song's title",
    "track:single-fallback": "a liked single with no album after the fallback wait",
    "track:smallest": "the smallest release holding a liked song",
    "track:smallest:covered-by-follow": "a liked song whose artist you follow",
    "track:various-artists": "a liked song on a various-artists release",
    UNAVAILABLE_STEP: "a liked song Spotify no longer serves",
    "track:non-studio": "a liked song found only on a non-studio release",
    REMIX_ONLY_STEP: "a liked song whose only release is a remix, kept although remix releases are off",
    "track:pending": "a liked single waiting for its album",
    "album:various-artists": "a saved various-artists album",
    "followed:catalogue": "a followed artist's catalogue",
    "artist:spotify-url": "a followed artist, by their Spotify link",
    "artist:spotify-url:in-library": "a followed artist already in Lidarr",
    "artist:search": "a followed artist, by name",
    "artist:ambiguous-name": "a followed artist whose name several MusicBrainz artists share",
    "artist:ambiguous-link": "a followed artist whose Spotify link points at several artists",
    AMBIGUOUS_SAME_NAME_STEP: "a name and title that two different MusicBrainz artists share",
}


def _step_seen(step: str) -> str:
    """`describe_step` for the terminal: the words, plus the raw step in brackets, so a person
    disagreeing with a match always has the exact step to report a bug against."""
    words = describe_step(step)
    return words if words == step else f"{words} [{step}]"


_CANDIDATES = re.compile(r"(\d+) candidate\(s\) considered")


DENIABLE_KINDS = frozenset({ReasonKind.LIKED, ReasonKind.PLAYLIST, ReasonKind.FOLLOWED})
"""The reasons `[rules] deny_releases` takes a release away from: a liked or playlist
song resolves elsewhere, and a followed artist's catalogue leaves the release out. A saved album
overrides the opt-outs, and a release kept by hand is never dropped, so neither is here."""


def deniable(reasons: Iterable[Reason]) -> bool:
    """Whether "Not this one" would stop this release being wanted: every reason is a deniable one."""
    kinds = {r.kind for r in reasons}
    return bool(kinds) and kinds <= DENIABLE_KINDS


def deny_note(reasons: Iterable[Reason]) -> str:
    """Where the choice lives instead, for a release "Not this one" cannot stop; ``""`` when it can
    (or when nothing wants it)."""
    kinds = {r.kind for r in reasons}
    if not kinds or kinds <= DENIABLE_KINDS:
        return ""
    if ReasonKind.MANUAL in kinds:
        return "You kept this by hand when likearr adopted your library; likearr never drops it."
    if kinds & {ReasonKind.LIKED, ReasonKind.PLAYLIST}:
        return "Your saved album still wants this; refusing it only moves the song."
    return "Unsave the album on Spotify to stop this."


def describe_step(step: str) -> str:
    """How the resolver found a release, in words; an unknown step is shown as it is."""
    if step in _STEPS:
        return _STEPS[step]
    if step.startswith("track:smallest:"):
        return _STEPS["track:smallest"]
    if step.startswith("track:excluded:"):
        return "a liked song whose only release is one you opted out of"
    if step.startswith("album:"):
        return "a saved album"
    return step or "an unrecorded step"


def _plain(detail: str) -> str:
    """The resolver's own detail line, with its bits of jargon said in words."""

    def candidates(match: re.Match[str]) -> str:
        n = int(match[1])
        return "1 possible release looked at" if n == 1 else f"{n} possible releases looked at"

    return _CANDIDATES.sub(candidates, detail)


STATUSES = (
    "downloaded",
    "waiting",
    "monitored",
    "not-monitored",
    "not-in-lidarr",
    "skipped",
    "unmatched",
    "ambiguous",
    "failed",
    "pending",
    "excluded",
    "no-longer-wanted",
    "kept-by-hand",
)
"""Where a match landed, one word per card: the web UI's status pill."""

_KINDS = {"Album": "album", "Single": "single", "EP": "EP", "Broadcast": "broadcast", "Other": "release"}


def release_label(title: str, primary: str, secondary: Sequence[str], date: str) -> str:
    """ "Tease Me (single, 1993)": two same-titled releases are otherwise the same row."""
    kind = _KINDS.get(primary, primary.lower() or "release")
    if secondary:
        kind = f"{' '.join(t.lower() for t in secondary)} {kind}"
    year = date[:4] if date[:4].isdigit() else ""
    return f"{title} ({kind}, {year})" if year else f"{title} ({kind})"


def _rg_label(rg: ReleaseGroup) -> str:
    return release_label(
        rg.title,
        str(rg.primary_type or ""),
        sorted(str(s) for s in rg.secondary_types),
        rg.first_release_date.isoformat() if rg.first_release_date else "",
    )


@dataclass(frozen=True, slots=True)
class SummaryItem:
    text: str
    """A few plain sentences: what happened, and what to do next."""
    wrong_match: bool = False
    links: tuple[tuple[str, str], ...] = ()
    """(label, url) pairs for the MusicBrainz pages the text talks about."""
    headline: str = ""
    """What a card leads with: `text` without what the facts below it say. Empty: show `text`."""
    status: str = ""
    """One of `STATUSES`."""
    facts: tuple[tuple[str, str], ...] = ()
    """(label, text): "On Spotify", "Matched to", "In Lidarr", "What likearr wants", "Why it may be wrong"."""
    detail: str = ""
    """This match's own part of the detail: the ids, the resolver's step, what it looked at."""
    lidarr_path: str = ""
    """``/artist/<mbid>`` or ``/album/<release group>`` when Lidarr holds it, to open it there."""
    release: str = ""
    """The matched release group, for "Not this one"."""


def more_note(left_out: int) -> str:
    """ "3 more matches not shown: ..."; empty for none."""
    n = left_out
    return f"{n} more match{'es' if n > 1 else ''} not shown: make the search more specific." if n > 0 else ""


@dataclass(frozen=True, slots=True)
class Explanation:
    query: str
    summary: list[SummaryItem] = field(default_factory=list)
    details: str = ""
    left_out: int = 0
    """Matches past the limit: counted, not built."""

    @property
    def more_note(self) -> str:
        return more_note(self.left_out)

    def as_dict(self) -> dict[str, object]:
        """The report as JSON-ready data: ``explain --json`` prints it, the web UI renders it."""
        return {
            "query": self.query,
            "summary": [
                {
                    "text": item.text,
                    "wrong_match": item.wrong_match,
                    "links": [{"label": label, "url": url} for label, url in item.links],
                    "headline": item.headline,
                    "status": item.status,
                    "facts": [[label, text] for label, text in item.facts],
                    "detail": item.detail,
                    "lidarr_path": item.lidarr_path,
                    "release": item.release,
                }
                for item in self.summary
            ],
            "details": self.details,
            "left_out": self.left_out,
        }


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything the blocks read, beyond the plan itself."""

    view: LidarrView
    owned: Mapping[ReleaseKey, OwnedRelease]
    resolutions: Mapping[str, Resolution]
    intents: Mapping[str, TrackIntent | AlbumIntent | ArtistIntent]
    collisions: Mapping[str, NameCollision]
    disambiguation: Callable[[str], str | None]
    playlist_names: Mapping[str, str]
    matched_artists: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """A Spotify artist's name (casefolded) to the MusicBrainz artists its songs and albums matched."""
    desired: DesiredState | None = None
    unmonitor: frozenset[ReleaseKey] | None = None
    guards: tuple[Guard, ...] = ()

    def label(self, mbid: str, name: str) -> str:
        """An artist's name with MusicBrainz's disambiguation, when there is one."""
        known = self.disambiguation(mbid) or ""
        if not known:
            for c in self.collisions.values():
                if c.wanted_mbid == mbid:
                    known = c.wanted_disambiguation
                elif c.existing_mbid == mbid:
                    known = c.existing_disambiguation
        return f"{name} ({known})" if known else name


def _matches(query: str, *fields: str) -> bool:
    """True when the normalised query is a substring of any normalised field, or an exact MBID."""
    raw = query.strip().lower()
    as_name = normalize_name(query)
    as_title = normalize_title(query)
    for f in fields:
        if not f:
            continue
        if f.lower() == raw:
            return True
        if as_name and as_name in normalize_name(f):
            return True
        if as_title and as_title in normalize_title(f):
            return True
    return False


def explain(
    query: str,
    *,
    desired: DesiredState,
    owned: Mapping[ReleaseKey, OwnedRelease],
    view: LidarrView,
    resolutions: Mapping[str, Resolution],
    artist_resolutions: Mapping[str, ArtistResolution],
    snapshot: SourceSnapshot | None = None,
    collisions: Sequence[NameCollision] = (),
    disambiguation: Callable[[str], str | None] | None = None,
    playlist_names: Mapping[str, str] | None = None,
) -> str:
    """The report as text: the summary, then the detail. See `explain_report`."""
    return render_report(
        explain_report(
            query,
            desired=desired,
            owned=owned,
            view=view,
            resolutions=resolutions,
            artist_resolutions=artist_resolutions,
            snapshot=snapshot,
            collisions=collisions,
            disambiguation=disambiguation,
            playlist_names=playlist_names,
        )
    )


def render_report(report: Explanation) -> str:
    lines = [f'likearr explain: "{report.query}"', ""]
    if report.summary:
        lines.append("In short:")
        for item in report.summary:
            lines.append(f"- {item.text}")
        if report.more_note:
            lines.append(f"- {report.more_note}")
        lines.append("")
        lines.append("Details:")
        lines.append("")
    lines.append(report.details.rstrip())
    return "\n".join(lines).rstrip() + "\n"


def explain_report(
    query: str,
    *,
    desired: DesiredState,
    owned: Mapping[ReleaseKey, OwnedRelease],
    view: LidarrView,
    resolutions: Mapping[str, Resolution],
    artist_resolutions: Mapping[str, ArtistResolution],
    snapshot: SourceSnapshot | None = None,
    collisions: Sequence[NameCollision] = (),
    disambiguation: Callable[[str], str | None] | None = None,
    playlist_names: Mapping[str, str] | None = None,
    limit: int | None = None,
    unmonitor: Collection[ReleaseKey] | None = None,
    guards: Sequence[Guard] = (),
) -> Explanation:
    """Explain everything matching `query`: artists, releases, and unresolved intents.

    The query is matched, case- and punctuation-insensitively, against artist names, release
    titles, artist MBIDs and release group MBIDs. Sorted by artist name then release title, so two
    runs over the same data produce identical reports.

    Args:
        snapshot: the Spotify read, for what Spotify credits (song, album, year). Optional: without
            it the summary says only what likearr matched.
        collisions: this run's `Diff.name_collisions`, so a skipped artist is never said to be added.
        disambiguation: MusicBrainz's one-line disambiguation by artist MBID (``ArtistDetails``).
        playlist_names: playlist id to name, for "in your playlist ...".
        unmonitor: the plan's unmonitors after its guards, and `guards` the guards themselves, so a
            release nothing asks for any more is said to be unmonitored only when the plan does it.
            ``None`` when unknown: the summary then says a guard may hold it back.
        limit: at most this many answers, in the order above; the rest are counted, not built.
            The web UI answers in its own process, where "a" must not build the whole library.
    """
    ctx = _Context(
        view=view,
        owned=owned,
        resolutions=resolutions,
        intents=_intents_by_key(snapshot),
        collisions={c.wanted_mbid: c for c in collisions},
        disambiguation=disambiguation or (lambda _mbid: None),
        playlist_names=playlist_names or {},
        matched_artists=_matched_artists(snapshot, resolutions),
        desired=desired,
        unmonitor=frozenset(unmonitor) if unmonitor is not None else None,
        guards=tuple(guards),
    )
    summary: list[SummaryItem] = []
    details: list[str] = []
    left_out = 0

    def room() -> bool:
        nonlocal left_out
        if limit is None or len(summary) < limit:
            return True
        left_out += 1
        return False

    artist_hits = sorted(
        (mbid for mbid, name in desired.artists.items() if _matches(query, name, mbid)),
        key=lambda m: (desired.artists[m].lower(), m),
    )
    artist_cards: dict[str, int] = {}
    """A shown artist's index in `summary`, and (below) the detail of their folded releases."""
    folded_detail: dict[str, list[str]] = {}
    for mbid in artist_hits:
        if not room():
            continue
        block = _artist_block(mbid, desired, artist_resolutions, ctx)
        artist_cards[mbid] = len(summary)
        folded_detail[mbid] = list(block)
        summary.append(_artist_summary(mbid, desired, ctx))
        details.extend(block)

    release_hits = sorted(
        (
            key
            for key, release in desired.releases.items()
            if _matches(
                query,
                release.release_group.title,
                key.rg_mbid,
                release.release_group.artist_name,
                key.artist_mbid,
                *_spotify_names(release, ctx),
            )
        ),
        key=lambda k: (
            desired.releases[k].release_group.artist_name.lower(),
            desired.releases[k].release_group.title.lower(),
            k.rg_mbid,
        ),
    )
    for key in release_hits:
        release = desired.releases[key]
        # A followed artist's own catalogue is answered once, by the artist's summary; a release
        # wanted for any other reason as well (a liked song) keeps its own answer.
        folded = key.artist_mbid in artist_hits and key.artist_mbid in desired.followed_artists
        block = _release_block(key, release, ctx)
        if not (folded and all(r.kind is ReasonKind.FOLLOWED for r in release.reasons)):
            if not room():
                continue
            summary.append(_release_summary(key, release, ctx, block))
        elif limit is not None and len(summary) >= limit:
            continue
        elif key.artist_mbid in folded_detail:
            folded_detail[key.artist_mbid].extend(block)
        details.extend(block)
    for mbid, index in artist_cards.items():
        summary[index] = replace(summary[index], detail=_joined(folded_detail[mbid]))

    owned_only = sorted(
        (
            key
            for key in owned
            if key not in desired.releases and _matches(query, key.rg_mbid, key.artist_mbid, _owned_title(key, view))
        ),
        key=lambda k: (k.artist_mbid, k.rg_mbid),
    )
    for key in owned_only:
        if not room():
            continue
        block = _orphan_block(key, owned[key], ctx)
        summary.append(_orphan_summary(key, owned[key], ctx, block))
        details.extend(block)

    unresolved = sorted(
        (
            r
            for r in _iter_unresolved(resolutions, artist_resolutions)
            if _matches(query, r.detail, r.intent_key, _title_of(r), *_intent_names(r.intent_key, ctx))
        ),
        key=lambda r: (r.step, r.intent_key),
    )
    for resolution in unresolved:
        if not room():
            continue
        block = _unresolved_block(resolution, ctx)
        summary.append(replace(_unresolved_summary(resolution, ctx), detail=_joined(block)))
        details.extend(block)

    report = Explanation(query=query.strip(), summary=summary, left_out=left_out)
    if left_out:
        details.append(report.more_note)
    if not summary and not left_out:
        details.append(f"nothing matches {query.strip()!r}.")
        details.append("Try an artist name, a release title, a song, an artist MBID or a release group MBID.")
    return replace(report, details="\n".join(details).rstrip() + "\n")


def _joined(block: Sequence[str]) -> str:
    return "\n".join(block).strip("\n")


# ---------------------------------------------------------------- the Spotify side


def _intents_by_key(snapshot: SourceSnapshot | None) -> dict[str, TrackIntent | AlbumIntent | ArtistIntent]:
    if snapshot is None:
        return {}
    out: dict[str, TrackIntent | AlbumIntent | ArtistIntent] = {}
    for intent in (*snapshot.tracks, *snapshot.albums, *snapshot.artists):
        out.setdefault(intent.reason.key, intent)
    return out


def _spotify_names(release: DesiredRelease, ctx: _Context) -> list[str]:
    """What Spotify calls the songs and albums behind a release, so a song title finds it too."""
    names: list[str] = []
    for r in release.reasons:
        names.extend(_intent_names(r.key, ctx))
    return names


def _intent_names(key: str, ctx: _Context) -> list[str]:
    intent = ctx.intents.get(key)
    if isinstance(intent, TrackIntent):
        return [intent.name, intent.album.name]
    if isinstance(intent, AlbumIntent):
        return [intent.album.name]
    if isinstance(intent, ArtistIntent):
        return [intent.name]
    return []


def _year(value: object) -> int | None:
    return getattr(value, "year", None) if value is not None else None


def _who(names: Sequence[str]) -> str:
    return " and ".join(names) if names else "an unknown artist"


def _what_you_did(reason: Reason, ctx: _Context) -> str:
    """One sentence: what the user did on Spotify, and what Spotify credits."""
    intent = ctx.intents.get(reason.key)
    if isinstance(intent, TrackIntent):
        album = intent.album
        year = _year(album.release_date)
        credit = f' (on Spotify: the album "{album.name}"' + (f", {year}" if year else "") + ")"
        if reason.kind is ReasonKind.PLAYLIST:
            return f'"{intent.name}" by {_who(intent.artist_names)} is in {_playlist(reason, ctx)}{credit}.'
        return f'You liked "{intent.name}" by {_who(intent.artist_names)}{credit}.'
    if isinstance(intent, AlbumIntent):
        year = _year(intent.album.release_date)
        return f'You saved the album "{intent.album.name}" by {_who(intent.album.artist_names)}' + (
            f" ({year})." if year else "."
        )
    if isinstance(intent, ArtistIntent):
        return f"You follow {intent.name} on Spotify."
    return _reason_phrase(reason, ctx).capitalize() + "."


def _playlist(reason: Reason, ctx: _Context) -> str:
    """'your playlist "Road trip"', or - its name not known yet - "one of your playlists", never
    its raw id (a Look up links the playlist on Spotify instead)."""
    name = ctx.playlist_names.get(reason.playlist_id or "")
    return f'your playlist "{name}"' if name else "one of your playlists"


def _reason_phrase(reason: Reason, ctx: _Context) -> str:
    """A reason in words, for the detail lines: 'you liked "Busy Earnin\'"'."""
    intent = ctx.intents.get(reason.key)
    song = f'"{intent.name}"' if isinstance(intent, TrackIntent) else "a song"
    if reason.kind is ReasonKind.LIKED:
        return f"you liked {song}"
    if reason.kind is ReasonKind.PLAYLIST:
        return f"{song} is in {_playlist(reason, ctx)}"
    if reason.kind is ReasonKind.SAVED:
        return f'you saved the album "{intent.album.name}"' if isinstance(intent, AlbumIntent) else "you saved an album"
    if reason.kind is ReasonKind.FOLLOWED:
        return "you follow the artist"
    if reason.kind is ReasonKind.PENDING_ALBUM:
        return "a liked single waiting for its album"
    return "kept by hand when likearr adopted the library"


# ---------------------------------------------------------------- summaries


def _collision_sentence(c: NameCollision, *, lead: str) -> str:
    theirs = f"{c.existing_disambiguation}, " if c.existing_disambiguation else ""
    if not c.in_lidarr:
        return (
            f"{lead}likearr also wants a different artist named {c.existing_name or c.name} "
            f"({theirs}{c.existing_mbid}), and Lidarr can't hold two artists with the same name, so neither is added"
        )
    return (
        f"{lead}Lidarr already has a different artist named {c.existing_name or c.name} "
        f"({theirs}Lidarr id {c.existing_lidarr_id}), and Lidarr can't hold two artists with the same name"
    )


def _artist_summary(mbid: str, desired: DesiredState, ctx: _Context) -> SummaryItem:
    name = desired.artists[mbid]
    label = ctx.label(mbid, name)
    links = [("the artist on MusicBrainz", f"{_MUSICBRAINZ}/artist/{mbid}")]
    collision = ctx.collisions.get(mbid)
    artist = ctx.view.artists.get(mbid)
    followed = mbid in desired.followed_artists
    parts: list[str] = []
    wanted = [r for k, r in desired.releases.items() if k.artist_mbid == mbid]
    count = desired.followed_counts.get(mbid, 0)
    profile = str(desired.profile_needs.get(mbid, Profile.LEAN)).capitalize()
    others = len(wanted) - count if followed else len(wanted)
    if followed:
        headline = f"You follow {label} on Spotify, so likearr wants their studio albums and EPs."
        spotify = "You follow them" + (
            f"; songs or albums you liked, saved or keep in playlists want {others} more of their releases"
            if others > 0
            else ""
        )
    else:
        headline = f"likearr wants {label} for songs or albums you liked, saved or keep in playlists."
        spotify = "Not followed"
    facts: list[tuple[str, str]] = [("On Spotify", spotify), ("Matched to", label)]
    status = "not-in-lidarr"
    lidarr_path = ""
    if followed:
        parts.append(
            f"You follow {label} on Spotify, so likearr wants their studio albums and EPs - {count} of them - "
            f"on the {profile} profile (the followed_artists setting is on)."
        )
    if artist is not None:
        releases = sorted(
            (r for k, r in desired.releases.items() if k.artist_mbid == mbid),
            key=lambda r: (r.release_group.first_release_date or date.max, r.release_group.title.lower()),
        )
        albums = [(r, ctx.view.album(r.key)) for r in releases]
        monitored = [r for r, a in albums if a is not None and a.monitored]
        with_files = [r for r, a in albums if a is not None and a.has_files]
        where = f"In Lidarr as artist id {artist.id}" if artist.id else "In Lidarr"
        if not followed:
            where = f"{label} is in Lidarr" + (f" as artist id {artist.id}" if artist.id else "")
        if releases:
            others = len(releases) - desired.followed_counts.get(mbid, 0) if followed else 0
            of = (
                f"of the {len(releases)} releases likearr wants from them (the {len(releases) - others} above, "
                f"plus {others} for songs or albums you liked, saved or keep in playlists)"
                if others > 0
                else f"of the {len(releases)}"
            )
            parts.append(f"{where}: {of}, {len(monitored)} monitored, {len(with_files)} with files on disk.")
            facts.append(
                ("In Lidarr", f"{len(monitored)} of {len(releases)} monitored, {len(with_files)} with files on disk")
            )
        else:
            parts.append(f"{where}.")
            facts.append(("In Lidarr", "Yes"))
        lidarr_path = f"/artist/{mbid}"
        if releases and len(with_files) == len(releases):
            status = "downloaded"
        else:
            status = "monitored" if monitored else "not-monitored"
        if followed:
            waiting = [r for r, a in albums if a is not None and not a.monitored]
            missing = [r for r, a in albums if a is None]
            if monitored:
                parts.append(f"Monitored: {_titles(monitored)}.")
            if waiting:
                it = "it" if len(waiting) == 1 else "them"
                parts.append(f"Not monitored yet: {_titles(waiting)}; the next apply monitors {it}.")
            if missing:
                parts.append(f"Not in Lidarr's list of their releases yet: {_titles(missing)}.")
    elif collision is not None:
        parts.append(f"{label} won't be added: " + _collision_sentence(collision, lead="") + ".")
        facts.append(("In Lidarr", _skipped(collision)))
        status = "skipped"
        if collision.existing_mbid:
            links.append((_other_artist_link(collision), f"{_MUSICBRAINZ}/artist/{collision.existing_mbid}"))
    else:
        parts.append(f"{label} isn't in Lidarr yet; the next apply adds them.")
        facts.append(("In Lidarr", "Not yet: the next apply adds them"))
    if followed:
        facts.append(("What likearr wants", f"Studio albums and EPs ({count}), {profile} profile"))
    doubtful = bool(wanted) and all(_wrong_match(r.key, r, ctx) for r in wanted)
    if doubtful:
        one = len(wanted) == 1
        parts.append(
            ("The only release" if one else "Every release")
            + f" likearr wants from {name} looks like a wrong match (see "
            + ("its answer" if one else "their answers")
            + f" below). {WRONG_MATCH_NOTE}"
        )
        facts.append(
            (
                "Why it may be wrong",
                ("The only release" if one else "Every release")
                + f" likearr wants from them looks like a wrong match. {WRONG_MATCH_NOTE}",
            )
        )
    return SummaryItem(
        " ".join(parts),
        wrong_match=doubtful,
        links=tuple(links),
        headline=headline,
        status=status,
        facts=tuple(facts),
        lidarr_path=lidarr_path,
    )


def _other_artist_link(collision: NameCollision) -> str:
    if collision.in_lidarr:
        return "the artist Lidarr has, on MusicBrainz"
    return "the other artist of that name, on MusicBrainz"


def _skipped(collision: NameCollision) -> str:
    theirs = f" ({collision.existing_disambiguation})" if collision.existing_disambiguation else ""
    if not collision.in_lidarr:
        return f"Not added: likearr also wants a different {collision.existing_name or collision.name}{theirs}"
    return f"Not added: Lidarr already has a different {collision.existing_name or collision.name}{theirs}"


def _on_spotify(reasons: Iterable[Reason], ctx: _Context) -> str:
    """Every reason a release is wanted, in a few words each: the card's "On Spotify"."""
    ordered = sorted(reasons, key=lambda r: (r.kind is not ReasonKind.LIKED, r.key))
    phrases = list(dict.fromkeys(_reason_phrase(r, ctx) for r in ordered))
    more = len(phrases) - _REASONS_SHOWN
    text = "; ".join(phrases[:_REASONS_SHOWN]) + (f"; and {more} more" if more > 0 else "")
    return text[:1].upper() + text[1:]


_REASONS_SHOWN = 3


_TITLES_SHOWN = 10


def _titles(releases: Sequence[DesiredRelease]) -> str:
    """ "Living Room (2016), Hotel TV (2020)", and how many more past the first ten."""
    shown = []
    for release in releases[:_TITLES_SHOWN]:
        year = _year(release.release_group.first_release_date)
        shown.append(release.release_group.title + (f" ({year})" if year else ""))
    more = len(releases) - _TITLES_SHOWN
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def _outcome(key: ReleaseKey, ctx: _Context) -> str:
    collision = ctx.collisions.get(key.artist_mbid)
    in_lidarr = key.artist_mbid in ctx.view.artists
    album = ctx.view.album(key)
    if not in_lidarr and collision is not None:
        theirs = f"{collision.existing_disambiguation}, " if collision.existing_disambiguation else ""
        if not collision.in_lidarr:
            return (
                f"likearr also wants a different {collision.existing_name or collision.name} "
                f"({theirs}{collision.existing_mbid}), and Lidarr can't hold two artists with the same name, "
                "so likearr added neither and nothing was downloaded."
            )
        return (
            f"Lidarr already has a different {collision.existing_name or collision.name} "
            f"({theirs}Lidarr id {collision.existing_lidarr_id}), and Lidarr can't hold two artists with the same "
            "name, so likearr skipped this one and nothing was downloaded."
        )
    if not in_lidarr:
        return "The next apply adds the artist to Lidarr and monitors it."
    if album is None:
        return "Lidarr's catalogue for the artist doesn't list this release yet; likearr tries again every run."
    if album.monitored:
        files = album.track_file_count
        return "It's monitored in Lidarr" + (
            f", with {files} file(s) on disk." if files else "; nothing is downloaded yet."
        )
    return "The next apply monitors it in Lidarr."


def _wrong_match(key: ReleaseKey, release: DesiredRelease, ctx: _Context) -> list[str]:
    """Reasons to doubt the match, each one sentence. Empty when there are none."""
    rg = release.release_group
    doubts: list[str] = []
    matched_year = _year(rg.first_release_date)
    for reason in sorted(release.reasons, key=lambda r: r.key):
        intent = ctx.intents.get(reason.key)
        album = intent.album if isinstance(intent, (TrackIntent, AlbumIntent)) else None
        spotify_year = _year(album.release_date) if album is not None else None
        if spotify_year and matched_year and abs(spotify_year - matched_year) > WRONG_MATCH_YEARS:
            doubts.append(
                f"This looks like a wrong match: Spotify's album is from {spotify_year}, "
                f"the one likearr matched from {matched_year}."
            )
            break
    artist = _first_spotify_artist(release, ctx)
    if artist and _others_matched_elsewhere(artist, rg, ctx):
        doubts.append(
            f"Other songs by {artist} on Spotify matched a different artist, so this may be the wrong {artist}."
        )
    return doubts


def _first_spotify_artist(release: DesiredRelease, ctx: _Context) -> str:
    for reason in sorted(release.reasons, key=lambda r: r.key):
        intent = ctx.intents.get(reason.key)
        if isinstance(intent, TrackIntent) and intent.artist_names:
            return intent.artist_names[0]
        if isinstance(intent, AlbumIntent) and intent.album.artist_names:
            return intent.album.artist_names[0]
    return ""


def _others_matched_elsewhere(artist: str, rg: ReleaseGroup, ctx: _Context) -> bool:
    """Whether another song or album by the same Spotify artist resolved to a different MB artist."""
    return any(mbid != rg.artist_mbid for mbid in ctx.matched_artists.get(artist.casefold(), ()))


def _matched_artists(
    snapshot: SourceSnapshot | None, resolutions: Mapping[str, Resolution]
) -> dict[str, frozenset[str]]:
    """Built once per report, so the wrong-artist check costs a lookup rather than a scan."""
    out: dict[str, set[str]] = {}
    for key, intent in _intents_by_key(snapshot).items():
        names = (
            intent.artist_names
            if isinstance(intent, TrackIntent)
            else intent.album.artist_names
            if isinstance(intent, AlbumIntent)
            else ()
        )
        resolution = resolutions.get(key)
        if (
            names
            and resolution is not None
            and resolution.status is ResolutionStatus.RESOLVED
            and resolution.release_group is not None
        ):
            out.setdefault(names[0].casefold(), set()).add(resolution.release_group.artist_mbid)
    return {name: frozenset(mbids) for name, mbids in out.items()}


def _release_state(key: ReleaseKey, ctx: _Context) -> tuple[str, str]:
    """The status and the "In Lidarr" fact of a wanted release; `_outcome` says the same at length."""
    collision = ctx.collisions.get(key.artist_mbid)
    in_lidarr = key.artist_mbid in ctx.view.artists
    album = ctx.view.album(key)
    if not in_lidarr and collision is not None:
        return "skipped", _skipped(collision)
    if not in_lidarr:
        return "not-in-lidarr", "Not yet: the next apply adds the artist and monitors it"
    if album is None:
        return "not-in-lidarr", "Not in Lidarr's list of the artist's releases yet; likearr tries again every run"
    if album.monitored:
        files = album.track_file_count
        if files:
            return "downloaded", f"Monitored, {files} file{'' if files == 1 else 's'} on disk"
        return "waiting", "Monitored, nothing downloaded yet"
    return "not-monitored", "Not monitored yet: the next apply monitors it"


def _release_summary(key: ReleaseKey, release: DesiredRelease, ctx: _Context, block: Sequence[str] = ()) -> SummaryItem:
    rg = release.release_group
    reasons = sorted(release.reasons, key=lambda r: (r.kind is not ReasonKind.LIKED, r.key))
    first = next((r for r in reasons if r.key in ctx.intents), reasons[0] if reasons else None)
    parts = [_what_you_did(first, ctx)] if first is not None else []
    headline = list(parts)
    if len(reasons) > 1:
        parts.append(f"({len(reasons) - 1} other reason(s) want it too.)")
    year = _year(rg.first_release_date)
    matched = (
        f'likearr matched it to "{rg.title}"'
        + (f" ({year})" if year else "")
        + f" by {ctx.label(rg.artist_mbid, rg.artist_name)}."
    )
    parts.append(matched)
    headline.append(matched)
    parts.append(_outcome(key, ctx))
    status, in_lidarr = _release_state(key, ctx)
    facts = [
        ("On Spotify", _on_spotify(release.reasons, ctx)),
        ("Matched to", f"{_rg_label(rg)} by {ctx.label(rg.artist_mbid, rg.artist_name)}"),
        ("In Lidarr", in_lidarr),
    ]
    note = deny_note(release.reasons)
    if note:
        facts.append(("To stop monitoring it", note))
    doubts = _wrong_match(key, release, ctx)
    if doubts:
        parts.extend(doubts)
        parts.append(WRONG_MATCH_NOTE)
        facts.append(("Why it may be wrong", " ".join([*doubts, WRONG_MATCH_NOTE])))
    if ctx.view.album(key) is not None:
        lidarr_path = f"/album/{rg.mbid}"
    elif key.artist_mbid in ctx.view.artists:
        lidarr_path = f"/artist/{key.artist_mbid}"
    else:
        lidarr_path = ""
    links = [
        ("the matched release on MusicBrainz", f"{_MUSICBRAINZ}/release-group/{rg.mbid}"),
        ("the matched artist on MusicBrainz", f"{_MUSICBRAINZ}/artist/{rg.artist_mbid}"),
    ]
    collision = ctx.collisions.get(key.artist_mbid)
    if collision is not None and collision.existing_mbid and key.artist_mbid not in ctx.view.artists:
        links.append((_other_artist_link(collision), f"{_MUSICBRAINZ}/artist/{collision.existing_mbid}"))
    if first is not None and first.kind is ReasonKind.PLAYLIST and first.playlist_id not in ctx.playlist_names:
        url = playlist_url(first.playlist_id or "")
        if url is not None:
            links.append(("the playlist on Spotify", url))
    return SummaryItem(
        " ".join(parts),
        wrong_match=bool(doubts),
        links=tuple(links),
        headline=" ".join(headline),
        status=status,
        facts=tuple(facts),
        detail=_joined(block),
        lidarr_path=lidarr_path,
        release=rg.mbid if deniable(release.reasons) else "",
    )


def _unresolved_summary(resolution: Resolution | ArtistResolution, ctx: _Context) -> SummaryItem:
    intent = ctx.intents.get(resolution.intent_key)
    if isinstance(intent, TrackIntent) and intent.reason.kind is ReasonKind.PLAYLIST:
        what = f'"{intent.name}" by {_who(intent.artist_names)} is in {_playlist(intent.reason, ctx)}'
    elif isinstance(intent, TrackIntent):
        what = f'You liked "{intent.name}" by {_who(intent.artist_names)}'
    elif isinstance(intent, AlbumIntent):
        what = f'You saved the album "{intent.album.name}" by {_who(intent.album.artist_names)}'
    elif isinstance(intent, ArtistIntent):
        what = f"You follow {intent.name}"
    else:
        what = f"The Spotify item {resolution.intent_key}"
    outcome = resolution_outcome(resolution)
    nothing = ("In Lidarr", "Nothing monitored for it")
    looked = f" ({_plain(resolution.detail)})" if resolution.detail else ""
    if outcome == "pending":
        single = resolution.single_release_group if isinstance(resolution, Resolution) else None
        return SummaryItem(
            f"{what}. It's a single with no album yet; likearr waits for the album before monitoring anything.",
            status="pending",
            facts=(
                ("Matched to", (f'The single "{single.title}"' if single else "A single") + ", waiting for its album"),
                nothing,
            ),
        )
    if outcome == "excluded":
        headline = (
            f"{what}, but the only release it's on is one your settings leave out, so nothing is monitored for it."
        )
        return SummaryItem(
            f"{headline} {_plain(resolution.detail)}".rstrip(),
            headline=headline,
            status="excluded",
            facts=(("Matched to", f"Only releases your settings leave out{looked}"), nothing),
        )
    if outcome == "failed":
        return SummaryItem(
            f"{what}, but looking it up failed this run, so nothing is monitored for it yet; likearr tries "
            "again at the next run.",
            status="failed",
            facts=(("Matched to", "Nothing yet: the MusicBrainz lookup failed; likearr tries again next run"), nothing),
        )
    if outcome == "ambiguous":
        held = _held_from_before(resolution.intent_key, ctx)
        if held:
            kept = (
                f"likearr keeps {_titles_quoted(held)} monitored from before until it can tell which artist you meant"
            )
        else:
            kept = "likearr monitors nothing new for it"
        headline = (
            f"{what}, but two different artists share this name and album title; likearr couldn't tell which "
            f"one you meant, so {kept}."
        )
        return SummaryItem(
            f"{headline} {_plain(resolution.detail)}".rstrip(),
            headline=headline,
            status="ambiguous",
            facts=(
                ("Matched to", f"Nothing: two different artists share this name and title{looked}"),
                ("In Lidarr", f"Kept monitored from before: {_titles_quoted(held)}") if held else nothing,
            ),
        )
    headline = f"{what}, but likearr couldn't match it to MusicBrainz, so nothing is monitored for it."
    return SummaryItem(
        f"{headline} {_plain(resolution.detail)}".rstrip(),
        headline=headline,
        status="unmatched",
        facts=(("Matched to", f"Nothing on MusicBrainz{looked}"), nothing),
    )


def _held_from_before(intent_key: str, ctx: _Context) -> list[str]:
    """Titles of releases likearr owns for this intent and Lidarr still has monitored.

    An unresolved intent never lets go of what it held (`core.diff`'s `still_live`): a like that
    resolved to a release before and is ambiguous now keeps that release monitored, so "nothing is
    monitored for it" would be false. Empty when ownership is not known, as on the unmatched page.
    """
    titles: list[str] = []
    for key, record in sorted(ctx.owned.items(), key=lambda kv: (kv[0].artist_mbid, kv[0].rg_mbid)):
        if not any(r.key == intent_key for r in record.reasons):
            continue
        album = ctx.view.album(key)
        if album is not None and album.monitored:
            titles.append(album.title or key.rg_mbid)
    return titles


def _titles_quoted(titles: Sequence[str]) -> str:
    quoted = [f'"{t}"' for t in titles]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


LOOKUP_FAILED_STEP = "error:metadata"
"""The resolver's step for an intent whose MusicBrainz lookup failed this run (`core.resolver`)."""


def resolution_outcome(resolution: Resolution | ArtistResolution) -> str:
    """Where an intent landed: ``matched``, ``pending`` (a single waiting for its album),
    ``excluded`` (left out by the settings: a remix, a compilation, a denied release), ``failed``
    (the lookup failed this run; retried at the next), ``ambiguous`` (two different artists share
    the name and the title, and nothing said which was meant) or ``unmatched``
    (nothing found: a true miss)."""
    if resolution.status is ResolutionStatus.RESOLVED:
        return "matched"
    if resolution.status is ResolutionStatus.PENDING_ALBUM:
        return "pending"
    if is_excluded(resolution):
        return "excluded"
    if resolution.step == LOOKUP_FAILED_STEP:
        return "failed"
    if resolution.step == AMBIGUOUS_SAME_NAME_STEP:
        return "ambiguous"
    return "unmatched"


# ---------------------------------------------------------------- detail blocks


def _iter_unresolved(
    resolutions: Mapping[str, Resolution],
    artist_resolutions: Mapping[str, ArtistResolution],
) -> Iterable[Resolution | ArtistResolution]:
    for r in resolutions.values():
        if r.status != ResolutionStatus.RESOLVED:
            yield r
    for a in artist_resolutions.values():
        if a.status != ResolutionStatus.RESOLVED:
            yield a


def _title_of(resolution: Resolution | ArtistResolution) -> str:
    if isinstance(resolution, ArtistResolution):
        return resolution.artist_name
    for rg in (resolution.release_group, resolution.single_release_group, resolution.source_release_group):
        if rg is not None:
            return rg.title
    return ""


def _owned_title(key: ReleaseKey, view: LidarrView) -> str:
    album = view.album(key)
    return album.title if album is not None else ""


def _artist_block(
    mbid: str,
    desired: DesiredState,
    artist_resolutions: Mapping[str, ArtistResolution],
    ctx: _Context,
) -> list[str]:
    name = desired.artists[mbid]
    out = [f"artist: {ctx.label(mbid, name)} ({mbid})"]
    artist = ctx.view.artists.get(mbid)
    collision = ctx.collisions.get(mbid)
    if artist is not None:
        out.append(
            f"{_INDENT}in Lidarr: yes (id {artist.id}, metadata profile {artist.metadata_profile_id}, "
            f"monitorNewItems={artist.monitor_new_items})"
        )
    elif collision is not None:
        out.append(f"{_INDENT}in Lidarr: no, and won't be added - " + _collision_sentence(collision, lead=""))
    else:
        out.append(f"{_INDENT}in Lidarr: no - the next apply adds it")
    profile = desired.profile_needs.get(mbid, Profile.LEAN)
    out.append(f"{_INDENT}profile needed: {profile}")
    if mbid in desired.followed_artists:
        count = desired.followed_counts.get(mbid, 0)
        out.append(f"{_INDENT}followed on Spotify: yes - {count} studio album(s)/EP(s) wanted")
        for resolution in sorted(artist_resolutions.values(), key=lambda r: r.intent_key):
            if resolution.artist_mbid == mbid:
                out.append(f"{_INDENT}matched as {_step_seen(resolution.step)}: {_plain(resolution.detail)}")
    else:
        out.append(f"{_INDENT}followed on Spotify: no - wanted only through individual releases")
    out.append("")
    return out


def _release_block(key: ReleaseKey, release: DesiredRelease, ctx: _Context) -> list[str]:
    rg = release.release_group
    out = [f"release: {rg.artist_name} - {rg.title} ({rg.mbid})"]
    kind = str(rg.primary_type) if rg.primary_type else "unknown type"
    secondary = ", ".join(sorted(str(s) for s in rg.secondary_types))
    year = _year(rg.first_release_date)
    out.append(
        f"{_INDENT}type: {kind}"
        + (f" + {secondary}" if secondary else "")
        + (" (studio)" if rg.is_studio else "")
        + (f", first released {year}" if year else "")
    )
    out.append(f"{_INDENT}artist: {ctx.label(rg.artist_mbid, rg.artist_name)} ({rg.artist_mbid})")
    album = ctx.view.album(key)
    if album is None:
        out.append(f"{_INDENT}in Lidarr: no album with this release group")
    else:
        state = "monitored" if album.monitored else "not monitored"
        out.append(f"{_INDENT}in Lidarr: {state}, album id {album.id}, {album.track_file_count} file(s)")
    record = ctx.owned.get(key)
    if record is None:
        out.append(f"{_INDENT}owned by likearr: no - it will never be unmonitored by this tool")
    else:
        out.append(
            f"{_INDENT}owned by likearr: yes since {record.monitored_at.isoformat()} "
            f"(resolver v{record.resolver_version}, found as {_step_seen(record.step)})"
        )
    out.append(f"{_INDENT}wanted because:")
    for reason in sorted(release.reasons, key=lambda r: r.key):
        step = release.steps.get(reason.key, "")
        detail = ctx.resolutions[reason.key].detail if reason.key in ctx.resolutions else ""
        out.append(f"{_INDENT * 2}{_reason_phrase(reason, ctx)} - found as {_step_seen(step)} [{reason.key}]")
        if detail:
            out.append(f"{_INDENT * 3}{_plain(detail)}")
    if record is not None:
        stale = record.reasons - release.reasons
        for reason in sorted(stale, key=lambda r: r.key):
            out.append(f"{_INDENT * 2}{_reason_phrase(reason, ctx)} - recorded earlier, no longer resolving here")
    out.append("")
    return out


def _orphan_block(key: ReleaseKey, record: OwnedRelease, ctx: _Context) -> list[str]:
    album = ctx.view.album(key)
    title = album.title if album is not None else "(not in Lidarr)"
    out = [f"release: {title} ({key.rg_mbid})"]
    out.append(f"{_INDENT}artist mbid: {key.artist_mbid}")
    if album is None:
        out.append(f"{_INDENT}in Lidarr: no album with this release group")
    else:
        out.append(
            f"{_INDENT}in Lidarr: {'monitored' if album.monitored else 'not monitored'}, "
            f"album id {album.id}, {album.track_file_count} file(s)"
        )
    out.append(
        f"{_INDENT}owned by likearr: yes since {record.monitored_at.isoformat()} (found as {_step_seen(record.step)})"
    )
    out.append(f"{_INDENT}wanted because: nothing - no source asks for it any more")
    for reason in sorted(record.reasons, key=lambda r: r.key):
        out.append(f"{_INDENT * 2}{_reason_phrase(reason, ctx)} [lost]")
    out.append(f"{_INDENT}outcome: {_orphan_outcome(key, record, ctx)}")
    out.append("")
    return out


def _orphan_summary(key: ReleaseKey, record: OwnedRelease, ctx: _Context, block: Sequence[str]) -> SummaryItem:
    album = ctx.view.album(key)
    title = (album.title if album is not None else "") or key.rg_mbid
    if album is None:
        in_lidarr = "Not in Lidarr"
    elif album.monitored:
        files = album.track_file_count
        in_lidarr = f"Monitored, {files} file{'' if files == 1 else 's'} on disk" if files else "Monitored, no files"
    else:
        in_lidarr = "Not monitored"
    return SummaryItem(
        f'"{title}": ' + _orphan_outcome(key, record, ctx),
        links=(("the release on MusicBrainz", f"{_MUSICBRAINZ}/release-group/{key.rg_mbid}"),),
        status="kept-by-hand" if record.is_manual else "no-longer-wanted",
        facts=(("On Spotify", "Nothing asks for it any more"), ("In Lidarr", in_lidarr)),
        detail=_joined(block),
        lidarr_path=f"/album/{key.rg_mbid}" if album is not None else "",
    )


def _orphan_outcome(key: ReleaseKey, record: OwnedRelease, ctx: _Context) -> str:
    """What happens to a release likearr owns that no source wants: the diff's own rules
    (`core.diff`, "releases to unmonitor"), then the plan's guards. Summary and detail both say
    this, so they cannot disagree."""
    album = ctx.view.album(key)
    if record.is_manual:
        return (
            "it was kept by hand when likearr adopted the library, so likearr never unmonitors it, "
            "though nothing on Spotify asks for it any more."
        )
    if album is None or not album.monitored:
        return "nothing on Spotify asks for it any more, and it isn't monitored in Lidarr, so there is nothing to do."
    live = [r for r in record.reasons if _still_live(r, key, ctx)]
    if live:
        return (
            f"{_reason_phrase(live[0], ctx)} is still on Spotify but didn't match anything this run (MusicBrainz "
            "trouble, for example), so likearr keeps it monitored rather than read a failed match as an unlike."
        )
    held = [g for g in ctx.guards if g.blocked_unmonitors]
    if ctx.unmonitor is not None and key in ctx.unmonitor:
        return (
            "it is monitored because of likearr, but nothing on Spotify asks for it any more, so the next apply "
            "unmonitors it (files already downloaded stay where they are)."
        )
    if ctx.unmonitor is not None and held:
        return (
            "nothing on Spotify asks for it any more, so likearr would unmonitor it, but a guard held that back: "
            + "; ".join(g.message for g in held)
            + "."
        )
    return (
        "nothing on Spotify asks for it any more, so likearr unmonitors it at the next apply unless a guard "
        "holds it back (files already downloaded stay where they are)."
    )


def _still_live(reason: Reason, key: ReleaseKey, ctx: _Context) -> bool:
    """`core.diff`'s rule: a reason still in the source that resolved nowhere else this run merely
    failed to match, and holds its release. An opted-out one lets go."""
    if reason.key not in ctx.intents:
        return False
    resolution = ctx.resolutions.get(reason.key)
    if resolution is not None and is_excluded(resolution):
        return False
    if ctx.desired is not None:
        elsewhere = {k for k, r in ctx.desired.releases.items() if reason in r.reasons} - {key}
        if elsewhere:
            return False
    return True


def _unresolved_block(resolution: Resolution | ArtistResolution, ctx: _Context) -> list[str]:
    label = "pending album" if resolution.status == ResolutionStatus.PENDING_ALBUM else "unmapped"
    names = _intent_names(resolution.intent_key, ctx)
    out = [f"{label}: {names[0] if names else resolution.intent_key} [{resolution.intent_key}]"]
    out.append(f"{_INDENT}looked for as: {_step_seen(resolution.step)}")
    out.append(f"{_INDENT}{_plain(resolution.detail)}")
    if isinstance(resolution, Resolution) and resolution.single_release_group is not None:
        single = resolution.single_release_group
        when = resolution.single_release_date.isoformat() if resolution.single_release_date else "unknown"
        out.append(f"{_INDENT}waiting on: {single.title!r} ({single.mbid}), single released {when}")
    out.append("")
    return out
