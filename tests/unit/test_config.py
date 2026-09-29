from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path

import pytest

from likearr.config import (
    KNOWN_KEYS,
    KNOWN_SECTIONS,
    MIN_SCHEDULE_INTERVAL_MINUTES,
    PLACEHOLDER_CONTACT,
    Config,
    ConfigError,
    GuardsConfig,
    HealthConfig,
    LidarrConfig,
    MqttSinkConfig,
    MusicBrainzConfig,
    PruneConfig,
    RulesConfig,
    ScheduleConfig,
    SpotifyConfig,
    UiConfig,
    WebhookSinkConfig,
    load_config,
    parse_config,
    validate_cron_and_timezone,
    write_initial_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG_PATH = REPO_ROOT / "deploy" / "config.example.toml"

MINIMAL_RAW = {
    "lidarr": {
        "root_folder": "/music",
        "quality_profile": "Standard",
    },
    "spotify": {
        "token_file": "token.json",
    },
    "state": {
        "db": "state.sqlite",
    },
}


def _raw(**overrides: dict) -> dict:
    raw = {k: dict(v) for k, v in MINIMAL_RAW.items()}
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return raw


# ---------------------------------------------------------------- happy path


def test_parse_config_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "lidarr-key")
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "spotify-client-id")

    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    assert config.lidarr.url == "http://lidarr:8686"
    assert config.lidarr.root_folder == "/music"
    assert config.lidarr.quality_profile == "Standard"
    assert config.lidarr.lean_profile == "Lean"
    assert config.lidarr.full_profile == "Full"
    assert config.lidarr.tag == "likearr"
    assert config.lidarr.refresh_timeout_s == 300.0
    assert config.lidarr.refresh_per_album_s == 2.0
    assert config.lidarr.refresh_timeout_max_s == 3600.0
    assert config.lidarr.max_refreshes_per_run == 10
    assert config.lidarr.recent_gap_refresh_hours == 24.0
    assert config.lidarr.api_key == "lidarr-key"

    assert config.spotify.token_file == tmp_path / "token.json"
    assert config.spotify.playlists == ()
    assert config.spotify.redirect_uri == "http://127.0.0.1:8765/callback"
    assert config.spotify.followed_artists is True
    assert config.spotify.saved_albums is True
    assert config.spotify.liked_tracks is True
    assert config.spotify.client_id == "spotify-client-id"
    assert config.spotify.client_secret is None

    assert config.musicbrainz.contact == "https://github.com/sysdad/likearr"
    assert config.musicbrainz.base_url == "https://musicbrainz.org/ws/2"
    assert config.musicbrainz.min_interval_s == 1.0
    assert config.musicbrainz.negative_cache_days == 7
    assert config.musicbrainz.positive_cache_days == 90

    assert config.state_db == tmp_path / "state.sqlite"
    assert config.lock_file is None

    assert config.rules.singles_fallback_days == 180
    assert config.rules.albums_only_tag == "albums-only"
    assert config.rules.liked_track_scope == "album"
    assert config.rules.recent_release_days == 60

    assert config.guards.max_unmonitors_scheduled == 100
    assert config.guards.source_shrink_pct == 10.0
    assert config.guards.artist_shrink_pct == 30.0
    assert config.guards.unmapped_ratio_amber == 0.05
    assert config.guards.projected_wanted_max == 1000

    assert config.health.stdout is True
    assert config.health.mqtt is None
    assert config.health.webhook is None


def test_parse_config_full_overrides(tmp_path: Path) -> None:
    raw = _raw(
        lidarr={
            "lean_profile": "MyLean",
            "full_profile": "MyFull",
            "tag": "mytag",
            "refresh_timeout_s": 60,
            "refresh_per_album_s": 0.5,
            "refresh_timeout_max_s": 900,
            "max_refreshes_per_run": 3,
            "recent_gap_refresh_hours": 6,
        },
        spotify={
            "playlists": ["p1", "p2"],
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "followed_artists": False,
            "saved_albums": False,
            "liked_tracks": False,
        },
        musicbrainz={
            "base_url": "https://mb.example.invalid/ws/2/",
            "min_interval_s": 2.5,
            "negative_cache_days": 14,
            "positive_cache_days": 30,
        },
        state={"lock_file": "run.lock"},
        rules={
            "singles_fallback_days": 90,
            "albums_only_tag": "albums",
            "liked_track_scope": "smallest",
            "recent_release_days": 14,
        },
        guards={
            "max_unmonitors_scheduled": 50,
            "source_shrink_pct": 5.0,
            "artist_shrink_pct": 20.0,
            "unmapped_ratio_amber": 0.1,
            "projected_wanted_max": 500,
        },
    )

    config = parse_config(raw, base_dir=tmp_path)

    assert config.lidarr.lean_profile == "MyLean"
    assert config.lidarr.full_profile == "MyFull"
    assert config.lidarr.tag == "mytag"
    assert config.lidarr.refresh_timeout_s == 60.0
    assert config.lidarr.refresh_per_album_s == 0.5
    assert config.lidarr.refresh_timeout_max_s == 900.0
    assert config.lidarr.max_refreshes_per_run == 3
    assert config.lidarr.recent_gap_refresh_hours == 6.0

    assert config.spotify.playlists == ("p1", "p2")
    assert config.spotify.redirect_uri == "http://127.0.0.1:9999/cb"
    assert config.spotify.followed_artists is False
    assert config.spotify.saved_albums is False
    assert config.spotify.liked_tracks is False

    assert config.musicbrainz.base_url == "https://mb.example.invalid/ws/2"
    assert config.musicbrainz.min_interval_s == 2.5
    assert config.musicbrainz.negative_cache_days == 14
    assert config.musicbrainz.positive_cache_days == 30

    assert config.lock_file == tmp_path / "run.lock"
    assert config.rules.singles_fallback_days == 90
    assert config.rules.albums_only_tag == "albums"
    assert config.rules.liked_track_scope == "smallest"
    assert config.rules.recent_release_days == 14

    assert config.guards.max_unmonitors_scheduled == 50
    assert config.guards.source_shrink_pct == 5.0
    assert config.guards.artist_shrink_pct == 20.0
    assert config.guards.unmapped_ratio_amber == 0.1
    assert config.guards.projected_wanted_max == 500


# ---------------------------------------------------------------- missing required keys


