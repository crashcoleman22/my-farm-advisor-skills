# Field Explorer — CDL, SSURGO, and Sentinel-2 NDVI

One interactive map for all 30 demo fields:

| State | County | Grower | Fields | Acres |
|---|---|---|---:|---:|
| IL | Iroquois | `illinois-grower` | 10 | 1,521 |
| IA | Kossuth | `iowa-grower` | 10 | 1,363 |
| NE | Phelps | `nebraska-grower` | 10 | 1,837 |

The page combines the Assignment 1 field boundaries with four layers:

1. **Field boundaries** — grower outline colour, field tooltips, selection outline.
2. **CDL crop fill** — dominant USDA CDL crop for 2021–2025, with year selector.
3. **SSURGO soils** — USDA NRCS map-unit polygons clipped to each field.
4. **Sentinel-2 NDVI** — observed 10 m cloud-masked pixels per scene, 2021–2025.

The right-hand inspector shows acreage, grower, farm, field ID, county, CDL crop
history, NASA POWER weather summaries, SSURGO map units, and the field-average
NDVI time series with a year/scene selector.

This is **not** the Assignment 1 grower map. That generator stays unchanged and
still embeds geometry only. This explorer adds soil polygons and NDVI PNG
overlays as **sibling files**; rasters are never embedded in the HTML.

## Requirements

- A populated runtime at `${DATA_PIPELINE_DATA_ROOT}/data-pipeline` with field
  boundaries, SSURGO GeoJSON, CDL tables, weather CSVs, and Sentinel-2 scenes.
- The runtime virtualenv, because the generator uses shapely, pyproj, numpy,
  rasterio, and Pillow:
  `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python`.
- Network access on the **build** machine for the first SSURGO attribute fetch
  (USDA Soil Data Access). After the first build, `soil_attributes.json` is
  cached and `--offline` works.
- Network access in the **browser** for Leaflet (`unpkg.com`) and Esri basemap
  tiles (`server.arcgisonline.com`). The map states this in the About section.

No API keys or paid services are required. USDA SDA, USDA CDL (already in the
runtime), NASA POWER (already summarized in the runtime), Microsoft Planetary
Computer Sentinel-2 (already downloaded), and Esri World Imagery are all used
without credentials.

## Build

Full 30-field build:

```bash
cd /path/to/assignment-2
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  my-farm-advisor/data-pipeline/web-maps/field-explorer/generate.py \
  --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
  --root "${DATA_PIPELINE_DATA_ROOT}" \
  --title "Three-state field explorer: Iroquois IL, Kossuth IA, Phelps NE"
```

Three-field smoke build (one per state) to a separate directory:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  my-farm-advisor/data-pipeline/web-maps/field-explorer/generate.py \
  --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
  --limit-per-state 1 \
  --root "${DATA_PIPELINE_DATA_ROOT}" \
  --out "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/web-maps/field-explorer-validation"
```

Useful options:

| Option | Effect |
|---|---|
| `--grower <slug>` | Repeatable. Omit to discover every grower with field polygons. |
| `--limit-per-state N` | Keep the first N fields per state for smoke runs. |
| `--start-year` / `--end-year` | CDL and NDVI year window (default 2021–2025). |
| `--out <dir>` | Override the output directory. |
| `--offline` | Do not call USDA SDA; use the cached attributes if present. |
| `--refresh-soil-attributes` | Ignore the SDA cache. |
| `--force-ndvi` | Rewrite NDVI PNGs even when they exist. |
| `--skip-ndvi` | Build without NDVI assets. |
| `--min-valid-fraction` | Exclude scenes below this field-pixel valid share (default 0.05). |

## Outputs

Everything is written under the runtime tree, never the skill checkout:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/web-maps/field-explorer/
  index.html                 # page, inline CSS/JS and field metadata payload
  soil_map_units.geojson     # 605 clipped polygons, fetched by the page
  soil_attributes.json       # cached USDA SDA map-unit attributes
  ndvi_index.json            # per-field NDVI scene index (redundant copy)
  ndvi/<field>/<date>.png    # one transparent PNG overlay per usable scene
  manifest.json              # generation metadata, counts, storage, input hashes
```

Measured on the current 30-field runtime:

| Artifact | Size |
|---|---:|
| `index.html` | ~559 KB (572,036 bytes) |
| `soil_map_units.geojson` | ~689 KB (705,854 bytes) |
| 945 NDVI PNGs | ~1.0 MB (1,007,265 bytes) |
| `ndvi_index.json` | ~0.5 MB (533,808 bytes) |
| `soil_attributes.json` | ~23 KB (23,027 bytes) |
| Total on disk | ~5.7 MB |

