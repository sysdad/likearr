"""`scripts/demo_state.py`: the demo data directory the README screenshots are taken from.

The script is run into `tmp_path` and the app is pointed at what it wrote, through Starlette's test
client: no uvicorn, no child job, and the suite's network guard is on, so a script that reached
Spotify, MusicBrainz or a real Lidarr would fail here rather than quietly succeed.
"""

from __future__ import annotations

import html
import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from starlette.testclient import TestClient

from likearr.web.app import WebSettings, create_app
from likearr.web.auth import LoginLimiter

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_state.py"
PASSWORD = "demo password for the screenshot test"
NOW = datetime(2026, 9, 25, 14, 37, tzinfo=UTC)


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("likearr_demo_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    out = tmp_path_factory.mktemp("demo")
    return out, _script().build_demo(out, now=NOW)


@pytest.fixture
def client(demo: tuple[Path, Any]) -> Iterator[TestClient]:
    out, _info = demo
    app = create_app(
        WebSettings(
            config_path=out / "config.toml",
            password=PASSWORD,
            # Never spawned by these GETs; a child that did would fail rather than reach anything.
            cli=[sys.executable, "-c", "raise SystemExit(1)"],
            now=lambda: NOW,
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
        )
    )
    # Loopback is always allowed, whatever LIKEARR_ALLOWED_HOSTS says.
    with TestClient(app, base_url="http://127.0.0.1") as c:
        response = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 303
        yield c


def test_the_script_writes_a_complete_data_directory(demo: tuple[Path, Any]) -> None:
    out, info = demo
    for name in ("config.toml", "state.sqlite", "last-run.json", "spotify-token.json", "ui/playlist-names.json"):
        assert (out / name).is_file(), name
    assert (out / "ui" / "jobs" / info.plan_id / "diff.json").is_file()
    token = (out / "spotify-token.json").read_text()
    assert "access_token" not in token and "refresh_token" not in token  # nothing a child could spend


def test_status_is_all_good_with_coverage_and_history(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "All good." in page
    assert "% downloaded" in page  # coverage, from the last run's facts
    assert "Road Trip" in page  # the playlist's name, from the names file
    assert "What changed" in page  # the last scheduled apply monitored something
    assert page.count('href="/runs/') >= 8  # history rows, each linking its run


def test_the_plan_list_and_the_plan_review(client: TestClient, demo: tuple[Path, Any]) -> None:
    _out, info = demo
    listing = client.get("/plan")
    assert listing.status_code == 200
    assert f"/plan/{info.plan_id}" in listing.text

    review = client.get(f"/plan/{info.plan_id}")
    assert review.status_code == 200
    page = review.text
    assert "superseded" not in page and "expired" not in page
    for shown in (*info.added_artists, *info.monitored, *info.unmonitored):
        assert shown in page, shown


@pytest.mark.parametrize("query_kind", ["artist", "album"])
def test_look_up_answers_from_the_last_run(client: TestClient, demo: tuple[Path, Any], query_kind: str) -> None:
    _out, info = demo
    query, expected = info.lookups[query_kind]
    response = client.get("/explain", params={"query": query})
    assert response.status_code == 200
    assert expected in response.text


def test_not_added_has_entries_across_reasons(client: TestClient, demo: tuple[Path, Any]) -> None:
    _out, info = demo
    response = client.get("/unmatched")
    assert response.status_code == 200
    page = html.unescape(response.text)
    for group, title in info.not_added:
        assert group in page, group
        assert title in page, title
