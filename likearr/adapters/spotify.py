"""Spotify Web API source adapter (Development Mode rules, Feb 2026+).

Implements :class:`likearr.ports.SourcePort`. The read is strictly all-or-nothing: any HTTP
failure, auth failure, quota rejection, JSON decode error or failed schema canary raises
``SourceError``/``SchemaError`` and no snapshot is returned. A partial snapshot would make the
diff think the user un-liked everything that failed to load.

Development Mode facts this module encodes:

- Playlist items come from ``GET /playlists/{id}/items`` (not ``/tracks``) and the item is
  nested under ``items[].item``; ``items[].track`` is accepted as a legacy fallback.
- A playlist the user does not own returns **zero items** rather than an error. likearr
  detects that by comparing against the playlist's own ``tracks.total`` and fails loudly.
- ``external_ids`` (UPC / ISRC) was removed once and restored; it may vanish again. Its absence
  degrades the run (``schema_ok=False``) but never fails it, because name-based resolution is a
  first-class fallback.
- Pagination ends at the first page whose ``next`` is null. Each source's entry count is checked
  against the ``total`` its first page reports, so a run that ends early degrades the run
  (``schema_ok=False``, which refuses every unmonitor) instead of passing as the whole library.
- Redirect URIs must be a loopback **IP literal**; ``localhost`` is rejected by Spotify.
"""

from __future__ import annotations

import base64
import calendar
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from likearr.adapters.http import HttpError, default_retry_on, request_with_retries
from likearr.config import SpotifyConfig
from likearr.fsio import write_atomic
from likearr.models import (
    SPOTIFY_COLLABORATIVE_SCOPE,
    SPOTIFY_READ_SCOPES,
    SPOTIFY_WRITE_SCOPES,
    AlbumIntent,
    ArtistIntent,
    Reason,
    ReasonKind,
    SourceKind,
    SourceSnapshot,
    SpotifyAlbumRef,
    TrackIntent,
)
from likearr.ports import QuotaExceeded, SchemaError, SourceError

__all__ = [
    "ALL_SCOPES",
    "ME_URL",
    "READ_SCOPES",
    "REFRESH_TOKEN_LIFETIME_MONTHS",
    "TOKEN_REQUEST_WORST_CASE_S",
    "AccountRefused",
    "SpotifyAccount",
    "SpotifyAuth",
    "SpotifySource",
    "TokenSet",
    "album_ref",
    "asks_for_write_scopes",
    "authorized_request",
    "can_read_collaborative",
    "describe_wait",
    "fetch_account",
    "lacks_collaborative",
    "read_account",
    "read_authorized_at",
    "read_granted_scopes",
    "reauth_due",
]

ACCOUNTS_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
ACCOUNTS_TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
ME_URL = f"{API_BASE}/me"

READ_SCOPES = " ".join(SPOTIFY_READ_SCOPES)
"""What a sign-in asks for by default (#161): read follows, the library, and playlists the user
owns or collaborates on. Every command but `promote-save` needs nothing more."""

ALL_SCOPES = " ".join((*SPOTIFY_READ_SCOPES, *SPOTIFY_WRITE_SCOPES))
"""`READ_SCOPES` plus the two things `promote-save` writes (`user-follow-modify`,
`user-library-modify`). Asked for only when the user opts in (`likearr auth --promote-save`, or the
web UI's equivalent), or when the token being replaced already has them - see
`asks_for_write_scopes`.

Changing either string does not invalidate a stored token: a refresh sends no scope and keeps the
scopes the token was granted at consent time, never more. So a token granted before a scope was
added keeps working for everything it could already do, and gains the new scope only at the next
Connect / `likearr auth`. Whatever needs a scope checks the stored token for it and says so
(promote-save's write scopes; `can_read_collaborative` for collaborative playlists, #103); nothing
re-authorizes automatically.
"""

PAGE_LIMIT = 50
"""Spotify's maximum for /me/following, /me/albums, /me/tracks and playlist items."""

TOTAL_TOLERANCE = 2
"""How far a source's entry count may sit from the ``total`` Spotify reported before the read is
not trusted (#176). A like added or removed mid-read moves ``total``, and offset paging can skip or
repeat an entry when the library changes under it. Past this, ``schema_ok`` goes false and every
unmonitor is held back this run."""

_REFRESH_SKEW_S = 60.0
"""Refresh the access token this many seconds before it actually expires."""

_TOKEN_LOCK_TIMEOUT_S = 30.0
"""How long a process waits for another process's token refresh before giving up."""

_TOKEN_LOCK_POLL_S = 0.1
"""How often to retry the non-blocking lock while waiting - fast enough that tests don't."""

_QUOTA_MARKER = "QUOTA_EXCEEDED"

_TOKEN_ATTEMPTS = 2
_TOKEN_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_TOKEN_MAX_BACKOFF_S = 5.0

TOKEN_REQUEST_WORST_CASE_S = _TOKEN_ATTEMPTS * 30.0 + _TOKEN_MAX_BACKOFF_S
"""The longest a token request can take, retries and backoff included: 65 s.

Stop signals are held for exactly that long (`_stop_signals_deferred`), so it is also how long
a stopped `likearr` may take to go, and the web UI's job runner waits longer than this before it
escalates SIGTERM to SIGKILL - a SIGKILL inside the window would strand a rotated refresh token.
The token endpoint therefore gets its own short profile instead of the general one (4 attempts,
a 60 s read timeout and up to 60 s of backoff: minutes). Per attempt, 30 s is the sum of the
connect, write, read and pool timeouts above.
"""

REFRESH_TOKEN_LIFETIME_MONTHS = 6
"""How long a Spotify refresh token lives, counted from the user's authorization, in months.

Since 2026-07-20 Spotify expires a refresh token six months after the user authorized the app,
and a refresh does **not** extend that: the rotated refresh token inherits the original deadline.
After it, every refresh answers ``invalid_grant`` and only a fresh ``likearr auth`` helps. See
https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration.
"""


# ---------------------------------------------------------------------------- tokens


