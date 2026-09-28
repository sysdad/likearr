"""`likearr promote-save`: carry the review page's `promote` and `save` decisions to Spotify.

The prune review produced a decisions file whose `trash` and `trash_artists` `prune-stage`
already applied. Its other fields were always meant for a Spotify-side step, and this is it:

- **`promote`** (artist MBIDs): follow that artist on Spotify, so likearr keeps monitoring their
  studio albums and EPs from then on.
- **`save`** (artist MBIDs): save the albums of theirs that **a human reviewed and kept**.
- **`save_releases`** (release-group MBIDs): save that one album, which a human reviewed and
  chose to keep and save on its own. The same rule as `save`, one album at a time: it must be in
  the review snapshot with files, and still hold files in Lidarr.
- **`save_exclude_releases`** (release-group MBIDs): an album of a `save` artist that a human
  chose to keep with no change on Spotify. It is left out of that artist's save.

Both are cross-checked against the review snapshot's per-album ``save`` flag, which the review page
writes from the same decisions: an album is saved only when the two agree, and a disagreement is
listed in `unmatched` rather than resolved either way.

Same discipline as `run`: plan, review, apply.

    likearr promote-save --decisions FILE --reviewed review-data.json [--out PLAN.json]
    likearr promote-save --apply PLAN.json      # executes exactly that plan

**The rule this command is built around:** promote-save carries out decisions made by hand in a
Clean up review; likearr never saves or follows on Spotify on its own. It is not an ongoing mirror
of the library, and it never saves an album or follows an artist automatically, on any schedule,
or as a side effect of any rule: album saves and artist follows are high-intent, high-impact
actions on a personal account. In particular, a release monitored because of a playlist or
liked-track rule, or because of the followed-artist catalogue rule, never causes an album save or
an artist follow. Those rules exist to get the required tracks into Lidarr under the strictest
match, and that is where they stop.

Which is why the save set is intersected with the review snapshot (`--reviewed`) rather than
read from "what this artist has files for today". Library state drifts; decisions do not. There
is **no** scheduled mode, no `--scheduled`, and nothing in `likearr run` reaches this code.

The plan carries a digest of all three of its inputs - the decisions file, the review snapshot
and the Lidarr albums it looked at - and `--apply` refuses a plan whose world has moved, exit 3,
exactly as `run --apply` does.

Two rules shape everything here:

- **Never guess.** A MusicBrainz id has no Spotify equivalent, so it is mapped in three tiers,
  best first: MusicBrainz's own Spotify URL relationship (an editor's identity claim - nothing to
  compare, and no Spotify quota spent), then a UPC search, then a conservative title search judged
  by :mod:`likearr.core.match`, which accepts only an unambiguous normalised equality. Every match
  records **which** tier produced it, so the summary prints the breakdown and a human can
  spot-check the weakest one. Anything less than confident lands in `unmatched`, is written into
  the plan and is printed. A wrong match saves the wrong album into a real library; a miss is a
  line of output.
- **Idempotent, and resumable.** Nothing is written without first reading what the account
  already follows and has saved, so a re-run writes only what is genuinely missing. Every search
  is cached on disk by the adapter, so a re-plan after a quota error costs nothing for the work
  already done.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from likearr.adapters.lock import run_lock
from likearr.core.match import (
    STEP_ALBUM_LINK,
    STEP_ALBUM_NAME,
    STEP_ALBUM_UPC,
    STEP_ARTIST_LINK,
    STEP_ARTIST_NAME,
    MatchResult,
    match_album,
    match_artist,
)
from likearr.fsio import write_atomic
from likearr.models import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_STALE,
    PROMOTE_SAVE_PLAN_VERSION,
    SPOTIFY_WRITE_SCOPES,
    FollowArtist,
    LidarrAlbum,
    LidarrArtist,
    LidarrView,
    PromoteSavePlan,
    ReleaseKey,
    SaveAlbum,
    Unmatched,
)
from likearr.ports import (
    LidarrError,
    ReleaseLinkLookup,
    ScopeError,
    SearchBudgetExceeded,
    SourceError,
    SpotifyLibraryPort,
)
from likearr.shell.context import Context
from likearr.shell.output import emit

_TIER_ORDER = {
    STEP_ARTIST_LINK: 0,
    STEP_ALBUM_LINK: 0,
    STEP_ALBUM_UPC: 1,
    STEP_ARTIST_NAME: 2,
    STEP_ALBUM_NAME: 2,
}
"""Print order for the tier breakdown: most authoritative first, so the weakest reads last."""

__all__ = [
    "DEFAULT_PLAN_PATH",
    "PromoteSaveError",
    "promote_save_command",
    "read_plan",
    "tier_breakdown",
    "write_plan",
]

log = logging.getLogger(__name__)

DEFAULT_PLAN_PATH = Path("promote-save.json")

REQUIRED_SCOPES = frozenset(SPOTIFY_WRITE_SCOPES) | {"user-follow-read", "user-library-read"}
"""Writing needs the two modify scopes; the idempotency checks need the two read ones."""

MAX_UPC_TRIES = 2
"""Barcodes to try per album before falling back to a name search.

