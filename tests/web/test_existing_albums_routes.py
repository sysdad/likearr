"""A first check's "Albums you already monitor" in the web UI: the review section, the confirm
step that repeats the choice, and the `run --apply` flags the choice becomes."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from starlette.testclient import TestClient

from likearr.core.adopt import HELD_ITEM, AdoptPlan, HeldRelease
from likearr.models import OwnedRelease, Reason, ReasonKind, ReleaseKey, UnmonitorRelease
from likearr.shell.adopt_io import ExistingAlbums, existing_to_dict
from likearr.shell.diff_io import read_diff, write_diff
from tests.web.app_support import _forget_first_apply, _login, _start_plan, _wait_for_job

KEEP = "bbbbbbbb-0000-0000-0000-000000000002"
DROP = "bbbbbbbb-0000-0000-0000-000000000003"
HELD = "bbbbbbbb-0000-0000-0000-000000000004"
CLAIM = "bbbbbbbb-0000-0000-0000-000000000001"


def _with_existing(planned_diff: Path) -> None:
    """One match, two albums nothing wants and one held, written into the planned diff."""
    adoption = AdoptPlan(
        claim=[
            OwnedRelease(
                key=ReleaseKey("h1", CLAIM),
                reasons=frozenset({Reason(ReasonKind.SAVED, "al-1")}),
                step="album:barcode",
                resolver_version=1,
                monitored_at=datetime(2026, 9, 23),
                lidarr_album_id=11,
            )
        ],
        unmonitor=[
            UnmonitorRelease(key=ReleaseKey("h2", KEEP), title="Kept By Hand", lost_reasons=frozenset()),
            UnmonitorRelease(key=ReleaseKey("h2", DROP), title="Nobody Wants", lost_reasons=frozenset()),
        ],
        held=[HeldRelease(key=ReleaseKey("h2", HELD), title="Held Back", step="error:metadata", cause=HELD_ITEM)],
        unmonitor_rest=True,
    )
    existing = ExistingAlbums(
        adoption=adoption,
        lidarr_digest="d",
        artists={"h1": "Hand Artist", "h2": "Other Hand"},
        titles={CLAIM: "Matching Album"},
    )
    write_diff(read_diff(planned_diff), planned_diff, existing_albums=existing_to_dict(existing))


def _plan(client: TestClient, data_dir: Path, planned_diff: Path, *, first_applied: bool = False) -> str:
    _with_existing(planned_diff)
    if not first_applied:
        _forget_first_apply(data_dir)
    _login(client)
    return _start_plan(client)


def _token(client: TestClient, job_id: str) -> str:
    page = client.get(f"/plan/{job_id}/apply").text
    return re.search(r'name="plan_token" value="([0-9a-f]+)"', page)[1]  # type: ignore[index]


def test_the_review_opens_with_the_albums_already_monitored_before_the_first_apply(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    page = client.get(f"/plan/{job_id}").text
    section = page.split('id="existing-albums"', 1)[1].split("</section>", 1)[0]

    assert page.index('id="existing-albums"') < page.index('class="plan-counts"')
    assert "1 match what you like on Spotify." in section
    assert re.search(r'<input type="checkbox" name="claim" value="1" form="existing" checked>', section)
    assert "If you unlike one later, it's unmonitored." in section
    assert "3 don't match. They stay monitored, and likearr never touches them." in section
    assert '<details class="panel advanced" id="unmonitor-rest">' in section, "collapsed"
    assert "hard to undo by hand" in section
    assert 'name="unmonitor_rest" value="1" form="existing">' in section, "unticked"
    assert f'name="keep" value="{KEEP}"' in section and f'name="keep" value="{DROP}"' in section
    assert f'name="keep" value="{HELD}"' not in section, "a held album has no keep tick"
    assert "Held" in section and "could not be looked up" in section
    assert "Matching Album" in section and "you saved the album" in section
    assert '<button type="submit" class="primary" form="existing">' in page
    assert f'action="/plan/{job_id}/confirm"' in page


def test_the_section_is_gone_after_the_first_apply(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    job_id = _plan(client, data_dir, planned_diff, first_applied=True)

    page = client.get(f"/plan/{job_id}").text

    assert "Albums you already monitor" not in page
    assert 'form="existing"' not in page


def test_confirm_repeats_a_claim_only_choice(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    page = client.post(f"/plan/{job_id}/confirm", data={"claim": "1"}).text

    assert re.search(r'<span class="n">1</span><span class="what">to manage</span>', page)
    assert "to unmonitor" not in page.split("Albums you already monitor", 1)[1].split("<form", 1)[0]
    assert "Albums that don't match stay monitored." in page
    assert '<input type="hidden" name="claim" value="1">' in page
    assert "--claim-existing" in page and "--unmonitor-rest" not in page


def test_confirm_repeats_the_unmonitor_counts_and_its_warning(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    page = client.post(f"/plan/{job_id}/confirm", data={"unmonitor_rest": "1", "keep": [KEEP, HELD, "not-listed"]}).text

    assert re.search(r'<span class="n zero">0</span><span class="what">to manage</span>', page), "claim unticked"
    assert re.search(r'<span class="n">1</span><span class="what">to unmonitor <span class="note">\(1 kept\)', page)
    assert re.search(r'<span class="n">1</span><span class="what">held, stay monitored</span>', page)
    assert "1 album you monitor will be unmonitored in Lidarr." in page
    assert f'<input type="hidden" name="keep" value="{KEEP}">' in page
    assert f'name="keep" value="{HELD}"' not in page and "not-listed" not in page


def test_the_default_confirm_page_claims_and_unmonitors_nothing(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    page = client.get(f"/plan/{job_id}/apply").text

    assert re.search(r'<span class="n">1</span><span class="what">to manage</span>', page)
    assert "will be unmonitored in Lidarr" not in page


def _apply(client: TestClient, data_dir: Path, job_id: str, **form: str | list[str]) -> list[str]:
    response = client.post(
        f"/plan/{job_id}/apply", data={"plan_token": _token(client, job_id), **form}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    apply_id = response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, apply_id)
    return json.loads((data_dir / "ui" / "jobs" / apply_id / "meta.json").read_text())["argv"]


def test_applying_passes_the_claim(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    argv = _apply(client, data_dir, job_id, claim="1")

    assert argv[-4:] == ["run", "--apply", str(data_dir / "ui" / "jobs" / job_id / "diff.json"), "--claim-existing"]


def test_applying_with_the_claim_unticked_passes_nothing(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    argv = _apply(client, data_dir, job_id)

    assert argv[-1] == str(data_dir / "ui" / "jobs" / job_id / "diff.json")


def test_applying_the_unmonitor_writes_the_kept_albums_for_keep(
    client: TestClient, data_dir: Path, planned_diff: Path
) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    argv = _apply(client, data_dir, job_id, claim="1", unmonitor_rest="1", keep=[KEEP, "not-listed"])

    keep_file = data_dir / "ui" / "jobs" / job_id / "existing-keep.txt"
    assert argv[argv.index("--apply") + 2 :] == ["--claim-existing", "--unmonitor-rest", "--keep", str(keep_file)]
    assert keep_file.read_text() == f"{KEEP}\n"
    assert oct(keep_file.stat().st_mode & 0o777) == "0o600"


def test_a_keep_without_the_unmonitor_is_dropped(client: TestClient, data_dir: Path, planned_diff: Path) -> None:
    job_id = _plan(client, data_dir, planned_diff)

    argv = _apply(client, data_dir, job_id, keep=[KEEP])

    assert "--keep" not in argv
    assert not (data_dir / "ui" / "jobs" / job_id / "existing-keep.txt").exists()
