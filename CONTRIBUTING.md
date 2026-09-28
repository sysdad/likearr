# Contributing

## Dev setup

```bash
uv sync --extra dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Integration tests run against an empty, disposable Lidarr when `LIKEARR_TEST_LIDARR_URL`,
`LIKEARR_TEST_LIDARR_API_KEY` and `LIKEARR_TEST_LIDARR_ROOT` are all set. They add and delete
artists, and refuse to run against a Lidarr that already has any.

## Tests never contact Spotify

Use `FakeSource` from `tests/shell/conftest.py` instead of a real Spotify client. Real calls spend
the developer account's small, shared quota.

## README screenshots

Take them from a demo data directory, never a real library:

```bash
uv run python scripts/demo_state.py /tmp/likearr-demo
LIKEARR_UI_PASSWORD='a demo password, 16+ chars' LIKEARR_SPOTIFY_CLIENT_ID=demo \
  uv run likearr start -c /tmp/likearr-demo/config.toml --port 8770
```

Then run `uv run --with playwright python scripts/readme_screenshots.py <password file> <plan id>`,
which writes `docs/images/*.png`. Its docstring explains the arguments.

## Style

Plain, specific language in docs and UI text. Short dashes (`-`), not em dashes.

Docs and comments describe the project, not one install of it: no first person, and no counts,
dates or rulings from a particular deployment.

## Versioning

Releases are tagged `vX.Y.Z` (semantic versioning). Record user-facing changes under
`Unreleased` in `CHANGELOG.md`; they move under the new version's own heading when it's tagged.

## Pull requests

Open against `main`. Say what changed and why, link the issue it fixes, and see the PR template's
checklist for what needs to pass before review.

A new source (anything besides Spotify) starts as a feature request issue, not a PR: see the
README's [Scope](README.md#scope) section for what's not planned and what's possible later.
