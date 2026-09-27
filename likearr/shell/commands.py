"""The commands besides `likearr run` that have no module of their own: auth, playlists, adopt,
lidarr-files, explain.

These commands share one shape. They read, they print what they would do, and they change
something only when the user typed `--apply`. `doctor` and `setup-profiles` live in
`shell.setup_commands`, the prune commands in `shell.prune_commands` and `promote-save` in
`shell.promote_save`.
"""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime
from pathlib import Path

from likearr.adapters.http import build_client
from likearr.adapters.lock import run_lock
from likearr.adapters.spotify import asks_for_write_scopes, authorized_request, can_read_collaborative, reauth_due
from likearr.adapters.spotify_library import PlaylistEntry
from likearr.core.adopt import adopt_digest, plan_adoption
from likearr.core.explain import explain_report, render_report
from likearr.fsio import write_atomic
from likearr.models import EXIT_ERROR, EXIT_OK, EXIT_STALE, RESOLVER_VERSION, LidarrView
from likearr.playlist_names import COLLABORATIVE_REAUTH_REASON, NOT_OWNED_REASON, NOT_OWNED_WORKAROUND, read_names
from likearr.ports import LidarrError, QuotaExceeded, SourceError
from likearr.shell.adopt_io import AdoptPlanFile, read_adopt_plan, write_adopt_plan
from likearr.shell.context import Context
from likearr.shell.diff_io import DiffFileError, read_diff
from likearr.shell.last_run import explain_from_last_run
from likearr.shell.output import emit
from likearr.shell.run import NAMES_SHOWN, plan
from likearr.shell.setup_commands import _quota_check

__all__ = [
    "adopt_command",
    "auth_command",
    "explain_command",
    "playlists_command",
]

SPOTIFY_ME_URL = "https://api.spotify.com/v1/me"
"""The cheapest authenticated call there is, used to confirm a fresh token works."""


# ---------------------------------------------------------------------------- auth


