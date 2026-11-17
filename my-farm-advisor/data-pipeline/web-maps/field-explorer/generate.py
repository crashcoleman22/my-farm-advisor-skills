#!/usr/bin/env python3
"""Build one interactive field explorer for several growers.

The explorer is a static HTML page plus sibling data files. It reuses the
Assignment 1 grower web-map helpers for field discovery, geometry preparation,
CDL parsing, basemap constants and manifest conventions, and adds three layers
that the grower map deliberately omits:

* USDA CDL crop history for every requested year,
* SSURGO map-unit polygons clipped to each field, enriched from USDA SDA,
* Sentinel-2 L2A NDVI scenes (cloud-masked with the SCL band).

Rasters are never embedded in the HTML. Each NDVI scene becomes a small PNG
overlay in the runtime output directory, and the page fetches it on demand.
Field averages are computed from the same cloud-masked pixels and are shown
separately from the raster so an average is never mistaken for imagery.

Run from the skill checkout with the runtime virtualenv:

    python my-farm-advisor/data-pipeline/web-maps/field-explorer/generate.py \
        --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
        --root ~/my-farm-advisor-runtime

Generated outputs stay under the runtime root:

    ${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/web-maps/field-explorer/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WEB_DIR = Path(__file__).resolve().parent
DATA_PIPELINE_DIR = WEB_DIR.parents[1]
REPORTING_DIR = DATA_PIPELINE_DIR / "src" / "scripts" / "reporting"
LIB_DIR = DATA_PIPELINE_DIR / "src" / "scripts" / "lib"
for _path in (REPORTING_DIR, LIB_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import generate_grower_web_map as a1  # noqa: E402

STATE_NAMES = {
    "IL": "Illinois",
    "IA": "Iowa",
    "NE": "Nebraska",
}
# Presentation order follows the request (Illinois, Iowa, Nebraska) rather than
# alphabetical state codes.
STATE_ORDER = {"IL": 0, "IA": 1, "NE": 2}
# Outlines carry the grower/state. Nebraska uses purple instead of the EDA
# report's green so it cannot be confused with the soybean fill colour.
GROWER_COLORS = {"IL": "#2f6fb0", "IA": "#d9772f", "NE": "#7a4fa3"}
CROP_COLORS = {
    "Corn": "#e3b23c",
    "Soybeans": "#4f9d69",
    "Other": "#8c6d46",
    "Not classified": "#b9c0cb",
}
CROP_ORDER = ("Corn", "Soybeans", "Other", "Not classified")

SENTINEL_CLOUD_CLASSES = (3, 8, 9, 10, 11)
SENTINEL_RESOLUTION_M = 10
MAX_HTML_BYTES = 2_000_000
NCCPI_RULE = "NCCPI - National Commodity Crop Productivity Index (Ver 3.0)"
SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
ACRES_PER_SQ_M = 1.0 / 4046.8564224

# Discrete NDVI palette. Discrete colours keep the PNG overlays small and give
# the browser a legend that is easy to read.
NDVI_BINS = (-1.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01)
NDVI_COLORS = (
    (140, 81, 10),
    (191, 129, 45),
    (223, 194, 125),
    (246, 232, 195),
    (199, 234, 229),
    (128, 205, 193),
    (53, 151, 143),
    (1, 102, 94),
    (27, 120, 55),
    (0, 68, 27),
)

_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class ExplorerError(RuntimeError):
    """Raised when the explorer cannot be built from the runtime data."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_component(value: str) -> str:
    cleaned = _UNSAFE_SLUG.sub("-", str(value)).strip("-")
    if not cleaned:
        raise ExplorerError(f"unsafe empty path component: {value!r}")
    return cleaned


def round_coords(node: Any, decimals: int = 6) -> Any:
    if isinstance(node, (int, float)):
        return round(float(node), decimals)
    if isinstance(node, (list, tuple)):
        return [round_coords(item, decimals) for item in node]
    return node


def geometry_bbox(geometry: dict[str, Any]) -> list[float]:
    xs: list[float] = []
    ys: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, (list, tuple)) and node:
            if isinstance(node[0], (int, float)):
                xs.append(float(node[0]))
                ys.append(float(node[1]))
                return
            for item in node:
                walk(item)

    walk(geometry.get("coordinates"))
    return [min(xs), min(ys), max(xs), max(ys)]


def area_acres(geometry: Any) -> float:
    """Equal-area acreage for a shapely geometry in EPSG:4326."""
    try:
        from pyproj import Transformer
    except ImportError:  # pragma: no cover - runtime venv always has pyproj
        return 0.0
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    try:
        import numpy as np
        from shapely import transform as shapely_transform

        def project(coords: Any) -> Any:
            x, y = transformer.transform(coords[:, 0], coords[:, 1])
            return np.column_stack([x, y])

        projected = shapely_transform(geometry, project)
    except ImportError:  # pragma: no cover - Shapely < 2.0
        from shapely.ops import transform as ops_transform

        projected = ops_transform(transformer.transform, geometry)
    return float(projected.area) * ACRES_PER_SQ_M


# --------------------------------------------------------------------------- #
# discovery and field records
# --------------------------------------------------------------------------- #


