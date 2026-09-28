"""The Settings page's routes end to end (`likearr.web.routes.settings`): the settings form,
pause and schedule, the playlist picker, Spotify connect, Lidarr setup and Doctor.

Split out of `test_app.py` with the routes themselves; the shared fixtures are in
`conftest.py`, the fake CLI and the other shared helpers in `app_support.py`.
"""

from __future__ import annotations

import html
import json
import logging
import re
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from likearr.web.app import WebSettings, create_app
from tests.web.app_support import (
    _PUBLIC_URL,
    API_KEY_SENTINEL,
    CONFIG,
    FIRST_APPLY_LINE,
    NOW,
    PASSWORD,
    _app,
    _cache_names,
    _clean_up_off,
    _file_hash,
    _forget_first_apply,
    _jobs_of,
    _login,
    _names_done,
    _settings_form,
    _wait_for_picker,
    _wait_until,
)

# ---------------------------------------------------------------- pause / resume


def test_pausing_saves_at_once_no_confirm(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)

    response = client.post(
        "/settings/pause", data={"file_hash": file_hash, "reason": "maintenance window"}, follow_redirects=False
    )

    assert response.status_code == 303
    text = (data_dir / "config.toml").read_text()
    assert "enabled = false" in text
    assert "maintenance window" in text
    page = client.get("/settings").text
    assert "Paused since" in page
    assert "maintenance window" in page
    assert "Resume scheduled runs" in page


def test_resuming_needs_a_second_confirm(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)
    client.post("/settings/pause", data={"file_hash": file_hash, "reason": "testing"}, follow_redirects=False)
    paused_hash = _file_hash(client)

    first = client.post("/settings/resume", data={"file_hash": paused_hash}, follow_redirects=False)

    assert first.status_code == 200
    assert "turns unattended applies back on" in first.text
    digest = re.search(r'name="confirm_digest" value="([^"]+)"', first.text)[1]  # type: ignore[index]
    assert "enabled = false" in (data_dir / "config.toml").read_text()  # still paused: not confirmed yet

    second = client.post(
        "/settings/resume",
        data={"file_hash": paused_hash, "confirm_digest": digest},
        follow_redirects=False,
    )

    assert second.status_code == 303
    text = (data_dir / "config.toml").read_text()
    assert "enabled = true" in text
    assert "Resume scheduled runs" not in client.get("/settings").text


def test_pausing_with_a_stale_file_hash_saves_nothing(client: TestClient, data_dir: Path) -> None:
    _login(client)
    before = (data_dir / "config.toml").read_text()

    response = client.post("/settings/pause", data={"file_hash": "stale", "reason": "x"}, follow_redirects=False)

    assert response.status_code == 409
    assert (data_dir / "config.toml").read_text() == before


# ---------------------------------------------------------------- schedule editing and Run now


def test_settings_shows_a_schedule_preview(client: TestClient) -> None:
    _login(client)

    page = client.get("/settings").text

    assert 'name="schedule.cron"' in page
    assert "20 */6 * * *" in page
    assert "Next fires:" in page


def test_settings_says_scheduled_runs_are_enabled_without_the_cli_flags(client: TestClient) -> None:
    """Settings speaks in plain language, not CLI flags or the Home Assistant amber
    state - `likearr setup-profiles` and `run --scheduled --apply` are gone, and the guard field
    talks about Status, not amber."""
    _login(client)

    page = client.get("/settings").text

    assert "Scheduled runs are on." in page
    assert "run --scheduled" not in page
    assert "setup-profiles" not in page
    assert "before amber" not in page  # the old field label
    assert "turning amber" not in page  # the old confirm-text ending
    assert "Unmapped share before Status needs attention (0-1)" in page


