"""The web app end to end, through Starlette's test client, with a fake CLI as the job child."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from likearr import __version__
from likearr.adapters.state_sqlite import SqliteState
from likearr.models import Guard, ReasonKind, RunStatus
from likearr.playlist_names import names_path, write_names
from likearr.web.app import WebSettings, create_app
from likearr.web.auth import LoginLimiter
from tests.web.app_support import (
    API_KEY_SENTINEL,
    CONFIG,
    DENIABLE,
    FIRST_APPLY_LINE,
    NOW,
    PASSWORD,
    _app,
    _apply_form,
    _build_prune,
    _cache_names,
    _enable_mqtt,
    _file_hash,
    _forget_first_apply,
    _jobs_of,
    _login,
    _names_done,
    _plan_monitoring,
    _record,
    _settings_form,
    _start_plan,
    _wait_for_job,
    _wait_for_picker,
    _wait_until,
    _web_of,
)

# ---------------------------------------------------------------- access


def test_healthz_answers_without_a_login(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "ok"


def test_healthz_reports_a_state_database_it_cannot_open(client: TestClient, data_dir: Path) -> None:
    (data_dir / "state.sqlite").unlink()
    (data_dir / "state.sqlite").mkdir()

    assert client.get("/healthz").status_code == 503


def test_a_page_without_a_session_goes_to_the_login_form(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_an_unknown_url_is_styled_and_404s_when_logged_in(client: TestClient) -> None:
    """No route matches `/no-such-page` at all - Starlette's own answer is a bare
    `text/plain` "Not Found" (9 bytes, no nav, no viewport tag), which renders far too wide on a
    phone. `exception_handlers[404]` swaps in `missing.html` instead."""
    _login(client)

    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert '<meta name="viewport"' in response.text
    assert 'href="/settings"' in response.text  # the nav, not a bare page
    assert "There is no such page." in response.text


def test_an_unknown_url_still_goes_to_login_when_logged_out(client: TestClient) -> None:
    """The styled 404 only ever answers a logged-in visitor: `AuthGateMiddleware` sends anyone
    else to `/login` before the router ever gets a chance to say a path has no route."""
    response = client.get("/no-such-page", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_missing_static_file_is_a_bare_404_when_logged_out(client: TestClient) -> None:
    """`/static/` is open without a session, so a missing file there reaches the 404 handler
    logged out: it gets no nav, no running-job pill and no version."""
    response = client.get("/static/missing", follow_redirects=False)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Not Found"
    assert __version__ not in response.text


def test_a_missing_static_file_is_styled_when_logged_in(client: TestClient) -> None:
    _login(client)

    response = client.get("/static/missing")

    assert response.status_code == 404
    assert 'href="/settings"' in response.text
    assert "There is no such page." in response.text


def test_a_session_from_before_a_logout_gets_the_bare_404(client: TestClient) -> None:
    _login(client)
    stolen = client.cookies.get("likearr_session")
    assert stolen
    client.post("/logout")
    client.cookies.set("likearr_session", stolen)

    response = client.get("/static/missing")

    assert response.status_code == 404
    assert response.text == "Not Found"


def test_an_htmx_poll_without_a_session_moves_the_whole_page_to_login(client: TestClient) -> None:
    response = client.get("/jobs/2026-09-22T14-03-11Z-a1b2c3/fragment", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_a_post_without_a_session_is_refused(client: TestClient) -> None:
    response = client.post("/explain", data={"query": "Radiohead"}, follow_redirects=False)

    assert response.status_code == 401


def test_the_wrong_password_is_refused(client: TestClient) -> None:
    response = client.post("/login", data={"password": "nope"}, follow_redirects=False)

    assert response.status_code == 401
    assert "That is not the password." in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_names_the_setting_the_password_comes_from(client: TestClient) -> None:
    """A fresh install's login page was one password field with no hint - a nicety for
    whoever is handed only the URL, not the person who set LIKEARR_UI_PASSWORD."""
    page = client.get("/login").text

    assert "LIKEARR_UI_PASSWORD" in page


def test_the_right_password_sets_a_strict_http_only_session_cookie(client: TestClient) -> None:
    """`strict`, kept that way for every route: Spotify's direct-callback mode does not
    depend on this cookie reaching `/spotify/callback` at all - that one route is exempted from
    the login gate instead and authorizes itself with a single-use server-side `state` - so there
    is no reason to loosen the cookie that gates every other page. See `create_app`'s middleware
    stack and `web.spotify_connect`."""
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "; secure" not in cookie  # plain http: a Secure cookie would never come back
    assert client.get("/").status_code == 200


def test_the_cookie_is_secure_when_the_request_came_over_https(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"X-Forwarded-Proto": "https"}, follow_redirects=False
    )

    assert "; secure" in response.headers["set-cookie"].lower()


def test_five_bad_passwords_pause_logins_even_with_the_right_one(client: TestClient) -> None:
    for _ in range(5):
        client.post("/login", data={"password": "nope"})

    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    assert response.status_code == 429
    assert "Too many attempts" in response.text


def test_a_forwarded_for_header_does_not_dodge_the_pause(client: TestClient) -> None:
    for i in range(5):
        client.post("/login", data={"password": "nope"}, headers={"X-Forwarded-For": f"10.9.9.{i}"})

    response = client.post("/login", data={"password": PASSWORD}, headers={"X-Forwarded-For": "10.9.9.99"})

    assert response.status_code == 429


def test_logging_out_ends_the_session(client: TestClient) -> None:
    _login(client)

    client.post("/logout")

    assert client.get("/", follow_redirects=False).status_code == 303


def test_a_cross_origin_post_is_refused_before_it_reaches_a_route(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": "http://evil.example"}, follow_redirects=False
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_a_same_origin_post_over_plain_http_passes(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": "http://testserver"}, follow_redirects=False
    )

    assert response.status_code == 303


def test_an_unknown_host_is_refused(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Host": "rebound.evil.example"}).status_code == 400


def test_a_refused_host_is_named_along_with_the_setting_that_fixes_it(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Host": "192.168.1.50:8770"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/plain")
    assert '"192.168.1.50"' in response.text
    assert "8770" not in response.text
    assert "LIKEARR_ALLOWED_HOSTS" in response.text


def test_a_refused_host_still_carries_the_security_headers(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Host": "<script>evil.example"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


def test_an_ipv6_host_is_refused_without_being_offered_as_the_fix(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Host": "[fd00::1]:8770"})

    assert response.status_code == 400
    assert "fd00" not in response.text
    assert "IPv6" in response.text


def test_a_missing_host_gets_the_generic_message(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Host": ""})

    assert response.status_code == 400
    assert "LIKEARR_ALLOWED_HOSTS" in response.text


def test_a_refused_host_logs_one_warning_and_stops_repeating_it(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="likearr.web.auth"):
        client.get("/healthz", headers={"Host": "rebound.evil.example"})
        client.get("/healthz", headers={"Host": "rebound.evil.example"})
        client.get("/healthz", headers={"Host": "another.evil.example"})

    messages = [r.message for r in caplog.records]
    assert sum("rebound.evil.example" in m for m in messages) == 1
    assert sum("another.evil.example" in m for m in messages) == 1


def test_more_than_the_logged_host_bound_stops_logging_new_hosts(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="likearr.web.auth"):
        for i in range(25):
            response = client.get("/healthz", headers={"Host": f"evil-{i}.example"})
            assert response.status_code == 400

    assert len(caplog.records) == 20


def test_loopback_is_always_allowed_for_the_healthcheck(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Host": "127.0.0.1:8770"}).status_code == 200


def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    response = client.get("/login")

    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_errors_js_is_served_as_javascript_without_a_login(client: TestClient) -> None:
    """No `hx-post` page can show its failed-action banner if the script that draws it
    needs a session that just expired - and the CSP (`default-src 'self'`) is satisfied by any
    same-origin static file, so a passing status code here is what "passes the CSP" means for a
    script that adds no inline content of its own."""
    response = client.get("/static/errors.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


def test_icon_svg_is_served_without_a_login(client: TestClient) -> None:
    """The login page needs its icon too, and `/static/` is already open
    (`auth._OPEN_PREFIX`) - this just confirms the file is actually there and answers 200."""
    response = client.get("/static/icon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_apple_touch_icon_is_served_without_a_login(client: TestClient) -> None:
    response = client.get("/static/apple-touch-icon.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


def test_favicon_ico_answers_with_and_without_a_session(client: TestClient) -> None:
    """Browsers still probe `/favicon.ico` directly: logged out, that must not 303
    to `/login` (it needs `/favicon.ico` in `auth._OPEN_PATHS`), and logged in it must not 404."""
    logged_out = client.get("/favicon.ico", follow_redirects=False)
    assert logged_out.status_code == 200
    assert logged_out.headers["content-type"].startswith("image/svg+xml")

    _login(client)
    logged_in = client.get("/favicon.ico", follow_redirects=False)
    assert logged_in.status_code == 200
    assert logged_in.headers["content-type"].startswith("image/svg+xml")


def test_every_page_head_has_the_icon_links_and_theme_colors(client: TestClient) -> None:
    """Every page, including `/login`, carries the icon link, the
    apple-touch-icon link and both light/dark `theme-color` meta tags."""
    login_page = client.get("/login").text
    _login(client)
    status_page = client.get("/").text

    for page in (login_page, status_page):
        assert '<link rel="icon" type="image/svg+xml" href="/static/icon.svg">' in page
        assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in page
        assert '<meta name="theme-color"' in page
        assert 'media="(prefers-color-scheme: light)"' in page
        assert 'media="(prefers-color-scheme: dark)"' in page


def test_the_footer_shows_the_version_on_a_signed_in_page(client: TestClient) -> None:
    """Every signed-in page shows the version in the footer."""
    _login(client)

    page = client.get("/").text

    assert f'<footer class="app-footer">likearr {__version__}</footer>' in page


def test_the_footer_is_absent_from_login(client: TestClient) -> None:
    page = client.get("/login").text

    assert "app-footer" not in page
    assert __version__ not in page


def test_the_footer_shows_the_commit_when_the_image_set_it(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LIKEARR_COMMIT` (set by the Docker image's `VCS_REF` build arg) is read once when the app
    is built, the same way a real process only ever sees the env it started with."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_COMMIT", "abc1234")
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
        _login(c)
        page = c.get("/").text

    assert f'<footer class="app-footer">likearr {__version__} (abc1234)</footer>' in page


def test_no_get_route_changes_anything(client: TestClient, data_dir: Path) -> None:
    _login(client)
    before = (data_dir / "config.toml").read_bytes()

    for path in ["/", "/settings", "/explain", "/healthz", "/login"]:
        client.get(path)

    assert (data_dir / "config.toml").read_bytes() == before
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())


# ---------------------------------------------------------------- status


def test_the_status_page_has_its_own_tab_title(client: TestClient) -> None:
    """Status was the only page falling back to the base `<title>likearr</title>`;
    every other page sets `X - likearr` (see `settings.html`)."""
    _login(client)

    page = client.get("/").text

    assert "<title>Status - likearr</title>" in page


def _newest_run_page(client: TestClient, data_dir: Path) -> str:
    """The `/runs/<id>` page of the newest recorded run: where "What changed" lives."""
    with SqliteState(data_dir / "state.sqlite") as state:
        (row,) = state.run_history(limit=1)
    return client.get(f"/runs/{row.id}").text


LOST_STATE_TEXT = "2 artists tagged likearr that likearr&#39;s state database doesn&#39;t know"


def test_status_warns_about_tagged_artists_the_state_database_has_no_record_of(
    client: TestClient, data_dir: Path
) -> None:
    """A run found likearr-tagged artists in Lidarr with no `owned_artists` row."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, tagged_without_state=2), None)
    _login(client)

    assert LOST_STATE_TEXT in client.get("/").text


def test_status_says_nothing_about_lost_state_without_the_count(client: TestClient) -> None:
    _login(client)

    assert "state database doesn&#39;t know" not in client.get("/").text


def test_status_renders_a_last_run_recorded_before_the_lost_state_count(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, tagged_without_state=2), None)
        state._conn.execute(  # a record written before the field existed
            "UPDATE runs SET record_json = json_remove(record_json, '$.tagged_without_state')"
        )
    _login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Monitored 4 releases" in response.text
    assert "state database doesn&#39;t know" not in response.text


def test_history_s_when_column_does_not_wrap(client: TestClient) -> None:
    """At 1440px every When cell wrapped to three or four lines, even on rows with no
    message - `nowrap` on the cell is the fix; kept to one line at any width."""
    _login(client)

    page = client.get("/").text

    assert '<td class="nowrap">' in page


def test_history_rows_link_to_their_run_page(client: TestClient, data_dir: Path) -> None:
    """Every history row's When cell used to be plain text, so a paused or failed run had
    no way to be opened. Every row with a run id now links to `/runs/<id>`."""
    with SqliteState(data_dir / "state.sqlite") as state:
        rows = state.run_history(limit=10)
    _login(client)

    page = client.get("/").text
    history = page[page.index('id="history"') : page.index("</section>", page.index('id="history"'))]

    for row in rows:
        assert f'<a href="/runs/{row.id}"' in history


def test_a_long_history_message_is_truncated_with_the_full_text_in_title(client: TestClient, data_dir: Path) -> None:
    """The history table's Message cell, not the full-text note `_run.html` shows for the one run
    Status singles out (Last applied / Most recent) - that one is meant to be read in full."""
    long_message = "error: " + "z" * 500
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, message=long_message), None)
    _login(client)

    page = client.get("/").text
    history = page[page.index('id="history"') : page.index("</section>", page.index('id="history"'))]

    assert f'title="{long_message}"' in history
    assert long_message not in history.replace(f'title="{long_message}"', "")  # not shown in full


