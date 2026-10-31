# Album box: show a random album cover; tap it to play that album on the soundbar.
#
# Covers and the album list come from the Pi over Wi-Fi (server/docker-compose.yml,
# "covers" service); the Pi's address comes from its UDP beacon (server/beacon.py).
#
#   tap               play the album on screen
#   swipe left        next cover            (spinner while it loads)
#   swipe right       previous cover
#   swipe up          next track on the soundbar
#   swipe down        previous track
#   every 30 minutes  next cover
#
# Bottom bar: the track the soundbar is playing, polled from Music Assistant.
# LEDs: amber while a command is in flight, then green (ok) or red (failed).
# Written for Pimoroni's Presto firmware v0.1.0 (MicroPython 1.25 preview).

import gc
import json
import math
import os
import random
import socket
import time

import jpegdec
import network
import requests
from presto import Presto

import secrets

BEACON_PORT = 50505
NEXT_AFTER_S = 30 * 60
PLAY_TIMEOUT_S = 60       # MA can take 30 s+ to give up on an unplayable album
SWIPE_PX = 80             # movement that counts as a swipe, not a tap
HISTORY = 20              # how many covers "swipe right" can go back
POLL_S = 5                # how often to ask MA what's playing
STATE_FILE = "pi.json"    # last known Pi URLs, so a reboot works before the beacon is heard

presto = Presto(full_res=True)
display = presto.display
WIDTH, HEIGHT = display.get_bounds()
BLACK = display.create_pen(0, 0, 0)
WHITE = display.create_pen(255, 255, 255)
GREY = display.create_pen(140, 140, 140)
DIM = display.create_pen(70, 70, 70)
jpeg = jpegdec.JPEG(display)
wlan = network.WLAN(network.STA_IF)
display.set_font("bitmap8")

BAR_H = 40


# ---------- screen and LEDs ----------

def message(title, detail=""):
    display.set_pen(BLACK)
    display.clear()
    display.set_pen(WHITE)
    display.text(title, 24, 180, WIDTH - 48, 4)
    if detail:
        display.set_pen(GREY)
        display.text(detail, 24, 260, WIDTH - 48, 2)
    presto.update()


def leds(r, g, b):
    for i in range(presto.NUM_LEDS):
        presto.set_led_rgb(i, r, g, b)


FOLD = {"à": "a", "á": "a", "â": "a", "ä": "a", "ã": "a", "å": "a", "ç": "c", "è": "e", "é": "e",
        "ê": "e", "ë": "e", "ì": "i", "í": "i", "î": "i", "ï": "i", "ñ": "n", "ò": "o", "ó": "o",
        "ô": "o", "ö": "o", "õ": "o", "ø": "o", "ù": "u", "ú": "u", "û": "u", "ü": "u", "ý": "y",
        "ÿ": "y", "ß": "ss", "æ": "ae", "œ": "oe", "’": "'", "‘": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "…": "...",
        "À": "A", "Á": "A", "Â": "A", "Ä": "A", "Å": "A", "Ç": "C", "È": "E", "É": "E", "Ê": "E",
        "Í": "I", "Ñ": "N", "Ó": "O", "Ô": "O", "Ö": "O", "Ø": "O", "Ú": "U", "Ü": "U", "Æ": "AE"}


def ascii_only(s):
    """The bitmap font only has ASCII; fold common accents, drop the rest."""
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in FOLD:
            out.append(FOLD[ch])
    return "".join(out)


def fit(text, width, scale):
    if display.measure_text(text, scale) <= width:
        return text
    while text and display.measure_text(text + "...", scale) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def draw_bar(now_playing):
    if not now_playing:
        return
    display.set_pen(BLACK)
    display.rectangle(0, HEIGHT - BAR_H, WIDTH, BAR_H)
    display.set_pen(WHITE)
    # Play symbol: the bitmap font has no U+25B6, so draw it as a triangle.
    top = HEIGHT - BAR_H + 12
    display.triangle(12, top, 12, top + 15, 25, top + 7)
    display.text(fit(now_playing, WIDTH - 46, 2), 34, top, WIDTH, 2)


SPIN_R = 34      # radius of the ring of dots
SPIN_BOX = 120   # square around the centre that the spinner redraws


