# Local Instructions

## Purpose

This folder owns the Indiana Field Explorer: a read-only browser app that serves
Johnson County field boundaries, biweekly Sentinel-2 NDVI, SSURGO soils, and CDL
crop history from an existing `indiana-field-discovery` runtime.

## Safe edit scope

Edits stay in this folder and its children unless the user asks for a broader
change. Do not modify the discovery pipeline, parent `SKILL.md`, sibling
field-management workflows, or root policy from an explorer task.

## Read nearby docs first

Read `GUIDE.md` first. For upstream data conventions read
`../indiana-field-discovery/GUIDE.md` and `../indiana-field-discovery/JOHNSON-COUNTY.md`.
For routing context read `../INDEX.md` and `../../SKILL.md`.

## Data handling

- Never write datasets, cache files, or rasters into the repository. Everything
  generated goes under `$INDIANA_RUNTIME_ROOT/state/explorer/`.
- The app must not fabricate NDVI. Missing periods stay `null` and surface as
  `not_computed`, an empty value, or transparent pixels, never as an
  interpolated number.
- Do not expose cloud statistics or cloud layers in the UI; masking is internal.
- Do not bulk-download imagery. Only per-period, per-tile COG windows are read,
  and only for the requested window.
- The map shows per-pixel 10 m NDVI from cached raster composites. Field-average
  NDVI belongs to the trend chart only.
- Soil polygons come from the IndianaMap hosted SSURGO service and are cached;
  do not download a statewide geodatabase.

## Running

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
cd my-farm-advisor/field-management/indiana-field-explorer
PYTHONPATH=src "$INDIANA_RUNTIME_ROOT/venv-indiana/bin/python" \
  -m indiana_field_explorer serve --port 8420
```

Build caches explicitly when needed:

```bash
PYTHONPATH=src ... -m indiana_field_explorer raster --year 2025 --periods 10
PYTHONPATH=src ... -m indiana_field_explorer soils
```

## Local validation

- `python -m compileall src/indiana_field_explorer` for syntax.
- `node --check web/geo.js` and `node --check web/app.js` for the browser bundle.
- `curl -s localhost:8420/healthz` and `/api/meta` after starting the server.
- Verify field selection first with `web/geo.js`: every field's representative
  point must resolve back to that field, and the 486-acre test field
  `181623000957517` plus neighbor `181623000957518` must resolve exactly.
- Check `/api/field-ndvi?field_id=...&year=2025&period=10` and
  `/api/field-ndvi.png?...` for one field; the PNG must be masked to the field
  and `stats` must match `/api/field-raster-stats`.
- Check `/api/soils.geojson?field_id=...` returns only that field's polygons.
- Check `/api/field/<id>` for soils, crops, and the NDVI series.
- Run `./scripts/validate.sh` from the repository root after structural changes.
