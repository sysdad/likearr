# Contributing

## Dev setup

```bash
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

The resolver ships with a golden corpus of public MusicBrainz releases in `tests/fixtures/mb/`.
Integration tests run against an empty, disposable Lidarr container when `LIKEARR_TEST_LIDARR_URL`,
`LIKEARR_TEST_LIDARR_API_KEY` and `LIKEARR_TEST_LIDARR_ROOT` are all set. The instance must have
no artists when the session starts - the tests add artists and delete them again, and fail the
session immediately with a clear message if the instance already has a library. In CI,
`.github/workflows/integration.yml` provides that container and runs them nightly, on manual
dispatch, and on PRs that touch the Lidarr adapter, the apply loop or the integration tests
themselves; it is advisory and never blocks a merge.

## Tests never contact Spotify

Use `FakeSource` from `tests/shell/conftest.py` instead of a real Spotify client in any test.
The Dev Mode quota an app runs under is shared across everything using that client id and it's
small - a burst of ~700 calls from an ad-hoc script has exhausted it before. A test suite that
called Spotify for real would burn through it fast and leave a developer's own dev environment
unable to run a real check.

## README screenshots

The screenshots come from a demo data directory, never a real library. `uv run python
scripts/demo_state.py /tmp/likearr-demo` writes one by running the real plan and apply code against
the test fakes (no Spotify, MusicBrainz or Lidarr call). Then start it with
`LIKEARR_UI_PASSWORD='a demo password, 16+ chars' LIKEARR_SPOTIFY_CLIENT_ID=demo uv run likearr
start -c /tmp/likearr-demo/config.toml --port 8770` and screenshot the pages the script lists. Its
times are relative to when it ran, so regenerate it right before taking them, and don't click
anything that starts a job.

`scripts/readme_screenshots.py` drives that server with Playwright and writes the three
`docs/images/*.png` files itself: put the demo password in a file (never on the command line) and
run `uv run --with playwright python scripts/readme_screenshots.py <password file> <plan id>` -
see the script's own docstring for the plan id and the one-time browser install.

## Style

Plain, specific language in docs and UI text. Short dashes (`-`), not em dashes.

Docs and comments describe the project, not one install of it: no first person, and no counts,
dates or rulings from a particular deployment. CI's narrative lint checks this against
`scripts/narrative_lint_phrases.txt`; run it with `python3 scripts/narrative_lint.py`. A real
third-party hit, such as a quoted album title, is kept with `narrative:allow` on that line.

## Versioning

Releases are tagged `vX.Y.Z` (semantic versioning). Record user-facing changes under
`Unreleased` in `CHANGELOG.md`; they move under the new version's own heading when it's tagged.

## Pull requests

Open against `main`. Say what changed and why, link the issue it fixes, and see the PR template's
checklist for what needs to pass before review.

A fork PR's identity-guard check always fails - that's expected, not a hit, and not something to
fix on your end; see `docs/dev/identity-guard.md` for why.

A new source (anything besides Spotify) starts as a feature request issue, not a PR: see the
README's [Scope](README.md#scope) section for what's not planned and what's possible later.