def test_saving_a_less_frequent_schedule_saves_at_once(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)

    response = client.post(
        "/settings/schedule",
        data={"file_hash": file_hash, "schedule.cron": "0 0 * * *", "schedule.timezone": "America/New_York"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    text = (data_dir / "config.toml").read_text()
    assert 'cron = "0 0 * * *"' in text


def test_saving_a_more_frequent_schedule_needs_a_second_confirm(client: TestClient, data_dir: Path) -> None:
    # Hourly - exactly MIN_SCHEDULE_INTERVAL_MINUTES (60), and more often than the fixture's
    # default "20 */6 * * *" (every 6h).
    _login(client)
    file_hash = _file_hash(client)

    first = client.post(
        "/settings/schedule",
        data={"file_hash": file_hash, "schedule.cron": "0 * * * *", "schedule.timezone": "America/New_York"},
        follow_redirects=False,
    )

    assert first.status_code == 200
    assert "fires more often" in first.text
    digest = re.search(r'name="confirm_digest" value="([^"]+)"', first.text)[1]  # type: ignore[index]
    assert 'cron = "0 * * * *"' not in (data_dir / "config.toml").read_text()  # not saved yet

    second = client.post(
        "/settings/schedule",
        data={
            "file_hash": file_hash,
            "schedule.cron": "0 * * * *",
            "schedule.timezone": "America/New_York",
            "confirm_digest": digest,
        },
        follow_redirects=False,
    )

    assert second.status_code == 303
    assert 'cron = "0 * * * *"' in (data_dir / "config.toml").read_text()


def test_a_bad_cron_line_is_shown_inline_and_saves_nothing(client: TestClient, data_dir: Path) -> None:
    _login(client)
    file_hash = _file_hash(client)
    before = (data_dir / "config.toml").read_text()

    response = client.post(
        "/settings/schedule",
        data={"file_hash": file_hash, "schedule.cron": "not a cron line", "schedule.timezone": "UTC"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert (data_dir / "config.toml").read_text() == before


# ---------------------------------------------------------------- live schedule preview


def test_the_live_schedule_preview_shows_fires_in_the_typed_timezone(client: TestClient) -> None:
    _login(client)

    response = client.get(
        "/settings/schedule/preview", params={"schedule.cron": "0 6 * * *", "schedule.timezone": "Europe/Paris"}
    )

    assert response.status_code == 200
    assert "Next fires:" in response.text
    assert "Every day at 06:00" in response.text
    assert "error" not in response.text


def test_the_live_schedule_preview_reports_a_bad_cron_line(client: TestClient, data_dir: Path) -> None:
    _login(client)
    before = (data_dir / "config.toml").read_text()

    response = client.get(
        "/settings/schedule/preview", params={"schedule.cron": "not a cron line", "schedule.timezone": "UTC"}
    )

    assert response.status_code == 200
    assert "five fields" in response.text
    assert (data_dir / "config.toml").read_text() == before  # read-only: no file_hash, no write


def test_the_live_schedule_preview_reports_a_bad_timezone(client: TestClient) -> None:
    _login(client)

    response = client.get(
        "/settings/schedule/preview", params={"schedule.cron": "0 6 * * *", "schedule.timezone": "Nowhere/At_All"}
    )

    assert response.status_code == 200
    assert "is not an IANA timezone name" in response.text


def test_the_live_schedule_preview_needs_login(client: TestClient) -> None:
    response = client.get(
        "/settings/schedule/preview",
        params={"schedule.cron": "0 6 * * *", "schedule.timezone": "UTC"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------- the first reviewed apply


def test_settings_says_scheduled_runs_start_after_the_first_reviewed_apply_until_one_has(
    client: TestClient, data_dir: Path
) -> None:
    _login(client)
    assert FIRST_APPLY_LINE not in client.get("/settings").text

    _forget_first_apply(data_dir)
    page = client.get("/settings").text

    assert FIRST_APPLY_LINE in page
    assert "Scheduled runs are on." in page, "the schedule is on; it is waiting, not paused"


def test_settings_with_no_state_database_says_so_and_creates_none(client: TestClient, data_dir: Path) -> None:
    for path in data_dir.glob("state.sqlite*"):
        path.unlink()
    _login(client)

    assert FIRST_APPLY_LINE in client.get("/settings").text
    assert not (data_dir / "state.sqlite").exists()


# ---------------------------------------------------------------- settings


def test_settings_has_three_save_buttons_one_per_editable_fieldset(client: TestClient) -> None:
    """Save reachable without scrolling past the other fieldsets - but still one form, one
    `file_hash`, one save path, so any of the three submits every field."""
    _login(client)

    page = client.get("/settings").text
    form = re.search(r'<form method="post" action="/settings".*?</form>', page, re.S)[0]  # type: ignore[index]

    assert form.count("<fieldset>") == 3
    submits = re.findall(r"<button[^>]*>Save</button>", form)
    assert len(submits) == 3
    assert all('type="submit"' in b for b in submits)
    assert form.count('name="file_hash"') == 1  # only the one hidden input, shared by every Save


def test_settings_renders_only_the_allowlisted_keys(client: TestClient) -> None:
    _login(client)

    page = client.get("/settings").text
    form = re.search(r'<form method="post" action="/settings".*?</form>', page, re.S)[0]  # type: ignore[index]

    names = set(re.findall(r'name="([^"]+)"', form))
    assert names == {
        "file_hash",
        "picker",  # the picker's marker: the playlists below are the form's, not the file's
        "spotify.followed_artists",
        "spotify.saved_albums",
        "spotify.liked_tracks",
        "spotify.playlists",
        "rules.liked_track_scope",
        "rules.singles_fallback_days",
        "rules.recent_release_days",
        "rules.albums_only_tag",
        "rules.allow_compilation_fallback",
        "rules.allow_remix_releases",
        "rules.keep_remix_only_tracks",
        "rules.deny_releases",
        "guards.max_unmonitors_scheduled",
        "guards.source_shrink_pct",
        "guards.artist_shrink_pct",
        "guards.unmapped_ratio_amber",
        "guards.projected_wanted_max",
    }


def _field_div(page: str, name: str) -> str:
    start = page.index(f'id="field-{name}"')
    return page[start : page.index("</div>", start)]


def test_settings_shows_a_help_line_under_every_guard(client: TestClient) -> None:
    """The five Guards had no help text at all - a bare number under each label."""
    _login(client)

    page = client.get("/settings").text

    for key in (
        "max_unmonitors_scheduled",
        "source_shrink_pct",
        "artist_shrink_pct",
        "unmapped_ratio_amber",
        "projected_wanted_max",
    ):
        field = _field_div(page, f"guards.{key}")
        assert '<p class="help">' in field, f"guards.{key} has no help line"


def test_settings_no_longer_shows_the_flagged_jargon(client: TestClient) -> None:
    """The reworded help drops "Release group MBIDs" and "catalogue gap this new" jargon."""
    _login(client)

    page = client.get("/settings").text

    assert "Release group MBIDs" not in page
    assert "catalogue gap this new" not in page


def test_liked_track_scope_shows_sentence_labels_with_the_stored_values_as_option_values(
    client: TestClient,
) -> None:
    """The select shows a sentence per option, but `value` stays "album" / "smallest"."""
    _login(client)

    field = _field_div(client.get("/settings").text, "rules.liked_track_scope")

    assert '<option value="album"' in field
    assert '<option value="smallest"' in field
    assert ">album</option>" not in field  # the raw value is no longer the visible text
    assert ">smallest</option>" not in field
    assert "The studio album or EP the song is on" in field
    assert "The smallest release that has the song" in field


def test_saving_each_liked_track_scope_option_writes_the_same_stored_value(client: TestClient, data_dir: Path) -> None:
    """Sentence labels in the dropdown must not change what gets written to config.toml.

    Both options re-resolve every liked and playlist track (`_RE_RESOLVE`), so each save needs the
    usual second confirm - see `test_a_re_resolving_change_asks_first_then_saves`.
    """
    _login(client)
    for value in ("smallest", "album"):
        form = _settings_form(client.get("/settings").text)
        form["rules.liked_track_scope"] = [value]

        confirm = client.post("/settings", data=form)
        assert confirm.status_code == 200
        assert "Confirm this change" in confirm.text

        carried = _settings_form(confirm.text)
        response = client.post("/settings", data=carried, follow_redirects=False)

        assert response.status_code == 303
        assert f'liked_track_scope = "{value}"' in (data_dir / "config.toml").read_text()


def test_a_save_backs_up_and_rewrites_keeping_comments(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.singles_fallback_days"] = ["90"]

    response = client.post("/settings", data=form, follow_redirects=False)

    assert response.status_code == 303
    text = (data_dir / "config.toml").read_text()
    assert "singles_fallback_days = 90" in text
    assert "# likearr - fixture config for the web tests." in text
    assert 'liked_track_scope = "album"  # keep' in text
    backups = [p for p in data_dir.iterdir() if ".bak-" in p.name]
    assert len(backups) == 1
    assert backups[0].read_text() == CONFIG
    assert "Saved rules.singles_fallback_days" in client.get("/settings").text


def test_an_untouched_save_writes_nothing(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)

    client.post("/settings", data=form)

    assert (data_dir / "config.toml").read_text() == CONFIG
    assert not [p for p in data_dir.iterdir() if ".bak-" in p.name]


def test_a_hand_edit_while_the_page_was_open_is_never_reverted(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    (data_dir / "config.toml").write_text(CONFIG.replace("album", "smallest"))
    form["rules.singles_fallback_days"] = ["90"]

    response = client.post("/settings", data=form)

    assert response.status_code == 409
    assert "changed since you opened this page" in response.text
    assert 'liked_track_scope = "smallest"' in (data_dir / "config.toml").read_text()
    assert "singles_fallback_days" not in (data_dir / "config.toml").read_text()


def test_an_invalid_value_is_shown_at_its_field_and_nothing_is_written(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.deny_releases"] = ["not-an-mbid"]

    response = client.post("/settings", data=form)

    assert response.status_code == 400
    assert "is not a MusicBrainz release group id" in response.text
    assert (data_dir / "config.toml").read_text() == CONFIG


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("guards.max_unmonitors_scheduled", "-5"),
        ("guards.source_shrink_pct", "150"),
        ("rules.singles_fallback_days", "-1"),
    ],
)
def test_an_out_of_range_number_is_shown_at_its_field_and_nothing_is_written(
    client: TestClient, data_dir: Path, field: str, value: str
) -> None:
    """`parse_form` leaves ranges to `parse_config`, which now has them."""
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form[field] = [value]

    response = client.post("/settings", data=form)

    assert response.status_code == 400
    section, key = field.split(".")
    assert f"[{section}] {key} must be" in response.text
    assert (data_dir / "config.toml").read_text() == CONFIG
    assert not [p for p in data_dir.iterdir() if ".bak-" in p.name]


def test_a_re_resolving_change_asks_first_then_saves(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.liked_track_scope"] = ["smallest"]
    form["rules.deny_releases"] = ["0f0f0f0f-1111-2222-3333-444444444444"]

    confirm = client.post("/settings", data=form)
    assert confirm.status_code == 200
    assert "Confirm this change" in confirm.text
    assert "resolve" in confirm.text
    assert (data_dir / "config.toml").read_text() == CONFIG

    carried = _settings_form(confirm.text)
    response = client.post("/settings", data=carried, follow_redirects=False)

    assert response.status_code == 303
    text = (data_dir / "config.toml").read_text()
    assert 'liked_track_scope = "smallest"' in text
    assert "0f0f0f0f-1111-2222-3333-444444444444" in text
    assert 'playlists = ["pl-owned", "pl-gone"]' in text


# ---------------------------------------------------------------- readable confirm, IDs on demand


def test_the_confirm_page_reads_a_bool_change_as_on_off(client: TestClient) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.allow_remix_releases"] = []  # unchecked: True -> False, and this re-resolves

    confirm = client.post("/settings", data=form)

    assert confirm.status_code == 200
    assert "Allow remix releases" in confirm.text
    before_details = confirm.text.split("<details>", 1)[0]
    assert "on -&gt; off" in before_details
    assert "rules.allow_remix_releases" not in before_details  # the raw key is inside <details>
    assert "rules.allow_remix_releases" in confirm.text  # ...but present, inside it


def test_the_confirm_page_names_playlists_and_keeps_ids_in_details(client: TestClient) -> None:
    _login(client)
    waiting = client.post("/settings/playlists")
    job_id = re.search(r"/settings/playlists/([^\"]+)\"", waiting.text)[1]  # type: ignore[index]
    deadline = time.monotonic() + 20
    while (final := client.get(f"/settings/playlists/{job_id}")).status_code != 286:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert "Gym" in final.text  # names are cached now

    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = ["pl-owned", "pl-other"]  # drop pl-gone (unnamed), add pl-other ("Gym")

    confirm = client.post("/settings", data=form)

    assert confirm.status_code == 200
    assert "Playlists" in confirm.text
    before_details = confirm.text.split("<details>", 1)[0]
    assert "added: Gym" in before_details
    assert "pl-other" not in before_details  # the id of a named playlist stays out of the main text
    assert "pl-other" in confirm.text  # ...but is still there, inside <details>


def test_the_confirm_page_counts_refused_releases_and_keeps_the_mbid_in_details(client: TestClient) -> None:
    _login(client)
    mbid = "0f0f0f0f-1111-2222-3333-444444444444"
    form = _settings_form(client.get("/settings").text)
    form["rules.deny_releases"] = [mbid]

    confirm = client.post("/settings", data=form)

    assert confirm.status_code == 200
    assert "Refused releases" in confirm.text
    before_details = confirm.text.split("<details>", 1)[0]
    assert "1 release added to the refused list" in before_details
    assert mbid not in before_details  # the MBID stays out of the main text
    assert mbid in confirm.text  # ...but is still there, inside <details>


def test_a_forged_post_cannot_touch_a_key_outside_the_allowlist(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["lidarr.url"] = ["http://evil.example"]
    form["state.db"] = ["/tmp/x.sqlite"]

    client.post("/settings", data=form)

    assert (data_dir / "config.toml").read_text() == CONFIG


# ---------------------------------------------------------------- the playlist picker


def test_the_picker_starts_with_the_configured_ids_and_a_button_to_ask_spotify(client: TestClient) -> None:
    _login(client)

    page = client.get("/settings").text

    assert 'hx-post="/settings/playlists"' in page
    assert 'hx-trigger="load"' not in page
    assert 'value="pl-owned" checked' in page
    assert 'value="pl-gone" checked' in page


def test_the_picker_names_owned_playlists_and_flags_a_configured_one_that_is_gone(client: TestClient) -> None:
    _login(client)

    waiting = client.post("/settings/playlists")
    assert 'name="spotify.playlists" value="pl-owned"' in waiting.text  # still savable while it waits
    job_id = re.search(r"/settings/playlists/([^\"]+)\"", waiting.text)[1]  # type: ignore[index]
    deadline = time.monotonic() + 20
    while (final := client.get(f"/settings/playlists/{job_id}")).status_code != 286:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert "Road trip" in final.text
    assert 'value="pl-owned" checked' in final.text
    assert 'value="pl-other" >' in final.text or 'value="pl-other"' in final.text
    assert "not found on this account" in final.text
    # A fresh answer is reused rather than asked for again.
    assert "Road trip" in client.post("/settings/playlists").text


def test_the_picker_falls_back_to_the_configured_ids(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_FAIL", "1")
    _login(client)

    job_id = re.search(r"/settings/playlists/([^\"]+)\"", client.post("/settings/playlists").text)[1]  # type: ignore[index]
    deadline = time.monotonic() + 20
    while (final := client.get(f"/settings/playlists/{job_id}")).status_code != 286:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    assert "Spotify did not list your playlists" in final.text
    assert f'href="/jobs/{job_id}"' in final.text
    assert 'value="pl-owned" checked' in final.text
    assert 'value="pl-gone" checked' in final.text


def test_the_picker_greys_out_a_non_owned_playlist_with_reason_and_workaround(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `/me/playlists` entry is listed, but a followed, someone else's
    (collaborative included) or Spotify-owned playlist is shown greyed out, disabled, with the
    plain reason and the workaround - and, since it is disabled, it cannot be submitted."""
    monkeypatch.setenv("FAKE_PLAYLISTS_UNOWNED", "1")
    _login(client)

    final = html.unescape(_wait_for_picker(client, client.post("/settings/playlists").text))

    assert "Discover Weekly" in final
    row = final.split('value="pl-discover"', 1)[1].split("</label>", 1)[0]
    assert "disabled" in row
    assert "Spotify doesn't share this playlist's songs with a personal app" in row
    assert "like the songs you want, or copy them into a playlist you own" in row
    # Owned playlists stay ordinary, enabled checkboxes.
    owned_row = final.split('value="pl-owned"', 1)[1].split("</label>", 1)[0]
    assert "disabled" not in owned_row


def test_a_hand_posted_non_owned_playlist_id_is_refused(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker disables a non-owned checkbox, but the server refuses one anyway - crafted, or
    posted from a stale page."""
    monkeypatch.setenv("FAKE_PLAYLISTS_UNOWNED", "1")
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)
    before = (data_dir / "config.toml").read_text()

    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = [*form.get("spotify.playlists", []), "pl-discover"]

    response = client.post("/settings", data=form)
    body = html.unescape(response.text)

    assert response.status_code == 400
    assert "Spotify doesn't share this playlist's songs with a personal app" in body
    assert "like the songs you want, or copy them into a playlist you own" in body
    assert (data_dir / "config.toml").read_text() == before, "nothing was saved"


def test_a_collaborative_playlist_is_offered_and_one_needing_a_reauth_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone else's playlist you collaborate on is a normal checkbox once the token
    may read it. One listed by a token from before likearr asked for collaborative playlists is
    greyed out with "re-authorize", not with "copy it into a playlist you own"."""
    monkeypatch.setenv("FAKE_PLAYLISTS_COLLAB", "1")
    _login(client)

    final = html.unescape(_wait_for_picker(client, client.post("/settings/playlists").text))

    band = final.split('value="pl-band"', 1)[1].split("</label>", 1)[0]
    assert "disabled" not in band
    old = final.split('value="pl-oldshare"', 1)[1].split("</label>", 1)[0]
    assert "disabled" in old
    assert "You collaborate on this playlist" in old and "re-authorize Spotify" in old
    assert "copy them into a playlist you own" not in old


def test_a_collaborative_playlist_saves_and_one_needing_a_reauth_is_refused_with_why(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_COLLAB", "1")
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)
    config_path = data_dir / "config.toml"

    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = [*form.get("spotify.playlists", []), "pl-oldshare"]
    refused = client.post("/settings", data=form)
    body = html.unescape(refused.text)
    assert refused.status_code == 400
    assert "not saved - pl-oldshare: you collaborate on this playlist" in body
    assert "pl-oldshare" not in config_path.read_text()

    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = [*form.get("spotify.playlists", []), "pl-band"]
    saved = client.post("/settings", data=form, follow_redirects=False)
    if saved.status_code == 200:  # adding a playlist asks for a confirm first
        saved = client.post("/settings", data=_settings_form(saved.text), follow_redirects=False)
    assert saved.status_code == 303
    assert "pl-band" in config_path.read_text()


def test_a_configured_collaborative_playlist_needing_a_reauth_says_so_and_is_kept(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_COLLAB", "1")
    config_path = data_dir / "config.toml"
    config_path.write_text(config_path.read_text().replace('["pl-owned", "pl-gone"]', '["pl-owned", "pl-oldshare"]'))
    _login(client)

    final = html.unescape(_wait_for_picker(client, client.post("/settings/playlists").text))

    row = final.split('value="pl-oldshare"', 1)[1].split("Remove from settings", 1)[0]
    assert "In your settings, but not read yet: you collaborate on this playlist" in row
    assert "Spotify won't share its songs" not in row


def _configure_pl_discover(data_dir: Path) -> Path:
    """Add a non-owned playlist to `[spotify].playlists`, as if it had been added by hand before
    the picker could grey it out - or before it lost access to it."""
    config_path = data_dir / "config.toml"
    old = '["pl-owned", "pl-gone"]'
    new = '["pl-owned", "pl-gone", "pl-discover"]'
    config_path.write_text(config_path.read_text().replace(old, new))
    return config_path


def test_a_configured_non_owned_playlist_survives_an_unrelated_save(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """likearr never changes what you set without saying so: a save that touches something else
    entirely must not silently drop a playlist that is configured but not owned."""
    monkeypatch.setenv("FAKE_PLAYLISTS_UNOWNED", "1")
    config_path = _configure_pl_discover(data_dir)
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)

    form = _settings_form(client.get("/settings").text)
    assert "pl-discover" in form["spotify.playlists"], "the hidden input must carry it forward"
    form["spotify.saved_albums"] = []  # an unrelated change: switch it off

    response = client.post("/settings", data=form, follow_redirects=False)

    assert response.status_code == 303
    text = config_path.read_text()
    assert "pl-discover" in text
    assert "saved_albums = false" in text


def test_a_configured_non_owned_playlist_can_be_removed_on_purpose(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_UNOWNED", "1")
    config_path = _configure_pl_discover(data_dir)
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)

    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists.remove"] = ["pl-discover"]

    response = client.post("/settings", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert "pl-discover" not in config_path.read_text()


def test_the_picker_notes_a_configured_playlist_that_is_not_owned(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_UNOWNED", "1")
    _configure_pl_discover(data_dir)
    _login(client)

    final = html.unescape(_wait_for_picker(client, client.post("/settings/playlists").text))

    assert "In your settings, but Spotify won't share its songs with a personal app" in final
    assert "Remove from settings" in final
    row = final.split('value="pl-discover"', 1)[1].split("</label>", 1)[0]
    assert "checked disabled" in row, "kept, greyed out, and not itself submittable"


# ---------------------------------------------------------------- playlist names


def test_settings_names_playlists_from_the_cache_without_starting_a_job(client: TestClient, data_dir: Path) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip"})
    _login(client)

    page = client.get("/settings").text

    assert "Road trip" in page
    assert "names as of" in page
    assert "Refresh names from Spotify" in page
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())
    # The one the cache doesn't know is its id, linked to Spotify.
    assert 'href="https://open.spotify.com/playlist/pl-gone"' not in page  # "-" is not a Spotify id
    assert "<code>pl-gone</code>" in page


def test_an_unnamed_playlist_links_to_spotify(client: TestClient, data_dir: Path) -> None:
    config = data_dir / "config.toml"
    config.write_text(config.read_text().replace('["pl-owned", "pl-gone"]', '["37i9dQZF1DXcBWIGoYBM5M"]'))
    _login(client)

    page = client.get("/settings").text

    assert 'href="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"' in page


def test_with_no_cache_settings_shows_ids_and_asks_for_one_press(client: TestClient, data_dir: Path) -> None:
    _login(client)

    page = client.get("/settings").text

    assert "<code>pl-owned</code>" in page
    assert "to show your playlists by name" in page
    assert "names as of" not in page


def test_a_successful_refresh_fills_the_cache_and_the_names_stay(client: TestClient, data_dir: Path) -> None:
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)

    cache = json.loads((data_dir / "ui" / "playlist-names.json").read_text())
    assert cache["names"] == {"pl-owned": "Road trip", "pl-other": "Gym"}
    # Long after the answer went stale, the names are still there, and no job was started for them.
    for job in (data_dir / "ui" / "jobs").iterdir():
        meta = json.loads((job / "meta.json").read_text())
        meta["finished_at"] = "2026-09-01T00:00:00+00:00"
        (job / "meta.json").write_text(json.dumps(meta))
    page = client.get("/settings").text
    assert "Road trip" in page
    assert len(list((data_dir / "ui" / "jobs").iterdir())) == 1


def test_a_failed_refresh_keeps_the_old_names(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache_names(data_dir, {"pl-owned": "Road trip"})
    before = cache.read_text()
    monkeypatch.setenv("FAKE_PLAYLISTS_FAIL", "1")
    _login(client)

    final = _wait_for_picker(client, client.post("/settings/playlists").text)

    assert cache.read_text() == before
    assert "Road trip" in final
    assert "Spotify did not list your playlists" in final


def test_the_waiting_picker_names_the_selection_too(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip"})
    monkeypatch.setenv("FAKE_PLAYLISTS_SLEEP", "1")
    _login(client)

    waiting = client.post("/settings/playlists").text

    assert "Road trip" in waiting
    assert 'name="spotify.playlists" value="pl-owned"' in waiting
    _wait_for_picker(client, waiting)


def test_adding_a_playlist_names_it_on_the_confirm_page(client: TestClient, data_dir: Path) -> None:
    _cache_names(data_dir, {"pl-new": "Late night"})
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = ["pl-owned", "pl-gone", "pl-new"]

    page = client.post("/settings", data=form).text

    assert "Late night" in page


# ---------------------------------------------------------------- review fixes


def test_a_playlist_name_that_looks_like_a_header_still_parses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_NAME", "Authorization: Denied")
    _login(client)

    final = _wait_for_picker(client, client.post("/settings/playlists").text)

    assert "Authorization: Denied" in final
    assert "did not list your playlists" not in final


def test_a_failed_fetch_is_remembered_rather_than_retried_on_every_visit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_FAIL", "1")
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)

    again = client.post("/settings/playlists").text
    page = client.get("/settings").text

    assert len(list((data_dir / "ui" / "jobs").iterdir())) == 1
    assert "did not list your playlists" in again
    assert 'hx-post="/settings/playlists"' not in page


def test_a_reload_during_a_fetch_follows_the_fetch_already_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.setenv("FAKE_PLAYLISTS_SLEEP", "1")
    _login(client)
    first = client.post("/settings/playlists").text
    second = client.post("/settings/playlists").text

    job_id = re.search(r'hx-get="/settings/playlists/([^"]+)"', first)[1]  # type: ignore[index]
    assert f'hx-get="/settings/playlists/{job_id}"' in second
    assert "Road trip" in _wait_for_picker(client, second)
    assert len(list((data_dir / "ui" / "jobs").iterdir())) == 1


def test_the_picker_keeps_the_selection_on_the_form_not_the_file(client: TestClient) -> None:
    # A 400 re-render shows the user's unsaved unchecks; the picker that loads into it must keep them.
    _login(client)
    form = {"picker": "1", "spotify.playlists": ["pl-owned"]}

    waiting = client.post("/settings/playlists", data=form).text
    assert 'type="checkbox"' not in waiting  # nothing to change while it waits, so nothing is lost
    assert 'name="spotify.playlists" value="pl-owned"' in waiting
    assert 'value="pl-gone"' not in waiting
    final = _wait_for_picker(client, waiting, params=form)

    assert 'value="pl-owned" checked' in final
    assert "pl-gone" not in final  # deselected on the form, and not a playlist Spotify lists


# ---------------------------------------------------------------- security findings


def test_a_huge_guard_value_is_a_field_error_not_a_crash(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["guards.max_unmonitors_scheduled"] = ["9" * 400]

    response = client.post("/settings", data=form)

    assert response.status_code == 400
    assert "out of range" in response.text
    assert (data_dir / "config.toml").read_text() == CONFIG


def test_opening_settings_starts_nothing_until_asked(client: TestClient, data_dir: Path) -> None:
    _login(client)

    page = client.get("/settings").text

    assert 'hx-trigger="load"' not in page
    assert "Refresh names from Spotify" in page
    assert 'value="pl-owned" checked' in page  # still editable, nothing is loading
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())


def test_a_fresh_answer_is_shown_when_settings_opens(client: TestClient) -> None:
    _login(client)
    _wait_for_picker(client, client.post("/settings/playlists").text)

    page = client.get("/settings").text

    assert "Road trip" in page


# ---------------------------------------------------------------- the source confirm, end to end


def test_switching_a_source_on_asks_first_through_the_real_form(client: TestClient, data_dir: Path) -> None:
    (data_dir / "config.toml").write_text(CONFIG.replace("[state]", "liked_tracks = false\n\n[state]"))
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["spotify.liked_tracks"] = ["on"]

    confirm = client.post("/settings", data=form)

    assert "Confirm this change" in confirm.text
    assert "with no cap. Review changes first" in confirm.text
    assert "liked_tracks = false" in (data_dir / "config.toml").read_text()
    saved = client.post("/settings", data=_settings_form(confirm.text), follow_redirects=False)
    assert saved.status_code == 303
    assert "liked_tracks = true" in (data_dir / "config.toml").read_text()


def test_swapping_one_playlist_for_another_asks_first(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["spotify.playlists"] = ["pl-owned", "pl-new"]  # pl-gone out, pl-new in

    confirm = client.post("/settings", data=form)

    assert "Confirm this change" in confirm.text
    assert "pl-new" in confirm.text
    assert (data_dir / "config.toml").read_text() == CONFIG


# ---------------------------------------------------------------- the confirm is bound to what it showed


def test_confirmed_yes_alone_does_not_skip_the_confirm(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.liked_track_scope"] = ["smallest"]
    form["confirmed"] = ["yes"]

    response = client.post("/settings", data=form)

    assert "Confirm this change" in response.text
    assert 'liked_track_scope = "album"' in (data_dir / "config.toml").read_text()


def test_a_value_changed_after_the_confirm_page_is_confirmed_again(client: TestClient, data_dir: Path) -> None:
    _login(client)
    form = _settings_form(client.get("/settings").text)
    form["rules.liked_track_scope"] = ["smallest"]
    carried = _settings_form(client.post("/settings", data=form).text)
    carried["guards.max_unmonitors_scheduled"] = ["5000"]  # edited on the way back

    response = client.post("/settings", data=carried)

    assert "Confirm this change" in response.text
    assert "max_unmonitors_scheduled" in response.text
    assert "max_unmonitors_scheduled" not in (data_dir / "config.toml").read_text()


# ---------------------------------------------------------------- playlist names load by themselves


def test_a_fresh_server_fetches_the_names_once_and_settings_shows_them(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _wait_until(lambda: _names_done(data_dir))
        _login(client)

        page = client.get("/settings").text

    assert len(_jobs_of(data_dir, "playlists")) == 1
    assert "Road trip" in page
    assert len(_jobs_of(data_dir, "playlists")) == 1  # the GET started nothing


def test_names_already_known_are_not_fetched_again(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip", "pl-gone": "Old"})
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _login(client)
        client.get("/settings")

    assert _jobs_of(data_dir, "playlists") == []


def test_a_settings_save_that_adds_a_playlist_fetches_its_name_from_the_post(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_names(data_dir, {"pl-owned": "Road trip", "pl-gone": "Old"})
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _login(client)
        form = _settings_form(client.get("/settings").text)
        form["spotify.playlists"] = ["pl-owned", "pl-gone", "pl-other"]
        confirm = client.post("/settings", data=form)
        assert _jobs_of(data_dir, "playlists") == []  # asked, not saved: nothing yet
        client.post("/settings", data=_settings_form(confirm.text), follow_redirects=False)
        _wait_until(lambda: _names_done(data_dir))
        page = client.get("/settings").text

    assert len(_jobs_of(data_dir, "playlists")) == 1
    assert "Gym" in page  # pl-other's name, fetched


def test_connecting_spotify_fetches_names_once_the_token_exists(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating `names_needed` on the token means a fresh Connect Spotify must itself ask for
    names - the next automatic trigger (a check, or a settings save) may be a while off."""
    (data_dir / "spotify-token.json").unlink()
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    with TestClient(_app(data_dir, fake_cli, auto_fetch_names=True), base_url="http://testserver") as client:
        _login(client)
        assert _jobs_of(data_dir, "playlists") == []

        page = _connect_start(client)
        state = re.search(r'state=([^&"]+)', page)
        assert state is not None
        url = f"http://127.0.0.1:8765/callback?code=some-code&state={state[1]}"
        with respx.mock:
            respx.get(ME_URL).mock(return_value=httpx.Response(200, json=ME))
            respx.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "expires_in": 3600,
                        "scope": "user-library-read",
                    },
                )
            )
            finish = client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)
            assert finish.status_code == 303
        _wait_until(lambda: _names_done(data_dir))

    assert len(_jobs_of(data_dir, "playlists")) == 1


# ---------------------------------------------------------------- Spotify connect


SPOTIFY_CLIENT_ID = "spotify-client-id-SENTINEL"


TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"
ME = {"id": "fake-user", "display_name": "Test User"}


def _connect_start(client: TestClient) -> str:
    """POST the "Connect Spotify" form and return the settings page's HTML (paste-back mode)."""
    response = client.post("/settings/spotify/connect", follow_redirects=False)
    assert response.status_code == 200
    return response.text


def test_settings_says_spotify_is_not_configured_without_a_client_id(client: TestClient) -> None:
    _login(client)
    page = client.get("/settings").text
    assert "Spotify is not configured" in page
    assert "LIKEARR_SPOTIFY_CLIENT_ID" in page


def test_connect_spotify_shows_the_authorize_link_and_paste_back_form(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)

    page = _connect_start(client)

    assert "accounts.spotify.com/authorize" in page
    assert "Paste the full address it sent you to" in page
    assert 'action="/spotify/callback"' not in page  # paste-back mode: no direct callback offered


def test_the_redirect_uri_to_register_is_named_while_connecting_in_paste_back_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redirect URI is shown once a connect is under way, where Spotify's "invalid
    redirect URI" is seen, not on the Settings page itself. `data_dir`'s CONFIG doesn't set
    `[spotify] redirect_uri`, so this is the config default."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)

    assert "http://127.0.0.1:8765/callback" not in client.get("/settings").text

    page = _connect_start(client)

    assert "Add <code>http://127.0.0.1:8765/callback</code> to your Spotify app" in page
    assert 'placeholder="http://127.0.0.1:8765/callback?code=...' in page


def test_a_configured_redirect_uri_is_named_not_the_default(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `[spotify] redirect_uri` set, the connect step names that value - never a hardcoded
    default that no longer matches what likearr actually sends Spotify."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    (data_dir / "config.toml").write_text(
        CONFIG.replace(
            'token_file = "spotify-token.json"',
            'token_file = "spotify-token.json"\nredirect_uri = "http://127.0.0.1:9999/callback"',
        )
    )
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    with TestClient(app) as client:
        _login(client)
        page = _connect_start(client)

    assert "Add <code>http://127.0.0.1:9999/callback</code> to your Spotify app" in page
    assert 'placeholder="http://127.0.0.1:9999/callback?code=...' in page
    assert "http://127.0.0.1:8765/callback" not in page


def test_callback_mode_names_its_redirect_uri_while_connecting(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app) as client:
        _login(client)
        page = client.post("/settings/spotify/connect", follow_redirects=False).text

    assert "Add <code>https://likearr.example.org/spotify/callback</code> to your Spotify app" in page


def test_the_spotify_button_shows_the_icon_when_re_authorizing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data_dir` already writes a spotify-token.json, so this is the "Re-authorize" state."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)

    page = client.get("/settings").text

    assert '<button type="submit" class="spotify-button">' in page
    assert "<svg" in page.split('class="spotify-button"', 1)[1].split("</button>", 1)[0]
    assert "Re-authorize Spotify" in page


def test_the_spotify_button_shows_the_icon_when_connecting(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    (data_dir / "spotify-token.json").unlink()
    _login(client)

    page = client.get("/settings").text

    assert '<button type="submit" class="spotify-button">' in page
    assert "<svg" in page.split('class="spotify-button"', 1)[1].split("</button>", 1)[0]
    assert "Connect Spotify" in page


def test_connect_spotify_is_refused_without_a_client_id(client: TestClient) -> None:
    _login(client)

    response = client.post("/settings/spotify/connect", follow_redirects=False)

    assert response.status_code == 400
    assert "cannot connect Spotify" in response.text
    assert "LIKEARR_SPOTIFY_CLIENT_ID" in response.text


def test_the_https_callback_mode_is_only_offered_when_a_public_url_is_configured(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    (data_dir / "config.toml").write_text(
        CONFIG.replace(
            "[ui]\n",
            '[ui]\npublic_url = "https://likearr.example.org"\n',
        )
    )
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post("/settings/spotify/connect", follow_redirects=False)
        # Direct-callback mode links straight to Spotify - no paste-back field is shown.
        url = _callback_authorize_url(response)
        assert url.startswith("https://accounts.spotify.com/authorize")
        assert "redirect_uri=https%3A%2F%2Flikearr.example.org%2Fspotify%2Fcallback" in url
        assert "spotify-redirect-url" not in response.text

        callback = client.get("/spotify/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
        assert callback.status_code != 404


@pytest.mark.parametrize("data", [{}, {"promote_save": "1"}], ids=["connect", "clean-up-write-access"])
def test_callback_mode_connect_at_another_address_links_to_spotify_instead(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, data: dict[str, str]
) -> None:
    """The UI opened at an address other than `public_url` (here the test client's own
    host, standing in for a LAN address) keeps the two-click flow. Chromium and WebKit browsers
    check form-action on each redirect of a form submission, and Spotify's last hop back to
    `<public_url>/spotify/callback` is another origin from there. So the answer is a page on this
    origin with a plain same-tab link to Spotify (a link click is not a form submission), both
    for Settings' Connect and for Clean up's "Authorize write access" button, and its CSP is the
    default one."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app) as client:
        _login(client)
        response = client.post("/settings/spotify/connect", data=data, follow_redirects=False)

    assert not response.is_redirect
    assert response.headers["content-security-policy"] == DEFAULT_CSP
    link = re.search(r'<a [^>]*id="spotify-continue"[^>]*>', response.text)
    assert link is not None
    assert 'target="_blank"' not in link[0]  # same tab: Spotify sends this tab back to /spotify/callback
    # No second Connect form beside it: pressing that would restart the attempt, dropping the
    # write access Clean up's button asked for.
    assert 'action="/settings/spotify/connect"' not in response.text
    assert _callback_authorize_url(response).startswith("https://accounts.spotify.com/authorize?")


DEFAULT_CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
ONE_CLICK_CSP = (
    "default-src 'self'; frame-ancestors 'none'; "
    "form-action 'self' https://accounts.spotify.com https://likearr.example.org; base-uri 'none'"
)
_AT_PUBLIC_URL = "https://likearr.example.org"


@pytest.mark.parametrize("data", [{}, {"promote_save": "1"}], ids=["connect", "clean-up-write-access"])
def test_callback_mode_connect_at_the_public_url_redirects_straight_to_spotify(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, data: dict[str, str]
) -> None:
    """Opened at the `public_url` origin, one click reaches Spotify. The POST keeps its CSRF
    check and answers 303 to the authorize URL; the page that holds the form (Settings here) is
    the one whose `form-action` the browser checks each hop against, and it allows Spotify and the
    `public_url` origin its callback comes back to."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app, base_url=_AT_PUBLIC_URL) as client:
        _login(client)
        page = client.get("/settings")
        response = client.post("/settings/spotify/connect", data=data, follow_redirects=False)

    assert page.headers["content-security-policy"] == ONE_CLICK_CSP
    assert 'action="/settings/spotify/connect"' in page.text
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://accounts.spotify.com/authorize?")
    assert "redirect_uri=https%3A%2F%2Flikearr.example.org%2Fspotify%2Fcallback" in location
    assert response.headers["content-security-policy"] == ONE_CLICK_CSP
    expected = [*READ, *WRITE] if data else READ
    assert _asked_scopes(location) == expected


def test_one_click_connect_finishes_through_the_callback(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state the 303 carries is a pending attempt like any other: the callback finishes it."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app, base_url=_AT_PUBLIC_URL) as client:
        _login(client)
        location = client.post("/settings/spotify/connect", follow_redirects=False).headers["location"]
    state = re.search(r"state=([^&]+)", location)
    assert state is not None

    assert "Spotify connected" in _callback_with(app, state[1], " ".join(READ))


def test_callback_mode_at_another_port_of_the_public_host_keeps_the_link(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public name reached straight at likearr's own port is another origin: link, default CSP."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app, base_url="http://likearr.example.org:8080") as client:
        _login(client)
        page = client.get("/settings")
        response = client.post("/settings/spotify/connect", follow_redirects=False)

    assert page.headers["content-security-policy"] == DEFAULT_CSP
    assert response.headers["content-security-policy"] == DEFAULT_CSP
    assert _callback_authorize_url(response).startswith("https://accounts.spotify.com/authorize?")


def test_only_the_pages_with_the_connect_form_widen_form_action(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other page keeps `form-action 'self'` exactly, even at the `public_url` origin."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app, base_url=_AT_PUBLIC_URL) as client:
        login = client.get("/login")
        _login(client)
        for path in ("/", "/plan", "/jobs"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-security-policy"] == DEFAULT_CSP, path
    assert login.headers["content-security-policy"] == DEFAULT_CSP


def test_paste_back_mode_is_unchanged_at_any_address(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No `public_url`: never a redirect, the paste-back link and form, and the default CSP."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    client.base_url = _AT_PUBLIC_URL  # type: ignore[assignment]
    _login(client)
    page = client.get("/settings")
    response = client.post("/settings/spotify/connect", follow_redirects=False)

    assert page.headers["content-security-policy"] == DEFAULT_CSP
    assert response.status_code == 200
    assert "location" not in response.headers
    assert response.headers["content-security-policy"] == DEFAULT_CSP
    assert _paste_back_url(response.text).startswith("https://accounts.spotify.com/authorize?")
    assert "spotify-redirect-url" in response.text


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "sec-fetch-site"],
)
def test_one_click_connect_still_refuses_a_cross_site_post(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    with TestClient(app, base_url=_AT_PUBLIC_URL) as client:
        _login(client)
        response = client.post("/settings/spotify/connect", headers=headers, follow_redirects=False)

    assert response.status_code == 403
    assert "location" not in response.headers
    assert "accounts.spotify.com" not in response.text


def test_the_callback_route_is_a_404_without_a_public_url(client: TestClient) -> None:
    _login(client)
    response = client.get("/spotify/callback", follow_redirects=False)
    assert response.status_code == 404


def test_an_unknown_state_is_refused_and_leaves_the_token_untouched(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    token_file = data_dir / "spotify-token.json"
    before = token_file.read_text()
    _login(client)
    _connect_start(client)  # starts a real attempt, but the paste-back carries an unrelated state

    response = client.post(
        "/settings/spotify/finish",
        data={"redirect_url": "http://127.0.0.1:8765/callback?code=some-code&state=not-the-real-state"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "expired" in page or "already been used" in page
    assert token_file.read_text() == before


def test_a_reused_state_is_refused_and_leaves_the_token_untouched(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)
    page = _connect_start(client)
    state = re.search(r"state=([^&\"]+)", page)
    assert state is not None
    url = f"http://127.0.0.1:8765/callback?code=some-code&state={state[1]}"

    with respx.mock:
        respx.get(ME_URL).mock(return_value=httpx.Response(200, json=ME))
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "scope": "user-library-read",
                },
            )
        )
        first = client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)
        assert first.status_code == 303
        first_flash = client.get(first.headers["location"]).text
        assert "Spotify connected" in first_flash

    after_first = (data_dir / "spotify-token.json").read_text()
    # `state` is consumed - removed from the server-side store - the moment the first finish
    # checks it, so a second POST of the same pasted URL (a stale bookmark, a doubled form submit)
    # is refused the same way an unknown one is, and never touches Spotify or the token file again.
    second = client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)
    page2 = client.get(second.headers["location"]).text
    assert "no longer valid" in page2 or "expired" in page2
    assert (data_dir / "spotify-token.json").read_text() == after_first


def test_the_callback_route_never_requires_login(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/spotify/callback` is exempted from the login gate (`auth._OPEN_PATHS`): Spotify reaches it
    by a cross-site GET redirect that never carries the `SameSite=Strict` session cookie. An
    unrecognised `state` is refused - the page says so - but the route itself never bounces an
    unauthenticated request to `/login` the way every other page does."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    (data_dir / "config.toml").write_text(
        CONFIG.replace(
            "[ui]\n",
            '[ui]\npublic_url = "https://likearr.example.org"\n',
        )
    )
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    with TestClient(app) as anon:
        response = anon.get("/spotify/callback", params={"code": "x", "state": "unknown"}, follow_redirects=False)
        assert response.status_code == 200
        assert "no longer valid" in response.text or "expired" in response.text
        assert "Log in" not in response.text  # never the login form


def test_the_callback_hides_a_config_error_and_logs_it(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The callback needs no login (and in direct-callback mode faces the internet), so a broken
    config.toml answers 503 with no detail; the detail goes to the log, as `/healthz` does.
    Signed-in pages keep showing the full message."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    (data_dir / "config.toml").write_text(
        CONFIG.replace('timezone = "America/New_York"', 'timezone = "Not/A_Zone_Leak"')
    )
    with TestClient(app) as anon, caplog.at_level(logging.WARNING, logger="likearr.web.app"):
        response = anon.get("/spotify/callback", params={"code": "x", "state": "y"}, follow_redirects=False)

    assert response.status_code == 503
    assert "likearr's configuration has a problem; log in to see it" in html.unescape(response.text)
    assert "Not/A_Zone_Leak" not in response.text
    assert "config.toml" not in response.text and str(data_dir) not in response.text
    assert any("Not/A_Zone_Leak" in r.getMessage() for r in caplog.records)

    with TestClient(app) as signed_in:
        signed_in.post("/login", data={"password": PASSWORD})
        assert "Not/A_Zone_Leak" in signed_in.get("/settings").text


def test_the_callback_never_echoes_an_arbitrary_error_text(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback needs no login, so anything it echoes from the query string is text anyone can
    put on likearr's own page with a link. Only a standard OAuth error code is shown."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    (data_dir / "config.toml").write_text(
        CONFIG.replace(
            "[ui]\n",
            '[ui]\npublic_url = "https://likearr.example.org"\n',
        )
    )
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    with TestClient(app) as anon:
        spoof = anon.get(
            "/spotify/callback", params={"error": "Session expired, re-enter your password at evil.example"}
        )
        assert "evil.example" not in spoof.text
        assert "re-enter your password" not in spoof.text
        denied = anon.get("/spotify/callback", params={"error": "access_denied", "state": "x"})
        assert "access_denied" in denied.text


def test_a_valid_state_from_an_unauthenticated_client_still_connects(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `state` a logged-in session minted is the whole of the callback's authorization: a
    second, entirely separate client - no cookies at all, standing in for the browser after
    Spotify's redirect drops the session cookie on this cross-site navigation - can finish the
    flow with it. That is the point of exempting the route rather than loosening the cookie."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    (data_dir / "config.toml").write_text(
        CONFIG.replace(
            "[ui]\n",
            '[ui]\npublic_url = "https://likearr.example.org"\n',
        )
    )
    app = create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )
    with TestClient(app) as logged_in:
        _login(logged_in)
        start = logged_in.post("/settings/spotify/connect", follow_redirects=False)
        state = re.search(r"state=([^&]+)", _callback_authorize_url(start))
        assert state is not None

    with TestClient(app) as anon:  # a fresh client: no cookies, standing in for the redirected browser
        with respx.mock:
            respx.get(ME_URL).mock(return_value=httpx.Response(200, json=ME))
            respx.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "at-cb",
                        "refresh_token": "rt-cb",
                        "expires_in": 3600,
                        "scope": "user-library-read",
                    },
                )
            )
            response = anon.get(
                "/spotify/callback", params={"code": "some-code", "state": state[1]}, follow_redirects=False
            )
        assert response.status_code == 200
        assert "Spotify connected" in response.text

    on_disk = json.loads((data_dir / "spotify-token.json").read_text())
    assert on_disk["access_token"] == "at-cb"


# ---------------------------------------------------------------- read scopes by default


READ = ["user-follow-read", "user-library-read", "playlist-read-private", "playlist-read-collaborative"]


WRITE = ["user-follow-modify", "user-library-modify"]


def _asked_scopes(url: str) -> list[str]:
    import html
    import urllib.parse

    return urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(url)).query)["scope"][0].split()


