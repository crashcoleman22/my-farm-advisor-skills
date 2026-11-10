#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Generate one lightweight interactive grower field map (single HTML file).

The map is built from the field polygons that the data pipeline already wrote:

    growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson

It renders those polygons on an online satellite basemap (Esri World Imagery)
with click popups (grower, farm, field name, field id, acreage, county/state,
boundary source, last known CDL crop) and a field list that zooms to a field.

Output (runtime, never committed):

    growers/<grower>/derived/reports/<grower>_fields_map.html
    growers/<grower>/manifests/grower_web_map.json

Design constraints:

* No raster, NDVI, weather, or imagery bytes are embedded; geometry is the only
  payload, so the HTML stays a few tens of kilobytes.
* Basemap tiles and Leaflet load from the internet; the file itself opens from
  ``file://`` and shows an explicit error message if the CDN is unreachable.
* No weather aggregates are displayed, so the known ``annual_precip_mm``
  five-year-sum reporting bug cannot leak into a popup.
* Crop attribution comes from the farm CDL tables and is always labelled with
  its year. Farm CDL history currently ends in 2023, so 2024-2025 crop
  attribution is reported as pending instead of guessed.

Usage:
    python src/scripts/reporting/generate_grower_web_map.py --grower illinois-grower
    python src/scripts/reporting/generate_grower_web_map.py --all-growers
    python src/scripts/reporting/generate_grower_web_map.py --grower iowa-grower \
        --farm iowa-demo-farm --out /tmp/iowa-fields.html
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_PATH = Path(__file__).resolve()
_LOCAL_LIB = _SCRIPT_PATH.parents[1] / "lib"
if str(_LOCAL_LIB) not in sys.path:
    sys.path.insert(0, str(_LOCAL_LIB))

from runtime_paths import resolve_runtime_paths  # noqa: E402

LEAFLET_VERSION = "1.9.4"
LEAFLET_CSS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css"
LEAFLET_JS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js"
BASEMAP_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
BASEMAP_ATTRIBUTION = (
    "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community"
)
LABELS_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)
LABELS_ATTRIBUTION = "Labels &copy; Esri"