def resolve_pipeline_root(raw_root: str | None) -> Path:
    root = a1._pipeline_root(raw_root)
    if not root.is_dir():
        raise ExplorerError(f"runtime data-pipeline directory not found: {root}")
    return root


def discover_growers(pipeline_root: Path, requested: list[str]) -> list[str]:
    if requested:
        growers = []
        for slug in requested:
            a1._safe_slug(slug, "grower")
            if not (pipeline_root / "growers" / slug).is_dir():
                raise ExplorerError(f"grower not found in runtime: {slug}")
            growers.append(slug)
        return growers
    growers = a1._discover_growers(pipeline_root)
    if not growers:
        raise ExplorerError("no growers with grower.json found in the runtime")
    return growers


def discover_farms(pipeline_root: Path, growers: Iterable[str]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for grower_slug in growers:
        grower_dir = pipeline_root / "growers" / grower_slug
        grower_meta = a1._read_json(grower_dir / "grower.json", {})
        grower_name = a1._display_name(grower_meta, grower_slug)
        for farm_slug in a1._discover_farms(pipeline_root, grower_slug):
            farm_dir = grower_dir / "farms" / farm_slug
            farm_meta = a1._read_json(farm_dir / "farm.json", {})
            targets.append(
                {
                    "grower_slug": grower_slug,
                    "grower_name": grower_name,
                    "farm_slug": farm_slug,
                    "farm_name": a1._display_name(farm_meta, farm_slug),
                    "state": str(farm_meta.get("state") or "").strip(),
                    "farm_dir": farm_dir,
                    "boundary_path": farm_dir / "boundary" / "field_boundaries.geojson",
                }
            )
    return targets


def load_fields(targets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for target in targets:
        payload = a1._read_json(target["boundary_path"], {})
        if not isinstance(payload, dict):
            continue
        for feature in payload.get("features") or []:
            geometry = feature.get("geometry")
            props = feature.get("properties") or {}
            field_id = str(props.get("field_id") or "").strip()
            if not isinstance(geometry, dict) or not field_id:
                continue
            field_dir = target["farm_dir"] / "fields" / field_id
            field_meta = a1._read_json(field_dir / "field.json", {})
            field_name = a1._display_name(field_meta, field_id)
            state = str(props.get("state") or target["state"] or "").strip()
            try:
                acres = float(props.get("area_acres") or 0.0)
            except (TypeError, ValueError):
                acres = 0.0
            records.append(
                {
                    "field_id": field_id,
                    "field_name": field_name,
                    "field_dir": field_dir,
                    "farm_dir": target["farm_dir"],
                    "grower_slug": target["grower_slug"],
                    "grower_name": target["grower_name"],
                    "farm_slug": target["farm_slug"],
                    "farm_name": target["farm_name"],
                    "state": state,
                    "county": str(props.get("county_name") or "").strip(),
                    "acres": round(acres, 1),
                    "source": str(props.get("source") or "").strip(),
                    "geometry": a1._prepare_geometry(geometry, simplify=False),
                    "bbox": geometry_bbox(geometry),
                }
            )
    if not records:
        raise ExplorerError("no field boundaries found for the requested growers")
    records.sort(key=lambda item: (item["state"], -item["acres"], item["field_id"]))
    return records


def select_fields(
    fields: list[dict[str, Any]], limit_per_state: int | None
) -> list[dict[str, Any]]:
    """Deterministically keep the first N fields per state for smoke runs."""
    if not limit_per_state or limit_per_state <= 0:
        return list(fields)
    seen: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    for field in sorted(fields, key=lambda item: (item["state"], item["field_id"])):
        state = field["state"]
        if seen.get(state, 0) >= limit_per_state:
            continue
        seen[state] = seen.get(state, 0) + 1
        selected.append(field)
    return selected


# --------------------------------------------------------------------------- #
# CDL
# --------------------------------------------------------------------------- #


def load_cdl_by_field(fields: Iterable[dict[str, Any]], years: list[int]) -> dict[str, dict]:
    result: dict[str, dict[int, dict[str, Any]]] = {}
    farm_dirs = {str(field["farm_dir"]) for field in fields}
    for farm_dir_raw in farm_dirs:
        farm_dir = Path(farm_dir_raw)
        tables = a1._cdl_tables(farm_dir)
        for year in years:
            table = tables.get(year)
            if table is None:
                continue
            per_field: dict[str, dict[str, float]] = {}
            with table.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    field_id = (row.get("field_id") or "").strip()
                    crop = (row.get("crop_name") or "").strip()
                    if not field_id or not crop:
                        continue
                    try:
                        pct = float(row.get("pct") or 0.0)
                    except ValueError:
                        pct = 0.0
                    if pct <= 0:
                        continue
                    crops = per_field.setdefault(field_id, {})
                    crops[crop] = crops.get(crop, 0.0) + pct
            for field_id, crops in per_field.items():
                ranked = sorted(crops.items(), key=lambda item: item[1], reverse=True)
                if not ranked:
                    continue
                total = sum(crops.values())
                result.setdefault(field_id, {})[year] = {
                    "crop": ranked[0][0],
                    "pct": round(ranked[0][1], 1),
                    "classified_pct": round(min(total, 100.0), 1),
                    "top": [
                        {"crop": crop, "pct": round(pct, 1)} for crop, pct in ranked[:3]
                    ],
                    "mixed": bool(ranked[0][1] < 60.0 and len(ranked) > 1),
                }
    return result


# --------------------------------------------------------------------------- #
# weather
# --------------------------------------------------------------------------- #


def _weather_row_from_csv(row: dict[str, str]) -> dict[str, Any] | None:
    try:
        year = int(float(row.get("year") or 0))
    except ValueError:
        return None
    if year <= 0:
        return None

    def num(key: str) -> float | None:
        raw = row.get(key)
        if raw in (None, "", "nan", "NaN"):
            return None
        try:
            return round(float(raw), 2)
        except ValueError:
            return None

    return {
        "year": year,
        "precip_mm": num("gs_precip_mm"),
        "gdd": num("gs_gdd"),
        "heat_days": num("heat_days_35c"),
        "frost_free_days": num("frost_free_days"),
        "dry_spell_days": num("max_dry_spell_days"),
        "mean_temp_c": num("gs_mean_temp_c"),
    }


def load_weather(
    pipeline_root: Path,
    fields: list[dict[str, Any]],
    years: list[int],
) -> tuple[dict[str, dict[int, dict[str, Any]]], str]:
    table = pipeline_root / "shared" / "eda" / "field-level" / "tables" / "weather_seasonal_metrics.csv"
    by_field: dict[str, dict[int, dict[str, Any]]] = {}
    source = "eda_table"
    if table.is_file():
        with table.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                field_id = (row.get("field_id") or "").strip()
                record = _weather_row_from_csv(row)
                if field_id and record and record["year"] in years:
                    by_field.setdefault(field_id, {})[record["year"]] = record
        return by_field, source

    source = "daily_csv"
    try:
        import pandas as pd

        from eda_metrics import weather_season_metrics
    except ImportError as exc:  # pragma: no cover - runtime venv always has these
        raise ExplorerError(
            "weather_seasonal_metrics.csv is missing and pandas/eda_metrics are "
            f"unavailable: {exc}"
        ) from exc

    frames = []
    for field in fields:
        daily = field["field_dir"] / "weather" / "daily_weather.csv"
        if not daily.is_file():
            continue
        frame = pd.read_csv(daily)
        frame["field_id"] = field["field_id"]
        frames.append(frame)
    if not frames:
        return {}, source
    metrics = weather_season_metrics(pd.concat(frames, ignore_index=True))
    for row in metrics.to_dict("records"):
        field_id = str(row.get("field_id"))
        year = int(row.get("year"))
        if year not in years:
            continue
        by_field.setdefault(field_id, {})[year] = {
            "year": year,
            "precip_mm": round(float(row["gs_precip_mm"]), 2),
            "gdd": round(float(row["gs_gdd"]), 2),
            "heat_days": int(row["heat_days_35c"]),
            "frost_free_days": None
            if row.get("frost_free_days") is None
            else round(float(row["frost_free_days"]), 1),
            "dry_spell_days": int(row["max_dry_spell_days"]),
            "mean_temp_c": round(float(row["gs_mean_temp_c"]), 2),
        }
    return by_field, source


def annotate_shared_weather(
    fields: list[dict[str, Any]],
    weather_by_field: dict[str, dict[int, dict[str, Any]]],
    years: list[int],
) -> None:
    """Flag fields whose five-year weather vectors are identical.

    Identical vectors mean the fields fall in one NASA POWER grid cell. The
    map states that explicitly instead of presenting the values as independent
    microclimate measurements.
    """
    groups: dict[tuple, list[str]] = {}
    for field in fields:
        rows = weather_by_field.get(field["field_id"], {})
        signature = tuple(
            (
                year,
                None if year not in rows else rows[year].get("precip_mm"),
                None if year not in rows else rows[year].get("gdd"),
                None if year not in rows else rows[year].get("frost_free_days"),
            )
            for year in years
        )
        groups.setdefault(signature, []).append(field["field_id"])
    counts = {
        field_id: len(group) for group in groups.values() for field_id in group
    }
    for field in fields:
        field["weather_shared_with"] = counts.get(field["field_id"], 1)


# --------------------------------------------------------------------------- #
# SSURGO
# --------------------------------------------------------------------------- #


def _sda_query(sql: str, *, timeout: int = 120) -> list[list[Any]]:
    import requests

    response = requests.post(
        SDA_URL, data={"query": sql, "format": "JSON"}, timeout=timeout
    )
    response.raise_for_status()
    return response.json().get("Table", [])


def _chunks(values: list[str], size: int = 200) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fetch_ssurgo_attributes(
    mukeys: Iterable[str],
    cache_path: Path,
    *,
    offline: bool = False,
    refresh: bool = False,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return map-unit attributes keyed by mukey, caching the SDA response.

    The cache is a runtime artifact. If USDA SDA is unreachable the map still
    renders the clipped polygons and shows the mukey, with properties marked
    unavailable.
    """
    wanted = sorted({str(mukey) for mukey in mukeys if str(mukey).strip()})
    if not wanted:
        return {}, "no-mukeys"

    if cache_path.is_file() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            attributes = cached.get("attributes") or {}
            if set(wanted).issubset(attributes):
                return attributes, "cache"
        except (OSError, json.JSONDecodeError):
            pass

    if offline:
        return {}, "offline-no-cache"

    attributes: dict[str, dict[str, Any]] = {}
    try:
        for chunk in _chunks(wanted):
            quoted = ",".join(repr(mukey) for mukey in chunk)
            rows = _sda_query(
                "SELECT mu.mukey, mu.musym, mu.muname, "
                "a.slopegraddcp, a.slopegradwta, a.drclassdcd, "
                "a.hydgrpdcd, a.aws050wta "
                "FROM mapunit mu LEFT JOIN muaggatt a ON mu.mukey = a.mukey "
                f"WHERE mu.mukey IN ({quoted})"
            )
            for row in rows:
                if len(row) < 8:
                    continue
                mukey = str(row[0])
                attributes[mukey] = {
                    "musym": _clean_text(row[1]),
                    "muname": _clean_text(row[2]),
                    "slope_dominant_pct": _clean_float(row[3]),
                    "slope_weighted_pct": _clean_float(row[4]),
                    "drainage": _clean_text(row[5]),
                    "hydrologic_group": _clean_text(row[6]),
                    "aws_050cm": _clean_float(row[7]),
                    "nccpi": None,
                    "nccpi_class": None,
                }

            nccpi_rows = _sda_query(
                "SELECT c.mukey, c.comppct_r, ci.interphr, ci.interphrc "
                "FROM component c JOIN cointerp ci ON c.cokey = ci.cokey "
                f"WHERE c.mukey IN ({quoted}) AND ci.rulename = {NCCPI_RULE!r}"
            )
            ratings: dict[str, list[tuple[float, float, str | None]]] = {}
            for row in nccpi_rows:
                if len(row) < 4:
                    continue
                mukey = str(row[0])
                weight = _clean_float(row[1])
                value = _clean_float(row[2])
                if weight is None or value is None or weight <= 0:
                    continue
                ratings.setdefault(mukey, []).append(
                    (float(weight), float(value), _clean_text(row[3]))
                )
            for mukey, rows_for_unit in ratings.items():
                total_weight = sum(item[0] for item in rows_for_unit)
                if total_weight <= 0:
                    continue
                weighted = sum(item[0] * item[1] for item in rows_for_unit) / total_weight
                dominant = max(rows_for_unit, key=lambda item: item[0])
                unit = attributes.setdefault(mukey, {})
                unit["nccpi"] = round(weighted, 3)
                unit["nccpi_class"] = dominant[2]
    except Exception as exc:  # network, SDA schema or query failure
        print(f"warn  SSURGO attributes unavailable from USDA SDA: {exc}")
        return attributes or {}, f"unavailable: {exc.__class__.__name__}"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "source": SDA_URL,
                "retrieved_at": utc_now(),
                "mukeys": wanted,
                "attributes": attributes,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return attributes, "usda-sda"


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    return text


def _clean_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def load_soil_units(
    field: dict[str, Any],
    attributes: dict[str, dict[str, Any]],
    *,
    decimals: int = 6,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (inspector rows, GeoJSON features) for one field's map units."""
    path = field["field_dir"] / "soil" / "ssurgo_soil_types.geojson"
    if not path.is_file():
        return [], []
    payload = a1._read_json(path, {})
    if not isinstance(payload, dict):
        return [], []

    try:
        from shapely.geometry import shape
    except ImportError:  # pragma: no cover
        shape = None

    field_acres = float(field.get("acres") or 0.0)
    units: dict[str, dict[str, Any]] = {}
    features: list[dict[str, Any]] = []
    for feature in payload.get("features") or []:
        geometry = feature.get("geometry")
        props = feature.get("properties") or {}
        if not isinstance(geometry, dict):
            continue
        mukey = str(props.get("mukey") or "").strip()
        acres = 0.0
        if shape is not None:
            try:
                acres = area_acres(shape(geometry))
            except Exception:
                acres = 0.0
        unit = units.setdefault(
            mukey, {"mukey": mukey, "acres": 0.0}
        )
        unit["acres"] += acres
        # Keep one GeoJSON feature per polygon so the map still shows every
        # disjoint piece, but merge them into one inspector row per map unit.
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": geometry.get("type", "Polygon"),
                    "coordinates": round_coords(geometry.get("coordinates"), decimals),
                },
                "properties": {"field_id": field["field_id"], "mukey": mukey},
            }
        )

    rows: list[dict[str, Any]] = []
    for mukey, unit in units.items():
        acres = float(unit["acres"])
        pct = (acres / field_acres * 100.0) if field_acres > 0 else 0.0
        attrs = attributes.get(mukey, {})
        rows.append(
            {
                "mukey": mukey,
                "musym": attrs.get("musym"),
                "muname": attrs.get("muname"),
                "acres": round(acres, 1),
                "pct": round(min(pct, 100.0), 1),
                "slope_pct": attrs.get("slope_weighted_pct"),
                "drainage": attrs.get("drainage"),
                "hydrologic_group": attrs.get("hydrologic_group"),
                "aws_050cm": attrs.get("aws_050cm"),
                "nccpi": attrs.get("nccpi"),
                "nccpi_class": attrs.get("nccpi_class"),
                "attributes_available": bool(attrs),
            }
        )
    rows.sort(key=lambda item: item["acres"], reverse=True)
    return rows, features


