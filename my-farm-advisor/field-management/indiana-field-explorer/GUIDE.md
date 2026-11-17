---
name: indiana-field-explorer
description: Interactive Johnson County, Indiana field explorer with biweekly Sentinel-2 NDVI (2020-2026), SSURGO soils, and USDA CDL crop history. Use when someone needs a browser map of every agricultural field in the county with per-field NDVI trends, served locally or through a Coder HTTPS app URL.
license: MIT
compatibility: Requires Python 3.11+ with geopandas, numpy, pandas, pyarrow, rasterio, requests. Reads a completed indiana-field-discovery runtime; no additional downloads beyond on-demand Sentinel-2 COG windows.
metadata:
  author: Boreal Bytes
  version: "1.0.0"
  category: geospatial
  tags: [indiana, johnson-county, explorer, ndvi, sentinel-2, ssurgo, cdl, leaflet]
---

# Indiana Field Explorer

_A browser map of every agricultural field in Johnson County, Indiana, with
biweekly Sentinel-2 NDVI, soils, and crop history._

The explorer is a read-only reporting app on top of the
[county field discovery](../indiana-field-discovery/GUIDE.md) output. It adds no
new ingest work: boundaries, SSURGO summaries, CDL crop history, and the daily
NDVI parquet all come from the existing `in-johnson/countywide` runtime farm.

---

## What it shows

- Every field boundary in the county (3,787 polygons; 3,293 classified as crop).
- A choropleth of field-average NDVI for 15 biweekly periods, April 1 – October 31.
- Year selection for 2020–2026 and a biweekly timeline slider with play/pause.
- Click a field for acreage, dominant SSURGO map unit and topsoil properties,
  CDL crop history, and a multi-year NDVI trend chart.
- No cloud statistics and no cloud layers: masking happens before aggregation and
  only the resulting field means are exposed.

Biweekly values are the valid-pixel-weighted mean of daily field means, using
only dates with at least 50 % valid coverage. A period with no usable imagery is
left empty (`null`) — never imputed or back-filled.

---

## Environment

Use the same interpreter that runs the discovery pipeline:

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
export PYTHONPATH=/path/to/repo/my-farm-advisor/field-management/indiana-field-explorer/src
```

The app uses only the Python standard library for serving and the packages above
for data access; nothing extra needs to be installed.

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

## 2020 on demand

The daily parquet starts in 2021, so 2020 is computed per biweekly period from
Sentinel-2 STAC items using the discovery pipeline's tile/date machinery. Only
the requested period is read, and results are cached:

```
$INDIANA_RUNTIME_ROOT/state/explorer/ndvi_biweekly/2020/P01.parquet … P15.parquet
```

In the app, select 2020, pick a period, and press **Compute this period**.
A job runs in the background and the UI polls `/api/job` for progress.

Pre-compute ahead of time (each period takes roughly 40–60 s):

```bash
PYTHONPATH=src python -m indiana_field_explorer warmup --periods 1-15
# or a single period
PYTHONPATH=src python -m indiana_field_explorer compute 7
```

Computation is idempotent: an existing cache file is reused and never
recomputed. If a period has no usable acquisitions, an empty parquet is cached
and the period stays blank in the UI.

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
| 2020 cache | `state/explorer/ndvi_biweekly/2020/P*.parquet` |
| Simplified polygons | `state/explorer/fields_simplified.geojson` |
| Field index raster | `state/explorer/field_index/` |

`<farm>` defaults to
`$INDIANA_RUNTIME_ROOT/data-pipeline/growers/in-johnson/farms/countywide`.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/meta` | County totals, years, and the 15 period definitions |
| `GET /api/fields.geojson` | Simplified boundaries with acreage and class |
| `GET /api/ndvi?year=&period=` | Field values, or `status: not_computed` |
| `GET /api/field/<field_id>` | Acreage, soils, crop history, NDVI series |
| `GET /api/job` | Current compute job state |
| `POST /api/compute-2020/<period>` | Start a background 2020 period job |
| `GET /healthz` | Liveness |

---

## Limitations

- The daily parquet is aggregated as-is; 2020 is the only on-demand year.
- Fields smaller than one 10 m pixel, or outside the crop classification, have no
  NDVI. Those cells stay `null`.
- Periods with fewer than 50 % valid pixels are dropped, so some fields show
  gaps even inside the growing season.
- Soils are SSURGO component-weighted topsoil (0–30 cm) summaries, not a
  laboratory analysis.
- CDL history runs 2021–2025; unreleased years are absent rather than guessed.
- The app is single-county and single-farm by design.
