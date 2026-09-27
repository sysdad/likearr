"""The Plans routes end to end (`likearr.web.routes.plans`): the Check for changes page, a dry
run and its review, applying it, "Not this one" and the files-on-disk column.

Split out of `test_app.py` with the routes themselves (#154); the shared fixtures are in
`conftest.py`, the fake CLI and the other shared helpers in `app_support.py`.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from likearr.adapters.state_sqlite import SqliteState
from tests.web.app_support import (
    API_KEY_SENTINEL,
    CONFIG,
    DENIABLE,
    NOW,
    _app,
    _apply_form,
    _cache_names,
    _enable_mqtt,
    _enable_webhook,
    _jobs,
    _jobs_of,
    _login,
    _names_done,
    _plan_monitoring,
    _record,
    _start_plan,
    _wait_for_job,
    _wait_until,
)

# ---------------------------------------------------------------- plans (#30)


def _schedule_fires_soon(data_dir: Path) -> None:
    """A schedule that fires 10 minutes after the fixture's fixed `NOW` (18:00 UTC): with no
    earlier finished plan, `_estimate` falls back to the 15-minute `PLAN_ESTIMATE`, so `now +
    estimate` (18:15) lands after this fire (18:10) and the plan page's overlap warning fires."""
    config = data_dir / "config.toml"
    text = config.read_text().replace('cron = "20 */6 * * *"', 'cron = "10 18 * * *"')
    config.write_text(text.replace('timezone = "America/New_York"', 'timezone = "UTC"'))


def test_the_plan_page_warns_about_an_overlap_without_naming_home_assistant(client: TestClient, data_dir: Path) -> None:
    """No `[health.mqtt]` in the fixture config (issue #139): the overlap warning names the health
    status generically, not Home Assistant's retained record."""
    _schedule_fires_soon(data_dir)
    _login(client)

    page = client.get("/plan").text

    assert "may still be running then" in page
    assert "the health status would show that skip, with no counts, until the next apply." in page
    assert "Home Assistant" not in page


def test_the_plan_page_names_home_assistant_in_the_overlap_warning_with_mqtt_configured(
    client: TestClient, data_dir: Path
) -> None:
    _schedule_fires_soon(data_dir)
    _enable_mqtt(data_dir)
    _login(client)

    page = client.get("/plan").text

    assert "Home Assistant's retained record would show that skip, with no counts, until the next apply." in page


def test_the_plan_page_warns_about_an_overlap_with_only_a_webhook_configured(
    client: TestClient, data_dir: Path
) -> None:
    """With only `[health.webhook]` set (issue #139), the neutral wording renders too: it never
    claims a notification target that isn't there."""
    _schedule_fires_soon(data_dir)
    _enable_webhook(data_dir)
    _login(client)

    page = client.get("/plan").text

    assert "the health status would show that skip, with no counts, until the next apply." in page
    assert "Home Assistant" not in page


def test_the_plan_page_offers_a_dry_run_with_the_shrink_choice(client: TestClient, data_dir: Path) -> None:
    from likearr.models import Guard
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.guards[:] = [Guard(code="source-shrink", message="liked_tracks shrank 40%", blocked_unmonitors=12)]
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), diff)
    _login(client)

    page = client.get("/plan").text

    assert 'name="accept_shrink"' in page
    assert "liked_tracks shrank 40%" in page
    assert "Wed 23 Sep 18:20 EDT" in page  # the next scheduled run
    assert 'action="/plan"' in page


def test_the_plan_page_warns_a_first_check_can_take_hours_with_no_state_database(
    client: TestClient, data_dir: Path
) -> None:
    (data_dir / "state.sqlite").unlink()
    _login(client)

    page = client.get("/plan").text

    assert "first check can take several hours" in page


def test_the_plan_page_does_not_warn_once_a_check_is_recorded(client: TestClient, data_dir: Path) -> None:
    from tests.adapters.test_state_sqlite import _diff

    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), _diff())
    _login(client)

    page = client.get("/plan").text

    assert "first check can take several hours" not in page


