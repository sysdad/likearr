"""The narrative lint (`scripts/narrative_lint.py`), its rules file and its CI workflow.

The command-line tests run against a throwaway git repository in `tmp_path` with a made-up rules
file, so a hit can only come from the test's own fixtures. The real rules file is read only to
check what it holds and how its patterns behave.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT = REPO_ROOT / "scripts" / "narrative_lint.py"
REAL_RULES = REPO_ROOT / "scripts" / "narrative_lint_phrases.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "narrative-lint.yml"

# Made-up words, so nothing but a fixture can match. The voice rule is a made-up pronoun.
RULES = r"""# a comment, then a blank line

zorblat quux
re:\((?:[A-Z]\w*\s+){0,3}[A-Z]\w*,\s*20\d{2}-\d
voice:(?i)\bthoo\b
"""
PHRASE = '"zorblat quux" (rules line 3)'
DATED = "(rules line 4)"
VOICE = '"voice:(?i)\\bthoo\\b" (rules line 5)'


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = str(tmp_path / "home")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


class Repo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.write("scripts/narrative_lint_phrases.txt", RULES)

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True, timeout=30
        )
        return result.stdout

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        self.git("add", rel)

    def lint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LINT), *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    (tmp_path / "home").mkdir()
    return Repo(tmp_path / "repo", _env(tmp_path))


@functools.cache
def _import_lint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("narrative_lint_under_test", LINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


def _hit_lines(result: subprocess.CompletedProcess[str], path: str) -> list[int]:
    """The line numbers the lint reported for `path`, in order."""
    prefix = f"narrative lint: {path}:"
    return sorted(
        int(line[len(prefix) :].split(":")[0]) for line in result.stderr.splitlines() if line.startswith(prefix)
    )


# -- the command line ----------------------------------------------------------------------------


def test_a_clean_tree_passes(repo: Repo) -> None:
    repo.write("README.md", "A plain description of the project.\n")
    result = repo.lint()
    assert result.returncode == 0, result.stderr
    assert "ok, 1 tracked text file(s) clean against 3 rules" in result.stdout


def test_a_phrase_is_reported_with_its_file_and_line(repo: Repo) -> None:
    repo.write("likearr/mod.py", 'X = 1\nLABEL = "made by Zorblat  Quux\'s team"\n')
    result = repo.lint()
    assert result.returncode == 1
    assert f"likearr/mod.py:2: {PHRASE}" in result.stderr
    assert "FAILED, 1 hit(s)" in result.stderr


@pytest.mark.parametrize("text", ["zorblat quuxes", "xzorblat quux", "zorblat_quux", "zorblat-quuxy"])
def test_a_phrase_matches_only_as_a_whole_phrase(repo: Repo, text: str) -> None:
    repo.write("notes.txt", f"{text}\n")
    assert repo.lint().returncode == 0


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("decided (Somebody, 2026-01-02)", True),  # narrative:allow
        ("decided (Some Body, 2025-12", True),  # narrative:allow
        ("the release (EP, 2011) instead", False),
        ("(somebody, 2026-01-02)", False),
    ],
)
def test_a_dated_attribution_pattern(repo: Repo, text: str, hit: bool) -> None:
    repo.write("notes.txt", f"{text}\n")
    result = repo.lint()
    assert result.returncode == (1 if hit else 0), result.stderr
    if hit:
        assert "notes.txt:1: " in result.stderr and DATED in result.stderr


def test_voice_in_markdown_prose_but_not_code_or_quotations(repo: Repo) -> None:
    repo.write(
        "docs/page.md",
        "\n".join(
            [
                "Thoo wrote this line.",  # 1: hit
                "```",
                "thoo = 1",  # 3: fenced code
                "```",
                'A user asks "can thoo see it?" here.',  # 5: quoted
                "Run `thoo --help` first.",  # 6: code span
                'A quotation "that starts here',  # 7
                'and has thoo in it" ends here.',  # 8: still inside the quotation
                "",
                "then thoo again.",  # 10: hit
            ]
        )
        + "\n",
    )
    result = repo.lint()
    assert result.returncode == 1
    lines = _hit_lines(result, "docs/page.md")
    assert lines == [1, 10]


def test_a_fence_closes_only_on_its_own_kind_of_run(repo: Repo) -> None:
    repo.write(
        "docs/page.md",
        "\n".join(
            [
                "````markdown",
                "```python",  # 2: an example fence inside the block, still code
                "thoo = 1",  # 3: code
                "```",  # 4: shorter than the opener, still code
                "````",
                "```",
                "thoo = 2",  # 7: code
                "```python",  # 8: an info string, so not a closer
                "thoo = 3",  # 9: code
                "```",
                "then thoo again.",  # 11: hit
            ]
        )
        + "\n",
    )
    result = repo.lint()
    assert _hit_lines(result, "docs/page.md") == [11], result.stderr


def test_a_file_with_carriage_return_line_ends_is_read_line_by_line(repo: Repo) -> None:
    repo.write("likearr/mod.py", b'x = 1\r"""Where thoo speaks."""\rzorblat_quux = 2  # thoo\r')
    result = repo.lint()
    assert result.returncode == 1, result.stderr
    assert _hit_lines(result, "likearr/mod.py") == [2, 3]


def test_voice_in_python_comments_and_docstrings_only(repo: Repo) -> None:
    source = (
        '"""Module docstring where thoo speaks."""\n'  # 1: hit
        "\n"
        "thoo_count = 0  # a comment by thoo\n"  # 3: hit (the comment, not the name)
        'LABEL = "thoo in a string literal"\n'  # 4: data
        "\n"
        "\n"
        "def f() -> None:\n"
        '    """First line.\n'
        "\n"
        "    Then thoo on a later line.\n"  # 10: hit
        '    """\n'
        "    thoo_count2 = 1\n"
    )
    repo.write("likearr/mod.py", source)
    result = repo.lint()
    assert result.returncode == 1
    lines = _hit_lines(result, "likearr/mod.py")
    assert lines == [1, 3, 10]


