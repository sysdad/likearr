# Deploying likearr

likearr is one long-running service, `likearr start`: the web UI, the scheduler and every run
(each as a child process of the service) live in a single container. `docker compose up -d` and a
browser is the whole install; there is nothing to run from host cron. The CLI itself is still
there underneath - every hand command (`run`, `auth`, `doctor`, `adopt`, `prune-stage`,
`promote-save`, and so on) is the same binary the service shells out to - and `likearr-cli` (the
one-shot container, see "Web UI" below) is how you run those by hand. See "Scheduling" for how the
service's own schedule replaces host cron.

Every command shown below is written in its bare form (`likearr ... -c /data/config.toml`). Under
Compose, run it as `docker compose run --rm likearr-cli ...` instead - for example,
`docker compose run --rm likearr-cli auth --manual -c /data/config.toml`.

See `docs/dev/DESIGN.md` for what it does and why. This doc is only about getting it installed and
scheduled safely.

## Install

Docker Compose is the preferred, supported install - see below. A plain `docker run` (or
`docker build`) and a from-source build also work, further down. There is no PyPI package.

### Docker Compose

No clone needed. The [Quick start](../README.md#quick-start) pastes a single-service block with
its secrets inline (or in an `.env` file) and nothing else - copy
[`deploy/compose.example.yaml`](../deploy/compose.example.yaml) to `compose.yaml` instead once you
want more: the `likearr-cli` service (hand commands - "First run", "Sanity check: doctor" and
"Lidarr setup", below, all need it), a commented-out second-instance template (see "Running more
than one instance"), and secrets in an `.env` file by default. Either way, the `image:` line pulls
the published, version-pinned image the first time you run `docker compose up -d likearr` (or `up
-d`, for a file with just the one service), and it reads `.env` (if used) and `./likearr-data` from
the same directory as `compose.yaml`. Upgrade by editing the tag in `compose.yaml` and running
`docker compose up -d`; roll back the same way, to an older tag. Building it yourself instead:
comment out each service's `image:` line and uncomment the `build:` block below it, then
`git clone` the repo and upgrade with `git pull && docker compose up -d --build`.

Everything persistent (config, Spotify token, state database) lives under `/data`, a declared
volume, and `/data` is also the container's working directory, so the default outputs (`diff.json`,
`adopt.json`, `prune.json`, `promote-save.json`) land there without an `--out`. See
`deploy/env.example` / `deploy/config.example.toml` for the files it expects in that volume.

**The data directory must be writable by whichever uid ends up running the container.** A NAS uid
is rarely 1000: Synology starts at 1026, Unraid uses 99, TrueNAS apps use 568, and Docker on a
root-run host (Proxmox LXC, Unraid) auto-creates a missing bind mount as `root:root 755`, which
1000 can't write either. The first thing that write matters for is the Spotify token, then a
settings save - both come back as a 500 in the browser, and the container itself reads unhealthy
(`/healthz` answers 503) before anything else looks broken. Fastest fix, before the first `docker
compose up -d`: `chown -R 1000:1000 likearr-data` (or whatever uid you built the image with - see
below) so the default image can write it right away. To have the container run as that uid
instead of chowning the directory: pass `LIKEARR_UID` / `LIKEARR_GID` as build args (`docker build
--build-arg LIKEARR_UID=1026 --build-arg LIKEARR_GID=100 ...`, or uncomment `build.args` in
`deploy/compose.example.yaml`) matching the data directory's owner. A no-rebuild `user:` override
on the default image would be more convenient, but it isn't documented here yet: the image's code
and venv are root-owned and world-readable, so any uid can run them, but what `HOME` falls back to
for an arbitrary uid with no passwd entry needs a live check before it is recommended.

### Docker, without Compose

Pull the published image:

```
docker pull ghcr.io/sysdad/likearr:0.5.0
```

Pin the version tag rather than `:latest`, so an upgrade is something you choose (bump the tag)
rather than something that happens under you. GHCR keeps every published version, so rolling back
is the same move in reverse - set the tag back to the older version and pull again.

Prefer building it yourself? That still works, from a clone:

```
docker build -t likearr:local .
```

Either way, the image is a multi-stage build on `python:3.12-slim` with no compilers in the final
layer. It runs as a non-root user (uid 1000 by default; a source build's uid is overridable with
`--build-arg LIKEARR_UID=...` / `LIKEARR_GID=...` if you need to match a host directory's
ownership - see "Docker Compose", above, for the writability requirement).

The app version is baked into the installed package (`pyproject.toml`'s `version`) and
needs no build arg. The commit does: `.dockerignore` excludes `.git`, so without a build arg
`likearr doctor` and the web footer show no commit. Pass one at build time so they do:

```
docker build --build-arg VCS_REF=$(git rev-parse --short HEAD) -t likearr:local .
```

`VERSION` is also accepted, for the `org.opencontainers.image.version` label only (`docker inspect`,
not the running app) - useful when building from a tag or a `git describe` rather than the
checked-out commit.

### From source (bare metal)

```
uv tool install git+https://github.com/sysdad/likearr
```

or, from a clone:

```
git clone https://github.com/sysdad/likearr && cd likearr
uv sync
```

Needs Python 3.12+. A bare `uv venv` can still pick an older interpreter it finds first (3.11 on
one test machine) - pass `uv venv --python 3.12` if you build a venv by hand. Either route puts a
`likearr` binary on your PATH (`uv tool install`) or in `.venv/bin` (`uv sync`). You'll still need
a config file and a place for the state database - see below.

### Mounting the library for `prune-stage`

Only Clean up needs this, and Clean up is off by default: skip this section unless you turn it on
(`[prune] enabled = true`, or Settings > Advanced).

Every other command only talks to Lidarr's API. `prune-stage` moves files, and it moves them by
the paths Lidarr reports, i.e. under `lidarr.root_folder` as *Lidarr* sees it. The likearr
container therefore has to see the library at that same path. If Lidarr's root folder is
`/data/media/music` and the host keeps it at `/path/to/media`, add

```
volumes:
  - ./likearr-data:/data
  - /path/to/media:/data/media      # the library, at Lidarr's path
```

(or pass `-v /path/to/media:/data/media` to `docker compose run`). Put the holding directory
next to the root folder on the same filesystem, e.g. `--holding /data/media/_likearr-holding`,
so every move is a rename: thousands of files move in minutes that way, even on a network mount.
A holding directory *inside* the root folder is refused, whether the path says so or gets there
through `..` or a symlink. Before it lists a single move, `prune-stage` - the dry run as well as
`--apply` - checks the mount: the root folder is visible at Lidarr's path; the holding folder is
on the same filesystem and, where `/proc/self/mountinfo` can tell, on the same mount (two bind
mounts of one filesystem share a device number, yet a rename between them fails); and every file
Lidarr lists is there, at the size Lidarr lists. A wrong mount is refused by the preview with what
to mount. The web UI's own preview has no library mount and says so (`--no-mount-check`, refused
with `--apply`); the terminal preview is the one that checks.

`--apply` renames each file and never copies: should a rename still fail across mounts (EXDEV), the
stage stops at that file rather than copying hundreds of GB. Each move is recorded as it happens,
in `moves.jsonl` in the day's holding folder (flushed to disk per file), and `manifest.json` gathers
the day's moves when the stage ends or stops (an error, a full disk, Ctrl-C). A stage that stops
part-way leaves an exact record, removes only an artist all of whose files moved, and has the
others that lost a file rescanned. Run the same command again to move the rest: files an earlier
stage moved are recognised from the journal and left alone, and the artists it finishes are
removed then.

`[prune] enabled` (default `false`) turns Clean up on in the web UI, and with it the promote-save
write-access box in Settings; Settings > Advanced sets it. The prune commands run from a terminal
either way, and print one warning line while it is off. `[prune] holding_dir` sets the holding
folder the web UI's commands use (default:
`_likearr-holding` beside the root folder; absolute, no `..`), and `[ui] cli_command` the way a
terminal runs likearr on this host (default: `docker compose run --rm likearr-cli`), so the commands
Clean up shows paste and run as they are. The prefix must run the likearr-cli image with the library
mounted and pass the command through unchanged - not through something that re-parses it, like
`ssh host '...'`.

## Configuration

Copy `deploy/config.example.toml` to `config.toml` (in your data directory) and fill in your
Lidarr URL, root folder, quality profile, and MusicBrainz contact. Every key is commented in the
example file.

Copy `deploy/env.example` to `.env` (or wherever your process manager reads env vars from) and
fill in `LIKEARR_LIDARR_API_KEY`, `LIKEARR_SPOTIFY_CLIENT_ID` and `LIKEARR_UI_PASSWORD` (the
service refuses to start without the last one, or with one shorter than 16 characters;
`openssl rand -base64 24` makes one). Secrets never go in the TOML
file - only in environment variables. Don't commit the filled-in `.env`.

## First run: authenticate with Spotify