MusicBrainz often records several (the CD, the vinyl, a reissue) and Spotify carries only some of
them, so a second try is worth one call. A third would cost more quota than it finds.
"""


class PromoteSaveError(Exception):
    """`promote-save` refused to run. Nothing was written to Spotify when this is raised."""


# ---------------------------------------------------------------------------- the decisions file


@dataclass(frozen=True, slots=True)
class Decisions:
    """The fields of a prune-review decisions file that this command consumes."""

    path: Path
    promote: tuple[str, ...]
    save: tuple[str, ...]
    save_releases: tuple[str, ...] = ()
    """Release groups saved one at a time."""
    save_exclude_releases: tuple[str, ...] = ()
    """Release groups of `save` artists kept with no change on Spotify: never saved."""

    @property
    def saves_anything(self) -> bool:
        return bool(self.save or self.save_releases)

    def digest(self) -> str:
        """Stable hash of the decisions, so `--apply` can refuse a plan made from a different set.
        A file without `save_releases` / `save_exclude_releases` hashes exactly as it did before
        the fields existed (pinned by a test against a digest computed before the new fields existed)."""
        h = hashlib.sha256()
        for mbid in sorted(self.promote):
            h.update(b"p" + mbid.encode())
        for mbid in sorted(self.save):
            h.update(b"s" + mbid.encode())
        for mbid in sorted(self.save_releases):
            h.update(b"r" + mbid.encode())
        for mbid in sorted(self.save_exclude_releases):
            h.update(b"x" + mbid.encode())
        return h.hexdigest()


def read_decisions(path: Path) -> Decisions:
    """Read `promote`, `save`, `save_releases` and `save_exclude_releases` out of a decisions file
    (docs/cli.md, "Decisions file").

    `trash` and `trash_artists` are `prune-stage`'s business and are ignored here, exactly as
    `prune-stage` ignores these two.

    Raises:
        PromoteSaveError: the file is missing, is not JSON, or is not an object.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromoteSaveError(f"cannot read the decisions file at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PromoteSaveError(f"{path} is not a prune decisions file")
    if raw.get("version") != 1:
        emit(f"warn  {path} does not declare decisions file version 1; treating it as version 1 anyway")
    promote = _string_list(raw.get("promote"))
    save = _string_list(raw.get("save"))
    save_releases = _string_list(raw.get("save_releases"))
    if not promote and not save and not save_releases:
        raise PromoteSaveError(f"{path} has nothing in 'promote', 'save' or 'save_releases'; there is nothing to do")
    return Decisions(
        path=path,
        promote=promote,
        save=save,
        save_releases=save_releases,
        save_exclude_releases=_string_list(raw.get("save_exclude_releases")),
    )


def _string_list(value: object) -> tuple[str, ...]:
    """De-duplicated, order-preserving list of non-empty strings."""
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


# ---------------------------------------------------------------------------- the Lidarr side


@dataclass(frozen=True, slots=True)
class KeptAlbum:
    """One album of a `save` artist that survived the prune: it still has files on disk."""

    artist: LidarrArtist
    album: LidarrAlbum

    @property
    def key(self) -> ReleaseKey:
        return ReleaseKey(artist_mbid=self.artist.mbid, rg_mbid=self.album.rg_mbid)


@dataclass(slots=True)
class LidarrSide:
    """Everything `promote-save` reads out of Lidarr, plus the digest that pins it."""

    view: LidarrView
    kept: list[KeptAlbum]
    missing_artists: list[tuple[str, str]]
    """``(kind, artist_mbid)`` for decisions naming an artist Lidarr does not have."""
    empty_artists: list[str]
    """`save` artists Lidarr has but with no album holding a file."""
    excluded: list[KeptAlbum]
    """Albums with files that were **not** in the review snapshot, so are not saveable. See
    `ReviewSnapshot` - this list is the visible count of what the constraint kept out."""
    refused_releases: list[tuple[str, str, str, str]] = field(default_factory=list)
    """``(artist_mbid, rg_mbid, name, reason)`` for an album asked to be saved that is not: a
    `save_releases` album not in the review snapshot with files, or with no files in Lidarr now,
    and any album on which the decisions file and the snapshot's ``save`` flag disagree. Reported,
    never guessed at."""
    kept_unsaved: list[KeptAlbum] = field(default_factory=list)
    """Albums of `save` artists kept with no change on Spotify, by hand (`save_exclude_releases`)."""

    def digest(self, decisions: Decisions) -> str:
        """Hash exactly what a match depends on: the artists asked for and the reviewed albums.

        Deliberately narrow, like `core.diff.lidarr_digest`: unrelated Lidarr activity between
        planning and applying must not invalidate a reviewed plan, but an album losing its files
        (or an artist row vanishing) must. Excluded albums are outside the hash on purpose -
        new downloads arriving is exactly the drift the exclusion exists to ignore.
        """
        h = hashlib.sha256()
        for mbid in sorted(decisions.promote):
            artist = self.view.artists.get(mbid)
            h.update(b"p" + f"{mbid}:{artist.name if artist else ''}".encode())
        for item in sorted(self.kept, key=lambda k: (k.artist.mbid, k.album.rg_mbid)):
            h.update(b"a" + f"{item.artist.mbid}:{item.album.rg_mbid}:{item.album.track_file_count}".encode())
        return h.hexdigest()


# ---------------------------------------------------------------------------- the review snapshot


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """What the library review actually put in front of a human, per artist.

    This is what keeps the rule. "The albums this artist has files for" is *library state*, and
    library state drifts: between a review and a plan, the followed-artist catalogue rule, the
    liked and playlist track rules and anything added outside likearr all grow it. None of those
    albums was ever a decision, and saving them would be likearr deciding on its own what belongs
    in a personal Spotify library.

    So the save set is intersected with this snapshot: an album is a candidate only if it was
    in front of the reviewer **and** already had files then.
    """

    path: Path
    reviewed: dict[str, frozenset[str]]
    """artist mbid -> the release-group mbids reviewed *with files* for that artist."""
    save_flags: dict[str, bool] = field(default_factory=dict)
    """rg mbid -> the snapshot's own ``save`` flag, for releases that carry one. An older snapshot
    has none, and nothing is cross-checked."""

    def save_flag(self, rg_mbid: str) -> bool | None:
        return self.save_flags.get(rg_mbid)

    def allows(self, artist_mbid: str, rg_mbid: str) -> bool:
        return rg_mbid in self.reviewed.get(artist_mbid, frozenset())

    def artist_of(self, rg_mbid: str) -> str | None:
        """The artist a release group was reviewed under *with files*, or None: the only way a
        `save_releases` id becomes an album likearr can look up."""
        return next((artist for artist, rgs in self.reviewed.items() if rg_mbid in rgs), None)

    def digest(self) -> str:
        """Pins the snapshot into the plan, so `--apply` refuses a plan built from a different one."""
        h = hashlib.sha256()
        for artist_mbid in sorted(self.reviewed):
            for rg_mbid in sorted(self.reviewed[artist_mbid]):
                h.update(b"r" + f"{artist_mbid}:{rg_mbid}".encode())
        for rg_mbid, flag in sorted(self.save_flags.items()):
            h.update(b"f" + f"{rg_mbid}:{int(flag)}".encode())
        return h.hexdigest()


def read_reviewed(path: Path) -> ReviewSnapshot:
    """Read the review page's own export (``review-data.json``).

    Shape: a top-level ``artists`` list, each with ``mbid`` and ``releases``, each release with
    ``rg``, ``files``, ``title`` and ``type``, and ``save``. Only ``mbid``, ``rg``,
    ``files`` and ``save`` are read; a release with no files at review time was not a "keep" and
    cannot become a save.

    Raises:
        PromoteSaveError: the file is missing, is not JSON, or has no ``artists`` list.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromoteSaveError(f"cannot read the review snapshot at {path}: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("artists"), list):
        raise PromoteSaveError(f"{path} is not a review snapshot (no top-level 'artists' list)")

    reviewed: dict[str, frozenset[str]] = {}
    save_flags: dict[str, bool] = {}
    for entry in raw["artists"]:
        if not isinstance(entry, Mapping):
            continue
        artist_mbid = str(entry.get("mbid") or "")
        releases = entry.get("releases")
        if not artist_mbid or not isinstance(releases, list):
            continue
        rgs = {
            str(r.get("rg"))
            for r in releases
            if isinstance(r, Mapping) and r.get("rg") and int(r.get("files") or 0) > 0
        }
        if rgs:
            reviewed[artist_mbid] = frozenset(rgs)
        for r in releases:
            if isinstance(r, Mapping) and str(r.get("rg")) in rgs and isinstance(r.get("save"), bool):
                save_flags[str(r.get("rg"))] = bool(r.get("save"))
    log.info(
        "review snapshot: %d artists, %d reviewed albums with files",
        len(reviewed),
        sum(len(v) for v in reviewed.values()),
    )
    return ReviewSnapshot(path=path, reviewed=reviewed, save_flags=save_flags)


def read_lidarr(ctx: Context, decisions: Decisions, reviewed: ReviewSnapshot) -> LidarrSide:
    """One Lidarr read covering both decision kinds: names for `promote`, albums for `save`.

    An album is a save candidate only when Lidarr still has files for it **and** it was in the
    review snapshot with files. Lidarr answers "is this still here"; the snapshot answers "did a
    human decide about this", and only the second makes something saveable. Everything with files
    that the snapshot does not name is collected into `excluded` and counted, never dropped
    silently - the whole point is that the number is visible.
    """
    # A single album is looked up under the artist it was reviewed under; one the snapshot does
    # not list with files is refused below without asking Lidarr anything about it.
    by_release = {rg: reviewed.artist_of(rg) for rg in decisions.save_releases}
    wanted = sorted({*decisions.promote, *decisions.save, *(a for a in by_release.values() if a)})
    view = ctx.lidarr.load_view(wanted)

    kept: list[KeptAlbum] = []
    excluded: list[KeptAlbum] = []
    unsaved: list[KeptAlbum] = []
    refused: list[tuple[str, str, str, str]] = []
    missing: list[tuple[str, str]] = []
    empty: list[str] = []
    exclude = set(decisions.save_exclude_releases)
    for mbid in decisions.promote:
        if mbid not in view.artists:
            missing.append(("artist", mbid))
    for mbid in decisions.save:
        artist = view.artists.get(mbid)
        if artist is None:
            missing.append(("album", mbid))
            continue
        with_files = sorted(
            (a for a in view.albums.get(mbid, {}).values() if a.has_files), key=lambda a: (a.title, a.rg_mbid)
        )
        theirs = [KeptAlbum(artist=artist, album=a) for a in with_files]
        for item in theirs:
            rg = item.album.rg_mbid
            if not reviewed.allows(mbid, rg):
                excluded.append(item)
                continue
            by_hand = rg in exclude
            flag = reviewed.save_flag(rg)
            if flag is not None and flag == by_hand:  # the file and the snapshot disagree
                refused.append((mbid, rg, f"{artist.name} - {item.album.title}", _DISAGREE))
            elif by_hand:
                unsaved.append(item)
            else:
                kept.append(item)
        if not any(reviewed.allows(mbid, item.album.rg_mbid) for item in theirs):
            empty.append(mbid)
    have = {(k.artist.mbid, k.album.rg_mbid) for k in kept}
    for rg, artist_mbid in by_release.items():
        if artist_mbid is None:
            refused.append(
                ("", rg, rg, "not in the review snapshot with files; likearr never saves an album nobody decided on")
            )
            continue
        artist = view.artists.get(artist_mbid)
        album = view.albums.get(artist_mbid, {}).get(rg)
        if artist is None or album is None or not album.has_files:
            name = f"{artist.name} - {rg}" if artist is not None else rg
            refused.append(
                (artist_mbid, rg, name, "Lidarr has no files for it any more, so there is nothing kept to save")
            )
            continue
        if rg in exclude or reviewed.save_flag(rg) is not True:
            refused.append((artist_mbid, rg, f"{artist.name} - {album.title}", _DISAGREE))
            continue
        if (artist_mbid, rg) not in have:  # an album of a `save` artist is saved once
            kept.append(KeptAlbum(artist=artist, album=album))
            have.add((artist_mbid, rg))
    return LidarrSide(
        view=view,
        kept=kept,
        missing_artists=missing,
        empty_artists=empty,
        excluded=excluded,
        refused_releases=refused,
        kept_unsaved=unsaved,
    )


_DISAGREE = (
    "the decisions file and the review snapshot disagree about saving this album; "
    "nothing is saved - export the review again"
)


# ---------------------------------------------------------------------------- planning


def _match_one_artist(
    library: SpotifyLibraryPort,
    links: ReleaseLinkLookup | None,
    artist: LidarrArtist,
) -> MatchResult:
    """Tier 1 the MusicBrainz relationship, tier 3 the name search. Artists have no tier 2."""
    if links is not None:
        linked = links.spotify_artist_id(artist.mbid)
        if linked:
            return MatchResult(spotify_id=linked, step=STEP_ARTIST_LINK)
    return match_artist(artist.name, library.search_artists(artist.name))


def _match_one_album(
    library: SpotifyLibraryPort,
    links: ReleaseLinkLookup | None,
    item: KeptAlbum,
) -> MatchResult:
    """The three tiers, in order of authority, stopping at the first confident answer.

    1. **MusicBrainz's Spotify relationship.** A human editor asserted that this release group is
       that Spotify album. There is nothing to compare, so nothing to get wrong, and it costs no
       Spotify quota at all.
    2. **A UPC search.** A barcode identifies a release; a title only describes one. The hit is
       still gated on the artist credit by `match_album`, because a barcode typo in MusicBrainz
       would otherwise point straight at a stranger's record.
    3. **A title search**, with the conservative rules in `core.match`.

    A near miss at any tier does not win; it falls through, and the last tier's reason is what
    the plan reports.
    """
    artist_name, title = item.artist.name, item.album.title
    codes: tuple[str, ...] = ()
    if links is not None:
        linked = links.spotify_album_id(item.album.rg_mbid)
        if linked:
            return MatchResult(spotify_id=linked, step=STEP_ALBUM_LINK)
        codes = tuple(links.release_group_barcodes(item.album.rg_mbid))[:MAX_UPC_TRIES]
    for upc in codes:
        hits = library.search_albums_by_upc(upc)
        if not hits:
            continue
        result = match_album(artist_name, title, hits, step=STEP_ALBUM_UPC)
        if result.matched:
            return result
    return match_album(artist_name, title, library.search_albums(artist_name, title), step=STEP_ALBUM_NAME)


def plan_promote_save(
    decisions: Decisions,
    reviewed: ReviewSnapshot,
    side: LidarrSide,
    *,
    library: SpotifyLibraryPort,
    links: ReleaseLinkLookup | None,
    now: datetime,
) -> PromoteSavePlan:
    """Match every decision to a Spotify id, ask what is already there, and describe the rest.

    Nothing is written. The only Spotify calls are searches and the two library listings.
    """
    unmatched: list[Unmatched] = []
    for kind, mbid in side.missing_artists:
        unmatched.append(
            Unmatched(
                kind=kind,
                artist_mbid=mbid,
                rg_mbid="",
                name=mbid,
                reason="Lidarr has no artist with this MBID, so there is no name to search for",
            )
        )
    for artist_mbid, rg_mbid, name, reason in side.refused_releases:
        unmatched.append(Unmatched(kind="album", artist_mbid=artist_mbid, rg_mbid=rg_mbid, name=name, reason=reason))
    for mbid in side.empty_artists:
        artist = side.view.artists.get(mbid)
        unmatched.append(
            Unmatched(
                kind="album",
                artist_mbid=mbid,
                rg_mbid="",
                name=artist.name if artist else mbid,
                reason=("no reviewed album of theirs still has files, so there is nothing they were decided to keep"),
            )
        )

    follow: list[FollowArtist] = []
    save: list[SaveAlbum] = []
    budget_exhausted = False
    remaining: list[Unmatched] = []

    try:
        for mbid in decisions.promote:
            artist = side.view.artists.get(mbid)
            if artist is None:
                continue  # already reported as missing
            result = _match_one_artist(library, links, artist)
            if result.matched:
                follow.append(
                    FollowArtist(artist_mbid=mbid, name=artist.name, spotify_id=result.spotify_id, step=result.step)
                )
            else:
                unmatched.append(
                    Unmatched(kind="artist", artist_mbid=mbid, rg_mbid="", name=artist.name, reason=result.reason)
                )

        for item in side.kept:
            result = _match_one_album(library, links, item)
            label = f"{item.artist.name} - {item.album.title}"
            if result.matched:
                save.append(
                    SaveAlbum(
                        key=item.key,
                        artist_name=item.artist.name,
                        title=item.album.title,
                        spotify_id=result.spotify_id,
                        step=result.step,
                    )
                )
            else:
                unmatched.append(
                    Unmatched(
                        kind="album",
                        artist_mbid=item.artist.mbid,
                        rg_mbid=item.album.rg_mbid,
                        name=label,
                        reason=result.reason,
                    )
                )
    except SearchBudgetExceeded as exc:
        # Everything matched so far is kept and written to the plan; the adapter's search cache
        # means a re-run picks up here instead of repeating the calls that have already been made.
        budget_exhausted = True
        log.warning("%s", exc)
        done = {f.artist_mbid for f in follow} | {u.artist_mbid for u in unmatched if not u.rg_mbid}
        saved_keys = {(s.key.artist_mbid, s.key.rg_mbid) for s in save}
        saved_keys |= {(u.artist_mbid, u.rg_mbid) for u in unmatched if u.rg_mbid}
        for mbid in decisions.promote:
            artist = side.view.artists.get(mbid)
            if artist is not None and mbid not in done:
                remaining.append(
                    Unmatched(kind="artist", artist_mbid=mbid, rg_mbid="", name=artist.name, reason=str(exc))
                )
        for item in side.kept:
            if (item.artist.mbid, item.album.rg_mbid) not in saved_keys:
                remaining.append(
                    Unmatched(
                        kind="album",
                        artist_mbid=item.artist.mbid,
                        rg_mbid=item.album.rg_mbid,
                        name=f"{item.artist.name} - {item.album.title}",
                        reason=str(exc),
                    )
                )

    already_followed, follow = _split_follow(library, follow)
    already_saved, save = _split_save(library, save)

    return PromoteSavePlan(
        created_at=now,
        decisions_path=str(decisions.path),
        decisions_digest=decisions.digest(),
        lidarr_digest=side.digest(decisions),
        reviewed_path=str(reviewed.path),
        reviewed_digest=reviewed.digest(),
        excluded_unreviewed=[
            Unmatched(
                kind="album",
                artist_mbid=item.artist.mbid,
                rg_mbid=item.album.rg_mbid,
                name=f"{item.artist.name} - {item.album.title}",
                reason="not in the review snapshot; likearr never saves an album nobody decided on",
            )
            for item in side.excluded
        ],
        follow=sorted(follow, key=lambda f: (f.name.casefold(), f.artist_mbid)),
        save=sorted(save, key=lambda s: (s.artist_name.casefold(), s.title.casefold(), s.key.rg_mbid)),
        already_followed=sorted(already_followed, key=lambda f: (f.name.casefold(), f.artist_mbid)),
        already_saved=sorted(already_saved, key=lambda s: (s.artist_name.casefold(), s.title.casefold())),
        unmatched=[*unmatched, *remaining],
        searches_used=getattr(library, "searches", 0),
        budget_exhausted=budget_exhausted,
    )


def tier_breakdown(plan: PromoteSavePlan) -> dict[str, int]:
    """How many matches each tier produced, counting what is already there as well.

    The point is spot-checking: `album:mb-rel` is an editor's assertion and needs no review,
    while a large `album:name` count is where a human should look hardest before applying.
    """
    counts: dict[str, int] = {}
    for item in (*plan.follow, *plan.already_followed, *plan.save, *plan.already_saved):
        counts[item.step] = counts.get(item.step, 0) + 1
    return dict(sorted(counts.items()))


def _split_follow(
    library: SpotifyLibraryPort, matched: Sequence[FollowArtist]
) -> tuple[list[FollowArtist], list[FollowArtist]]:
    """``(already followed, still to follow)`` - the plan shows only real changes.

    Membership comes from the user's whole follow list, read once and reused for every test,
    because `GET /me/following/contains` is 403 for a Development Mode app (see the library adapter).
    """
    if not matched:
        return [], []
    known = library.followed_artist_ids()
    already = [f for f in matched if f.spotify_id in known]
    todo = [f for f in matched if f.spotify_id not in known]
    return already, todo


def _split_save(library: SpotifyLibraryPort, matched: Sequence[SaveAlbum]) -> tuple[list[SaveAlbum], list[SaveAlbum]]:
    """``(already saved, still to save)``, from the user's whole saved-album list."""
    if not matched:
        return [], []
    known = library.saved_album_ids()
    already = [s for s in matched if s.spotify_id in known]
    todo = [s for s in matched if s.spotify_id not in known]
    return already, todo


# ---------------------------------------------------------------------------- the plan file


def plan_to_dict(plan: PromoteSavePlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "created_at": plan.created_at.isoformat(),
        "decisions_path": plan.decisions_path,
        "decisions_digest": plan.decisions_digest,
        "reviewed_path": plan.reviewed_path,
        "reviewed_digest": plan.reviewed_digest,
        "lidarr_digest": plan.lidarr_digest,
        "summary": {
            "follow": len(plan.follow),
            "save": len(plan.save),
            "already_followed": len(plan.already_followed),
            "already_saved": len(plan.already_saved),
            "unmatched": len(plan.unmatched),
            "excluded_unreviewed": len(plan.excluded_unreviewed),
            "searches_used": plan.searches_used,
            "by_step": tier_breakdown(plan),
        },
        "budget_exhausted": plan.budget_exhausted,
        "follow": [_follow_to_dict(f) for f in plan.follow],
        "save": [_save_to_dict(s) for s in plan.save],
        "already_followed": [_follow_to_dict(f) for f in plan.already_followed],
        "already_saved": [_save_to_dict(s) for s in plan.already_saved],
        "unmatched": [_unmatched_to_dict(u) for u in plan.unmatched],
        "excluded_unreviewed": [_unmatched_to_dict(u) for u in plan.excluded_unreviewed],
    }


def _unmatched_to_dict(item: Unmatched) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "artist_mbid": item.artist_mbid,
        "rg_mbid": item.rg_mbid,
        "name": item.name,
        "reason": item.reason,
    }


