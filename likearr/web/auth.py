"""Access control for `likearr start`: one shared password, and a same-origin rule under it.

The UI has a button that can unmonitor hundreds of albums, and every host on the LAN can reach
its port directly over plain http, so it authenticates for itself:

- **One password**, ``LIKEARR_UI_PASSWORD``, compared in constant time. The server refuses to
  start without it, or with one shorter than 16 characters - there is no open mode to fall back to.
- **A signed session cookie** after login: ``HttpOnly``, ``SameSite=Strict``, and ``Secure``
  whenever the request arrived over https. Its signing key is made at startup, so a restart
  logs everyone out; for a one-user admin page that is a feature, and there is no key to keep.
  ``Strict`` is never loosened for any route - Spotify's direct-callback mode (``/spotify/callback``)
  reaches this service by a cross-site GET redirect, which a ``Strict`` cookie is never
  sent on, so that one route is exempted from the login gate below (``_OPEN_PATHS``) instead and
  authorizes itself a different way: see ``web.spotify_connect``'s docstring.
- **Five failed logins from one address in a minute pause that address for a minute.** The
  address is the TCP peer, never a forwarded header. Behind the reverse proxy every request
  shares the proxy's address, so the pause then applies to everyone at once, which is the
  conservative way round.
- **No trust in forwarded headers for any access decision**, and no "local addresses need no
  password" switch: that switch, fed a spoofed ``X-Forwarded-For``, is exactly how several *arr
  tools were broken in 2026. The only forwarded header read anywhere is ``X-Forwarded-Proto``,
  and only to *add* ``Secure`` to the cookie - spoofing it can only stop the spoofer's own
  browser sending the cookie back.

Under the login sits a cross-origin rule on every unsafe method, following Go 1.25's
``CrossOriginProtection``: a browser's ``Sec-Fetch-Site`` of ``same-origin`` or ``none`` passes;
without that header, ``Origin`` must name the same host as ``Host``. Browsers send
``Sec-Fetch-Site`` only over https, so on the LAN's plain http the ``Origin`` comparison is the
check that actually runs. A request with neither header is not from a browser, so it is not a
cross-site forgery, and still needs the session cookie.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any

from likearr.config import ALLOWED_HOSTS_ENV

__all__ = [
    "MAX_BODY_BYTES",
    "AllowedHostMiddleware",
    "AuthGateMiddleware",
    "BodyLimitMiddleware",
    "CrossOriginMiddleware",
    "LoginLimiter",
    "SecureCookieMiddleware",
    "SecurityHeadersMiddleware",
    "content_security_policy",
    "cross_origin_allowed",
    "password_matches",
    "refused_host_message",
    "signed_in",
]

log = logging.getLogger(__name__)

SESSION_KEY = "authenticated"
GENERATION_KEY = "generation"
"""The session generation a cookie was issued under. Logout moves the server's generation on, so a
copied cookie stops working when its owner logs out - the cookie itself cannot be revoked."""

MAX_BODY_BYTES = 1024 * 1024
"""The largest request body accepted. The biggest real one, a settings save, is a few KB."""

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_OPEN_PATHS = frozenset({"/login", "/healthz", "/spotify/callback", "/favicon.ico"})
"""The paths that answer without a session, matched exactly; `/static/` (`_OPEN_PREFIX`) is open
too, matched as a prefix. The login form and the container healthcheck need none by design;
`/spotify/callback` is reached by a cross-site GET redirect from Spotify that never carries the
`SameSite=Strict` session cookie, so it authorizes itself with a single-use, server-side PKCE
`state` instead (see `web.spotify_connect`) and is exempted here rather than by loosening the
cookie for every route. `/favicon.ico` is what a browser asks for on its own, ignoring the
`<link rel="icon">` in the page head - without this entry a logged-out visitor's request for it
303s to `/login` instead of getting the icon. A prefix match would also open "/loginx" and any
later route that happens to start the same way."""

_OPEN_PREFIX = "/static/"
"""The stylesheet and script the login form needs. A mount, so it is matched as a prefix."""

Scope = MutableMapping[str, Any]
Receive = Callable[[], Any]
Send = Callable[[MutableMapping[str, Any]], Any]
ASGIApp = Callable[[Scope, Receive, Send], Any]


def signed_in(session: Mapping[str, Any], generation: str) -> bool:
    """Whether `session` is a logged-in one of the current `generation`."""
    return session.get(SESSION_KEY) is True and session.get(GENERATION_KEY) == generation


def password_matches(given: str, expected: str) -> bool:
    """Constant-time comparison, so response timing says nothing about how much was right."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class LoginLimiter:
    """Failed logins per address, in memory. Enough for a LAN, and it needs no store."""

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_s: float = 60.0,
        pause_s: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._pause = pause_s
        self._now = now
        self._failures: dict[str, deque[float]] = {}
        self._paused_until: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._failures.keys() | self._paused_until.keys())

    def blocked_for(self, address: str) -> float:
        """Seconds until `address` may try again; 0 when it may try now."""
        return max(0.0, self._paused_until.get(address, 0.0) - self._now())

    def failed(self, address: str) -> None:
        now = self._now()
        self._forget_idle(now)
        failures = self._failures.setdefault(address, deque())
        failures.append(now)
        while failures and failures[0] <= now - self._window:
            failures.popleft()
        if len(failures) >= self._max:
            self._paused_until[address] = now + self._pause
            failures.clear()

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)
        self._paused_until.pop(address, None)

    def _forget_idle(self, now: float) -> None:
        """Drop addresses with nothing recent, so a scan of the LAN cannot grow this for ever."""
        for address in [a for a, f in self._failures.items() if not f or f[-1] <= now - self._window]:
            del self._failures[address]
        for address in [a for a, until in self._paused_until.items() if until <= now]:
            del self._paused_until[address]


