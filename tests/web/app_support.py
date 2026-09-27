"""What the web app's end-to-end tests share: the fixture config, the fake CLI every job runs
as, and the helpers more than one test file calls. The fixtures themselves are in `conftest.py`.

Split out of `test_app.py` (#154).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from likearr.models import HealthRecord, RunStatus
from likearr.web.app import WebSettings, create_app

PASSWORD = "correct horse battery staple"
API_KEY_SENTINEL = "lidarr-key-SENTINEL-0123456789"

CONFIG = """\
# likearr - fixture config for the web tests.

[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "spotify-token.json"
playlists = ["pl-owned", "pl-gone"]

[state]
db = "state.sqlite"
lock_file = "likearr.lock"

[rules]
liked_track_scope = "album"  # keep

[schedule]
cron = "20 */6 * * *"
timezone = "America/New_York"

[prune]
enabled = true  # Clean up is off by default (#148); its suites run with it on

[ui]
"""


FAKE_CLI = r"""
import json, os, sys, time
args = sys.argv[1:]
assert args[0] == "-c", args
args = args[2:]
cmd = args[0]
if os.environ.get("FAKE_CONFIG_ERROR"):  # `shell/cli.py`'s own top-level `except ConfigError`:
    # printed to stdout before any command-specific code runs, for any subcommand, with no run
    # record - config.toml never loaded far enough to build one.
    print(f"config error: {os.environ['FAKE_CONFIG_ERROR']}")
    sys.exit(1)
if cmd == "explain":
    query = args[args.index("--") + 1]
    print("sources read: followed_artists=1", file=sys.stderr, flush=True)
    time.sleep(float(os.environ.get("FAKE_EXPLAIN_SLEEP", "0")))
    if os.environ.get("FAKE_EXPLAIN_OOM"):  # what the kernel's OOM killer does to a child
        import signal
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get("FAKE_EXPLAIN_ERROR"):  # a logged ERROR line, no FAIL line and no run record
        print(f"2026-01-01T00:00:00+00:00 ERROR likearr.shell.run: {os.environ['FAKE_EXPLAIN_ERROR']}",
              file=sys.stderr, flush=True)
        sys.exit(1)
    if os.environ.get("FAKE_EXPLAIN_FAIL"):
        print("FAIL  spotify: token expired")
        sys.exit(1)
    answer = f"explain: {query!r} is monitored because you like it"
    if "--json" not in args or os.environ.get("FAKE_EXPLAIN_PLAIN"):  # output with no report in it
        print(answer)
        sys.exit(0)
    link = os.environ.get("FAKE_EXPLAIN_LINK", "https://musicbrainz.org/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da")
    detail = f"{query}\n  why: you liked it [liked:abc]\n"
    print(json.dumps({"query": query, "summary": [
        {"text": answer, "wrong_match": False, "links": [{"label": "the artist on MusicBrainz", "url": link}],
         "headline": answer, "status": "monitored", "facts": [], "detail": detail},
        {"text": "This looks like a wrong match.", "wrong_match": True, "links": [],
         "headline": "This looks like a wrong match.", "status": "monitored", "facts": [], "detail": detail},
    ], "details": detail + "\n" + detail, "left_out": 0}))
elif cmd == "prune-report":
    import shutil
    shutil.copy(os.environ["FAKE_PRUNE"], args[args.index("--out") + 1])
    print("likearr prune report: 4 candidates")
elif cmd == "run" and "--apply" in args:
    time.sleep(float(os.environ.get("FAKE_APPLY_SLEEP", "0")))
    code = int(os.environ.get("FAKE_APPLY_EXIT", "0"))
    status = {0: "ok", 2: "guarded", 3: "stale"}.get(code, "error")
    message = os.environ.get("FAKE_APPLY_MESSAGE", "applied")
    print("likearr applied: 1 monitored")
    if not os.environ.get("FAKE_APPLY_NO_RECORD"):  # [health] stdout = false prints no record
        # A subset of the real health record's keys (`HealthRecord.to_dict`), like the stdout sink
        # prints - enough for the job page to detect and split it off by its keys, not its text.
        record = {"ts": 0, "resolver_version": 9, "status": status, "message": message,
                  "dry_run": False, "exit_code": code}
        if "FAKE_APPLY_SPOTIFY_OK" in os.environ:
            record["spotify_ok"] = os.environ["FAKE_APPLY_SPOTIFY_OK"] == "1"
        if "FAKE_APPLY_LIDARR_OK" in os.environ:
            record["lidarr_ok"] = os.environ["FAKE_APPLY_LIDARR_OK"] == "1"
        print(json.dumps(record))
    sys.exit(code)
elif cmd == "run":
    import shutil
    out = args[args.index("--out") + 1]
    time.sleep(float(os.environ.get("FAKE_RUN_SLEEP", "0")))
    exit_code = int(os.environ.get("FAKE_RUN_EXIT", "0"))
    if exit_code == 0:
        shutil.copy(os.environ["FAKE_DIFF"], out)
        print("likearr plan: 1 to monitor" + (" (shrinks accepted)" if "--accept-shrink" in args else ""))
    else:
        message = os.environ.get("FAKE_RUN_MESSAGE", "")
        if message:
            record = {"ts": 0, "resolver_version": 9, "status": "error", "message": message,
                      "dry_run": True, "exit_code": exit_code}
            if "FAKE_RUN_SPOTIFY_OK" in os.environ:
                record["spotify_ok"] = os.environ["FAKE_RUN_SPOTIFY_OK"] == "1"
            if "FAKE_RUN_LIDARR_OK" in os.environ:
                record["lidarr_ok"] = os.environ["FAKE_RUN_LIDARR_OK"] == "1"
            print(json.dumps(record))
    sys.exit(exit_code)
elif cmd == "playlists":
    time.sleep(float(os.environ.get("FAKE_PLAYLISTS_SLEEP", "0")))
    if os.environ.get("FAKE_PLAYLISTS_FAIL"):
        print("FAIL  spotify: quota exceeded")
        sys.exit(1)
    road = os.environ.get("FAKE_PLAYLISTS_NAME", "Road trip")
    playlists = [{"id": "pl-owned", "name": road, "track_count": 40, "owned": True, "readable": True},
                 {"id": "pl-other", "name": "Gym", "track_count": 12, "owned": True, "readable": True}]
    if os.environ.get("FAKE_PLAYLISTS_UNOWNED"):
        playlists.append({"id": "pl-discover", "name": "Discover Weekly", "track_count": 30, "owned": False,
                          "readable": False})
    if os.environ.get("FAKE_PLAYLISTS_COLLAB"):
        playlists.append({"id": "pl-band", "name": "Band Van", "track_count": 8, "owned": False,
                          "readable": True, "needs_reauth": False})
        playlists.append({"id": "pl-oldshare", "name": "Old Share", "track_count": 5, "owned": False,
                          "readable": False, "needs_reauth": True})
    print(json.dumps({"playlists": playlists, "configured": ["pl-owned", "pl-gone"], "missing": ["pl-gone"]}))
elif cmd == "lidarr-files":
    time.sleep(float(os.environ.get("FAKE_FILES_SLEEP", "0")))
    if os.environ.get("FAKE_FILES_FAIL"):
        print("FAIL  lidarr GET /artist: connection refused")
        sys.exit(1)
    json.load(open(args[args.index("--plan") + 1]))  # the plan it was given reads
    answer = {"albums": {"rg2": {"track_files": 12, "size_on_disk": 99}}, "missing": []}
    with open(args[args.index("--out") + 1], "w") as f:
        f.write(json.dumps(answer))
    print("likearr lidarr-files: 1 release(s) to unmonitor, 1 with files on disk")
elif cmd == "prune-stage":
    import hashlib
    time.sleep(float(os.environ.get("FAKE_STAGE_SLEEP", "0")))
    if os.environ.get("FAKE_STAGE_FAIL"):
        print("refused: " + os.environ["FAKE_STAGE_FAIL"])
        sys.exit(1)
    decisions = open(args[args.index("--decisions") + 1], "rb").read()
    summary = {"version": 1, "decisions_sha256": hashlib.sha256(decisions).hexdigest(),
               "holding": args[args.index("--holding") + 1], "files": 7, "bytes": 1700, "albums": 2,
               "remove": [{"name": "Big Band", "why": "every file of theirs is staged"}],
               "rescan": [{"name": "Guarded", "why": "a protected release stays"}],
               "mount_checked": "--no-mount-check" not in args}
    with open(args[args.index("--out") + 1], "w") as f:
        f.write(json.dumps(summary))
    print("7 files, 1.7 KiB, from 2 albums")
elif cmd == "promote-save":
    import shutil
    if os.environ.get("FAKE_PROMOTE_FAIL"):
        print("refused: " + os.environ["FAKE_PROMOTE_FAIL"])
        sys.exit(1)
    shutil.copy(os.environ["FAKE_PROMOTE_PLAN"], args[args.index("--out") + 1])
    print("promote-save plan: follow 1, save 0")
elif cmd == "prune-checks":
    default = {"version": 1, "checked_at": "2026-09-23T18:00:00+00:00", "import_lists": [], "queue": [], "errors": {}}
    with open(args[args.index("--out") + 1], "w") as f:
        f.write(os.environ.get("FAKE_CHECKS") or json.dumps(default))
    print("ok    Lidarr's command queue is idle")
elif cmd == "doctor":
    time.sleep(float(os.environ.get("FAKE_DOCTOR_SLEEP", "0")))
    default = {"checks": [{"level": "PASS", "name": "lidarr", "detail": "reachable"}],
               "summary": {"total": 1, "failed": 0, "warnings": 0, "skipped": 0}}
    print(os.environ.get("FAKE_DOCTOR") or json.dumps(default))
    sys.exit(int(os.environ.get("FAKE_DOCTOR_EXIT", "0")))
elif cmd == "setup-profiles":
    default = {"profiles": [{"name": "Lean", "kind": "lean", "status": "missing"},
                             {"name": "Full", "kind": "full", "status": "missing"}],
               "tag": {"name": "likearr", "status": "missing"},
               "root_folder": {"path": "/music", "status": "missing"},
               "todo": ["create metadata profile 'Lean' (lean)", "create metadata profile 'Full' (full)",
                        "create tag 'likearr'", "create root folder '/music' (monitor none / new items none)"],
               "needs_apply": True}
    time.sleep(float(os.environ.get("FAKE_SETUP_SLEEP", "0")))
    if "--apply" in args:
        print("ok    metadata profile 'Lean' -> id 1")
        sys.exit(int(os.environ.get("FAKE_SETUP_APPLY_EXIT", "0")))
    print(os.environ.get("FAKE_SETUP_PROFILES") or json.dumps(default))
    sys.exit(int(os.environ.get("FAKE_SETUP_PROFILES_EXIT", "0")))
"""


NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)


def _record(**overrides: object) -> HealthRecord:
    defaults: dict[str, object] = dict(
        ts=int((NOW - timedelta(hours=3)).timestamp()),
        version="0.1.0",
        resolver_version=9,
        exit_code=0,
        status=RunStatus.OK,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={"followed_artists": 250, "liked_tracks": 2500, "playlist:pl-owned": 40, "monitored": 4},
        unmapped=0,
        pending_album=0,
        dry_run=False,
        baseline="compared",
    )
    defaults.update(overrides)
    return HealthRecord(**defaults)  # type: ignore[arg-type]


def _login(client: TestClient) -> None:
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def _clean_up_off(data_dir: Path) -> None:
    """Turn Clean up off in the fixture config.toml (#148): it ships with `[prune] enabled = true`
    so Clean up's own suites run as before. Read fresh on every request, like `_enable_mqtt`."""
    config = data_dir / "config.toml"
    text = config.read_text()
    assert "enabled = true  # Clean up" in text
    config.write_text(text.replace("enabled = true  # Clean up", "enabled = false  # Clean up"))


def _enable_mqtt(data_dir: Path) -> None:
    """Append `[health.mqtt]` to the fixture config.toml: `_Web.config()` reads the file fresh on
    every request (see `context.py`), so this takes effect for requests made after it runs, with no
    need to rebuild the `client` fixture."""
    with (data_dir / "config.toml").open("a") as f:
        f.write('\n[health.mqtt]\nhost = "homeassistant.local"\ntopic = "likearr/health"\n')


def _enable_webhook(data_dir: Path) -> None:
    with (data_dir / "config.toml").open("a") as f:
        f.write('\n[health.webhook]\nurl = "https://example.invalid/hook"\n')


def _wait_for_job(client: TestClient, job_id: str) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}/fragment", headers={"HX-Request": "true"})
        if response.status_code == 286:
            return response.text
        assert response.status_code == 200
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _file_hash(client: TestClient) -> str:
    return re.search(r'name="file_hash" value="([^"]+)"', client.get("/settings").text)[1]  # type: ignore[index]


FIRST_APPLY_LINE = "Scheduled runs start after your first reviewed apply"


def _forget_first_apply(data_dir: Path) -> None:
    """The fixture's install, as if it had never applied by hand."""
    conn = sqlite3.connect(data_dir / "state.sqlite")
    conn.execute("DELETE FROM first_apply")
    conn.commit()
    conn.close()


def _settings_form(page: str) -> dict[str, list[str]]:
    """The form as a browser would submit it: every input's value, checked boxes only."""
    form: dict[str, list[str]] = {}
    for tag in re.findall(r"<input[^>]*>", page):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        if 'type="checkbox"' in tag and "checked" not in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        form.setdefault(name[1], []).append(value[1] if value else "on")
    for name, body in re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', page, re.S):
        form[name] = [body]
    for name, body in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        form[name] = [re.search(r'<option value="([^"]+)" selected', body)[1]]  # type: ignore[index]
    return form


def _cache_names(data_dir: Path, names: dict[str, str], when: str = "2026-09-20T12:00:00+00:00") -> Path:
    path = data_dir / "ui" / "playlist-names.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": when, "names": names}))
    return path