def _unmatched_from_dict(raw: Mapping[str, Any]) -> Unmatched:
    return Unmatched(
        kind=str(raw.get("kind") or ""),
        artist_mbid=str(raw.get("artist_mbid") or ""),
        rg_mbid=str(raw.get("rg_mbid") or ""),
        name=str(raw.get("name") or ""),
        reason=str(raw.get("reason") or ""),
    )


def _follow_to_dict(item: FollowArtist) -> dict[str, Any]:
    return {
        "artist_mbid": item.artist_mbid,
        "name": item.name,
        "spotify_id": item.spotify_id,
        "step": item.step,
    }


def _save_to_dict(item: SaveAlbum) -> dict[str, Any]:
    return {
        "artist_mbid": item.key.artist_mbid,
        "rg_mbid": item.key.rg_mbid,
        "artist_name": item.artist_name,
        "title": item.title,
        "spotify_id": item.spotify_id,
        "step": item.step,
    }


def plan_from_dict(raw: Mapping[str, Any]) -> PromoteSavePlan:
    """Rebuild the plan `--apply` executes. Raises `PromoteSaveError` on anything unexpected."""
    version = int(raw.get("version") or 0)
    if version != PROMOTE_SAVE_PLAN_VERSION:
        raise PromoteSaveError(
            f"this plan is version {version}, but this likearr writes version "
            f"{PROMOTE_SAVE_PLAN_VERSION}; re-plan before applying"
        )
    try:
        return PromoteSavePlan(
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            decisions_path=str(raw.get("decisions_path") or ""),
            decisions_digest=str(raw["decisions_digest"]),
            lidarr_digest=str(raw["lidarr_digest"]),
            follow=[_follow_from_dict(f) for f in _items(raw, "follow")],
            save=[_save_from_dict(s) for s in _items(raw, "save")],
            already_followed=[_follow_from_dict(f) for f in _items(raw, "already_followed")],
            already_saved=[_save_from_dict(s) for s in _items(raw, "already_saved")],
            unmatched=[_unmatched_from_dict(u) for u in _items(raw, "unmatched")],
            reviewed_path=str(raw.get("reviewed_path") or ""),
            reviewed_digest=str(raw["reviewed_digest"]),
            excluded_unreviewed=[_unmatched_from_dict(u) for u in _items(raw, "excluded_unreviewed")],
            version=version,
            searches_used=int(raw.get("summary", {}).get("searches_used") or 0),
            budget_exhausted=bool(raw.get("budget_exhausted")),
        )
    except PromoteSaveError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PromoteSaveError(f"this does not look like a likearr promote-save plan: {exc}") from exc