The six artifacts total ~2.8 MB of actual bytes; filesystem block usage is ~5.7 MB
because each of the 945 small PNGs occupies a 4 KB block.

The generator enforces a 2 MB HTML ceiling and never inlines raster bytes.

## Data sources and methods

### Field boundaries and CDL

Boundaries come from the pipeline's `boundary/field_boundaries.geojson`. CDL
comes from `derived/tables/*_<year>_cdl.csv`. The map shows the dominant crop
and its pixel share, plus up to two secondary crops. Shares are percentages of
CDL pixels, **not** planted acres. A field is tagged `mixed` when its top crop
is below 60% of classified pixels.

### Weather

The preferred source is
`shared/eda/field-level/tables/weather_seasonal_metrics.csv`. If it is missing,
the generator falls back to per-field `weather/daily_weather.csv` through
`lib/eda_metrics.weather_season_metrics`. The inspector shows growing-season
precipitation, GDD, heat days ≥35 °C, frost-free days, maximum dry spell, and
mean temperature.

NASA POWER is gridded. Fields whose five-year vectors are identical share one
grid cell and the inspector says so. Current grouping: IL 7+3, IA 9+1, NE 10.
The Assignment 2 EDA text's claim that every field in a farm has its own cell is
not accurate; the explorer reports the actual grouping.

### SSURGO

Polygons are the pipeline's `soil/ssurgo_soil_types.geojson`, already clipped to
each field. Disjoint polygons of the same `mukey` are merged into one inspector
row (acres and percent summed) but stay separate map polygons.

Attributes are fetched from USDA NRCS Soil Data Access:

- `mapunit` for `musym` and `muname`
- `muaggatt` for `slopegradwta` (weighted slope, %), `drclassdcd` (drainage),
  `hydgrpdcd` (hydrologic group), and `aws050wta` (available water, 0–50 cm)
- `component` + `cointerp` for NCCPI, rule
  `NCCPI - National Commodity Crop Productivity Index (Ver 3.0)`

NCCPI is the component-percentage-weighted average of `interphr` for components
whose `comppct_r > 0`. `interphrc` supplies the class text. This SDA schema has
no `nccpi` table, so the rating is computed rather than read from a summary
column. All 605 units currently resolve to a map-unit name and NCCPI; where a
component is unrated the inspector says so instead of showing a number.

Percent of field = unit acres ÷ boundary acres. Unit shares sum to 99.7–100.4%
because of clipping and rounding.

### Sentinel-2 NDVI

Scenes are Sentinel-2 L2A from Microsoft Planetary Computer, already downloaded
by the pipeline as per-field, EPSG:4326 NDVI GeoTIFFs. The explorer does not
download imagery; it reuses `satellite/sentinel/<year>/<scene>/…_ndvi.tif`.

- Resolution: 10 m.
- Cloud mask: SCL classes **3** (cloud shadow), **8/9/10** (cloud), **11**
  (snow), matching `lib/satellite_imagery.SENTINEL_CLOUD_CLASSES`.
- Valid pixels: finite NDVI inside the field polygon. Coverage is measured
  against **field pixels**, not the raster bounding box, so a field that fills
  half its rectangle is not reported as half cloud-covered.
- Scenes with `< 5%` valid field pixels are excluded.
- Scenes with `< 35%` valid field pixels are kept but flagged "low coverage";
  transparent pixels are excluded from the average.
- Current build: **945 usable scenes**, **26 excluded**, **13 kept with low
  coverage**. Per-year counts: 2021 = 221, 2022 = 166, 2023 = 163, 2024 = 191,
  2025 = 204.
- Acquisition dates present: 2021-03-01 through 2025-11-16.
- Scene cloud cover shown in the inspector is the provider's **tile-level** STAC
  `eo:cloud_cover`; the valid-pixel share is the field-level measure.
- Missing months per field/year are listed in the inspector's About section.

Each usable scene becomes a PNG overlay using a discrete 10-class brown→green
palette with transparent nodata. The map does **not** interpolate between dates
and does not fill clouds. The overlay is only the observed pixels of the selected
scene.

### Raster versus average

The page enforces the distinction in three places:

- Map overlay legend: "Observed NDVI pixels — cloud-masked Sentinel-2, 10 m."
- Chart caption: "Field average NDVI per scene: line is the mean of valid
  pixels, band is p10–p90. This is a summary, not an image."
