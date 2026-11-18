---
name: indiana-field-explorer
description: Interactive Johnson County, Indiana field explorer with per-pixel Sentinel-2 NDVI (10 m, 2020-2026), field-average NDVI trends, SSURGO soil map-unit polygons, and USDA CDL crop history. Use when someone needs a browser map of every agricultural field in the county.
license: MIT
compatibility: Requires Python 3.11+ with geopandas, numpy, pandas, pyarrow, rasterio, requests. Reads a completed indiana-field-discovery runtime; imagery and soil geometry are fetched only for the requested period.
metadata:
  author: Boreal Bytes
  version: "1.2.0"
  category: geospatial
  tags: [indiana, johnson-county, explorer, ndvi, sentinel-2, ssurgo, cdl, leaflet]
---

# Indiana Field Explorer

_A browser map of every agricultural field in Johnson County, Indiana, with
10 m per-pixel Sentinel-2 NDVI, SSURGO soil map units, and crop history._

The explorer is a read-only reporting app on top of the
[county field discovery](../indiana-field-discovery/GUIDE.md) output. Boundaries,
SSURGO summaries, CDL crop history, and the daily NDVI parquet all come from the
existing `in-johnson/countywide` runtime farm. Nothing is downloaded in full:
imagery is read as remote COG windows for one biweekly window at a time.

---

## What it shows

- Every field boundary in the county (3,787 polygons; 3,293 classified as crop).
- **Selection-driven field view.** Click a field (or its outline) to highlight it,
  zoom to it, and load only that field's overlays. Click another field to switch,
  or press `Esc` / the ✕ to clear.
- **Per-pixel NDVI at native 10 m resolution** for the selected biweekly period
  and **selected field only**. Each pixel is the mean of clear observations in
  that window, so within-field variability is preserved instead of one average
  color per field. Pixels outside the field stay transparent.
- Year selection for 2020–2026 and a biweekly timeline slider with play/pause.
- Adjustable NDVI color ramp and stretch: fixed −0.2…1.0, manual bounds, and a
  **field-relative 2–98 %** stretch computed from the selected field.
- An NDVI legend that prints the numeric values of the active scale.
- An independent **NDVI toggle** and **SSURGO toggle**. Both are disabled until a
  field is selected and report loading, hidden, unavailable, and error states.
- A toggleable **SSURGO soil map-unit layer** showing real MUPOLYGON boundaries
  clipped to the selected field; click a polygon for its symbol, name, drainage
  class, hydrologic group, slope, available water capacity, water table, and
  flood class without changing the selected field.
- Click a field for acreage, soil summary, crop history, pixel statistics
  (min/max/mean/std), and a multi-year field-average NDVI trend chart.
- No cloud statistics and no cloud layers: masking happens before compositing.

The map never colors a whole field with one value. Field-average NDVI is used
only by the trend chart; its biweekly value is the valid-pixel-weighted mean of
daily field means over dates with at least 50 % valid coverage.

---

## Environment

Use the same interpreter that runs the discovery pipeline:

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
export PYTHONPATH=/path/to/repo/my-farm-advisor/field-management/indiana-field-explorer/src
```

The app uses only the Python standard library for serving; the packages above are
already required by the discovery pipeline.

---

## Run

```bash
cd /path/to/repo/my-farm-advisor/field-management/indiana-field-explorer

PYTHONPATH=src "$INDIANA_RUNTIME_ROOT/venv-indiana/bin/python" \
  -m indiana_field_explorer serve --port 8420
```

Open `http://127.0.0.1:8420/`. Startup loads the daily NDVI parquet (about ten
seconds) and writes a simplified field GeoJSON under
`$INDIANA_RUNTIME_ROOT/state/explorer/`.

### Coder app URL

When the workspace is a Coder workspace, the same port is reachable as an app:

```
https://8420--main--indiana--crashcoleman22.dollarsperacrecropsolutions.app
```

The app subdomain pattern is `<port>--<workspace>--<owner>.<deployment-domain>`,
not under `coder.`. Unauthenticated requests return a 303 to the Coder login
flow; open the URL in a browser with a Coder session to see the app.

---

## 10 m NDVI raster composites

