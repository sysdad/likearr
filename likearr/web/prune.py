"""The prune review in the web UI: a prune-report, decided per artist, exported as files.

`likearr prune-report` runs as a job and writes `prune.json` into its job directory. This module
reads that report, keeps the reviewer's decisions in a draft beside it, and turns them into the
two files the terminal steps take - it exports, it never executes:

- **the decisions file** (docs/cli.md, "Decisions file"): ``trash`` and
  ``trash_artists`` for `prune-stage --decisions`; ``promote``, ``save``, ``save_releases`` and
  ``save_exclude_releases`` for `promote-save`;
- **the review snapshot** (``review-data.json``) that `promote-save --reviewed` requires: the
  artists and releases the reviewer looked at, with their file counts at review time and whether
  each one is to be saved.

Per artist the reviewer picks keep (no change on Spotify), promote (keep, and follow the artist on
Spotify), save (keep, and save all their albums on Spotify) or trash (stage every candidate album).
An album can override its artist: keep it with no change on Spotify - which also takes it out of
its artist's save - keep it and save it on its own, or trash it. A protected row - the only local
copy of a liked track - is never trashed, whatever the artist's decision says; it can still be
saved, because saving moves no file. It says why in plain words (`kept_why`): the song by its
title and the playlist by its name, never an id.

**Carried over.** Every export is recorded in the ledger (`likearr.prune_ledger`), and a
report's draft is pre-filled from what the ledger holds from *other* reviews (`prefill`): what was
kept before is kept again, and says when. Two things never pre-fill: a past trash (it is on disk
again and needs a fresh look) and a past follow or save (every Spotify write comes from a click in
this review). The list opens on the artists with something not decided before.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from likearr.config import is_mbid
from likearr.core.prune import PROTECTION_KINDS, Protection, split_track_key
from likearr.fsio import write_atomic
from likearr.prune_ledger import ARTIST_DECISIONS as LEDGER_ARTIST_DECISIONS
from likearr.prune_ledger import RELEASE_DECISIONS as LEDGER_RELEASE_DECISIONS
from likearr.prune_ledger import Entry, Ledger, parse_entries

__all__ = [
    "ARTIST_DECISIONS",
    "PAGE_SIZE",
    "RELEASE_OVERRIDES",
    "SHOW_FILTERS",
    "DecisionError",
    "Draft",
    "KeptWhy",
    "PruneArtist",
    "PruneRelease",
    "PruneView",
    "StaleChange",
    "decide",
    "decisions_file",
    "effective_decision",
    "human_day",
    "kept_why",
    "ledger_changes",
    "ledger_source",
    "legacy_protection",
    "needs_decision",
    "net_effect",
    "prefill",
    "read_draft",
    "read_report",
    "review_data",
    "select_artists",
    "why_unasked",
    "write_draft",
]

ARTIST_DECISIONS = ("undecided", "keep", "promote", "save", "trash")
"""``undecided`` exports nothing, exactly like ``keep``; it is there so progress can be counted."""

RELEASE_OVERRIDES = ("", "keep", "save", "trash")
"""``""`` follows the artist's decision. ``keep`` is keep with no change on Spotify - also out of
an artist-level save. ``save`` is keep, and save this one album on Spotify."""

SHOW_FILTERS = ("needs", "all", *ARTIST_DECISIONS)
"""What the list can be narrowed to. ``needs`` - the default - is the artists with at least one
album not decided in an earlier review; ``all`` is everything."""

PAGE_SIZE = 25
MAX_NOTES = 4000

_CARRIED = ("keep", "save")
"""A past decision that counts as decided. It pre-fills as a plain keep, never as a save; and a
past ``trash`` never pre-fills at all: see `prefill`."""


@dataclass(frozen=True, slots=True)
class PruneRelease:
    rg_mbid: str
    title: str
    kind: str
    """The primary type and any secondary types, as words ("Album", "Album + Live")."""
    released: str
    files: int
    size: int
    protected_reason: str = ""
    """Non-empty for a protected row, which can never be trashed; empty for a candidate. The
    report's terminal line, ids and all: the page shows `kept_why` instead."""
    why: str = ""
    """Why nothing asks for it, in plain words; empty for a protected row and for a report from
    before prune-report said who is followed."""
    protection: Protection | None = None
    """Why a protected row is kept, as data: the report's ``protection`` object, or what could be
    read back out of an older report's `protected_reason` (`legacy_protection`); ``None`` when
    neither says."""

    @property
    def protected(self) -> bool:
        return bool(self.protected_reason)


@dataclass(frozen=True, slots=True)
class PruneArtist:
    mbid: str
    name: str
    releases: tuple[PruneRelease, ...]
    followed: bool | None = None
    """Followed on Spotify when the report was built; ``None`` when nobody can say (follows were
    not read, or from an older report)."""

    @property
    def candidates(self) -> tuple[PruneRelease, ...]:
        return tuple(r for r in self.releases if not r.protected)

    @property
    def size(self) -> int:
        return sum(r.size for r in self.candidates)


