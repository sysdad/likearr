"""Connect / re-authorize Spotify from the web UI (issue #79).

Reuses `adapters.spotify.SpotifyAuth` end to end - `build_authorize_url` and `exchange_code`, the
same calls `likearr auth --manual` makes - so there is exactly one PKCE implementation, in
`adapters.spotify`. This module holds only what is specific to running that flow from a browser:
the server-side PKCE state store, and the two small calls that build a throwaway `SpotifyAuth` to
drive it.

**Why the exchange runs in the server process, not a child job** (unlike every other Spotify or
Lidarr write, which is a `likearr` child process through `JobRunner` - see `web.jobs`'s
docstring): the exchange needs no Lidarr client, no MusicBrainz client and no state database, only
`SpotifyConfig` and one `httpx.Client`, so building the full `Context` a job's `likearr` process
would construct is unneeded weight. More importantly, a child job would need the PKCE code
verifier as a command-line argument, visible in `ps` to anyone on the host with a shell for as
long as the argument list survives - the opposite of "never render or log the verifier, code or
tokens". Kept in-process, called only from a route already behind the login and cross-origin
gates, and off the event loop via `anyio.to_thread` so the exchange's one blocking HTTP round trip
never blocks the server, it avoids that without a second OAuth implementation. The token file is
still written under the same ``spotify-token.json.lock`` `SpotifyAuth` always takes, so a
scheduled run's own token refresh cannot race it.

**Where `state`/`verifier` live, and what actually authorizes finishing the flow**: server-side, in
`PendingSpotifyAuthStore` - an in-memory dict on the running server, never in the session cookie
and never in the database. `state` alone is the capability: it is minted (`secrets.token_urlsafe`,
via `adapters.spotify.SpotifyAuth.build_authorize_url`, ~128 bits) only from
`spotify_connect_start`, a route behind the login gate, so only a session that was logged in when
the flow started can ever hold a valid one - there is no separate session binding to check on top
of it, and none is needed. `PendingSpotifyAuthStore.consume` compares the presented `state` against
each stored one with `hmac.compare_digest` rather than a dict-key equality check, so a wrong guess
costs no more information than "no match" a bit at a time. Single use (`consume` removes the entry
the moment it is checked, matched or not) and a ten-minute expiry, per issue #79's Requirements.

**Which scopes an attempt asks for** (#161) is decided in `spotify_connect_start`, behind the login
gate: read-only, unless the user opted into promote-save's write scopes or the token being replaced
already has them (`adapters.spotify.asks_for_write_scopes`). The choice is baked into the authorize
URL and recorded in the pending attempt (`PendingAuth.include_write`) beside the verifier, so the
callback reads it from there and never from its own query string: nothing a crafted callback
carries can change what was asked for. Spotify grants scopes at the consent screen, so the scopes
the token ends up with are whatever the user approved there; `include_write` only decides what
likearr says about them afterwards.

This is also why the direct-callback route (`GET /spotify/callback`, reached by a cross-site
top-level GET redirect from Spotify) can be exempted from the login gate (`auth._OPEN_PATHS`)
instead of loosening the session cookie's `SameSite=Strict` for every route: the redirect never
carries the session cookie either way, and does not need to - `state` is enough on its own.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from likearr.adapters.http import build_client
from likearr.adapters.spotify import ACCOUNTS_AUTHORIZE_URL, SpotifyAuth, TokenSet
from likearr.config import SpotifyConfig

__all__ = [
    "PENDING_AUTH_TTL_S",
    "PendingAuth",
    "PendingSpotifyAuthStore",
    "build_authorize",
    "exchange",
    "one_click_form_action",
]

SPOTIFY_ACCOUNTS_ORIGIN = "https://" + urllib.parse.urlsplit(ACCOUNTS_AUTHORIZE_URL).netloc
"""Where the direct-callback flow's form POST is redirected to (#11)."""

_HOST_NAME = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
"""A plain DNS host name, lower-cased. A `public_url` host of any other shape never goes into a CSP
header: it keeps the two-click link instead."""

PENDING_AUTH_TTL_S = 600.0
"""Ten minutes (issue #79's Requirements): a state token older than this is refused as expired."""


@dataclass(frozen=True, slots=True)
class PendingAuth:
    state: str
    verifier: str
    redirect_uri: str
    mode: str
    """``"paste"`` (the default, everywhere) or ``"callback"`` (issue #79's direct-https mode,
    only offered when `[ui] public_url` is configured)."""
    created_at: float
    include_write: bool = False
    """Whether the authorize URL asked for promote-save's write scopes (#161). Server-side only."""