def cross_origin_allowed(method: str, headers: Mapping[str, str]) -> bool:
    """Whether an unsafe request looks same-origin. `headers` keys are lower-case."""
    if method.upper() in _SAFE_METHODS:
        return True
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in {"same-origin", "none"}
    origin = headers.get("origin")
    if origin is None:
        return True
    parsed = urllib.parse.urlsplit(origin)
    if not parsed.scheme or not parsed.netloc:
        return False  # "null", or something that is not an origin at all
    return parsed.netloc.lower() == headers.get("host", "").lower()


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _plain(send: Send, status: int, body: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = body.encode("utf-8")
    headers = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": payload})


class CrossOriginMiddleware:
    """Refuses an unsafe request that is not same-origin, before it reaches any route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not cross_origin_allowed(scope["method"], _headers(scope)):
            await _plain(send, 403, "cross-origin request refused")
            return
        await self.app(scope, receive, send)


class SecureCookieMiddleware:
    """Adds ``Secure`` to every cookie set on a request that arrived over https.

    Starlette's session middleware takes ``https_only`` once, at startup, but this app is reached
    both through the proxy over https and directly over the LAN's http. ``X-Forwarded-Proto``
    is read here and nowhere else, and it can only ever make a cookie stricter.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https" or _headers(scope).get("x-forwarded-proto", "").lower() == "https"

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if https and message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"set-cookie" and b"; secure" not in value.lower():
                        value = value + b"; Secure"
                    headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyLimitMiddleware:
    """Refuses a body over `MAX_BODY_BYTES` with 413 before any route reads it.

    Refused up front on a declared length, and otherwise counted as it arrives, so neither a big
    ``Content-Length`` nor a chunked stream gets further than the limit - including on the
    unauthenticated login form, where an unlimited form parse would buffer or spool whatever a
    stranger sends. The body is read here and replayed to the app; at this size that is nothing.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _headers(scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await _plain(send, 413, "request body too large")
            return
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return  # the client went away before sending its body
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await _plain(send, 413, "request body too large")
                return
            chunks.append(chunk)
            more = bool(message.get("more_body", False))
        pending: list[MutableMapping[str, Any]] = [
            {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        ]

        async def replay() -> Any:
            return pending.pop() if pending else await receive()

        await self.app(scope, replay, send)


class AuthGateMiddleware:
    """Everything except `_OPEN_PATHS` and `_OPEN_PREFIX` needs a logged-in session of the current
    generation.

    Sits inside the session middleware.

    A page request without one is sent to the login form. An htmx request (a poll that outlived
    its session, say) gets a 401 carrying ``HX-Redirect``, so the whole page moves to the login
    form instead of the form being swapped into a fragment. Anything else is a bare 401.
    """

    def __init__(self, app: ASGIApp, generation: Callable[[], str]) -> None:
        self.app = app
        self.generation = generation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # No route takes a websocket; refuse one here rather than trust that stays true.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http" or scope["path"] in _OPEN_PATHS or scope["path"].startswith(_OPEN_PREFIX):
            await self.app(scope, receive, send)
            return
        if signed_in(scope.get("session") or {}, self.generation()):
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        if headers.get("hx-request") == "true":
            await _plain(send, 401, "log in again", [(b"hx-redirect", b"/login")])
        elif scope["method"] in _SAFE_METHODS:
            await send({"type": "http.response.start", "status": 303, "headers": [(b"location", b"/login")]})
            await send({"type": "http.response.body", "body": b""})
        else:
            await _plain(send, 401, "log in first")


_MAX_HOST_CHARS = 253
"""RFC 1035's limit on a domain name's length - also where a refused Host header is truncated
before it goes into a refusal body or a log line, so neither grows without bound on an arbitrary
header nobody has checked yet."""

_GENERIC_REFUSAL = (
    "likearr does not answer to this address. If you reached likearr at this address on purpose, "
    f"add it to {ALLOWED_HOSTS_ENV} in likearr's environment and restart likearr. This check stops "
    "other web pages from reaching likearr through your browser."
)

_IPV6_REFUSAL = (
    f"likearr does not answer to this address. {ALLOWED_HOSTS_ENV} cannot hold an IPv6 address - "
    "browse to likearr by its host name or an IPv4 address instead, or add that name to "
    f"{ALLOWED_HOSTS_ENV} in likearr's environment and restart likearr. This check stops other web "
    "pages from reaching likearr through your browser."
)


def _is_ipv4(host: str) -> bool:
    """A dotted-quad IPv4 literal, strictly: four decimal parts, no leading zeros, no name that
    merely looks numeric (`ipaddress` refuses ``1.2.3``, ``0x7f.1`` and ``01.2.3.4``)."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def _sanitized(value: str) -> str:
    """`value` kept to printable ASCII and truncated to `_MAX_HOST_CHARS` - safe to put in a
    `text/plain` body or a log line built from a header nobody has checked yet."""
    return "".join(c for c in value if " " <= c <= "~")[:_MAX_HOST_CHARS]


def refused_host_message(host_header: str) -> str:
    """The message for a Host header outside `ALLOWED_HOSTS_ENV`: names the refused
    host, sanitised (see `_sanitized`), and the setting that would admit it - conditional wording,
    because a hostile rebinding page might be the one reading this, not the person who typed the
    address on purpose, so it never reads as an order to add the sender's own name.

    A missing Host header, or one that is empty once its port is stripped, gets the generic line:
    there is nothing safe to name. A bracketed IPv6 literal (``[fd00::1]``) gets a different line
    and is never echoed into an "add ... to allowed_hosts" instruction, because `config.py` already
    refuses an IPv6 literal there - suggesting one would send a user in a circle.
    """
    sanitized = _sanitized(host_header)
    if sanitized.startswith("["):
        return _IPV6_REFUSAL
    host = sanitized.split(":", 1)[0]
    if not host:
        return _GENERIC_REFUSAL
    return (
        f'likearr does not answer to "{host}". If you reached likearr at this address on purpose, '
        f'add "{host}" to {ALLOWED_HOSTS_ENV} in likearr\'s environment and restart likearr. This check '
        "stops other web pages from reaching likearr through your browser."
    )


class AllowedHostMiddleware:
    """Refuses a request whose Host header names a host outside `allowed_hosts`, 400 `text/plain`.

    Replaces Starlette's `TrustedHostMiddleware`. Same strict, exact-match rule on the
    part before the first ":" of the Host header (see `app.LOOPBACK_HOSTS`'s docstring for why
    that beats comparing the whole header), and no www redirect - but the body now names the host
    it refused and the setting that would admit it, instead of a bare "Invalid host header" that
    points nowhere. Written as its own middleware rather than a `TrustedHostMiddleware` subclass:
    that class builds its response inline, with no hook to change the body.

    Every refused host is logged once at WARNING, with the same message, up to `_MAX_LOGGED_HOSTS`
    distinct hosts - past that bound likearr keeps refusing but stops adding to the log, so a LAN
    client cycling Host values cannot grow this set, or the log, without bound.

    Applies to `http` requests only, like the rest of this module's middleware. Nothing routes a
    websocket - `AuthGateMiddleware` closes every one unconditionally, regardless of Host - so the
    match this class would need for one is moot.

    **With `any_ipv4` (`ALLOWED_HOSTS_ENV` unset), any IPv4 literal is accepted as well**,
    so a Compose install reached at its LAN address answers without naming that address first.
    Host names are still refused. Security rationale: the hosts check exists to stop DNS
    rebinding, where a hostile page re-points its own domain at this server and the browser then
    sends that domain as the Host header. A rebinding page can only ever send a name it controls,
    never a bare IP address - the browser's origin is the attacker's domain, and the Host header
    follows the origin - so accepting IP literals does not open the rebinding path. What a bare IP
    does allow, a person on the LAN typing the address, the network already allowed (the port is
    published), and the login password still gates every page. "Trust the first host seen" was
    rejected, because a rebinding page could win that race. IPv6 literals stay refused, as they
    always have: the match compares only what precedes the first ":" of the header.
    """

    _MAX_LOGGED_HOSTS = 20

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str], *, any_ipv4: bool = False) -> None:
        self.app = app
        self.allowed_hosts = frozenset(allowed_hosts)
        self.any_ipv4 = any_ipv4
        self._logged: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The first Host header, as Starlette's own check read it: `_headers` keeps the last, and the
        # app builds its URLs from the first, so judging any other would check a name it never uses.
        raw_host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"host"), "")
        matched = raw_host.split(":", 1)[0]
        if matched in self.allowed_hosts or (self.any_ipv4 and _is_ipv4(matched)):
            await self.app(scope, receive, send)
            return
        message = refused_host_message(raw_host)
        if matched not in self._logged and len(self._logged) < self._MAX_LOGGED_HOSTS:
            self._logged.add(matched)
            log.warning(message)
        await _plain(send, 400, message)


def content_security_policy(*, form_action: Sequence[str] = ()) -> str:
    """The policy every response carries, with `form_action`'s sources added after ``'self'``.

    Only one-click Connect Spotify passes any: see `web.spotify_connect.one_click_form_action`
    for which pages, and which two origins.
    """
    sources = " ".join(("'self'", *form_action))
    return f"default-src 'self'; frame-ancestors 'none'; form-action {sources}; base-uri 'none'"


class SecurityHeadersMiddleware:
    """A strict content security policy and the usual framing and sniffing headers on every response.

    Everything the pages load is served from this origin - htmx is vendored, there is no CDN -
    so ``'self'`` is the whole policy, and nothing inline is ever allowed to run.

    A response that already carries a ``Content-Security-Policy`` keeps it: the pages with one-click
    Connect Spotify set `content_security_policy` with a wider `form-action`, and nothing else.
    The other headers are added regardless.
    """

    _CSP = (b"content-security-policy", content_security_policy().encode("ascii"))
    _HEADERS = [
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == self._CSP[0] for name, _ in headers):
                    headers.append(self._CSP)
                message["headers"] = headers + self._HEADERS
            await send(message)

        await self.app(scope, receive, send_wrapper)
