"""Clean up's routes end to end (`likearr.web.routes.cleanup`): building a prune report,
deciding and exporting, the carried-over decisions and the ledger, always-kept albums in words,
and the read-only previews and finish checklist.

Split out of `test_app.py` with the routes themselves; the shared fixtures are in
`conftest.py`, the fake CLI and the other shared helpers in `app_support.py`.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from likearr.web.app import WebSettings, create_app
from likearr.web.auth import LoginLimiter
from tests.web.app_support import (
    _PUBLIC_URL,
    API_KEY_SENTINEL,
    CONFIG,
    NOW,
    PASSWORD,
    _build_prune,
    _cache_names,
    _clean_up_off,
    _login,
    _wait_for_job,
    _wait_until,
    _web_of,
)

# ---------------------------------------------------------------- prune review


def test_the_prune_page_builds_the_report_as_a_job_and_starts_nothing_by_itself(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    _login(client)

    page = client.get("/prune").text
    assert '<button type="submit" class="primary">Find unneeded albums</button>' in page
    assert "<h1>Clean up your library</h1>" in page
    assert "a few minutes once the first check has run" in page
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())

    job_id = _build_prune(client)

    meta = json.loads((data_dir / "ui" / "jobs" / job_id / "meta.json").read_text())
    assert meta["kind"] == "prune"
    assert meta["argv"][-3:] == ["prune-report", "--out", str(data_dir / "ui" / "jobs" / job_id / "prune.json")]
    assert f'href="/prune/{job_id}"' in client.get("/prune").text  # continue the review


def test_a_finished_search_opens_on_its_review_with_no_click_through(client: TestClient, prune_report: Path) -> None:
    _login(client)
    response = client.post("/prune", follow_redirects=False)
    job_id = response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, job_id)

    fragment = client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})
    page = client.get(f"/jobs/{job_id}", follow_redirects=False)
    review = client.get(f"/prune/{job_id}").text

    assert fragment.status_code == 286  # polling stops...
    assert fragment.headers["HX-Redirect"] == f"/prune/{job_id}"  # ...and the browser goes to the review
    assert page.status_code == 303 and page.headers["location"] == f"/prune/{job_id}"
    assert ">Decide what to keep</a>" not in review  # the old click-through
    assert "<summary>Technical log</summary>" in review
    assert "likearr prune report: 4 candidates" in review  # the CLI's own words, tucked away


def test_a_search_that_failed_stays_on_its_job_page(client: TestClient, data_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_PRUNE", str(data_dir / "no-such-report.json"))
    _login(client)
    response = client.post("/prune", follow_redirects=False)
    job_id = response.headers["location"].removeprefix("/jobs/")
    fragment = _wait_for_job(client, job_id)

    page = client.get(f"/jobs/{job_id}", follow_redirects=False)

    assert page.status_code == 200
    assert "FileNotFoundError" in page.text and "FileNotFoundError" in fragment
    assert "HX-Redirect" not in client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"}).headers


def test_a_finished_search_with_an_unreadable_report_keeps_its_log(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    prune_report.write_text("{")
    _login(client)
    job_id = _build_prune(client)

    page = client.get(f"/jobs/{job_id}", follow_redirects=False)

    assert page.status_code == 200
    assert "likearr prune report: 4 candidates" in page.text


def test_the_review_lists_artists_largest_first_with_their_albums(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)

    page = client.get(f"/prune/{job_id}").text

    assert page.index("Big Band") < page.index("Small Band") < page.index("Guarded")
    assert "Only copy of a song you liked. Its album, <a" in page  # the report's line, in words
    assert "faketrack0000000000064" not in page
    assert ">Same as artist: not decided yet</option>" in page
    assert f'id="artist-{BIG}"' in page
    assert "0 of 3" in page  # reviewed so far


def test_a_decision_is_saved_and_answers_with_the_row_and_the_summary(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG

    _login(client)
    job_id = _build_prune(client)

    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    response = client.post(
        f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "release": RG[0], "value": "keep"}
    )

    assert response.status_code == 200
    assert "decision-trash" in response.text
    assert '<div id="prune-summary" hx-swap-oob="true">' in response.text
    assert "1 of 3" in response.text
    draft = json.loads((data_dir / "ui" / "jobs" / job_id / "prune-draft.json").read_text())
    assert draft["artists"] == {BIG: "trash"} and draft["releases"] == {RG[0]: "keep"}


@pytest.mark.parametrize(
    "form",
    [
        {"artist": "44444444-4444-4444-4444-444444444444", "decision": "trash"},
        {"artist": "../../etc", "decision": "trash"},
        {"decision": "trash"},
    ],
)
def test_a_decision_outside_the_report_is_refused(client: TestClient, prune_report: Path, form: dict[str, str]) -> None:
    _login(client)
    job_id = _build_prune(client)

    assert client.post(f"/prune/{job_id}/decide", data=form).status_code == 400


def test_a_protected_album_cannot_be_moved_out(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import GUARDED, RG

    _login(client)
    job_id = _build_prune(client)

    response = client.post(
        f"/prune/{job_id}/decide", data={"artist": GUARDED, "rev": "0", "release": RG[4], "value": "trash"}
    )

    assert response.status_code == 400
    assert "always kept" in response.text


def test_the_table_filters_by_decision(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})

    trashed = client.get(f"/prune/{job_id}/rows", params={"show": "trash"}).text
    undecided = client.get(f"/prune/{job_id}/rows", params={"show": "undecided", "q": "small"}).text

    assert "Big Band" in trashed and "Small Band" not in trashed
    assert "Small Band" in undecided and "Big Band" not in undecided


def test_export_writes_both_files_beside_the_report_and_shows_the_commands(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG, SMALL

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "0", "decision": "save"})

    response = client.post(f"/prune/{job_id}/export", data={"notes": "first pass"}, follow_redirects=True)

    job_dir = data_dir / "ui" / "jobs" / job_id
    decisions = json.loads((job_dir / "decisions.json").read_text())
    assert decisions == {
        "version": 1,
        "trash": [RG[0], RG[1]],
        "trash_artists": [],
        "promote": [],
        "save": [SMALL],
        "save_releases": [],
        "save_exclude_releases": [],
        "notes": "first pass",
    }
    assert json.loads((job_dir / "review-data.json").read_text())["artists"]
    assert f"--decisions {job_dir}/decisions.json" in response.text
    assert f"--reviewed {job_dir}/review-data.json" in response.text


def test_the_downloads_are_attachments_of_the_current_decisions(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "promote"})

    response = client.get(f"/prune/{job_id}/download/decisions.json")

    assert response.headers["content-disposition"] == 'attachment; filename="decisions.json"'
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["promote"] == [BIG]
    assert client.get(f"/prune/{job_id}/download/prune-draft.json").status_code == 404
    assert client.get(f"/prune/{job_id}/download/..%2Fmeta.json").status_code == 404


# ---------------------------------------------------------------- two tabs, stale exports, expiry


def test_a_stale_second_tab_gets_409_and_cannot_undo_a_keep(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    # Both tabs now show the row at revision 1. Tab B keeps album First.
    kept = client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "release": RG[0], "value": "keep"})
    assert kept.status_code == 200

    # Tab A, still at revision 1, changes album Second.
    stale = client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "release": RG[1], "value": "keep"})

    assert stale.status_code == 409
    assert "Changed in another tab" in stale.text
    assert (
        '<option value="keep" selected>Keep - no change on Spotify</option>' in stale.text
    )  # the row as saved: First kept
    draft = json.loads((data_dir / "ui" / "jobs" / job_id / "prune-draft.json").read_text())
    assert draft["releases"] == {RG[0]: "keep"}
    decisions = client.get(f"/prune/{job_id}/download/decisions.json").json()
    assert decisions["trash"] == [RG[1]] and decisions["trash_artists"] == []


def test_a_change_after_export_removes_the_stale_files_and_says_so(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, SMALL

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    job_dir = data_dir / "ui" / "jobs" / job_id
    assert (job_dir / "decisions.json").is_file()

    response = client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "0", "decision": "save"})

    assert not (job_dir / "decisions.json").exists() and not (job_dir / "review-data.json").exists()
    assert "Changed since export - export again." in response.text
    page = client.get(f"/prune/{job_id}").text
    assert "Changed since export - export again." in page
    assert "likearr-cli prune-stage" not in page
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    page = client.get(f"/prune/{job_id}").text
    assert "Changed since export" not in page and "likearr-cli prune-stage" in page


def test_albums_trashed_under_undecided_artists_are_named_before_export(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import RG, SMALL

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "0", "release": RG[2], "value": "trash"})

    page = client.get(f"/prune/{job_id}").text

    assert page.index("1 album set to trash under artists you haven") < page.index("Export the decisions")
    assert "0 of 3" in page  # still unreviewed


def test_a_report_that_expired_mid_review_says_so(client: TestClient, data_dir: Path, prune_report: Path) -> None:
    import shutil

    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)
    shutil.rmtree(data_dir / "ui" / "jobs" / job_id)

    decided = client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    exported = client.post(f"/prune/{job_id}/export", data={"notes": ""})

    for response in (decided, exported):
        assert response.status_code == 409
        assert "This report expired - build a new one." in response.text


def test_a_revision_that_is_not_a_small_number_is_a_stale_tab(client: TestClient, prune_report: Path) -> None:
    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)

    for rev in ["-1", "x", "9" * 5000]:
        response = client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": rev, "decision": "trash"})
        assert response.status_code == 409


# ---------------------------------------------------------------- Clean up carries decisions over


@pytest.fixture
def followed_report(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.web.test_prune import FOLLOWED_REPORT

    path = data_dir / "fixture-followed.json"
    path.write_text(json.dumps(FOLLOWED_REPORT))
    monkeypatch.setenv("FAKE_PRUNE", str(path))
    return path


def _card(page: str, mbid: str) -> str:
    start = page.index(f'id="artist-{mbid}"')
    end = page.find('class="card prune-artist', start + 1)
    return page[start : end if end != -1 else len(page)]


def test_a_followed_artist_has_a_tag_no_follow_option_and_a_reason_per_album(
    client: TestClient, followed_report: Path
) -> None:
    from tests.web.test_prune import LAWRENCE, NOBODY, QUEEN

    _login(client)
    job_id = _build_prune(client)

    page = client.get(f"/prune/{job_id}").text

    for mbid in (QUEEN, LAWRENCE):
        card = _card(page, mbid)
        assert "Followed on Spotify</span>" in card
        assert 'value="promote"' not in card
        assert "Trash all listed albums" in card
    assert "Compilation: following Queen brings studio albums and EPs only" in page
    assert "Live: following Queen brings studio albums and EPs only" in page
    assert "Single: following Lawrence brings studio albums and EPs only" in page
    nobody = _card(page, NOBODY)
    assert 'value="promote"' in nobody and "Followed on Spotify" not in nobody
    assert "You don&#39;t follow Nobody Much on Spotify" in nobody
    assert "Trash goes to the holding folder. Nothing is deleted until you empty it yourself." in page
    refused = client.post(f"/prune/{job_id}/decide", data={"artist": QUEEN, "rev": "0", "decision": "promote"})
    assert refused.status_code == 400 and "already followed" in refused.text


def test_an_album_can_be_kept_and_saved_on_its_own_even_a_protected_one(
    client: TestClient, data_dir: Path, followed_report: Path
) -> None:
    from tests.web.test_prune import FRG, QUEEN

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": QUEEN, "rev": "0", "release": FRG[0], "value": "save"})
    row = client.post(f"/prune/{job_id}/decide", data={"artist": QUEEN, "rev": "1", "release": FRG[5], "value": "save"})

    assert row.status_code == 200
    assert '<option value="save" selected>Keep and save this album on Spotify</option>' in row.text
    decisions = client.get(f"/prune/{job_id}/download/decisions.json").json()
    assert decisions["save_releases"] == [FRG[0], FRG[5]]


def _seed_ledger(data_dir: Path, **releases: str) -> None:
    from likearr.prune_ledger import Entry, Ledger, write_ledger

    write_ledger(
        data_dir / "ui" / "prune-ledger.json",
        Ledger(releases={rg: Entry(d, "2026-01-15", "review of 2026-01-15") for rg, d in releases.items()}),
    )


def test_the_review_starts_from_earlier_decisions_and_opens_on_what_needs_one(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG, SMALL

    _seed_ledger(data_dir, **{RG[0]: "keep", RG[1]: "keep", RG[2]: "trash"})
    _login(client)
    job_id = _build_prune(client)

    page = client.get(f"/prune/{job_id}").text

    assert '<option value="needs" selected>Needs a decision</option>' in page
    assert f'id="artist-{BIG}"' not in page  # all kept before: not listed until "All artists"
    small = _card(page, SMALL)
    assert "You trashed this on 15 Jan 2026 - it&#39;s on disk again" in small
    assert 'name="decision" value="undecided" checked' in small  # never pre-filled as trash
    assert "2 albums carried over from earlier reviews. 2 need a decision, under 2 artists" in page
    assert "1 of them trashed before and back on disk" in page
    everyone = client.get(f"/prune/{job_id}/rows", params={"show": "all"}).text
    big = _card(everyone, BIG)
    assert 'name="decision" value="keep" checked' in big and "You kept this on 15 Jan 2026." in big
    draft = json.loads((data_dir / "ui" / "jobs" / job_id / "prune-draft.json").read_text())
    assert draft["artists"] == {BIG: "keep"} and set(draft["past_releases"]) == {RG[0], RG[1], RG[2]}


def test_an_export_is_remembered_by_the_next_review(client: TestClient, data_dir: Path, prune_report: Path) -> None:
    from likearr.prune_ledger import read_ledger
    from tests.web.test_prune import BIG, RG, SMALL

    _login(client)
    first = _build_prune(client)
    client.post(f"/prune/{first}/decide", data={"artist": BIG, "rev": "0", "decision": "save"})
    client.post(f"/prune/{first}/decide", data={"artist": SMALL, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{first}/export", data={"notes": ""})

    ledger = read_ledger(data_dir / "ui" / "prune-ledger.json")
    assert {rg: e.decision for rg, e in ledger.releases.items()} == {RG[0]: "save", RG[1]: "save", RG[2]: "trash"}
    assert ledger.artists[BIG].decision == "save" and ledger.releases[RG[0]].on == "2026-09-23"
    assert ledger.releases[RG[0]].source == f"Clean up {first}"

    second = _build_prune(client)
    page = client.get(f"/prune/{second}/rows", params={"show": "all"}).text
    big = _card(page, BIG)
    # Kept - but the save is not asked of Spotify again unless it is chosen in this review.
    assert 'name="decision" value="keep" checked' in big
    assert "Earlier, on 23 Sep 2026: saved their albums on Spotify." in big
    assert "You kept this and saved it on Spotify on 23 Sep 2026." in big
    assert client.get(f"/prune/{second}/download/decisions.json").json()["save"] == []
    assert "You trashed this on 23 Sep 2026" in _card(page, SMALL)
    # The first review's own export does not make its own albums "decided before" in it.
    assert "carried over" not in client.get(f"/prune/{first}").text


def test_the_seed_can_come_after_the_review_was_opened(client: TestClient, data_dir: Path, prune_report: Path) -> None:
    from tests.web.test_prune import BIG, RG

    _login(client)
    job_id = _build_prune(client)
    assert "carried over" not in client.get(f"/prune/{job_id}").text  # no ledger yet: nothing used up

    _seed_ledger(data_dir, **{RG[0]: "keep", RG[1]: "keep"})
    page = client.get(f"/prune/{job_id}/rows", params={"show": "all"}).text

    assert 'name="decision" value="keep" checked' in _card(page, BIG)
    assert "2 albums carried over" in client.get(f"/prune/{job_id}").text


def test_a_ledger_that_will_not_read_is_reported_and_not_written_over(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG

    ledger = data_dir / "ui" / "prune-ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"releases": {"trunc')
    _login(client)
    job_id = _build_prune(client)

    page = client.get(f"/prune/{job_id}").text
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "keep"})
    exported = client.post(f"/prune/{job_id}/export", data={"notes": ""}, follow_redirects=True)

    assert "Earlier decisions can&#39;t be read." in page or "Earlier decisions can't be read." in page
    assert (data_dir / "ui" / "jobs" / job_id / "decisions.json").is_file()  # the export itself still works
    assert "were not remembered for the next review" in exported.text
    assert ledger.read_text() == '{"releases": {"trunc'


def test_a_review_started_before_the_ledger_keeps_its_choices_and_its_exports_go_stale(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG, SMALL

    _login(client)
    job_id = _build_prune(client)
    job_dir = data_dir / "ui" / "jobs" / job_id
    # An older draft and export: Big Band promoted.
    (job_dir / "prune-draft.json").write_text(json.dumps({"artists": {BIG: "promote"}, "revs": {BIG: 3}}))
    (job_dir / "decisions.json").write_text("{}")
    (job_dir / "review-data.json").write_text("{}")
    _seed_ledger(data_dir, **{RG[0]: "keep", RG[1]: "keep", RG[2]: "keep"})

    page = client.get(f"/prune/{job_id}/rows", params={"show": "all"}).text

    big = _card(page, BIG)
    assert 'name="decision" value="promote" checked' in big  # the job's draft wins
    assert '<option value="keep" selected>' not in big, "nothing filled in under a decided artist"
    assert 'name="decision" value="keep" checked' in _card(page, SMALL)
    assert not (job_dir / "decisions.json").exists(), "the pre-fill changed it: the old export is stale"
    assert "Changed since export - export again." in client.get(f"/prune/{job_id}").text
    stale = client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "0", "decision": "trash"})
    assert stale.status_code == 409  # a tab from before the pre-fill cannot write over it
    assert "Changed in another tab" in stale.text


def test_an_export_while_the_seed_holds_the_ledger_waits_a_bounded_time_off_the_event_loop(
    client: TestClient, data_dir: Path, prune_report: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import likearr.prune_ledger as pl
    from tests.web.test_prune import BIG

    monkeypatch.setattr(pl, "LOCK_TRIES", 3)
    monkeypatch.setattr(pl, "LOCK_PAUSE_S", 0.01)
    waited_on_loop: list[bool] = []
    real_sleep = pl.time.sleep

    def sleep(seconds: float) -> None:
        try:
            asyncio.get_running_loop()
            waited_on_loop.append(True)
        except RuntimeError:
            waited_on_loop.append(False)
        real_sleep(seconds)

    monkeypatch.setattr(pl.time, "sleep", sleep)
    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "keep"})

    with pl.ledger_lock(data_dir / "ui" / "prune-ledger.json"):  # the seed, as far as flock can tell
        exported = client.post(f"/prune/{job_id}/export", data={"notes": ""}, follow_redirects=True)

    assert (data_dir / "ui" / "jobs" / job_id / "decisions.json").is_file()
    assert "another likearr process is writing" in exported.text
    assert waited_on_loop and not any(waited_on_loop), "the wait ran on the event loop"
    assert not (data_dir / "ui" / "prune-ledger.json").exists()


def test_a_carried_album_shows_same_as_artist_and_a_hand_keep_is_a_real_change(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG, RG

    _seed_ledger(data_dir, **{RG[0]: "keep"})  # First kept before; Second is new
    _login(client)
    job_id = _build_prune(client)

    big = _card(client.get(f"/prune/{job_id}/rows", params={"show": "all"}).text, BIG)

    assert '<option value="" selected>Same as artist: not decided yet</option>' in big
    assert "You kept this on 15 Jan 2026." in big
    assert "until you decide" not in big and "kept for now" not in big
    assert '<option value="keep">Keep - no change on Spotify</option>' in big  # not selected: choosing it counts
    # The pre-fill gave Big Band revision 1 (First became carried over).
    kept = client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "release": RG[0], "value": "keep"})
    assert '<option value="keep" selected>Keep - no change on Spotify</option>' in kept.text
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "2", "decision": "trash"})
    assert client.get(f"/prune/{job_id}/download/decisions.json").json()["trash"] == [RG[1]]


def test_an_older_export_reaching_the_ledger_late_never_overwrites_a_newer_one(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from likearr.prune_ledger import read_ledger
    from likearr.web import prune
    from likearr.web.routes.cleanup import _record_in_ledger
    from tests.web.test_prune import BIG, RG

    _login(client)
    job_id = _build_prune(client)
    job_dir = data_dir / "ui" / "jobs" / job_id
    ledger = data_dir / "ui" / "prune-ledger.json"
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    first = prune.read_draft(job_dir / "prune-draft.json")  # what export one recorded from
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "decision": "keep"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    assert read_ledger(ledger).releases[RG[0]].decision == "keep"

    # Export one's worker, late: the draft on disk has moved on, so it writes nothing.
    view = prune.read_report(job_dir / "prune.json")
    assert view is not None
    late = _record_in_ledger(ledger, view, job_dir, first, "2026-09-23", prune.ledger_source(job_id))

    assert "changed while it was exporting" in late
    assert read_ledger(ledger).releases[RG[0]].decision == "keep"


def test_the_net_effect_is_rendered_with_the_card_once_a_decision_is_picked(
    client: TestClient, followed_report: Path
) -> None:
    from tests.web.test_prune import ONE_PROTECTED, QUEEN, TRASH_QUEEN

    _login(client)
    job_id = _build_prune(client)
    assert 'class="note effects"' not in _card(client.get(f"/prune/{job_id}").text, QUEEN)

    trashed = client.post(f"/prune/{job_id}/decide", data={"artist": QUEEN, "rev": "0", "decision": "trash"})
    undone = client.post(f"/prune/{job_id}/decide", data={"artist": QUEEN, "rev": "1", "decision": "undecided"})

    assert trashed.status_code == 200
    card = _card(trashed.text, QUEEN)
    assert 'class="note effects"' in card
    assert TRASH_QUEEN.replace("'", "&#39;") in card and ONE_PROTECTED in card
    assert 'class="note effects"' not in _card(undone.text, QUEEN)


# ---------------------------------------------------------------- always kept, in words


def _aretha_report(data_dir: Path, monkeypatch: pytest.MonkeyPatch, *, song: str, playlist: str) -> str:
    """A report's Aretha Franklin row (string-only `protected_reason`), the playlist named in
    the cache, and the song in the last run's snapshot. Returns the review page's Aretha card."""
    from likearr.shell.last_run import last_run_facts, write_last_run
    from tests.unit.fakes import lidarr_view, snapshot, spotify_album, track_intent
    from tests.unit.test_diff import desired_state
    from tests.web.test_prune import ARETHA, LIVE_LINE, PLAYLIST, RG, TRACK, _protected, _row

    report = {
        "created_at": "2026-01-05T18:02:10+00:00",
        "summary": {},
        "candidates": [_row(ARETHA, "Aretha Franklin", RG[1], "Young, Gifted and Black", 50)],
        "protected": [_protected(RG[0], "Aretha Now", LIVE_LINE)],
    }
    path = data_dir / "fixture-aretha.json"
    path.write_text(json.dumps(report))
    monkeypatch.setenv("FAKE_PRUNE", str(path))
    _cache_names(data_dir, {PLAYLIST: playlist})
    think = track_intent(song, spotify_album("Aretha Now"), spotify_id=TRACK, playlist_id=PLAYLIST)
    facts = last_run_facts(
        ran_at=NOW - timedelta(hours=1),
        snapshot=snapshot(tracks=[think]),
        resolutions={},
        artist_resolutions={},
        desired=desired_state(),
        view=lidarr_view(),
        owned_keys=(),
        collisions=[],
    )
    write_last_run(data_dir / "last-run.json", facts)
    return ARETHA


def test_the_aretha_franklin_album_reads_always_kept_with_names_and_no_ids(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.web.test_prune import PLAYLIST, RESPECT, TRACK

    aretha = _aretha_report(data_dir, monkeypatch, song="Think", playlist="Road trip")
    _login(client)
    job_id = _build_prune(client)

    card = _card(client.get(f"/prune/{job_id}?show=all").text, aretha)

    assert '<span class="pill tone-ok">Always kept</span>' in card
    assert (
        '<span class="kept-why">Only copy of &#34;Think&#34;, a song in your playlist &#34;Road trip&#34;. '
        f'Its album, <a href="https://musicbrainz.org/release-group/{RESPECT}" target="_blank" '
        'rel="noopener noreferrer">Respect</a>, isn&#39;t downloaded yet.</span>'
    ) in card
    assert "1 album · 50 B · 1 always kept" in card
    assert PLAYLIST not in card and TRACK not in card and "liked:" not in card and "playlist:" not in card
    assert RESPECT not in card.replace(f"/release-group/{RESPECT}", "")  # only in the link
    # The row answered after a decision says the same, and so does the net effect.
    decided = client.post(f"/prune/{job_id}/decide", data={"artist": aretha, "rev": "0", "decision": "keep"}).text
    assert "a song in your playlist &#34;Road trip&#34;" in decided
    assert "1 album holds the only copy of a song in your playlists and is always kept." in decided


def test_the_always_kept_line_escapes_names_and_titles(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aretha = _aretha_report(data_dir, monkeypatch, song='Th"ink <b>&', playlist="<script>alert(1)</script>")
    _login(client)
    job_id = _build_prune(client)

    card = _card(client.get(f"/prune/{job_id}?show=all").text, aretha)

    assert "<script>alert(1)</script>" not in card and "<b>" not in card
    assert "&#34;Th&#34;ink &lt;b&gt;&amp;&#34;" in card
    assert "&#34;&lt;script&gt;alert(1)&lt;/script&gt;&#34;" in card


class _CountingWeb:
    """Just what `_prune_words` reads, counted: an empty names file costs a jobs scan per call."""

    def __init__(self) -> None:
        self.names_reads = 0
        self.config_reads = 0

    def playlist_names(self) -> Any:
        from likearr.playlist_names import NamesCache

        self.names_reads += 1
        return NamesCache({"pl-1": "Road trip"})

    def config(self) -> Any:
        from likearr.config import ConfigError

        self.config_reads += 1
        raise ConfigError("no config in this test")

    def last_run(self, _path: Path) -> None:
        raise AssertionError("never reached: the config does not read")


def test_the_names_and_the_last_run_are_read_only_when_a_row_needs_them() -> None:
    from likearr.core.prune import Protection
    from likearr.web.prune import PruneArtist, PruneRelease
    from likearr.web.routes import cleanup as prune_routes

    def artist(*protections: Protection | None) -> PruneArtist:
        releases = tuple(
            PruneRelease(f"rg-{i}", f"T{i}", "Album", "2010", 1, 1, protected_reason="x" if p else "", protection=p)
            for i, p in enumerate(protections)
        )
        return PruneArtist(mbid="a", name="A", releases=releases)

    liked = Protection("pending_album", "liked:t1", song="Think")
    listed = Protection("pending_album", "playlist:pl-1:t2", song="Rock Steady")
    untitled = Protection("pending_album", "liked:t3")

    web = _CountingWeb()
    assert prune_routes._prune_words(cast(Any, web), [artist(None, liked)]) == {"playlist_names": {}, "songs": {}}
    assert prune_routes._prune_words(cast(Any, web), [artist()]) == {"playlist_names": {}, "songs": {}}
    assert (web.names_reads, web.config_reads) == (0, 0)

    assert prune_routes._prune_words(cast(Any, web), [artist(liked, listed)])["playlist_names"] == {"pl-1": "Road trip"}
    assert (web.names_reads, web.config_reads) == (1, 0)
    prune_routes._prune_words(cast(Any, web), [artist(untitled)])  # a song with no title: the last run, not the names
    assert (web.names_reads, web.config_reads) == (1, 1)


# ---------------------------------------------------------------- finish the clean up


@pytest.fixture
def preview_client(data_dir: Path, fake_cli: list[str], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
            auto_preview_prune=True,
        )
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def promote_plan(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from likearr.models import FollowArtist, PromoteSavePlan, Unmatched
    from likearr.shell.promote_save import write_plan

    path = data_dir / "fixture-promote.json"
    plan = PromoteSavePlan(
        created_at=NOW,
        decisions_path="decisions.json",
        decisions_digest="d",
        lidarr_digest="l",
        follow=[FollowArtist(artist_mbid="a", name="Small Band", spotify_id="sp-small", step="artist:name")],
        save=[],
        already_followed=[],
        already_saved=[],
        unmatched=[Unmatched(kind="album", artist_mbid="a", rg_mbid="r", name="Small Band - Only", reason="no match")],
    )
    write_plan(plan, path)
    monkeypatch.setenv("FAKE_PROMOTE_PLAN", str(path))
    return path


def _finished_previews(client: TestClient, job_id: str) -> str:
    """The checklist once no preview is running: it stops refreshing itself."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        body = client.get(f"/prune/{job_id}/finish").text
        if "hx-trigger" not in body:
            return body
        time.sleep(0.05)
    raise AssertionError("the previews did not finish")