def test_status_shows_the_next_cron_fire_in_the_crontab_timezone(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    # 18:00 UTC is 14:00 in New York; the next "20 */6" fire is 18:20 EDT.
    assert "Wed 23 Sep 18:20 EDT" in page


def test_status_shows_the_spotify_reauth_date(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    # Authorized 2026-09-20 00:00 UTC, so due 2027-03-20 00:00 UTC: 20:00 the evening before in New York.
    assert "due by Fri 19 Mar 20:00 EDT" in page
    # 177 days out: well outside the warn window, so the banner still says nothing needs you.
    assert "nothing needs you" in page


def _set_authorized_at(data_dir: Path, authorized_at: datetime) -> None:
    """Rewrites the fixture's token file so `reauth_due` (six months later, same day/time) lands
    on a chosen date - the way each banner scenario is set up below."""
    (data_dir / "spotify-token.json").write_text(
        json.dumps(
            {
                "access_token": "tok-SENTINEL",
                "refresh_token": "ref-SENTINEL",
                "expires_at": 1,
                "authorized_at": int(authorized_at.timestamp()),
            }
        )
    )


def test_status_banner_shows_the_reauth_date_when_it_is_due_soon(client: TestClient, data_dir: Path) -> None:
    """A re-authorization due within the warn window is the banner's business, not a loose
    line underneath it that a green "nothing needs you" headline could contradict."""
    _set_authorized_at(data_dir, datetime(2026, 4, 13, 18, 0, tzinfo=UTC))  # due 2026-10-13, in 20 days
    _login(client)

    page = client.get("/").text

    assert "nothing needs you" not in page
    assert page.count("re-authorize by") == 1  # the banner, and nowhere else
    assert '<a href="/settings">Settings</a>' in page
    assert "All good." in page  # the run itself was fine: still the green banner


def test_status_banner_says_nothing_needs_you_well_before_the_warn_window(client: TestClient, data_dir: Path) -> None:
    _set_authorized_at(data_dir, datetime(2026, 5, 22, 18, 0, tzinfo=UTC))  # due 2026-11-22, in 60 days
    _login(client)

    page = client.get("/").text

    assert "nothing needs you" in page
    assert "re-authorize" not in page.lower()


def test_status_banner_says_reauth_was_due_for_an_expired_token_and_stays_green(
    client: TestClient, data_dir: Path
) -> None:
    """An expired token stays out of `glance.problems`, so the
    banner keeps mirroring Home Assistant - it names the date that passed but does not turn amber
    on its own. Confirmed here: the last published run in `data_dir` is OK, so the banner is still
    "All good.", not amber, even though the token is 10 days overdue."""
    _set_authorized_at(data_dir, datetime(2026, 3, 13, 18, 0, tzinfo=UTC))  # due 2026-09-13, 10 days ago
    _login(client)

    page = client.get("/").text

    assert page.count("was due") == 1
    assert "All good." in page
    assert "nothing needs you" not in page


def test_status_banner_lists_the_run_problem_and_the_reauth_sentence_together(
    client: TestClient, data_dir: Path
) -> None:
    """An ERROR run plus a token due soon: the amber banner names both - the run problem
    `health_glance` already reports, and the re-auth sentence this issue adds next to it."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(ts=int(NOW.timestamp()) - 60, status=RunStatus.ERROR, message="spotify: timed out"), None
        )
    _set_authorized_at(data_dir, datetime(2026, 4, 13, 18, 0, tzinfo=UTC))  # due in 20 days
    _login(client)

    page = client.get("/").text

    assert "Needs attention" in page
    assert "The last run failed: spotify: timed out." in page
    assert page.count("re-authorize by") == 1


def test_status_with_no_token_says_not_connected_not_the_unknown_date_hint(client: TestClient, data_dir: Path) -> None:
    """A new install has no token file at all - the hint for a token missing only its date
    does not apply to it."""
    (data_dir / "spotify-token.json").unlink()
    _login(client)

    page = client.get("/").text

    assert "Spotify is not connected" in page
    assert '<a href="/settings#spotify">Settings</a>' in page
    assert "date is unknown" not in page


def test_status_with_a_token_missing_its_date_points_to_settings(client: TestClient, data_dir: Path) -> None:
    """A token file exists (so `likearr auth` was already run), it just predates the
    `authorized_at` field. Re-authorizing from Settings records the date, so both the banner and
    the Spotify row send the user there, and to no command."""
    (data_dir / "spotify-token.json").write_text(
        json.dumps(
            {
                "access_token": "tok-SENTINEL",
                "refresh_token": "ref-SENTINEL",
                "expires_at": 1,
                "scope": "user-library-read",
            }
        )
    )
    _login(client)

    page = client.get("/").text

    assert 'date is unknown. Re-authorize from <a href="/settings#spotify">Settings</a>' in page
    assert 'unknown: re-authorize from <a href="/settings">Settings</a>' in page
    assert "<code>likearr auth" not in page
    assert "Spotify is not connected" not in page


def test_status_shows_projected_wanted_from_the_diff(client: TestClient, data_dir: Path) -> None:
    from tests.adapters.test_state_sqlite import _diff

    with SqliteState(data_dir / "state.sqlite") as state:
        diff = _diff()
        diff.guards.append(Guard(code="x", message="a guard held"))
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _login(client)

    page = client.get("/").text

    assert "10 <span" in page
    assert "monitored with no files" in page
    assert "a guard held" in page


# ---------------------------------------------------------------- what changed


def _run76_diff() -> Any:
    from likearr.models import (
        AddArtist,
        MonitorRelease,
        Profile,
        ProfileRatchet,
        Reason,
        ReasonKind,
        ReleaseKey,
        UnmonitorRelease,
    )
    from tests.adapters.test_state_sqlite import _diff

    # The base fixture already gives 1 add_artist, 1 monitor row and 1 unmonitor row with a
    # followed lost reason; topped up to match the issue's live example: 2 added, 4 monitored,
    # 3 unmonitored (all with a followed lost reason), 1 ratchet.
    diff = _diff()
    diff.add_artists.append(
        AddArtist(artist_mbid="a5", name="The Clifford Brown-Max Roach Quintet", profile=Profile.LEAN)
    )
    for i in range(2, 5):
        diff.monitor.append(
            MonitorRelease(
                key=ReleaseKey(f"a{i}", f"rg{i}"),
                title=f"Track {i}",
                reasons=frozenset({Reason(ReasonKind.LIKED, f"t{i}")}),
                step="",
            )
        )
    for i in range(2, 4):
        diff.unmonitor.append(
            UnmonitorRelease(
                key=ReleaseKey(f"b{i}", f"rgu{i}"),
                title=f"EP {i}",
                lost_reasons=frozenset({Reason(ReasonKind.FOLLOWED, f"b{i}")}),
            )
        )
    diff.ratchets.append(
        ProfileRatchet(artist_mbid="a9", name="Daisy Jones & the Six", to_profile=Profile.FULL, because="")
    )
    return diff


def test_the_run_page_shows_what_changed_with_named_rows(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), _run76_diff())
    _login(client)

    page = _newest_run_page(client, data_dir)

    assert "<h2>What changed</h2>" in page
    assert "David Bromberg Band" in page or "Artist" in page  # the base fixture's add_artists name
    assert "The Clifford Brown-Max Roach Quintet" in page
    assert "Daisy Jones &amp; the Six" in page or "Daisy Jones" in page
    assert "Some Album" in page  # the base fixture's monitor row title
    assert "Track 2" in page and "Track 3" in page and "Track 4" in page
    # why_no_longer_needed for a followed lost reason, with no last-run facts to say otherwise:
    assert "no longer counts among the studio albums and EPs of an artist you follow" in page


def test_the_run_page_shows_the_resolver_change_note(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, baseline="resolver-version-changed"), _run76_diff())
    _login(client)

    page = _newest_run_page(client, data_dir)

    # It says the run couldn't be compared, not that matching caused every change (the live 12:20
    # run mixed matching, Spotify edits and a MusicBrainz reclassification).
    assert "likearr&#39;s matching changed since the run before this one" in page
    assert "not from your Spotify." not in page


def test_the_run_page_labels_a_part_way_apply_as_the_plan_it_was_attempting(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(
                ts=int(NOW.timestamp()) - 60,
                status=RunStatus.ERROR,
                exit_code=1,
                changes_made=1,
                changes_planned=9,
                lidarr_changed=True,
                message="the apply stopped part-way",
            ),
            _run76_diff(),
        )
    _login(client)

    page = _newest_run_page(client, data_dir)

    assert "stopped part-way" in page
    assert "1 of 9 changes made" in page
    assert "not all of it reached Lidarr" in page


def test_the_run_page_does_not_call_a_fully_landed_apply_part_way(client: TestClient, data_dir: Path) -> None:
    """Every planned change reached Lidarr and only confirming it failed - the run page must
    not say "stopped part-way: 3 of 3" or "not all of it reached Lidarr"."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(
                ts=int(NOW.timestamp()) - 60,
                status=RunStatus.ERROR,
                exit_code=1,
                changes_made=3,
                changes_planned=3,
                lidarr_changed=True,
                message="the apply finished: all 3 planned changes were made, but confirming it failed",
            ),
            _run76_diff(),
        )
    _login(client)

    page = _newest_run_page(client, data_dir)

    assert "made all 3 of its planned changes" in page
    assert "stopped part-way" not in page
    assert "not all of it reached Lidarr" not in page


def test_the_run_page_names_the_artists_set_to_none_in_what_changed(client: TestClient, data_dir: Path) -> None:
    """An applied run's "Monitor New Albums" write is shown by name, like every other change."""
    from likearr.models import PrimaryType, ReleaseGroup, Resolution, ResolutionStatus
    from tests.adapters.test_state_sqlite import _diff

    with SqliteState(data_dir / "state.sqlite") as state:
        diff = _diff()  # set_new_items_none: ["a1"]
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
        rg = ReleaseGroup(
            mbid="rg-0", title="Deep Cut 0", artist_mbid="a1", artist_name="Fake Band", primary_type=PrimaryType.ALBUM
        )
        state.cache_resolution(Resolution(intent_key="liked:t0", status=ResolutionStatus.RESOLVED, release_group=rg))
    _login(client)

    page = _newest_run_page(client, data_dir)
    changed = page.split('<section id="changes">')[1]

    section = changed[changed.index("Artists to stop auto-monitoring") :].split("</section>")[0]
    assert "Fake Band" in section


def test_the_run_page_hides_empty_sections_in_what_changed(client: TestClient, data_dir: Path) -> None:
    from tests.adapters.test_state_sqlite import _diff

    with SqliteState(data_dir / "state.sqlite") as state:
        diff = _diff()
        diff.ratchets.clear()
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _login(client)

    page = _newest_run_page(client, data_dir)
    changed = page.split('<section id="changes">')[1]

    assert "Profiles to widen" not in changed


def test_run_page_shows_every_row_and_needs_no_login_redirect_for_htmx(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), _run76_diff())
        (row,) = state.run_history(limit=1)
    _login(client)

    page = client.get(f"/runs/{row.id}")

    assert page.status_code == 200
    assert f"Run #{row.id}" in page.text
    assert "Track 2" in page.text and "Track 3" in page.text and "Track 4" in page.text
    assert "Show all" not in page.text  # the full page needs no cap
    assert "Applied:" in page.text  # the run record itself, not only its diff
    assert "What changed" in page.text


def test_run_page_shows_the_run_record_with_no_what_changed_section_when_there_is_no_diff(
    client: TestClient, data_dir: Path
) -> None:
    """A failure before the check ran, or a config-stale refusal, stores no diff - the run
    page must still exist and show the run's status, message and guards, just no "What changed"."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(
                ts=int(NOW.timestamp()) - 60,
                status=RunStatus.ERROR,
                exit_code=1,
                dry_run=False,
                message="could not reach Lidarr",
            ),
            None,
        )
        (row,) = state.run_history(limit=1)
    _login(client)

    page = client.get(f"/runs/{row.id}")

    assert page.status_code == 200
    assert f"Run #{row.id}" in page.text
    assert "Failed: could not reach Lidarr" in page.text
    assert "What changed" not in page.text


def test_run_page_links_to_the_job_whose_window_contains_it(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    """The reverse of a job's own "What changed" link - the run page links back to
    a kept apply or scheduled job whose window contains it."""
    _login(client)
    plan_id = _start_plan(client)
    apply_response = client.post(f"/plan/{plan_id}/apply", data=_apply_form(client, plan_id), follow_redirects=False)
    assert apply_response.status_code == 303, apply_response.text
    apply_id = apply_response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()), dry_run=False), _run76_diff())
        (row,) = state.run_history(limit=1)

    page = client.get(f"/runs/{row.id}").text

    assert f'href="/jobs/{apply_id}"' in page
    assert "Job log" in page


def test_run_page_shows_no_job_link_when_no_kept_job_matches(client: TestClient, data_dir: Path) -> None:
    """A run from before the UI (host cron), or whose job was pruned, gets no link."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, dry_run=False), None)
        (row,) = state.run_history(limit=1)
    _login(client)

    page = client.get(f"/runs/{row.id}").text

    assert "Job log" not in page


def test_run_page_shows_no_job_link_for_a_dry_run(client: TestClient, data_dir: Path) -> None:
    """A dry run never has a job that changed Lidarr to point at (`_RUN_LINKED_KINDS` excludes
    "plan"), so the job-log note is skipped rather than shown on every dry run."""
    with SqliteState(data_dir / "state.sqlite") as state:
        (dry_row,) = [r for r in state.run_history(limit=10) if r.record.dry_run]
    _login(client)

    page = client.get(f"/runs/{dry_row.id}").text

    assert "Job log" not in page
    assert "no longer kept" not in page


def test_run_page_404s_for_a_missing_or_non_numeric_id(client: TestClient) -> None:
    _login(client)

    assert client.get("/runs/999999").status_code == 404
    assert client.get("/runs/not-a-number").status_code == 404
    assert client.get("/runs/-1").status_code == 404
    assert client.get("/runs/0").status_code == 404


def test_a_missing_run_says_no_such_run_not_no_such_job(client: TestClient) -> None:
    """`missing.html` is shared with job pages, whose default wording ("no such job... only the
    newest 20 are kept") is wrong for a run: runs are not capped the same way."""
    _login(client)

    page = client.get("/runs/999999").text

    assert "There is no such run." in page
    assert "no such job" not in page
    assert "newest 20" not in page


