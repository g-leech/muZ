#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["ytmusicapi==1.12.3"]  # the version Music Assistant 2.10.9 uses
# ///
"""Check every URI in lookup.csv against Spotify, Bandcamp and YouTube Music.

Looks each item up much as Music Assistant would, counts the tracks that can be streamed,
and compares title and artist with index.csv. Writes the problems to lookup_check.csv.

    uv run tools/check_lookup.py          # everything (about 5,300 lookups, several minutes)
    uv run tools/check_lookup.py 33 41    # only these index_IDs

Statuses, worst first:
  dead        no such item
  unplayable  found, but no track can be streamed
  partial     some tracks can't be streamed (Bandcamp artists often limit this)
  check       found, but the title or artist looks different; check by eye
  error       the lookup itself failed; re-run for those IDs
The "used" column is yes when the URI is the row's first choice, i.e. what the box will play.
Spotify is checked via oEmbed, which confirms the album exists but not that it streams in the UK.
"""

import csv
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

from ytmusicapi import YTMusic

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = ("spotify_uri", "fallback_uri", "fallback2_uri")
WORKERS = {"spotify": 4, "bandcamp": 2, "ytmusic": 4}
USER_AGENT = "Mozilla/5.0 (album-box lookup check)"


class NotFound(Exception):
    pass


def get_json(url, tries=5):
    """GET url as JSON, retrying on rate limits, server errors and timeouts."""
    for attempt in range(tries):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound from e
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt == tries - 1:
                raise
            delay = int(e.headers.get("Retry-After") or 0) or 2**attempt
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            delay = 2**attempt
        time.sleep(delay)


# Each lookup returns (title, artist or None, streamable tracks or None, total tracks or None).


def spotify(kind, item_id):
    page = urllib.parse.quote(f"https://open.spotify.com/{kind}/{item_id}", safe="")
    data = get_json(f"https://open.spotify.com/oembed?url={page}")
    return data["title"], None, None, None


def bandcamp(kind, item_id):
    band_id, tralbum_id = item_id.split("-")[:2]
    # MA's Bandcamp client retries an album id that isn't found as a single track.
    for tralbum_type in ("a", "t") if kind == "album" else ("t",):
        data = get_json(
            "https://bandcamp.com/api/mobile/24/tralbum_details"
            f"?band_id={band_id}&tralbum_id={tralbum_id}&tralbum_type={tralbum_type}"
        )
        if not data.get("error"):
            break
        if "No such" not in data.get("error_message", ""):
            raise RuntimeError(data.get("error_message"))
    else:
        raise NotFound
    tracks = data.get("tracks") or []
    artist = data.get("tralbum_artist") or (data.get("band") or {}).get("name")
    return data.get("title"), artist, sum(1 for t in tracks if t.get("streaming_url")), len(tracks)


_yt = threading.local()


def ytmusic(kind, item_id):
    if not hasattr(_yt, "client"):
        _yt.client = YTMusic(location="GB")
    yt = _yt.client
    try:
        if kind == "track":
            song = yt.get_song(item_id)
            video = song.get("videoDetails") or {}
            if not video:
                raise NotFound
            playable = song.get("playabilityStatus", {}).get("status") == "OK"
            return video.get("title"), video.get("author"), int(playable), 1
        if kind == "album":
            data = yt.get_album(item_id)
        else:
            data = yt.get_playlist(item_id, limit=None)
    except KeyError as e:  # ytmusicapi's way of saying the page has no content
        raise NotFound from e
    tracks = data.get("tracks") or []
    artist = ", ".join(a["name"] for a in data.get("artists") or []) or (
        (data.get("author") or {}).get("name")
    )
    return data.get("title"), artist, sum(1 for t in tracks if t.get("isAvailable")), len(tracks)


LOOKUPS = {"spotify": spotify, "bandcamp": bandcamp, "ytmusic": ytmusic}


def norm(text):
    """Casefold, drop accents and punctuation; keep non-Latin letters."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def similarity(expected, found):
    """Best match of expected (with and without bracketed parts) against found, 0 to 1."""
    found = norm(found)
    best = 0.0
    for variant in (expected, re.sub(r"\s*[\[(].*?[\])]", "", expected or "")):
        variant = norm(variant)
        if not variant or not found:
            continue
        if variant in found or found in variant:
            return 1.0
        best = max(best, SequenceMatcher(None, variant, found).ratio())
    return best


def check(row, album, column, uri):
    provider, rest = uri.split("://", 1)
    kind, item_id = rest.split("/", 1)
    result = {
        "index_ID": row["index_ID"],
        "title": album["title"],
        "artist": album["artist"],
        "column": column,
        "uri": uri,
        "used": "yes" if column == next(c for c in COLUMNS if row[c]) else "",
    }
    try:
        title, artist, streamable, total = LOOKUPS[provider](kind, item_id)
    except NotFound:
        return {**result, "status": "dead", "problems": "no such item"}
    except Exception as e:  # noqa: BLE001 - report and move on
        return {**result, "status": "error", "problems": f"{type(e).__name__}: {e}"[:200]}

    problems = []
    if total is not None and streamable == 0:
        problems.append(("unplayable", f"0/{total} tracks streamable"))
    elif total is not None and streamable < total:
        problems.append(("partial", f"{streamable}/{total} tracks streamable"))
    if similarity(album["title"], title) < 0.6:
        problems.append(("check", "title differs"))
    # Playlists and single videos carry the uploader's name, not the artist's.
    if kind == "album" and artist and album["artist"] != "Various Artists":
        if similarity(album["artist"], artist) < 0.5:
            problems.append(("check", "artist differs"))
    return {
        **result,
        "status": problems[0][0] if problems else "ok",
        "problems": "; ".join(p for _, p in problems),
        "found_title": title,
        "found_artist": artist,
        "streamable": "" if total is None else f"{streamable}/{total}",
    }


def main():
    only = set(sys.argv[1:])
    albums = {r["index_ID"]: r for r in csv.DictReader(open(ROOT / "index.csv", encoding="utf-8"))}
    rows = [r for r in csv.DictReader(open(ROOT / "lookup.csv", encoding="utf-8"))
            if not only or r["index_ID"] in only]

    pools = {p: ThreadPoolExecutor(n) for p, n in WORKERS.items()}
    futures = [
        pools[row[c].split("://")[0]].submit(check, row, albums[row["index_ID"]], c, row[c])
        for row in rows for c in COLUMNS if row[c]
    ]
    results = []
    for done, future in enumerate(as_completed(futures), 1):
        results.append(future.result())
        if done % 200 == 0 or done == len(futures):
            print(f"{done}/{len(futures)} checked", file=sys.stderr)
    for pool in pools.values():
        pool.shutdown()

    order = {"dead": 0, "unplayable": 1, "partial": 2, "check": 3, "error": 4}
    problems = sorted((r for r in results if r["status"] != "ok"),
                      key=lambda r: (order[r["status"]], int(r["index_ID"]), r["column"]))
    fields = ["index_ID", "title", "artist", "column", "uri", "used", "status", "problems",
              "found_title", "found_artist", "streamable"]
    out = ROOT / "lookup_check.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(problems)

    print(f"\n{len(results)} URIs checked; {len(problems)} problems written to {out.name}")
    for status in order:
        n = sum(r["status"] == status for r in problems)
        used = sum(r["status"] == status and r["used"] == "yes" for r in problems)
        if n:
            print(f"  {status:<11} {n:>5}  ({used} of them first choice)")


if __name__ == "__main__":
    main()