def test_an_empty_file_loads_with_a_default_or_an_unset_value_for_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing in the file is required any more. Paths default beside it, the
    deployment settings come from the environment, and the two `[lidarr]` keys a user picks in
    Settings are empty - reported by `unset`, never a load failure."""
    monkeypatch.delenv("LIKEARR_LIDARR_URL")

    config = parse_config({}, base_dir=tmp_path)

    assert config.spotify.token_file == tmp_path / "spotify-token.json"
    assert config.state_db == tmp_path / "state.sqlite"
    assert config.musicbrainz.contact == "https://github.com/sysdad/likearr"
    assert config.lidarr.url == ""
    assert (config.lidarr.root_folder, config.lidarr.quality_profile) == ("", "")
    assert config.lidarr.unset == ("LIKEARR_LIDARR_URL", "[lidarr] root_folder", "[lidarr] quality_profile")
    assert config.ui.allowed_hosts == ()
    assert config.ui.errors == ()


def test_the_deployment_settings_come_from_the_environment_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_URL", " http://lidarr.example.test:8686/\r\n")
    monkeypatch.setenv("LIKEARR_ALLOWED_HOSTS", " Likearr.Example.org , 192.168.1.20,likearr.example.org ")
    monkeypatch.setenv("LIKEARR_MUSICBRAINZ_CONTACT", " someone@example.invalid\n")

    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    assert config.lidarr.url == "http://lidarr.example.test:8686"
    assert config.ui.allowed_hosts == ("likearr.example.org", "192.168.1.20")
    assert config.musicbrainz.contact == "someone@example.invalid"
    assert config.lidarr.unset == ()
    assert config.lidarr.setup_needed == ""


@pytest.mark.parametrize(
    ("value", "bad"),
    [
        ("*.example.org", "*.example.org"),
        ("likearr.lan:8770", "likearr.lan:8770"),
        ("fd00::20", "fd00::20"),
        ("a.example.org,,b.example.org", ""),
    ],
)
def test_bad_allowed_hosts_are_a_recorded_problem_naming_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str, bad: str
) -> None:
    monkeypatch.setenv("LIKEARR_ALLOWED_HOSTS", value)

    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    assert config.ui.allowed_hosts == ()
    assert len(config.ui.errors) == 1
    assert config.ui.errors[0].startswith("LIKEARR_ALLOWED_HOSTS entries")
    assert repr(bad) in config.ui.errors[0]


@pytest.mark.parametrize(
    ("section", "key", "env"),
    [
        ("lidarr", "url", "LIKEARR_LIDARR_URL"),
        ("ui", "allowed_hosts", "LIKEARR_ALLOWED_HOSTS"),
        ("musicbrainz", "contact", "LIKEARR_MUSICBRAINZ_CONTACT"),
    ],
)
def test_a_removed_key_fails_the_load_naming_its_env_var_without_echoing_the_value(
    tmp_path: Path, section: str, key: str, env: str
) -> None:
    """These moved to the environment. `[ui]` never fails a load otherwise; this does."""
    raw = _raw(**{section: {key: "VALUE-SENTINEL"}})

    with pytest.raises(ConfigError) as caught:
        parse_config(raw, base_dir=tmp_path)

    assert f"[{section}] {key} is no longer read from config.toml: set {env} instead" in str(caught.value)
    assert "VALUE-SENTINEL" not in str(caught.value)


def test_every_removed_key_is_named_at_once(tmp_path: Path) -> None:
    raw = _raw(lidarr={"url": "x"}, ui={"allowed_hosts": ["x"]}, musicbrainz={"contact": "x"})

    with pytest.raises(ConfigError) as caught:
        parse_config(raw, base_dir=tmp_path)

    for env in ("LIKEARR_LIDARR_URL", "LIKEARR_ALLOWED_HOSTS", "LIKEARR_MUSICBRAINZ_CONTACT"):
        assert env in str(caught.value)


@pytest.mark.parametrize(
    ("unset", "expected"),
    [
        ({"root_folder": ""}, "[lidarr] root_folder is not set"),
        ({"quality_profile": ""}, "[lidarr] quality_profile is not set"),
        ({"root_folder": "", "quality_profile": ""}, "[lidarr] root_folder and [lidarr] quality_profile are not set"),
        ({"url": ""}, "LIKEARR_LIDARR_URL is not set"),
    ],
)
def test_setup_needed_names_what_is_missing_and_where_to_set_it(unset: dict[str, str], expected: str) -> None:
    lidarr = dataclasses.replace(
        LidarrConfig(url="http://lidarr:8686", root_folder="/music", quality_profile="Standard"), **unset
    )

    message = lidarr.setup_needed

    assert expected in message
    if "url" in unset:
        assert "environment" in message
    else:
        assert "Settings" in message


def test_a_first_start_writes_the_example_and_the_next_load_reads_it_unchanged(tmp_path: Path) -> None:
    """Compose start: an empty data directory and environment variables only."""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "config.toml"

    assert write_initial_config(path) is True

    written = path.read_bytes()
    assert written == EXAMPLE_CONFIG_PATH.read_bytes(), "the example, comments and all"
    assert path.stat().st_mode & 0o777 == 0o660
    first = load_config(path)
    assert first.state_db == data / "state.sqlite"
    assert first.lidarr.url == "http://lidarr:8686"

    assert write_initial_config(path) is False, "a second start never writes again"
    assert path.read_bytes() == written
    assert load_config(path) == first
    assert [p.name for p in data.iterdir()] == ["config.toml"], "no temp file left behind"


def test_a_first_start_never_overwrites_a_file_already_there(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# mine\n")

    assert write_initial_config(path) is False
    assert path.read_text() == "# mine\n"


def test_a_first_start_never_writes_through_a_dangling_symlink(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.symlink_to(tmp_path / "elsewhere.toml")

    assert write_initial_config(path) is False
    assert not (tmp_path / "elsewhere.toml").exists()


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "does-not-exist.toml")


def test_load_config_invalid_toml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not [ valid toml")

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)


# ---------------------------------------------------------------- relative path resolution


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "conf"
    config_dir.mkdir()

    config = parse_config(MINIMAL_RAW, base_dir=config_dir)

    assert config.spotify.token_file == config_dir / "token.json"
    assert config.state_db == config_dir / "state.sqlite"


def test_absolute_paths_are_not_rebased(tmp_path: Path) -> None:
    absolute_token = tmp_path / "elsewhere" / "token.json"
    raw = _raw(spotify={"token_file": str(absolute_token)})

    config = parse_config(raw, base_dir=tmp_path / "conf")

    assert config.spotify.token_file == absolute_token


def test_load_config_resolves_relative_to_file_location(tmp_path: Path) -> None:
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    config_path = config_dir / "likearr.toml"

    toml_text = """