COORD_DECIMALS = 6
SIMPLIFY_VERTEX_THRESHOLD = 20_000
SIMPLIFY_TOLERANCE_DEGREES = 1e-5
MAX_HTML_BYTES = 2_000_000
CDL_TABLE_PATTERN = re.compile(r"^(?P<prefix>.+)_(?P<year>\d{4})_cdl\.csv$")
_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class WebMapError(RuntimeError):
    """Raised when a grower map cannot be built from the runtime data."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_slug(value: str, label: str) -> str:
    """Mirror ``lib.paths.validate_path_slug`` without importing its env-coupled module."""
    slug = str(value)
    if not slug or slug in {".", ".."}:
        raise WebMapError(f"{label} must not be empty, '.' or '..': {value!r}")
    if "/" in slug or "\\" in slug or Path(slug).is_absolute():
        raise WebMapError(f"{label} must not contain path separators: {value!r}")
    if not _SLUG_PATTERN.fullmatch(slug):
        raise WebMapError(f"{label} contains unsafe characters: {value!r}")
    return slug


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _display_name(metadata: Any, fallback: str) -> str:
    if isinstance(metadata, dict):
        name = metadata.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return fallback


def _pipeline_root(raw_root: str | None) -> Path:
    if raw_root:
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            raise WebMapError(f"--root must be an absolute path, got: {raw_root}")
        root = candidate
    else:
        root = resolve_runtime_paths().data_root
    return (root / "data-pipeline").resolve(strict=False)


# --------------------------------------------------------------------------- #
# crop attribution
# --------------------------------------------------------------------------- #


def _cdl_tables(farm_dir: Path) -> dict[int, Path]:
    tables_dir = farm_dir / "derived" / "tables"
    if not tables_dir.is_dir():
        return {}
    found: dict[int, Path] = {}
    for path in sorted(tables_dir.glob("*_cdl.csv")):
        match = CDL_TABLE_PATTERN.match(path.name)
        if not match:
            continue
        found[int(match.group("year"))] = path
    return found


def _dominant_crops(table_path: Path) -> dict[str, tuple[str, float]]:
    """field_id -> (dominant crop_name, percent) from one farm CDL table."""
    result: dict[str, tuple[str, float]] = {}
    if not table_path.is_file():
        return result
    try:
        with table_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                field_id = (row.get("field_id") or "").strip()
                crop = (row.get("crop_name") or "").strip()
                if not field_id or not crop:
                    continue
                try:
                    pct = float(row.get("pct") or 0.0)
                except ValueError:
                    pct = 0.0
                current = result.get(field_id)
                if current is None or pct > current[1]:
                    result[field_id] = (crop, pct)
    except OSError:
        return {}
    return result


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #


def _round_coords(node: Any, decimals: int) -> Any:
    if isinstance(node, (int, float)):
        return round(float(node), decimals)
    if isinstance(node, list):
        return [_round_coords(item, decimals) for item in node]
    if isinstance(node, tuple):
        return [_round_coords(item, decimals) for item in node]
    return node


def _count_vertices(geometry: dict[str, Any]) -> int:
    coordinates = geometry.get("coordinates")
    total = 0

    def walk(node: Any) -> int:
        nonlocal total
        if isinstance(node, list) and node and isinstance(node[0], (int, float)):
            total += 1
            return total
        if isinstance(node, list):
            for item in node:
                walk(item)
        return total

    walk(coordinates)
    return total


def _simplify_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    """Douglas-Peucker simplification for very large polygons, best effort."""
    try:
        from shapely.geometry import mapping, shape
    except ImportError:  # pragma: no cover - shapely is a pipeline dependency
        return geometry
    try:
        simplified = shape(geometry).simplify(
            SIMPLIFY_TOLERANCE_DEGREES, preserve_topology=True
        )
    except Exception:  # pragma: no cover - defensive
        return geometry
    if simplified.is_empty:
        return geometry
    mapped = mapping(simplified)
    return {"type": mapped["type"], "coordinates": mapped["coordinates"]}


def _prepare_geometry(geometry: dict[str, Any], *, simplify: bool) -> dict[str, Any]:
    if simplify and _count_vertices(geometry) > SIMPLIFY_VERTEX_THRESHOLD:
        geometry = _simplify_geometry(geometry)
    return {
        "type": geometry.get("type", "Polygon"),
        "coordinates": _round_coords(geometry.get("coordinates"), COORD_DECIMALS),
    }


# --------------------------------------------------------------------------- #
# data collection
# --------------------------------------------------------------------------- #


def _farm_html_report(farm_dir: Path) -> str | None:
    reports_dir = farm_dir / "derived" / "reports"
    if not reports_dir.is_dir():
        return None
    candidates = sorted(reports_dir.glob("*_farm_report.html"))
    return candidates[0].name if candidates else None


def _collect_farm(
    farm_dir: Path,
    *,
    grower_slug: str,
    grower_name: str,
    farm_slug: str,
    simplify: bool,
) -> dict[str, Any] | None:
    boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
    if not boundary_path.is_file():
        return None
    payload = _read_json(boundary_path)
    if not isinstance(payload, dict):
        raise WebMapError(f"unreadable field boundary file: {boundary_path}")

    farm_meta = _read_json(farm_dir / "farm.json", {})
    farm_name = _display_name(farm_meta, farm_slug)

    cdl_tables = _cdl_tables(farm_dir)
    cdl_year = max(cdl_tables) if cdl_tables else None
    crops = _dominant_crops(cdl_tables[cdl_year]) if cdl_year else {}

    report_name = _farm_html_report(farm_dir)
    report_rel = (
        "../../farms/"
        f"{farm_slug}/derived/reports/{report_name}"
        if report_name
        else None
    )

    features: list[dict[str, Any]] = []
    total_acres = 0.0
    for feature in payload.get("features") or []:
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or not geometry.get("coordinates"):
            continue
        props = feature.get("properties") or {}
        field_id = str(props.get("field_id") or "").strip()
        if not field_id:
            continue
        field_dir = farm_dir / "fields" / field_id
        field_meta = _read_json(field_dir / "field.json", {})
        field_name = _display_name(field_meta, field_id)

        try:
            acres = float(props.get("area_acres") or 0.0)
        except (TypeError, ValueError):
            acres = 0.0
        total_acres += acres

        last_crop, last_crop_pct = crops.get(field_id, (None, None))
        county = str(props.get("county_name") or "").strip()
        state = str(props.get("state_fips") or "").strip()
        state_label = str(farm_meta.get("state") or "").strip() if farm_meta else ""
        location = ", ".join(part for part in (county, state_label or state) if part)

        features.append(
            {
                "type": "Feature",
                "geometry": _prepare_geometry(geometry, simplify=simplify),
                "properties": {
                    "field_id": field_id,
                    "field_name": field_name,
                    "farm_slug": farm_slug,
                    "farm_name": farm_name,
                    "grower_slug": grower_slug,
                    "grower_name": grower_name,
                    "area_acres": round(acres, 1),
                    "county": county,
                    "state": state_label,
                    "location": location,
                    "source": str(props.get("source") or "").strip(),
                    "land_use": str(props.get("crop_name") or "").strip(),
                    "last_crop_name": last_crop,
                    "last_crop_year": cdl_year if last_crop else None,
                    "last_crop_pct": round(last_crop_pct, 1) if last_crop_pct else None,
                    "farm_report_href": report_rel,
                },
            }
        )

    if not features:
        return None

    features.sort(key=lambda item: item["properties"]["area_acres"], reverse=True)
    return {
        "farm_slug": farm_slug,
        "farm_name": farm_name,
        "boundary_path": boundary_path,
        "boundary_sha256": _sha256(boundary_path),
        "field_count": len(features),
        "acres": round(total_acres, 1),
        "cdl_year": cdl_year,
        "features": features,
    }


def _discover_farms(pipeline_root: Path, grower_slug: str) -> list[str]:
    farms_dir = pipeline_root / "growers" / grower_slug / "farms"
    if not farms_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in farms_dir.iterdir()
        if path.is_dir() and (path / "boundary" / "field_boundaries.geojson").is_file()
    )


def _discover_growers(pipeline_root: Path) -> list[str]:
    growers_dir = pipeline_root / "growers"
    if not growers_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in growers_dir.iterdir()
        if path.is_dir() and path.name != "default-grower" and (path / "grower.json").is_file()
    )


def _build_payload(
    pipeline_root: Path,
    grower_slug: str,
    farm_filter: list[str] | None,
    *,
    title: str | None,
    simplify: bool,
) -> dict[str, Any]:
    grower_dir = pipeline_root / "growers" / grower_slug
    if not grower_dir.is_dir():
        raise WebMapError(f"grower not found in runtime: {grower_dir}")

    grower_meta = _read_json(grower_dir / "grower.json", {})
    grower_name = _display_name(grower_meta, grower_slug)

    available = _discover_farms(pipeline_root, grower_slug)
    if not available:
        raise WebMapError(f"grower has no farm boundary files: {grower_slug}")
    farms = (
        [farm for farm in available if farm in set(farm_filter)]
        if farm_filter
        else available
    )
    if not farms:
        raise WebMapError(
            f"none of the requested farms exist for {grower_slug}: {', '.join(farm_filter or [])}"
        )

    collected: list[dict[str, Any]] = []
    for farm_slug in farms:
        farm = _collect_farm(
            pipeline_root / "growers" / grower_slug / "farms" / farm_slug,
            grower_slug=grower_slug,
            grower_name=grower_name,
            farm_slug=farm_slug,
            simplify=simplify,
        )
        if farm is not None:
            collected.append(farm)
    if not collected:
        raise WebMapError(f"no usable field polygons found for {grower_slug}")

    features = [feature for farm in collected for feature in farm["features"]]
    field_count = len(features)
    acres = round(sum(farm["acres"] for farm in collected), 1)
    cdl_year = max((farm["cdl_year"] or 0) for farm in collected) or None

    map_title = title or f"{grower_name} field map"
    crop_note = (
        f"Last known crop is CDL {cdl_year}. Crop attribution for years after "
        f"{cdl_year} is pending backfill; the map never guesses it."
        if cdl_year
        else "No farm CDL crop table is available yet, so crop attribution is omitted."
    )

    return {
        "meta": {
            "title": map_title,
            "generated_at": _utc_now(),
            "grower_slug": grower_slug,
            "grower_name": grower_name,
            "field_count": field_count,
            "acres": acres,
            "farms": [
                {
                    "farm_slug": farm["farm_slug"],
                    "farm_name": farm["farm_name"],
                    "field_count": farm["field_count"],
                    "acres": farm["acres"],
                }
                for farm in collected
            ],
            "basemap": {
                "name": "Esri World Imagery",
                "url": BASEMAP_URL,
                "attribution": BASEMAP_ATTRIBUTION,
                "labels_url": LABELS_URL,
                "labels_attribution": LABELS_ATTRIBUTION,
            },
            "leaflet_version": LEAFLET_VERSION,
            "crop_note": crop_note,
            "cdl_year": cdl_year,
            "tiles_need_internet": True,
        },
        "geojson": {"type": "FeatureCollection", "features": features},
        "_farms": collected,
    }


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="stylesheet" href="{{LEAFLET_CSS}}" crossorigin="">
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #111827; background: #0f172a;
  }
  #app { display: flex; height: 100%; }
  #sidebar {
    width: 340px; flex: 0 0 340px; display: flex; flex-direction: column;
    background: #f8fafc; border-right: 1px solid #d7dde5; z-index: 500;
  }
  #sidebar header { padding: 16px 16px 10px; border-bottom: 1px solid #e2e8f0; }
  #sidebar h1 { margin: 0; font-size: 16px; line-height: 1.3; letter-spacing: -0.01em; }
  #sidebar .sub { margin: 4px 0 0; font-size: 12px; color: #64748b; }
  #summary { display: flex; gap: 8px; padding: 10px 16px; border-bottom: 1px solid #e2e8f0; }
  .stat {
    flex: 1; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
    padding: 6px 8px;
  }
  .stat b { display: block; font-size: 15px; }
  .stat span { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #64748b; }
  #controls { display: flex; gap: 8px; padding: 10px 16px; }
  button {
    font: inherit; font-size: 12px; padding: 6px 10px; border-radius: 6px;
    border: 1px solid #cbd5e1; background: #fff; color: #1f2937; cursor: pointer;
  }
  button:hover { background: #eef2f7; }
  button.primary { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
  button.primary:hover { background: #1e40af; }
  #fields { flex: 1; overflow-y: auto; padding: 4px 8px 12px; }
  .farm-group { margin: 8px 8px 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #64748b; }
  .field-row {
    display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 8px 8px; border: 1px solid transparent; border-radius: 8px;
    background: none; text-align: left; cursor: pointer;
  }
  .field-row:hover { background: #eef2f7; }
  .field-row:focus-visible { outline: 2px solid #1d4ed8; outline-offset: 1px; }
  .field-row.active { background: #e0ecff; border-color: #bfd3f7; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; flex: 0 0 10px; }
  .field-text { min-width: 0; flex: 1; }
  .field-name {
    display: block; font-size: 12.5px; font-weight: 600; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .field-meta { display: block; font-size: 11px; color: #64748b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #sidebar footer { padding: 10px 16px; border-top: 1px solid #e2e8f0; font-size: 10.5px; color: #64748b; line-height: 1.45; }
  #map-wrap { position: relative; flex: 1; min-width: 0; }
  #map { position: absolute; inset: 0; background: #1f2937; }
  #map-error {
    position: absolute; inset: 0; display: none; place-items: center; padding: 24px;
    background: rgba(15, 23, 42, .92); color: #e2e8f0; text-align: center; z-index: 900;
  }
  #map-error.show { display: grid; }
  #map-error div { max-width: 420px; font-size: 13px; line-height: 1.5; }
  #sidebar-toggle { position: absolute; top: 10px; left: 56px; z-index: 800; display: none; }
  .leaflet-popup-content { margin: 12px 14px; font-size: 12.5px; line-height: 1.5; }
  .leaflet-popup-content h2 { margin: 0 0 2px; font-size: 14px; }
  .popup-farm { margin: 0 0 8px; font-size: 11px; color: #64748b; }
  .popup-grid { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; margin: 0; }
  .popup-grid dt { color: #64748b; }
  .popup-grid dd { margin: 0; font-weight: 500; }
  .popup-note { margin: 8px 0 0; font-size: 10.5px; color: #64748b; }
  .popup-link { display: inline-block; margin-top: 8px; font-size: 11.5px; }
  @media (max-width: 760px) {
    #sidebar {
      position: absolute; inset: 0 auto 0 0; height: 100%;
      transform: translateX(-100%); transition: transform .2s ease;
      box-shadow: 0 0 24px rgba(15, 23, 42, .35);
    }
    #sidebar.open { transform: translateX(0); }
    #sidebar-toggle { display: block; }
  }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <header>
      <h1 id="map-title"></h1>
      <p class="sub" id="map-sub"></p>
      <p class="sub">Click a field for details, or zoom to one from the list.</p>
    </header>
    <div id="summary"></div>
    <div id="controls">
      <button type="button" class="primary" id="reset-view">Reset view</button>
      <button type="button" id="toggle-labels">Labels on</button>
    </div>
    <div id="fields"></div>
    <footer id="map-footer"></footer>
  </aside>
  <main id="map-wrap">
    <div id="map"></div>
    <button type="button" id="sidebar-toggle">Fields</button>
    <div id="map-error"><div id="map-error-text"></div></div>
  </main>
</div>
<script id="map-data" type="application/json">{{DATA_JSON}}</script>
<script src="{{LEAFLET_JS}}" crossorigin=""></script>
<script>
(function () {
  "use strict";

  var dataEl = document.getElementById("map-data");
  var data = JSON.parse(dataEl.textContent);
  var meta = data.meta;
  var geojson = data.geojson;

  document.getElementById("map-title").textContent = meta.title;
  document.getElementById("map-sub").textContent =
    meta.grower_name + " \\u2022 generated " + meta.generated_at;

  function showError(message) {
    document.getElementById("map-error-text").textContent = message;
    document.getElementById("map-error").classList.add("show");
  }

  if (!window.L) {
    showError(
      "The mapping library could not be loaded from the CDN. The field data is " +
      "embedded in this file, but the basemap and controls need internet access. " +
      "Reconnect and reload."
    );
    return;
  }

  document.getElementById("summary").innerHTML =
    '<div class="stat"><b>' + meta.field_count + '</b><span>Fields</span></div>' +
    '<div class="stat"><b>' + meta.acres.toLocaleString() + '</b><span>Acres</span></div>' +
    '<div class="stat"><b>' + meta.farms.length + '</b><span>Farms</span></div>';

  document.getElementById("map-footer").innerHTML =
    "Boundaries: pipeline field polygons. Basemap: " + meta.basemap.name +
    " (online tiles).<br>" + meta.crop_note;

  var PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed",
                 "#0891b2", "#be123c", "#4d7c0f"];
  var colorByField = {};
  geojson.features.forEach(function (feature, index) {
    colorByField[feature.properties.field_id] = PALETTE[index % PALETTE.length];
  });

  var map = L.map("map", { zoomControl: true, preferCanvas: true });
  map.attributionControl.setPrefix("");

  var imagery = L.tileLayer(meta.basemap.url, {
    maxZoom: 19,
    attribution: meta.basemap.attribution
  }).addTo(map);

  var labels = L.tileLayer(meta.basemap.labels_url, {
    maxZoom: 19,
    attribution: meta.basemap.labels_attribution
  });

  L.control.scale({ imperial: true, metric: true, position: "bottomright" }).addTo(map);

  var labelsOn = false;
  var labelsButton = document.getElementById("toggle-labels");
  labelsButton.addEventListener("click", function () {
    labelsOn = !labelsOn;
    if (labelsOn) { labels.addTo(map); } else { map.removeLayer(labels); }
    labelsButton.textContent = labelsOn ? "Labels off" : "Labels on";
  });

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function baseStyle(fieldId) {
    return {
      color: colorByField[fieldId] || "#2563eb",
      weight: 2,
      opacity: 0.95,
      fillColor: colorByField[fieldId] || "#2563eb",
      fillOpacity: 0.22
    };
  }

  var layersById = {};
  var activeId = null;

  function applyStyles() {
    Object.keys(layersById).forEach(function (fieldId) {
      var layer = layersById[fieldId];
      var style = baseStyle(fieldId);
      if (fieldId === activeId) {
        style.weight = 4;
        style.fillOpacity = 0.38;
      }
      layer.setStyle(style);
      if (fieldId === activeId) { layer.bringToFront(); }
    });
  }

  function popupHtml(props) {
    var rows = [
      ["Grower", props.grower_name],
      ["Farm", props.farm_name],
      ["Field", props.field_name],
      ["Field ID", props.field_id],
      ["Area", props.area_acres.toLocaleString() + " acres"],
      ["County", props.location || "Unknown"],
      ["Boundary", props.source || "Unknown"]
    ];
    if (props.land_use) { rows.push(["Land use", props.land_use]); }
    if (props.last_crop_name) {
      rows.push(["Last CDL crop", props.last_crop_name + " (" + props.last_crop_year +
                 (props.last_crop_pct ? ", " + props.last_crop_pct + "%" : "") + ")"]);
    }
    var html = "<h2>" + escapeHtml(props.field_name) + "</h2>" +
      '<p class="popup-farm">' + escapeHtml(props.farm_name) + "</p>" +
      '<dl class="popup-grid">';
    rows.forEach(function (row) {
      html += "<dt>" + escapeHtml(row[0]) + "</dt><dd>" + escapeHtml(row[1]) + "</dd>";
    });
    html += "</dl>";
    if (props.last_crop_name) {
      html += '<p class="popup-note">Crop labels stop at the latest available CDL ' +
        "year; later years are not guessed.</p>";
    }
    if (props.farm_report_href) {
      html += '<a class="popup-link" href="' + escapeHtml(props.farm_report_href) +
        '" target="_blank" rel="noopener">Open farm report (HTML)</a>';
    }
    return html;
  }

  var fieldLayer = L.geoJSON(geojson, {
    style: function (feature) { return baseStyle(feature.properties.field_id); },
    onEachFeature: function (feature, layer) {
      var props = feature.properties;
      layersById[props.field_id] = layer;
      layer.bindPopup(popupHtml(props), { maxWidth: 320, autoPan: true });
      layer.bindTooltip(props.field_name, { sticky: true, direction: "top" });
      layer.on({
        mouseover: function () { layer.setStyle({ fillOpacity: 0.38 }); },
        mouseout: function () {
          layer.setStyle({ fillOpacity: props.field_id === activeId ? 0.38 : 0.22 });
        },
        click: function () {
          activeId = props.field_id;
          applyStyles();
          highlightRow(activeId);
        }
      });
    }
  }).addTo(map);

  var fullBounds = fieldLayer.getBounds();
  map.fitBounds(fullBounds, { padding: [28, 28] });

  function highlightRow(fieldId) {
    var rows = document.querySelectorAll(".field-row");
    for (var i = 0; i < rows.length; i += 1) {
      rows[i].classList.toggle("active", rows[i].getAttribute("data-field-id") === fieldId);
    }
  }

  function focusField(fieldId, openPopup) {
    var layer = layersById[fieldId];
    if (!layer) { return; }
    activeId = fieldId;
    applyStyles();
    highlightRow(fieldId);
    map.fitBounds(layer.getBounds(), { padding: [48, 48], maxZoom: 17 });
    if (openPopup) {
      layer.openPopup();
    }
    if (window.matchMedia("(max-width: 760px)").matches) {
      document.getElementById("sidebar").classList.remove("open");
    }
  }

  var fieldsEl = document.getElementById("fields");
  var currentFarm = null;
  geojson.features.forEach(function (feature) {
    var props = feature.properties;
    if (props.farm_slug !== currentFarm) {
      currentFarm = props.farm_slug;
      var group = document.createElement("div");
      group.className = "farm-group";
      group.textContent = props.farm_name;
      fieldsEl.appendChild(group);
    }
    var row = document.createElement("button");
    row.type = "button";
    row.className = "field-row";
    row.setAttribute("data-field-id", props.field_id);
    row.innerHTML =
      '<span class="swatch" style="background:' + escapeHtml(colorByField[props.field_id]) + '"></span>' +
      '<span class="field-text"><span class="field-name">' + escapeHtml(props.field_name) +
      '</span><span class="field-meta">' + props.area_acres.toLocaleString() + " acres" +
      (props.last_crop_name ? " \\u00b7 " + escapeHtml(props.last_crop_name) : "") +
      "</span></span>";
    row.addEventListener("click", function () { focusField(props.field_id, true); });
    fieldsEl.appendChild(row);
  });

  document.getElementById("reset-view").addEventListener("click", function () {
    activeId = null;
    applyStyles();
    highlightRow(null);
    map.closePopup();
    map.fitBounds(fullBounds, { padding: [28, 28] });
  });

  var toggle = document.getElementById("sidebar-toggle");
  toggle.addEventListener("click", function () {
    document.getElementById("sidebar").classList.toggle("open");
  });

  if (window.matchMedia("(max-width: 760px)").matches) {
    document.getElementById("sidebar").classList.remove("open");
  }
})();
</script>
</body>
</html>
"""