def test_a_docstrings_own_quotes_are_not_a_quotation(repo: Repo) -> None:
    repo.write("likearr/mod.py", "def f() -> None:\n    'Where thoo speaks.'\n")
    result = repo.lint()
    assert result.returncode == 1
    assert f"likearr/mod.py:2: {VOICE}" in result.stderr


@pytest.mark.parametrize(
    ("path", "content", "hit_lines"),
    [
        (".github/workflows/x.yml", "name: thoo\n# thoo in a comment\nrun: echo thoo  # and thoo\n", [2, 3]),
        ("deploy/config.example.toml", 'key = "thoo"\n# thoo\n', [2]),
        ("Dockerfile", "FROM thoo\n# thoo\n", [2]),
        ("scripts/hook", "#!/bin/sh\n# thoo\necho thoo\n", [2]),
        ("likearr/web/templates/a.html", "<p>thoo</p>\n<!-- thoo\nstill thoo -->\n{# thoo #}\n", [2, 3, 4]),
        ("likearr/web/static/a.js", "const thoo = 1; // thoo\n/* thoo\n thoo */ let x = thoo;\n", [1, 2, 3]),
        ("likearr/web/static/a.css", ".thoo { color: red; } /* thoo */\n", [1]),
        ("likearr/web/static/b.js", 'fetch("https://example.com/thoo");\n', []),
        ("likearr/web/static/vendor.min.js", "/* thoo */\n", []),
        ("tests/fixtures/data.json", '{"title": "thoo"}\n', []),
    ],
)
def test_voice_is_read_in_the_comments_of_each_file_type(
    repo: Repo, path: str, content: str, hit_lines: list[int]
) -> None:
    repo.write(path, content)
    result = repo.lint()
    lines = _hit_lines(result, f"{path}")
    assert lines == hit_lines, result.stderr
    assert result.returncode == (1 if hit_lines else 0)


def test_the_allow_marker_skips_only_its_own_line(repo: Repo) -> None:
    repo.write(
        "docs/page.md",
        "zorblat quux, a third-party title <!-- narrative:allow -->\nThoo, narrative:allow\nzorblat quux\n",
    )
    result = repo.lint()
    assert result.returncode == 1
    assert "docs/page.md:1:" not in result.stderr
    assert "docs/page.md:2:" not in result.stderr
    assert f"docs/page.md:3: {PHRASE}" in result.stderr


def test_binary_and_untracked_files_are_skipped(repo: Repo) -> None:
    repo.write("image.png", b"\x89PNG\x00\x00zorblat quux\x00")
    (repo.path / "scratch.md").write_text("zorblat quux\n")
    assert repo.lint().returncode == 0