def test_run_page_needs_login(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), _run76_diff())
        (row,) = state.run_history(limit=1)

    response = client.get(f"/runs/{row.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_finished_apply_job_links_to_its_run(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    """The fake CLI (unlike the real one) never writes to the state database, so this stands in
    for what `likearr run --apply` itself does: record the run right as the job finishes. The job
    runner's clock is the fixture's fixed `NOW` (see `client`), so the two line up exactly."""
    _login(client)
    plan_id = _start_plan(client)
    apply_response = client.post(f"/plan/{plan_id}/apply", data=_apply_form(client, plan_id), follow_redirects=False)
    assert apply_response.status_code == 303, apply_response.text
    apply_id = apply_response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()), dry_run=False), _run76_diff())
        (row,) = state.run_history(limit=1)

    job_page = client.get(f"/jobs/{apply_id}").text

    assert f'href="/runs/{row.id}"' in job_page
    assert client.get(f"/runs/{row.id}").status_code == 200


# ---------------------------------------------------------------- /jobs


def _seed_kept_job(data_dir: Path, n: int, *, kind: str = "plan") -> str:
    """Write a job directory straight to disk, bypassing the runner: fast enough to seed more
    jobs than `KEEP_JOBS` without spawning that many fake-CLI subprocesses. `n` also orders the
    id lexicographically (and so chronologically), 0 oldest."""
    job_id = f"2026-09-23T00-{n:02d}-00Z-{n:06x}"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    started = f"2026-09-23T00:{n:02d}:00+00:00"
    meta = {
        "id": job_id,
        "kind": kind,
        "argv": [],
        "label": "",
        "started_at": started,
        "finished_at": started,
        "exit_code": 0,
        "state": "done",
        "drain": False,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    return job_id


def test_jobs_page_lists_every_kept_job_past_the_top_five_newest_first(client: TestClient, data_dir: Path) -> None:
    """Status only ever shows the newest 5 jobs; `/jobs` lists every one the runner still
    keeps (seeded here well past 5, up to what `KEEP_JOBS` allows)."""
    ids = [_seed_kept_job(data_dir, i) for i in range(8)]
    _login(client)

    page = client.get("/jobs").text

    for job_id in ids:
        assert f'href="/jobs/{job_id}"' in page
    assert "Check for changes" in page  # job_title for kind "plan"
    # newest first: the job started last (id 7) appears before the oldest (id 0).
    assert page.index(f'href="/jobs/{ids[7]}"') < page.index(f'href="/jobs/{ids[0]}"')


def test_status_links_to_every_job_from_its_recent_jobs(client: TestClient, data_dir: Path) -> None:
    """Status shows the newest few jobs and links the full list."""
    _seed_kept_job(data_dir, 0)
    _login(client)

    page = client.get("/").text

    assert 'href="/jobs"' in page


def test_jobs_page_needs_login(client: TestClient) -> None:
    response = client.get("/jobs", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_jobs_page_links_what_changed_when_a_run_matches(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    plan_id = _start_plan(client)
    apply_response = client.post(f"/plan/{plan_id}/apply", data=_apply_form(client, plan_id), follow_redirects=False)
    assert apply_response.status_code == 303, apply_response.text
    apply_id = apply_response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()), dry_run=False), _run76_diff())
        (row,) = state.run_history(limit=1)

    page = client.get("/jobs").text

    assert f'href="/jobs/{apply_id}"' in page
    assert f'href="/runs/{row.id}"' in page


def test_split_run_record_finds_the_line_by_its_keys_not_their_order() -> None:
    """The health record's fields can be reordered (or `HealthRecord` can gain one) without
    silently breaking the split - unlike matching the JSON's literal leading text."""
    from likearr.web.app import _split_run_record

    reordered = '{"status": "ok", "resolver_version": 9, "exit_code": 0, "ts": 0, "message": "hi"}'
    output = f"likearr applied: 1 monitored\n{reordered}"

    trimmed, record = _split_run_record(output)

    assert trimmed == "likearr applied: 1 monitored"
    assert record == reordered


def test_split_run_record_skips_trailing_blank_lines_to_find_it() -> None:
    from likearr.web.app import _split_run_record

    record = '{"ts": 0, "resolver_version": 9, "exit_code": 0, "status": "ok"}'

    trimmed, found = _split_run_record(f"likearr applied: 1 monitored\n{record}\n\n")

    assert trimmed == "likearr applied: 1 monitored"
    assert found == record


def test_split_run_record_leaves_output_with_no_health_record_alone() -> None:
    from likearr.web.app import _split_run_record

    assert _split_run_record("likearr applied: 1 monitored") == ("likearr applied: 1 monitored", "")
    assert _split_run_record("") == ("", "")
    assert _split_run_record('not json\n{"status": "ok"}') == ('not json\n{"status": "ok"}', "")


def test_phase_says_reading_spotify_before_the_sources_are_read() -> None:
    from likearr.web.app import _phase

    assert _phase("") == "Reading Spotify"


def test_phase_says_resolving_once_sources_are_read() -> None:
    from likearr.web.app import _phase

    assert _phase("2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=1") == (
        "Spotify read; resolving against MusicBrainz and reading Lidarr"
    )


def test_phase_says_applying_once_the_apply_marker_is_in_the_log() -> None:
    """`_phase` never said this before - the whole add-and-refresh loop of an apply
    used to still show the "resolving against MusicBrainz" wording of the plan that preceded it."""
    from likearr.web.app import _phase

    log_text = "2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=1\nlikearr-phase: apply\n"
    assert _phase(log_text) == "Applying changes to Lidarr"


def test_phase_says_reading_lidarr_once_resolving_is_over(caplog: pytest.LogCaptureFixture) -> None:
    """Once `shell.plan.plan` logs its post-resolve marker, the job page should stop
    saying "resolving against MusicBrainz" - that work is done - and stop implying a resolve ETA
    is still coming."""
    from likearr.web.app import _phase

    log_text = (
        "2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=1\n"
        "2026-09-25T00:00:30+00:00 INFO likearr.shell.run: progress: resolving 1/1 songs and "
        "artists, 1 MusicBrainz lookups (1 live)\n"
        "2026-09-25T00:00:31+00:00 INFO likearr.shell.run: progress: reading Lidarr and building the plan\n"
    )
    assert _phase(log_text) == "Reading Lidarr and building the plan"


def test_phase_says_applying_even_after_the_post_resolve_marker() -> None:
    """The apply marker always wins over the post-resolve one: it is logged later, by definition,
    once an apply's own plan phase has already logged the post-resolve marker."""
    from likearr.web.app import _phase

    log_text = (
        "2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=1\n"
        "2026-09-25T00:00:31+00:00 INFO likearr.shell.run: progress: reading Lidarr and building the plan\n"
        "likearr-phase: apply\n"
    )
    assert _phase(log_text) == "Applying changes to Lidarr"


def test_progress_line_returns_the_newest_progress_line_stripped_of_the_timestamp() -> None:
    from likearr.web.app import _progress_line

    log_text = (
        "2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=2900\n"
        "2026-09-25T00:01:00+00:00 INFO likearr.shell.run: progress: resolving 100/2900 songs and "
        "artists, 100 MusicBrainz lookups (100 live)\n"
        "2026-09-25T00:02:00+00:00 INFO likearr.shell.run: progress: resolving 850/2900 songs and "
        "artists, 3120 MusicBrainz lookups (412 live), about 1h40m left\n"
    )
    assert _progress_line(log_text) == (
        "progress: resolving 850/2900 songs and artists, 3120 MusicBrainz lookups (412 live), about 1h40m left"
    )


def test_progress_line_is_empty_when_the_log_has_no_progress_line_yet() -> None:
    from likearr.web.app import _progress_line

    assert _progress_line("2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=1") == ""
    assert _progress_line("") == ""


def test_progress_line_drops_the_stale_eta_once_resolving_is_over() -> None:
    """The post-resolve marker is itself a `progress:` line, so once it is the newest
    one in the log the page shows it - no numbers, no ETA - instead of the last resolve line, which
    could otherwise sit on screen, ETA included, all through the Lidarr-read-and-diff work after
    resolving."""
    from likearr.web.app import _progress_line

    log_text = (
        "2026-09-25T00:00:00+00:00 INFO likearr.shell.run: sources read: saved_albums=2900\n"
        "2026-09-25T00:02:00+00:00 INFO likearr.shell.run: progress: resolving 850/2900 songs and "
        "artists, 3120 MusicBrainz lookups (412 live), about 1h40m left\n"
        "2026-09-25T00:02:01+00:00 INFO likearr.shell.run: progress: resolving 2900/2900 songs and "
        "artists, 4820 MusicBrainz lookups (612 live)\n"
        "2026-09-25T00:02:02+00:00 INFO likearr.shell.run: progress: reading Lidarr and building the plan\n"
    )
    assert _progress_line(log_text) == "progress: reading Lidarr and building the plan"


def test_the_job_page_shows_the_newest_progress_line_while_a_run_is_in_progress(
    data_dir: Path, fake_cli: list[str]
) -> None:
    """The job page already polls every 2s and already shows the log tail - this is
    the phase paragraph picking up the newest `progress:` line from it."""
    job_id = "2026-09-23T17-00-00Z-a1b2c3"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": "plan",
        "argv": ["likearr", "plan"],
        "label": "Check for changes",
        "started_at": "2026-09-23T17:00:00+00:00",
        "finished_at": None,
        "exit_code": None,
        "state": "running",
        "drain": False,
        "pid": 4242,
        "pid_start": "123",
        "adopted": True,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    (job_dir / "log.txt").write_text(
        "2026-09-23T17:00:01+00:00 INFO likearr.shell.run: sources read: saved_albums=2900\n"
        "2026-09-23T17:01:01+00:00 INFO likearr.shell.run: progress: resolving 100/2900 songs and "
        "artists, 100 MusicBrainz lookups (100 live)\n"
        "2026-09-23T17:41:01+00:00 INFO likearr.shell.run: progress: resolving 850/2900 songs and "
        "artists, 3120 MusicBrainz lookups (412 live), about 1h40m left\n"
    )
    client = TestClient(_app(data_dir, fake_cli), base_url="http://testserver")  # no lifespan: no recover()
    _login(client)

    page = client.get(f"/jobs/{job_id}").text

    assert (
        "progress: resolving 850/2900 songs and artists, 3120 MusicBrainz lookups (412 live), about 1h40m left" in page
    )
    # the raw log tail (already shown in the Technical log panel) still carries every line, but
    # the phase paragraph shows only the newest one - never duplicated there
    assert page.count("resolving 100/2900") == 1


def test_a_finished_apply_shows_the_summary_outside_details_and_the_json_only_inside(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    """The readable 'likearr applied:' summary stays in view; the raw run-record
    JSON line the health stdout sink prints after it moves into the Technical log."""
    _login(client)
    job_id = _start_plan(client)
    apply_response = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False)
    apply_id = apply_response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)

    page = client.get(f"/jobs/{apply_id}").text

    assert "likearr applied: 1 monitored" in page
    before_details = page[: page.index("<details")]
    after_details = page[page.index("<details") :]
    assert "likearr applied: 1 monitored" in before_details
    assert "&#34;ts&#34;" not in before_details  # the JSON line, Jinja-escaped
    assert "&#34;ts&#34;" in after_details


