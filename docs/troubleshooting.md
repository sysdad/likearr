# Troubleshooting

Start with **Settings -> Doctor -> Run checks** (or `likearr doctor -c /data/config.toml`). It
changes nothing and works even when a run can't.

Commands below are written as `likearr <command> -c /data/config.toml`. Under Compose, run them as
`docker compose run --rm likearr-cli <command> -c /data/config.toml`.

## The container is unhealthy

The container can't write its data folder. Settings saves and Connect Spotify fail with an error
too. The container runs as uid 1000 by default:

```
docker compose down
sudo chown -R 1000:1000 likearr-data
docker compose up -d
```

If you built the image with other `LIKEARR_UID` / `LIKEARR_GID` values, use those. Check
`docker compose logs likearr` for the path it couldn't write.

## The service won't start

`docker compose logs likearr` names the problem. The common ones:

- **`LIKEARR_UI_PASSWORD`** is missing or shorter than 16 characters.
- **A setting in `config.toml`** is misspelt, quoted when it should be a number, or out of range.
  The message names the key and suggests the right one.
- **`[lidarr] url`, `[ui] allowed_hosts` or `[musicbrainz] contact`** is in `config.toml`. Remove it
  and set the environment variable the message names.
- **A mistake in `[ui]` or `LIKEARR_ALLOWED_HOSTS`.** The message names it.

## A run needs attention

Status says what happened in words. By health status:

- **`guarded`**: a guard held back unmonitors; everything else applied.
  - *A source or artist shrank* (for example, you emptied a playlist). If the shrink is real,
    check for changes with "Let this check unmonitor what a shrink guard held back" ticked, read
    the plan and apply it. From a terminal: `likearr run --accept-shrink`, then
    `likearr run --apply diff.json`. If it isn't real, wait: the guard holds until the count
    recovers.
  - *Over the scheduled cap*: a scheduled run that would unmonitor more than 100 releases
    unmonitors none of them. Check for changes, read the plan and apply it by hand.
- **`degraded`**: something is newly wrong. The message names it: an artist Lidarr couldn't add or
  refresh, a new name clash, a MusicBrainz or Lidarr metadata outage. Most clear on their own by
  the next run. To stop one you've decided to live with being flagged, tick "Also stop flagging
  these as new problems" when you apply, or `likearr run --apply diff.json --accept-health`.
- **`stale`**: the plan you applied no longer matched Spotify, Lidarr or your settings. Check for
  changes again and apply the new plan.
- **`error`**: the run stopped. The message says why. If
  Spotify's quota is spent, wait for it to recover and don't run extra checks meanwhile.

An artist Lidarr can't add because its metadata doesn't know them yet is retried every run. It can
take weeks for Lidarr's metadata to catch up.

## Lidarr looks stuck

likearr waits on Lidarr's `RefreshArtist` commands, up to 300 seconds plus 2 seconds per release,
capped at an hour per artist. Anything else using Lidarr's command queue waits behind them. Look at
the queue before restarting anything, with `.env` loaded (`set -a; . ./.env; set +a`):

```
curl -s -H "X-Api-Key: $LIKEARR_LIDARR_API_KEY" "$LIKEARR_LIDARR_URL/api/v1/command" \
  | jq '.[] | select(.status=="queued" or .status=="started") | {id, name, status}'
```

Cancel a queued command with
`curl -X DELETE -H "X-Api-Key: $LIKEARR_LIDARR_API_KEY" "$LIKEARR_LIDARR_URL/api/v1/command/<id>"`.
A started one has to finish. Never delete an artist in Lidarr while a `RefreshArtist` for it is
queued or running.

