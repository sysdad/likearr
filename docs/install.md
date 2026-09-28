# Installing likearr

The [Quick start](../README.md#quick-start) is the shortest install: one service, set up in the
browser. This page covers the rest.

Commands below are written as `likearr <command> -c /data/config.toml`. Under Compose, run them as
`docker compose run --rm likearr-cli <command> -c /data/config.toml`, which needs the `likearr-cli`
service from [The full compose file](#the-full-compose-file).

## Spotify app setup

Create a Spotify developer app and put its Client ID in `LIKEARR_SPOTIFY_CLIENT_ID`. Follow
[`docs/spotify.md`](spotify.md). Two steps people miss: register the redirect URI exactly, and add
the account likearr reads to the app's **User Management** list.

## Settings and secrets

Settings live in two places:

- **Environment variables** hold the secrets and where likearr runs: `LIKEARR_LIDARR_URL`,
  `LIKEARR_LIDARR_API_KEY`, `LIKEARR_SPOTIFY_CLIENT_ID`, `LIKEARR_UI_PASSWORD`, and optionally
  `LIKEARR_ALLOWED_HOSTS`, `LIKEARR_MUSICBRAINZ_CONTACT` and `LIKEARR_MQTT_USERNAME` /
  `LIKEARR_MQTT_PASSWORD`. [`deploy/env.example`](../deploy/env.example) describes each one. Put
  them in `.env` beside `compose.yaml` and run `chmod 600 .env`. Never commit it.
- **`config.toml`** in the data folder holds everything Settings edits. likearr writes it on first
  start. [`deploy/config.example.toml`](../deploy/config.example.toml) is the reference for every
  key. A `[lidarr] url`, `[ui] allowed_hosts` or `[musicbrainz] contact` in it fails the load: set
  the environment variable it names instead.

`config.toml` holds a secret when `[health.webhook] url` carries a token. Keep it private
(`chmod o-rwx likearr-data/config.toml`) and don't paste it into a forum post.

`LIKEARR_LIDARR_URL` is how the likearr container reaches Lidarr. `http://lidarr:8686` works only
when both containers share a Docker network; otherwise use Lidarr's LAN address and port.

## The full compose file

For hand commands, Clean up or a second instance, copy
[`deploy/compose.example.yaml`](../deploy/compose.example.yaml) to `compose.yaml` instead of the
Quick start block, and fill in `deploy/env.example` as `.env` beside it. It adds the `likearr-cli`
service, which runs one command and exits.

**The data folder must be writable by the container's user**, uid 1000 by default. Before the first
`docker compose up -d`, run:

```
mkdir -p likearr-data && sudo chown -R 1000:1000 likearr-data
```

On a NAS the owner is usually another uid (Synology from 1026, Unraid 99, TrueNAS apps 568). To run
the container as that uid instead, uncomment each service's `build:` block in the compose file, set
`LIKEARR_UID` and `LIKEARR_GID` to the folder's owner, and run `docker compose up -d --build` from a
clone of this repository.

Without Compose, run the same image with `docker run`:

```
docker run -d --name likearr --env-file .env -v "$PWD/likearr-data:/data" -p 8770:8770 \
  --memory 768m --stop-timeout 1800 --restart unless-stopped ghcr.io/sysdad/likearr:0.5.1
```

## Lidarr setup

likearr needs two metadata profiles (Lean and Full), the `likearr` tag, and a root folder whose
defaults monitor nothing.

In the browser: **Settings -> Preview Lidarr setup** shows what's missing, and **Apply** creates it.
An existing metadata profile with the same name is never changed. An existing root folder's monitor
defaults are reset to none; the preview says so first.

Then pick the **root folder** and **quality profile** likearr adds artists with, from Lidarr's own
lists. A Lidarr with one root folder has it picked for you. No run plans until both are set.

From a terminal:

```
likearr setup-profiles -c /data/config.toml           # show what's missing
likearr setup-profiles --apply -c /data/config.toml   # create it
```

Or set `root_folder` and `quality_profile` under `[lidarr]` in `config.toml`.

## First run

### Connect Spotify

In the browser: **Settings -> Connect Spotify**, approve on Spotify's page, and paste the address it
sends you to back into Settings. That page fails to load; copy the address anyway. With
`[ui] public_url` set to likearr's own `https://` address, Spotify sends you straight back instead.
See [`docs/spotify.md`](spotify.md#connecting-and-re-authorizing).

From a terminal, on any machine:

```
likearr auth --manual -c /data/config.toml
```

Open the printed URL, approve, and paste the address you land on into the prompt. It writes the
token file (`[spotify] token_file`) beside the config file. If you ran it on another machine, copy
the token file into the data folder and `chmod 600` it.

### Check the setup

**Settings -> Doctor -> Run checks**, or `likearr doctor -c /data/config.toml`. It checks the config,
the Spotify token, Lidarr, the root folder and profiles, and MusicBrainz, and changes nothing. See
[Common doctor failures](troubleshooting.md#common-doctor-failures).

### Plan and apply

In the browser: **Review changes -> Check for changes**, read the plan, then **Apply**.

From a terminal:

```
likearr run -c /data/config.toml                       # plan only: writes diff.json
likearr run --apply diff.json -c /data/config.toml     # apply exactly that plan
```

The apply is refused (exit 3) if Spotify, Lidarr or your settings changed since the plan; plan
again. The first plan reads every song through MusicBrainz at one request a second, so it can take
several hours for a few thousand songs. Later plans take minutes.

In `diff.json`, `add_artists`, `monitor` and `unmonitor` are what the apply changes, and `guards`
lists anything held back. For a first plan, `unmonitor` is empty.

Scheduled runs start after this first reviewed apply. Until then each one changes nothing.

## Taking over an existing library

You don't need this for a library you curated by hand: likearr never unmonitors a release it didn't
monitor, so a plain run leaves your existing monitors alone.

`adopt` is for a library that grew mostly from Lidarr's own import lists. It claims every monitored
release a Spotify source still wants, keeps what you list, and **unmonitors everything else,
including releases you monitored by hand**.

1. Disable Lidarr's own Spotify import lists.
2. Write a keep file: one release group MBID, or `artist:<mbid>`, per line.
3. Plan, read `adopt.json`, then apply:

   ```
   likearr adopt --keep keep.txt --out adopt.json -c /data/config.toml
   likearr adopt --apply adopt.json -c /data/config.toml
   ```

4. Then plan and apply a normal run, as in [First run](#plan-and-apply).

Don't use `adopt` to recover a lost state database: it would unmonitor every hand-monitored
release. [Restore from backup](troubleshooting.md#restoring-from-backup) instead.

## More than one instance or Spotify account

One likearr instance reads one Spotify account. For a household, run one instance per account, all
pointing at the same Lidarr.

1. Share one Spotify developer app: add each account to its **User Management** list (up to five).
   See [`docs/spotify.md`](spotify.md#adding-another-person). The accounts share the app's request
   quota.
2. Give each instance its own data folder, port, `LIKEARR_UI_PASSWORD` and env file. The
   commented-out `likearr-2` service in `deploy/compose.example.yaml` is a template.
3. Connect each instance from a browser signed in to that instance's Spotify account. Spotify's
   page names the account; use its "Not you?" link to switch.

On a shared Lidarr:

- If one person unlikes a release the other still wants, it is unmonitored and then monitored again
  by the other instance's next run.
- Don't run `adopt` on a second instance. If you must, list everything the first instance and the
  other person want in its keep file.
- Clean up lists everything the other person likes as unneeded. Check every candidate against both
  accounts before you stage it.

## Scheduling

The service runs its own schedule; there is no host cron to set up. In **Settings**, set the cron
line (five fields) and timezone. Settings shows the next few runs before you save. The default is
`20 */6 * * *` in UTC. A schedule can't run more often than every 60 minutes.

- **Pause and resume** in Settings. Paused, a scheduled run does nothing and reports `paused`.
  Plans you review and apply yourself still run.
- **Run and apply now** on Status starts a scheduled run straight away.
- A run missed while the service was down is caught up once, five minutes after it starts.
- If a job is already running when a run is due, the run waits up to an hour, then is skipped.

A scheduled run applies without review, with one limit: if it would unmonitor more than
`[guards] max_unmonitors_scheduled` releases (100), it unmonitors none of them and reports
`guarded`. After changing a rule such as `liked_track_scope` or an opt-out, check for changes and
apply by hand rather than waiting for the schedule.

## Health and Home Assistant

Every run writes a one-line JSON health record to stdout and the run history. Dry runs stop there.
Other runs also go to MQTT and a webhook, if configured.

The `status` field is one of:

| Status | Meaning | Alert? |
|---|---|---|
| `ok` | Nothing newly wrong | no |
| `paused` | Scheduled runs are paused, or waiting for the first reviewed apply | no |
| `skipped` | Another run held the lock, or Spotify's quota was spent | no |
| `degraded` | Something is newly wrong; `new_conditions` says what | yes |
| `guarded` | A guard held back unmonitors | yes |
| `stale` | An apply was refused because things changed since the plan | yes |
| `error` | The run failed | yes |

See [Troubleshooting](troubleshooting.md) for what to do about each.

### MQTT

Uncomment `[health.mqtt]` in `config.toml` and set `host` and `topic`. If the broker needs a login,
set `LIKEARR_MQTT_USERNAME` and `LIKEARR_MQTT_PASSWORD`. Records are published retained.

A Home Assistant sensor, a problem flag, and a stale flag that goes unavailable when no run has
reported for 13 hours:

```yaml
mqtt:
  sensor:
    - name: "likearr status"
      state_topic: "likearr/health"
      value_template: "{{ value_json.status }}"
      json_attributes_topic: "likearr/health"
  binary_sensor:
    - name: "likearr problem"
      state_topic: "likearr/health"
      value_template: "{{ 'ON' if value_json.status in ['error', 'stale', 'guarded', 'degraded'] else 'OFF' }}"
      device_class: problem
    - name: "likearr stale"
      state_topic: "likearr/health"
      value_template: "{{ 'ON' if (as_timestamp(now()) - value_json.ts) > 3600 * 13 else 'OFF' }}"
      expire_after: 46800
      device_class: problem
```

13 hours (46800 seconds) suits the default six-hourly schedule. Change both numbers if yours differs, allowing for one missed
run. Enable these after the second applied run: the first has nothing to compare against.

### Webhook

Uncomment `[health.webhook]` and set `url`. likearr POSTs the health record as JSON, plus `title`,
`body` and `type` fields for notification services. Set `notify = "problems"` to send only new
problems and one "back to ok" when they clear. A webhook can't tell you likearr has stopped
running; use the stale check above for that.

[Apprise API](https://github.com/caronc/apprise-api) reads `title`, `body` and `type` as sent:

```toml
[health.webhook]
url = "http://apprise:8000/notify/likearr"
notify = "problems"
```

ntfy needs templating turned on in the URL (ntfy server 2.10.0 or later):

```toml
[health.webhook]
url = "https://ntfy.sh/<your-topic>?tpl=yes&t={{.title}}&m={{.body}}"
notify = "problems"
```

Test the URL once before relying on it:

```
curl --globoff -H "Content-Type: application/json" \
  -d '{"title": "likearr: test", "body": "Checking the webhook.", "type": "info"}' \
  '<your [health.webhook] url>'
```

## Reverse proxy and exposure

Keep likearr on your LAN or a VPN. Don't port-forward it or put it behind a public tunnel: its
login is one shared password.

A reverse proxy for https is optional. Point it at `http://<docker host>:8770`, then:

- Add the proxy's host name to `LIKEARR_ALLOWED_HOSTS`.
- Give likearr its own host name. The session cookie is shared by every service on the same name.
- Make sure the proxy sends `X-Forwarded-Proto: https`. Caddy and Nginx Proxy Manager do. To check,
  look for `secure` in the cookie:

  ```
  read -rs pw; printf %s "$pw" | curl -sk -D - -o /dev/null -X POST https://<proxy name>/login \
    --data-urlencode password@- | grep -i set-cookie; unset pw
  ```

- Optionally set `[ui] public_url` to the proxy's `https://` address, so Connect Spotify needs no
  copy and paste. Register `<public_url>/spotify/callback` in the Spotify app too.
- If the proxy runs on the same host, publish the port on loopback only: `"127.0.0.1:8770:8770"`.

Five failed logins in a minute pause logins from that address for a minute. Behind a proxy, that
pauses everyone using the proxy.

## Clean up setup

Only Clean up (`prune-stage`) needs the music library, and only in the `likearr-cli` service. The
container must see the library at the same path Lidarr does, with the holding folder beside it on
the same filesystem.

### Mounting the library for Clean up

If Lidarr's root folder is `/data/media/music` and the host keeps the library at `/path/to/media`,
add this to `likearr-cli`'s `volumes:`:

```yaml
      - /path/to/media:/data/media
```

Mount the library's parent, not the root folder itself, so the holding folder
(`/data/media/_likearr-holding` by default) is on the same mount. `prune-stage` checks the mount
before it moves anything and says what's wrong.

Turn Clean up on in **Settings > Advanced**. If your compose file isn't the documented one, set
`[ui] cli_command` so the commands Clean up shows run as pasted.

## From source

Needs Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/sysdad/likearr && cd likearr
uv sync
cp deploy/env.example .env && chmod 600 .env    # then fill it in
set -a; . ./.env; set +a                        # likearr doesn't read .env itself
mkdir -p likearr-data
uv run likearr start -c likearr-data/config.toml    # http://127.0.0.1:8770
```

`start` writes `config.toml` on first start, and keeps the token and state database beside it. Load `.env` the same way in every shell you run likearr
from; otherwise it fails with "LIKEARR_LIDARR_API_KEY is not set". To upgrade, `git pull && uv sync`
and restart.
