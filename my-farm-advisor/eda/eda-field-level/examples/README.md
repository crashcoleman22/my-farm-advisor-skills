# Field-Level EDA Examples

Committed sample outputs from the field-level EDA workflow. The report and small
tables are committed here; full-resolution figures and the complete table set
stay in the external runtime root because they are large generated outputs.

## Files

- **`assignment-2/`** - the Assignment 2 run bundle:
  - `field_level_eda_report.html` - the single-page, self-contained report
    deliverable (all figures embedded as base64 images).
  - `boundary_stats_by_state.csv` - median area, compactness, rectangle fill,
    and shape-class shares for each state.
  - `weather_stats_by_state.csv` - median growing-season precipitation, GDD,
    heat days, frost-free days, and dry-spell length by state and year.
  - `cdl_stats_by_state.csv` - rotation and crop-year statistics by state.
  - `cdl_composition_by_state_year.csv` - area-weighted crop mix per state-year.
  - `cdl_rotation_summary.csv` - per-field rotation sequence and Shannon
    diversity.
  - `data_audit.csv` - input coverage and completeness checks.
  - `walkthrough-script.md` - narration and screen plan for the video walkthrough.
  - `README.md` - provenance and regeneration notes for the bundle.

## Regenerating

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
python scripts/eda/eda_field_level.py \
  --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
  --start-year 2021 --end-year 2025 \
  --cdl-years 2021,2022,2023,2024,2025 --command all --seed 2026

python scripts/reporting/generate_eda_report.py \
  --eda-dir "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/eda/field-level"
```

The run writes the full bundle under `shared/eda/field-level/`, including PNG
figures, every CSV table, `audit/data_audit.md`, and `manifest.json`.

## Relationship to the report

`scripts/reporting/generate_eda_report.py` reads the manifest and tables and
writes one self-contained HTML report. The CSVs here are a committed snapshot
for reviewers; they are not report inputs.
