---
name: eda-field-level
description: Run a repeatable field-level exploratory data analysis across one or more growers, producing static boundary, weather, and USDA CDL figures plus CSV tables and a self-contained HTML report.
version: 1.0.0
author: Boreal Bytes
tags: [eda, field-boundaries, weather, cdl, geospatial, reporting]
---

# Workflow: eda-field-level

## Description

This workflow turns installed grower data into a small, auditable EDA bundle:
field-boundary statistics, growing-season weather metrics, USDA CDL crop
history, one static geospatial map, CSV tables, a data audit, and a manifest.
It is read-only and never downloads data, so the same command can be rerun after
any pipeline refresh.

The analysis command and the report generator are intentionally separate:

- `data-pipeline/src/scripts/eda/eda_field_level.py` writes tables, figures, an
  audit, and `manifest.json`.
- `data-pipeline/src/scripts/reporting/generate_eda_report.py` reads that bundle
  and writes one self-contained HTML page with base64 images.

## When to Use This Workflow

- You need a first structured EDA pass over several farms or states.
- You want to compare field geometry, weather, or crop history across growers.
- You need static PNG/SVG figures and CSV tables for a report or review.
- You want a reproducible audit of which boundaries and years were analyzed.
- You do **not** need an interactive dashboard or soil analysis.

## Prerequisites

