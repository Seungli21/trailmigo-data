#!/usr/bin/env python3
"""Pre-loads close-up aerial photos (zoom 17, 1024 px) along the most popular SoCal trails into the
Ridgetrove photo server, so the first hiker to zoom in on a trail gets it instantly.

Each tile is requested through tiles.ridgetrove.com, which fetches it from USGS once and keeps it.
Progress is kept in state.json (pushed to the "seed-state" branch) so each run continues the last.

  python3 seeding/seed_aerial.py --top 500 --state state.json --max-seconds 20000
"""
import argparse, json, os, sqlite3, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from seed_tiles import corridor  # noqa: E402

RELAY = "https://tiles.ridgetrove.com/naip/{z}/{x}/{y}.jpg"
UA = "Ridgetrove tile seeder (https://ridgetrove.com)"


def tiles_for(pack, top):
    rows = sqlite3.connect(pack).execute(
        "SELECT g.poly FROM trails t JOIN trail_geom g ON g.id = t.id ORDER BY t.score DESC, t.id LIMIT ?", (top,)
    ).fetchall()
    seen, order = set(), []
    for (poly,) in rows:
        for t in sorted(corridor(poly, z=17)):
            if t not in seen:
                seen.add(t)
                order.append((17,) + t)
    return order


def fetch(t):
    z, x, y = t
    req = urllib.request.Request(RELAY.format(z=z, x=x, y=y), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            r.read()
            src = r.headers.get("X-Tile-Source") or "cache"
            return "retry" if src == "usgs-512" else src  # a stand-in photo was not kept: try again later
    except Exception:
        return "retry"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="socal.db")
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--max-seconds", type=int, default=20000)
    ap.add_argument("--concurrency", type=int, default=12)
    a = ap.parse_args()

    tiles = tiles_for(a.pack, a.top)
    st = json.load(open(a.state)) if os.path.exists(a.state) else {}
    if st.get("top") != a.top:
        st = {"top": a.top, "next": st.get("next", 0), "retry": st.get("retry", [])}
    start = st["next"]
    todo = [tuple(t) for t in st.get("retry", [])] + tiles[start:]
    print(f"{len(tiles)} close-up tiles for the top {a.top} trails; at {start}, {len(st.get('retry', []))} to retry", flush=True)

    t0 = time.time()
    counts, retry, done_main = {}, [], 0
    n_retry = len(st.get("retry", []))
    with ThreadPoolExecutor(a.concurrency) as pool:
        i = 0
        while i < len(todo) and time.time() - t0 < a.max_seconds:
            chunk = todo[i:i + a.concurrency * 10]
            for t, res in zip(chunk, pool.map(fetch, chunk)):
                counts[res] = counts.get(res, 0) + 1
                if res == "retry":
                    retry.append(list(t))
            i += len(chunk)
            done_main = max(0, i - n_retry)
            if (i // len(chunk)) % 10 == 0:
                print(f"  {i} requested in {time.time() - t0:.0f}s {counts}", flush=True)
    if i < n_retry:  # ran out of time inside the retry list
        retry += [list(t) for t in todo[i:n_retry]]
    st["next"] = min(len(tiles), start + done_main)
    st["retry"] = retry[:20000]
    st["total"] = len(tiles)
    st["updated"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    st["last_run"] = counts
    json.dump(st, open(a.state, "w"), indent=1)
    print(f"Done: progress {st['next']}/{len(tiles)} ({100 * st['next'] / len(tiles):.1f}%), {len(retry)} to retry. {counts}")


if __name__ == "__main__":
    main()