def test_run_page_labels_a_stale_refusal_as_no_changes_made(client: TestClient, data_dir: Path) -> None:
    """A diff can go stale after it was planned (the world moved) - the apply is refused, but
    (unlike a config-stale refusal) its unexecuted diff is still stored (`run` records it via
    `_publish`), and job history can still link to it (its `ts` lands right at the refused job's
    `finished_at`). Without the note this reads exactly like an applied run's "What changed"."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(ts=int(NOW.timestamp()) - 60, status=RunStatus.STALE, exit_code=3, dry_run=False),
            _run76_diff(),
        )
        (row,) = state.run_history(limit=1)
    _login(client)

    page = client.get(f"/runs/{row.id}").text

    assert "This run made no changes" in page
    assert "Track 2" in page  # the plan it would have applied is still shown, by name


def test_the_run_page_labels_a_guarded_run_s_blocked_unmonitors(client: TestClient, data_dir: Path) -> None:
    """A guard holds back *every* unmonitor for a guarded run (`allow_unmonitors=not guarded` in
    `shell.run._execute`), but the stored diff still lists them - the plan it was attempting, not
    what changed in Lidarr. `RunStatus.GUARDED` is in `APPLIED_STATUSES`, so this reaches Status's
    "Last applied run" like a clean apply would, and needs the same kind of caveat."""
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, status=RunStatus.GUARDED), _run76_diff())
    _login(client)

    page = _newest_run_page(client, data_dir)

    assert "A guard held back 2 unmonitors this run" in page
    assert "not what changed in Lidarr" in page
    assert "EP 2" in page  # the blocked plan is still shown, by name


# ---------------------------------------------------------------- Status cards

STATUS_FIRST_APPLY_LINE = "Starts after you first review and apply changes"


def _card(page: str, card_id: str) -> str:
    start = page.index(f'id="{card_id}"')
    return page[
        start : page.index('<div class="card stat"', start + 1)
        if '<div class="card stat"' in page[start + 1 :]
        else None
    ]


def _scheduled_fire(data_dir: Path, state: str, log: str = "") -> str:
    """A scheduled job an hour before `NOW`, in `state`, recorded as the last fire."""
    job_id = "2026-09-23T17-00-00Z-5c4ed1"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": "scheduled",
        "argv": ["likearr", "run", "--scheduled", "--apply"],
        "label": "Scheduled run",
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "finished_at": (NOW - timedelta(hours=1)).isoformat(),
        "exit_code": None if state == "skipped" else (0 if state == "done" else 1),
        "state": state,
        "drain": True,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    (job_dir / "log.txt").write_text(log)
    with SqliteState(data_dir / "state.sqlite") as db:
        db.record_scheduled_fire(NOW - timedelta(hours=1))
    return job_id


def test_automatic_runs_when_paused_gives_the_reason_and_a_link_to_resume(client: TestClient, data_dir: Path) -> None:
    config_path = data_dir / "config.toml"
    config_path.write_text(
        config_path.read_text().replace("[schedule]\n", '[schedule]\nenabled = false\npaused_reason = "away"\n')
    )
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Paused" in card and "away" in card
    assert '<a href="/settings#schedule">Resume in Settings</a>' in card
    assert "Run and apply now" not in card


def test_automatic_runs_shows_a_finished_last_fire_quietly(client: TestClient, data_dir: Path) -> None:
    job_id = _scheduled_fire(data_dir, "done")
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: finished 1 h ago" in card
    assert f'<a href="/jobs/{job_id}">details</a>' in card
    assert "tone-warn" not in card


def test_automatic_runs_shows_a_skipped_fire_with_its_reason(client: TestClient, data_dir: Path) -> None:
    _scheduled_fire(data_dir, "skipped", log="another likearr command holds the run lock; try again when it finishes")
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: skipped 1 h ago - another likearr command holds the run lock" in card
    assert "tone-warn" in card


def test_automatic_runs_shows_a_failed_fire_in_the_warning_style(client: TestClient, data_dir: Path) -> None:
    _scheduled_fire(data_dir, "failed")
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: failed 1 h ago" in card
    assert "tone-warn" in card


def test_automatic_runs_has_no_last_line_before_the_first_fire(client: TestClient) -> None:
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Next:" in card
    assert "Last:" not in card


def test_last_change_says_what_the_last_apply_did_and_links_what_changed(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        applied = next(r for r in state.run_history(limit=10) if not r.record.dry_run)
    _login(client)

    card = _card(client.get("/").text, "last-change")

    assert "Monitored 4 releases, unmonitored 0, added 0 artists." in card
    assert f'<a href="/runs/{applied.id}#changes">What changed</a>' in card


def test_last_change_skips_a_newer_apply_that_changed_nothing(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        older = next(r for r in state.run_history(limit=10) if not r.record.dry_run)
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, counts={"liked_tracks": 2500, "monitored": 0}), None)
    _login(client)

    card = _card(client.get("/").text, "last-change")

    assert "Monitored 4 releases" in card
    assert "Monitored 0 releases" not in card
    assert f'<a href="/runs/{older.id}#changes">What changed</a>' in card


def test_last_change_when_no_kept_apply_changed_anything(client: TestClient, data_dir: Path) -> None:
    for path in data_dir.glob("state.sqlite*"):
        path.unlink()
    with SqliteState(data_dir / "state.sqlite") as state:
        for minutes in (120, 60):
            state.record_run(_record(ts=int(NOW.timestamp()) - minutes * 60, counts={"monitored": 0}), None)
    _login(client)

    card = _card(client.get("/").text, "last-change")

    assert "None in the last 2 runs." in card
    assert "What changed" not in card


def test_last_change_shows_a_part_way_apply_with_its_headline(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(
                ts=int(NOW.timestamp()) - 60,
                status=RunStatus.ERROR,
                exit_code=1,
                counts={"monitored": 0},
                changes_made=1,
                changes_planned=9,
                lidarr_changed=True,
                message="the apply stopped part-way",
            ),
            None,
        )
    _login(client)

    card = _card(client.get("/").text, "last-change")

    assert "Stopped part-way: 1 of 9 changes made." in card


def test_last_change_before_anything_was_applied(client: TestClient, data_dir: Path) -> None:
    for path in data_dir.glob("state.sqlite*"):
        path.unlink()
    _login(client)

    assert "Nothing changed yet." in _card(client.get("/").text, "last-change")


def test_pending_changes_shows_a_check_newer_than_the_last_apply(client: TestClient) -> None:
    _login(client)

    card = _card(client.get("/").text, "pending")

    assert "A check found 4 to monitor." in card
    assert '<a href="/plan">Review changes</a>' in card
    assert "The next automatic run applies these." in card
    assert "tone-warn" not in card


def test_pending_changes_is_hidden_once_the_newest_run_applied(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), None)
    _login(client)

    page = client.get("/").text

    assert 'id="pending"' not in page
    assert "Pending changes" not in page


def test_pending_changes_says_a_guard_holds_unmonitors_back(client: TestClient, data_dir: Path) -> None:
    from likearr.models import Guard
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.guards[:] = [Guard(code="source-shrink", message="liked_tracks shrank 40%", blocked_unmonitors=3)]
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, dry_run=True, status=RunStatus.GUARDED), diff)
    _login(client)

    card = _card(client.get("/").text, "pending")

    assert "a guard holds some unmonitors back" in card
    assert "tone-warn" in card


def test_pending_changes_says_the_unmonitor_cap_holds_them_back(client: TestClient, data_dir: Path) -> None:
    counts = {"liked_tracks": 2500, "monitored": 1, "unmonitored": 150}
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, dry_run=True, counts=counts), None)
    _login(client)

    card = _card(client.get("/").text, "pending")

    assert "holds back all 150 unmonitors: over the cap of 100" in card


def test_pending_changes_while_paused_applies_only_when_reviewed(client: TestClient, data_dir: Path) -> None:
    config_path = data_dir / "config.toml"
    config_path.write_text(config_path.read_text().replace("[schedule]\n", "[schedule]\nenabled = false\n"))
    _login(client)

    assert "these apply only when you review them" in _card(client.get("/").text, "pending")


def test_the_runs_table_says_check_and_applied(client: TestClient) -> None:
    _login(client)

    history = client.get("/").text.split('id="history"', 1)[1]

    assert "<td>check</td>" in history and "<td>applied</td>" in history
    assert "dry run" not in history


# ---------------------------------------------------------------- pause / resume


def test_status_shows_paused_instead_of_the_next_run(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)
    client.post(
        "/settings/pause", data={"file_hash": file_hash, "reason": "maintenance window"}, follow_redirects=False
    )

    page = client.get("/").text

    assert "Paused" in page
    assert "maintenance window" in page
    assert "18:20 EDT" not in page  # the "next run" time, which must not show while paused


def test_paused_by_hand_with_no_timestamp_says_off_in_config_not_an_unknown_time(
    client: TestClient, data_dir: Path
) -> None:
    """A hand edit to config.toml (not the UI's pause action) leaves no `paused_at`: before,
    Settings and Status said "since an unknown time", which reads like a fault rather than a setting
    someone chose."""
    config_path = data_dir / "config.toml"
    config_path.write_text(config_path.read_text().replace("[schedule]\n", "[schedule]\nenabled = false\n"))
    _login(client)

    settings_page = client.get("/settings").text

    assert "Scheduled runs are off in config.toml" in settings_page
    assert "an unknown time" not in settings_page

    status_page = client.get("/").text
    assert "Resume in Settings" in status_page
    assert "an unknown time" not in status_page


# ---------------------------------------------------------------- live schedule preview


def test_the_all_good_banner_notes_a_failed_scheduled_fire(client: TestClient, data_dir: Path) -> None:
    """A scheduled fire that failed without reaching Home Assistant leaves the banner green, but it
    no longer says nothing needs you: it notes the failure and links to the job."""
    job_id = "2026-09-23T17-00-00Z-f1f1f1"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": "scheduled",
        "argv": ["likearr", "run", "--scheduled", "--apply"],
        "label": "Scheduled run",
        "started_at": "2026-09-23T17:00:00+00:00",
        "finished_at": "2026-09-23T17:00:05+00:00",
        "exit_code": 1,
        "state": "failed",
        "drain": True,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    (job_dir / "log.txt").write_text("")
    _login(client)

    page = client.get("/").text

    glance = page[page.index("At a glance") : page.index('id="history"')]
    assert "All good." in glance
    assert "nothing needs you" not in glance
    assert f'The last scheduled run failed: <a href="/jobs/{job_id}">' in glance


def test_status_labels_run_now_as_run_and_apply_with_a_review_first_note(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    assert "Run and apply now" in page
    assert "Run now</button>" not in page
    assert "Checks and applies in one go" not in page  # the button says what it does


def test_status_paused_drops_the_run_now_form(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)
    client.post(
        "/settings/pause", data={"file_hash": file_hash, "reason": "maintenance window"}, follow_redirects=False
    )

    page = client.get("/").text

    assert "Paused" in page
    assert "Run and apply now" not in page
    assert 'action="/run-now"' not in page


def test_run_now_while_paused_flashes_and_starts_no_job(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)
    client.post(
        "/settings/pause", data={"file_hash": file_hash, "reason": "maintenance window"}, follow_redirects=False
    )

    response = client.post("/run-now", follow_redirects=False)

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Scheduled runs are paused - resume them in Settings." in page
    jobs_dir = data_dir / "ui" / "jobs"
    assert not (jobs_dir.is_dir() and list(jobs_dir.iterdir())), "Run now must not start a job while paused"
    with SqliteState(data_dir / "state.sqlite") as state:
        assert state.last_scheduled_fire() is None, "Run now must not record a fire while paused"


# ---------------------------------------------------------------- the first reviewed apply


def _run_now_button(page: str) -> str:
    return re.search(r"<button[^>]*>Run and apply now</button>", page)[0]  # type: ignore[index]


def test_status_before_the_first_reviewed_apply_says_when_the_schedule_starts_and_disables_run_now(
    client: TestClient, data_dir: Path
) -> None:
    _forget_first_apply(data_dir)
    _login(client)

    page = client.get("/").text

    assert STATUS_FIRST_APPLY_LINE in page
    assert "disabled" in _run_now_button(page)
    assert "18:20 EDT" not in page, "no countdown to a fire that will not apply anything"


def test_status_on_a_new_install_with_no_state_database_says_the_same_and_creates_none(
    client: TestClient, data_dir: Path
) -> None:
    for path in data_dir.glob("state.sqlite*"):
        path.unlink()
    _login(client)

    page = client.get("/").text

    assert STATUS_FIRST_APPLY_LINE in page
    assert "disabled" in _run_now_button(page)
    assert not (data_dir / "state.sqlite").exists()


def test_status_after_the_first_reviewed_apply_shows_the_next_run_and_an_enabled_run_now(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    assert FIRST_APPLY_LINE not in page
    assert "disabled" not in _run_now_button(page)


def test_run_now_before_the_first_reviewed_apply_flashes_and_starts_no_job(client: TestClient, data_dir: Path) -> None:
    """A stale tab, or a direct POST, past the disabled button: refused before anything fires."""
    _forget_first_apply(data_dir)
    _login(client)

    response = client.post("/run-now", follow_redirects=False)

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Scheduled runs start after your first reviewed apply - review changes and apply them first." in page
    jobs_dir = data_dir / "ui" / "jobs"
    assert not (jobs_dir.is_dir() and list(jobs_dir.iterdir())), "Run now must not start a job"
    with SqliteState(data_dir / "state.sqlite") as state:
        assert state.last_scheduled_fire() is None, "Run now must not record a fire"


def test_run_now_starts_the_fixed_scheduled_job(client: TestClient, data_dir: Path) -> None:
    _login(client)

    response = client.post("/run-now", follow_redirects=False)

    assert response.status_code == 303
    meta: dict[str, Any] | None = None
    for _ in range(200):
        jobs = list((data_dir / "ui" / "jobs").iterdir()) if (data_dir / "ui" / "jobs").is_dir() else []
        try:
            meta_path = next(iter(jobs)) / "meta.json" if jobs else None
            meta = json.loads(meta_path.read_text()) if meta_path is not None else None
        except (FileNotFoundError, json.JSONDecodeError):
            meta = None  # caught mid-write or mid-rename; retry
        if meta is not None:
            break
        time.sleep(0.02)
    assert meta is not None, "Run now never created a readable job"
    assert meta["kind"] == "scheduled"
    assert meta["argv"][-3:] == ["run", "--scheduled", "--apply"]


def test_a_redeploy_shutdown_during_planning_marks_the_fire_cancelled_for_catchup(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake CLI never prints the apply-phase marker, so `shutdown` cancels
    it as still planning; `_after_scheduled` (wired as the `scheduled` after-callback) must then
    mark the fire cancelled so the missed-fire catch-up re-fires it."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("FAKE_APPLY_SLEEP", "30")
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
        web = _web_of(c)
        web.runner.submit_scheduled("scheduled", ["run", "--scheduled", "--apply"], label="Scheduled run")
        for _ in range(200):
            if any(m.kind == "scheduled" for m in web.runner.jobs()):
                break
            time.sleep(0.01)
        with SqliteState(data_dir / "state.sqlite") as state:
            state.record_scheduled_fire(NOW)

        web.runner.shutdown(timeout=5)

    with SqliteState(data_dir / "state.sqlite") as state:
        assert state.scheduled_fire_cancelled()


def test_the_server_refuses_to_start_on_an_old_ui_cron_key(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `[ui] cron_*` aliases are gone, so a leftover one is an unknown `[ui]` key and
    `likearr start` names it rather than firing on a default schedule nobody chose."""
    from likearr.config import ConfigError

    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    config = data_dir / "config.toml"
    config.write_text(config.read_text().replace("[ui]\n", '[ui]\ncron_schedule = "0 * * * *"\n'))
    settings = WebSettings(config_path=config, password=PASSWORD, cli=fake_cli, now=lambda: NOW, limiter=LoginLimiter())

    with pytest.raises(ConfigError, match=re.escape("[ui] unknown key 'cron_schedule'")):
        create_app(settings)


def test_the_scheduler_refuses_to_start_with_more_than_one_worker(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            scheduler=True,
            workers=2,
        )
    )

    with pytest.raises(RuntimeError), TestClient(app):
        pass


def test_the_scheduler_starts_with_the_default_single_worker_settings(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            scheduler=True,
            shutdown_timeout_s=5,
        )
    )

    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # the app came up: the scheduler did not blow up startup


def test_run_now_requires_login(client: TestClient) -> None:
    response = client.post("/run-now", follow_redirects=False)

    assert response.status_code == 401


def test_no_page_renders_a_secret_or_a_path(client: TestClient) -> None:
    _login(client)

    for path in ["/", "/settings", "/explain"]:
        page = client.get(path).text
        assert API_KEY_SENTINEL not in page
        assert "tok-SENTINEL" not in page
        assert "ref-SENTINEL" not in page
        assert "contact-SENTINEL" not in page
        assert "http://lidarr:8686" not in page
        assert "state.sqlite" not in page


# ---------------------------------------------------------------- explain and jobs


def test_explain_runs_as_a_job_and_shows_the_answer(client: TestClient) -> None:
    _login(client)

    response = client.post("/explain", data={"query": "  Radiohead  "}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].removeprefix("/jobs/")

    final = _wait_for_job(client, job_id)
    assert "explain: &#39;Radiohead&#39; is monitored because you like it" in final
    assert "Finished." in final
    assert "hx-get" not in final  # stopped polling
    page = client.get(f"/jobs/{job_id}").text
    assert "Radiohead" in page

    # The job's own poll swaps all of #job (outerHTML) on every trigger; the live region
    # wrapping it in job.html must sit outside that so it stays the same element across polls.
    assert page.count('role="status"') == 1
    assert page.index('role="status"') < page.index('id="job"')


def test_a_failed_explain_is_not_presented_as_an_answer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_FAIL", "1")
    _login(client)

    job_id = client.post("/explain", data={"query": "Radiohead"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "Failed." in final
    assert "spotify: token expired" in final
    assert "comes from a plan" not in final


def test_a_query_that_looks_like_a_flag_is_still_a_query(client: TestClient) -> None:
    _login(client)

    job_id = client.post("/explain", data={"query": "-v --apply"}, follow_redirects=False).headers["location"][6:]

    assert "&#39;-v --apply&#39;" in _wait_for_job(client, job_id)


def test_an_empty_query_is_refused(client: TestClient) -> None:
    _login(client)

    assert client.post("/explain", data={"query": "   "}).status_code == 400


def test_a_second_job_is_refused_while_one_runs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_SLEEP", "3")
    _login(client)
    first = client.post("/explain", data={"query": "a"}, follow_redirects=False).headers["location"][6:]

    running = client.get(f"/jobs/{first}/fragment", headers={"HX-Request": "true"})
    second = client.post("/explain", data={"query": "b"})

    assert running.status_code == 200
    assert 'hx-trigger="every 2s"' in running.text
    assert second.status_code == 409
    assert "already running" in second.text
    client.post(f"/jobs/{first}/cancel")
    assert "Cancelled." in _wait_for_job(client, first)


def test_a_job_id_that_is_not_one_is_a_404(client: TestClient) -> None:
    _login(client)

    assert client.get("/jobs/..%2F..%2Fetc").status_code == 404
    assert client.get("/jobs/not-a-job/fragment").status_code == 404


# ---------------------------------------------------------------- explain from the last run


def _record_last_run(data_dir: Path) -> None:
    from likearr.shell.last_run import last_run_facts, write_last_run
    from tests.unit.fakes import lidarr_artist, lidarr_view
    from tests.unit.test_explain import (
        BUSY,
        EXISTING,
        JUNGLE_COLLISION,
        JUNGLE_RESOLUTION,
        JUNGLE_SNAPSHOT,
        _jungle_desired,
    )

    facts = last_run_facts(
        ran_at=NOW - timedelta(hours=2),
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={BUSY.key: JUNGLE_RESOLUTION},
        artist_resolutions={},
        desired=_jungle_desired(),
        view=lidarr_view(artists=[lidarr_artist(EXISTING, id=9003, name="Jungle")]),
        owned_keys=(),
        collisions=[JUNGLE_COLLISION],
    )
    write_last_run(data_dir / "last-run.json", facts)


def test_explain_answers_at_once_from_the_last_run(client: TestClient, data_dir: Path) -> None:
    _record_last_run(data_dir)
    _login(client)

    response = client.get("/explain", params={"query": "Busy Earnin'"})

    assert response.status_code == 200
    page = response.text
    assert "As of the last run" in page
    assert "(a dry run)" in page and "(an apply)" not in page
    assert "You liked &#34;Busy Earnin&#39;&#34; by Jungle" in page
    assert '<span class="pill tone-warn">Skipped (name collision)</span>' in page
    assert '<span class="pill tone-warn">Looks like a wrong match</span>' in page
    assert "<dt>In Lidarr</dt><dd>Not added: Lidarr already has a different Jungle" in page
    assert "<dt>Matched to</dt><dd>Jungle (album, 1969) by Jungle" in page
    assert 'href="https://musicbrainz.org/artist/59074e0f-ede4-4ff1-bee2-cbfd3a273095"' in page
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())
    # The slow, live check is one button away, for this query.
    assert 'method="post" action="/explain"' in page
    assert 'name="query" value="Busy Earnin&#39;"' in page
    assert "Check against live Spotify and Lidarr" in page
    assert "a few minutes once the first check has run" in page


def test_explain_with_no_run_recorded_offers_the_live_check(client: TestClient, data_dir: Path) -> None:
    _login(client)

    page = client.get("/explain", params={"query": "Jungle"}).text

    assert "No run has been recorded for Look up yet" in page
    assert "Check against live Spotify and Lidarr" in page
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())


