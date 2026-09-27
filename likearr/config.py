"""Configuration: one TOML file for everything deployment-specific, env vars for secrets.

Nothing personal or site-specific lives in code. Secrets are never read from the TOML file.

Env vars (secrets only):
  LIKEARR_LIDARR_API_KEY        Lidarr API key (required)
  LIKEARR_SPOTIFY_CLIENT_ID     Spotify app client id (required for Spotify)
  LIKEARR_SPOTIFY_CLIENT_SECRET optional; PKCE flow needs none
  LIKEARR_MQTT_USERNAME / LIKEARR_MQTT_PASSWORD   optional, for the MQTT health sink
  LIKEARR_UI_PASSWORD           required by `likearr start` (the web UI) only, 16+ characters
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
import sys
import tomllib
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from likearr.core.cron import CronError, min_interval_minutes, parse_cron
from likearr.models import LIKED_TRACK_SCOPE_ALBUM, LIKED_TRACK_SCOPES, ExclusionRules

log = logging.getLogger(__name__)

UI_PASSWORD_ENV = "LIKEARR_UI_PASSWORD"
"""The web UI's password. Defined here, not in `likearr.web`, so the CLI - every cron run - can name
it without importing the web package: `start` reads it and takes it out of its own environment,
and the job runner strips it from every child's."""

UI_PASSWORD_MIN_LENGTH = 16
"""`start` refuses a shorter `UI_PASSWORD_ENV`, counted as given, not stripped (issue #170). The
login pause is per address, so a short or dictionary password is the weak point it cannot cover."""


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class LidarrConfig:
    url: str
    root_folder: str
    quality_profile: str
    lean_profile: str = "Lean"
    full_profile: str = "Full"
    tag: str = "likearr"
    refresh_timeout_s: float = 300.0
    """The floor: how long a RefreshArtist is waited for however small the catalogue."""
    refresh_per_album_s: float = 2.0
    """Extra wait per release group in the artist's catalogue, on top of the floor."""
    refresh_timeout_max_s: float = 3600.0
    """The ceiling, so one enormous catalogue cannot hold a run for hours. Never below the floor."""
    max_refreshes_per_run: int = 10
    """How many followed artists a run may refresh to chase a recent release (issue #8).

    Each refresh is a Lidarr command likearr waits on, so an uncapped run could spend hours on a
    week when a lot of artists released at once. The artists that miss the cap are simply picked
    up by the next run - the gap is still reported either way.
    """
    recent_gap_refresh_hours: float = 24.0
    """How long to leave an artist alone after refreshing them for a recent catalogue gap.

    Without it, an artist with a recent gap is refreshed on *every* run - four a day on a
    six-hourly schedule - for as long as the gap stays inside the recency window. MusicBrainz
    regularly dates promo and non-Official Album/EP release groups that Lidarr's metadata will
    never carry, so that is up to hundreds of pointless commands per artist against a metadata
    proxy that is already a separate failure domain. An artist inside the backoff is still reported
    as a recent gap; it is only the asking that is rate-limited.
    """

    def refresh_timeout_for(self, albums: int) -> float:
        """How long to wait for RefreshArtist on an artist with `albums` release groups.

        Lidarr's refresh time grows with the catalogue: a flat 300 s was blown by Jean Sibelius,
        Bing Crosby, Springsteen and Johnny Cash. An unknown size (0) gets the floor.
        """
        wanted = self.refresh_timeout_s + self.refresh_per_album_s * max(albums, 0)
        return min(wanted, max(self.refresh_timeout_max_s, self.refresh_timeout_s))

    @property
    def api_key(self) -> str:
        # Stripped (issue #7): a CR or LF left by a Windows-line-ending env file or a file-based
        # Kubernetes Secret makes h11 refuse the header, quoting the whole key in its error.
        key = os.environ.get("LIKEARR_LIDARR_API_KEY", "").strip()
        if not key:
            raise ConfigError("LIKEARR_LIDARR_API_KEY is not set")
        return key


@dataclass(frozen=True, slots=True)
class SpotifyConfig:
    token_file: Path
    playlists: tuple[str, ...] = ()
    """Playlist IDs the user OWNS or collaborates on (the latter once the token has
    ``playlist-read-collaborative``, #103). Any other playlist returns no items under Spotify Dev Mode."""
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    followed_artists: bool = True
    saved_albums: bool = True
    liked_tracks: bool = True

    @property
    def client_id(self) -> str:
        cid = os.environ.get("LIKEARR_SPOTIFY_CLIENT_ID", "").strip()  # stripped, as `api_key` (#7)
        if not cid:
            raise ConfigError("LIKEARR_SPOTIFY_CLIENT_ID is not set")
        return cid

    @property
    def client_secret(self) -> str | None:
        return os.environ.get("LIKEARR_SPOTIFY_CLIENT_SECRET", "").strip() or None


