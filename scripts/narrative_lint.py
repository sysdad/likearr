#!/usr/bin/env python3
"""Narrative lint: fail when a tracked text file carries a voice or provenance phrase.

Stdlib only, so CI can run it with the runner's own `python3` and nothing installed. Not shipped:
the Docker image copies `likearr/` only.

    python3 scripts/narrative_lint.py
    python3 scripts/narrative_lint.py --phrases path/to/list.txt

The docs and comments describe the project, not one install of it or one person's view of it.
The rules live in `scripts/narrative_lint_phrases.txt` (or `--phrases`); its header explains the
three kinds of line. In short:

- a plain phrase is matched case-insensitively as a whole phrase on every line of every tracked
  text file;
- `re:` is a regular expression, matched the same way;
- `voice:` is a regular expression for first-person voice, matched only in prose: Markdown outside
  fenced code, and comments and docstrings in code (Python `#` comments and bare string
  statements; `#` comments in YAML, TOML, shell and similar files; `<!-- -->` and `{# #}` in
  HTML templates; `/* */` and `//` in JavaScript and CSS). Code, string literals and data files
  are never read for voice, so an identifier or a third-party title in test data is not a hit.
  Inside prose, text in double quotes or backticks is left out, since a quotation is someone
  else's voice. A quotation that runs onto the next line is followed until a blank line.

A line holding `narrative:allow` is skipped, for a real third-party hit. Binary files (a NUL byte
in the first 8000 bytes) and minified `.min.js` files (for voice) are skipped, and so is the rules
file itself.

The output is `path:line: "phrase"` for each hit, with the rule as written in the rules file and
its line there. The phrases are public, so they are printed.

Exit status: 0 clean, 1 at least one hit, 2 the check could not run (no rules, a git error, a
Python file that does not parse).
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "narrative:allow"
DEFAULT_RULES = Path("scripts") / "narrative_lint_phrases.txt"
BINARY_SNIFF_BYTES = 8000

HASH_COMMENT_SUFFIXES = {".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".example", ".conf"}
HASH_COMMENT_NAMES = {"Dockerfile", ".gitignore", ".dockerignore", ".gitleaksignore", ".gitattributes"}


class LintError(Exception):
    """The check could not run."""


@dataclass(frozen=True)
class Rule:
    line: int  # 1-based line in the rules file
    text: str  # as written there, for the output
    pattern: re.Pattern[str]
    voice: bool  # matched in prose only


def parse_rules(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        voice = line.startswith("voice:")
        try:
            if voice or line.startswith("re:"):
                source = line.split(":", 1)[1]
                if not source:
                    raise LintError(f"rules line {line_no} has an empty pattern")
                pattern = re.compile(source)
            else:
                words = [re.escape(word) for word in line.split()]
                pattern = re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)
        except re.error as exc:
            raise LintError(f"rules line {line_no} is not a valid regular expression: {exc}") from None
        rules.append(Rule(line=line_no, text=line, pattern=pattern, voice=voice))
    return rules


def load_rules(path: Path) -> list[Rule]:
    if not path.is_file():
        raise LintError(f"rules file not found: {path}")
    rules = parse_rules(path.read_text(encoding="utf-8"))
    if not rules:
        raise LintError(f"rules file {path} has no rules. An empty list would pass everything.")
    return rules


# -- prose ---------------------------------------------------------------------------------------
#
# Each extractor returns (line number, prose text on that line) for the prose parts of a file.


# A fence line: its run of backticks or tildes, then the rest (an info string on an opener).
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _markdown_prose(lines: Sequence[str]) -> list[tuple[int, str]]:
    """Every line outside fenced code. As in CommonMark, a fence closes only on a bare run of the
    same character at least as long as the one that opened it, so a "```python" line inside a
    "````" block is code, not the end of the block."""
    prose: list[tuple[int, str]] = []
    fence: str | None = None  # the run that opened the current block
    for line_no, line in enumerate(lines, start=1):
        match = _FENCE.match(line)
        if fence is None:
            # A backtick opener's info string cannot hold a backtick: "```a``` b" is a code span.
            if match and not (match[1][0] == "`" and "`" in match[2]):
                fence = match[1]
            else:
                prose.append((line_no, line))
        elif match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
            fence = None
    return prose


def _python_prose(text: str, lines: Sequence[str]) -> list[tuple[int, str]]:
    prose: dict[int, str] = {}
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError, ValueError, RecursionError) as exc:
        raise LintError(f"a Python file does not parse ({type(exc).__name__})") from None
    for node in ast.walk(tree):
        # A bare string statement: a docstring, or an attribute's documentation. Its own quotes
        # are taken off, so they don't read as a quotation around the whole text.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            segment = _source_segment(lines, node.value)
            match = _STRING_OPEN.match(segment)
            if match:
                quote = match[1]
                segment = segment[match.end() :]
                if segment.endswith(quote):
                    segment = segment[: -len(quote)]
            for offset, part in enumerate(segment.split("\n")):
                prose[node.lineno + offset] = part
    for token in tokens:
        if token.type == tokenize.COMMENT:
            line_no = token.start[0]
            prose[line_no] = (prose[line_no] + " " + token.string) if line_no in prose else token.string
    return sorted(prose.items())


def _source_segment(lines: Sequence[str], node: ast.expr) -> str:
    """`ast.get_source_segment` from lines already split, which it would re-split on every call.
    The column offsets are UTF-8 byte offsets."""
    first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    start, end = node.col_offset, node.end_col_offset
    if first == last:
        return lines[first].encode()[start:end].decode("utf-8", "replace")
    head = lines[first].encode()[start:].decode("utf-8", "replace")
    tail = lines[last].encode()[:end].decode("utf-8", "replace")
    return "\n".join([head, *lines[first + 1 : last], tail])


_STRING_OPEN = re.compile(r"^[rRbBuUfF]*(\"\"\"|'''|\"|')")
_HASH_COMMENT = re.compile(r"(?:^|\s)#")
# `//` starts a comment only at the start of a line or after whitespace, as `#` does above, so
# the `//` of a URL in a string literal ("https://...") is not read as one.
_SLASH_COMMENT = re.compile(r"(?<!\S)//")


def _hash_comment_prose(lines: Sequence[str]) -> list[tuple[int, str]]:
    prose: list[tuple[int, str]] = []
    for line_no, line in enumerate(lines, start=1):
        match = _HASH_COMMENT.search(line)
        if match:
            prose.append((line_no, line[match.end() :]))
    return prose


def _block_comment_prose(
    lines: Sequence[str], opener: str, closer: str, line_comment: re.Pattern[str] | None
) -> list[tuple[int, str]]:
    """Comments delimited by `opener` and `closer` (which may span lines), and, if given, a
    `line_comment` match to the end of the line."""
    prose: list[tuple[int, str]] = []
    inside = False
    for line_no, line in enumerate(lines, start=1):
        parts: list[str] = []
        pos = 0
        while pos <= len(line):
            if inside:
                end = line.find(closer, pos)
                if end < 0:
                    parts.append(line[pos:])
                    break
                parts.append(line[pos:end])
                pos = end + len(closer)
                inside = False
                continue
            start = line.find(opener, pos)
            single = line_comment.search(line, pos) if line_comment else None
            if single and (start < 0 or single.start() < start):
                parts.append(line[single.end() :])
                break
            if start < 0:
                break
            pos = start + len(opener)
            inside = True
        if parts:
            prose.append((line_no, " ".join(parts)))
    return prose


def _html_prose(lines: Sequence[str]) -> list[tuple[int, str]]:
    merged: dict[int, str] = {}
    for opener, closer in (("<!--", "-->"), ("{#", "#}")):
        for line_no, text in _block_comment_prose(lines, opener, closer, None):
            merged[line_no] = (merged[line_no] + " " + text) if line_no in merged else text
    return sorted(merged.items())


def _has_shell_shebang(lines: Sequence[str]) -> bool:
    return bool(lines) and lines[0].startswith("#!") and ("sh" in lines[0] or "python" in lines[0])


def prose_lines(path: str, text: str) -> list[tuple[int, str]]:
    """The prose on each line of a file, by its type. Empty for a type that holds no prose."""
    lines = text.split("\n")
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix == ".md":
        return _markdown_prose(lines)
    if suffix == ".py":
        return _python_prose(text, lines)
    if suffix == ".html":
        return _html_prose(lines)
    if name.endswith(".min.js"):
        return []
    if suffix == ".js":
        return _block_comment_prose(lines, "/*", "*/", _SLASH_COMMENT)
    if suffix == ".css":
        return _block_comment_prose(lines, "/*", "*/", None)
    if suffix in HASH_COMMENT_SUFFIXES or name in HASH_COMMENT_NAMES or _has_shell_shebang(lines):
        return _hash_comment_prose(lines)
    return []


# Quotation marks: straight double quotes toggle; curly ones open and close.
_QUOTE_TOGGLES = {'"', "`"}


def without_quotations(prose: Iterable[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    """Each prose line with the text inside double quotes and backticks blanked out. A quotation
    left open at the end of a line carries onto the next prose line, until a blank line or a gap
    in the line numbers (the end of that comment or paragraph). Triple quotes (a docstring's
    delimiters) are not quotations."""
    open_mark: str | None = None
    previous = 0
    for line_no, text in prose:
        if line_no != previous + 1 or not text.strip():
            open_mark = None
        previous = line_no
        text = text.replace('"""', "   ").replace("'''", "   ")
        out: list[str] = []
        for char in text:
            if open_mark is None:
                if char in _QUOTE_TOGGLES:
                    open_mark = char
                elif char == "“":
                    open_mark = "”"
                out.append(char if open_mark is None else " ")
            else:
                if char == open_mark:
                    open_mark = None
                out.append(" ")
        yield line_no, "".join(out)


