# Grower Web Maps — Agent Instructions

## When to use this subskill

Use it when a user asks for an interactive or web map of a grower's fields, a
browsable field index, or a clickable satellite view built from the data
pipeline's field boundaries.

Do not use it for static PNG field maps (already produced by the pipeline), for
basemap-free diagrams, or for embedding raster/NDVI layers.

## Entry point

```bash
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python \
  ${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_web_map.py \
  --grower <grower-slug>
```

`--all-growers` rebuilds every grower with field polygons. `--help` lists all
options.

Read [`README.md`](README.md) for outputs, options, and troubleshooting.

## Hard constraints

- Output stays inside the runtime tree:
  `growers/<grower>/derived/reports/<grower>_fields_map.html` plus
  `growers/<grower>/manifests/grower_web_map.json`. Never write these into the
  skill checkout.
- Never embed rasters, imagery, NDVI rasters, or full data bundles. Geometry
  plus metadata only; the generator enforces a 2 MB HTML ceiling.
- Keep the Esri attribution intact. The basemap is online imagery, so the page
  must never imply offline completeness.
- Do not surface `annual_precip_mm`. It is a known five-year sum mislabeled as
  annual. Add weather only after that summarizer is fixed.
- Crop attribution must stay year-labeled. Farm CDL history currently ends in
  2023; never present 2024+ crops as known.
- Do not add this script to `run_farm_pipeline.py`. It is an explicit
  grower-level reporting command.

## Validation

```bash
python -m unittest \
  my-farm-advisor/data-pipeline/src/scripts/tests/test_generate_grower_web_map.py
cd ../.. && ./scripts/validate.sh
```

The unit tests are offline and synthetic; they must not require real grower data.