@dataclass(frozen=True, slots=True)
class PruneView:
    created_at: str
    artists: tuple[PruneArtist, ...]
    """Largest candidate size first, then by name."""

    def artist(self, mbid: str) -> PruneArtist | None:
        return next((a for a in self.artists if a.mbid == mbid), None)


@dataclass(slots=True)
class Draft:
    """The reviewer's decisions so far. Kept beside the report, so a review survives a reload."""

    artists: dict[str, str] = field(default_factory=dict)
    releases: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    export_stale: bool = False
    """A change was made after the last export: the exported files were removed and must be
    exported again before the terminal commands can use them."""
    revs: dict[str, int] = field(default_factory=dict)
    """artist mbid -> how many changes it has had. A change must name the revision it was made
    against, so a second tab still showing an older row cannot silently undo the first."""
    past_releases: dict[str, Entry] = field(default_factory=dict)
    """The ledger entries from other reviews already read into this draft, per album. An entry is
    applied once: one seen before is never applied again, so a choice changed since stays changed."""
    past_artists: dict[str, Entry] = field(default_factory=dict)
    """The same for this report's artists: an earlier follow or save, shown as a note only."""
    carried: set[str] = field(default_factory=set)
    """Albums the pre-fill marked kept-before - not a hand choice. Such an album is kept while its
    artist is undecided and follows the artist once the artist has a decision: a "save all their
    albums" saves it, a "Trash all listed albums" trashes it. It becomes a hand choice (and leaves this set) only
    when its own control is changed. A hand "Keep - no change on Spotify" is different: it keeps
    the album out of the artist's save."""
    reset_artists: set[str] = field(default_factory=set)
    """Artists set back to Undecided by hand: never given a pre-filled decision again here."""

    def rev(self, artist_mbid: str) -> int:
        return self.revs.get(artist_mbid, 0)

    def decided_before(self, rg_mbid: str) -> bool:
        """Decided in an earlier review as a keep (or keep and save): nothing new to decide."""
        past = self.past_releases.get(rg_mbid)
        return past is not None and past.decision in _CARRIED


# ---------------------------------------------------------------- reading


def _kind(raw: Mapping[str, Any]) -> str:
    primary = str(raw.get("primary_type") or "")
    secondary = [str(s) for s in raw.get("secondary_types") or []]
    return " + ".join([p for p in [primary] if p] + secondary) or "Unknown"


_NOTHING_ASKS = "no liked song, saved album or playlist track is matched to this release"


def why_unasked(raw: Mapping[str, Any], artist_name: str) -> str:
    """Why no source asks for this album, in the reviewer's words, or ``""`` for a report from
    before prune-report recorded who is followed.

    A followed artist brings their studio albums and EPs, and nothing else - so a followed
    artist's compilation, live album, single or remix is on disk only because something once
    asked for it and no longer does. For an artist nobody follows, nothing at all asks. Where
    likearr cannot know about the follow - follows were not read, or a Spotify follow of that name
    never matched a MusicBrainz artist - it says so rather than "you don't follow them".
    """
    if "artist_followed" not in raw:
        return ""
    followed = raw.get("artist_followed")
    primary = str(raw.get("primary_type") or "")
    secondary = [str(s) for s in raw.get("secondary_types") or []]
    if followed is True:
        if secondary or primary not in ("Album", "EP"):
            label = " + ".join(secondary) or primary or "This release"
            return f"{label}: following {artist_name} brings studio albums and EPs only"
        return (
            f"You follow {artist_name}, but this isn't among the studio albums and EPs "
            "likearr reads for them from MusicBrainz"
        )
    if followed is not False:
        return "No liked song, saved album or playlist track is matched to this release (follows aren't read)"
    if raw.get("follow_unmatched") is True:
        return (
            f"likearr couldn't match a Spotify follow named {artist_name} to a MusicBrainz artist, "
            f"so it can't tell whether you follow them; {_NOTHING_ASKS}"
        )
    return f"You don't follow {artist_name} on Spotify, and {_NOTHING_ASKS}"


_LEGACY_KEY = re.compile(r"holds a liked track \(((?:liked|playlist):[A-Za-z0-9:]+)\)")
_LEGACY_WAITING = " that is still waiting for an album;"
_LEGACY_NO_FILES = " whose album "
_LEGACY_ALBUM = re.compile(r" whose album (.{2,1000}) \(([0-9a-fA-F-]{36})\) has no files yet")


