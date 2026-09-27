# Changelog

Notable changes to likearr are recorded here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/): newest first, grouped as `Breaking`,
`Added`, `Changed`, `Fixed` and `Upgrade notes`.

0.5.0 is likearr's first public release. Its entry below is a short summary of what it's for, not
a list of every change that went into it; the detailed history is in the individual pull request
descriptions, not here. Future releases go back to the fuller `Added` / `Changed` / `Fixed` /
`Upgrade notes` style above.

## [Unreleased]

## [0.5.1]

### Breaking

Read this before upgrading from 0.5.0. A `config.toml` that isn't changed as below does not load,
and the service does not start.

- **Three settings moved from `config.toml` to the environment.** Where likearr runs and how it is
  reached now come only from environment variables, and a `config.toml` that still has any of the
  three old keys fails to load, naming the variable to set instead:

  | Old key in `config.toml` | New environment variable                                        |
  | ------------------------ | --------------------------------------------------------------- |
  | `[lidarr] url`           | `LIKEARR_LIDARR_URL` (required)                                 |
  | `[ui] allowed_hosts`     | `LIKEARR_ALLOWED_HOSTS` (optional, comma-separated)             |
  | `[musicbrainz] contact`  | `LIKEARR_MUSICBRAINZ_CONTACT` (optional; project URL if unset)  |

  Before, in `config.toml`:

  ```toml
  [lidarr]
  url = "http://lidarr:8686"

  [ui]
  allowed_hosts = ["likearr.example.org", "192.168.1.20"]

  [musicbrainz]
  contact = "you@example.org"
  ```

  After, in `compose.yaml` (or `.env`), on **every** likearr service, including `likearr-cli`:

  ```yaml
  services:
    likearr:
      image: ghcr.io/sysdad/likearr:0.5.1
      environment:
        LIKEARR_LIDARR_URL: "http://lidarr:8686"
        LIKEARR_ALLOWED_HOSTS: "likearr.example.org,192.168.1.20"
        LIKEARR_MUSICBRAINZ_CONTACT: "you@example.org"
  ```

  Then delete those three lines (and any section left empty) from `config.toml`. Back it up first.

- **New default for allowed hosts.** With `LIKEARR_ALLOWED_HOSTS` unset, the web UI answers to
  loopback (`localhost`, `127.0.0.1`) and to any IPv4 address, such as
  `http://192.168.1.20:8770`, and refuses every host name. You must set `LIKEARR_ALLOWED_HOSTS` if
  you reach likearr by a host name: behind a reverse proxy, at a `[ui] public_url`, or by a LAN
  DNS name. Once set, only the listed names and addresses (plus loopback) are accepted, so also
  list any IP address you still browse to.

- **The first run re-resolves everything.** `RESOLVER_VERSION` is now 12, so every cached
  MusicBrainz answer is recomputed on the first run after upgrading. Expect that run, or the first
  Check for changes, to take noticeably longer and to make many more MusicBrainz requests. Only
  answers reached through the Lidarr album-search fallback can change (see Fixed).

### Added

- A narrative lint in CI (`scripts/narrative_lint.py`) fails when a tracked file uses first-person
  voice in docs or comments, or wording that describes one install or one person rather than the
  project. The phrase list is `scripts/narrative_lint_phrases.txt`; `narrative:allow` on a line
  keeps a real third-party hit.

### Changed

- With `[ui] public_url` set and likearr opened at that address, Connect Spotify in Settings, and
  Clean up's "Authorize write access" button, now go straight to Spotify in one click. Those
  two pages, and only those, allow forms to lead to `https://accounts.spotify.com` and the
  `public_url` address. Opened at any other address, the "Continue to Spotify" link stays.
  Paste-back mode is unchanged.

- The docs, example files, comments and tests now use generic or invented examples in place of
  details from one install, and the Settings pause-reason box suggests "e.g. away this week".
- `doctor` no longer creates the run lock file on a fresh install. The file now appears only
  when a command that takes the run lock, such as `run`, first runs.
- A followed artist that MusicBrainz has not linked to their Spotify page, and whose name several
  MusicBrainz artists share, is no longer matched to the top search result. likearr leaves the
  artist unmatched and lists every candidate in the run summary, instead of adding a stranger and
  monitoring their albums. To settle it, link the right artist to their Spotify page on
  MusicBrainz (the next run picks it up), or add the right artist in Lidarr by hand. A namesake an
  earlier version added stays in Lidarr with what it monitored, and is not removed automatically.
