# Album box: show a random album cover; tap it to play that album on the soundbar.
#
# Covers and the album list come from the Pi over Wi-Fi (server/docker-compose.yml,
# "covers" service); the Pi's address comes from its UDP beacon (server/beacon.py).
#
#   tap               play the album on screen and keep it there (stops the timer below)
#   swipe left        next cover (decoded ahead; spinner if it isn't ready); restarts the timer
#   swipe right       previous cover; restarts the timer
#   swipe up          next track on the soundbar
#   swipe down        previous track
#   every 30 minutes  next cover, unless a tap has kept the playing album on screen
#
# Bottom bar: the track the soundbar is playing, polled from Music Assistant.
# LEDs: amber while a command is in flight, then green (ok) or red (failed).
# Written for Pimoroni's Presto firmware v0.1.0 (MicroPython 1.25 preview). v2.0.0 was tried
# and dropped: unreliable Wi-Fi reconnects and freezes on this unit.

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
from presto import DISPLAY_PRESTO_FULL_RES, PEN_RGB565, PicoGraphics, Presto

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


def read_body(r, on_chunk=None):
    """Read a response body in 4 KB chunks. On firmware v2.0.0, r.content and r.json()
    (one unbounded read) are slow and get worse with size: 65 s for the 278 KB albums.json,
    against 0.35 s chunked. on_chunk(n) is called after each chunk (for the spinner)."""
    parts, n = [], 0
    while True:
        chunk = r.raw.read(4096)
        if not chunk:
            break
        parts.append(chunk)
        n += 1
        if on_chunk is not None:
            on_chunk(n)
    return b"".join(parts)


def http_get(url, timeout=15, on_chunk=None):
    """GET url into bytes."""
    r = requests.get(url, timeout=timeout)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, url))
        return read_body(r, on_chunk)
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
            print(command, "failed:", r.status_code, read_body(r)[:200])
            return False, None
        return True, json.loads(read_body(r))
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

FRAME_BYTES = WIDTH * HEIGHT * 2          # one decoded cover, RGB565
STRIP_OFF = (HEIGHT - BAR_H) * WIDTH * 2  # where the bottom bar starts in a frame
AHEAD_JPEGS = 15                          # covers downloaded ahead
AHEAD_FRAMES = 3                          # covers decoded ahead
BEHIND_FRAMES = 2                         # recent covers kept decoded, for swipe right
POOL = AHEAD_FRAMES + BEHIND_FRAMES + 2   # frame buffers: those, the one on screen, a spare


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


class Slot:
    """A spare frame buffer with its own PicoGraphics and JPEG decoder.

    The display's own buffer is never swapped out with set_framebuffer(): on firmware
    v2.0.0 that let the firmware keep writing to the freed original buffer, corrupting
    memory (snow on screen, lost variables, freezes). Instead each spare buffer gets its
    own PicoGraphics, and a decoded cover is copied into the display's buffer.
    """

    def __init__(self):
        self.buf = bytearray(FRAME_BYTES)
        self.pg = PicoGraphics(DISPLAY_PRESTO_FULL_RES, buffer=self.buf, layers=1, pen_type=PEN_RGB565)
        self.jpeg = jpegdec.JPEG(self.pg)