def legacy_protection(reason: str) -> Protection | None:
    """What an older report said in `protected_reason` alone, read back best-effort.

    The line was ``holds a liked track (<intent key>) that is still waiting for an album; ...`` or
    ``... whose album '<title>' (<mbid>) has no files yet; ...``: the key, the kind and the album
    come back out. Each part is read where the line put it, right after the key, so an album title
    holding one of those phrases cannot change the kind. Anything else is ``None``, and the page
    says the generic line.
    """
    key = _LEGACY_KEY.match(reason)
    if key is None or split_track_key(key.group(1)) is None:
        return None
    if reason.startswith(_LEGACY_WAITING, key.end()):
        return Protection(kind="pending_album", intent_key=key.group(1))
    if not reason.startswith(_LEGACY_NO_FILES, key.end()) or "has no files yet" not in reason:
        return None
    album, album_mbid = "", ""
    if (found := _LEGACY_ALBUM.match(reason, key.end())) is not None:
        # The title was written with `repr`, so it reads back as a string literal - and nothing else.
        try:
            title = ast.literal_eval(found.group(1))
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            title = None
        if isinstance(title, str):
            album, album_mbid = title, found.group(2)
    return Protection(kind="album_not_downloaded", intent_key=key.group(1), album=album, album_mbid=album_mbid)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _protection_of(row: Mapping[str, Any]) -> Protection | None:
    """A protected row's `Protection`: its ``protection`` object, else its old line."""
    raw = row.get("protection")
    if isinstance(raw, dict) and raw.get("kind") in PROTECTION_KINDS and isinstance(raw.get("intent_key"), str):
        artists = raw.get("song_artists")
        return Protection(
            kind=str(raw["kind"]),
            intent_key=str(raw["intent_key"]),
            song=_text(raw.get("song")),
            song_artists=tuple(a for a in artists if isinstance(a, str)) if isinstance(artists, list) else (),
            album=_text(raw.get("album")),
            album_mbid=_text(raw.get("album_mbid")),
        )
    return legacy_protection(_text(row.get("protected_reason")))


def read_report(path: Path) -> PruneView | None:
    """The `prune.json` a prune-report job wrote, grouped by artist; ``None`` if it will not read.

    A protected row is read with its ``protection`` object, or - in a report written before there
    was one - with what its `protected_reason` line still says (`legacy_protection`)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [(r, "") for r in raw["candidates"]] + [
            (r, str(r.get("protected_reason") or "protected")) for r in raw["protected"]
        ]
        by_artist: dict[str, tuple[str, bool | None, list[PruneRelease]]] = {}
        for row, protected in rows:
            mbid = str(row["artist_mbid"])
            name = str(row.get("artist_name") or mbid)
            followed_raw = row.get("artist_followed")
            _name, _followed, releases = by_artist.setdefault(
                mbid, (name, followed_raw if isinstance(followed_raw, bool) else None, [])
            )
            releases.append(
                PruneRelease(
                    rg_mbid=str(row["rg_mbid"]),
                    title=str(row.get("title") or row["rg_mbid"]),
                    kind=_kind(row),
                    released=str(row.get("release_date") or "")[:4],
                    files=int(row.get("track_file_count") or 0),
                    size=int(row.get("size_on_disk") or 0),
                    protected_reason=protected,
                    why="" if protected else why_unasked(row, _name),
                    protection=_protection_of(row) if protected else None,
                )
            )
        artists = [
            PruneArtist(
                mbid=mbid,
                name=name,
                releases=tuple(sorted(rels, key=lambda r: (r.released, r.title.lower()))),
                followed=followed,
            )
            for mbid, (name, followed, rels) in by_artist.items()
        ]
        artists.sort(key=lambda a: (-a.size, a.name.lower()))
        return PruneView(created_at=str(raw.get("created_at") or ""), artists=tuple(artists))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def read_draft(path: Path) -> Draft:
    """The draft beside a report, or an empty one. Never raises: a lost draft is a fresh start."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Draft()
    if not isinstance(raw, dict):
        return Draft()
    artists_raw, releases_raw, revs_raw = raw.get("artists"), raw.get("releases"), raw.get("revs")
    artists: dict[str, Any] = artists_raw if isinstance(artists_raw, dict) else {}
    releases: dict[str, Any] = releases_raw if isinstance(releases_raw, dict) else {}
    revs: dict[str, Any] = revs_raw if isinstance(revs_raw, dict) else {}
    return Draft(
        artists={str(k): str(v) for k, v in artists.items() if v in ARTIST_DECISIONS},
        releases={str(k): str(v) for k, v in releases.items() if v in RELEASE_OVERRIDES and v},
        notes=str(raw.get("notes") or "")[:MAX_NOTES],
        export_stale=raw.get("export_stale") is True,
        revs={str(k): int(v) for k, v in revs.items() if isinstance(v, int) and not isinstance(v, bool)},
        past_releases=parse_entries(raw.get("past_releases"), LEDGER_RELEASE_DECISIONS),
        past_artists=parse_entries(raw.get("past_artists"), LEDGER_ARTIST_DECISIONS),
        carried=_mbids(raw.get("carried")),
        reset_artists=_mbids(raw.get("reset_artists")),
    )


def _mbids(raw: object) -> set[str]:
    return {v for v in raw if isinstance(v, str) and is_mbid(v)} if isinstance(raw, list) else set()