_PREVIEW_ORDER = {"prune-preview": 0, "spotify-preview": 1, "prune-checks": 2}


def _preview_jobs(data_dir: Path) -> list[dict[str, Any]]:
    """The preview jobs in chain order: every job here starts at the same fixed NOW, so their ids do
    not sort by time. A second checks job comes after the first (by its meta file's mtime)."""
    paths = sorted((data_dir / "ui" / "jobs").glob("*/meta.json"), key=lambda p: p.stat().st_mtime_ns)
    metas = [json.loads(p.read_text()) for p in paths]
    return sorted((m for m in metas if m["kind"] in _PREVIEW_ORDER), key=lambda m: _PREVIEW_ORDER[m["kind"]])


def _export_for_preview(client: TestClient) -> str:
    from tests.web.test_prune import BIG, SMALL

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "0", "decision": "promote"})
    response = client.post(f"/prune/{job_id}/export", data={"notes": ""}, follow_redirects=False)
    assert response.headers["location"] == f"/prune/{job_id}#finish"
    return job_id


def test_export_previews_the_move_spotify_and_lidarr_read_only(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    client = preview_client
    job_id = _export_for_preview(client)

    body = _finished_previews(client, job_id)
    jobs = _preview_jobs(data_dir)

    assert [m["kind"] for m in jobs] == ["prune-preview", "spotify-preview", "prune-checks"]
    assert all(m["plan_id"] == job_id for m in jobs)
    for m in jobs:
        assert "--apply" not in m["argv"] and "--force" not in m["argv"], m["argv"]
    assert "--no-mount-check" in jobs[0]["argv"]
    assert "7 files, 1.7 KB, from 2 albums. Clean up said 2 albums, 1.7 KB." in body
    assert "Remove Big Band: every file of theirs is staged" in body
    assert "Follow 1 artist, save 0 albums" in body and "Small Band - Only: no match" in body
    assert "No import list adds artists automatically." in body
    assert "Lidarr's command queue is idle." in body


def test_each_preview_starts_only_once_the_step_before_it_has_finished(
    data_dir: Path, fake_cli: list[str], prune_report: Path, promote_plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain's own order: the move, then Spotify, then the Lidarr checks.
    `_preview_jobs` sorts by kind, so this reads the jobs' times, from a clock that moves a second
    a read (job times are to the second)."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    ticks = itertools.count()
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW + timedelta(seconds=next(ticks)),
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
            auto_preview_prune=True,
        )
    )
    with TestClient(app) as client:
        _export_for_preview(client)

        def all_three_finished() -> bool:
            jobs = _preview_jobs(data_dir)
            return len(jobs) == 3 and all(m.get("finished_at") for m in jobs)

        _wait_until(all_three_finished, timeout=30)

    by_kind = {m["kind"]: m for m in _preview_jobs(data_dir)}
    stage, spotify, checks = (by_kind[k] for k in ("prune-preview", "spotify-preview", "prune-checks"))
    assert datetime.fromisoformat(spotify["started_at"]) > datetime.fromisoformat(stage["finished_at"])
    assert datetime.fromisoformat(checks["started_at"]) > datetime.fromisoformat(spotify["finished_at"])


