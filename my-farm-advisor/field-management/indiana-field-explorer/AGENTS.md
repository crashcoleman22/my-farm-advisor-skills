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
  `not_computed` or an empty value, never as an interpolated number.
- Do not expose cloud statistics or cloud layers in the UI; masking is internal.
- Do not bulk-download imagery. Only per-period, per-tile COG windows are read,
  and only for the requested 2020 window.

## Running

```bash
export INDIANA_RUNTIME_ROOT=/path/to/indiana-runtime
cd my-farm-advisor/field-management/indiana-field-explorer
PYTHONPATH=src "$INDIANA_RUNTIME_ROOT/venv-indiana/bin/python" \
  -m indiana_field_explorer serve --port 8420
```

## Local validation

- `python -m compileall src/indiana_field_explorer` for syntax.
- `curl -s localhost:8420/healthz` and `/api/meta` after starting the server.
- Check one field's `/api/field/<id>` for soils, crops, and NDVI series.
- Run `./scripts/validate.sh` from the repository root after structural changes.
