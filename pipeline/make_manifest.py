"""Writes packs.json describing the packs built in this run (merging with the previous manifest)."""
import hashlib, json, os, sqlite3, sys, time
out_dir, base_url, old = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
manifest = {"version": 1, "packs": []}
if old and os.path.exists(old):
    try:
        manifest = json.load(open(old))
    except Exception:
        pass
regions = {r["id"]: r for r in json.load(open("pipeline/regions.json"))}
for rid, r in regions.items():
    db_path = os.path.join(out_dir, f"{rid}.db")
    gz_path = db_path + ".gz"
    if not os.path.exists(gz_path):
        continue
    db = sqlite3.connect(db_path)
    bbox = db.execute("select min(min_lat), min(min_lon), max(max_lat), max(max_lon) from trails").fetchone()
    trails = db.execute("select count(*) from trails").fetchone()[0]
    sha = hashlib.sha256(open(gz_path, "rb").read()).hexdigest()
    entry = {
        "id": rid, "name": r["name"], "url": f"{base_url}/{rid}.db.gz",
        "bytes": os.path.getsize(gz_path), "db_bytes": os.path.getsize(db_path), "sha256": sha,
        "built": time.strftime("%Y-%m-%d"), "trails": trails, "bbox": [round(v, 4) for v in bbox], "schema": 1,
    }
    manifest["packs"] = [p for p in manifest["packs"] if p["id"] != rid] + [entry]
json.dump(manifest, open(os.path.join(out_dir, "packs.json"), "w"), indent=2)
print(json.dumps(manifest, indent=2))