# --------------------------------------------------------------------------- #
# Sentinel-2 NDVI
# --------------------------------------------------------------------------- #


def _ndvi_bounds(dataset, transform, height: int, width: int) -> list[list[float]]:
    from rasterio.warp import transform_bounds
    from rasterio.transform import array_bounds

    left, bottom, right, top = array_bounds(height, width, transform)
    if dataset.crs is not None and str(dataset.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
        left, bottom, right, top = transform_bounds(
            dataset.crs, "EPSG:4326", left, bottom, right, top, densify_pts=21
        )
    return [[bottom, left], [top, right]]


def ndvi_scene_stats(
    values: Any,
    *,
    field_mask: Any | None = None,
    min_valid_fraction: float = 0.05,
) -> dict[str, Any]:
    """Summarize a cloud-masked NDVI array.

    ``values`` is a float array where NaN means cloud, shadow, snow or nodata.
    ``field_mask`` is an optional boolean array marking pixels whose centre falls
    inside the field boundary. Without it the denominator would be the raster
    bounding box, which understates coverage because a field polygon never fills
    its own rectangle. The function is deliberately pure so tests can exercise
    missing pixels without touching a raster.
    """
    import numpy as np

    array = np.asarray(values, dtype="float32")
    valid = np.isfinite(array)
    if field_mask is not None:
        mask = np.asarray(field_mask, dtype=bool)
        if mask.shape == array.shape:
            valid = valid & mask
            total = int(mask.sum())
        else:  # pragma: no cover - defensive, shape always matches
            total = int(array.size)
    else:
        total = int(array.size)
    count = int(valid.sum())
    fraction = (count / total) if total else 0.0
    if count == 0:
        return {
            "valid_pixels": 0,
            "total_pixels": total,
            "valid_fraction": 0.0,
            "mean": None,
            "p10": None,
            "p50": None,
            "p90": None,
            "low_coverage": True,
            "usable": False,
        }
    sample = array[valid]
    p10, p50, p90 = np.percentile(sample, [10, 50, 90])
    return {
        "valid_pixels": count,
        "total_pixels": total,
        "valid_fraction": round(fraction, 3),
        "mean": round(float(sample.mean()), 3),
        "p10": round(float(p10), 3),
        "p50": round(float(p50), 3),
        "p90": round(float(p90), 3),
        "low_coverage": fraction < 0.35,
        "usable": fraction >= min_valid_fraction,
    }


def _write_ndvi_png(values: Any, path: Path) -> None:
    import numpy as np
    from PIL import Image

    array = np.asarray(values, dtype="float32")
    valid = np.isfinite(array)
    filled = np.where(valid, array, NDVI_BINS[0])
    indices = np.digitize(filled, NDVI_BINS) - 1
    indices = np.clip(indices, 0, len(NDVI_COLORS) - 1)
    rgba = np.zeros((array.shape[0], array.shape[1], 4), dtype="uint8")
    palette = np.asarray(NDVI_COLORS, dtype="uint8")
    rgba[..., :3] = palette[indices]
    rgba[..., 3] = np.where(valid, 235, 0).astype("uint8")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path, format="PNG", optimize=True)


