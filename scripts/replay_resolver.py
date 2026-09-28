"""Offline resolver replay: re-resolve a state snapshot's intents from its `mb_cache` alone.

Read-only and network-free. Written for a saved-album regression at RESOLVER_VERSION 6; kept
because every future resolver change deserves the same check. Not shipped: the Docker image
copies `likearr/` only.

    uv run python scripts/replay_resolver.py SNAPSHOT.sqlite --out replay.json
    PYTHONPATH=/path/to/other/checkout python scripts/replay_resolver.py SNAPSHOT.sqlite --out old.json
    python scripts/replay_resolver.py --compare new.json old.json [...]

What it does, and what it cannot do:

- **The baseline** is the snapshot's own `resolutions` table - the answers that install holds.
- **No network.** The snapshot is copied to a temporary file (the adapter opens it read-write) and
  the MusicBrainz adapter is given an HTTP transport that refuses every request and counts it. Every
  cache entry answers whatever its age, exactly as a MusicBrainz outage would be served. An intent
  needing a lookup the cache does not hold fails with `error:metadata` and is reported as skipped.
  The snapshot itself is never written. An older snapshot may hold `isrc-search` and
  `rg-tracks` rows stored whole, which answer exactly as trimmed ones do; a newer one's rows hold
  only the fields today's parsers read, so a resolver change that needs a dropped field has to
  refetch first.
- **Intents are reconstructed** from the stored resolutions, because the Spotify snapshot is not in
  the state database. A saved album is exact: its detail quotes Spotify's artist and title, or its
  barcode. A track is approximate: its detail quotes the song but not Spotify's album title or the
  ISRC, so the album is taken from the cached name search that returned the release Spotify named,
  and the ISRC from a cached ISRC search whose recording carries the song's title and holds that
  release or the answer. Spotify's release year is not stored, so the saved-album year preference
  cannot act here. Unmapped intents are not stored and are not replayed.
- **The relationship rule is on** when the code under test has it, as in a normal run, so the
  replay runs the code that ships. It only acts where a track would otherwise be UNMAPPED at
  `track:album:search`, which a stored intent reaches only if the replay's approximate
  reconstruction misses; its `artist-rels:` lookup is then usually not cached, so it is skipped.
- **The opt-outs are the ones each resolution was made under**, read back from its stored `rules`
  token (``""``, ``c1r0``, ``c1r0k0``, ...), passing only the fields the code under test has - so a
  checkout from before `keep_remix_only_tracks` replays ``c1r0`` as it always did. The deny
  list is not in the token and is not replayed. Excluded tracks are UNMAPPED and so not stored:
  what the remix-only rule would now keep is invisible here, and only moves *away* from a stored
  answer are measured.

The `--compare` mode prints one line per intent whose release group differs from the baseline in
the first result file, and says for each whether the other result files (other code) differ too.

**The Lidarr fallback check**. The resolver replay above has no Lidarr: an answer that
Lidarr's album search supplied after a MusicBrainz miss replays as a miss. And Lidarr's search
responses are never stored - `lidarr_negative_cache` keeps only the terms that failed - so no replay
can reproduce what Lidarr returned. What the snapshot does hold, for every answer the name search
matched, is Spotify's artist and album title (quoted in the detail) and the release group that
matched. Lidarr must have returned that release group, so the check hands exactly it to the code
under test's `LidarrClient.search_release_group_candidates`, through a stand-in transport, and
records whether that code still accepts it for Spotify's names. A refusal is a move to UNMAPPED.
It cannot see a *new* acceptance: what Lidarr returned beside the stored answer is not stored, and
an unmapped intent is not stored either.

Each row carries where the answer most likely came from, read off `mb_cache`: ``musicbrainz`` when
a cached name search lists the release group (Lidarr was never asked, so a refusal there changes
nothing), ``lidarr-fallback`` when the search is cached but does not list it, and ``unknown`` when
the search is not cached at all. It also carries ``ascii_fold_empty``: whether the ASCII-only fold
the Lidarr adapter used previously turns any of the four compared names into "" - the case now fixed.

Only counts go to standard output. The per-row detail stays in the result file (``--out``), and
`--compare` writes the rows that moved to ``--lidarr-detail`` when given. Keep both files local.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SNAPSHOT_NOW = datetime(2026, 9, 23, 1, 30, tzinfo=UTC)

_REPR = r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_SAVED_BY_NAME = re.compile(rf"^{_REPR} - {_REPR} matched release group")
_BARCODE = re.compile(r"^barcode (\S+) is release group")
_TRACK_NAME = re.compile(rf"(?:^Spotify filed |^){_REPR}")
_HOLDING = re.compile(rf"holding {_REPR} was")
_RULES_TOKEN = re.compile(r"^c([01])r([01])(k0)?$")
_NAME_MATCH = re.compile(
    rf"(?:^|; ){_REPR} - {_REPR} matched release group {_REPR} \((\S+)\) by name( after stripping)?"
)
_SAME_NAME = re.compile(rf"\d+ different artists named {_REPR} each have a release titled {_REPR}: ")
_OLD_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_OLD_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_LIDARR_STAND_IN = "http://lidarr-stand-in.invalid"


def _rules_from_token(token: str, rules_type: type) -> Any:
    """The `ExclusionRules` a stored `Resolution.rules` token was made under, or None for a token
    this script cannot read. Only fields `rules_type` actually has are passed, so older code works."""
    if not token:
        return rules_type()
    m = _RULES_TOKEN.match(token)
    if m is None:
        return None
    wanted = {
        "allow_compilation_fallback": m[1] == "1",
        "allow_remix_releases": m[2] == "1",
        "keep_remix_only_tracks": m[3] is None,
    }
    fields = set(getattr(rules_type, "__dataclass_fields__", {}))
    return rules_type(**{k: v for k, v in wanted.items() if k in fields})


def _unrepr(text: str) -> str:
    value = ast.literal_eval(text)
    return value if isinstance(value, str) else str(value)


class _NoNetwork(httpx.BaseTransport):
    """Refuses every request, and counts them, so "no network" is a measured fact."""

    def __init__(self) -> None:
        self.attempts = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        raise httpx.ConnectError("replay is offline", request=request)


def _search_pairs(con: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """Release group MBID -> every cached name search (artist, title) that returned it."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, body in con.execute("SELECT key, body FROM mb_cache WHERE key LIKE 'rg-search:%' AND negative = 0"):
        artist, _, title = key.split(":", 1)[1].partition("|")
        for raw in json.loads(body).get("release-groups") or []:
            if isinstance(raw, dict) and raw.get("id"):
                out[str(raw["id"])].append((artist, title))
    return out