def write_draft(path: Path, draft: Draft) -> None:
    body = {
        "artists": draft.artists,
        "releases": draft.releases,
        "notes": draft.notes,
        "export_stale": draft.export_stale,
        "revs": draft.revs,
        "past_releases": {k: e.to_dict() for k, e in draft.past_releases.items()},
        "past_artists": {k: e.to_dict() for k, e in draft.past_artists.items()},
        "carried": sorted(draft.carried),
        "reset_artists": sorted(draft.reset_artists),
    }
    write_atomic(path, json.dumps(body, indent=1), mode=0o600)


# ---------------------------------------------------------------- carrying decisions over


def ledger_source(job_id: str) -> str:
    """How this review signs what it records in the ledger - and how `prefill` knows to skip it."""
    return f"Clean up {job_id}"


def prefill(draft: Draft, view: PruneView, ledger: Ledger, *, own_source: str) -> Draft:
    """The draft with what *other* reviews decided filled in. Safe to call on every read.

    Only ledger entries this draft has not seen are applied, and never this review's own
    (`own_source`: `_merge` keeps an entry's first source when a later export repeats it, so an
    album kept in January and kept again here is still January's). So another review's export
    made after the page was opened reaches it on the next read, and an entry is applied once: a
    choice the reviewer changed since stays changed.

    - An album kept before (``keep`` or ``save``) is marked **carried** (`Draft.carried`), not
      given a hand choice: "Same as artist", so nothing happens to it while its artist is
      undecided and it goes with the artist once the artist is decided. Its earlier decision is
      shown as information ("You kept this on 15 Jan 2026."), and it can be changed like any
      other choice.
    - An album **trashed** before and on disk again is left undecided: it is remembered only for
      the note that says so. Pre-filling it as a trash would move it on an export nobody looked
      at, and it is usually a second copy the first move missed, which deserves a look.
    - **A past follow or save never pre-fills as one**, for an artist or an album. It shows as a
      note, and a Spotify action needs a click in this review: a save artist's new album was never
      reviewed, and a follow undone on Spotify since must not come back on its own.
    - An artist with every candidate album kept before gets an artist decision too: keep. An
      artist with anything new stays undecided, so the new album is decided by a person.
    - This review's own choices win: an artist already decided here gets nothing filled in, not
      even per album; an artist set back to Undecided by hand is never given a decision again;
      and an album already decided here keeps its choice.

    Nothing depends on the order entries arrive in: the ledger all at once or across several reads
    gives the same draft, because a carried album is not a hand choice that could outlive the
    artist decision that later covers it.

    Every artist the pre-fill changes gets a new revision, so a tab opened before it cannot write
    over what it filled in. Returns `draft` itself (unchanged) when there is nothing new.
    """
    artists, revs = dict(draft.artists), dict(draft.revs)
    past_releases, past_artists = dict(draft.past_releases), dict(draft.past_artists)
    carried = set(draft.carried)
    for artist in view.artists:
        decided_here = artist.mbid in draft.artists
        seen_new = changed = False
        for release in artist.releases:
            entry = ledger.releases.get(release.rg_mbid)
            if entry is None or entry.source == own_source or past_releases.get(release.rg_mbid) == entry:
                continue
            past_releases[release.rg_mbid] = entry
            seen_new = True
            if (
                entry.decision not in _CARRIED
                or decided_here
                or release.rg_mbid in draft.releases
                or release.rg_mbid in carried
                or release.protected  # always kept anyway
            ):
                continue
            carried.add(release.rg_mbid)
            changed = True
        artist_entry = ledger.artists.get(artist.mbid)
        if (
            artist_entry is not None
            and artist_entry.source != own_source
            and past_artists.get(artist.mbid) != artist_entry
        ):
            past_artists[artist.mbid] = artist_entry
        whole = bool(artist.candidates) and all(
            (p := past_releases.get(r.rg_mbid)) is not None and p.decision in _CARRIED for r in artist.candidates
        )
        if seen_new and whole and not decided_here and artist.mbid not in draft.reset_artists:
            artists[artist.mbid] = "keep"
            changed = True
        if changed:
            revs[artist.mbid] = revs.get(artist.mbid, 0) + 1
    if (artists, carried, past_releases, past_artists) == (
        draft.artists,
        draft.carried,
        draft.past_releases,
        draft.past_artists,
    ):
        return draft
    return replace(
        draft,
        artists=artists,
        revs=revs,
        past_releases=past_releases,
        past_artists=past_artists,
        carried=carried,
    )


def _override(artist_decision: str, draft: Draft, rg_mbid: str) -> str:
    """The album's choice as it stands: its hand choice, else - for a carried album whose artist is
    still undecided - keep, else "" (same as artist)."""
    own = draft.releases.get(rg_mbid, "")
    if own or rg_mbid not in draft.carried:
        return own
    return "keep" if artist_decision == "undecided" else ""


# ---------------------------------------------------------------- what the choices mean


def effective_decision(artist: PruneArtist, draft: Draft) -> str:
    """The artist's decision as it is exported. A follow of an artist the report says is followed
    already is a keep: the follow is done, and the page no longer offers it."""
    decision = draft.artists.get(artist.mbid, "undecided")
    return "keep" if decision == "promote" and artist.followed else decision


