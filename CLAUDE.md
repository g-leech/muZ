# Album box

A desk gadget: a square screen shows a random album cover from a fixed list; pressing a button plays that album on the living-room soundbar. The context below comes from a planning chat on claude.ai (October 2026). Verify anything version-specific before relying on it.

## Hardware

| Part | Notes |
|---|---|
| Pimoroni Presto | RP2350, 4" 480×480 touchscreen, 2.4 GHz Wi-Fi (RM2 module), MicroPython preloaded, microSD, Qw/ST (I²C) port, 7 RGB LEDs, boot button usable as a user button |
| SparkFun Qwiic Button | Physical play button on the Presto's Qw/ST port (I²C) |
| Raspberry Pi 4, ≥2 GB | Server. 64-bit Raspberry Pi OS Lite. Wire it to the router if at all possible |
| Samsung HW-Q990F | Output. Chromecast built in, AirPlay 2, Spotify Connect, dual-band Wi-Fi, no Ethernet port |

## Architecture

Presto → HTTP on the LAN → Music Assistant (MA) on the Pi → Google Cast → Q990F

- MA server runs in Docker on the Pi with host networking (needed for player discovery). From MA's install docs:

  ```yaml
  services:
    music-assistant-server:
      image: ghcr.io/music-assistant/server:latest
      container_name: music-assistant-server
      restart: unless-stopped
      network_mode: host
      volumes:
        - ${USERDIR:-$HOME}/docker/music-assistant-server/data:/data/
  ```

- MA needs a 64-bit OS, a Pi 4 or newer, and at least 2 GB RAM (4 GB+ if the Pi runs other things).
- Spotify: MA's Spotify provider with the librespot engine (the default). Needs Premium. librespot generally fails for accounts created since December 2024 (symptom: `Key Error` in the log); in that case switch to the Spotify Soloist engine, which is lossless but plays on one player at a time per account, starts and seeks more slowly, has unclear terms when used via MA, and does not work with Docker bridge networking.
- Output: MA's Google Cast provider. Do not use AirPlay for this soundbar: MA's docs say Samsung's AirPlay 1 implementation plays silently, and MA's AirPlay 2 support is new and slower to start.
- Bandcamp (later, optional): MA's Bandcamp provider streams at most 128 kbps MP3. Prefer downloaded FLACs via MA's local files provider.

## The play command (the only call the Presto makes)

```
POST http://<pi-ip>:8095/api
Authorization: Bearer <MA long-lived token>
Content-Type: application/json

{"message_id": "1", "command": "player_queues/play_media",
 "args": {"queue_id": "<soundbar player_id>", "media": "spotify://album/<album_id>"}}
```

- Token: MA web UI → Settings → Profile → long-lived access tokens.
- `queue_id` equals the soundbar's `player_id` unless it is grouped. Find it with `player_queues/all`.
- Generated API docs live at `http://<pi-ip>:8095/api-docs`.
- `music/item_by_uri` with `{"uri": "spotify://album/<id>"}` returns full item details (check for artwork URLs there).

## Device behaviour (Presto, MicroPython)

- On boot: connect to Wi-Fi (reconnect if it drops), find the Pi, download `albums.json` from it, show a random cover.
- Shuffle-bag: no repeats until every album has been shown, then reshuffle.
- Screen tap: play the album on screen and keep it there: the 30-minute timer stops until the next left/right swipe. Only a successful play command does this. (Qwiic Button dropped for now.)
- Swipe left/right: next/previous cover; swipe up/down: next/previous track (`player_queues/next`, `player_queues/previous`). Timer: next cover every 30 minutes, unless a tap has kept the cover on screen; a left/right swipe restarts it.
- Bottom bar: ▶ (drawn as a triangle) and "Title - Artist" while the soundbar plays, polled every 5 s via `players/get`. ASCII only; common accents are folded.
- Prefetch: next 15 covers downloaded, next 3 decoded, last 2 kept decoded; a swipe is a ~100 ms copy (decode alone is ~300 ms).
- LEDs: brief green on HTTP 200, red on failure or timeout.
- Finding the Pi: listen on UDP 50505 for `{"service": "albumbox", "url": "http://<ip>:8095", "covers": "http://<ip>:8099"}` (broadcast every 5 s by `server/beacon.py`); at boot, broadcast `albumbox?` to 255.255.255.255:50505 for an immediate reply. Cache the last URL; switch when a different one arrives.
- HTTP timeout for the play command: about 60 s (MA can take 30 s+ to fail when no track is playable, then returns HTTP 500).
- `secrets.py` holds Wi-Fi SSID/password, MA token, queue_id and optional fallback MA and cover URLs. Keep it out of git.

### Presto firmware: what we learned (31 October 2026)