def spinner(step):
    """One frame of a ring of 8 dots with a bright head; only the centre is pushed to the panel."""
    cx, cy = WIDTH // 2, HEIGHT // 2
    display.set_pen(BLACK)
    display.circle(cx, cy, SPIN_R + 16)
    for i in range(8):
        a = 2 * math.pi * i / 8
        x = cx + int(SPIN_R * math.sin(a))
        y = cy - int(SPIN_R * math.cos(a))
        age = (step - i) % 8
        display.set_pen(WHITE if age == 0 else GREY if age < 3 else DIM)
        display.circle(x, y, 7 if age == 0 else 5)
    h = SPIN_BOX // 2
    presto.partial_update(cx - h, cy - h, SPIN_BOX, SPIN_BOX)


# ---------- finding the Pi ----------

def load_urls():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"url": getattr(secrets, "MA_URL", None),
                "covers": getattr(secrets, "COVERS_URL", None)}


def save_urls(urls):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"url": urls.get("url"), "covers": urls.get("covers")}, f)
    except OSError:
        pass


def open_beacon():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("0.0.0.0", BEACON_PORT))
    s.setblocking(False)
    return s


def ask_beacon(s):
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, 0x20, 1)  # SO_BROADCAST
        except OSError:
            pass
        s.sendto(b"albumbox?", ("255.255.255.255", BEACON_PORT))
    except OSError:
        pass


def read_beacon(s, urls):
    """Drain pending beacon datagrams; returns True if the URLs changed."""
    changed = False
    while True:
        try:
            data, _ = s.recvfrom(256)
        except OSError:
            return changed
        if not data.startswith(b"{"):
            continue  # our own query, echoed back
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if msg.get("service") != "albumbox":
            continue
        urls["heard"] = True
        for key in ("url", "covers"):
            if msg.get(key) and msg[key] != urls.get(key):
                urls[key] = msg[key]
                changed = True


def wait_for_beacon(s, urls, seconds):
    """Ask, then wait up to `seconds` for an answer. True if we have URLs (fresh or saved)."""
    ask_beacon(s)
    end = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        read_beacon(s, urls)
        if urls.get("heard"):
            break
        time.sleep_ms(100)
    return bool(urls.get("url") and urls.get("covers"))


# ---------- network ----------

def ensure_wifi():
    if wlan.isconnected():
        return True
    message("Connecting to Wi-Fi...", secrets.WIFI_SSID)
    try:
        presto.connect()
    except Exception as e:  # ezwifi raises on bad secrets or timeout
        print("wifi:", e)
    return wlan.isconnected()


def http_get(url, timeout=15, on_chunk=None):
    """GET url into bytes. on_chunk(n) is called between 4 KB reads (for the spinner)."""
    r = requests.get(url, timeout=timeout)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, url))
        if on_chunk is None:
            return r.content
        parts, n = [], 0
        while True:
            chunk = r.raw.read(4096)
            if not chunk:
                break
            parts.append(chunk)
            n += 1
            on_chunk(n)
        return b"".join(parts)
    finally:
        r.close()


def ma(urls, command, args, timeout=10):
    """Send one MA API command. Returns (ok, result)."""
    body = {"message_id": "1", "command": command, "args": args}
    headers = {"Authorization": "Bearer " + secrets.MA_TOKEN,
               "Content-Type": "application/json"}
    r = requests.post(urls["url"] + "/api", data=json.dumps(body), headers=headers,
                      timeout=timeout)
    try:
        if r.status_code != 200:
            print(command, "failed:", r.status_code, r.text[:200])
            return False, None
        return True, r.json()
    finally:
        r.close()


def now_playing(urls):
    """'Title - Artist' if the soundbar is playing, '' if not, None if MA couldn't be asked."""
    try:
        ok, p = ma(urls, "players/get", {"player_id": secrets.QUEUE_ID}, timeout=3)
    except Exception as e:
        print("poll:", e)
        return None
    if not ok or not p or p.get("playback_state") != "playing":
        return "" if ok else None
    media = p.get("current_media") or {}
    title, artist = media.get("title") or "", media.get("artist") or ""
    text = title + (" - " + artist if artist else "")
    return ascii_only(text)


def command_with_leds(urls, command, args, timeout):
    leds(255, 120, 0)
    ok = False
    try:
        ok = ensure_wifi() and ma(urls, command, args, timeout)[0]
    except Exception as e:
        print(command, e)
    leds(0, 255, 0) if ok else leds(255, 0, 0)
    time.sleep(1 if ok else 3)
    leds(0, 0, 0)
    return ok


# ---------- covers ----------

class Bag:
    """Shuffle-bag: every album once, in random order, then reshuffle."""

    def __init__(self, n):
        self.n = n
        self.order = []

    def next(self):
        if not self.order:
            self.order = list(range(self.n))
            for i in range(self.n - 1, 0, -1):  # Fisher-Yates
                j = random.randint(0, i)
                self.order[i], self.order[j] = self.order[j], self.order[i]
        return self.order.pop()


