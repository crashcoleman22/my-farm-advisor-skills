# Grower Web Maps

One lightweight interactive HTML map per grower, built from the field polygons
the data pipeline already downloaded. The map shows every field of the grower
on an online satellite basemap, lets the user zoom, and opens a metadata popup
when a field is clicked.

This is a **grower-level reporting subskill**, not a farm-pipeline step. It
reads finished runtime data and adds no ingest work.

## Why it exists

The pipeline already produces per-field PNG maps, farm HTML reports, and
posters. None of those is an interactive view of *where the fields are*. This
subskill fills that gap without duplicating data: it embeds only boundary
geometry (about 2 KB per farm here) and lets Esri serve the imagery tiles.

## Requirements

- A usable runtime with `DATA_PIPELINE_DATA_ROOT` set.
- At least one farm with `farms/<farm>/boundary/field_boundaries.geojson`.
- Internet access when viewing the map (Leaflet and Esri tiles load from CDNs).
- Python 3.10+; the script itself uses the standard library only. Shapely is
  used only to simplify unusually large polygons.

## Commands

Run from the installed runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"

# one grower
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --grower illinois-grower

# every grower that has field polygons
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py --all-growers

# explicit farm filter and custom output path
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py \
  --grower iowa-grower --farm iowa-demo-farm --out /tmp/iowa-fields.html
```

From the skill checkout, point at the runtime with `--root`:

```bash
python my-farm-advisor/data-pipeline/src/scripts/reporting/generate_grower_web_map.py \
  --grower illinois-grower --root ~/my-farm-advisor-runtime
```

Options: `--grower`, `--all-growers`, `--farm` (repeatable), `--out`, `--title`,
`--root`, `--no-simplify`, `--quiet`.

Behavior notes:

- `--all-growers` skips growers that have no usable polygons and reports `SKIP`
  on stderr; it fails only if no grower produced a map.
- `--grower` fails loudly when the grower or its boundaries are missing.
- `--out` is only valid with `--grower`; the manifest is written next to the
  output as `<name>.manifest.json`.
- Slugs are validated before any path is built, so a slug cannot escape the
  runtime root.

## Outputs

Default locations under the runtime base:

```text
growers/<grower>/derived/reports/<grower>_fields_map.html
growers/<grower>/manifests/grower_web_map.json
```

The manifest records the generation time, generator SHA-256, Leaflet version,
basemap URL and attribution, each input boundary path and SHA-256, field IDs and
acreage, the CDL year used, and the output byte size and SHA-256. Generated
artifacts are runtime data and must stay out of Git.

## What the map contains

| Piece | Detail |
|---|---|
| Basemap | Esri World Imagery XYZ tiles, with Esri attribution preserved |
| Optional overlay | "Labels on" button toggles the Esri boundaries/places reference layer |
| Field geometry | Inline GeoJSON from `boundary/field_boundaries.geojson`, coordinates rounded to 6 decimals |
| Initial view | Fit to the whole grower extent |
| Zoom | Standard Leaflet zoom control, wheel zoom, drag, scale bar |
| Click popup | Grower, farm, field name, field ID, acreage, county/state, boundary source, OSM land use, last known CDL crop with year, link to the farm HTML report when present |
| Sidebar | Field list grouped by farm, sorted by acreage, each row zooms to that field and opens its popup; "Reset view" restores the full extent |
| Failure mode | If the Leaflet CDN is unreachable, the page shows an explicit error instead of a blank map |

Size budget: the generator refuses to write anything above 2 MB. Real growers
here produce a 16 KB file because only geometry is embedded.

## Data-quality guardrails

Two known pipeline issues are handled deliberately:

1. **`annual_precip_mm` is a five-year sum mislabeled as an annual value.** The
   map reads and displays no weather aggregates at all, so the bad summary
   cannot appear in a popup. Add weather back only after that summarizer is
   fixed or re-aggregated per year.
2. **Farm CDL crop history ends in 2023.** Popups show
   `Last CDL crop: Corn (2023)` and never claim a crop for 2024 or 2025. The
   sidebar footer states that later years are pending backfill. The OSM
   `crop_name` tag is shown separately as *land use*, not as a crop-year claim.

When the CDL backfill lands, re-running the command picks up the newer year
automatically because the script selects the newest `*_<year>_cdl.csv` table.

## Tests

```bash
python -m unittest \
  my-farm-advisor/data-pipeline/src/scripts/tests/test_generate_grower_web_map.py
```

The suite builds a synthetic runtime tree in a temporary directory, so it needs
no network and no real grower data. It covers the output paths and manifest,
dominant-crop selection, crop-year fallback, `--all-growers` skipping empty
growers, farm filtering, slug rejection, and the `--out` constraint.

Structural checks on a real grower map:

```bash
python - <<'PY'
import json, re
from pathlib import Path
html = Path("growers/illinois-grower/derived/reports/illinois-grower_fields_map.html").read_text()
data = json.loads(re.search(r'<script id="map-data" type="application/json">(.*?)</script>', html, re.S).group(1))
print(len(data["geojson"]["features"]), "fields", sorted({f["properties"]["field_id"] for f in data["geojson"]["features"]}))
PY
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "mapping library could not be loaded" | CDN blocked or offline | Allow `unpkg.com` and `server.arcgisonline.com` |
| Grey map, polygons visible | Tiles blocked but data fine | Check the browser network tab for tile requests |
| `grower not found in runtime` | Wrong slug or wrong `--root` | List `growers/` under the runtime base |
| `grower has no farm boundary files` | Farm has no `field_boundaries.geojson` | Run the farm pipeline for that farm first |
| Popup link 404s | HTML moved away from its reports directory | Keep the map in `growers/<grower>/derived/reports/` |

## Not a pipeline step

`run_farm_pipeline.py` does not call this script. Farm runs stay unchanged, and
the map is regenerated on demand after boundaries or CDL tables change. If a
grower gains fields later, re-run the command; the map is rebuilt from scratch
and the manifest is overwritten.
