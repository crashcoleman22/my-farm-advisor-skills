---
name: indiana-field-discovery
description: Build a countywide agricultural field database for any US county from USDA Crop Sequence Boundaries (FIBOA), NRCS SSURGO soils, USDA CDL crop history, and streamed Sentinel-2 NDVI. Use when a county needs a reproducible, county-scale field inventory with soils, crop rotation, and six seasons of vegetation index statistics without bulk imagery downloads.
license: MIT
compatibility: Requires Python 3.11+ with geopandas, shapely, pyproj, rasterio, pyarrow, fsspec, numpy, pandas. Network access to Source Cooperative, USDA SDA, NASS, and AWS Earth Search.
metadata:
  author: Boreal Bytes
  version: "1.0.0"
  category: geospatial
  tags: [county, field-discovery, csb, fiboa, ssurgo, cdl, sentinel-2, ndvi, indiana]
---

# County Field Discovery Workflow

_One county, one reproducible field database: boundaries, soils, crop history, NDVI._

A completed, verified run is documented in
[JOHNSON-COUNTY.md](JOHNSON-COUNTY.md): Johnson County, Indiana (FIPS 18081),
3,787 fields, 101,617.5 acres, soils on every field, CDL 2021–2025, and six
Sentinel-2 NDVI seasons. Use it as the reference output for a new county.

---

## What this workflow produces

1. A county-clipped CSB field inventory with original CSB ids and crop classifications.
2. Acreage in an equal-area CRS (EPSG:5070) with a local UTM cross-check.
3. Every SSURGO map unit that touches each field, plus component-weighted topsoil properties.
4. Annual CDL crop history per field, with unreleased years flagged rather than guessed.
5. Per-field, per-date Sentinel-2 NDVI statistics streamed from remote COGs, then aggregated to monthly, annual, seasonal-trend, and multi-year tables.

Generated data stays outside Git under `INDIANA_RUNTIME_ROOT` (default convention:
`<runtime>/data-pipeline/growers/<grower>/farms/<farm>/`). Nothing is downloaded in full:
only field-level statistics are stored, and statewide CDL rasters are deleted after use.

---

## Environment

Keep the tool environment separate from the repository's full installer so the
2 GB project budget is not consumed by unrelated dependencies:

```bash
python3 -m venv /path/to/venv-indiana
/path/to/venv-indiana/bin/pip install --no-cache-dir \
  numpy pandas pyarrow geopandas shapely pyproj rasterio requests matplotlib fsspec
```

