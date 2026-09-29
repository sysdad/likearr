# likearr CLI

The web UI covers day-to-day use. Use the CLI for scripting and for the steps the UI hands you as
commands (`prune-stage --apply`, `promote-save --apply`).

```bash
docker compose run --rm likearr-cli <command> -c /data/config.toml   # Compose
likearr <command> -c config.toml                                     # from source
```

Every command takes `-c FILE` (default: `$LIKEARR_CONFIG` or `./config.toml`) and `-v` for debug
logging. Exit codes are in [Troubleshooting](troubleshooting.md#exit-codes).

## Commands

| Command | Does |
|---|---|
| `run` | Plans a run and writes `diff.json`; applies a reviewed plan with `--apply`. |
| `auth` | Connects likearr to your Spotify account. |
| `doctor` | Checks config, Lidarr, MusicBrainz and Spotify. Changes nothing. |
| `setup-profiles` | Creates the metadata profiles, the tag and the root folder defaults in Lidarr. |
| `adopt` | Lets likearr manage albums you already monitor that match what you like. |
| `playlists` | Lists your Spotify playlists and which ones a run can read. Changes nothing. |
| `explain` | Says why a release, song or artist is or isn't monitored. |
| `lidarr-files` | Lists the files Lidarr holds for releases a plan would unmonitor. Changes nothing. |
| `prune-report` | Lists albums on disk that no Spotify source wants. |
| `prune-checks` | Checks Lidarr's import lists and command queue before a stage. Changes nothing. |
| `prune-stage` | Moves unwanted albums to a holding folder. Never deletes. |
| `promote-save` | Follows artists and saves albums on Spotify, from a Clean up review. |
| `start` | Runs the service: web UI, scheduler and jobs. |

## Flags

### `run`

- `--out FILE` - where to write the plan (default `diff.json`).
- `--apply [FILE]` - apply that reviewed plan (default: the `--out` path). Refused if Spotify,
  Lidarr or your settings changed since.
- `--scheduled` - unattended: with `--apply` and no file, plans and applies in one go. Refuses to
  unmonitor anything if the plan would unmonitor more than `[guards] max_unmonitors_scheduled`.
- `--accept-shrink` - on a plan: let a source or artist that shrank unmonitor anyway.
- `--accept-health` - on an apply: stop reporting this run's new problems as new.
- `--force` - apply a stale plan anyway. Rarely what you want.
- `--claim-existing` - on a first apply: let likearr manage the albums you already monitor that
  match what you like (the plan's "Albums you already monitor").
- `--unmonitor-rest` - on a first apply: unmonitor the albums you already monitor that match
  nothing you like. Held albums stay monitored.
- `--keep FILE` - with `--unmonitor-rest`: release group MBIDs to leave monitored, one per line.

### `auth`

- `--manual` - print the URL and paste the address you land on back into the prompt. Use this on a
  machine with no browser.
- `--promote-save` - also ask for the write access `promote-save` needs. Sign-in is read-only by
  default.

### `doctor`

- `--no-spotify` - skip the Spotify checks.
- `--json` - one line of JSON.

### `setup-profiles`

- `--apply` - make the changes. Without it, shows them.
- `--json` - one line of JSON.

### `adopt`

By default it only claims: albums you monitor that match what you like become likearr's, and the
rest are left as they are. The plan says which mode it is in, and `--apply` does exactly that.

- `--unmonitor-rest` - also unmonitor every monitored album nothing on Spotify wants and the keep
  file doesn't list. Albums MusicBrainz couldn't check this run are held, not unmonitored.
- `--keep FILE` - with `--unmonitor-rest` only: releases and artists to keep monitored, one release
  group MBID or `artist:<mbid>` per line.
- `--out FILE` - where to write the plan (default `adopt.json`).
- `--apply [FILE]` - apply that reviewed plan. Refused if those albums changed since.

### `playlists`

- `--json` - one line of JSON.

### `explain QUERY`

`QUERY` is an artist, release title, song or MBID.

- `--from-last-run` - answer from the last run, without asking Spotify or Lidarr.
- `--json` - one line of JSON.

### `lidarr-files`

- `--plan FILE` - the plan to read (required).
- `--out FILE` - also write the answer as JSON.
- `--json` - one line of JSON.

### `prune-report`

- `--out FILE` - where to write the report (default `prune.json`).

### `prune-checks`

- `--out FILE` - also write the answer as JSON.

### `prune-stage`

Needs the library mounted: see [Clean up setup](install.md#clean-up-setup).

- `--manifest FILE` - the `prune-report` output to act on (required).
- `--holding DIR` - where to move files, beside the library on the same filesystem (required).
- One of `--artists A,B` (names or MBIDs), `--all-candidates`, or `--decisions FILE` (see
  [Decisions file](#decisions-file)).
- `--apply` - move the files. Without it, shows the moves and what Lidarr will be told.
- `--out FILE` - also write the preview as JSON.
- `--no-mount-check` - preview without checking the mount. Refused with `--apply`.

### `promote-save`

Needs write access: run `likearr auth --manual --promote-save` first.

- `--decisions FILE` - the decisions file from Clean up (required to plan).
- `--reviewed FILE` - the `review-data.json` Clean up exported (required to plan any save).
- `--out FILE` - where to write the plan (default `promote-save.json`).
- `--apply [FILE]` - write that reviewed plan to Spotify.
- `--force` - apply a stale plan anyway.

### `start`

Needs `LIKEARR_UI_PASSWORD` (16 or more characters).

- `--host ADDRESS` - address to listen on (default `127.0.0.1`; the image uses `0.0.0.0`).
- `--port PORT` - port to listen on (default `8770`).

## Decisions file

Clean up exports this file. You can also write it by hand:

```json
{"version": 1, "trash": ["<release group mbid>"], "trash_artists": ["<artist mbid>"],
 "promote": ["<artist mbid>"], "save": ["<artist mbid>"], "save_releases": ["<release group mbid>"],
 "save_exclude_releases": ["<release group mbid>"], "notes": "free text"}
```

- `prune-stage` reads `trash` (albums to move) and `trash_artists` (every candidate album of those
  artists). It refuses an album that holds the only copy of a liked or playlist song.
- `promote-save` reads `promote` (artists to follow), `save` (artists whose reviewed albums to
  save), `save_releases` (single albums to save) and `save_exclude_releases` (albums of a `save`
  artist to leave alone).
