"""issue #106: a `CHANGELOG.md` at the repo root, and the release-specific passages moved out of
`docs/DEPLOY.md`.

issue #168 (CHANGELOG option C): at publication, the detailed `Unreleased` entries are replaced by
a short `## [0.5.0]` summary (8 to 10 bullets, no issue numbers), and `Unreleased` sits above it
for whatever accumulates after the tag.

Text-level checks, same style as `test_compose_example.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
DEPLOY_PATH = REPO_ROOT / "docs" / "DEPLOY.md"
README_PATH = REPO_ROOT / "README.md"


def test_changelog_exists_with_an_unreleased_section() -> None:
    assert CHANGELOG_PATH.is_file()
    text = CHANGELOG_PATH.read_text()
    assert "## [Unreleased]" in text


def test_the_unreleased_section_comes_before_0_5_0() -> None:
    text = CHANGELOG_PATH.read_text()
    unreleased_idx = text.index("## [Unreleased]")
    released_idx = text.index("## [0.5.0]")
    assert unreleased_idx < released_idx


def test_version_headings_are_newest_first_and_the_newest_is_pyprojects_version() -> None:
    import tomllib

    text = CHANGELOG_PATH.read_text()
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert re.findall(r"^##\s*\[\d[^\]]*\]", text, re.MULTILINE) == [f"## [{version}]", "## [0.5.0]"]


def test_the_0_5_1_section_opens_with_its_breaking_changes() -> None:
    # 0.5.1 moves three config.toml keys to the environment; an upgrader must see that first.
    section = CHANGELOG_PATH.read_text().partition("## [0.5.1]")[2].partition("\n## ")[0]
    assert section.lstrip().startswith("### Breaking")
    for name in ("LIKEARR_LIDARR_URL", "LIKEARR_ALLOWED_HOSTS", "LIKEARR_MUSICBRAINZ_CONTACT", "RESOLVER_VERSION"):
        assert name in section.partition("### Added")[0], name


def _the_0_5_0_section() -> str:
    text = CHANGELOG_PATH.read_text()
    after = text.partition("## [0.5.0]")[2]
    return after.partition("\n## ")[0]


def test_the_0_5_0_section_is_a_short_bulleted_summary() -> None:
    section = _the_0_5_0_section()
    lines = section.splitlines()
    bullets = [i for i, line in enumerate(lines) if line.startswith("- ")]
    assert 8 <= len(bullets) <= 10, len(bullets)
    # Every non-blank line is either a bullet or a wrapped continuation of one (indented, no
    # sub-headings snuck back in).
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        assert line.startswith("- ") or line.startswith("  "), (i, line)


def test_the_0_5_0_section_names_no_issue_person_or_host() -> None:
    section = _the_0_5_0_section()
    assert not re.search(r"#\d+", section)
    # Personal names and host labels are the identity guard's job (scripts/identity_guard.py, which
    # reads the private list); this keeps only the generic check.
    assert "homelab" not in section


def test_readme_links_to_the_changelog() -> None:
    assert "CHANGELOG.md" in README_PATH.read_text()


def test_deploy_doc_has_no_release_relative_wording() -> None:
    text = DEPLOY_PATH.read_text()
    # Word-boundaried so "paused too" (unrelated, legitimate wording) does not false-positive on
    # "used to" the way a plain substring grep would.
    assert not re.search(r"\bthis release\b|\bcurrent release\b|\bbefore this release\b|\bused to\b", text)
