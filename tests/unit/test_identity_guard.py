"""The identity leak guard (`scripts/identity_guard.py`) and its pre-push hook (`scripts/pre-push`).

Every test runs against a throwaway git repository in `tmp_path` and a fake denylist written for
the test, through the scripts' real command lines. The real denylist is never read: `HOME` points
at `tmp_path`, and `IDENTITY_DENYLIST` / `LIKEARR_IDENTITY_DENYLIST_FILE` are always set or cleared
by the test itself. The fake entries are made-up words no real file here should contain.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "identity_guard.py"
HOOK = REPO_ROOT / "scripts" / "pre-push"

# Deliberately odd, so a hit can only come from the test's own fixtures.
SUBSTRING = "Quorvex"
WORD = "Zanth"
EMAIL_BIT = "fakehost-zz9.example.org"

DENYLIST = f"""\
# a comment line, then a blank line, neither of them an entry

{SUBSTRING}
w:{WORD}
  {EMAIL_BIT}
"""
# Entry numbers as the guard reports them: comments and blank lines are not counted.
SUBSTRING_ENTRY = "entry 1 (denylist line 3)"
WORD_ENTRY = "entry 2 (denylist line 4)"
EMAIL_ENTRY = "entry 3 (denylist line 5)"

SECRETS = [SUBSTRING, WORD, EMAIL_BIT]

CLEAN_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.invalid",
}


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("IDENTITY_DENYLIST", None)
    env.pop("LIKEARR_IDENTITY_DENYLIST_FILE", None)
    env["HOME"] = str(tmp_path / "home")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(CLEAN_IDENTITY)
    return env


class Repo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def commit(self, message: str = "a commit", **identity: str) -> str:
        env = {**self.env, **identity}
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "home").mkdir()
    return _base_env(tmp_path)


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Repo:
    return Repo(tmp_path / "repo", env)


@pytest.fixture
def denylist_file(tmp_path: Path) -> Path:
    path = tmp_path / "denylist.txt"
    path.write_text(DENYLIST)
    return path


def _guard(
    repo: Repo, *args: str, denylist: Path | None = None, env_list: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(repo.env)
    if env_list is not None:
        env["IDENTITY_DENYLIST"] = env_list
    argv = [sys.executable, str(GUARD)]
    if denylist is not None:
        argv += ["--denylist-file", str(denylist)]
    return subprocess.run(argv + list(args), cwd=repo.path, env=env, capture_output=True, text=True, timeout=60)


def _assert_no_echo(result: subprocess.CompletedProcess[str]) -> None:
    output = (result.stdout + result.stderr).lower()
    for secret in SECRETS:
        assert secret.lower() not in output, "the guard echoed a denylist entry or the text it matched"


# -- the denylist ------------------------------------------------------------------------------


def test_a_clean_tree_passes(repo: Repo, denylist_file: Path) -> None:
    repo.write("README.md", "Nothing to see here.\n")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 0, result.stderr
    assert "ok, 1 tracked file(s) clean against 3 entries" in result.stdout


def test_the_list_can_come_from_the_environment(repo: Repo) -> None:
    repo.write("notes.txt", f"hello {SUBSTRING}\n")
    repo.commit()
    result = _guard(repo, env_list=DENYLIST)
    assert result.returncode == 1
    assert f"notes.txt:1: matches {SUBSTRING_ENTRY}" in result.stderr
    _assert_no_echo(result)


@pytest.mark.parametrize("listing", ["", "   \n\n", "# only a comment\n\n# and another\n"])
def test_an_empty_list_in_the_environment_fails(repo: Repo, listing: str) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    result = _guard(repo, env_list=listing)
    assert result.returncode == 2
    assert "cannot run" in result.stderr
    assert "ok," not in result.stdout


def test_an_unset_environment_list_fails(repo: Repo) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    result = _guard(repo)
    assert result.returncode == 2
    assert "IDENTITY_DENYLIST is unset or empty" in result.stderr


def test_a_missing_list_file_fails(repo: Repo, tmp_path: Path) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    result = _guard(repo, denylist=tmp_path / "nope.txt")
    assert result.returncode == 2
    assert "denylist file not found" in result.stderr


def test_a_list_file_of_only_comments_fails(repo: Repo, tmp_path: Path) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n\n")
    result = _guard(repo, denylist=empty)
    assert result.returncode == 2
    assert "has no entries" in result.stderr


def test_a_whole_word_entry_with_no_word_fails(repo: Repo, tmp_path: Path) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    bad = tmp_path / "bad.txt"
    bad.write_text("w:\n")
    result = _guard(repo, denylist=bad)
    assert result.returncode == 2


def test_comment_lines_are_not_entries(repo: Repo, denylist_file: Path) -> None:
    # The denylist's own comment text must not become something the guard searches for.
    repo.write("README.md", "a comment line, then a blank line, neither of them an entry\n")
    repo.commit()
    assert _guard(repo, denylist=denylist_file).returncode == 0


# -- matching ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"{SUBSTRING}",
        f"{SUBSTRING.lower()} in lower case",
        f"{SUBSTRING.upper()}",
        f"inside a word: x{SUBSTRING}y",
        f"https://{EMAIL_BIT.upper()}/path",
    ],
)
def test_a_substring_entry_matches_anywhere_in_any_case(repo: Repo, denylist_file: Path, text: str) -> None:
    repo.write("file.txt", f"first line\n{text}\n")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 1
    assert "file.txt:2: matches entry" in result.stderr
    _assert_no_echo(result)


@pytest.mark.parametrize("text", [WORD, f"{WORD}'s", WORD.lower(), f"({WORD.upper()})", f"a-{WORD}-b"])
def test_a_whole_word_entry_matches_the_word(repo: Repo, denylist_file: Path, text: str) -> None:
    repo.write("file.txt", f"{text}\n")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 1
    assert f"file.txt:1: matches {WORD_ENTRY}" in result.stderr
    _assert_no_echo(result)


@pytest.mark.parametrize("text", [f"{WORD}ander", f"x{WORD}", f"{WORD}_1", f"{WORD}2"])
def test_a_whole_word_entry_does_not_match_inside_a_longer_word(repo: Repo, denylist_file: Path, text: str) -> None:
    repo.write("file.txt", f"{text}\n")
    repo.commit()
    assert _guard(repo, denylist=denylist_file).returncode == 0


@pytest.mark.parametrize(
    ("entry", "text", "hit"), [("@Vexl", "by @vexl.", True), ("Vexl.", "Vexl. ok", True), ("@Vexl", "x@Vexl", False)]
)
def test_a_whole_word_entry_may_start_or_end_with_punctuation(
    repo: Repo, tmp_path: Path, entry: str, text: str, hit: bool
) -> None:
    # `\b` would need a word character inside the entry's own edge, and never match these.
    listing = tmp_path / "punct.txt"
    listing.write_text(f"w:{entry}\n")
    repo.write("file.txt", f"{text}\n")
    repo.commit()
    assert _guard(repo, denylist=listing).returncode == (1 if hit else 0)


def test_an_unreadable_tracked_file_is_an_error_not_a_hit(repo: Repo, denylist_file: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    repo.write("locked.txt", "clean\n")
    repo.commit()
    (repo.path / "locked.txt").chmod(0)
    try:
        result = _guard(repo, denylist=denylist_file)
    finally:
        (repo.path / "locked.txt").chmod(0o644)
    assert result.returncode == 2, result.stderr
    assert "cannot run" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_allow_marker_suppresses_only_its_own_line(repo: Repo, denylist_file: Path) -> None:
    repo.write(
        "file.txt",
        f"a third-party name {SUBSTRING}  # identity:allow\nclean\nbut this {SUBSTRING} is not allowed\n",
    )
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 1
    assert "file.txt:1:" not in result.stderr
    assert f"file.txt:3: matches {SUBSTRING_ENTRY}" in result.stderr


def test_every_matching_entry_is_reported(repo: Repo, denylist_file: Path) -> None:
    repo.write("file.txt", f"{SUBSTRING} and {WORD}\n")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert f"file.txt:1: matches {SUBSTRING_ENTRY}, {WORD_ENTRY}" in result.stderr


def test_counts_only_leaves_out_where_and_which(repo: Repo, denylist_file: Path) -> None:
    repo.write("file.txt", f"{SUBSTRING} and {WORD}\n")
    repo.write("other.txt", f"{EMAIL_BIT}\n")
    repo.commit()
    result = _guard(repo, "--counts-only", denylist=denylist_file)
    assert result.returncode == 1
    assert "FAILED, 2 hit(s) in 2 tracked file(s) against 3 entries" in result.stderr
    assert "Run the guard locally" in result.stderr
    for detail in ("file.txt", "other.txt", "entry", "denylist line"):
        assert detail not in result.stderr
    _assert_no_echo(result)


def test_counts_only_still_passes_a_clean_tree(repo: Repo, denylist_file: Path) -> None:
    repo.write("README.md", "Nothing to see here.\n")
    repo.commit()
    result = _guard(repo, "--counts-only", denylist=denylist_file)
    assert result.returncode == 0, result.stderr
    assert "ok, 1 tracked file(s) clean against 3 entries" in result.stdout


# -- what is scanned ---------------------------------------------------------------------------


def test_binary_files_are_skipped(repo: Repo, denylist_file: Path) -> None:
    repo.write("image.png", b"\x89PNG\r\n\x1a\n\x00\x00" + SUBSTRING.encode() + b"\x00")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_untracked_files_are_not_scanned(repo: Repo, denylist_file: Path) -> None:
    repo.write("README.md", "clean\n")
    repo.commit()
    repo.write("scratch.txt", f"{SUBSTRING}\n")
    assert _guard(repo, denylist=denylist_file).returncode == 0


def test_every_tracked_file_is_scanned_from_a_subdirectory_too(repo: Repo, denylist_file: Path) -> None:
    repo.write("docs/deep/page.md", "clean\n")
    repo.write("top.txt", f"{WORD}\n")
    repo.commit()
    env = dict(repo.env)
    result = subprocess.run(
        [sys.executable, str(GUARD), "--denylist-file", str(denylist_file)],
        cwd=repo.path / "docs" / "deep",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert f"top.txt:1: matches {WORD_ENTRY}" in result.stderr


def test_a_path_that_matches_is_withheld_from_the_output(repo: Repo, denylist_file: Path) -> None:
    repo.write("a.txt", "clean\n")
    repo.write(f"people/{SUBSTRING}/notes.txt", f"about {WORD}\n")
    repo.commit()
    result = _guard(repo, denylist=denylist_file)
    assert result.returncode == 1
    assert f"tracked file #2 (path withheld): its path matches {SUBSTRING_ENTRY}" in result.stderr
    assert f"tracked file #2 (path withheld):1: matches {WORD_ENTRY}" in result.stderr
    _assert_no_echo(result)


def test_a_symlink_is_scanned_as_its_target_and_not_followed(repo: Repo, denylist_file: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(f"{SUBSTRING}\n")
    (repo.path / "link").symlink_to(outside)
    repo.commit()
    # The target path holds nothing on the list, and the file it points at is never read.
    assert _guard(repo, denylist=denylist_file).returncode == 0


def test_rev_scans_the_committed_tree_not_the_working_tree(repo: Repo, denylist_file: Path) -> None:
    repo.write("file.txt", f"{SUBSTRING}\n")
    bad = repo.commit()
    repo.write("file.txt", "fixed in the working tree only\n")
    assert _guard(repo, denylist=denylist_file).returncode == 0
    result = _guard(repo, "--rev", bad, denylist=denylist_file)
    assert result.returncode == 1
    assert f"file.txt:1: matches {SUBSTRING_ENTRY}" in result.stderr
    assert "at " + bad in result.stderr
    _assert_no_echo(result)


def test_rev_skips_binary_blobs_and_withholds_matching_paths(repo: Repo, denylist_file: Path) -> None:
    repo.write("blob.bin", b"\x00" + WORD.encode())
    repo.write(f"{EMAIL_BIT}.txt", "clean\n")
    head = repo.commit()
    result = _guard(repo, "--rev", head, denylist=denylist_file)
    assert result.returncode == 1
    assert f"(path withheld): its path matches {EMAIL_ENTRY}" in result.stderr
    assert WORD_ENTRY not in result.stderr
    _assert_no_echo(result)


# -- commits -----------------------------------------------------------------------------------


def _commit_range(repo: Repo, **identity: str) -> tuple[str, str]:
    repo.write("README.md", "clean\n")
    base = repo.commit("base")
    repo.write("README.md", "still clean\n")
    head = repo.commit(identity.pop("message", "change"), **identity)
    return base, head


@pytest.mark.parametrize(
    ("identity", "field", "entry"),
    [
        ({"GIT_AUTHOR_NAME": f"{WORD} Person"}, "author name", WORD_ENTRY),
        ({"GIT_AUTHOR_EMAIL": f"me@{EMAIL_BIT}"}, "author email", EMAIL_ENTRY),
        ({"GIT_COMMITTER_NAME": f"x{SUBSTRING}"}, "committer name", SUBSTRING_ENTRY),
        ({"GIT_COMMITTER_EMAIL": f"{SUBSTRING.lower()}@example.invalid"}, "committer email", SUBSTRING_ENTRY),
    ],
)
def test_commits_checks_author_and_committer(
    repo: Repo, denylist_file: Path, identity: dict[str, str], field: str, entry: str
) -> None:
    base, head = _commit_range(repo, **identity)
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {head[:12]}: {field} matches {entry}" in result.stderr
    assert base[:12] not in result.stderr
    _assert_no_echo(result)


def test_commits_checks_the_message_with_the_allow_marker(repo: Repo, denylist_file: Path) -> None:
    message = f"Subject line\n\nThanks to {WORD} identity:allow\nand {SUBSTRING} too\n"
    base, head = _commit_range(repo, message=message)
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {head[:12]}: message line 4 matches {SUBSTRING_ENTRY}" in result.stderr
    assert WORD_ENTRY not in result.stderr
    _assert_no_echo(result)


def test_commits_outside_the_range_are_not_checked(repo: Repo, denylist_file: Path) -> None:
    repo.write("a.txt", "a\n")
    old = repo.commit(f"old {SUBSTRING}")
    repo.write("a.txt", "b\n")
    head = repo.commit("new and clean")
    result = _guard(repo, "--commits", f"{old}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr
    assert "1 commit(s) clean" in result.stdout


def test_a_lone_revision_checks_everything_reachable(repo: Repo, denylist_file: Path) -> None:
    repo.write("a.txt", "a\n")
    root = repo.commit(f"root {SUBSTRING}")
    repo.write("a.txt", "b\n")
    head = repo.commit("clean")
    result = _guard(repo, "--commits", head, denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {root[:12]}: message line 1 matches {SUBSTRING_ENTRY}" in result.stderr


@pytest.mark.parametrize("as_range", [False, True])
def test_commits_checks_an_annotated_tag_tip(repo: Repo, denylist_file: Path, as_range: bool) -> None:
    base, head = _commit_range(repo)
    tag_env = {**repo.env, "GIT_COMMITTER_NAME": f"{WORD} Person"}
    repo.git("tag", "-a", "v9", "-m", f"release\n\nby {SUBSTRING}", head, env=tag_env)
    tag_sha = repo.git("rev-parse", "v9").strip()
    result = _guard(repo, "--commits", f"{base}..v9" if as_range else "v9", denylist=denylist_file)
    assert result.returncode == 1, result.stderr
    assert f"tag object {tag_sha[:12]}: tagger name matches {WORD_ENTRY}" in result.stderr
    assert f"tag object {tag_sha[:12]}: message line 3 matches {SUBSTRING_ENTRY}" in result.stderr
    _assert_no_echo(result)


def test_a_clean_annotated_tag_passes(repo: Repo, denylist_file: Path) -> None:
    base, head = _commit_range(repo)
    repo.git("tag", "-a", "v9", "-m", "release", head)
    result = _guard(repo, "--commits", f"{base}..v9", denylist=denylist_file)
    assert result.returncode == 0, result.stderr
    assert "1 commit(s) and 1 annotated tag(s) clean" in result.stdout


def test_a_clean_range_passes(repo: Repo, denylist_file: Path) -> None:
    base, head = _commit_range(repo)
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_commits_rejects_a_revision_that_looks_like_an_option(repo: Repo, denylist_file: Path) -> None:
    _commit_range(repo)
    result = _guard(repo, "--commits", "--output=/tmp/x", denylist=denylist_file)
    assert result.returncode == 2


def test_an_unknown_revision_is_an_error_not_a_pass(repo: Repo, denylist_file: Path) -> None:
    _commit_range(repo)
    result = _guard(repo, "--commits", "0123456789abcdef0123456789abcdef01234567..HEAD", denylist=denylist_file)
    assert result.returncode == 2


def test_exclude_remote_leaves_out_what_the_remote_already_has(repo: Repo, denylist_file: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=repo.env, check=True, timeout=30)
    repo.write("a.txt", "a\n")
    repo.commit(f"already public {SUBSTRING}")
    repo.git("remote", "add", "pub", str(remote))
    repo.git("push", "-q", "pub", "main")
    repo.write("a.txt", "b\n")
    head = repo.commit("new and clean")
    assert _guard(repo, "--commits", head, "--exclude-remote", "pub", denylist=denylist_file).returncode == 0
    assert _guard(repo, "--commits", head, denylist=denylist_file).returncode == 1


# -- added lines in each commit ----------------------------------------------------------------


def _add_then_remove(repo: Repo, text: str, path: str = "notes.txt") -> tuple[str, str, str]:
    """A base commit, one that adds `text` as line 2 of `path`, and one that takes it out again:
    the tip's tree is clean, the history is not."""
    repo.write(path, "line one\nline three\n")
    base = repo.commit("base")
    repo.write(path, f"line one\n{text}\nline three\n")
    leak = repo.commit("add a line")
    repo.write(path, "line one\nline three\n")
    head = repo.commit("take it out again")
    return base, leak, head