Run every stage from the package `src/` directory with the runtime root exported:

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
export PYTHONPATH=$(pwd)
python -m indiana_field_discovery --help
```

---

## Stages

Stages are independent and resumable; each writes manifests next to the data and
keeps resumability checkpoints under `<runtime>/state/`.

### 1. `county-fields` — boundaries

```bash
python -m indiana_field_discovery county-fields --state-fips 18 --county-fips 081
```

- Reads the national FIBOA CSB GeoParquet with HTTP range requests, pruning row
  groups by bbox statistics, then clips each polygon to the county.
- Keeps `field_id` (original CSB id), `crop_code`, `crop_name`, `crop_code_list`,
  `determination_datetime`, `csb_admin_county`, and geometry.
- Classifies every polygon as `crop`, `ag_non_crop`, or `non_ag` from the CDL code
  and writes `county_field_exclusions.csv` for everything outside `crop`.
- Records `edge_intersections` (clipped polygons), `duplicates_removed`, and
  county-name/administrative mismatches instead of silently dropping rows.
- Outputs: `boundary/field_boundaries.parquet`, `boundary/field_boundaries_ag.parquet`,
  `boundary/field_boundaries.geojson`, `manifests/*.csv`, `manifests/county_field_stats.json`,
  and a static map under `derived/reports/`.

### 2. `soils` — SSURGO

```bash
python -m indiana_field_discovery soils --survey-area IN081
```

- Uses `SDA_Get_Mukey_from_intersection_with_WktWgs84`, so map units come from
  polygon intersection, never a centroid proxy.
- Retains **all** touching map units per field (`field_soil_map_units.csv`) and the
  full 0-30 cm horizon table (`derived/soil/ssurgo_horizons.parquet`).
- Property means are component-percentage weighted within a map unit and then
  equally weighted across map units.
- **Limitation (explicit in the outputs):** the tabular SDA API returns map unit
  membership without polygon area, so map unit fractions are **not area-weighted**.
  Map unit ids are retained so a future pass can area-weight with `mupolygon`.

### 3. `crop-history` — CDL

```bash
python -m indiana_field_discovery crop-history --start-year 2021 --end-year 2026
```

- Downloads the by-FIPS CDL GeoTIFF per year, takes the majority CDL code inside
  each field, then deletes the statewide raster immediately.
- A year whose file is not published is recorded as `unreleased` with the HTTP
  status; it is never filled forward from another year.

### 4. `ndvi` — Sentinel-2 (streamed)

```bash
# gate: small pilot first
python -m indiana_field_discovery ndvi --start-year 2025 --end-year 2025 \
  --pilot-fields 10 --workers 2
# county run
python -m indiana_field_discovery ndvi --start-year 2021 --end-year 2026 --workers 2
```

- Searches Earth Search once per season (Apr 1 - Oct 31), keeps the lowest-cloud
  item per acquisition date (newest processing baseline on ties), and caches the
  manifest under `state/ndvi/manifests/`.
- Rasterizes one 10 m field-index raster for the run, then processes imagery by
  `(tile, date)` — never per field — reading only the county window from remote
  COGs through `/vsicurl`. No scene is ever downloaded whole.
- Masks SCL classes `{0,1,3,8,9,10,11}` (nodata, saturated, shadow, cloud,
  cirrus, snow) and requires positive red and NIR reflectance. Earth Search /
  sentinel-cogs assets are already BOA-offset corrected (collection scale 0.0001,
  offset 0; red medians 288-430 DN on baselines 03.00-05.12), so no 1000 DN
  subtraction is applied; the STAC flag and baseline are recorded per row.
- Emits per field and date: `ndvi_mean`, `ndvi_median`, `ndvi_std`, `ndvi_p10`,
  `ndvi_p90`, `valid_px`, `field_px`, `coverage_pct`, `satellite`, `resolution_m`,
  `scene_id`, `scene_cloud_pct`, `boa_offset_applied`, `boa_offset_subtracted`,
  `baseline`.
- Percentiles come from a 400-bin per-field histogram, so they are accurate to
  about one bin (0.005 NDVI); mean and standard deviation are exact.
- Resume markers and per-date row caches make a restart safe and complete.

### 5. `ndvi-summary` — aggregation

```bash
python -m indiana_field_discovery ndvi-summary
```

- Monthly per-field summaries (weighted daily mean, spread, date count, coverage).
- Annual per-field peak NDVI and its date, season mean, early/mid/late means, and
  an in-season day-of-year slope. Peaks and slopes only use dates with
  `coverage_pct >= 50`.
- Multi-year per-field trend of peak and season-mean NDVI, plus a county
  year-by-year coverage and peak distribution table.
- A static peak-NDVI map under `derived/reports/`.

---

## Data contract

| Layer | File | Grain |
| --- | --- | --- |
| Boundaries | `boundary/field_boundaries.parquet` | one polygon per CSB id |
| Crop fields | `boundary/field_boundaries_ag.parquet` | `ag_class == "crop"` |
| Soils (all touches) | `manifests/field_soil_map_units.csv` | field x map unit |
| Soil summary | `manifests/field_soil_summary.csv` | field |
| Crop history | `manifests/field_crop_history.csv` | field x year |
| Crop availability | `manifests/field_crop_history_availability.csv` | year |
| Daily NDVI | `derived/ndvi/field_ndvi_daily.parquet` | field x date |
| Monthly NDVI | `derived/ndvi/field_ndvi_monthly.parquet` | field x year x month |
| Annual NDVI | `derived/ndvi/field_ndvi_annual.parquet` | field x year |
| NDVI trends | `derived/ndvi/field_ndvi_trends.parquet` | field |

All tables share `field_id`, which is the original CSB id, so a field can be
traced from boundary to soils to crop history to NDVI without a surrogate key.

---

## Selecting and retrieving individual fields

Load the boundary table once and filter it. Every other table is keyed by the
same `field_id`.

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
export FARM="$INDIANA_RUNTIME_ROOT/data-pipeline/growers/<grower>/farms/<farm>"
```

```python
import geopandas as gpd
import pandas as pd

fields = gpd.read_parquet(f"{FARM}/boundary/field_boundaries.parquet")

# by CSB id
fid = "181623000956756"
one = fields[fields["field_id"] == fid]

# by crop, class, or size
corn = fields[(fields["crop_name"] == "Corn") & (fields["ag_class"] == "crop")]
large = fields[fields["area_acres_clipped"] >= 40]

# by point (transform the point into the field CRS first)
from shapely.geometry import Point
pt = gpd.GeoSeries([Point(-86.10, 39.55)], crs="EPSG:4326").to_crs(fields.crs)
hit = fields[fields.intersects(pt.iloc[0])]

# export one field
one.to_file("/tmp/field.geojson", driver="GeoJSON")
```

`boundary/field_boundaries_ag.parquet` holds the `ag_class == "crop"` subset for
workflows that should ignore non-crop land.

Retrieve the attached records with the same id:

```python
soil   = pd.read_csv(f"{FARM}/manifests/field_soil_summary.csv", dtype={"field_id": str})
units  = pd.read_csv(f"{FARM}/manifests/field_soil_map_units.csv", dtype={"field_id": str})
crop   = pd.read_csv(f"{FARM}/manifests/field_crop_history.csv", dtype={"field_id": str})
daily  = pd.read_parquet(f"{FARM}/derived/ndvi/field_ndvi_daily.parquet")
annual = pd.read_parquet(f"{FARM}/derived/ndvi/field_ndvi_annual.parquet")

soil[soil.field_id == fid]                     # topsoil properties + dominant map unit
units[units.field_id == fid]                   # every touching map unit
crop[crop.field_id == fid].sort_values("year") # annual crop rotation
daily[(daily.field_id == fid) & (daily.year == 2025)].sort_values("date")
annual[annual.field_id == fid]                 # peaks and season means
```

See [JOHNSON-COUNTY.md](JOHNSON-COUNTY.md#3-select-and-retrieve-a-single-field)
for a complete worked example with real values.

---

## Adding another county

The pipeline is county-parameterised; no code change is needed.

1. Find the FIPS pair (`--state-fips`, `--county-fips`) and the SSURGO survey
   area symbol (`--survey-area`, e.g. `IN081` for Johnson County). The area list
   is available from Soil Data Access:

   ```sql
   SELECT areasymbol, areaname, saverest FROM sacatalog WHERE areasymbol LIKE 'IN%';
   ```

   A county covered by more than one survey area needs one `soils` run per area,
   or an extension to the loader.

2. Pick new `--grower` and `--farm` slugs so the new county writes to its own
   directory and never overwrites an existing one.

3. Run the stages in order, keeping the pilot gate and worker budget:

   ```bash
   python -m indiana_field_discovery county-fields --state-fips 18 --county-fips 063 --grower in-other --farm countywide
   python -m indiana_field_discovery soils --survey-area IN063 --grower in-other --farm countywide
   python -m indiana_field_discovery crop-history --grower in-other --farm countywide
   python -m indiana_field_discovery ndvi --pilot-fields 10 --workers 2 --grower in-other --farm countywide
   python -m indiana_field_discovery ndvi --workers 2 --grower in-other --farm countywide
   python -m indiana_field_discovery ndvi-summary --grower in-other --farm countywide
   ```

4. Re-run the validation gates below and record the result in a county page like
   [JOHNSON-COUNTY.md](JOHNSON-COUNTY.md).

Non-Indiana counties only change the FIPS and survey-area values; the data
sources are national. NDVI training/validation is not required because no model
is fitted — only per-pixel index statistics are computed.

---

## Validation gates

Run these before scaling up or publishing:

1. Boundary gate: field count, crop acreage, county acreage, exclusions, duplicate
   count, and edge-clipped count all reported; no silent row drops.
2. Soil gate: every field has at least one map unit, and `n_map_units` distribution
   is reported. A field with no soil is a failure, not a fill.
3. Crop gate: every available year matches the crop-field count; unavailable years
   appear only as `unreleased`/`error` rows.
4. NDVI pilot gate: 10-field pilot must run error-free, then spot-check mean,
   median, std, p10, and p90 against a brute-force per-pixel computation.
5. Resource gate: peak disk under the agreed budget, at least 20 GB free, and only
   the approved worker count.

If a gate fails, stop and report — do not expand workers, storage, or scope.

---

## Known limitations

- FIBOA boundaries are a dissolved 2016-2023 CSB vintage; the FIBOA crop attribute
  is a single year, so historical crop comes from CDL, not the boundary file.
- CDL is 30 m, so very small fields have few pixels; `crop_pct` in the crop history
  shows how dominant the majority code was.
- SSURGO property averages are not area-weighted (see above).
- NDVI is a single index from red/NIR; it is not a biomass or yield estimate, and
  cloud/shadow masking still leaves some residual contamination.
- Sentinel-2 10 m pixels are not resampled to field interiors; partial pixels at
  field edges are included, which slightly damps extreme values in narrow fields.
