"""Shared HTTP plumbing for every adapter.

Two hard rules live here, and every adapter inherits them:

1. **Nothing leaks.** No header value, query string, token or API key is ever logged,
   printed or put into an exception message. Every string that could reach a log or an
   exception passes through :func:`redact` first, and URLs are stripped of their query
   string by :func:`safe_url` before they are shown at all.
2. **Transient failures are retried.** 429 and 5xx responses, plus transport errors, are
   retried with exponential backoff that honours ``Retry-After``. Everything else fails
   fast with an :class:`HttpError` carrying the status code and a short redacted body
   excerpt.

The module deliberately has no knowledge of Spotify, MusicBrainz or Lidarr.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Container, Iterable, Mapping
from typing import Any

import httpx

from likearr import __version__

__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "HttpError",
    "RedirectRefused",
    "build_client",
    "default_retry_on",
    "redact",
    "redact_literals",
    "request_with_retries",
    "safe_url",
    "sent_secrets",
]

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = f"likearr/{__version__} (+https://github.com/sysdad/likearr)"
"""Sent on every request unless an adapter overrides it (MusicBrainz policy needs a contact)."""

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

REDACTED = "REDACTED"
_BODY_EXCERPT_CHARS = 200

# ---------------------------------------------------------------------------- redaction

# Order matters: the Authorization header is swallowed whole first, so that a later, narrower
# pattern cannot stop at the space in "Bearer <token>" and leave the token behind.
_AUTH_HEADER_RE = re.compile(r"""(?i)\b(authorization|proxy-authorization)(["']?\s*[:=]\s*)[^\r\n,;}]*""")

_SECRET_KEY_RE = re.compile(
    r"""(?ix)
    \b(
        x-api-key | api[-_]?key | access[-_]token | refresh[-_]token
      | client[-_]secret | code[-_]verifier | code[-_]challenge | id[-_]token | token
    )\b
    (["']?\s*[:=]\s*)
    ("[^"]*" | '[^']*' | [^\s&,;}\)\]"']+)
    """
)

_CODE_PARAM_RE = re.compile(r"""(?i)(^|[?&])(code=)[^&\s"']*""")
_CODE_JSON_RE = re.compile(r"""(?i)(["']code["']\s*:\s*)("[^"]*"|'[^']*')""")
_SCHEME_TOKEN_RE = re.compile(r"""(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+""")


_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "apikey"})
_SECRET_FIELDS = frozenset(
    {
        "apikey",
        "api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "code",
        "code_verifier",
        "id_token",
        "token",
    }
)
_MIN_LITERAL_LEN = 8
"""Never blanket-replace a short value - it would corrupt unrelated text without adding safety."""


def redact(text: str, *, literals: Iterable[str] = ()) -> str:
    """Strip anything that could be a credential out of an arbitrary string.

    Two layers, because either alone is insufficient:

    - **Literal**: values this process actually sent (``literals``) are replaced wherever they
      appear, including inside an upstream error that echoes the key back in prose. Patterns
      cannot catch that, so this layer is what makes a leak impossible by construction.
    - **Pattern**: labelled secrets in header form (``X-Api-Key: abc``), query form
      (``apikey=abc``, ``code=abc``) and JSON form (``"access_token": "abc"``), which catches
      credentials this process never saw.

    Non-secret context is left intact so errors stay diagnosable.
    """
    out = redact_literals(text, literals)
    out = _AUTH_HEADER_RE.sub(rf"\1\2{REDACTED}", out)
    out = _SCHEME_TOKEN_RE.sub(rf"\1 {REDACTED}", out)
    out = _SECRET_KEY_RE.sub(rf"\1\2{REDACTED}", out)
    out = _CODE_PARAM_RE.sub(rf"\1\g<2>{REDACTED}", out)
    return _CODE_JSON_RE.sub(rf"\1{REDACTED}", out)


def redact_literals(text: str, literals: Iterable[str]) -> str:
    """Only the literal layer of `redact`: replace exactly these values and nothing else.

    For text that is data rather than a diagnostic - an answer shown to a person - where the
    pattern layer's false positives ("Basic Channel") would corrupt what they came to read.

    Each literal's repr-escaped form is replaced too (issue #7): an error that quotes a value's
    repr, as h11's "Illegal header value" does, spells a CR or LF as a backslash and a letter.
    """
    out = text
    for literal in literals:
        if literal and len(literal) >= _MIN_LITERAL_LEN:
            escaped = repr(literal)[1:-1]
            if escaped != literal:
                out = out.replace(escaped, REDACTED)
            out = out.replace(literal, REDACTED)
    return out