def _app(data_dir: Path, fake_cli: list[str], **overrides: Any) -> Any:
    settings: dict[str, Any] = dict(
        config_path=data_dir / "config.toml",
        password=PASSWORD,
        cli=fake_cli,
        now=lambda: NOW,
        shutdown_timeout_s=5,
    )
    settings.update(overrides)
    return create_app(WebSettings(**settings))  # type: ignore[arg-type]


def _wait_for_picker(client: TestClient, response_text: str, params: dict[str, list[str]] | None = None) -> str:
    match = re.search(r'hx-get="/settings/playlists/([^"]+)"', response_text)
    if match is None:
        return response_text
    deadline = time.monotonic() + 20
    while (final := client.get(f"/settings/playlists/{match[1]}", params=params)).status_code != 286:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return final.text


def _jobs(data_dir: Path) -> list[Any]:
    from likearr.web.jobs import JobMeta

    root = data_dir / "ui" / "jobs"
    return (
        [JobMeta.from_mapping(json.loads((d / "meta.json").read_text())) for d in root.iterdir()]
        if root.exists()
        else []
    )


def _jobs_of(data_dir: Path, kind: str) -> list[Any]:
    return [m for m in _jobs(data_dir) if m.kind == kind]


def _wait_until(predicate: Any, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _names_done(data_dir: Path) -> bool:
    jobs = _jobs_of(data_dir, "playlists")
    return bool(jobs) and all(m.state != "running" for m in jobs)


_PUBLIC_URL = ("[ui]\n", '[ui]\npublic_url = "https://likearr.example.org"\n')


def _build_prune(client: TestClient) -> str:
    response = client.post("/prune", follow_redirects=False)
    assert response.status_code == 303, response.text
    job_id = response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, job_id)
    return job_id