@dataclass(frozen=True, slots=True)
class MusicBrainzConfig:
    contact: str
    """Required by MusicBrainz's User-Agent policy: an email or project URL."""
    base_url: str = "https://musicbrainz.org/ws/2"
    min_interval_s: float = 1.0
    negative_cache_days: int = 7
    positive_cache_days: int = 90
    """How long a *successful* MusicBrainz answer is trusted before it is looked up again.

    Positive answers used to live for ever, so a corrected Spotify link or an artist merge was
    never seen and - after a merge - that artist's releases quietly stopped being monitored
    (issue #9). Each entry's real expiry is jittered deterministically by up to 25% of this, so
    the cache does not fall due all at once and spend a run at 1 request/second catching up.
    A refetch that fails keeps serving the cached answer, so this can never cost a mapping.

    It also sets how long a resolved song or saved-album answer is reused before it is worked out
    again: 4/3 of this, jittered by intent key the same way (issue #165,
    `shell.plan.resolution_max_age`). Zero re-resolves every answer on every run.
    """


@dataclass(frozen=True, slots=True)
class RulesConfig:
    singles_fallback_days: int = 180
    """A liked single with no album after this many days is monitored itself."""
    albums_only_tag: str = "albums-only"
    """Lidarr tag: followed artist gets studio Albums only (no EPs)."""
    liked_track_scope: str = LIKED_TRACK_SCOPE_ALBUM
    """Which release a liked/playlist track resolves to: 'album' (the Singles rule) or 'smallest'.

    Validated in `parse_config`, so the resolver may trust it. See docs/dev/DESIGN.md.
    """
    recent_release_days: int = 60
    """How new a followed artist's release group has to be for its catalogue gap to be 'recent'.

    A gap this new (or future-dated) is Lidarr's metadata lagging behind MusicBrainz, which a
    scoped RefreshArtist fixes; an older one is almost always a promo or bootleg Lidarr will never
    track. See docs/dev/DESIGN.md, "Catalogue gaps".
    """
    allow_compilation_fallback: bool = True
    """Opt-out: when false, a track whose only home is a Compilation never monitors it."""
    allow_remix_releases: bool = True
    """Opt-out: when false, a remix release is never chosen unless the liked track is a remix."""
    keep_remix_only_tracks: bool = True
    """With remixes off, still keep a liked track whose every release is a remix (issue #89)."""
    deny_releases: tuple[str, ...] = ()
    """Release group MBIDs the resolver must never choose. Validated and lowercased in
    `parse_config`, so the resolver may trust their shape."""

    @property
    def exclusions(self) -> ExclusionRules:
        """The opt-outs as the pure core takes them. See `models.ExclusionRules`."""
        return ExclusionRules(
            allow_compilation_fallback=self.allow_compilation_fallback,
            allow_remix_releases=self.allow_remix_releases,
            keep_remix_only_tracks=self.keep_remix_only_tracks,
            deny_releases=frozenset(self.deny_releases),
        )


@dataclass(frozen=True, slots=True)
class GuardsConfig:
    max_unmonitors_scheduled: int = 100
    source_shrink_pct: float = 10.0
    artist_shrink_pct: float = 30.0
    unmapped_ratio_amber: float = 0.05
    projected_wanted_max: int = 1000


DEFAULT_SCHEDULE_CRON = "20 */6 * * *"
DEFAULT_SCHEDULE_TIMEZONE = "UTC"
"""The code default. A deployment can set its own zone explicitly in
`config.toml`; `UTC` is the right default for a config nobody has touched yet, since a wrong guess
at the host's local time is worse than an explicit unfamiliar one (see `core.cron`'s docstring)."""

MIN_SCHEDULE_INTERVAL_MINUTES = 60
"""No cron line may fire more often than this. A scheduled run reads the whole of Spotify - liked
tracks, saved albums, followed artists, every configured playlist - a few dozen requests for a
typical account. Spotify Developer Mode's quota is a daily allowance, not a per-run one, and a
tight schedule can burn through it in a few hours. Checked once at load
(`core.cron.min_interval_minutes`), not at every fire, so a too-tight schedule is refused before it
ever runs rather than discovered by a `QUOTA_EXCEEDED`."""


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """`[schedule]`: when the scheduled run fires, and whether it does. Read by the in-service
    scheduler (`likearr.web.schedule`, issue #68) and by `run --scheduled` itself, not just the web
    UI, which is why it lives here rather than in `[ui]` - `[ui]` never stops or times a run
    (docs/dev/DESIGN.md) - and outside `Config.plan_fingerprint`, so pausing or rescheduling can never make
    a reviewed plan stale.
    """

    cron: str = DEFAULT_SCHEDULE_CRON
    """A five-field cron line (`core.cron.parse_cron`), in `timezone`. It drives the scheduler."""
    timezone: str = DEFAULT_SCHEDULE_TIMEZONE
    """The IANA zone `cron` fires in."""
    enabled: bool = True
    """False stops every unattended (`--scheduled`) run. A hand run (`run`, `run --apply`) never
    reads this: pausing is a brake on scheduled runs only."""
    paused_reason: str = ""
    """Why it's paused - shown on Status and carried in the health record and MQTT. Empty when
    `enabled` is true, or when it was paused with no reason given."""
    paused_at: datetime | None = None
    """When it was paused. Set by the UI's pause action; a hand-edited config may leave it empty
    even with `enabled = false`."""