- **Stay on v0.1.0** (March 2025). v2.0.0 (August 2026) was tried and dropped on this unit: Wi-Fi reconnects hung at "connecting", the device froze at startup, and `r.content`/`r.json()` became very slow (65 s for the 278 KB `albums.json`). Firmware files are in `~/presto-fw/` on the Pi.
- **Never call `display.set_framebuffer()`** on the main display. It led to memory corruption: snow on screen, lost globals, hard freezes. To draw off-screen, give each spare buffer its own `PicoGraphics(DISPLAY_PRESTO_FULL_RES, buffer=..., pen_type=PEN_RGB565)` and copy into `memoryview(display)`.
- **Read HTTP bodies in 4 KB chunks** (`r.raw.read(4096)`), never `r.content`/`r.json()`.
- `Presto(full_res=True)` for 480×480; JPEG decode is ~300 ms, `presto.update()` ~24 ms.
- Install and debug with `mpremote` (`~/.venvs/mpremote/bin/mpremote`). If the Presto hangs in C code, USB stops responding: power-cycle it. To stop `main.py` running at boot, hold BOOT while powering on and reflash the with-filesystem UF2, or rename `main.py` while you still have a REPL.

## Covers

- `albums.json`: `[{"uri": "spotify://album/<id>", "artist": "...", "title": "...", "cover": "covers/<id>.jpg"}]`
- Built by `tools/prep_covers.py` from `index.csv`, `lookup.csv` and `album_art/` (already 480×480): picks each album's first URI whose provider is set up in MA (`--providers`, default `spotify`), writes `device/albums.json` and `device/covers/<index_ID>.jpg` as **baseline (non-progressive) JPEG**, since microcontroller JPEG decoders handle progressive files badly or not at all.
- **Served by the Pi, not stored on the Presto** (128 MB for 2,130 albums wouldn't fit in flash). The `covers` service in `server/docker-compose.yml` serves `device/` on port 8099. The Presto downloads `albums.json` at boot and each cover (~60 KB) into RAM when it shows it, then decodes from memory. Re-run `prep_covers.py` on the Pi to change the set; no need to touch the Presto.

## Known risks: test these before writing firmware

1. **Soundbar wake.** The Q990F powers down after 18 minutes without an audio signal. A Q990D owner on Samsung's forum reported having to switch it on by hand before every Chromecast session. Test: put the Q990F in standby, send the play command with curl, see whether it wakes. If it doesn't, add a wake step on the Pi before casting: HDMI-CEC from the Pi into one of the Q990F's HDMI inputs (untested on this model), or a BroadLink RM4 mini IR blaster driven by python-broadlink (assumes the remote is infrared).
2. **Wi-Fi dropouts.** MA streams with only about 2 s of buffer, so Wi-Fi faults become audible. Wire the Pi, put the soundbar on 5 GHz, and keep everything on one flat network (no guest Wi-Fi, client isolation or IoT VLAN between them). The Starlink router can't reserve addresses, so the Pi's IP may change; the Presto finds it via the beacon (see Device behaviour).
3. **Cast playback stops mid-track.** MA's Cast page: keep the HTTP profile at "Profile 2 - no content length", set the output codec to MP3; if it still fails, try "Profile 1 - chunked".
4. **Fallback for Spotify** if Cast over Wi-Fi stays flaky: start playback on the soundbar's own Spotify Connect via the Web API (`PUT /v1/me/player/play` with `context_uri` and `device_id`). Since 2026, development-mode apps need a Premium owner and allow at most 5 users, refresh tokens expire 6 months after authorisation, and the soundbar must appear in `GET /me/player/devices` while idle.

## Tasks, in order

1. `server/docker-compose.yml` and `server/SETUP.md`: MA install, Spotify provider, Google Cast provider, creating the token.
2. `server/wake_test.sh`: curl the play command with the soundbar in standby; record whether it wakes.
3. `tools/prep_covers.py` (Python 3 + Pillow, run on the Pi) → `device/covers/*.jpg` and `device/albums.json`, served on port 8099.
4. `device/main.py` (Presto MicroPython): display, shuffle-bag, touch, POST to MA, LEDs, Wi-Fi reconnect, now-playing bar, prefetch. Done; see the firmware notes under Device behaviour before changing it.

## Parts list (The Pi Hut prices, October 2026)

- Pimoroni Presto £69; SparkFun Qwiic Button £5.30; 200 mm Qw/ST cable £1
- Raspberry Pi 4 Starter Kit, 2 GB, £100.70 (only if no Pi already); Pi 4 case fan £4.80
- Optional: BroadLink RM4 mini (about £20–25), only if the wake test fails

## References

- MA API: https://www.music-assistant.io/api/
- MA installation: https://www.music-assistant.io/installation/
- MA Google Cast: https://www.music-assistant.io/player-support/google-cast/
- MA Spotify: https://www.music-assistant.io/music-providers/spotify/
- MA networking: https://www.music-assistant.io/faq/networking/
- Presto firmware and examples: https://github.com/pimoroni/presto
- Qwiic Button: https://github.com/sparkfun/Qwiic_Button
- Spotify Web API changes (Feb 2026): https://developer.spotify.com/documentation/web-api/references/changes/february-2026

## Owner

Gavin, ML postdoc. Laptop runs TuxedoOS (Ubuntu-based). Write in plain, concise British English.
