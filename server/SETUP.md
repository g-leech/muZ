# Server setup: Music Assistant on the Pi

Checked against Music Assistant (MA) 2.10.9, the stable release on 30 October 2026. Menu names come from the web UI that ships with it.

## 1. Pi and network

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its settings, set a hostname (e.g. `albumbox`), your user, and an SSH key. Skip Wi-Fi if the Pi will be wired.
2. Plug the Pi into the router. In the router, give it a **DHCP reservation**; the Presto hard-codes its address.
3. Put the soundbar on **5 GHz**. The Pi, the soundbar and the Presto must be on one flat network: no guest Wi-Fi, client isolation or IoT VLAN between them. MA's docs call this the commonest cause of support requests.
4. On the Pi:

   ```sh
   sudo apt update && sudo apt full-upgrade -y
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER   # then log out and back in
   ```

## 2. Start MA

From the laptop, copy this folder over and start it:

```sh
rsync -a server/ albumbox.local:album-box/
ssh albumbox.local
cd album-box && docker compose up -d
docker logs -f music-assistant-server    # Ctrl-C to stop following
```

The log should show MA using the Pi's LAN IP. Open `http://<pi-ip>:8095` and create the admin account. **There is no password recovery**; keep the credentials in a password manager. (Last resort: delete `auth.db` in the data folder.)

The data folder is `~/docker/music-assistant-server/data`. Back it up once the setup works.

## 3. Output: Google Cast

MA adds the Google Cast, AirPlay, DLNA, Sendspin and Sonos player providers on first start. The soundbar should appear under Players within a minute. If it doesn't, make sure it shows up as a Cast target on a phone first (Chromecast built-in may need setting up in the Google Home app).

- **Disable the AirPlay and DLNA player providers.** MA merges a device it finds over several protocols into one player and could pick AirPlay, which plays silently on Samsung bars. Sonos can go too.
- Leave the soundbar's player settings (HTTP profile, output codec) at their defaults to start with. If playback stops mid-track, change one thing at a time: output codec **MP3**, then HTTP profile **Profile 2 - no content length**, then **Profile 1 - chunked**.

## 4. Music sources

Settings → Music sources → Add a music source. Playing a provider URI (`spotify://album/…`) works without adding the album to MA's library, so there's no need to sync anything.

How the albums in `lookup.csv` split by first available source:

| Source | Albums |
|---|---|
| Spotify | 2,130 |
| Bandcamp only | 84 |
| YouTube Music only | 133 |
| None | 113 (local files later, if at all) |

**Spotify** (needs Premium)

1. Add Spotify and sign in. Keep the default engine, **librespot**.
2. Approve playback once: MA asks you to do this from the Spotify app on a phone on the same network.
3. Test (step 6). If nothing plays and the log shows `Key Error`, the account is too new for librespot. Switch the engine to **Spotify Soloist**: accept Spotify's terms, create a Soloist API key on Spotify's page (Premium only), paste it into MA, then in the Spotify app open the device picker and choose "Music Assistant Pairing". Afterwards, don't control playback from the Spotify app ("Music Assistant Playback" device); it interferes.

**Bandcamp** (no account needed)

Add Bandcamp; searching and playing need no login. Streams are 128 kbps MP3. The optional `identity` cookie only adds your purchases as a library.

**YouTube Music** (optional; needs YouTube Music Premium)

1. On the Pi: `docker compose --profile ytmusic up -d` (starts the PO token server MA needs).
2. Get the login cookie: open music.youtube.com in a private window and sign in. Open developer tools → Network, filter for `/browse`, then open a page that needs a login (e.g. a library playlist). Click a `/browse` request and copy the **Cookie** request header. Close the window *without* logging out.
3. Add YouTube Music: Username = your Gmail address, Login Cookie = the cookie. Leave the PO token server URL at its default.

Cookies expire; `401: Unauthorized` in the log means repeat step 2.

## 5. A token for the Presto

The token will sit in plain text on the Presto, so give it a user that can only reach the soundbar.

1. Settings → Users → add a user `presto` with role **User**. Set its **Player filter** to the soundbar.
2. Sign in as `presto` in a private window. Settings → Profile → Long-lived access tokens → Create new token. Copy it; it is shown once.
3. **The token expires after one year** and doesn't renew. (The web UI says 10 years; the server code says 365 days.) Put a reminder in the calendar. When it lapses, the Presto will get HTTP 401 and show red.

Music sources added by the admin are shared with every user, so `presto` can play them.

## 6. Find the soundbar's player_id and test

On the Pi or the laptop:

```sh
cp ma.env.example ma.env      # fill in MA_URL and the presto token
./ma.sh players               # copy the soundbar's player_id into QUEUE_ID in ma.env
./ma.sh state
./ma.sh play spotify://album/6EcqWi6PA9CbvtulGbEsPN          # Songs of Leonard Cohen
./ma.sh play bandcamp://album/724446940-2229563677           # Welcome Interstate Managers
./ma.sh play ytmusic://album/MPREb_O1nDD7bXGHa               # Have One On Me (if YT Music is set up)
./ma.sh stop
```

The `queue_id` equals the soundbar's `player_id` unless the bar is grouped. Success returns `null`; errors print MA's message and exit non-zero. Generated API docs: `http://<pi-ip>:8095/api-docs`.

The Presto will send exactly this:

```sh
curl -sS http://<pi-ip>:8095/api \
  -H "Authorization: Bearer $MA_TOKEN" -H 'Content-Type: application/json' \
  -d '{"message_id": "1", "command": "player_queues/play_media",
       "args": {"queue_id": "<player_id>", "media": "spotify://album/<id>"}}'
```

## 7. Wake test

```sh
./wake_test.sh spotify://album/6EcqWi6PA9CbvtulGbEsPN "remote off, 2 min"
```

Run it once per state: off with the remote for a few minutes; idle for 25+ minutes (past the 18-minute auto power off); overnight; on but switched to the TV input. Each run appends a line to `wake_results.tsv`.

Reading the results:

- **Wakes and plays every time:** nothing more to do.
- **Fails only after auto power off:** look in the SmartThings app's soundbar settings for an Auto Power Off switch (Samsung added one to the Q990D in an update), turn it off and retest.
- **MA shows the player unavailable before the command, or the command fails:** the bar drops off the network in standby. It needs a wake step from the Pi (HDMI-CEC or an IR blaster; see `CLAUDE.md`).
- **MA reports playing but no sound:** the bar didn't switch input. Same fix.

## Maintenance

```sh
docker compose pull && docker compose up -d     # update MA
docker logs --since 1h music-assistant-server   # recent log
```

Update when a provider breaks; Spotify and YouTube changes are the usual cause.