Before you restart or upgrade likearr, check Status for a running apply and let it finish. Stopping
the container waits up to 30 minutes for one. A scheduled run that is still planning is simply
run again after the restart.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK. Also `degraded`, `paused` and `skipped`: read the status | Nothing, or read the status |
| 1 | Error: Spotify, MusicBrainz or Lidarr failed | Read the message; run doctor |
| 2 | Guarded: some unmonitors were held back, the rest applied | See [A run needs attention](#a-run-needs-attention) |
| 3 | Stale: the plan no longer matches | Plan again and apply the new plan |
| 4 | Busy: another run holds the lock, nothing was done | Run it again when the other one finishes |

A scheduled run that finds the lock held exits 0 with status `skipped`.

## Failed Spotify connects

See [If connecting fails](spotify.md#if-connecting-fails) in `docs/spotify.md` for each message
Settings shows. The usual causes:

- **Invalid redirect URI.** Register `http://127.0.0.1:8765/callback` in the Spotify app, exactly.
  With `[ui] public_url` set, register `<public_url>/spotify/callback` as well.
- **`403`, or "Spotify won't let likearr use that account".** Add the account to the app's
  **User Management** list.
- **Runs fail with a Spotify authorization error.** Spotify authorizations last six months. Use
  **Settings -> Re-authorize Spotify**. Status warns 30 days ahead.
- **`QUOTA_EXCEEDED`.** Every app on your Spotify developer account shares one quota. Stop other
  scripts using it and wait. Scheduled runs skip themselves until it recovers.

## Common doctor failures

| Check | What to do |
|---|---|
| `lidarr` | Check `LIKEARR_LIDARR_URL` from inside the container, and `LIKEARR_LIDARR_API_KEY`. `http://lidarr:8686` only works on a shared Docker network. |
| `root folder` / `quality profile` | Pick them in **Settings -> Lidarr setup**, or fix the name in `config.toml` to one Lidarr lists. |
| `lean profile` / `full profile` (WARN) | **Settings -> Preview Lidarr setup -> Apply**, or `likearr setup-profiles --apply`. |
| `tag` (WARN) | Nothing: the tag is created on the first apply. |
| `state matches lidarr` | The state database was lost or replaced. [Restore it from backup](#restoring-from-backup). |
| `duplicate artists` | Two Lidarr artists share a name, so Lidarr can't import for either. Delete the one you don't want in Lidarr (the artist, not the files). |
| `unmonitored artists` | Check for changes and apply; the apply re-monitors them. |
| `musicbrainz` | MusicBrainz is unreachable or slow. Runs retry; check your network if it persists. |
| `spotify` (WARN, not configured) | Set `LIKEARR_SPOTIFY_CLIENT_ID`, then Connect Spotify. |
| `spotify token` | Re-authorize in Settings. |
| `spotify quota` | See `QUOTA_EXCEEDED` [above](#failed-spotify-connects). |

## Restoring from backup

Everything likearr keeps is in the data folder (`likearr-data`), plus `.env` beside `compose.yaml`.
Back up both, and keep the copies private: they hold your credentials. The state database is the
one file that matters most; without it likearr no longer knows which releases it monitored.

To copy the database while the service runs:

```
docker compose exec likearr python -c "import sqlite3; s=sqlite3.connect('/data/state.sqlite'); d=sqlite3.connect('/data/state-backup.sqlite'); s.backup(d); d.close()"
docker compose cp likearr:/data/state-backup.sqlite ./likearr-state-$(date +%F).sqlite
docker compose exec likearr rm /data/state-backup.sqlite
```

Or stop the service (`docker compose stop likearr`), copy the whole `likearr-data` folder, and start
it again.

To restore:

1. `docker compose stop likearr`
2. Put the backed-up files back in `likearr-data`, owned by uid 1000 (or your `LIKEARR_UID` /
   `LIKEARR_GID`). Keep `spotify-token.json` at mode 600.
3. Put `.env` back beside `compose.yaml`.
4. `docker compose up -d`, then run Doctor.

Without a backup of the state database, likearr treats every release as not its own, so it never
unmonitors anything it monitored before. Don't use `adopt --unmonitor-rest` to rebuild it: it
unmonitors every release no Spotify source wants, including ones you monitored by hand.

## Rolling back an upgrade

Set the image tag in `compose.yaml` back to the version you had, then
`docker compose pull && docker compose up -d`. The older version runs against the same data folder.
Restore the data folder from your pre-upgrade backup only if the [changelog](../CHANGELOG.md) entry
for the version you're leaving says to.
