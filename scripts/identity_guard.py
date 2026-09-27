#!/usr/bin/env python3
"""Identity leak guard: fail when a tracked file or a commit carries an entry from a private denylist.

Stdlib only, so CI can run it with the runner's own `python3` and nothing installed. Not shipped:
the Docker image copies `likearr/` only. See `docs/dev/identity-guard.md` for the setup.

    IDENTITY_DENYLIST="$(cat list.txt)" python3 scripts/identity_guard.py
    python3 scripts/identity_guard.py --denylist-file list.txt
    python3 scripts/identity_guard.py --denylist-file list.txt --rev HEAD
    python3 scripts/identity_guard.py --denylist-file list.txt --commits origin/main..HEAD

The denylist comes from `--denylist-file`, or else from the `IDENTITY_DENYLIST` environment
variable (the list itself, not a path). One entry per line; blank lines and lines starting `#` are
ignored. A line starting `w:` is a whole-word match (no word character either side); any other line
is a substring match. Both are case-insensitive. A missing or empty list is a failure, never a pass.

Modes:

- default: every file `git ls-files` lists, read from the working tree;
- `--rev REV`: every file in the tree of commit REV, read from git's object store (what a push
  sends, whatever the working tree holds);
- `--commits REV...`: author name and email, committer name and email, and message of every commit
  the revisions select (`A..B`, `^A B`, or a lone `B` for everything reachable from it),
  every line each of those commits added (and the path of each file it added or changed), and the
  tagger and message of any annotated tag named as a tip. A leak added in one commit and removed
  in the next never reaches the tip's tree, but it is still in the history a push publishes. A
  merge's added lines are those new against every parent (git's combined diff).
  `--exclude-remote NAME` also leaves out commits already on that remote's tracking branches.

Binary files (a NUL byte in the first 8000 bytes, git's own test) are skipped, and so is a
commit's change to a file when a line it added holds a NUL byte. A line holding `identity:allow`
is skipped, for a real third-party false positive.

The output names where a hit is (a path and line, a commit and field, or a commit, path and line)
and WHICH entry matched, by its number, never the matched text or the entry: CI logs are readable
and the list is secret. A path that itself matches an entry is shown by its position instead (in
the file list, or in the commit's diff). `--counts-only` leaves out the where and the which, and
prints only how many hits there are: CI uses it, because a public repository's logs can be read by
anyone, and a red run there should not point at the line.

Exit status: 0 clean, 1 at least one hit, 2 the check could not run (no denylist, a git error).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "IDENTITY_DENYLIST"
ALLOW_MARKER = "identity:allow"
WORD_PREFIX = "w:"
BINARY_SNIFF_BYTES = 8000
_GITLINK_MODE = "160000"


class GuardError(Exception):
    """The check could not run. The message must never quote a denylist entry."""


@dataclass(frozen=True)
class Entry:
    number: int  # 1-based, counting entries only (not comments or blank lines)
    line: int  # 1-based line in the denylist
    pattern: re.Pattern[str]


def parse_denylist(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(WORD_PREFIX):
            word = line[len(WORD_PREFIX) :].strip()
            if not word:
                raise GuardError(f"denylist line {line_no} is a whole-word entry with no word")
            # Not `\b`: that needs a word character inside the entry's own edge, so `w:@handle`
            # or `w:J.` would never match. "Not next to another word character" works for both.
            pattern = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(line), re.IGNORECASE)
        entries.append(Entry(number=len(entries) + 1, line=line_no, pattern=pattern))
    return entries


def load_denylist(file_arg: str | None) -> list[Entry]:
    if file_arg is not None:
        path = Path(file_arg)
        if not path.is_file():
            raise GuardError(f"denylist file not found: {path}")
        text = path.read_text(encoding="utf-8")
        source = f"denylist file {path}"
    else:
        text = os.environ.get(ENV_VAR, "")
        source = f"${ENV_VAR}"
        if not text.strip():
            raise GuardError(
                f"{ENV_VAR} is unset or empty, and no --denylist-file was given. "
                "An empty denylist would pass everything, so this is a failure."
            )
    entries = parse_denylist(text)
    if not entries:
        raise GuardError(f"{source} has no entries (only blank lines or comments). This is a failure, not a pass.")
    return entries


def matching_entries(text: str, entries: Sequence[Entry]) -> list[Entry]:
    return [entry for entry in entries if entry.pattern.search(text)]


def _describe(entries: Iterable[Entry]) -> str:
    return ", ".join(f"entry {e.number} (denylist line {e.line})" for e in entries)


# -- git ---------------------------------------------------------------------------------------


def _git(args: Sequence[str], *, stdin: bytes | None = None, cwd: Path | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            input=stdin,
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"git {args[0]} could not run: {type(exc).__name__}") from None
    if result.returncode != 0:
        # git's stderr names revisions and paths, never file contents, so it is safe to show.
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result.stdout


def _cat_file_batch(object_ids: Sequence[str]) -> Iterator[tuple[str, str, bytes]]:
    """Yields (object id, type, raw content) for each id, in order, from one `git cat-file`."""
    if not object_ids:
        return
    out = _git(["cat-file", "--batch"], stdin="".join(f"{oid}\n" for oid in object_ids).encode())
    pos = 0
    for _ in object_ids:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].decode("ascii", "replace").split()
        if len(header) != 3:
            raise GuardError(f"git cat-file could not read object {header[0] if header else '?'}")
        oid, kind, size = header[0], header[1], int(header[2])
        start = header_end + 1
        yield oid, kind, out[start : start + size]
        pos = start + size + 1  # the content is followed by a newline


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


# -- scans -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedFile:
    index: int  # 1-based position in the file list
    path: str
    content: bytes | None  # None: not readable as a regular file (a submodule, a deleted file)


def _working_tree_files() -> Iterator[TrackedFile]:
    top = Path(_decode(_git(["rev-parse", "--show-toplevel"])).strip())
    # From the top, so a run from a subdirectory still lists every tracked file.
    # surrogateescape, not "replace": a path that isn't UTF-8 must still name the file on disk,
    # or its content would go unread.
    listing = _git(["ls-files", "-z"], cwd=top).decode("utf-8", "surrogateescape")
    paths = [p for p in listing.split("\0") if p]
    for index, rel in enumerate(paths, start=1):
        full = top / rel
        if full.is_symlink():
            # What git stores for a symlink is its target, so scan that and never follow it.
            content: bytes | None = os.readlink(full).encode("utf-8", "surrogateescape")
        elif full.is_file():
            content = full.read_bytes()
        else:
            content = None
        yield TrackedFile(index, rel, content)


def _revision_files(rev: str) -> Iterator[TrackedFile]:
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", rev])
    blobs: list[tuple[str, str]] = []  # (object id, path)
    gitlinks: list[str] = []
    for record in _decode(listing).split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, kind, oid = meta.split()
        if mode == _GITLINK_MODE or kind != "blob":
            gitlinks.append(path)
        else:
            blobs.append((oid, path))
    contents = {oid: data for oid, _kind, data in _cat_file_batch(sorted({oid for oid, _ in blobs}))}
    ordered = sorted([(path, contents[oid]) for oid, path in blobs] + [(path, None) for path in gitlinks])
    for index, (path, content) in enumerate(ordered, start=1):
        yield TrackedFile(index, path, content)


def scan_files(files: Iterable[TrackedFile], entries: Sequence[Entry]) -> list[str]:
    hits: list[str] = []
    for tracked in files:
        path_hits = matching_entries(tracked.path, entries)
        if path_hits:
            # The path itself would echo the entry, so it is never printed.
            where = f"tracked file #{tracked.index} (path withheld)"
            hits.append(f"{where}: its path matches {_describe(path_hits)}")
        else:
            where = tracked.path
        if tracked.content is None or _is_binary(tracked.content):
            continue
        for line_no, line in enumerate(_decode(tracked.content).splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            found = matching_entries(line, entries)
            if found:
                hits.append(f"{where}:{line_no}: matches {_describe(found)}")
    return hits


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str


_IDENT = re.compile(r"^(?P<name>.*?) ?<(?P<email>[^>]*)> \S+ [+-]\d{4}$")


def _split_ident(value: str) -> tuple[str, str]:
    match = _IDENT.match(value)
    if match is None:
        return value, ""
    return match["name"], match["email"]


def _headers(raw: bytes, keys: Sequence[str]) -> tuple[dict[str, str], str]:
    """The first value of each of `keys` in a commit or tag object's header, and its message."""
    header_bytes, _, message_bytes = raw.partition(b"\n\n")
    headers: dict[str, str] = {}
    for line in _decode(header_bytes).split("\n"):
        key, _, value = line.partition(" ")
        if key in keys and key not in headers:
            headers[key] = value
    return headers, _decode(message_bytes)


