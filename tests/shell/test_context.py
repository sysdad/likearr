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
    config = tmp_path / "config.toml"
    config.write_text(
        '[lidarr]\nurl = "http://lidarr.example.test:8686"\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[musicbrainz]\ncontact = "me@example.invalid"\n'
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
        '[lidarr]\nurl = "http://lidarr.example.test:8686"\nroot_folder = "/music"\nquality_profile = "Standard"\n'
        '[spotify]\ntoken_file = "token.json"\n'
        '[musicbrainz]\ncontact = "me@example.invalid"\n'
        '[state]\ndb = "state.sqlite"\n'
    )

    with context.build_context(config, need_spotify=False) as ctx:
        lock = ctx.lock_path
        assert not lock.exists()

    assert not lock.exists()
