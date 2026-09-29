"""The redacting log filter must never let a credential-shaped value through."""

from __future__ import annotations

import logging

import pytest

from likearr.logging_setup import RedactingFilter, setup_logging


def _filtered(message: str, *args: object) -> str:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)
    RedactingFilter().filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("X-Api-Key: abc123secret", "abc123secret"),
        ("x-api-key=abc123secret", "abc123secret"),
        ("Authorization: Bearer tok.en-value", "tok.en-value"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("GET /api?apikey=abc123secret&x=1", "abc123secret"),
        ('{"access_token": "at-secret", "refresh_token": "rt-secret"}', "at-secret"),
        ('{"access_token": "at-secret", "refresh_token": "rt-secret"}', "rt-secret"),
        ("refresh_token=rt-secret", "rt-secret"),
        ("spotify_access_token=at-secret", "at-secret"),
        ("lidarr_apikey=abc123secret", "abc123secret"),
        ('"GET /spotify/callback?code=spotify-code-secret&state=abc HTTP/1.1" 303', "spotify-code-secret"),
        ("GET /spotify/callback?state=abc&code=spotify-code-secret HTTP/1.1", "spotify-code-secret"),
    ],
)
def test_secret_values_are_redacted(message: str, secret: str) -> None:
    out = _filtered(message)
    assert secret not in out
    assert "REDACTED" in out


def test_args_are_rendered_before_redaction() -> None:
    out = _filtered("Authorization: Bearer %s", "tok-secret")
    assert "tok-secret" not in out


def test_plain_messages_untouched() -> None:
    assert _filtered("monitored 3 albums for artist X") == "monitored 3 albums for artist X"


def test_setup_logging_quiets_httpx_and_installs_filter(
    capsys: pytest.CaptureFixture[str], preserve_root_logging: None
) -> None:
    setup_logging(verbose=True)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    logging.getLogger("likearr.test").info("X-Api-Key: shh-secret")
    err = capsys.readouterr().err
    assert "shh-secret" not in err
    assert "REDACTED" in err


def test_a_traceback_carrying_a_token_is_redacted(
    capsys: pytest.CaptureFixture[str], preserve_root_logging: None
) -> None:
    setup_logging(verbose=False)
    try:
        raise RuntimeError("refresh failed: Authorization: Bearer tb-secret-token")
    except RuntimeError:
        logging.getLogger("likearr.test").exception("spotify said no")
    err = capsys.readouterr().err
    assert "Traceback" in err and "RuntimeError" in err
    assert "tb-secret-token" not in err
    assert "REDACTED" in err


def test_a_malformed_log_call_does_not_raise_and_is_still_redacted() -> None:
    out = _filtered("token=abc123secret %s and %s", "tk-secret-value")
    assert "tk-secret-value" not in out, "an unlabelled argument is never rendered"
    assert "abc123secret" not in out
    assert "log arguments did not fit the format" in out


def test_a_message_whose_str_raises_does_not_raise() -> None:
    class Broken:
        def __str__(self) -> str:
            raise ValueError("no")

    record = logging.LogRecord("t", logging.INFO, __file__, 1, Broken(), (), None)
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "(a log message that could not be rendered)"
