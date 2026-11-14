# Johnson County, Indiana — completed field database

Status: **complete and validated** (built 13 Nov 2026, re-verified 14 Nov 2026).
This page records the finished Johnson County run: what it contains, how it was
produced, how to retrieve a single field, and what is still missing. The reusable
method lives in [GUIDE.md](GUIDE.md); this page is the concrete county-specific
record.

All generated data lives **outside Git** under `INDIANA_RUNTIME_ROOT`. The
repository contains only the pipeline code and documentation.

---

## 1. Snapshot

| Item | Value |
| --- | --- |
| County | Johnson, Indiana (FIPS 18081) |
| County area | 205,946.2 acres (EPSG:5070) |
| Field polygons | **3,787** |
| Total field acres (clipped) | **101,617.5** |
| Crop-classified fields | 3,293 (97,636.1 acres) |
| Agricultural non-crop | 402 (2,947.9 acres) |
| Non-agricultural | 92 (1,033.4 acres) |
| Fields with SSURGO map units | **3,787 / 3,787 (100 %)** |
| CDL years processed | 2021–2025 |
| NDVI seasons processed | **2021–2026 (6 seasons)** |
| Fields with NDVI | 3,291 of 3,293 crop fields |
| Farm output size | 60 MB |
| Runtime state | 39 MB |
| Python venv | 724 MB |
| Total runtime footprint | 822 MB |

Boundary vintage is USDA CSB via FIBOA (2016–2023 rev23), clipped to the TIGER
county boundary. 80 polygons were edge-clipped by the county line; 0 duplicate
CSB ids were removed; 42 polygons whose CSB administrative county differs from
the spatial county were flagged, not dropped.

---

## 2. Exact commands used

```bash
export INDIANA_RUNTIME_ROOT=/home/coder/workspace/indiana-runtime
export PYTHONPATH=/home/coder/workspace/my-farm-advisor-skills/my-farm-advisor/field-management/indiana-field-discovery/src
PY=/home/coder/workspace/indiana-runtime/venv-indiana/bin/python

# 1. boundaries (CSB / FIBOA, clipped to FIPS 18081)
$PY -m indiana_field_discovery county-fields \
  --state-fips 18 --county-fips 081 --grower in-johnson --farm countywide

# 2. soils (SSURGO survey area IN081, all touching map units)
$PY -m indiana_field_discovery soils \
  --survey-area IN081 --grower in-johnson --farm countywide

# 3. crop history (CDL 2021-2026; 2026 recorded as unreleased)
$PY -m indiana_field_discovery crop-history \
  --start-year 2021 --end-year 2026 --grower in-johnson --farm countywide

# 4. NDVI (pilot gate first, then county run, 2 workers)
$PY -m indiana_field_discovery ndvi \
  --start-year 2025 --end-year 2025 --pilot-fields 10 --workers 2 \
  --grower in-johnson --farm countywide
$PY -m indiana_field_discovery ndvi \
  --start-year 2021 --end-year 2026 --workers 2 \
  --grower in-johnson --farm countywide

# 5. monthly / annual / trend aggregation and maps
$PY -m indiana_field_discovery ndvi-summary \
  --grower in-johnson --farm countywide
```

Every stage is idempotent and resumable. Re-running a finished stage reuses
cached manifests and per-date row caches under `state/ndvi/`.

Runtime layout for this run:

```
/home/coder/workspace/indiana-runtime/
  data-pipeline/growers/in-johnson/farms/countywide/
    farm.json
    boundary/        field boundaries (parquet, geojson)
    manifests/       field, soil, crop tables + provenance
    derived/soil/    SSURGO horizon table
    derived/ndvi/    daily, monthly, annual, trend tables
    derived/reports/ report markdown + two PNG maps
    derived/pilot/   brute-force pilot check
  state/             resumability checkpoints (reusable across farms)
  venv-indiana/      runtime environment
```

---

## 3. Select and retrieve a single field

```python
import geopandas as gpd
from pathlib import Path

FARM = Path("/home/coder/workspace/indiana-runtime/data-pipeline"
            "/growers/in-johnson/farms/countywide")
fields = gpd.read_parquet(FARM / "boundary/field_boundaries.parquet")
```

**By id** (CSB id is the stable key used by every table):