def auth_command(
    ctx: Context,
    *,
    manual: bool = False,
    promote_save: bool = False,
) -> int:
    """`likearr auth`: run the Spotify PKCE flow and write the token file.

    The consent screen asks to read only (#161), unless `promote_save` (``--promote-save``) opts
    into the write scopes `promote-save` needs, or the token being replaced already has them
    (`asks_for_write_scopes`: a routine re-auth keeps what you approved before).

    Neither the authorization code nor any token is ever printed. What is printed is the scopes
    Spotify granted, when the access token expires, when Spotify's six-month refresh-token clock
    runs out, and the account the token belongs to - which is the one thing a user actually needs
    to check (it is easy to authorize the wrong account).
    """
    if ctx.auth is None:
        emit(f"FAIL  spotify is not configured: {ctx.spotify_error or 'no client id'}")
        return EXIT_ERROR

    include_write = asks_for_write_scopes(ctx.config.spotify.token_file, promote_save=promote_save)
    url, verifier, state = ctx.auth.build_authorize_url(include_write=include_write)
    if promote_save:
        emit("Asking Spotify to read your library, and for the write access promote-save needs.")
    elif include_write:
        emit("Asking Spotify to read your library, and for the write access your current token already has.")
    else:
        emit(
            "Asking Spotify to read your library: read-only. To let promote-save follow artists and "
            "save albums, run this again with --promote-save."
        )
    emit("Open this URL in a browser and approve access:")
    emit("")
    emit(f"  {url}")
    emit("")

    try:
        if manual:
            pasted = input("Paste the full redirect URL you were sent to: ").strip()
            code, returned_state = ctx.auth.parse_redirect_url(pasted)
        else:
            emit(f"Waiting for the callback on {ctx.config.spotify.redirect_uri} ...")
            code, returned_state = ctx.auth.run_local_callback_server(
                ctx.config.spotify.redirect_uri, expected_state=state
            )
    except (SourceError, OSError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    if returned_state != state:  # a missing state fails too, as in the web flow (#171)
        emit("FAIL  the 'state' parameter did not match; the callback did not come from this run")
        return EXIT_ERROR

    try:
        tokens = ctx.auth.exchange_code(code, verifier)
    except SourceError as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    expires = datetime.fromtimestamp(tokens.expires_at, tz=UTC).isoformat(timespec="seconds")
    emit(f"PASS  token written to {ctx.config.spotify.token_file}")
    emit(f"      scopes:  {tokens.scope or '(none reported)'}")
    emit(f"      expires: {expires}")
    if tokens.authorized_at is not None:
        due = reauth_due(datetime.fromtimestamp(tokens.authorized_at, tz=UTC))
        emit(f"      re-auth due: {due.date().isoformat()} (Spotify refresh tokens last six months)")

    try:
        who = _spotify_me(ctx)
    except QuotaExceeded as exc:
        # The token is written and good; only the account check could not be asked. WARN, as for
        # any other /me failure, but in the words doctor uses, so the quota reads the same everywhere.
        quota = _quota_check("GET /me", exc, skipping=False)
        emit(f"WARN  {quota.name}: {quota.detail} The token was still written")
        return EXIT_OK
    except (SourceError, OSError) as exc:  # OSError: the token lock file, as in the callback step
        emit(f"WARN  could not confirm the account with GET /me ({exc}); the token was still written")
        return EXIT_OK
    if who is None:
        emit("WARN  GET /me named no account; the token was still written")
        return EXIT_OK
    emit(f"      account: {who}")
    return EXIT_OK


def _spotify_me(ctx: Context) -> str | None:
    """``display_name (id)`` for the authorized account, or None if the answer named neither.

    Raises:
        SourceError: the request failed. `QuotaExceeded` among them, and never retried, like
            every other call that goes through `authorized_request`.
    """
    if ctx.auth is None:
        return None
    with build_client() as client:
        payload = authorized_request(client, ctx.auth, "GET", SPOTIFY_ME_URL, context="GET /me")
    name = str(payload.get("display_name") or "")
    account_id = str(payload.get("id") or "")
    return f"{name} ({account_id})" if name else account_id or None


# ---------------------------------------------------------------------------- playlists


def playlists_command(ctx: Context, *, as_json: bool = False) -> int:
    """`likearr playlists [--json]`: every Spotify playlist this account can see, which of them
    are configured, and which are not readable at all.

    Read-only: one ``GET /me`` and the pages of ``GET /me/playlists``. Every playlist Spotify
    lists is included, readable or not - Development Mode returns items only for a playlist you
    own or collaborate on, so the rest is what the web picker greys out, not what a run can read.

    ``--json`` is a machine interface - the web UI runs this command as a child process and
    parses its stdout - so it prints exactly one line and its shape is a contract::

        {"playlists": [{"id": str, "name": str, "track_count": int, "owned": bool,
                        "readable": bool, "needs_reauth": bool}, ...],
         "configured": [str, ...],
         "missing": [str, ...]}

    ``playlists`` is sorted by name (casefolded), then id. ``owned`` is false for a playlist this
    account can see but does not own - followed, someone else's, or one of Spotify's own
    algorithmic or editorial playlists. ``readable`` is what a run can read and the picker offers:
    owned, or one you collaborate on once the token has ``playlist-read-collaborative`` (#103,
    item 3). ``needs_reauth`` marks a collaborative one the stored token predates: re-authorizing,
    not copying it, makes it readable. ``configured`` is
    ``[spotify].playlists`` in config order, and ``missing`` is the configured ids Spotify does not
    list at all (a deleted playlist, or the wrong id), in the same order - a configured id that is
    merely not readable shows up in ``playlists`` with ``readable: false`` instead. Only add keys.

    Any failure - Spotify not configured, an expired authorization, the quota - is one ``FAIL``
    line and exit 1, with or without ``--json``; a caller must treat a non-zero exit as "no
    answer" and never parse that line.
    """
    if ctx.library is None:
        emit(f"FAIL  spotify is not configured: {ctx.spotify_error or 'no client id'}")
        return EXIT_ERROR
    try:
        entries = ctx.library.all_playlists()
    except SourceError as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    configured = list(ctx.config.spotify.playlists)
    known_ids = {p.id for p in entries}
    missing = [playlist_id for playlist_id in configured if playlist_id not in known_ids]

    if as_json:
        emit(
            json.dumps(
                {
                    "playlists": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "track_count": p.track_count,
                            "owned": p.owned,
                            "readable": p.readable,
                            "needs_reauth": p.needs_reauth,
                        }
                        for p in entries
                    ],
                    "configured": configured,
                    "missing": missing,
                }
            )
        )
        return EXIT_OK

    readable = [p for p in entries if p.readable]
    reauth = [p for p in entries if p.needs_reauth]
    unreadable = [p for p in entries if not p.readable and not p.needs_reauth]

    def row(p: PlaylistEntry) -> str:
        mark = "*" if p.id in configured else " "
        shared = " (collaborative)" if p.collaborative and not p.owned else ""
        name = _clip(p.name, 40 - len(shared)) + shared
        return f"{mark} {name:<40}  {p.id:<22}  {p.track_count:>6}"

    if not entries:
        emit("no playlists: this Spotify account has none")
    else:
        emit(f"  {'NAME':<40}  {'ID':<22}  {'TRACKS':>6}")
        for p in readable:
            emit(row(p))
        if reauth:
            emit("")
            emit(f"Not readable yet ({COLLABORATIVE_REAUTH_REASON}):")
            for p in reauth:
                emit(row(p))
        if unreadable:
            emit("")
            emit(f"Not readable ({NOT_OWNED_REASON}; {NOT_OWNED_WORKAROUND}):")
            for p in unreadable:
                emit(row(p))
        emit("")
        emit(
            f"{len(readable)} readable, {len(reauth) + len(unreadable)} not readable; "
            "* = already in [spotify].playlists"
        )
    for p in reauth:
        if p.id in configured:
            emit(f"WARN  [spotify].playlists has {p.id} ('{p.name}'): {COLLABORATIVE_REAUTH_REASON}.")
    for p in unreadable:
        if p.id in configured:
            emit(
                f"WARN  [spotify].playlists has {p.id} ('{p.name}'), which is not owned by you: "
                f"{NOT_OWNED_REASON}; {NOT_OWNED_WORKAROUND}."
            )
    # A token without playlist-read-collaborative may not be shown someone else's collaborative
    # playlist at all (#103, item 3), so a missing id may only need a re-authorization.
    hint = ""
    if missing:
        try:
            scopes: frozenset[str] | None = ctx.library.granted_scopes()
        except SourceError:
            scopes = None
        if not can_read_collaborative(scopes):
            hint = "; or, if you collaborate on it, re-authorize Spotify (Settings, or `likearr auth`)"
    for playlist_id in missing:
        emit(
            f"WARN  [spotify].playlists has {playlist_id}, which this account cannot see at all: "
            f"deleted, or the wrong id{hint}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------- adopt


def lidarr_files_command(ctx: Context, *, plan_file: Path, out: Path | None = None, as_json: bool = False) -> int:
    """`likearr lidarr-files --plan FILE [--out FILE] [--json]`: how many track files Lidarr holds
    for each release a plan would unmonitor, so a reviewer sees what stays on disk.

    Read-only: one ``load_view`` over the unmonitored releases' artists (``GET /artist``, then
    ``GET /album?artistId=`` per artist, and the profile and tag lists). It writes nothing to
    Lidarr, takes no run lock and is never part of ``run``: the web UI starts it as a child job
    after a check finishes.

    The answer is a contract (``--out`` writes it, ``--json`` prints it as one line)::

        {"albums": {"<release group>": {"track_files": int, "size_on_disk": int}, ...},
         "missing": ["<release group>", ...]}

    ``missing`` is an unmonitored release Lidarr no longer lists. Only add keys. Any failure is
    one ``FAIL`` line and exit 1, and no file is written: a caller must read that as "unavailable".
    """
    try:
        diff = read_diff(plan_file)
        keys = [u.key for u in diff.unmonitor]
        view = ctx.lidarr.load_view(sorted({k.artist_mbid for k in keys}))
    except (DiffFileError, LidarrError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR
    albums: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    for key in keys:
        album = view.album(key)
        if album is None:
            missing.append(key.rg_mbid)
        else:
            albums[key.rg_mbid] = {"track_files": album.track_file_count, "size_on_disk": album.size_on_disk}
    answer = {"albums": albums, "missing": missing}
    if out is not None:
        write_atomic(out, json.dumps(answer) + "\n")
    if as_json:
        emit(json.dumps(answer))
        return EXIT_OK
    with_files = sum(1 for a in albums.values() if a["track_files"])
    emit(
        f"likearr lidarr-files: {len(keys)} release(s) to unmonitor, {with_files} with files on disk"
        + (f", {len(missing)} not in Lidarr" if missing else "")
    )
    return EXIT_OK


def _view_of_everything(ctx: Context) -> LidarrView:
    """Every artist's albums, loaded one artist at a time.

    `adopt` and `prune-report` are the two commands whose subject is precisely what likearr does
    *not* know about, so the planning view - which loads albums only for artists some source or
    ownership record named - is blind to exactly the rows they exist to find. This is still one
    ``?artistId=`` request per artist and never an unfiltered ``GET /album``.
    """
    artists = ctx.lidarr.load_view(None).artists
    return ctx.lidarr.load_view(sorted(artists))


def read_keep_file(path: Path | None) -> set[str]:
    """Read a keep-list: one release group MBID or ``artist:<mbid>`` per line, ``#`` comments."""
    if path is None:
        return set()
    keep: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            keep.add(line)
    return keep


def adopt_command(
    ctx: Context,
    *,
    keep_file: Path | None = None,
    apply_path: Path | None = None,
    out: Path = Path("adopt.json"),
    now: datetime | None = None,
) -> int:
    """`likearr adopt`: take responsibility for monitoring that predates likearr.

    Plan -> reviewed file -> apply, like `run` and `promote-save`. Without `apply_path` this only
    plans: it writes `out` and changes nothing. With `apply_path` it executes exactly that plan,
    and refuses (exit 3) one whose world has moved. Both hold the run lock.

    Three outcomes per monitored release Lidarr has and likearr does not own: **claim** (a source
    wants it anyway - recorded as owned with its real reasons, plus `manual` when it is on the
    keep-list too, Lidarr untouched), **keep** (on
    the keep-list - recorded as `manual`, which the tool can never unmonitor) and **unmonitor**
    (nobody asked for it). Unmonitored releases are deliberately NOT recorded as owned: they were
    never likearr's, and recording them would claim something it did not do.

    The keep list is part of the plan and is not read again at apply time. That is the safety
    property: a bare re-run cannot unmonitor a manual monitor because the keep file went missing.

    Returns:
        0 ok, 1 refused or failed, 3 the plan is stale.

    Raises:
        LockHeld: another run holds the lock.
        DiffFileError: the plan file is missing or is not an adopt plan.
    """
    now = now or datetime.now(UTC)
    if apply_path is not None and keep_file is not None:
        emit("refused: --keep applies when planning; the reviewed plan already carries its keep list")
        return EXIT_ERROR
    with run_lock(ctx.lock_path):
        if apply_path is None:
            return _adopt_plan(ctx, keep_file=keep_file, out=out, now=now)
        return _adopt_apply(ctx, apply_path, now=now)


def _adopt_plan(ctx: Context, *, keep_file: Path | None, out: Path, now: datetime) -> int:
    try:
        result = plan(ctx, now=now, scheduled=False)
    except (SourceError, LidarrError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    keep = read_keep_file(keep_file)
    owned = ctx.state.owned_releases()
    view = _view_of_everything(ctx)
    adoption = plan_adoption(result.desired, view, owned, keep, now=now)

    emit(f"{'action':<10} {'artist':<28} {'title':<38} {'type':<12} files")
    # A claim on the keep list is kept by hand too; the label says so, so the reviewed plan shows it.
    rows = [
        *(("claim+keep" if r.is_manual else "claim", r.key) for r in adoption.claim),
        *(("keep", r.key) for r in adoption.keep_as_manual),
        *(("unmonitor", u.key) for u in adoption.unmonitor),
    ]
    for label, key in rows:
        album = view.album(key)
        artist = view.artists.get(key.artist_mbid)
        emit(
            f"{label:<10} {_clip(artist.name if artist else key.artist_mbid, 28):<28} "
            f"{_clip(album.title if album else key.rg_mbid, 38):<38} "
            f"{_clip(str(album.primary_type or '?') if album else '?', 12):<12} "
            f"{album.track_file_count if album else 0}"
        )

    write_adopt_plan(
        AdoptPlanFile(
            created_at=now,
            source_digest=result.snapshot.digest(),
            lidarr_digest=adopt_digest(view, adoption, owned),
            resolver_version=RESOLVER_VERSION,
            adoption=adoption,
        ),
        out,
    )
    emit("")
    emit(
        f"{len(adoption.claim)} to claim, {len(adoption.keep_as_manual)} to keep, "
        f"{len(adoption.unmonitor)} to unmonitor"
    )
    # Adopt only records ownership. The next `likearr run` then sets "Monitor New Albums" to None on
    # every artist holding a claimed or kept release (#172), so the write is stated here, where the
    # decision is made.
    held = {record.key.artist_mbid for record in (*adoption.claim, *adoption.keep_as_manual)}
    names = sorted(
        (
            a.name or a.mbid
            for mbid in held
            if (a := view.artists.get(mbid)) is not None and a.monitor_new_items != "none"
        ),
        key=str.casefold,
    )
    if names:
        more = f", and {len(names) - NAMES_SHOWN} more" if len(names) > NAMES_SHOWN else ""
        emit(
            f'the next run sets "Monitor New Albums" to None on {len(names)} artist(s) whose releases '
            f"likearr will then own: {', '.join(names[:NAMES_SHOWN])}{more}"
        )
    emit(f"written to {out}")
    emit(f"review it, then run `likearr adopt --apply {out}` to execute exactly that plan")
    return EXIT_OK


def _adopt_apply(ctx: Context, plan_path: Path, *, now: datetime) -> int:
    """Execute exactly the reviewed plan, after checking that its world has not moved."""
    reviewed = read_adopt_plan(plan_path)
    if reviewed.resolver_version != RESOLVER_VERSION:
        raise DiffFileError(
            f"the adopt plan at {plan_path} was made by resolver version {reviewed.resolver_version}, "
            f"but this likearr is version {RESOLVER_VERSION}; re-plan before applying"
        )
    try:
        fresh = plan(ctx, now=now, scheduled=False)
        view = _view_of_everything(ctx)
    except (SourceError, LidarrError) as exc:
        emit(f"FAIL  {exc}")
        return EXIT_ERROR

    adoption = reviewed.adoption
    if reviewed.source_digest != fresh.snapshot.digest() or reviewed.lidarr_digest != adopt_digest(
        view, adoption, ctx.state.owned_releases()
    ):
        emit(f"the world moved since {plan_path} was planned; nothing was changed")
        emit("  re-run `likearr adopt` and review the new plan")
        return EXIT_STALE

    with ctx.state.transaction():
        ctx.state.record_monitored([*adoption.claim, *adoption.keep_as_manual])

    album_ids = [album.id for item in adoption.unmonitor if (album := view.album(item.key)) is not None]
    from likearr.adapters.lidarr import BATCH_SIZE

    for start in range(0, len(album_ids), BATCH_SIZE):
        ctx.lidarr.set_albums_monitored(album_ids[start : start + BATCH_SIZE], False)
    emit(f"ok    claimed {len(adoption.claim)}, kept {len(adoption.keep_as_manual)}, unmonitored {len(album_ids)}")
    return EXIT_OK


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------------------- explain


def explain_command(
    ctx: Context,
    query: str,
    *,
    now: datetime | None = None,
    as_json: bool = False,
    names_file: Path | None = None,
    from_last_run: bool = False,
) -> int:
    """`likearr explain [--json] [--from-last-run] <query>`: why a release is (or is not) monitored.

    Writes nothing. The report leads with a plain-language summary (see `core.explain`).

    Live, the default: plans afresh, reading Spotify and Lidarr as a dry run does - minutes. It is
    told what this plan's guards did (the diff's name collisions), what Spotify credits, and each
    matched artist's MusicBrainz disambiguation.

    ``--from-last-run``: the same report over what the last `run` recorded (`shell.last_run`), with
    no request to anyone - at once. It says when that run was.

    Playlists are named from the web UI's names file either way. ``--json`` prints the report as one
    line, which the web UI renders: ``{"query", "summary": [{"text", "wrong_match", "links":
    [{"label", "url"}]}], "details"}``, plus ``as_of`` and ``applied`` from the last run.
    """
    names = read_names(names_file).names if names_file is not None else {}
    extra: dict[str, object] = {}
    if from_last_run:
        answer = explain_from_last_run(query, config=ctx.config, owned=ctx.state.owned_releases(), playlist_names=names)
        if answer is None:
            emit(
                "FAIL  no run has been recorded for explain yet: run `likearr run` once, or leave out "
                "--from-last-run to ask Spotify and Lidarr live"
            )
            return EXIT_ERROR
        report, last = answer
        extra = {"as_of": last.ran_at.isoformat(), "applied": last.applied, "kind": last.kind}
        heading = (
            f"As of the last run, {last.ran_at.astimezone():%Y-%m-%d %H:%M %Z} ({last.label}). "
            "Leave out --from-last-run to ask live.\n"
        )
    else:
        now = now or datetime.now(UTC)
        try:
            result = plan(ctx, now=now, scheduled=False, persist=False)
        except (SourceError, LidarrError) as exc:
            emit(f"FAIL  {exc}")
            return EXIT_ERROR
        details = ctx.artist_details
        report = explain_report(
            query,
            desired=result.desired,
            owned=ctx.state.owned_releases(),
            view=result.view,
            resolutions=result.resolve_result.resolutions,
            artist_resolutions=result.resolve_result.artist_resolutions,
            snapshot=result.snapshot,
            collisions=result.diff.name_collisions,
            disambiguation=functools.cache(details.artist_disambiguation) if details is not None else None,
            playlist_names=names,
            unmonitor=[u.key for u in result.diff.unmonitor],
            guards=result.diff.guards,
        )
        heading = ""
    if as_json:
        emit(json.dumps({**report.as_dict(), **extra}))
    else:
        emit((heading + "\n" if heading else "") + render_report(report).rstrip("\n"))
    return EXIT_OK
