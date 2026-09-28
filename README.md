# likearr

Mirror your Spotify follows, saved albums, and Liked Songs into Lidarr - and nothing else.

Not affiliated with Spotify or Lidarr.

Lidarr's own Spotify import lists can add artists, but they can't say *how much* of an artist you
want, they can't read Liked Songs, and when their token dies they fail silently. likearr reads
Spotify directly, works out exactly which MusicBrainz release groups your library should want, and
sets Lidarr's monitoring to match. Unfollow an artist or un-like a song and the matching releases
are unmonitored again. A run never unmonitors anything you monitored by hand.

likearr is a self-hosted service - one container, managed in the browser. It checks Spotify on a
schedule and sets Lidarr's monitoring to match; a CLI is there underneath for hand work and
scripting.

> Status: beta. Defaults such as Clean up's may still change before 1.0. Expect rough edges, and
> read the [safety model](#safety-model) before pointing it at a library you love.

## Screenshots

![Status page showing "All good", the last and next scheduled run, download coverage in Lidarr and what the last applied run changed](docs/images/status.png)

![Review changes page showing a plan: two artists to add, releases to monitor grouped by why they're wanted, and releases to unmonitor](docs/images/review-changes.png)

![Look up page answering why "Abbey Road" is monitored, matched from a liked song to the album on Spotify and in Lidarr](docs/images/look-up.png)

Taken from a demo library seeded from well-known public artists - see
[`scripts/demo_state.py`](scripts/demo_state.py).

## Is this for you?

**For you** if you want your Spotify follows, saved albums and Liked Songs mirrored into a Lidarr
you already run, with the exact releases you like monitored rather than whole discographies.

**Not for you** if you need playlists you neither own nor collaborate on (see
[What can't be synced](#what-cant-be-synced)), or a tool that searches or downloads (that's still
Lidarr and your indexers - see [Getting the music downloaded](#getting-the-music-downloaded)).

## Why not Lidarr's import lists?

Lidarr's metadata profile is per *artist*, not per *source*: it can't say "albums and EPs for the
artists I follow, but only this one single I liked". It has no Liked Songs list, and its lists go
through a shared auth proxy whose token can die without a warning.

If you move to likearr, disable Lidarr's own Spotify import lists first. Otherwise they re-add
every artist Clean up removes.

## Requirements

- A host that can run Docker.
- Lidarr 2.x or 3.x. Any other major version is refused.
- A Spotify Premium account that owns a free developer app in Development Mode - see
  [`docs/spotify.md`](docs/spotify.md). Up to five Spotify accounts can share that app, one likearr
  instance each - see [More than one instance](docs/install.md#more-than-one-instance-or-spotify-account).

## Quick start

No clone needed. Paste this as `compose.yaml` (or into an existing stack) and fill in the values.
To keep secrets out of `compose.yaml`, replace the `environment:` block with `env_file: [.env]` and
fill in [`deploy/env.example`](deploy/env.example) as `.env` beside it. Either way, the file holds
secrets: `chmod 600` it.

```yaml
services:
  likearr:
    image: ghcr.io/sysdad/likearr:0.5.1
    environment:
      LIKEARR_LIDARR_URL: "http://lidarr:8686"              # how this container reaches Lidarr
      LIKEARR_LIDARR_API_KEY: "<your lidarr api key>"       # Lidarr Settings -> General -> Security
      LIKEARR_SPOTIFY_CLIENT_ID: "<your spotify client id>" # see docs/spotify.md
      LIKEARR_UI_PASSWORD: "<16+ random characters>"        # e.g. `openssl rand -base64 24`
      # Optional: host names you browse to likearr by, comma-separated. Unset, it answers to
      # any IPv4 address (http://192.168.1.20:8770) but to no host name.
      # LIKEARR_ALLOWED_HOSTS: "likearr.example.org"
      # Optional: an email or URL MusicBrainz can reach you at. Unset, likearr's project URL.
      # LIKEARR_MUSICBRAINZ_CONTACT: "you@example.org"
    volumes:
      - ./likearr-data:/data
    ports:
      - "8770:8770"
    mem_limit: 768m
    stop_grace_period: 30m
    restart: unless-stopped
```

`http://lidarr:8686` works when both containers share a Docker network. Otherwise use Lidarr's
LAN address, such as `http://192.168.1.10:8686`. Then start it:

```bash
docker compose up -d
```

The container runs as uid 1000. If it shows as unhealthy in `docker ps`, it can't write
`./likearr-data`: see [Troubleshooting](docs/troubleshooting.md#the-container-is-unhealthy).

Open `http://<host>:8770` and log in with `LIKEARR_UI_PASSWORD`. Everything else is set up in the
browser:

1. **Settings -> Connect Spotify.** Approve access on Spotify's page, then paste back the address
   it sends you to. That page fails to load; that's expected.
2. **Settings -> Preview Lidarr setup**, then **Apply**. This creates the metadata profiles and tag
   likearr needs. Pick the root folder and quality profile likearr adds artists with.
3. **Review changes -> Check for changes**, read the plan, then **Apply** it.

Scheduled runs start after that first apply. The first check reads every song through MusicBrainz
at one request a second, so it can take several hours for a few thousand songs. Later checks take
minutes. If you stop it, it keeps what it has already looked up.

[`docs/install.md`](docs/install.md) covers the rest: taking over an existing library, a second
instance, scheduling, a reverse proxy and Home Assistant.

## Safety model

- **Nothing changes without a plan.** What you apply is exactly the plan you reviewed, and it is
  refused if Spotify, Lidarr or your settings changed since.
- **It only unmonitors what it monitored**, and never deletes a file.
- **A bad read never looks like un-liking.** A failed Spotify read unmonitors nothing, and a source
  or artist that suddenly shrinks is held back until you accept it.
- **Scheduled runs are capped.** A scheduled run that would unmonitor more than 100 releases
  unmonitors none of them and reports why. Review and apply those by hand.
- **It won't guess an artist.** Artists are matched by MusicBrainz's link to the Spotify page, and
  a different credit is accepted only when MusicBrainz records the two artists as related.
- **It writes to Spotify only when you run `promote-save --apply`.**
- **Every run records its health**, to stdout and the run history, and to MQTT or a webhook if you
  set one up.
- **Clean up moves files, never deletes them.**

In Lidarr, likearr adds artists (tagged `likearr`, nothing monitored, no search), monitors and
unmonitors releases, re-monitors an artist holding a wanted release, moves an artist from the Lean
to the Full metadata profile when a wanted release needs it, and sets "Monitor New Albums" to None
on artists holding a release it monitors. It never changes an existing artist's quality profile.
Lidarr setup and the by-hand commands (`adopt`, `prune-stage`) change more, and each shows you what
it will change before it does.

## Using likearr

| Page | What it's for |
|---|---|
| **Status** | Health in plain words, the last and next scheduled run with a "Run and apply now" button, and what the last applied run changed. |
| **Review changes** | Check for changes, read the plan (what gets added, monitored, unmonitored and why), then apply exactly that plan. |
| **Look up** | Why a song, album or artist is monitored, waiting, left out or unmatched, from the last run. |
| **Not added** | What couldn't be added, and why. |
| **Settings** | Spotify connection, Lidarr setup, Doctor, rules, guards, sources, playlists and the schedule. Clean up's switch is under Advanced. |

The web UI never applies Clean up's file moves or Spotify changes. It shows you the commands to run
in a terminal instead - see [`docs/cli.md`](docs/cli.md).

### Optional: Clean up

Clean up is off by default. It lists the albums on disk that nothing on Spotify asks for, lets you
decide per artist and per album what to keep and what to trash, and exports those decisions. You
then move the files with `prune-stage` and, if you like, follow artists or save albums on Spotify
with `promote-save`. Trash goes to a holding folder; nothing is deleted until you empty it.

It needs the library mounted in the `likearr-cli` container - see
[Clean up setup](docs/install.md#clean-up-setup). Turn it on in Settings > Advanced.

## Getting the music downloaded

likearr never searches or downloads. A release that comes out after likearr monitors it is usually
grabbed by Lidarr from your indexers' RSS feeds. An album that's already out needs a search: use
Lidarr's Wanted > Missing > Search All, a scheduled `MissingAlbumSearch` through Lidarr's API, or a
companion tool that works from Lidarr's wanted list. After a big first apply, spread the searches
out: hundreds at once can get you banned from an indexer.

## What it monitors

| You did this on Spotify | Lidarr monitors |
|---|---|
| Followed an artist | Their studio **albums and EPs**, present and future. No singles, remixes, live albums, compilations or DJ mixes. |
| Saved an album | That album, whatever type it is. |
| Liked a song | The studio album or EP the song is on. If that album isn't out yet, it waits, and after 180 days monitors the single. Set `liked_track_scope = "smallest"` for the smallest release holding the song instead. |
| Added a song to a playlist you own or collaborate on | Same as a liked song. |
| Added an artist in Lidarr yourself | Nothing new. Once a source wants one of its releases, likearr monitors that one. |

The rules and opt-outs (box sets, remix EPs, a deny list) are set in Settings and described in
[`deploy/config.example.toml`](deploy/config.example.toml).

## What can't be synced

Spotify's Development Mode only returns playlist items for playlists you own or collaborate on.
Followed playlists, other people's playlists, and Spotify's own (Discover Weekly, Release Radar,
Daily Mix, editorial playlists) come back empty, so likearr can't read them. The playlist picker in
Settings greys them out.

**Workaround:** like the songs you want, or copy them into a playlist you own.

If Settings says a playlist you collaborate on needs a re-authorization, use **Re-authorize
Spotify** in Settings, then refresh the playlist list.

## Changing things in Lidarr by hand

- **You don't want a release likearr monitored.** Unmonitoring it in Lidarr is undone on the next
  run. Use **Not this one** on the plan row or the Look up card, or unlike the song on Spotify. For
  a saved album, unsave it on Spotify.
- **You don't want Lidarr searching an artist.** Unfollow them, unlike their songs and unsave their
  albums on Spotify, or use Not this one on each release.
- **You want to keep a release after you unlike it.** Let likearr unmonitor it, then monitor it
  again in Lidarr. From then on it's yours and likearr leaves it alone.

## Scope

One Spotify account into one Lidarr, per instance. A household runs one instance per account.

**Not planned:** one instance reading several Spotify accounts; likes from Deezer, Tidal, YouTube
Music, Apple Music, Qobuz, Plex or Navidrome. **Possible later:** ListenBrainz loved tracks.

## Upgrading

1. Back up by copying the `likearr-data` folder somewhere safe.
2. Read [`CHANGELOG.md`](CHANGELOG.md) for the new version.
3. Change the image tag in `compose.yaml`, then run `docker compose pull && docker compose up -d`.

To roll back, see [Troubleshooting](docs/troubleshooting.md#rolling-back-an-upgrade).

## More

- [`docs/install.md`](docs/install.md) - setup in detail
- [`docs/troubleshooting.md`](docs/troubleshooting.md) - when something goes wrong
- [`docs/cli.md`](docs/cli.md) - every command
- [`docs/spotify.md`](docs/spotify.md) - the Spotify developer app
- [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), [`CHANGELOG.md`](CHANGELOG.md)

## Licence

MIT.
