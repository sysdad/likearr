# Setting up your Spotify developer app

likearr reads Spotify through its Web API, and Spotify requires every app that calls it to be
registered by one of its users - there is no shared likearr app. This is the thing you do before
`LIKEARR_SPOTIFY_CLIENT_ID` means anything. One app covers a whole household: up to five Spotify
accounts can share it (see [Adding another person](#adding-another-person)), so this is normally a
one-time setup, not one per instance.

## Create the app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in
   with the Spotify account that will own this app - it needs Premium (step 6), and creating the
   app is the one thing only the owner can do. It doesn't have to be the account likearr reads
   from: using a household member's account instead of the owner's own is
   [Adding another person](#adding-another-person), below. Click **Create app** (Spotify's own
   dashboard copy has read slightly differently across versions; either wording starts the same
   form).
2. Fill in any **App name** and **App description**. Neither is shown to anyone but you.
3. **Redirect URIs**: add the exact value of `spotify.redirect_uri` from your `config.toml`. The
   default, if you haven't changed it, is:

   ```
   http://127.0.0.1:8765/callback
   ```

   Use the loopback IP `127.0.0.1`, never `localhost` - Spotify rejects `localhost` outright, and
   likearr's own redirect URI check does the same before it ever sends a request. Registering
   anything other than your actual `spotify.redirect_uri` is why "Connect Spotify" fails on the
   first try with Spotify's `INVALID_CLIENT: Invalid redirect URI`.

   If `[ui] public_url` is set to an `https://` address (see "First run: authenticate with
   Spotify" in `docs/DEPLOY.md`), also add `<public_url>/spotify/callback` - that's the
   direct-callback redirect URI, on top of the loopback one.
4. **APIs used**: check **Web API** only. Save.
5. Open the app and copy its **Client ID** into `LIKEARR_SPOTIFY_CLIENT_ID` in your `.env` file.
   likearr authorizes with PKCE (Authorization Code with PKCE) and never sends a client secret, so
   leave **Client Secret** alone - `deploy/env.example` comments that line out for exactly this
   reason. When you approve access, Spotify's consent screen lists what likearr is asking for.
   It is read-only by default, for every read command:
   `user-follow-read user-library-read playlist-read-private playlist-read-collaborative`.
   `user-follow-modify user-library-modify` are added only when you opt in for `promote-save`
   (`likearr auth --promote-save`, or the promote-save box in Settings). See the README's
   "Scopes, and when you have to re-authorize" section for what each is for and when you'll need
   to grant them again.
6. The account that owns the app needs Spotify Premium. See "Spotify Development Mode" in the
   [README](../README.md#spotify-development-mode) for what a non-Premium or free account blocks.
7. Under the app's **User Management**, add whichever Spotify account *this instance* will read
   for to the allowlist - that's the owner's own account for most first instances, but not
   necessarily: the account from step 1 (who owns the app, and needs Premium) and the account here
   (whose follows, saved albums and Liked Songs get mirrored) can already differ on a first
   instance, and always differ once you're [adding another person](#adding-another-person). A
   Development Mode app answers every account not on that list with `403`, including its owner's -
   this step is easy to skip because it looks optional.
8. Spotify refresh tokens die six months after you last authorized, and refreshing does not
   extend that. See "Back-filling the Spotify re-auth date" in `docs/DEPLOY.md` if you're carrying
   an older token, and use Settings -> "Re-authorize Spotify" once Status turns amber.

## Adding another person

A second Spotify account in the household doesn't need a second app: add it to the same app's
**User Management** allowlist (step 7, above) instead of creating a new one - up to five accounts
can share it. Every likearr instance still has its own `/data` directory (state database, token
file and `config.toml`), so accounts sharing an app can each run their own instance without
touching each other's config - see `docs/DEPLOY.md`,
["Running more than one instance"](DEPLOY.md#running-more-than-one-instance). Spotify documents
the Premium requirement for the app **owner** only; whether an allowlisted account that isn't the
owner also needs Premium isn't stated either way, so this doesn't promise one.

The redirect URI is shared, not per instance: the loopback URI from step 3 works for every
instance, and only needs registering once in the shared app. An instance using the
direct-callback URI instead (`[ui] public_url` set) needs its own `<public_url>/spotify/callback`
added to that same app - one app can hold more than one redirect URI.

**The trap:** Settings -> Connect Spotify authorizes whichever Spotify account the browser
happens to be signed into, not necessarily the account you meant to connect. Sign out of Spotify
in that browser first (or use a private window) so the sign-in prompt actually asks, then confirm
which account got connected. Settings has no readback for this; the CLI does -
`docker compose run --rm likearr-cli auth --manual -c /data/config.toml` prints the connected
account's name once the token is written.

Every instance sharing an app also shares its request quota - see "Development Mode limits" below
for what that means day to day.

## Development Mode limits, and what they mean for likearr

Every app you create starts in Spotify's Development Mode. As of Spotify's own
[Web API quota updates for Development Mode](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates)
post (2026-07-23) and its
[quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
reference:

- **A 5-user allowlist, per app.** Up to 5 Spotify accounts can be added under User Management,
  and every account not on that list gets `403`. For likearr this means step 7 above isn't
  optional - your own account has to be on the list, not just named as the app's owner. See
  [Adding another person](#adding-another-person) for sharing this allowlist across a household.
- **The app owner needs Spotify Premium.** See the README's "Spotify Development Mode" section for
  what that gates.
- **Up to 25 Development Mode client IDs per developer account, all sharing one quota.** Spotify
  raised this from 1 client ID per developer (reported when the current Development Mode rules
  took effect in February 2026) to 25 in the July 2026 update above. If you already run another
  Development Mode app - a different tool, a personal script - you can now create a separate one
  for likearr instead of reusing it. They still draw from the same account-level quota budget
  though, so running both hard at the same time still risks `QUOTA_EXCEEDED`; extra client IDs
  don't buy extra quota.
- **Playlist contents come back empty for playlists you don't own.** Followed and editorial
  playlists return nothing to a Development Mode app; likearr refuses them loudly rather than
  treating an empty answer as "no tracks here". See the README's "What it monitors" table.

## Spotify quirks outside likearr's control

A few of Spotify's documented endpoints don't behave as documented for a Development Mode app.
likearr already works around all of these; nothing here needs action from you:

- Per-type library writes (`PUT /me/following`, `PUT /me/albums`) answer `403`; likearr writes
  through `PUT /me/library` instead (`likearr/ports.py`, the `follow_artists` and `save_albums`
  docstrings).
- The `/contains` endpoints (`GET /me/following/contains`, `GET /me/albums/contains`) answer
  `403`; likearr reads the full follows and saved-albums lists instead, which is safe to re-run
  and gets cached (`docs/CLI.md`, `promote-save`).
- `external_ids` (ISRC/UPC) were removed from Spotify's API and then restored in 2026; likearr
  treats them as helpful, not required (see the README's "Spotify Development Mode" section).

See `docs/dev/DESIGN.md` ("Upstream quirks") for the measurements behind these.
