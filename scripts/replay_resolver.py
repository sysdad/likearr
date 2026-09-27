"""Offline resolver replay: re-resolve a state snapshot's intents from its `mb_cache` alone.

Read-only and network-free. Written for the saved-album regression that followed #40
(RESOLVER_VERSION 6); kept because every future resolver change deserves the same check. Not
shipped: the Docker image copies `likearr/` only.

    uv run python scripts/replay_resolver.py SNAPSHOT.sqlite --out replay.json
    PYTHONPATH=/path/to/other/checkout python scripts/replay_resolver.py SNAPSHOT.sqlite --out old.json
    python scripts/replay_resolver.py --compare new.json old.json [...]

What it does, and what it cannot do:

- **The baseline** is the snapshot's own `resolutions` table - the answers that install holds.
- **No network.** The snapshot is copied to a temporary file (the adapter opens it read-write) and
  the MusicBrainz adapter is given an HTTP transport that refuses every request and counts it. Every
  cache entry answers whatever its age, exactly as a MusicBrainz outage would be served. An intent
  needing a lookup the cache does not hold fails with `error:metadata` and is reported as skipped.
  The snapshot itself is never written. A snapshot from before #123 may hold `isrc-search` and
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
- **The relationship rule (#14) is on** when the code under test has it, as in a normal run, so the
  replay runs the code that ships. It only acts where a track would otherwise be UNMAPPED at
  `track:album:search`, which a stored intent reaches only if the replay's approximate
  reconstruction misses; its `artist-rels:` lookup is then usually not cached, so it is skipped.
- **The opt-outs are the ones each resolution was made under**, read back from its stored `rules`
  token (``""``, ``c1r0``, ``c1r0k0``, ...), passing only the fields the code under test has - so a
  checkout from before `keep_remix_only_tracks` (#89) replays ``c1r0`` as it always did. The deny
  list is not in the token and is not replayed. Excluded tracks are UNMAPPED and so not stored:
  what the remix-only rule would now keep is invisible here, and only moves *away* from a stored
  answer are measured.

The `--compare` mode prints one line per intent whose release group differs from the baseline in
the first result file, and says for each whether the other result files (other code) differ too.
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
        # A checkout from before #14 (RESOLVER_VERSION 7 or older) has no `relations` parameter.
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
                        # Not on the stored answer - the #32 case, where that answer is the wrong
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
    }
    out.write_text(json.dumps(doc, indent=1, sort_keys=True))
    print(
        f"resolver {RESOLVER_VERSION}: {len(results)} of {len(stored)} replayed, {len(skipped)} skipped, "
        f"{transport.attempts} network attempt(s) refused"
    )


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
    args = parser.parse_args(argv)
    if args.compare:
        compare(args.compare[0], args.compare[1:])
        return 0
    if args.snapshot is None or args.out is None:
        parser.error("give a snapshot and --out, or --compare")
    run(args.snapshot, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