@dataclass(frozen=True, slots=True)
class TokenSet:
    """The contents of the token file. Written atomically at mode 0600."""

    access_token: str
    refresh_token: str
    expires_at: float
    """Epoch seconds."""
    scope: str = ""
    token_type: str = "Bearer"
    authorized_at: float | None = None
    """Epoch seconds when the user last authorized the app (`likearr auth`), or ``None`` for a
    token written before this was recorded. It starts Spotify's six-month refresh-token clock
    (see `REFRESH_TOKEN_LIFETIME_MONTHS`), so a refresh carries it forward unchanged; only a new
    authorization ever sets it."""
    user_id: str | None = None
    """The Spotify account the token belongs to (``GET /me``'s ``id``), or ``None`` until recorded.
    A refresh carries it forward, and records it when it is missing."""
    display_name: str | None = None

    def expired(self, now: float, *, skew: float = _REFRESH_SKEW_S) -> bool:
        return self.expires_at - skew <= now

    def to_json(self) -> str:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }
        # Omitted rather than written as null while unknown, so a token file from before this
        # field existed round-trips byte-for-byte through a refresh.
        if self.authorized_at is not None:
            data["authorized_at"] = self.authorized_at
        if self.user_id is not None:
            data["user_id"] = self.user_id
            data["display_name"] = self.display_name or ""
        return json.dumps(data, indent=2)

    @property
    def account(self) -> SpotifyAccount | None:
        return SpotifyAccount(self.user_id, self.display_name or "") if self.user_id is not None else None

    def with_account(self, account: SpotifyAccount | None) -> TokenSet:
        if account is None:
            return self
        return replace(self, user_id=account.id, display_name=account.name)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TokenSet:
        try:
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data.get("refresh_token") or ""),
                expires_at=float(data.get("expires_at") or 0.0),
                scope=str(data.get("scope") or ""),
                token_type=str(data.get("token_type") or "Bearer"),
                authorized_at=_epoch_or_none(data.get("authorized_at")),
                user_id=_text_or_none(data.get("user_id")),
                display_name=_text_or_none(data.get("display_name")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"spotify: token file is missing or malformed ({exc})") from exc


def _epoch_or_none(value: object) -> float | None:
    """A positive, finite epoch-seconds number, or ``None`` for anything else.

    Used for `TokenSet.authorized_at`, which is advisory: a missing or mangled date must never
    stop the token itself from loading, so garbage (a string, a bool, NaN, a negative number)
    reads as "not recorded" rather than raising.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _text_or_none(value: object) -> str | None:
    """A non-empty string, or ``None``: like `_epoch_or_none`, the account fields never stop a
    token from loading."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class SpotifyAccount:
    """Who a token belongs to: ``GET /me``'s ``id`` and ``display_name`` (which may be empty)."""

    id: str
    name: str

    @property
    def label(self) -> str:
        return self.name or self.id


def read_account(token_file: Path) -> SpotifyAccount | None:
    """The account recorded in the token file, or ``None``. The same guarantees as
    `read_authorized_at`: no lock, no token returned, and ``None`` for any problem at all."""
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    user_id = _text_or_none(data.get("user_id"))
    if user_id is None:
        return None
    return SpotifyAccount(user_id, _text_or_none(data.get("display_name")) or "")


class AccountRefused(SourceError):
    """``GET /me`` answered 403: Spotify let this account approve the app, but the app may not
    serve it. A Development Mode app serves only the accounts on its dashboard's User Management
    list."""


def fetch_account(
    client: httpx.Client, access_token: str, *, sleep: Callable[[float], None] = time.sleep
) -> SpotifyAccount:
    """``GET /me`` with `access_token` - not the stored token - on the token endpoint's short
    retry profile, since it can run inside the token lock.

    Raises:
        AccountRefused: a 403.
        SchemaError: the answer has no ``id``.
        SourceError: any other failure, `QuotaExceeded` among them.
    """
    try:
        response = request_with_retries(
            client,
            "GET",
            ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            retry_on=_retry_on,
            sleep=sleep,
            max_attempts=_TOKEN_ATTEMPTS,
            max_backoff=_TOKEN_MAX_BACKOFF_S,
            timeout=_TOKEN_TIMEOUT,
        )
    except HttpError as exc:
        if exc.status_code == 403:
            raise AccountRefused(f"spotify GET /me: Spotify refused this account (HTTP 403). ({exc})") from exc
        raise _as_source_error(exc, "spotify GET /me") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("spotify GET /me: response body is not valid JSON") from exc
    user_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise SchemaError("spotify GET /me: the answer has no 'id'")
    name = payload.get("display_name")
    return SpotifyAccount(user_id, name if isinstance(name, str) else "")


_SCOPE_NAME = re.compile(r"[a-z0-9-]{1,64}")


def read_granted_scopes(token_file: Path) -> frozenset[str] | None:
    """The scope names the stored token was granted, for a process that must never refresh it (the
    web server's Clean up checklist, #58). The same guarantees as `read_authorized_at`: no token
    lock, nothing but scope names returned (each checked to look like one), and ``None`` - never
    an exception or a log line - for any problem at all."""
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scope"), str):
        return None
    return frozenset(name for name in data["scope"].split() if _SCOPE_NAME.fullmatch(name))


def can_read_collaborative(granted: frozenset[str] | None) -> bool:
    """Whether a token with these scopes may read a playlist the user collaborates on (#103).

    ``None`` (no token, or no scopes recorded) is ``False``: until a Connect grants
    `SPOTIFY_COLLABORATIVE_SCOPE`, a collaborative playlist someone else owns is not offered.
    """
    return granted is not None and SPOTIFY_COLLABORATIVE_SCOPE in granted


def lacks_collaborative(granted: frozenset[str] | None) -> bool:
    """Whether a stored token (``None``: no token) predates `SPOTIFY_COLLABORATIVE_SCOPE` (#103):
    what Settings and Status show their one-time "re-authorize for collaborative playlists" note on.
    """
    return granted is not None and not can_read_collaborative(granted)


def asks_for_write_scopes(token_file: Path, *, promote_save: bool = False) -> bool:
    """Whether a sign-in should ask for `ALL_SCOPES` rather than `READ_SCOPES` (#161).

    Always when the user opted in (`promote_save`). Otherwise only when the token being replaced
    already has every write scope: re-authorizing replaces the token, and a routine re-auth every
    six months should not quietly drop write access the user approved before ("keep what you
    have"). A new user - no token file, or one that recorded no scopes - gets read-only.
    Reads the token file like `read_granted_scopes`: no lock, no exception, no network.
    """
    if promote_save:
        return True
    granted = read_granted_scopes(token_file)
    return granted is not None and set(SPOTIFY_WRITE_SCOPES) <= granted