def test_explain_refuses_a_bad_query_without_answering(client: TestClient) -> None:
    _login(client)

    assert client.get("/explain", params={"query": "Radio\x00head"}).status_code == 400
    assert client.get("/explain", params={"query": "x" * 201}).status_code == 400
    assert "Check against live" not in client.get("/explain").text  # no query, no answer


# ---------------------------------------------------------------- explain's summary


def test_explain_asks_for_json_and_leads_with_the_summary(client: TestClient, data_dir: Path) -> None:
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    meta = json.loads((data_dir / "ui" / "jobs" / job_id / "meta.json").read_text())
    argv = meta["argv"]
    assert argv[argv.index("explain") :] == ["explain", "--json", "--", "Jungle"]
    assert final.index("is monitored because you like it") < final.index("<details")
    assert "Looks like a wrong match" in final
    assert "This looks like a wrong match." in final
    assert (
        'href="https://musicbrainz.org/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da" target="_blank" '
        'rel="noopener noreferrer">The artist on MusicBrainz</a>'
    ) in final
    assert "why: you liked it [liked:abc]" in final  # the detail, collapsed below
    assert '{"query"' not in final  # never the raw JSON


def test_an_explain_link_that_is_not_https_is_not_a_link(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_LINK", "javascript:alert(1)")
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "javascript:" not in final
    assert "is monitored because you like it" in final


def test_an_explain_job_whose_output_holds_no_report_still_shows_its_output(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output with no report in it (a failed parse, or a job directory an older likearr wrote)
    is shown as plain output, like any other job's, rather than hidden."""
    monkeypatch.setenv("FAKE_EXPLAIN_PLAIN", "1")
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]

    assert "explain: &#39;Jungle&#39; is monitored because you like it" in _wait_for_job(client, job_id)


# ---------------------------------------------------------------- playlist names


def test_status_names_playlists_from_the_cache(client: TestClient, data_dir: Path) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip"})
    _login(client)

    assert "Playlist: Road trip" in client.get("/").text


def test_names_fetched_before_the_names_file_existed_are_still_shown(client: TestClient, data_dir: Path) -> None:
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)
    (data_dir / "ui" / "playlist-names.json").unlink()  # as on an upgrade: the job, but no file

    assert "Playlist: Road trip" in client.get("/").text


def test_explain_in_the_server_answers_a_broad_query_in_part(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likearr.web.app as web_app

    _record_last_run(data_dir)
    _login(client)
    monkeypatch.setattr(web_app, "EXPLAIN_LIMIT", 0)

    page = client.get("/explain", params={"query": "Jungle"}).text

    assert "not shown: make the search more specific" in page


# ---------------------------------------------------------------- review fixes


def test_concurrent_wrong_passwords_cannot_outrun_the_pause(data_dir: Path, fake_cli: list[str]) -> None:
    # The pause must hold for requests already in flight: each one is checked after its body has
    # arrived, in the same step as the comparison and the recording of its failure.
    import asyncio

    app = _app(data_dir, fake_cli)

    async def attempt(gate: asyncio.Event) -> int:
        body = b"password=nope"
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            await gate.wait()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        statuses: list[int] = []

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                statuses.append(int(message["status"]))  # type: ignore[arg-type]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("10.0.0.9", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return statuses[0]

    async def burst() -> list[int]:
        gate = asyncio.Event()
        tasks = [asyncio.create_task(attempt(gate)) for _ in range(20)]
        await asyncio.sleep(0.05)  # every handler is now waiting for its body
        gate.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(burst())

    assert results.count(401) == 5
    assert results.count(429) == 15


def test_an_injected_limiter_is_the_one_used(data_dir: Path, fake_cli: list[str]) -> None:
    clock = [1000.0]
    limiter = LoginLimiter(now=lambda: clock[0])
    with TestClient(_app(data_dir, fake_cli, limiter=limiter)) as client:
        for _ in range(5):
            client.post("/login", data={"password": "nope"})
        assert client.post("/login", data={"password": PASSWORD}).status_code == 429
        clock[0] += 61
        assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303


def test_a_ui_block_problem_appearing_after_start_is_shown(client: TestClient, data_dir: Path) -> None:
    _login(client)
    (data_dir / "config.toml").write_text(CONFIG.replace("[ui]\n", '[ui]\ncli_command = ""\n'))

    page = client.get("/").text

    assert "[ui] cli_command" in page


@pytest.mark.parametrize("path", ["/loginx", "/login/x", "/healthz/x", "/healthzz"])
def test_only_the_exact_open_paths_skip_the_login(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_websocket_is_refused_before_it_reaches_the_app(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/"):
        pass


def test_a_500_still_carries_the_security_headers(data_dir: Path, fake_cli: list[str]) -> None:
    with TestClient(_app(data_dir, fake_cli), raise_server_exceptions=False) as client:
        _login(client)
        (data_dir / "state.sqlite").write_bytes(b"not a database, not even close" * 100)

        response = client.get("/")

    assert response.status_code == 500
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-frame-options"] == "DENY"


def test_healthz_reads_healthy_on_a_fresh_install_with_no_state_database_yet(
    client: TestClient, data_dir: Path
) -> None:
    (data_dir / "state.sqlite").unlink()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "ok (no runs yet)"
    assert not (data_dir / "state.sqlite").exists()


def test_healthz_stays_unhealthy_when_the_state_directory_is_missing(client: TestClient, data_dir: Path) -> None:
    (data_dir / "state.sqlite").unlink()
    (data_dir / "config.toml").write_text(CONFIG.replace('db = "state.sqlite"', 'db = "missing-dir/state.sqlite"'))

    response = client.get("/healthz")

    assert response.status_code == 503
    assert not (data_dir / "missing-dir").exists()


def test_healthz_stays_unhealthy_when_the_state_directory_is_read_only(client: TestClient, data_dir: Path) -> None:
    (data_dir / "state.sqlite").unlink()
    os.chmod(data_dir, 0o500)
    try:
        response = client.get("/healthz")
    finally:
        os.chmod(data_dir, 0o700)

    assert response.status_code == 503
    assert not (data_dir / "state.sqlite").exists()


def test_status_with_no_state_database_says_so_without_creating_one(client: TestClient, data_dir: Path) -> None:
    _login(client)
    (data_dir / "state.sqlite").unlink()

    page = client.get("/").text

    assert "No state database" in page
    assert not (data_dir / "state.sqlite").exists()


def test_a_query_with_control_characters_is_refused(client: TestClient, data_dir: Path) -> None:
    _login(client)

    response = client.post("/explain", data={"query": "Radio\x00head"})

    assert response.status_code == 400
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())


def test_an_explain_answer_is_shown_as_written(client: TestClient) -> None:
    _login(client)

    job_id = client.post("/explain", data={"query": "Basic Channel"}, follow_redirects=False).headers["location"][6:]

    assert "Basic Channel" in _wait_for_job(client, job_id)


def test_the_why_page_of_a_failed_playlists_job_shows_its_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_FAIL", "1")
    _login(client)
    final = _wait_for_picker(client, client.post("/settings/playlists").text)
    job_id = re.search(r'href="/jobs/([^"]+)"', final)[1]  # type: ignore[index]

    assert "spotify: quota exceeded" in client.get(f"/jobs/{job_id}").text


def test_a_background_poll_does_not_swallow_the_saved_message(client: TestClient) -> None:
    _login(client)
    job_id = client.post("/explain", data={"query": "a"}, follow_redirects=False).headers["location"][6:]
    _wait_for_job(client, job_id)
    form = _settings_form(client.get("/settings").text)
    form["rules.singles_fallback_days"] = ["90"]
    client.post("/settings", data=form, follow_redirects=False)

    client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})  # another tab's poll

    assert "Saved rules.singles_fallback_days" in client.get("/settings").text


# ---------------------------------------------------------------- security findings


def test_an_oversized_body_is_refused_by_its_declared_length(client: TestClient) -> None:
    response = client.post(
        "/login",
        content=b"password=" + b"x" * (1024 * 1024 + 1),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413


def test_an_oversized_body_without_a_length_is_refused_as_it_streams(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"password="
        for _ in range(40):
            yield b"x" * 65536

    response = client.post("/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"})

    assert response.status_code == 413


def test_login_takes_only_a_urlencoded_form(client: TestClient) -> None:
    response = client.post("/login", files={"password": ("p.txt", b"x" * 10000)})

    assert response.status_code == 415


def test_a_logged_out_cookie_cannot_be_replayed(client: TestClient) -> None:
    _login(client)
    stolen = client.cookies.get("likearr_session")
    assert stolen

    client.post("/logout")
    client.cookies.set("likearr_session", stolen)

    assert client.get("/", follow_redirects=False).status_code == 303


def test_cancel_answers_at_once_and_the_page_says_it_is_stopping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_SLEEP", "5")
    _login(client)
    job_id = client.post("/explain", data={"query": "a"}, follow_redirects=False).headers["location"][6:]

    started = time.monotonic()
    client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)

    assert time.monotonic() - started < 1
    assert "Cancelled." in _wait_for_job(client, job_id)


# ---------------------------------------------------------------- name collisions


def test_status_shows_a_collision_card_with_honest_advice(client: TestClient, data_dir: Path) -> None:
    from likearr.models import NameCollision
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.name_collisions.append(
        NameCollision(
            name="Jungle",
            wanted_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095",
            existing_mbid="6bbb3983-ce8a-4971-96e0-7cae73268fc4",
            existing_lidarr_id=9003,
            existing_name="Jungle",
            wanted_disambiguation="US psychedelic rock",
            existing_disambiguation="London modern soul collective",
            dropped_releases=3,
        )
    )
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _login(client)

    page = client.get("/").text

    assert "US psychedelic rock" in page
    assert "London modern soul collective" in page
    assert "9003" in page
    assert 'href="https://musicbrainz.org/artist/59074e0f-ede4-4ff1-bee2-cbfd3a273095"' in page
    assert 'href="http://lidarr:8686/artist/6bbb3983-ce8a-4971-96e0-7cae73268fc4"' in page
    assert 'href="http://lidarr:8686/add/search?term=lidarr%3A59074e0f-ede4-4ff1-bee2-cbfd3a273095"' in page
    assert '<form method="get" action="/explain"' in page  # Explain from the last run: at once
    assert '<input type="hidden" name="query" value="Jungle">' in page
    assert "distinct name" not in page  # Lidarr takes an artist's name from its metadata
    assert "can't hold two artists with the same name" in page


# ---------------------------------------------------------------- the confirm is bound to what it showed


def test_the_last_run_is_parsed_once_per_version_of_the_file(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likearr.web.context as web_context

    reads: list[Path] = []
    real = web_context.read_last_run

    def counting(path: Path):  # type: ignore[no-untyped-def]
        reads.append(path)
        return real(path)

    monkeypatch.setattr(web_context, "read_last_run", counting)
    _record_last_run(data_dir)
    _login(client)

    for query in ("Jungle", "Busy", "nothing"):
        assert client.get("/explain", params={"query": query}).status_code == 200
    assert len(reads) == 1

    _record_last_run(data_dir)  # the next run replaces the file
    client.get("/explain", params={"query": "Jungle"})
    assert len(reads) == 2


def test_the_live_check_is_bounded_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import likearr.web.app as web_app

    monkeypatch.setattr(web_app, "EXPLAIN_LIMIT", 1)
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "1 more match not shown: make the search more specific." in final
    assert "This looks like a wrong match." not in final  # the second answer, cut


# ---------------------------------------------------------------- Status at a glance (PR B)


def test_status_leads_with_health_last_run_and_next_run(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    assert page.index("At a glance") < page.index('id="history"')
    glance = page[page.index("At a glance") : page.index('id="history"')]
    assert "All good." in glance
    assert "The last run went fine, and nothing needs you." in glance
    assert "Home Assistant" not in glance
    assert "A check found 4 to monitor" in glance  # the newest run is the fixture's check
    assert "Automatic runs" in glance
    assert "More detail after the next run" in glance  # no last-run facts yet: the record's counts


def test_status_needs_attention_for_what_home_assistant_would_flag(client: TestClient, data_dir: Path) -> None:
    from likearr.models import NameCollision
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.name_collisions.append(NameCollision(name="Jungle", wanted_mbid="59074e0f-ede4-4ff1-bee2-cbfd3a273095"))
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(ts=int(NOW.timestamp()) - 60, status=RunStatus.DEGRADED, new_conditions=("new-name-collision",)),
            diff,
        )
    _login(client)

    page = client.get("/").text

    assert "Needs attention" in page
    assert (
        '<a href="#collisions">likearr skipped Jungle: a different artist with the same name is already in '
        "Lidarr - see below.</a>"
    ) in page
    assert 'id="collisions"' in page


def _record_unmatched_run(data_dir: Path, *, tracks: Sequence[Any] = (), step: str = "track:none") -> None:
    from likearr.models import Resolution, ResolutionStatus, SourceSnapshot
    from likearr.shell.last_run import last_run_facts, write_last_run
    from tests.unit.fakes import lidarr_view, spotify_album, track_intent
    from tests.unit.test_diff import desired_state

    song = track_intent(
        "Lost Song", spotify_album("Lost Album", artists=("Nobody",)), spotify_id="t-lost", artists=("Nobody",)
    )
    songs = (song, *tracks)
    snapshot = SourceSnapshot(fetched_at=NOW, artists=(), albums=(), tracks=songs, counts={"liked_tracks": len(songs)})
    facts = last_run_facts(
        ran_at=NOW - timedelta(hours=1),
        snapshot=snapshot,
        resolutions={
            t.reason.key: Resolution(t.reason.key, ResolutionStatus.UNMAPPED, step=step, detail="no release matched")
            for t in songs
        },
        artist_resolutions={},
        desired=desired_state(),
        view=lidarr_view(),
        owned_keys=(),
        collisions=[],
    )
    write_last_run(data_dir / "last-run.json", facts)


def test_status_counts_coverage_and_links_what_could_not_be_matched(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir)
    _login(client)

    page = client.get("/").text
    unmatched = client.get("/unmatched").text

    assert "% downloaded" in page
    assert '<a href="/unmatched#unmatched">Couldn\'t be matched</a>' in page
    assert "<strong>Nobody - Lost Album</strong>" in unmatched


def test_the_waiting_for_a_download_note_links_to_lidarr_when_an_address_is_configured(
    client: TestClient, data_dir: Path
) -> None:
    _record_unmatched_run(data_dir)
    _login(client)

    page = client.get("/").text

    assert "likearr doesn't search." in page
    assert '<a href="http://lidarr:8686/wanted/missing">' in page


def test_the_waiting_for_a_download_note_has_no_link_without_a_lidarr_address(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_URL", "http://lidarr:8686?x=1")
    _record_unmatched_run(data_dir)
    _login(client)

    page = client.get("/").text

    assert "likearr doesn't search." in page
    assert "/wanted/missing" not in page


def _many_unmatched() -> list[Any]:
    """30 albums one liked song each, and TRY! with 12, all found nowhere on MusicBrainz."""
    from tests.unit.fakes import spotify_album, track_intent

    try_album = spotify_album("TRY! - Live In Concert", spotify_id="sp-try", artists=("John Mayer",))
    tracks = [
        track_intent(
            f"Track {i:02d}",
            spotify_album(f"Album {i:02d}", spotify_id=f"sp-a{i}", artists=(f"Artist {i:02d}",)),
            spotify_id=f"t{i}",
            artists=(f"Artist {i:02d}",),
        )
        for i in range(30)
    ]
    tracks += [track_intent(f"Song {i}", try_album, spotify_id=f"try{i}", artists=("John Mayer",)) for i in range(12)]
    return tracks


def test_the_unmatched_page_opens_on_cards_and_the_first_page_of_each_part(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)

    page = client.get("/unmatched").text

    card = '<a href="#unmatched">Couldn&#39;t be matched</a></span><span class="value tone-warn">43</span>'
    assert card in page
    assert "songs, albums or follows · 32 releases" in page
    assert page.count("<strong>") == 25
    assert "Page 1 of 2" in page
    assert "MusicBrainz has no release by this artist with this title" in page
    assert 'id="unmatched-filter"' in page and 'hx-get="/unmatched/rows"' in page


def test_the_unmatched_rows_group_songs_by_album(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)

    rows = client.get("/unmatched/rows", params={"q": "song 7"}).text

    assert "<html" not in rows
    assert rows.count("<strong>") == 1
    assert "<strong>John Mayer - TRY! - Live In Concert</strong>" in rows
    assert "<summary>12 liked songs</summary>" in rows
    assert "<li>Song 11</li>" in rows
    assert 'href="/explain?query=TRY%21+-+Live+In+Concert"' in rows
    assert "https://musicbrainz.org/search?query=releasegroup" in rows


def test_each_section_heading_says_its_card_s_numbers_in_its_card_s_words(client: TestClient, data_dir: Path) -> None:
    """A heading and its card never disagree, nor count different things."""
    import re

    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)
    page = client.get("/unmatched").text

    cards = dict(re.findall(r'<a href="#[a-z]+">([^<]+)</a></span><span class="value[^"]*">(\d+)</span>', page))
    headings = dict(re.findall(r"<h2>([^:<]+): (\d+) (?:song|songs),", page))
    assert headings == {"Couldn&#39;t be matched": "43"}
    assert all(cards[title] == n for title, n in headings.items()), (cards, headings)
    card_subs = re.findall(r'<span class="sub">[^<]* · ([^<]+)</span>', page)
    heading_subs = re.findall(r'<span class="note">· ([^<]+?)</span></h2>', page)
    assert heading_subs == ["32 releases"] and heading_subs[0] in card_subs
    assert "(32 releases)" in page  # a reason's heading counts its rows, and says in what


def test_a_filtered_section_keeps_its_card_s_numbers_and_says_how_many_match(
    client: TestClient, data_dir: Path
) -> None:
    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)

    rows = client.get("/unmatched/rows", params={"q": "song 7"}).text

    assert "<h2>Couldn&#39;t be matched: 43 songs, albums or follows" in rows
    assert "· 32 releases · 1 matches the filters</span></h2>" in rows