def test_a_line_added_then_removed_is_caught_in_the_commit_that_added_it(repo: Repo, denylist_file: Path) -> None:
    base, leak, head = _add_then_remove(repo, f"written by {SUBSTRING}")
    # The tip is clean, so the tree scans alone pass...
    assert _guard(repo, denylist=denylist_file).returncode == 0
    assert _guard(repo, "--rev", head, denylist=denylist_file).returncode == 0
    # ...and the commit check reads the line the middle commit added.
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"commit {leak[:12]}: notes.txt:2: added line matches {SUBSTRING_ENTRY}" in result.stderr
    assert head[:12] not in result.stderr
    assert "FAILED, 1 hit(s) in 2 commit(s)" in result.stderr
    _assert_no_echo(result)


def test_counts_only_leaves_out_the_commit_path_and_field(repo: Repo, denylist_file: Path) -> None:
    base, leak, _head = _add_then_remove(repo, f"written by {SUBSTRING}")
    repo.write("README.md", "clean\n")
    named = repo.commit("change", GIT_AUTHOR_NAME=f"{WORD} Person")
    result = _guard(repo, "--counts-only", "--commits", f"{base}..{named}", denylist=denylist_file)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED, 2 hit(s) in 3 commit(s)" in result.stderr
    for detail in (leak[:12], named[:12], "notes.txt", "author name", "entry", "denylist line"):
        assert detail not in result.stderr
    _assert_no_echo(result)