```python
fid = "181623000956756"
one = fields[fields["field_id"] == fid]
print(one[["field_id", "crop_name", "ag_class",
           "area_acres_clipped", "admin_county_mismatch"]])
```

**By point, in the field's own CRS:**

```python
from shapely.geometry import Point
pt = gpd.GeoSeries([Point(-86.10, 39.55)], crs="EPSG:4326").to_crs(fields.crs)
hit = fields[fields.intersects(pt.iloc[0])]
```

**By crop or size:**

```python
corn_2023 = fields[(fields["crop_name"] == "Corn") & (fields["ag_class"] == "crop")]
large = fields[fields["area_acres_clipped"] >= 40]
```

**Export one field as GeoJSON or a shapefile:**

```python
one.to_file("/tmp/field_181623000956756.geojson", driver="GeoJSON")
```

Useful boundary columns: `field_id`, `field_slug`, `source_id`, `crop_code`,
`crop_name`, `crop_code_list`, `ag_class`, `csb_admin_county`,
`admin_county_mismatch`, `determination_datetime`, `geometry_hash`,
`area_m2_5070`, `area_acres_clipped`, `area_acres_original`,
`area_acres_utm16n`, `clip_ratio`, `edge_flag`.

The `boundary/field_boundaries_ag.parquet` subset contains only
`ag_class == "crop"` (3,293 rows) for workflows that should ignore non-crop land.

---

## 4. Access soil characteristics

Two tables, both keyed by `field_id`:

- `manifests/field_soil_summary.csv` — one row per field (3,787).
- `manifests/field_soil_map_units.csv` — one row per field × map unit (16,005).
- `derived/soil/ssurgo_horizons.parquet` — 935 component/horizon rows keyed by
  `mukey`, for depths and properties beyond the 0–30 cm summary.

```python
import pandas as pd
soil = pd.read_csv(FARM / "manifests/field_soil_summary.csv", dtype={"field_id": str})
units = pd.read_csv(FARM / "manifests/field_soil_map_units.csv", dtype={"field_id": str})
horizons = pd.read_parquet(FARM / "derived/soil/ssurgo_horizons.parquet")

soil[soil["field_id"] == fid].T           # dominant map unit + topsoil properties
units[units["field_id"] == fid]           # every touching map unit
horizons[horizons["mukey"].isin(units.loc[units.field_id == fid, "mukey"])]
```

Summary columns include `dominant_mukey`, `dominant_musym`, `dominant_muname`,
`dominant_comppct`, `n_map_units`, `mukey_list`, `musym_list`, and 0–30 cm
thickness-weighted means: `top_om_r` (organic matter %), `top_cec7_r` (CEC),
`top_awc_r` (available water capacity), `top_ph1to1h2o_r`, `top_claytotal_r`,
`top_sandtotal_r`, `top_silttotal_r`, `top_dbthirdbar_r`, plus `top_kwfact`,
`aws025wta`, `aws050wta`, `aws0100wta`, `slopegradwta`, `wtdepannmin`.

**Weighting and its limitation:** properties are thickness-weighted over 0–30 cm
within a component, component-percentage weighted across components inside a map
unit, then **equally** weighted across map units. The SDA tabular API returns map
unit membership without polygon area, so map-unit fractions are **not
area-weighted**. `mukey_list` is retained so a future pass can area-weight with
`mupolygon`. This is recorded in `weighting_method`, `area_weighted`, and
`limitation` on every row.

County medians (0–30 cm): OM 2.10 %, CEC 15.0, AWC 0.190, pH 6.48, clay 23.8 %.
Median map units per field is 4 (max 14); 88 unique map units cover the county.

---

## 5. Access crop history

```python
crop = pd.read_csv(FARM / "manifests/field_crop_history.csv", dtype={"field_id": str})
avail = pd.read_csv(FARM / "manifests/field_crop_history_availability.csv")

crop[crop["field_id"] == fid].sort_values("year")
```

Each row is a field × year majority CDL class with `crop_code`, `crop_name`,
`pixels`, `field_pixels`, `crop_pct`, `ag_class`, `source`, and `source_sha256`.
`crop_pct` shows how dominant the majority code was, which matters for fields
near the 30 m pixel limit.

Pivot to a rotation table:

```python
rotation = (crop.pivot(index="field_id", columns="year", values="crop_name")
                .rename_axis(None, axis=1))
```

