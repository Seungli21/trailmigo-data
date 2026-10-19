#!/usr/bin/env python3
"""Pre-load NAIP aerial photo tiles along SoCal trails into the Ridgetrove relay (tiles.ridgetrove.com).

Requesting a tile through the relay makes it fetch the photo from USGS once and keep it in R2, so the
first hiker to look at a trail already gets it instantly. Trails are done in order of popularity.

  python3 tools/seed_tiles.py --start N --max-seconds S --concurrency C --state seed-state.json

Progress is an index into the (deterministic) ordered tile list, saved in the state file so the next
run continues where this one stopped. Each tile is requested once per pass, which keeps the Worker's
request count low (the free plan allows 100,000 requests a day).
"""
import argparse, gzip, json, math, os, sqlite3, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PACK_URL = "https://github.com/Seungli21/trailmigo-data/releases/download/packs/socal.db.gz"
RELAY = "https://tiles.ridgetrove.com/naip/{z}/{x}/{y}.jpg"
UA = "Ridgetrove tile seeder (https://ridgetrove.com)"


def decode(s):
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in (0, 1):
            res = sh = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                res |= (b & 0x1F) << sh
                sh += 5
                if b < 0x20:
                    break
            vals.append(~(res >> 1) if res & 1 else res >> 1)
        lat += vals[0]
        lon += vals[1]
        pts.append((lat / 1e5, lon / 1e5))
    return pts


def xy(lat, lon, z):
    n = 2 ** z
    r = math.radians(lat)
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(r)) / math.pi) / 2 * n


def corridor(poly, z=16, buf=1):
    """Zoom-16 tiles along a trail, plus one tile on each side (about 500 m)."""
    out = set()
    for piece in poly.split(";"):
        if not piece:
            continue
        p = decode(piece)
        for a, b in zip(p, p[1:] + p[-1:]):
            ax, ay = xy(*a, z)
            bx, by = xy(*b, z)
            steps = max(1, int(math.hypot(bx - ax, by - ay) * 4))
            for s in range(steps + 1):
                x = int(ax + (bx - ax) * s / steps)
                y = int(ay + (by - ay) * s / steps)
                for dx in range(-buf, buf + 1):
                    for dy in range(-buf, buf + 1):
                        out.add((x + dx, y + dy))
    return out


def tile_list(db_path, z17_top=0):
    """Zoom 15-16 tiles along every trail, most popular trails first; then, if z17_top, the sharpest
    level (zoom 17) along that many of the most popular trails. The order is stable, so a longer list
    keeps the same beginning and progress through it stays valid."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT g.poly FROM trails t JOIN trail_geom g ON g.id = t.id ORDER BY t.score DESC, t.id"
    ).fetchall()
    seen, order = set(), []
    def add(tiles):
        for t in tiles:
            if t not in seen:
                seen.add(t)
                order.append(t)
    for (poly,) in rows:
        z16 = sorted(corridor(poly))
        z15 = sorted({(x // 2, y // 2) for x, y in z16})
        add([(15, x, y) for x, y in z15] + [(16, x, y) for x, y in z16])
    for (poly,) in rows[:z17_top]:
        add([(17, x, y) for x, y in sorted(corridor(poly, z=17))])
    return order


def fetch(t):
    z, x, y = t
    req = urllib.request.Request(RELAY.format(z=z, x=x, y=y), headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
                return r.headers.get("X-Tile-Source") or "cache"
        except Exception as e:
            err = str(e)
            time.sleep(5 * (attempt + 1))
    return "error: " + err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="seed-state.json")
    ap.add_argument("--max-seconds", type=int, default=19800)
    ap.add_argument("--max-tiles", type=int, default=12000)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--pack", default="socal.db")
    a = ap.parse_args()

    if not os.path.exists(a.pack):
        with urllib.request.urlopen(PACK_URL, timeout=120) as r:
            open(a.pack, "wb").write(gzip.decompress(r.read()))
    tiles = tile_list(a.pack)
    state = json.load(open(a.state)) if os.path.exists(a.state) else {}
    start = state.get("next", 0)
    if start >= len(tiles):
        print(f"All {len(tiles)} tiles already seeded.")
        return
    batch = tiles[start:start + a.max_tiles]
    print(f"{len(tiles)} tiles in total; starting at {start}, up to {len(batch)} this run")

    t0 = time.time()
    counts, done = {}, 0
    with ThreadPoolExecutor(a.concurrency) as pool:
        it = iter(batch)
        running = {}
        def submit():
            t = next(it, None)
            if t is not None:
                running[pool.submit(fetch, t)] = t
        for _ in range(a.concurrency):
            submit()
        while running:
            fut = next(as_completed(list(running)))
            running.pop(fut)
            src = fut.result()
            key = "error" if src.startswith("error") else src
            counts[key] = counts.get(key, 0) + 1
            done += 1
            if done % 500 == 0:
                print(f"  {done} done ({time.time() - t0:.0f}s) {counts}", flush=True)
            if time.time() - t0 < a.max_seconds:
                submit()
    # Progress only advances over a contiguous prefix; with in-order submission that's simply `done`.
    nxt = start + done
    state = {"next": nxt, "total": len(tiles), "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
             "last_run": counts}
    json.dump(state, open(a.state, "w"), indent=1)
    pct = 100 * nxt / len(tiles)
    print(f"Run finished: {done} tiles in {time.time() - t0:.0f}s {counts}. Progress {nxt}/{len(tiles)} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
