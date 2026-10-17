# Trailmigo trail data

Pre-built trail databases ("packs") for the [Trailmigo](https://github.com/Seungli21) hiking app.
Each pack is a SQLite file listing every named trail and hiking route in a region, with
length, route type, elevation gain and profile, difficulty, nearby features, park and trailhead info,
so the app can search and filter trails instantly and offline.

Packs are built by GitHub Actions (`.github/workflows/build-packs.yml`) from OpenStreetMap
extracts and published to the `packs` release, refreshed monthly. Add a region in `pipeline/regions.json`.

**Data licenses**
- Trail data © OpenStreetMap contributors, available under the Open Database License (ODbL).
- Elevation: Mapzen / AWS Open Data terrain tiles.

The pack files are derived databases of OpenStreetMap and are themselves under the ODbL.