def read_authorized_at(token_file: Path) -> datetime | None:
    """When the user authorized likearr, from the token file, as an aware UTC datetime.

    For a process that must never refresh the token - the web server - and so reads the file
    **without** the token lock: every write is `likearr.fsio.write_atomic`, a temp file renamed
    into place, so a read always sees one whole file, old or new, and waiting behind a run's
    refresh would buy nothing. It returns only the authorization date: never a token, and nothing that could
    carry one. Any problem at all (no file, unreadable, not JSON, no date, a nonsense date)
    answers ``None`` rather than raising or logging, because the file's contents must not reach
    an exception message or a log line either.
    """
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    epoch = _epoch_or_none(data.get("authorized_at"))
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def reauth_due(authorized_at: datetime) -> datetime:
    """The date Spotify stops honouring the refresh token: `REFRESH_TOKEN_LIFETIME_MONTHS` later.

    Calendar months, with the day clamped to the target month's length - authorized on 31 August,
    due on 28 February (29th in a leap year) - and the time of day and timezone kept. Pure; the
    Status page and `likearr auth` both show it. Spotify's post
    (https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration) says "six months"
    without defining the arithmetic, so clamping errs on the early side of any ambiguity.
    """
    months = authorized_at.month - 1 + REFRESH_TOKEN_LIFETIME_MONTHS
    year = authorized_at.year + months // 12
    month = months % 12 + 1
    day = min(authorized_at.day, calendar.monthrange(year, month)[1])
    return authorized_at.replace(year=year, month=month, day=day)


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@contextmanager
def _stop_signals_deferred() -> Iterator[None]:
    """Hold SIGTERM and SIGINT pending while a token request is in flight and its answer is saved.

    Spotify rotates the refresh token on a refresh. A process stopped between the answer and the
    save leaves an already-used refresh token on disk, and every later run fails until someone runs
    `likearr auth --manual`. Stops do happen mid-request: the web UI's Cancel and a container
    recreate both send SIGTERM to a running job. Blocked, the signal is delivered the moment this
    block ends - after the save - and the process stops as it would have anyway. Only the main
    thread can do this (signals are delivered to it), so elsewhere this changes nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


# ---------------------------------------------------------------------------- PKCE / auth


def _pkce_verifier() -> str:
    """RFC 7636 code verifier: 43-128 chars from the unreserved set."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")[:128]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _check_https_redirect(redirect_uri: str) -> None:
    """Validate a direct-callback redirect URI (issue #79): https, a real host, no query/fragment.

    Used only when the web UI's `[ui] public_url` is configured and https; the loopback check
    above governs every other path (the CLI, and the web UI's default paste-back mode).
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "https":
        raise SourceError(f"spotify: a direct-callback redirect_uri must use https, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise SourceError("spotify: redirect_uri has no host")
    if parsed.query or parsed.fragment:
        raise SourceError("spotify: redirect_uri must not carry a query string or fragment")


LOOPBACK_READ_TIMEOUT_S = 5.0
"""How long the `likearr auth` loopback server waits on one connection's request before dropping it."""


def _loopback_answer(query: str, *, expected_state: str) -> tuple[int, dict[str, str]]:
    """What the `likearr auth` loopback server does with one request's query string.

    ``(200, {"code", "state"})`` or ``(200, {"error"})`` for a callback carrying the issued state,
    which ends the wait; ``(400, {})`` for a callback whose state is missing or different, and
    ``(404, {})`` for anything that is not a callback (a favicon request), both of which keep it
    waiting. Mirrors the web flow, which refuses an empty or unknown state.
    """
    params = urllib.parse.parse_qs(query)
    if "error" not in params and "code" not in params:
        return 404, {}
    state = params.get("state", [""])[0]
    if not state or not hmac.compare_digest(state.encode(), expected_state.encode()):
        return 400, {}
    if "error" in params:
        return 200, {"error": params["error"][0]}
    return 200, {"code": params["code"][0], "state": state}


