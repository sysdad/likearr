"""The app's route table, pinned: splitting `likearr/web/app.py` into route modules must not
add, drop, reorder or rewire a single route."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount, Route

from likearr.web.app import WebSettings, create_app

CONFIG = """\
[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "spotify-token.json"
playlists = ["pl-owned"]

[state]
db = "state.sqlite"
lock_file = "likearr.lock"
"""

ROUTES = [
    ("/healthz", ("GET", "HEAD"), "healthz"),
    ("/favicon.ico", ("GET", "HEAD"), "favicon_ico"),
    ("/login", ("GET", "HEAD"), "login_form"),
    ("/login", ("POST",), "login"),
    ("/logout", ("POST",), "logout"),
    ("/", ("GET", "HEAD"), "status"),
    ("/run-now", ("POST",), "run_now"),
    ("/runs/{run_id}", ("GET", "HEAD"), "run_page"),
    ("/unmatched", ("GET", "HEAD"), "unmatched"),
    ("/unmatched/rows", ("GET", "HEAD"), "unmatched_rows"),
    ("/unmatched/part", ("GET", "HEAD"), "unmatched_part"),
    ("/explain", ("GET", "HEAD"), "explain_form"),
    ("/explain", ("POST",), "explain_start"),
    ("/explain/deny", ("POST",), "explain_deny"),
    ("/settings", ("GET", "HEAD"), "settings_page"),
    ("/settings", ("POST",), "settings_save"),
    ("/settings/pause", ("POST",), "settings_pause"),
    ("/settings/resume", ("POST",), "settings_resume"),
    ("/settings/cleanup", ("POST",), "settings_cleanup"),
    ("/settings/manage-monitored", ("POST",), "settings_manage_monitored"),
    ("/settings/schedule", ("POST",), "settings_schedule"),
    ("/settings/schedule/preview", ("GET", "HEAD"), "settings_schedule_preview"),
    ("/settings/playlists", ("POST",), "playlists_refresh"),
    ("/settings/playlists/{job_id}", ("GET", "HEAD"), "playlists_poll"),
    ("/settings/spotify/connect", ("POST",), "spotify_connect_start"),
    ("/settings/spotify/finish", ("POST",), "spotify_connect_finish"),
    ("/settings/spotify/switch", ("POST",), "spotify_switch"),
    ("/spotify/callback", ("GET", "HEAD"), "spotify_callback"),
    ("/settings/lidarr-setup/preview", ("POST",), "lidarr_setup_preview_start"),
    ("/settings/lidarr-library", ("POST",), "lidarr_library"),
    ("/settings/lidarr-setup/{job_id}", ("GET", "HEAD"), "lidarr_setup_poll"),
    ("/settings/lidarr-setup/{job_id}/apply", ("POST",), "lidarr_setup_apply"),
    ("/doctor", ("GET", "HEAD"), "doctor_redirect"),
    ("/doctor", ("POST",), "doctor_start"),
    ("/settings/doctor/{job_id}", ("GET", "HEAD"), "doctor_poll"),
    ("/prune", ("GET", "HEAD"), "prune_page"),
    ("/prune", ("POST",), "prune_start"),
    ("/prune/{job_id}", ("GET", "HEAD"), "prune_review"),
    ("/prune/{job_id}/rows", ("GET", "HEAD"), "prune_rows"),
    ("/prune/{job_id}/decide", ("POST",), "prune_decide"),
    ("/prune/{job_id}/export", ("POST",), "prune_export"),
    ("/prune/{job_id}/download/{name}", ("GET", "HEAD"), "prune_download"),
    ("/prune/{job_id}/preview", ("POST",), "prune_preview"),
    ("/prune/{job_id}/checks", ("POST",), "prune_checks"),
    ("/prune/{job_id}/finish", ("GET", "HEAD"), "prune_finish"),
    ("/plan", ("GET", "HEAD"), "plan_page"),
    ("/plan", ("POST",), "plan_start"),
    ("/plan/{job_id}", ("GET", "HEAD"), "plan_review"),
    ("/plan/{job_id}/section/{name}", ("GET", "HEAD"), "plan_section"),
    ("/plan/{job_id}/apply", ("GET", "HEAD"), "plan_apply_page"),
    ("/plan/{job_id}/apply", ("POST",), "plan_apply"),
    ("/plan/{job_id}/confirm", ("POST",), "plan_confirm"),
    ("/plan/{job_id}/deny", ("POST",), "plan_deny"),
    ("/jobs", ("GET", "HEAD"), "jobs_page"),
    ("/jobs/{job_id}", ("GET", "HEAD"), "job_page"),
    ("/jobs/{job_id}/fragment", ("GET", "HEAD"), "job_fragment"),
    ("/jobs/{job_id}/cancel", ("POST",), "job_cancel"),
    ("/static", (), "static"),
]
"""Every route in `create_app`, in order - Starlette matches the first route that fits, so order is
part of the table - as (path, methods, the handler's name). Starlette adds HEAD to every GET."""


def test_the_route_table_is_exactly_the_pinned_one(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(CONFIG)
    app = create_app(WebSettings(config_path=tmp_path / "config.toml", password="x"))
    while not isinstance(app, Starlette):  # under the security-header and body-limit wrappers
        app = app.app  # type: ignore[attr-defined]

    table: list[tuple[str, tuple[str, ...], str]] = []
    for route in app.routes:
        if isinstance(route, Route):
            table.append((route.path, tuple(sorted(route.methods or ())), route.endpoint.__name__))
        else:
            assert isinstance(route, Mount), route
            table.append((route.path, (), str(route.name)))

    assert table == ROUTES
