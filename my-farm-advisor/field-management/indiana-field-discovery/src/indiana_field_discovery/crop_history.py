"""Phase 3: USDA CDL annual crop history per field (2021-2025), 2026 flagged."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.features import rasterize
from rasterio.windows import from_bounds
from pyproj import Transformer

from . import sources
from .paths import farm_paths

CDL_URL = "https://nassgeodata.gmu.edu/nass_data_cache/byfips/CDL_{year}_{label}.tif"


def _download(year: int, label: str, dest: Path) -> tuple[Path, str]:
    url = CDL_URL.format(year=year, label=label)
    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=300) as response:
        if response.status_code == 404:
            raise FileNotFoundError(url)
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=2**20):
                if chunk:
                    handle.write(chunk)
                    digest.update(chunk)
    return dest, digest.hexdigest()


def _field_majority(
    raster_path: Path, fields: gpd.GeoDataFrame, state_fips: str
) -> pd.DataFrame:
    field_ids = fields["field_id"].astype(str).to_numpy()
    index_of = {fid: i + 1 for i, fid in enumerate(field_ids)}

    with rasterio.open(raster_path) as src:
        ids_5070 = fields.to_crs(src.crs)
        minx, miny, maxx, maxy = ids_5070.total_bounds
        window = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        window = window.round_offsets().round_lengths()
        data = src.read(1, window=window)
        transform = src.window_transform(window)
        shapes = ((geom, index_of[fid]) for fid, geom in zip(field_ids, ids_5070.geometry))
        field_raster = rasterize(
            shapes, out_shape=data.shape, transform=transform,
            fill=0, dtype="int32", all_touched=False,
        )

    valid = (field_raster > 0) & (data > 0) & (data < 255)
    field_idx = field_raster[valid].astype(np.int64)
    crop = data[valid].astype(np.int64)
    if field_idx.size == 0:
        return pd.DataFrame(columns=["field_id", "crop_code", "pixels", "field_pixels", "crop_pct"])

    # Encode (field, crop) pairs into one bincount key.
    keys = field_idx * 1000 + crop
    counts = np.bincount(keys, minlength=(len(field_ids) + 1) * 1000)
    per_field_valid = np.bincount(field_idx, minlength=len(field_ids) + 1)

    nz = np.nonzero(counts)[0]
    pairs = pd.DataFrame({
        "field_idx": nz // 1000,
        "crop_code": (nz % 1000),
        "pixels": counts[nz],
    })
    pairs["field_pixels"] = per_field_valid[pairs["field_idx"]]
    pairs["crop_pct"] = (pairs["pixels"] / pairs["field_pixels"] * 100).round(2)
    pairs = pairs.sort_values(["field_idx", "pixels"], ascending=[True, False])
    winners = pairs.groupby("field_idx").head(1).copy()
    winners["field_id"] = [field_ids[i - 1] for i in winners["field_idx"]]
    return winners[["field_id", "crop_code", "pixels", "field_pixels", "crop_pct"]]


def build_crop_history(args: argparse.Namespace) -> int:
    t0 = time.time()
    paths = farm_paths(args.grower, args.farm).ensure()
    boundary = paths.boundary_dir / "field_boundaries_ag.parquet"
    if not boundary.exists():
        raise SystemExit(f"missing {boundary}; run county-fields first")
    fields = gpd.read_parquet(boundary)
    print(f"[cdl] {len(fields)} crop fields")

    rows: list[pd.DataFrame] = []
    availability: list[dict] = []
    for year in range(args.start_year, args.end_year + 1):
        label = args.state_fips.zfill(2)
        tmp_path = paths.tmp_dir / f"CDL_{year}_{label}.tif"
        try:
            raster_path, sha = _download(year, label, tmp_path)
        except FileNotFoundError:
            print(f"[cdl] {year}: not released (404)")
            availability.append({"year": year, "status": "unreleased", "source": CDL_URL.format(year=year, label=label)})
            continue
        except Exception as exc:  # noqa: BLE001 - network guard
            print(f"[cdl] {year}: download failed: {exc}")
            availability.append({"year": year, "status": f"error: {exc}", "source": CDL_URL.format(year=year, label=label)})
            continue

        try:
            majority = _field_majority(raster_path, fields, args.state_fips)
        finally:
            tmp_path.unlink(missing_ok=True)

        if majority.empty:
            availability.append({"year": year, "status": "no_overlap", "source": CDL_URL.format(year=year, label=label)})
            continue
        majority["year"] = year
        majority["crop_name"] = majority["crop_code"].map(sources.crop_name)
        majority["ag_class"] = majority["crop_code"].map(sources.ag_class)
        majority["source"] = CDL_URL.format(year=year, label=label)
        majority["source_sha256"] = sha
        rows.append(majority)
        availability.append({"year": year, "status": "processed", "source": CDL_URL.format(year=year, label=label),
                             "sha256": sha, "fields_matched": int(len(majority))})
        print(f"[cdl] {year}: {len(majority)} fields, {majority['pixels'].sum():,} px, "
              f"{time.time() - t0:.1f}s")

    if rows:
        history = pd.concat(rows, ignore_index=True)
        history = history.sort_values(["year", "field_id"]).reset_index(drop=True)
    else:
        history = pd.DataFrame(columns=["field_id", "year", "crop_code", "crop_name"])
    history.to_csv(paths.manifests_dir / "field_crop_history.csv", index=False)

    availability_df = pd.DataFrame(availability)
    availability_df.to_csv(paths.manifests_dir / "field_crop_history_availability.csv", index=False)

    provenance = {
        "source": "USDA NASS Cropland Data Layer (CropScape by-FIPS GeoTIFF)",
        "resolution_m": 30,
        "years_requested": [args.start_year, args.end_year],
        "available_years": availability_df.loc[availability_df["status"] == "processed", "year"].tolist(),
        "missing_years": availability_df.loc[availability_df["status"] != "processed", "year"].tolist(),
        "accessed_at": sources.utc_now(),
        "notes": [
            "Temporary state rasters are deleted immediately after county clipping.",
            "Majority crop per field uses the 30 m CDL grid; small fields can have few pixels.",
        ],
    }
    (paths.manifests_dir / "field_crop_history_provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[cdl] done in {time.time() - t0:.1f}s; available {provenance['available_years']}, "
          f"missing {provenance['missing_years']}")
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("crop-history", help="Attach annual CDL crop history per field")
    parser.add_argument("--state-fips", default="18")
    parser.add_argument("--grower", default="in-johnson")
    parser.add_argument("--farm", default="countywide")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.set_defaults(func=build_crop_history)