@dataclass(frozen=True, slots=True)
class MqttSinkConfig:
    host: str
    topic: str
    port: int = 1883
    retain: bool = True

    @property
    def username(self) -> str | None:
        return os.environ.get("LIKEARR_MQTT_USERNAME") or None

    @property
    def password(self) -> str | None:
        return os.environ.get("LIKEARR_MQTT_PASSWORD") or None


WEBHOOK_NOTIFY_ALWAYS = "always"
WEBHOOK_NOTIFY_PROBLEMS = "problems"
WEBHOOK_NOTIFY_MODES = (WEBHOOK_NOTIFY_ALWAYS, WEBHOOK_NOTIFY_PROBLEMS)


@dataclass(frozen=True, slots=True)
class WebhookSinkConfig:
    url: str
    timeout_s: float = 10.0
    notify: str = WEBHOOK_NOTIFY_ALWAYS
    """Which runs the webhook is sent (#112). `always`: every run that reaches a remote sink, as
    before. `problems`: only a run `core.health.should_notify` says is news - a new problem, or the
    recovery from one. The default keeps an existing webhook (a heartbeat, a Home Assistant
    trigger) getting every run."""


@dataclass(frozen=True, slots=True)
class HealthConfig:
    mqtt: MqttSinkConfig | None = None
    webhook: WebhookSinkConfig | None = None
    stdout: bool = True


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


DEFAULT_CLI_COMMAND = "docker compose run --rm likearr-cli"
"""The documented install's way to run a likearr command (`deploy/compose.example.yaml`)."""
MAX_CLI_COMMAND = 200


@dataclass(frozen=True, slots=True)
class UiConfig:
    """`[ui]`: what `likearr start` needs to know that nothing else in likearr does.

    Informational and access settings only. Nothing here changes what a run plans or applies,
    which is why it is outside `Config.plan_fingerprint`.
    """

    allowed_hosts: tuple[str, ...] = _LOOPBACK_HOSTS
    """Host names (without a port) the web UI answers to. Anything else is refused before any page
    is served, which is what stops DNS rebinding: a hostile page that rebinds its own name to this
    server still sends its own name as the host. ``localhost`` and ``127.0.0.1`` are always
    allowed on top of this list, for the container healthcheck. No wildcards, ports or IPv6."""
    lidarr_url: str = ""
    """Where a browser reaches Lidarr, for the Status page's links (``https://lidarr.example.org``).
    Defaults to `[lidarr] url`, which is what likearr itself calls and may be a container name no
    browser can resolve. Never called by likearr."""
    cli_command: str = DEFAULT_CLI_COMMAND
    """How a terminal on this install runs likearr, put in front of every command Clean up shows
    (#58), so they paste and run as they are. The documented install is the default; another
    setup (an ssh to the host, a container exec) is this one setting. Shown, never run."""
    public_url: str = ""
    """This service's own https address, e.g. ``https://likearr.example.org`` - empty by default.
    When set, Spotify's "Connect Spotify" (issue #79) offers a direct-callback mode that redirects
    straight to ``<public_url>/spotify/callback`` instead of the paste-back flow, using this exact
    URI registered in the Spotify developer app. Must be ``https://``; a bare host, an http:// URL,
    a query string or a fragment is a recorded error, same as every other `[ui]` value - it never
    stops likearr from starting, only from offering direct-callback mode."""
    errors: tuple[str, ...] = ()
    """Problems found in the block, each naming its key. Never raised at load (see `_ui`)."""


HOLDING_DIR_NAME = "_likearr-holding"


def default_holding_dir(root_folder: str) -> str:
    """`_likearr-holding` beside the Lidarr root folder - in its parent, never inside it - so a
    move out of the library stays on the same filesystem and is a rename."""
    root = PurePosixPath(root_folder.rstrip("/") or "/")
    return str(root.parent / HOLDING_DIR_NAME)


