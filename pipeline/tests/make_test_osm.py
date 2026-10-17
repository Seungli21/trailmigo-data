"""Writes a small synthetic OSM file that exercises the pack builder."""
import sys
nodes, ways, rels = [], [], []
nid = [0]
def node(lat, lon, **tags):
    nid[0] += 1
    nodes.append((nid[0], lat, lon, tags))
    return nid[0]
def line(points):
    return [node(lat, lon) for lat, lon in points]
wid = [0]
def way(nds, **tags):
    wid[0] += 1
    ways.append((wid[0], nds, tags))
    return wid[0]

B = (34.10, -118.30)
# Park polygon (closed way) around everything
pk = line([(34.09, -118.31), (34.09, -118.27), (34.13, -118.27), (34.13, -118.31)])
way(pk + [pk[0]], leisure="park", name="Test Park")
# Lake polygon near the canyon trail
lk = line([(34.1205, -118.2955), (34.1205, -118.2935), (34.1220, -118.2935), (34.1220, -118.2955)])
way(lk + [lk[0]], natural="water", water="lake", name="Mirror Lake")

# Loop trail: 3 ways forming a ~2.4 km loop
a = line([(34.100, -118.300), (34.104, -118.300), (34.108, -118.300)])
b = [a[-1]] + line([(34.108, -118.296), (34.108, -118.292)])
c = [b[-1]] + line([(34.104, -118.292), (34.100, -118.292), (34.100, -118.296)]) + [a[0]]
for nds in (a, b, c):
    way(nds, highway="path", name="Loop Trail", surface="dirt", dog="leashed")
# Peak on the loop
node(34.108, -118.2965, natural="peak", name="Test Peak", ele="500")

# Out & back canyon trail with a waterfall, ending near the lake
d = line([(34.112, -118.300), (34.115, -118.298), (34.118, -118.296)])
e = [d[-1]] + line([(34.1195, -118.295), (34.1210, -118.2945)])
way(d, highway="path", name="Canyon Trail", sac_scale="mountain_hiking")
way(e, highway="path", name="Canyon Trail")
node(34.1182, -118.2958, natural="waterfall", name="Hidden Falls")
way([d[0], a[-1]], highway="path")  # connector from the canyon trailhead to the loop
node(34.1121, -118.3003, amenity="parking")

# Route relation over two unnamed ways
r1 = line([(34.095, -118.285), (34.098, -118.282), (34.101, -118.279)])
r2 = [r1[-1]] + line([(34.104, -118.276), (34.107, -118.273)])
w1 = way(r1, highway="path")
w2 = way(r2, highway="track")
rels.append((1, [w1, w2], dict(type="route", route="hiking", name="Ridge Route", wikipedia="en:Griffith Park")))

# Sidewalk with a name: must be excluded
s = line([(34.090, -118.300), (34.095, -118.300)])
way(s, highway="footway", footway="sidewalk", name="Sidewalk Path")
# Too short
t = line([(34.091, -118.290), (34.0915, -118.290)])
way(t, highway="path", name="Tiny Trail")
# Two separate "West Trail"s far apart
w = line([(34.125, -118.309), (34.129, -118.309)])
way(w, highway="path", name="West Trail")
w = line([(34.200, -118.400), (34.205, -118.400)])
way(w, highway="path", name="West Trail")

out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
esc = lambda v: str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
for i, lat, lon, tags in nodes:
    if tags:
        out.append(f'<node id="{i}" version="1" lat="{lat}" lon="{lon}">' + "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items()) + '</node>')
    else:
        out.append(f'<node id="{i}" version="1" lat="{lat}" lon="{lon}"/>')
for i, nds, tags in ways:
    out.append(f'<way id="{i}" version="1">' + "".join(f'<nd ref="{n}"/>' for n in nds) + "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items()) + '</way>')
for i, members, tags in rels:
    out.append(f'<relation id="{i}" version="1">' + "".join(f'<member type="way" ref="{m}" role=""/>' for m in members) + "".join(f'<tag k="{k}" v="{esc(v)}"/>' for k, v in tags.items()) + '</relation>')
out.append('</osm>')
open(sys.argv[1], "w").write("\n".join(out))
