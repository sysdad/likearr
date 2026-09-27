"""`likearr start`: refuses to start without its password, and never builds a Context."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from likearr.config import UI_PASSWORD_MIN_LENGTH
from likearr.models import EXIT_ERROR, EXIT_OK
from likearr.shell import cli
from tests.web.app_support import CONFIG

PASSWORD = "start-test-password-long-enough"
"""Long enough for `start`'s minimum (issue #170), for every test that is not about the password."""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("start must not build a Context: it would construct Spotify and Lidarr clients")

    monkeypatch.setattr(cli, "build_context", refuse)


@pytest.fixture(autouse=True)
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every test here: record what `start` would have run instead of running it.

    Autouse, because a real `uvicorn.run` serves until it is killed - a test that reached it by
    mistake once hung a session for hours.
    """
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    return calls


@pytest.mark.parametrize("value", [None, "", "   "])
def test_start_refuses_to_start_without_a_password(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("LIKEARR_UI_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("LIKEARR_UI_PASSWORD", value)

    assert cli.main(["start", "-c", str(config_path)]) == EXIT_ERROR

    assert "LIKEARR_UI_PASSWORD" in capsys.readouterr().out
    assert uvicorn_calls == []


@pytest.mark.parametrize(
    "value",
    [
        "Q",  # the issue's reproduction: one character started the service
        "Zq",  # a two-character password
        "Kw7vRbX2pLm9tQs",  # 15, one short of the minimum
        "  Hj4nT8  ",  # padded with spaces, and short either way
    ],
)
def test_start_refuses_a_password_shorter_than_the_minimum(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
    value: str,
) -> None:
    """A one-character password used to start the service and log in (issue #170)."""
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", value)

    assert cli.main(["start", "-c", str(config_path)]) == EXIT_ERROR

    captured = capsys.readouterr()
    out = captured.out + captured.err
    count = "1 character" if len(value) == 1 else f"{len(value)} characters"
    assert (
        f"refusing to start: LIKEARR_UI_PASSWORD is {count}; use at least 16 (openssl rand -base64 24 makes one)"
    ) in out
    # Never the password, nor any part of it that could narrow a guess.
    assert value.strip() not in out
    assert uvicorn_calls == []


def test_the_minimum_is_16() -> None:
    assert UI_PASSWORD_MIN_LENGTH == 16


@pytest.mark.parametrize(
    "value",
    [
        "Kw7vRbX2pLm9tQs4",  # exactly the minimum
        " Kw7vRbX2pLm9tQ ",  # 16 as given, 14 once stripped: measured as given, passed on as given
    ],
)
def test_start_accepts_a_password_of_exactly_the_minimum(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    value: str,
) -> None:
    import likearr.web.server as server_module

    seen: dict[str, Any] = {}

    def fake_serve(config_path: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(server_module, "serve", fake_serve)
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", value)

    assert cli.main(["start", "-c", str(config_path)]) == EXIT_OK
    assert seen["password"] == value


def test_start_runs_uvicorn_without_trusting_proxy_headers(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)

    assert cli.main(["start", "-c", str(config_path), "--host", "0.0.0.0", "--port", "8771"]) == EXIT_OK

    (call,) = uvicorn_calls
    assert call["host"] == "0.0.0.0"
    assert call["port"] == 8771
    assert call["proxy_headers"] is False
    assert call["forwarded_allow_ips"] == ""


def test_start_logs_the_allowed_host_list(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`docker compose logs` is where a stuck user looks next after a refused Host (issue #169)."""
    import likearr.web.server as server_module

    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)
    # `setup_logging` clears the root logger's handlers, including caplog's - a real `start` calls
    # it before `create_app`, so it is turned off here the same way `context_module.setup_logging`
    # is in tests/shell/test_run.py.
    monkeypatch.setattr(server_module, "setup_logging", lambda _verbose: None)

    with caplog.at_level(logging.INFO, logger="likearr.web.app"):
        assert cli.main(["start", "-c", str(config_path)]) == EXIT_OK

    (record,) = [r for r in caplog.records if "allowed_hosts" in r.message]
    assert "testserver" in record.message
    assert "likearr.example.org" in record.message


def test_start_turns_the_scheduler_on(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
) -> None:
    """`likearr start` runs the scheduler (issue #68 phase 2); tests turn it off by default so a
    background thread is never running behind a test that never asked for it."""
    import likearr.web.server as server_module
    from likearr.web.app import WebSettings as _WebSettings

    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)
    seen: list[_WebSettings] = []
    real_create_app = server_module.create_app

    def spy(settings: _WebSettings) -> Any:
        seen.append(settings)
        return real_create_app(settings)

    monkeypatch.setattr(server_module, "create_app", spy)

    assert cli.main(["start", "-c", str(config_path)]) == EXIT_OK

    (settings,) = seen
    assert settings.scheduler is True
    assert settings.workers == 1
    assert settings.reload is False


def test_start_binds_to_loopback_unless_told_otherwise(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)

    cli.main(["start", "-c", str(config_path)])

    assert uvicorn_calls[0]["host"] == "127.0.0.1"
    assert uvicorn_calls[0]["port"] == 8770


def test_start_with_a_broken_config_is_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_context: None
) -> None:
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)

    assert cli.main(["start", "-c", str(tmp_path / "missing.toml")]) == EXIT_ERROR
    assert "config error" in capsys.readouterr().out


def test_start_refuses_a_ui_block_with_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], no_context: None
) -> None:
    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.replace('allowed_hosts = ["testserver", "likearr.example.org"]', "allowed_hosts = []"))

    assert cli.main(["start", "-c", str(path)]) == EXIT_ERROR
    assert "[ui] allowed_hosts" in capsys.readouterr().out


def test_start_takes_the_password_out_of_the_environment(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_context: None,
    uvicorn_calls: list[dict[str, Any]],
) -> None:
    # Nothing the server spawns can inherit what the server no longer holds.
    import os

    monkeypatch.setenv("LIKEARR_UI_PASSWORD", PASSWORD)

    cli.main(["start", "-c", str(config_path)])

    assert "LIKEARR_UI_PASSWORD" not in os.environ