def _isrc_index(
    con: sqlite3.Connection,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    """Two views of every cached ISRC search: release group MBID -> (isrc, recording title), and
    recording title -> (isrc, credited artist name)."""
    by_rg: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_title: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, body in con.execute("SELECT key, body FROM mb_cache WHERE key LIKE 'isrc-search:%' AND negative = 0"):
        isrc = key.split(":", 1)[1]
        for rec in json.loads(body).get("recordings") or []:
            if not isinstance(rec, dict) or (rec.get("isrcs") and isrc not in rec["isrcs"]):
                continue
            title = str(rec.get("title") or "")
            credit = rec.get("artist-credit") or [{}]
            by_title[title].append((isrc, str(credit[0].get("name") or "")))
            for release in rec.get("releases") or []:
                rg = release.get("release-group") if isinstance(release, dict) else None
                if isinstance(rg, dict) and rg.get("id"):
                    by_rg[str(rg["id"])].append((isrc, title))
    return by_rg, by_title


def _old_ascii_fold(value: str) -> str:
    """The Lidarr adapter's old normaliser, frozen here only to label rows: it folded to
    ASCII, so a name written wholly in a non-Latin script came out as ""."""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    folded = _OLD_PAREN.sub(" ", folded).replace("&", " and ")
    return _OLD_NON_ALNUM.sub(" ", folded).strip()


def _lidarr_album(rg: dict[str, Any]) -> dict[str, Any]:
    """A stored release group as Lidarr's ``album/lookup`` would return it."""
    return {
        "foreignAlbumId": rg.get("mbid"),
        "title": rg.get("title") or "",
        "albumType": rg.get("primary_type"),
        "secondaryTypes": list(rg.get("secondary_types") or []),
        "releaseDate": rg.get("first_release_date"),
        "artist": {"foreignArtistId": rg.get("artist_mbid") or "", "artistName": rg.get("artist_name") or ""},
    }


def _search_source(con: sqlite3.Connection, mb_key: str, rg_mbid: str) -> str:
    """Where a name-search answer most likely came from; see the module docstring."""
    held = False
    for key in (f"rg-search:{mb_key}", f"rg-search-free:{mb_key}"):
        row = con.execute("SELECT body, negative FROM mb_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            continue
        held = True
        if row[1]:
            continue
        for raw in json.loads(row[0]).get("release-groups") or []:
            if isinstance(raw, dict) and str(raw.get("id") or "") == rg_mbid:
                return "musicbrainz"
    return "lidarr-fallback" if held else "unknown"


def _searched(d: dict[str, Any], strip: Any) -> tuple[str, str, str] | None:
    """Spotify's artist, the title the name search asked for, and the release group it matched,
    read off a stored answer's detail; None when the detail records no name search."""
    detail = str(d.get("detail") or "")
    m = _NAME_MATCH.search(detail)
    if m is not None:
        return _unrepr(m[1]), strip(_unrepr(m[2])) if m[5] else _unrepr(m[2]), m[4]
    same = _SAME_NAME.search(detail)
    chosen = d.get("source_release_group") or d.get("release_group")
    if same is not None and chosen:
        # Several same-named artists, and the track's ISRC chose one. The quoted title is
        # the one searched, already stripped when the retry found them.
        return _unrepr(same[1]), _unrepr(same[2]), str(chosen.get("mbid") or "")
    return None


def lidarr_check(con: sqlite3.Connection, stored: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Whether the code under test's Lidarr name search still accepts each stored name-search answer.

    Offline: the stand-in transport answers ``album/lookup`` with the one stored release group and
    refuses anything else, counting it. The API key is a placeholder, never the environment's.
    """
    from likearr.adapters.lidarr import LidarrClient
    from likearr.adapters.musicbrainz import _normalize as mb_normalize  # the mb_cache key's own fold
    from likearr.config import LidarrConfig
    from likearr.core.normalize import strip_release_qualifiers

    serving: list[dict[str, Any]] = []
    other_requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal other_requests
        if request.method == "GET" and request.url.path.endswith("/api/v1/album/lookup"):
            return httpx.Response(200, json=serving)
        other_requests += 1
        return httpx.Response(599, text="the stand-in answers album/lookup only")

    config = LidarrConfig(url=_LIDARR_STAND_IN, root_folder="/stand-in", quality_profile="stand-in")
    rows: dict[str, dict[str, Any]] = {}
    unparsed = 0
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        lidarr = LidarrClient(config, client, api_key="stand-in-no-key", sleep=lambda _s: None)
        for key, d in stored:
            searched = _searched(d, strip_release_qualifiers)
            if searched is None:
                if str(d.get("step") or "").endswith(":search") and d.get("release_group"):
                    unparsed += 1
                continue
            artist, title, mbid = searched
            rg = next(
                (g for g in (d.get("source_release_group"), d.get("release_group")) if g and g.get("mbid") == mbid),
                None,
            )
            if rg is None:
                unparsed += 1
                continue
            serving[:] = [_lidarr_album(rg)]
            found = lidarr.search_release_group_candidates(artist, title)
            names = (artist, title, str(rg.get("artist_name") or ""), str(rg.get("title") or ""))
            rows[key] = {
                "source": _search_source(con, f"{mb_normalize(artist)}|{mb_normalize(title)}", mbid),
                "step": d.get("step"),
                "ascii_fold_empty": any(not _old_ascii_fold(n) for n in names),
                "accepted": any(g.mbid == mbid for g in found),
                "spotify_artist": artist,
                "spotify_title": title,
                "stored_artist": rg.get("artist_name"),
                "stored_title": rg.get("title"),
                "stored_mbid": mbid,
            }
    return {"rows": rows, "unparsed": unparsed, "other_requests": other_requests}


def _lidarr_counts(check: dict[str, Any]) -> str:
    by_source: dict[str, int] = defaultdict(int)
    refused: dict[str, int] = defaultdict(int)
    for r in check["rows"].values():
        by_source[r["source"]] += 1
        if not r["accepted"]:
            refused[r["source"]] += 1
    order = ("lidarr-fallback", "unknown", "musicbrainz")
    return (
        f"lidarr fallback check: {len(check['rows'])} name-search answer(s) "
        f"({', '.join(f'{s} {by_source[s]}' for s in order)}); refused by this code: "
        f"{', '.join(f'{s} {refused[s]}' for s in order)}; {check['unparsed']} not parseable; "
        f"{check['other_requests']} other Lidarr request(s) refused"
    )


def run(snapshot: Path, out: Path) -> None:
    import likearr
    from likearr.adapters.musicbrainz import MusicBrainzLookup
    from likearr.config import MusicBrainzConfig
    from likearr.core.normalize import normalize_name, normalize_title
    from likearr.core.resolver import resolve_album, resolve_track
    from likearr.models import (
        RESOLVER_VERSION,
        VARIOUS_ARTISTS_MBID,
        AlbumIntent,
        ExclusionRules,
        Reason,
        ReasonKind,
        SpotifyAlbumRef,
        TrackIntent,
    )
    from likearr.ports import MetadataError

    ro = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    stored = [(k, json.loads(j)) for k, j in ro.execute("SELECT intent_key, json FROM resolutions ORDER BY intent_key")]
    pairs = _search_pairs(ro)
    isrcs, isrcs_by_title = _isrc_index(ro)

    titles_normalized: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for title, entries in isrcs_by_title.items():
        titles_normalized[normalize_title(title)].extend(entries)
    lidarr = lidarr_check(ro, stored)
    ro.close()

    followed = frozenset(
        (d.get("release_group") or {}).get("artist_mbid", "") for _, d in stored if d.get("followed") is True
    )

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "snapshot.sqlite"
        shutil.copyfile(snapshot, copy)
        transport = _NoNetwork()
        lookup = MusicBrainzLookup(
            MusicBrainzConfig(contact="replay@localhost", negative_cache_days=10**6),
            httpx.Client(transport=transport),
            cache_path=copy,
            max_age_days=None,
            now=lambda: SNAPSHOT_NOW.timestamp(),
            sleep=lambda _s: None,
        )
        # A checkout at RESOLVER_VERSION 7 or older has no `relations` parameter.
        relations = {"relations": lookup} if "relations" in inspect.signature(resolve_track).parameters else {}
        results: dict[str, dict[str, Any]] = {}
        skipped: dict[str, str] = {}
        for key, d in stored:
            kind, _, source_id = key.partition(":")
            detail = str(d.get("detail") or "")
            rg = d.get("release_group") or {}
            src = d.get("source_release_group") or rg
            try:
                if kind == "saved":
                    reason = Reason(ReasonKind.SAVED, source_id)
                    by_name, barcode = _SAVED_BY_NAME.match(detail), _BARCODE.match(detail)
                    if by_name:
                        artist, title, upc = _unrepr(by_name[1]), _unrepr(by_name[2]), None
                    elif barcode:
                        artist, title, upc = str(rg.get("artist_name") or ""), str(rg.get("title") or ""), barcode[1]
                    else:
                        skipped[key] = f"saved-album detail not parseable: {detail[:80]}"
                        continue
                    album = SpotifyAlbumRef(source_id, title, (artist,), upc, "album", None)
                    fresh = resolve_album(AlbumIntent(album=album, reason=reason), lookup)
                else:
                    rules = _rules_from_token(str(d.get("rules") or ""), ExclusionRules)
                    if rules is None:
                        skipped[key] = f"rules token not readable: {d.get('rules')!r}"
                        continue
                    playlist_id, _, track_id = source_id.rpartition(":") if kind == "playlist" else ("", "", source_id)
                    reason = Reason(ReasonKind(kind), track_id, playlist_id or None)
                    named = _HOLDING.search(detail) or _TRACK_NAME.search(detail)
                    if not named or not src.get("mbid"):
                        skipped[key] = f"track detail not parseable: {detail[:80]}"
                        continue
                    song = _unrepr(named[1])
                    found = pairs.get(src["mbid"]) or []
                    artist, title = found[0] if found else (str(src.get("artist_name") or ""), str(src.get("title") or ""))
                    want = normalize_title(song)
                    held = {src["mbid"], rg.get("mbid")}
                    isrc_hits = sorted({i for m in held if m for i, t in isrcs.get(m, []) if normalize_title(t) == want})
                    if not isrc_hits:
                        # Not on the stored answer - a case where that answer is the wrong
                        # artist's. Fall back to a recording with the song's title and credit, only
                        # when exactly one cached ISRC has both.
                        by_credit = {
                            i
                            for i, name in titles_normalized.get(want, [])
                            if normalize_name(name) == normalize_name(artist)
                        }
                        isrc_hits = sorted(by_credit) if len(by_credit) == 1 else []
                    isrc = isrc_hits[0] if isrc_hits else None
                    album = SpotifyAlbumRef(f"sp-{src['mbid']}", title, (artist,), None, "album", None)
                    # On a Various Artists compilation the song's own artist is the answer's, not the album's.
                    own = str(rg.get("artist_name") or artist) if src.get("artist_mbid") == VARIOUS_ARTISTS_MBID else artist
                    intent = TrackIntent(track_id, song, isrc, (own,), album, None, reason)
                    fresh = resolve_track(
                        intent,
                        lookup,
                        now=SNAPSHOT_NOW,
                        pending_since=None,
                        fallback_days=180,
                        scope=str(d.get("scope") or "album"),
                        followed_artist_mbids=followed,
                        rules=rules,
                        **relations,
                    )
            except MetadataError as exc:
                skipped[key] = f"lookup not in the cache: {str(exc)[:80]}"
                continue
            got = fresh.release_group
            results[key] = {
                "status": str(fresh.status),
                "step": fresh.step,
                "mbid": got.mbid if got else None,
                "title": got.title if got else None,
                "type": str(got.primary_type) if got and got.primary_type else None,
                "stored_mbid": rg.get("mbid"),
                "stored_title": rg.get("title"),
                "stored_type": rg.get("primary_type"),
            }
        lookup.close()

    doc = {
        "likearr": str(Path(likearr.__file__).parent),
        "resolver_version": RESOLVER_VERSION,
        "stored": len(stored),
        "replayed": len(results),
        "skipped": skipped,
        "network_attempts": transport.attempts,
        "results": results,
        "lidarr_check": lidarr,
    }
    out.write_text(json.dumps(doc, indent=1, sort_keys=True))
    print(
        f"resolver {RESOLVER_VERSION}: {len(results)} of {len(stored)} replayed, {len(skipped)} skipped, "
        f"{transport.attempts} network attempt(s) refused"
    )
    print(_lidarr_counts(lidarr))


def compare_lidarr(first: Path, others: list[Path], detail: Path | None) -> None:
    """Where the Lidarr fallback check moved between result files: counts to standard output, rows
    to `detail` only. A move is expected when the other file accepts and this one refuses, the source
    is ``lidarr-fallback`` or ``unknown``, and a name folds to "" under the old ASCII fold."""
    main = json.loads(first.read_text()).get("lidarr_check")
    if main is None:
        print(f"\n{first.name} has no lidarr fallback check")
        return
    moved: dict[str, list[dict[str, Any]]] = {}
    for p in others:
        doc = json.loads(p.read_text()).get("lidarr_check")
        if doc is None:
            print(f"\n{p.name} has no lidarr fallback check")
            continue
        counts: dict[str, int] = defaultdict(int)
        rows: list[dict[str, Any]] = []
        for key in sorted(set(main["rows"]) | set(doc["rows"])):
            a, b = main["rows"].get(key), doc["rows"].get(key)
            if a is None or b is None:
                counts["checked in one file only"] += 1
                continue
            if a["accepted"] == b["accepted"]:
                continue
            expected = (
                b["accepted"]
                and not a["accepted"]
                and a["source"] in {"lidarr-fallback", "unknown"}
                and a["ascii_fold_empty"]
            )
            label = "expected" if expected else "needs an explanation"
            counts[f"{label}: {a['source']}, {p.stem} {_verdict(b)} -> {first.stem} {_verdict(a)}"] += 1
            rows.append({"key": key, "expected": expected, first.stem: a, p.stem: b})
        moved[p.stem] = rows
        print(f"\nlidarr fallback check, {p.stem} -> {first.stem}: {len(rows)} answer(s) moved")
        for label, n in sorted(counts.items()):
            print(f"  {n:5d}  {label}")
    if detail is not None:
        detail.write_text(json.dumps(moved, indent=1, sort_keys=True))
        print(f"rows written to {detail}")


def _verdict(row: dict[str, Any]) -> str:
    return "accepts" if row["accepted"] else "refuses"


def compare(first: Path, others: list[Path]) -> None:
    main = json.loads(first.read_text())
    rest = [json.loads(p.read_text()) for p in others]
    print(
        f"{first.name}: resolver {main['resolver_version']}, {main['replayed']} of {main['stored']} replayed, "
        f"{len(main['skipped'])} skipped, {main['network_attempts']} network attempt(s)"
    )
    for p, doc in zip(others, rest, strict=True):
        print(f"{p.name}: resolver {doc['resolver_version']}, {doc['replayed']} replayed, {len(doc['skipped'])} skipped")
    lines = 0
    for key, r in sorted(main["results"].items()):
        if r["mbid"] == r["stored_mbid"]:
            continue
        lines += 1
        also = []
        for p, doc in zip(others, rest, strict=True):
            o = doc["results"].get(key)
            same = o is not None and o["mbid"] == r["mbid"]
            differs = o is not None and o["mbid"] != o["stored_mbid"]
            also.append(f"{p.stem}: {'same answer' if same else ('differs too' if differs else 'matches stored')}")
        stored = f"{(r['stored_mbid'] or '-')[:8]} {r['stored_type']} {r['stored_title']!r}"
        new = f"{(r['mbid'] or '-')[:8]} {r['type']} {r['title']!r} [{r['step']}]"
        print(f"{key}: stored {stored} -> {new}" + (f"  ({'; '.join(also)})" if also else ""))
    print(f"{lines} intent(s) differ from the stored resolutions")
    for p, doc in zip(others, rest, strict=True):
        print(f"\nwhere {first.stem} and {p.stem} answer differently (the effect of the code between them):")
        n = 0
        for key in sorted(set(main["results"]) | set(doc["results"])):
            a, b = main["results"].get(key), doc["results"].get(key)
            if a is None or b is None or a["mbid"] != b["mbid"]:
                n += 1
                fmt = lambda r: "skipped" if r is None else f"{(r['mbid'] or '-')[:8]} {r['type']} {r['title']!r} [{r['step']}]"  # noqa: E731
                ref = a or b or {}
                print(
                    f"{key}: stored {(ref.get('stored_mbid') or '-')[:8]} {ref.get('stored_type')} "
                    f"{ref.get('stored_title')!r} | {p.stem}: {fmt(b)} | {first.stem}: {fmt(a)}"
                )
        print(f"{n} intent(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", nargs="?", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs="+", type=Path, metavar="RESULT")
    parser.add_argument(
        "--lidarr-detail", type=Path, metavar="FILE", help="with --compare: write the Lidarr check's moved rows here"
    )
    args = parser.parse_args(argv)
    if args.compare:
        compare(args.compare[0], args.compare[1:])
        compare_lidarr(args.compare[0], args.compare[1:], args.lidarr_detail)
        return 0
    if args.snapshot is None or args.out is None:
        parser.error("give a snapshot and --out, or --compare")
    run(args.snapshot, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
