"""`likearr.__version__`, `commit()` and `build_info()`.

One version source: the static `version` in `pyproject.toml`, read at import time through
`importlib.metadata` off the installed distribution. These tests are hermetic - no subprocess, no
reimport of `likearr` - by exercising `_resolve_version()` directly and monkeypatching the
`metadata` module `likearr/__init__.py` already imports, and by monkeypatching `os.environ` for
the commit, which `commit()` reads fresh on every call rather than caching at import time.
"""

from __future__ import annotations

from importlib import metadata

import pytest

import likearr


def test_dunder_version_matches_the_installed_distribution() -> None:
    """The two can no longer drift apart, because there is only
    one of them - `__version__` *is* what `importlib.metadata` reports, not a hand-kept copy."""
    assert likearr.__version__ == metadata.version("likearr")


def test_dunder_version_is_the_pyproject_version() -> None:
    assert likearr.__version__ == "0.5.4"


def test_resolve_version_falls_back_when_the_package_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(likearr.metadata, "version", _raise)

    assert likearr._resolve_version() == "0+unknown"


def test_commit_is_none_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIKEARR_COMMIT", raising=False)

    assert likearr.commit() is None


def test_commit_is_none_when_the_env_var_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Docker image built without `--build-arg VCS_REF=...` still sets `ENV LIKEARR_COMMIT=""`
    (the Dockerfile's default), which must read the same as "not set", not as an empty commit."""
    monkeypatch.setenv("LIKEARR_COMMIT", "")

    assert likearr.commit() is None


def test_commit_reads_the_env_var_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIKEARR_COMMIT", "abc1234")

    assert likearr.commit() == "abc1234"


def test_build_info_is_just_the_version_without_a_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIKEARR_COMMIT", raising=False)

    assert likearr.build_info() == likearr.__version__


def test_build_info_appends_the_commit_in_parentheses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIKEARR_COMMIT", "abc1234")

    assert likearr.build_info() == f"{likearr.__version__} (abc1234)"