def _trashed(artist: PruneArtist, draft: Draft) -> list[PruneRelease]:
    """The candidate releases this artist's decision and overrides send to the holding directory."""
    decision = effective_decision(artist, draft)
    out = []
    for release in artist.candidates:
        override = draft.releases.get(release.rg_mbid, "")
        if override == "trash" or (decision == "trash" and not override):
            out.append(release)
    return out


def _saved(artist: PruneArtist, draft: Draft) -> list[PruneRelease]:
    """The albums of this artist that the export asks `promote-save` to save: each one saved on its
    own, and - under "save all their albums" - every album not trashed and not kept with no change
    on Spotify. Protected albums included: a save moves no file."""
    decision = effective_decision(artist, draft)
    trashed = {r.rg_mbid for r in _trashed(artist, draft)}
    out = []
    for release in artist.releases:
        override = draft.releases.get(release.rg_mbid, "")
        if release.rg_mbid in trashed:
            continue
        if override == "save" or (decision == "save" and not override):
            out.append(release)
    return out


def ledger_changes(view: PruneView, draft: Draft) -> tuple[dict[str, str], dict[str, str]]:
    """What one export tells the ledger: ``(releases, artists)``, id -> decision.

    An album records what the export does with it: ``trash``, ``save`` (saved on its own or with
    its artist), or ``keep`` when the reviewer decided to keep it - its own choice, or its
    artist's. An album left undecided records nothing: it was not decided. A protected album
    records only a choice made about it (its own, or its artist's save): "always kept" is the
    report's rule, not the reviewer's, and must not read as a decision if it stops being
    protected. An artist records ``promote`` / ``save``, and ``""`` (forget) when decided otherwise.
    """
    releases: dict[str, str] = {}
    artists: dict[str, str] = {}
    for artist in view.artists:
        decision = effective_decision(artist, draft)
        trashed = {r.rg_mbid for r in _trashed(artist, draft)}
        saved = {r.rg_mbid for r in _saved(artist, draft)}
        for release in artist.releases:
            override = _override(decision, draft, release.rg_mbid)
            if release.rg_mbid in trashed:
                releases[release.rg_mbid] = "trash"
            elif release.rg_mbid in saved:
                releases[release.rg_mbid] = "save"
            elif override == "keep" or (decision in ("keep", "promote") and not release.protected):
                releases[release.rg_mbid] = "keep"
        if decision in ("promote", "save"):
            artists[artist.mbid] = decision
        elif decision in ("keep", "trash"):
            artists[artist.mbid] = ""
    return releases, artists


# ---------------------------------------------------------------- deciding


class DecisionError(ValueError):
    """A decision that names nothing in the report, or a value outside the fixed choices."""


class StaleChange(DecisionError):
    """The change was made against an older revision of the artist: another tab changed it since."""


def decide(
    draft: Draft,
    view: PruneView,
    artist_mbid: str,
    *,
    rev: int,
    decision: str | None = None,
    release: tuple[str, str] | None = None,
) -> Draft:
    """Record ONE change to one artist: its decision, or one album's override - never the rest of
    the row, so a change can only ever say what the person just did.

    `rev` is the artist's revision the change was made against. Anything else is a stale tab:
    the change is refused (`StaleChange`), so it cannot undo what the other tab recorded.

    Raises:
        DecisionError: an unknown artist, release or value, a protected album asked to move, a
            follow of an artist already followed, or not exactly one change.
        StaleChange: `rev` is not the artist's current revision.
    """
    artist = view.artist(artist_mbid) if is_mbid(artist_mbid) else None
    if artist is None:
        raise DecisionError("not an artist in this report")
    if (decision is None) == (release is None):
        raise DecisionError("one change at a time: a decision or one album")
    if rev != draft.rev(artist_mbid):
        raise StaleChange("changed in another tab - reload")
    artists, releases = dict(draft.artists), dict(draft.releases)
    carried, reset_artists = set(draft.carried), set(draft.reset_artists)
    if decision is not None:
        if decision not in ARTIST_DECISIONS:
            raise DecisionError("not a decision")
        if decision == "promote" and artist.followed:
            raise DecisionError(f"{artist.name} is already followed on Spotify")
        artists[artist_mbid] = decision
        if decision == "undecided":
            artists.pop(artist_mbid)
            reset_artists.add(artist_mbid)
    else:
        assert release is not None
        rg, value = release
        by_rg = {r.rg_mbid: r for r in artist.releases}
        if rg not in by_rg or value not in RELEASE_OVERRIDES:
            raise DecisionError("not a release of this artist, or not a choice")
        if value == "trash" and by_rg[rg].protected:
            raise DecisionError(
                f"{by_rg[rg].title} is always kept: it holds the only copy of {_whose_song([by_rg[rg]])}"
            )
        releases[rg] = value
        if not value:
            releases.pop(rg)
        carried.discard(rg)  # a hand choice now, whatever it is
    revs = {**draft.revs, artist_mbid: draft.rev(artist_mbid) + 1}
    return replace(draft, artists=artists, releases=releases, revs=revs, carried=carried, reset_artists=reset_artists)