def test_a_dry_run_starts_as_a_run_job_writing_into_its_own_directory(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)

    job_id = _start_plan(client, accept_shrink="on")

    meta = json.loads((data_dir / "ui" / "jobs" / job_id / "meta.json").read_text())
    assert meta["kind"] == "plan"
    assert meta["argv"][-4:] == [
        "run",
        "--out",
        str(data_dir / "ui" / "jobs" / job_id / "diff.json"),
        "--accept-shrink",
    ]
    assert (data_dir / "ui" / "jobs" / job_id / "diff.json").is_file()
    job_page = client.get(f"/jobs/{job_id}").text
    # The review is on the finished check's own page: no click-through, the apply link at the end,
    # the CLI's own output tucked into the technical log.
    assert f'href="/plan/{job_id}/apply"' in job_page
    assert job_page.index("releases to monitor") < job_page.index("Apply these changes")
    assert job_page.index("Apply these changes") < job_page.index("Technical log")


def test_a_running_plan_job_notes_a_first_check_can_take_hours_with_no_state_database(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / "state.sqlite").unlink()
    monkeypatch.setenv("FAKE_RUN_SLEEP", "1")
    _login(client)

    job_id = client.post("/plan", data={}, follow_redirects=False).headers["location"].removeprefix("/jobs/")
    running = client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})

    assert "A first check can take hours" in running.text
    _wait_for_job(client, job_id)


def test_a_running_plan_job_does_not_note_first_check_once_one_is_recorded(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.adapters.test_state_sqlite import _diff

    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60), _diff())
    monkeypatch.setenv("FAKE_RUN_SLEEP", "1")
    _login(client)

    job_id = client.post("/plan", data={}, follow_redirects=False).headers["location"].removeprefix("/jobs/")
    running = client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})

    assert "A first check can take hours" not in running.text
    _wait_for_job(client, job_id)


# ---------------------------------------------------------------- failed job page: reason + remedy (#127)