The county composite is still built for the selected `(year, period)`, but the map
no longer drapes the whole county raster. On selection the browser calls
`/api/field-ndvi` for one field; the server crops the cached composite, warps it
to EPSG:3857, masks it to the field polygon, and serves a small PNG. If the
composite is not cached, the **Compute 10 m NDVI for this period** button starts a
background job that:

1. searches Sentinel-2 L2A STAC items for the 14-day window,
2. keeps one best item per acquisition date,
3. reads `red`, `nir`, and `scl` COG windows on the shared 10 m field-index grid,
4. masks SCL classes `{0,1,3,8,9,10,11}` (nodata, saturated, shadow, cloud,
   cirrus, snow) and invalid reflectance,
5. averages every clear observation per pixel.

The county result is cached as a compressed GeoTIFF plus a grayscale+alpha PNG:

```
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_raster/<year>/P<nn>.tif
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_raster/<year>/P<nn>.png
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_raster/<year>/P<nn>.json
```

Per-field display crops are cached separately and are derived from the GeoTIFF,
never re-downloaded:

```
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_field/<year>/P<nn>/<field_id>.png
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_field/<year>/P<nn>/<field_id>.json
```

The crop uses `10 / cos(latitude)` metres so the ground sample distance stays at
10 m while the image grid is axis-aligned in Web Mercator. Nearest resampling is
deliberate: no interpolated NDVI. Pixels outside the field are transparent. This
replaces an earlier countywide image overlay that was misaligned by up to ~500 m
because a UTM grid is rotated relative to Web Mercator.

Both PNGs store NDVI over the fixed encoding domain −0.2…1.0 in the gray channel
and validity in alpha. The browser decodes the field PNG once and recolors a
canvas locally, so changing ramp or stretch never refetches imagery and never
invents values. The CSS uses `image-rendering: pixelated` so 10 m cells stay
crisp when zoomed to a field.

Pre-compute periods ahead of time:

```bash
PYTHONPATH=src python -m indiana_field_explorer raster --year 2025 --periods 1-15
```

Each period takes roughly 40–90 s depending on how many dates the window has.
Computation is idempotent; an existing cache is reused.

### Field pixel statistics

`/api/field-ndvi` also returns the **exact** statistics from the UTM mask (the
same numbers as `/api/field-raster-stats`), while the displayed Web Mercator crop
reports its own `crop_stats` only as a sanity check. Example (2025 P10, Johnson
County):

```
field 181623000957517, 486 ac
pixels 19,697 · min 0.064 · max 0.940 · mean 0.881 · std 0.074
display crop 122 × 206 px, ~13 KB PNG, 19,765 valid cells
```

The pixel count equals the field's pixel count in the shared field-index raster,
which is how alignment with the field boundaries is verified.

---

## SSURGO soil polygons

The discovery pipeline stores tabular map-unit membership only. For the map
layer, `soils_layer.py` fetches the matching `MUPOLYGON` features for the
county's map units from the IndianaMap hosted SSURGO service (NRCS data), clips
them to the field footprints, simplifies them, and caches:

```
$INDIANA_RUNTIME_ROOT/state/explorer/soil_map_units.geojson
$INDIANA_RUNTIME_ROOT/state/explorer/soil_map_units.provenance.json
```

Rebuild the cache at any time:

```bash
PYTHONPATH=src python -m indiana_field_explorer soils
```

The browser never downloads the ~21 MB county GeoJSON. `/api/soils.geojson`
without a `field_id` still serves it for tooling, but the map calls
`/api/soils.geojson?field_id=...`. The server loads the county cache once into a
GeoDataFrame with a spatial index, clips to the selected field, and caches:

```
$INDIANA_RUNTIME_ROOT/state/explorer/soil_by_field/<field_id>.geojson
```

A typical field returns 5–60 polygons instead of 11,340. Polygons are
non-interactive and drawn in their own pane above the NDVI crop and below field
outlines, so field outlines remain visible and clickable. Clicking inside the
selected field opens a soil attribute popup without changing the selection.

The layer carries `mukey`, `musym`, `muname`, `drclassdcd`, `hydgrpdcd`,
`slopegradwta`, `aws050wta`, `wtdepannmin`, and `flodfreqdcd`. Component-weighted
topsoil attributes remain available per field through `/api/field/<id>`.

New soil cache builds simplify at 0.5 m and keep polygons down to 5 m² so small
map-unit slivers are not silently dropped. Existing caches built with the older
2 m / 40 m² settings remain valid; rebuild explicitly if those slivers matter.
Per-field clipping removes any area outside the field regardless of cache age.