def decisions_file(view: PruneView, draft: Draft) -> dict[str, Any]:
    """The decisions file: `prune-stage --decisions` reads trash, `promote-save` the rest.

    Every album to move is listed on its own in ``trash``, and ``trash_artists`` is always empty:
    `prune-stage` reads an artist there as "every candidate of theirs in whatever report it is
    given", so a rebuilt report would move albums nobody reviewed. Album by album, the moves and
    the Lidarr rows removed are the same.

    ``save_releases`` names each album saved on its own; ``save_exclude_releases`` each album of a
    ``save`` artist kept with no change on Spotify, which `promote-save` must leave out of that
    artist's save. Both are checked against the review snapshot's per-album ``save`` flag.
    """
    trash: list[str] = []
    promote: list[str] = []
    save: list[str] = []
    save_releases: list[str] = []
    save_exclude_releases: list[str] = []
    for artist in view.artists:
        decision = effective_decision(artist, draft)
        trash.extend(r.rg_mbid for r in _trashed(artist, draft))
        save_releases.extend(r.rg_mbid for r in artist.releases if draft.releases.get(r.rg_mbid) == "save")
        if decision == "promote":
            promote.append(artist.mbid)
        elif decision == "save":
            save.append(artist.mbid)
            save_exclude_releases.extend(r.rg_mbid for r in artist.releases if draft.releases.get(r.rg_mbid) == "keep")
    return {
        "version": 1,
        "trash": trash,
        "trash_artists": [],
        "promote": promote,
        "save": save,
        "save_releases": save_releases,
        "save_exclude_releases": save_exclude_releases,
        "notes": draft.notes,
    }


def review_data(view: PruneView, draft: Draft) -> dict[str, Any]:
    """The review snapshot `promote-save --reviewed` requires: every artist and release looked
    at, with its file count at review time and what was decided. `promote-save` reads ``mbid``,
    ``rg``, ``files`` and ``save``; a release trashed here is listed with its decision, and loses
    its files when `prune-stage` moves them, so it can never become a save."""
    return {
        "version": 1,
        "created_at": view.created_at,
        "artists": [
            {
                "mbid": a.mbid,
                "name": a.name,
                "decision": effective_decision(a, draft),
                "releases": [
                    {
                        "rg": r.rg_mbid,
                        "title": r.title,
                        "type": r.kind,
                        # A moved-out album is not a keep: `promote-save` reads `files`, and 0 means
                        # it can never become a save, whatever `trash` says.
                        "files": 0 if r.rg_mbid in trashed else r.files,
                        "files_at_review": r.files,
                        "trash": r.rg_mbid in trashed,
                        "save": r.rg_mbid in saved,
                        "protected": r.protected,
                    }
                    for r in a.releases
                ],
            }
            for a in view.artists
            for trashed, saved in [({r.rg_mbid for r in _trashed(a, draft)}, {r.rg_mbid for r in _saved(a, draft)})]
        ],
    }


# ---------------------------------------------------------------- the table


def needs_decision(artist: PruneArtist, draft: Draft) -> list[PruneRelease]:
    """The artist's candidate albums not kept in an earlier review: new ones, and ones back on
    disk after a trash. What "Needs a decision" lists; it changes only when the ledger brings
    something new, never as the reviewer decides, so paging through it does not shift."""
    return [r for r in artist.candidates if not draft.decided_before(r.rg_mbid)]