@dataclass(frozen=True, slots=True)
class PruneConfig:
    """`[prune]`: whether Clean up is on (#148), and where `prune-stage` moves files, for the
    commands Clean up shows (#58).

    Checked but never fatal, like `[ui]`: only the web UI reads it (the CLI only warns), and a typo
    must not stop a scheduled run. A value with a problem falls back to the default and the problem
    is recorded.
    """

    enabled: bool = False
    """Whether the web UI offers Clean up and promote-save's Spotify write access (#148). Off by
    default: Clean up needs the library mounted at Lidarr's path, the likearr-cli container and a
    holding folder, so a user turns it on knowingly. The prune CLI commands still run when it is
    off, with a one-line warning."""
    holding_dir: str = ""
    """Absolute, outside `[lidarr] root_folder`. Defaults to `default_holding_dir`."""
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    lidarr: LidarrConfig
    spotify: SpotifyConfig
    musicbrainz: MusicBrainzConfig
    state_db: Path
    rules: RulesConfig = field(default_factory=RulesConfig)
    guards: GuardsConfig = field(default_factory=GuardsConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    lock_file: Path | None = None
    ui: UiConfig = field(default_factory=UiConfig)
    prune: PruneConfig = field(default_factory=PruneConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    @property
    def holding_dir(self) -> str:
        """Where Clean up's commands tell `prune-stage` to move files: `[prune] holding_dir`, or
        `_likearr-holding` beside the root folder."""
        return self.prune.holding_dir or default_holding_dir(self.lidarr.root_folder)

    @property
    def lock_path(self) -> Path:
        """The run lock `likearr run` takes: the configured file, or the state DB with ``.lock``.

        One definition for the CLI and the web UI, which checks this same file before it spawns a
        run job; two copies could drift and leave that check guarding a file no run locks.
        """
        return self.lock_file or self.state_db.with_suffix(".lock")

    @property
    def plan_fingerprint(self) -> dict[str, dict[str, Any]]:
        """The configuration a plan is a function of, recorded in `diff.json` and checked on apply.

        The whole of `[rules]` and `[guards]`, deliberately coarse. `ExclusionRules.token` is not
        enough on its own: it leaves `deny_releases` out on purpose (see its docstring), and adding
        a release group there between planning and `--apply` is exactly the change a saved diff
        must not survive, or it monitors the release the user has just refused. Every field rather
        than a chosen few, so a setting added to either section later is covered without anyone
        remembering to add it here - and a diff planned before it existed is refused, which costs a
        re-plan and nothing else.

        Plain JSON types (a tuple becomes a list), so it compares equal to the copy read back from
        the file. `[lidarr]` is left out: it says how to talk to Lidarr - where, which profiles, how
        many refreshes and how long to wait - rather than what is wanted.
        """
        return {
            "rules": {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self.rules).items()},
            "guards": asdict(self.guards),
        }


PLACEHOLDER_CONTACT = "you@example.com"
"""`[musicbrainz] contact` as `deploy/config.example.toml` ships it. Loads, but Doctor warns: it
identifies nobody, which defeats MusicBrainz's reason for asking (issue #110)."""

KNOWN_KEYS: dict[str, frozenset[str]] = {
    "lidarr": frozenset(
        {
            "url",
            "root_folder",
            "quality_profile",
            "lean_profile",
            "full_profile",
            "tag",
            "refresh_timeout_s",
            "refresh_per_album_s",
            "refresh_timeout_max_s",
            "max_refreshes_per_run",
            "recent_gap_refresh_hours",
        }
    ),
    "spotify": frozenset(
        {"token_file", "playlists", "redirect_uri", "followed_artists", "saved_albums", "liked_tracks"}
    ),
    "musicbrainz": frozenset({"contact", "base_url", "min_interval_s", "negative_cache_days", "positive_cache_days"}),
    "state": frozenset({"db", "lock_file"}),
    "rules": frozenset(
        {
            "singles_fallback_days",
            "albums_only_tag",
            "liked_track_scope",
            "recent_release_days",
            "allow_compilation_fallback",
            "allow_remix_releases",
            "keep_remix_only_tracks",
            "deny_releases",
        }
    ),
    "guards": frozenset(
        {
            "max_unmonitors_scheduled",
            "source_shrink_pct",
            "artist_shrink_pct",
            "unmapped_ratio_amber",
            "projected_wanted_max",
        }
    ),
    "health": frozenset({"stdout", "mqtt", "webhook"}),
    "health.mqtt": frozenset({"host", "topic", "port", "retain"}),
    "health.webhook": frozenset({"url", "timeout_s", "notify"}),
    "ui": frozenset({"allowed_hosts", "lidarr_url", "cli_command", "public_url"}),
    "prune": frozenset({"enabled", "holding_dir"}),
    "schedule": frozenset({"cron", "timezone", "enabled", "paused_reason", "paused_at"}),
}
"""Every key `parse_config` reads, per section, by its TOML name. Written out rather than taken
from the dataclasses because some names differ (`[state] db` is `Config.state_db`); a test ties
the two together. Anything else in a section is a typo or a stale key, and fails the load - or,
in `[ui]` and `[prune]`, which never stop a load, is a recorded problem."""

KNOWN_SECTIONS: frozenset[str] = frozenset(k for k in KNOWN_KEYS if "." not in k)

MUSICBRAINZ_MIN_INTERVAL_S = 1.0
"""The floor on `[musicbrainz] min_interval_s` against musicbrainz.org itself: their published
limit is one request a second. A self-hosted mirror has no such limit and may go lower."""


_QUOTED = "'{}'"
_BRACKETED = "[{}]"


def _did_you_mean(word: str, known: frozenset[str], template: str) -> str:
    close = difflib.get_close_matches(word, sorted(known), n=1)
    return f" (did you mean {template.format(close[0])}?)" if close else ""


_SECRET_ENV: dict[tuple[str, str], str] = {
    ("lidarr", "api_key"): "LIKEARR_LIDARR_API_KEY",
    ("spotify", "client_id"): "LIKEARR_SPOTIFY_CLIENT_ID",
    ("spotify", "client_secret"): "LIKEARR_SPOTIFY_CLIENT_SECRET",
    ("health.mqtt", "username"): "LIKEARR_MQTT_USERNAME",
    ("health.mqtt", "password"): "LIKEARR_MQTT_PASSWORD",
}
"""Secrets someone might write into the file. They are never read from it, so they are unknown keys
like any other; the message names the env var instead of guessing at a spelling."""


def _unknown_keys(table: dict[str, Any], name: str) -> list[str]:
    """One message per key in `table` that `[name]` does not read, each with its likely intended key.
    Never includes a value: the key may be a secret written into the wrong place."""
    known = KNOWN_KEYS[name]
    out: list[str] = []
    for key in table:
        if key in known:
            continue
        env = _SECRET_ENV.get((name, key))
        hint = (
            f" (secrets are read only from the environment: set {env})" if env else _did_you_mean(key, known, _QUOTED)
        )
        out.append(f"[{name}] unknown key {key!r}{hint}")
    return out


def _check_sections(raw: dict[str, Any]) -> None:
    unknown = [
        f"unknown section [{key}]{_did_you_mean(key, KNOWN_SECTIONS, _BRACKETED)}"
        for key in raw
        if key not in KNOWN_SECTIONS
    ]
    if unknown:
        raise ConfigError("; ".join(unknown))


def _table(parent: dict[str, Any], key: str, name: str) -> dict[str, Any]:
    """`parent[key]` as a table (empty when absent), refused when it is anything else."""
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table, not {value!r}")
    return value


def _checked(parent: dict[str, Any], key: str, name: str) -> dict[str, Any]:
    """`_table`, then refuse any key `[name]` does not read, naming every one at once."""
    table = _table(parent, key, name)
    unknown = _unknown_keys(table, name)
    if unknown:
        raise ConfigError("; ".join(unknown))
    return table


def _number(
    section: dict[str, Any],
    key: str,
    default: float,
    *,
    name: str,
    whole: bool,
    at_least: float | None = None,
    at_most: float | None = None,
) -> float:
    """A TOML number, checked for type and range. NaN and infinity are never a setting.

    A string is refused rather than converted: `"20%"` has no right reading, and `"lots"` used to
    reach the CLI as a bare `ValueError` that named no key. A whole float (`100.0`) is accepted
    for a whole-number key, since it is not ambiguous.
    """
    value = section.get(key, default)
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    if ok and isinstance(value, float):
        ok = math.isfinite(value) and (not whole or value.is_integer())
    elif ok and not whole:
        ok = abs(value) <= sys.float_info.max  # an integer too big for a float would overflow `float()`
    if ok and at_least is not None and value < at_least:
        ok = False
    if ok and at_most is not None and value > at_most:
        ok = False
    if not ok:
        kind = "a whole number" if whole else "a number"
        if at_least is not None and at_most is not None:
            kind += f" between {at_least:g} and {at_most:g}"
        elif at_least is not None:
            kind += f" >= {at_least:g}"
        raise ConfigError(f"[{name}] {key} must be {kind}, got {value!r}")
    return value


def _int(
    section: dict[str, Any],
    key: str,
    default: int,
    *,
    name: str,
    at_least: int | None = None,
    at_most: int | None = None,
) -> int:
    return int(_number(section, key, default, name=name, whole=True, at_least=at_least, at_most=at_most))


def _float(
    section: dict[str, Any],
    key: str,
    default: float,
    *,
    name: str,
    at_least: float | None = None,
    at_most: float | None = None,
) -> float:
    return float(_number(section, key, default, name=name, whole=False, at_least=at_least, at_most=at_most))


def _bool(section: dict[str, Any], key: str, default: bool, *, name: str) -> bool:
    """A TOML boolean. `bool("false")` is True, so a quoted value is refused, never cast."""
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{name}] {key} must be true or false, got {value!r}")
    return value


