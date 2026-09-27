"""Docs-drift regression tests (issue #117): text-level checks, like
`test_compose_example.py`, that the install instructions and a few other reader-facing claims
match what the code actually does. No behaviour under test here - just words.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
CLI_PATH = REPO_ROOT / "docs" / "CLI.md"
DEPLOY_PATH = REPO_ROOT / "docs" / "DEPLOY.md"
CONFIG_EXAMPLE_PATH = REPO_ROOT / "deploy" / "config.example.toml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

DOCS_FILES = [README_PATH, CLI_PATH, DEPLOY_PATH]

# The backup section (out of scope for #117 - covered by the backup and restore issue) uses
# "cron" to mean a host cron job that runs a backup after likearr finishes. Everything else in
# DEPLOY.md either means the [schedule] block's own `cron` expression or explicitly says there is
# no host cron any more.
_DEPLOY_BACKUP_SECTION_MARKER = "## Backup and restore"


def test_no_pypi_install_instructions() -> None:
    pattern = re.compile(r"uv tool install likearr($|[^\[])|pipx install likearr")
    for path in [README_PATH, CLI_PATH, DEPLOY_PATH, PYPROJECT_PATH]:
        text = path.read_text()
        assert not pattern.search(text), f"{path} still tells a reader to install from PyPI"


def test_cli_md_has_no_bare_see_below() -> None:
    text = CLI_PATH.read_text()
    assert "see below" not in text


def test_cli_md_setup_profiles_row_says_root_folder_defaults_are_reset() -> None:
    text = CLI_PATH.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `setup-profiles"))
    # The old wording claimed an existing root folder is "left as it is - never overwritten",
    # true only for a metadata profile; --apply resets a differing root folder's monitor
    # defaults, and the row must say so.
    assert "root folder" in row and "resets them" in row


def test_readme_page_table_has_no_doctor_row() -> None:
    text = README_PATH.read_text()
    assert "| **Doctor** |" not in text


def test_readme_safety_model_health_line_mentions_stdout_and_opt_in() -> None:
    text = README_PATH.read_text()
    _, _, after = text.partition("Every run records its health")
    # The bullet may wrap across lines in the source; take the paragraph up to the next bullet.
    paragraph = after.split("\n- ", 1)[0]
    assert "stdout" in paragraph
    assert "if you" in paragraph and "set one up" in paragraph


def test_config_example_no_longer_says_likearr_takes_no_lock() -> None:
    text = CONFIG_EXAMPLE_PATH.read_text()
    assert "does not take a lock itself" not in text
    assert "cron + flock" not in text


def test_deploy_md_cron_wording_is_only_the_schedule_expression_or_the_backup_section() -> None:
    text = DEPLOY_PATH.read_text()
    before_backup, _, after_backup = text.partition(_DEPLOY_BACKUP_SECTION_MARKER)
    assert after_backup, "DEPLOY.md's Backup and restore section is missing"
    # Before the backup section, "cron" only ever means: the absence of host cron (the
    # single-service cutover, #68), the [schedule] block's own `cron` key/expression, or a
    # comment referring to that expression. It must not say exit code 4 is cron-only any more.
    assert "never from cron" not in before_backup
    assert "4 comes only from a hand-run command" in before_backup


def test_pyproject_has_project_urls_pointing_at_the_repo() -> None:
    import tomllib

    data = tomllib.loads(PYPROJECT_PATH.read_text())
    urls = data["project"]["urls"]
    assert urls["Homepage"] == "https://github.com/sysdad/likearr"
    assert "sysdad/likearr" in urls["Issues"]


def test_every_pinned_image_and_release_tag_matches_pyprojects_version() -> None:
    """A release bump has to move every hardcoded `ghcr.io/sysdad/likearr:X.Y.Z` image pin and
    every `vX.Y.Z` release-tag URL at once - README's quick start, docs/DEPLOY.md's `docker pull`
    example and every service in deploy/compose.example.yaml - or one of them silently pins the
    old version."""
    import tomllib

    version = tomllib.loads(PYPROJECT_PATH.read_text())["project"]["version"]
    image_ref = f"ghcr.io/sysdad/likearr:{version}"
    tag_ref = f"/likearr/v{version}/"

    readme = README_PATH.read_text()
    assert image_ref in readme, "README.md's compose block doesn't pin the current version"
    assert tag_ref in readme, "README.md's curl URL doesn't fetch the current version's tag"

    deploy = DEPLOY_PATH.read_text()
    assert image_ref in deploy, "docs/DEPLOY.md's docker pull example doesn't pin the current version"

    compose = (REPO_ROOT / "deploy" / "compose.example.yaml").read_text()
    image_lines = [
        line.strip() for line in compose.splitlines() if "image:" in line and "ghcr.io/sysdad/likearr:" in line
    ]
    assert image_lines, "no image: lines found in deploy/compose.example.yaml"
    for line in image_lines:
        assert image_ref in line, f"stale image pin in deploy/compose.example.yaml: {line!r}"


def test_models_write_scopes_docstring_names_the_real_endpoint() -> None:
    text = (REPO_ROOT / "likearr" / "models.py").read_text()
    _, _, after = text.partition("SPOTIFY_WRITE_SCOPES")
    docstring = after.split('"""', 2)[1]
    assert "PUT /me/library" in docstring
    assert "PUT /me/following" not in docstring
    assert "PUT /me/albums" not in docstring


