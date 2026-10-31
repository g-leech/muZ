#!/usr/bin/env python3
"""Build device/albums.json and device/covers/ from index.csv, lookup.csv and album_art/.

    python3 tools/prep_covers.py                          # Spotify albums only (default)
    python3 tools/prep_covers.py --providers spotify,bandcamp,ytmusic
    python3 tools/prep_covers.py --min-rating 85          # a smaller set

For each album, picks the first URI in lookup.csv (spotify_uri, fallback_uri, fallback2_uri)
whose provider is in --providers and that lookup_check.csv doesn't mark dead or unplayable.
Albums with none are left out. Covers are written as covers/<index_ID>.jpg: 480x480,
baseline (non-progressive) JPEG, since the Presto's decoder can't read progressive files.
Covers that are already 480x480 baseline JPEGs are copied unchanged.

Needs Pillow. Runs on the laptop or the Pi.
"""
import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SIZE = 480
URI_COLUMNS = ("spotify_uri", "fallback_uri", "fallback2_uri")
BAD_STATUSES = {"dead", "unplayable"}


def read_csv(name):
    path = ROOT / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pick_uri(lookup_row, providers, bad):
    for col in URI_COLUMNS:
        uri = (lookup_row.get(col) or "").strip()
        if uri and uri.split("://")[0] in providers and uri not in bad:
            return uri
    return None


def write_cover(src, dst, quality):
    """Copy src if it is already a 480x480 baseline JPEG; otherwise re-encode. Returns True if re-encoded."""
    with Image.open(src) as im:
        ok = (im.format == "JPEG" and im.size == (SIZE, SIZE)
              and not im.info.get("progressive") and not im.info.get("progression")
              and im.mode in ("RGB", "L"))
        if ok:
            shutil.copyfile(src, dst)
            return False
        im = ImageOps.fit(im.convert("RGB"), (SIZE, SIZE), Image.LANCZOS)
        im.save(dst, "JPEG", quality=quality, progressive=False, optimize=True)
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--providers", default="spotify",
                    help="comma-separated providers set up in MA (default: spotify)")
    ap.add_argument("--min-rating", type=int, default=0, help="only albums rated at least this")
    ap.add_argument("--out", type=Path, default=ROOT / "device", help="output folder (default: device/)")
    ap.add_argument("--quality", type=int, default=85, help="JPEG quality when re-encoding")
    args = ap.parse_args()
    providers = {p.strip() for p in args.providers.split(",") if p.strip()}

    index = read_csv("index.csv")
    lookup = {r["index_ID"]: r for r in read_csv("lookup.csv")}
    bad = {r["uri"] for r in read_csv("lookup_check.csv") if r.get("status") in BAD_STATUSES}

    covers_dir = args.out / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    albums, skipped, reencoded, missing = [], 0, 0, []
    for row in index:
        rating = int(row["user_rating"]) if row["user_rating"].isdigit() else 0
        if rating < args.min_rating:
            continue
        uri = pick_uri(lookup.get(row["index_ID"], {}), providers, bad)
        if not uri:
            skipped += 1
            continue
        src = ROOT / row["cover"]
        if not row["cover"] or not src.exists():
            missing.append(row["index_ID"])
            continue
        name = f"{row['index_ID']}.jpg"
        reencoded += write_cover(src, covers_dir / name, args.quality)
        albums.append({"uri": uri, "artist": row["artist"], "title": row["title"],
                       "cover": f"covers/{name}"})

    # Remove covers left over from an earlier, larger run.
    keep = {Path(a["cover"]).name for a in albums}
    stale = [p for p in covers_dir.glob("*.jpg") if p.name not in keep]
    for p in stale:
        p.unlink()

    with (args.out / "albums.json").open("w", encoding="utf-8") as f:
        json.dump(albums, f, ensure_ascii=False, separators=(",", ":"))

    size = sum(p.stat().st_size for p in covers_dir.glob("*.jpg"))
    print(f"{len(albums)} albums -> {args.out / 'albums.json'}")
    print(f"covers: {size / 1e6:.0f} MB in {covers_dir} ({reencoded} re-encoded, {len(stale)} stale removed)")
    print(f"left out: {skipped} with no playable URI for {sorted(providers)}")
    if missing:
        print(f"missing cover files for index_IDs: {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