# -- the scan ------------------------------------------------------------------------------------


def _git(args: Sequence[str], cwd: Path | None = None) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LintError(f"git {args[0]} could not run: {type(exc).__name__}") from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise LintError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result.stdout


def scan_text(path: str, text: str, rules: Sequence[Rule]) -> list[str]:
    # One line numbering for every rule: Python's parser also ends a line at a lone `\r`, so
    # without this its line numbers run past `text.split("\n")`.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    found: set[tuple[int, Rule]] = set()
    whole = [rule for rule in rules if not rule.voice]
    voice = [rule for rule in rules if rule.voice]
    lines = text.split("\n")
    for line_no, line in enumerate(lines, start=1):
        if ALLOW_MARKER not in line:
            found |= {(line_no, rule) for rule in whole if rule.pattern.search(line)}
    if voice:
        for line_no, prose in without_quotations(prose_lines(path, text)):
            if ALLOW_MARKER not in lines[line_no - 1]:
                found |= {(line_no, rule) for rule in voice if rule.pattern.search(prose)}
    ordered = sorted(found, key=lambda hit: (hit[0], hit[1].line))
    return [f'{path}:{line_no}: "{rule.text}" (rules line {rule.line})' for line_no, rule in ordered]


def scan_repository(top: Path, rules: Sequence[Rule], skip: set[Path]) -> tuple[int, list[str]]:
    listing = _git(["ls-files", "-z"], cwd=top).decode("utf-8", "surrogateescape")
    paths = [p for p in listing.split("\0") if p]
    hits: list[str] = []
    scanned = 0
    for rel in paths:
        full = top / rel
        if full.resolve() in skip or full.is_symlink() or not full.is_file():
            continue
        data = full.read_bytes()
        if b"\0" in data[:BINARY_SNIFF_BYTES]:
            continue
        scanned += 1
        try:
            hits += scan_text(rel, data.decode("utf-8", "replace"), rules)
        except LintError as exc:
            raise LintError(f"{rel}: {exc}") from None
    return scanned, hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a tracked text file carries a voice or provenance phrase.")
    parser.add_argument("--phrases", metavar="PATH", help=f"the rules file (default: {DEFAULT_RULES} at the top)")
    args = parser.parse_args(argv)
    try:
        top = Path(_git(["rev-parse", "--show-toplevel"]).decode("utf-8", "surrogateescape").strip())
        rules_path = Path(args.phrases) if args.phrases else top / DEFAULT_RULES
        rules = load_rules(rules_path)
        # The rules in use and the default list both hold every phrase they name.
        scanned, hits = scan_repository(top, rules, {rules_path.resolve(), (top / DEFAULT_RULES).resolve()})
    except LintError as exc:
        sys.stderr.write(f"narrative lint: cannot run: {exc}\n")
        return 2
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"narrative lint: cannot run: a file could not be read ({type(exc).__name__}: {exc})\n")
        return 2
    for hit in hits:
        sys.stderr.write(f"narrative lint: {hit}\n")
    if hits:
        sys.stderr.write(
            f"narrative lint: FAILED, {len(hits)} hit(s) in {scanned} tracked text file(s). Reword them to describe "
            f"the project rather than one install or one person, or mark a real third-party hit with "
            f"'{ALLOW_MARKER}' on that line.\n"
        )
        return 1
    sys.stdout.write(f"narrative lint: ok, {scanned} tracked text file(s) clean against {len(rules)} rules.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