def _choice(section: dict[str, Any], key: str, default: str, allowed: tuple[str, ...], *, name: str) -> str:
    """A TOML string that must be one of `allowed`, exactly. Case matters: the value is a keyword,
    and quietly folding `"Problems"` would teach a spelling the docs never use."""
    value = section.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(repr(a) for a in allowed)
        raise ConfigError(f"[{name}] {key} must be one of {options}, got {value!r}")
    return value


def _is_musicbrainz_org(base_url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    return host == "musicbrainz.org" or host.endswith(".musicbrainz.org")


_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_mbid(value: str) -> bool:
    """A MusicBrainz id in the lowercase 8-4-4-4-12 form likearr stores. The one shape check."""
    return _MBID.fullmatch(value) is not None


def _deny_releases(section: dict[str, Any]) -> tuple[str, ...]:
    """`[rules] deny_releases`, validated as MBIDs, lowercased, deduplicated and sorted.

    The shape is checked here rather than tolerated in the resolver because a mistyped MBID would
    otherwise do nothing at all, silently: the entry would simply never match a release group and
    the box set the user meant to refuse would keep being monitored, run after run, with no
    report saying why. Failing at load is the only way that mistake is ever visible.
    """
    raw = section.get("deny_releases", [])
    if isinstance(raw, str) or not isinstance(raw, list):
        raise ConfigError("[rules] deny_releases must be a list of MusicBrainz release group ids")
    out: set[str] = set()
    for item in raw:
        mbid = str(item).strip().lower()
        if not is_mbid(mbid):
            raise ConfigError(
                f"[rules] deny_releases entry {item!r} is not a MusicBrainz release group id "
                "(36 characters, 8-4-4-4-12 hex)"
            )
        out.add(mbid)
    return tuple(sorted(out))


def _browser_url(value: str) -> str | None:
    """`value` as a base for browser links, or ``None``: http(s), a host name, no query or fragment.

    `urlsplit` raises on a malformed IPv6 host ("http://[::1"); that is an answer here, not an error.
    """
    text = value.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not host or parts.query or parts.fragment:
        return None
    return text


def _ui(section: object, *, lidarr_url: str) -> UiConfig:
    """`[ui]`, checked but never fatal: a problem is recorded in `UiConfig.errors` instead of raised.

    Only `likearr start` reads this block, but every command loads the same file - the cron run
    included - so a typo here must not stop a run, least of all before any health sink exists to
    report it. `start` refuses to start on a recorded problem, and the Status page shows any that
    appear later. A value with a problem falls back to its default, and a key it does not read
    is a recorded problem too (issue #110).
    """
    if not isinstance(section, dict):
        return UiConfig(lidarr_url=_browser_url(lidarr_url) or "", errors=(f"[ui] must be a table, not {section!r}",))
    errors: list[str] = _unknown_keys(section, "ui")
    hosts = section.get("allowed_hosts", list(_LOOPBACK_HOSTS))
    cleaned: tuple[str, ...] = _LOOPBACK_HOSTS
    if isinstance(hosts, str) or not isinstance(hosts, list) or not hosts:
        errors.append("[ui] allowed_hosts must be a non-empty list of host names")
    else:
        names = tuple(str(h).strip().lower() for h in hosts)
        # Starlette compares only what comes before the first ":" of the Host header, so a port or
        # an IPv6 literal could never match: refusing them here beats a UI that answers 400 to all.
        bad = [h for h in names if not h or "*" in h or ":" in h]
        if bad:
            errors.append(
                f"[ui] allowed_hosts entries {bad!r} must be exact host names or IPv4 addresses: "
                "no wildcards, no ports, no IPv6 literals"
            )
        else:
            cleaned = names
    # A browser follows these links, so the fallback is held to the same rule as a value written
    # here - it simply is not blamed on a key the user never set, and fails to no links at all.
    browser_lidarr = _browser_url(lidarr_url) or ""
    if "lidarr_url" in section:
        given = _browser_url(str(section["lidarr_url"]))
        if given is None:
            errors.append(
                f"[ui] lidarr_url {section['lidarr_url']!r} must be an http:// or https:// address with a host "
                "name and no query or fragment"
            )
        else:
            browser_lidarr = given
    cli_command = DEFAULT_CLI_COMMAND
    if "cli_command" in section:
        given_command = section["cli_command"]
        if isinstance(given_command, str) and _one_line(given_command.strip(), MAX_CLI_COMMAND):
            cli_command = given_command.strip()
        else:
            errors.append(
                f"[ui] cli_command must be one line of at most {MAX_CLI_COMMAND} characters, "
                f"like {DEFAULT_CLI_COMMAND!r}"
            )
    public_url = ""
    if "public_url" in section:
        given_url = section["public_url"]
        checked = _browser_url(str(given_url)) if isinstance(given_url, str) else None
        # No path either: this exact value gets "/spotify/callback" appended to build the
        # redirect_uri (web.routes.settings.spotify_connect_start), and the route is registered at that
        # literal path with no prefix - a public_url with its own path would silently build a
        # redirect_uri nothing serves, which "must be https://..." alone would not catch.
        has_path = checked is not None and urllib.parse.urlsplit(checked).path not in ("", "/")
        if checked is None or not checked.lower().startswith("https://") or has_path:
            errors.append(
                f"[ui] public_url {given_url!r} must be an https:// address with a host name, "
                "no path, query or fragment"
            )
        else:
            public_url = checked
    return UiConfig(
        allowed_hosts=cleaned,
        lidarr_url=browser_lidarr,
        cli_command=cli_command,
        public_url=public_url,
        errors=tuple(errors),
    )


def _one_line(value: str, limit: int) -> bool:
    """Non-empty, at most `limit` characters, and no control or format character (Unicode Cc, Cf): a
    value shown in a command someone pastes into a shell must carry no line break, no escape
    sequence, no C1 control and no right-to-left override that makes it read as something else."""
    return 0 < len(value) <= limit and not any(unicodedata.category(c) in {"Cc", "Cf"} for c in value)


def _paused_at(value: object) -> datetime | None:
    """`[schedule] paused_at`: a native TOML datetime (what the UI writes), an ISO 8601 string (what
    a hand edit might write), or absent. Naive input is read as UTC - the UI always writes an
    aware one, so naive only happens by hand."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"[schedule] paused_at {value!r} is not a timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def validate_cron_and_timezone(cron: str, timezone: str) -> None:
    """Refuse a cron line or timezone the way `_schedule` refuses one at load: a line
    `core.cron.parse_cron` cannot read, one that fires more often than
    `MIN_SCHEDULE_INTERVAL_MINUTES`, or a timezone `zoneinfo` does not recognise.

    The one place this wording lives, so a live preview (`web.settings.preview_schedule`) and a
    rejected save can never disagree about why a line is bad - see issue #86. Validates only the
    cron line and the timezone; `_schedule` also checks `enabled` and `paused_reason`, which have
    no live-preview equivalent.

    Raises:
        ConfigError: names `[schedule] cron` or `[schedule] timezone`, and why.
    """
    try:
        parse_cron(cron)
    except CronError as exc:
        raise ConfigError(f"[schedule] cron: {exc}") from exc
    gap = min_interval_minutes(cron)
    if gap < MIN_SCHEDULE_INTERVAL_MINUTES:
        raise ConfigError(
            f"[schedule] cron {cron!r} can fire as often as every {gap:g} minutes; the scheduler refuses anything "
            f"more often than every {MIN_SCHEDULE_INTERVAL_MINUTES} minutes - a scheduled run reads all of "
            "Spotify, and Spotify's Developer Mode quota is a daily allowance, not a per-run one"
        )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:  # "America" is a dir; a 300-char name is OSError
        raise ConfigError(f"[schedule] timezone {timezone!r} is not an IANA timezone name") from exc


def _schedule(section: dict[str, Any]) -> ScheduleConfig:
    """`[schedule]`, validated like `[rules]` or `[guards]`: a bad value stops the load rather than
    being quietly recorded, because - unlike `[ui]` - a scheduled run reads this block itself.
    Falling back to the default schedule on a typo would fire unattended applies at a time the
    operator never chose, which is worse than refusing to start.
    """
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        # `bool("false")` is True: a quoted value would silently leave unattended applies running.
        raise ConfigError(f"[schedule] enabled must be true or false, not {enabled!r}")
    reason = str(section.get("paused_reason", ""))
    if reason and not _one_line(reason, 200):
        raise ConfigError(f"[schedule] paused_reason must be one line of at most 200 characters, not {reason!r}")

    cron = str(section.get("cron", "")).strip() or DEFAULT_SCHEDULE_CRON
    timezone = str(section.get("timezone", "")).strip() or DEFAULT_SCHEDULE_TIMEZONE
    validate_cron_and_timezone(cron, timezone)

    return ScheduleConfig(
        cron=cron,
        timezone=timezone,
        enabled=enabled,
        paused_reason=reason,
        paused_at=_paused_at(section.get("paused_at")),
    )


def _prune(section: object, *, root_folder: str) -> PruneConfig:
    """`[prune]`, checked but never fatal (see `PruneConfig`). A key it does not read is recorded."""
    default = default_holding_dir(root_folder)
    if not isinstance(section, dict):
        return PruneConfig(holding_dir=default, errors=(f"[prune] must be a table, not {section!r}",))
    problems = _unknown_keys(section, "prune")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        problems.append(f"[prune] enabled must be true or false, not {enabled!r}; Clean up stays off")
        enabled = False
    if "holding_dir" not in section:
        return PruneConfig(enabled=enabled, holding_dir=default, errors=tuple(problems))
    given = section["holding_dir"]
    text = given.strip().rstrip("/") if isinstance(given, str) else ""
    path = PurePosixPath(text) if text else None
    root = PurePosixPath(root_folder.rstrip("/") or "/")
    problem = ""
    if path is None or not path.is_absolute() or not _one_line(text, 4096) or ".." in path.parts:
        problem = f"[prune] holding_dir {given!r} must be an absolute path with no '..' in it"
    elif path == root or path.is_relative_to(root):
        problem = (
            f"[prune] holding_dir {text!r} is inside the Lidarr root folder {str(root)!r}; "
            f"put it beside it, e.g. {default!r}"
        )
    if problem:
        return PruneConfig(enabled=enabled, holding_dir=default, errors=(*problems, problem))
    return PruneConfig(enabled=enabled, holding_dir=text, errors=tuple(problems))


def _req(d: dict, key: str, section: str) -> object:
    if key not in d:
        raise ConfigError(f"[{section}] is missing required key '{key}'")
    return d[key]


def load_config(path: Path | str) -> Config:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config file {p} is not valid TOML: {e}") from e
    return parse_config(raw, base_dir=p.parent)


def parse_config(raw: dict, *, base_dir: Path | None = None) -> Config:
    base = base_dir or Path.cwd()

    def path_of(v: object) -> Path:
        q = Path(str(v)).expanduser()
        return q if q.is_absolute() else (base / q)

    _check_sections(raw)
    li = _checked(raw, "lidarr", "lidarr")
    lidarr = LidarrConfig(
        url=str(_req(li, "url", "lidarr")).rstrip("/"),
        root_folder=str(_req(li, "root_folder", "lidarr")),
        quality_profile=str(_req(li, "quality_profile", "lidarr")),
        lean_profile=str(li.get("lean_profile", "Lean")),
        full_profile=str(li.get("full_profile", "Full")),
        tag=str(li.get("tag", "likearr")),
        refresh_timeout_s=_float(li, "refresh_timeout_s", 300.0, name="lidarr"),
        refresh_per_album_s=_float(li, "refresh_per_album_s", 2.0, name="lidarr", at_least=0),
        refresh_timeout_max_s=_float(li, "refresh_timeout_max_s", 3600.0, name="lidarr", at_least=0),
        max_refreshes_per_run=_int(li, "max_refreshes_per_run", 10, name="lidarr", at_least=0),
        recent_gap_refresh_hours=_float(li, "recent_gap_refresh_hours", 24.0, name="lidarr", at_least=0),
    )
    sp = _checked(raw, "spotify", "spotify")
    spotify = SpotifyConfig(
        token_file=path_of(_req(sp, "token_file", "spotify")),
        playlists=tuple(str(x) for x in sp.get("playlists", [])),
        redirect_uri=str(sp.get("redirect_uri", "http://127.0.0.1:8765/callback")),
        followed_artists=_bool(sp, "followed_artists", True, name="spotify"),
        saved_albums=_bool(sp, "saved_albums", True, name="spotify"),
        liked_tracks=_bool(sp, "liked_tracks", True, name="spotify"),
    )
    mb = _checked(raw, "musicbrainz", "musicbrainz")
    base_url = str(mb.get("base_url", "https://musicbrainz.org/ws/2")).rstrip("/")
    min_interval_s = _float(mb, "min_interval_s", MUSICBRAINZ_MIN_INTERVAL_S, name="musicbrainz")
    if min_interval_s < MUSICBRAINZ_MIN_INTERVAL_S and _is_musicbrainz_org(base_url):
        raise ConfigError(
            f"[musicbrainz] min_interval_s must be at least {MUSICBRAINZ_MIN_INTERVAL_S:g} against musicbrainz.org, "
            f"which allows one request a second, got {min_interval_s:g}; only a self-hosted mirror may go lower"
        )
    musicbrainz = MusicBrainzConfig(
        contact=str(_req(mb, "contact", "musicbrainz")),
        base_url=base_url,
        min_interval_s=min_interval_s,
        negative_cache_days=_int(mb, "negative_cache_days", 7, name="musicbrainz", at_least=0),
        positive_cache_days=_int(mb, "positive_cache_days", 90, name="musicbrainz", at_least=0),
    )
    st = _checked(raw, "state", "state")
    state_db = path_of(_req(st, "db", "state"))
    lock_file = path_of(st["lock_file"]) if "lock_file" in st else None
    ru = _checked(raw, "rules", "rules")
    scope = str(ru.get("liked_track_scope", LIKED_TRACK_SCOPE_ALBUM))
    if scope not in LIKED_TRACK_SCOPES:
        allowed = ", ".join(repr(s) for s in sorted(LIKED_TRACK_SCOPES))
        raise ConfigError(f"[rules] liked_track_scope must be one of {allowed}, not {scope!r}")
    rules = RulesConfig(
        singles_fallback_days=_int(ru, "singles_fallback_days", 180, name="rules", at_least=0),
        albums_only_tag=str(ru.get("albums_only_tag", "albums-only")),
        liked_track_scope=scope,
        recent_release_days=_int(ru, "recent_release_days", 60, name="rules", at_least=0),
        allow_compilation_fallback=_bool(ru, "allow_compilation_fallback", True, name="rules"),
        allow_remix_releases=_bool(ru, "allow_remix_releases", True, name="rules"),
        keep_remix_only_tracks=_bool(ru, "keep_remix_only_tracks", True, name="rules"),
        deny_releases=_deny_releases(ru),
    )
    gu = _checked(raw, "guards", "guards")
    guards = GuardsConfig(
        max_unmonitors_scheduled=_int(gu, "max_unmonitors_scheduled", 100, name="guards", at_least=0),
        source_shrink_pct=_float(gu, "source_shrink_pct", 10.0, name="guards", at_least=0, at_most=100),
        artist_shrink_pct=_float(gu, "artist_shrink_pct", 30.0, name="guards", at_least=0, at_most=100),
        unmapped_ratio_amber=_float(gu, "unmapped_ratio_amber", 0.05, name="guards", at_least=0, at_most=1),
        projected_wanted_max=_int(gu, "projected_wanted_max", 1000, name="guards", at_least=0),
    )
    he = _checked(raw, "health", "health")
    mqtt = None
    if "mqtt" in he:
        m = _checked(he, "mqtt", "health.mqtt")
        mqtt = MqttSinkConfig(
            host=str(_req(m, "host", "health.mqtt")),
            topic=str(_req(m, "topic", "health.mqtt")),
            port=_int(m, "port", 1883, name="health.mqtt"),
            retain=_bool(m, "retain", True, name="health.mqtt"),
        )
    webhook = None
    if "webhook" in he:
        w = _checked(he, "webhook", "health.webhook")
        webhook = WebhookSinkConfig(
            url=str(_req(w, "url", "health.webhook")),
            timeout_s=_float(w, "timeout_s", 10.0, name="health.webhook"),
            notify=_choice(w, "notify", WEBHOOK_NOTIFY_ALWAYS, WEBHOOK_NOTIFY_MODES, name="health.webhook"),
        )
    health = HealthConfig(mqtt=mqtt, webhook=webhook, stdout=_bool(he, "stdout", True, name="health"))

    # `[ui]` and `[prune]` are checked but never fatal (see `_ui`), so a problem with either table
    # itself is recorded there.
    return Config(
        lidarr=lidarr,
        spotify=spotify,
        musicbrainz=musicbrainz,
        state_db=state_db,
        rules=rules,
        guards=guards,
        health=health,
        lock_file=lock_file,
        ui=_ui(raw.get("ui", {}), lidarr_url=lidarr.url),
        prune=_prune(raw.get("prune", {}), root_folder=lidarr.root_folder),
        schedule=_schedule(_checked(raw, "schedule", "schedule")),
    )