def _callback_authorize_url(response: Any) -> str:
    """The Spotify authorize URL a direct-callback Connect answers with: a same-tab link on this
    origin's own page, never a redirect (a form POST redirected off-origin is blocked by the
    page's `form-action 'self'` in Chromium and WebKit browsers)."""
    assert response.status_code == 200
    assert "location" not in response.headers
    found = re.search(
        r'<a [^>]*id="spotify-continue"[^>]*href="(https://accounts\.spotify\.com/authorize[^"]+)"', response.text
    )
    assert found is not None
    return html.unescape(found[1])


def _paste_back_url(page: str) -> str:
    found = re.search(r'href="(https://accounts\.spotify\.com/authorize[^"]+)"', page)
    assert found is not None
    return found[1]


def _set_token_scope(data_dir: Path, scope: str | None) -> None:
    token = data_dir / "spotify-token.json"
    if scope is None:
        token.unlink()
    else:
        token.write_text(json.dumps({**json.loads(token.read_text()), "scope": scope}))


@pytest.mark.parametrize("scope", [None, " ".join(READ)], ids=["new user", "read-only token"])
def test_connect_spotify_asks_for_read_scopes_only_by_default(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch, scope: str | None
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _set_token_scope(data_dir, scope)
    _login(client)

    assert _asked_scopes(_paste_back_url(_connect_start(client))) == READ


def test_connect_spotify_with_promote_save_asks_for_the_write_scopes_too(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _set_token_scope(data_dir, " ".join(READ))
    _login(client)

    response = client.post("/settings/spotify/connect", data={"promote_save": "1"}, follow_redirects=False)

    assert response.status_code == 200
    assert _asked_scopes(_paste_back_url(response.text)) == [*READ, *WRITE]


def test_re_authorize_keeps_the_write_scopes_the_token_already_has(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain Re-authorize never quietly drops write access you approved."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _set_token_scope(data_dir, " ".join([*READ, *WRITE]))
    _login(client)

    assert _asked_scopes(_paste_back_url(_connect_start(client))) == [*READ, *WRITE]


def test_settings_offers_write_access_only_when_the_token_lacks_it(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)

    _set_token_scope(data_dir, " ".join(READ))
    form = client.get("/settings").text.split('action="/settings/spotify/connect"', 1)[1].split("</form>", 1)[0]
    assert '<input type="checkbox" name="promote_save" value="1">' in form
    assert "promote-save" in form

    _set_token_scope(data_dir, " ".join([*READ, *WRITE]))
    page = client.get("/settings").text
    form = page.split('action="/settings/spotify/connect"', 1)[1].split("</form>", 1)[0]
    assert 'name="promote_save"' not in form
    assert "read and write access" in page


def _callback_app(data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    (data_dir / "config.toml").write_text(CONFIG.replace(*_PUBLIC_URL))
    return create_app(
        WebSettings(config_path=data_dir / "config.toml", password=PASSWORD, cli=fake_cli, now=lambda: NOW)
    )


def _callback_with(app: Any, state: str, granted: str, **extra: str) -> str:
    """Finish a direct-callback attempt from a cookie-less client, Spotify granting `granted`."""
    with TestClient(app) as anon, respx.mock:
        respx.get(ME_URL).mock(return_value=httpx.Response(200, json=ME))
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "at-cb", "refresh_token": "rt-cb", "expires_in": 3600, "scope": granted}
            )
        )
        response = anon.get(
            "/spotify/callback", params={"code": "some-code", "state": state, **extra}, follow_redirects=False
        )
    assert response.status_code == 200
    return response.text


def _start_in_callback_mode(app: Any, **data: str) -> tuple[list[str], str]:
    with TestClient(app) as logged_in:
        _login(logged_in)
        start = logged_in.post("/settings/spotify/connect", data=data, follow_redirects=False)
    location = _callback_authorize_url(start)
    state = re.search(r"state=([^&]+)", location)
    assert state is not None
    return _asked_scopes(location), state[1]


def test_callback_mode_carries_promote_save_through_the_server_side_state(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    _set_token_scope(data_dir, " ".join(READ))

    asked, state = _start_in_callback_mode(app, promote_save="1")
    assert asked == [*READ, *WRITE]

    page = _callback_with(app, state, " ".join([*READ, *WRITE]))
    assert "Spotify connected" in page
    assert "promote-save" not in page  # it asked for write access, and got it: nothing to warn about
    assert json.loads((data_dir / "spotify-token.json").read_text())["scope"] == " ".join([*READ, *WRITE])


def test_callback_mode_says_so_when_spotify_did_not_grant_the_write_access_asked_for(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    _set_token_scope(data_dir, None)

    _, state = _start_in_callback_mode(app, promote_save="1")
    page = _callback_with(app, state, " ".join(READ))

    assert "Spotify connected" in page
    assert "did not grant the write access promote-save needs" in page
    assert "user-follow-modify, user-library-modify" in page


def test_the_callback_cannot_change_what_the_attempt_asked_for(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback needs no login, so its query string is anyone's text. The write-scope choice
    was made behind the login gate and lives only in the server-side pending attempt: extra
    `promote_save` or `scope` parameters on the callback change nothing."""
    app = _callback_app(data_dir, fake_cli, monkeypatch)
    _set_token_scope(data_dir, None)

    asked, state = _start_in_callback_mode(app)
    assert asked == READ

    page = _callback_with(
        app, state, " ".join(READ), promote_save="1", include_write="1", scope=" ".join([*READ, *WRITE])
    )

    assert "Spotify connected" in page
    assert "Read access only" in page  # the read-only attempt's message, not the write one's
    assert "did not grant" not in page


def test_every_other_route_still_requires_login(client: TestClient) -> None:
    for path in ("/", "/settings", "/doctor", "/settings/doctor/x", "/plan"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_a_successful_exchange_writes_the_token_0600_and_updates_authorized_at(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)
    page = _connect_start(client)
    state = re.search(r"state=([^&\"]+)", page)
    assert state is not None
    url = f"http://127.0.0.1:8765/callback?code=some-code&state={state[1]}"

    with respx.mock:
        respx.get(ME_URL).mock(return_value=httpx.Response(200, json=ME))
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-new",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                    "scope": "user-follow-read user-library-read",
                },
            )
        )
        response = client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)
    assert response.status_code == 303

    token_file = data_dir / "spotify-token.json"
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600
    on_disk = json.loads(token_file.read_text())
    assert on_disk["access_token"] == "at-new"
    # The exchange runs its own `SpotifyAuth` (`spotify_connect.exchange`), which - like every
    # child job's `likearr` process - uses the real clock, not the web layer's fixed `now`.
    assert abs(on_disk["authorized_at"] - time.time()) < 30

    flash = client.get("/settings").text
    assert "Spotify connected" in flash
    assert "user-follow-read" in flash
    assert "at-new" not in flash and "rt-new" not in flash  # tokens never rendered


def test_a_failed_exchange_leaves_the_old_token_untouched(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    token_file = data_dir / "spotify-token.json"
    before = token_file.read_text()
    _login(client)
    page = _connect_start(client)
    state = re.search(r"state=([^&\"]+)", page)
    assert state is not None
    url = f"http://127.0.0.1:8765/callback?code=some-code&state={state[1]}"

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        response = client.post("/settings/spotify/finish", data={"redirect_url": url}, follow_redirects=False)
    assert response.status_code == 303

    assert token_file.read_text() == before
    flash = client.get("/settings").text
    assert "Spotify authorization failed" in flash


def test_nothing_secret_appears_in_the_rendered_settings_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _login(client)
    page = _connect_start(client)
    assert "code_verifier" not in page
    assert "verifier" not in page.lower()


# ---------------------------------------------------------------- Lidarr setup + Doctor in Settings


HX = {"HX-Request": "true"}


def _poll_until_stopped(client: TestClient, url: str) -> str:
    """Poll a Settings fragment the way htmx does until it answers 286 (stop polling)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(url, headers=HX)
        if response.status_code == 286:
            return response.text
        assert response.status_code == 200
        assert 'hx-trigger="every 2s"' in response.text, "an unfinished job's fragment keeps polling"
        time.sleep(0.05)
    raise AssertionError("fragment never stopped polling")


def _poll_url(fragment: str) -> str:
    found = re.search(r'hx-get="([^"]+)" hx-trigger="every 2s"', fragment)
    assert found is not None, fragment
    return found[1]


def _section(page: str, section_id: str) -> str:
    start = page.index(f'id="{section_id}"')
    return page[start : page.index("</fieldset>", start)]


def test_settings_shows_the_doctor_section_without_running_it(client: TestClient, data_dir: Path) -> None:
    """Opening Settings must never run doctor: it calls Spotify, and the quota has run out before."""
    _login(client)

    page = client.get("/settings").text

    doctor = _section(page, "doctor")
    assert "Run checks" in doctor
    assert "every 2s" not in doctor
    assert not _jobs_of(data_dir, "doctor")


def test_the_nav_has_no_doctor_link_and_old_doctor_links_land_on_settings(client: TestClient) -> None:
    _login(client)

    page = client.get("/settings").text
    nav = page[page.index("<nav>") : page.index("</nav>")]
    response = client.get("/doctor", follow_redirects=False)

    assert "/doctor" not in nav
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#doctor"


def test_doctor_polls_while_running_then_lists_failures_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCTOR_SLEEP", "0.5")
    monkeypatch.setenv(
        "FAKE_DOCTOR",
        json.dumps(
            {
                "checks": [
                    {"level": "PASS", "name": "config", "detail": "loaded"},
                    {"level": "WARN", "name": "tag", "detail": "tag-missing-detail"},
                    {"level": "PASS", "name": "lidarr", "detail": "reachable"},
                    {"level": "FAIL", "name": "musicbrainz", "detail": "mb-down-detail"},
                    {"level": "SKIP", "name": "spotify liked", "detail": "not requested"},
                ],
                "summary": {"total": 5, "failed": 1, "warnings": 1, "skipped": 1},
            }
        ),
    )
    _login(client)

    started = client.post("/doctor", headers=HX)
    assert started.status_code == 200
    assert 'hx-trigger="every 2s"' in started.text
    result = _poll_until_stopped(client, _poll_url(started.text))

    assert "every 2s" not in result
    assert "1 failed" in result and "1 warning" in result
    assert result.index("mb-down-detail") < result.index("tag-missing-detail") < result.index("reachable")
    passes = result[result.index("<details") :]
    assert "2 checks passed" in passes and "reachable" in passes
    assert "tag-missing-detail" not in passes and "mb-down-detail" not in passes

    page = client.get("/settings").text
    doctor = _section(page, "doctor")
    assert "mb-down-detail" in doctor
    assert "every 2s" not in doctor


# ---------------------------------------------------------------- the nav pill on every poll


def test_a_running_poll_s_oob_nav_update_keeps_the_pill(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A poll answer that isn't finished yet still carries an out-of-band nav update - with the
    pill, since something is still running - so a second tab polling elsewhere also sees it."""
    monkeypatch.setenv("FAKE_DOCTOR_SLEEP", "2")
    _login(client)

    started = client.post("/doctor", headers=HX)

    assert started.status_code == 200
    oob = re.search(r'<span id="nav-running" hx-swap-oob="true">(.*?)</span>', started.text, re.S)
    assert oob is not None, started.text
    assert '<a class="running"' in oob[1]


def test_a_finished_poll_s_oob_nav_update_has_no_pill(client: TestClient) -> None:
    """The pill was rendered from `runner.current()` at page load; only an htmx poll answering
    later - here, Doctor's own last fragment - can tell every tab the run it named has ended."""
    _login(client)

    started = client.post("/doctor", headers=HX)
    result = _poll_until_stopped(client, _poll_url(started.text))

    assert '<span id="nav-running" hx-swap-oob="true"></span>' in result
    assert '<a class="running"' not in result[result.index('id="nav-running"') :]


def test_doctor_all_passed_says_so_in_one_line(client: TestClient) -> None:
    _login(client)
    started = client.post("/doctor", headers=HX)
    result = _poll_until_stopped(client, _poll_url(started.text))

    assert "All 1 check passed" in result


def test_doctor_without_htmx_starts_and_goes_back_to_settings(client: TestClient, data_dir: Path) -> None:
    _login(client)

    response = client.post("/doctor", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings#doctor"
    assert _jobs_of(data_dir, "doctor")


def test_the_doctor_fragment_without_htmx_goes_to_settings(client: TestClient) -> None:
    _login(client)
    started = client.post("/doctor", headers=HX)

    response = client.get(_poll_url(started.text), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings#doctor"


def test_doctor_still_renders_when_config_does_not_load(client: TestClient, data_dir: Path) -> None:
    """Doctor is most useful exactly when config.toml is broken, so the section survives it."""
    _login(client)
    (data_dir / "config.toml").write_text(CONFIG + "\n[rules\n")

    page = client.get("/settings").text

    assert "config.toml does not load" in page
    assert "Run checks" in _section(page, "doctor")


def test_a_wrongly_typed_guard_shows_the_broken_config_view_not_a_500(client: TestClient, data_dir: Path) -> None:
    """A wrong type used to escape as a bare ValueError, which no handler caught."""
    _login(client)
    (data_dir / "config.toml").write_text(CONFIG + '\n[guards]\nmax_unmonitors_scheduled = "lots"\n')

    response = client.get("/settings")

    assert response.status_code == 200
    assert "config.toml does not load" in response.text
    assert "[guards] max_unmonitors_scheduled must be a whole number" in response.text
    assert "Run checks" in _section(response.text, "doctor")


def test_doctor_renders_before_the_first_run(client: TestClient, data_dir: Path) -> None:
    _login(client)
    (data_dir / "state.sqlite").unlink()

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Run checks" in _section(response.text, "doctor")


def test_the_doctor_panel_has_one_live_region_outside_the_polled_panel(client: TestClient) -> None:
    """The panel's own poll swaps all of `#doctor-panel` (outerHTML), including any
    `role="status"` inside it, so a screen reader may not reliably announce a live region that
    was itself just inserted along with its content. The wrapper in settings.html sits outside
    that swap and stays the same element across every poll."""
    _login(client)

    doctor = _section(client.get("/settings").text, "doctor")

    assert doctor.count('role="status"') == 1
    assert doctor.index('role="status"') < doctor.index('id="doctor-panel"')


def _preview(client: TestClient) -> str:
    """Start a Lidarr setup preview the way the button does and poll it to its table."""
    started = client.post("/settings/lidarr-setup/preview", headers=HX)
    assert started.status_code == 200
    return _poll_until_stopped(client, _poll_url(started.text))


def _setup_jobs(data_dir: Path) -> list[Any]:
    return _jobs_of(data_dir, "lidarr-setup-apply")


def test_lidarr_setup_preview_polls_then_shows_the_table_in_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SETUP_SLEEP", "0.5")
    _login(client)

    started = client.post("/settings/lidarr-setup/preview", headers=HX)
    assert 'hx-trigger="every 2s"' in started.text
    preview = _poll_until_stopped(client, _poll_url(started.text))

    assert "will be created" in preview
    assert "every 2s" not in preview
    assert "Apply</button>" in preview

    section = _section(client.get("/settings").text, "lidarr-setup")
    assert "will be created" in section
    assert "every 2s" not in section


def test_the_lidarr_setup_panel_has_one_live_region_outside_the_polled_panel(client: TestClient) -> None:
    """Same reasoning as the Doctor panel's: the wrapper stays put while
    `#lidarr-setup-panel` itself is swapped in full on every poll."""
    _login(client)

    section = _section(client.get("/settings").text, "lidarr-setup")

    assert section.count('role="status"') == 1
    assert section.index('role="status"') < section.index('id="lidarr-setup-panel"')


def test_a_second_preview_request_follows_the_running_one(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tabs that both saw an apply finish both ask for the re-check: one preview, not a refusal."""
    monkeypatch.setenv("FAKE_SETUP_SLEEP", "1")
    _login(client)

    first = client.post("/settings/lidarr-setup/preview", headers=HX)
    second = client.post("/settings/lidarr-setup/preview", headers=HX)

    assert _poll_url(first.text) == _poll_url(second.text)
    assert len(_jobs_of(data_dir, "lidarr-setup-preview")) == 1
    _poll_until_stopped(client, _poll_url(first.text))


def test_lidarr_setup_fully_set_up_offers_no_apply(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FAKE_SETUP_PROFILES",
        json.dumps(
            {
                "profiles": [
                    {"name": "Lean", "kind": "lean", "status": "ok", "id": 1},
                    {"name": "Full", "kind": "full", "status": "ok", "id": 2},
                ],
                "tag": {"name": "likearr", "status": "ok", "id": 3},
                "root_folder": {"path": "/music", "status": "ok"},
                "todo": [],
                "needs_apply": False,
            }
        ),
    )
    _login(client)

    preview = _preview(client)

    assert "Nothing to do" in preview
    assert "Apply</button>" not in preview


def test_lidarr_setup_apply_without_the_confirm_writes_nothing(client: TestClient, data_dir: Path) -> None:
    _login(client)
    preview = _preview(client)
    apply_url = re.search(r'hx-post="(/settings/lidarr-setup/[^"]+/apply)"', preview)
    assert apply_url is not None

    confirm = client.post(apply_url[1], headers=HX)

    assert confirm.status_code == 200
    assert "This writes to Lidarr:" in confirm.text
    assert "Yes, apply" in confirm.text
    assert 'name="confirmed" value="yes"' in confirm.text
    assert not _setup_jobs(data_dir), "no apply job before the confirm"


def test_lidarr_setup_apply_without_htmx_confirms_inline_on_settings(client: TestClient, data_dir: Path) -> None:
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    confirm = client.post(f"/settings/lidarr-setup/{preview_id}/apply")

    assert confirm.status_code == 200
    assert "This writes to Lidarr:" in _section(confirm.text, "lidarr-setup")
    assert not _setup_jobs(data_dir)


def test_lidarr_setup_apply_with_the_confirm_spawns_the_fixed_argv_and_rechecks(
    client: TestClient, data_dir: Path
) -> None:
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    applied = client.post(f"/settings/lidarr-setup/{preview_id}/apply", data={"confirmed": "yes"}, headers=HX)

    assert applied.status_code == 200
    (apply_job,) = _setup_jobs(data_dir)
    assert apply_job.argv[-2:] == ["setup-profiles", "--apply"]
    done = _poll_until_stopped(client, _poll_url(applied.text))
    assert "every 2s" not in done
    # The poll that sees it finish asks for a fresh, read-only preview: the re-check.
    assert 'hx-post="/settings/lidarr-setup/preview"' in done
    assert 'hx-trigger="load"' in done

    # Opening Settings later shows the finished apply without starting anything.
    section = _section(client.get("/settings").text, "lidarr-setup")
    assert 'hx-trigger="load"' not in section
    assert "every 2s" not in section


def test_lidarr_setup_apply_output_hides_a_secret_from_the_environment(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The apply's stdout is shown inline, so it gets the job page's redaction backstop."""
    monkeypatch.setenv("LIKEARR_TEST_API_KEY", "metadata profile 'Lean'")
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    applied = client.post(f"/settings/lidarr-setup/{preview_id}/apply", data={"confirmed": "yes"}, headers=HX)
    done = _poll_until_stopped(client, _poll_url(applied.text))

    assert "-&gt; id 1" in done
    assert "metadata profile &#39;Lean&#39;" not in done


def test_old_links_to_a_pruned_job_still_land_on_the_settings_section(client: TestClient) -> None:
    """A bookmark to a page whose job is long gone still lands on the section, not a bare 404."""
    _login(client)

    setup = client.get("/settings/lidarr-setup/20200101T000000Z-000000", follow_redirects=False)
    doctor = client.get("/settings/doctor/20200101T000000Z-000000", follow_redirects=False)

    assert (setup.status_code, setup.headers["location"]) == (303, "/settings#lidarr-setup")
    assert (doctor.status_code, doctor.headers["location"]) == (303, "/settings#doctor")


def test_lidarr_setup_confirmed_without_htmx_goes_back_to_settings(client: TestClient, data_dir: Path) -> None:
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    applied = client.post(
        f"/settings/lidarr-setup/{preview_id}/apply", data={"confirmed": "yes"}, follow_redirects=False
    )

    assert applied.status_code == 303
    assert applied.headers["location"] == "/settings#lidarr-setup"
    assert [m.argv[-2:] for m in _setup_jobs(data_dir)] == [["setup-profiles", "--apply"]]


def test_lidarr_setup_apply_offers_nothing_when_nothing_is_needed(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_SETUP_PROFILES",
        json.dumps(
            {
                "profiles": [{"name": "Lean", "kind": "lean", "status": "ok", "id": 1}],
                "tag": {"name": "likearr", "status": "ok", "id": 3},
                "root_folder": {"path": "/music", "status": "ok"},
                "todo": [],
                "needs_apply": False,
            }
        ),
    )
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    response = client.post(f"/settings/lidarr-setup/{preview_id}/apply", data={"confirmed": "yes"}, headers=HX)

    assert "Nothing to apply" in response.text
    assert not _setup_jobs(data_dir)


def test_old_lidarr_setup_links_land_on_the_settings_section(client: TestClient, data_dir: Path) -> None:
    _login(client)
    _preview(client)
    preview_id = _jobs_of(data_dir, "lidarr-setup-preview")[0].id

    response = client.get(f"/settings/lidarr-setup/{preview_id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings#lidarr-setup"


# ---------------------------------------------------------------- Clean up's switch


def _advanced(page: str) -> str:
    return page.split('id="advanced"', 1)[1].split("</details>", 1)[0]


def test_the_clean_up_switch_sits_first_in_a_collapsed_advanced_section(client: TestClient, data_dir: Path) -> None:
    _clean_up_off(data_dir)
    _login(client)

    page = client.get("/settings").text

    assert '<details class="panel advanced" id="advanced">' in page  # collapsed: no `open`
    advanced = _advanced(page)
    assert advanced.index("<legend>Clean up</legend>") < advanced.index("<form")
    assert "holding folder outside the library" in advanced
    assert "#optional-clean-up" in advanced
    assert "Clean up is off." in advanced
    assert page.index("<legend>Guards</legend>") < page.index('id="advanced"')  # Rules and Guards stay visible


def test_the_clean_up_switch_round_trips_through_a_backed_up_write_and_leaves_the_ledger_alone(
    client: TestClient, data_dir: Path
) -> None:
    ledger = data_dir / "ui" / "prune-ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b'{"version": 1, "releases": {}, "artists": {}, "imports": ["x"]}\n')
    before = ledger.read_bytes()
    config = data_dir / "config.toml"
    _clean_up_off(data_dir)
    _login(client)

    on = client.post(
        "/settings/cleanup", data={"file_hash": _file_hash(client), "enabled": "1"}, follow_redirects=False
    )

    assert on.status_code == 303 and on.headers["location"] == "/settings"
    assert "enabled = true" in config.read_text().split("[prune]", 1)[1].split("[", 1)[0]
    backups = sorted(data_dir.glob("config.toml.bak-*"))
    assert len(backups) == 1 and "enabled = false" in backups[0].read_text()
    assert 'href="/prune"' in client.get("/").text
    assert "Clean up is on." in _advanced(client.get("/settings").text)

    off = client.post(
        "/settings/cleanup", data={"file_hash": _file_hash(client), "enabled": "0"}, follow_redirects=False
    )

    assert off.status_code == 303
    assert "enabled = false" in config.read_text().split("[prune]", 1)[1].split("[", 1)[0]
    assert 'href="/prune"' not in client.get("/").text
    assert ledger.read_bytes() == before


def test_the_clean_up_switch_adds_a_prune_table_when_there_is_none(client: TestClient, data_dir: Path) -> None:
    config = data_dir / "config.toml"
    config.write_text(config.read_text().replace("[prune]\nenabled = true  # Clean up", "# Clean up"))
    _login(client)
    assert "Clean up is off." in _advanced(client.get("/settings").text)

    client.post("/settings/cleanup", data={"file_hash": _file_hash(client), "enabled": "1"}, follow_redirects=False)

    from likearr.config import load_config

    assert load_config(config).prune.enabled is True
    assert load_config(config).prune.errors == ()


def test_the_clean_up_switch_refuses_a_stale_page(client: TestClient, data_dir: Path) -> None:
    _clean_up_off(data_dir)
    _login(client)
    config = data_dir / "config.toml"
    text = config.read_text()

    response = client.post("/settings/cleanup", data={"file_hash": "stale", "enabled": "1"}, follow_redirects=False)

    assert response.status_code == 409
    assert config.read_text() == text


def test_the_same_clean_up_answer_again_saves_nothing(client: TestClient, data_dir: Path) -> None:
    _login(client)
    config = data_dir / "config.toml"
    text = config.read_text()

    response = client.post(
        "/settings/cleanup", data={"file_hash": _file_hash(client), "enabled": "1"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert config.read_text() == text
    assert not list(data_dir.glob("config.toml.bak-*"))


def test_with_clean_up_off_settings_offers_no_promote_save_write_access(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _set_token_scope(data_dir, " ".join(READ))
    _clean_up_off(data_dir)
    _login(client)

    form = client.get("/settings").text.split('action="/settings/spotify/connect"', 1)[1].split("</form>", 1)[0]
    asked = client.post("/settings/spotify/connect", data={"promote_save": "1"}, follow_redirects=False)

    assert 'name="promote_save"' not in form
    assert "promote-save" not in form
    assert _asked_scopes(_paste_back_url(asked.text)) == READ  # a posted box is ignored while off


def test_with_clean_up_off_a_re_authorize_still_keeps_write_access_it_has(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching Clean up off never quietly drops Spotify access already approved."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID)
    _set_token_scope(data_dir, " ".join([*READ, *WRITE]))
    _clean_up_off(data_dir)
    _login(client)

    assert _asked_scopes(_paste_back_url(_connect_start(client))) == [*READ, *WRITE]


# ---------------------------------------------------------------- Lidarr library, first start


UNSET_LIBRARY = CONFIG.replace('root_folder = "/music"\nquality_profile = "Standard"\n', "")


def _lidarr_lists(monkeypatch: pytest.MonkeyPatch, root_folders: list[str]) -> None:
    """The fake CLI's `setup-profiles --json` answer, with no root folder chosen yet."""
    monkeypatch.setenv(
        "FAKE_SETUP_PROFILES",
        json.dumps(
            {
                "profiles": [
                    {"name": "Lean", "kind": "lean", "status": "ok", "id": 1},
                    {"name": "Full", "kind": "full", "status": "ok", "id": 2},
                ],
                "tag": {"name": "likearr", "status": "ok", "id": 3},
                "root_folder": {"path": "", "status": "unset", "applies": False},
                "todo": [],
                "needs_apply": False,
                "root_folders": root_folders,
                "quality_profiles": ["Lossless", "Standard"],
            }
        ),
    )


def _library(data_dir: Path) -> tuple[str, str]:
    from likearr.config import load_config

    lidarr = load_config(data_dir / "config.toml").lidarr
    return lidarr.root_folder, lidarr.quality_profile


def test_lidarrs_only_root_folder_is_taken_when_none_is_chosen(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / "config.toml").write_text(UNSET_LIBRARY)
    _lidarr_lists(monkeypatch, ["/music"])
    _login(client)

    preview = _preview(client)

    assert "not chosen yet" in preview
    _wait_until(lambda: _library(data_dir)[0] == "/music")
    assert _library(data_dir) == ("/music", ""), "the quality profile is always picked by hand"
    assert list(data_dir.glob("config.toml.bak-*")), "written like any Settings save, backup first"
    assert "# likearr - fixture config for the web tests." in (data_dir / "config.toml").read_text()


def test_two_root_folders_are_left_to_the_picker_and_a_pick_is_saved(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / "config.toml").write_text(UNSET_LIBRARY)
    _lidarr_lists(monkeypatch, ["/music", "/audiobooks"])
    _login(client)
    _preview(client)
    time.sleep(0.3)  # long enough for an after-callback that would (wrongly) pick one
    assert _library(data_dir) == ("", "")

    section = _section(client.get("/settings").text, "lidarr-setup")
    assert 'action="/settings/lidarr-library"' in section
    assert '<option value="/audiobooks">' in section
    assert '<option value="Lossless">' in section
    file_hash = re.search(r'id="lidarr-library".*?name="file_hash" value="([^"]+)"', section, re.S)[1]  # type: ignore[index]

    response = client.post(
        "/settings/lidarr-library",
        data={"file_hash": file_hash, "root_folder": "/audiobooks", "quality_profile": "Lossless"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _library(data_dir) == ("/audiobooks", "Lossless")
    assert 'action="/settings/lidarr-library"' not in _section(client.get("/settings").text, "lidarr-setup")


@pytest.mark.parametrize(
    "pick",
    [{"root_folder": "/elsewhere"}, {"quality_profile": "Made Up"}],
)
def test_a_pick_lidarr_did_not_list_writes_nothing(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch, pick: dict[str, str]
) -> None:
    (data_dir / "config.toml").write_text(UNSET_LIBRARY)
    _lidarr_lists(monkeypatch, ["/music", "/audiobooks"])
    _login(client)
    _preview(client)
    before = (data_dir / "config.toml").read_bytes()

    response = client.post(
        "/settings/lidarr-library", data={"file_hash": _file_hash(client), **pick}, follow_redirects=False
    )

    assert response.status_code == 303
    assert (data_dir / "config.toml").read_bytes() == before
    assert "not one of Lidarr's choices" in html.unescape(client.get("/settings").text)


def test_status_says_what_is_not_set_up_yet(client: TestClient, data_dir: Path) -> None:
    _login(client)
    assert 'id="setup-needed"' not in client.get("/").text

    (data_dir / "config.toml").write_text(UNSET_LIBRARY)
    page = html.unescape(client.get("/").text)

    assert 'id="setup-needed"' in page
    assert "[lidarr] root_folder and [lidarr] quality_profile are not set" in page
    assert 'href="/settings#lidarr-setup"' in page


def test_status_names_the_lidarr_url_variable_while_it_is_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    monkeypatch.delenv("LIKEARR_LIDARR_URL")

    page = client.get("/").text

    assert "LIKEARR_LIDARR_URL is not set" in page


@pytest.mark.parametrize(("config", "started"), [(UNSET_LIBRARY, True), (CONFIG, False)])
def test_the_service_previews_lidarr_at_start_only_while_the_library_is_unset(
    data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch, config: str, started: bool
) -> None:
    (data_dir / "config.toml").write_text(config)
    _lidarr_lists(monkeypatch, ["/music", "/audiobooks"])

    with TestClient(_app(data_dir, fake_cli, auto_preview_setup=True)):
        jobs = _jobs_of(data_dir, "lidarr-setup-preview")
        assert bool(jobs) is started
        if jobs:
            _wait_until(lambda: all(m.state != "running" for m in _jobs_of(data_dir, "lidarr-setup-preview")))
