#!/usr/bin/env python3
"""
Builds a Trailmigo trail pack (a SQLite database) for one region from OpenStreetMap data.

For every named trail and hiking route it works out, ahead of time, what a hiking app needs
for instant offline search: length, route type (loop / out & back / point to point),
elevation gain and profile (from open terrain data), a difficulty rating, nearby features
(waterfalls, peaks, viewpoints, lakes, ...), the park it is in, trailhead parking,
dog / wheelchair access and links to Wikipedia / Wikidata.

Usage:  build_pack.py INPUT.osm.pbf OUTPUT.db --region-id socal --region-name "Southern California"

Data: © OpenStreetMap contributors (ODbL). Terrain: Mapzen / AWS Open Data terrain tiles.
"""
import argparse
import io
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import osmium
import shapely.wkb
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

# ---------------------------------------------------------------- helpers

R_EARTH = 6371000.0


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def line_len(coords):
    """coords: list of (lat, lon)"""
    return sum(hav(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def encode_polyline(coords, precision=5):
    """Google polyline encoding of (lat, lon) pairs."""
    factor = 10 ** precision
    out = []
    plat = plon = 0
    for lat, lon in coords:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def simplify(coords, tol_m=4.0):
    """Douglas-Peucker in degrees (approx meters) to keep packs small."""
    if len(coords) < 3:
        return coords
    ls = LineString([(lon, lat) for lat, lon in coords])
    s = ls.simplify(tol_m / 111000.0, preserve_topology=False)
    return [(y, x) for x, y in s.coords]


def norm_name(n):
    return " ".join(n.lower().replace("’", "'").split())


# ---------------------------------------------------------------- terrain

class Terrain:
    """Elevation from AWS terrain tiles (Terrarium encoding), zoom 12 (~31 m pixels at 34°N)."""
    URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

    def __init__(self, zoom=12, cache_dir="terrain-cache", mock=False):
        self.z = zoom
        self.cache_dir = cache_dir
        self.mock = mock
        self.tiles = {}
        os.makedirs(cache_dir, exist_ok=True)

    def _xy(self, lat, lon):
        n = 2 ** self.z
        x = (lon + 180.0) / 360.0 * n
        r = math.radians(lat)
        y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
        return x, y

    def prefetch(self, points):
        need = set()
        for lat, lon in points:
            x, y = self._xy(lat, lon)
            need.add((int(x), int(y)))
        need -= set(self.tiles)
        if self.mock:
            return
        print(f"terrain: fetching {len(need)} tiles", flush=True)
        import requests
        from PIL import Image
        import numpy as np
        session = requests.Session()
        session.headers["User-Agent"] = "trailmigo-pack-builder (github.com/Seungli21/trailmigo-data)"

        def load(t):
            tx, ty = t
            path = os.path.join(self.cache_dir, f"{self.z}_{tx}_{ty}.png")
            if not os.path.exists(path):
                for attempt in range(4):
                    try:
                        r = session.get(self.URL.format(z=self.z, x=tx, y=ty), timeout=30)
                        if r.status_code == 200:
                            with open(path, "wb") as f:
                                f.write(r.content)
                            break
                        if r.status_code == 404:
                            return t, None
                    except Exception:
                        pass
                    time.sleep(1 + attempt * 2)
                else:
                    return t, None
            img = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
            ele = img[:, :, 0] * 256.0 + img[:, :, 1] + img[:, :, 2] / 256.0 - 32768.0
            return t, ele

        with ThreadPoolExecutor(16) as ex:
            for t, ele in ex.map(load, sorted(need)):
                self.tiles[t] = ele
        print("terrain: done", flush=True)

    def ele(self, lat, lon):
        if self.mock:
            # Smooth fake hills for local tests.
            return 300 + 200 * math.sin(lat * 400) + 150 * math.cos(lon * 300)
        x, y = self._xy(lat, lon)
        tx, ty = int(x), int(y)
        px, py = (x - tx) * 256 - 0.5, (y - ty) * 256 - 0.5
        t = self.tiles.get((tx, ty))
        if t is None:
            return None
        x0, y0 = int(math.floor(px)), int(math.floor(py))
        fx, fy = px - x0, py - y0

        def at(ix, iy):
            ix = min(max(ix, 0), 255)
            iy = min(max(iy, 0), 255)
            return float(t[iy, ix])

        v = (at(x0, y0) * (1 - fx) * (1 - fy) + at(x0 + 1, y0) * fx * (1 - fy) +
             at(x0, y0 + 1) * (1 - fx) * fy + at(x0 + 1, y0 + 1) * fx * fy)
        return v


# ---------------------------------------------------------------- reading OSM

TRAIL_HW = {"path", "footway", "bridleway", "track", "steps"}
PAVED = {"asphalt", "concrete", "paved", "paving_stones", "concrete:plates", "sett", "chipseal"}
ROUTE_TYPES = {"hiking", "foot", "running"}
HISTORIC_SIGHTS = {"ruins", "archaeological_site", "fort", "mine", "mine_shaft", "wreck", "castle", "monument",
                   "aircraft", "tomb", "building", "manor", "railway_car", "locomotive", "cannon"}


class Reader(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = {}           # id -> (tags dict, [(lat, lon)])
        self.routes = []         # (id, tags, [way ids])
        self.points = []         # (kind, name, lat, lon, extra)
        self.parks = []          # (name, kind, polygon, wikipedia, wikidata)
        self.water = []          # (name, polygon)
        self.rivers = []         # (name, [(lat, lon)])
        self.parking = []        # (lat, lon)
        self.trailheads = []     # (lat, lon, name, kind)
        self.wkb = osmium.geom.WKBFactory()

    def node(self, n):
        t = n.tags
        if not t:
            return
        lat, lon = n.location.lat, n.location.lon
        name = t.get("name", "")
        nat = t.get("natural")
        if nat == "peak" or nat == "volcano":
            self.points.append(("peak", name, lat, lon, t.get("ele", "")))
        elif nat == "waterfall" or t.get("waterway") == "waterfall":
            self.points.append(("waterfall", name, lat, lon, ""))
        elif t.get("tourism") == "viewpoint":
            self.points.append(("viewpoint", name, lat, lon, ""))
        elif nat in ("hot_spring", "geyser"):
            self.points.append(("hot_spring", name, lat, lon, ""))
        elif nat == "cave_entrance":
            self.points.append(("cave", name, lat, lon, ""))
        elif nat == "spring":
            self.points.append(("spring", name, lat, lon, ""))
        elif nat == "beach":
            self.points.append(("beach", name, lat, lon, ""))
        elif nat == "arch":
            self.points.append(("arch", name, lat, lon, ""))
        if t.get("historic") in HISTORIC_SIGHTS:
            self.points.append(("historic", name, lat, lon, t.get("historic")))
        if t.get("amenity") == "parking":
            self.parking.append((lat, lon))
            self.trailheads.append((lat, lon, name, "parking"))
        if t.get("highway") == "trailhead":
            self.parking.append((lat, lon))
            self.trailheads.append((lat, lon, name, "trailhead"))

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        try:
            coords = [(n.location.lat, n.location.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        if hw in TRAIL_HW:
            self.ways[w.id] = (dict(t), coords, [n.ref for n in w.nodes])
        if t.get("waterway") == "river" and t.get("name"):
            self.rivers.append((t.get("name"), coords))
        if t.get("amenity") == "parking":
            clat = sum(c[0] for c in coords) / len(coords)
            clon = sum(c[1] for c in coords) / len(coords)
            self.parking.append((clat, clon))
            self.trailheads.append((clat, clon, t.get("name", ""), "parking"))

    def relation(self, r):
        t = r.tags
        if t.get("type") == "route" and t.get("route") in ROUTE_TYPES:
            ways = [m.ref for m in r.members if m.type == "w"]
            has_rel = any(m.type == "r" for m in r.members)
            if ways and not has_rel:
                self.routes.append((r.id, dict(t), ways))

    def area(self, a):
        t = a.tags
        name = t.get("name")
        if not name:
            # unnamed water still useful for "lake" detection only if it's sizeable; skip for simplicity
            return
        kind = None
        if t.get("boundary") in ("national_park", "protected_area") or t.get("leisure") in ("nature_reserve", "park"):
            kind = t.get("boundary") or t.get("leisure")
        is_water = t.get("natural") == "water" and t.get("water", "lake") in ("lake", "reservoir", "pond", "lagoon", "basin")
        if not kind and not is_water:
            return
        try:
            poly = shapely.wkb.loads(self.wkb.create_multipolygon(a), hex=True)
        except Exception:
            return
        if not poly.is_valid:
            poly = poly.buffer(0)
        if is_water:
            self.water.append((name, poly))
        if kind:
            self.parks.append((name, kind, poly, t.get("wikipedia", ""), t.get("wikidata", "")))


# ---------------------------------------------------------------- building trails

def is_trail_way(tags):
    hw = tags.get("highway")
    if hw not in TRAIL_HW:
        return False
    if tags.get("access") in ("private", "no") or tags.get("foot") in ("private", "no"):
        return False
    if hw == "footway":
        if tags.get("footway") in ("sidewalk", "crossing", "traffic_island", "access_aisle"):
            return False
        if tags.get("surface", "") in PAVED:
            return False
    return True


def paved_majority(tags_list):
    surfaces = [t.get("surface", "") for t in tags_list]
    return sum(1 for x in surfaces if x in PAVED) > len(surfaces) / 2


def chain(pieces, join_m=30.0):
    """Greedily joins line pieces end to end. Returns (main_chain, leftover_pieces)."""
    pieces = [p for p in pieces if len(p) >= 2]
    if not pieces:
        return [], []
    pieces = sorted(pieces, key=line_len, reverse=True)
    main = list(pieces[0])
    rest = pieces[1:]
    grew = True
    while grew and rest:
        grew = False
        best = None
        for i, p in enumerate(rest):
            for at_end in (True, False):
                anchor = main[-1] if at_end else main[0]
                for rev in (False, True):
                    q = p[::-1] if rev else p
                    d = hav(anchor[0], anchor[1], q[0][0], q[0][1]) if at_end else hav(anchor[0], anchor[1], q[-1][0], q[-1][1])
                    if d <= join_m and (best is None or d < best[0]):
                        best = (d, i, at_end, q)
        if best:
            _, i, at_end, q = best
            main = main + q[1:] if at_end else q[:-1] + main
            rest.pop(i)
            grew = True
    return main, rest


def resample(coords, step=30.0, max_points=2500):
    total = line_len(coords)
    if total == 0:
        return [coords[0]], step
    step = max(step, total / max_points)
    out = [coords[0]]
    acc = 0.0
    next_at = step
    for a, b in zip(coords, coords[1:]):
        d = hav(a[0], a[1], b[0], b[1])
        while d > 0 and acc + d >= next_at:
            f = (next_at - acc) / d
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            next_at += step
        acc += d
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out, step


def gain_loss(profile, threshold=3.0):
    if not profile:
        return 0.0, 0.0
    # light smoothing
    sm = []
    for i in range(len(profile)):
        w = profile[max(0, i - 1): i + 2]
        sm.append(sum(w) / len(w))
    gain = loss = 0.0
    anchor = sm[0]
    for v in sm[1:]:
        d = v - anchor
        if d >= threshold:
            gain += d
            anchor = v
        elif d <= -threshold:
            loss += -d
            anchor = v
    return gain, loss


def difficulty(trip_m, gain_m, sac):
    miles = trip_m / 1609.344
    gain_ft = gain_m * 3.28084
    shenandoah = math.sqrt(max(0.0, gain_ft * 2 * miles))
    level = 1 if shenandoah < 50 else 2 if shenandoah < 100 else 3 if shenandoah < 150 else 4
    if sac in ("demanding_mountain_hiking", "alpine_hiking"):
        level = max(level, 3)
    if sac in ("demanding_alpine_hiking", "difficult_alpine_hiking"):
        level = 4
    return level


# ---------------------------------------------------------------- suggested routes (loops & destinations)

DEST_WEIGHT = {"peak": 3.0, "waterfall": 3.0, "viewpoint": 2.0, "hot_spring": 2.5, "arch": 2.0, "cave": 1.5}


def build_graph(rd):
    """Trail network: vertices are junctions and ends, edges are trail pieces between them."""
    use = defaultdict(int)
    ways = {wid: v for wid, v in rd.ways.items() if is_trail_way(v[0])}
    for wid, (tags, coords, refs) in ways.items():
        for i, r in enumerate(refs):
            use[r] += 2 if i in (0, len(refs) - 1) else 1
    vpos = {}
    edges = []   # (u, v, coords, length, way_id)
    adj = defaultdict(list)
    for wid, (tags, coords, refs) in ways.items():
        cut = [i for i, r in enumerate(refs) if use[r] >= 2]
        for a, b in zip(cut, cut[1:]):
            seg = coords[a:b + 1]
            if len(seg) < 2:
                continue
            L = line_len(seg)
            if L <= 0:
                continue
            e = len(edges)
            edges.append((refs[a], refs[b], seg, L, wid))
            adj[refs[a]].append((e, refs[b]))
            adj[refs[b]].append((e, refs[a]))
            vpos[refs[a]] = coords[a]
            vpos[refs[b]] = coords[b]
    return ways, edges, adj, vpos


def dijkstra(adj, edges, src, cutoff, penalty=None):
    import heapq
    dist = {src: 0.0}
    prev = {}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, 1e18) or d > cutoff:
            continue
        for e, v in adj[u]:
            w = edges[e][3] * (penalty.get(e, 1.0) if penalty else 1.0)
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = (u, e)
                heapq.heappush(heap, (nd, v))
    return dist, prev


def walk(prev, src, dst):
    """List of (from_vertex, edge) from src to dst, or None."""
    if dst not in prev and dst != src:
        return None
    out = []
    v = dst
    while v != src:
        u, e = prev[v]
        out.append((u, e))
        v = u
    out.reverse()
    return out


def path_coords(edges, steps):
    pts = []
    for u, e in steps:
        a, b, seg, L, wid = edges[e]
        seg = seg if a == u else seg[::-1]
        pts.extend(seg if not pts else seg[1:])
    return pts


def build_suggested(rd, args, park_tree, park_geoms):
    t0 = time.time()
    ways, edges0, _adj0, vpos = build_graph(rd)
    print(f"graph: {len(vpos)} junctions, {len(edges0)} trail pieces", flush=True)
    if not edges0:
        return []
    etree = STRtree([LineString([(lon, lat) for lat, lon in e[2]]) for e in edges0])

    def locate(lat, lon, max_m):
        """Nearest point on the trail network: (edge, coordinate index) or None."""
        idx = etree.query_nearest(Point(lon, lat), max_distance=max_m / 111000.0 * 1.5)
        if len(idx) == 0:
            return None
        e = int(idx[0])
        seg = edges0[e][2]
        i = min(range(len(seg)), key=lambda k: hav(lat, lon, seg[k][0], seg[k][1]))
        if hav(lat, lon, seg[i][0], seg[i][1]) > max_m:
            return None
        return (e, i)

    def in_park(lat, lon):
        if park_tree is None:
            return False
        return len(park_tree.query(Point(lon, lat), predicate="intersects")) > 0

    # Where trailheads and destinations touch the trail network
    th_hits = []
    for lat, lon, name, kind in rd.trailheads:
        if kind != "trailhead" and not in_park(lat, lon):
            continue  # parking counts only in parks / protected areas
        loc = locate(lat, lon, 300)
        if loc:
            th_hits.append((loc, name, kind))
    dest_hits = []
    for kind, name, lat, lon, extra in rd.points:
        if kind not in DEST_WEIGHT or (kind == "peak" and not name):
            continue
        loc = locate(lat, lon, 150)
        if loc:
            pr = DEST_WEIGHT[kind] + (0.5 if name else 0)
            if kind == "peak":
                try:
                    pr += min(float(extra.split()[0]) / 2000.0, 1.5)
                except Exception:
                    pass
            dest_hits.append((loc, (pr, kind, name, extra)))

    # Split trail pieces where they are touched, so those spots become junctions.
    cuts = defaultdict(set)
    for (e, i), *_ in th_hits + dest_hits:
        cuts[e].add(i)
    edges = []
    adj = defaultdict(list)
    vertex_at = {}  # (edge0, index) -> vertex id
    for e0, (u, v, seg, L, wid) in enumerate(edges0):
        points = sorted({0, len(seg) - 1} | cuts.get(e0, set()))
        ids = []
        for i in points:
            vid = u if i == 0 else v if i == len(seg) - 1 else ("s", e0, i)
            vertex_at[(e0, i)] = vid
            vpos[vid] = seg[i]
            ids.append(vid)
        for (a, va), (b, vb) in zip(zip(points, ids), list(zip(points, ids))[1:]):
            piece = seg[a:b + 1]
            pl = line_len(piece)
            if len(piece) < 2 or pl <= 0:
                continue
            k = len(edges)
            edges.append((va, vb, piece, pl, wid))
            adj[va].append((k, vb))
            adj[vb].append((k, va))

    starts = {}
    for (loc, name, kind) in th_hits:
        sv = vertex_at[loc]
        if sv not in starts or (name and not starts[sv][0]):
            starts[sv] = (name, kind)
    dests = defaultdict(list)
    for loc, item in dest_hits:
        dests[vertex_at[loc]].append(item)
    print(f"trailheads: {len(starts)}, destinations: {len(dests)}", flush=True)

    def way_name(e):
        return ways[edges[e][4]][0].get("name", "")

    cands = []
    for s_v, (s_name, s_kind) in starts.items():
        dist, prev = dijkstra(adj, edges, s_v, args.route_max_km * 1000)
        options = []
        for d_v, items in dests.items():
            d = dist.get(d_v)
            if d is None or d < 700 or d > args.route_max_km * 1000:
                continue
            best = max(items)
            options.append((best[0], d, d_v, best))
        options.sort(key=lambda o: (-o[0], o[1]))
        for pr, d, d_v, (_, kind, name, extra) in options[:3]:
            steps = walk(prev, s_v, d_v)
            if not steps:
                continue
            e1 = [e for _, e in steps]
            set1 = set(e1)
            # Try to come back a different way.
            pen = {e: 8.0 for e in e1}
            dist2, prev2 = dijkstra(adj, edges, d_v, d * 3 + 2000, pen)
            back = walk(prev2, d_v, s_v)
            is_loop = False
            if back:
                L2 = sum(edges[e][3] for _, e in back)
                overlap = sum(edges[e][3] for _, e in back if e in set1)
                if L2 <= 2.0 * d and overlap / max(L2, 1) < 0.3:
                    is_loop = True
            steps_all = steps + (back if is_loop else [])
            all_edges = [e for _, e in steps_all]
            # Names of the trails used, in order, weighted by length
            share = defaultdict(float)
            order = []
            for e in all_edges:
                n = way_name(e)
                if n:
                    share[n] += edges[e][3]
                    if n not in order:
                        order.append(n)
            total = sum(edges[e][3] for e in all_edges)
            dominant = max(share, key=share.get) if share else ""
            if not is_loop and dominant and share[dominant] >= 0.9 * total:
                continue  # just a stretch of one named trail, which is already listed
            label = name or {"viewpoint": "Viewpoint", "waterfall": "Waterfall", "hot_spring": "Hot Spring",
                              "arch": "Rock Arch", "cave": "Cave"}.get(kind, "Summit")
            if is_loop:
                title = f"{label} Loop"
            elif dominant and dominant.lower() not in label.lower() and label.lower() not in dominant.lower():
                title = f"{label} via {dominant}"
            else:
                title = dominant or f"{label} Trail"
            coords = path_coords(edges, steps_all)
            if len(coords) < 2:
                continue
            cands.append(dict(
                prio=pr + (0.8 if is_loop else 0), edges=set(all_edges),
                weights={e: edges[e][3] for e in all_edges},
                cand=dict(
                    name=title, kind="suggested", pieces=[coords],
                    way_tags=[ways[edges[e][4]][0] for e in dict.fromkeys(all_edges)],
                    rel_tags={}, osm=",".join(f"w{edges[e][4]}" for e in list(dict.fromkeys(all_edges))[:20]),
                    fixed_start=True, via=order[:6], dest=(kind, name, extra),
                ),
            ))
    # Keep the best route for each distinct path (drop near-duplicates).
    cands.sort(key=lambda c: -c["prio"])
    kept = []
    by_edge = defaultdict(list)
    for c in cands:
        dup = False
        seen_k = set()
        for e in c["edges"]:
            for k in by_edge[e]:
                if k in seen_k:
                    continue
                seen_k.add(k)
                other = kept[k]
                inter = sum(c["weights"][x] for x in c["edges"] & other["edges"])
                union = sum(c["weights"].values()) + sum(other["weights"].values()) - inter
                if union > 0 and inter / union > 0.6:
                    dup = True
                    break
            if dup:
                break
        if dup:
            continue
        k = len(kept)
        kept.append(c)
        for e in c["edges"]:
            by_edge[e].append(k)
    print(f"suggested routes: {len(kept)} (from {len(cands)} candidates, {time.time()-t0:.0f}s)", flush=True)
    return [c["cand"] for c in kept]


def build(args):
    t0 = time.time()
    rd = Reader()
    print("reading", args.input, flush=True)
    rd.apply_file(args.input, locations=True, idx="flex_mem")
    print(f"read: {len(rd.ways)} trail ways, {len(rd.routes)} routes, {len(rd.points)} points, "
          f"{len(rd.parks)} parks, {len(rd.water)} lakes, {len(rd.parking)} parking  ({time.time()-t0:.0f}s)", flush=True)

    candidates = []  # dict(name, kind, pieces, way_tags, rel_tags, osm)

    # 1. Hiking route relations
    route_names = set()
    for rid, tags, way_ids in rd.routes:
        pieces = [rd.ways[w][1] for w in way_ids if w in rd.ways]
        if not pieces:
            continue
        total = sum(line_len(p) for p in pieces)
        name = tags.get("name") or tags.get("ref")
        if not name or total < 300:
            continue
        if total > args.max_route_km * 1000:
            continue  # long-distance trails (e.g. the PCT) are too big to be one trail
        route_names.add(norm_name(name))
        candidates.append(dict(
            name=name, kind="route", pieces=pieces,
            way_tags=[rd.ways[w][0] for w in way_ids if w in rd.ways],
            rel_tags=tags, osm=f"r{rid}",
        ))

    # 2. Named trail ways, grouped by name and touching ends
    by_name = defaultdict(list)
    for wid, (tags, coords, _refs) in rd.ways.items():
        n = tags.get("name")
        if n and is_trail_way(tags):
            by_name[norm_name(n)].append(wid)

    cell = 0.0004
    for nn, wids in by_name.items():
        parent = {w: w for w in wids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        grid = defaultdict(list)
        for w in wids:
            c = rd.ways[w][1]
            for p in (c[0], c[-1]):
                grid[(round(p[0] / cell), round(p[1] / cell))].append(w)
        for (gx, gy), ws in grid.items():
            near = list(ws)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        near += grid.get((gx + dx, gy + dy), [])
            for w in near[1:]:
                parent[find(w)] = find(near[0])
        clusters = defaultdict(list)
        for w in wids:
            clusters[find(w)].append(w)
        for members in clusters.values():
            pieces = [rd.ways[w][1] for w in members]
            total = sum(line_len(p) for p in pieces)
            if total < args.min_trail_m or total > args.max_route_km * 1000:
                continue
            tags_list = [rd.ways[w][0] for w in members]
            # a named track (fire road) counts only if it looks like a trail, not a farm road
            if all(t.get("highway") == "track" for t in tags_list):
                if not any(t.get("foot") in ("yes", "designated") or "trail" in t.get("name", "").lower()
                           or "fire road" in t.get("name", "").lower() or "motorway" in t.get("name", "").lower()
                           for t in tags_list):
                    continue
            name = tags_list[0].get("name")
            if nn in route_names:
                # Same name as a route relation: keep only if it is somewhere else entirely.
                pass
            candidates.append(dict(
                name=name, kind="trail", pieces=pieces, way_tags=tags_list, rel_tags={},
                osm=",".join(f"w{w}" for w in sorted(members)[:20]),
            ))
    # 3. Suggested routes from trailheads to destinations (loops where the trails allow)
    if not args.no_suggested:
        sp_geoms = [p for _, _, p, _, _ in rd.parks]
        sp_tree = STRtree(sp_geoms) if sp_geoms else None
        candidates += build_suggested(rd, args, sp_tree, sp_geoms)
    print(f"candidates: {len(candidates)}", flush=True)

    # 3. Geometry: chain, resample, terrain
    for c in candidates:
        main, rest = chain(c["pieces"])
        c["main"] = main
        c["rest"] = rest
        c["total"] = sum(line_len(p) for p in c["pieces"])
        c["main_len"] = line_len(main)
        c["samples"], c["step"] = resample(main)

    # drop route-relation duplicates of named-way clusters that cover the same ground
    terrain = Terrain(zoom=12, cache_dir=args.terrain_cache, mock=args.mock_terrain)
    terrain.prefetch([p for c in candidates for p in c["samples"]])

    # 4. Spatial indexes for features, parks, parking
    feat_pts = [Point(lon, lat) for _, _, lat, lon, _ in rd.points]
    feat_tree = STRtree(feat_pts) if feat_pts else None
    water_geoms = [p for _, p in rd.water]
    water_tree = STRtree(water_geoms) if water_geoms else None
    river_geoms = [LineString([(lon, lat) for lat, lon in c]) for _, c in rd.rivers]
    river_tree = STRtree(river_geoms) if river_geoms else None
    park_geoms = [p for _, _, p, _, _ in rd.parks]
    park_tree = STRtree(park_geoms) if park_geoms else None
    park_area = [p.area for p in park_geoms]
    parking_pts = [Point(lon, lat) for lat, lon in rd.parking]
    parking_tree = STRtree(parking_pts) if parking_pts else None

    out_rows = []
    seen = []  # (name, center) to drop exact duplicates
    for c in candidates:
        main = c["main"]
        if len(main) < 2:
            continue
        lats = [p[0] for pc in c["pieces"] for p in pc]
        lons = [p[1] for pc in c["pieces"] for p in pc]
        min_lat, max_lat, min_lon, max_lon = min(lats), max(lats), min(lons), max(lons)
        clat, clon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2

        # Route type
        start, end = main[0], main[-1]
        closed = hav(start[0], start[1], end[0], end[1]) < 150 and c["main_len"] >= 0.8 * c["total"]
        if closed:
            rtype, trip = "loop", c["total"]
        elif c["rel_tags"].get("roundtrip") == "no":
            rtype, trip = "point", c["total"]
        else:
            rtype, trip = "out_back", 2 * c["total"]

        # Elevation
        profile = [terrain.ele(lat, lon) for lat, lon in c["samples"]]
        profile = [p for p in profile if p is not None]
        g, l = gain_loss(profile)
        if rtype == "out_back":
            trip_gain, trip_loss = g + l, g + l
        else:
            trip_gain, trip_loss = g, l
        max_ele = max(profile) if profile else None
        min_ele = min(profile) if profile else None
        # Trailhead: the lower end is usually where people start, unless parking says otherwise
        if profile and rtype != "loop" and not c.get("fixed_start") and profile[-1] < profile[0] - 20:
            main = main[::-1]
            profile = profile[::-1]
            start, end = main[0], main[-1]

        # Tags across the trail's ways
        wt = c["way_tags"]
        rt = c["rel_tags"]
        sac_order = ["hiking", "mountain_hiking", "demanding_mountain_hiking", "alpine_hiking",
                     "demanding_alpine_hiking", "difficult_alpine_hiking"]
        sacs = [t.get("sac_scale") for t in wt if t.get("sac_scale") in sac_order]
        sac = max(sacs, key=sac_order.index) if sacs else ""
        diff = difficulty(trip, trip_gain, sac)
        dogs = ""
        for t in wt + [rt]:
            if t.get("dog") in ("no",):
                dogs = "no"
                break
            if t.get("dog") in ("yes", "leashed", "designated"):
                dogs = t.get("dog")
        wheelchair = int(any(t.get("wheelchair") in ("yes", "designated") for t in wt))
        surfaces = [t.get("surface", "") for t in wt]
        paved = int(sum(1 for s in surfaces if s in PAVED) > len(surfaces) / 2)
        acts = {"hike"}
        if any(t.get("bicycle") in ("yes", "designated") or "mtb:scale" in t for t in wt) or rt.get("route") == "mtb":
            acts.add("bike")
        if any(t.get("horse") in ("yes", "designated") or t.get("highway") == "bridleway" for t in wt):
            acts.add("horse")
        if rt.get("route") == "running":
            acts.add("run")

        # Features along the trail (within ~150 m)
        line = LineString([(lon, lat) for lat, lon in main]) if len(main) > 1 else None
        geom_all = [LineString([(lon, lat) for lat, lon in p]) for p in c["pieces"] if len(p) > 1]
        buf_deg = 150.0 / 111000.0 / max(0.3, math.cos(math.radians(clat)))
        feats = set()
        peaks = []
        near_names = []
        if feat_tree is not None:
            for g_ in geom_all:
                for idx in feat_tree.query(g_.buffer(buf_deg)):
                    kind, name, plat, plon, extra = rd.points[int(idx)]
                    if kind == "peak":
                        if g_.distance(feat_pts[int(idx)]) < buf_deg * 0.7:
                            feats.add("peak")
                            feats.add("views")
                            if name:
                                peaks.append((name, extra))
                    elif kind == "viewpoint":
                        feats.add("views")
                    elif kind in ("waterfall", "hot_spring", "cave", "beach", "historic", "spring", "arch"):
                        feats.add(kind)
                    if name and kind in ("waterfall", "hot_spring", "cave", "beach"):
                        near_names.append(name)
        if water_tree is not None:
            for g_ in geom_all:
                for idx in water_tree.query(g_.buffer(buf_deg), predicate="intersects"):
                    feats.add("lake")
                    near_names.append(rd.water[int(idx)][0])
        if river_tree is not None:
            for g_ in geom_all:
                if len(river_tree.query(g_.buffer(buf_deg * 0.6), predicate="intersects")):
                    feats.add("river")
                    break

        # Park: the smallest named park/protected area containing the trail's middle
        park = park_wp = park_wd = ""
        if park_tree is not None:
            mid = main[len(main) // 2]
            pt = Point(mid[1], mid[0])
            best = None
            for idx in park_tree.query(pt, predicate="intersects"):
                i = int(idx)
                if best is None or park_area[i] < park_area[best]:
                    best = i
            if best is not None:
                park, _, _, park_wp, park_wd = rd.parks[best]

        # City walkways (sidewalk-like footways, paved promenades) only count inside a park.
        hw_kinds = {t.get("highway") for t in wt}
        urban_like = hw_kinds <= {"footway", "steps"} or paved_majority(wt)
        if urban_like and not park and c["kind"] != "route":
            continue

        # Parking near either end
        parking = 0
        if parking_tree is not None:
            r_deg = 400.0 / 111000.0
            near_start = len(parking_tree.query(Point(start[1], start[0]).buffer(r_deg)))
            near_end = len(parking_tree.query(Point(end[1], end[0]).buffer(r_deg)))
            if near_end and not near_start and rtype != "loop" and not c.get("fixed_start"):
                main = main[::-1]
                profile = profile[::-1]
                start, end = main[0], main[-1]
            parking = int(bool(near_start or near_end))

        kid = int(diff == 1 and trip <= 5000)
        wikipedia = rt.get("wikipedia") or next((t.get("wikipedia") for t in wt if t.get("wikipedia")), "")
        wikidata = rt.get("wikidata") or next((t.get("wikidata") for t in wt if t.get("wikidata")), "")
        website = rt.get("website") or ""
        description = rt.get("description") or next((t.get("description") for t in wt if t.get("description")), "")

        # Dedupe: same name, overlapping area
        key = norm_name(c["name"])
        dup = False
        for (n2, b2) in seen:
            if n2 == key and not (max_lat < b2[0] or min_lat > b2[1] or max_lon < b2[2] or min_lon > b2[3]):
                dup = True
                break
        if dup:
            continue
        seen.append((key, (min_lat, max_lat, min_lon, max_lon)))

        score = (math.log1p(trip / 1000.0) + 0.6 * len(feats) + (1.5 if c["kind"] == "route" else 0)
                 + (2.0 if wikipedia or wikidata else 0) + (0.5 if park else 0) + (0.4 if parking else 0)
                 + (0.3 if rtype == "loop" else 0)
                 + (1.2 + (0.6 if rtype == "loop" else 0) if c["kind"] == "suggested" else 0))

        pieces_out = [main] + c["rest"]
        poly = ";".join(encode_polyline(simplify(p)) for p in pieces_out if len(p) > 1)
        prof = ",".join(str(int(round(v))) for v in profile)

        display = " / ".join(x.strip() for x in c["name"].split(";") if x.strip())
        out_rows.append(dict(
            name=display, name_lc=norm_name(display), park=park, park_lc=norm_name(park),
            kind=c["kind"], th_lat=start[0], th_lon=start[1], c_lat=clat, c_lon=clon,
            min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
            length_m=round(c["total"], 1), trip_m=round(trip, 1), route_type=rtype,
            gain_m=round(trip_gain, 1), loss_m=round(trip_loss, 1),
            max_ele=round(max_ele, 1) if max_ele is not None else None,
            min_ele=round(min_ele, 1) if min_ele is not None else None,
            difficulty=diff, features=",".join(sorted(feats)), dogs=dogs, wheelchair=wheelchair,
            paved=paved, kid=kid, activities=",".join(sorted(acts)), sac=sac,
            parking=parking, peaks=json.dumps(peaks[:5]) if peaks else "",
            near=json.dumps(sorted(set(near_names))[:6]) if near_names else "",
            wikipedia=wikipedia, wikidata=wikidata, website=website, description=description[:500],
            park_wikipedia=park_wp, park_wikidata=park_wd,
            osm=c["osm"], score=round(score, 3), via=json.dumps(c.get("via", [])) if c.get("via") else "",
            poly=poly, profile=prof, step_m=round(c["step"], 2),
        ))
    print(f"trails: {len(out_rows)}  ({time.time()-t0:.0f}s)", flush=True)

    # Parks with trails
    park_counts = defaultdict(int)
    for r in out_rows:
        if r["park"]:
            park_counts[r["park"]] += 1
    parks_out = []
    seen_parks = set()
    for name, kind, poly, wp, wd in rd.parks:
        if park_counts.get(name, 0) == 0 or name in seen_parks:
            continue
        seen_parks.add(name)
        minx, miny, maxx, maxy = poly.bounds
        c = poly.representative_point()
        parks_out.append((name, norm_name(name), kind, c.y, c.x, miny, minx, maxy, maxx, park_counts[name], wp, wd))

    write_db(args, out_rows, parks_out)
    print(f"done in {time.time()-t0:.0f}s", flush=True)


def write_db(args, rows, parks):
    if os.path.exists(args.output):
        os.remove(args.output)
    db = sqlite3.connect(args.output)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE trails (
            id INTEGER PRIMARY KEY, name TEXT, name_lc TEXT, park TEXT, park_lc TEXT, kind TEXT,
            th_lat REAL, th_lon REAL, c_lat REAL, c_lon REAL,
            min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
            length_m REAL, trip_m REAL, route_type TEXT, gain_m REAL, loss_m REAL, max_ele REAL, min_ele REAL,
            difficulty INTEGER, features TEXT, dogs TEXT, wheelchair INTEGER, paved INTEGER, kid INTEGER,
            activities TEXT, sac TEXT, parking INTEGER, peaks TEXT, near TEXT,
            wikipedia TEXT, wikidata TEXT, website TEXT, description TEXT,
            park_wikipedia TEXT, park_wikidata TEXT, osm TEXT, score REAL, via TEXT);
        CREATE TABLE trail_geom (id INTEGER PRIMARY KEY, poly TEXT, profile TEXT, step_m REAL);
        CREATE TABLE parks (id INTEGER PRIMARY KEY, name TEXT, name_lc TEXT, kind TEXT, c_lat REAL, c_lon REAL,
            min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL, trails INTEGER, wikipedia TEXT, wikidata TEXT);
    """)
    cols = [k for k in rows[0].keys() if k not in ("poly", "profile", "step_m")] if rows else []
    for i, r in enumerate(rows, start=1):
        db.execute(f"INSERT INTO trails (id,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))})",
                   [i] + [r[k] for k in cols])
        db.execute("INSERT INTO trail_geom VALUES (?,?,?,?)", (i, r["poly"], r["profile"], r["step_m"]))
    db.executemany("INSERT INTO parks (name,name_lc,kind,c_lat,c_lon,min_lat,min_lon,max_lat,max_lon,trails,wikipedia,wikidata) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", parks)
    db.executescript("""
        CREATE INDEX idx_trails_c ON trails (c_lat, c_lon);
        CREATE INDEX idx_trails_name ON trails (name_lc);
        CREATE INDEX idx_parks_name ON parks (name_lc);
    """)
    meta = {
        "schema": "1",
        "region_id": args.region_id,
        "region_name": args.region_name,
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "trails": str(len(rows)),
        "attribution": "Trail data © OpenStreetMap contributors (ODbL). Elevation: Mapzen / AWS Open Data terrain tiles.",
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--region-id", required=True)
    ap.add_argument("--region-name", required=True)
    ap.add_argument("--min-trail-m", type=float, default=400)
    ap.add_argument("--max-route-km", type=float, default=60)
    ap.add_argument("--terrain-cache", default="terrain-cache")
    ap.add_argument("--mock-terrain", action="store_true")
    ap.add_argument("--route-max-km", type=float, default=11, help="max one-way distance to a destination")
    ap.add_argument("--no-suggested", action="store_true")
    build(ap.parse_args())
