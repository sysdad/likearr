"""issue #115: `docs/DEPLOY.md`'s "Backup and restore" section names every file under `/data` (plus
the compose `.env` file) that the always-on service needs to survive a disk loss or a host move.

Every file it lists is checked against the code that actually defines its path, the same way
`test_docs_spotify.py` and `test_compose_example.py` check their own docs, so a path that moves in
the code fails this test instead of silently going stale in the doc. Text-level checks confirm the
things the issue called out explicitly: no leftover cron-timing or `sqlite3` CLI advice, and the
two "restore from backup" pointers elsewhere in the docs link to this section.
"""

from __future__ import annotations

import re
from pathlib import Path

from likearr.playlist_names import names_path
from likearr.prune_ledger import ledger_path
from likearr.shell.promote_save import DEFAULT_PLAN_PATH
from likearr.shell.run import DEFAULT_DIFF_PATH
from likearr.web.settings import BACKUP_KEEP

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_PATH = REPO_ROOT / "docs" / "DEPLOY.md"
DESIGN_PATH = REPO_ROOT / "docs" / "dev" / "DESIGN.md"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
CONFIG_EXAMPLE_PATH = REPO_ROOT / "deploy" / "config.example.toml"
ENV_EXAMPLE_PATH = REPO_ROOT / "deploy" / "env.example"
CLI_PATH = REPO_ROOT / "likearr" / "shell" / "cli.py"
LAST_RUN_PATH = REPO_ROOT / "likearr" / "shell" / "last_run.py"
SPOTIFY_SNAPSHOT_PATH = REPO_ROOT / "likearr" / "shell" / "spotify_snapshot.py"
SPOTIFY_ADAPTER_PATH = REPO_ROOT / "likearr" / "adapters" / "spotify.py"
WEB_CONTEXT_PATH = REPO_ROOT / "likearr" / "web" / "context.py"


def _deploy_text() -> str:
    return DEPLOY_PATH.read_text()


def _section() -> str:
    """The "Backup and restore" section's own text, heading to heading."""
    text = _deploy_text()
    _, _, rest = text.partition("\n## Backup and restore\n")
    assert rest, "docs/DEPLOY.md has no '## Backup and restore' section"
    section, _, _ = rest.partition("\n## ")
    return section


def test_the_section_exists() -> None:
    assert "## Backup and restore" in _deploy_text()


# ---------------------------------------------------------------------------- files it names


def test_the_state_db_default_name_is_documented_and_matches_the_example_config() -> None:
    match = re.search(r'^db\s*=\s*"([^"]+)"', CONFIG_EXAMPLE_PATH.read_text(), re.MULTILINE)
    assert match, "deploy/config.example.toml no longer sets [state] db"
    assert match.group(1) in _section()


def test_the_config_backup_pattern_and_keep_count_match_settings_py() -> None:
    section = _section()
    assert "config.toml.bak-" in section
    assert str(BACKUP_KEEP) in section


def test_the_spotify_token_file_default_name_is_documented() -> None:
    match = re.search(r'^token_file\s*=\s*"([^"]+)"', CONFIG_EXAMPLE_PATH.read_text(), re.MULTILINE)
    assert match, "deploy/config.example.toml no longer sets [spotify] token_file"
    assert match.group(1) in _section()


def test_the_spotify_token_lock_file_is_documented_as_needing_no_backup() -> None:
    # adapters/spotify.py builds the lock file as "<token_file.name>.lock"; DESIGN.md documents it
    # as an fcntl.flock guard, and DEPLOY.md's backup section says it needs no backup.
    assert '.lock")' in SPOTIFY_ADAPTER_PATH.read_text()
    assert "`<token_file>.lock` (`fcntl.flock`" in DESIGN_PATH.read_text()
    section = _section()
    assert "spotify-token.json.lock" in section
    assert "needs no backup" in section


def test_the_prune_ledger_path_matches_the_code() -> None:
    assert ledger_path(Path("/data/config.toml")) == Path("/data/ui/prune-ledger.json")
    assert "ui/prune-ledger.json" in _section()


def test_the_playlist_names_path_matches_the_code() -> None:
    assert names_path(Path("/data/config.toml")) == Path("/data/ui/playlist-names.json")
    assert "ui/playlist-names.json" in _section()


def test_the_spotify_snapshot_path_matches_the_code() -> None:
    text = SPOTIFY_SNAPSHOT_PATH.read_text()
    assert '"ui" / "spotify-snapshot.json"' in text
    assert "ui/spotify-snapshot.json" in _section()


def test_the_jobs_directory_path_matches_the_code() -> None:
    text = WEB_CONTEXT_PATH.read_text()
    assert '"ui" / "jobs"' in text
    assert "ui/jobs" in _section()


def test_the_last_run_path_matches_the_code() -> None:
    text = LAST_RUN_PATH.read_text()
    assert '"last-run.json"' in text
    assert "last-run.json" in _section()


def test_the_cli_output_defaults_match_the_code() -> None:
    assert Path("diff.json") == DEFAULT_DIFF_PATH
    assert Path("promote-save.json") == DEFAULT_PLAN_PATH
    assert 'default=Path("adopt.json")' in CLI_PATH.read_text()
    assert 'default=Path("prune.json")' in CLI_PATH.read_text()
    section = _section()
    for name in ("diff.json", "adopt.json", "prune.json", "promote-save.json"):
        assert name in section


def test_the_env_file_is_documented_as_outside_data_and_secret() -> None:
    assert re.search(r"^# Copy to \.env", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)
    section = _section()
    assert "`.env`" in section
    assert "secret" in section


def test_the_restore_ownership_matches_the_dockerfile_build_args() -> None:
    dockerfile = DOCKERFILE_PATH.read_text()
    assert "ARG LIKEARR_UID=1000" in dockerfile
    assert "ARG LIKEARR_GID=1000" in dockerfile
    section = _section()
    assert "LIKEARR_UID" in section
    assert "LIKEARR_GID" in section


# ---------------------------------------------------------------------------- what it must not say


def test_no_sqlite3_cli_invocation_remains() -> None:
    section = _section()
    assert "sqlite3 /data" not in section
    assert "PRAGMA wal_checkpoint" not in section
    assert "sqlite3 CLI" not in section


def test_no_cron_timing_advice_remains() -> None:
    section = _section()
    assert not re.search(r"\bcron\b", section, re.IGNORECASE)


def test_it_uses_the_stdlib_sqlite3_module_the_service_itself_already_depends_on() -> None:
    # likearr's own state adapter imports sqlite3, so the module is already a hard runtime
    # dependency of the built image - the doc's online-copy command needs nothing extra.
    state_adapter = (REPO_ROOT / "likearr" / "adapters" / "state_sqlite.py").read_text()
    assert "import sqlite3" in state_adapter
    assert "import sqlite3" in _section()
    assert ".backup(" in _section()


# ---------------------------------------------------------------------------- cross-references


def test_the_adopt_recovery_warning_links_to_the_new_section() -> None:
    text = _deploy_text()
    assert "Restore the database from backup first" in text
    idx = text.index("Restore the database from backup first")
    window = re.sub(r"\s+", " ", text[idx : idx + 200])
    assert "Backup and restore" in window


def test_design_doc_points_the_other_restore_warning_at_the_new_section() -> None:
    text = DESIGN_PATH.read_text()
    assert "Restore the state DB from backup instead" in text
    idx = text.index("Restore the state DB from backup instead")
    window = text[idx : idx + 200]
    assert "DEPLOY.md#backup-and-restore" in window