# --------------------------------------------------------------------------------------------- #
# README storefront (issue #108): requirements and "is this for you" before Quick start, the
# resolver detail moved to docs/dev/DESIGN.md, and every link and image path the README carries
# resolves.

_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _slugify(heading: str) -> str:
    """GitHub's heading-to-anchor rule, close enough for this repo's docs: strip inline code and
    emphasis markers, lowercase, drop anything but word characters/spaces/hyphens, then turn
    whitespace into hyphens."""
    text = heading.replace("`", "")
    text = re.sub(r"[*_]+", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _heading_slugs(text: str) -> set[str]:
    return {_slugify(heading) for _level, heading in _HEADING_PATTERN.findall(text)}


def _links(text: str) -> list[str]:
    return _LINK_PATTERN.findall(text)


def test_readme_links_and_images_resolve() -> None:
    text = README_PATH.read_text()
    readme_slugs = _heading_slugs(text)
    checked = 0
    for target in _links(text):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or target.startswith("mailto:"):
            continue  # external; not this test's job
        path_part, _, anchor = target.partition("#")
        if path_part:
            resolved = (REPO_ROOT / path_part).resolve()
            assert resolved.is_file(), f"README.md links to {target!r}, but {path_part} does not exist"
            if anchor:
                slugs = _heading_slugs(resolved.read_text()) if resolved.suffix == ".md" else None
                assert slugs is not None, f"README.md links to an anchor in a non-Markdown file: {target!r}"
                assert anchor in slugs, f"README.md links to {target!r}, but {path_part} has no such heading"
        else:
            assert anchor in readme_slugs, f"README.md links to {target!r}, but has no such heading itself"
        checked += 1
    assert checked > 10, "the link pattern matched far fewer links than README.md actually has"


def test_readme_requirements_and_is_this_for_you_come_before_quick_start() -> None:
    text = README_PATH.read_text()
    requirements = text.index("\n## Requirements")
    is_this_for_you = text.index("\n## Is this for you?")
    quick_start = text.index("\n## Quick start")
    assert is_this_for_you < quick_start
    assert requirements < quick_start


def test_readme_states_lidarr_versions_docker_and_spotify_premium() -> None:
    text = README_PATH.read_text()
    _, _, requirements = text.partition("## Requirements")
    requirements = requirements.split("\n## ", 1)[0]
    assert "Docker" in requirements
    assert "2.x or 3.x" in requirements
    assert "Premium" in requirements
    assert "developer app" in requirements


def test_readme_says_not_affiliated() -> None:
    text = README_PATH.read_text()
    assert "not affiliated with spotify or lidarr" in text.lower()


def test_readme_has_no_resolver_detail_above_quick_start() -> None:
    text = README_PATH.read_text()
    before_quick_start, _, _ = text.partition("## Quick start")
    for needle in ("deny_releases", "allow_remix_releases", "John Mayer Trio"):
        assert needle not in before_quick_start, f"{needle!r} still appears above Quick start"


def test_readme_safety_model_is_directly_after_quick_start() -> None:
    text = README_PATH.read_text()
    _, _, after_quick_start = text.partition("## Quick start")
    next_heading = re.search(r"\n## (.+)", after_quick_start)
    assert next_heading is not None
    assert next_heading.group(1).strip() == "Safety model"


def test_readme_safety_model_states_the_credit_rule() -> None:
    text = README_PATH.read_text()
    _, _, safety = text.partition("## Safety model")
    paragraph = safety.split("### What likearr writes", 1)[0]
    assert "related" in paragraph and "MusicBrainz" in paragraph


def test_readme_screenshots_have_alt_text_and_no_personal_library() -> None:
    text = README_PATH.read_text()
    images = re.findall(r"!\[([^\]]*)\]\((docs/images/[^)]+)\)", text)
    assert len(images) >= 2
    for alt, path in images:
        assert alt.strip(), f"{path} has no alt text"
        assert (REPO_ROOT / path).is_file()


def test_design_md_still_has_the_resolver_detail_sections() -> None:
    text = (REPO_ROOT / "docs" / "dev" / "DESIGN.md").read_text()
    assert "### Singles rule" in text
    assert "### Opting out: box sets, remix EPs and a deny list" in text
    assert "deny_releases" in text


# --------------------------------------------------------------------------------------------- #
# What can't be synced (issue #103): the README, docs/dev/DESIGN.md, the picker and `likearr
# playlists`' own text must all say the same thing about which playlists work - owned ones, and
# collaborative ones once Spotify is re-authorized with playlist-read-collaborative (item 3) - and
# use the same reason and workaround wording, not each their own paraphrase.


def test_readme_has_a_what_cant_be_synced_section_with_the_workaround() -> None:
    text = README_PATH.read_text()
    _, _, after = text.partition("## What can't be synced")
    assert after, "README.md has no 'What can't be synced' section"
    section = after.split("\n## ", 1)[0]
    assert "only returns playlist items for playlists you own or collaborate on" in section
    assert "like the songs" in section and "copy them into a playlist you own" in section


def test_readme_and_playlist_names_agree_on_the_not_owned_reason() -> None:
    from likearr.playlist_names import NOT_OWNED_REASON, NOT_OWNED_WORKAROUND

    readme = README_PATH.read_text()
    picker_html = (REPO_ROOT / "likearr" / "web" / "templates" / "_picker.html").read_text()
    commands_py = (REPO_ROOT / "likearr" / "shell" / "commands.py").read_text()

    assert NOT_OWNED_REASON in readme
    assert NOT_OWNED_WORKAROUND in readme
    # The picker and `likearr playlists` render the same constants rather than their own text.
    assert "picker.not_owned_reason" in picker_html
    assert "picker.not_owned_workaround" in picker_html
    assert "NOT_OWNED_REASON" in commands_py
    assert "NOT_OWNED_WORKAROUND" in commands_py


def test_every_doc_says_collaborative_playlists_work_once_re_authorized() -> None:
    """Item 3 landed: the README, DESIGN, the picker and `likearr playlists` all say a playlist you
    collaborate on works, and that a token from before needs one re-authorization - none still
    says likearr "does not yet" ask for the scope."""
    from likearr.playlist_names import COLLABORATIVE_REAUTH_REASON

    readme = README_PATH.read_text()
    design = (REPO_ROOT / "docs" / "dev" / "DESIGN.md").read_text()
    picker_html = (REPO_ROOT / "likearr" / "web" / "templates" / "_picker.html").read_text()
    commands_py = (REPO_ROOT / "likearr" / "shell" / "commands.py").read_text()
    for text in (readme, design):
        assert "playlist-read-collaborative" in text
        assert "does not yet request" not in text and "doesn't yet ask" not in text
    assert "A playlist you collaborate on works like one you own" in readme
    assert "picker.collaborative_reauth_reason" in picker_html
    assert "COLLABORATIVE_REAUTH_REASON" in commands_py
    assert "re-authorize Spotify" in COLLABORATIVE_REAUTH_REASON
