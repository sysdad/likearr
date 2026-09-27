# Identity guard

`scripts/identity_guard.py` fails when a tracked file, or a commit's author, committer, message or
added lines, carries an entry from a private denylist. It runs in CI
(`.github/workflows/identity-guard.yml`) and as a local pre-push hook (`scripts/pre-push`). It
prints where a hit is and which entry number matched, never the entry or the text that matched, so
CI logs are safe to read.

## Install the pre-push hook

From the repository root:

```bash
ln -sf "$PWD/scripts/pre-push" "$(git rev-parse --git-path hooks)/pre-push"
```

The hook reads the denylist from `$LIKEARR_IDENTITY_DENYLIST_FILE`, or else
`~/.config/likearr/identity-denylist.txt`, and stops the push when neither exists. For each ref
pushed it scans the tree of the commit being pushed (not the working tree), and checks every commit
the remote doesn't already have, and the tagger and message of an annotated tag being pushed.

## Added lines

The commit check (`--commits`, used by the hook and by CI's `commits` job) also reads every line
each selected commit added, and the path of every file it added or changed. A line added in one
commit and removed in the next never reaches the tip's tree, but it is still in the history a push
publishes, and in the pull request ref GitHub keeps. A hit names the commit, the path and the line
number in that commit's version of the file:

```
identity guard: commit 0123456789ab: docs/notes.md:12: added line matches entry 3 (denylist line 5)
```

Removed lines are not read. For a merge, only a line new against every parent counts (git's
combined diff): a line one side brought in is read in that side's own commit, or was already
public. A commit's change to a file is skipped when a line it added holds a NUL byte (binary
content, as the tree scan skips it). A path that matches an entry is withheld and shown as its
position in that commit's diff.

## The denylist

One entry per line. Blank lines and lines starting `#` are ignored.

- `w:word` matches `word` as a whole word: `w:Qux` catches "Qux" and "Qux's", not "Quxly". "Whole"
  means no letter, digit or `_` either side, so `w:@qux` works too.
- Any other line matches as a substring.
- Both are case-insensitive.

An empty or missing list is a failure, never a pass. In CI the list is the `IDENTITY_DENYLIST`
repository secret (the list itself, not a path); both checks fail where it isn't set, including
on pull requests from forks and Dependabot, which don't get repository secrets (Dependabot needs
its own copy under Dependabot secrets). A fork PR's CI check failing here is therefore expected,
not a sign of a hit, and re-running it can't change the result. It also isn't something the
contributor can work around: the denylist is private, so a fork PR has no way to run this check at
all, with the real list, before it's merged - "Run it by hand" below only helps with a copy of the
list, which nobody outside the project holds. Someone in the project who holds the list runs the
real check by hand against the merge commit before merging; CI does not do it for a fork PR.

## A false positive

A real third-party name or string that happens to match: put `identity:allow` on the same line
(in a comment, in a commit message line). It suppresses that line only.

## Run it by hand

```bash
python3 scripts/identity_guard.py --denylist-file LIST                  # tracked files, working tree
python3 scripts/identity_guard.py --denylist-file LIST --rev HEAD       # the tree of a commit
python3 scripts/identity_guard.py --denylist-file LIST --commits origin/main..HEAD
```

Exit status: 0 clean, 1 a hit, 2 the check couldn't run (no list, a git error).

## What it doesn't cover

The tree scan reads one tree: the working tree, the pushed tip, or the CI checkout; history is
the commit check's job. That check reads the commits of a push or a pull request, not the history
already on `main`, which was checked when it was pushed (its added lines only since this check
began reading them). A merge's own change to a binary file (a NUL byte in its first 8000 bytes) is
not read, as the tree scan skips it; no `.gitattributes` setting changes what is read. CI's
`commits` job runs only where the repository owner is the one the workflow names. CI runs on
pull requests and pushes to `main`, not on tag pushes, so a tag's tagger and message are checked
by the pre-push hook only.
