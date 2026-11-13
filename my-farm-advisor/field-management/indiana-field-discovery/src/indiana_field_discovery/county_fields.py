"""Phase 1: build a countywide agricultural field inventory from FIBOA CSB."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import box

from . import sources
from .paths import farm_paths

PROJECT_CRS = "EPSG:5070"      # equal-area, CSB-native (acreage)
LOCAL_CRS = "EPSG:32616"       # UTM 16N, Indiana (cross-check)
OUTPUT_CRS = "EPSG:4326"


def _slug(field_id: str) -> str:
    return "csb-" + str(field_id).strip().lower().replace(" ", "-")


def _int_code(value) -> int | None:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _crop_list(value) -> str:
    if value is None:
        return ""
    return str(value)[:200]


def build_county_fields(args: argparse.Namespace) -> int:
    t_start = time.time()
    paths = farm_paths(args.grower, args.farm).ensure()
    county_cache = paths.manifests_dir / f"county_{args.county_fips}_{args.state_fips}.geojson"

    if county_cache.exists() and not args.refresh_county:
        county = gpd.read_file(county_cache)
        print(f"[county] cached {county_cache}")
    else:
        county = sources.fetch_county_boundary(args.state_fips, args.county_fips)
        county.to_file(county_cache, driver="GeoJSON")
        print(f"[county] fetched TIGER boundary -> {county_cache}")

    if len(county) != 1:
        raise SystemExit(f"expected 1 county polygon, got {len(county)}")
    county_name = str(county.iloc[0].get("BASENAME") or county.iloc[0].get("NAME"))
    county_5070 = county.to_crs(PROJECT_CRS)
    county_geom = county_5070.geometry.iloc[0]
    print(f"[county] {county_name} state={args.state_fips} fips={args.county_fips}")

    pf = sources.open_fiboa()
    xmin, ymin, xmax, ymax = (float(v) for v in county_5070.total_bounds)
    bbox_5070 = (xmin, ymin, xmax, ymax)
    row_groups = sources.select_row_groups(pf, bbox_5070)
    print(f"[fiboa] {pf.metadata.num_rows:,} rows / {pf.metadata.num_row_groups} groups; "
          f"{len(row_groups)} candidate groups")

    min_area_m2 = args.min_area_acres * 4046.8564224
    records: list[dict] = []
    clipped_geoms = []
    original_geoms = []
    duplicate_rows = []
    seen: dict[str, int] = {}
    rows_scanned = 0
    county_name_mismatch = 0

    for data in sources.iter_fiboa_rows(pf, row_groups):
        ids = data["id"]
        for i, field_id in enumerate(ids):
            rows_scanned += 1
            raw = data["geometry"][i]
            if raw is None:
                continue
            geom = sources.load_wkb(raw)
            if geom.is_empty or not geom.intersects(county_geom):
                continue
            admin_name = data["administrative_area_level_2"][i]
            admin_mismatch = bool(
                admin_name and county_name.lower() not in str(admin_name).lower()
            )
            if admin_mismatch:
                county_name_mismatch += 1

            clipped = geom.intersection(county_geom)
            if clipped.is_empty or clipped.area <= min_area_m2:
                continue
            if clipped.geom_type == "GeometryCollection":
                clipped = clipped.buffer(0)
            if clipped.is_empty:
                continue

            code = _int_code(data["crop:code"][i])
            record = {
                "field_id": str(field_id),
                "field_slug": _slug(field_id),
                "source": "usda_csb_fiboa",
                "source_id": str(field_id),
                "state_fips": args.state_fips.zfill(2),
                "county_fips": args.county_fips.zfill(3),
                "county_name": county_name,
                "csb_admin_county": admin_name or "",
                "admin_county_mismatch": admin_mismatch,
                "crop_code": code,
                "crop_name": (data["crop:name"][i] or (sources.crop_name(code) if code else "")),
                "ag_class": sources.ag_class(code),
                "crop_code_list": _crop_list(data["crop:code_list"][i]),
                "determination_datetime": data["determination_datetime"][i],
                "geometry_hash": hashlib.sha1(bytes(raw)).hexdigest(),
            }
            key = f"{record['source']}:{record['field_id']}"
            if key in seen:
                duplicate_rows.append({"source": record["source"], "field_id": record["field_id"],
                                       "kept_index": seen[key], "dropped_index": len(records)})
                continue
            seen[key] = len(records)
            records.append(record)
            clipped_geoms.append(clipped)
            original_geoms.append(geom)

    if not records:
        raise SystemExit("no CSB polygons found for county")

    gdf = gpd.GeoDataFrame(records, geometry=clipped_geoms, crs=PROJECT_CRS)
    original = gpd.GeoDataFrame(geometry=original_geoms, crs=PROJECT_CRS)

    gdf["area_m2_5070"] = gdf.area.round(3)
    gdf["area_acres_clipped"] = (gdf["area_m2_5070"] / 4046.8564224).round(4)
    gdf["area_acres_original"] = (original.area / 4046.8564224).round(4)
    gdf["area_acres_utm16n"] = (gdf.to_crs(LOCAL_CRS).area / 4046.8564224).round(4)
    gdf["clip_ratio"] = (gdf["area_m2_5070"] / original.area).clip(upper=1.0).round(6)
    gdf["edge_flag"] = gdf["clip_ratio"] < 0.999

    # Preserve original geometry for polygons that crossed the county line.
    gdf["geometry_original_wkb"] = None
    edge_mask = gdf["edge_flag"].to_numpy()
    if edge_mask.any():
        wkb_values = original.geometry.to_wkb()
        gdf.loc[edge_mask, "geometry_original_wkb"] = [
            wkb_values[i] for i in range(len(gdf)) if edge_mask[i]
        ]

    gdf = gdf.sort_values("field_id").reset_index(drop=True)

    # ---- outputs ---------------------------------------------------------
    boundary_parquet = paths.boundary_dir / "field_boundaries.parquet"
    boundary_geojson = paths.boundary_dir / "field_boundaries.geojson"
    gdf.to_parquet(boundary_parquet, index=False)
    geojson_view = gdf.drop(columns=["geometry_original_wkb"]).to_crs(OUTPUT_CRS)
    geojson_view.to_file(boundary_geojson, driver="GeoJSON")

    ag = gdf[gdf["ag_class"] == "crop"].copy()
    ag.to_parquet(paths.boundary_dir / "field_boundaries_ag.parquet", index=False)

    gdf[["field_id", "field_slug"]].to_csv(paths.manifests_dir / "field-inventory.csv", index=False)
    gdf.drop(columns=["geometry_original_wkb"]).drop(columns="geometry").to_csv(
        paths.manifests_dir / "field_attributes.csv", index=False
    )

    summary = (
        gdf.groupby("ag_class")
        .agg(polygons=("field_id", "count"),
             acres_clipped=("area_acres_clipped", "sum"),
             acres_original=("area_acres_original", "sum"))
        .reset_index()
    )
    summary["acres_clipped"] = summary["acres_clipped"].round(1)
    summary["acres_original"] = summary["acres_original"].round(1)
    summary.loc[len(summary)] = {
        "ag_class": "total",
        "polygons": int(summary["polygons"].sum()),
        "acres_clipped": round(float(summary["acres_clipped"].sum()), 1),
        "acres_original": round(float(summary["acres_original"].sum()), 1),
    }
    summary.to_csv(paths.manifests_dir / "county_field_summary.csv", index=False)

    exclusions = (
        gdf[gdf["ag_class"] != "crop"]
        .groupby(["crop_code", "crop_name", "ag_class"]).size()
        .reset_index(name="polygons")
        .sort_values("polygons", ascending=False)
    )
    exclusions.to_csv(paths.manifests_dir / "county_field_exclusions.csv", index=False)

    pd.DataFrame(duplicate_rows).to_csv(
        paths.manifests_dir / "county_field_duplicates.csv", index=False
    )

    county_stats = {
        "county": county_name,
        "state_fips": args.state_fips,
        "county_fips": args.county_fips,
        "county_area_acres_5070": round(float(county.to_crs(PROJECT_CRS).area.iloc[0] / 4046.8564224), 1),
        "polygons_total": int(len(gdf)),
        "polygons_crop": int(len(ag)),
        "polygons_ag_non_crop": int((gdf["ag_class"] == "ag_non_crop").sum()),
        "polygons_non_ag": int((gdf["ag_class"] == "non_ag").sum()),
        "polygons_unknown": int((gdf["ag_class"] == "unknown").sum()),
        "acres_clipped_total": round(float(gdf["area_acres_clipped"].sum()), 1),
        "acres_clipped_crop": round(float(ag["area_acres_clipped"].sum()), 1),
        "acres_original_total": round(float(gdf["area_acres_original"].sum()), 1),
        "edge_intersections": int(gdf["edge_flag"].sum()),
        "duplicates_removed": len(duplicate_rows),
        "rows_scanned": rows_scanned,
        "county_name_mismatches_skipped": county_name_mismatch,
        "row_groups_scanned": len(row_groups),
        "row_groups_total": pf.metadata.num_row_groups,
        "area_method": "EPSG:5070 equal-area; EPSG:32616 cross-check column",
        "source": sources.FIBOA_URL,
        "limitations": [
            "FIBOA boundaries are the 2016-2023 rev23 CSB window, dissolved on equal 2023 crop.",
            "FIBOA has no state field; adjacency to county name is a guard, spatial clip is authoritative.",
            "Crop attribute is the 2023 crop year only.",
        ],
    }
    (paths.manifests_dir / "county_field_stats.json").write_text(
        json.dumps(county_stats, indent=2) + "\n", encoding="utf-8"
    )

    farm_json = {
        "farm_slug": args.farm,
        "farm_name": f"{county_name} County, {args.state_fips} (countywide)",
        "grower_slug": args.grower,
        "country": "US",
        "state": args.state_fips,
        "county_fips": f"{args.state_fips}{args.county_fips}",
        "county_name": county_name,
        "field_source": "usda_csb_fiboa",
        "geometry_vintage": "2016-2023 rev23 (FIBOA)",
        "created_at": sources.utc_now(),
    }
    (paths.farm_dir / "farm.json").write_text(json.dumps(farm_json, indent=2) + "\n", encoding="utf-8")

    # ---- static map ------------------------------------------------------
    _render_map(gdf.to_crs(OUTPUT_CRS), county, paths, county_name)

    elapsed = time.time() - t_start
    print(f"[done] {len(gdf)} fields ({len(ag)} crop) in {elapsed:.1f}s")
    print(f"[done] clipped acres total {gdf['area_acres_clipped'].sum():,.1f}; "
          f"crop {ag['area_acres_clipped'].sum():,.1f}")
    print(json.dumps(county_stats, indent=2))
    return 0


def _render_map(gdf: gpd.GeoDataFrame, county: gpd.GeoDataFrame, paths, county_name: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 9), dpi=150)
    county.boundary.plot(ax=ax, color="black", linewidth=1.2, zorder=3)
    colors = {"crop": "#2e7d32", "ag_non_crop": "#c8a415", "non_ag": "#9e9e9e", "unknown": "#6a1b9a"}
    for klass, color in colors.items():
        subset = gdf[gdf["ag_class"] == klass]
        if len(subset):
            subset.plot(ax=ax, color=color, edgecolor="none", alpha=0.75,
                        label=f"{klass} (n={len(subset)})", zorder=2)
    ax.set_title(f"{county_name} County field discovery — {len(gdf)} CSB polygons")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.legend(loc="lower left", frameon=True)
    ax.set_aspect("equal")
    fig.tight_layout()
    out = paths.derived_dir / "reports" / f"{county_name.lower()}_in_field_map.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"[map] {out}")


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("county-fields", help="Build countywide CSB field inventory")
    parser.add_argument("--state-fips", default="18")
    parser.add_argument("--county-fips", default="081")
    parser.add_argument("--grower", default="in-johnson")
    parser.add_argument("--farm", default="countywide")
    parser.add_argument("--min-area-acres", type=float, default=0.01)
    parser.add_argument("--refresh-county", action="store_true")
    parser.set_defaults(func=build_county_fields)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(build_county_fields(argparse.Namespace()))
