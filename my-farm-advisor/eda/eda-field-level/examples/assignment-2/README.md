# Assignment 2 - Field-Level EDA Bundle

This is the review bundle for the Assignment 2 run: three growers, one farm each,
ten fields each, in Illinois, Iowa, and Nebraska. Everything here was produced by
two committed scripts from real OpenStreetMap field boundaries, NASA POWER daily
weather, and USDA Cropland Data Layer (CDL) rasters for 2021-2025.

## Files

- `field_level_eda_report.html` - the report deliverable. One self-contained
  page with every figure embedded as a data URI, the scope table, key findings,
  methods, comparison scales, all supporting tables, and limitations. No server,
  no external assets.
- `boundary_stats_by_state.csv` - field area, compactness, rectangle fill, and
  shape-class shares per state.
- `weather_stats_by_state.csv` - growing-season precipitation, GDD, heat days,
  frost-free days, and dry-spell length per state-year.
- `cdl_stats_by_state.csv` - crop-year shares and rotation statistics per state.
- `cdl_composition_by_state_year.csv` - area-weighted corn / soybean / other mix
  per state-year.
- `cdl_rotation_summary.csv` - per-field crop sequence, changes, and Shannon
  diversity.
- `data_audit.csv` - input coverage checks for boundaries, weather, and CDL.
- `walkthrough-script.md` - the narration and screen-by-screen plan for the
  2-5 minute video walkthrough.

The full bundle lives in the runtime root, not in Git:
`${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/eda/field-level/` contains all
PNG figures, the remaining tables (per-field boundary metrics, seasonal weather
metrics, pairwise weather correlations, crop history, transitions), the audit
markdown, and `manifest.json` with input SHA-256 hashes and run settings.

## Farms

| State | Grower | Farm | County | Fields | Acres |
| --- | --- | --- | --- | ---: | ---: |
| IL | `illinois-grower` | `illinois-demo-farm` | Iroquois | 10 | 1,521 |
| IA | `iowa-grower` | `iowa-demo-farm` | Kossuth | 10 | 1,363 |
| NE | `nebraska-grower` | `nebraska-demo-farm` | Phelps | 10 | 1,837 |

Nebraska uses Phelps County because it has both corn/soybean production and
extensive center-pivot irrigation; four sampled fields are pivot-shaped circles.

## Headline numbers

- Median field area: IL 76 ac, IA 147 ac, NE 127 ac.
- Shape mix: IL 80% irregular / 20% rectangular; IA 80% rectangular / 20%
  irregular; NE 40% pivot-like / 20% rectangular / 40% irregular.
- Median growing-season totals: IL 592 mm and 1,964 GDD, IA 542 mm and 1,713 GDD,
  NE 467 mm and 2,062 GDD (base 10 °C, 30 °C cap, April-October).
- 2025 crop mix: IL 50% corn / 50% soybean, IA 43% / 55%, NE 76% / 21%.
- IL fields alternate corn and soybean every year (C3 off-diagonal 1.00); IA and
  NE keep more corn-on-corn, including 20% continuous corn in Nebraska.

## How it was made

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"

python scripts/eda/eda_field_level.py \
  --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
  --start-year 2021 --end-year 2025 \
  --cdl-years 2021,2022,2023,2024,2025 \
  --command all --formats png --seed 2026

python scripts/reporting/generate_eda_report.py \
  --eda-dir "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/eda/field-level"
```

Farm boundaries were sampled with
`scripts/ingest/bootstrap_farm_from_county.py` (minimum 20 acres, 8 km cluster
around a fixed anchor, `--seed 2026`, and `--pivot-quota 4` for Nebraska). The
farm pipeline then produced per-field NASA POWER weather and farm CDL
composition tables, and `download_cdl.py --cdl-years` extended CDL to the
weather window.

## Read with care

- OSM boundaries are not legal survey boundaries; CDL is a 30 m product and field
  edges mix crops, roads, and non-crop pixels.
- One Nebraska polygon is tagged `landuse=farmland` in OSM but CDL reports it as
  mostly forest/woodland. It is kept and grouped as Other rather than silently
  dropped, so the report states the disagreement instead of hiding it.
- NASA POWER cells are coarser than fields, so all fields in a farm share one
  weather series. Within-farm temperature correlations are effectively 1.0 by
  construction; the useful weather comparison is between states.
- Ten fields per state support descriptive comparison, not statewide inference.