def parse_commit(sha: str, raw: bytes) -> Commit:
    idents, message = _headers(raw, ("author", "committer"))
    author_name, author_email = _split_ident(idents.get("author", ""))
    committer_name, committer_email = _split_ident(idents.get("committer", ""))
    return Commit(sha, author_name, author_email, committer_name, committer_email, message)


@dataclass(frozen=True)
class Tag:
    sha: str
    tagger_name: str
    tagger_email: str
    message: str
    target: str  # the object the tag points at, which may itself be a tag


def parse_tag(sha: str, raw: bytes) -> Tag:
    headers, message = _headers(raw, ("object", "tagger"))
    tagger_name, tagger_email = _split_ident(headers.get("tagger", ""))
    return Tag(sha, tagger_name, tagger_email, message, headers.get("object", ""))


def _selection(revs: Sequence[str], exclude_remote: str | None) -> list[str]:
    """The revision arguments that select the commits to check, for `rev-list` and `log` alike."""
    for rev in revs:
        if rev.startswith("-"):
            raise GuardError("a --commits revision may not start with '-'")
    args: list[str] = []
    if exclude_remote:
        # `--not` flips the sense of what follows, so this excludes that remote's tracking
        # branches and then flips back for the revisions themselves.
        args += ["--not", f"--remotes={exclude_remote}", "--not"]
    return [*args, "--end-of-options", *revs]