def test_the_checklist_commands_paste_and_run_on_the_documented_install(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    client = preview_client
    job_id = _export_for_preview(client)
    body = _finished_previews(client, job_id)
    job_dir = data_dir / "ui" / "jobs" / job_id
    spotify_dir = data_dir / "ui" / "jobs" / _preview_jobs(data_dir)[1]["id"]
    config = (data_dir / "config.toml").absolute()
    prefix = "docker compose run --rm likearr-cli"
    stage = (
        f"{prefix} prune-stage -c {config} --manifest {job_dir}/prune.json --holding /_likearr-holding "
        f"--decisions {job_dir}/decisions.json"
    )

    assert f"<pre>{stage}</pre>" in body
    assert f"<pre>{stage} --apply</pre>" in body
    assert f"<pre>{prefix} promote-save -c {config} --apply {spotify_dir}/promote-save.json</pre>" in body
    assert "--force" not in body
    assert " rm " not in body and "delete" not in body.lower().replace("nothing is ever deleted", "").replace(
        "nothing is deleted", ""
    )
    assert body.count("--apply") == 3  # the move, the page's Spotify plan, a fresh one


def test_a_holding_dir_and_a_command_prefix_come_from_the_config(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    config = data_dir / "config.toml"
    text = config.read_text().replace("[ui]\n", '[ui]\ncli_command = "ssh nas docker exec likearr likearr"\n')
    config.write_text(text.replace("[prune]\n", '[prune]\nholding_dir = "/srv/hold ing"\n'))
    client = preview_client
    job_id = _export_for_preview(client)
    body = _finished_previews(client, job_id)

    assert "ssh nas docker exec likearr likearr prune-stage" in body
    assert "--holding &#39;/srv/hold ing&#39;" in body  # quoted, and escaped for the page
    assert "--holding" in _preview_jobs(data_dir)[0]["argv"]
    assert "/srv/hold ing" in _preview_jobs(data_dir)[0]["argv"]


def test_a_preview_that_refuses_says_so_and_links_its_output(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "FAKE_STAGE_FAIL", "the holding directory /music/_likearr-holding is inside the Lidarr root folder"
    )
    client = preview_client
    job_id = _export_for_preview(client)
    body = _finished_previews(client, job_id)
    stage_id = _preview_jobs(data_dir)[0]["id"]

    assert f'Previewing the move didn\'t finish (failed). <a href="/jobs/{stage_id}">See why</a>' in body
    assert "inside the Lidarr root folder" in client.get(f"/jobs/{stage_id}").text
    assert [m["kind"] for m in _preview_jobs(data_dir)] == ["prune-preview", "spotify-preview", "prune-checks"]


def test_the_lidarr_checks_warn_and_link_the_import_lists(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path, monkeypatch
) -> None:
    checks = {
        "version": 1,
        "checked_at": "2026-09-23T18:00:00+00:00",
        "import_lists": [{"id": 3, "name": "Last.fm <top>", "auto_add": True}],
        "queue": [{"name": "RescanFolders", "status": "started"}],
        "errors": {},
    }
    monkeypatch.setenv("FAKE_CHECKS", json.dumps(checks))
    client = preview_client
    job_id = _export_for_preview(client)
    body = _finished_previews(client, job_id)

    assert "Turn off automatic add on Last.fm &lt;top&gt;" in body
    assert 'href="http://lidarr:8686/settings/importlists"' in body
    assert "Lidarr is busy: 1 command queued or running (RescanFolders)" in body

    again = client.post(f"/prune/{job_id}/checks", follow_redirects=False)
    assert again.status_code == 303
    _finished_previews(client, job_id)
    assert [m["kind"] for m in _preview_jobs(data_dir)][-1] == "prune-checks"


def test_a_token_without_the_write_scopes_gets_the_auth_command(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    token = data_dir / "spotify-token.json"
    token.write_text(json.dumps({**json.loads(token.read_text()), "scope": "user-follow-read user-library-read"}))
    client = preview_client
    job_id = _export_for_preview(client)
    body = _finished_previews(client, job_id)

    assert "missing user-follow-modify, user-library-modify" in body
    assert (
        f"docker compose run --rm likearr-cli auth -c {(data_dir / 'config.toml').absolute()} --manual --promote-save"
        in body
    )
    assert "tok-SENTINEL" not in body and "ref-SENTINEL" not in body
    # Paste-back mode: the command is the way; there is no one-click button to offer.
    assert 'action="/settings/spotify/connect"' not in body


def test_in_callback_mode_a_token_without_the_write_scopes_also_gets_a_button(
    data_dir: Path, fake_cli: list[str], prune_report: Path, promote_plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `[ui] public_url` set, the missing-write-scope hint also offers a button that
    starts Settings' connect flow with promote-save's write scopes."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    (data_dir / "config.toml").write_text(CONFIG.replace(*_PUBLIC_URL))
    token = data_dir / "spotify-token.json"
    token.write_text(json.dumps({**json.loads(token.read_text()), "scope": "user-follow-read user-library-read"}))
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
            auto_preview_prune=True,
        )
    )
    with TestClient(app) as client:
        job_id = _export_for_preview(client)
        body = _finished_previews(client, job_id)

    assert "--manual --promote-save" in body
    form = body.split('<form method="post" action="/settings/spotify/connect"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="promote_save" value="1">' in form
    assert "write access" in form


@pytest.mark.parametrize(
    ("base_url", "form_action"),
    [
        ("https://likearr.example.org", "form-action 'self' https://accounts.spotify.com https://likearr.example.org;"),
        ("http://testserver", "form-action 'self';"),
    ],
    ids=["at-public-url", "elsewhere"],
)
def test_the_clean_up_page_allows_one_click_write_access_only_at_the_public_url(
    data_dir: Path,
    fake_cli: list[str],
    prune_report: Path,
    promote_plan: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    form_action: str,
) -> None:
    """The page holding "Authorize write access on Spotify" is the document whose form-action
    the browser checks on each hop of that POST's redirect, so it widens exactly as Settings does:
    at the `public_url` origin only."""
    monkeypatch.setenv("LIKEARR_LIDARR_API_KEY", API_KEY_SENTINEL)
    (data_dir / "config.toml").write_text(CONFIG.replace(*_PUBLIC_URL))
    app = create_app(
        WebSettings(
            config_path=data_dir / "config.toml",
            password=PASSWORD,
            cli=fake_cli,
            now=lambda: NOW,
            limiter=LoginLimiter(),
            shutdown_timeout_s=5,
            auto_preview_prune=True,
        )
    )
    with TestClient(app, base_url=base_url) as client:
        job_id = _export_for_preview(client)
        _finished_previews(client, job_id)
        page = client.get(f"/prune/{job_id}")

    assert page.status_code == 200
    assert f"; {form_action} " in page.headers["content-security-policy"]


def test_a_preview_is_shown_only_beside_the_export_it_previewed(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    from tests.web.test_prune import SMALL

    client = preview_client
    job_id = _export_for_preview(client)
    _finished_previews(client, job_id)

    decided = client.post(f"/prune/{job_id}/decide", data={"artist": SMALL, "rev": "1", "decision": "keep"})
    assert '<div id="finish-body" hx-swap-oob="true"></div>' in decided.text
    assert "7 files" not in client.get(f"/prune/{job_id}").text
    assert client.post(f"/prune/{job_id}/preview").status_code == 409  # nothing exported to preview

    # A hand-edited decisions file is a different export: the old preview is not shown for it.
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    _finished_previews(client, job_id)
    job_dir = data_dir / "ui" / "jobs" / job_id
    (job_dir / "decisions.json").write_text((job_dir / "decisions.json").read_text().replace("[]", "[ ]", 1))
    assert "7 files" not in client.get(f"/prune/{job_id}/finish").text


def test_nothing_to_do_on_spotify_skips_its_preview(
    preview_client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    from tests.web.test_prune import BIG

    client = preview_client
    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    body = _finished_previews(client, job_id)

    assert [m["kind"] for m in _preview_jobs(data_dir)] == ["prune-preview", "prune-checks"]
    assert "Nothing to do: this export follows and saves nothing." in body
    assert "promote-save" not in body


def test_preview_again_while_a_preview_runs_keeps_the_running_chain(
    preview_client: TestClient, data_dir: Path, prune_report: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_STAGE_SLEEP", "1")
    client = preview_client
    from tests.web.test_prune import BIG

    _login(client)
    job_id = _build_prune(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "0", "decision": "trash"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})
    binding = json.loads((data_dir / "ui" / "jobs" / job_id / "previews.json").read_text())

    again = client.post(f"/prune/{job_id}/preview", follow_redirects=True)

    assert "The previews didn&#39;t start: another job is already running" in again.text
    assert json.loads((data_dir / "ui" / "jobs" / job_id / "previews.json").read_text()) == binding
    assert "7 files" in _finished_previews(client, job_id)


# ---------------------------------------------------------------- each binding check is load-bearing


def _spotify_plan_dir(data_dir: Path) -> Path:
    [spotify] = [m for m in _preview_jobs(data_dir) if m["kind"] == "spotify-preview"]
    return data_dir / "ui" / "jobs" / spotify["id"]


def test_a_stale_spotify_preview_and_its_apply_command_are_hidden_after_the_export_changes(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    """The page's binding check: the Spotify preview has no sha of its own to catch a changed export."""
    client = preview_client
    job_id = _export_for_preview(client)
    before = _finished_previews(client, job_id)
    plan = _spotify_plan_dir(data_dir) / "promote-save.json"
    assert "Follow 1 artist" in before and f"--apply {plan}" in before

    decisions = data_dir / "ui" / "jobs" / job_id / "decisions.json"
    decisions.write_text(decisions.read_text().replace("[]", "[ ]", 1))  # an export the previews never saw
    after = client.get(f"/prune/{job_id}/finish").text

    assert "Follow 1 artist" not in after
    assert f"--apply {plan}" not in after


def test_the_move_preview_is_shown_only_for_the_decisions_it_read(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    """The stage summary's own sha check: a binding that matches is not enough on its own."""
    client = preview_client
    job_id = _export_for_preview(client)
    assert "7 files" in _finished_previews(client, job_id)
    [stage] = [m for m in _preview_jobs(data_dir) if m["kind"] == "prune-preview"]
    summary = data_dir / "ui" / "jobs" / stage["id"] / "stage.json"
    summary.write_text(summary.read_text().replace('"decisions_sha256": "', '"decisions_sha256": "0', 1))

    assert "7 files" not in client.get(f"/prune/{job_id}/finish").text


def test_a_chain_stops_when_the_export_changes_mid_chain(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path, monkeypatch
) -> None:
    """The chained step's sha check: a step never starts for an export the chain did not begin with."""
    from tests.web.test_prune import BIG

    monkeypatch.setenv("FAKE_STAGE_SLEEP", "1")
    client = preview_client
    job_id = _export_for_preview(client)
    client.post(f"/prune/{job_id}/decide", data={"artist": BIG, "rev": "1", "decision": "keep"})
    client.post(f"/prune/{job_id}/export", data={"notes": ""})  # refused a new chain: the slot is busy
    _finished_previews(client, job_id)
    deadline = time.monotonic() + 10
    while _web_of(client).runner.current() is not None and time.monotonic() < deadline:
        time.sleep(0.05)

    assert [m["kind"] for m in _preview_jobs(data_dir)] == ["prune-preview"]


def test_a_job_from_another_chain_cannot_advance_this_export_s_chain(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    """The chain ownership check: only a job the binding names moves the chain on."""
    from likearr.web import cleanup
    from likearr.web.routes import cleanup as prune_routes

    client = preview_client
    job_id = _export_for_preview(client)
    _finished_previews(client, job_id)
    web = _web_of(client)
    job_dir = data_dir / "ui" / "jobs" / job_id
    [old_stage] = [m for m in _preview_jobs(data_dir) if m["kind"] == "prune-preview"]
    digest = cleanup.decisions_digest(job_dir / "decisions.json")
    assert digest is not None
    cleanup.write_binding(job_dir, cleanup.Binding(digest, stage="a-newer-chain", asks_spotify=True))
    before = len(_preview_jobs(data_dir))

    prune_routes._after_preview(web, web.runner.get(old_stage["id"]))

    assert len(_preview_jobs(data_dir)) == before
    assert cleanup.read_binding(job_dir) == cleanup.Binding(digest, stage="a-newer-chain", asks_spotify=True)


def test_check_again_between_two_steps_delays_the_chain_rather_than_ending_it(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    from likearr.web import cleanup

    client = preview_client
    job_id = _export_for_preview(client)
    _finished_previews(client, job_id)
    web = _web_of(client)
    job_dir = data_dir / "ui" / "jobs" / job_id
    binding = cleanup.read_binding(job_dir)
    assert binding is not None
    # As if "Check again" took the slot just as the move preview finished: Spotify is still to come.
    binding.spotify = ""
    cleanup.write_binding(job_dir, binding)

    assert client.post(f"/prune/{job_id}/checks", follow_redirects=False).status_code == 303
    _finished_previews(client, job_id)
    deadline = time.monotonic() + 10
    while web.runner.current() is not None and time.monotonic() < deadline:
        time.sleep(0.05)

    kinds = [m["kind"] for m in _preview_jobs(data_dir)]
    assert kinds.count("spotify-preview") == 2, kinds
    assert "Follow 1 artist" in client.get(f"/prune/{job_id}/finish").text


def test_the_page_says_where_the_command_prefix_comes_from(
    preview_client: TestClient, data_dir: Path, prune_report: Path, promote_plan: Path
) -> None:
    client = preview_client
    job_id = _export_for_preview(client)

    body = _finished_previews(client, job_id)

    assert (
        "Each command starts with <code>docker compose run --rm likearr-cli</code>, your <code>[ui] cli_command</code>"
        in body
    )


# ---------------------------------------------------------------- Clean up off


def test_with_clean_up_off_the_nav_has_no_clean_up_link(client: TestClient, data_dir: Path) -> None:
    _login(client)
    assert 'href="/prune"' in client.get("/").text

    _clean_up_off(data_dir)

    assert 'href="/prune"' not in client.get("/").text
    assert 'href="/prune"' not in client.get("/settings").text


def test_with_clean_up_off_the_prune_page_says_how_to_turn_it_on_and_starts_nothing(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    _clean_up_off(data_dir)
    _login(client)

    page = client.get("/prune")
    head = client.head("/prune")
    started = client.post("/prune", follow_redirects=False)

    assert page.status_code == 200
    assert head.status_code == 200  # Starlette's HEAD for the GET route answers the same
    assert "<h1>Clean up is off</h1>" in page.text
    assert '<a href="/settings#advanced">Settings &gt; Advanced</a>' in page.text
    assert "likearr-cli" in page.text
    assert "Find unneeded albums" not in page.text
    assert started.status_code == 404
    assert "<h1>Clean up is off</h1>" in started.text
    assert not (data_dir / "ui" / "jobs").exists() or not any((data_dir / "ui" / "jobs").iterdir())


def test_with_clean_up_off_every_other_prune_route_is_not_there(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    """A review started while it was on: its routes answer 404 once it is off, and nothing in its
    job directory or the ledger changes."""
    _login(client)
    job_id = _build_prune(client)
    job_dir = data_dir / "ui" / "jobs" / job_id
    before = {p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()}
    _clean_up_off(data_dir)

    for method, path in [
        ("GET", f"/prune/{job_id}"),
        ("GET", f"/prune/{job_id}/rows"),
        ("POST", f"/prune/{job_id}/decide"),
        ("POST", f"/prune/{job_id}/export"),
        ("GET", f"/prune/{job_id}/download/decisions.json"),
        ("POST", f"/prune/{job_id}/preview"),
        ("POST", f"/prune/{job_id}/checks"),
        ("GET", f"/prune/{job_id}/finish"),
    ]:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 404, (method, path)
        assert "Clean up is off" in response.text, (method, path)

    assert {p.name: p.read_bytes() for p in job_dir.iterdir() if p.is_file()} == before
    assert not (data_dir / "ui" / "prune-ledger.json").exists()


def test_with_clean_up_off_a_finished_search_opens_its_plain_job_page(
    client: TestClient, data_dir: Path, prune_report: Path
) -> None:
    _login(client)
    job_id = _build_prune(client)
    _clean_up_off(data_dir)

    page = client.get(f"/jobs/{job_id}", follow_redirects=False)
    fragment = client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert "HX-Redirect" not in fragment.headers
    assert f"/prune/{job_id}" not in page.text
