"""Docs checks that catch real errors: links and anchors that don't resolve, screenshots that are
missing or have no alt text, and pinned versions that fall behind `pyproject.toml`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_FENCE_PATTERN = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _markdown_files() -> list[Path]:
    """Every tracked Markdown file, so a new doc is checked without being listed here."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    paths = {REPO_ROOT / line for line in out.splitlines() if line}
    return sorted(path for path in paths if path.is_file())


def _slugify(heading: str) -> str:
    """GitHub's heading-to-anchor rule, close enough for this repo's docs."""
    text = heading.replace("`", "")
    text = re.sub(r"[*_]+", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text.strip())


def _heading_slugs(text: str) -> set[str]:
    text = _FENCE_PATTERN.sub("", text)
    return {_slugify(heading) for _level, heading in _HEADING_PATTERN.findall(text)}


def _links(text: str) -> list[str]:
    return _LINK_PATTERN.findall(_FENCE_PATTERN.sub("", text))


def test_there_are_markdown_files_to_check() -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in _markdown_files()}
    assert {"README.md", "docs/install.md", "docs/troubleshooting.md", "docs/cli.md"} <= names


def test_every_relative_link_and_anchor_resolves() -> None:
    problems: list[str] = []
    for doc in _markdown_files():
        text = doc.read_text()
        for target in _links(text):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue  # external or mailto
            path_part, _, anchor = target.partition("#")
            where = f"{doc.relative_to(REPO_ROOT)} -> {target}"
            resolved = (doc.parent / path_part).resolve() if path_part else doc
            if not resolved.exists():
                problems.append(f"{where}: no such file")
                continue
            if anchor:
                if resolved.suffix != ".md":
                    problems.append(f"{where}: anchor into a non-Markdown file")
                elif anchor not in _heading_slugs(resolved.read_text()):
                    problems.append(f"{where}: no such heading")
    assert not problems, "\n".join(problems)


def test_readme_screenshots_exist_and_have_alt_text() -> None:
    images = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", README_PATH.read_text())
    assert images, "the README shows no screenshots"
    for alt, path in images:
        assert alt.strip(), f"{path} has no alt text"
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist"


def test_no_pypi_install_instructions() -> None:
    pattern = re.compile(r"uv tool install likearr($|[^\[])|pipx install likearr")
    for path in [*_markdown_files(), PYPROJECT_PATH]:
        assert not pattern.search(path.read_text()), f"{path} tells a reader to install from PyPI"


def test_pyproject_has_project_urls_pointing_at_the_repo() -> None:
    import tomllib

    data = tomllib.loads(PYPROJECT_PATH.read_text())
    urls = data["project"]["urls"]
    assert urls["Homepage"] == "https://github.com/sysdad/likearr"
    assert "sysdad/likearr" in urls["Issues"]


def test_every_pinned_image_matches_pyprojects_version() -> None:
    """A release bump has to move every `ghcr.io/sysdad/likearr:X.Y.Z` pin at once."""
    import tomllib

    version = tomllib.loads(PYPROJECT_PATH.read_text())["project"]["version"]
    image_ref = f"ghcr.io/sysdad/likearr:{version}"
    assert image_ref in README_PATH.read_text(), "README.md's compose block doesn't pin the current version"

    pinned = re.compile(r"ghcr\.io/sysdad/likearr:(\d+\.\d+\.\d+)")
    docs = [path for path in _markdown_files() if path.name != "CHANGELOG.md"]
    for path in [*docs, REPO_ROOT / "deploy" / "compose.example.yaml"]:
        for found in pinned.findall(path.read_text()):
            assert found == version, f"{path.relative_to(REPO_ROOT)} pins {found}, not {version}"


def test_models_write_scopes_docstring_names_the_real_endpoint() -> None:
    text = (REPO_ROOT / "likearr" / "models.py").read_text()
    _, _, after = text.partition("SPOTIFY_WRITE_SCOPES")
    docstring = after.split('"""', 2)[1]
    assert "PUT /me/library" in docstring
    assert "PUT /me/following" not in docstring
    assert "PUT /me/albums" not in docstring


def test_the_picker_and_playlists_command_render_the_shared_reason() -> None:
    """The not-owned reason and the collaborative re-auth reason are written once, in
    `likearr.playlist_names`, and both the picker and `likearr playlists` use them."""
    picker_html = (REPO_ROOT / "likearr" / "web" / "templates" / "_picker.html").read_text()
    commands_py = (REPO_ROOT / "likearr" / "shell" / "commands.py").read_text()
    for name in ("not_owned_reason", "not_owned_workaround", "collaborative_reauth_reason"):
        assert f"picker.{name}" in picker_html
        assert name.upper() in commands_py