---

## 2020 field-average on demand

The daily parquet starts in 2021, so the 2020 **chart series** is computed per
biweekly period from Sentinel-2 STAC items using the discovery pipeline's
tile/date machinery. Only the requested period is read, and results are cached:

```
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_biweekly/2020/P01.parquet … P15.parquet
```

```bash
PYTHONPATH=src python -m indiana_field_explorer warmup --periods 1-15
PYTHONPATH=src python -m indiana_field_explorer compute 7
```

This cache feeds only the field-average trend chart; the map raster is built by
the `raster` command above. A period with no usable acquisition stays blank in
both views.

---

## Data contract

| Item | Location |
| --- | --- |
| Boundaries | `<farm>/boundary/field_boundaries.parquet` |
| Soils summary | `<farm>/manifests/field_soil_summary.csv` |
| Soil map units | `<farm>/manifests/field_soil_map_units.csv` |
| Crop history | `<farm>/manifests/field_crop_history.csv` |
| County stats | `<farm>/manifests/county_field_stats.json` |
| Daily NDVI | `<farm>/derived/ndvi/field_ndvi_daily.parquet` |
| 2020 chart cache | `state/explorer/ndvi_biweekly/2020/P*.parquet` |
| NDVI raster cache | `state/explorer/ndvi_raster/<year>/P*` |
| Per-field NDVI crops | `state/explorer/ndvi_field/<year>/P<nn>/<field_id>.*` |
| Soil polygons | `state/explorer/soil_map_units.geojson` |
| Per-field soil clips | `state/explorer/soil_by_field/<field_id>.geojson` |
| Simplified fields | `state/explorer/fields_simplified.geojson` |
| Field index grid | `state/explorer/field_index/` |

`<farm>` defaults to
`$INDIANA_RUNTIME_ROOT/data-pipeline/growers/in-johnson/farms/countywide`.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/meta` | County totals, years, and the 15 period definitions |
| `GET /api/fields.geojson` | Simplified boundaries with acreage and class |
| `GET /api/ndvi-raster?year=&period=` | County composite metadata, bounds, stats |
| `GET /api/ndvi-raster.png?year=&period=` | County gray8+alpha8 NDVI image (tooling) |
| `GET /api/field-ndvi?year=&period=&field_id=` | Per-field crop metadata, Web Mercator bounds, exact stats |
| `GET /api/field-ndvi.png?year=&period=&field_id=` | Per-field NDVI crop, masked to the field |
| `GET /api/field-raster-stats?year=&period=&field_id=` | Pixel min/max/mean/std |
| `GET /api/rasters` | Cached raster periods |
| `GET /api/soils.geojson` | County SSURGO map-unit polygons (tooling) |
| `GET /api/soils.geojson?field_id=` | SSURGO polygons clipped to one field |
| `GET /api/ndvi?year=&period=` | Field-average values (chart API) |
| `GET /api/field/<field_id>` | Acreage, soils, crop history, NDVI series |
| `GET /api/job` | Current compute job state |
| `POST /api/compute-raster/<year>/<period>` | Start a raster job |
| `POST /api/compute-2020/<period>` | Start a 2020 chart-series job |
| `GET /healthz` | Liveness |

---

## Limitations

- The map composite is the per-pixel mean of clear observations in the window;
  it is not a gap-filled surface. Pixels without a clear observation are
  transparent and are never interpolated.
- The PNG encoding clips display colors outside −0.2…1.0, but reported statistics
  and the cached GeoTIFF keep the actual values.
- The Web Mercator display crop is resampled with nearest-neighbour to ~13 m
  cells (10 m on the ground). Panel statistics always come from the exact UTM
  mask, so displayed min/max/mean/std do not change with the reprojection.
- SCL cloud masking is conservative; a period with few clear dates yields sparse
  coverage (for example 2021 P4–P5 in the field-average series).
- Soils are SSURGO map units from IndianaMap with NRCS attributes; tabular
  component-weighted topsoil summaries come from the discovery manifests.
  `FdbA` has no source polygon for the test field, and two county fields have no
  soil geometry at all; those show explicit unavailable states.
- CDL history runs 2021–2025; unreleased years are absent rather than guessed.
- The app is single-county and single-farm by design.