class Covers:
    """Downloads and decodes covers ahead of time, so a swipe is a ~90 ms copy.

    jpegs:  album index -> JPEG bytes (upcoming covers and a few recent ones)
    frames: album index -> Slot holding that cover decoded. Frames stay clean; the
            bottom bar and spinner are drawn only on the display's buffer.
    """

    def __init__(self, urls, albums, n_frames):
        self.urls, self.albums = urls, albums
        self.pool = []
        for _ in range(n_frames):
            self.pool.append(Slot())
        self.screen = memoryview(display)  # the display's own buffer
        self.frames = {}
        self.jpegs = {}
        self.current = None  # album index on screen
        self.bar = False     # bar drawn on screen

    def jpeg_for(self, i, on_chunk=None):
        data = self.jpegs.get(i)
        if data is None:
            data = http_get(self.urls["covers"] + "/" + self.albums[i]["cover"], on_chunk=on_chunk)
            self.jpegs[i] = data
        return data

    def _free_slot(self, keep):
        used = list(self.frames.values())
        for s in self.pool:
            if not any(s is u for u in used):
                return s
        for i in list(self.frames):
            if i not in keep and i != self.current:
                return self.frames.pop(i)
        return None

    def decode(self, i, keep):
        if i in self.frames:
            return True
        slot = self._free_slot(keep)
        if slot is None:
            return False
        data = self.jpeg_for(i)
        slot.jpeg.open_RAM(data)
        slot.jpeg.decode(0, 0, jpegdec.JPEG_SCALE_FULL, dither=False)
        self.frames[i] = slot
        return True

    def show(self, i, keep, playing):
        if i not in self.frames:
            # Not ready yet: spinner while it downloads, then decode.
            spinner(0)
            self.jpeg_for(i, on_chunk=spinner)
            if not self.decode(i, keep | {i}):
                raise MemoryError("no free frame buffer")
        self.screen[:] = self.frames[i].buf
        self.current = i
        self.bar = False
        self.set_bar(playing, push=False)
        presto.update()

    def set_bar(self, playing, push=True):
        if self.current not in self.frames:
            return
        if playing:
            draw_bar(playing)
            self.bar = True
        elif self.bar:
            self.screen[STRIP_OFF:] = memoryview(self.frames[self.current].buf)[STRIP_OFF:]
            self.bar = False
        else:
            return
        if push:
            presto.partial_update(0, HEIGHT - BAR_H, WIDTH, BAR_H)

    def work(self, upcoming, history):
        """Do one piece of background work (one download or one decode). False if none left."""
        keep = set(upcoming[:AHEAD_FRAMES]) | set(history[-BEHIND_FRAMES:])
        for i in upcoming[:AHEAD_FRAMES] + history[-BEHIND_FRAMES:][::-1]:
            if i not in self.frames:
                if i not in self.jpegs:
                    self.jpeg_for(i)
                    return True
                if self.decode(i, keep):
                    return True
        for i in upcoming[:AHEAD_JPEGS]:
            if i not in self.jpegs:
                self.jpeg_for(i)
                return True
        return False

    def prune(self, upcoming, history):
        keep = set(upcoming[:AHEAD_JPEGS]) | set(history[-BEHIND_FRAMES:]) | {self.current}
        for i in list(self.jpegs):
            if i not in keep:
                del self.jpegs[i]


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

    covers = None
    for n in range(POOL, 2, -1):  # as many frame buffers as fit
        try:
            covers = Covers(urls, albums, n)
            break
        except MemoryError:
            covers = None
            gc.collect()
    gc.collect()
    print(len(albums), "albums;", len(covers.pool), "frame buffers; free RAM", gc.mem_free())

    bag = Bag(len(albums))
    upcoming = [bag.next() for _ in range(AHEAD_JPEGS)]
    history = []          # covers shown before the current one
    current = upcoming.pop(0)
    upcoming.append(bag.next())
    pending = current     # cover to put on screen, or None
    playing = ""          # bottom bar text
    next_poll = time.ticks_ms()
    last_change = time.time()
    pinned = False        # a tap started this album: keep it on screen, no timer
    touch_start = None    # (x, y) where the current touch began
    swiped = False        # the current touch has already acted as a swipe
    idle_from = time.ticks_ms()  # background work waits until touches have settled

    def go_next():
        nonlocal current, pending
        history.append(current)
        del history[:-HISTORY]
        current = upcoming.pop(0)
        upcoming.append(bag.next())
        pending = current

    def go_back():
        nonlocal current, pending
        if history:
            upcoming.insert(0, current)
            current = history.pop()
            pending = current

    while True:
        if pending is not None:
            try:
                keep = set(upcoming[:AHEAD_FRAMES]) | set(history[-BEHIND_FRAMES:])
                covers.show(pending, keep, playing)
                pending = None
                last_change = time.time()
                covers.prune(upcoming, history)
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
            idle_from = time.ticks_add(time.ticks_ms(), 400)
            if touch_start is None:
                touch_start = (x, y)
                swiped = False
            dx, dy = x - touch_start[0], y - touch_start[1]
            # Act on a swipe as soon as the finger has moved far enough, not on release.
            if not swiped and max(abs(dx), abs(dy)) >= SWIPE_PX:
                swiped = True
                if abs(dx) >= abs(dy):                   # swipe left / right
                    pinned = False                       # back to the timed loop
                    last_change = time.time()            # (counts even with no history to go back to)
                    go_next() if dx < 0 else go_back()
                else:                                    # swipe up/down: next/previous track
                    command = "player_queues/next" if dy < 0 else "player_queues/previous"
                    command_with_leds(urls, command, {"queue_id": secrets.QUEUE_ID}, 10)
                    next_poll = time.ticks_add(time.ticks_ms(), 1000)
        elif touch_start is not None:
            touch_start = None
            if not swiped:                               # tap: play the album, keep it on screen
                if command_with_leds(urls, "player_queues/play_media",
                                     {"queue_id": secrets.QUEUE_ID, "media": albums[current]["uri"]},
                                     PLAY_TIMEOUT_S):
                    pinned = True
                next_poll = time.ticks_add(time.ticks_ms(), 1000)

        if pending is None and touch_start is None:
            if time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
                text = now_playing(urls)
                if text is not None and text != playing:
                    playing = text
                    covers.set_bar(playing)
                next_poll = time.ticks_add(time.ticks_ms(), POLL_S * 1000)
            elif time.ticks_diff(time.ticks_ms(), idle_from) >= 0:
                try:
                    if not covers.work(upcoming, history):
                        idle_from = time.ticks_add(time.ticks_ms(), 1000)
                except Exception as e:
                    print("prefetch:", e)
                    idle_from = time.ticks_add(time.ticks_ms(), 5000)

        if not pinned and time.time() - last_change > NEXT_AFTER_S:
            go_next()

        time.sleep_ms(20)


main()