**From the browser, once `likearr start` is running:** open Settings and press
"Connect Spotify". By default this is the paste-back flow below, run for you: you approve on
Spotify's site, it redirects to a page that fails to load (expected - it's the loopback address
only your Spotify app knows about), and you paste that address bar's contents back into the field
Settings shows. The exchange happens on the server; the token file is written the same way `auth
--manual` writes it, atomically at `0600`, under the same lock. If `[ui] public_url` is set to an
https address (below), there is no copy/paste: Spotify sends you straight back to
`<public_url>/spotify/callback`, which must also be registered as a redirect URI in the Spotify
developer app. Opened at the `public_url` address itself, "Connect Spotify" goes straight to
Spotify in one click. Opened at any other address (a LAN address, or likearr's own port), Settings
shows a "Continue to Spotify" link to click instead: from there, some browsers would block the
redirect back to `public_url`. Either way, Settings then shows the
granted scopes and the new authorization date, and Status's re-authorize warning clears.

Signing in is read-only by default: likearr asks Spotify to read your follows, library and
playlists, and nothing more. Only `promote-save` writes to Spotify, and its write access is
opt-in: tick "Also let promote-save follow artists and save albums" in Settings before you
connect, or run `likearr auth --manual --promote-save`. See
[below](#when-likearrs-scopes-change-you-re-authorize) for what a re-authorization keeps.

**Haven't created the Spotify app itself yet?** See
[`docs/spotify.md`](spotify.md) for the numbered steps, including which redirect URI to
register and what Spotify's Development Mode limits mean for likearr.

**From a terminal**, the same flow, run by hand:

```
likearr auth --manual -c /data/config.toml
```

`--manual` prints a URL instead of trying to open a browser and catch the redirect itself - useful
when you're running this on a headless box (a container, a server with no browser). Steps:

1. Run the command on any machine - your laptop is fine, it doesn't need to be the machine that
   will run likearr day to day.
2. Open the printed URL, log into Spotify, approve access.
3. Paste the redirect URL you land on back into the prompt.
4. This writes the token file (`spotify.token_file` in config.toml, e.g. `spotify-token.json`) to
   wherever you ran the command.
5. Copy that token file to the `/data` directory (or wherever `token_file` points) on the machine
   that will actually run likearr, and set its permissions to `0600` - it's a long-lived
   credential:

   ```
   chmod 600 /data/spotify-token.json
   ```

likearr refreshes the token itself on subsequent runs; you only do this once by hand or in the
browser (or again if the token is ever revoked).

### When likearr's scopes change, you re-authorize

Spotify grants scopes at the consent screen, and refreshing a token never widens them. So when a
likearr release asks for a scope your stored token does not carry, the *only* fix is to
re-authorize: from Settings ("Re-authorize Spotify"), or `likearr auth --manual` again followed by
copying the new token file over. Nothing re-authorizes itself, and nothing should.

`promote-save` needs `user-follow-modify` and `user-library-modify` to follow artists and save
albums, and a sign-in asks for them only on opt-in. If your token doesn't have them (a read-only
sign-in, or one from before they existed), every read command keeps working unchanged, and
`promote-save` refuses up front - naming the missing scopes and the fix - rather than failing
halfway through a write. The fix asks for write access too:

```
likearr auth --manual --promote-save -c /data/config.toml
```

or, in Settings, tick the promote-save box before "Re-authorize Spotify" (with `[ui] public_url`
set, Clean up's finish page also has a button for it).

A plain re-authorization keeps what you have: if the token it replaces already has the write
scopes, it asks for them again, so Spotify's six-monthly re-auth never quietly drops access you
approved. Otherwise it stays read-only.

## Sanity check: doctor

```
likearr doctor -c /data/config.toml
```

Checks config parses, the Spotify token is valid, the schema canary passes, Lidarr is reachable
and reports a version, the root folder and profiles exist, no two Lidarr artists share a name
(a **FAIL**: Lidarr cannot match an import by name while they do), and MusicBrainz is reachable.
Writes nothing. `--no-spotify` skips every Spotify check. If Spotify answers `QUOTA_EXCEEDED`,
doctor prints one `FAIL  spotify quota` line with Spotify's `Retry-After`, asks Spotify nothing
more, and marks the Spotify checks it did not run as `SKIP`; every `run` aborts with zero
unmonitors until the quota recovers. Run this after any config change and whenever something
looks off before digging further. The Doctor section of Settings in the browser runs the
same checks read-only and lists them pass/warn/fail; it works even before the first run.

## Lidarr setup: profiles, tag, root folder

`likearr` needs the Lean and Full metadata profiles, the `likearr` tag, and the root folder
configured to default new artists to monitoring nothing. From Settings, "Preview
Lidarr setup" shows exactly what is missing or would change - reusing the same planning `doctor`
and `setup-profiles` use, never a second implementation - and "Apply", behind a second confirm,
runs `setup-profiles --apply` as a child job. It is idempotent (nothing offered when everything
already matches), and it never edits an existing metadata profile that differs from what likearr
expects - the preview says so, and names exactly what applying would still do (create whatever
really is missing) without touching a profile that already exists under that name. The root
folder is not covered by that promise: likearr owns its monitor defaults, so if one already
exists with different defaults, "Apply" resets them to monitor none / new items none - the
preview says so too, and it is the same thing `setup-profiles --apply` has always done from a
terminal. The equivalent from a terminal:

```
likearr setup-profiles -c /data/config.toml           # show what is missing
likearr setup-profiles --apply -c /data/config.toml    # create it
```

## First dry run

```
likearr run -c /data/config.toml
```

Dry-run is the default - nothing is written to Lidarr. This reads your sources, resolves releases
against MusicBrainz, and writes `diff.json` (or wherever `--out` points) describing what a real
run would add, monitor, and unmonitor, plus a printed summary.

The first run reads every song through MusicBrainz at 1 request per second, so with an empty cache
it can take several hours for a few thousand songs.
Later runs take minutes, since a song already resolved stays cached. Cancelling or restarting keeps
what it has already looked up, so let it finish. Applying costs more too: each new artist waits on
a Lidarr RefreshArtist, typically about 2 minutes on a real catalogue. likearr waits for it up to
300 s plus 2 s per release group, capped at an hour (`[lidarr] refresh_timeout_s` and friends). See ["Running it"](#running-it)
for what that means for `stop_grace_period`.

**Read `diff.json` before applying it**, especially the first time:

- `add_artists` - artists likearr would add to Lidarr.
- `monitor_artists` - unmonitored Lidarr artists that hold a wanted release and would be re-monitored;
  Lidarr never searches an album whose artist is unmonitored.
- `set_new_items_none` - Lidarr artists whose "Monitor New Albums" would be set to None, so their
  new albums are monitored only when you like them. Only artists holding a release likearr owns,
  including one whose release this plan claims, whose profile it widens or which it re-monitors (see "A followed
  artist's new album" below).
- `refresh_artists` - followed artists with a release MusicBrainz lists and Lidarr's catalogue does
  not hold yet, which likearr would ask Lidarr to refresh. Each one is a `RefreshArtist` command
  likearr waits on, scoped to that artist's folder. The release is monitored on the *next* run.
- `monitor` - releases it would start monitoring, with the reason each one is wanted.
- `unmonitor` - releases it would stop monitoring. Empty on a genuinely first run, since likearr
  owns nothing yet.
- `guards` - every guard that fired, with the code and how many unmonitors it blocked. Most block
  unmonitors (`source-shrink`, `artist-shrink`, `schema`, `scheduled-cap`); `name-collision` and
  `projected-wanted` block none and are there to be read.
- `name_collisions` - artists that were *not* added because Lidarr already holds their name under
  a different MBID, with both MBIDs and the releases the skip cost.
- `pending` / `unmapped` - liked singles waiting for their album, and anything the resolver
  couldn't map at all, including anything you opted out of (below).

### Reading a dry-run after turning an opt-out on

`[rules] allow_compilation_fallback`, `allow_remix_releases`, `keep_remix_only_tracks` and `deny_releases` (see
`docs/dev/DESIGN.md`) are the one case where a dry-run is likely to carry a large `unmonitor` list,
because an opted-out track lets go of the release it was holding.

**The workflow is: change the config, dry-run by hand, review, apply by hand.** Never change the
config and wait for the scheduled run. A scheduled run caps unmonitors at `[guards] max_unmonitors_scheduled`
(100), so it would refuse them and report `guarded` - correctly, but you would be reading amber
instead of the list - and a scheduled run applies as it plans, which is not where you want to
first see what an opt-out did to a library of a few thousand intents.

Turn one on, then:

```
likearr run -c /data/config.toml --out /data/diff-optout.json
```

The printed summary carries **counts only**; the per-release detail is in the file. What would
leave your library:

```
jq -r '.unmonitor[] | "\(.title)\t\(.key.rg_mbid)\t\(.key.artist_mbid)"' /data/diff-optout.json
```

And why each track stopped wanting it, one line per track, naming the release it refused and the
setting that refused it:

```
jq -r '.unmapped[] | select(.step | startswith("track:excluded:")) | "\(.step)\t\(.intent_key)\t\(.detail)"' /data/diff-optout.json
```

A count by reason, to see the shape before reading the lines:

```
jq -r '.unmapped[].step' /data/diff-optout.json | sort | uniq -c | sort -rn
```

Three things to know before you apply it:

- **An `unmonitor` entry carries the album title and the artist MBID, not the artist name.** For
  prose on any one of them, `likearr explain "The Complete Dinah Washington"` prints the release,
  who wanted it and which reasons it lost.
- **Size the change before you turn the switch on.** `owned_releases.step` in the state database
  is the population each switch would move:
  `sqlite3 /data/state.sqlite "SELECT step, COUNT(*) FROM owned_releases GROUP BY step ORDER BY 2 DESC;"`
  The `track:non-studio` and `track:smallest:ep` rows are the ones at stake.
- **Until you apply by hand, scheduled runs will report `guarded`** if the list is longer than
  `[guards] max_unmonitors_scheduled` (100). That is the cap doing its job: it refuses the
  unmonitors on an unattended run and tells you to review them. Your own `run --apply` is not
  subject to it, and the amber clears once you have applied.

Turning either switch on also re-resolves every liked and playlist track and re-baselines the
health comparison (`baseline: rules-changed` in the health record), which is why that run reports
`ok` rather than a mapping-shortfall jump. Deploying the code **without** changing the config
re-resolves nothing and moves nothing, so a first dry-run after the upgrade should be empty.

## Applying a diff

```
likearr run --apply diff.json -c /data/config.toml
```

This executes exactly the diff you reviewed - not a fresh recomputation. If Spotify or the
relevant Lidarr albums have changed since the diff was written, likearr refuses (exit code 3,
"stale diff") rather than apply something you didn't actually review. Re-run without `--apply` to
get a fresh diff.

## Adopting a pre-existing Lidarr library

You don't need `adopt` to protect anything: likearr never unmonitors a release it doesn't own, so
albums you monitored by hand before installing it are invisible to it and stay untouched by a
plain `run`. `adopt` is for a library that grew mostly from Lidarr's own import lists rather than
by hand - it claims what your sources still back and unmonitors the rest, including any release
you did monitor by hand that isn't on the keep list (see below). It is plan -> review -> apply,
like `run`:

```
likearr adopt --keep keep.txt --out adopt.json -c /data/config.toml   # plan only
# read adopt.json: claim / keep / unmonitor
likearr adopt --apply adopt.json -c /data/config.toml                 # exactly that plan
```

`--keep` is one release group MBID or `artist:<mbid>` per line; it belongs to the plan step, and
the plan carries it, so the apply never needs the file. `--apply` refuses (exit 3) a plan whose
Spotify or Lidarr inputs moved, and takes the run lock, so it cannot overlap a scheduled `run`.

Anything monitored that no Spotify source backs and that is not in the keep list **is
unmonitored** - including releases you monitored by hand. See `docs/dev/DESIGN.md` for exactly what
it does.

**It is not a casual recovery step after losing the state database.** Lost state means "likearr
owns nothing", which is safe for `run` (nothing is unmonitored), but re-running `adopt` treats
every hand-monitored release as unwanted. Restore the database from backup first (see "Backup and
restore", below). If you cannot, rebuild the keep file from what you know you monitored by hand,
read the plan, and only then apply.

## Order of steps for an existing library

The order to follow, with what to expect at each step:

1. `auth --manual` (or Settings' "Connect Spotify"), `doctor`, `setup-profiles --apply` (or
   Settings' "Preview Lidarr setup" / "Apply").
2. Only if the existing library grew mostly from Lidarr's own import lists rather than by hand:
   `adopt` plan, read `adopt.json`, then `adopt --apply adopt.json`. This is where such a library
   gets unmonitored down to what Spotify still backs: where import lists pulled in a lot, expect
   thousands of releases unmonitored and hundreds claimed. Skip this step for a library you
   curated by hand - a plain `run` already leaves it alone.
3. `run` dry-run, read `diff.json`, `run --apply diff.json`. Every artist added is one
   `RefreshArtist` plus one artist-folder rescan in Lidarr; watch `GET /api/v1/command` if the
   queue grows.
4. `prune-report`, review it (the report is JSON on purpose: build a page, sort by artist, decide
   at artist level with per-release overrides), write a decisions file.
   **In the web UI, Clean up does steps 5 to 8 with you** (turn it on first, in Settings >
   Advanced): after "Export the decisions" it
   previews the move, checks Lidarr's import lists and command queue, previews the Spotify
   changes, and gives the exact commands for the rest. From a terminal, the same steps:
5. `prune-stage --decisions` dry-run with the library mounted. Read the totals **and the Lidarr
   plan** it prints: which artists it will remove and which it will only rescan, with a reason each.
6. Disable Lidarr's own Spotify import lists (`enableAutomaticAdd = false`; keep the lists), or
   their next sync re-adds everything you are about to stage.
7. Wait for an idle Lidarr command queue, then `prune-stage --decisions ... --apply` from a
   terminal, never scheduled. `--apply` holds the run lock like `run`, `adopt --apply` and
   `promote-save --apply` do, so a scheduled run that starts while the stage is applying is
   skipped rather than racing it; that lock is a backstop, not a reason to schedule the stage.
   Keep the holding directory for a few weeks before emptying it by hand.
8. `promote-save --decisions ... --reviewed /data/review-data.json` - the same decisions file's
   other half, applied to Spotify. This is the point where the token needs
   `user-follow-modify` / `user-library-modify`, so re-run `auth --manual --promote-save` first
   if you signed in read-only (the default). `--reviewed` is the review page's own export and is
   **required**: only albums a human reviewed and kept may be saved, and without it the command
   refuses rather than falling back to whatever the library holds today. Read the plan - its
   `unmatched` list (what it would not guess at) and its `excluded_unreviewed` count (what
   arrived after the review and is therefore not saveable) - then
   `promote-save --apply promote-save.json`. Run it after the stage, not before.
9. The schedule is on by default, and it has been waiting for step 3: scheduled runs start after
   the first reviewed apply (`run --apply` by hand, or Apply in Review changes). `adopt --apply`
   does not count. Until then every fire publishes `paused` with "waiting for your first reviewed
   apply" and changes nothing. Enable the health sensor after its first clean publish.

Changing `[rules] liked_track_scope` later is a repeat of step 3: a large reviewed diff applied
by hand. A scheduled run that meets it first refuses the unmonitors above the cap (exit 2).

### Files promote-save reads and writes

- reads `/data/decisions.json` (the same file `prune-stage` consumed) - only its `promote`,
  `save`, `save_releases` and `save_exclude_releases` fields (`save_releases`: albums saved one at
  a time, under the same rule as `save`; `save_exclude_releases`: albums of a `save` artist kept
  with no change on Spotify);
- reads `/data/review-data.json`, the review page's snapshot of what was actually put in front of
  you, which is what decides which albums may be saved at all;
- writes `promote-save.json` next to wherever you ran it, unless `--out` says otherwise. In
  Docker the working directory is `/data`, so that is `/data/promote-save.json`;
- adds a `spotify_search_cache` table to the state database, so re-planning costs no API calls.
  It is backed up by whatever already backs up the state DB, and losing it costs only searches.

`promote-save` is a by-hand command like `adopt` and `prune-stage`. **Never schedule it.**
likearr performs no automatic Spotify writes, by design: saving an album or following an artist
is a high-intent action on a personal account, so it happens once, from a plan a human read, and
never as a side effect of a monitoring rule. There is no `--scheduled` for this command and
nothing in `run` reaches it.

## Running more than one instance

likearr is one Spotify account per instance - there is no way to point a single instance at two
accounts. A household with several Spotify accounts runs one instance per account instead:

1. **Register the Spotify side** - see [`docs/spotify.md`](spotify.md) for the steps. One shared
   app is the common case; a separate app per person also works. They compare like this:

   | | One shared app | An app per person |
   |---|---|---|
   | Who registers it | One person, once (see [Adding another person](spotify.md#adding-another-person)) | Each person, for their own |
   | Premium | Only the app owner - Spotify doesn't say whether the rest need it too | Each app's own owner |
   | Request quota | Shared across every instance using the app | Separate per instance |
   | Allowlist | Up to 5 accounts, added under one app's User Management | Not needed - each app has one user |

2. **Give each instance its own `/data` directory** (state database, token file and
   `config.toml`), as separate compose services or container instances - see
   [`deploy/compose.example.yaml`](../deploy/compose.example.yaml). Each also needs its own `[ui]`
   block (`allowed_hosts`, and `public_url` if used), port and `LIKEARR_UI_PASSWORD`.
3. **Connect each instance from its own browser session**, signed in as that instance's account -
   see [`docs/spotify.md`, "Adding another person"](spotify.md#adding-another-person) for the trap
   (Connect authorizes whichever account the browser is signed into) and how to check which
   account got connected.

Several instances can point at the same Lidarr. Ownership is tracked per instance, not shared, so
whichever instance monitors a release first is the one that owns it. If that person then unlikes it,
their instance unmonitors it even though the other person still wants it. The other instance's next
run sees a release it wants that isn't monitored, monitors it again and takes ownership. No files
move and nothing is lost; it corrects itself within one run of the other instance.

Two commands need more care on a shared Lidarr:

- **`adopt`.** It claims every already-monitored release your instance's own sources back, keeps
  whatever you list in `--keep`, and unmonitors everything else - including releases the other
  instance already owns and releases the other person monitored by hand. Don't run `adopt` on a
  second instance against a library the first instance already set up. If you do need to, put
  every release or artist to leave alone in the `--keep` file first, including a copy of the first
  instance's own `--keep` list. Anything the first instance kept by hand that way and that no
  source asks for stays unmonitored afterwards for good - the first instance's next run only
  re-monitors what it still wants from Spotify, not a hand-kept release with nothing behind it.
- **Clean up and `prune-stage`.** A candidate is any album with files that your instance's own
  sources don't want and don't own. On a shared library that includes everything the other person
  likes, so review every candidate against both accounts before staging a move.

## Scheduling

`likearr start` runs its own schedule - there is no host cron line to set up, and no separate
worker container. `docker compose up -d` and a browser is the whole install. The schedule lives in
`config.toml`'s `[schedule]` block (`cron`, `timezone`, `enabled`), edited from the Settings page,
which shows the next few fires before you save. See docs/dev/DESIGN.md, "The scheduler", for the
design.

**The cron expression can't fire more often than every 60 minutes**
(`config.MIN_SCHEDULE_INTERVAL_MINUTES`): a scheduled run reads all of Spotify and Lidarr, so
anything tighter would mostly be redundant reads. A tighter expression fails config load with the
reason.

- **Pause/resume** is a Settings control, not a config edit: set `[schedule] enabled = false`
  (Settings does this for you, with a reason and a timestamp) and the next fire does nothing at
  all - no Spotify, MusicBrainz or Lidarr call, exit 0, health record `status: "paused"`. `ts`
  still updates, so the HA dead-man does not trip while paused on purpose. A hand run - a terminal
  `likearr run --apply`, or an apply started from Review changes - ignores the pause completely;
  only a scheduled fire ever reads it. Resuming from Settings needs a second confirm, because it
  turns unattended applies back on.
- **Scheduled runs wait for the first reviewed apply.** On a new install the schedule is on
  from the start, but until a plan has been reviewed and applied by hand once - Apply in Review
  changes, or a terminal `likearr run --apply` - every fire does nothing at all: no Spotify,
  MusicBrainz or Lidarr call, exit 0, health record `status: "paused"` with the message `waiting
  for your first reviewed apply: connect Spotify, then review and apply your first plan`. That
  covers the fires before Spotify is connected too, which are `paused` rather than `error`. With no
  state database yet, a scheduled `run` does not create one. Status and Settings say "Scheduled
  runs start after your first reviewed apply", and "Run and apply now" is disabled until then.
  `adopt --apply` does not count: it claims what Lidarr already monitors, it is not a reviewed
  plan.
- **Missed-fire catch-up.** A fire missed while the container was down (an outage, a redeploy) is
  caught up once, five minutes after the service starts back up - never once per fire missed. A
  fresh install, or a fresh state database, never triggers a false catch-up: there is nothing to
  have missed yet.
- **A scheduled fire waits, rather than being refused, behind a UI job.** If a check, an apply or a
  Clean up preview is already running when the schedule is due, the fire queues for up to an hour
  and starts the moment the slot frees; past an hour it gives up and is recorded `skipped` (visible
  in job history, same as any other job).
- **Status shows the next fire, the last fire and its result, and a "Run and apply now" button.**
  It submits the exact same `run --scheduled --apply` job a real fire would, through the same lock
  and the same queue - the *arr "Run now" pattern. So while scheduled runs are paused the button is
  not shown (the tile links Resume instead), and a stale tab's press is refused with a note rather
  than recorded. Use the plan review to run by hand.
- `--scheduled` caps unmonitors at `guards.max_unmonitors_scheduled` per run and applies the rest;
  anything blocked shows up in the health record's `status: "guarded"` and in the run's guards.
- The run lock (`state.lock_file`) still applies: a hand command started while a scheduled run is
  going exits 4 (busy) and does nothing, and a scheduled fire that lands on a held lock is
  `skipped`, not an error.

Nothing here is CLI-only: `likearr run --scheduled --apply` still works exactly as above if you
ever want to run likearr without the web UI. There is nothing to run from host cron - the service
owns its own schedule end to end.

## Web UI

`likearr start` is the service: Status, Review changes, Look up, Missing, Clean up (when
`[prune] enabled` is on) and Settings
(docs/dev/DESIGN.md, "Web UI"), the scheduler ("Scheduling", above) and the job runner, all in one
process. It changes the live `config.toml` when you save a setting.

**The live `config.toml` is the source of truth.** Once the UI can save settings, any other copy of
the file - a repo copy kept for distribution or documentation - is exactly that, and must never be
copied back over the live file: that would silently undo every browser save since. Every save
leaves a `config.toml.bak-YYYYMMDD-HHMMSS` beside it (the newest 30 are kept).

### What it needs

- `LIKEARR_UI_PASSWORD` in the environment, at least 16 characters (`openssl rand -base64 24`
  makes one). `likearr start` refuses to start without it or with a shorter one. Every
  host on the LAN can reach the published port over plain http, which is why the UI authenticates
  for itself; keep the password in your password manager.
- A `[ui]` block in `config.toml` (see `deploy/config.example.toml`): `allowed_hosts` is every name
  and IPv4 address you will browse to, without a port (`localhost` and `127.0.0.1` are always
  allowed on top, for the healthcheck); `lidarr_url` is where a browser reaches Lidarr, for the
  Status page's links (it defaults to `[lidarr] url`, which may be a name only the containers can
  resolve). Optionally `public_url` - this service's own `https://` address - offers Spotify
  Connect's direct-callback mode instead of paste-back; leave it unset to keep
  paste-back as the only mode. A mistake in this block never stops a run - runs ignore it - but
  `likearr start` refuses to start and names it, and the Status page shows one made later.
- A `[schedule]` block: `cron` and `timezone` are when the in-service scheduler fires (default
  `"20 */6 * * *"` and `"UTC"` - most deployments set `timezone` explicitly, since a wrong guess at
  the host's local time is worse than an unfamiliar but correct one), edited from Settings, which
  shows the next few fires before you save. `enabled` (default
  `true`) is whether a *scheduled* run does anything at all - Settings' pause/resume control sets
  it, and you would normally never hand-edit any of these. Unlike `[ui]`, a bad value here fails
  the whole config load, because the scheduler and a scheduled run both read this block themselves.
- `config.toml` and its directory writable by the service user: a settings save writes a backup
  beside the file and replaces the file. The replacement is a new file, so it ends up owned by the
  service user (1000:1000 by default, or the uid/gid you built or run it with - see "Docker
  Compose" above), and a `config.toml` that was a symlink becomes a regular file with the link
  left behind.
- The session cookie is scoped to the host, not the port: every service on the same host name or
  address can read it. Browse to the UI by its own name.

### Exposure and reverse proxy

**The supported model is LAN or VPN only.** likearr is not designed to be port-forwarded or put
behind a public tunnel: the login is a single shared password with no second factor, and the
login pause (below) is sized for a LAN, not a hostile internet. If you need to reach it away from
home, put it on a VPN such as WireGuard or Tailscale and treat it as LAN access from there.

A reverse proxy is optional - likearr works directly over plain http on the LAN, which is why it
has a login - but if you want it on https with its own name, any proxy that terminates TLS and
forwards to `127.0.0.1:8770` (or the docker host's LAN address) works. A minimal
[Caddy](https://caddyserver.com/) example, which provisions its own certificate:

```
likearr.example.lan {
    reverse_proxy 127.0.0.1:8770
}
```

Nginx Proxy Manager and any other proxy work the same way: terminate TLS at the proxy and forward
plain http to likearr.

What the proxy must do, and why:

- **Send `X-Forwarded-Proto: https`.** `SecureCookieMiddleware`
  (`likearr/web/auth.py`) only adds `Secure` to the session cookie when the request
  arrived over https or carries this header. Caddy and NPM send it by default; a hand-written
  nginx or HAProxy config may not, and then the cookie goes out without `Secure` even though the
  browser used TLS. Check with your browser's devtools, or
  `curl -sk -D - -o /dev/null -X POST https://<proxy name>/login -d password=... | grep -i set-cookie`
  and look for `; secure`.
- **Add the proxy's name to `[ui] allowed_hosts`.** likearr refuses any request whose `Host`
  header isn't in the list (`likearr/config.py`, `UiConfig.allowed_hosts`); the proxy's name is
  the `Host` a browser sends once you put it behind one.
- **Give likearr its own host name behind the proxy.** The session cookie is scoped to the host,
  not the port (see above), so a name shared with another service on the same box would let that
  service read likearr's cookie.
- **Set `[ui] public_url` to the proxy's `https://` address**, only if you want Spotify's
  direct-callback mode (`docs/DEPLOY.md`, "What it needs", above) instead of paste-back. It is
  optional.
- **Publish the port to loopback only if the proxy runs on the same host**:
  `"127.0.0.1:8770:8770"` in `deploy/compose.example.yaml`, so only the proxy can reach it.
  Otherwise the default `"8770:8770"` stays, and any LAN host can still reach likearr directly
  over plain http - which is exactly why it has a login.

**The login pause is per proxy, not per browser.** `likearr start` runs uvicorn with
`proxy_headers=False` and `forwarded_allow_ips=""` (`likearr/web/server.py`), and
`LoginLimiter` (`likearr/web/auth.py`) keys five-failed-logins-in-a-minute on the TCP
peer address, never a forwarded header, deliberately: a header is whatever the client says it
is. Behind a proxy, every request's peer address is the
proxy's own address, so five failed logins from *anyone* going through the proxy pause *everyone*
going through it for a minute. A request straight to the LAN address is counted separately. This
is a conservative trade-off, not a bug, and there is no config to change it.

### Running it

```
LIKEARR_UI_PASSWORD=... likearr start -c /data/config.toml                 # 127.0.0.1:8770
likearr start --host 0.0.0.0 --port 8770 -c /data/config.toml             # in a container
```

For Docker, `deploy/compose.example.yaml` has a `likearr` service: `restart: unless-stopped`, the
port published on the host's LAN interface, a healthcheck on `http://127.0.0.1:8770/healthz`, and
`stop_grace_period: 30m`, so stopping the container waits for an apply the UI started instead of
cutting it off halfway. It mounts `/data` only, **never** the library - that mount lives on
`likearr-cli` instead, since only `prune-stage` needs it. An apply that adds many artists waits
from 300 s up to an hour for each one's RefreshArtist, depending on the catalogue, so it can run
past ten minutes; the server waits 28 of the 30 for it (`STOP_GRACE_PERIOD_S` in
`likearr/web/context.py`, less two for its own shutdown).
`mem_limit: 768m`: the server is small, but "Find unneeded albums" (`prune-report`) runs as a
child that builds the whole library report in memory - 256m was OOM-killed on a large library. A job
the system kills that way shows as "ran out of memory", not as a bare failure. The healthcheck is
baked into the image (a `HEALTHCHECK` in the Dockerfile, curl-free, on `127.0.0.1:8770/healthz`);
the compose example no longer repeats it. Once the state database exists it must open and its last
run must read - never created here - but a fresh install with a valid config and no database yet
reads `200 ok (no runs yet)` instead of failing the check, so the container shows `healthy` in
`docker ps` before the first run, not hours into it. A missing or unwritable `/data` (a bad mount,
or a directory the container's uid can't write to) still reads unhealthy, since that is the case
worth catching before any run gets the chance to write there.

Jobs the service starts (a scheduled run, a check or an apply, Find unneeded albums, the Clean up
previews and pre-flight checks, file counts, a live explain, the playlist list) run as `likearr`
child processes, one at a time, and keep their output under `<config dir>/ui/jobs/`, the newest 20
of them. Only a check or an apply takes the run lock; the others change nothing in Lidarr or
Spotify. **One worker only** - `likearr start` never exposes `--workers` or `--reload`; running two
copies of the service against the same `config.toml` (two containers, not the compose service
twice) would fire every scheduled run twice, caught only by the run lock making the second
`skipped`. Don't do that on purpose.

### Redeploys and scheduled runs

A scheduled run is a child of the service being redeployed, and what happens to it on a redeploy's
SIGTERM depends on how far it got:

- **Still planning (no Lidarr write yet): cancelled, not waited for.** The container stops at once;
  the job is recorded `interrupted` with "cancelled for shutdown during planning" in its log. It
  costs nothing - planning writes nothing to Lidarr - and the missed-fire catch-up re-runs that
  exact slot once the service is back (five minutes after startup, same as any other missed fire).
  Its Spotify read is not wasted either: if the read had already finished, the re-fired run reuses
  it (under 30 minutes old, same sources config) instead of asking Spotify again - Status's log for
  that run says "reusing the Spotify read from &lt;time&gt;".
- **Already applying: drained, exactly as a UI apply always has been.** `stop_grace_period: 30m`,
  and the server waits 28 minutes for it.
- **A Spotify quota rejection is not a failure to wait out.** If the scheduled run's Spotify read
  hits `QUOTA_EXCEEDED`, it exits 0, publishes `skipped: Spotify quota exceeded`, and the *next
  regular slot* tries again - no catch-up burns more of a quota that is already gone.

**Before recreating the service, this means:** check Status for a job whose kind is `scheduled` and
state is `running`. If its log already shows the apply-phase line (or Status shows it mid-apply),
wait for it - the grace period is the backstop, not the plan, same as a UI apply. If it is still
planning, there is nothing to wait for: `docker compose up -d` any time, and the run picks back up
on its own.

**Look up answers from the last run.** Every `likearr run` ends by writing `last-run.json`
beside the state database (a few MB, replaced each run), and Look up answers from it at once.
Before the first run has written one, Look up says so and offers the live check instead. A
failure to write it is a warning in the run's log and changes nothing else about the run.

**Playlist names.** After deploying, press "Refresh names from Spotify" on Settings once: the
names are kept in `<config dir>/ui/playlist-names.json` from then on, and Settings, Status and
Look up show playlists by name. Cancel asks a job to stop and gives
it up to two minutes: a job in the middle of saving a new Spotify token finishes the save first,
because a job killed there would leave a used refresh token and every later run failing.

### Clean up remembers earlier decisions

Every Clean up export records what it decided in `<config dir>/ui/prune-ledger.json`
(`/data/ui/prune-ledger.json` in Docker), and a report is pre-filled from it, opening on the
albums that still need a decision. A kept album is recorded as kept (as saved, when the review
asked to save it on Spotify), a trashed one as trashed, and the `promote` / `save` artists as such.

### The Spotify re-authorization date

Spotify refresh tokens die six months after you authorize, and refreshing does not extend that.
`likearr auth` records the date, and the Status page counts down to it. When the page turns amber
(30 days out), run `likearr auth --manual` as described above.

### Before recreating the service

A deploy that rebuilds the image and recreates `likearr` stops any job it is running. For an
explain that costs nothing; for an apply started from Review changes it leaves Lidarr partly changed until the next
plan. So first check that no job is running:

```
grep -l '"state": "running"' /path/to/data/ui/jobs/*/meta.json
```

No output means it is safe to recreate. If a file is listed, read its `kind`. An `apply` job
(`"drain": true`) is worth waiting for. A `scheduled` job is worth waiting for only once it has
reached the apply phase - `grep likearr-phase: /path/to/data/ui/jobs/<id>/log.txt` shows a line if
it has; if it hasn't, recreate freely, since the container's own SIGTERM cancels it cleanly and the
missed-fire catch-up re-fires it (see "Redeploys and scheduled runs", above). The grace period is
the backstop, not the plan.

### Deploy runbook (first install)

These touch live infrastructure, so they are run by hand, in this order:

1. **DNS.** Add a local record for the UI's own name pointing at the docker host, using whatever
   your DNS setup is, then check the name resolves.
2. **Reverse proxy.** See ["Exposure and reverse proxy"](#exposure-and-reverse-proxy), above, for
   the model and what the proxy must send. Point it at `http://<docker host LAN IP>:8770` - an IP
   upstream, never a bare hostname.
3. **No public exposure.** Confirm there is no public DNS record and no tunnel ingress for the
   name. Their absence is the access control; do not "fix" it by adding one.
4. **Secret.** Create the password manager item, then put `LIKEARR_UI_PASSWORD` in the stack's
   `.env` from a structured read, never by printing it.
5. **Config.** Add the `[ui]` block to the live `config.toml`: `allowed_hosts` with the UI's name
   and the docker host's LAN address, and `lidarr_url` with the address a browser uses for Lidarr.
   Set `[schedule] cron` and `timezone` too (or leave the defaults and adjust from Settings).
6. **Compose.** Back up `compose.yaml` (`compose.yaml.bak-YYYYMMDD-likearr`), add the service,
   pull the new image (or build it), `docker compose up -d likearr`. Check `docker ps` shows it
   healthy, then open the page from a phone on Wi-Fi and log in.
7. **Log check.** Start one live Look up from the UI, then check its log carries no credential:
   `grep -iE 'bearer|access_token|refresh_token|api[_-]?key' <data>/ui/jobs/*/log.txt` should show
   nothing, or only `<redacted>` values.

## When Lidarr looks stuck

likearr waits on Lidarr's `RefreshArtist` commands, and anything else that uses Lidarr's queue
(soularr's `DownloadedAlbumsScan`, manual imports) waits behind whatever is in it. If a run or a
neighbouring tool hangs, read the queue before restarting anything:

```
curl -s -H "X-Api-Key: $KEY" http://lidarr:8686/api/v1/command | jq '.[] | select(.status=="queued" or .status=="started") | {id, name, status, folders: .body.folders}'
```

A pile of `RescanFolders` with `folders: ["<your root folder>"]` is the full-library rescan
described in `docs/dev/DESIGN.md` (Upstream quirks). Queued ones cancel with
`DELETE /api/v1/command/{id}`; a started one has to finish. Never delete an artist while a
`RefreshArtist` for it is queued or running.

## Health wiring

Every run - success or failure - emits one `HealthRecord` (see `docs/dev/DESIGN.md` for the shape) to
every sink configured under `[health]` in config.toml. `stdout` is on by default; MQTT and webhook
are opt-in.

**A dry run (`"dry_run": true`) publishes to `stdout` only, never to MQTT or the webhook.** MQTT
is retained, so publishing a hand-run dry-run check there would overwrite the last scheduled
apply's record and reset the `ts` dead-man's-switch for a run that changed nothing.
Run a dry-run check as often as you like - `likearr run` with no `--apply` - and it stays purely
informational: it prints its record and is still written to the local run history, but Home
Assistant keeps showing the last real run until the next `--scheduled --apply` fires. Every
non-dry terminal status - `error`, `stale`, `guarded`, and a scheduled run's `skipped` - still
reaches every configured sink, exactly as before, except that a webhook set to
`notify = "problems"` is sent only the runs that are news (see "Generic webhook" below).

### Health record example

One line, on stdout, at the end of every run. This is a real record from a dry run that planned
one artist and two releases:

```json
{"ts": 1789714800, "version": "0.1.0", "resolver_version": 4, "exit_code": 0, "status": "ok",
 "spotify_ok": true, "spotify_schema_ok": true, "mb_ok": true, "lidarr_ok": true,
 "lidarr_metadata_ok": true,
 "counts": {"followed_artists": 1, "saved_albums": 0, "liked_tracks": 0, "intents": 1,
            "desired": 2, "monitored": 2, "unmonitored": 0, "added": 1},
 "unmapped": 0, "pending_album": 0, "message": "", "dry_run": true,
 "baseline": "compared", "baseline_advanced": false, "new_conditions": []}
```

(The change-detection counts are all zero here and elided for readability; `docs/dev/DESIGN.md` has
the full shape.)

On a dry run `monitored` / `unmonitored` / `added` are what the diff *proposes*. On an apply they
are what actually happened, which is a smaller number whenever an artist was skipped for a Lidarr
metadata outage or a release group has not reached Lidarr's catalogue yet - `message` says which.

### A healthy run on a library with chronic problems

This is what most runs look like, and it is `ok`. A fifth of the intents have never mapped, two
search terms return 503 on every run, and Lidarr's catalogue does not hold a few dozen releases that
MusicBrainz lists. None of that changed today, so none of it is news:

```json
{"ts": 1789736400, "version": "0.1.0", "resolver_version": 4, "exit_code": 0, "status": "ok",
 "spotify_ok": true, "spotify_schema_ok": true, "mb_ok": true, "lidarr_ok": true,
 "lidarr_metadata_ok": false,
 "counts": {"followed_artists": 412, "saved_albums": 210, "liked_tracks": 603, "intents": 1225,
            "desired": 3410, "monitored": 4, "unmonitored": 1, "added": 0},
 "unmapped": 240, "pending_album": 7, "message": "", "dry_run": false,
 "unmapped_new": 2, "unmapped_resolved": 3, "unmapped_ratio": 0.196, "regressions": 0,
 "catalogue_gaps": 41, "catalogue_gaps_new": 1,
 "catalogue_gaps_recent": 2, "catalogue_gaps_recent_new": 2, "refresh_failures": 0,
 "absent_in_lidarr": 37, "absent_in_lidarr_new": 0,
 "lidarr_metadata_errors": 2, "lidarr_metadata_errors_new": 0, "mb_errors": 0,
 "skipped_artists": 0, "skipped_artists_new": 0,
 "name_collisions": 0, "name_collisions_new": 0,
 "catalogue_too_large": 0, "catalogue_too_large_new": 0,
 "baseline": "compared", "baseline_advanced": true, "new_conditions": [],
 "changes_made": 5, "changes_planned": 5, "lidarr_changed": true, "tagged_without_state": 0}
```

Note `lidarr_metadata_ok: false` next to `status: "ok"`. That is correct, and it is the point: two
lookups failed, both of them the same two that fail every run, so nothing is newly wrong.
`unmapped_new: 2` with `regressions: 0` says those two are brand-new intents that never mapped,
which is the ordinary base rate rather than a fault.

Those two terms are also why `lidarr_metadata_errors` stays 2 instead of climbing: once a term has
failed once, likearr stops re-asking Lidarr for it while its negative-cache entry is fresh
(`[musicbrainz] negative_cache_days`, default 7) and retries it only once that expires. It still
counts here and keeps the same identity either way, so this number and `new_conditions` are
unaffected either way - the caching only cuts the wasted `album/lookup` calls, four times a day.

`catalogue_gaps_recent: 2` is two followed artists who released something Lidarr's metadata has
not got yet; likearr queued a `RefreshArtist` for each, and both are `_new`, so this run is the
first to see them. That is the normal shape. **The number worth watching is
`catalogue_gaps_recent` minus `catalogue_gaps_recent_new`:** those gaps survived a previous apply
that already refreshed their artist, which means Lidarr's metadata proxy is not picking the
release up. It deliberately does not degrade the run - a new release taking a day or two is the
system working - but if that difference stays above zero for a week, look at Lidarr: check
`GET /api/v1/command` for stuck `RefreshArtist` jobs, and whether `api.lidarr.audio` is answering
for that artist.

### A followed artist's new album

likearr sets `monitorNewItems = none` on every artist holding a release it owns (see below), so
Lidarr never auto-monitors their new releases; likearr monitors one on the run after Lidarr's
catalogue has the release group. So a new album takes two
runs: the first reports it under `catalogue_gaps_recent` and refreshes the artist
(`refresh_artists` in the plan, `N artists refreshed for a recent release` in the apply summary),
and the second monitors it. `[lidarr] max_refreshes_per_run` (default 10) caps how many artists
one run will chase, and `[lidarr] recent_gap_refresh_hours` (default 24) stops the same artist
being asked again too soon - without it a promo Lidarr will never carry would be chased four times
a day for 60 days. Artists either limit drops are still reported; only the asking is rationed. Set
`max_refreshes_per_run = 0` to turn the chase off and go back to waiting on Lidarr's own schedule.

A freshness refresh that fails or times out is **not** a skipped artist: the artist keeps its
monitors for the run and the failure is counted in `refresh_failures`. It costs only the one
release it was chasing, and the backoff means it is retried tomorrow rather than in six hours.

An artist Lidarr refuses to add because its metadata does not know them yet (usually an artist new
to MusicBrainz) is left out of that run and asked for again on the next one. Everything else in
the run still applies, and this does not make the run `degraded`: the apply summary and the run's message name the
artist ("not added: Lidarr's metadata does not know them yet"). It clears on its own once Lidarr's
metadata catches up, which can take weeks.

**`monitorNewItems = none` also applies to artists you manage by hand**, as long as they hold a
release likearr owns (one it monitored for you, one `adopt` claimed, or one a `manual` reason
keeps). likearr decides what is monitored under such an artist, and letting Lidarr auto-monitor
there would monitor releases no source asked for. It applies too when likearr widens the artist's
profile to Full: the refresh after widening shows more release types, and on `all` Lidarr would
monitor every one of them. And when likearr re-monitors an artist you had unmonitored: once it
is monitored again, `all` would auto-monitor its future albums. Otherwise an artist whose wanted releases you already monitor keeps its
setting: likearr claims nothing there, so it never unmonitors what Lidarr auto-monitors for it. If
you want Lidarr's auto-monitoring back for an artist, that artist must not hold a release likearr
owns.

### A run with something actually wrong

```json
{"ts": 1789758000, "version": "0.1.0", "resolver_version": 4, "exit_code": 0, "status": "degraded",
 "spotify_ok": true, "spotify_schema_ok": true, "mb_ok": true, "lidarr_ok": true,
 "lidarr_metadata_ok": true,
 "counts": {"followed_artists": 413, "saved_albums": 210, "liked_tracks": 603, "intents": 1226,
            "desired": 3410, "monitored": 0, "unmonitored": 0, "added": 1},
 "unmapped": 240, "pending_album": 7, "dry_run": false,
 "message": "1 artist(s) newly skipped for a Lidarr metadata failure",
 "skipped_artists": 1, "skipped_artists_new": 1,
 "baseline": "compared", "baseline_advanced": true, "new_conditions": ["new-skipped-artist"]}
```

`new_conditions` is the field to read: it names what is new, and it is empty on every `ok` run.
This one keeps reporting until that artist refreshes cleanly, or until you accept it by hand with
`likearr run --apply diff.json --accept-health`. It will not quietly become the new normal.

Every code that can appear in `new_conditions`:

| Code | Meaning | Repeats until |
|---|---|---|
| `spotify-schema` | A Spotify response was missing fields likearr depends on, or read fewer items than it reported | The response is well-formed and complete again |
| `mb-outage` | A MusicBrainz lookup failed with no cached answer to fall back on | MusicBrainz answers again |
| `lidarr-metadata-outage` | Most of this run's *attempted* Lidarr metadata lookups failed - a likely `api.lidarr.audio` outage, not the usual handful of chronically-failing terms | Lidarr's metadata proxy recovers |
| `new-skipped-artist` | An artist newly skipped because Lidarr's metadata server failed while adding or refreshing it | It refreshes cleanly, or you `--accept-health` |
| `new-catalogue-too-large` | A followed artist newly past MusicBrainz's browse ceiling | You `--accept-health` (permanent otherwise) |
| `new-name-collision` | A new artist name collides with one already in Lidarr | Only one of the two is wanted any more, or you `--accept-health`. Lidarr cannot hold either under a different name; `likearr explain <name>` shows why the second one was wanted, which is often a wrong match |
| `mapping-shortfall-jump` | Releases that mapped last run no longer do, above `guards.unmapped_ratio_amber` | The next run is back under the threshold |

### The first run after a baseline reset

The first apply has nothing to compare against, so it publishes `ok` with `"baseline":
"first-run"` and absorbs the standing mapping shortfall. The second apply publishes `"baseline":
"compared"` and the signal is live from then on. The same happens after a `RESOLVER_VERSION` bump,
a `liked_track_scope` change, or a playlist added or removed - the value of `baseline` says which.
A new collision or skipped artist is **not** absorbed by a first run, so one present on the first
run is still reported from the second run on.

A guarded run looks like this, and is the one to read rather than skim:

```json
{"ts": 1789715700, "version": "0.1.0", "resolver_version": 4, "exit_code": 2, "status": "guarded",
 "spotify_ok": true, "spotify_schema_ok": true, "mb_ok": true, "lidarr_ok": true,
 "lidarr_metadata_ok": true,
 "counts": {"followed_artists": 1, "liked_tracks": 0, "desired": 2,
            "monitored": 2, "unmonitored": 0, "added": 0},
 "unmapped": 0, "pending_album": 0,
 "message": "source 'liked_tracks' fell 100.0% (40 -> 0), over the 10.0% limit; if that is right, plan with `likearr run --accept-shrink` and apply the reviewed diff",
 "dry_run": false}
```

`unmonitored: 0` with `status: guarded` is the guard doing its job: the adds and monitors were
applied, every unmonitor was refused, and the shrunken count was **not** recorded: the next run measures
against the same baseline and is refused again, until the source recovers.

If the shrink is real (you deliberately removed a playlist or un-liked a lot), look at what would be
unmonitored, then accept it by hand: `likearr run --accept-shrink` writes a `diff.json` whose
`accept_shrink` is true and whose shrink guards are not applied; read it, then
`likearr run --apply diff.json`. That apply records the new counts as the baseline. `--accept-shrink`
is refused with `--scheduled` and with `--apply`, and a scheduled run will not apply such a diff.
Unfollowing an artist is not a shrink and needs none of this, and nor is refusing one of a followed
artist's releases ("Not this one") or tagging them albums-only: the per-artist guard counts the
catalogue before your own filters.

### Accepting a chronic fault you have decided to live with

`degraded` for a new name collision, a newly skipped artist or a followed artist too large for
MusicBrainz to browse repeats on **every** run until the fault clears. Most clear on their own -
a skipped artist usually refreshes fine next run. For the ones you decide to live with (a second
artist sharing a name that you are never going to add by hand, say), fold them in once:

```bash
likearr run --apply diff.json --accept-health
```

The condition stays in the record as a count; it just stops being reported as new. It is refused
with `--scheduled` and on a dry run, for the same reason `--accept-shrink` is: acceptance is a
human act, and an unattended scheduled run carrying it would silence the signal permanently.

### MusicBrainz cache expiry

Successful MusicBrainz answers expire after `[musicbrainz] positive_cache_days` (90), each entry
spread deterministically over up to 25% longer so the cache does not all fall due on one run.
Without that expiry a corrected Spotify link or an artist merge would never be picked up - and
after a merge that artist's releases would quietly stop being monitored.

What you will see: the **first run past the 90-day mark refetches more than usual**, trickling out
over about three weeks rather than landing on one day, at MusicBrainz's 1 request/second. An
entry whose refetch fails keeps serving its cached answer - a failed lookup has never been allowed
to drop a mapping - and is counted in `mb_errors` without touching `mb_ok` or the run's status.
So a rising `mb_errors` on `ok` runs means MusicBrainz is wobbling and likearr is coping; `mb_ok:
false` still means a lookup had no answer at all.

A song or saved album that already resolved makes no MusicBrainz lookup at all while its answer
is reused, so the cache's expiry alone never reaches it. Each answer is therefore looked up again
at 4/3 of `positive_cache_days` (120 days by default), spread over up to a month by intent key.
Expect a trickle of re-resolved songs from about four months after an upgrade, not a spike; an
answer stored before this started its clock on the first run after the upgrade.

### MQTT -> Home Assistant

Uncomment `[health.mqtt]` in config.toml and set `host` and `topic`. The message is published
retained, QoS 1, so Home Assistant (or anything else subscribing) always sees the last run's
result immediately on (re)connect, not just at the moment a run happens.

Example Home Assistant MQTT sensor, plus a dead-man's-switch binary sensor that goes unavailable
if likearr hasn't reported in (catches "cron stopped running entirely", which a plain state sensor
can't):

```yaml
mqtt:
  sensor:
    - name: "likearr status"
      state_topic: "likearr/health"
      value_template: "{{ value_json.status }}"
      json_attributes_topic: "likearr/health"
      icon: mdi:music-box-multiple

  binary_sensor:
    - name: "likearr stale"
      state_topic: "likearr/health"
      value_template: >
        {{ (as_timestamp(now()) - value_json.ts) > 3600 * 13 }}
      device_class: problem
```

`3600 * 13` (13 hours) is two six-hourly runs plus slack; adjust it to however far apart your
scheduled runs actually are, with slack for one missed run. The web
UI's Status page uses the same 13 hours for its "no run for ..." warning (`web/status.py`,
`STALE_AFTER`), so the two agree.

**`paused` is its own state, not a stale.** Pausing a scheduled run (from
Settings) still publishes on the schedule's would-be fire, with `status: "paused"` - `ts` keeps
moving, so `binary_sensor.likearr_stale` above stays `false` exactly as intended: paused-on-purpose
must never look like "cron stopped running". `sensor.likearr_status` simply reads `paused`; give it
its own colour or icon in your dashboard if you want it visually distinct from `ok` (both are
"fine", but they mean different things) - it is deliberately left out of the amber template below.

### Which statuses to alert on

`degraded` means *something is newly wrong* rather than *this library has standing problems*, so
it is worth reacting to:

| Status | What it means | Suggested treatment |
|---|---|---|
| `ok` | Nothing changed for the worse. Chronic counts may still be large | green |
| `skipped` | A scheduled run found the lock held; the other run is doing the work | green |
| `paused` | `[schedule] enabled` is false, or no reviewed apply has happened yet; the scheduled run did nothing on purpose | green |
| `degraded` | Something is newly wrong, or a dependency failed this run | amber |
| `guarded` | A guard refused unmonitors (exit 2) | amber |
| `stale` | `--apply` refused a diff the world moved under (exit 3) | red |
| `error` | The run failed (exit 1), or a hand-run command found the lock held and did nothing (exit 4) | red |

As a single problem flag:

```yaml
value_template: >-
  {{ value_json.status in ['error', 'stale', 'guarded', 'degraded'] }}
```

`paused` is deliberately left out of that list, exactly like `skipped` and `ok`: it is not a
problem, it is the schedule doing what Settings told it to.

Two attributes are worth putting on a dashboard next to it:
`{{ state_attr('sensor.likearr_status', 'new_conditions') | join(', ') }}` says *what* is new, and
`baseline` says whether the run could compare at all.

**Do not turn this on until the second run after deploying.** The first run publishes
`"baseline": "first-run"`, which is not a comparison; the second publishes `"baseline":
"compared"`, and only then does `degraded` mean what the table above says.

One caveat: `guarded` is sticky by design. A shrink guard repeats every run until the count
recovers or you accept it with `--accept-shrink`, so amber can legitimately persist for days.

### Generic webhook

Uncomment `[health.webhook]` and set `url`. likearr POSTs every key of the MQTT payload and the
stdout line, unchanged, plus three fields a notification service can show:

- `title`: a fixed line for the status - `likearr: run failed` (`error`), `likearr: plan is stale`
  (`stale`), `likearr: guard held back changes` (`guarded`), `likearr: new problems` (`degraded`),
  `likearr: back to ok` (an `ok` that clears a problem), `likearr: run ok` (any other `ok`),
  `likearr: scheduled runs are paused` (`paused`) and `likearr: scheduled run skipped` (`skipped`).
- `body`: the record's own `message`, the same text MQTT gets, or a fixed line when it is empty (as
  it is on a clean run). Credentials are stripped where the message is built. likearr adds no
  address of its own, but an error message can name the Lidarr address it was talking to, so keep
  that in mind before pointing the webhook at a public topic.
- `type`: `failure` (`error`, `stale`), `warning` (`guarded`, `degraded`), `success` (`ok`) or
  `info` (`paused`, `skipped`).

MQTT and stdout never get these three fields. An error run's body looks like this (most record keys
elided):

```json
{"ts": 1789714800, "status": "error", "exit_code": 1, "dry_run": false,
 "message": "the apply failed before changing anything: lidarr GET /system/status: HTTP 503",
 "title": "likearr: run failed",
 "body": "the apply failed before changing anything: lidarr GET /system/status: HTTP 503",
 "type": "failure"}
```

`notify` picks which runs are sent:

| `notify` | Sent |
|---|---|
| `"always"` (the default) | Every run that reaches the webhook: every non-dry run except a reviewed diff refused because the settings changed, exactly as before `notify` existed |
| `"problems"` | An `error`, `guarded`, `degraded` or `stale` run whose status or message differs from the previous run's, and one `ok` when a problem clears. Never `paused` or `skipped` |

"The previous run" is the last one the webhook could have been sent, passing over `paused` and
`skipped` ticks, so a pause in the middle of an outage neither hides it nor looks like a recovery.
A guard that holds for days, or an outage that fails every run, is one notification rather than
four a day. A problem whose message changes (a guard's count moving, a different error) is sent
again, since that is news too.

A non-2xx answer from the endpoint is logged as a warning naming the status code (never the URL,
which may hold a key). Both sinks are best-effort: a broker or endpoint being down never fails the
run itself, it just logs a warning.

**`notify = "problems"` cannot tell you likearr has stopped running.** A dead service sends nothing
at all, which looks exactly like a quiet week. Don't rely on the webhook as your only signal that
likearr is running - the stale-check pattern above (comparing `ts` to "now") is what catches that.

#### Recipe: Apprise API

[Apprise API](https://github.com/caronc/apprise-api) fans one request out to Discord, Slack,
Telegram, Pushover, email and the rest. Its `/notify/{KEY}` endpoint reads `title`, `body` and
`type` from a JSON body - the names likearr sends - and ignores every other key, so no templating
is needed. Save your Apprise URLs under a key (here `likearr`) with Apprise API's `/add/likearr`,
then:

```toml
[health.webhook]
url = "http://apprise:8000/notify/likearr"
notify = "problems"
```

If the URLs under the key carry tags, add `?tag=<tag>` to the url to choose which ones fire: Apprise
API reads `tag` from the query string when the body has none, and answers 424 when the tags don't
match (likearr logs that as a warning). Without a saved key, `http://apprise:8000/notify` sends to
the URLs in the Apprise container's `APPRISE_STATELESS_URLS` environment variable instead.

#### Recipe: ntfy

ntfy shows a JSON body as raw text unless templating is on. Its inline templating (ntfy server
v2.10.0 or later) fills the notification from the body's fields, set from the URL:

```toml
[health.webhook]
url = "https://ntfy.sh/<your-topic>?tpl=yes&t={{.title}}&m={{.body}}"
notify = "problems"
```

`tpl=yes` turns templating on, and `t` and `m` are the title and message templates. Use the topic
URL, not ntfy's root URL: publishing JSON to the root URL needs a `topic` field in the body, which
likearr does not send. On ntfy.sh anyone who knows a topic's name can read it, so the name is the
password - pick one nobody would guess. A self-hosted ntfy with access control takes the token in
the URL as `&auth=<value>`; ntfy's publishing docs ("Authentication", "Query param") say how to
encode it.

Both recipes are written from each project's own documentation and source (ntfy's publishing docs
and v2.10.0 release notes, Apprise API's README and `/notify` handler). They have not yet been
tried against a live Apprise API or ntfy server, so check yours once before relying on it by
posting a body shaped like likearr's to the same url (`--globoff` keeps curl from reading the
`{{...}}` in an ntfy url as a pattern):

```
curl --globoff -H "Content-Type: application/json" \
  -d '{"title": "likearr: test", "body": "Checking the webhook.", "type": "info"}' \
  '<your [health.webhook] url>'
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok - ran cleanly, nothing *newly* wrong (also `degraded`, `skipped` and `paused`; read `status`) |
| 1 | error - a source, MusicBrainz, or Lidarr failure aborted the run |
| 2 | guarded - a guard blocked some unmonitors; the rest of the run still applied |
| 3 | stale plan - `--apply` refused because the reviewed diff (or promote-save plan) no longer matches reality |
| 4 | busy - a hand-run command found another run holding the run lock and did nothing; run it again when that one ends. A *scheduled* run that finds the lock held exits 0 (`skipped`) instead |

Alert on non-zero. Treat 2 as "look at it soon," not "look at it now" - it's a safety guard
working as intended, not a crash. 4 comes only from a hand-run command, never from a scheduled run.

## Backup and restore

Everything likearr needs to remember lives in `/data` (the volume mounted at `./likearr-data` on
the host), plus the `.env` file beside `compose.yaml`, which sits outside `/data` and is not part
of the volume. Back up both.

### What to back up

- **The state database** - the file named by `[state] db` in `config.toml` (`state.sqlite` in the
  example config; a real install may use another name), plus its `-wal` and `-shm` files if
  present. It is likearr's only record of what it owns - every release it has ever monitored, why,
  and which artists it added - and it holds the health baseline (the previous apply's condition
  identities, which is how a run tells "newly wrong" from "wrong for months"). Losing it doesn't
  touch your Lidarr library, but the next run treats everything as unowned: `run` unmonitors
  nothing, but `adopt` is not a safe way to rebuild it (it unmonitors whatever no source backs) -
  restore this file instead (see "Adopting a pre-existing Lidarr library", above). If it goes
  missing while Lidarr keeps the artists likearr tagged, every run logs a warning, Status says so,
  and `doctor` fails: restore this file from backup. The schema
  upgrades in place when a newer likearr opens an older file, so nothing here needs a step at
  deploy time - see ["Upgrade and roll back"](#upgrade-and-roll-back), below, for the full
  procedure.
- **`config.toml`** - every setting saved from the browser (see "Web UI", above). The rotating
  `config.toml.bak-*` files beside it (newest 30 kept) are optional; the live file is what
  matters.
- **`spotify-token.json`** - the Spotify sign-in. Losing it means redoing first-time Spotify auth
  (see "First run: authenticate with Spotify", above). Keep it mode 0600.
  `spotify-token.json.lock` holds no content of its own and needs no backup.
- **`ui/prune-ledger.json`** - past Clean up decisions (see "Clean up remembers earlier decisions",
  above). Losing it means every album needs a decision again the next time you export a review.
- **`ui/playlist-names.json`** - cosmetic playlist names, restored with one press of "Refresh names
  from Spotify" on Settings.
- **`ui/spotify-snapshot.json`** - a regenerable cache of a cancelled scheduled run's Spotify read.
- **`ui/jobs`, `last-run.json`, and the `diff.json` / `adopt.json` / `prune.json` /
  `promote-save.json` output files** - history only, safe to skip.
- **`.env`** beside `compose.yaml` - the Lidarr API key, Spotify client ID, and whatever UI
  password and MQTT credentials you set. Outside `/data`, but back it up too, and treat the backup
  as secret: it holds every credential likearr reads.

### Getting a consistent copy of the database while the service runs

likearr opens the state database in WAL mode, so a plain file copy taken while a write might be
happening can be inconsistent. For the always-on service, the closest thing to a safe moment for a
plain copy is while Status shows no job running - but a scheduled run can start at any time, so
prefer one of these two instead:

- **Online copy, service left running.** Python's stdlib `sqlite3` module is already in the image
  - likearr itself opens the database with it - and takes a consistent copy without stopping
  anything:

  ```
  docker compose exec likearr python -c "import sqlite3; s=sqlite3.connect('/data/state.sqlite'); d=sqlite3.connect('/data/state-backup.sqlite'); s.backup(d); d.close()"
  docker compose cp likearr:/data/state-backup.sqlite ./likearr-state-$(date +%F).sqlite
  docker compose exec likearr rm /data/state-backup.sqlite
  ```

  Use the path your `[state] db` actually names if it isn't `state.sqlite`.

- **Stop, copy, start.** `docker compose stop likearr` (this can wait up to 30 minutes for an
  apply in progress to finish - `stop_grace_period: 30m` in the example compose file), copy the
  whole `./likearr-data` directory, then `docker compose up -d`.

Either way, copy the whole data directory, not just the database, so the rest of the files above
move with it.

### Restore

1. Stop the service: `docker compose stop likearr`.
2. Put the backed-up files back into `./likearr-data`, owned by the image's user (`LIKEARR_UID` /
   `LIKEARR_GID`, default 1000:1000 - see "Option B: Docker", above). Keep `spotify-token.json` at
   mode 0600.
3. Put the `.env` file back beside `compose.yaml`.
4. Start the service: `docker compose up -d`.
5. Run Doctor from Settings to confirm config parses, the Spotify token is valid, and Lidarr is
   reachable.

The state database can be a few hundred MB on a large library - keep backup copies outside `/data`
rather than piling them up beside the live file.

## Upgrade and roll back

### Upgrade

1. Back up `/data` and the compose `.env` file - see ["Backup and restore"](#backup-and-restore),
   above.
2. Read `CHANGELOG.md` for the version you are upgrading to.
3. Get the new image: bump the version tag in `compose.yaml` and `docker compose pull`. Building
   it yourself instead: `docker compose up -d --build` from a fresh `git pull` of this checkout.
4. `docker compose up -d`. The stop can wait up to 30 minutes for an apply in progress to finish
   (`stop_grace_period: 30m` in `deploy/compose.example.yaml`) - see ["Redeploys and scheduled
   runs"](#redeploys-and-scheduled-runs), above, for what happens to a run caught mid-redeploy.
5. Check Status, then run Doctor from Settings.

Schema changes happen on their own the first time the new image opens the database - there is no
separate migration step to run by hand. Every schema change so far has been additive (a new table
or a new column with a default), and none discards data. A version can also run a one-time step of
its own on its first start after an upgrade; check that version's entry in `CHANGELOG.md`, under
"Upgrade notes", for whether this one has any.

### Roll back

Start the previous image against the same `/data`. This works because the schema version is only
ever raised, never lowered, and older code ignores any table or column it does not know about -
it holds only while migrations stay additive, which is true of every schema version so far. The
same goes for files outside the schema, such as `last-run.json`: an older image that predates one
simply ignores it, so a rollback needs nothing extra there either.

Restore `/data` from the pre-upgrade backup only if the release notes for the version you are
leaving say to. Per-release rollback caveats live in that release's entry in `CHANGELOG.md`, under
"Upgrade notes" - not in this guide.