def render(cover, playing):
    jpeg.open_RAM(cover)
    display.set_pen(BLACK)
    display.clear()
    jpeg.decode(0, 0, jpegdec.JPEG_SCALE_FULL, dither=False)
    draw_bar(playing)
    presto.update()


# ---------- main loop ----------

def main():
    random.seed(int.from_bytes(os.urandom(4), "little"))
    leds(0, 0, 0)
    urls = load_urls()

    while not ensure_wifi():
        message("No Wi-Fi", "Retrying in 10 s. Check WIFI_SSID and WIFI_PASSWORD in secrets.py.")
        time.sleep(10)

    beacon = open_beacon()
    message("Looking for the Pi...")
    if not wait_for_beacon(beacon, urls, 6):
        message("Can't find the Pi", "Is the album box server running? Retrying.")
    save_urls(urls)

    albums = None
    while albums is None:
        try:
            albums = json.loads(http_get(urls["covers"] + "/albums.json"))
        except Exception as e:
            print("albums.json:", e)
            message("Can't load the album list", str(e)[:120])
            time.sleep(10)
            ensure_wifi()
            ask_beacon(beacon)
            time.sleep(2)
            if read_beacon(beacon, urls):
                save_urls(urls)
    gc.collect()
    print(len(albums), "albums; free RAM", gc.mem_free())

    bag = Bag(len(albums))
    history = []          # indices shown before the current one
    current = bag.next()
    cover = None          # JPEG bytes of the cover on screen, kept for redraws
    need_cover = True
    spin = False          # show the spinner while the next cover downloads
    playing = ""          # bottom bar text
    next_poll = time.ticks_ms()
    last_change = time.time()
    touch_start = None    # (x, y) where the current touch began
    swiped = False        # the current touch has already acted as a swipe

    def next_cover():
        nonlocal current, need_cover, spin
        history.append(current)
        del history[:-HISTORY]
        current = bag.next()
        need_cover = spin = True

    while True:
        if need_cover:
            try:
                tick = spinner if spin else None
                if spin:
                    spinner(0)
                data = http_get(urls["covers"] + "/" + albums[current]["cover"], on_chunk=tick)
                cover = None
                gc.collect()
                cover = data
                render(cover, playing)
                need_cover = spin = False
                last_change = time.time()
            except Exception as e:
                print("cover:", e)
                if not ensure_wifi():
                    continue
                ask_beacon(beacon)
                time.sleep(2)
                if read_beacon(beacon, urls):
                    save_urls(urls)
                    continue  # retry at once with the new address
                message("Can't load the cover", str(e)[:120])
                time.sleep(5)
                continue

        if read_beacon(beacon, urls):
            print("Pi moved:", urls)
            save_urls(urls)

        presto.touch_poll()
        x, y, down = presto.touch_a
        if down:
            if touch_start is None:
                touch_start = (x, y)
                swiped = False
            dx, dy = x - touch_start[0], y - touch_start[1]
            # Act on a swipe as soon as the finger has moved far enough, not on release.
            if not swiped and max(abs(dx), abs(dy)) >= SWIPE_PX:
                swiped = True
                if abs(dx) >= abs(dy):
                    if dx < 0:                        # swipe left: next cover
                        next_cover()
                    elif history:                     # swipe right: previous cover
                        current = history.pop()
                        need_cover = spin = True
                    if need_cover:
                        spinner(0)                    # instant feedback
                else:                                 # swipe up/down: next/previous track
                    command = "player_queues/next" if dy < 0 else "player_queues/previous"
                    command_with_leds(urls, command, {"queue_id": secrets.QUEUE_ID}, 10)
                    next_poll = time.ticks_add(time.ticks_ms(), 1000)
        elif touch_start is not None:
            touch_start = None
            if not swiped:                            # tap: play the album
                command_with_leds(urls, "player_queues/play_media",
                                  {"queue_id": secrets.QUEUE_ID, "media": albums[current]["uri"]},
                                  PLAY_TIMEOUT_S)
                next_poll = time.ticks_add(time.ticks_ms(), 1000)

        if touch_start is None and time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
            text = now_playing(urls)
            if text is not None and text != playing:
                playing = text
                if cover is not None and not need_cover:
                    render(cover, playing)
            next_poll = time.ticks_add(time.ticks_ms(), POLL_S * 1000)

        if time.time() - last_change > NEXT_AFTER_S:
            next_cover()
            spin = False

        time.sleep_ms(20)


main()