def test_a_removed_line_is_not_a_hit(repo: Repo, denylist_file: Path) -> None:
    repo.write("notes.txt", f"old {WORD}\nkeep\n")
    base = repo.commit("base")
    repo.write("notes.txt", "keep\n")
    head = repo.commit("remove it")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_added_lines_are_numbered_as_in_the_commit_that_added_them(repo: Repo, denylist_file: Path) -> None:
    repo.write("a.txt", "1\n2\n3\n4\n5\n")
    base = repo.commit("base")
    repo.write("a.txt", f"1\n{WORD}\n2\n3\n4\nx {EMAIL_BIT}\n5\n")
    head = repo.commit("two additions")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {head[:12]}: a.txt:2: added line matches {WORD_ENTRY}" in result.stderr
    assert f"commit {head[:12]}: a.txt:6: added line matches {EMAIL_ENTRY}" in result.stderr
    _assert_no_echo(result)


def test_the_allow_marker_works_on_an_added_line(repo: Repo, denylist_file: Path) -> None:
    base, _, head = _add_then_remove(repo, f"a third-party {SUBSTRING} identity:allow")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_a_lone_revision_reads_the_root_commit_too(repo: Repo, denylist_file: Path) -> None:
    repo.write("first.txt", f"{WORD}\n")
    root = repo.commit("root")
    repo.write("first.txt", "clean\n")
    head = repo.commit("clean")
    result = _guard(repo, "--commits", head, denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {root[:12]}: first.txt:1: added line matches {WORD_ENTRY}" in result.stderr


def test_added_lines_outside_the_range_are_not_read(repo: Repo, denylist_file: Path) -> None:
    repo.write("a.txt", f"{SUBSTRING}\n")
    repo.commit("old")
    repo.write("a.txt", "clean\n")
    old = repo.commit("fixed")
    repo.write("b.txt", "new and clean\n")
    head = repo.commit("new")
    result = _guard(repo, "--commits", f"{old}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_exclude_remote_leaves_out_added_lines_the_remote_already_has(
    repo: Repo, denylist_file: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=repo.env, check=True, timeout=30)
    repo.write("a.txt", f"{SUBSTRING}\n")
    repo.commit("already public")
    repo.git("remote", "add", "pub", str(remote))
    repo.git("push", "-q", "pub", "main")
    repo.write("a.txt", "clean\n")
    head = repo.commit("clean")
    assert _guard(repo, "--commits", head, "--exclude-remote", "pub", denylist=denylist_file).returncode == 0
    assert _guard(repo, "--commits", head, denylist=denylist_file).returncode == 1


def test_an_added_path_that_matches_is_withheld(repo: Repo, denylist_file: Path) -> None:
    repo.write("keep.txt", "clean\n")
    base = repo.commit("base")
    repo.write("a.txt", "clean\n")
    repo.write(f"people/{SUBSTRING}.txt", f"about {WORD}\n")
    added = repo.commit("add")
    (repo.path / "people" / f"{SUBSTRING}.txt").unlink()
    head = repo.commit("remove")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    where = f"commit {added[:12]}: changed file #2 (path withheld)"
    assert f"{where}: its path matches {SUBSTRING_ENTRY}" in result.stderr
    assert f"{where}:1: added line matches {WORD_ENTRY}" in result.stderr
    # The deletion adds nothing and publishes no new path.
    assert head[:12] not in result.stderr
    _assert_no_echo(result)


def test_an_empty_added_file_is_still_checked_by_its_path(repo: Repo, denylist_file: Path) -> None:
    repo.write("keep.txt", "clean\n")
    base = repo.commit("base")
    repo.write(f"{WORD}.txt", "")
    head = repo.commit("empty file")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    assert f"commit {head[:12]}: changed file #1 (path withheld): its path matches {WORD_ENTRY}" in result.stderr
    _assert_no_echo(result)


@pytest.mark.parametrize("name", ["with space.txt", 'quote"d.txt', "tab\there.txt", "café.txt"])
def test_an_unusual_path_is_named_as_it_is(repo: Repo, denylist_file: Path, name: str) -> None:
    repo.write("keep.txt", "clean\n")
    base = repo.commit("base")
    repo.write(f"dir/{name}", f"{SUBSTRING}\n")
    head = repo.commit("add")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1, result.stderr
    assert f"commit {head[:12]}: dir/{name}:1: added line matches {SUBSTRING_ENTRY}" in result.stderr


def test_binary_content_added_in_a_commit_is_skipped(repo: Repo, denylist_file: Path) -> None:
    repo.write("keep.txt", "clean\n")
    base = repo.commit("base")
    repo.write("blob.bin", b"\x00\x01\n" + SUBSTRING.encode() + b"\n")
    head = repo.commit("binary")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 0, result.stderr


def test_an_attribute_cannot_hide_a_text_file(repo: Repo, denylist_file: Path) -> None:
    repo.write(".gitattributes", "* -diff\n")
    base = repo.commit("base")
    repo.write("a.txt", f"{WORD}\n")
    head = repo.commit("add")
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1
    assert f"a.txt:1: added line matches {WORD_ENTRY}" in result.stderr


def test_the_users_diff_config_does_not_change_what_is_read(repo: Repo, denylist_file: Path) -> None:
    for key, value in [
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.relative", "true"),
        ("diff.renames", "copies"),
        ("diff.context", "5"),
        ("color.ui", "always"),
        ("core.quotePath", "true"),
    ]:
        repo.git("config", key, value)
    repo.write("docs/deep/page.md", "clean\n")
    repo.write("top.txt", "clean\n")
    base = repo.commit("base")
    (repo.path / "top.txt").rename(repo.path / "moved.txt")
    repo.write("docs/deep/page.md", f"clean\n{WORD}\n")
    head = repo.commit("move and edit")
    result = subprocess.run(
        [sys.executable, str(GUARD), "--denylist-file", str(denylist_file), "--commits", f"{base}..{head}"],
        cwd=repo.path / "docs" / "deep",
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert f"commit {head[:12]}: docs/deep/page.md:2: added line matches {WORD_ENTRY}" in result.stderr
    assert "\x1b[" not in result.stderr


def test_the_users_blank_line_and_submodule_config_does_not_change_what_is_read(
    repo: Repo, denylist_file: Path
) -> None:
    for key, value in [
        ("diff.suppressBlankEmpty", "true"),
        ("diff.interHunkContext", "3"),
        ("diff.submodule", "log"),
        ("diff.ignoreSubmodules", "all"),
    ]:
        repo.git("config", key, value)
    repo.write("a.txt", "one\n\nthree\nfour\n")
    base = repo.commit("base")
    # Two hunks joined by a blank context line, and a gitlink whose path matches an entry.
    repo.write("a.txt", "ONE\n\nthree\nFOUR\n" + f"{WORD}\n")
    repo.git("add", "a.txt")
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{base},vendor/{SUBSTRING}")
    repo.git("commit", "-q", "-m", "edit and add a gitlink")
    head = repo.git("rev-parse", "HEAD").strip()
    result = _guard(repo, "--commits", f"{base}..{head}", denylist=denylist_file)
    assert result.returncode == 1, result.stderr
    assert f"commit {head[:12]}: a.txt:5: added line matches {WORD_ENTRY}" in result.stderr
    assert f"(path withheld): its path matches {SUBSTRING_ENTRY}" in result.stderr
    _assert_no_echo(result)


def test_a_merge_is_read_for_what_it_adds_itself(repo: Repo, denylist_file: Path) -> None:
    # main gets a line from elsewhere (already public: outside the range), a branch forks
    # before it, then merges main in and slips a new line into the merge itself.
    repo.write("a.txt", "one\ntwo\n")
    fork = repo.commit("base")
    repo.write("a.txt", f"one\ntwo\nfrom main {SUBSTRING}\n")
    main_tip = repo.commit("on main")
    repo.git("checkout", "-q", "-b", "topic", fork)
    repo.write("b.txt", "topic work\n")
    repo.commit("topic")
    repo.git("merge", "-q", "--no-commit", "main")
    repo.write("a.txt", f"one\ntwo\nfrom main {SUBSTRING}\nin the merge {WORD}\n")
    merge = repo.commit("merge main")
    result = _guard(repo, "--commits", f"{main_tip}..{merge}", denylist=denylist_file)
    assert result.returncode == 1, result.stderr
    assert f"commit {merge[:12]}: a.txt:4: added line matches {WORD_ENTRY}" in result.stderr
    assert SUBSTRING_ENTRY not in result.stderr
    assert "FAILED, 1 hit(s)" in result.stderr


def test_an_attribute_cannot_hide_what_a_merge_adds(repo: Repo, denylist_file: Path) -> None:
    repo.write(".gitattributes", "* -diff\n")
    repo.write("a.txt", "one\ntwo\n")
    fork = repo.commit("base")
    repo.write("a.txt", "one\ntwo\nmain\n")
    main_tip = repo.commit("on main")
    repo.git("checkout", "-q", "-b", "topic", fork)
    repo.write("a.txt", "one\ntwo\ntopic\n")
    repo.commit("topic")
    subprocess.run(["git", "merge", "-q", "main"], cwd=repo.path, env=repo.env, capture_output=True, timeout=30)
    repo.write("a.txt", f"one\ntwo\nmain\ntopic\n{WORD}\n")
    merge = repo.commit("merge main")
    repo.write("a.txt", "one\ntwo\nmain\ntopic\n")
    head = repo.commit("take it out")
    result = _guard(repo, "--commits", f"{main_tip}..{head}", denylist=denylist_file)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"commit {merge[:12]}: a.txt:5: added line matches {WORD_ENTRY}" in result.stderr


def test_the_added_lines_parser_reads_an_octopus_merge() -> None:
    guard = _import_guard()
    output = (
        "\x01" + "a" * 40 + "\n\n"
        "diff --cc x.txt\n"
        "index 1,2,3..4\n"
        "--- a/x.txt\n"
        "+++ b/x.txt\n"
        "@@@@ -1,1 -1,1 -1,1 +1,3 @@@@\n"
        "+++new everywhere\n"
        " + from one side\n"
        "-   gone\n"
        "+++also new\n"
    )
    shas, files = guard.parse_added_lines(output)
    assert shas == ["a" * 40]
    assert [(f.path, f.added) for f in files] == [("x.txt", [(1, "new everywhere"), (3, "also new")])]


def _import_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("identity_guard_under_test", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


def test_the_hook_stops_a_leak_that_a_later_commit_removed(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", "clean\n")
    repo.commit()
    assert _push(repo, denylist_file).returncode == 0
    repo.write("README.md", f"clean\n{EMAIL_BIT}\n")
    leak = repo.commit("add")
    repo.write("README.md", "clean\n")
    repo.commit("fix")
    result = _push(repo, denylist_file)
    assert result.returncode != 0
    assert f"commit {leak[:12]}: README.md:2: added line matches {EMAIL_ENTRY}" in result.stderr
    assert "push stopped" in result.stderr
    _assert_no_echo(result)


# -- the pre-push hook -------------------------------------------------------------------------


@pytest.fixture
def pushable(repo: Repo, tmp_path: Path) -> tuple[Repo, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=repo.env, check=True, timeout=30)
    repo.git("remote", "add", "pub", str(remote))
    hooks = repo.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").symlink_to(HOOK)
    # The hook runs the guard from the repository being pushed.
    (repo.path / "scripts").mkdir()
    (repo.path / "scripts" / "identity_guard.py").write_bytes(GUARD.read_bytes())
    return repo, remote


def _push(repo: Repo, denylist: Path | None, *refspec: str) -> subprocess.CompletedProcess[str]:
    env = dict(repo.env)
    if denylist is not None:
        env["LIKEARR_IDENTITY_DENYLIST_FILE"] = str(denylist)
    return subprocess.run(
        ["git", "push", "pub", *(refspec or ("main",))],
        cwd=repo.path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_hook_lets_a_clean_push_through(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", "clean\n")
    repo.commit()
    result = _push(repo, denylist_file)
    assert result.returncode == 0, result.stderr


def test_the_hook_stops_a_push_with_no_denylist(pushable: tuple[Repo, Path], tmp_path: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", "clean\n")
    repo.commit()
    # HOME is tmp_path/home, so the default location is empty too.
    result = _push(repo, tmp_path / "missing.txt")
    assert result.returncode != 0
    assert "no denylist" in result.stderr
    result = _push(repo, None)
    assert result.returncode != 0
    assert "no denylist" in result.stderr


def test_the_hook_reads_the_default_location(pushable: tuple[Repo, Path], tmp_path: Path) -> None:
    repo, _ = pushable
    default = tmp_path / "home" / ".config" / "likearr" / "identity-denylist.txt"
    default.parent.mkdir(parents=True)
    default.write_text(DENYLIST)
    repo.write("README.md", f"{WORD}\n")
    repo.commit()
    result = _push(repo, None)
    assert result.returncode != 0
    assert f"README.md:1: matches {WORD_ENTRY}" in result.stderr


def test_the_hook_scans_the_pushed_tree(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", f"{SUBSTRING}\n")
    repo.commit()
    repo.write("README.md", "fixed but not committed\n")
    result = _push(repo, denylist_file)
    assert result.returncode != 0
    assert f"README.md:1: matches {SUBSTRING_ENTRY}" in result.stderr
    assert "push stopped" in result.stderr
    _assert_no_echo(result)


def test_the_hook_checks_the_new_commits_only(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, remote = pushable
    repo.write("README.md", "clean\n")
    repo.commit()
    assert _push(repo, denylist_file).returncode == 0
    repo.write("README.md", "still clean\n")
    bad = repo.commit("tidy", GIT_AUTHOR_EMAIL=f"me@{EMAIL_BIT}")
    result = _push(repo, denylist_file)
    assert result.returncode != 0
    assert f"commit {bad[:12]}: author email matches {EMAIL_ENTRY}" in result.stderr
    _assert_no_echo(result)
    # Nothing reached the remote.
    remote_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=remote, env=repo.env, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    assert remote_head != bad


def test_the_hook_checks_every_commit_of_a_new_branch(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", "clean\n")
    first = repo.commit(f"by {WORD}")
    repo.write("README.md", "still clean\n")
    repo.commit("clean")
    result = _push(repo, denylist_file)
    assert result.returncode != 0
    assert f"commit {first[:12]}: message line 1 matches {WORD_ENTRY}" in result.stderr


def test_the_hook_ignores_a_branch_deletion(pushable: tuple[Repo, Path], denylist_file: Path) -> None:
    repo, _ = pushable
    repo.write("README.md", "clean\n")
    repo.commit()
    repo.git("branch", "extra")
    assert _push(repo, denylist_file, "main", "extra").returncode == 0
    result = _push(repo, denylist_file, "--delete", "extra")
    assert result.returncode == 0, result.stderr


# -- the CI workflow ---------------------------------------------------------------------------

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "identity-guard.yml"


def _commits_step_script() -> str:
    """The `run: |` block of the commits job's check step, dedented, read as text (no YAML parser
    in the dev extra)."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "name: Check commit authors" in line)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


def test_the_workflow_pins_actions_and_reads_only() -> None:
    text = WORKFLOW.read_text()
    uses = [line.split("uses:", 1)[1].strip() for line in text.splitlines() if "uses:" in line]
    assert uses
    for ref in uses:
        _action, _, rest = ref.partition("@")
        sha, _, comment = rest.partition(" # ")
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), ref
        assert comment.startswith("v"), ref
    assert "permissions:\n  contents: read\n" in text
    assert text.count("IDENTITY_DENYLIST: ${{ secrets.IDENTITY_DENYLIST }}") == 2
    assert "if: github.repository_owner == 'sysdad'" in text
    # The tree job runs everywhere: only the commits job is gated.
    assert text.count("if: github.repository_owner") == 1
    # CI logs are public: both jobs print only how many hits there are.
    assert text.count("scripts/identity_guard.py --counts-only") == 2
    assert text.index("  tree:") < text.index("  commits:") < text.index("if: github.repository_owner")


@pytest.mark.parametrize(
    ("event", "before", "expect_hit"),
    [
        ("pull_request", None, False),  # base..head: the bad root commit is on the base side
        ("push", "zeros", True),  # a first push checks everything reachable
        ("push", "base", False),  # before..after
        ("push", "unknown", True),  # a force push whose old tip is gone: everything reachable
    ],
)
def test_the_workflow_commit_range(
    repo: Repo, denylist_file: Path, tmp_path: Path, event: str, before: str | None, expect_hit: bool
) -> None:
    repo.write("a.txt", "a\n")
    base = repo.commit(f"root by {WORD}")
    repo.write("a.txt", "b\n")
    head = repo.commit("clean")
    (repo.path / "scripts").mkdir()
    (repo.path / "scripts" / "identity_guard.py").write_bytes(GUARD.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    env = dict(repo.env)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["IDENTITY_DENYLIST"] = denylist_file.read_text()
    env["EVENT_NAME"] = event
    env["PR_BASE"] = base if event == "pull_request" else ""
    env["PR_HEAD"] = head if event == "pull_request" else ""
    env["PUSH_BEFORE"] = {"zeros": "0" * 40, "base": base, "unknown": "1" * 40, None: ""}[before]
    env["PUSH_AFTER"] = head if event == "push" else ""
    result = subprocess.run(
        ["sh", "-c", _commits_step_script()],
        cwd=repo.path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if expect_hit:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "FAILED, 1 hit(s) in 2 commit(s)" in result.stderr
        assert base[:12] not in result.stderr and "entry" not in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 commit(s) clean" in result.stdout
    _assert_no_echo(result)


def test_the_workflow_catches_a_leak_a_later_commit_in_the_pr_removed(
    repo: Repo, denylist_file: Path, tmp_path: Path
) -> None:
    base, leak, head = _add_then_remove(repo, f"by {WORD}")
    (repo.path / "scripts").mkdir()
    (repo.path / "scripts" / "identity_guard.py").write_bytes(GUARD.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    env = dict(repo.env)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["IDENTITY_DENYLIST"] = denylist_file.read_text()
    env.update(EVENT_NAME="pull_request", PR_BASE=base, PR_HEAD=head, PUSH_BEFORE="", PUSH_AFTER="")
    result = subprocess.run(
        ["sh", "-c", _commits_step_script()],
        cwd=repo.path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED, 1 hit(s) in 2 commit(s)" in result.stderr
    assert leak[:12] not in result.stderr and "notes.txt" not in result.stderr
    _assert_no_echo(result)