def _scene_ndvi_path(
    field: dict[str, Any],
    year: int,
    scene: dict[str, Any],
    pipeline_root: Path,
) -> Path | None:
    raw = scene.get("ndvi_tif")
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            # Manifest paths are relative to the runtime data-pipeline root,
            # not to the skill checkout that holds this generator.
            candidate = pipeline_root / candidate
        if candidate.is_file():
            return candidate
    date = str(scene.get("scene_date") or "").replace("-", "")
    if not date:
        return None
    candidate = (
        field["field_dir"]
        / "satellite"
        / "sentinel"
        / str(year)
        / f"sentinel_{date}"
        / f"sentinel_{date}_ndvi.tif"
    )
    return candidate if candidate.is_file() else None


def build_ndvi_assets(
    field: dict[str, Any],
    output_dir: Path,
    pipeline_root: Path,
    *,
    start_year: int,
    end_year: int,
    force: bool = False,
    min_valid_fraction: float = 0.05,
) -> dict[str, Any]:
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.mask import mask as raster_mask
    from shapely.geometry import mapping, shape

    manifest_path = field["field_dir"] / "satellite" / "sentinel" / "manifest.json"
    if not manifest_path.is_file():
        return {"available": False, "years": {}, "excluded": 0, "reason": "no Sentinel-2 manifest"}
    manifest = a1._read_json(manifest_path, {})
    if not isinstance(manifest, dict):
        return {"available": False, "years": {}, "excluded": 0, "reason": "unreadable Sentinel-2 manifest"}

    geometry = shape(
        {"type": field["geometry"]["type"], "coordinates": field["geometry"]["coordinates"]}
    )
    years_out: dict[str, list[dict[str, Any]]] = {}
    excluded = 0
    considered = 0
    for year_bucket in manifest.get("years") or []:
        try:
            year = int(year_bucket.get("year"))
        except (TypeError, ValueError):
            continue
        if year < start_year or year > end_year:
            continue
        for scene in year_bucket.get("scenes") or []:
            date = str(scene.get("scene_date") or "").strip()
            tif_path = _scene_ndvi_path(field, year, scene, pipeline_root)
            if not date or tif_path is None:
                excluded += 1
                continue
            considered += 1
            try:
                with rasterio.open(tif_path) as dataset:
                    geometry_in_crs = geometry
                    if dataset.crs is not None and str(dataset.crs).upper() not in {
                        "EPSG:4326",
                        "OGC:CRS84",
                    }:
                        from rasterio.warp import transform_geom

                        geometry_in_crs = shape(
                            transform_geom("EPSG:4326", dataset.crs, mapping(geometry))
                        )
                    masked, transform = raster_mask(
                        dataset,
                        [mapping(geometry_in_crs)],
                        crop=True,
                        all_touched=False,
                        filled=False,
                    )
                    values = np.ma.filled(masked[0], np.nan).astype("float32")
                    height, width = values.shape
                    # Coverage is measured against field pixels, not the crop
                    # rectangle, so a field that fills half its bbox is not
                    # reported as half cloud-covered.
                    field_pixels = geometry_mask(
                        [mapping(geometry_in_crs)],
                        out_shape=(height, width),
                        transform=transform,
                        invert=True,
                        all_touched=False,
                    )
                    values = np.where(field_pixels, values, np.nan).astype("float32")
                    pixels = ndvi_scene_stats(
                        values,
                        field_mask=field_pixels,
                        min_valid_fraction=min_valid_fraction,
                    )
                    if not pixels["usable"]:
                        excluded += 1
                        continue
                    png_rel = (
                        Path("ndvi")
                        / safe_component(field["field_id"])
                        / f"{date}.png"
                    )
                    png_path = output_dir / png_rel
                    if force or not png_path.is_file():
                        _write_ndvi_png(values, png_path)
                    bounds = _ndvi_bounds(dataset, transform, height, width)
            except Exception as exc:
                print(
                    f"warn  NDVI scene skipped for {field['field_id']} {date}: {exc}"
                )
                excluded += 1
                continue

            years_out.setdefault(str(year), []).append(
                {
                    "date": date,
                    "year": year,
                    "scene_id": scene.get("scene_id"),
                    "cloud_cover": scene.get("cloud_cover"),
                    "mean": pixels["mean"],
                    "p10": pixels["p10"],
                    "p50": pixels["p50"],
                    "p90": pixels["p90"],
                    "valid_fraction": pixels["valid_fraction"],
                    "valid_pixels": pixels["valid_pixels"],
                    "low_coverage": pixels["low_coverage"],
                    "png": png_rel.as_posix(),
                    "bounds": bounds,
                    "resolution_m": SENTINEL_RESOLUTION_M,
                }
            )

    for scenes in years_out.values():
        scenes.sort(key=lambda item: item["date"])

    return {
        "available": bool(years_out),
        "years": years_out,
        "excluded": excluded,
        "considered": considered,
        "reason": None if years_out else "no usable cloud-free scenes",
    }