- A callout in the NDVI section repeats that averages are never drawn as
  imagery.

No averages, composites, or interpolated surfaces are rendered as map rasters.

## Using the map

- **Layers**: Field boundaries, CDL crop fill, SSURGO soils, Sentinel-2 NDVI.
- **CDL year**: 2021–2025; changes fills, list labels, and legend.
- **NDVI opacity**: 30–100% (default 85%).
- **Fields**: search by field, farm, grower, county, or ID; filter by state;
  click a row to select and zoom.
- **Inspector**: CDL, weather, soils, NDVI, and a collapsible About section.
  Click a soil row to zoom to that map unit.
- **NDVI selector**: year and scene; default scene is the peak-greenness scene
  of the latest year with usable data.
- **Legend**: 10 NDVI classes plus the crop classes for the selected CDL year.

When soils or NDVI are on, the CDL fill drops to a light tint so the thematic
layer is visible. Toggle the layers to compare.

## Serving the map

The page must be served over HTTP (the soil GeoJSON is fetched, not inlined):

```bash
python3 -m http.server 8771 --bind 127.0.0.1 \
  --directory "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/web-maps"
```

Then open:

```text
http://127.0.0.1:8771/field-explorer/index.html
```

In a Coder workspace, forward port **8771** in the workspace's Ports panel (or
share it with your team) and open:

```text
https://coder.dollarsperacrecropsolutions.app/@crashcoleman22/dollarspera-workspace-2026-11-04/ports/8771/field-explorer/index.html
```

Port-sharing steps:

1. Start the server with the command above; keep it bound to `127.0.0.1`.
2. In the Coder dashboard, open the workspace → **Ports** tab.
3. Find port `8771` (or add it), set visibility to **Workspace** for yourself
   or **Shared** for the team, and click **Open**.
4. The proxied URL follows the pattern
   `…/ports/8771/field-explorer/index.html`.
5. If the workspace restarts, restart the server; Coder keeps the port rule.

Do not bind to `0.0.0.0` just to share it; Coder proxies the loopback port.

## Tests

Offline, synthetic, no network and no real grower data:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" -m unittest \
  my-farm-advisor/data-pipeline/web-maps/field-explorer/tests/test_field_explorer.py
```

Coverage (27 tests):

- field selection per state and deterministic smoke limits
- NDVI statistics, field-pixel denominator, minimum-valid rejection, low-coverage
  flagging, transparent palette
- soil area/percent math, attributes, and disjoint-polygon merging
- CDL dominance, top crops, mixed flag, missing year
- weather row parsing and shared-cell detection
- SSURGO cache hit and offline degradation
- payload shape, state order, HTML rendering, and missing-data placeholders
- synthetic GeoTIFF → PNG scene build, bounds, exclusions, missing manifest

Browser smoke test (optional, requires Playwright and network):

```bash
node check.js
```

`tests/` does not include a browser runner; the Playwright script used during
delivery lives outside the repo. The manual checklist is: 30 field rows, state
filters, search, inspector sections, CDL year change, NDVI overlay and scene
change, soil layer path count, soil-row zoom, and layer toggles.

## Limitations

- CDL shares are pixel percentages, not planted acres.
- Weather is gridded NASA POWER, not on-farm sensors; several fields share a cell.
- SSURGO attributes require a one-time SDA call; without the cache and without
  network the inspector shows map units with "attributes unavailable" rather
  than invented values.
- NDVI uses L2A surface reflectance without topographic correction. Cloud
  shadows at the 10 m edge can remain, and residual haze lowers NDVI.
- No gap filling, compositing, or temporal interpolation. A missing month stays
  missing and is listed in the About section.
- Sentinel-2 scene availability is uneven across years and states (for example,
  fewer winter scenes); the map never draws data for a date that was not observed.
- The basemap and Leaflet require internet access; the map fails explicitly if
  Leaflet cannot load.
- The explorer is a static snapshot. Re-run the generator after boundaries, CDL,
  soils, weather, or Sentinel scenes change.
- `ndvi_index.json` duplicates the inline NDVI index for external consumers; it
  is convenient but not required by the page.

## Related

- Assignment 1 grower map: [`../README.md`](../README.md)
- Assignment 2 EDA report: `my-farm-advisor/eda/` and the committed report bundle
- Satellite helper: `my-farm-advisor/data-pipeline/src/scripts/lib/satellite_imagery.py`
- Soil ingest: `my-farm-advisor/data-pipeline/src/scripts/ingest/download_soil.py`
