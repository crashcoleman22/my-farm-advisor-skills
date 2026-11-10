# Provenance — Grower Web Maps

## What this is

A reporting subskill of `my-farm-advisor/data-pipeline`. It renders the field
boundary polygons that the data pipeline already generated into one interactive
HTML map per grower.

## Source of record

- Repository: `crashcoleman22/my-farm-advisor-skills` (personal fork)
- Branch: `assignment-1`
- Upstream reference: `borealBytes/my-farm-advisor-skills` (read-only reference
  remote; no code was copied from it for this subskill)
- Scope: new implementation written for Assignment 1 Phase 3. No third-party
  skill or workflow was vendored, mirrored, or imported.

## Upstream data inputs (runtime, not committed)

| Input | Origin | Notes |
|---|---|---|
| `farms/<farm>/boundary/field_boundaries.geojson` | Data pipeline ingest step | Field polygons downloaded by the pipeline; source recorded per feature (for example `OpenStreetMap/Overpass`) |
| `farms/<farm>/derived/tables/*_<year>_cdl.csv` | Pipeline CDL step | Supplies the dominant crop and the year label shown in popups |
| `grower.json`, `farm.json`, `field.json` | Pipeline bootstrap | Display names for grower, farm, and field |

Generated maps and manifests are runtime artifacts under
`${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/` and must not be committed.

## Third-party components loaded at view time

| Component | Version | License / terms | How it is used |
|---|---|---|---|
| Leaflet | 1.9.4 | BSD-2-Clause | Loaded from `unpkg.com`; not vendored into this repo |
| Esri World Imagery tiles | live service | Esri terms of use; attribution required | Basemap tiles from `server.arcgisonline.com` |
| Esri World Boundaries and Places | live service | Esri terms of use; attribution required | Optional labels overlay |

No tile imagery, raster, or copyright-bearing data is stored in the repository
or embedded in the generated HTML. The generated page preserves the required
attribution strings.

## Reproducibility

The generator writes `growers/<grower>/manifests/grower_web_map.json` with the
generator SHA-256, input boundary SHA-256 values, the CDL year used, the basemap
URL, and the output size and SHA-256. Re-running the command on the same runtime
data reproduces the same geometry payload and manifest structure; only
`generated_at` and the output hash change if the template or inputs change.
