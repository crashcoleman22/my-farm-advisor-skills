"""Remote data sources: TIGER county geometry, FIBOA CSB parquet, CDL legend."""

from __future__ import annotations

import io
import json
import datetime as dt
from typing import Iterator

import fsspec
import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
from shapely import wkb

FIBOA_URL = "https://data.source.coop/fiboa/us-usda-cropland/us_usda_cropland.parquet"
TIGERWEB_COUNTY_QUERY = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/"
    "MapServer/1/query"
)

# Physical leaf indices of the flattened bbox struct in the FIBOA parquet.
BBOX_LEAF = {"xmin": 7, "ymin": 8, "xmax": 9, "ymax": 10}

# ---- Crop classification -------------------------------------------------
# CDL crop categories (1-60, 66-80, 204-254) are "agricultural crop fields".
CROP_RANGES = ((1, 60), (66, 80), (204, 254))
AG_NON_CROP_CODES = {61, 62, 176}  # fallow/idle, pasture/grass, grassland pasture
NON_AG_CODES = {63, 64, 65, 81, 82, 83, 87, 88, 92, *range(111, 196)}

# Small legend for reporting; FIBOA carries its own crop:name.
CDL_NAMES = {
    1: "Corn", 2: "Cotton", 3: "Rice", 4: "Sorghum", 5: "Soybeans",
    6: "Sunflower", 10: "Peanuts", 11: "Tobacco", 12: "Sweet Corn",
    13: "Pop or Orn Corn", 21: "Barley", 22: "Durum Wheat", 23: "Spring Wheat",
    24: "Winter Wheat", 25: "Other Small Grains", 26: "Dbl Crop WinWht/Soybeans",
    27: "Rye", 28: "Oats", 29: "Millet", 31: "Canola", 32: "Flaxseed",
    36: "Alfalfa", 37: "Other Hay/Non Alfalfa", 41: "Sugarbeets",
    42: "Dry Beans", 43: "Potatoes", 44: "Other Crops", 45: "Sugarcane",
    47: "Misc Vegs & Fruits", 51: "Chick Peas", 52: "Lentils", 53: "Peas",
    58: "Clover/Wildflowers", 59: "Sod/Grass Seed", 60: "Switchgrass",
    61: "Fallow/Idle Cropland", 62: "Pasture/Grass", 63: "Forest",
    64: "Shrubland", 65: "Barren", 81: "Clouds/No Data", 82: "Developed",
    83: "Water", 87: "Wetlands", 88: "Nonag/Undefined", 92: "Aquaculture",
    111: "Open Water", 121: "Developed/Open Space", 122: "Developed/Low Intensity",
    123: "Developed/Med Intensity", 124: "Developed/High Intensity",
    131: "Barren", 141: "Deciduous Forest", 142: "Evergreen Forest",
    143: "Mixed Forest", 152: "Shrubland", 176: "Grassland/Pasture",
    190: "Woody Wetlands", 195: "Herbaceous Wetlands",
}


def crop_name(code: int) -> str:
    return CDL_NAMES.get(int(code), f"CDL {code}")


def ag_class(code: int | None) -> str:
    """Classify a CDL code into crop / ag_non_crop / non_ag / unknown."""
    if code is None or pd.isna(code):
        return "unknown"
    code = int(code)
    if any(lo <= code <= hi for lo, hi in CROP_RANGES):
        return "crop"
    if code in AG_NON_CROP_CODES:
        return "ag_non_crop"
    if code in NON_AG_CODES:
        return "non_ag"
    return "unknown"


# ---- TIGER -----------------------------------------------------------------
def fetch_county_boundary(state_fips: str, county_fips: str) -> gpd.GeoDataFrame:
    """Resolve one county polygon from the TIGERweb Census service (EPSG:4326)."""
    params = {
        "where": f"STATE='{state_fips.zfill(2)}' AND COUNTY='{county_fips.zfill(3)}'",
        "outFields": "NAME,BASENAME,STATE,COUNTY",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    import requests

    response = requests.get(TIGERWEB_COUNTY_QUERY, params=params, timeout=90)
    response.raise_for_status()
    payload = response.json()
    features = payload.get("features") or []
    if len(features) != 1:
        raise RuntimeError(
            f"Expected exactly one TIGER county for {state_fips}/{county_fips}, "
            f"got {len(features)}"
        )
    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    gdf["state_fips"] = state_fips.zfill(2)
    gdf["county_fips"] = county_fips.zfill(3)
    return gdf


# ---- FIBOA -----------------------------------------------------------------
def open_fiboa(url: str = FIBOA_URL) -> pq.ParquetFile:
    return pq.ParquetFile(fsspec.open(url, block_size=2**26).open())


def file_crs(pf: pq.ParquetFile) -> CRS:
    kv = {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in (pf.metadata.metadata or {}).items()
    }
    return CRS.from_json_dict(json.loads(kv["geo"])["columns"]["geometry"]["crs"])


def select_row_groups(
    pf: pq.ParquetFile, bbox_5070: tuple[float, float, float, float]
) -> list[int]:
    """Row groups whose bbox statistics overlap ``bbox_5070`` (xmin, ymin, xmax, ymax)."""
    xmin, ymin, xmax, ymax = bbox_5070
    selected: list[int] = []
    for i in range(pf.metadata.num_row_groups):
        group = pf.metadata.row_group(i)
        stats = {k: group.column(idx) for k, idx in BBOX_LEAF.items()}
        if any(not (c.statistics and c.statistics.has_min_max) for c in stats.values()):
            selected.append(i)
            continue
        if (
            stats["xmax"].statistics.max > xmin
            and stats["xmin"].statistics.min < xmax
            and stats["ymax"].statistics.max > ymin
            and stats["ymin"].statistics.min < ymax
        ):
            selected.append(i)
    return selected


def iter_fiboa_rows(
    pf: pq.ParquetFile, row_groups: list[int], columns: list[str] | None = None
) -> Iterator[dict]:
    """Yield row-group dictionaries, one row group at a time (bounded memory)."""
    columns = columns or [
        "id",
        "crop:code",
        "crop:name",
        "administrative_area_level_2",
        "determination_datetime",
        "crop:code_list",
        "geometry",
    ]
    for index in row_groups:
        yield pf.read_row_group(index, columns=columns).to_pydict()


def load_wkb(raw: bytes):
    return wkb.loads(raw)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