def test_a_dry_run_is_not_started_while_a_scheduled_run_holds_the_lock(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    from likearr.adapters.lock import run_lock

    _login(client)
    with run_lock(data_dir / "likearr.lock"):
        response = client.post("/plan", data={})

    assert response.status_code == 409
    assert "scheduled run is in progress" in response.text


def test_the_review_page_reads_the_plan_in_plain_language(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    from likearr.models import PrimaryType, ReleaseGroup, Resolution, ResolutionStatus

    with SqliteState(data_dir / "state.sqlite") as state:
        rg = ReleaseGroup(
            mbid="rg-0", title="Deep Cut 0", artist_mbid="a1", artist_name="Fake Band", primary_type=PrimaryType.ALBUM
        )
        state.cache_resolution(Resolution(intent_key="liked:t0", status=ResolutionStatus.RESOLVED, release_group=rg))
    _login(client)
    job_id = _start_plan(client)

    page = client.get(f"/plan/{job_id}").text

    assert "releases to monitor" in page
    assert "Deep Cut 0" in page
    assert "Fake Band" in page
    assert "a liked song" in page
    assert "a liked song&#39;s album" in page
    assert "Releases to unmonitor" in page
    assert "Guards" in page
    assert f'href="/plan/{job_id}/apply"' in page
    assert "Deep Cut 59" not in page  # paged: 50 to a page


def test_a_section_can_be_filtered_and_paged(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)

    second = client.get(f"/plan/{job_id}/section/monitor", params={"page": "2"}).text
    filtered = client.get(f"/plan/{job_id}/section/monitor", params={"q": "cut 42"}).text
    nothing = client.get(f"/plan/{job_id}/section/monitor", params={"q": "zzz"}).text

    assert "Deep Cut 59" in second
    assert "Deep Cut 42" in filtered
    assert "Deep Cut 41" not in filtered
    assert "Nothing here matches" in nothing


def test_the_apply_action_appears_twice_with_the_same_href_and_label(client: TestClient, planned_diff: Path) -> None:
    """#97.2: reachable right under the summary cards and again at the bottom, from the one partial
    (`_plan_apply_action.html`) so the two copies can never disagree."""
    _login(client)
    job_id = _start_plan(client)

    page = client.get(f"/plan/{job_id}").text

    hrefs = re.findall(rf'href="(/plan/{job_id}/apply)"', page)
    labels = re.findall(r'class="button primary" href="[^"]+">([^<]+)</a>', page)
    assert len(hrefs) == 2
    assert hrefs[0] == hrefs[1]
    assert len(labels) == 2
    assert labels[0] == labels[1] == "Apply these changes…"


# ---------------------------------------------------------------- #35 review fixes


def test_a_page_that_is_a_digit_but_not_a_number_is_page_one(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)

    response = client.get(f"/plan/{job_id}/section/monitor", params={"page": "²"})
    huge = client.get(f"/plan/{job_id}/section/monitor", params={"page": "9" * 5000})

    assert response.status_code == 200
    assert "Deep Cut 0" in response.text
    assert huge.status_code == 200, "a page number too long for int() is page one, not a 500"


def test_a_broken_config_on_review_is_said_not_a_500(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)
    (data_dir / "config.toml").write_text(CONFIG + "\n[rules\n")

    review = client.get(f"/plan/{job_id}")
    section = client.get(f"/plan/{job_id}/section/monitor")

    assert review.status_code == 409 and "config.toml does not load" in review.text
    assert section.status_code == 409 and "config.toml does not load" in section.text


def test_shrinks_accepted_at_the_last_plan_are_said_so(client: TestClient, data_dir: Path) -> None:
    from tests.adapters.test_state_sqlite import _diff

    diff = _diff()
    diff.guards[:] = []
    diff.accept_shrink = True
    with SqliteState(data_dir / "state.sqlite") as state:
        state.record_run(_record(ts=int(NOW.timestamp()) - 60, dry_run=True), diff)
    _login(client)

    page = client.get("/plan").text

    assert "the shrink guards were skipped (accepted) at the last plan" in page
    assert "None fired" not in page


def test_plan_again_keeps_the_shrink_choice(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likearr.shell.diff_io import read_diff, write_diff

    diff = read_diff(planned_diff)
    diff.accept_shrink = True
    diff.config_fingerprint = {"rules": {}, "guards": {}}  # planned under other settings: superseded
    write_diff(diff, planned_diff)
    _login(client)
    job_id = _start_plan(client, accept_shrink="on")

    page = client.get(f"/plan/{job_id}").text

    assert "Check again" in page
    assert 'name="accept_shrink" checked' in page


# ---------------------------------------------------------------- apply (#30)


def test_a_finished_plan_records_its_token(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    from likearr.web.plans import plan_token_of_file

    _login(client)
    job_id = _start_plan(client)

    meta = json.loads((data_dir / "ui" / "jobs" / job_id / "meta.json").read_text())
    assert meta["plan_token"] == plan_token_of_file(job_id, data_dir / "ui" / "jobs" / job_id / "diff.json")


def test_the_apply_page_restates_the_plan_and_offers_accept_health_never_force(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)
    assert f'href="/plan/{job_id}/apply"' in client.get(f"/plan/{job_id}").text

    page = client.get(f"/plan/{job_id}/apply").text

    assert "releases to monitor" in page
    assert 'name="accept_health"' in page
    assert f"likearr run --apply {data_dir / 'ui' / 'jobs' / job_id / 'diff.json'}" in page
    assert "--force" not in page
    assert 'name="plan_token"' in page


def test_applying_starts_a_draining_apply_job_on_that_plans_file(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)

    response = client.post(
        f"/plan/{job_id}/apply", data={**_apply_form(client, job_id), "accept_health": "on"}, follow_redirects=False
    )

    assert response.status_code == 303
    apply_id = response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)
    meta = json.loads((data_dir / "ui" / "jobs" / apply_id / "meta.json").read_text())
    assert meta["kind"] == "apply"
    assert meta["drain"] is True
    assert meta["plan_id"] == job_id
    assert meta["argv"][-4:] == [
        "run",
        "--apply",
        str(data_dir / "ui" / "jobs" / job_id / "diff.json"),
        "--accept-health",
    ]
    assert "--force" not in meta["argv"]


def test_a_plan_whose_file_changed_since_review_is_refused(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    diff_file = data_dir / "ui" / "jobs" / job_id / "diff.json"
    diff_file.write_text(diff_file.read_text().replace('"source_digest": "abc"', '"source_digest": "zzz"'))

    response = client.post(f"/plan/{job_id}/apply", data=form)

    assert response.status_code == 409
    assert "edited on disk" in response.text
    assert [m for m in (data_dir / "ui" / "jobs").iterdir() if m.name != job_id] == []


def test_a_forged_token_is_refused(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)

    response = client.post(f"/plan/{job_id}/apply", data={"plan_token": "0" * 64})

    assert response.status_code == 409


def test_an_apply_waits_for_a_scheduled_run(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    from likearr.adapters.lock import run_lock

    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    with run_lock(data_dir / "likearr.lock"):
        response = client.post(f"/plan/{job_id}/apply", data=form)

    assert response.status_code == 409
    assert "scheduled run is in progress" in response.text


def test_a_stale_apply_is_a_message_with_a_way_forward(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_APPLY_EXIT", "3")
    monkeypatch.setenv("FAKE_APPLY_MESSAGE", "the settings changed since this diff was planned: [rules] deny_releases")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    final = _wait_for_job(client, apply_id)

    assert "the settings changed since this diff was planned: [rules] deny_releases" in final
    assert "Nothing was applied" in final
    assert 'action="/plan"' in final
    assert "Failed" not in final


# ---------------------------------------------------------------- "Not this one" (#30)


def test_not_this_one_refuses_a_value_that_is_no_release(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)

    assert client.post(f"/plan/{job_id}/deny", data={"release": "../../etc"}).status_code == 400


# ---------------------------------------------------------------- apply slice review fixes


def test_not_this_one_takes_only_a_release_this_plan_monitors(client: TestClient, planned_diff: Path) -> None:
    _login(client)
    job_id = _start_plan(client)

    response = client.post(f"/plan/{job_id}/deny", data={"release": DENIABLE})  # a valid MBID, not in the plan

    assert response.status_code == 400


def test_a_broken_config_on_apply_and_deny_is_said_not_a_500(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _plan_monitoring(planned_diff)
    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    (data_dir / "config.toml").write_text(CONFIG + "\n[rules\n")

    page = client.get(f"/plan/{job_id}/apply")
    applied = client.post(f"/plan/{job_id}/apply", data=form)
    denied = client.post(f"/plan/{job_id}/deny", data={"release": DENIABLE})

    for response in (page, applied, denied):
        assert response.status_code == 409
        assert "config.toml does not load" in response.text
    assert not any(m.kind == "apply" for m in _jobs(data_dir))


def test_a_stale_apply_with_no_printed_record_still_offers_plan_again(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_APPLY_EXIT", "3")
    monkeypatch.setenv("FAKE_APPLY_NO_RECORD", "1")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    final = _wait_for_job(client, apply_id)

    assert "Spotify, Lidarr or the settings changed since this plan was made." in final
    assert "Check again" in final


def test_plan_again_after_a_stale_apply_keeps_the_shrink_choice(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likearr.shell.diff_io import read_diff, write_diff

    diff = read_diff(planned_diff)
    diff.accept_shrink = True
    write_diff(diff, planned_diff)
    monkeypatch.setenv("FAKE_APPLY_EXIT", "3")
    _login(client)
    job_id = _start_plan(client, accept_shrink="on")
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    final = _wait_for_job(client, apply_id)

    assert 'name="accept_shrink" checked' in final


def test_the_apply_job_runs_exactly_run_apply_on_the_plans_file_and_never_force(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    job_id = _start_plan(client)
    form = {**_apply_form(client, job_id), "accept_health": "on", "force": "on", "args": "--force"}

    apply_id = client.post(f"/plan/{job_id}/apply", data=form, follow_redirects=False).headers["location"][6:]
    _wait_for_job(client, apply_id)

    meta = next(m for m in _jobs(data_dir) if m.id == apply_id)
    tail = meta.argv[meta.argv.index("run") :]
    assert tail == ["run", "--apply", str(data_dir / "ui" / "jobs" / job_id / "diff.json"), "--accept-health"]
    assert "--force" not in meta.argv
    assert meta.drain and meta.plan_id == job_id


# ---------------------------------------------------------------- #38 adversarial review


def test_an_apply_cannot_be_cancelled(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_APPLY_SLEEP", "1")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    refused = client.post(f"/jobs/{apply_id}/cancel")

    assert refused.status_code == 409
    assert "An apply is not cancelled" in refused.text
    assert "Finished." in _wait_for_job(client, apply_id)


def test_editing_the_plans_contents_after_review_is_refused(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    from likearr.shell.diff_io import read_diff, write_diff

    _login(client)
    job_id = _start_plan(client)
    form = _apply_form(client, job_id)
    path = data_dir / "ui" / "jobs" / job_id / "diff.json"
    diff = read_diff(path)
    del diff.monitor[1:]  # cut monitors; the digests and settings it names are unchanged
    write_diff(diff, path)

    response = client.post(f"/plan/{job_id}/apply", data=form)

    assert response.status_code == 409
    assert "edited on disk since you reviewed them" in response.text
    assert not any(m.kind == "apply" for m in _jobs(data_dir))


def test_a_stale_browser_apply_says_what_will_show_as_stale_without_mqtt(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `[health.mqtt]` in the fixture config (issue #139): the job fragment names Status only,
    never Home Assistant."""
    monkeypatch.setenv("FAKE_APPLY_EXIT", "3")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    page = _wait_for_job(client, apply_id)
    assert "Status will show this as needing attention until the next scheduled run." in page
    assert "Home Assistant" not in page


def test_a_stale_browser_apply_names_home_assistant_with_mqtt_configured(
    client: TestClient, data_dir: Path, planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_mqtt(data_dir)
    monkeypatch.setenv("FAKE_APPLY_EXIT", "3")
    _login(client)
    job_id = _start_plan(client)
    apply_id = client.post(f"/plan/{job_id}/apply", data=_apply_form(client, job_id), follow_redirects=False).headers[
        "location"
    ][6:]

    page = _wait_for_job(client, apply_id)
    assert (
        'Status will show this as needing attention, and Home Assistant as "stale", until the next scheduled run.'
        in page
    )


# ---------------------------------------------------------------- UI design pass


def test_starting_a_check_is_the_primary_action_and_accept_shrink_is_optional(client: TestClient) -> None:
    _login(client)

    page = client.get("/plan").text

    assert '<button type="submit" class="primary">Check for changes</button>' in page
    advanced = page[page.index("<summary>Advanced (optional)</summary>") :]
    assert 'name="accept_shrink"' in advanced
    assert "You don't need this to check." in advanced
    assert "--accept-" not in page  # CLI flags are not named on the label (issue #139)


def test_apply_is_the_one_required_action_and_accept_health_is_optional_and_unticked(
    client: TestClient, planned_diff: Path
) -> None:
    """No `[health.mqtt]` in the fixture config (issue #139): the optional-box copy names Status
    only, the raw command is tucked behind a disclosure, and no checkbox label names a CLI flag."""
    _login(client)
    job_id = _start_plan(client)

    page = client.get(f"/plan/{job_id}/apply").text

    assert '<button type="submit" class="primary">Apply these changes</button>' in page
    assert page.index("Apply these changes</button>") < page.index("<summary>Advanced (optional)</summary>")
    tag = re.search(r'<input type="checkbox" name="accept_health"[^>]*>', page)[0]  # type: ignore[index]
    assert "checked" not in tag
    assert "You don't need this to apply." in page
    assert 'stop showing as "needs attention" here' in page
    assert "Home Assistant" not in page
    assert "--accept-" not in page  # the checkbox label names no CLI flag
    assert re.search(r"<details><summary>Show the command</summary><code>[^<]+</code></details>", page)
    before_details = page[: page.index("<details><summary>Show the command</summary>")]
    assert "likearr run" not in before_details  # the argv itself is only inside the disclosure


def test_apply_names_home_assistant_in_the_optional_box_with_mqtt_configured(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _enable_mqtt(data_dir)
    _login(client)
    job_id = _start_plan(client)

    page = client.get(f"/plan/{job_id}/apply").text

    assert 'stop showing as "needs attention" here and in Home Assistant' in page


def test_a_check_with_nothing_to_change_says_so(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    from likearr.shell.diff_io import read_diff, write_diff

    diff = read_diff(planned_diff)
    for part in (
        diff.add_artists,
        diff.monitor,
        diff.unmonitor,
        diff.ratchets,
        diff.monitor_artists,
        diff.set_new_items_none,
        diff.refresh_artists,
    ):
        part.clear()
    diff.guards.clear()
    write_diff(diff, planned_diff)
    _login(client)
    job_id = _start_plan(client)

    page = client.get(f"/jobs/{job_id}").text

    assert "Nothing to change." in page
    assert "Nothing in: artists to add, releases to monitor" in page


def _only_monitor_new_albums(data_dir: Path, planned_diff: Path) -> None:
    """Rewrite the fixture plan so its one change is setting "Monitor New Albums" to None on one
    artist, named from the cached resolutions as a real one would be."""
    from likearr.models import PrimaryType, ReleaseGroup, Resolution, ResolutionStatus
    from likearr.shell.diff_io import read_diff, write_diff

    diff = read_diff(planned_diff)
    for part in (
        diff.add_artists,
        diff.monitor,
        diff.unmonitor,
        diff.ratchets,
        diff.monitor_artists,
        diff.refresh_artists,
    ):
        part.clear()
    diff.guards.clear()
    assert diff.set_new_items_none == ["a1"]
    write_diff(diff, planned_diff)
    with SqliteState(data_dir / "state.sqlite") as state:
        rg = ReleaseGroup(
            mbid="rg-0", title="Deep Cut 0", artist_mbid="a1", artist_name="Fake Band", primary_type=PrimaryType.ALBUM
        )
        state.cache_resolution(Resolution(intent_key="liked:t0", status=ResolutionStatus.RESOLVED, release_group=rg))


def test_a_check_whose_only_change_is_monitor_new_albums_shows_it_and_offers_a_real_apply(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    """#172: the review used to say "Nothing to change" and offer "Apply anyway" for this plan."""
    _only_monitor_new_albums(data_dir, planned_diff)
    _login(client)
    job_id = _start_plan(client)

    review = client.get(f"/plan/{job_id}").text
    confirm = client.get(f"/plan/{job_id}/apply").text

    section = review[review.index("<h2>Artists to stop auto-monitoring") :].split("</section>")[0]
    assert "Fake Band" in section
    assert "artists to stop auto-monitoring" in review  # its count card
    for page in (review, confirm):
        assert "Nothing to change." not in page
        assert "Apply anyway" not in page
    assert "Apply these changes…" in review
    assert '<button type="submit" class="primary">Apply these changes</button>' in confirm


# ---------------------------------------------------------------- files on disk behind an unmonitor (#30)


def _counting_app(data_dir: Path, fake_cli: list[str]) -> Any:
    return _app(data_dir, fake_cli, auto_count_files=True)


def _files_done(data_dir: Path) -> bool:
    jobs = _jobs_of(data_dir, "files")
    return bool(jobs) and all(m.state != "running" for m in jobs)


def test_a_check_counts_the_files_behind_its_unmonitors_in_a_child_job(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    with TestClient(_counting_app(data_dir, fake_cli), base_url="http://testserver") as client:
        _login(client)
        plan_id = _start_plan(client)
        _wait_until(lambda: _files_done(data_dir))
        page = client.get(f"/plan/{plan_id}").text

    (files,) = _jobs_of(data_dir, "files")
    assert files.plan_id == plan_id and files.state == "done"
    plan_file = str(data_dir / "ui" / "jobs" / plan_id / "diff.json")
    assert files.argv[files.argv.index("lidarr-files") :] == [
        "lidarr-files",
        "--plan",
        plan_file,
        "--out",
        str(data_dir / "ui" / "jobs" / files.id / "files.json"),
    ]
    assert "<th>On disk</th>" in page
    assert "12 files on disk (stay where they are)" in page


def test_after_a_check_the_names_fetch_waits_for_the_file_count(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The after-check chain's order (#266 moved it): the count starts first, and the names fetch
    only once the count has finished, never alongside it or instead of it."""
    _cache_names(data_dir, {"pl-owned": "Road trip"})  # pl-gone has no name: names are needed
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    ticks = itertools.count()  # a clock that moves a second a read: job times are to the second
    app = _app(
        data_dir,
        fake_cli,
        auto_count_files=True,
        auto_fetch_names=True,
        now=lambda: NOW + timedelta(seconds=next(ticks)),
    )

    def chain_done() -> bool:
        try:
            return _files_done(data_dir) and len(_jobs_of(data_dir, "playlists")) == 2 and _names_done(data_dir)
        except FileNotFoundError:  # a job directory made a moment before its meta.json
            return False

    with TestClient(app, base_url="http://testserver") as client:
        _wait_until(lambda: _names_done(data_dir))  # the one at start
        _login(client)
        plan_id = _start_plan(client)
        _wait_until(chain_done)

    (files,) = _jobs_of(data_dir, "files")
    after_check = max(_jobs_of(data_dir, "playlists"), key=lambda m: datetime.fromisoformat(m.started_at))
    assert files.plan_id == plan_id and files.state == "done"
    assert datetime.fromisoformat(after_check.started_at) > datetime.fromisoformat(files.finished_at)


def test_a_failed_count_says_the_file_count_is_unavailable(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("FAKE_FILES_FAIL", "1")
    with TestClient(_counting_app(data_dir, fake_cli), base_url="http://testserver") as client:
        _login(client)
        plan_id = _start_plan(client)
        _wait_until(lambda: _files_done(data_dir))
        page = client.get(f"/plan/{plan_id}").text

    assert _jobs_of(data_dir, "files")[0].state == "failed"
    assert "file count unavailable" in page
    assert "files on disk" not in page


def test_while_counting_the_unmonitor_rows_refresh_themselves(
    data_dir: Path, fake_cli: list[str], planned_diff: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setenv("FAKE_FILES_SLEEP", "3")
    with TestClient(_counting_app(data_dir, fake_cli), base_url="http://testserver") as client:
        _login(client)
        plan_id = _start_plan(client)
        section = client.get(f"/plan/{plan_id}/section/unmonitor").text
        _wait_until(lambda: _files_done(data_dir))
        after = client.get(f"/plan/{plan_id}/section/unmonitor").text

    assert "counting files..." in section
    assert 'hx-trigger="load delay:2s"' in section
    assert "12 files on disk (stay where they are)" in after
    assert "hx-trigger" not in after  # counted: no more refreshing


def test_a_server_that_does_not_count_shows_no_column_and_starts_no_job(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    _login(client)
    plan_id = _start_plan(client)

    page = client.get(f"/plan/{plan_id}").text

    assert _jobs_of(data_dir, "files") == []
    assert "On disk" not in page