def sent_secrets(*sources: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Collect the secret values this process is about to send, so they can be redacted literally.

    Looks at header names (``Authorization``, ``X-Api-Key``, ...) and form/query field names
    (``code``, ``access_token``, ...). A ``Bearer``/``Basic`` prefix is stripped so the bare
    token is matched too.
    """
    found: list[str] = []
    for source in sources:
        if not source:
            continue
        for key, value in source.items():
            name = str(key).strip().lower()
            if name not in _SECRET_HEADERS and name not in _SECRET_FIELDS:
                continue
            text = str(value)
            found.append(text)
            parts = text.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in ("bearer", "basic"):
                found.append(parts[1])
    return tuple(found)


def safe_url(url: str | httpx.URL, *, literals: Iterable[str] = ()) -> str:
    """Scheme, host and path only - the query string never survives this function."""
    try:
        u = httpx.URL(str(url))
    except (httpx.InvalidURL, ValueError):
        return redact(str(url), literals=literals)
    host = u.netloc.decode("ascii", "replace") if isinstance(u.netloc, bytes) else str(u.netloc)
    if not u.scheme:
        return redact(str(url), literals=literals)
    return redact(f"{u.scheme}://{host}{u.path}", literals=literals)


def _body_excerpt(response: httpx.Response, literals: Iterable[str]) -> str:
    try:
        raw = response.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - defensive
        return ""
    collapsed = " ".join(raw.split())[:_BODY_EXCERPT_CHARS]
    return redact(collapsed, literals=literals)


# ---------------------------------------------------------------------------- errors


class HttpError(Exception):
    """A request that could not be completed. Message, URL and body excerpt are pre-redacted.

    Adapters translate this into their own port-level error (``SourceError``,
    ``MetadataError``, ``LidarrError``) rather than letting it escape.
    """

    __slots__ = ("body_excerpt", "retry_after", "status_code", "url")

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | httpx.URL = "",
        body_excerpt: str = "",
        literals: Iterable[str] = (),
        retry_after: float | None = None,
    ) -> None:
        literals = tuple(literals)
        self.status_code = status_code
        self.retry_after = retry_after  # the final response's Retry-After in seconds, if parseable
        self.url = safe_url(url, literals=literals) if url else ""
        self.body_excerpt = redact(body_excerpt, literals=literals)
        parts = [redact(message, literals=literals)]
        if status_code is not None:
            parts.append(f"HTTP {status_code}")
        if self.url:
            parts.append(self.url)
        if self.body_excerpt:
            parts.append(f"body: {self.body_excerpt}")
        super().__init__(" - ".join(parts))

    @property
    def is_server_side(self) -> bool:
        """True for a transport failure or a 5xx - i.e. 'their fault, try later'."""
        return self.status_code is None or self.status_code >= 500


class RedirectRefused(HttpError):
    """A request to another origin, refused by a client built with ``pinned_origin``.

    In practice a redirect: the client's own calls all go to the pinned origin, so only a redirect
    hop can lead anywhere else. ``origin`` is that request's scheme, host and port only. Its path
    and query string are never kept: a login page's redirect often carries a token there.
    """

    __slots__ = ("origin",)

    def __init__(self, message: str, *, origin: str, url: str | httpx.URL) -> None:
        self.origin = origin
        super().__init__(message, url=url)

    @property
    def is_server_side(self) -> bool:
        """False: a misconfiguration to fix, not an outage to wait out (it has no status code,
        which the base class would read as a transport failure)."""
        return False


def _port_or_default(url: httpx.URL) -> int | None:
    return url.port if url.port is not None else {"http": 80, "https": 443}.get(url.scheme)


def _origin(url: httpx.URL) -> str:
    """``scheme://host[:port]`` - no user info, path, query or fragment."""
    host = f"[{url.host}]" if ":" in url.host else url.host
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{host}{port}"


def _origin_allowed(pinned: httpx.URL, url: httpx.URL) -> bool:
    """Same scheme, host and port as ``pinned``, or the same host upgraded from http to https on
    the default ports. The same two cases in which httpx keeps an ``Authorization`` header on a
    redirect."""
    if pinned.host != url.host:
        return False
    if pinned.scheme == url.scheme and _port_or_default(pinned) == _port_or_default(url):
        return True
    return (
        pinned.scheme == "http"
        and _port_or_default(pinned) == 80
        and url.scheme == "https"
        and _port_or_default(url) == 443
    )


def _pin_origin(pinned_url: str) -> Callable[[httpx.Request], None]:
    """A request hook that refuses any request off ``pinned_url``'s origin.

    httpx runs request hooks before every hop of a redirect chain, on the URL it is about to send
    to, so this checks where a request actually goes rather than re-deriving a ``Location``.
    httpx strips only ``Authorization`` on a cross-origin redirect; a key in any other header
    (Lidarr's ``X-Api-Key``) would otherwise go wherever the ``Location`` points.

    ``pinned_url`` is parsed on the first request, not when the client is built, so a malformed
    ``LIKEARR_LIDARR_URL`` still fails only the commands that call Lidarr, as it did before.
    """

    def check(request: httpx.Request) -> None:
        pinned = httpx.URL(pinned_url)
        if _origin_allowed(pinned, request.url):
            return
        origin = _origin(request.url)
        raise RedirectRefused(
            f"{request.method} to {origin} refused: not {_origin(pinned)}", origin=origin, url=_origin(pinned)
        )

    return check


# ---------------------------------------------------------------------------- client


def build_client(
    *,
    base_url: str = "",
    headers: Mapping[str, str] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: httpx.Timeout | None = None,
    transport: httpx.BaseTransport | None = None,
    follow_redirects: bool = True,
    pinned_origin: str | None = None,
) -> httpx.Client:
    """Build the one kind of ``httpx.Client`` this project uses.

    Connect timeout 10s, read timeout 60s, an explicit User-Agent, and no automatic
    ``raise_for_status`` - :func:`request_with_retries` owns error handling.

    Args:
        pinned_origin: a URL whose origin (scheme, host, port) is the only one this client may
            send to, plus a same-host upgrade from http:80 to https:443. Any other request, a
            redirect hop included, raises `RedirectRefused` before it is sent. For a client whose
            credential travels in a header httpx does not strip on a redirect.
    """
    merged: dict[str, str] = {"User-Agent": user_agent, "Accept": "application/json"}
    if headers:
        merged.update(headers)
    return httpx.Client(
        base_url=base_url,
        headers=merged,
        timeout=timeout or DEFAULT_TIMEOUT,
        transport=transport,
        follow_redirects=follow_redirects,
        event_hooks={"request": [_pin_origin(pinned_origin)]} if pinned_origin is not None else None,
    )


# ---------------------------------------------------------------------------- retries


def default_retry_on(response: httpx.Response) -> bool:
    """Retry 429 and 5xx. Adapters override this to veto a retry (e.g. a hard quota error)."""
    return response.status_code == 429 or 500 <= response.status_code < 600


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = when.timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):  # a nonsense offset overflows a C int
        return None
    return max(0.0, delta)


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data: Any = None,
    headers: Mapping[str, str] | None = None,
    max_attempts: int = 4,
    retry_on: Callable[[httpx.Response], bool] = default_retry_on,
    allow_status: Container[int] = (),
    sleep: Callable[[float], None] = time.sleep,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
    before_attempt: Callable[[], None] | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    """Perform one request, retrying transient failures with backoff.

    Args:
        allow_status: statuses returned to the caller instead of raising (e.g. 404 as
            "not found", 401 as "refresh the token and try again").
        retry_on: decides whether a non-2xx response is worth another attempt.
        sleep: injected so tests never actually wait.
        before_attempt: called before EVERY attempt, retries included (e.g. a rate limiter's
            `acquire`), so a retry can never jump a per-host request budget.
        timeout: overrides the client's timeout for this request's attempts, for a caller that
            needs a known worst case (the Spotify token request).

    `Retry-After` is a floor, never a ceiling: a server saying "0" (MusicBrainz does, on 503)
    still gets likearr's exponential backoff, so a throttled API is never hammered. But a
    `Retry-After` longer than `max_backoff` is never shortened to fit either: the request raises
    `HttpError` right away instead of sleeping the capped amount and retrying early, so likearr
    never retries inside a window the server asked it to stay away from.

    Returns:
        The successful (or explicitly allowed) response.

    Raises:
        HttpError: transport failure, or a final non-2xx response. The message carries the
            status code and a short redacted body excerpt; never a header or query string.
    """
    literals = sent_secrets(
        headers, params if isinstance(params, Mapping) else None, data if isinstance(data, Mapping) else None
    )
    shown = safe_url(client.base_url.join(url) if client.base_url else url, literals=literals)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        backoff = min(max_backoff, base_backoff * (2 ** (attempt - 1)))
        if before_attempt is not None:
            before_attempt()
        try:
            response = client.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise HttpError(
                    f"{method} {shown} failed: {type(exc).__name__}: {exc}", url=shown, literals=literals
                ) from exc
            log.debug("%s %s: transport error, retrying in %.1fs", method, shown, backoff)
            sleep(backoff)
            continue

        if response.is_success or response.status_code in allow_status:
            return response

        if attempt < max_attempts and retry_on(response):
            delay = _retry_after_seconds(response)
            if delay is not None and delay > max_backoff:
                # The server asked for a longer pause than max_backoff allows. Retrying
                # anyway, capped to max_backoff, would mean retrying inside that window - so stop
                # instead of shortening the wait to fit.
                raise HttpError(
                    f"{method} {shown} failed",
                    status_code=response.status_code,
                    url=shown,
                    body_excerpt=_body_excerpt(response, literals),
                    literals=literals,
                    retry_after=delay,
                )
            wait = backoff if delay is None else max(backoff, delay)
            log.debug("%s %s: HTTP %s, retrying in %.1fs", method, shown, response.status_code, wait)
            sleep(wait)
            continue

        raise HttpError(
            f"{method} {shown} failed",
            status_code=response.status_code,
            url=shown,
            body_excerpt=_body_excerpt(response, literals),
            literals=literals,
            retry_after=_retry_after_seconds(response),
        )

    raise HttpError(
        f"{method} {shown} failed after {max_attempts} attempts", url=shown, literals=literals
    ) from last_exc