def _commits(revs: Sequence[str], exclude_remote: str | None) -> list[Commit]:
    shas = _decode(_git(["rev-list", *_selection(revs, exclude_remote)])).split()
    commits: list[Commit] = []
    for sha, kind, raw in _cat_file_batch(shas):
        if kind != "commit":
            raise GuardError(f"{sha} is a {kind}, not a commit")
        commits.append(parse_commit(sha, raw))
    return commits


def _annotated_tags(revs: Sequence[str]) -> list[Tag]:
    """The annotated tags among the revisions' tips (and any tag they point at in turn): a pushed
    release tag's tagger and message are as public as a commit's author and message."""
    names = _decode(_git(["rev-parse", "--revs-only", "--end-of-options", *revs])).split()
    pending = [name for name in names if not name.startswith("^")]
    tags: list[Tag] = []
    seen: set[str] = set()
    while pending:
        targets: list[str] = []
        for sha, kind, raw in _cat_file_batch(pending):
            if kind == "tag" and sha not in seen:
                seen.add(sha)
                tag = parse_tag(sha, raw)
                tags.append(tag)
                if tag.target:
                    targets.append(tag.target)
        pending = targets
    return tags


def _scan_record(label: str, idents: Iterable[tuple[str, str]], message: str, entries: Sequence[Entry]) -> list[str]:
    hits: list[str] = []
    for field, value in idents:
        found = matching_entries(value, entries)
        if found:
            hits.append(f"{label}: {field} matches {_describe(found)}")
    for line_no, line in enumerate(message.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        found = matching_entries(line, entries)
        if found:
            hits.append(f"{label}: message line {line_no} matches {_describe(found)}")
    return hits


def scan_commits(commits: Iterable[Commit], entries: Sequence[Entry]) -> list[str]:
    hits: list[str] = []
    for commit in commits:
        idents = (
            ("author name", commit.author_name),
            ("author email", commit.author_email),
            ("committer name", commit.committer_name),
            ("committer email", commit.committer_email),
        )
        hits += _scan_record(f"commit {commit.sha[:12]}", idents, commit.message, entries)
    return hits


def scan_tags(tags: Iterable[Tag], entries: Sequence[Entry]) -> list[str]:
    hits: list[str] = []
    for tag in tags:
        idents = (("tagger name", tag.tagger_name), ("tagger email", tag.tagger_email))
        hits += _scan_record(f"tag object {tag.sha[:12]}", idents, tag.message, entries)
    return hits


# -- added lines -------------------------------------------------------------------------------
#
# A line added in one commit and removed in the next never reaches the tip's tree, but it is in
# history, and history is published with the push. So `--commits` also reads what each commit in
# the range added: one `git log -p -U0` over the same selection, added lines only.

_COMMIT_MARK = "\x01"  # starts each commit's record; no diff line can start with it
_HUNK = re.compile(r"^(@{2,}) (?:-\S+ )+\+(\d+)(?:,\d+)? @{2,}")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


@dataclass
class ChangedFile:
    sha: str
    index: int  # 1-based position in this commit's diff
    path: str
    deleted: bool = False
    added: list[tuple[int, str]] = dataclasses.field(default_factory=list)  # (line in the commit's version, text)


def _unquote(token: str) -> str:
    """Undoes git's C-style quoting of a path (`"a\\tb"`, `"\\303\\251"`), or returns it as is."""
    if not (len(token) >= 2 and token.startswith('"') and token.endswith('"')):
        return token
    out = bytearray()
    body = token[1:-1]
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\" or i + 1 == len(body):
            out += char.encode("utf-8", "surrogateescape")
            i += 1
        elif body[i + 1] in _C_ESCAPES:
            out.append(_C_ESCAPES[body[i + 1]])
            i += 2
        else:
            digits = body[i + 1 : i + 4]
            if len(digits) != 3 or any(d not in "01234567" for d in digits):
                raise GuardError("git log printed a quoted path the guard could not read")
            out.append(int(digits, 8) & 0xFF)
            i += 4
    return _decode(bytes(out))


def _diff_header_path(line: str) -> str:
    """The path a `diff --git a/P b/P`, `diff --combined P` or `diff --cc P` header names.
    Renames are off, so the two sides of a `--git` header are always the same path."""
    for plain in ("diff --combined ", "diff --cc "):
        if line.startswith(plain):
            return _unquote(line[len(plain) :])
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        # `"a/P" "b/P"`: two quoted tokens of the same length.
        path = _unquote(rest[: (len(rest) - 1) // 2])
        if path.startswith("a/"):
            return path[2:]
    else:
        path = rest[2 : 2 + (len(rest) - 5) // 2]
        if rest == f"a/{path} b/{path}":
            return path
    raise GuardError("git log printed a diff header the guard could not read")


def parse_added_lines(log_output: str) -> tuple[list[str], list[ChangedFile]]:
    """The commits `_added_lines_log` printed, and every file they changed with the lines each
    added (numbered as in that commit's version of the file).

    A merge's diff is git's combined diff, and only a line new against every parent counts as
    added: a line one side brought in is checked in that side's own commit, or was already public.
    """
    shas: list[str] = []
    files: list[ChangedFile] = []
    current: ChangedFile | None = None
    index = 0  # files so far in this commit's diff
    columns = 1
    next_line = 0
    in_hunk = False
    for line in log_output.split("\n"):
        if line.startswith(_COMMIT_MARK):
            shas.append(line[1:].strip())
            current, index, in_hunk = None, 0, False
        elif line.startswith(("diff --git ", "diff --combined ", "diff --cc ")):
            if not shas:
                raise GuardError("git log printed a diff before any commit")
            index += 1
            current, in_hunk = ChangedFile(shas[-1], index, _diff_header_path(line)), False
            files.append(current)
        elif current is None:
            continue
        elif line.startswith("@"):
            match = _HUNK.match(line)
            if match is None:
                raise GuardError("git log printed a hunk header the guard could not read")
            columns, next_line, in_hunk = len(match[1]) - 1, int(match[2]), True
        elif not in_hunk:
            if line.startswith("deleted file mode"):
                current.deleted = True
        else:
            prefix = line[:columns]
            if len(prefix) < columns or set(prefix) - {" ", "+"}:
                continue  # a removed line, or "\ No newline at end of file"
            if prefix == "+" * columns:
                current.added.append((next_line, line[columns:]))
            next_line += 1
    return shas, files


def _added_lines_log(revs: Sequence[str], exclude_remote: str | None) -> str:
    # Every option that shapes the output is fixed here, whatever the user's config: no renames
    # (so a header's two paths are the same), no colour, no external diff or textconv, full paths
    # from the top whatever the directory, every gitlink as its own `diff --git` (so its path is
    # read), and a blank context line kept as a space (so the lines after it keep their numbers).
    # No attribute can hide a file: `--text` covers an ordinary commit, but a merge's combined
    # diff ignores it, so attributes are read from an empty tree and no attributes file. What is
    # left is git's own binary test (a NUL byte in the first 8000 bytes), the tree scan's rule.
    empty_tree = _decode(_git(["hash-object", "-t", "tree", "--stdin"], stdin=b"")).strip()
    out = _git(
        [
            "--attr-source",
            empty_tree,
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.quotePath=false",
            "-c",
            "log.showSignature=false",
            "-c",
            "diff.suppressBlankEmpty=false",
            "log",
            f"--format=format:{_COMMIT_MARK}%H",
            "--patch",
            "--unified=0",
            "--text",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-relative",
            "--submodule=short",
            "--ignore-submodules=none",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--diff-merges=combined",
            "--root",
            *_selection(revs, exclude_remote),
        ]
    )
    return _decode(out)


def scan_added_lines(files: Iterable[ChangedFile], entries: Sequence[Entry]) -> list[str]:
    hits: list[str] = []
    for changed in files:
        label = f"commit {changed.sha[:12]}"
        where = changed.path
        path_hits = matching_entries(changed.path, entries)
        if path_hits:
            # The path itself would echo the entry, so it is never printed.
            where = f"changed file #{changed.index} (path withheld)"
            if not changed.deleted:
                hits.append(f"{label}: {where}: its path matches {_describe(path_hits)}")
        if any("\0" in text for _, text in changed.added):
            continue  # binary content, skipped as the tree scan skips it
        for line_no, text in changed.added:
            if ALLOW_MARKER in text:
                continue
            found = matching_entries(text, entries)
            if found:
                hits.append(f"{label}: {where}:{line_no}: added line matches {_describe(found)}")
    return hits


def added_line_hits(
    revs: Sequence[str], exclude_remote: str | None, commits: Sequence[Commit], entries: Sequence[Entry]
) -> list[str]:
    shas, files = parse_added_lines(_added_lines_log(revs, exclude_remote))
    # Fail closed: the log must have read exactly the commits the metadata check read.
    if sorted(shas) != sorted(commit.sha for commit in commits):
        raise GuardError("git log and git rev-list selected different commits")
    return scan_added_lines(files, entries)


# -- entry point -------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail when a tracked file or a commit carries an entry from a private denylist.",
    )
    parser.add_argument(
        "--denylist-file",
        metavar="PATH",
        help=f"read the denylist from this file instead of ${ENV_VAR}",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rev", metavar="REV", help="scan the tree of this commit instead of the working tree")
    mode.add_argument(
        "--commits",
        metavar="REV",
        nargs="+",
        help="check each selected commit's author, committer, message and added lines",
    )
    parser.add_argument(
        "--exclude-remote",
        metavar="NAME",
        help="with --commits: leave out commits already on this remote's tracking branches",
    )
    parser.add_argument(
        "--counts-only",
        action="store_true",
        help="on a hit, print only how many hits there are, not where or which entry (for public CI logs)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.exclude_remote and not args.commits:
            raise GuardError("--exclude-remote only applies to --commits")
        entries = load_denylist(args.denylist_file)
        if args.commits:
            commits = _commits(args.commits, args.exclude_remote)
            tags = _annotated_tags(args.commits)
            hits = scan_commits(commits, entries) + scan_tags(tags, entries)
            hits += added_line_hits(args.commits, args.exclude_remote, commits, entries)
            scope = f"{len(commits)} commit(s)" + (f" and {len(tags)} annotated tag(s)" if tags else "")
        else:
            files = list(_revision_files(args.rev) if args.rev else _working_tree_files())
            hits = scan_files(files, entries)
            scope = f"{len(files)} tracked file(s)" + (f" at {args.rev}" if args.rev else "")
    except GuardError as exc:
        sys.stderr.write(f"identity guard: cannot run: {exc}\n")
        return 2
    except (OSError, UnicodeError) as exc:
        # An unreadable file or list. The exception's own text would name a path, which could
        # itself be an entry, so only its type is shown. Exit 2, not a traceback's 1 (a "hit").
        sys.stderr.write(f"identity guard: cannot run: a file could not be read ({type(exc).__name__})\n")
        return 2
    if not args.counts_only:
        for hit in hits:
            sys.stderr.write(f"identity guard: {hit}\n")
    if hits:
        where = "Run the guard locally with the denylist file to see where. " if args.counts_only else ""
        sys.stderr.write(
            f"identity guard: FAILED, {len(hits)} hit(s) in {scope} against {len(entries)} entries. "
            f"{where}Remove them, or mark a real third-party false positive with '{ALLOW_MARKER}' on that line.\n"
        )
        return 1
    sys.stdout.write(f"identity guard: ok, {scope} clean against {len(entries)} entries.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
