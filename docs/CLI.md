# likearr CLI

The web UI covers day-to-day use (see the [README](../README.md)). This page is for hand work and scripting.

The CLI is the same engine the service runs: every run, scheduled or from the UI, is a child
process of this binary. Use it directly for first-time setup, the commands that stay CLI-only by
design (`prune-stage --apply`, `promote-save --apply`), and anything you'd rather script.

```bash
docker compose run --rm likearr-cli <command> -c /data/config.toml   # in the compose stack
likearr <command> -c config.toml                                     # from-source install, see docs/DEPLOY.md#install
```

## Commands

| Command | Does |
|---|---|
| `run [--out DIFF] [--apply [DIFF]] [--scheduled] [--accept-shrink] [--accept-health]` | Dry-run by default; `--apply DIFF` executes a reviewed diff; `--scheduled --apply` (no file) plans and applies in one pass with the unmonitor cap. `--scheduled` alone is still only a dry-run. `--accept-shrink` (hand-run plans only, never with `--scheduled` or `--apply`) accepts the shrinks the plan sees and records that in the diff. `--accept-health` (on a hand-run apply) accepts this run's new collisions, skipped artists and oversized catalogues as the baseline, so later runs stop calling them new. `--force` applies a stale diff (or one planned under different rules) anyway: rarely what you want, and never offered by the web UI |
| `auth [--manual] [--promote-save]` | Spotify sign-in, read-only by default; `--promote-save` also asks for the write access `promote-save` needs, and a re-auth keeps write access the current token already has. `--manual` prints the URL and accepts the pasted redirect (works over SSH). Spotify refresh tokens die six months after sign-in; `auth` records the date and prints when to re-authorize. Settings' "Connect Spotify" / "Re-authorize Spotify" runs the same flow from the browser - see [docs/DEPLOY.md](DEPLOY.md#first-run-authenticate-with-spotify) |
| `playlists [--json]` | Every Spotify playlist your account lists: the ones a run can read (owned, or collaborative once re-authorized), the ones it can't and why, and which are already in `[spotify] playlists`. Changes nothing |
| `start [--host] [--port 8770]` | The likearr service: web UI, scheduler and job runner (see [Using likearr](../README.md#using-likearr)). Needs `LIKEARR_UI_PASSWORD` (16+ characters); see [docs/DEPLOY.md](DEPLOY.md#web-ui) |
| `explain <artist, title or MBID>` | Why is this monitored, pending, unmapped or untouched? |
| `adopt [--keep FILE] [--out PLAN]` / `--apply PLAN` | One-time takeover of releases monitored before likearr existed. Plans first (`--keep` belongs to the plan); `--apply PLAN` executes exactly that reviewed file, refuses a stale one, and holds the run lock. On a shared Lidarr, see [docs/DEPLOY.md](DEPLOY.md#running-more-than-one-instance) |
| `doctor [--no-spotify] [--json]` | Config, auth, schema canary, Lidarr version, root folder and profiles, MusicBrainz reachability, a FAIL on any two Lidarr artists sharing a name, and a FAIL on an unmonitored artist holding a release likearr monitored. A spent Spotify quota is one FAIL line with Spotify's retry-after, and the remaining Spotify checks are skipped rather than spending more. Writes nothing. `--json` feeds the read-only Doctor section of the web UI's Settings |
| `setup-profiles [--apply] [--json]` | Create the Lean / Full metadata profiles, the `likearr` tag, and set root-folder defaults to "none". An existing metadata profile of the same name that differs from what likearr would create is left as it is - never overwritten. An existing root folder is not: likearr owns its monitor defaults, so `--apply` resets them to none / none if they differ, shown in the preview first. `--json` is Settings' Lidarr setup panel, which previews with this and applies the same way (a second confirm, then `--apply` as a job) |
| `promote-save --decisions FILE --reviewed FILE [--out PLAN]` / `--apply PLAN` | Clean up's Spotify decisions, applied to **Spotify**: follow every `promote` artist, save every `save` artist's kept albums (minus any you set to Keep) and every album you chose to save on its own. Plans first; `--apply` refuses a plan whose inputs moved. `--reviewed` is mandatory for any save. Needs the write scopes, which a sign-in asks for only with `auth --promote-save` - see [docs/DEPLOY.md](DEPLOY.md#when-likearrs-scopes-change-you-re-authorize) |
| `prune-report [--out prune.json]` | Every album with files on disk that no Spotify source backs and likearr does not own, each with why nothing asks for it; an album holding the only copy of a liked or playlist song is listed as protected |
| `prune-stage --manifest prune.json --holding DIR (--artists A,B \| --all-candidates \| --decisions FILE) [--apply] [--out FILE]` | Move those files to a holding folder, then tell Lidarr: remove the rows of artists left with nothing, rescan the rest. The preview checks the mount and prints every move and the Lidarr plan (`--out` writes it as JSON). `--apply` renames only, journals each move, refuses a manifest with an album that has gained a Spotify reason or likearr ownership since the report was written, and names it. `--no-mount-check` is for the web UI's preview and is refused with `--apply`. Deleting is your job. On a shared Lidarr, see [docs/DEPLOY.md](DEPLOY.md#running-more-than-one-instance) |
| `prune-checks [--out FILE]` | Before a stage: Lidarr import lists with automatic add (they would re-add what you remove) and whether Lidarr's command queue is idle. Read-only |
| `lidarr-files --plan DIFF [--out FILE] [--json]` | The track files Lidarr holds for each release a plan would unmonitor. Read-only |

Clean up is off by default (`[prune] enabled`, see [docs/DEPLOY.md](DEPLOY.md)). `prune-report`,
`prune-stage`, `prune-checks` and `promote-save` run either way; while it is off each prints one
`WARN` line saying so first.

Exit codes: `0` ok, `1` error, `2` guarded (something was refused, look at the output), `3` stale diff,
`4` busy (a hand-run command found another run holding the lock; a scheduled run exits 0 instead).

## `promote-save`

Reviewing a library produces decisions in both directions. `prune-stage` applies the "throw it
away" half; `promote-save` applies the other half, on Spotify rather than in Lidarr:

```bash
likearr promote-save --decisions decisions.json --reviewed review-data.json   # plans only
likearr promote-save --apply promote-save.json                                # writes to Spotify
```

**likearr never writes to Spotify on its own.** There is no scheduled mode for this and no rule
that triggers it: `promote-save` is a one-time reconciliation of decisions you made by hand, run
by you, from a plan you read first. Saving an album or following an artist is a high-intent
action on a personal account, so nothing in likearr does it as a side effect. In particular, a
release monitored because of a playlist, a liked track, or a followed artist's catalogue will
never be saved or followed - those rules exist to get tracks into Lidarr, and that is where they
stop.

- **`promote` artists get followed**, so the normal rules monitor their albums and EPs from the
  next run on.
- **`save` artists get their reviewed albums saved** - the albums of theirs that you actually
  saw and kept during the review, and that still have files. `--reviewed` (the review page's
  `review-data.json`) is what says which those were, and it is **required**: without it the
  command refuses to plan any save rather than falling back to whatever the library holds today.
  That fallback was a real bug: between a review and a plan the library keeps growing (a followed
  artist's back catalogue, liked-track pickups), and none of those albums was ever a decision. The
  plan counts and lists everything it excluded, so the number is visible.
- **It never guesses.** MusicBrainz ids have no Spotify equivalent, so each is mapped in tiers,
  best first:
  1. **MusicBrainz's own Spotify link** - it records a "free streaming" relationship pointing at
     `open.spotify.com`. An editor asserted that identity, so there is nothing to compare and
     nothing to get wrong, and it costs no Spotify quota at all.
  2. **A UPC search**, where MusicBrainz has a barcode. Albums only - an artist has no barcode.
     A barcode identifies a release; a title only describes one.
  3. **A name or title search**, accepted only on an unambiguous match. *Ghosts* is not
     *Ghosts I-IV*, and a right title by the wrong artist is refused.

  Every match records which tier found it, and the summary prints the breakdown, so you can skim
  tier 1 and look hard at tier 3. Anything not confidently matched is listed as `unmatched`, in
  the plan and on screen, for you to sort out by hand. A wrong match quietly saves a stranger's
  record into your library; a miss is a line of output.
- **It is safe to re-run.** It reads what you already follow and have already saved before
  writing, so a second pass writes nothing, and every search it makes is cached, so re-planning
  is free. (It reads those two lists in full rather than using Spotify's `/contains` endpoints,
  which return 403 for a Development Mode app.)

It needs two write scopes your token may not have: sign-in is read-only by default, so run
`likearr auth --manual --promote-save` once first - see [docs/DEPLOY.md](DEPLOY.md#when-likearrs-scopes-change-you-re-authorize).