class PendingSpotifyAuthStore:
    """Server-side, in-memory store of in-flight PKCE attempts, keyed by `state`.

    One instance per server process, held by `_Web` - like `JobRunner`, never persisted, so a
    restart quietly drops every in-flight attempt (the same "start again" a ten-minute expiry
    would give). Thread-safe: the event loop thread and `anyio.to_thread` workers can both touch
    it.
    """

    def __init__(self, *, now: Callable[[], float] = time.time, ttl_s: float = PENDING_AUTH_TTL_S) -> None:
        self._now = now
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._pending: dict[str, PendingAuth] = {}

    def start(self, *, state: str, verifier: str, redirect_uri: str, mode: str, include_write: bool = False) -> None:
        with self._lock:
            self._sweep()
            self._pending[state] = PendingAuth(
                state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                mode=mode,
                created_at=self._now(),
                include_write=include_write,
            )

    def consume(self, state: str) -> PendingAuth | None:
        """The pending attempt for `state`, removed so it can never be replayed - or `None` for
        one that is missing, expired, or already used.

        `state` is the sole authorization for finishing the flow (see this module's docstring), so
        it is matched with `hmac.compare_digest` against each stored key rather than a plain dict
        lookup's hash-then-`==`, the same reasoning `auth.password_matches` uses for the UI
        password: nothing about how much of a guess was right should be observable from timing.
        """
        with self._lock:
            self._sweep()
            match = next((key for key in self._pending if hmac.compare_digest(key, state)), None)
            return self._pending.pop(match) if match is not None else None

    def __len__(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._pending)

    def _sweep(self) -> None:
        cutoff = self._now() - self._ttl
        expired = [key for key, pending in self._pending.items() if pending.created_at < cutoff]
        for key in expired:
            del self._pending[key]


def build_authorize(
    config: SpotifyConfig, *, redirect_uri: str | None, include_write: bool = False
) -> tuple[str, str, str]:
    """A throwaway `SpotifyAuth` for `SpotifyAuth.build_authorize_url` - no network call, so this
    is cheap enough to call straight from a route without `anyio.to_thread`, but callers do anyway
    for symmetry with `exchange` and because a future PKCE step here should not have to remember.
    `include_write` adds promote-save's write scopes (#161); the caller records it with the
    attempt (`PendingSpotifyAuthStore.start`)."""
    with build_client() as client:
        return SpotifyAuth(config, client).build_authorize_url(redirect_uri=redirect_uri, include_write=include_write)


def exchange(config: SpotifyConfig, code: str, verifier: str, redirect_uri: str) -> TokenSet:
    """A throwaway `SpotifyAuth` for `SpotifyAuth.exchange_code`: one POST to Spotify's token
    endpoint, and the same atomic, 0600, lock-held token write `likearr auth` always does. Call
    this from a worker thread (`anyio.to_thread`), never the event loop itself."""
    with build_client() as client:
        return SpotifyAuth(config, client).exchange_code(code, verifier, redirect_uri=redirect_uri)


def _host_and_port(host: str, port: int | None) -> tuple[str, int | None]:
    return host.lower(), None if port == 443 else port


def one_click_form_action(host_header: str, public_url: str) -> tuple[str, ...]:
    """The extra `form-action` sources a page or a Connect POST needs for one-click Connect
    Spotify (#11), or ``()`` when it keeps the two-click "Continue to Spotify" link.

    One click needs direct-callback mode and a request that reached likearr at `public_url`'s own
    origin. Chromium and WebKit check `form-action` on every hop of a form submission's redirect
    chain, against the page that submitted it: this POST, then Spotify's authorize page (and its
    login page), then - for a user already signed in who approved the app before - straight back
    to ``<public_url>/spotify/callback``. From any other address that last hop is another origin,
    so there the link stays.

    "The request's origin" is the ``Host`` header, as the browser sent it and as
    `auth.AllowedHostMiddleware` already admitted it (by name; the port here is compared too). The
    scheme is not compared: behind the TLS-terminating reverse proxy that `public_url` implies,
    likearr sees plain http, and `server.serve` trusts no forwarded header, so the scheme it sees
    says nothing about the browser's. A missing port stands for 443, `public_url`'s own default;
    another port - likearr's own, reached across the LAN - is another origin. A wrong answer here
    either way costs only which of the two flows is shown: the Host header is the browser's own,
    so spoofing it changes only the spoofer's response.
    """
    if not public_url:
        return ()
    try:
        parts = urllib.parse.urlsplit(public_url)
        want_port = parts.port
        asked = urllib.parse.urlsplit(f"//{host_header}")
        asked_port = asked.port
    except ValueError:
        return ()
    name = (parts.hostname or "").lower()
    if not _HOST_NAME.fullmatch(name) or parts.netloc.lower() not in {name, f"{name}:{want_port}"}:
        return ()
    if not asked.hostname or _host_and_port(asked.hostname, asked_port) != _host_and_port(name, want_port):
        return ()
    origin = f"https://{name}" if _host_and_port(name, want_port)[1] is None else f"https://{name}:{want_port}"
    return (SPOTIFY_ACCOUNTS_ORIGIN, origin)
