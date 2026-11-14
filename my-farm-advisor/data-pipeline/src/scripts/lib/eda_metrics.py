"""Reusable metric helpers for field-level exploratory data analysis.

The functions here are intentionally free of runtime globals: callers pass
GeoDataFrames or DataFrames in and get plain tables back. That keeps the
field-level EDA workflow reusable across growers, farms and countries while
the command-line orchestrator stays in ``scripts/eda/eda_field_level.py``.
"""

from __future__ import annotations

import math
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy import stats

# Corn and soybean growing-season conventions used by the My Farm Advisor
# weather reports: GDD base 10 C, upper cutoff 30 C, season April-October.
GDD_BASE_C = 10.0
GDD_CAP_C = 30.0
SEASON_START_MONTH = 4
SEASON_END_MONTH = 10
HEAT_STRESS_C = 35.0
DRY_SPELL_MM = 1.0

CORN = "Corn"
SOYBEANS = "Soybeans"
OTHER = "Other"

_CROP_GROUP_MAP = {
    "corn": CORN,
    "soybeans": SOYBEANS,
    "soybean": SOYBEANS,
    "winter wheat": OTHER,
    "spring wheat": OTHER,
    "wheat": OTHER,
    "alfalfa": OTHER,
    "grass/pasture": OTHER,
    "grassland": OTHER,
    "fallow/idle": OTHER,
    "forest": OTHER,
}


# ---------------------------------------------------------------------------
# Field boundaries
# ---------------------------------------------------------------------------


def _exterior_vertex_count(geometry) -> int:
    if geometry is None or geometry.is_empty:
        return 0
    if geometry.geom_type == "Polygon":
        return max(len(geometry.exterior.coords) - 1, 0)
    if geometry.geom_type == "MultiPolygon":
        return sum(max(len(part.exterior.coords) - 1, 0) for part in geometry.geoms)
    return 0