def test_an_unmatched_part_pages_on_its_own(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)

    second = client.get("/unmatched/part", params={"group": "unmatched", "reason": "no-release", "page": "2"})
    sorted_by_songs = client.get(
        "/unmatched/part", params={"group": "unmatched", "reason": "no-release", "sort": "songs"}
    )

    assert second.status_code == 200 and second.text.count("<strong>") == 7
    assert "Page 2 of 2" in second.text
    assert 'id="part-unmatched-no-release"' in second.text
    assert sorted_by_songs.text.index("TRY!") < sorted_by_songs.text.index("Album 00")
    assert client.get("/unmatched/part", params={"group": "nope", "reason": "no-release"}).status_code == 404
    huge = client.get("/unmatched/part", params={"group": "unmatched", "reason": "no-release", "page": "9" * 5000})
    assert huge.status_code == 200 and "Page 1 of 2" in huge.text


def test_the_unmatched_page_escapes_what_spotify_sends_and_ignores_unknown_filters(
    client: TestClient, data_dir: Path
) -> None:
    from tests.unit.fakes import spotify_album, track_intent

    evil = track_intent("<script>alert(1)</script>", spotify_album("<img src=x onerror=alert(1)>"), spotify_id="t-evil")
    _record_unmatched_run(data_dir, tracks=[evil], step="track:album:search")
    _login(client)

    page = client.get("/unmatched", params={"show": "<b>x</b>", "sort": "<i>", "source": "\"'><s>"}).text

    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page
    assert "<b>x</b>" not in page and "<s>" not in page


def test_the_unmatched_page_only_links_it_never_writes(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir, tracks=_many_unmatched(), step="track:album:search")
    _login(client)

    page = client.get("/unmatched").text
    sections = page[page.index('id="unmatched-sections"') :]

    assert "<form" not in sections
    assert 'method="post"' not in page.split("</nav>", 1)[-1]


def test_left_out_rows_link_the_setting_that_leaves_them_out(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir, step="track:excluded:remix")
    _login(client)

    page = client.get("/unmatched").text

    assert 'href="/settings#field-rules.allow_remix_releases"' in page
    assert 'id="field-rules.allow_remix_releases"' in client.get("/settings").text


def test_the_unmatched_list_before_any_run_says_so(client: TestClient) -> None:
    _login(client)

    assert "appears after the next run" in client.get("/unmatched").text