def _render_html(payload: dict[str, Any]) -> str:
    render_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    data_json = json.dumps(render_payload, separators=(",", ":")).replace("</", "<\\/")
    html = (
        _HTML_TEMPLATE.replace("{{TITLE}}", payload["meta"]["title"])
        .replace("{{LEAFLET_CSS}}", LEAFLET_CSS_URL)
        .replace("{{LEAFLET_JS}}", LEAFLET_JS_URL)
        .replace("{{DATA_JSON}}", data_json)
    )
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        raise WebMapError(
            f"generated HTML is {size} bytes, above the {MAX_HTML_BYTES} byte budget; "
            "reduce geometry or split the grower map"
        )
    return html


def _write_outputs(
    *,
    pipeline_root: Path,
    payload: dict[str, Any],
    html: str,
    out_override: Path | None,
) -> tuple[Path, Path]:
    grower_slug = payload["meta"]["grower_slug"]
    if out_override is not None:
        html_path = out_override
        manifest_path = out_override.with_suffix(".manifest.json")
    else:
        reports_dir = pipeline_root / "growers" / grower_slug / "derived" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        html_path = reports_dir / f"{grower_slug}_fields_map.html"
        manifest_dir = pipeline_root / "growers" / grower_slug / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "grower_web_map.json"

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")

    try:
        output_rel = html_path.resolve().relative_to(pipeline_root).as_posix()
    except ValueError:
        output_rel = str(html_path)

    farms = payload["_farms"]
    manifest = {
        "grower_slug": grower_slug,
        "grower_name": payload["meta"]["grower_name"],
        "generated_at": payload["meta"]["generated_at"],
        "generator": "data-pipeline/src/scripts/reporting/generate_grower_web_map.py",
        "generator_sha256": _sha256(_SCRIPT_PATH),
        "leaflet_version": LEAFLET_VERSION,
        "basemap": {
            "name": payload["meta"]["basemap"]["name"],
            "url": BASEMAP_URL,
            "attribution": BASEMAP_ATTRIBUTION,
        },
        "farms": [
            {
                "farm_slug": farm["farm_slug"],
                "farm_name": farm["farm_name"],
                "boundary_path": farm["boundary_path"]
                .resolve()
                .relative_to(pipeline_root)
                .as_posix(),
                "boundary_sha256": farm["boundary_sha256"],
                "field_count": farm["field_count"],
                "acres": farm["acres"],
                "cdl_year": farm["cdl_year"],
            }
            for farm in farms
        ],
        "fields": [
            {
                "field_id": feature["properties"]["field_id"],
                "field_name": feature["properties"]["field_name"],
                "farm_slug": feature["properties"]["farm_slug"],
                "area_acres": feature["properties"]["area_acres"],
                "last_crop_name": feature["properties"]["last_crop_name"],
                "last_crop_year": feature["properties"]["last_crop_year"],
            }
            for feature in payload["geojson"]["features"]
        ],
        "output": {
            "path": output_rel,
            "bytes": len(html.encode("utf-8")),
            "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return html_path, manifest_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a lightweight interactive field map (HTML) for a grower, "
            "using the pipeline's field boundary polygons."
        )
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--grower", help="grower slug, e.g. illinois-grower")
    target.add_argument(
        "--all-growers",
        action="store_true",
        help="build one map for every grower with field boundaries",
    )
    parser.add_argument(
        "--farm",
        action="append",
        default=None,
        help="farm slug to include; repeatable; defaults to every farm",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output HTML path (single grower only); defaults to the runtime reports tree",
    )
    parser.add_argument("--title", default=None, help="override the map title")
    parser.add_argument(
        "--root",
        default=None,
        help="runtime data root (default: DATA_PIPELINE_DATA_ROOT)",
    )
    parser.add_argument(
        "--no-simplify",
        action="store_true",
        help="keep every vertex even for very large polygons",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print output paths",
    )
    return parser.parse_args(argv)


def _log(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        pipeline_root = _pipeline_root(args.root)
        if args.all_growers:
            if args.out:
                raise WebMapError("--out cannot be combined with --all-growers")
            grower_slugs = _discover_growers(pipeline_root)
            if not grower_slugs:
                raise WebMapError(f"no growers found under {pipeline_root / 'growers'}")
        else:
            grower_slugs = [_safe_slug(args.grower, "grower_slug")]

        failures: list[str] = []
        built = 0
        for grower_slug in grower_slugs:
            try:
                payload = _build_payload(
                    pipeline_root,
                    grower_slug,
                    args.farm,
                    title=args.title,
                    simplify=not args.no_simplify,
                )
                html = _render_html(payload)
                out_override = Path(args.out).expanduser() if args.out else None
                html_path, manifest_path = _write_outputs(
                    pipeline_root=pipeline_root,
                    payload=payload,
                    html=html,
                    out_override=out_override,
                )
                built += 1
                size_kb = html_path.stat().st_size / 1024
                _log(
                    f"{grower_slug}: {payload['meta']['field_count']} fields, "
                    f"{payload['meta']['acres']} acres -> {html_path} "
                    f"({size_kb:.1f} KB)",
                    quiet=args.quiet,
                )
                _log(f"  manifest: {manifest_path}", quiet=args.quiet)
            except WebMapError as exc:
                if args.all_growers:
                    print(f"SKIP {grower_slug}: {exc}", file=sys.stderr, flush=True)
                else:
                    failures.append(f"{grower_slug}: {exc}")
                    print(f"ERROR {grower_slug}: {exc}", file=sys.stderr, flush=True)
        if failures or (args.all_growers and built == 0):
            if args.all_growers and built == 0:
                print("ERROR: no grower produced a map", file=sys.stderr, flush=True)
            return 1
        return 0
    except WebMapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
