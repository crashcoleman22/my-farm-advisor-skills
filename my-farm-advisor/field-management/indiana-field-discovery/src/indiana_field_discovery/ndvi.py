"""Phases 4-5: Sentinel-2 field-level NDVI statistics (streamed, no scene storage).

Imagery is read as remote COG windows (``/vsicurl``); only compact per-field
statistics are written. Processing is organised by (tile, date) - never per
field - using one 10 m field-index raster for the county.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import reproject
from rasterio.windows import from_bounds

from . import sources
from .paths import farm_paths

STAC_URL = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
SCL_MASK = {0, 1, 3, 8, 9, 10, 11}  # nodata, saturated, shadow, cloud, cirrus, snow
NBINS = 400
BIN_LO, BIN_HI = -1.0, 1.0

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MAX_RETRY": "4",
    "CPL_VSIL_CURL_CACHE_SIZE": str(64 * 2**20),
    "VSI_CACHE": "FALSE",
}


# --------------------------------------------------------------------------
# STAC
# --------------------------------------------------------------------------
def search_items(bbox, start: str, end: str, timeout: int = 120) -> list[dict]:
    body = {"collections": [COLLECTION], "bbox": list(bbox),
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 250}
    import requests
    features: list[dict] = []
    url = STAC_URL
    for _ in range(40):  # bounded pagination
        response = requests.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        features.extend(payload.get("features", []))
        next_link = next(
            (link for link in payload.get("links", []) if link.get("rel") == "next"), None
        )
        if not next_link:
            break
        url, body = next_link["href"], next_link.get("body") or body
    return features


def dedupe_by_date(items: list[dict]) -> list[dict]:
    """One best item per acquisition date: lowest cloud, then newest baseline."""
    best: dict[str, dict] = {}
    for item in items:
        props = item["properties"]
        date = props["datetime"][:10]
        cloud = props.get("eo:cloud_cover", 100.0)
        baseline = float(props.get("s2:processing_baseline") or 0.0)
        key = (cloud, -baseline)
        if date not in best:
            best[date] = item
            continue
        current = best[date]["properties"]
        current_key = (
            current.get("eo:cloud_cover", 100.0),
            -float(current.get("s2:processing_baseline") or 0.0),
        )
        if key < current_key:
            best[date] = item
    return [best[d] for d in sorted(best)]


def build_manifest(paths, bbox, years: list[int]) -> dict[int, list[dict]]:
    manifest_dir = paths.state_dir / "ndvi" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, list[dict]] = {}
    for year in years:
        cache = manifest_dir / f"s2_{year}.json"
        if cache.exists():
            payload = json.loads(cache.read_text())
        else:
            items = search_items(bbox, f"{year}-04-01", f"{year}-10-31")
            deduped = dedupe_by_date(items)
            payload = {
                "year": year,
                "raw_items": len(items),
                "dates": len(deduped),
                "items": [{k: item.get(k) for k in ("id", "properties", "assets")} for item in deduped],
            }
            cache.write_text(json.dumps(payload))
        out[year] = payload["items"]
    return out


# --------------------------------------------------------------------------
# Field raster
# --------------------------------------------------------------------------
def _county_bounds_utm(fields: gpd.GeoDataFrame, crs) -> tuple[float, float, float, float]:
    projected = fields.to_crs(crs)
    return tuple(float(v) for v in projected.total_bounds)


def build_field_raster(paths, fields: gpd.GeoDataFrame, item: dict, bounds, out_path: Path) -> dict:
    red_url = item["assets"]["red"]["href"]
    with rasterio.Env(**GDAL_ENV), rasterio.open(red_url) as src:
        crs = src.crs
        window = from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        transform = src.window_transform(window)
        shape = (int(window.height), int(window.width))

    field_ids = fields["field_id"].astype(str).tolist()
    index_of = {fid: i + 1 for i, fid in enumerate(field_ids)}
    projected = fields.to_crs(crs)
    shapes = ((geom, index_of[fid]) for fid, geom in zip(field_ids, projected.geometry))
    index_raster = rasterize(shapes, out_shape=shape, transform=transform, fill=0,
                             dtype="int32", all_touched=False)

    profile = {
        "driver": "GTiff", "height": shape[0], "width": shape[1], "count": 1,
        "dtype": "int32", "crs": crs.to_string(), "transform": transform,
        "compress": "deflate", "predictor": 2, "tiled": True,
        "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(index_raster, 1)
    mapping = pd.DataFrame({"field_index": range(1, len(field_ids) + 1), "field_id": field_ids})
    mapping.to_csv(out_path.parent / "field_index.csv", index=False)
    return {"crs": crs.to_string(), "transform": list(transform)[:6], "shape": list(shape),
            "bounds": list(bounds), "fields": len(field_ids),
            "pixels": int(shape[0] * shape[1])}


# --------------------------------------------------------------------------
# Per-date processing
# --------------------------------------------------------------------------
def _read_window(url: str, bounds, env) -> tuple[np.ndarray, object, object]:
    with rasterio.Env(**env), rasterio.open(url) as src:
        window = from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        data = src.read(1, window=window)
        return data, src.window_transform(window), src.crs


def process_date(item: dict, raster_path: Path, bounds) -> pd.DataFrame:
    props = item["properties"]
    assets = item["assets"]
    boff = bool(props.get("earthsearch:boa_offset_applied", False))
    baseline = float(props.get("s2:processing_baseline") or 0.0)
    # Earth Search / sentinel-cogs assets already carry the ESA BOA offset: the
    # collection declares scale 0.0001 / offset 0 and red medians are ~300-430 DN
    # on every baseline, so DN/10000 is reflectance directly. Verified across
    # baselines 03.00-05.12; subtracting 1000 would drive dark canopies above NDVI 1.
    subtract_offset = False
    with rasterio.open(raster_path) as src:
        index = src.read(1)
        dst_transform = src.transform
        dst_crs = src.crs

    scl, scl_tf, scl_crs = _read_window(assets["scl"]["href"], bounds, GDAL_ENV)

    if (scl.shape[0], scl.shape[1]) != index.shape:
        scl_10m = np.zeros(index.shape, dtype=np.uint8)
        reproject(source=scl, destination=scl_10m, src_transform=scl_tf, src_crs=scl_crs,
                  dst_transform=dst_transform, dst_crs=dst_crs, resampling=Resampling.nearest)
        scl = scl_10m

    # Cloud screening first: if no field pixel is usable, skip the 10 m band reads.
    clear = (index > 0) & ~np.isin(scl, list(SCL_MASK))
    if not clear.any():
        return pd.DataFrame()

    red, _, _ = _read_window(assets["red"]["href"], bounds, GDAL_ENV)
    nir, _, _ = _read_window(assets["nir"]["href"], bounds, GDAL_ENV)
    if red.shape != index.shape or nir.shape != index.shape:
        raise RuntimeError(f"shape mismatch {red.shape} {nir.shape} {index.shape}")

    red_f = red.astype(np.float32)
    nir_f = nir.astype(np.float32)
    if subtract_offset:
        red_f -= 1000.0
        nir_f -= 1000.0
    red_f /= 10000.0
    nir_f /= 10000.0

    denom = nir_f + red_f
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir_f - red_f) / denom
    valid = (index > 0) & (red_f > 0) & (nir_f > 0) & (denom > 0) & np.isfinite(ndvi)
    valid &= ~np.isin(scl, list(SCL_MASK))

    n_fields = int(index.max())
    field_idx = index[valid].astype(np.int64)
    values = ndvi[valid]
    if field_idx.size == 0:
        return pd.DataFrame()

    field_px = np.bincount(index.ravel(), minlength=n_fields + 1)[1:]
    counts = np.bincount(field_idx, minlength=n_fields + 1)[1:].astype(np.int64)
    sums = np.bincount(field_idx, weights=values, minlength=n_fields + 1)[1:]
    sumsq = np.bincount(field_idx, weights=values.astype(np.float64) ** 2, minlength=n_fields + 1)[1:]

    scaled = np.clip((values - BIN_LO) / (BIN_HI - BIN_LO), 0, 0.999999)
    bins = (scaled * NBINS).astype(np.int32)
    keys = field_idx * NBINS + bins
    hist = np.bincount(keys, minlength=(n_fields + 1) * NBINS).reshape(n_fields + 1, NBINS)[1:]

    def quantile(q: float) -> np.ndarray:
        """Interpolate the empirical CDF through bin mid-ranks.

        Equivalent to the within-bin assumption when bins are dense, and to linear
        interpolation between sample values when a field has very few pixels.
        """
        cumulative = np.cumsum(hist, axis=1)
        centres = BIN_LO + (np.arange(NBINS) + 0.5) * (BIN_HI - BIN_LO) / NBINS
        out = np.full(n_fields, np.nan, dtype=np.float64)
        targets = q * np.maximum(counts - 1, 0)
        for i in range(n_fields):
            if counts[i] == 0:
                continue
            row = hist[i]
            nz = np.nonzero(row)[0]
            mid_rank = cumulative[i, nz] - (row[nz] + 1) / 2.0
            out[i] = np.interp(targets[i], mid_rank, centres[nz])
        return out

    with np.errstate(invalid="ignore", divide="ignore"):
        mean = sums / counts
        var = sumsq / counts - mean**2
        std = np.sqrt(np.clip(var, 0, None))
    median = quantile(0.5)
    p10 = quantile(0.10)
    p90 = quantile(0.90)

    mapped = pd.read_csv(raster_path.parent / "field_index.csv")
    ids = mapped["field_id"].astype(str).to_numpy()

    date = props["datetime"][:10]
    df = pd.DataFrame({
        "field_id": ids,
        "date": date,
        "year": int(date[:4]),
        "satellite": props.get("platform", ""),
        "resolution_m": 10,
        "scene_id": item["id"],
        "scene_cloud_pct": props.get("eo:cloud_cover"),
        "boa_offset_applied": boff,
        "boa_offset_subtracted": subtract_offset,
        "baseline": props.get("s2:processing_baseline", ""),
        "valid_px": counts,
        "field_px": field_px,
        "coverage_pct": np.round(counts / np.maximum(field_px, 1) * 100, 2),
        "ndvi_mean": np.round(mean, 5),
        "ndvi_median": np.round(median, 5),
        "ndvi_std": np.round(std, 5),
        "ndvi_p10": np.round(p10, 5),
        "ndvi_p90": np.round(p90, 5),
    })
    df = df[df["valid_px"] > 0]
    return df


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def _net_rx() -> int:
    try:
        with open("/proc/net/dev") as handle:
            total = 0
            for line in handle.readlines()[2:]:
                total += int(line.split(":")[1].split()[0])
            return total
    except Exception:  # noqa: BLE001
        return 0


def _pilot_fields(fields: gpd.GeoDataFrame, count: int) -> gpd.GeoDataFrame:
    """Pick a compact cluster of crop fields (smallest bbox around a seed field)."""
    crop = fields[fields["ag_class"] == "crop"].copy()
    crop["cx"] = crop.geometry.centroid.x
    crop["cy"] = crop.geometry.centroid.y
    seed = crop.iloc[len(crop) // 2]
    crop["dist"] = np.hypot(crop["cx"] - seed["cx"], crop["cy"] - seed["cy"])
    return crop.nsmallest(count, "dist").drop(columns=["cx", "cy", "dist"])


def run(args: argparse.Namespace) -> int:
    t0 = time.time()
    paths = farm_paths(args.grower, args.farm).ensure()
    field_path = paths.boundary_dir / ("field_boundaries_ag.parquet" if args.crop_only else "field_boundaries.parquet")
    fields = gpd.read_parquet(field_path)
    print(f"[ndvi] {len(fields)} fields from {field_path.name}")

    if args.pilot_fields:
        fields = _pilot_fields(fields, args.pilot_fields)
        print(f"[ndvi] pilot cluster: {len(fields)} fields")

    years = list(range(args.start_year, args.end_year + 1))
    county_bbox = tuple(fields.to_crs(4326).total_bounds)
    manifests = build_manifest(paths, county_bbox, years)
    total_dates = sum(len(v) for v in manifests.values())
    print(f"[ndvi] {total_dates} unique tile-dates across {years}")

    rx_start = _net_rx()
    disk_start = sum(f.stat().st_size for f in paths.root.rglob("*") if f.is_file())

    first_item = manifests[years[0]][0]
    with rasterio.Env(**GDAL_ENV), rasterio.open(first_item["assets"]["red"]["href"]) as src:
        crs = src.crs
    bounds = _county_bounds_utm(fields, crs)
    run_key = (
        ("pilot" if args.pilot_fields else "county")
        + "_"
        + hashlib.sha1(
            (json.dumps([list(bounds), len(fields)])).encode("utf-8")
        ).hexdigest()[:12]
    )
    run_dir = paths.state_dir / "ndvi" / "runs" / run_key
    raster_path = run_dir / "field_index.tif"
    meta = build_field_raster(paths, fields, first_item, bounds, raster_path)
    meta["run_key"] = run_key
    print(f"[ndvi] field raster {meta['shape']} {meta['pixels']:,} px crs={meta['crs']} run={run_key}")

    out_dir = paths.derived_dir / ("pilot" if args.pilot_fields else "ndvi")
    out_dir.mkdir(parents=True, exist_ok=True)
    done_dir = paths.state_dir / "ndvi" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)

    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    processed = 0

    jobs = [(year, item) for year in years for item in manifests[year]]
    if args.max_dates:
        jobs = jobs[: args.max_dates]
    rows_dir = run_dir / "rows"
    rows_dir.mkdir(parents=True, exist_ok=True)

    def work(job):
        year, item = job
        date = item["properties"]["datetime"][:10]
        marker = done_dir / f"{run_key}_{year}_{date}.json"
        cached = rows_dir / f"{year}_{date}.parquet"
        if marker.exists():
            if cached.exists():
                return year, date, pd.read_parquet(cached), "skipped"
            return year, date, pd.DataFrame(), "skipped"
        try:
            df = process_date(item, raster_path, bounds)
            if not df.empty:
                df.to_parquet(cached, index=False)
            return year, date, df, "ok"
        except Exception as exc:  # noqa: BLE001 - keep going, record failure
            return year, date, exc, "error"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, job) for job in jobs]
        for future in as_completed(futures):
            year, date, result, status = future.result()
            processed += 1
            if status in ("ok", "skipped"):
                if result is not None and not result.empty:
                    frames.append(result)
                if status == "ok":
                    (done_dir / f"{run_key}_{year}_{date}.json").write_text(
                        json.dumps({"run_key": run_key, "year": year, "date": date,
                                    "rows": 0 if result is None else len(result),
                                    "finished_at": sources.utc_now()})
                    )
            elif status == "error":
                errors.append(f"{date}: {result}")
                print(f"[ndvi] {date} failed: {result}")
            if processed % 25 == 0:
                print(f"[ndvi] {processed}/{len(jobs)} dates, {time.time() - t0:.0f}s")

    rx = _net_rx() - rx_start

    if frames:
        result = pd.concat(frames, ignore_index=True)
    else:
        result = pd.DataFrame()
    result = result.sort_values(["field_id", "date"]).reset_index(drop=True)

    if args.pilot_fields:
        result.to_csv(out_dir / "pilot_ndvi.csv", index=False)
    else:
        result.to_parquet(out_dir / "field_ndvi_daily.parquet", index=False)
        # Partitioned yearly copies for incremental appends.
        parts = out_dir / "yearly"
        parts.mkdir(exist_ok=True)
        for year, chunk in result.groupby("year"):
            chunk.to_parquet(parts / f"year={year}.parquet", index=False)

    disk_end = sum(f.stat().st_size for f in paths.root.rglob("*") if f.is_file())
    rss_peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    benchmark = {
        "mode": "pilot" if args.pilot_fields else "county",
        "fields": int(len(fields)),
        "dates_requested": len(jobs),
        "dates_processed": int(result["date"].nunique()) if not result.empty else 0,
        "rows": int(len(result)),
        "errors": errors,
        "wall_seconds": round(time.time() - t0, 1),
        "rx_bytes": rx,
        "rx_mb": round(rx / 2**20, 1),
        "disk_delta_mb": round((disk_end - disk_start) / 2**20, 1),
        "disk_peak_mb": round(disk_end / 2**20, 1),
        "rss_peak_mb": round(rss_peak_mb, 1),
        "field_raster": meta,
        "workers": args.workers,
        "years": years,
    }
    (out_dir / "benchmark.json").write_text(json.dumps(benchmark, indent=2) + "\n")
    with (out_dir / "benchmark_history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(benchmark) + "\n")
    print(json.dumps(benchmark, indent=2))
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("ndvi", help="Sentinel-2 field-level NDVI statistics")
    parser.add_argument("--grower", default="in-johnson")
    parser.add_argument("--farm", default="countywide")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--crop-only", action="store_true", default=True)
    parser.add_argument("--all-fields", dest="crop_only", action="store_false")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--pilot-fields", type=int, default=0)
    parser.add_argument("--max-dates", type=int, default=0,
                        help="Process only the first N tile-dates (smoke test).")
    parser.set_defaults(func=run)