def select_artists(
    view: PruneView, draft: Draft, *, query: str, show: str, page: int
) -> tuple[list[PruneArtist], int, int, int]:
    """Filter (artist or album title, case-insensitive; needs a decision, a decision, or all),
    then page.

    Returns (the page, how many matched, how many pages, the page actually shown).
    """
    needle = query.strip().casefold()

    def shown(a: PruneArtist) -> bool:
        if show == "needs":
            return bool(needs_decision(a, draft))
        if show in ARTIST_DECISIONS:
            return effective_decision(a, draft) == show
        return True

    matched = [
        a
        for a in view.artists
        if (not needle or needle in a.name.casefold() or any(needle in r.title.casefold() for r in a.releases))
        and shown(a)
    ]
    pages = max(1, math.ceil(len(matched) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    return matched[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], len(matched), pages, page


def summary(view: PruneView, draft: Draft) -> dict[str, int]:
    """Progress, what was carried over, and what the export would do, for the cards at the top."""
    exported = decisions_file(view, draft)
    trashed = [r for a in view.artists for r in _trashed(a, draft)]
    candidates = [r for a in view.artists for r in a.candidates]
    needs = [r for r in candidates if not draft.decided_before(r.rg_mbid)]
    return {
        "artists": len(view.artists),
        "decided": sum(1 for a in view.artists if a.mbid in draft.artists),
        "candidates": len(candidates),
        "candidate_bytes": sum(a.size for a in view.artists),
        "trash_albums": len(trashed),
        "trash_bytes": sum(r.size for r in trashed),
        "promote": len(exported["promote"]),
        "save": len(exported["save"]),
        "save_albums": sum(len(_saved(a, draft)) for a in view.artists),
        "undecided_trash": sum(len(_trashed(a, draft)) for a in view.artists if a.mbid not in draft.artists),
        "carried": len(candidates) - len(needs),
        "needs": len(needs),
        "needs_artists": sum(1 for a in view.artists if needs_decision(a, draft)),
        "returning": sum(
            1 for r in needs if (p := draft.past_releases.get(r.rg_mbid)) is not None and p.decision == "trash"
        ),
    }


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _same_as_artist(release: PruneRelease, decision: str, trashed: bool) -> str:
    """What "Same as artist" resolves to for this album right now, Spotify action included. An
    album of an undecided artist is simply not decided yet - a carried-over one too: its earlier
    decision is shown as information, under its title, and never as a choice."""
    if decision == "undecided":
        return "Same as artist: not decided yet"
    if trashed:
        return "Same as artist: trash"
    if decision == "save":
        return "Same as artist: keep and save on Spotify"
    if decision == "trash":  # a protected album is never trashed
        return "Same as artist: keep (always kept)"
    return "Same as artist: keep"


def human_day(day: str) -> str:
    """``2026-01-15`` as "15 Jan 2026", for the reviewer; the stored form stays ISO."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{parsed.day} {parsed:%b} {parsed.year}"


def _past_note(entry: Entry) -> str:
    """What an earlier review did with this album, as information only."""
    day = human_day(entry.on)
    if entry.decision == "trash":
        return f"You trashed this on {day} - it's on disk again (often a second copy in a differently spelled folder)."
    if entry.decision == "save":
        return f"You kept this and saved it on Spotify on {day}."
    return f"You kept this on {day}."


_KINDS = {
    "Compilation": ("compilation", "compilations"),
    "Soundtrack": ("soundtrack", "soundtracks"),
    "Spokenword": ("spoken-word release", "spoken-word releases"),
    "Interview": ("interview", "interviews"),
    "Audiobook": ("audiobook", "audiobooks"),
    "Audio drama": ("audio drama", "audio dramas"),
    "Live": ("live album", "live albums"),
    "Remix": ("remix", "remixes"),
    "DJ-mix": ("DJ mix", "DJ mixes"),
    "Mixtape/Street": ("mixtape", "mixtapes"),
    "Demo": ("demo", "demos"),
    "Field recording": ("field recording", "field recordings"),
    "Album": ("studio album", "studio albums"),
    "EP": ("EP", "EPs"),
    "Single": ("single", "singles"),
    "Broadcast": ("broadcast", "broadcasts"),
    "Other": ("other release", "other releases"),
}


def _kinds_in_words(releases: Sequence[PruneRelease]) -> str:
    """ "2 live albums and 1 compilation": each album counted once, by its most telling type (a
    secondary type such as Live over the primary Album)."""
    counts: dict[str, int] = {}
    for release in releases:
        parts = release.kind.split(" + ")
        kind = parts[1] if len(parts) > 1 else parts[0]
        counts[kind] = counts.get(kind, 0) + 1
    words = []
    for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        one, many = _KINDS.get(kind, (kind.lower(), f"{kind.lower()} releases"))
        words.append(f"{n} {one if n == 1 else many}")
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _albums(n: int) -> str:
    return f"{n} album{'' if n == 1 else 's'}"


def net_effect(artist: PruneArtist, draft: Draft) -> list[str]:
    """What the artist's decision does, in plain words, once one is picked: only the lines that
    apply, with real counts. A decision acts on the albums listed here and nothing else, and a rule
    it meets - a follow, a protected album, an album decided on its own - changes what it does, so
    the page says the net result. Nothing about following when the report cannot say who is
    followed (`followed` is None)."""
    decision = effective_decision(artist, draft)
    if decision == "undecided":
        return []
    lines: list[str] = []
    trashed = _trashed(artist, draft)
    if decision == "trash" and artist.followed and trashed:
        lines.append(
            f"Trashes the {_albums(len(trashed))} listed here ({_kinds_in_words(trashed)}). You follow "
            f"{artist.name} on Spotify, so their studio albums and EPs aren't listed and stay."
        )
    if decision == "keep" and artist.followed:
        lines.append(
            "Keeps the listed albums on disk. Following brings in studio albums and EPs only, so likearr "
            "won't fetch more like these."
        )
    protected = [r for r in artist.releases if r.protected]
    if protected:
        n = len(protected)
        lines.append(
            f"{_albums(n)} {'holds' if n == 1 else 'hold'} the only copy of {_whose_song(protected)} and "
            f"{'is' if n == 1 else 'are'} always kept."
        )
    own = [r for r in artist.releases if r.rg_mbid in draft.releases]
    if own:
        n = len(own)
        lines.append(f"{_albums(n)} you set individually {'keeps its' if n == 1 else 'keep their'} own choice.")
    if decision == "save":
        kept = [r for r in artist.releases if draft.releases.get(r.rg_mbid) == "keep"]
        if kept:
            n = len(kept)
            lines.append(f"{_albums(n)} you set to Keep {'isn' if n == 1 else 'aren'}'t saved.")
    return lines


# ---------------------------------------------------------------- why an album is always kept

_MUSICBRAINZ = "https://musicbrainz.org"

_SOME_SONG = "a song from your liked songs or playlists"
"""Whose song a protected album holds, when the report does not say: never an id."""


def _whose_song(releases: Sequence[PruneRelease]) -> str:
    """ "a song you liked", "a song in your playlists", or - mixed, or not known - `_SOME_SONG`."""
    sources = {r.protection.source if r.protection is not None else "" for r in releases}
    if sources == {"liked"}:
        return "a song you liked"
    if sources == {"playlist"}:
        return "a song in your playlists"
    return _SOME_SONG


@dataclass(frozen=True, slots=True)
class KeptWhy:
    """Why a protected album is always kept: one sentence in three parts, so the page can make the
    album's title a link - `lead`, then `album` (linked to `album_url` when there is one), then
    `tail`. Plain text throughout; the template escapes it."""

    lead: str
    album: str = ""
    album_url: str = ""
    tail: str = ""

    @property
    def text(self) -> str:
        return f"{self.lead}{self.album}{self.tail}"


def kept_why(
    release: PruneRelease, *, playlist_names: Mapping[str, str] | None = None, songs: Mapping[str, str] | None = None
) -> KeptWhy:
    """Why this protected album is always kept, in words with no id in them:

    - *Only copy of "Think", a song in your playlist "Road trip". Its album, Respect, isn't
      downloaded yet.*
    - *Only copy of "Think", a song you liked. likearr is waiting for its album to be released.*

    The song's title comes from the report, else from `songs` (the last run's snapshot, by intent
    key), else it is just "a song"; the playlist's name from `playlist_names` (the playlist-name
    cache), else "one of your playlists". A row that says nothing readable gets the generic line.
    """
    protection = release.protection
    if protection is None or not protection.source:
        return KeptWhy(f"Only copy of {_SOME_SONG}.")
    song = protection.song or (songs or {}).get(protection.intent_key, "")
    if protection.source == "playlist":
        name = (playlist_names or {}).get(protection.playlist_id, "")
        source = f'a song in your playlist "{name}"' if name else "a song in one of your playlists"
    else:
        source = "a song you liked"
    lead = f'Only copy of "{song}", {source}.' if song else f"Only copy of {source}."
    if protection.kind == "pending_album":
        return KeptWhy(f"{lead} likearr is waiting for its album to be released.")
    if not protection.album:
        return KeptWhy(f"{lead} Its album isn't downloaded yet.")
    url = f"{_MUSICBRAINZ}/release-group/{protection.album_mbid}" if is_mbid(protection.album_mbid) else ""
    return KeptWhy(f"{lead} Its album, ", protection.album, url, ", isn't downloaded yet.")


def rows_for_page(
    artists: Sequence[PruneArtist],
    draft: Draft,
    *,
    playlist_names: Mapping[str, str] | None = None,
    songs: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """What the template needs per artist: the artist, its decision and what it does, each
    release's own choice and what "Same as artist" means for it, what earlier reviews did, and why
    each protected album is always kept (`kept_why`, given `playlist_names` and `songs`)."""
    rows = []
    for a in artists:
        decision = effective_decision(a, draft)
        trashed = {r.rg_mbid for r in _trashed(a, draft)}
        rows.append(
            {
                "artist": a,
                "decision": decision,
                "follow_done": draft.artists.get(a.mbid) == "promote" and decision == "keep",
                # The hand choice only: a carried album selects "Same as artist".
                "overrides": {r.rg_mbid: draft.releases.get(r.rg_mbid, "") for r in a.releases},
                "same_as_artist": {r.rg_mbid: _same_as_artist(r, decision, r.rg_mbid in trashed) for r in a.releases},
                "effects": net_effect(a, draft),
                "rev": draft.rev(a.mbid),
                "trashed": trashed,
                "past": {
                    r.rg_mbid: draft.past_releases[r.rg_mbid] for r in a.releases if r.rg_mbid in draft.past_releases
                },
                "past_notes": {
                    r.rg_mbid: _past_note(draft.past_releases[r.rg_mbid])
                    for r in a.releases
                    if r.rg_mbid in draft.past_releases
                    and (not r.protected or draft.past_releases[r.rg_mbid].decision != "keep")
                },
                "past_artist": draft.past_artists.get(a.mbid),
                "past_artist_on": human_day(draft.past_artists[a.mbid].on) if a.mbid in draft.past_artists else "",
                "needs": len(needs_decision(a, draft)),
                "kept": {
                    r.rg_mbid: kept_why(r, playlist_names=playlist_names, songs=songs)
                    for r in a.releases
                    if r.protected
                },
            }
        )
    return rows