def _items(raw: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise PromoteSaveError(f"plan file field {key!r} is not a list")
    return [item for item in value if isinstance(item, Mapping)]


def _follow_from_dict(raw: Mapping[str, Any]) -> FollowArtist:
    return FollowArtist(
        artist_mbid=str(raw["artist_mbid"]),
        name=str(raw.get("name") or ""),
        spotify_id=str(raw["spotify_id"]),
        step=str(raw.get("step") or ""),
    )


def _save_from_dict(raw: Mapping[str, Any]) -> SaveAlbum:
    return SaveAlbum(
        key=ReleaseKey(artist_mbid=str(raw["artist_mbid"]), rg_mbid=str(raw["rg_mbid"])),
        artist_name=str(raw.get("artist_name") or ""),
        title=str(raw.get("title") or ""),
        spotify_id=str(raw["spotify_id"]),
        step=str(raw.get("step") or ""),
    )


def write_plan(plan: PromoteSavePlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(plan_to_dict(plan), indent=2) + "\n", mode=0o600)


def read_plan(path: Path) -> PromoteSavePlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PromoteSaveError(f"no plan file at {path} - run `likearr promote-save --decisions FILE` first") from exc
    except (OSError, ValueError) as exc:
        raise PromoteSaveError(f"cannot read the plan at {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PromoteSaveError(f"the plan at {path} is not a JSON object")
    return plan_from_dict(raw)


# ---------------------------------------------------------------------------- the command


def promote_save_command(
    ctx: Context,
    *,
    decisions: Path | None = None,
    out: Path = DEFAULT_PLAN_PATH,
    apply_path: Path | None = None,
    do_apply: bool = False,
    force: bool = False,
    reviewed: Path | None = None,
    now: datetime | None = None,
    library: SpotifyLibraryPort | None = None,
    links: ReleaseLinkLookup | None = None,
) -> int:
    """Plan, or apply a reviewed plan.

    Args:
        decisions: the decisions file. Required when planning; when applying it defaults to the
            path the plan recorded, so `--apply PLAN.json` on its own works.
        reviewed: the review snapshot (`review-data.json`). Required whenever the decisions ask
            for any save, and defaults on apply to the path the plan recorded. Not optional by
            accident: see `_load_reviewed`.
        library: injected in tests; normally `Context.library`.
        links: injected in tests; normally `Context.composite`, whose MusicBrainz side answers
            the Spotify-relationship and barcode questions.

    Returns:
        0 ok, 1 refused, 3 the plan is stale.
    """
    now = now or datetime.now(UTC)
    library = library or ctx.library
    if library is None:
        raise SourceError(ctx.spotify_error or "spotify is not configured; run `likearr auth --manual` first")
    if links is None:
        links = ctx.composite

    _require_scopes(library)

    if do_apply:
        # Under the run lock, so an apply cannot overlap a scheduled `run` (or another apply);
        # `LockHeld` reaches the CLI as one line and exit 4 (`EXIT_BUSY`). Planning writes nothing, so it needs none.
        with run_lock(ctx.lock_path):
            return _apply(ctx, apply_path or out, decisions, reviewed, library=library, force=force)

    if decisions is None:
        raise PromoteSaveError("promote-save needs --decisions FILE to plan from")
    parsed = read_decisions(decisions)
    snapshot = _load_reviewed(parsed, reviewed)
    side = read_lidarr(ctx, parsed, snapshot)
    plan = plan_promote_save(parsed, snapshot, side, library=library, links=links, now=now)
    write_plan(plan, out)
    print_plan(plan, out)
    if side.kept_unsaved:
        emit("")
        emit(f"{len(side.kept_unsaved)} album(s) of 'save' artists kept with no change on Spotify, by hand:")
        for item in side.kept_unsaved:
            emit(f"  {item.artist.name} - {item.album.title}")
    return EXIT_OK


def _load_reviewed(decisions: Decisions, reviewed: Path | None) -> ReviewSnapshot:
    """The review snapshot is mandatory whenever anything could be saved. There is no fallback.

    Falling back to "whatever this artist currently has files for" is precisely what the rule
    forbids: library state drifts, decisions do not. An album that arrived because
    a followed artist's catalogue was monitored, or because a liked or playlist track needed it,
    was never decided on by anyone - and likearr may not turn it into a save on its own. So a
    missing snapshot is a refusal, not a silent widening.
    """
    if reviewed is not None:
        return read_reviewed(reviewed)
    if not decisions.saves_anything:
        # Nothing could be saved, so there is nothing to constrain: follows come from `promote`
        # alone and never from library state. An empty snapshot is the honest representation.
        return ReviewSnapshot(path=Path(), reviewed={})
    raise PromoteSaveError(
        "promote-save needs --reviewed FILE (the review snapshot, e.g. review-data.json) before it "
        f"will plan any save: {decisions.path} asks to save {len(decisions.save)} artist(s) and "
        f"{len(decisions.save_releases)} single album(s), and the "
        "only albums that may be saved are the ones a human actually reviewed and kept. Falling back "
        "to whatever those artists currently have files for would save albums nobody decided on - "
        "a followed artist's back catalogue, or an album some liked track pulled in. likearr never "
        "writes to Spotify on its own. (Nothing was changed.)"
    )


def _require_scopes(library: SpotifyLibraryPort) -> None:
    """Refuse up front when the stored token cannot write, and say exactly how to fix it.

    Scopes are granted at consent time and a refresh never widens them, so there is no automatic
    recovery here on purpose: re-authorizing is a browser round trip only the user can make.
    """
    granted = library.granted_scopes()
    missing = sorted(REQUIRED_SCOPES - granted)
    if not missing:
        return
    have = " ".join(sorted(granted)) or "(none recorded)"
    raise ScopeError(
        "the stored Spotify token cannot write to your library: it is missing "
        f"{', '.join(missing)}. It has: {have}. promote-save follows artists and saves albums, "
        "which needs a fresh consent screen that asks for write access - run "
        "`likearr auth --manual --promote-save` and approve the new permissions, then run "
        "promote-save again. (Nothing was changed.)"
    )


def _apply(
    ctx: Context,
    plan_path: Path,
    decisions_override: Path | None,
    reviewed_override: Path | None,
    *,
    library: SpotifyLibraryPort,
    force: bool,
) -> int:
    """Execute exactly the reviewed plan, after checking that its world has not moved."""
    plan = read_plan(plan_path)
    decisions_path = decisions_override or (Path(plan.decisions_path) if plan.decisions_path else None)
    if decisions_path is None:
        raise PromoteSaveError(
            f"{plan_path} does not record which decisions file it was planned from; re-run with --decisions FILE"
        )
    parsed = read_decisions(decisions_path)
    reviewed_path = reviewed_override or (Path(plan.reviewed_path) if plan.reviewed_path else None)
    snapshot = _load_reviewed(parsed, reviewed_path)
    side = read_lidarr(ctx, parsed, snapshot)

    stale = (
        plan.decisions_digest != parsed.digest()
        or plan.reviewed_digest != snapshot.digest()
        or plan.lidarr_digest != side.digest(parsed)
    )
    if stale:
        if not force:
            emit(f"the world moved since {plan_path} was planned; nothing was changed")
            emit("  re-run `likearr promote-save --decisions ...` and review the new plan")
            return EXIT_STALE
        emit(f"--force: applying a stale plan from {plan_path} anyway")

    # Re-read the account rather than trusting the plan's own membership check: the plan may be hours
    # old, and this is also what makes a half-finished apply safe to simply run again.
    _, to_follow = _split_follow(library, plan.follow)
    _, to_save = _split_save(library, plan.save)

    try:
        library.follow_artists([f.spotify_id for f in to_follow])
        library.save_albums([s.spotify_id for s in to_save])
    except (SourceError, LidarrError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    emit("likearr promote-save applied:")
    emit(f"  {len(to_follow):>6} artists followed")
    emit(f"  {len(to_save):>6} albums saved")
    skipped = (len(plan.follow) - len(to_follow)) + (len(plan.save) - len(to_save))
    if skipped:
        emit(f"  {skipped:>6} already there when we looked (nothing rewritten)")
    if plan.unmatched:
        emit(f"  {len(plan.unmatched):>6} unmatched, and still unmatched - see {plan_path}")
    return EXIT_OK


def print_plan(plan: PromoteSavePlan, out: Path) -> None:
    """The summary a human reads before deciding whether to apply."""
    emit(f"likearr promote-save plan ({out}):")
    emit(f"  {len(plan.follow):>6} artists to follow")
    emit(f"  {len(plan.save):>6} albums to save")
    emit(f"  {len(plan.already_followed):>6} artists already followed")
    emit(f"  {len(plan.already_saved):>6} albums already saved")
    emit(f"  {len(plan.unmatched):>6} unmatched (nothing is written for these)")
    emit(f"  {len(plan.excluded_unreviewed):>6} excluded as unreviewed (arrived after the review; never saveable)")
    emit(f"  {plan.searches_used:>6} Spotify search calls used")
    breakdown = tier_breakdown(plan)
    if breakdown:
        emit("")
        emit("matched by (best tier first - check the last one hardest):")
        for step, count in sorted(breakdown.items(), key=lambda kv: (_TIER_ORDER.get(kv[0], 99), kv[0])):
            emit(f"  {count:>6} {step}")
    if plan.unmatched:
        emit("")
        emit("unmatched:")
        for item in plan.unmatched:
            emit(f"  {item.kind:<6} {item.name}: {item.reason}")
    if plan.budget_exhausted:
        emit("")
        emit("WARNING: the search budget ran out. Every result so far is cached, so re-running")
        emit("         this plan continues from here rather than starting over.")
    if plan.is_empty:
        emit("")
        emit("nothing to do")
    else:
        emit("")
        emit(f"re-run with --apply {out} to write these to Spotify")
