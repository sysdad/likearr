"""`pyproject.toml`: the web stack is a core dependency.

`likearr start` has been the whole product since the single-service cutover; a plain
`likearr` install that cannot run it is a defect, not a lean-CLI feature. This checks the parsed
TOML directly (`tomllib` is stdlib on the 3.12+ this project requires) rather than the file's text,
so a reordering or reformatting of the dependency lists does not make it flaky.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"

WEB_PACKAGES = {
    "itsdangerous",
    "jinja2",
    "python-multipart",
    "starlette",
    "tomlkit",
    "tzdata",
    "uvicorn",
}


def _data() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text())


def _names(requirements: list[str]) -> set[str]:
    """`"starlette>=1.6.0"` -> `"starlette"`; a bare name has no specifier to strip."""
    return {re.split(r"[><=!~\[]", req, maxsplit=1)[0].strip() for req in requirements}


def test_the_web_packages_are_core_dependencies() -> None:
    dependencies = _names(_data()["project"]["dependencies"])
    assert dependencies >= WEB_PACKAGES


def test_there_is_no_web_extra() -> None:
    optional = _data()["project"].get("optional-dependencies", {})
    assert "web" not in optional


def test_the_dev_extra_does_not_reference_the_web_extra() -> None:
    dev = _data()["project"]["optional-dependencies"]["dev"]
    assert not any("likearr[web]" in req for req in dev)


def test_the_classifiers_include_web_environment() -> None:
    classifiers = _data()["project"]["classifiers"]
    assert "Environment :: Web Environment" in classifiers