Use an installed runtime with field boundaries, per-field weather, and farm CDL
tables already generated:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
```

Expected inputs per farm:

- `growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson`
- `growers/<grower>/farms/<farm>/fields/<field>/weather/daily_weather.csv`
- `growers/<grower>/farms/<farm>/derived/tables/*_cdl_*_full_composition.csv`

The EDA command stops with a clear message if any input family is missing.

## Quick Start

Run every section for three growers:

```bash
python scripts/eda/eda_field_level.py \
  --grower illinois-grower \
  --grower iowa-grower \
  --grower nebraska-grower \
  --start-year 2021 \
  --end-year 2025 \
  --cdl-years 2021,2022,2023,2024,2025 \
  --command all \
  --seed 2026
```

Run one section while iterating:

```bash
python scripts/eda/eda_field_level.py \
  --grower illinois-grower --command boundaries --formats png
```

Write to a scratch directory instead of `shared/eda/field-level`:

```bash
python scripts/eda/eda_field_level.py \
  --grower illinois-grower --grower iowa-grower \
  --out /tmp/eda-preview --formats png --no-basemap
```

Then build the report:

```bash
python scripts/reporting/generate_eda_report.py \
  --eda-dir "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/eda/field-level"
```

## Commands

| Command | Output |
| --- | --- |
| `--command audit` | `audit/data_audit.md`, `tables/data_audit.csv` |
| `--command boundaries` | A1-A3 figures, boundary metrics and correlations |
| `--command weather` | B1-B3 figures, seasonal weather metrics and pairwise correlations |
| `--command cdl` | C1-C3 figures, crop history, rotations, transitions |
| `--command map` | D static map |
| `--command all` | Everything plus `manifest.json` |

Useful flags:

- `--grower` is repeatable. `--grower slug:farm` narrows to one farm.
- `--cdl-years` overrides the default `start-year..end-year` range.
- `--formats png,svg` controls figure output; the report embeds downscaled copies
  of the figures so the HTML stays a reasonable size.
- `--no-basemap` skips contextily tiles when the map should be fully offline.
- `--root` sets `DATA_PIPELINE_DATA_ROOT` when the shell variable is not exported.
- `--seed` fixes sampling and permutation tests.

## Output Layout

```text
shared/eda/field-level/
├── audit/
│   └── data_audit.md
├── figures/
│   ├── fig01_boundary_area_by_state.png|svg
│   ├── ...
│   └── fig10_map_fields_by_crop.png|svg
├── tables/
│   ├── boundary_stats_by_state.csv
│   ├── boundary_correlations.csv
│   ├── field_boundary_metrics.csv
│   ├── weather_seasonal_metrics.csv
│   ├── weather_stats_by_state.csv
│   ├── weather_pairwise_correlations.csv
│   ├── cdl_crop_history.csv
│   ├── cdl_rotation_summary.csv
│   ├── cdl_stats_by_state.csv
│   ├── cdl_composition_by_state_year.csv
│   ├── cdl_transitions.csv
│   └── data_audit.csv
├── report/
│   └── field_level_eda_report.html
└── manifest.json
```

## How to Read the Figures

### A. Field boundaries

- **A1** compares field area by state. A log x-axis keeps small meadows and large
  quarters in the same view.
- **A2** separates shape from size. Polsby-Popper compactness is `4πA/P²`; a
  circle is 1.0 and a square is about 0.785. Rectangle fill is area divided by
  the minimum rotated rectangle area.
- **A3** tests perimeter-area scaling. A self-similar outline scales with slope
  0.5 on log-log axes; steeper slopes imply elongation or irregular edges.
  Spearman rho is reported with a permutation p-value.

Shape classes are descriptive thresholds, not legal classifications:

| Class | Rule |
| --- | --- |
| `pivot-like` | compactness ≥ 0.90 and rectangle fill 0.68-0.87 |
| `rectangular` | rectangle fill ≥ 0.90 |
| `irregular` | everything else |

### B. Historical weather

- **B1** shows April-October precipitation for every field-year. Thin lines are
  fields; the black line is the state median.
- **B2** shows cumulative GDD with base 10 °C and a 30 °C cap. Lines are state
  medians and bands are interquartile ranges.
- **B3** correlates daily mean temperature between field pairs and plots that
  correlation against distance. Same-farm pairs should sit above cross-state
  pairs at the same distance.

### C. USDA CDL crop history

- **C1** is the dominant-crop heatmap (C = corn, S = soybeans, O = other).
- **C2** combines area-weighted crop mix by state-year with rotation diversity
  (Shannon entropy of a field's mean crop shares).
- **C3** is a row-normalised year-to-year transition matrix. The diagonal is
  repetition; off-diagonal cells are rotations.

### D. Static map

- **D** draws the real field polygons, coloured by the latest CDL dominant crop.
  It is a single static PNG/SVG, not a dashboard.

## Comparison Scales

The same tables support three comparisons the assignment asks for:

1. **Within a field across years** - use `cdl_crop_history.csv`,
   `cdl_rotation_summary.csv`, and `weather_seasonal_metrics.csv`.
2. **Across fields in a grower** - use `field_boundary_metrics.csv` and
   `weather_pairwise_correlations.csv`.
3. **Across states** - use `boundary_stats_by_state.csv`,
   `weather_stats_by_state.csv`, and `cdl_stats_by_state.csv`.

## Adding a State or County

Use the county bootstrap before the EDA command. For a Nebraska county with
corn/soy and center-pivot irrigation, Phelps County is a good example:

```bash
python scripts/ingest/bootstrap_farm_from_county.py \
  --state-fips 31 --county-name Phelps \
  --count 10 --min-area-acres 20 \
  --cluster-radius-km 8 \
  --anchor-lat 40.601754 --anchor-lon -99.313056 \
  --pivot-quota 4 --seed 2026 \
  --grower-slug nebraska-grower \
  --farm-slug nebraska-demo-farm \
  --farm-name "Nebraska Demo Farm"
```

- `--cluster-radius-km` keeps sampled fields near the anchor; the sampler
  expands the radius only if too few candidates exist.
- `--pivot-quota` reserves slots for round pivot-shaped fields, which is useful
  when the county is known for irrigation.
- Run the farm pipeline after bootstrap to create weather and CDL inputs, then
  rerun the EDA command with `--cdl-years` matching the weather window.

## Report Generation

`generate_eda_report.py` is a reporting step, not part of the reusable EDA
analysis. It reads `manifest.json`, the CSV tables, and the PNG figures, then
writes one HTML file with embedded images. It does not fetch data, does not
start a server, and does not change the analysis outputs.

## Validation

Offline unit tests cover shape classification, GDD, frost dates, rotations,
transitions, and Shannon diversity:

```bash
python -m unittest \
  my-farm-advisor/data-pipeline/src/scripts/tests/test_eda_metrics.py
```

Repository-level validation:

```bash
./scripts/validate.sh
```

## Notes and Limits

- Boundaries come from OpenStreetMap and are not survey-grade.
- CDL is a 30 m product; edges mix crops, roads, and non-crop pixels.
- NASA POWER cells are coarser than fields, so nearby fields can share values.
- Ten fields per state support descriptive comparison, not statewide inference.
- The workflow intentionally excludes soil analysis and interactive dashboards.