def _check_loopback_redirect(redirect_uri: str) -> tuple[str, int, str]:
    """Validate the redirect URI and return (host, port, path).

    Spotify requires a loopback **IP literal** since 2025; ``http://localhost:PORT/...`` is
    rejected at the authorize step with an opaque error, so it is refused up front.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    host = (parsed.hostname or "").strip("[]")
    if host.lower() == "localhost":
        raise SourceError(
            "spotify: redirect_uri must be a loopback IP literal, not 'localhost'. "
            f"Use http://127.0.0.1:{parsed.port or 8765}{parsed.path or '/callback'} "
            "and register exactly that URI in the Spotify developer dashboard."
        )
    if host not in {"127.0.0.1", "::1"}:
        raise SourceError(
            f"spotify: redirect_uri host {host!r} is not a loopback address. "
            "Use http://127.0.0.1:PORT/callback (or http://[::1]:PORT/callback)."
        )
    if parsed.scheme != "http":
        raise SourceError("spotify: a loopback redirect_uri must use the http scheme")
    if not parsed.port:
        raise SourceError("spotify: redirect_uri must include an explicit port, e.g. http://127.0.0.1:8765/callback")
    return host, parsed.port, parsed.path or "/"


class SpotifyAuth:
    """Authorization Code with PKCE, plus the token file that survives between runs.

    No client secret is required. If ``LIKEARR_SPOTIFY_CLIENT_SECRET`` is set it is sent as a
    form field, which keeps the adapter usable with a classic confidential app.

    Spotify rotates refresh tokens: whenever a token response carries a new ``refresh_token``
    it is persisted immediately, before the access token is handed out.

    Every path that loads, refreshes or saves the token holds an exclusive ``fcntl.flock`` on
    ``<token_file>.lock`` for the whole load-check-refresh-save sequence, so two processes (a
    ``run`` and an ``explain``, say) can't both load the same refresh token, both refresh, and
    have the second save clobber the first. See ``_token_lock``.
    """

    def __init__(
        self,
        config: SpotifyConfig,
        client: httpx.Client,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._client = client
        self._now = now
        self._sleep = sleep
        self._tokens: TokenSet | None = None

    # ---------------------------------------------------------------- authorize

    def build_authorize_url(
        self, *, redirect_uri: str | None = None, include_write: bool = False
    ) -> tuple[str, str, str]:
        """Return ``(url, code_verifier, state)`` for the PKCE authorization step.

        The consent screen asks for `READ_SCOPES`, or `ALL_SCOPES` with `include_write` (#161):
        callers decide that with `asks_for_write_scopes`.

        `redirect_uri` is the CLI's and the web UI's paste-back mode's default: the configured
        loopback URI (`[spotify] redirect_uri`), checked with `_check_loopback_redirect` exactly
        as before. Passing one overrides it - the web UI's direct-callback mode (issue #79) passes
        its ``https://<public_url>/spotify/callback``, checked with `_check_https_redirect`
        instead. Either way this is the one place a PKCE authorize URL is built; `exchange_code`
        must be called with the same `redirect_uri`, since Spotify requires an exact match.
        """
        uri = redirect_uri if redirect_uri is not None else self._config.redirect_uri
        if redirect_uri is None:
            _check_loopback_redirect(uri)
        else:
            _check_https_redirect(uri)
        verifier = _pkce_verifier()
        state = secrets.token_urlsafe(16)
        query = urllib.parse.urlencode(
            {
                "client_id": self._config.client_id,
                "response_type": "code",
                "redirect_uri": uri,
                "code_challenge_method": "S256",
                "code_challenge": _pkce_challenge(verifier),
                "state": state,
                "scope": ALL_SCOPES if include_write else READ_SCOPES,
                # Always show Spotify's page, which names the account about to approve: without
                # it, an app that already holds every scope is sent straight back.
                "show_dialog": "true",
            }
        )
        return f"{ACCOUNTS_AUTHORIZE_URL}?{query}", verifier, state

    @staticmethod
    def parse_redirect_url(pasted_url: str) -> tuple[str, str]:
        """Pull ``(code, state)`` out of the redirect URL the user pasted back.

        Raises:
            SourceError: the URL carries an ``error`` parameter or has no ``code``.
        """
        parsed = urllib.parse.urlparse(pasted_url.strip())
        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            raise SourceError(f"spotify: authorization was refused ({params['error'][0]})")
        code = params.get("code", [""])[0]
        if not code:
            raise SourceError(
                "spotify: that URL has no 'code' parameter. Paste the full address bar contents "
                "after approving, including everything from 'http://127.0.0.1'."
            )
        return code, params.get("state", [""])[0]

    def run_local_callback_server(
        self, redirect_uri: str, timeout_s: float = 300.0, *, expected_state: str
    ) -> tuple[str, str]:
        """Serve the loopback redirect once and return ``(code, state)``.

        Binds the exact host/port from ``redirect_uri``, answers the first callback carrying
        ``expected_state`` (the one `build_authorize_url` issued) with a short 'you can close this
        tab' page, and gives up after ``timeout_s``. A callback with no state or another one is
        answered 400 and the wait goes on (#171), so nothing else on this machine can end it.
        """
        host, port, _path = _check_loopback_redirect(redirect_uri)
        captured: dict[str, str] = {}

        class _Handler(BaseHTTPRequestHandler):
            # Bounds each connection's reads: the server now keeps waiting after a refused
            # callback, so a local client that connects and sends nothing must not hold
            # `handle_request` past the deadline (#171).
            timeout = LOOPBACK_READ_TIMEOUT_S

            def do_GET(self) -> None:  # http.server's required spelling
                query = urllib.parse.urlparse(self.path).query
                status, answer = _loopback_answer(query, expected_state=expected_state)
                if status == 404:
                    self.send_response(404)
                    self.end_headers()
                    return
                if status == 200:
                    captured.update(answer)
                    body = b"<html><body><h3>likearr: you can close this tab.</h3></body></html>"
                else:
                    body = (
                        b"<html><body><h3>likearr: this callback is not from the sign-in likearr is "
                        b"waiting for (its state does not match). Still waiting.</h3></body></html>"
                    )
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                """Silence http.server's stderr logging - it would print the query string."""

        deadline = self._now() + timeout_s
        with HTTPServer((host, port), _Handler) as server:
            server.timeout = 1.0
            while not captured and self._now() < deadline:
                server.handle_request()

        if "error" in captured:
            raise SourceError(f"spotify: authorization was refused ({captured['error']})")
        if "code" not in captured:
            raise SourceError(
                f"spotify: no authorization callback arrived within {timeout_s:.0f}s. "
                "Re-run with --manual to paste the redirect URL by hand."
            )
        return captured["code"], captured.get("state", "")

    # ---------------------------------------------------------------- token exchange

    def exchange_code(self, code: str, verifier: str, *, redirect_uri: str | None = None) -> TokenSet:
        """Trade an authorization code for a token set and persist it.

        Locked like every other write, even though it's the first token: `likearr auth` run twice
        at once (or racing an in-flight refresh of a still-valid prior token) must not interleave.

        This is an authorization, so it starts Spotify's six-month refresh-token clock afresh:
        `TokenSet.authorized_at` is now.

        `redirect_uri` must be exactly what `build_authorize_url` was called with for this
        `code` - Spotify rejects a mismatch - so it defaults the same way: the configured loopback
        URI unless the caller (the web UI's direct-callback mode) passes its own.
        """
        with self._token_lock():
            tokens = self.request_code_tokens(code, verifier, redirect_uri=redirect_uri)
            self._save(tokens)
            return tokens

    def request_code_tokens(self, code: str, verifier: str, *, redirect_uri: str | None = None) -> TokenSet:
        """`exchange_code` without the save: the tokens for `code`, written nowhere. The web UI
        checks whose account they are before it calls `save_authorization`."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri if redirect_uri is not None else self._config.redirect_uri,
            "client_id": self._config.client_id,
            "code_verifier": verifier,
        }
        return self._token_request(form, previous_refresh_token="", authorized_at=self._now(), save=False)

    def save_authorization(self, tokens: TokenSet) -> None:
        """Write tokens from `request_code_tokens`, under the token lock."""
        with self._token_lock():
            self._save(tokens)

    def record_account(self, account: SpotifyAccount) -> None:
        """Add `account` to the stored token, changing nothing else."""
        with self._token_lock():
            self._tokens = None
            self._save(self._load().with_account(account))

    def refresh(self) -> TokenSet:
        """Force a refresh using the stored refresh token and persist the result.

        Always makes a token request - callers (`doctor`'s canary, in particular) use this to
        prove the refresh endpoint itself works, not merely that the cached token is still valid.
        Still takes the token lock and re-reads the token file first, so a refresh_token another
        process rotated while this one waited is what gets sent, not a stale in-memory copy.
        """
        with self._token_lock():
            self._tokens = None
            tokens = self._load()
            return self._do_refresh(tokens)

    def access_token(self) -> str:
        """Return a usable access token, refreshing it if fewer than 60s remain.

        Loads, checks expiry, refreshes and saves as one locked critical section. A process that
        waited for the lock re-reads the token file before deciding anything: if another process
        already refreshed while this one waited, the fresh token on disk is used as-is and no
        token request is made at all.
        """
        with self._token_lock():
            self._tokens = None
            tokens = self._load()
            if tokens.expired(self._now()):
                tokens = self._do_refresh(tokens)
            return tokens.access_token

    def granted_scopes(self) -> frozenset[str]:
        """The scopes Spotify granted the stored token, from the token file. No network, no token.

        A token written before scopes were recorded reports the empty set, which callers treat the
        same as a missing scope: re-authorize. A refresh never widens scopes, so this is the whole
        answer.
        """
        return frozenset(self._load().scope.split())

    def _do_refresh(self, tokens: TokenSet) -> TokenSet:
        """Send `tokens.refresh_token` and persist the result. Caller must hold the token lock.

        Carries `tokens.authorized_at` forward unchanged, ``None`` included: a refresh does not
        extend Spotify's six-month refresh-token lifetime, so it must not restart the clock.
        """
        if not tokens.refresh_token:
            raise SourceError("spotify: token file has no refresh_token - run `likearr auth` again")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self._config.client_id,
        }
        fresh = self._token_request(
            form,
            previous_refresh_token=tokens.refresh_token,
            authorized_at=tokens.authorized_at,
            account=tokens.account,
        )
        if fresh.account is not None:
            return fresh
        # A token file from before accounts were recorded: record it now, or leave it for the next
        # refresh. The rotated refresh token is already saved either way.
        try:
            account = fetch_account(self._client, fresh.access_token, sleep=self._sleep)
        except SourceError:
            return fresh
        fresh = fresh.with_account(account)
        self._save(fresh)
        return fresh

    def _token_request(
        self,
        form: dict[str, str],
        *,
        previous_refresh_token: str,
        authorized_at: float | None,
        save: bool = True,
        account: SpotifyAccount | None = None,
    ) -> TokenSet:
        """POST to the token endpoint and persist the answer (unless `save` is false).

        `authorized_at` and `account` are the caller's decision, never derived here: now and
        unknown for a code exchange, the stored values for a refresh.
        """
        secret = self._config.client_secret
        if secret:
            form = {**form, "client_secret": secret}
        with _stop_signals_deferred():
            tokens = self._send(form, previous_refresh_token=previous_refresh_token, authorized_at=authorized_at)
            tokens = tokens.with_account(account)
            if save:
                self._save(tokens)
            return tokens

    def _send(self, form: dict[str, str], *, previous_refresh_token: str, authorized_at: float | None) -> TokenSet:
        try:
            response = request_with_retries(
                self._client,
                "POST",
                ACCOUNTS_TOKEN_URL,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                retry_on=_retry_on,
                sleep=self._sleep,
                max_attempts=_TOKEN_ATTEMPTS,
                max_backoff=_TOKEN_MAX_BACKOFF_S,
                timeout=_TOKEN_TIMEOUT,
            )
        except HttpError as exc:
            raise _as_source_error(exc, "spotify auth", token_request=True) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("spotify auth: token endpoint returned a non-JSON body") from exc
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise SourceError("spotify auth: token response has no access_token")

        # Spotify rotates refresh tokens: `_token_request` persists this before anything else can
        # fail, or the next run is locked out with a refresh token that has already been used.
        refresh_token = str(payload.get("refresh_token") or previous_refresh_token)
        expires_in = float(payload.get("expires_in") or 3600)
        return TokenSet(
            access_token=str(payload["access_token"]),
            refresh_token=refresh_token,
            expires_at=self._now() + expires_in,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or "Bearer"),
            authorized_at=authorized_at,
        )

    # ---------------------------------------------------------------- cross-process lock

    def _lock_path(self) -> Path:
        token_file = self._config.token_file
        return token_file.with_name(f"{token_file.name}.lock")

    @contextmanager
    def _token_lock(self) -> Iterator[None]:
        """Hold an exclusive lock on ``<token_file>.lock`` for a load-check-refresh-save section.

        `run`'s own lock (``adapters/lock.py``) only serialises `likearr run`; `explain` and the
        planned web UI's jobs refresh without it, so the token file needs a lock of its own.
        Polls with ``LOCK_NB`` plus the injected ``sleep`` instead of blocking inside `flock`
        itself, so a contended wait goes through the fake clock in tests instead of actually
        sleeping, and so a stuck holder can never wedge a caller forever - `SourceError` after
        `_TOKEN_LOCK_TIMEOUT_S` seconds beats hanging.
        """
        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fd:
            with suppress(OSError):  # pragma: no cover - best effort; a bad filesystem shouldn't block auth
                os.chmod(path, 0o600)
            deadline = self._now() + _TOKEN_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if self._now() >= deadline:
                        # Never advise deleting the lock file: the kernel drops a flock when its
                        # holder dies, so a timeout means a live process holds it, and deleting
                        # the file would let the next process lock a fresh inode alongside it -
                        # exactly the concurrent refresh this lock exists to prevent.
                        raise SourceError(
                            f"spotify: timed out after {_TOKEN_LOCK_TIMEOUT_S:.0f}s waiting for "
                            f"another likearr process to finish with the Spotify token ({path}); "
                            "retry once it has finished"
                        ) from exc
                    self._sleep(_TOKEN_LOCK_POLL_S)
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)

    # ---------------------------------------------------------------- token file

    def _load(self) -> TokenSet:
        if self._tokens is not None:
            return self._tokens
        path = self._config.token_file
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SourceError(
                f"spotify: no token file at {path} - Connect Spotify in Settings (or run `likearr auth`)"
            ) from exc
        except OSError as exc:
            raise SourceError(f"spotify: cannot read the token file at {path}: {exc.strerror}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SourceError(f"spotify: token file at {path} is not valid JSON") from exc
        if not isinstance(data, dict):
            raise SourceError(f"spotify: token file at {path} is not a JSON object")
        self._tokens = TokenSet.from_mapping(data)
        return self._tokens

    def _save(self, tokens: TokenSet) -> None:
        path = self._config.token_file
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, tokens.to_json(), mode=0o600)
        self._tokens = tokens


# ---------------------------------------------------------------------------- helpers


def _retry_on(response: httpx.Response) -> bool:
    """Retry like everyone else, except a hard quota rejection which will not improve."""
    if response.status_code == 429 and _QUOTA_MARKER in _peek(response):
        return False
    return default_retry_on(response)


def _peek(response: httpx.Response) -> str:
    try:
        return response.text[:400]
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - defensive
        return ""


def describe_wait(seconds: float) -> str:
    """A ``Retry-After`` for a person: ``45 s``, ``3600 s (about 60 min)``, ``86400 s (about 24 h)``."""
    whole = round(seconds)
    if whole < 120:
        return f"{whole} s"
    if whole < 7200:
        return f"{whole} s (about {round(whole / 60)} min)"
    return f"{whole} s (about {round(whole / 3600)} h)"


def _not_owned_message(playlist_id: str, detail: str, *, auth: SpotifyAuth) -> str:
    """The one message a playlist you don't own gets, whichever way Spotify said no.

    Shared by both detection paths - a 200 with zero items whose metadata reports tracks, and a
    403 straight from ``GET /playlists/{id}/items`` (issue #103, item 1) - so a hand-added
    playlist never reads like a token or scope failure. ``detail`` names which one happened;
    the diagnosis and the workaround stay identical either way. When the stored token predates
    `SPOTIFY_COLLABORATIVE_SCOPE`, it adds that a playlist you collaborate on needs a re-auth
    (#103, item 3), since that is the one case a re-authorization fixes.
    """
    message = (
        f"spotify: playlist {playlist_id} is not owned by you; {detail}. Remove it from "
        "[spotify].playlists, or copy it into a playlist you own."
    )
    try:
        granted: frozenset[str] | None = auth.granted_scopes()
    except SourceError:
        granted = None
    if not can_read_collaborative(granted):
        message += (
            " If you collaborate on it, re-authorize Spotify (Settings > Re-authorize Spotify, or "
            "`likearr auth`) so likearr may read playlists you collaborate on."
        )
    return message


def _as_source_error(exc: HttpError, context: str, *, token_request: bool = False) -> SourceError:
    if exc.status_code == 429 and _QUOTA_MARKER in exc.body_excerpt:
        wait = "" if exc.retry_after is None else f" Spotify says retry after {describe_wait(exc.retry_after)}."
        return QuotaExceeded(
            f"{context}: Spotify rejected the request with QUOTA_EXCEEDED. Development Mode quota is "
            f"per developer account; the run is aborted with zero unmonitors.{wait} ({exc})",
            retry_after=exc.retry_after,
            token_request=token_request,
        )
    if exc.status_code in (401, 403):
        return SourceError(f"{context}: Spotify refused the request (HTTP {exc.status_code}). ({exc})")
    if exc.status_code == 429 and exc.retry_after is not None:
        return SourceError(f"{context}: Spotify says retry after {describe_wait(exc.retry_after)}. ({exc})")
    return SourceError(f"{context}: {exc}")


def _parse_release_date(value: object, precision: object) -> date | None:
    """Parse Spotify's 'YYYY' / 'YYYY-MM' / 'YYYY-MM-DD' into a date, padding coarse values.

    ``release_date_precision`` is advisory: the string's own shape is authoritative, because
    Spotify has shipped mismatched pairs. Unparseable values (including the '0000' sentinel)
    become ``None`` rather than an error.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        return None
    if year < 1:
        return None
    if str(precision) == "year":
        month, day = 1, 1
    elif str(precision) == "month":
        day = 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_added_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _artist_names(entity: Mapping[str, Any]) -> tuple[str, ...]:
    artists = entity.get("artists")
    if not isinstance(artists, list):
        return ()
    return tuple(str(a.get("name") or "") for a in artists if isinstance(a, Mapping))


def authorized_request(
    client: httpx.Client,
    auth: SpotifyAuth,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    context: str,
    expect_object: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    not_owned_playlist_id: str | None = None,
) -> Any:
    """One authenticated Spotify call, with a single forced token refresh on a 401.

    Shared by the read source and the library adapter so both inherit the same retry policy, the
    same quota handling and the same "refresh once, then give up with an actionable message".

    Args:
        expect_object: the default, because every paged Spotify response is a JSON object and a
            bare array would mean the shape changed. Spotify's ``/contains`` endpoints genuinely
            answer a bare array of booleans; nothing in likearr calls them today (they are 403 on
            a Development Mode account), but the flag costs nothing and documents the exception.
        not_owned_playlist_id: set only by the playlist-items read. A 403 here means the same
            thing a 200-with-zero-items does elsewhere - the playlist is not owned by the
            authorized user - so it is worth its own plain message instead of the generic "Spotify
            refused the request" (issue #103, item 1: the Get Playlist Items reference documents
            403 for a non-owner, non-collaborator).

    Returns:
        The decoded JSON, or ``{}`` for a body-less success (Spotify's writes answer 200 or 204
        with an empty body).

    Raises:
        SourceError: auth failed twice, the quota is exhausted, or the transport gave up.
        SchemaError: `expect_object` and the body decoded to something else.
    """
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {auth.access_token()}"}
        try:
            response = request_with_retries(
                client,
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                retry_on=_retry_on,
                allow_status=(401,),
                sleep=sleep,
            )
        except HttpError as exc:
            if not_owned_playlist_id is not None and exc.status_code == 403:
                raise SourceError(
                    _not_owned_message(
                        not_owned_playlist_id,
                        "Spotify refused the request with HTTP 403 rather than its items",
                        auth=auth,
                    )
                ) from exc
            raise _as_source_error(exc, f"spotify {context}") from exc
        if response.status_code == 401:
            if attempt == 1:
                auth.refresh()
                continue
            raise SourceError(
                f"spotify {context}: still unauthorized after refreshing the access token - run `likearr auth` again"
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"spotify {context}: response body is not valid JSON") from exc
        if expect_object and not isinstance(payload, dict):
            raise SchemaError(f"spotify {context}: expected a JSON object, got {type(payload).__name__}")
        return payload
    raise AssertionError("unreachable")  # pragma: no cover


def _album_ref(album: Mapping[str, Any]) -> SpotifyAlbumRef:
    external = album.get("external_ids")
    upc = external.get("upc") if isinstance(external, Mapping) else None
    return SpotifyAlbumRef(
        spotify_id=str(album.get("id") or ""),
        name=str(album.get("name") or ""),
        artist_names=_artist_names(album),
        upc=str(upc) if upc else None,
        album_type=str(album.get("album_type") or ""),
        release_date=_parse_release_date(album.get("release_date"), album.get("release_date_precision")),
    )


album_ref = _album_ref
"""Public alias: the library adapter maps search hits with the same Spotify-album reader."""


def _playable_track(item: object) -> Mapping[str, Any] | None:
    """Return the track if it is a real, non-local track; ``None`` for locals and episodes."""
    if not isinstance(item, Mapping):
        return None
    if item.get("is_local"):
        return None
    if str(item.get("type") or "track") != "track":
        return None
    return item if item.get("id") else None


def _has_external_ids(entity: Mapping[str, Any], key: str) -> bool:
    external = entity.get("external_ids")
    return isinstance(external, Mapping) and bool(external.get(key))


# ---------------------------------------------------------------------------- the source


class SpotifySource:
    """Reads every configured Spotify source into one immutable :class:`SourceSnapshot`.

    Implements :class:`likearr.ports.SourcePort`.
    """

    def __init__(
        self,
        config: SpotifyConfig,
        auth: SpotifyAuth,
        client: httpx.Client,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._auth = auth
        self._client = client
        self._now = now
        self._sleep = sleep

    # ---------------------------------------------------------------- SourcePort

    def read(self) -> SourceSnapshot:
        """Read follows, saved albums, liked tracks and every configured playlist.

        Raises:
            SchemaError: a structural field the tool depends on is missing.
            SourceError: any other failure. Never returns a partial snapshot.
        """
        warnings: list[str] = []
        counts: dict[str, int] = {}
        artists: list[ArtistIntent] = []
        albums: list[AlbumIntent] = []
        tracks: list[TrackIntent] = []

        if self._config.followed_artists:
            artists = self._read_followed_artists(warnings)
            counts[str(SourceKind.FOLLOWED_ARTISTS)] = len(artists)
        if self._config.saved_albums:
            albums = self._read_saved_albums(warnings)
            counts[str(SourceKind.SAVED_ALBUMS)] = len(albums)
        if self._config.liked_tracks:
            liked = self._read_liked_tracks(warnings)
            tracks.extend(liked)
            counts[str(SourceKind.LIKED_TRACKS)] = len(liked)
        for playlist_id in self._config.playlists:
            items = self._read_playlist(playlist_id, warnings)
            tracks.extend(items)
            counts[f"playlist:{playlist_id}"] = len(items)

        return SourceSnapshot(
            fetched_at=self._now(),
            artists=tuple(artists),
            albums=tuple(albums),
            tracks=tuple(tracks),
            counts=counts,
            schema_ok=not warnings,
            schema_warnings=tuple(warnings),
        )

    # ---------------------------------------------------------------- transport

    def _get(
        self, url: str, params: Mapping[str, Any] | None, context: str, *, not_owned_playlist_id: str | None = None
    ) -> dict[str, Any]:
        """One authenticated GET, with a single forced token refresh on a 401."""
        return authorized_request(
            self._client,
            self._auth,
            "GET",
            url,
            params=params,
            context=context,
            sleep=self._sleep,
            not_owned_playlist_id=not_owned_playlist_id,
        )

    def _pages(
        self, url: str, params: Mapping[str, Any] | None, context: str, *, not_owned_playlist_id: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield each page, following ``next`` (an absolute URL that already carries its query)."""
        next_url: str | None = url
        first = True
        while next_url:
            page = self._get(next_url, params if first else None, context, not_owned_playlist_id=not_owned_playlist_id)
            yield page
            first = False
            nxt = page.get("next")
            next_url = str(nxt) if isinstance(nxt, str) and nxt else None

    # ---------------------------------------------------------------- followed artists

    def _read_followed_artists(self, warnings: list[str]) -> list[ArtistIntent]:
        """Cursor-paginated. Artists carry no external_ids; only a short read degrades the run."""
        context = str(SourceKind.FOLLOWED_ARTISTS)
        out: list[ArtistIntent] = []
        next_url: str | None = f"{API_BASE}/me/following"
        params: Mapping[str, Any] | None = {"type": "artist", "limit": PAGE_LIMIT}
        first = True
        total: int | None = None
        raw_items = 0

        while next_url:
            page = self._get(next_url, params, context)
            block = page.get("artists")
            if not isinstance(block, dict):
                raise SchemaError(f"spotify {context}: response has no 'artists' object")
            items = block.get("items")
            if not isinstance(items, list):
                raise SchemaError(f"spotify {context}: 'artists.items' is missing or not a list")
            if first:
                if "cursors" not in block:
                    raise SchemaError(f"spotify {context}: 'artists.cursors' is missing (cursor pagination changed?)")
                _require_id_and_name(items, context, "artists.items")
                total = _reported_total(block)
                first = False
            raw_items += len(items)
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                spotify_id = str(item.get("id") or "")
                if not spotify_id:
                    continue
                out.append(
                    ArtistIntent(
                        spotify_id=spotify_id,
                        name=str(item.get("name") or ""),
                        reason=Reason(ReasonKind.FOLLOWED, spotify_id),
                    )
                )
            nxt = block.get("next")
            next_url = str(nxt) if isinstance(nxt, str) and nxt else None
            params = None

        _check_total(context, raw_items, total, warnings)
        return out

    # ---------------------------------------------------------------- saved albums

    def _read_saved_albums(self, warnings: list[str]) -> list[AlbumIntent]:
        context = str(SourceKind.SAVED_ALBUMS)
        out: list[AlbumIntent] = []
        checked_external_ids = False
        total: int | None = None
        raw_items = 0

        for index, page in enumerate(self._pages(f"{API_BASE}/me/albums", {"limit": PAGE_LIMIT}, context)):
            items = _require_items(page, context)
            if index == 0:
                total = _reported_total(page)
            raw_items += len(items)
            albums = [i["album"] for i in items if isinstance(i, Mapping) and isinstance(i.get("album"), Mapping)]
            if len(albums) != len(items):
                raise SchemaError(f"spotify {context}: an entry in 'items' has no 'album' object")
            _require_id_and_name(albums, context, "items[].album")
            if albums and not checked_external_ids:
                checked_external_ids = True
                if not any(_has_external_ids(a, "upc") for a in albums):
                    warnings.append(f"{context}: external_ids absent (Spotify Dev Mode field removal?)")
            for album in albums:
                spotify_id = str(album.get("id") or "")
                out.append(
                    AlbumIntent(album=_album_ref(album), reason=Reason(ReasonKind.SAVED, spotify_id)),
                )
        _check_total(context, raw_items, total, warnings)
        return out

    # ---------------------------------------------------------------- liked tracks

    def _read_liked_tracks(self, warnings: list[str]) -> list[TrackIntent]:
        context = str(SourceKind.LIKED_TRACKS)
        out: list[TrackIntent] = []
        checked_external_ids = False
        total: int | None = None
        raw_items = 0

        for index, page in enumerate(self._pages(f"{API_BASE}/me/tracks", {"limit": PAGE_LIMIT}, context)):
            items = _require_items(page, context)
            if index == 0:
                total = _reported_total(page)
            # Counted before `_playable_track` drops anything: Spotify's total includes those too.
            raw_items += len(items)
            kept: list[Mapping[str, Any]] = []
            for entry in items:
                if not isinstance(entry, Mapping):
                    continue
                track = _playable_track(entry.get("track"))
                if track is None:
                    continue
                kept.append(track)
                out.append(self._track_intent(track, _parse_added_at(entry.get("added_at")), ReasonKind.LIKED, None))
            if kept:
                _require_track_shape(kept, context)
                if not checked_external_ids:
                    checked_external_ids = True
                    if not any(_has_external_ids(t, "isrc") for t in kept):
                        warnings.append(f"{context}: external_ids absent (Spotify Dev Mode field removal?)")
        _check_total(context, raw_items, total, warnings)
        return out

    # ---------------------------------------------------------------- playlists

    def _read_playlist(self, playlist_id: str, warnings: list[str]) -> list[TrackIntent]:
        context = f"playlist:{playlist_id}"
        out: list[TrackIntent] = []
        checked_external_ids = False
        total: int | None = None
        raw_items = 0

        pages = self._pages(
            f"{API_BASE}/playlists/{playlist_id}/items",
            {"limit": PAGE_LIMIT},
            context,
            not_owned_playlist_id=playlist_id,
        )
        for index, page in enumerate(pages):
            items = _require_items(page, context)
            if index == 0:
                total = _reported_total(page)
            # Counted before `_playable_track` drops local files and removed tracks, which Spotify's
            # total includes.
            raw_items += len(items)
            kept: list[Mapping[str, Any]] = []
            for entry in items:
                if not isinstance(entry, Mapping):
                    continue
                # Dev Mode renamed the nested object to 'item'; 'track' survives on some responses.
                raw = entry.get("item")
                if not isinstance(raw, Mapping):
                    raw = entry.get("track")
                track = _playable_track(raw)
                if track is None:
                    continue
                kept.append(track)
                out.append(
                    self._track_intent(track, _parse_added_at(entry.get("added_at")), ReasonKind.PLAYLIST, playlist_id)
                )
            if kept:
                _require_track_shape(kept, context)
                if not checked_external_ids:
                    checked_external_ids = True
                    if not any(_has_external_ids(t, "isrc") for t in kept):
                        warnings.append(f"{context}: external_ids absent (Spotify Dev Mode field removal?)")

        if raw_items == 0:
            self._assert_playlist_is_really_empty(playlist_id)
        _check_total(context, raw_items, total, warnings)
        return out

    def _assert_playlist_is_really_empty(self, playlist_id: str) -> None:
        """Distinguish 'empty playlist' from 'not yours, so Dev Mode hides its items'."""
        # No `fields` filter on purpose: Dev Mode renamed the nested collection once already, and
        # a `fields` value naming the wrong key would break the very check that catches that.
        # With no items returned the playlist object is small either way.
        meta = self._get(f"{API_BASE}/playlists/{playlist_id}", None, f"playlist:{playlist_id}")
        total = 0
        for key in ("tracks", "items"):
            block = meta.get(key)
            if isinstance(block, Mapping) and isinstance(block.get("total"), int):
                total = max(total, int(block["total"]))
        if total > 0:
            raise SourceError(
                _not_owned_message(
                    playlist_id,
                    f"Spotify Dev Mode returns no items (its metadata reports {total} tracks)",
                    auth=self._auth,
                )
            )

    # ---------------------------------------------------------------- mapping

    def _track_intent(
        self,
        track: Mapping[str, Any],
        added_at: datetime | None,
        kind: ReasonKind,
        playlist_id: str | None,
    ) -> TrackIntent:
        external = track.get("external_ids")
        isrc = external.get("isrc") if isinstance(external, Mapping) else None
        album = track.get("album")
        album_map: Mapping[str, Any] = album if isinstance(album, Mapping) else {}
        spotify_id = str(track.get("id") or "")
        return TrackIntent(
            spotify_id=spotify_id,
            name=str(track.get("name") or ""),
            isrc=str(isrc) if isrc else None,
            artist_names=_artist_names(track),
            album=_album_ref(album_map),
            added_at=added_at,
            reason=Reason(kind, spotify_id, playlist_id),
        )


# ---------------------------------------------------------------------------- schema canary


def _require_items(page: Mapping[str, Any], context: str) -> list[object]:
    items = page.get("items")
    if not isinstance(items, list):
        raise SchemaError(f"spotify {context}: 'items' is missing or not a list")
    if "next" not in page:
        raise SchemaError(f"spotify {context}: 'next' is missing (pagination shape changed?)")
    return items


def _reported_total(block: Mapping[str, Any]) -> int | None:
    """The ``total`` a paged response reports, or None when it carries no usable one."""
    total = block.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


def _check_total(context: str, read: int, total: int | None, warnings: list[str]) -> None:
    """Degrade the run when a source's entry count and Spotify's reported total disagree (#176).

    A warning, not a ``SchemaError``: ``schema_ok`` then goes false and the diff's ``schema`` guard
    refuses every unmonitor this run, while adds still go ahead. Raising would stop every run,
    adds included, if Dev Mode ever reported ``total`` inconsistently.
    """
    if total is None or abs(read - total) <= TOTAL_TOLERANCE:
        return
    warnings.append(f"{context}: read {read} of {total} items Spotify reported")


def _require_id_and_name(entities: Sequence[Mapping[str, Any]], context: str, where: str) -> None:
    for entity in entities:
        if not entity.get("id"):
            raise SchemaError(f"spotify {context}: an entry in {where} has no 'id'")
        if "name" not in entity:
            raise SchemaError(f"spotify {context}: an entry in {where} has no 'name'")


def _require_track_shape(tracks: Sequence[Mapping[str, Any]], context: str) -> None:
    """Canary for a page of tracks: id, name, and the album id every mapping step needs."""
    _require_id_and_name(tracks, context, "items[].track")
    for track in tracks:
        album = track.get("album")
        if not isinstance(album, Mapping):
            raise SchemaError(f"spotify {context}: a track has no 'album' object")
        if not album.get("id"):
            raise SchemaError(f"spotify {context}: a track's 'album' has no 'id'")
