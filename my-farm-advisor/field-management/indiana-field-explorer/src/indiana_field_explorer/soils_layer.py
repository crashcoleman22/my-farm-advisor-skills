"""SSURGO MUPOLYGON layer for the explorer.

The discovery pipeline stores tabular map-unit membership only. This module
fetches the matching real soil polygons from IndianaMap's hosted SSURGO layer
(NRCS data), clips them to the county, simplifies them for display, and caches a
single GeoJSON next to the other explorer state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import geopandas as gpd
import pandas as pd
import requests

SSURGO_QUERY = ("https://gisdata.in.gov/server/rest/services/Hosted/"
                "Soil_Map_Units_SSURGO/FeatureServer/4/query")
OUT_FIELDS = ("mukey,musym,muname,drclassdcd,hydgrpdcd,slopegradwta,aws050wta,"
              "wtdepannmin,flodfreqdcd")
PAGE = 2000
MAX_OFFSET = 30000  # pagination cap: 2,000 features per page, 15 pages


def _county_bbox(county: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    bounds = county.to_crs(4326).total_bounds
    return tuple(float(v) for v in bounds)


def _fetch_page(mukeys: list[str], bbox: tuple[float, float, float, float],
                offset: int) -> dict:
    quoted = ",".join(f"'{m}'" for m in mukeys)
    params = {
        "where": f"mukey IN ({quoted})",
        "geometry": ",".join(f"{v:.6f}" for v in bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": OUT_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "maxAllowableOffset": "0.00002",
        "geometryPrecision": "5",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE),
        "f": "geojson",
    }
    response = requests.post(SSURGO_QUERY, data=params, timeout=180)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"SSURGO query failed: {payload['error']}")
    return payload


def build(cfg, fields: gpd.GeoDataFrame, progress=None) -> dict:
    units = pd.read_csv(cfg.farm_dir / "manifests/field_soil_map_units.csv",
                        dtype={"field_id": str, "mukey": str})
    mukeys = sorted(units["mukey"].dropna().astype(str).unique())
    county = gpd.read_file(cfg.farm_dir / "manifests/county_081_18.geojson")
    bbox = _county_bbox(county)

    features: list[dict] = []
    offset = 0
    while offset <= MAX_OFFSET:
        if progress:
            progress(f"fetching soil polygons ({len(features)} so far)", offset, MAX_OFFSET)
        payload = _fetch_page(mukeys, bbox, offset)
        page = payload.get("features", [])
        features.extend(page)
        if len(page) < PAGE:
            break
        offset += len(page)

    if not features:
        raise RuntimeError("no SSURGO polygons returned")

    gdf = gpd.GeoDataFrame.from_features(features, crs=4326)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.make_valid()

    if progress:
        progress(f"clipping {len(gdf)} polygons to field footprints", 1, 3)
    field_area = fields[fields.geometry.notna() & ~fields.geometry.is_empty].copy()
    field_area = field_area.to_crs(4326)
    clipped = gpd.clip(gdf, field_area)
    clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty].copy()

    if progress:
        progress("simplifying polygons", 2, 3)
    projected = clipped.to_crs(32616)
    projected["geometry"] = projected.geometry.simplify(0.5, preserve_topology=True)
    projected["_area"] = projected.geometry.area
    projected = projected[projected["_area"] >= 5.0].copy()
    clipped = projected.to_crs(4326)

    records = []
    for row in clipped.itertuples(index=False):
        def num(value):
            return None if pd.isna(value) else round(float(value), 2)

        records.append({
            "type": "Feature",
            "properties": {
                "mukey": str(row.mukey),
                "musym": row.musym,
                "muname": row.muname,
                "drainage": None if pd.isna(row.drclassdcd) else row.drclassdcd,
                "hydgrp": None if pd.isna(row.hydgrpdcd) else row.hydgrpdcd,
                "slope_pct": num(row.slopegradwta),
                "awc_050": num(row.aws050wta),
                "wt_depth_cm": num(row.wtdepannmin),
                "flood_freq": None if pd.isna(row.flodfreqdcd) else row.flodfreqdcd,
            },
            "geometry": row.geometry.__geo_interface__,
        })

    payload = {"type": "FeatureCollection", "features": records}
    cfg.soil_cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.soil_cache.with_name("soil_map_units.tmp.geojson")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(cfg.soil_cache)

    provenance = {
        "source": SSURGO_QUERY,
        "dataset": "IndianaMap Soil Map Units SSURGO (NRCS)",
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "mukeys": len(mukeys),
        "features_downloaded": len(gdf),
        "features_after_clip": len(records),
        "simplify_m": 0.5,
        "min_polygon_m2": 5.0,
        "note": ("MUPOLYGON geometry only; attributes also live in "
                 "manifests/field_soil_summary.csv and field_soil_map_units.csv."),
    }
    cfg.soil_provenance.write_text(json.dumps(provenance, indent=2) + "\n",
                                   encoding="utf-8")
    return provenance
