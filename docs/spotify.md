# Setting up your Spotify developer app

likearr reads Spotify through its Web API, which needs a developer app registered by one of its
users. There is no shared likearr app. One app covers a household: up to five Spotify accounts can
share it (see [Adding another person](#adding-another-person)).

## Create the app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in
   with the Spotify account that will own the app. It needs Premium. It doesn't have to be the
   account likearr reads from. Click **Create app**.
2. Fill in any **App name** and **App description**.
3. **Redirect URIs**: add the exact value of `[spotify] redirect_uri` from `config.toml`. The
   default is:

   ```
   http://127.0.0.1:8765/callback
   ```

   Use `127.0.0.1`, never `localhost`: Spotify rejects `localhost`, and so does likearr. If
   `[ui] public_url` is set to an `https://` address, also add `<public_url>/spotify/callback`.
4. **APIs used**: check **Web API** only. Save.
5. Copy the app's **Client ID** into `LIKEARR_SPOTIFY_CLIENT_ID`. likearr signs in with PKCE, so
   leave the **Client Secret** alone.
6. Under **User Management**, add the Spotify account this likearr instance reads. Do this even if
   it's the owner's account: a Development Mode app answers every account not on the list with
   `403`.

## Adding another person

Add the second account to the same app's **User Management** list (step 6) instead of creating a
new app. Each person runs their own likearr instance - see
[More than one instance](install.md#more-than-one-instance-or-spotify-account).

The loopback redirect URI works for every instance. An instance with `[ui] public_url` set needs its
own `<public_url>/spotify/callback` added to the app; an app can hold several redirect URIs.

**Check the account before approving.** Connect Spotify authorizes whichever account the browser is
signed in to. Spotify's page names it; use its "Not you?" link to switch before you approve.

Spotify documents the Premium requirement for the app owner only.

## Connecting and re-authorizing

Settings -> Connect Spotify (Re-authorize Spotify, once connected) always opens Spotify's own
page. After you approve, Settings shows who likearr is connected as, what access it has and until
when.

You need to re-authorize only when Settings gives a reason: the six months are nearly up, a scope
is missing, or Spotify refused the saved authorization on the last run. Otherwise re-authorizing
only switches accounts.

If a different account approves than the one likearr is connected as, likearr asks before it
switches, and saves nothing until you confirm. After a switch, the next run plans against the new
account's library, so review it before applying.

### Access likearr asks for

Sign-in is read-only by default:
`user-follow-read user-library-read playlist-read-private playlist-read-collaborative`.

`promote-save`, which follows artists and saves albums on Spotify, also needs
`user-follow-modify user-library-modify`. To grant them, tick "Also let promote-save follow artists
and save albums" in Settings (shown once Clean up is on) before you connect, or run:

```
likearr auth --manual --promote-save -c /data/config.toml
```

A plain re-authorization keeps the write access your current token has.

## If connecting fails

- **Spotify says the redirect URI is invalid** (`INVALID_CLIENT: Invalid redirect URI`): add the
  exact redirect URI to the app's **Redirect URIs** (step 3). That is `spotify.redirect_uri`
  (default `http://127.0.0.1:8765/callback`) for the copy-and-paste flow, or
  `<public_url>/spotify/callback` when `[ui] public_url` is set. Settings shows the exact value
  while a connect is under way.
- **"Spotify won't let likearr use that account"**: Spotify answered `403` for it. A Development
  Mode app serves only the accounts on its **User Management** list (step 6). Add the account
  there and connect again. The existing connection is unchanged.
- **"likearr could not check which Spotify account that is"**: the check after you approved
  failed (a network error, or the quota). Nothing was saved; connect again later.
- **"Spotify authorization failed"**: the code could not be exchanged for a token. A redirect URI
  that differs from the registered one fails here too.
- **"That Spotify authorization attempt has expired"**: an attempt can be finished once, within
  ten minutes. Click Connect Spotify again.
- **The paste-back page fails to load**: that is expected in the copy-and-paste flow. Copy the
  whole address from the address bar into Settings anyway.

## Development Mode limits

Every app starts in Spotify's Development Mode
([quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)):

- **Five accounts per app**, on the **User Management** list. Every other account gets `403`.
- **The app owner needs Premium.**
- **One request quota per developer account**, shared by all your apps. A day of back-to-back
  checks, or another script using the same account, can spend it. Then runs fail until it
  recovers; scheduled runs skip themselves.
- **Only playlists you own or collaborate on** return their songs. See
  [What can't be synced](../README.md#what-cant-be-synced).