- Two different artists with the same name that are both new in one run are no longer both added
  to Lidarr. Both are skipped and reported as a name collision, the same as when Lidarr already
  holds one of them.
- A release kept by hand at adoption stays kept once a followed artist, a saved album or a like
  also wants it. Before, the source's reason replaced the kept-by-hand one, and unfollowing or
  unliking later unmonitored the release. A release on the adopt keep list that a source already
  wanted is now kept by hand as well, not only claimed, and the adopt plan lists it as
  `claim+keep`.
- Refusing one release of a followed artist ("Not this one"), or tagging them albums-only, no
  longer reads as that artist's catalogue shrinking. Before, the artist-shrink guard then held
  every scheduled run until a shrink was accepted by hand; the refused release is now let go on
  the next run.
- Deploy docs no longer tell an existing library's owner to run `adopt` first "so likearr doesn't
  try to unmonitor things it never touched" - that's backwards. A plain run already leaves
  hand-monitored releases alone; `adopt` is only for a library that grew from Lidarr's own import
  lists.
- Contributing, security and packaging docs now agree that likearr has a tagged release:
  releases are `vX.Y.Z`, only the latest is supported, and the project is Beta.
- New-source feature requests go to an issue now, not a GitHub Discussion (which is disabled).
  Contributing docs also now say a fork PR's identity-guard check always fails and isn't something
  the contributor can fix - the PR is checked by hand against the real list before merging.
- Deploy docs and the example templates no longer describe one-time upgrade steps or private
  tracking numbers left over from before the first public release.
- The quick start now covers what an unwritable `/data` directory looks like (an unhealthy
  container, a failed Connect or Settings save) and the `chown` fix, instead of leaving that only
  in the deploy guide.
- The README, `docs/spotify.md` and the deploy guide now lead with "a household with several
  Spotify accounts is supported" (one instance per person, sharing one Spotify app and Lidarr)
  instead of reading like a single account is all that works, with a new README section and a
  compose example for the second instance.
- The quick start now pastes a Docker Compose block instead of cloning the repository first just
  to copy three files out of it.
- The identity guard has a `--counts-only` option, and CI uses it: a failing check in CI now says
  how many hits there are, not which file, line, commit or denylist entry, since the CI logs of a
  public repository can be read by anyone. Run the guard locally with the list to see where.