def _web_of(client: TestClient) -> Any:
    app: Any = client.app
    while not hasattr(app, "state"):
        app = app.app
    return app.state.web


def _start_plan(client: TestClient, **form: str) -> str:
    response = client.post("/plan", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    job_id = response.headers["location"].removeprefix("/jobs/")
    _wait_for_job(client, job_id)
    return job_id


def _apply_form(client: TestClient, job_id: str) -> dict[str, str]:
    page = client.get(f"/plan/{job_id}/apply").text
    token = re.search(r'name="plan_token" value="([0-9a-f]+)"', page)[1]  # type: ignore[index]
    return {"plan_token": token}


DENIABLE = "0f0f0f0f-1111-2222-3333-444444444444"


def _plan_monitoring(planned_diff: Path, rg: str = DENIABLE, *, kinds: tuple[str, ...] = ("liked",)) -> None:
    """Put a monitor row for `rg` first in the planned diff, wanted for each reason kind in `kinds`."""
    from likearr.models import MonitorRelease, Reason, ReasonKind, ReleaseKey
    from likearr.shell.diff_io import read_diff, write_diff

    diff = read_diff(planned_diff)
    reasons = frozenset(Reason(ReasonKind(kind), f"t-real-{i}") for i, kind in enumerate(kinds))
    diff.monitor.insert(0, MonitorRelease(ReleaseKey("a1", rg), "A Real One", reasons, ""))
    write_diff(diff, planned_diff)
