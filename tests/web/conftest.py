"""The web app's end-to-end fixtures: a data directory, the fake CLI and a logged-out client.

Split out of `test_app.py`; what they are built from is in `app_support.py`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from likearr.adapters.state_sqlite import SqliteState
from likearr.web.app import WebSettings, create_app
from likearr.web.auth import LoginLimiter
from tests.web.app_support import (
    API_KEY_SENTINEL,
    CONFIG,
    FAKE_CLI,
    NOW,
    PASSWORD,
    _record,
)


@pytest.fixture(autouse=True)
def _web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host names the test client uses, and a contact no page may show (both env-only)."""
    monkeypatch.setenv("LIKEARR_ALLOWED_HOSTS", "testserver,likearr.example.org")
    monkeypatch.setenv("LIKEARR_MUSICBRAINZ_CONTACT", "contact-SENTINEL@example.invalid")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "spotify-token.json").write_text(
        json.dumps(
            {
                "access_token": "tok-SENTINEL",
                "refresh_token": "ref-SENTINEL",
                "expires_at": 1,
                "authorized_at": 1789862400,
            }
        )
    )
    with SqliteState(tmp_path / "state.sqlite") as state:
        state.record_run(_record(), None)
        state.record_run(_record(ts=int((NOW - timedelta(minutes=5)).timestamp()), dry_run=True), None)
        state.record_first_apply(NOW - timedelta(days=1))  # an install that has applied before
    return tmp_path


@pytest.fixture
def fake_cli(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return [sys.executable, str(script), "-c", str(tmp_path / "config.toml")]


@pytest.fixture
def client(data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
        )
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def prune_report(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.web.test_prune import REPORT

    path = data_dir / "fixture-prune.json"
    path.write_text(json.dumps(REPORT))
    monkeypatch.setenv("FAKE_PRUNE", str(path))
    return path


@pytest.fixture
def planned_diff(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A diff.json the fake `likearr run` hands out, planned under the current settings."""
    from likearr.config import load_config
    from likearr.models import MonitorRelease, Reason, ReasonKind, ReleaseKey
    from likearr.shell.diff_io import write_diff
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.config_fingerprint = load_config(data_dir / "config.toml").plan_fingerprint
    for i in range(60):
        diff.monitor.append(
            MonitorRelease(
                key=ReleaseKey("a1", f"rg-{i}"),
                title=f"Deep Cut {i}",
                reasons=frozenset({Reason(ReasonKind.LIKED, f"t{i}")}),
                step="track:album",
            )
        )
    path = data_dir / "fixture-diff.json"
    write_diff(diff, path)
    monkeypatch.setenv("FAKE_DIFF", str(path))
    return path
