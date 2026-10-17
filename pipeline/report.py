"""Prints a quick quality report for a pack (used in the build logs)."""
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
q = lambda s, *a: db.execute(s, a).fetchall()
print("trails:", q("select count(*) from trails")[0][0], " parks:", q("select count(*) from parks")[0][0])
print("by kind:", q("select kind, count(*) from trails group by kind"))
print("by route type:", q("select route_type, count(*) from trails group by route_type"))
print("by difficulty:", q("select difficulty, count(*) from trails group by difficulty"))
print("missing elevation:", q("select count(*) from trails where max_ele is null")[0][0])
for f in ["waterfall", "peak", "views", "lake", "river", "beach", "hot_spring", "cave", "historic"]:
    print(f"  feature {f}:", q("select count(*) from trails where features like ?", f"%{f}%")[0][0])
print("with wikipedia/wikidata:", q("select count(*) from trails where wikipedia<>'' or wikidata<>''")[0][0])
print("\ntop 25 by score:")
for r in q("select name, park, route_type, round(trip_m/1609.344,1), round(gain_m*3.28084), difficulty, features from trails order by score desc limit 25"):
    print("  ", r)
print("\nsuggested routes, top 25:")
for r in q("select name, park, route_type, round(trip_m/1609.344,1), round(gain_m*3.28084), difficulty, via from trails where kind='suggested' order by score desc limit 25"):
    print("  ", r)
print("\nspot checks:")
for name in ["sandstone", "mishe mokwa", "switzer", "sturtevant", "eaton canyon falls", "mount hollywood", "bridge to nowhere", "mount baldy", "cucamonga", "eaton canyon", "sandstone peak",
             "potato chip", "torrey pines", "san jacinto", "cactus to clouds", "runyon", "echo mountain", "mount wilson"]:
    for r in q("select name, park, route_type, round(trip_m/1609.344,1), round(gain_m*3.28084), difficulty, features "
               "from trails where name_lc like ? order by score desc limit 3", f"%{name}%"):
        print("  ", r)
print("\nlargest parks:", q("select name, trails from parks order by trails desc limit 15"))
