"""Per-pixel Sentinel-2 NDVI composites for the explorer map.

The discovery pipeline streams COGs and keeps only per-field statistics. This
module keeps the full 10 m grid for one biweekly window at a time, caches it as
a compressed GeoTIFF, and prepares a grayscale+alpha PNG the browser can recolor
without losing the real pixel values.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.windows import from_bounds

NDVI_MIN = -0.2
NDVI_MAX = 1.0
SCALE = 10000.0
NODATA = -32768


# ----------------------------------------------------------------------
# Grid helpers
# ----------------------------------------------------------------------
def grid_from(path: Path) -> dict:
    """Read the shared 10 m field-index grid so rasters align with boundaries."""
    with rasterio.open(path) as src:
        return {
            "width": src.width,
            "height": src.height,
            "crs": src.crs,
            "transform": src.transform,
            "bounds": tuple(src.bounds),
            "shape": (src.height, src.width),
        }


def _read_aligned(url: str, grid: dict, resampling) -> np.ndarray:
    """Read one COG band over the grid bounds, reprojecting only if needed."""
    with rasterio.Env(GDAL_ENV), rasterio.open(url) as src:
        window = from_bounds(*grid["bounds"], transform=src.transform)
        window = window.round_offsets().round_lengths()
        data = src.read(1, window=window)
        transform = src.window_transform(window)
        crs = src.crs

    if data.shape == grid["shape"] and crs == grid["crs"] and transform == grid["transform"]:
        return data

    out = np.zeros(grid["shape"], dtype=data.dtype)
    reproject(
        source=data, destination=out,
        src_transform=transform, src_crs=crs,
        dst_transform=grid["transform"], dst_crs=grid["crs"],
        resampling=resampling,
    )
    return out


# GDAL settings shared with the discovery pipeline (COG range reads, retries).
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "536870912",
}


# ----------------------------------------------------------------------
# Composite
# ----------------------------------------------------------------------
def build_composite(items: list[dict], grid: dict, scl_mask: set[int],
                    progress=None) -> tuple[np.ndarray, list[str]]:
    """Mean of clear observations per 10 m pixel over the biweekly window."""
    shape = grid["shape"]
    acc = np.zeros(shape, dtype=np.float32)
    count = np.zeros(shape, dtype=np.uint16)
    days: list[str] = []

    for index, item in enumerate(items, start=1):
        date = item["properties"]["datetime"][:10]
        if progress:
            progress(f"reading {date}", index, len(items))

        red = _read_aligned(item["assets"]["red"]["href"], grid, Resampling.bilinear)
        nir = _read_aligned(item["assets"]["nir"]["href"], grid, Resampling.bilinear)
        scl = _read_aligned(item["assets"]["scl"]["href"], grid, Resampling.nearest)

        red_f = red.astype(np.float32) / 10000.0
        nir_f = nir.astype(np.float32) / 10000.0
        denom = nir_f + red_f
        with np.errstate(divide="ignore", invalid="ignore"):
            ndvi = (nir_f - red_f) / denom

        valid = ((red > 0) & (nir > 0) & (denom > 0) & np.isfinite(ndvi)
                 & ~np.isin(scl, list(scl_mask)))
        acc[valid] += ndvi[valid]
        count[valid] += 1
        days.append(date)

    out = np.full(shape, np.nan, dtype=np.float32)
    seen = count > 0
    out[seen] = acc[seen] / count[seen]
    return out, days


def raster_stats(ndvi: np.ndarray) -> dict:
    values = ndvi[np.isfinite(ndvi)]
    if values.size == 0:
        return {"pixels": 0}
    p02, p98 = np.percentile(values, [2, 98])
    return {
        "pixels": int(values.size),
        "min": round(float(values.min()), 4),
        "max": round(float(values.max()), 4),
        "mean": round(float(values.mean()), 4),
        "std": round(float(values.std()), 4),
        "p02": round(float(p02), 4),
        "p98": round(float(p98), 4),
    }


def write_geotiff(path: Path, ndvi: np.ndarray, grid: dict) -> None:
    scaled = np.full(ndvi.shape, NODATA, dtype=np.int16)
    ok = np.isfinite(ndvi)
    scaled[ok] = np.clip(np.round(ndvi[ok] * SCALE), -32000, 32000).astype(np.int16)
    profile = {
        "driver": "GTiff", "height": grid["height"], "width": grid["width"],
        "count": 1, "dtype": "int16", "crs": grid["crs"].to_string(),
        "transform": grid["transform"], "nodata": NODATA,
        "compress": "deflate", "predictor": 2, "tiled": True,
        "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{id(ndvi)}.tmp.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(scaled, 1)
    tmp.replace(path)


def read_geotiff(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        scaled = src.read(1)
        nodata = src.nodata if src.nodata is not None else NODATA
    return np.where(scaled == nodata, np.nan, scaled.astype(np.float32) / SCALE)


def field_crop_3857(tif_path: Path, geometry_3857, latitude: float) -> dict | None:
    """Crop one field out of the cached UTM composite onto a Web Mercator grid.

    The composite lives on an EPSG:32616 grid, which is rotated relative to Web
    Mercator. Warping the crop to EPSG:3857 before handing it to Leaflet removes
    that rotation (the previous countywide image overlay was off by up to ~500 m
    and a UTM crop by up to ~30 m).

    Resolution is ``10 / cos(latitude)`` so the ground sample distance stays at
    the native 10 m. Nearest resampling is deliberate: no interpolated NDVI.
    Pixels outside the field polygon are masked to NaN and stay transparent.
    """
    import math

    from rasterio.features import geometry_mask
    from rasterio.transform import from_origin

    geometry_3857 = geometry_3857.buffer(0)
    minx, miny, maxx, maxy = geometry_3857.bounds
    if not all(np.isfinite([minx, miny, maxx, maxy])) or maxx <= minx or maxy <= miny:
        return None

    resolution = 10.0 / max(math.cos(math.radians(latitude)), 1e-6)
    width = max(1, int(math.ceil((maxx - minx) / resolution)))
    height = max(1, int(math.ceil((maxy - miny) / resolution)))
    if width * height > 8_000_000:  # refuse pathological allocations
        return None

    transform = from_origin(minx, maxy, resolution, resolution)
    destination = np.full((height, width), np.nan, dtype=np.float32)

    with rasterio.open(tif_path) as src:
        band = src.read(1)
        nodata = src.nodata if src.nodata is not None else NODATA
        source = np.where(band == nodata, np.nan, band.astype(np.float32) / SCALE)
        reproject(
            source=source, destination=destination,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs="EPSG:3857",
            resampling=Resampling.nearest,
            src_nodata=np.nan, dst_nodata=np.nan,
        )

    inside = geometry_mask([geometry_3857], out_shape=(height, width),
                           transform=transform, invert=True)
    destination[~inside] = np.nan
    valid = destination[np.isfinite(destination)]
    if valid.size == 0:
        return None

    return {
        "ndvi": destination,
        "transform": transform,
        "bounds": (minx, miny, maxx, maxy),
        "resolution_m": resolution,
        "width": width,
        "height": height,
        "valid_pixels": int(valid.size),
    }


# ----------------------------------------------------------------------
# PNG encode / serve side
# ----------------------------------------------------------------------
def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def write_gray_alpha_png(path: Path, ndvi: np.ndarray) -> None:
    """Encode NDVI as 8-bit gray over [-0.2, 1.0]; nodata is transparent.

    The browser needs the raw ordinal, not a baked palette, so the color ramp and
    stretch can change without refetching the raster.
    """
    valid = np.isfinite(ndvi)
    gray = np.zeros(ndvi.shape, dtype=np.uint8)
    scaled = (ndvi[valid] - NDVI_MIN) / (NDVI_MAX - NDVI_MIN) * 255.0
    gray[valid] = np.clip(np.round(scaled), 0, 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)

    height, width = ndvi.shape
    interleaved = np.empty((height, width * 2), dtype=np.uint8)
    interleaved[:, 0::2] = gray
    interleaved[:, 1::2] = alpha
    raw = np.empty((height, width * 2 + 1), dtype=np.uint8)
    raw[:, 0] = 0  # filter type none
    raw[:, 1:] = interleaved

    header = struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", header)
               + _chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
               + _chunk(b"IEND", b""))
    tmp = path.with_name(f"{path.stem}.{id(ndvi)}.tmp.png")
    tmp.write_bytes(payload)
    tmp.replace(path)


# ----------------------------------------------------------------------
# Field-level raster statistics
# ----------------------------------------------------------------------
def field_stats(tif_path: Path, geometry) -> dict | None:
    """Pixel statistics for one field polygon from the cached 10 m raster."""
    from rasterio.mask import mask as raster_mask

    try:
        with rasterio.open(tif_path) as src:
            data, _ = raster_mask(src, [geometry], crop=True, filled=False)
    except ValueError:
        return None
    values = data[0].compressed()
    if values.size == 0:
        return None
    ndvi = values.astype(np.float32) / SCALE
    p02, p98 = np.percentile(ndvi, [2, 98])
    return {
        "pixels": int(values.size),
        "min": round(float(ndvi.min()), 4),
        "max": round(float(ndvi.max()), 4),
        "mean": round(float(ndvi.mean()), 4),
        "std": round(float(ndvi.std()), 4),
        "p02": round(float(p02), 4),
        "p98": round(float(p98), 4),
    }


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())
