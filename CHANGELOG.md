# Changelog

Notable changes to likearr are recorded here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/): newest first, grouped as `Added`,
`Changed`, `Fixed` and `Upgrade notes`.

0.5.0 is likearr's first public release. Its entry below is a short summary of what it's for, not
a list of every change that went into it; the detailed history is in the individual pull request
descriptions, not here. Future releases go back to the fuller `Added` / `Changed` / `Fixed` /
`Upgrade notes` style above.

## [Unreleased]

### Added

- A narrative lint in CI (`scripts/narrative_lint.py`) fails when a tracked file uses first-person
  voice in docs or comments, or wording that describes one install or one person rather than the
  project. The phrase list is `scripts/narrative_lint_phrases.txt`; `narrative:allow` on a line
  keeps a real third-party hit.

### Fixed

- The identity guard's commit check (the pre-push hook and CI) now also reads every line each
  pushed or pull-request commit added. A denylisted string added in one commit and removed in the
  next is caught, where before only the final tree was scanned and the string still reached the
  published history.
- With `[ui] public_url` set, Connect Spotify in Settings, and Clean up's "Authorize write access"
  button, did nothing in Chrome, Edge, Safari and other Chromium or WebKit browsers: the browser
  silently blocked the jump to Spotify. They now show a "Continue to Spotify" link that works in
  every browser.

### Changed

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
- The quick start now pastes a Docker Compose block and fetches the example config directly,
  instead of cloning the repository first just to copy three files out of it.
- The identity guard has a `--counts-only` option, and CI uses it: a failing check in CI now says
  how many hits there are, not which file, line, commit or denylist entry, since the CI logs of a
  public repository can be read by anyone. Run the guard locally with the list to see where.

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