County coverage:

| Year | Status | Fields matched | Corn | Soybeans | Other crop | Non-crop majority |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2021 | processed | 3,285 | 1,328 | 1,675 | 245 | 37 |
| 2022 | processed | 3,285 | 1,301 | 1,693 | 231 | 60 |
| 2023 | processed | 3,285 | 1,272 | 1,709 | 285 | 19 |
| 2024 | processed | 3,285 | 1,227 | 1,422 | 177 | 459 |
| 2025 | processed | 3,285 | 1,113 | 1,391 | 270 | 511 |
| 2026 | **unreleased** | — | — | — | — | — |

CDL 2026 has not been published. It is recorded as `unreleased` in
`field_crop_history_availability.csv` with the HTTP status; no value is imputed
or carried forward.

Eight crop fields (0.02–0.47 acres) contain no CDL pixel at 30 m and therefore
have no crop-history rows; they are smaller than one pixel.

---

## 6. Access NDVI results

| Table | Grain | Rows | Use |
| --- | --- | ---: | --- |
| `derived/ndvi/field_ndvi_daily.parquet` | field × date | 467,103 | point-in-time index |
| `derived/ndvi/field_ndvi_monthly.parquet` | field × year × month | 137,764 | monthly summaries |
| `derived/ndvi/field_ndvi_annual.parquet` | field × year | 19,746 | peaks, season means, slopes |
| `derived/ndvi/field_ndvi_trends.parquet` | field | 3,291 | six-year comparison |
| `derived/ndvi/yearly/year=YYYY.parquet` | field × date | 6 files | per-season partition |
| `derived/ndvi/county_ndvi_year_summary.csv` | year | 6 | county distribution |
| `derived/ndvi/county_ndvi_coverage.csv` | year | 6 | coverage audit |

```python
daily   = pd.read_parquet(FARM / "derived/ndvi/field_ndvi_daily.parquet")
monthly = pd.read_parquet(FARM / "derived/ndvi/field_ndvi_monthly.parquet")
annual  = pd.read_parquet(FARM / "derived/ndvi/field_ndvi_annual.parquet")
trends  = pd.read_parquet(FARM / "derived/ndvi/field_ndvi_trends.parquet")

# one field, one season, daily curve
daily[(daily.field_id == fid) & (daily.year == 2025)] \
    .sort_values("date")[["date", "ndvi_median", "coverage_pct"]]

# monthly profile
monthly[(monthly.field_id == fid) & (monthly.year == 2025)] \
    .sort_values("month")

# peak and season summary
annual[(annual.field_id == fid) & (annual.year == 2025)]

# six-year trajectory
trends[trends.field_id == fid]
```

Daily rows carry `satellite`, `resolution_m`, `scene_id`, `scene_cloud_pct`,
`boa_offset_applied`, `boa_offset_subtracted`, `baseline`, `valid_px`,
`field_px`, and `coverage_pct` so any statistic can be filtered by quality.

Rules:

- Peak NDVI = maximum daily `ndvi_median` among dates with `coverage_pct >= 50`.
- Seasonal slope = OLS over day-of-year within the season, same coverage gate.
- Percentiles (`ndvi_p10`, `ndvi_p90`) come from a 400-bin per-field histogram
  (≈0.005 NDVI accuracy). Mean and standard deviation are exact.
- Earth Search / sentinel-cogs assets are already BOA-offset corrected, so no
  1000 DN subtraction is applied; the STAC flag and baseline are recorded per row.

County coverage by season:

| Year | Dates | Fields | Field-date rows | Median coverage | Median peak | Median peak date |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2021 | 34 | 3,291 | 74,816 | 100 % | 0.876 | 2021-07-26 |
| 2022 | 35 | 3,291 | 70,839 | 100 % | 0.897 | 2022-08-25 |
| 2023 | 30 | 3,291 | 58,059 | 100 % | 0.901 | 2023-08-30 |
| 2024 | 36 | 3,291 | 68,129 | 100 % | 0.897 | 2024-07-30 |
| 2025 | 44 | 3,291 | 102,082 | 100 % | 0.901 | 2025-08-24 |
| 2026 | 39 | 3,291 | 93,178 | 100 % | 0.898 | 2026-08-04 |