# --------------------------------------------------------------------------- #
# payload and HTML
# --------------------------------------------------------------------------- #


def _state_summary(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    codes = sorted(
        {field["state"] for field in fields if field["state"]},
        key=lambda code: (STATE_ORDER.get(code, 99), code),
    )
    for code in codes:
        subset = [field for field in fields if field["state"] == code]
        xs = [field["bbox"][0] for field in subset] + [field["bbox"][2] for field in subset]
        ys = [field["bbox"][1] for field in subset] + [field["bbox"][3] for field in subset]
        summary.append(
            {
                "code": code,
                "name": STATE_NAMES.get(code, code),
                "count": len(subset),
                "acres": round(sum(field["acres"] for field in subset), 1),
                "bbox": [[min(ys), min(xs)], [max(ys), max(xs)]],
            }
        )
    return summary


def build_payload(
    *,
    title: str,
    fields: list[dict[str, Any]],
    years: list[int],
    weather_source: str,
    soil_attribute_status: str,
    ndvi_meta: dict[str, Any],
    notes: list[str],
) -> dict[str, Any]:
    states = _state_summary(fields)
    features = [
        {
            "type": "Feature",
            "geometry": field["geometry"],
            "properties": {
                "field_id": field["field_id"],
                "field_name": field["field_name"],
                "state": field["state"],
                "acres": field["acres"],
            },
        }
        for field in fields
    ]
    return {
        "meta": {
            "title": title,
            "generated_at": utc_now(),
            "start_year": years[0],
            "end_year": years[-1],
            "years": years,
            "field_count": len(fields),
            "states": states,
            "grower_colors": GROWER_COLORS,
            "crop_colors": CROP_COLORS,
            "crop_order": list(CROP_ORDER),
            "weather_source": weather_source,
            "soil_attribute_status": soil_attribute_status,
            "ndvi": ndvi_meta,
            "notes": notes,
        },
        "fields": [
            {
                "field_id": field["field_id"],
                "field_name": field["field_name"],
                "grower_slug": field["grower_slug"],
                "grower_name": field["grower_name"],
                "farm_slug": field["farm_slug"],
                "farm_name": field["farm_name"],
                "state": field["state"],
                "state_name": STATE_NAMES.get(field["state"], field["state"]),
                "county": field["county"],
                "acres": field["acres"],
                "source": field["source"],
                "weather_shared_with": field.get("weather_shared_with", 1),
                "cdl": field.get("cdl") or {},
                "weather": field.get("weather") or {},
                "soil": field.get("soil") or [],
                "ndvi": field.get("ndvi") or {},
            }
            for field in fields
        ],
        "geojson": {"type": "FeatureCollection", "features": features},
    }


def render_html(payload: dict[str, Any], assets_dir: Path) -> str:
    template = (assets_dir / "index.html").read_text(encoding="utf-8")
    style = (assets_dir / "style.css").read_text(encoding="utf-8")
    app_js = (assets_dir / "app.js").read_text(encoding="utf-8")
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return (
        template.replace("{{TITLE}}", payload["meta"]["title"])
        .replace("{{LEAFLET_CSS}}", a1.LEAFLET_CSS_URL)
        .replace("{{LEAFLET_JS}}", a1.LEAFLET_JS_URL)
        .replace("{{STYLE}}", style)
        .replace("{{DATA_JSON}}", data_json)
        .replace("{{APP_JS}}", app_js)
    )


def write_manifest(
    output_dir: Path,
    *,
    payload: dict[str, Any],
    inputs: dict[str, str],
    soil_features: int,
    ndvi_scenes: int,
    ndvi_pngs: int,
    storage: dict[str, int],
) -> Path:
    manifest = {
        "dataset": "field-explorer",
        "generated_at": payload["meta"]["generated_at"],
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": file_sha256(Path(__file__).resolve()),
        "field_count": payload["meta"]["field_count"],
        "states": payload["meta"]["states"],
        "years": payload["meta"]["years"],
        "weather_source": payload["meta"]["weather_source"],
        "soil": {
            "map_units": soil_features,
            "attribute_status": payload["meta"]["soil_attribute_status"],
            "source": "USDA NRCS Soil Data Access / SSURGO",
        },
        "ndvi": {
            **payload["meta"]["ndvi"],
            "scene_count": ndvi_scenes,
            "png_count": ndvi_pngs,
            "collection": "Sentinel-2 L2A via Microsoft Planetary Computer",
            "cloud_mask": {
                "band": "SCL",
                "masked_classes": list(SENTINEL_CLOUD_CLASSES),
                "note": "3=cloud shadow, 8/9/10=cloud, 11=snow",
            },
            "resolution_m": SENTINEL_RESOLUTION_M,
        },
        "storage_bytes": storage,
        "inputs": inputs,
    }
    path = output_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower", action="append", default=[], help="Grower slug (repeatable).")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--root", default=None, help="Runtime base (contains data-pipeline/).")
    parser.add_argument("--out", default=None, help="Output directory override.")
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--limit-per-state",
        type=int,
        default=0,
        help="Keep only the first N fields per state (smoke validation).",
    )
    parser.add_argument("--offline", action="store_true", help="Do not call USDA SDA.")
    parser.add_argument(
        "--refresh-soil-attributes",
        action="store_true",
        help="Ignore the cached SDA attribute table.",
    )
    parser.add_argument("--force-ndvi", action="store_true", help="Rewrite NDVI PNGs.")
    parser.add_argument("--skip-ndvi", action="store_true", help="Skip NDVI assets.")
    parser.add_argument("--min-valid-fraction", type=float, default=0.05)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()
    years = list(range(args.start_year, args.end_year + 1))
    pipeline_root = resolve_pipeline_root(args.root)
    growers = discover_growers(pipeline_root, args.grower)
    targets = discover_farms(pipeline_root, growers)
    fields = load_fields(targets)
    fields = select_fields(fields, args.limit_per_state)
    if not fields:
        raise ExplorerError("no fields selected")

    title = args.title or "Field explorer"
    output_dir = (
        Path(args.out).expanduser().resolve()
        if args.out
        else pipeline_root / "shared" / "web-maps" / "field-explorer"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        print(
            f"Loaded {len(fields)} fields from {len({f['state'] for f in fields})} "
            f"state(s) and {len({f['grower_slug'] for f in fields})} grower(s)"
        )

    cdl_by_field = load_cdl_by_field(fields, years)
    weather_by_field, weather_source = load_weather(pipeline_root, fields, years)
    annotate_shared_weather(fields, weather_by_field, years)
    for field in fields:
        field["cdl"] = cdl_by_field.get(field["field_id"], {})
        field["weather"] = weather_by_field.get(field["field_id"], {})

    all_mukeys: set[str] = set()
    for field in fields:
        soil_path = field["field_dir"] / "soil" / "ssurgo_soil_types.geojson"
        payload = a1._read_json(soil_path, {}) if soil_path.is_file() else {}
        for feature in payload.get("features") or []:
            mukey = str((feature.get("properties") or {}).get("mukey") or "").strip()
            if mukey:
                all_mukeys.add(mukey)
    attributes, soil_attribute_status = fetch_ssurgo_attributes(
        all_mukeys,
        output_dir / "soil_attributes.json",
        offline=args.offline,
        refresh=args.refresh_soil_attributes,
    )
    if not args.quiet:
        print(
            f"SSURGO: {len(all_mukeys)} map units, attributes={soil_attribute_status}"
        )

    soil_features: list[dict[str, Any]] = []
    for field in fields:
        rows, features = load_soil_units(field, attributes)
        field["soil"] = rows
        soil_features.extend(features)
    soil_geojson = {"type": "FeatureCollection", "features": soil_features}
    (output_dir / "soil_map_units.geojson").write_text(
        json.dumps(soil_geojson, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    ndvi_scene_total = 0
    ndvi_png_total = 0
    ndvi_excluded_total = 0
    for index, field in enumerate(fields, start=1):
        if args.skip_ndvi:
            field["ndvi"] = {"available": False, "years": {}, "reason": "skipped"}
            continue
        if not args.quiet:
            print(f"  NDVI {index}/{len(fields)} {field['field_id']}")
        ndvi = build_ndvi_assets(
            field,
            output_dir,
            pipeline_root,
            start_year=args.start_year,
            end_year=args.end_year,
            force=args.force_ndvi,
            min_valid_fraction=args.min_valid_fraction,
        )
        field["ndvi"] = ndvi
        ndvi_scene_total += int(ndvi.get("considered") or 0) - int(
            ndvi.get("excluded") or 0
        )
        ndvi_png_total += sum(len(scenes) for scenes in ndvi.get("years", {}).values())
        ndvi_excluded_total += int(ndvi.get("excluded") or 0)

    ndvi_meta = {
        "scene_count": ndvi_png_total,
        "excluded_scenes": ndvi_excluded_total,
        "min_valid_fraction": args.min_valid_fraction,
        "cloud_classes": list(SENTINEL_CLOUD_CLASSES),
        "resolution_m": SENTINEL_RESOLUTION_M,
        "years": years,
        "distinction": (
            "Raster overlays are observed cloud-masked Sentinel-2 pixels. "
            "The chart and summary values are field averages of those pixels; "
            "they are never rendered as imagery."
        ),
    }
    notes = [
        "Sentinel-2 L2A scenes are cloud-masked with the SCL band "
        f"(classes {', '.join(str(c) for c in SENTINEL_CLOUD_CLASSES)}).",
        "NDVI PNGs are observed pixels clipped to each field; field averages "
        "are calculated from the same valid pixels and shown separately.",
        "SSURGO map units come from USDA NRCS Soil Data Access and are clipped "
        "by the data pipeline to each field boundary.",
        "NCCPI is a component-rating weighted average from the SSURGO "
        "cointerp table; it is shown only where USDA rates the component.",
        "Weather is NASA POWER gridded data. Fields with identical five-year "
        "series share one grid cell and are flagged in the inspector.",
        "CDL crop shares are pixel percentages, not planted acres.",
    ]

    payload = build_payload(
        title=title,
        fields=fields,
        years=years,
        weather_source=weather_source,
        soil_attribute_status=soil_attribute_status,
        ndvi_meta=ndvi_meta,
        notes=notes,
    )
    html = render_html(payload, WEB_DIR / "assets")
    html_bytes = len(html.encode("utf-8"))
    if html_bytes > MAX_HTML_BYTES:
        raise ExplorerError(
            f"generated HTML is {html_bytes} bytes, above the "
            f"{MAX_HTML_BYTES} byte budget; reduce fields or split the explorer"
        )
    html_path = output_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")
    (output_dir / "ndvi_index.json").write_text(
        json.dumps(
            {
                field["field_id"]: field["ndvi"]
                for field in fields
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )

    inputs: dict[str, str] = {}
    for field in fields:
        inputs[str(field["field_dir"] / "boundary" / "field_boundary.geojson")] = (
            file_sha256(field["field_dir"] / "boundary" / "field_boundary.geojson")
            if (field["field_dir"] / "boundary" / "field_boundary.geojson").is_file()
            else ""
        )
        sentinel_manifest = field["field_dir"] / "satellite" / "sentinel" / "manifest.json"
        if sentinel_manifest.is_file():
            inputs[str(sentinel_manifest)] = file_sha256(sentinel_manifest)

    png_bytes = sum(path.stat().st_size for path in (output_dir / "ndvi").rglob("*.png"))
    soil_bytes = (output_dir / "soil_map_units.geojson").stat().st_size
    storage = {
        "html": html_path.stat().st_size,
        "soil_geojson": soil_bytes,
        "ndvi_pngs": png_bytes,
        "total": html_path.stat().st_size + soil_bytes + png_bytes,
    }
    manifest_path = write_manifest(
        output_dir,
        payload=payload,
        inputs=inputs,
        soil_features=len(soil_features),
        ndvi_scenes=ndvi_png_total,
        ndvi_pngs=ndvi_png_total,
        storage=storage,
    )

    if not args.quiet:
        seconds = time.time() - started
        print(
            f"Wrote {html_path} ({storage['html'] / 1024:.0f} KB)\n"
            f"  soil units: {len(soil_features)} ({soil_bytes / 1024:.0f} KB)\n"
            f"  NDVI scenes: {ndvi_png_total} PNGs ({png_bytes / 1024 / 1024:.1f} MB)\n"
            f"  manifest: {manifest_path}\n"
            f"  elapsed: {seconds:.1f}s"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExplorerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