[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "token.json"

[state]
db = "state.sqlite"
"""
    config_path.write_text(toml_text)

    config = load_config(config_path)

    assert config.spotify.token_file == config_dir / "token.json"
    assert config.state_db == config_dir / "state.sqlite"


def test_tilde_expands_in_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    raw = _raw(spotify={"token_file": "~/token.json"})

    config = parse_config(raw, base_dir=tmp_path / "conf")

    assert config.spotify.token_file == tmp_path / "token.json"


# ---------------------------------------------------------------- env-var secrets


def test_lidarr_api_key_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    monkeypatch.delenv("LIKEARR_LIDARR_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="LIKEARR_LIDARR_API_KEY is not set"):
        _ = config.lidarr.api_key

    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "secret-key")
    assert config.lidarr.api_key == "secret-key"


def test_spotify_client_id_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    monkeypatch.delenv("LIKEARR_SPOTIFY_CLIENT_ID", raising=False)
    with pytest.raises(ConfigError, match="LIKEARR_SPOTIFY_CLIENT_ID is not set"):
        _ = config.spotify.client_id

    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "abc123")
    assert config.spotify.client_id == "abc123"


def test_spotify_client_secret_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    monkeypatch.delenv("LIKEARR_SPOTIFY_CLIENT_SECRET", raising=False)
    assert config.spotify.client_secret is None

    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_SECRET", "shh")
    assert config.spotify.client_secret == "shh"


@pytest.mark.parametrize(
    ("section", "key", "env"),
    [
        ("lidarr", "api_key", "LIKEARR_LIDARR_API_KEY"),
        ("spotify", "client_id", "LIKEARR_SPOTIFY_CLIENT_ID"),
        ("spotify", "client_secret", "LIKEARR_SPOTIFY_CLIENT_SECRET"),
    ],
)
@pytest.mark.parametrize("raw", ["secret-value\n", "secret-value\r\n", "  secret-value\r", "\tsecret-value "])
def test_a_secret_is_stripped_of_surrounding_whitespace_when_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, section: str, key: str, env: str, raw: str
) -> None:
    """A Windows-line-ending env file or a file-based Kubernetes Secret leaves a CR or LF
    on the value, which h11 then refuses to send - quoting the whole value in its error."""
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)
    monkeypatch.setenv(env, raw)

    assert getattr(getattr(config, section), key) == "secret-value"


@pytest.mark.parametrize(
    ("section", "key", "env"),
    [
        ("lidarr", "api_key", "LIKEARR_LIDARR_API_KEY"),
        ("spotify", "client_id", "LIKEARR_SPOTIFY_CLIENT_ID"),
    ],
)
def test_a_required_secret_that_is_only_whitespace_is_not_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, section: str, key: str, env: str
) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)
    monkeypatch.setenv(env, " \r\n")

    with pytest.raises(ConfigError, match=f"{env} is not set"):
        _ = getattr(getattr(config, section), key)


def test_a_client_secret_that_is_only_whitespace_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_SECRET", "\r\n")

    assert config.spotify.client_secret is None


@pytest.mark.parametrize(
    ("section", "key", "env"),
    [
        ("lidarr", "api_key", "LIKEARR_LIDARR_API_KEY"),
        ("spotify", "client_id", "LIKEARR_SPOTIFY_CLIENT_ID"),
        ("spotify", "client_secret", "LIKEARR_SPOTIFY_CLIENT_SECRET"),
    ],
)
def test_a_secret_in_the_toml_is_refused_pointing_at_its_env_var_without_echoing_it(
    tmp_path: Path, section: str, key: str, env: str
) -> None:
    """Config only ever trusts the env var, so a secret written into the file is never read. That
    makes it an unknown key, which fails the load - naming the env var, never the value."""
    raw = _raw(**{section: {key: "SECRET-SENTINEL"}})

    with pytest.raises(ConfigError) as caught:
        parse_config(raw, base_dir=tmp_path)

    assert f"[{section}] unknown key {key!r}" in str(caught.value)
    assert env in str(caught.value)
    assert "SECRET-SENTINEL" not in str(caught.value)


def test_a_mqtt_password_in_the_toml_is_refused_pointing_at_its_env_var(tmp_path: Path) -> None:
    raw = _raw(health={"mqtt": {"host": "broker", "topic": "t", "password": "SECRET-SENTINEL"}})

    with pytest.raises(ConfigError, match="LIKEARR_MQTT_PASSWORD") as caught:
        parse_config(raw, base_dir=tmp_path)

    assert "SECRET-SENTINEL" not in str(caught.value)


def test_secrets_are_read_from_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", "real-key")

    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.lidarr.api_key == "real-key"


# ---------------------------------------------------------------- mqtt / webhook optional sections


def test_health_defaults_have_no_mqtt_or_webhook(tmp_path: Path) -> None:
    config = parse_config(MINIMAL_RAW, base_dir=tmp_path)

    assert config.health.mqtt is None
    assert config.health.webhook is None
    assert config.health.stdout is True


def test_health_mqtt_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = _raw(health={"mqtt": {"host": "mqtt.local", "topic": "likearr/health"}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.mqtt is not None
    assert config.health.mqtt.host == "mqtt.local"
    assert config.health.mqtt.topic == "likearr/health"
    assert config.health.mqtt.port == 1883
    assert config.health.mqtt.retain is True

    monkeypatch.delenv("LIKEARR_MQTT_USERNAME", raising=False)
    assert config.health.mqtt.username is None
    monkeypatch.setenv("LIKEARR_MQTT_USERNAME", "mq-user")
    assert config.health.mqtt.username == "mq-user"


def test_health_mqtt_section_overrides(tmp_path: Path) -> None:
    raw = _raw(health={"mqtt": {"host": "mqtt.local", "topic": "t", "port": 8883, "retain": False}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.mqtt is not None
    assert config.health.mqtt.port == 8883
    assert config.health.mqtt.retain is False


def test_health_mqtt_missing_required_key_raises(tmp_path: Path) -> None:
    raw = _raw(health={"mqtt": {"host": "mqtt.local"}})

    with pytest.raises(ConfigError, match=r"\[health\.mqtt\] is missing required key 'topic'"):
        parse_config(raw, base_dir=tmp_path)


def test_health_webhook_section(tmp_path: Path) -> None:
    raw = _raw(health={"webhook": {"url": "https://example.invalid/hook"}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.webhook is not None
    assert config.health.webhook.url == "https://example.invalid/hook"
    assert config.health.webhook.timeout_s == 10.0


def test_health_webhook_section_overrides(tmp_path: Path) -> None:
    raw = _raw(health={"webhook": {"url": "https://example.invalid/hook", "timeout_s": 3}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.webhook is not None
    assert config.health.webhook.timeout_s == 3.0


def test_health_webhook_notify_defaults_to_always(tmp_path: Path) -> None:
    """An existing `[health.webhook]` with no `notify` keeps posting every run, as before."""
    raw = _raw(health={"webhook": {"url": "https://example.invalid/hook"}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.webhook is not None
    assert config.health.webhook.notify == "always"


@pytest.mark.parametrize("value", ["always", "problems"])
def test_health_webhook_notify_accepts_always_and_problems(tmp_path: Path, value: str) -> None:
    raw = _raw(health={"webhook": {"url": "https://example.invalid/hook", "notify": value}})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.webhook is not None
    assert config.health.webhook.notify == value


@pytest.mark.parametrize("value", ["errors", "Problems", "", True, 1])
def test_health_webhook_notify_refuses_anything_else(tmp_path: Path, value: object) -> None:
    raw = _raw(health={"webhook": {"url": "https://example.invalid/hook", "notify": value}})

    with pytest.raises(
        ConfigError, match=r"\[health\.webhook\] notify must be one of 'always', 'problems', got "
    ) as excinfo:
        parse_config(raw, base_dir=tmp_path)

    assert repr(value) in str(excinfo.value)


def test_health_webhook_missing_required_key_raises(tmp_path: Path) -> None:
    raw = _raw(health={"webhook": {}})

    with pytest.raises(ConfigError, match=r"\[health\.webhook\] is missing required key 'url'"):
        parse_config(raw, base_dir=tmp_path)


def test_health_stdout_can_be_disabled(tmp_path: Path) -> None:
    raw = _raw(health={"stdout": False})

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.stdout is False


def test_health_both_mqtt_and_webhook(tmp_path: Path) -> None:
    raw = _raw(
        health={
            "mqtt": {"host": "mqtt.local", "topic": "t"},
            "webhook": {"url": "https://example.invalid/hook"},
        }
    )

    config = parse_config(raw, base_dir=tmp_path)

    assert config.health.mqtt is not None
    assert config.health.webhook is not None


# ---------------------------------------------------------------- liked_track_scope


def test_liked_track_scope_rejects_an_unknown_value(tmp_path: Path) -> None:
    """A typo must fail the run at config time, not quietly resolve like 'album'."""
    with pytest.raises(ConfigError, match=r"\[rules\] liked_track_scope must be one of 'album', 'smallest'"):
        parse_config(_raw(rules={"liked_track_scope": "smallest-release"}), base_dir=tmp_path)


def test_liked_track_scope_accepts_both_documented_values(tmp_path: Path) -> None:
    for value in ("album", "smallest"):
        config = parse_config(_raw(rules={"liked_track_scope": value}), base_dir=tmp_path)
        assert config.rules.liked_track_scope == value


# ---------------------------------------------------------------- the opt-outs


def test_the_opt_outs_default_to_the_behaviour_that_shipped_before_them(tmp_path: Path) -> None:
    """A config that never heard of the opt-outs must resolve exactly as it did before them."""
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.rules.allow_compilation_fallback is True
    assert config.rules.allow_remix_releases is True
    assert config.rules.keep_remix_only_tracks is True
    assert config.rules.deny_releases == ()
    assert config.rules.exclusions.token == "", "the defaults must not re-resolve or re-baseline"


def test_flipping_either_switch_moves_the_token(tmp_path: Path) -> None:
    compilations = parse_config(_raw(rules={"allow_compilation_fallback": False}), base_dir=tmp_path)
    remixes = parse_config(_raw(rules={"allow_remix_releases": False}), base_dir=tmp_path)

    assert compilations.rules.exclusions.token == "c0r1"
    assert remixes.rules.exclusions.token == "c1r0"
    assert compilations.rules.exclusions.token != remixes.rules.exclusions.token


def test_keep_remix_only_tracks_reaches_the_core_and_moves_the_token(tmp_path: Path) -> None:
    """On by default, so a `c1r0` library keeps its token; off is its own token."""
    kept = parse_config(_raw(rules={"allow_remix_releases": False}), base_dir=tmp_path)
    dropped = parse_config(
        _raw(rules={"allow_remix_releases": False, "keep_remix_only_tracks": False}), base_dir=tmp_path
    )

    assert kept.rules.exclusions.keep_remix_only_tracks is True
    assert dropped.rules.exclusions.keep_remix_only_tracks is False
    assert kept.rules.exclusions.token == "c1r0"
    assert dropped.rules.exclusions.token == "c1r0k0"


def test_a_deny_list_is_lowercased_deduplicated_and_sorted(tmp_path: Path) -> None:
    raw = _raw(
        rules={
            "deny_releases": [
                "8832AD43-EA78-467F-B23F-F0148D615485",
                "2f26958e-b86d-3b3c-8a15-57253046ea58",
                "8832ad43-ea78-467f-b23f-f0148d615485",
            ]
        }
    )

    config = parse_config(raw, base_dir=tmp_path)

    assert config.rules.deny_releases == (
        "2f26958e-b86d-3b3c-8a15-57253046ea58",
        "8832ad43-ea78-467f-b23f-f0148d615485",
    )


def test_a_deny_list_entry_that_is_not_an_mbid_fails_the_run(tmp_path: Path) -> None:
    """A typo would otherwise refuse nothing at all, for ever, and say nothing about it."""
    with pytest.raises(ConfigError, match="is not a MusicBrainz release group id"):
        parse_config(_raw(rules={"deny_releases": ["The Complete Decca Masters"]}), base_dir=tmp_path)


def test_a_deny_list_that_is_not_a_list_fails_the_run(tmp_path: Path) -> None:
    """A bare string is 36 iterable characters, which would otherwise validate one at a time."""
    with pytest.raises(ConfigError, match=r"\[rules\] deny_releases must be a list"):
        parse_config(
            _raw(rules={"deny_releases": "2f26958e-b86d-3b3c-8a15-57253046ea58"}),
            base_dir=tmp_path,
        )


def test_the_deny_list_is_not_part_of_the_token(tmp_path: Path) -> None:
    """Adding one MBID must not re-resolve ~2,000 liked tracks to move a handful of them."""
    config = parse_config(
        _raw(rules={"deny_releases": ["2f26958e-b86d-3b3c-8a15-57253046ea58"]}),
        base_dir=tmp_path,
    )

    assert config.rules.exclusions.token == ""
    assert config.rules.exclusions.deny_releases == frozenset({"2f26958e-b86d-3b3c-8a15-57253046ea58"})


def test_refresh_timeout_scales_with_the_catalogue_and_never_drops_below_the_floor() -> None:
    lidarr = LidarrConfig(
        url="http://l", root_folder="/m", quality_profile="q",
        refresh_timeout_s=300.0, refresh_per_album_s=2.0, refresh_timeout_max_s=3600.0,
    )  # fmt: skip

    assert lidarr.refresh_timeout_for(0) == 300.0, "the floor is what an unknown catalogue gets"
    assert lidarr.refresh_timeout_for(10) == 320.0
    assert lidarr.refresh_timeout_for(1000) == 2300.0


def test_refresh_timeout_stops_at_the_ceiling() -> None:
    lidarr = LidarrConfig(
        url="http://l", root_folder="/m", quality_profile="q",
        refresh_timeout_s=300.0, refresh_per_album_s=2.0, refresh_timeout_max_s=3600.0,
    )  # fmt: skip

    assert lidarr.refresh_timeout_for(100_000) == 3600.0


def test_a_ceiling_below_the_floor_never_shortens_the_floor() -> None:
    lidarr = LidarrConfig(
        url="http://l", root_folder="/m", quality_profile="q",
        refresh_timeout_s=300.0, refresh_per_album_s=2.0, refresh_timeout_max_s=100.0,
    )  # fmt: skip

    assert lidarr.refresh_timeout_for(500) == 300.0


@pytest.mark.parametrize(
    "key", ["refresh_per_album_s", "refresh_timeout_max_s", "max_refreshes_per_run", "recent_gap_refresh_hours"]
)
def test_negative_refresh_settings_are_refused(tmp_path: Path, key: str) -> None:
    raw = _raw(lidarr={key: -1})

    with pytest.raises(ConfigError, match=key):
        parse_config(raw, base_dir=tmp_path)


@pytest.mark.parametrize(
    ("section", "key"),
    [("rules", "recent_release_days"), ("musicbrainz", "positive_cache_days")],
)
def test_negative_day_windows_are_refused(tmp_path: Path, section: str, key: str) -> None:
    raw = _raw(**{section: {key: -1}})

    with pytest.raises(ConfigError, match=key):
        parse_config(raw, base_dir=tmp_path)


# ---------------------------------------------------------------- the plan fingerprint


DENIED = "0b9c1c6e-5b1a-4d1e-9f2a-3c4d5e6f7a8b"


def test_the_plan_fingerprint_names_every_rules_and_guards_setting(tmp_path: Path) -> None:
    """A diff is only as good as the config it was planned under; all of it has to be recorded."""
    config = parse_config(_raw(rules={"deny_releases": [DENIED]}), base_dir=tmp_path)
    fingerprint = config.plan_fingerprint

    assert set(fingerprint) == {"rules", "guards"}
    assert fingerprint["rules"]["liked_track_scope"] == "album"
    assert fingerprint["rules"]["deny_releases"] == [DENIED]
    assert fingerprint["rules"]["allow_compilation_fallback"] is True
    assert fingerprint["guards"]["max_unmonitors_scheduled"] == 100


def test_the_plan_fingerprint_survives_a_json_round_trip(tmp_path: Path) -> None:
    """It is compared against a copy read back from diff.json, so no tuple may become a list."""
    import json

    config = parse_config(_raw(rules={"deny_releases": [DENIED]}), base_dir=tmp_path)

    assert json.loads(json.dumps(config.plan_fingerprint)) == config.plan_fingerprint


def test_denying_a_release_moves_the_plan_fingerprint(tmp_path: Path) -> None:
    """The case the whole fingerprint exists for: `ExclusionRules.token` alone does not see it."""
    before = parse_config(_raw(), base_dir=tmp_path)
    after = parse_config(_raw(rules={"deny_releases": [DENIED]}), base_dir=tmp_path)

    assert before.rules.exclusions.token == after.rules.exclusions.token
    assert before.plan_fingerprint != after.plan_fingerprint


# ---------------------------------------------------------------- [ui] (web UI)


def test_the_ui_block_is_optional_and_no_allowed_hosts_is_the_unset_default(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.allowed_hosts == ()
    assert config.ui.errors == ()


def test_the_ui_block_is_read(tmp_path: Path) -> None:
    config = parse_config(
        _raw(ui={"cli_command": "ssh host likearr", "public_url": "https://likearr.example.org"}),
        base_dir=tmp_path,
    )

    assert config.ui.cli_command == "ssh host likearr"
    assert config.ui.public_url == "https://likearr.example.org"


@pytest.mark.parametrize(
    ("ui", "problem"),
    [
        ({"cli_command": ""}, r"\[ui\] cli_command"),
        ({"public_url": "http://likearr.example.org"}, r"\[ui\] public_url"),
        ({"lidarr_url": "ftp://x"}, r"\[ui\] lidarr_url"),
        ({"allowed_host": ["x"]}, r"\[ui\] unknown key 'allowed_host'"),
    ],
)
def test_a_bad_ui_block_is_a_recorded_problem_never_a_load_failure(
    tmp_path: Path, ui: dict[str, object], problem: str
) -> None:
    # [ui] is only read by `likearr start`. A typo in it must never stop a run - the cron run loads
    # this same file - so it is recorded for the server to refuse on, and everything else loads.
    config = parse_config(_raw(ui=ui), base_dir=tmp_path)

    assert config.ui.errors
    assert any(re.search(problem, e) for e in config.ui.errors)
    assert config.lidarr.url == "http://lidarr:8686"


def test_a_bad_ui_value_falls_back_to_its_default(tmp_path: Path) -> None:
    config = parse_config(_raw(ui={"cli_command": ""}), base_dir=tmp_path)

    assert config.ui.cli_command == "docker compose run --rm likearr-cli"
    assert len(config.ui.errors) == 1


def test_a_good_ui_block_has_no_problems(tmp_path: Path) -> None:
    config = parse_config(_raw(ui={"public_url": "https://a.b"}), base_dir=tmp_path)

    assert config.ui.errors == ()


def test_the_ui_block_is_not_part_of_the_plan_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = parse_config(_raw(), base_dir=tmp_path)
    monkeypatch.setenv("LIKEARR_ALLOWED_HOSTS", "a.b")
    after = parse_config(_raw(ui={"cli_command": "x"}), base_dir=tmp_path)

    assert before.plan_fingerprint == after.plan_fingerprint


# ---------------------------------------------------------------- [schedule] cron/timezone


def test_the_schedule_block_defaults_to_every_six_hours_utc(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.schedule.cron == "20 */6 * * *"
    assert config.schedule.timezone == "UTC"


def test_the_schedule_block_cron_and_timezone_are_read(tmp_path: Path) -> None:
    config = parse_config(_raw(schedule={"cron": "0 * * * *", "timezone": "America/New_York"}), base_dir=tmp_path)

    assert config.schedule.cron == "0 * * * *"
    assert config.schedule.timezone == "America/New_York"


@pytest.mark.parametrize(
    ("schedule", "problem"),
    [
        ({"cron": "every six hours"}, r"\[schedule\] cron"),
        ({"cron": "@daily"}, r"\[schedule\] cron"),
        ({"cron": "20 */6 * * * /usr/bin/flock -n /var/run/likearr.flock likearr run"}, r"\[schedule\] cron"),
        ({"cron": "\u00b2 * * * *"}, r"\[schedule\] cron"),
        ({"timezone": "America/Nowhere"}, r"\[schedule\] timezone"),
    ],
)
def test_a_bad_schedule_cron_or_timezone_fails_the_load(
    tmp_path: Path, schedule: dict[str, object], problem: str
) -> None:
    # Unlike [ui] (informational only in phase 1), [schedule] drives the scheduler itself - a
    # typo here must not silently pick some other schedule.
    with pytest.raises(ConfigError, match=problem):
        parse_config(_raw(schedule=schedule), base_dir=tmp_path)


@pytest.mark.parametrize(
    ("cron", "timezone", "problem"),
    [
        ("every six hours", "UTC", r"\[schedule\] cron"),
        ("*/30 * * * *", "UTC", r"\[schedule\] cron.*every \d+ minutes"),
        ("0 * * * *", "America/Nowhere", r"\[schedule\] timezone"),
    ],
)
def test_validate_cron_and_timezone_matches_what_the_load_would_refuse(
    tmp_path: Path, cron: str, timezone: str, problem: str
) -> None:
    """`web.settings.preview_schedule`'s live preview calls `validate_cron_and_timezone` directly,
    rather than going through a full `parse_config` - this checks the two paths agree:
    same exception type, same message, for the same bad cron line or timezone.
    """
    with pytest.raises(ConfigError, match=problem) as direct:
        validate_cron_and_timezone(cron, timezone)
    with pytest.raises(ConfigError, match=problem) as via_load:
        parse_config(_raw(schedule={"cron": cron, "timezone": timezone}), base_dir=tmp_path)

    assert str(direct.value) == str(via_load.value)


def test_the_old_ui_cron_keys_are_unknown_ui_keys_not_a_schedule(tmp_path: Path) -> None:
    """`[ui] cron_schedule` / `cron_timezone` were aliases for `[schedule] cron` / `timezone`
    and are gone. They are now unknown `[ui]` keys like any other - recorded problems, which the
    server refuses to start on - and never quietly read as the schedule."""
    config = parse_config(
        _raw(ui={"cron_schedule": "0 * * * *", "cron_timezone": "America/New_York"}), base_dir=tmp_path
    )

    assert "[ui] unknown key 'cron_schedule'" in config.ui.errors
    assert "[ui] unknown key 'cron_timezone'" in config.ui.errors
    assert config.schedule.cron == "20 */6 * * *"
    assert config.schedule.timezone == "UTC"


def test_a_schedule_block_beside_ui_keys_loads_unchanged(tmp_path: Path) -> None:
    config = parse_config(
        _raw(
            ui={"lidarr_url": "https://lidarr.example.org"},
            schedule={"cron": "30 3 * * *", "timezone": "America/New_York"},
        ),
        base_dir=tmp_path,
    )

    assert config.schedule.cron == "30 3 * * *"
    assert config.schedule.timezone == "America/New_York"
    assert config.ui.errors == ()


def test_a_cron_that_fires_more_often_than_the_minimum_interval_fails_the_load(tmp_path: Path) -> None:
    # A scheduled run reads all of Spotify; a tight schedule burns through the daily dev-mode
    # quota (see MIN_SCHEDULE_INTERVAL_MINUTES's docstring).
    with pytest.raises(ConfigError, match=r"\[schedule\] cron.*every \d+ minutes"):
        parse_config(_raw(schedule={"cron": "*/30 * * * *"}), base_dir=tmp_path)


def test_a_cron_exactly_at_the_minimum_interval_is_fine(tmp_path: Path) -> None:
    config = parse_config(_raw(schedule={"cron": "0 * * * *"}), base_dir=tmp_path)  # hourly: exactly 60 minutes

    assert config.schedule.cron == "0 * * * *"
    assert MIN_SCHEDULE_INTERVAL_MINUTES == 60


def test_the_schedule_cron_and_timezone_are_not_part_of_the_plan_fingerprint(tmp_path: Path) -> None:
    before = parse_config(_raw(), base_dir=tmp_path)
    after = parse_config(_raw(schedule={"cron": "0 * * * *"}), base_dir=tmp_path)

    assert before.plan_fingerprint == after.plan_fingerprint


def test_the_run_lock_defaults_to_beside_the_state_database(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.lock_path == tmp_path / "state.lock"


def test_a_configured_run_lock_wins(tmp_path: Path) -> None:
    config = parse_config(_raw(state={"db": "state.sqlite", "lock_file": "likearr.lock"}), base_dir=tmp_path)

    assert config.lock_path == tmp_path / "likearr.lock"


@pytest.mark.parametrize("zone", ["America", "x" * 300, "../etc", "/etc/passwd", "America/"])
def test_a_timezone_zoneinfo_chokes_on_fails_the_load_under_schedule(tmp_path: Path, zone: str) -> None:
    # ZoneInfo raises IsADirectoryError for "America" and OSError for an over-long name: neither may
    # escape parse_config. An explicitly empty string is treated as "not set" (the default applies)
    # rather than a bad zone - see the next test.
    with pytest.raises(ConfigError, match=r"\[schedule\] timezone"):
        parse_config(_raw(schedule={"timezone": zone}), base_dir=tmp_path)


def test_an_explicitly_empty_schedule_timezone_uses_the_default(tmp_path: Path) -> None:
    config = parse_config(_raw(schedule={"timezone": ""}), base_dir=tmp_path)

    assert config.schedule.timezone == "UTC"


def test_lidarr_links_default_to_the_lidarr_url(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.lidarr_url == "http://lidarr:8686"


def test_lidarr_links_can_use_the_browser_facing_address(tmp_path: Path) -> None:
    config = parse_config(_raw(ui={"lidarr_url": "https://lidarr.example.org/"}), base_dir=tmp_path)

    assert config.ui.lidarr_url == "https://lidarr.example.org"
    assert config.ui.errors == ()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "lidarr.example.org", "ftp://x", "", "https://"])
def test_a_lidarr_url_that_is_not_a_web_address_is_a_problem(tmp_path: Path, url: str) -> None:
    config = parse_config(_raw(ui={"lidarr_url": url}), base_dir=tmp_path)

    assert config.ui.lidarr_url == "http://lidarr:8686"
    assert any("lidarr_url" in e for e in config.ui.errors)


def test_an_unset_lidarr_url_is_never_blamed_for_the_lidarr_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_URL", "lidarr:8686")

    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.errors == ()


@pytest.mark.parametrize(
    "url",
    ["http://[::1", "https://h?x=1", "https://h#f", "http://:80", "https:///path", "http://user@"],
)
def test_a_lidarr_url_urlsplit_chokes_on_or_that_carries_more_than_an_address_is_a_problem(
    tmp_path: Path, url: str
) -> None:
    config = parse_config(_raw(ui={"lidarr_url": url}), base_dir=tmp_path)

    assert config.ui.lidarr_url == "http://lidarr:8686"
    assert any("lidarr_url" in e for e in config.ui.errors)


def test_a_fallback_lidarr_url_a_browser_cannot_use_means_no_lidarr_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_URL", "lidarr:8686")
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.lidarr_url == ""
    assert config.ui.errors == ()


def test_an_unknown_ui_key_is_a_recorded_problem_never_a_load_failure(tmp_path: Path) -> None:
    # A misspelt key must never do nothing in silence. [ui] stays checked-but-never-fatal,
    # so the cron run still loads; `start` refuses on the recorded problem and names it.
    config = parse_config(_raw(ui={"public_ur": "https://a.b"}), base_dir=tmp_path)

    assert config.ui.errors == ("[ui] unknown key 'public_ur' (did you mean 'public_url'?)",)
    assert config.lidarr.url == "http://lidarr:8686"


# ---------------------------------------------------------------- Clean up's commands


def test_the_cli_command_defaults_to_the_documented_install(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.cli_command == "docker compose run --rm likearr-cli"
    assert config.ui.errors == ()


def test_a_cli_command_is_one_line(tmp_path: Path) -> None:
    ok = parse_config(_raw(ui={"cli_command": "  ssh nas docker exec likearr likearr "}), base_dir=tmp_path)
    bad = parse_config(_raw(ui={"cli_command": "likearr\nrm -rf /"}), base_dir=tmp_path)
    rtl = parse_config(_raw(ui={"cli_command": "likearr \u202eevil"}), base_dir=tmp_path)
    c1 = parse_config(_raw(ui={"cli_command": "likearr \x9b31m"}), base_dir=tmp_path)
    empty = parse_config(_raw(ui={"cli_command": ""}), base_dir=tmp_path)
    long = parse_config(_raw(ui={"cli_command": "x" * 201}), base_dir=tmp_path)

    assert ok.ui.cli_command == "ssh nas docker exec likearr likearr"
    for config in (bad, empty, long, rtl, c1):
        assert config.ui.cli_command == "docker compose run --rm likearr-cli"
        assert any("cli_command" in e for e in config.ui.errors)


# ---------------------------------------------------------------- [ui] public_url


def test_public_url_defaults_to_empty(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.ui.public_url == ""
    assert config.ui.errors == ()


def test_public_url_accepts_an_https_address(tmp_path: Path) -> None:
    config = parse_config(_raw(ui={"public_url": "https://likearr.example.org/"}), base_dir=tmp_path)

    assert config.ui.public_url == "https://likearr.example.org"
    assert config.ui.errors == ()


@pytest.mark.parametrize(
    "url",
    ["http://likearr.example.org", "likearr.example.org", "https://h?x=1", "https://h#f", "https://", ""],
)
def test_a_public_url_that_is_not_https_is_a_problem(tmp_path: Path, url: str) -> None:
    config = parse_config(_raw(ui={"public_url": url}), base_dir=tmp_path)

    assert config.ui.public_url == ""
    assert any("public_url" in e for e in config.ui.errors)


def test_a_public_url_with_a_path_is_a_problem(tmp_path: Path) -> None:
    """`spotify_connect_start` appends "/spotify/callback" to this exact value to build the
    redirect_uri, and the route is registered at that literal path with no prefix - a
    `public_url` carrying its own path would silently build a redirect_uri nothing serves."""
    config = parse_config(_raw(ui={"public_url": "https://likearr.example.org/some/prefix"}), base_dir=tmp_path)

    assert config.ui.public_url == ""
    assert any("public_url" in e and "no path" in e for e in config.ui.errors)


def test_public_url_is_outside_the_plan_fingerprint(tmp_path: Path) -> None:
    with_url = parse_config(_raw(ui={"public_url": "https://likearr.example.org"}), base_dir=tmp_path)
    without = parse_config(_raw(), base_dir=tmp_path)

    assert with_url.plan_fingerprint == without.plan_fingerprint


def test_the_holding_dir_defaults_beside_the_root_folder_never_inside(tmp_path: Path) -> None:
    config = parse_config(_raw(lidarr={"root_folder": "/data/media/music/"}), base_dir=tmp_path)

    assert config.holding_dir == "/data/media/_likearr-holding"
    assert config.prune.errors == ()


def test_a_holding_dir_can_be_set(tmp_path: Path) -> None:
    config = parse_config(_raw(prune={"holding_dir": "/srv/holding/"}), base_dir=tmp_path)

    assert config.holding_dir == "/srv/holding"


@pytest.mark.parametrize(
    "value", ["/music/_likearr-holding", "/music", "relative/holding", "", 7, "/x\n/y", "/srv/../music/hold"]
)
def test_a_holding_dir_inside_the_library_or_not_absolute_is_a_problem_never_fatal(tmp_path: Path, value) -> None:
    config = parse_config(_raw(prune={"holding_dir": value}), base_dir=tmp_path)

    assert config.holding_dir == "/_likearr-holding"  # beside /music
    (problem,) = config.prune.errors
    assert "[prune] holding_dir" in problem


def test_clean_up_is_off_by_default(tmp_path: Path) -> None:
    """Clean up is opt-in. No `[prune]` table, or one without `enabled`, means off."""
    assert parse_config(_raw(), base_dir=tmp_path).prune.enabled is False
    config = parse_config(_raw(prune={"holding_dir": "/srv/holding"}), base_dir=tmp_path)
    assert config.prune.enabled is False
    assert config.prune.errors == ()


def test_clean_up_can_be_turned_on_with_no_prune_problem(tmp_path: Path) -> None:
    config = parse_config(_raw(prune={"enabled": True, "holding_dir": "/srv/holding"}), base_dir=tmp_path)

    assert config.prune.enabled is True
    assert config.prune.errors == ()
    assert config.holding_dir == "/srv/holding"


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, [True]])
def test_a_prune_enabled_that_is_not_a_boolean_is_a_problem_and_off_never_fatal(tmp_path: Path, value: object) -> None:
    config = parse_config(_raw(prune={"enabled": value}), base_dir=tmp_path)

    assert config.prune.enabled is False
    (problem,) = config.prune.errors
    assert problem.startswith("[prune] enabled must be true or false")


def test_a_bad_prune_enabled_and_a_bad_holding_dir_are_both_recorded(tmp_path: Path) -> None:
    config = parse_config(_raw(prune={"enabled": "yes", "holding_dir": "relative"}), base_dir=tmp_path)

    assert config.prune.enabled is False
    assert config.holding_dir == "/_likearr-holding"
    assert [e.split(" ")[1] for e in config.prune.errors] == ["enabled", "holding_dir"]


def test_prune_enabled_is_outside_the_plan_fingerprint(tmp_path: Path) -> None:
    on = parse_config(_raw(prune={"enabled": True}), base_dir=tmp_path)

    assert on.plan_fingerprint == parse_config(_raw(), base_dir=tmp_path).plan_fingerprint


# ---------------------------------------------------------------- [schedule]


def test_the_schedule_block_is_optional_and_defaults_to_enabled(tmp_path: Path) -> None:
    config = parse_config(_raw(), base_dir=tmp_path)

    assert config.schedule.enabled is True
    assert config.schedule.paused_reason == ""
    assert config.schedule.paused_at is None


@pytest.mark.parametrize("value", ["false", "no", 0, 1])
def test_a_schedule_enabled_that_is_not_a_boolean_fails_the_load(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigError, match=r"\[schedule\] enabled"):
        parse_config(_raw(schedule={"enabled": value}), base_dir=tmp_path)


def test_the_schedule_block_is_read(tmp_path: Path) -> None:
    config = parse_config(
        _raw(
            schedule={"enabled": False, "paused_reason": "maintenance window", "paused_at": "2026-01-05T18:00:00+00:00"}
        ),
        base_dir=tmp_path,
    )

    assert config.schedule.enabled is False
    assert config.schedule.paused_reason == "maintenance window"
    assert config.schedule.paused_at is not None
    assert config.schedule.paused_at.isoformat() == "2026-01-05T18:00:00+00:00"


def test_a_native_toml_datetime_is_read_for_paused_at(tmp_path: Path) -> None:
    """What `tomlkit` writes when the UI pauses - a real TOML datetime, not a string."""
    import tomllib

    text = (
        '[lidarr]\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[state]\ndb = "state.sqlite"\n[schedule]\nenabled = false\npaused_at = 2026-01-05T18:00:00Z\n'
    )
    config = parse_config(tomllib.loads(text), base_dir=tmp_path)

    assert config.schedule.paused_at is not None
    assert config.schedule.paused_at.year == 2026


def test_a_naive_paused_at_is_read_as_utc(tmp_path: Path) -> None:
    config = parse_config(_raw(schedule={"paused_at": "2026-01-05T18:00:00"}), base_dir=tmp_path)

    from datetime import UTC

    assert config.schedule.paused_at is not None
    assert config.schedule.paused_at.tzinfo == UTC


def test_a_bad_paused_at_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[schedule\] paused_at"):
        parse_config(_raw(schedule={"paused_at": "not a date"}), base_dir=tmp_path)


def test_a_paused_reason_over_the_line_limit_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[schedule\] paused_reason"):
        parse_config(_raw(schedule={"paused_reason": "x" * 201}), base_dir=tmp_path)


def test_a_paused_reason_with_a_control_character_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[schedule\] paused_reason"):
        parse_config(_raw(schedule={"paused_reason": "line one\nline two"}), base_dir=tmp_path)


def test_the_schedule_block_is_not_part_of_the_plan_fingerprint(tmp_path: Path) -> None:
    before = parse_config(_raw(), base_dir=tmp_path)
    after = parse_config(_raw(schedule={"enabled": False, "paused_reason": "testing"}), base_dir=tmp_path)

    assert before.plan_fingerprint == after.plan_fingerprint


# ---------------------------------------------------------------- deploy/config.example.toml


def test_the_example_config_loads_as_written_leaving_only_the_library_to_pick() -> None:
    """A first start writes this file verbatim, so it must load as it is, and must not
    carry a placeholder root folder or quality profile that would be taken for a real choice."""
    config = load_config(EXAMPLE_CONFIG_PATH)

    assert config.ui.errors == ()
    assert config.prune.errors == ()
    assert config.lidarr.unset == ("[lidarr] root_folder", "[lidarr] quality_profile")
    assert config.spotify.token_file.name == "spotify-token.json"
    assert config.state_db.name == "state.sqlite"


# ---------------------------------------------------------------- strict validation


def _load_example_with(tmp_path: Path, old: str, new: str) -> Config:
    """The example config with one line edited, loaded the way a run loads it."""
    text = EXAMPLE_CONFIG_PATH.read_text()
    assert old in text, old
    path = tmp_path / "config.toml"
    path.write_text(text.replace(old, new, 1))
    return load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("[guards]", "[guard]", r"unknown section \[guard\] \(did you mean \[guards\]\?\)"),
        (
            "max_unmonitors_scheduled = 100",
            "max_unmonitor_scheduled = 100",
            r"\[guards\] unknown key 'max_unmonitor_scheduled' \(did you mean 'max_unmonitors_scheduled'\?\)",
        ),
        (
            "deny_releases = []",
            'deny_release = ["0b9c1c6e-5b1a-4d1e-9f2a-3c4d5e6f7a8b"]',
            r"\[rules\] unknown key 'deny_release' \(did you mean 'deny_releases'\?\)",
        ),
        (
            "max_unmonitors_scheduled = 100",
            'max_unmonitors_scheduled = "lots"',
            r"\[guards\] max_unmonitors_scheduled must be a whole number >= 0, got 'lots'",
        ),
        (
            "source_shrink_pct = 10.0",
            'source_shrink_pct = "20%"',
            r"\[guards\] source_shrink_pct must be a number between 0 and 100, got '20%'",
        ),
        (
            "max_unmonitors_scheduled = 100",
            "max_unmonitors_scheduled = -5",
            r"\[guards\] max_unmonitors_scheduled must be a whole number >= 0, got -5",
        ),
        (
            "allow_remix_releases = true",
            'allow_remix_releases = "false"',
            r"\[rules\] allow_remix_releases must be true or false, got 'false'",
        ),
        ("source_shrink_pct = 10.0", "source_shrink_pct = nan", r"\[guards\] source_shrink_pct must be a number"),
        ("artist_shrink_pct = 30.0", "artist_shrink_pct = inf", r"\[guards\] artist_shrink_pct must be a number"),
        ("unmapped_ratio_amber = 0.05", "unmapped_ratio_amber = -inf", r"\[guards\] unmapped_ratio_amber"),
    ],
)
def test_each_audit_probe_fails_the_load_naming_the_section_and_key(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        _load_example_with(tmp_path, old, new)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("guards", "projected_wanted_max", -1, r"\[guards\] projected_wanted_max must be a whole number >= 0"),
        ("guards", "source_shrink_pct", 101, r"\[guards\] source_shrink_pct must be a number between 0 and 100"),
        ("guards", "artist_shrink_pct", -0.5, r"\[guards\] artist_shrink_pct must be a number between 0 and 100"),
        ("guards", "unmapped_ratio_amber", 1.5, r"\[guards\] unmapped_ratio_amber must be a number between 0 and 1"),
        ("guards", "max_unmonitors_scheduled", 2.5, r"\[guards\] max_unmonitors_scheduled must be a whole number"),
        ("guards", "max_unmonitors_scheduled", True, r"\[guards\] max_unmonitors_scheduled must be a whole number"),
        ("musicbrainz", "negative_cache_days", -1, r"\[musicbrainz\] negative_cache_days must be a whole number >= 0"),
        ("rules", "singles_fallback_days", -1, r"\[rules\] singles_fallback_days must be a whole number >= 0"),
        ("lidarr", "refresh_timeout_s", "slow", r"\[lidarr\] refresh_timeout_s must be a number, got 'slow'"),
        ("lidarr", "refresh_timeout_s", 10**400, r"\[lidarr\] refresh_timeout_s must be a number"),
        ("health", "stdout", "yes", r"\[health\] stdout must be true or false"),
        ("spotify", "liked_tracks", 1, r"\[spotify\] liked_tracks must be true or false"),
    ],
)
def test_a_wrong_type_or_out_of_range_value_names_its_key(
    tmp_path: Path, section: str, key: str, value: object, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(_raw(**{section: {key: value}}), base_dir=tmp_path)


def test_a_whole_float_still_reads_as_a_whole_number(tmp_path: Path) -> None:
    config = parse_config(_raw(guards={"max_unmonitors_scheduled": 50.0}), base_dir=tmp_path)

    assert config.guards.max_unmonitors_scheduled == 50
    assert isinstance(config.guards.max_unmonitors_scheduled, int)


def test_the_range_edges_load(tmp_path: Path) -> None:
    config = parse_config(
        _raw(
            guards={
                "max_unmonitors_scheduled": 0,
                "projected_wanted_max": 0,
                "source_shrink_pct": 100,
                "artist_shrink_pct": 0,
                "unmapped_ratio_amber": 1,
            }
        ),
        base_dir=tmp_path,
    )

    assert config.guards.source_shrink_pct == 100.0
    assert isinstance(config.guards.source_shrink_pct, float)
    assert config.guards.unmapped_ratio_amber == 1.0


def test_an_unknown_key_in_a_health_sink_names_the_sink(tmp_path: Path) -> None:
    raw = _raw(health={"mqtt": {"host": "broker", "topic": "t", "retian": True}})

    with pytest.raises(ConfigError, match=r"\[health\.mqtt\] unknown key 'retian' \(did you mean 'retain'\?\)"):
        parse_config(raw, base_dir=tmp_path)


def test_every_unknown_key_in_a_section_is_named_at_once(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(_raw(guards={"max_unmonitor_scheduled": 1, "zzz": 2}), base_dir=tmp_path)

    assert "'max_unmonitor_scheduled'" in str(caught.value)
    assert "'zzz'" in str(caught.value)


def test_an_unknown_section_with_nothing_close_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown section \[zzzz\]$"):
        parse_config(_raw(zzzz={"a": 1}), base_dir=tmp_path)


def test_a_section_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    raw: dict[str, object] = _raw()
    raw["guards"] = 5

    with pytest.raises(ConfigError, match=r"\[guards\] must be a table"):
        parse_config(raw, base_dir=tmp_path)


def test_an_unknown_prune_key_is_a_recorded_problem_never_a_load_failure(tmp_path: Path) -> None:
    config = parse_config(_raw(prune={"holding_dirs": "/data/holding"}), base_dir=tmp_path)

    assert config.prune.errors == ("[prune] unknown key 'holding_dirs' (did you mean 'holding_dir'?)",)
    assert config.holding_dir == "/_likearr-holding"


def test_min_interval_below_one_second_is_refused_against_musicbrainz_org(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[musicbrainz\] min_interval_s"):
        parse_config(_raw(musicbrainz={"min_interval_s": 0}), base_dir=tmp_path)
    with pytest.raises(ConfigError, match=r"\[musicbrainz\] min_interval_s"):
        parse_config(
            _raw(musicbrainz={"base_url": "https://musicbrainz.org/ws/2/", "min_interval_s": 0.5}), base_dir=tmp_path
        )


def test_a_self_hosted_mirror_may_go_below_one_second(tmp_path: Path) -> None:
    config = parse_config(
        _raw(musicbrainz={"base_url": "http://mb.lan:5000/ws/2", "min_interval_s": 0}), base_dir=tmp_path
    )

    assert config.musicbrainz.min_interval_s == 0.0


def test_one_second_against_musicbrainz_org_loads(tmp_path: Path) -> None:
    config = parse_config(_raw(musicbrainz={"min_interval_s": 1}), base_dir=tmp_path)

    assert config.musicbrainz.min_interval_s == 1.0


def test_the_old_example_contact_placeholder_still_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    # Option B: Doctor warns about it; the load does not refuse it.
    monkeypatch.setenv("LIKEARR_MUSICBRAINZ_CONTACT", PLACEHOLDER_CONTACT)
    config = load_config(EXAMPLE_CONFIG_PATH)

    assert config.musicbrainz.contact == PLACEHOLDER_CONTACT


def test_the_example_config_documents_only_known_keys() -> None:
    raw = tomllib.loads(EXAMPLE_CONFIG_PATH.read_text())

    assert set(raw) <= KNOWN_SECTIONS
    for section, table in raw.items():
        assert set(table) <= KNOWN_KEYS[section], section


def test_known_keys_match_the_dataclass_fields() -> None:
    """The TOML names are written out by hand, since some differ from the fields (`[state] db` is
    `Config.state_db`). This ties the two together so a new setting cannot be read but refused."""

    def names(cls: type, *, minus: tuple[str, ...] = ()) -> set[str]:
        return {f.name for f in dataclasses.fields(cls)} - set(minus)

    # The deployment settings are fields, read from the environment, never keys.
    assert KNOWN_KEYS["lidarr"] == names(LidarrConfig, minus=("url",))
    assert KNOWN_KEYS["spotify"] == names(SpotifyConfig)
    assert KNOWN_KEYS["musicbrainz"] == names(MusicBrainzConfig, minus=("contact",))
    assert KNOWN_KEYS["rules"] == names(RulesConfig)
    assert KNOWN_KEYS["guards"] == names(GuardsConfig)
    assert KNOWN_KEYS["health"] == names(HealthConfig)
    assert KNOWN_KEYS["health.mqtt"] == names(MqttSinkConfig)
    assert KNOWN_KEYS["health.webhook"] == names(WebhookSinkConfig)
    assert KNOWN_KEYS["schedule"] == names(ScheduleConfig)
    assert KNOWN_KEYS["prune"] == names(PruneConfig, minus=("errors",))
    assert KNOWN_KEYS["ui"] == names(UiConfig, minus=("errors", "allowed_hosts"))
    # `[state]` fills two fields of `Config` itself.
    assert KNOWN_KEYS["state"] == {"db", "lock_file"}
    assert {"state_db", "lock_file"} <= names(Config)
    # Every other top-level field of `Config` is a section of the same name.
    assert (names(Config) - {"state_db", "lock_file"}) | {"state"} == KNOWN_SECTIONS
    assert set(KNOWN_KEYS) == KNOWN_SECTIONS | {"health.mqtt", "health.webhook"}


def test_manage_monitored_is_off_when_the_key_is_missing(tmp_path: Path) -> None:
    """A config.toml written before the key existed loads unchanged, with it off."""
    assert parse_config(_raw(rules={"allow_remix_releases": True}), base_dir=tmp_path).rules.manage_monitored is False
    assert parse_config(_raw(rules={"manage_monitored": True}), base_dir=tmp_path).rules.manage_monitored is True


def test_manage_monitored_must_be_a_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="manage_monitored"):
        parse_config(_raw(rules={"manage_monitored": "yes"}), base_dir=tmp_path)