276 tile-dates were requested and 218 contained usable imagery; 0 processing
errors. Three thousand two hundred ninety-one of 3,293 crop fields are observed;
the two exceptions are 0.02–0.03 acre slivers smaller than a single 10 m pixel.

---

## 7. Expand to another Indiana county

1. Pick the FIPS pair and the SSURGO survey area:

   ```sql
   -- NRCS Soil Data Access
   SELECT areasymbol, areaname, saverest
   FROM sacatalog
   WHERE areasymbol LIKE 'IN%';
   ```

   Indiana county survey areas follow `IN081` for Johnson; a county may be
   covered by more than one survey area, in which case run `soils` once per area
   or extend `--survey-area` handling.

2. Choose new grower/farm slugs and run the five stages in order with
   `--state-fips`, `--county-fips`, `--survey-area`, `--grower`, `--farm`.
   Boundaries and NDVI use the field geometries, so no other county-specific
   input is required.

3. Keep the NDVI pilot gate (`--pilot-fields 10`) and 2 workers. A new county
   reuses the cached STAC manifests but rasterizes its own field-index raster.

4. Re-run the validation gates in [GUIDE.md](GUIDE.md#validation-gates) before
   publishing. Existing counties are never overwritten by a different county
   because each farm directory is separate.

Optional: to cover agricultural non-crop polygons with NDVI and CDL, pass
`--all-fields` to `ndvi`; this build deliberately limits NDVI to
`ag_class == "crop"`.

---

## 8. Validation performed

Data checks (re-run 14 Nov 2026, all read-only):

- 3,787 polygons, unique `field_id`, 3,293 / 402 / 92 class split.
- Acreage sums 101,617.5 total and 97,636.1 crop; recomputed geometry area
  agrees with the stored column to 5 × 10⁻⁵ acre; EPSG:5070 confirmed; UTM 16N
  cross-check maximum difference 0.33 acre on the largest polygon.
- All geometries valid and non-empty.
- Soil: 3,787 / 3,787 fields have at least one map unit; 16,005 pairs match the
  sum of `n_map_units`; 88 unique map units; field ids align exactly with the
  boundary table.
- Crop: 5 years × 3,285 fields = 16,425 rows; 2026 = `unreleased`.
- NDVI: 467,103 daily rows, 218 dates, 3,291 fields, six yearly partitions that
  match `ndvi_summary.json`, 19,746 annual rows (3,291 × 6), 3,291 six-year
  trend rows, no duplicate field-date rows, coverage within 0–100 %.
- Pilot: 10-field brute-force agreement — mean/std within 5 × 10⁻⁶, percentiles
  within one histogram bin.
- Repository suite: `./scripts/validate.sh` → 44 pass, 0 warn, 0 fail.

---

## 9. Known limitations and missing data

- **CDL 2026 is unpublished** — recorded as `unreleased`, never imputed.
- **8 crop fields** (0.02–0.47 acres) have no CDL pixel at 30 m.
- **2 crop fields** (0.02–0.03 acres) are smaller than one 10 m Sentinel-2 pixel
  and have no NDVI rows.
- **SSURGO map-unit fractions are not area-weighted** (tabular SDA membership
  only); `mukey_list` is retained for a future `mupolygon` pass.
- **NDVI and CDL cover crop-classified fields only.** The 402 ag-non-crop and 92
  non-ag polygons have boundaries and soils but no vegetation or crop history in
  this build.
- **Percentiles are histogram-based** (400 bins, ≈0.005 accuracy).
- **SCL masking is not perfect**; thin cirrus and cloud edges can bias individual
  dates low.
- **NDVI is a canopy greenness index**, not biomass, yield, or crop condition.
- **FIBOA boundaries are a dissolved 2016–2023 vintage**; historical crop comes
  from CDL, not the boundary file.
- No Landsat, no additional counties, and no irrigation or yield layers are part
  of this build.

---

## 10. Where the data lives

Generated datasets, imagery caches, the venv, and temporary rasters are **never
committed**. They live under `INDIANA_RUNTIME_ROOT`
(`/home/coder/workspace/indiana-runtime`) and are excluded from Git; only this
documentation and the pipeline source are tracked.

Reports produced by the run:

```
derived/reports/johnson_in_field_database_report.md
derived/reports/johnson_in_field_map.png
derived/reports/johnson_in_ndvi_peak_map.png
```