def _rect_orientation_deg(rectangle) -> float:
    coords = list(rectangle.exterior.coords)
    edges: list[tuple[float, float, float]] = []
    for start, end in zip(coords[:-1], coords[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        edges.append((math.hypot(dx, dy), dx, dy))
    if not edges:
        return float("nan")
    _length, dx, dy = max(edges, key=lambda item: item[0])
    return float(math.degrees(math.atan2(dy, dx)) % 180.0)


def boundary_metrics(
    fields: gpd.GeoDataFrame,
    *,
    equal_area_crs: str = "EPSG:5070",
    label_columns: Iterable[str] = ("field_id", "grower", "farm", "state", "county_name"),
) -> pd.DataFrame:
    """Return one row per field with area, shape and orientation metrics.

    ``area_acres`` is recomputed in an equal-area CRS so the result is
    independent of the boundary file's storage CRS. Polsby-Popper
    compactness is ``4*pi*A/P**2`` (1.0 is a circle, ~0.785 is a circle
    inscribed in its bounding square). ``rect_fill`` is polygon area divided
    by its minimum rotated rectangle area (1.0 for a rectangle, ~0.785 for a
    circle).
    """
    if fields.empty:
        return pd.DataFrame()

    source = fields.copy()
    if "field_id" not in source.columns:
        source["field_id"] = [f"field-{index}" for index in range(len(source))]

    projected = source.to_crs(equal_area_crs)
    area_m2 = projected.geometry.area.astype(float)
    perimeter_m = projected.geometry.length.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        compactness = 4.0 * math.pi * area_m2 / (perimeter_m**2)
    rectangles = projected.geometry.apply(lambda geometry: geometry.minimum_rotated_rectangle)
    rect_area = rectangles.area.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rect_fill = area_m2 / rect_area

    aspects: list[float] = []
    orientations: list[float] = []
    for rectangle in rectangles:
        coords = list(rectangle.exterior.coords)
        side_lengths = [
            math.hypot(end[0] - start[0], end[1] - start[1])
            for start, end in zip(coords[:-1], coords[1:])
        ]
        ordered = sorted(side_lengths)
        short_side = ordered[1] if len(ordered) > 1 else ordered[0]
        long_side = ordered[-1]
        aspects.append(float(long_side / short_side) if short_side else float("nan"))
        orientations.append(_rect_orientation_deg(rectangle))

    centroids_wgs84 = gpd.GeoSeries(
        projected.geometry.centroid, crs=equal_area_crs
    ).to_crs("EPSG:4326")
    metrics = pd.DataFrame(
        {
            "field_id": source["field_id"].astype(str).to_numpy(),
            "area_acres": (area_m2 / 4046.8564224).to_numpy(),
            "area_ha": (area_m2 / 10000.0).to_numpy(),
            "perimeter_m": perimeter_m.to_numpy(),
            "compactness_pp": compactness.to_numpy(),
            "rect_fill": rect_fill.to_numpy(),
            "aspect_ratio": np.asarray(aspects, dtype=float),
            "orientation_deg": np.asarray(orientations, dtype=float),
            "vertex_count": [_exterior_vertex_count(geometry) for geometry in source.geometry],
            "convexity": np.asarray(
                [
                    (geometry.area / geometry.convex_hull.area)
                    if geometry is not None and not geometry.is_empty and geometry.convex_hull.area
                    else float("nan")
                    for geometry in projected.geometry
                ],
                dtype=float,
            ),
            "centroid_lon": centroids_wgs84.x.to_numpy(),
            "centroid_lat": centroids_wgs84.y.to_numpy(),
        }
    )

    for column in label_columns:
        if column in source.columns and column != "field_id":
            metrics[column] = source[column].astype(str).to_numpy()

    metrics["shape_class"] = [
        classify_shape(compactness_value, rect_value)
        for compactness_value, rect_value in zip(metrics["compactness_pp"], metrics["rect_fill"])
    ]
    return metrics.sort_values(["state", "field_id"] if "state" in metrics else ["field_id"]).reset_index(
        drop=True
    )


def classify_shape(compactness_pp: float, rect_fill: float) -> str:
    """Classify a field outline as pivot-like, rectangular or irregular.

    A pivot is a circle: Polsby-Popper compactness near 1.0 and a
    minimum-rotated-rectangle fill near pi/4 (~0.785). A rectangular field
    fills its rotated rectangle (>0.90) and is reasonably compact.
    """
    if not np.isfinite(compactness_pp) or not np.isfinite(rect_fill):
        return "unknown"
    if compactness_pp >= 0.90 and 0.68 <= rect_fill <= 0.87:
        return "pivot-like"
    if rect_fill >= 0.90:
        return "rectangular"
    return "irregular"


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def daily_gdd(
    tmax_c: pd.Series, tmin_c: pd.Series, *, base_c: float = GDD_BASE_C, cap_c: float = GDD_CAP_C
) -> pd.Series:
    """Corn-style daily GDD with a 30 C upper cutoff and 10 C base."""
    tmax = pd.to_numeric(tmax_c, errors="coerce").clip(upper=cap_c)
    tmin = pd.to_numeric(tmin_c, errors="coerce").clip(upper=cap_c)
    return (((tmax + tmin) / 2.0) - base_c).clip(lower=0.0)


def add_weather_calendar(daily: pd.DataFrame, date_column: str = "date") -> pd.DataFrame:
    frame = daily.copy()
    frame[date_column] = pd.to_datetime(frame[date_column])
    frame["year"] = frame[date_column].dt.year
    frame["doy"] = frame[date_column].dt.day_of_year
    frame["month"] = frame[date_column].dt.month
    return frame


def growing_season(
    daily: pd.DataFrame,
    *,
    start_month: int = SEASON_START_MONTH,
    end_month: int = SEASON_END_MONTH,
) -> pd.DataFrame:
    frame = add_weather_calendar(daily) if "doy" not in daily.columns else daily.copy()
    return frame[frame["month"].between(start_month, end_month)].copy()


def _frost_dates_for_year(year_frame: pd.DataFrame) -> tuple[float, float]:
    frozen = year_frame[year_frame["T2M_MIN"] <= 0.0]
    if frozen.empty:
        return float("nan"), float("nan")
    spring = frozen[frozen["doy"] <= 182]
    autumn = frozen[frozen["doy"] > 182]
    last_spring = float(spring["doy"].max()) if not spring.empty else float("nan")
    first_fall = float(autumn["doy"].min()) if not autumn.empty else float("nan")
    return last_spring, first_fall


def _longest_dry_spell(frame: pd.DataFrame, threshold_mm: float = DRY_SPELL_MM) -> int:
    longest = 0
    current = 0
    for value in frame["PRECTOTCORR"].fillna(0.0).to_numpy():
        if value < threshold_mm:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return int(longest)


def weather_season_metrics(
    daily: pd.DataFrame,
    *,
    field_column: str = "field_id",
    start_month: int = SEASON_START_MONTH,
    end_month: int = SEASON_END_MONTH,
) -> pd.DataFrame:
    """Summarize one growing season per field-year.

    Returns one row per field-year with growing-season precipitation, GDD,
    heat-stress days, frost-free window and longest Jun-Aug dry spell.
    """
    if daily.empty:
        return pd.DataFrame()

    frame = add_weather_calendar(daily)
    frame["GDD"] = daily_gdd(frame["T2M_MAX"], frame["T2M_MIN"])
    season = frame[frame["month"].between(start_month, end_month)].copy()
    summer = season[season["month"].between(6, 8)].copy()

    season_summary = (
        season.groupby([field_column, "year"], dropna=False)
        .agg(
            gs_days=("date", "nunique"),
            gs_precip_mm=("PRECTOTCORR", "sum"),
            gs_gdd=("GDD", "sum"),
            gs_mean_temp_c=("T2M", "mean"),
            gs_mean_solar=("ALLSKY_SFC_SW_DWN", "mean"),
            gs_mean_rh=("RH2M", "mean"),
            heat_days_35c=("T2M_MAX", lambda values: int((values >= HEAT_STRESS_C).sum())),
        )
        .reset_index()
    )
    dry_spells = (
        summer.groupby([field_column, "year"], dropna=False)
        .apply(lambda group: _longest_dry_spell(group), include_groups=False)
        .rename("max_dry_spell_days")
        .reset_index()
    )
    frost_rows: list[dict[str, object]] = []
    for (field_id, year), group in frame.groupby([field_column, "year"], dropna=False):
        last_spring, first_fall = _frost_dates_for_year(group)
        frost_rows.append(
            {
                field_column: field_id,
                "year": year,
                "last_spring_frost_doy": last_spring,
                "first_fall_frost_doy": first_fall,
                "frost_free_days": (
                    first_fall - last_spring
                    if np.isfinite(last_spring) and np.isfinite(first_fall)
                    else float("nan")
                ),
            }
        )
    frost = pd.DataFrame(frost_rows)

    summary = season_summary.merge(dry_spells, on=[field_column, "year"], how="left")
    summary = summary.merge(frost, on=[field_column, "year"], how="left")
    return summary.sort_values([field_column, "year"]).reset_index(drop=True)


def cumulative_gdd_curve(
    daily: pd.DataFrame,
    *,
    field_column: str = "field_id",
    start_month: int = SEASON_START_MONTH,
    end_month: int = SEASON_END_MONTH,
) -> pd.DataFrame:
    """Cumulative GDD by day-of-year for each field-year."""
    frame = add_weather_calendar(daily)
    frame["GDD"] = daily_gdd(frame["T2M_MAX"], frame["T2M_MIN"])
    season = frame[frame["month"].between(start_month, end_month)].copy()
    season = season.sort_values([field_column, "year", "doy"])
    season["cumulative_gdd"] = season.groupby([field_column, "year"])["GDD"].cumsum()
    return season[[field_column, "year", "doy", "date", "cumulative_gdd"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# CDL / crop history
# ---------------------------------------------------------------------------


def crop_group(crop_name: object) -> str:
    name = str(crop_name or "").strip().lower()
    if name in _CROP_GROUP_MAP:
        return _CROP_GROUP_MAP[name]
    if "corn" in name:
        return CORN
    if "soy" in name:
        return SOYBEANS
    return OTHER


def crop_history_metrics(composition: pd.DataFrame) -> pd.DataFrame:
    """Collapse CDL composition to one dominant-crop row per field-year."""
    if composition.empty:
        return pd.DataFrame()
    frame = composition.copy()
    frame["crop_group"] = frame["crop_name"].map(crop_group)
    grouped = (
        frame.groupby(["field_id", "year", "crop_group"], dropna=False)["pct"]
        .sum()
        .reset_index()
    )
    wide = (
        grouped.pivot_table(
            index=["field_id", "year"], columns="crop_group", values="pct", aggfunc="sum"
        )
        .fillna(0.0)
        .reset_index()
    )
    for group in (CORN, SOYBEANS, OTHER):
        if group not in wide.columns:
            wide[group] = 0.0
    wide = wide.rename(
        columns={CORN: "corn_pct", SOYBEANS: "soy_pct", OTHER: "other_pct"}
    )
    wide["total_pct"] = wide[["corn_pct", "soy_pct", "other_pct"]].sum(axis=1)
    wide["dominant_crop"] = wide[["corn_pct", "soy_pct", "other_pct"]].idxmax(axis=1).map(
        {"corn_pct": CORN, "soy_pct": SOYBEANS, "other_pct": OTHER}
    )
    wide["dominant_pct"] = wide[["corn_pct", "soy_pct", "other_pct"]].max(axis=1)
    wide["mixed_pixels"] = wide["dominant_pct"] < 50.0
    return wide.sort_values(["field_id", "year"]).reset_index(drop=True)


def shannon_diversity(shares: Iterable[float]) -> float:
    values = np.asarray([value for value in shares if value and value > 0], dtype=float)
    if values.size == 0:
        return float("nan")
    total = values.sum()
    if total <= 0:
        return float("nan")
    probabilities = values / total
    return float(-np.sum(probabilities * np.log(probabilities)))


def rotation_summary(history: pd.DataFrame) -> pd.DataFrame:
    """Per-field rotation sequence, crop counts and diversity."""
    if history.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for field_id, group in history.groupby("field_id", dropna=False):
        ordered = group.sort_values("year")
        sequence = "".join(
            {CORN: "C", SOYBEANS: "S"}.get(str(crop), "O")
            for crop in ordered["dominant_crop"]
        )
        mean_shares = [
            float(ordered["corn_pct"].mean()),
            float(ordered["soy_pct"].mean()),
            float(ordered["other_pct"].mean()),
        ]
        changes = int((ordered["dominant_crop"].to_numpy()[1:] != ordered["dominant_crop"].to_numpy()[:-1]).sum()) if len(ordered) > 1 else 0
        rows.append(
            {
                "field_id": field_id,
                "years": ",".join(str(int(year)) for year in ordered["year"]),
                "rotation_sequence": sequence,
                "n_years": int(len(ordered)),
                "n_distinct_crops": int(ordered["dominant_crop"].nunique()),
                "crop_changes": changes,
                "corn_years": int((ordered["dominant_crop"] == CORN).sum()),
                "soy_years": int((ordered["dominant_crop"] == SOYBEANS).sum()),
                "other_years": int((ordered["dominant_crop"] == OTHER).sum()),
                "mean_corn_pct": mean_shares[0],
                "mean_soy_pct": mean_shares[1],
                "mean_other_pct": mean_shares[2],
                "rotation_diversity": shannon_diversity(mean_shares),
                "continuous_corn": bool((ordered["dominant_crop"] == CORN).all()),
                "mixed_year_count": int(ordered["mixed_pixels"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("field_id").reset_index(drop=True)


def crop_transitions(history: pd.DataFrame) -> pd.DataFrame:
    """Return one row per field for each year-to-year dominant-crop transition.

    The long table keeps ``field_id`` and year pairs so callers can group by
    state, farm or rotation length without re-deriving the sequence.
    """
    if history.empty:
        return pd.DataFrame()
    records: list[dict[str, object]] = []
    for field_id, group in history.groupby("field_id", dropna=False):
        ordered = group.sort_values("year")
        crops = ordered["dominant_crop"].tolist()
        years = ordered["year"].tolist()
        for index in range(len(crops) - 1):
            records.append(
                {
                    "field_id": field_id,
                    "from_year": int(years[index]),
                    "to_year": int(years[index + 1]),
                    "from_crop": crops[index],
                    "to_crop": crops[index + 1],
                }
            )
    return pd.DataFrame(records)


def crop_transition_counts(transitions: pd.DataFrame, group_columns: Iterable[str] = ()) -> pd.DataFrame:
    """Aggregate long transitions into counts and row-normalised probabilities."""
    if transitions.empty:
        return pd.DataFrame()
    keys = [column for column in group_columns if column in transitions.columns]
    all_keys = keys + ["from_crop", "to_crop"]
    counts = transitions.groupby(all_keys).size().rename("count").reset_index()
    denominator = counts.groupby(keys + ["from_crop"])["count"].transform("sum")
    counts["probability"] = counts["count"] / denominator
    return counts


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def spearman_with_permutation(
    x: Iterable[float],
    y: Iterable[float],
    *,
    permutations: int = 10000,
    seed: int = 42,
) -> dict[str, float]:
    """Spearman rho with a permutation p-value and Fisher-z confidence bounds."""
    frame = pd.DataFrame({"x": list(x), "y": list(y)}).dropna()
    if len(frame) < 3 or frame["x"].nunique() < 2 or frame["y"].nunique() < 2:
        return {
            "n": float(len(frame)),
            "rho": float("nan"),
            "p_permutation": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
        }
    rho, _p_value = stats.spearmanr(frame["x"], frame["y"])
    rng = np.random.default_rng(seed)
    y_values = frame["y"].to_numpy()
    exceed = 0
    for _ in range(permutations):
        shuffled = rng.permutation(y_values)
        permuted, _ = stats.spearmanr(frame["x"], shuffled)
        if abs(permuted) >= abs(rho):
            exceed += 1
    p_permutation = (exceed + 1) / (permutations + 1)
    if abs(rho) < 1.0 and len(frame) > 3:
        z = np.arctanh(rho)
        standard_error = 1.0 / math.sqrt(len(frame) - 3)
        ci_low = math.tanh(z - 1.96 * standard_error)
        ci_high = math.tanh(z + 1.96 * standard_error)
    else:
        ci_low = ci_high = float("nan")
    return {
        "n": float(len(frame)),
        "rho": float(rho),
        "p_permutation": float(p_permutation),
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
    }


def kruskal_by_group(frame: pd.DataFrame, value_column: str, group_column: str) -> dict[str, float]:
    groups = [
        group[value_column].dropna().to_numpy()
        for _name, group in frame.groupby(group_column, dropna=False)
        if group[value_column].notna().sum() >= 2
    ]
    if len(groups) < 2:
        return {"statistic": float("nan"), "p_value": float("nan"), "groups": float(len(groups))}
    statistic, p_value = stats.kruskal(*groups)
    return {"statistic": float(statistic), "p_value": float(p_value), "groups": float(len(groups))}