def test_status_and_the_unmatched_list_share_the_parsed_last_run(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likearr.web.context as web_context

    reads: list[Path] = []
    real = web_context.read_last_run

    def counting(path: Path):  # type: ignore[no-untyped-def]
        reads.append(path)
        return real(path)

    monkeypatch.setattr(web_context, "read_last_run", counting)
    _record_unmatched_run(data_dir)
    _login(client)

    client.get("/")
    client.get("/unmatched")
    client.get("/explain", params={"query": "Lost"})
    assert len(reads) == 1

    _record_unmatched_run(data_dir)  # the next run replaces the file
    client.get("/")
    assert len(reads) == 2


def _in_lidarr_card(page: str) -> str:
    start = page.index('<span class="label">In Lidarr</span>')
    return page[start : page.index("</div>", start)]


def test_missing_shows_the_same_in_lidarr_meter_as_status(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir)
    _login(client)

    status_page = client.get("/").text
    unmatched_page = client.get("/unmatched").text

    status_card = _in_lidarr_card(status_page)
    unmatched_card = _in_lidarr_card(unmatched_page)
    assert status_card == unmatched_card
    assert "% downloaded" in unmatched_card
    assert 'class="meter"' in unmatched_card
    assert unmatched_page.index('<span class="label">In Lidarr</span>') < unmatched_page.index('class="stats five"')


def test_missing_shows_no_meter_without_a_run(client: TestClient) -> None:
    _login(client)

    page = client.get("/unmatched").text

    assert "In Lidarr" not in page


def test_status_health_reaches_back_past_many_dry_runs(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(ts=int(NOW.timestamp()) - 3600, status=RunStatus.DEGRADED, new_conditions=("mb-outage",)), None
        )
        for minutes in range(25, 0, -1):
            state.record_run(_record(ts=int(NOW.timestamp()) - minutes * 60, dry_run=True), None)
    _login(client)

    page = client.get("/").text

    assert "Needs attention" in page
    assert "MusicBrainz lookups failed this run" in page


def test_status_with_no_run_at_all_needs_attention(client: TestClient, data_dir: Path) -> None:
    """`data_dir` already has a token, so "No run has finished yet." is replaced by the
    first-run checklist (its "Check for changes" step is the same news, as a link)."""
    (data_dir / "state.sqlite").unlink()
    _login(client)

    page = client.get("/").text

    assert "No run has finished yet" not in page
    assert "Get set up" in page
    assert '<span class="ok">✓</span> Connect Spotify' in page
    assert '<a href="/plan">Check for changes</a>' in page
    assert "Home Assistant" not in page


def test_status_with_an_empty_run_history_says_so_without_a_0(client: TestClient, data_dir: Path) -> None:
    """A state database that exists but holds no runs yet (distinct from no database at
    all, covered elsewhere) used to read "Last 0 runs" and "in the last 0 runs" - both odd on a
    fresh install."""
    (data_dir / "state.sqlite").unlink()
    with SqliteState(data_dir / "state.sqlite"):
        pass  # fresh schema, no runs recorded
    _login(client)

    page = client.get("/").text

    assert "0 runs" not in page
    assert "No runs yet" in page


# ---------------------------------------------------------------- failed job page: reason + remedy


def test_a_failed_plan_shows_the_reason_and_connect_spotify(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RUN_EXIT", "1")
    monkeypatch.setenv("FAKE_RUN_MESSAGE", "spotify: no token file at spotify-token.json - run `likearr auth` first")
    monkeypatch.setenv("FAKE_RUN_SPOTIFY_OK", "0")
    _login(client)

    response = client.post("/plan", data={}, follow_redirects=False)
    job_id = response.headers["location"].removeprefix("/jobs/")
    final = _wait_for_job(client, job_id)

    assert "Failed." in final
    assert "The log below says why." not in final
    assert "spotify: no token file at spotify-token.json" in final
    assert "Connect Spotify in" in final
    assert '<a href="/settings">Settings</a>' in final
    assert '<details class="panel" open>' in final


def test_a_failed_apply_with_lidarr_ok_false_shows_the_doctor_remedy(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_APPLY_EXIT", "1")
    monkeypatch.setenv("FAKE_APPLY_MESSAGE", "lidarr GET /artist: connection refused")
    monkeypatch.setenv("FAKE_APPLY_LIDARR_OK", "0")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    final = _wait_for_job(client, apply_id)

    assert "lidarr GET /artist: connection refused" in final
    assert "Check the Lidarr address and key, then run the" in final
    assert '<a href="/settings#doctor">Doctor</a>' in final


def test_a_failed_explain_with_no_fail_line_shows_its_last_error_log_line(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_EXPLAIN_ERROR",
        "source read failed: spotify: no token file at spotify-token.json - run `likearr auth` first",
    )
    _login(client)

    job_id = client.post("/explain", data={"query": "Radiohead"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "source read failed: spotify: no token file at spotify-token.json" in final
    assert "Connect Spotify in" in final
    assert '<a href="/settings">Settings</a>' in final


def test_a_failed_job_with_a_config_error_shows_the_settings_remedy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `config error:` failure (`shell/cli.py`'s top-level `except ConfigError`) prints its
    line to stdout before any command-specific code runs - no ERROR log line, no FAIL line and no
    run record. `_failure_remedy`'s `config error:` branch was only reachable from a unit test that
    called it directly; nothing in `_failure_reason` ever produced that text from a real job, so a
    browser user could never land on the remedy it names. This is the reachable path."""
    monkeypatch.setenv("FAKE_CONFIG_ERROR", "config file /x.toml is not valid TOML: bad")
    _login(client)

    job_id = client.post("/explain", data={"query": "Radiohead"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "config error: config file /x.toml is not valid TOML: bad" in final
    assert "Fix the settings in" in final
    assert '<a href="/settings">Settings</a>' in final


def test_a_failed_job_with_no_error_line_and_no_record_keeps_the_fallback_text(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCTOR_EXIT", "1")
    _login(client)
    client.post("/doctor")
    job_id = _jobs_of(data_dir, "doctor")[0].id

    final = _wait_for_job(client, job_id)

    assert "Failed. The log below says why." in final
    assert '<p class="error">' not in final


def test_failure_remedy_maps_each_flag_or_message_to_its_link() -> None:
    """The pure mapping only - whether `_failure_reason` ever hands it a `config error:` string
    from a real job is `test_a_failed_job_with_a_config_error_shows_the_settings_remedy`'s job."""
    from likearr.web.app import _failure_remedy

    spotify = _failure_remedy("spotify: no token file at x", spotify_ok=None, lidarr_ok=None)
    assert spotify[1:] == ("Settings", "/settings")
    assert "Connect Spotify" in spotify[0]

    spotify_flag = _failure_remedy("some other message entirely", spotify_ok=False, lidarr_ok=None)
    assert spotify_flag[1:] == ("Settings", "/settings")

    lidarr = _failure_remedy("lidarr GET /artist: connection refused", spotify_ok=None, lidarr_ok=False)
    assert lidarr[1:] == ("Doctor", "/settings#doctor")
    assert "Lidarr" in lidarr[0]

    config = _failure_remedy(
        "config error: config file /x.toml is not valid TOML: bad", spotify_ok=None, lidarr_ok=None
    )
    assert config[1:] == ("Settings", "/settings")
    assert "settings" in config[0].lower()

    unknown = _failure_remedy("something unrelated broke", spotify_ok=None, lidarr_ok=None)
    assert unknown == ("", "", "")


def test_the_apply_action_is_absent_when_the_plan_is_not_reviewable(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.singles_fallback_days"] = ["90"]
    client.post("/settings", data=form)

    page = client.get(f"/plan/{job_id}").text

    assert "superseded" in page.lower()
    assert f'href="/plan/{job_id}/apply"' not in page


def test_a_settings_save_supersedes_the_plan(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.singles_fallback_days"] = ["90"]
    client.post("/settings", data=form)

    page = client.get(f"/plan/{job_id}").text

    assert "superseded" in page.lower()
    assert "singles_fallback_days" in page
    assert "Check again" in page


def test_an_unknown_plan_or_section_is_a_404(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)
    explain = client.post("/explain", data={"query": "x"}, follow_redirects=False).headers["location"][6:]
    _wait_for_job(client, explain)

    assert client.get(f"/plan/{job_id}/section/nope").status_code == 404
    assert client.get(f"/plan/{explain}").status_code == 404
    assert client.get("/plan/2026-09-22T14-03-11Z-a1b2c3").status_code == 404


# ---------------------------------------------------------------- apply


def test_a_superseded_plan_cannot_be_applied(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    settings = _settings_form(client.get("/settings").text)
    settings["rules.singles_fallback_days"] = ["90"]
    client.post("/settings", data=settings)

    response = client.post(f"/plan/{job_id}/apply", data=form)
    page = client.get(f"/plan/{job_id}/apply").text

    assert response.status_code == 409
    assert "superseded" in response.text.lower()
    assert "Check again" in page
    assert 'name="plan_token"' not in page


# ---------------------------------------------------------------- "Not this one"


def test_not_this_one_refuses_the_release_through_the_settings_confirm(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _plan_monitoring(planned_diff)
    _login(client)
    job_id = _start_plan(client)
    review = client.get(f"/plan/{job_id}").text
    assert f'action="/plan/{job_id}/deny"' in review
    rg = DENIABLE

    confirm = client.post(f"/plan/{job_id}/deny", data={"release": rg})

    assert confirm.status_code == 200
    assert "Confirm this change" in confirm.text
    assert rg in confirm.text
    assert rg not in (data_dir / "config.toml").read_text()
    saved = client.post("/settings", data=_settings_form(confirm.text), follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/plan"
    assert rg in (data_dir / "config.toml").read_text()
    assert "Check again" in client.get(f"/plan/{job_id}").text  # superseded by the save, never re-planned silently


@pytest.mark.parametrize(
    ("kinds", "note"),
    [
        (("saved",), "Unsave the album on Spotify to stop this."),
        (("liked", "saved"), "Your saved album still wants this; refusing it only moves the song."),
    ],
)
def test_not_this_one_is_not_offered_where_it_changes_nothing(
    client: TestClient, data_dir: Path, planned_diff: Path, kinds: tuple[str, ...], note: str
) -> None:
    """A saved album overrides `deny_releases`, so its row says where the choice lives instead,
    and a post for it anyway is refused with the same words and writes nothing."""
    _plan_monitoring(planned_diff, kinds=kinds)
    _login(client)
    job_id = _start_plan(client)
    review = client.get(f"/plan/{job_id}").text
    before = (data_dir / "config.toml").read_text()

    refused = client.post(f"/plan/{job_id}/deny", data={"release": DENIABLE}, follow_redirects=False)

    assert f'value="{DENIABLE}"' not in review
    assert note in review.replace("&#39;", "'")
    assert refused.status_code == 303 and refused.headers["location"] == f"/plan/{job_id}"
    assert note in client.get(refused.headers["location"]).text.replace("&#39;", "'")
    assert (data_dir / "config.toml").read_text() == before


def test_not_this_one_is_offered_on_a_followed_artists_release(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    """A deny now takes a release out of a followed artist's catalogue."""
    _plan_monitoring(planned_diff, kinds=("followed",))
    _login(client)
    job_id = _start_plan(client)

    assert f'<input type="hidden" name="release" value="{DENIABLE}">' in client.get(f"/plan/{job_id}").text
    confirm = client.post(f"/plan/{job_id}/deny", data={"release": DENIABLE})
    assert confirm.status_code == 200 and "Confirm this change" in confirm.text


# ---------------------------------------------------------------- adversarial review


def test_a_reviewable_plan_survives_twenty_jobs_after_it_and_still_applies(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    for i in range(22):  # live Explain checks count towards the job store's twenty
        _wait_for_job(
            client, client.post("/explain", data={"query": f"q{i}"}, follow_redirects=False).headers["location"][6:]
        )

    assert (data_dir / "ui" / "jobs" / job_id / "diff.json").exists()
    apply_id = client.post(f"/plan/{job_id}/apply", data=form, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, apply_id)

    assert "Finished." in final
    assert (data_dir / "ui" / "jobs" / job_id / "diff.json").exists()
    # The newest twenty, plus the two that are never pruned: the plan being applied and the apply.
    # (The test clock is frozen, so every id has the same timestamp and "newest" is arbitrary.)
    assert len([p for p in (data_dir / "ui" / "jobs").iterdir()]) <= 22


# ---------------------------------------------------------------- UI design pass


def test_the_nav_says_what_each_page_is_for_and_marks_the_current_one(client: TestClient) -> None:
    _login(client)

    page = client.get("/plan").text

    nav = page[page.index("<nav>") : page.index("</nav>")]
    # The brand link now also carries the decorative icon.svg mark ahead of its text.
    labels = re.findall(r'<a href="([^"]+)"[^>]*>(?:<img[^>]*>)?([^<]+)</a>', nav)
    assert labels == [
        ("/", "likearr"),
        ("/", "Status"),
        ("/plan", "Review changes"),
        ("/explain", "Look up"),
        ("/unmatched", "Not added"),
        ("/prune", "Clean up"),
        ("/jobs", "Jobs"),
        ("/settings", "Settings"),
    ]
    assert '<a href="/plan" aria-current="page">Review changes</a>' in nav


def test_a_running_job_stays_grouped_with_log_out_not_among_the_page_links(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long job title must never push Log out onto its own row: the running indicator
    renders compact, inside the right-hand group with Log out, with the full name in `title`."""
    monkeypatch.setenv("FAKE_RUN_SLEEP", "1")
    _login(client)
    started = client.post("/plan", data={}, follow_redirects=False)
    job_id = started.headers["location"].removeprefix("/jobs/")

    page = client.get("/").text

    nav = page[page.index("<nav>") : page.index("</nav>")]
    right = nav[nav.index('class="nav-right"') :]
    assert f'href="/jobs/{job_id}"' in right
    assert 'title="Check for changes: running"' in right
    assert 'aria-label="Check for changes: running"' in right
    assert "Running&hellip;" in right
    assert "Log out" in right
    _wait_for_job(client, job_id)


def test_status_leads_with_a_health_banner_and_stat_cards(client: TestClient, data_dir: Path) -> None:
    _record_unmatched_run(data_dir)
    _login(client)

    page = client.get("/").text

    top = page[: page.index('<section id="history">')]
    assert '<div class="banner tone-ok" role="status">' in top
    assert "All good." in top
    for label in ("Automatic runs", "Last change to Lidarr", "In Lidarr", "Couldn't be added"):
        assert f'<span class="label">{label}</span>' in top
    assert '<details class="panel" id="details">' in page  # the detailed tables, collapsed


def test_no_page_uses_an_inline_style_the_csp_would_refuse(
    client: TestClient, data_dir: Path, planned_diff: Path, prune_report: Path
) -> None:
    # default-src 'self' refuses style attributes and <style> blocks: a page that relied on one
    # would look right in a test and wrong in a browser.
    _record_unmatched_run(data_dir)
    _login(client)
    job_id = _start_plan(client)
    prune_id = _build_prune(client)

    for url in [
        "/prune",
        f"/prune/{prune_id}",
        "/",
        "/plan",
        f"/plan/{job_id}",
        f"/plan/{job_id}/apply",
        f"/jobs/{job_id}",
        "/explain?query=x",
        "/unmatched",
        "/settings",
    ]:
        page = client.get(url).text
        assert 'style="' not in page, url
        assert "<style" not in page, url


# ---------------------------------------------------------------- prune review


def test_a_prune_page_for_a_job_that_is_not_one_is_a_404(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    plan_id = _start_plan(client)

    assert client.get(f"/prune/{plan_id}").status_code == 404
    assert client.get("/prune/not-a-job").status_code == 404


# ---------------------------------------------------------------- two tabs, stale exports, expiry


def test_a_prune_review_is_kept_30_days_from_its_last_use(client: TestClient, data_dir: Path) -> None:
    import os

    from likearr.web.jobs import KEEP_JOBS

    root = data_dir / "ui" / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    old = (NOW - timedelta(days=40)).isoformat()

    def plant(job_id: str, kind: str) -> Path:
        d = root / job_id
        d.mkdir()
        meta = {
            "id": job_id,
            "kind": kind,
            "argv": [],
            "label": "",
            "started_at": old,
            "finished_at": old,
            "exit_code": 0,
            "state": "done",
            "drain": False,
        }
        (d / "meta.json").write_text(json.dumps(meta))
        return d

    in_use = plant("2026-08-01T00-00-00Z-aaaaa1", "prune")
    (in_use / "prune-draft.json").write_text("{}")
    os.utime(in_use / "prune-draft.json", (NOW.timestamp(), NOW.timestamp()))
    abandoned = plant("2026-08-01T00-00-00Z-aaaaa2", "prune")
    for i in range(KEEP_JOBS + 2):
        plant(f"2026-09-01T00-00-{i:02d}Z-bbbbbb", "explain")
    _login(client)

    _wait_for_job(client, client.post("/explain", data={"query": "x"}, follow_redirects=False).headers["location"][6:])

    assert in_use.exists()
    assert not abandoned.exists()


# ---------------------------------------------------------------- playlist names load by themselves


def test_a_check_is_followed_by_a_names_fetch_when_names_are_missing(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip"})  # pl-gone has no name: names are needed
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _wait_until(lambda: _names_done(data_dir))  # the one at start
        _login(client)
        _start_plan(client)
        _wait_until(lambda: len(_jobs_of(data_dir, "playlists")) == 2 and _names_done(data_dir))

    assert len(_jobs_of(data_dir, "playlists")) == 2


def test_with_no_token_nothing_starts_a_names_fetch(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`names_needed` is false with no Spotify token, so a fresh install's empty names cache
    never starts a "Spotify playlist names" job that could only fail with "no token file ... - run
    `likearr auth` first" - not at start, not after a check, not after a settings save."""
    (data_dir / "spotify-token.json").unlink()
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _login(client)
        assert _jobs_of(data_dir, "playlists") == []  # nothing started at startup

        _start_plan(client)
        assert _jobs_of(data_dir, "playlists") == []  # nothing started after a finished check

        form = _settings_form(client.get("/settings").text)
        client.post("/settings", data=form, follow_redirects=False)
        assert _jobs_of(data_dir, "playlists") == []  # nothing started after a settings save

    assert _jobs_of(data_dir, "playlists") == []


def test_a_reauth_that_grants_the_collaborative_scope_makes_names_needed_again(
    client: TestClient, data_dir: Path
) -> None:
    """The last listing greyed out a collaborative playlist for want of
    playlist-read-collaborative. Once the token has it, that answer is stale: `names_needed` says
    re-list, so the picker and a settings save stop refusing the playlist."""
    web = _web_of(client)
    write_names(
        names_path(data_dir / "config.toml"),
        {"pl-owned": "Road", "pl-gone": "Gone", "pl-shared": "Band Van"},
        fetched_at=NOW,
        not_owned=["pl-shared"],
        needs_reauth=["pl-shared"],
    )
    token = data_dir / "spotify-token.json"
    data = json.loads(token.read_text())
    token.write_text(json.dumps({**data, "scope": "playlist-read-private"}))
    assert web.names_needed() is False

    token.write_text(json.dumps({**data, "scope": "playlist-read-private playlist-read-collaborative"}))
    assert web.names_needed() is True


# ---------------------------------------------------------------- a job the system killed

OOM_TEXT = "This ran out of memory (the container&#39;s limit) and was stopped. Nothing was changed."


def test_a_job_the_system_killed_says_it_ran_out_of_memory(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_OOM", "1")
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert OOM_TEXT in final
    assert "(exit -9)" not in final
    jobs = client.get("/jobs").text
    assert '<span class="pill tone-bad">out of memory</span>' in jobs and OOM_TEXT in jobs
    assert OOM_TEXT in client.get("/explain").text


def test_a_cancelled_job_is_never_called_out_of_memory(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXPLAIN_SLEEP", "5")
    _login(client)
    job_id = client.post("/explain", data={"query": "a"}, follow_redirects=False).headers["location"][6:]

    client.post(f"/jobs/{job_id}/cancel")
    final = _wait_for_job(client, job_id)

    assert "Cancelled." in final
    assert "out of memory" not in final


def test_an_apply_the_system_killed_says_lidarr_may_be_partly_changed() -> None:
    from likearr.web.context import oom_note
    from likearr.web.jobs import JobMeta, JobState

    def meta(kind: str, state: JobState, code: int, phase: str = "") -> JobMeta:
        return JobMeta(
            id="x",
            kind=kind,
            argv=[],
            label="",
            started_at="",
            finished_at="",
            exit_code=code,
            state=state,
            drain=kind == "apply",
            phase=phase,
        )

    assert "partly changed" in oom_note(meta("apply", JobState.FAILED, -9))
    assert "Nothing was changed" in oom_note(meta("prune", JobState.FAILED, -9))
    assert oom_note(meta("prune", JobState.FAILED, 1)) == ""
    assert oom_note(meta("prune", JobState.CANCELLED, -9)) == ""

    # A scheduled run killed after it began applying: the phase says so, regardless of "kind".
    assert "partly changed" in oom_note(meta("scheduled", JobState.FAILED, -9, phase="apply"))
    # A scheduled run killed while still planning: nothing was written yet.
    assert "Nothing was changed" in oom_note(meta("scheduled", JobState.FAILED, -9, phase=""))


# ---------------------------------------------------------------- look up cards


def test_a_look_up_card_shows_no_bare_mbids_outside_its_details(client: TestClient, data_dir: Path) -> None:
    import re

    _record_last_run(data_dir)
    _login(client)

    page = client.get("/explain", params={"query": "Jungle"}).text
    main = page[page.index("<main") : page.index("</main>")]
    shown = re.sub(r"<details>.*?</details>", "", main, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", shown)  # attribute values (hrefs, hidden inputs) are not text

    assert "Details</summary>" in main
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
    assert "2 matches." in text


def test_look_up_reads_only_safe_values_from_a_report() -> None:
    from likearr.web.app import _lookup_view

    view = _lookup_view(
        {
            "summary": [
                {
                    "text": "t",
                    "status": "nonsense",
                    "facts": [["On Spotify", "x"], ["bad"], "bad"],
                    "links": [{"label": "x", "url": "javascript:alert(1)"}],
                    "lidarr_path": "javascript:alert(1)",
                    "release": "../../settings",
                },
            ],
            "details": "",
        }
    )

    (card,) = view["summary"]
    assert card["status"] == "" and card["headline"] == "t"
    assert card["facts"] == [("On Spotify", "x")]
    assert card["links"] == [] and card["lidarr_path"] == "" and card["release"] == ""


def test_not_this_one_on_a_look_up_card_goes_through_the_settings_confirm(client: TestClient, data_dir: Path) -> None:
    from tests.unit.test_explain import JUNGLE_1969

    _login(client)
    assert client.post("/explain/deny", data={"release": "not-an-mbid"}).status_code == 400
    _record_last_run(data_dir)

    page = client.get("/explain", params={"query": "Busy Earnin'"}).text
    confirm = client.post("/explain/deny", data={"release": JUNGLE_1969.mbid, "query": "Busy Earnin'"})

    assert f'<input type="hidden" name="release" value="{JUNGLE_1969.mbid}">' in page
    assert '<input type="hidden" name="query" value="Busy Earnin&#39;">' in page
    assert confirm.status_code == 200 and "deny_releases" in confirm.text
    assert JUNGLE_1969.mbid in confirm.text
    assert "deny_releases" not in (data_dir / "config.toml").read_text()  # nothing saved before the confirm


def test_not_this_one_for_a_release_a_newer_run_let_go_goes_back_with_a_note(
    client: TestClient, data_dir: Path
) -> None:
    _login(client)
    _record_last_run(data_dir)

    gone = client.post(
        "/explain/deny",
        data={"release": "11111111-2222-3333-4444-555555555555", "query": "Busy Earnin'"},
        follow_redirects=False,
    )

    assert gone.status_code == 303
    assert gone.headers["location"] == "/explain?query=Busy+Earnin%27"
    assert (
        "That release isn&#39;t wanted any more by the latest run - nothing to do."
        in client.get(gone.headers["location"]).text
    )


def _record_saved_last_run(data_dir: Path) -> str:
    """The Jungle last run, but the release is wanted because the album was saved."""
    from likearr.shell.last_run import last_run_facts, write_last_run
    from tests.unit.fakes import lidarr_artist, lidarr_view, reason
    from tests.unit.test_diff import desired_state
    from tests.unit.test_explain import EXISTING, JUNGLE_1969, JUNGLE_COLLISION, JUNGLE_SNAPSHOT

    saved = reason(ReasonKind.SAVED, "sp-jungle")
    facts = last_run_facts(
        ran_at=NOW - timedelta(hours=2),
        snapshot=JUNGLE_SNAPSHOT,
        resolutions={},
        artist_resolutions={},
        desired=desired_state((JUNGLE_1969, [saved])),
        view=lidarr_view(artists=[lidarr_artist(EXISTING, id=9003, name="Jungle")]),
        owned_keys=(),
        collisions=[JUNGLE_COLLISION],
    )
    write_last_run(data_dir / "last-run.json", facts)
    return JUNGLE_1969.mbid


def test_not_this_one_from_look_up_is_refused_for_a_saved_album(client: TestClient, data_dir: Path) -> None:
    """The server refuses it too, with the reason, and writes nothing."""
    _login(client)
    release = _record_saved_last_run(data_dir)
    before = (data_dir / "config.toml").read_text()

    refused = client.post("/explain/deny", data={"release": release, "query": "Jungle"}, follow_redirects=False)

    assert refused.status_code == 303 and refused.headers["location"] == "/explain?query=Jungle"
    assert "Unsave the album on Spotify to stop this." in client.get(refused.headers["location"]).text
    assert (data_dir / "config.toml").read_text() == before


def test_a_live_check_card_has_no_not_this_one(client: TestClient) -> None:
    _login(client)

    job_id = client.post("/explain", data={"query": "Jungle"}, follow_redirects=False).headers["location"][6:]
    final = _wait_for_job(client, job_id)

    assert "Not this one" not in final


# ---------------------------------------------------------------- files on disk behind an unmonitor


def test_a_job_adopted_after_a_restart_says_so_and_offers_no_cancel(data_dir: Path, fake_cli: list[str]) -> None:
    job_id = "2026-09-23T17-00-00Z-a1b2c3"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": "explain",
        "argv": ["likearr", "explain", "--json", "--", "Jungle"],
        "label": "Jungle",
        "started_at": "2026-09-23T17:00:00+00:00",
        "finished_at": None,
        "exit_code": None,
        "state": "running",
        "drain": False,
        "pid": 4242,
        "pid_start": "123",
        "adopted": True,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    client = TestClient(_app(data_dir, fake_cli), base_url="http://testserver")  # no lifespan: no recover()
    _login(client)

    page = client.get(f"/jobs/{job_id}").text

    assert "Still running from before likearr restarted" in page
    assert "can't be cancelled here" in page
    assert f'action="/jobs/{job_id}/cancel"' not in page


# ---------------------------------------------------------------- the collision card's actions


def _collision_on_status(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Jungle collision recorded by the last run, the last run's facts, and a fake check whose
    diff reports the same collision."""
    from likearr.config import load_config
    from likearr.shell.diff_io import write_diff
    from tests.adapters.test_state_sqlite import _diff
    from tests.unit.test_explain import JUNGLE_COLLISION

    diff = _diff()
    diff.name_collisions.append(JUNGLE_COLLISION)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _record_last_run(data_dir)
    diff.config_fingerprint = load_config(data_dir / "config.toml").plan_fingerprint
    path = data_dir / "fixture-collision-diff.json"
    write_diff(diff, path)
    monkeypatch.setenv("FAKE_DIFF", str(path))


def test_a_collision_card_offers_check_again_and_not_this_one_for_each_wanted_release(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_explain import JUNGLE_1969

    _collision_on_status(data_dir, monkeypatch)
    _login(client)

    card = client.get("/").text.split('<section id="collisions">', 1)[1].split("</section>", 1)[0]

    assert '<form method="post" action="/plan" class="inline"><button type="submit" class="small"' in card
    assert ">Check again</button>" in card
    assert f'<input type="hidden" name="release" value="{JUNGLE_1969.mbid}">' in card
    assert '<form method="post" action="/explain/deny" class="inline">' in card
    assert '<input type="hidden" name="query" value="Jungle">' in card
    assert "Accept as known</a>" not in card  # no check to apply yet
    assert "To accept it as known, check again first" in card

    confirm = client.post("/explain/deny", data={"release": JUNGLE_1969.mbid, "query": "Jungle"})
    assert confirm.status_code == 200 and "deny_releases" in confirm.text
    assert "deny_releases" not in (data_dir / "config.toml").read_text()  # nothing saved before the confirm


def test_a_collision_card_offers_no_not_this_one_for_a_saved_album(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card lists only releases "Not this one" can stop."""
    _collision_on_status(data_dir, monkeypatch)
    release = _record_saved_last_run(data_dir)
    _login(client)

    card = client.get("/").text.split('<section id="collisions">', 1)[1].split("</section>", 1)[0]

    assert f'value="{release}"' not in card
    assert 'action="/explain/deny"' not in card


def test_accept_as_known_opens_the_apply_confirm_with_accept_health_ticked(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `[health.mqtt]` configured, the collisions card and the apply confirm's
    accept banner still name Home Assistant."""
    _collision_on_status(data_dir, monkeypatch)
    _enable_mqtt(data_dir)
    _login(client)
    job_id = _start_plan(client)

    card = client.get("/").text.split('<section id="collisions">', 1)[1]
    link = f'href="/plan/{job_id}/apply?accept_health=1"'
    assert f'<a class="button small" {link}>Accept as known</a>' in card
    assert "stops flagging this as a problem" in card

    preset = client.get(f"/plan/{job_id}/apply", params={"accept_health": "1"}).text
    plain = client.get(f"/plan/{job_id}/apply").text
    assert '<input type="checkbox" name="accept_health" checked>' in preset
    assert "<details open>" in preset and "Accept the name collision as known" in preset
    assert "including ones that appeared after this check" in preset  # what the box really accepts
    assert "stops flagging the skip as a problem, here and in Home Assistant" in preset
    assert '<input type="checkbox" name="accept_health">' in plain and "Accept the name collision" not in plain

    token = re.search(r'name="plan_token" value="([0-9a-f]+)"', preset)
    assert token is not None
    applied = client.post(
        f"/plan/{job_id}/apply", data={"plan_token": token[1], "accept_health": "on"}, follow_redirects=False
    )
    apply_id = applied.headers["location"].removeprefix("/jobs/")
    assert json.loads((data_dir / "ui" / "jobs" / apply_id / "meta.json").read_text())["argv"][-1] == "--accept-health"
    assert "--force" not in json.loads((data_dir / "ui" / "jobs" / apply_id / "meta.json").read_text())["argv"]


def test_accept_as_known_says_status_not_home_assistant_without_mqtt(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `[health.mqtt]` in the fixture config: the collisions card and the apply
    confirm's accept banner name Status, never Home Assistant."""
    _collision_on_status(data_dir, monkeypatch)
    _login(client)
    job_id = _start_plan(client)

    card = client.get("/").text.split('<section id="collisions">', 1)[1]
    assert "stops flagging this as a problem" in card
    assert "Home Assistant" not in card

    preset = client.get(f"/plan/{job_id}/apply", params={"accept_health": "1"}).text
    assert "stops flagging the skip as a problem" in preset
    assert "Home Assistant" not in preset


def test_accept_as_known_is_only_offered_for_a_check_that_reports_the_collision(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch, planned_diff: Path
) -> None:
    """The newest check reports no collision (the planned_diff fixture has none): nothing to accept."""
    from tests.adapters.test_state_sqlite import _diff
    from tests.unit.test_explain import JUNGLE_COLLISION

    diff = _diff()
    diff.name_collisions.append(JUNGLE_COLLISION)
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _login(client)
    job_id = _start_plan(client)

    card = client.get("/").text.split('<section id="collisions">', 1)[1]

    assert f"/plan/{job_id}/apply?accept_health=1" not in card
    assert "To accept it as known, check again first" in card


@pytest.mark.parametrize(
    ("scope", "noted"),
    [
        ("user-follow-read user-library-read playlist-read-private", True),
        ("user-follow-read user-library-read playlist-read-private playlist-read-collaborative", False),
    ],
)
def test_status_and_settings_say_to_re_authorize_for_collaborative_playlists_only_when_needed(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch, scope: str, noted: bool
) -> None:
    """A token from before likearr asked for playlist-read-collaborative keeps working,
    so this is a note on Status and Settings, never a problem that turns the banner amber."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    token = data_dir / "spotify-token.json"
    token.write_text(json.dumps({**json.loads(token.read_text()), "scope": scope}))
    _login(client)

    status = client.get("/").text
    settings = client.get("/settings").text

    assert ("To also sync playlists you collaborate on, re-authorize" in status) is noted
    assert ("Re-authorize to also sync playlists you collaborate on" in settings) is noted
    assert "All good." in status


def test_last_change_says_how_many_albums_already_monitored_are_now_managed(client: TestClient, data_dir: Path) -> None:
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(
            _record(ts=int(NOW.timestamp()) - 60, counts={"monitored": 1, "unmonitored": 2, "claimed": 5}), None
        )
    _login(client)

    card = _card(client.get("/").text, "last-change")

    assert "Monitored 1 release, unmonitored 2, added 0 artists. 5 albums you already monitored now managed." in card


# ---------------------------------------------------------------- a fire waiting in the queue


def test_a_fire_waiting_in_the_queue_is_not_paired_with_the_previous_job(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_job = _scheduled_fire(data_dir, "done")
    with SqliteState(data_dir / "state.sqlite") as db:
        db.record_scheduled_fire(NOW - timedelta(minutes=2))
    monkeypatch.setattr(_web_of(client).runner, "scheduled_pending", lambda: True)
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: waiting for the running job" in card
    assert f"/jobs/{old_job}" not in card
    assert "disabled" in _run_now_button(card)


def test_a_fire_is_matched_to_its_job_at_whole_second_precision(client: TestClient, data_dir: Path) -> None:
    job_id = _scheduled_fire(data_dir, "done")
    with SqliteState(data_dir / "state.sqlite") as db:
        db.record_scheduled_fire(NOW - timedelta(hours=1) + timedelta(microseconds=900_000))
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: finished" in card
    assert f'<a href="/jobs/{job_id}">details</a>' in card


def test_a_fire_whose_job_is_gone_shows_only_its_time(client: TestClient, data_dir: Path) -> None:
    old_job = _scheduled_fire(data_dir, "failed")
    with SqliteState(data_dir / "state.sqlite") as db:
        db.record_scheduled_fire(NOW - timedelta(minutes=30))
    _login(client)

    card = _card(client.get("/").text, "automatic-runs")

    assert "Last: 30 min ago" in card
    assert f"/jobs/{old_job}" not in card and "tone-warn" not in card


# ---------------------------------------------------------------- Run and apply now, twice


def test_run_now_refuses_while_a_scheduled_run_is_running_or_queued(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_web_of(client).runner, "scheduled_pending", lambda: True)
    _login(client)

    response = client.post("/run-now", follow_redirects=False)

    assert response.status_code == 303
    page = client.get("/").text
    assert "A scheduled run is already running or waiting, so no second one was started." in page
    jobs_dir = data_dir / "ui" / "jobs"
    assert not (jobs_dir.is_dir() and list(jobs_dir.iterdir()))
    with SqliteState(data_dir / "state.sqlite") as state:
        assert state.last_scheduled_fire() is None


def test_the_run_now_form_disables_itself_on_submit(client: TestClient) -> None:
    _login(client)

    page = client.get("/").text

    assert '<form method="post" action="/run-now" data-submit-once>' in page
    assert '<script src="/static/forms.js" defer></script>' in page
    assert "data-submit-once" in client.get("/static/forms.js").text


# ---------------------------------------------------------------- the job page's phase


def _job_dir(data_dir: Path, kind: str, state: str, log: str) -> str:
    job_id = "2026-09-23T17-30-00Z-b0b0b0"
    job_dir = data_dir / "ui" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    meta = {
        "id": job_id,
        "kind": kind,
        "argv": ["likearr", "run", "--apply"],
        "label": kind,
        "started_at": (NOW - timedelta(minutes=30)).isoformat(),
        "finished_at": None if state == "running" else NOW.isoformat(),
        "exit_code": None,
        "state": state,
        "drain": True,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    (job_dir / "log.txt").write_text(log)
    (job_dir / "out.txt").write_text("")
    return job_id


def test_the_phase_line_reads_the_whole_log(client: TestClient, data_dir: Path) -> None:
    log = "x INFO likearr.shell.run: sources read: saved_albums=1\nlikearr-phase: apply\n"
    log += "".join(f"x INFO likearr.shell.run: added artist {i}\n" for i in range(300))
    job_id = _job_dir(data_dir, "apply", "running", log)
    _login(client)

    page = client.get(f"/jobs/{job_id}").text

    assert "Applying changes to Lidarr" in page
    assert "Reading Spotify" not in page


# ---------------------------------------------------------------- an apply cut off part-way


def test_an_apply_cut_off_after_it_began_says_lidarr_may_be_partly_changed(client: TestClient, data_dir: Path) -> None:
    job_id = _job_dir(data_dir, "apply", "interrupted", "likearr-phase: apply\n" + "noise\n" * 300)
    _login(client)

    assert "Lidarr may be partly changed" in client.get(f"/jobs/{job_id}").text


def test_an_apply_cut_off_while_planning_says_only_interrupted(client: TestClient, data_dir: Path) -> None:
    job_id = _job_dir(data_dir, "apply", "interrupted", "planning\n")
    _login(client)

    page = client.get(f"/jobs/{job_id}").text

    assert "Interrupted: likearr stopped or restarted while this was running." in page
    assert "partly changed" not in page


# ---------------------------------------------------------------- a folder likearr can't write


def _refuse_write(monkeypatch: pytest.MonkeyPatch, exc: OSError) -> None:
    from likearr.web import settings as cfg

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise exc

    monkeypatch.setattr(cfg, "write_config", refuse)


def _pause(client: TestClient, **headers: str) -> Any:
    return client.post(
        "/settings/pause",
        data={"file_hash": _file_hash(client), "reason": "away"},
        headers=headers,
        follow_redirects=False,
    )


def test_an_unwritable_data_folder_gets_a_page_naming_it_and_the_fix(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    _login(client)
    _refuse_write(monkeypatch, PermissionError(errno.EACCES, "Permission denied", "/data/.config.toml.ab12.tmp"))

    response = _pause(client)

    assert response.status_code == 403
    assert "Nothing was saved" in response.text
    assert "likearr can&#39;t write to /data. Give the user likearr runs as write access to it" in response.text
    assert "Internal Server Error" not in response.text


def test_a_single_file_mount_of_config_toml_is_named_as_that(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    _login(client)
    busy = OSError(errno.EBUSY, "Device or resource busy", "/data/.config.toml.ab12.tmp", None, "/data/config.toml")
    _refuse_write(monkeypatch, busy)

    response = _pause(client, **{"HX-Request": "true"})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/plain")
    assert "can't replace config.toml: it is mounted into the container as a single file" in response.text


def test_a_read_only_mount_is_named_and_other_os_errors_stay_server_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    _login(client)
    _refuse_write(monkeypatch, OSError(errno.EROFS, "Read-only file system", "/data/config.toml"))
    assert "likearr can&#39;t save to /data: it is read-only." in _pause(client).text

    _refuse_write(monkeypatch, OSError(errno.EIO, "I/O error", "/data/config.toml"))
    with pytest.raises(OSError, match="I/O error"):
        _pause(client)