- `docker compose up -d` with only environment variables and an empty `/data` is now a complete
  install (breaking; see Breaking above). Where likearr runs and how it is reached come only from the environment:
  `LIKEARR_LIDARR_URL` (required), `LIKEARR_ALLOWED_HOSTS` (optional, comma-separated) and
  `LIKEARR_MUSICBRAINZ_CONTACT` (optional; likearr's project URL by default). `[lidarr] url`,
  `[ui] allowed_hosts` and `[musicbrainz] contact` are no longer read from `config.toml`, and a
  file that still has any of them fails to load, naming the variable to set instead. On a first
  start with no `config.toml`, `likearr start` writes one from `deploy/config.example.toml`,
  comments intact, and never overwrites an existing file. `[spotify] token_file` and `[state] db`
  default to `spotify-token.json` and `state.sqlite` beside `config.toml`. `[lidarr] root_folder`
  and `quality_profile` may now be left unset: Settings, under Lidarr setup, picks them from
  Lidarr's own lists (one root folder is taken by itself), and until both are set, Status and
  Doctor say so and every run refuses. With `LIKEARR_ALLOWED_HOSTS` unset, the web UI answers to
  loopback and any IPv4 address, and refuses every host name; set, it answers only to the listed
  names and addresses, as before. The README quick start no longer fetches or edits a config file.

### Fixed

- When MusicBrainz missed an album and likearr fell back to Lidarr's album search, an artist or
  title written wholly in a non-Latin script (Japanese, Korean, Cyrillic, Greek and others) was
  compared as an empty string. A same-titled album by a different non-Latin artist then matched,
  and a run could add that artist and monitor the album. The fallback now compares names the same
  way the MusicBrainz search does, in any script. `RESOLVER_VERSION` is now 12, so every cached
  answer is recomputed on the first run after upgrading; only answers reached through the Lidarr
  album-search fallback can change, and a wrong one becomes unmapped.
- `adopt` no longer plans to unmonitor every hand-monitored album of a followed artist whose
  MusicBrainz catalogue could not be read, because it is too large to browse or because
  MusicBrainz failed for that artist during the plan. Their albums on the keep list, or wanted
  by a source, are still kept or claimed as usual; the rest are now held back, left monitored and
  unowned, and the plan lists them as held with the reason, both in its printed output and in a
  new `held` field of the plan file. The plan's existing fields are unchanged.
  When MusicBrainz failed during the plan, a warning at the top of the output says the plan is
  incomplete and suggests re-running `adopt` later. (#6)
- An artist that a plan adds, but that someone else added to Lidarr first (by hand or through an
  import list, between the plan and the apply), is no longer recorded as added by likearr. Before,
  every later run forced that artist's "Monitor New Albums" to None. The apply now leaves such an
  artist alone and logs why; one that carries likearr's tag is still recorded as likearr's own, as
  after a run that stopped between adding it and recording it. A reviewed plan is now also refused
  as stale when an artist it adds has appeared in Lidarr since it was made. A plan made by an
  earlier version stays valid as long as none of its artists to add has appeared. (#4)
- The live Lidarr integration tests (`tests/integration/test_lidarr_live.py`) no longer delete an
  artist they did not add, or default to an instance's existing root folder. The session now
  fails immediately, with a clear message, unless the instance has no artists when it starts;
  `LIKEARR_TEST_LIDARR_ROOT` is required and never inferred from the instance's own root folders;
  and teardown deletes only the artist ids the tests themselves added.
- The ambiguous-artist explanation ("Nothing of theirs is monitored.") no longer claims nothing is
  monitored when releases for that artist can in fact still be monitored - by an earlier likearr
  version that resolved the follow before this check existed, or by hand, a like, a saved album or
  a playlist. The wording now holds either way.
- The Status page and the Not added page rebuilt a set of monitored releases for every wanted
  release, so `coverage()` slowed down quadratically with the library's size (a few seconds at
  several thousand wanted releases). The set is now built once.
- A Lidarr API key ending in a carriage return or line feed, as a Windows-line-ending env file or a
  Kubernetes Secret created from a file leaves it, made every Lidarr request fail and printed the
  whole key in the error, which then reached `doctor`, the run's error, the webhook and the MQTT
  message. `LIKEARR_LIDARR_API_KEY`, `LIKEARR_SPOTIFY_CLIENT_ID` and
  `LIKEARR_SPOTIFY_CLIENT_SECRET` are now stripped of surrounding whitespace when read, and a value
  that is empty after stripping counts as unset. `LIKEARR_UI_PASSWORD` is still taken exactly as
  given. Secret redaction now also catches a value's escaped form (`\n` written as two characters),
  so such a key is never printed even when it reaches a request some other way.
- The identity guard's commit check (the pre-push hook and CI) now also reads every line each
  pushed or pull-request commit added. A denylisted string added in one commit and removed in the
  next is caught, where before only the final tree was scanned and the string still reached the
  published history.
- With `[ui] public_url` set, Connect Spotify in Settings, and Clean up's "Authorize write access"
  button, did nothing in Chrome, Edge, Safari and other Chromium or WebKit browsers: the browser
  silently blocked the jump to Spotify. They now show a "Continue to Spotify" link that works in
  every browser.

## [0.5.0]

- Mirrors one Spotify account's follows, saved albums and Liked Songs into one Lidarr instance,
  keeping monitoring in sync as you follow, save and like things, and reversing it when you
  unfollow or unlike - built and tested for one library, one Spotify account, one Lidarr.
- Every plan is reviewable before it applies, and a fresh install's schedule waits for a first
  hand-reviewed apply; guards hold a run back for a look when something about it seems off, such
  as a short Spotify read or an unusually large unmonitor.
- Spotify access is read-only by default. The two write scopes (following artists, saving albums)
  are only requested if you opt into the separate promote-save feature.
- Never deletes a library file. The optional Clean up feature, off by default, moves files to a
  holding folder and removes the Lidarr artist row; it never deletes anything from disk.
- Never searches or downloads; that stays with Lidarr and your indexers. An already-released
  album still needs Lidarr's own missing-album search, or a companion tool, to fetch it.
- Spotify is the only source today; other services and several accounts sharing one instance are
  named as future work, not silently unsupported.
- Config is checked at load, and every problem is named rather than silently ignored; a setting
  the web UI or scheduler can't run safely stops them from starting. A Settings save keeps
  config.toml and its backups out of reach of other users on the host.
- One self-hosted service (web UI, scheduler and every run, all in one container) with a CLI
  underneath for first-time setup, Lidarr setup and by-hand work.
- Docker is the supported install; a published, version-pinned image is the default, with a
  source build kept as an alternative.
- Status and Doctor surface what needs attention, such as a Spotify re-authorization coming due
  or a degraded run, before it turns into a support question.