def test_the_rules_file_itself_is_not_scanned(repo: Repo) -> None:
    # Its own lines hold every phrase, and it is tracked like any other file.
    result = repo.lint()
    assert result.returncode == 0, result.stderr


def test_a_rules_file_can_be_named(repo: Repo, tmp_path: Path) -> None:
    other = tmp_path / "other.txt"
    other.write_text("plimsoll\n")
    repo.write("notes.txt", "a plimsoll line\n")
    result = repo.lint("--phrases", str(other))
    assert result.returncode == 1
    assert 'notes.txt:1: "plimsoll" (rules line 1)' in result.stderr


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        (None, "rules file not found"),
        ("# only a comment\n\n", "has no rules"),
        ("re:(unclosed\n", "not a valid regular expression"),
        ("voice:\n", "empty pattern"),
    ],
)
def test_a_missing_empty_or_broken_rules_file_fails(
    repo: Repo, tmp_path: Path, rules: str | None, message: str
) -> None:
    path = tmp_path / "rules.txt"
    if rules is not None:
        path.write_text(rules)
    result = repo.lint("--phrases", str(path))
    assert result.returncode == 2
    assert message in result.stderr


def test_a_python_file_that_does_not_parse_is_an_error_not_a_pass(repo: Repo) -> None:
    repo.write("likearr/broken.py", "def f(:\n")
    result = repo.lint()
    assert result.returncode == 2
    assert "likearr/broken.py" in result.stderr


def test_it_runs_from_a_subdirectory(repo: Repo) -> None:
    repo.write("docs/deep/page.md", "clean\n")
    repo.write("top.md", "zorblat quux\n")
    result = subprocess.run(
        [sys.executable, str(LINT)],
        cwd=repo.path / "docs" / "deep",
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert f"top.md:1: {PHRASE}" in result.stderr


# -- the real rules ------------------------------------------------------------------------------


@functools.cache
def _real_rules() -> list[Any]:
    lint = _import_lint()
    return lint.load_rules(REAL_RULES)


def test_the_real_rules_hold_the_ruled_phrases() -> None:
    texts = {rule.text for rule in _real_rules()}
    for phrase in ["our own", "the maintainer", "the author", "in production", "the live library"]:  # narrative:allow
        assert phrase in texts, phrase


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("decided (Somebody, 2026-09-20): keep it", True),  # narrative:allow
        ("(Some Body, 2026-", True),  # narrative:allow
        ("(ruled by Somebody, 2026-09-20)", True),  # narrative:allow
        ('matches "Tease Me" (album, 1992) instead', False),
        ("(EP, 2011)", False),
        ("the authority, the authorized user, authorize", False),
        ("your own app", False),
    ],
)
def test_the_real_rules_on_every_line(text: str, hit: bool) -> None:
    lint = _import_lint()
    found = lint.scan_text("notes.txt", text + "\n", _real_rules())
    assert bool(found) == hit, found


@pytest.mark.parametrize(
    ("comment", "hit"),
    [
        ("# we retry once", True),
        ("# We're done", True),
        ("# the server asked us to wait", True),
        ("# a US band", False),
        ("# our cache", True),
        ("# your cache", False),
        ("# ours, not theirs", True),
        ("# I think so", True),
        ("# I'm sure", True),
        ("# I/O bound", False),
        ("# xargs -I{} curl", False),
        ("# for i in range", False),
        ("# my library", True),
        ('# the user asks "did my Spotify change?"', False),
        ("# see GET /me", False),
    ],
)
def test_the_real_voice_rules_in_a_comment(comment: str, hit: bool) -> None:
    lint = _import_lint()
    found = lint.scan_text("likearr/mod.py", f"x = 1  {comment}\n", _real_rules())
    assert bool(found) == hit, found


# -- the CI workflow -----------------------------------------------------------------------------


def test_the_workflow_pins_actions_reads_only_and_runs_the_lint() -> None:
    text = WORKFLOW.read_text()
    uses = [line.split("uses:", 1)[1].strip() for line in text.splitlines() if "uses:" in line]
    assert uses
    for ref in uses:
        _action, _, rest = ref.partition("@")
        sha, _, comment = rest.partition(" # ")
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), ref
        assert comment.startswith("v"), ref
    assert "permissions:\n  contents: read\n" in text
    assert "run: python3 scripts/narrative_lint.py\n" in text
    assert "secrets." not in text
    assert "pull_request:" in text and "pull_request_target" not in text
