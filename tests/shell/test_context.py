"""Wiring checks for `build_context` that need its real adapters rather than fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from likearr.adapters import http
from likearr.ports import LidarrError
from likearr.shell import context

FAKE_API_KEY = "fake-api-key-0000"


@pytest.mark.usefixtures("preserve_root_logging")
def test_the_lidarr_client_refuses_a_cross_origin_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs, Doctor and Lidarr setup all reach Lidarr through this one client (#171)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://sso.other-origin.test/login?token=t"})

    def build_client(**kwargs: Any) -> httpx.Client:
        return http.build_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(context, "build_client", build_client)
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("LIKEARR_LIDARR_URL", "http://lidarr.example.test:8686")
    config = tmp_path / "config.toml"
    config.write_text(
        '[lidarr]\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[state]\ndb = "state.sqlite"\n'
    )

    with (
        context.build_context(config, need_spotify=False) as ctx,
        pytest.raises(LidarrError, match=r"redirected to https://sso\.other-origin\.test;"),
    ):
        ctx.lidarr.check_version()

    assert [r.url.host for r in seen] == ["lidarr.example.test"]


@pytest.mark.usefixtures("preserve_root_logging")
def test_building_a_context_takes_no_run_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command builds a context, `doctor` included. Building one takes no run lock, so a
    fresh install's `doctor` leaves no lock file behind; only a command that takes the lock, such
    as `run`, creates it."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", FAKE_API_KEY)
    config = tmp_path / "config.toml"
    config.write_text(
        '[lidarr]\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[state]\ndb = "state.sqlite"\n'
    )

    with context.build_context(config, need_spotify=False) as ctx:
        lock = ctx.lock_path
        assert not lock.exists()

    assert not lock.exists()


_LOADABLE = '[lidarr]\nroot_folder = "/music"\nquality_profile = "Standard"\n[state]\ndb = "state.sqlite"\n'


@pytest.mark.usefixtures("preserve_root_logging")
def test_doctor_without_a_lidarr_url_is_one_line_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #3: read like the API key - the config loads, and building the Lidarr client refuses."""
    from likearr.models import EXIT_ERROR
    from likearr.shell import cli

    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", FAKE_API_KEY)
    monkeypatch.delenv("LIKEARR_LIDARR_URL")
    config = tmp_path / "config.toml"
    config.write_text(_LOADABLE)

    assert cli.main(["doctor", "--no-spotify", "-c", str(config)]) == EXIT_ERROR
    assert capsys.readouterr().out.strip() == "config error: LIKEARR_LIDARR_URL is not set"


@pytest.mark.usefixtures("preserve_root_logging")
def test_doctor_with_a_removed_key_names_the_variable_to_set_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from likearr.models import EXIT_ERROR
    from likearr.shell import cli

    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", FAKE_API_KEY)
    config = tmp_path / "config.toml"
    config.write_text(_LOADABLE.replace("[lidarr]\n", '[lidarr]\nurl = "http://lidarr:8686"\n'))

    assert cli.main(["doctor", "--no-spotify", "-c", str(config)]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "config error: [lidarr] url is no longer read from config.toml: set LIKEARR_LIDARR_URL instead" in out
    assert not (tmp_path / "state.sqlite").exists(), "refused before anything was opened"
